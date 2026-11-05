"""Shared constants and types for the dispatcher loop mixins."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cairn.dispatcher.config import WorkerConfig

LOG = logging.getLogger("cairn.dispatcher.scheduler.loop")
UNHEALTHY_RETRY_AFTER_SECONDS = 5
REJECTED_RETRY_AFTER_SECONDS = 5


@dataclass(slots=True)
class WorkerSelection:
    worker: WorkerConfig | None
    blocked_busy: list[str]
    blocked_unhealthy: list[str]
    blocked_rejected: list[str]
    blocked_task_type: list[str]
