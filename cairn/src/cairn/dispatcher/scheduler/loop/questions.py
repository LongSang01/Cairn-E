"""DispatcherLoop mixin: Side questions and rolling context summaries."""

from __future__ import annotations

import logging
import time

import requests

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.scheduler.worker_select import choose_worker
from cairn.dispatcher.tasks.question import run_question_task
from cairn.dispatcher.tasks.summary import SummaryRequest, run_summary_task

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")


class QuestionsMixin:
    def request_summary(self, request: SummaryRequest) -> bool:
        """Queue a background summary for history a Decide could not carry.

        Called from a worker thread (inside the Decide task), so it only touches the
        executor and a set guarded by the GIL. Returns False when a summary for this
        project is already in flight, which keeps concurrent Decide runs from fighting
        over the same channels.
        """
        if not request:
            return False
        worker = next((item for item in self.config.workers if item.name == request.worker), None)
        if worker is None:
            LOG.warning(
                "summary worker is gone worker=%s project=%s", request.worker, request.project_id
            )
            return False
        with self._summary_lock:
            if request.project_id in self.summary_inflight:
                return False
            try:
                future = self.summary_executor.submit(
                    run_summary_task,
                    self.config,
                    self.client,
                    self.container_manager,
                    worker,
                    request,
                )
            except Exception:  # a summary is best effort
                LOG.exception("failed to queue summary project=%s", request.project_id)
                return False
            self.summary_futures[future] = request.project_id
            self.summary_inflight.add(request.project_id)
        return True

    def _reap_summary_futures(self) -> None:
        with self._summary_lock:
            finished = [
                (future, self.summary_futures.pop(future))
                for future in list(self.summary_futures)
                if future.done()
            ]
            for _, project_id in finished:
                self.summary_inflight.discard(project_id)
        for future, project_id in finished:
            try:
                outcome = future.result()
            except Exception:  # never let a summary kill the loop
                LOG.exception("summary task crashed project=%s", project_id)
                continue
            self._log_changed(
                f"summary:{project_id}",
                logging.INFO,
                "graph summary %s project=%s",
                outcome,
                project_id,
            )

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
            except Exception:  # noqa: BLE001 - one unreadable project must not stop the loop
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
            except Exception:  # degrade to no snapshot, still answer
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
            except Exception:  # a failed submit must not stop the question pass
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
            except Exception:  # never let a question crash the loop
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
