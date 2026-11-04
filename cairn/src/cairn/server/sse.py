"""Server-sent event helpers shared by the streaming endpoints.

Both the session transcript stream and the question stream emit the same wire format and
need the same anti-buffering headers, so they build their responses here instead of
repeating the framing in every router.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator

from fastapi.responses import StreamingResponse

SSE_MEDIA_TYPE = "text/event-stream"

# Proxies must not buffer the stream, or clients receive every event in one burst.
SSE_HEADERS = {
    "Cache-Control": "no-cache, no-transform",
    "X-Accel-Buffering": "no",
    "Connection": "keep-alive",
}


def sse_event(name: str, payload: dict) -> str:
    """Frame one named event, JSON-encoded and unescaped for CJK output."""
    return f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def sse_response(events: AsyncIterator[str] | Iterator[str]) -> StreamingResponse:
    """Wrap an event generator with the SSE media type and headers."""
    return StreamingResponse(events, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS)
