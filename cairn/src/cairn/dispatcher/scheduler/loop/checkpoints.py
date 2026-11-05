"""DispatcherLoop mixin: Decide checkpoints and per-project runtime bookkeeping."""

from __future__ import annotations

import logging

from cairn.contracts.models import ProjectSummary
from cairn.dispatcher.models import DecideCheckpoint

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class CheckpointsMixin:
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
        except Exception:  # a stale checkpoint only costs a little context
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
