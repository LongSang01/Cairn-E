"""Helpers for the JSON event streams that agent CLIs print in streaming modes.

pi (`--mode json`) emits newline-delimited JSON events. The dispatcher uses
these helpers to keep the answer extraction and the transcript building on the
same parsing rules.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def iter_json_events(text: str) -> Iterator[dict[str, Any]]:
    """Yield every JSON object found on its own line, skipping anything else."""
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload


def text_from_content(content: Any) -> str:
    """Flatten a message content payload into the text blocks it carries."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        if isinstance(text, str):
            return text
        return text_from_content([content])
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                text = block.get("text")
                if isinstance(text, str) and text:
                    parts.append(text)
        return "\n".join(parts)
    return ""
