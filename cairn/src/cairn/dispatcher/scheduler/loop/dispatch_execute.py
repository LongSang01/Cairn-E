"""DispatcherLoop mixin: Submitting Execute runs and tracking step attempts."""

from __future__ import annotations

import logging

from cairn.contracts.models import ProjectDetail, Step
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.tasks.execute import run_execute_task

from ._base import LoopBase
from ._common import MAX_STEP_EXECUTE_ATTEMPTS

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class ExecuteDispatchMixin(LoopBase):
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
        cancellation = TaskCancellation()
        task = RunningTask(
            project.project.id, "execute", worker.name, cancellation, step_id=step.id
        )

        def job() -> str:
            return run_execute_task(
                self.config,
                self.client,
                self.container_manager,
                project,
                export_yaml,
                step,
                worker,
                cancellation,
                on_failure=lambda text: setattr(task, "failure_reason", text),
            )

        try:
            future = self.executor.submit(job)
        except Exception:  # keep the loop alive when a submit fails
            LOG.exception(
                "failed to submit execute task project=%s step=%s worker=%s",
                project.project.id,
                step.id,
                worker.name,
            )
            self._best_effort_release(project.project.id, step.id, worker.name)
            return False
        self.futures[future] = task
        self.runtime_project_ids.add(project.project.id)
        self._clear_project_log_state(project.project.id)
        LOG.info(
            "dispatched execute project=%s step=%s worker=%s",
            project.project.id,
            step.id,
            worker.name,
        )
        return True

    def _step_attempts_exhausted(self, project_id: str, step_id: str) -> bool:
        failure = self.step_failures.get((project_id, step_id))
        return failure is not None and failure.count >= MAX_STEP_EXECUTE_ATTEMPTS

    def _forget_closed_step_failures(self, project: ProjectDetail) -> None:
        """Drop counters for steps a Decide closed, so the map tracks only live steps."""
        open_ids = {
            step.id
            for step in project.steps
            if step.kind == "action" and step.status in ("pending", "running")
        }
        project_id = project.project.id
        for key in [
            key for key in self.step_failures if key[0] == project_id and key[1] not in open_ids
        ]:
            self.step_failures.pop(key, None)
