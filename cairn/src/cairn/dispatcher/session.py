from __future__ import annotations

import json
import logging
import re
from typing import Any, Iterable

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.process import ProcessResult

LOG = logging.getLogger(__name__)

# Content longer than this is stored truncated so one runaway tool output cannot
# bloat the project database.
MAX_MESSAGE_CHARS = 20000
# Bounds on how much raw stdout/stderr we keep as a fallback transcript.
MAX_FALLBACK_CHARS = 20000

_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "sk-***REDACTED***"),
    (re.compile(r"tp-[A-Za-z0-9_\-]{8,}"), "tp-***REDACTED***"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{8,}"), "gh*_***REDACTED***"),
    (re.compile(r"AKIA[0-9A-Z]{12,}"), "AKIA***REDACTED***"),
    (re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{8,}"), r"\1***REDACTED***"),
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:API[_-]?KEY|TOKEN|SECRET|PASSWORD|PASSWD|ACCESS[_-]?KEY)[A-Z0-9_]*)"
            r"(\s*[:=]\s*)(['\"]?)([^\s'\"]{4,})"
        ),
        r"\1\2\3***REDACTED***",
    ),
)


def redact(text: str) -> str:
    """Mask credential-looking substrings before anything is stored server side."""
    if not text:
        return text
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


def _truncate(text: str, limit: int = MAX_MESSAGE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif block.get("type") == "tool_use":
                    parts.append(f"[tool_use {block.get('name', '?')}] {json.dumps(block.get('input'))}")
                elif block.get("type") == "tool_result":
                    parts.append(f"[tool_result] {_text_from_content(block.get('content'))}")
        return "\n".join(part for part in parts if part)
    if isinstance(content, dict):
        return _text_from_content([content])
    return ""


def _iter_events(text: str) -> Iterable[dict[str, Any]]:
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            yield event


def _messages_from_events(events: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for event in events:
        kind = event.get("type")
        role: str | None = None
        content: Any = None
        if kind in ("message", "turn_start", "turn_end", "agent_start", "agent_end"):
            role = event.get("role")
            content = event.get("content") or event.get("message")
            if isinstance(content, dict):
                role = content.get("role") or role
                content = content.get("content")
        elif kind == "item":
            item = event.get("item")
            if isinstance(item, dict):
                role = item.get("role") or item.get("type")
                content = item.get("content") or item.get("text")
        if not role or content is None:
            continue
        text = _text_from_content(content).strip()
        if not text:
            continue
        if role not in ("user", "assistant", "system", "tool"):
            role = "assistant" if role in ("agent", "model") else "tool"
        messages.append({"role": role, "content": _truncate(redact(text))})
    return messages


def build_messages(
    prompt: str | None,
    result: ProcessResult,
    extra_streams: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Reconstruct the conversation from the worker's JSON event stream."""
    messages: list[dict[str, Any]] = []
    if prompt:
        messages.append({"role": "user", "content": _truncate(redact(prompt))})

    events = list(_iter_events(result.stdout or ""))
    messages.extend(_messages_from_events(events))
    if len(messages) > 1:
        return messages

    # No structured events: keep the raw streams so the run is still inspectable.
    for stream in (result.stdout, result.stderr, *extra_streams):
        text = (stream or "").strip()
        if not text:
            continue
        messages.append(
            {"role": "tool", "content": _truncate(redact(text)[:MAX_FALLBACK_CHARS])}
        )
    return messages


def record_session(
    client: CairnClient,
    project_id: str,
    *,
    worker: str,
    worker_type: str,
    phase: str,
    status: str,
    result: ProcessResult | None = None,
    step_id: str | None = None,
    native_session: str | None = None,
    prompt: str | None = None,
    duration_ms: int | None = None,
    error: str | None = None,
    extra_streams: Iterable[str] = (),
) -> None:
    """Upload one model conversation. Failures are logged and never affect the task."""
    try:
        messages = build_messages(prompt, result, extra_streams) if result is not None else []
        if prompt and (result is None or not messages):
            messages = [{"role": "user", "content": _truncate(redact(prompt))}]
        if not messages:
            return
        timeout = bool(result and result.timed_out)
        response = client.create_session(
            project_id,
            {
                "worker": worker,
                "worker_type": worker_type,
                "phase": phase,
                "step": step_id,
                "native_session": native_session,
                "status": status,
                "returncode": result.returncode if result is not None else None,
                "timed_out": timeout,
                "duration_ms": duration_ms,
                "error": redact(error) if error else None,
                "messages": messages,
            },
        )
        if not response.ok:
            LOG.warning(
                "session upload failed project=%s phase=%s worker=%s status=%s",
                project_id,
                phase,
                worker,
                response.status_code,
            )
    except Exception:  # noqa: BLE001 - never break a task over bookkeeping
        LOG.warning("session upload crashed project=%s phase=%s worker=%s", project_id, phase, worker, exc_info=True)
