"""Correlation identifiers shared by server request handling and dispatcher logs.

One context variable carries the current request id so every log line emitted
while handling an HTTP request can be joined afterwards. The dispatcher side can
later reuse the same variable to stitch dispatch rounds into the same picture.
"""

from __future__ import annotations

from contextvars import ContextVar

correlation_id: ContextVar[str | None] = ContextVar("cairn_correlation_id", default=None)
