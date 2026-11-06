"""DispatcherLoop mixin: Reaping finished futures and cleaning up container work."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime

from cairn.contracts.models import ProjectSummary
from cairn.dispatcher.models import RunningTask, StepFailure

from ._common import (
    DECIDE_RETRY_AFTER_SECONDS,
    MAX_CONSECUTIVE_DECIDE_FAILURES,
    REJECTED_RETRY_AFTER_SECONDS,
    UNHEALTHY_RETRY_AFTER_SECONDS,
)

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class ReaperMixin:
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
                    self.worker_unhealthy_until[task.worker_name] = (
                        time.time() + retry_after_seconds
                    )
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
                if task.task_type == "execute":
                    self._record_step_outcome(
                        task.project_id, task.step_id, outcome, task.failure_reason
                    )
                if task.task_type == "decide":
                    if outcome == "success":
                        self.decide_retry_until.pop(task.project_id, None)
                        self._forget_decide_failures(task.project_id)
                    elif outcome == "failed":
                        # The checkpoint is left untouched, so the same trigger would fire
                        # again on the next tick: cool down instead of re-spending a run.
                        # "unhealthy"/"rejected" keep their own worker-level cooldowns so a
                        # healthy worker can still fail over immediately.
                        self._record_decide_failure(task)
                        self.decide_retry_until[task.project_id] = (
                            time.time() + DECIDE_RETRY_AFTER_SECONDS
                        )
                if outcome == "success" and task.task_type == "decide":
                    # Re-read the graph after Decide mutated it, otherwise a Decide that only
                    # abandoned steps would leave a checkpoint that never triggers again.
                    self._refresh_decide_checkpoint(task.project_id)
            except Exception:  # a crashed task must not stop the loop
                if task.task_type == "decide":
                    self._record_decide_failure(task)
                    self.decide_retry_until[task.project_id] = (
                        time.time() + DECIDE_RETRY_AFTER_SECONDS
                    )
                LOG.exception(
                    "task crashed project=%s task=%s worker=%s",
                    task.project_id,
                    task.task_type,
                    task.worker_name,
                )

    def _record_decide_failure(self, task: RunningTask) -> None:
        """Count a Decide failure against the trigger it was dispatched for.

        Keyed by trigger so a graph change starts a fresh budget: the counter only
        grows while the scheduler keeps retrying the *same* dead end.
        """
        key = (task.project_id, task.trigger or "")
        count = self.decide_failures.get(key, 0) + 1
        self.decide_failures[key] = count
        if count >= MAX_CONSECUTIVE_DECIDE_FAILURES:
            LOG.error(
                "decide keeps failing project=%s trigger=%s consecutive=%s "
                "waiting_for_graph_change=true",
                task.project_id,
                task.trigger,
                count,
            )

    def _forget_decide_failures(self, project_id: str) -> None:
        for key in [key for key in self.decide_failures if key[0] == project_id]:
            self.decide_failures.pop(key, None)

    def _record_step_outcome(
        self,
        project_id: str,
        step_id: str | None,
        outcome: str,
        reason: str | None,
    ) -> None:
        """Track consecutive failed Execute runs so the next Decide can see them.

        A timeout or a crashed run never reaches the graph, so without this counter the
        scheduler re-claims the same step forever and Decide keeps re-planning the same
        dead end (proj_009: s062 burned four 600s runs).
        """
        if step_id is None:
            return
        key = (project_id, step_id)
        if outcome == "success":
            if self.step_failures.pop(key, None) is not None:
                LOG.info("step recovered project=%s step=%s", project_id, step_id)
            return
        if outcome != "failed":
            # cancelled / rejected / unhealthy are not the step's own failure.
            return
        failure = self.step_failures.get(key)
        if failure is None:
            failure = StepFailure()
        failure.count += 1
        failure.reason = reason or "execute failed"
        failure.at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.step_failures[key] = failure
        LOG.warning(
            "step failed project=%s step=%s consecutive=%s reason=%s",
            project_id,
            step_id,
            failure.count,
            failure.reason,
        )

    def _best_effort_release(self, project_id: str, step_id: str, worker_name: str) -> None:
        response = self.client.release_step(project_id, step_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning(
                "release failed project=%s step=%s worker=%s status=%s",
                project_id,
                step_id,
                worker_name,
                response.status_code,
            )

    def _best_effort_release_decide(self, project_id: str, worker_name: str) -> None:
        response = self.client.release_decide(project_id, worker_name)
        if not response.ok and response.status_code not in (403, 409):
            LOG.warning(
                "decide release failed project=%s worker=%s status=%s",
                project_id,
                worker_name,
                response.status_code,
            )

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
            future = self.cleanup_executor.submit(
                self.container_manager.cleanup_completed, summary.id
            )
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
            future = self.cleanup_executor.submit(
                self.container_manager.cleanup_stopped, summary.id
            )
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
            except Exception:  # bookkeeping must not break the task
                if project_id is not None:
                    self._inactive_cleanup_done.pop(project_id, None)
                LOG.exception("container cleanup failed container=%s", name)
