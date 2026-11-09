"""DispatcherLoop: scheduler concerns composed from focused mixins.

The class (and its import path) stays stable; ``time`` and ``shutil`` are re-exported
so existing callers and tests can still patch them through this module.
"""

from __future__ import annotations

import shutil
import time

from ._common import (
    LOG,
    REJECTED_RETRY_AFTER_SECONDS,
    UNHEALTHY_RETRY_AFTER_SECONDS,
    WorkerSelection,
)
from .checkpoints import CheckpointsMixin
from .core import CoreMixin
from .dispatch import DispatchMixin
from .dispatch_decide import DecideDispatchMixin
from .dispatch_execute import ExecuteDispatchMixin
from .log_state import LogStateMixin
from .questions import QuestionsMixin
from .reaper import ReaperMixin
from .scheduling import SchedulingMixin


class DispatcherLoop(
    CoreMixin,
    DispatchMixin,
    DecideDispatchMixin,
    ExecuteDispatchMixin,
    SchedulingMixin,
    QuestionsMixin,
    ReaperMixin,
    CheckpointsMixin,
    LogStateMixin,
):
    """One dispatcher process: polls the server and runs Decide / Execute / Q&A."""


__all__ = [
    "LOG",
    "REJECTED_RETRY_AFTER_SECONDS",
    "UNHEALTHY_RETRY_AFTER_SECONDS",
    "DispatcherLoop",
    "WorkerSelection",
    "shutil",
    "time",
]
