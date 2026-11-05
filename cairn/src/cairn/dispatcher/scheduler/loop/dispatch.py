"""DispatcherLoop mixin: Selecting workers and submitting Decide / Execute tasks."""

from __future__ import annotations

import logging
import time

from cairn.contracts.models import ProjectDetail, ProjectSummary, Step
from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.tasks.decide import run_decide_task
from cairn.dispatcher.tasks.execute import run_execute_task

from ._common import WorkerSelection

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class DispatchMixin:
    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        if len(self.futures) >= self.config.runtime.max_workers:
            self._log_changed(
                "dispatch/global",
                logging.INFO,
                "skip dispatch because max_workers reached running_tasks=%s",
                len(self.futures),
            )
            return
        active = [summary for summary in summaries if summary.status == "active"]
        if not active:
            self._log_changed("dispatch/global", logging.INFO, "skip dispatch because no active projects")
            return

        running_projects = self._ordered_projects(
            [summary for summary in active if summary.id in self.runtime_project_ids]
        )
        idle_projects = self._ordered_projects(
            [summary for summary in active if summary.id not in self.runtime_project_ids]
        )

        dispatched = True
        while dispatched and len(self.futures) < self.config.runtime.max_workers:
            dispatched = False
            for summary in running_projects:
                if self._try_dispatch_project(summary):
                    dispatched = True
                    if len(self.futures) >= self.config.runtime.max_workers:
                        return
            if dispatched:
                continue
            if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                self._log_changed(
                    "dispatch/idle-limit",
                    logging.INFO,
                    "skip idle project dispatch because max_running_projects reached running_projects=%s",
                    self._running_project_count(active),
                )
                return
            for summary in idle_projects:
                if self._running_project_count(active) >= self.config.runtime.max_running_projects:
                    self._log_changed(
                        "dispatch/idle-limit",
                        logging.INFO,
                        "stop idle project dispatch because max_running_projects reached running_projects=%s",
                        self._running_project_count(active),
                    )
                    return
                if self._try_dispatch_project(summary):
                    dispatched = True
                    break

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        if not summaries:
            return []
        ids = [summary.id for summary in summaries]
        ids.sort()
        offset = self.project_cursor % len(ids)
        ordered_ids = ids[offset:] + ids[:offset]
        by_id = {summary.id: summary for summary in summaries}
        self.project_cursor += 1
        return [by_id[project_id] for project_id in ordered_ids]

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        skip_scope = f"project:{summary.id}:skip"
        container_name = self.container_manager.container_name(summary.id)
        if container_name in self._cleanup_pending:
            self._log_changed(
                f"{skip_scope}:cleanup_pending",
                logging.DEBUG,
                "skip project=%s because container cleanup is still pending container=%s",
                summary.id,
                container_name,
            )
            return False
        if self._project_running_task_count(summary.id) >= self.config.runtime.max_project_workers:
            self._log_changed(
                f"{skip_scope}:max_project_workers",
                logging.INFO,
                "skip project=%s because max_project_workers reached running_tasks=%s",
                summary.id,
                self._project_running_task_summary(summary.id),
            )
            return False

        project = self.client.get_project(summary.id)
        if project.project.status != "active":
            self._log_changed(
                f"{skip_scope}:status",
                logging.INFO,
                "skip project=%s because status=%s",
                summary.id,
                project.project.status,
            )
            return False
        # Decide runs first and is serial per project: it evaluates the graph, then either
        # completes, adds/drops sub goals, or adds/reprioritizes/abandons steps.
        if project.project.decide is None:
            decide_trigger = self._decide_trigger(project)
            if decide_trigger is not None:
                export_yaml = self.client.export_project(summary.id)
                return self._dispatch_decide(project, export_yaml, decide_trigger)

        running_step_ids = self._project_running_steps(summary.id)
        pending_steps = [
            step
            for step in project.steps
            if step.kind == "action"
            and step.status == "pending"
            and step.worker is None
            and step.id not in running_step_ids
        ]
        if running_step_ids and not pending_steps:
            self._log_changed(
                f"{skip_scope}:execute_running",
                logging.DEBUG,
                "skip execute project=%s because all pending steps are already running locally steps=%s",
                summary.id,
                sorted(running_step_ids),
            )
        if pending_steps:
            next_step = max(
                pending_steps, key=lambda step: (step.priority, step.created_at, step.id)
            )
            export_yaml = self.client.export_project(summary.id)
            return self._dispatch_execute(project, export_yaml, next_step)
        if project.project.decide is not None:
            self._log_changed(
                f"{skip_scope}:decide_claimed",
                logging.DEBUG,
                "skip decide project=%s because decide is already claimed by %s",
                summary.id,
                project.project.decide.worker,
            )
            return False
        self._log_changed(
            f"{skip_scope}:graph_unchanged",
            logging.DEBUG,
            "skip decide project=%s because graph state unchanged facts=%s hints=%s findings=%s open_steps=%s",
            summary.id,
            len(project.facts),
            len(project.hints),
            len(project.findings),
            self._project_open_step_count(project),
        )
        return False

    def _dispatch_decide(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        selection = self._select_worker(project.project.id, "decide")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:decide",
                logging.INFO,
                "no worker available for decide project=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:decide")
        claim = self.client.claim_decide(project.project.id, worker.name, trigger)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "decide claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "decide claim failed project=%s worker=%s status=%s",
                project.project.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_decide_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                worker,
                cancellation := TaskCancellation(),
                summary_sink=self.request_summary,
            )
        except Exception:  # keep the loop alive when a submit fails
            LOG.exception("failed to submit decide task project=%s worker=%s", project.project.id, worker.name)
            self._best_effort_release_decide(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "decide",
            worker.name,
            cancellation,
            step_id=None,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            finding_count=len(project.findings),
            open_step_count=self._project_open_step_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched decide project=%s worker=%s trigger=%s", project.project.id, worker.name, trigger)
        return True

    def _dispatch_execute(self, project: ProjectDetail, export_yaml: str, step: Step) -> bool:
        selection = self._select_worker(project.project.id, "execute")
        worker = selection.worker
        if worker is None:
            self._log_changed(
                f"project:{project.project.id}:worker:execute",
                logging.INFO,
                "no worker available for execute project=%s step=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s",
                project.project.id,
                step.id,
                selection.blocked_busy,
                selection.blocked_unhealthy,
                selection.blocked_rejected,
            )
            return False
        self._clear_log_state(f"project:{project.project.id}:worker:execute")
        claim = self.client.heartbeat_step(project.project.id, step.id, worker.name)
        if claim.status_code in (403, 409):
            level = logging.INFO if claim.status_code == 403 else logging.WARNING
            LOG.log(
                level,
                "execute claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        if not claim.ok:
            LOG.warning(
                "execute claim failed project=%s step=%s worker=%s status=%s",
                project.project.id,
                step.id,
                worker.name,
                claim.status_code,
            )
            return False
        try:
            future = self.executor.submit(
                run_execute_task,
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                step,
                worker,
                cancellation := TaskCancellation(),
            )
        except Exception:  # keep the loop alive when a submit fails
            LOG.exception("failed to submit execute task project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
            self._best_effort_release(project.project.id, step.id, worker.name)
            return False
        self.futures[future] = RunningTask(project.project.id, "execute", worker.name, cancellation, step_id=step.id)
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info("dispatched execute project=%s step=%s worker=%s", project.project.id, step.id, worker.name)
        return True

    def _select_worker(self, project_id: str, task_type: str) -> WorkerSelection:
        now = time.time()
        candidates: list[WorkerConfig] = []
        blocked_busy: list[str] = []
        blocked_unhealthy: list[str] = []
        blocked_rejected: list[str] = []
        blocked_task_type: list[str] = []
        running_counts = self._worker_counts()
        for worker in self.config.workers:
            if task_type not in worker.task_types:
                blocked_task_type.append(worker.name)
                continue
            running = running_counts.get(worker.name, 0)
            if running >= worker.max_running:
                blocked_busy.append(f"{worker.name}({running}/{worker.max_running})")
                continue
            unhealthy_until = self.worker_unhealthy_until.get(worker.name, 0)
            if unhealthy_until > now:
                blocked_unhealthy.append(f"{worker.name}({unhealthy_until - now:.1f}s)")
                continue
            rejected_until = self.worker_rejected_until.get((project_id, task_type, worker.name), 0)
            if rejected_until > now:
                blocked_rejected.append(f"{worker.name}({rejected_until - now:.1f}s)")
                continue
            candidates.append(worker)
        if not candidates:
            LOG.debug(
                "worker selection project=%s task=%s no candidates blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s",
                project_id,
                task_type,
                blocked_busy,
                blocked_unhealthy,
                blocked_rejected,
                blocked_task_type,
            )
            return WorkerSelection(
                worker=None,
                blocked_busy=blocked_busy,
                blocked_unhealthy=blocked_unhealthy,
                blocked_rejected=blocked_rejected,
                blocked_task_type=blocked_task_type,
            )
        ordered = choose_worker(candidates, running_counts)
        LOG.debug(
            "worker selection project=%s task=%s candidates=%s blocked_busy=%s blocked_unhealthy=%s blocked_rejected=%s blocked_task_type=%s chosen=%s",
            project_id,
            task_type,
            [f"{worker.name}({running_counts.get(worker.name, 0)}/{worker.max_running},p{worker.priority})" for worker in candidates],
            blocked_busy,
            blocked_unhealthy,
            blocked_rejected,
            blocked_task_type,
            ordered[0].name if ordered else None,
        )
        return WorkerSelection(
            worker=ordered[0] if ordered else None,
            blocked_busy=blocked_busy,
            blocked_unhealthy=blocked_unhealthy,
            blocked_rejected=blocked_rejected,
            blocked_task_type=blocked_task_type,
        )

    def _worker_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for task in self.futures.values():
            counts[task.worker_name] = counts.get(task.worker_name, 0) + 1
        return counts

    def _project_running_task_count(self, project_id: str) -> int:
        return sum(1 for task in self.futures.values() if task.project_id == project_id)

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        summary: list[str] = []
        for task in self.futures.values():
            if task.project_id != project_id:
                continue
            if task.step_id is None:
                summary.append(f"{task.task_type}:{task.worker_name}")
            else:
                summary.append(f"{task.task_type}:{task.worker_name}:{task.step_id}")
        summary.sort()
        return summary

    def _project_running_steps(self, project_id: str) -> set[str]:
        return {
            task.step_id
            for task in self.futures.values()
            if task.project_id == project_id and task.task_type == "execute" and task.step_id is not None
        }

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        return len(self.runtime_project_ids & active_ids)

    def _project_open_step_count(self, project: ProjectDetail) -> int:
        return sum(
            1
            for step in project.steps
            if step.kind == "action" and step.status in ("pending", "running")
        )

    def _decide_trigger(self, project: ProjectDetail) -> str | None:
        """Decide is triggered by a new task, or by any change of the graph it already saw."""
        open_step_count = self._project_open_step_count(project)
        checkpoint = self.decide_checkpoints.get(project.project.id)
        if checkpoint is None:
            return "initial"
        changes: list[str] = []
        if len(project.facts) > checkpoint.fact_count:
            changes.append(f"facts:{checkpoint.fact_count}->{len(project.facts)}")
        if len(project.hints) > checkpoint.hint_count:
            changes.append(f"hints:{checkpoint.hint_count}->{len(project.hints)}")
        if len(project.findings) > checkpoint.finding_count:
            changes.append(f"findings:{checkpoint.finding_count}->{len(project.findings)}")
        if checkpoint.open_step_count > 0 and open_step_count == 0:
            changes.append(f"open_steps:{checkpoint.open_step_count}->0")
        if not changes:
            return None
        return ",".join(changes)
