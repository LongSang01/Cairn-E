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
    # The Decide trigger string, kept so a repeated failure can be told apart from a
    # fresh graph change (see ``decide_failures``).
    trigger: str | None = None
    fact_count: int | None = None
    hint_count: int | None = None
    finding_count: int | None = None
    open_step_count: int | None = None
    # Why the task failed, filled in by the task itself so the reaper can count it.
    failure_reason: str | None = None


@dataclass(slots=True)
class StepFailure:
    """Consecutive failed Execute runs of one step, surfaced to the next Decide.

    The dispatcher is the only place that sees a timeout or a crashed run, and the
    graph alone cannot tell Decide that a step has already burned several attempts.
    Keeping this per step lets the next Decide change route or abandon the step
    instead of the scheduler silently re-running it forever.
    """

    count: int = 0
    reason: str = ""
    at: str = ""


@dataclass(slots=True)
class DecideCheckpoint:
    """Graph state observed right before/after a Decide run, used to trigger the next one."""

    fact_count: int
    hint_count: int
    finding_count: int
    open_step_count: int
    # Decide runs the graph itself did not ask for (terminal graph, or one whose
    # remaining steps burned their Execute attempts). Bounded so a dead end stalls
    # instead of spending tokens forever.
    unprompted_attempts: int = 0
