from __future__ import annotations

import logging
import shutil
import subprocess
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import requests

from cairn.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from cairn.dispatcher.models import DecideCheckpoint, RunningTask
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.startup_healthcheck import format_failure_summary, run_startup_healthchecks
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.workers.registry import get_driver
from cairn.dispatcher.tasks.decide import run_decide_task
from cairn.dispatcher.tasks.execute import run_execute_task
from cairn.dispatcher.tasks.question import run_question_task
from cairn.server.models import ProjectDetail, ProjectSummary, Step

LOG = logging.getLogger(__name__)
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]


class DispatcherLoop:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.config = DispatchConfig.load(config_path)
        self.client = CairnClient(self.config.server)
        if self.config.runtime.execution == "local":
            self.container_manager = LocalBackend(self.config.local or LocalConfig())
        else:
            assert self.config.container is not None
            self.container_manager = ContainerManager(self.config.container)
        self.executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_workers)
        self.cleanup_executor = ThreadPoolExecutor(max_workers=max(1, min(8, self.config.runtime.max_workers)))
        # Side questions get their own pool: they never take a decide/execute slot.
        self.question_executor = ThreadPoolExecutor(max_workers=self.config.runtime.max_questions)
        self.futures: dict[Future[str], RunningTask] = {}
        self.cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]] = {}
        self.question_futures: dict[Future[str], str] = {}
        self.decide_checkpoints: dict[str, DecideCheckpoint] = {}
        self.runtime_project_ids: set[str] = set()
        self.worker_unhealthy_until: dict[str, float] = {}
        self.worker_rejected_until: dict[tuple[str, str, str], float] = {}
        self._log_state: dict[str, tuple[int, str, tuple[object, ...]]] = {}
        self._cleanup_pending: set[str] = set()
        self._inactive_cleanup_done: dict[str, str] = {}
        self.project_cursor = 0
        self._settings_checked = False
        self._startup_healthchecks_checked = False

    def close(self) -> None:
        if self.futures:
            LOG.info(
                "dispatcher shutting down waiting_for_tasks=%s running_projects=%s",
                len(self.futures),
                sorted({task.project_id for task in self.futures.values()}),
            )
        self.executor.shutdown(wait=True)
        self.cleanup_executor.shutdown(wait=True)
        self.question_executor.shutdown(wait=True)
        self.container_manager.close()
        self.client.close()

    def run(self, once: bool = False) -> None:
        try:
            self.run_startup_healthchecks()
            while True:
                try:
                    if not self._settings_checked:
                        self._validate_server_settings()
                        self._settings_checked = True
                    self._reap_futures()
                    self._reap_cleanup_futures()
                    summaries = self.client.list_projects()
                    self._initialize_decide_checkpoints(summaries)
                    self._refresh_runtime_projects(summaries)
                    self._cancel_inactive_tasks(summaries)
                    self._queue_container_cleanups(summaries)
                    self._dispatch_questions()
                    self._dispatch_available(summaries)
                except requests.RequestException as exc:
                    if once:
                        raise
                    LOG.warning(
                        "dispatcher server request failed error=%s retry_in=%ss",
                        exc,
                        self.config.runtime.interval,
                    )
                    time.sleep(self.config.runtime.interval)
                    continue
                if once:
                    break
                time.sleep(self.config.runtime.interval)
        finally:
            self.close()

    def run_startup_healthchecks_only(self) -> None:
        try:
            self.run_startup_healthchecks(show_commands=True, force=True)
        finally:
            self.close()

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        if self._startup_healthchecks_checked:
            return
        if self.config.runtime.execution == "local":
            self._run_local_binary_check()
            self._startup_healthchecks_checked = True
            return
        if not force and self.config.runtime.worker_healthcheck == "disabled":
            LOG.info("skip startup worker healthchecks because runtime.worker_healthcheck=disabled")
            self._startup_healthchecks_checked = True
            return
        self._run_startup_healthchecks(show_commands=show_commands)
        self._startup_healthchecks_checked = True

    def _run_local_binary_check(self) -> None:
        binaries: dict[str, list[str]] = {}
        for worker in self.config.workers:
            binary = get_driver(worker.type, "local").local_binary()
            if binary is None:
                continue
            binaries.setdefault(binary, []).append(worker.name)
        if not binaries:
            return

        LOG.info("[*] Local execution: checking %d worker CLI(s) on this host", len(binaries))
        available: list[str] = []
        missing: list[str] = []
        for binary in sorted(binaries):
            workers = ", ".join(sorted(binaries[binary]))
            path, runnable = self._probe_local_cli(binary)
            if path is None:
                missing.append(binary)
                LOG.error("[-] %-8s not found on PATH (workers: %s)", binary, workers)
            elif runnable:
                available.append(binary)
                LOG.info("[+] %-8s %s (workers: %s)", binary, path, workers)
            else:
                available.append(binary)
                LOG.warning("[!] %-8s %s found but `%s --help` failed (workers: %s)", binary, path, binary, workers)

        if not available:
            raise RuntimeError(
                "local execution: none of the configured worker CLIs are installed on PATH ("
                + ", ".join(sorted(binaries))
                + "). Install them and make sure each runs directly from your shell, then retry."
            )
        if missing:
            LOG.warning(
                "[!] Missing CLIs, their workers cannot run: %s. Install them or drop those workers.",
                ", ".join(sorted(missing)),
            )
        LOG.warning(
            "[!] Local mode uses each CLI's own host config: make sure %s already logged in / "
            "configured and usable directly (e.g. `claude -p ...` works) — Cairn injects no API keys.",
            ", ".join(sorted(available)),
        )

    @staticmethod
    def _probe_local_cli(binary: str) -> tuple[str | None, bool]:
        path = shutil.which(binary)
        if path is None:
            return None, False
        try:
            result = subprocess.run(
                [binary, "--help"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError):
            return path, False
        return path, result.returncode == 0

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
            )
        except Exception:
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
        except Exception:
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

    # --- side questions -----------------------------------------------------------

    def _dispatch_questions(self) -> None:
        """Answer pending side questions outside the decide/execute slots."""
        self._reap_question_futures()
        limit = self.config.runtime.max_questions
        free = limit - len(self.question_futures)
        if free <= 0:
            return
        pending = self.client.list_pending_questions(limit=free)
        if not pending:
            self._log_changed("questions/idle", logging.DEBUG, "no pending side questions")
            return
        for question in pending:
            if len(self.question_futures) >= limit:
                return
            try:
                project = self.client.get_project(question.project_id)
            except requests.RequestException:
                raise
            except Exception:
                LOG.warning(
                    "question project unavailable project=%s question=%s",
                    question.project_id,
                    question.id,
                )
                continue
            if project.project.status == "completed":
                continue
            worker = self._select_question_worker(question.project_id)
            if worker is None:
                self._log_changed(
                    "questions/worker",
                    logging.INFO,
                    "no decide-capable worker available for side questions",
                )
                continue
            claim = self.client.claim_question(question.project_id, question.id, worker.name)
            if not claim.ok:
                LOG.info(
                    "question claim skipped project=%s question=%s status=%s",
                    question.project_id,
                    question.id,
                    claim.status_code,
                )
                continue
            try:
                export_yaml = self.client.export_project(question.project_id)
            except Exception:
                LOG.warning(
                    "question snapshot unavailable project=%s question=%s",
                    question.project_id,
                    question.id,
                    exc_info=True,
                )
                self.client.answer_question(
                    question.project_id,
                    question.id,
                    status="failed",
                    error="could not read the graph snapshot",
                    worker=worker.name,
                )
                continue
            try:
                future = self.question_executor.submit(
                    run_question_task,
                    self.config,
                    self.client,
                    self.container_manager,
                    project,
                    question,
                    export_yaml,
                    worker,
                )
            except Exception:
                LOG.exception(
                    "failed to submit question project=%s question=%s", question.project_id, question.id
                )
                continue
            self.question_futures[future] = f"{question.project_id}/{question.id}"
            LOG.info(
                "dispatched question project=%s question=%s worker=%s",
                question.project_id,
                question.id,
                worker.name,
            )

    def _reap_question_futures(self) -> None:
        for future in [future for future in self.question_futures if future.done()]:
            key = self.question_futures.pop(future)
            try:
                outcome = future.result()
            except Exception:
                LOG.exception("question task crashed key=%s", key)
                continue
            if outcome != "success":
                LOG.info("question finished key=%s outcome=%s", key, outcome)

    def _select_question_worker(self, project_id: str) -> WorkerConfig | None:
        now = time.time()
        candidates = [
            worker
            for worker in self.config.workers
            if "decide" in worker.task_types
            and self.worker_unhealthy_until.get(worker.name, 0) <= now
        ]
        if not candidates:
            return None
        return choose_worker(candidates, self._worker_counts())[0]

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

    def _reap_futures(self) -> None:
        done = [future for future in self.futures if future.done()]
        for future in done:
            task = self.futures.pop(future)
            try:
                outcome = future.result()
                if outcome == "cancelled":
                    LOG.info(
                        "task cancelled project=%s task=%s worker=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                    )
                elif outcome != "success":
                    LOG.warning(
                        "task finished project=%s task=%s worker=%s outcome=%s",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        outcome,
                    )
                self._clear_project_log_state(task.project_id)
                if outcome == "unhealthy":
                    retry_after_seconds = UNHEALTHY_RETRY_AFTER_SECONDS
                    self.worker_unhealthy_until[task.worker_name] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked unhealthy worker=%s retry_after=%.0fs",
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_unhealthy_until.pop(task.worker_name, None)
                rejection_key = (task.project_id, task.task_type, task.worker_name)
                if outcome == "rejected":
                    retry_after_seconds = REJECTED_RETRY_AFTER_SECONDS
                    self.worker_rejected_until[rejection_key] = time.time() + retry_after_seconds
                    LOG.info(
                        "worker marked rejected project=%s task=%s worker=%s retry_after=%.0fs",
                        task.project_id,
                        task.task_type,
                        task.worker_name,
                        retry_after_seconds,
                    )
                else:
                    self.worker_rejected_until.pop(rejection_key, None)
                if outcome == "success" and task.task_type == "decide":
                    # Re-read the graph after Decide mutated it, otherwise a Decide that only
                    # abandoned steps would leave a checkpoint that never triggers again.
                    self._refresh_decide_checkpoint(task.project_id)
            except Exception:
                LOG.exception("task crashed project=%s task=%s worker=%s", task.project_id, task.task_type, task.worker_name)

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "completed":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_completed_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_completed, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "stopped":
                continue
            if self._inactive_cleanup_done.get(summary.id) == summary.status:
                continue
            container_name = self.container_manager.container_name(summary.id)
            if container_name in self._cleanup_pending:
                continue
            if not self.container_manager.needs_stopped_cleanup(summary.id):
                self._inactive_cleanup_done[summary.id] = summary.status
                continue
            future = self.cleanup_executor.submit(self.container_manager.cleanup_stopped, summary.id)
            self.cleanup_futures[future] = (container_name, summary.id, summary.status)
            self._cleanup_pending.add(container_name)

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        self._cleanup_completed_containers(summaries)
        self._cleanup_stopped_containers(summaries)

    def _reap_cleanup_futures(self) -> None:
        done = [future for future in self.cleanup_futures if future.done()]
        for future in done:
            name, project_id, target_status = self.cleanup_futures.pop(future)
            self._cleanup_pending.discard(name)
            try:
                success = future.result()
                if success and project_id is not None and target_status in ("completed", "stopped"):
                    self._inactive_cleanup_done[project_id] = target_status
                elif project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
            except Exception:
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)

    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        active_ids = {summary.id for summary in summaries if summary.status == "active"}
        self.runtime_project_ids.intersection_update(active_ids)
        inactive_status_by_id = {summary.id: summary.status for summary in summaries if summary.status != "active"}
        for project_id, status in list(self._inactive_cleanup_done.items()):
            current_status = inactive_status_by_id.get(project_id)
            if current_status != status:
                self._inactive_cleanup_done.pop(project_id, None)

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        status_by_project = {summary.id: summary.status for summary in summaries}
        for task in self.futures.values():
            status = status_by_project.get(task.project_id, "deleted")
            if status != "active" and task.cancellation.cancel(status):
                LOG.info(
                    "cancelling running task for inactive project project=%s task=%s worker=%s status=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                    status,
                )

    def _initialize_decide_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        for summary in summaries:
            if summary.status != "active":
                continue
            if summary.id in self.decide_checkpoints:
                continue
            open_step_count = summary.running_step_count + summary.pending_step_count
            if open_step_count == 0:
                # No checkpoint means "Decide has never run on this graph": it triggers once.
                continue
            self.decide_checkpoints[summary.id] = DecideCheckpoint(
                fact_count=summary.fact_count,
                hint_count=summary.hint_count,
                finding_count=summary.finding_count,
                open_step_count=open_step_count,
            )
            LOG.debug(
                "decide checkpoint initialized project=%s facts=%s hints=%s findings=%s open_steps=%s",
                summary.id,
                summary.fact_count,
                summary.hint_count,
                summary.finding_count,
                open_step_count,
            )

    def _refresh_decide_checkpoint(self, project_id: str) -> None:
        try:
            project = self.client.get_project(project_id)
        except Exception:
            LOG.exception("decide checkpoint refresh failed project=%s", project_id)
            return
        checkpoint = DecideCheckpoint(
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            finding_count=len(project.findings),
            open_step_count=self._project_open_step_count(project),
        )
        self.decide_checkpoints[project_id] = checkpoint
        LOG.debug(
            "decide checkpoint updated project=%s facts=%s hints=%s findings=%s open_steps=%s",
            project_id,
            checkpoint.fact_count,
            checkpoint.hint_count,
            checkpoint.finding_count,
            checkpoint.open_step_count,
        )

    def _best_effort_release(self, project_id: str, step_id: str, worker_name: str) -> None:
        response = self.client.release_step(project_id, step_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("release failed project=%s step=%s worker=%s status=%s", project_id, step_id, worker_name, response.status_code)

    def _best_effort_release_decide(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_decide(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning("decide release failed project=%s worker=%s status=%s", project_id, worker_name, response.status_code)

    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        state = (level, message, args)
        if self._log_state.get(scope) == state:
            return
        self._log_state[scope] = state
        LOG.log(level, message, *args)

    def _clear_log_state(self, scope: str) -> None:
        self._log_state.pop(scope, None)

    def _clear_project_log_state(self, project_id: str) -> None:
        prefix = f"project:{project_id}:"
        for scope in list(self._log_state):
            if scope.startswith(prefix):
                self._log_state.pop(scope, None)

    def _validate_server_settings(self) -> None:
        settings = self.client.get_settings()
        interval = self.config.runtime.interval
        for name, value in (("step_timeout", settings.step_timeout), ("decide_timeout", settings.decide_timeout)):
            if value <= interval:
                raise RuntimeError(
                    f"server {name}={value}s must be greater than dispatcher interval={interval}s"
                )
            if value < interval * 2:
                LOG.warning(
                    "server %s is tight %s=%ss interval=%ss; heartbeat slack is only %ss",
                    name,
                    name,
                    value,
                    interval,
                    value - interval,
                )
                continue
            LOG.info(
                "server setting validated %s=%ss interval=%ss",
                name,
                value,
                interval,
            )

    def _run_startup_healthchecks(self, *, show_commands: bool) -> None:
        results = run_startup_healthchecks(self.config, show_commands=show_commands)
        if any(result.ok for result in results):
            return
        raise RuntimeError(format_failure_summary(results))
