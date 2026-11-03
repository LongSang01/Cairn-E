from __future__ import annotations

from dataclasses import dataclass

from cairn.dispatcher.runtime.cancellation import TaskCancellation


@dataclass(slots=True)
class RunningTask:
    project_id: str
    task_type: str
    worker_name: str
    cancellation: TaskCancellation
    step_id: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    finding_count: int | None = None
    open_step_count: int | None = None


@dataclass(slots=True)
class DecideCheckpoint:
    """Graph state observed right before/after a Decide run, used to trigger the next one."""

    fact_count: int
    hint_count: int
    finding_count: int
    open_step_count: int
