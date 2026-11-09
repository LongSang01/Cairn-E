"""DispatcherLoop mixin: Deciding when to run Decide and submitting the run."""

from __future__ import annotations

import logging

from cairn.contracts.models import ProjectDetail
from cairn.dispatcher.models import RunningTask, StepFailure
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks.decide import run_decide_task

from ._base import LoopBase
from ._common import MAX_UNPROMPTED_DECIDE_RUNS

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class DecideDispatchMixin(LoopBase):
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
        if trigger.startswith(("idle:", "stuck:", "steers:pending:")):
            # Burn one unprompted attempt only when the run is actually dispatched; a
            # busy worker or a lost claim must not consume the budget.
            checkpoint = self.decide_checkpoints.get(project.project.id)
            if checkpoint is not None:
                checkpoint.unprompted_attempts += 1
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
                step_failures=self._project_step_failures(project.project.id),
            )
        except Exception:  # keep the loop alive when a submit fails
            LOG.exception(
                "failed to submit decide task project=%s worker=%s", project.project.id, worker.name
            )
            self._best_effort_release_decide(project.project.id, worker.name)
            return False
        self.futures[future] = RunningTask(
            project.project.id,
            "decide",
            worker.name,
            cancellation,
            step_id=None,
            trigger=trigger,
            fact_count=len(project.facts),
            hint_count=len(project.hints),
            finding_count=len(project.findings),
            open_step_count=self._project_open_step_count(project),
        )
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info(
            "dispatched decide project=%s worker=%s trigger=%s",
            project.project.id,
            worker.name,
            trigger,
        )
        return True

    def _project_step_failures(self, project_id: str) -> dict[str, StepFailure]:
        """The failure counters Decide gets to see, keyed by step id."""
        return {
            step_id: failure
            for (pid, step_id), failure in self.step_failures.items()
            if pid == project_id
        }

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
        if len(project.steers) > checkpoint.steer_count:
            changes.append(f"steers:{checkpoint.steer_count}->{len(project.steers)}")
        else:
            # A steer left unanswered means the previous run failed or was
            # cancelled. Retry with the same bounded budget as idle/stuck runs
            # instead of waiting forever for a graph change that may never come.
            # Kept separate from a new-steer trigger so a legitimate wake-up does
            # not consume the retry budget.
            unanswered = sum(1 for steer in project.steers if not steer.answer)
            if unanswered and checkpoint.unprompted_attempts < MAX_UNPROMPTED_DECIDE_RUNS:
                changes.append(f"steers:pending:{unanswered}")
        if len(project.findings) > checkpoint.finding_count:
            changes.append(f"findings:{checkpoint.finding_count}->{len(project.findings)}")
        if checkpoint.open_step_count > 0 and open_step_count == 0:
            changes.append(f"open_steps:{checkpoint.open_step_count}->0")
        if changes:
            return ",".join(changes)
        stuck_steps = self._stuck_step_ids(project)
        if stuck_steps:
            # Nothing dispatchable is left, so waiting for a graph change would wait
            # forever: ask Decide to re-route or abandon what keeps failing.
            if checkpoint.unprompted_attempts < MAX_UNPROMPTED_DECIDE_RUNS:
                return f"stuck:{','.join(stuck_steps)}"
            return None
        if open_step_count == 0 and checkpoint.unprompted_attempts < MAX_UNPROMPTED_DECIDE_RUNS:
            # Everything is terminal but the project is still active: give Decide a
            # bounded budget to declare completion, otherwise the graph stays a dead end.
            return f"idle:{checkpoint.unprompted_attempts + 1}"
        return None

    def _stuck_step_ids(self, project: ProjectDetail) -> list[str]:
        """Pending steps that burned their attempts while nothing healthy is left to run.

        Returns an empty list while any pending step can still be executed: the failing
        step is only the scheduler's problem once it is the last thing on the graph.
        """
        pending_steps = [
            step for step in project.steps if step.kind == "action" and step.status == "pending"
        ]
        if not pending_steps:
            return []
        exhausted = [
            step.id
            for step in pending_steps
            if self._step_attempts_exhausted(project.project.id, step.id)
        ]
        if len(exhausted) != len(pending_steps):
            return []
        return sorted(exhausted)
