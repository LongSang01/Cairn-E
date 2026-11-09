"""HTTP cache revalidation shared by the polled GET endpoints.

`no-cache` still lets a browser reuse the body after a 304: it must revalidate, but
the polled payloads (the heaviest GETs) are no longer transferred every few seconds.
"""

from __future__ import annotations

import hashlib

from fastapi import Request, Response
from pydantic import BaseModel

CACHE_CONTROL = "no-cache"


def etag_for(payload: BaseModel | list[BaseModel]) -> str:
    # Pydantic's own serializer is already what FastAPI sends; hashing it avoids a
    # second, slower jsonable_encoder pass on every poll.
    if isinstance(payload, list):
        raw = "[" + ",".join(item.model_dump_json(by_alias=True) for item in payload) + "]"
    else:
        raw = payload.model_dump_json(by_alias=True)
    return f'"{hashlib.sha256(raw.encode()).hexdigest()}"'


def cache_headers(
    request: Request, payload: BaseModel | list[BaseModel]
) -> tuple[dict[str, str], Response | None]:
    """Return the headers for a fresh body, or a 304 when the caller's copy is current."""
    tag = etag_for(payload)
    headers = {"ETag": tag, "Cache-Control": CACHE_CONTROL}
    offered = {
        item.strip().removeprefix("W/")
        for item in request.headers.get("if-none-match", "").split(",")
    }
    if tag in offered or "*" in offered:
        return headers, Response(status_code=304, headers=headers)
    return headers, None
