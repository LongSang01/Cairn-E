"""DispatcherLoop mixin: Selecting projects and submitting Decide / Execute tasks."""

from __future__ import annotations

import logging
import time

from cairn.contracts.models import ProjectSummary, Step

from ._base import LoopBase
from ._common import (
    MAX_CONSECUTIVE_DECIDE_FAILURES,
)
from .concurrency import conflict_reason

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


def _batch_key(step: Step) -> str:
    """Which parallel group a step belongs to.

    Steps carry the Decide run that proposed them; a step without one (data from before
    batches, or a manually created step) is its own group, which keeps it serial.
    """
    return step.batch or f"step:{step.id}"


class DispatchMixin(LoopBase):
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
            self._log_changed(
                "dispatch/global", logging.INFO, "skip dispatch because no active projects"
            )
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
            now = time.time()
            retry_at = self.decide_retry_until.get(summary.id, 0.0)
            if retry_at > now:
                self._log_changed(
                    f"{skip_scope}:decide_retry",
                    logging.DEBUG,
                    "skip decide project=%s because the last decide failed retry_in=%.0fs",
                    summary.id,
                    retry_at - now,
                )
            else:
                decide_trigger = self._decide_trigger(project)
                if decide_trigger is not None:
                    failures = self.decide_failures.get((summary.id, decide_trigger), 0)
                    if failures >= MAX_CONSECUTIVE_DECIDE_FAILURES:
                        # The cooldown alone keeps re-spending a run on the same dead end:
                        # stop until the graph (and with it the trigger) moves.
                        self._log_changed(
                            f"{skip_scope}:decide_failures",
                            logging.ERROR,
                            "skip decide project=%s because trigger=%s already failed %s times",
                            summary.id,
                            decide_trigger,
                            failures,
                        )
                    else:
                        export_yaml = self.client.export_project(summary.id)
                        return self._dispatch_decide(project, export_yaml, decide_trigger)

        self._forget_closed_step_failures(project)
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
        dispatchable_steps = [
            step for step in pending_steps if not self._step_attempts_exhausted(summary.id, step.id)
        ]
        if dispatchable_steps:
            # Parallelism is the planner's decision, not the scheduler's. A Decide run is
            # the unit of consistency: it saw one snapshot, so only the steps it proposed
            # together (same batch) may run at the same time. Steps from different runs
            # never overlap, because neither run knew about the other's actions.
            slots = self.config.runtime.max_running_steps
            # The server is the authority on what is running: a restarted dispatcher has
            # no futures yet, but the project still shows the steps in flight.
            server_running = [step for step in project.steps if step.status == "running"]
            in_flight = {step.id for step in server_running} | running_step_ids
            free = slots - len(in_flight)
            if free <= 0:
                self._log_changed(
                    f"{skip_scope}:execute_slots_full",
                    logging.DEBUG,
                    "skip execute project=%s because %s/%s slots are running",
                    summary.id,
                    len(in_flight),
                    slots,
                )
                return False

            running_batches = {_batch_key(step) for step in server_running}
            if len(running_batches) > 1:
                # Mixed batches only happen for pre-batch data; drain before adding more.
                self._log_changed(
                    f"{skip_scope}:mixed_batches",
                    logging.WARNING,
                    "skip execute project=%s because running steps span %s batches",
                    summary.id,
                    len(running_batches),
                )
                return False
            if running_batches:
                batch = next(iter(running_batches))
                candidates = [step for step in dispatchable_steps if _batch_key(step) == batch]
                if not candidates:
                    self._log_changed(
                        f"{skip_scope}:batch_drained",
                        logging.DEBUG,
                        "skip execute project=%s because no pending step belongs to the "
                        "running batch %s",
                        summary.id,
                        batch,
                    )
                    return False
            else:
                ordered = sorted(
                    dispatchable_steps,
                    key=lambda step: (step.priority, step.created_at, step.id),
                    reverse=True,
                )
                batch = _batch_key(ordered[0])
                candidates = [step for step in ordered if _batch_key(step) == batch]

            # Steps of one batch run in parallel by default. Only an exclusive step
            # holds its asset, so the slot goes to a sibling that can start now.
            running_steps = list(server_running)
            export_yaml = self.client.export_project(summary.id)
            dispatched = False
            for step in candidates:
                if free <= 0:
                    break
                reason = conflict_reason(step, running_steps)
                if reason is not None:
                    self._log_changed(
                        f"{skip_scope}:{reason}:{step.id}",
                        logging.DEBUG,
                        "skip execute project=%s step=%s because asset=%s is held (%s)",
                        summary.id,
                        step.id,
                        step.asset,
                        reason,
                    )
                    continue
                if self._dispatch_execute(project, export_yaml, step):
                    dispatched = True
                    free -= 1
                    running_steps.append(step)
            return dispatched
        if pending_steps:
            # Every remaining step already burned its attempts; Decide is the only one
            # that may re-route or abandon them, so do not spend another run here.
            self._log_changed(
                f"{skip_scope}:step_attempts_exhausted",
                logging.WARNING,
                "skip execute project=%s because the remaining steps burned their attempts steps=%s",
                summary.id,
                sorted(step.id for step in pending_steps),
            )
            return False
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
