from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Iterable
from typing import Any

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.workers.stream import text_from_content

LOG = logging.getLogger(__name__)

# Content longer than this is stored truncated so one runaway tool output cannot
# bloat the project database.
MAX_MESSAGE_CHARS = 20000
# Tool output is usually the bulkiest part of a transcript, keep a readable prefix.
MAX_TOOL_OUTPUT_CHARS = 8000
MAX_TOOL_INPUT_CHARS = 800
# Live sessions append as they run, so this is a hard stop rather than a trim point.
MAX_MESSAGES = 1000
# Bounds on how much raw stdout/stderr we keep as a fallback transcript.
MAX_FALLBACK_CHARS = 20000
# How often buffered transcript entries are pushed to the server while a worker runs.
FLUSH_INTERVAL_SECONDS = 1.0
FLUSH_JOIN_TIMEOUT_SECONDS = 5.0
# A quiet worker (long tool run, no output) still needs to prove it is alive, otherwise
# the server expires the running session after SESSION_STALE_SECONDS.
HEARTBEAT_INTERVAL_SECONDS = 5.0

# Replayed events repeat what an earlier event already carried (pi's turn_end /
# agent_end repeat the messages, claude's result repeats the last answer).
_REPLAY_EVENT_TYPES = ("turn_end", "agent_end", "result")

# Keys that usually hold the interesting part of a tool call's input.
_TOOL_INPUT_KEYS = ("command", "cmd", "file_path", "path", "pattern", "query", "url", "description")

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


def _flatten(value: Any) -> str:
    """Render a payload we do not model explicitly (tool result, mcp result)."""
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    try:
        return json.dumps(value, ensure_ascii=False, indent=None)
    except (TypeError, ValueError):
        return str(value)


def _normalize_role(role: Any, default: str = "assistant") -> str:
    if not isinstance(role, str):
        return default
    role = role.strip().lower()
    if role in ("user", "human"):
        return "user"
    if role in ("assistant", "agent", "model", "ai"):
        return "assistant"
    if role == "system":
        return "system"
    if role in ("tool", "toolresult", "tool_result", "tool_output", "function"):
        return "tool_result"
    if role in ("thinking", "reasoning"):
        return "thinking"
    return default


# --------------------------------------------------------------------------- messages


def _text_message(role: str, text: Any, *, limit: int = MAX_MESSAGE_CHARS) -> list[dict[str, Any]]:
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text:
        return []
    return [{"role": role, "content": _truncate(redact(text), limit)}]


def _describe_tool_input(tool_input: Any) -> str:
    if tool_input is None:
        return ""
    if isinstance(tool_input, str):
        return _truncate(tool_input.strip(), MAX_TOOL_INPUT_CHARS)
    if isinstance(tool_input, dict):
        for key in _TOOL_INPUT_KEYS:
            value = tool_input.get(key)
            if isinstance(value, str) and value.strip():
                return _truncate(value.strip(), MAX_TOOL_INPUT_CHARS)
    return _truncate(_flatten(tool_input), MAX_TOOL_INPUT_CHARS)


def _tool_call_messages(name: Any, tool_input: Any) -> list[dict[str, Any]]:
    label = (name if isinstance(name, str) else "").strip() or "tool"
    detail = _describe_tool_input(tool_input)
    content = f"{label}\n{detail}" if detail else label
    return [{"role": "tool_call", "content": _truncate(redact(content), MAX_MESSAGE_CHARS)}]


def _tool_result_messages(output: Any, exit_code: Any = None) -> list[dict[str, Any]]:
    if isinstance(output, dict) and "content" in output:
        output = output.get("content")
    text = text_from_content(output).strip() or (_flatten(output).strip() if not isinstance(output, str) else "")
    if not text:
        return []
    prefix = f"[exit {exit_code}]\n" if isinstance(exit_code, int) and not isinstance(exit_code, bool) else ""
    return [{"role": "tool_result", "content": _truncate(redact(prefix + text), MAX_TOOL_OUTPUT_CHARS)}]


def _blocks_to_messages(role: str, content: Any) -> list[dict[str, Any]]:
    """Expand one message's content blocks into individual transcript entries."""
    if content is None:
        return []
    if isinstance(content, str):
        return _text_message(role, content)

    blocks = content if isinstance(content, list) else [content]
    messages: list[dict[str, Any]] = []
    for block in blocks:
        if isinstance(block, str):
            messages.extend(_text_message(role, block))
            continue
        if not isinstance(block, dict):
            continue
        kind = block.get("type")
        if kind in (None, "text", "input_text", "output_text"):
            messages.extend(_text_message(role, block.get("text")))
        elif kind in ("thinking", "reasoning"):
            messages.extend(_text_message("thinking", block.get("thinking") or block.get("text")))
        elif kind in ("tool_use", "tool_call", "function_call"):
            messages.extend(_tool_call_messages(block.get("name") or block.get("tool"), block.get("input", block.get("arguments"))))
        elif kind in ("tool_result", "tool_output", "function_call_output"):
            payload = block.get("content") if "content" in block else block.get("output")
            messages.extend(_tool_result_messages(payload))
        elif kind == "image":
            messages.extend(_text_message(role, "[image]"))
    return messages


def _item_messages(item: Any) -> list[dict[str, Any]]:
    """Parse one stream item (codex `item.*`, or a generic tool event)."""
    if not isinstance(item, dict):
        return []
    kind = item.get("type")
    if kind is None and "role" in item:
        return _blocks_to_messages(_normalize_role(item.get("role")), item.get("content"))
    if kind in ("reasoning", "thinking"):
        return _text_message("thinking", item.get("text") or item.get("content"))
    if kind in ("agent_message", "assistant_message"):
        return _blocks_to_messages("assistant", item.get("text") if item.get("text") is not None else item.get("content"))
    if kind == "user_message":
        return _blocks_to_messages("user", item.get("text") if item.get("text") is not None else item.get("content"))
    if kind in ("tool_use", "function_call"):
        return _tool_call_messages(item.get("name") or item.get("tool"), item.get("input") if "input" in item else item.get("arguments"))
    if kind in ("tool_result", "tool_output", "function_call_output"):
        return _tool_result_messages(item.get("content") if "content" in item else item.get("output"))
    if kind == "command_execution":
        messages = _tool_call_messages(item.get("name") or "bash", {"command": item.get("command") or ""})
        messages.extend(
            _tool_result_messages(item.get("aggregated_output") or item.get("output"), item.get("exit_code"))
        )
        return messages
    if kind == "file_change":
        changes = [change for change in (item.get("changes") or []) if isinstance(change, dict)]
        summary = "\n".join(
            " ".join(part for part in (str(change.get("kind") or "edit"), str(change.get("path") or "")) if part)
            for change in changes
        )
        return _tool_call_messages("apply_patch", summary or item.get("path") or item.get("text"))
    if kind == "mcp_tool_call":
        name = ".".join(str(part) for part in (item.get("server"), item.get("tool") or item.get("name")) if part)
        messages = _tool_call_messages(name or "mcp", item.get("arguments") if "arguments" in item else item.get("input"))
        result = item.get("result")
        if isinstance(result, dict) and "content" in result:
            messages.extend(_tool_result_messages(result.get("content")))
        elif result is not None:
            messages.extend(_tool_result_messages(_flatten(result)))
        return messages
    if kind == "web_search":
        return _tool_call_messages("web_search", {"query": item.get("query")})
    if kind == "error":
        return _text_message("system", item.get("message") or item.get("text"))
    return []


def _event_messages(event: dict[str, Any]) -> list[dict[str, Any]]:
    kind = event.get("type")
    if not isinstance(kind, str):
        return []

    # claude stream-json: {"type":"assistant","message":{"role":..,"content":[..]}}
    if kind in ("assistant", "user"):
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        role = _normalize_role(message.get("role"), _normalize_role(kind))
        return _blocks_to_messages(role, message.get("content"))

    # pi --mode json and the generic event shape used by older workers.
    if kind in ("message", "message_end"):
        message = event.get("message") if isinstance(event.get("message"), dict) else event
        role = _normalize_role(message.get("role"), "assistant")
        return _blocks_to_messages(role, message.get("content"))

    if kind in ("turn_start", "turn_end", "agent_start", "agent_end"):
        messages: list[dict[str, Any]] = []
        message = event.get("message")
        if isinstance(message, dict):
            messages.extend(_blocks_to_messages(_normalize_role(message.get("role"), "assistant"), message.get("content")))
        elif "content" in event:
            messages.extend(_blocks_to_messages(_normalize_role(event.get("role"), "assistant"), event.get("content")))
        for entry in event.get("messages") or []:
            if isinstance(entry, dict):
                messages.extend(_blocks_to_messages(_normalize_role(entry.get("role"), "assistant"), entry.get("content")))
        for entry in event.get("toolResults") or []:
            if isinstance(entry, dict):
                messages.extend(_tool_result_messages(entry.get("result")))
        return messages

    if kind == "item":
        return _item_messages(event.get("item"))
    if kind == "item.completed":
        return _item_messages(event.get("item"))
    if kind in ("item.started", "item.updated"):
        # The matching `item.completed` carries the same payload plus the result.
        return []

    if kind == "tool_execution_start":
        return _tool_call_messages(event.get("toolName") or event.get("tool_name"), event.get("args") or event.get("arguments"))
    if kind == "tool_execution_end":
        result = event.get("result")
        output = result.get("content") if isinstance(result, dict) and "content" in result else result
        return _tool_result_messages(output)

    # claude stream-json: {"type":"result","result":"final answer","is_error":false}
    if kind == "result":
        return _text_message("system" if event.get("is_error") else "assistant", event.get("result"))

    if kind in ("error", "turn.failed"):
        error = event.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        return _text_message("system", detail or event.get("message") or _flatten(event))

    return []


class TranscriptBuilder:
    """Ordered, de-duplicated transcript built from a worker's output stream.

    Chunks can be fed while the process runs (`add_chunk`) and new entries are left in
    `pending` so a `SessionStream` can upload them incrementally.
    """

    def __init__(self, prompt: str | None = None) -> None:
        self.messages: list[dict[str, Any]] = []
        self.pending: list[dict[str, Any]] = []
        self._seen: set[tuple[str, str]] = set()
        self._buffer = ""
        self._raw: list[str] = []
        self._raw_chars = 0
        self._capped = False
        self._prompt_content: str | None = None
        if prompt:
            content = _truncate(redact(prompt))
            self._prompt_content = content
            self._seen.add(("user", content))
            self._append({"role": "user", "content": content})

    def add(self, messages: Iterable[dict[str, Any]], *, replay: bool = False) -> None:
        for message in messages:
            if (
                self._prompt_content is not None
                and message["role"] == "user"
                and message["content"] == self._prompt_content
            ):
                # Some CLIs echo the prompt back as a user message.
                continue
            key = (message["role"], message["content"])
            if replay and key in self._seen:
                continue
            if len(self.messages) >= MAX_MESSAGES:
                if not self._capped:
                    self._capped = True
                    self._append(
                        {
                            "role": "system",
                            "content": f"... [{MAX_MESSAGES} messages reached: the rest of this run is not recorded]",
                        }
                    )
                continue
            self._seen.add(key)
            self._append(message)

    def _append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)
        self.pending.append(message)

    def take_pending(self) -> list[dict[str, Any]]:
        batch, self.pending = self.pending, []
        return batch

    def requeue(self, batch: list[dict[str, Any]]) -> None:
        """Put an upload batch back at the front so a failed append is retried in order."""
        if batch:
            self.pending[:0] = batch

    def add_chunk(self, chunk: str) -> None:
        """Feed a raw stdout chunk; complete JSON lines become transcript entries."""
        if not chunk:
            return
        remaining = MAX_FALLBACK_CHARS - self._raw_chars
        if remaining > 0:
            kept = chunk[:remaining]
            self._raw.append(kept)
            self._raw_chars += len(kept)
        self._buffer += chunk
        while True:
            index = self._buffer.find("\n")
            if index < 0:
                return
            line, self._buffer = self._buffer[:index], self._buffer[index + 1 :]
            self._add_line(line)

    def _add_line(self, line: str) -> None:
        line = line.strip()
        if not line.startswith("{"):
            return
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(event, dict):
            return
        self.add(_event_messages(event), replay=event.get("type") in _REPLAY_EVENT_TYPES)

    def finalize(self, result: ProcessResult | None = None, extra_streams: Iterable[str] = ()) -> None:
        if self._buffer.strip():
            self._add_line(self._buffer)
        self._buffer = ""
        if len(self.messages) > 1:
            return
        # No structured events: keep the raw streams so the run is still inspectable.
        streams: list[str | None] = (
            [result.stdout, result.stderr] if result is not None else ["".join(self._raw)]
        )
        streams.extend(extra_streams)
        for stream in streams:
            text = (stream or "").strip()
            if not text:
                continue
            self.add([{"role": "tool", "content": _truncate(redact(text)[:MAX_FALLBACK_CHARS])}])


def build_messages(
    prompt: str | None,
    result: ProcessResult,
    extra_streams: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Reconstruct the conversation from the worker's JSON event stream."""
    builder = TranscriptBuilder(prompt)
    builder.add_chunk(result.stdout or "")
    builder.finalize(result, extra_streams)
    return builder.messages


class SessionStream:
    """A session that is visible while the worker is still running.

    The session is created up front (status `running`), transcript entries captured from
    stdout are appended every second, and the run is finalized with its real status at
    the end. Every server interaction is best effort: a broken session must never break
    a task, and a failed create falls back to a single upload at the end.
    """

    def __init__(
        self,
        client: CairnClient,
        project_id: str,
        *,
        worker: str,
        worker_type: str,
        phase: str,
        step_id: str | None = None,
        prompt: str | None = None,
        native_session: str | None = None,
        flush_interval: float = FLUSH_INTERVAL_SECONDS,
        heartbeat_interval: float = HEARTBEAT_INTERVAL_SECONDS,
    ) -> None:
        self.session_id: str | None = None
        self._client = client
        self._project_id = project_id
        self._worker = worker
        self._worker_type = worker_type
        self._phase = phase
        self._step_id = step_id
        self._native_session = native_session
        self._flush_interval = flush_interval
        self._heartbeat_interval = heartbeat_interval
        self._last_contact = time.monotonic()
        self._builder = TranscriptBuilder(prompt)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._flusher: threading.Thread | None = None
        self._finished = False

    def __enter__(self) -> SessionStream:
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if not self._finished:
            self.finish(status="failed", error=f"task crashed: {exc}" if exc else "session not finalized")
        return False

    # --- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        with self._lock:
            messages = list(self._builder.pending)
        payload = {
            "worker": self._worker,
            "worker_type": self._worker_type,
            "phase": self._phase,
            "step": self._step_id,
            "native_session": self._native_session,
            "status": "running",
            "messages": messages,
        }
        try:
            response = self._client.create_session(self._project_id, payload)
        except Exception:  # noqa: BLE001 - bookkeeping must not break a task
            LOG.warning(
                "session create crashed project=%s phase=%s worker=%s", self._project_id, self._phase, self._worker, exc_info=True
            )
            response = None
        if response is not None and response.ok and isinstance(response.data, dict):
            session_id = response.data.get("id")
            if isinstance(session_id, str) and session_id:
                self.session_id = session_id
        if self.session_id is not None:
            # Those messages are on the server now; keep them pending otherwise so the
            # end-of-run fallback upload still carries the prompt.
            with self._lock:
                self._builder.take_pending()
        else:
            LOG.warning(
                "session live update disabled project=%s phase=%s worker=%s", self._project_id, self._phase, self._worker
            )
        self._flusher = threading.Thread(target=self._flush_loop, name=f"session-{self._phase}", daemon=True)
        self._flusher.start()

    def feed(self, chunk: str) -> None:
        """stdout callback: runs on the process reader thread, never blocks on HTTP."""
        with self._lock:
            self._builder.add_chunk(chunk)

    def finish(
        self,
        *,
        status: str,
        result: ProcessResult | None = None,
        error: str | None = None,
        duration_ms: int | None = None,
        native_session: str | None = None,
        extra_streams: Iterable[str] = (),
    ) -> None:
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        flusher, self._flusher = self._flusher, None
        if flusher is not None and flusher is not threading.current_thread():
            flusher.join(timeout=FLUSH_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            self._builder.finalize(result, extra_streams)
        native_session = native_session or self._native_session
        if self.session_id is None:
            self._upload_once(status, result, error, duration_ms, native_session)
            return
        self._flush()
        payload = {
            "status": status,
            "returncode": result.returncode if result is not None else None,
            "timed_out": bool(result and result.timed_out),
            "duration_ms": duration_ms,
            "error": redact(error) if error else None,
            "native_session": native_session,
        }
        try:
            response = self._client.finish_session(self._project_id, self.session_id, payload)
        except Exception:  # noqa: BLE001
            LOG.warning(
                "session finish crashed project=%s phase=%s worker=%s", self._project_id, self._phase, self._worker, exc_info=True
            )
            return
        if not response.ok:
            LOG.warning(
                "session finish failed project=%s phase=%s worker=%s status=%s",
                self._project_id,
                self._phase,
                self._worker,
                response.status_code,
            )

    # --- uploads -----------------------------------------------------------------

    def _flush_loop(self) -> None:
        while not self._stop.wait(self._flush_interval):
            if self._flush_pending():
                continue
            # Nothing new to upload: prove we are alive so the server does not expire
            # this session while a long tool call keeps the worker quiet.
            if time.monotonic() - self._last_contact >= self._heartbeat_interval:
                self._heartbeat()

    def _flush(self) -> None:
        self._flush_pending()

    def _heartbeat(self) -> None:
        session_id = self.session_id
        if session_id is None:
            return
        self._last_contact = time.monotonic()
        try:
            response = self._client.session_heartbeat(self._project_id, session_id)
        except Exception:  # noqa: BLE001
            LOG.warning(
                "session heartbeat crashed project=%s phase=%s worker=%s",
                self._project_id,
                self._phase,
                self._worker,
                exc_info=True,
            )
            return
        if not response.ok:
            LOG.warning(
                "session heartbeat failed project=%s phase=%s worker=%s status=%s",
                self._project_id,
                self._phase,
                self._worker,
                response.status_code,
            )

    def _flush_pending(self) -> int:
        session_id = self.session_id
        if session_id is None:
            return 0
        with self._lock:
            batch = self._builder.take_pending()
        if not batch:
            return 0
        try:
            response = self._client.append_session_messages(self._project_id, session_id, batch)
        except Exception:  # noqa: BLE001
            LOG.warning(
                "session append crashed project=%s phase=%s worker=%s", self._project_id, self._phase, self._worker, exc_info=True
            )
            self._requeue(batch)
            return 0
        if not response.ok:
            LOG.warning(
                "session append failed project=%s phase=%s worker=%s status=%s messages=%s",
                self._project_id,
                self._phase,
                self._worker,
                response.status_code,
                len(batch),
            )
            if response.status_code != 409:
                self._requeue(batch)
            # 409 = the server closed this session (finished or expired); retrying
            # can never land, so drop the batch instead of requeueing it forever.
            return 0
        self._last_contact = time.monotonic()
        return len(batch)

    def _requeue(self, batch: list[dict[str, Any]]) -> None:
        with self._lock:
            self._builder.requeue(batch)

    def _upload_once(
        self,
        status: str,
        result: ProcessResult | None,
        error: str | None,
        duration_ms: int | None,
        native_session: str | None,
    ) -> None:
        """Fallback when the live session could not be created."""
        with self._lock:
            messages = self._builder.take_pending()
        if not messages:
            return
        payload = {
            "worker": self._worker,
            "worker_type": self._worker_type,
            "phase": self._phase,
            "step": self._step_id,
            "native_session": native_session,
            "status": status,
            "returncode": result.returncode if result is not None else None,
            "timed_out": bool(result and result.timed_out),
            "duration_ms": duration_ms,
            "error": redact(error) if error else None,
            "messages": messages,
        }
        try:
            response = self._client.create_session(self._project_id, payload)
        except Exception:  # noqa: BLE001
            LOG.warning(
                "session upload crashed project=%s phase=%s worker=%s", self._project_id, self._phase, self._worker, exc_info=True
            )
            return
        if not response.ok:
            LOG.warning(
                "session upload failed project=%s phase=%s worker=%s status=%s",
                self._project_id,
                self._phase,
                self._worker,
                response.status_code,
            )


def start_session(
    client: CairnClient,
    project_id: str,
    *,
    worker: str,
    worker_type: str,
    phase: str,
    step_id: str | None = None,
    prompt: str | None = None,
    native_session: str | None = None,
) -> SessionStream:
    """Create a live session and return its stream. Never raises."""
    stream = SessionStream(
        client,
        project_id,
        worker=worker,
        worker_type=worker_type,
        phase=phase,
        step_id=step_id,
        prompt=prompt,
        native_session=native_session,
    )
    stream.start()
    return stream
