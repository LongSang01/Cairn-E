"""Shared surface of ``DispatcherLoop``: state attributes and cross-mixin methods.

``DispatcherLoop`` is composed from focused mixins, so a mixin that calls a method
defined by a sibling would otherwise be invisible to static analysis. Declaring the
shared state and the cross-mixin entry points here keeps every mixin type-checkable
without a module-wide ``attr-defined`` suppression.

Nothing here is meant to run: the mixins override every method, and ``CoreMixin``
owns the constructor. The bodies raise so an accidental MRO regression fails loudly
instead of silently doing nothing.
"""

from __future__ import annotations

import _thread
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path

from cairn.contracts.models import ProjectDetail, ProjectSummary, Step
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.models import DecideCheckpoint, RunningTask, StepFailure
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.tasks.summary import SummaryRequest

from ._common import WorkerSelection


class LoopBase:
    """State and interface shared by every scheduler mixin."""

    # Lifetime and collaborators (set by CoreMixin.__init__).
    config_path: Path
    config: DispatchConfig
    client: CairnClient
    container_manager: ExecutionBackend

    # Task pools: decide/execute, container cleanup, side questions, summaries.
    executor: ThreadPoolExecutor
    cleanup_executor: ThreadPoolExecutor
    question_executor: ThreadPoolExecutor
    summary_executor: ThreadPoolExecutor

    futures: dict[Future[str], RunningTask]
    cleanup_futures: dict[Future[bool], tuple[str, str | None, str | None]]
    question_futures: dict[Future[str], str]
    summary_futures: dict[Future[str], str]
    summary_inflight: set[str]
    _summary_lock: _thread.LockType

    # Per-project bookkeeping.
    decide_checkpoints: dict[str, DecideCheckpoint]
    runtime_project_ids: set[str]
    worker_unhealthy_until: dict[str, float]
    worker_rejected_until: dict[tuple[str, str, str], float]
    decide_retry_until: dict[str, float]
    decide_failures: dict[tuple[str, str], int]
    step_failures: dict[tuple[str, str], StepFailure]
    _log_state: dict[str, tuple[int, str, tuple[object, ...]]]
    _cleanup_pending: set[str]
    _inactive_cleanup_done: dict[str, str]
    project_cursor: int
    _settings_checked: bool
    _startup_healthchecks_checked: bool

    # --- core: lifecycle ------------------------------------------------
    def close(self) -> None:
        raise NotImplementedError

    def run(self, once: bool = False) -> None:
        raise NotImplementedError

    def run_startup_healthchecks_only(self) -> None:
        raise NotImplementedError

    def run_startup_healthchecks(self, *, show_commands: bool = False, force: bool = False) -> None:
        raise NotImplementedError

    def _run_local_binary_check(self) -> None:
        raise NotImplementedError

    @staticmethod
    def _probe_local_cli(binary: str) -> tuple[str | None, bool]:
        raise NotImplementedError

    def _validate_server_settings(self) -> None:
        raise NotImplementedError

    # --- logging --------------------------------------------------------
    def _log_changed(self, scope: str, level: int, message: str, *args: object) -> None:
        raise NotImplementedError

    def _clear_log_state(self, scope: str) -> None:
        raise NotImplementedError

    def _clear_project_log_state(self, project_id: str) -> None:
        raise NotImplementedError

    # --- dispatch entry -------------------------------------------------
    def _dispatch_available(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _ordered_projects(self, summaries: list[ProjectSummary]) -> list[ProjectSummary]:
        raise NotImplementedError

    def _try_dispatch_project(self, summary: ProjectSummary) -> bool:
        raise NotImplementedError

    # --- decide dispatch ------------------------------------------------
    def _dispatch_decide(self, project: ProjectDetail, export_yaml: str, trigger: str) -> bool:
        raise NotImplementedError

    def _decide_trigger(self, project: ProjectDetail) -> str | None:
        raise NotImplementedError

    def _stuck_step_ids(self, project: ProjectDetail) -> list[str]:
        raise NotImplementedError

    def _project_step_failures(self, project_id: str) -> dict[str, StepFailure]:
        raise NotImplementedError

    # --- execute dispatch -----------------------------------------------
    def _dispatch_execute(self, project: ProjectDetail, export_yaml: str, step: Step) -> bool:
        raise NotImplementedError

    def _step_attempts_exhausted(self, project_id: str, step_id: str) -> bool:
        raise NotImplementedError

    def _forget_closed_step_failures(self, project: ProjectDetail) -> None:
        raise NotImplementedError

    # --- scheduling bookkeeping -----------------------------------------
    def _select_worker(self, project_id: str, task_type: str) -> WorkerSelection:
        raise NotImplementedError

    def _worker_counts(self) -> dict[str, int]:
        raise NotImplementedError

    def _project_running_task_count(self, project_id: str) -> int:
        raise NotImplementedError

    def _project_running_task_summary(self, project_id: str) -> list[str]:
        raise NotImplementedError

    def _project_running_steps(self, project_id: str) -> set[str]:
        raise NotImplementedError

    def _running_project_count(self, summaries: list[ProjectSummary]) -> int:
        raise NotImplementedError

    def _project_open_step_count(self, project: ProjectDetail) -> int:
        raise NotImplementedError

    # --- questions and summaries ----------------------------------------
    def request_summary(self, request: SummaryRequest) -> bool:
        raise NotImplementedError

    def _reap_summary_futures(self) -> None:
        raise NotImplementedError

    def _dispatch_questions(self) -> None:
        raise NotImplementedError

    def _reap_question_futures(self) -> None:
        raise NotImplementedError

    def _select_question_worker(self, project_id: str) -> WorkerConfig | None:
        raise NotImplementedError

    # --- reaping and cleanup --------------------------------------------
    def _reap_futures(self) -> None:
        raise NotImplementedError

    def _record_decide_failure(self, task: RunningTask) -> None:
        raise NotImplementedError

    def _forget_decide_failures(self, project_id: str) -> None:
        raise NotImplementedError

    def _record_step_outcome(
        self, project_id: str, step_id: str | None, outcome: str, reason: str | None
    ) -> None:
        raise NotImplementedError

    def _best_effort_release(self, project_id: str, step_id: str, worker_name: str) -> None:
        raise NotImplementedError

    def _best_effort_release_decide(self, project_id: str, worker_name: str) -> None:
        raise NotImplementedError

    def _cleanup_completed_containers(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _cleanup_stopped_containers(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _queue_container_cleanups(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _reap_cleanup_futures(self) -> None:
        raise NotImplementedError

    # --- checkpoints ----------------------------------------------------
    def _refresh_runtime_projects(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _cancel_inactive_tasks(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _initialize_decide_checkpoints(self, summaries: list[ProjectSummary]) -> None:
        raise NotImplementedError

    def _refresh_decide_checkpoint(self, project_id: str) -> None:
        raise NotImplementedError
