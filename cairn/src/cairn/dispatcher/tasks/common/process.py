"""Shared task helpers: Running a worker process and reading its result."""

from __future__ import annotations

import logging

from cairn.contracts.models import Step
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import OutputCallback, ProcessResult
from cairn.dispatcher.workers.base import WorkerDriver
from cairn.dispatcher.workers.usage import Usage

LOG = logging.getLogger(__name__)

PROCESS_COMMUNICATE_GRACE_SECONDS = 15


LOG_PREVIEW_LIMIT = 1200


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def driver_usage(driver: WorkerDriver, stdout: str, stderr: str) -> Usage | None:
    """Token usage reported by this run, or None when its CLI exposes none.

    Usage bookkeeping is observability, not a task result: a failing parser records no
    tokens instead of failing the run.
    """
    try:
        return driver.extract_usage(stdout, stderr)
    except Exception:
        LOG.warning("could not read worker token usage", exc_info=True)
        return None


def step_session_summary(step: Step, limit: int = 200) -> str:
    """The one-line description a session list shows for an execute run."""
    text = " ".join((step.title or step.description or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def run_worker_process(
    container_manager: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    on_stdout: OutputCallback | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
    )
    if on_stdout is not None:
        # Set before start() so no chunk is missed.
        process.on_stdout = on_stdout
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)
