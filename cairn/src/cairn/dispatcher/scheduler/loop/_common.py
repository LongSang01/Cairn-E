"""Shared constants and types for the dispatcher loop mixins."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cairn.dispatcher.config import WorkerConfig

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5
# A failed Decide leaves the checkpoint untouched, so the same trigger would fire again
# on the next tick. Cool down per project instead of re-spending a full Decide run.
DECIDE_RETRY_AFTER_SECONDS = 10
# The cooldown only spaces the runs out: a Decide that keeps failing on an unchanged
# graph would still burn a full run every DECIDE_RETRY_AFTER_SECONDS forever. After this
# many consecutive failures on the *same* trigger the scheduler waits for the graph to
# move instead (proj_009 burned 71 noop runs this way).
MAX_CONSECUTIVE_DECIDE_FAILURES = 5
# Bounded budget for Decide runs the graph itself did not ask for: an idle (fully
# terminal) graph, or one whose remaining steps already burned their Execute attempts.
MAX_UNPROMPTED_DECIDE_RUNS = 3
# Consecutive failed Execute runs after which a pending step stops being re-dispatched.
# Retrying it blindly only re-spends a model run on the same dead end (proj_009: s062
# burned four 600s runs), so the next Decide has to change route or abandon it.
MAX_STEP_EXECUTE_ATTEMPTS = 3


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]
