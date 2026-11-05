from __future__ import annotations

import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher import session as session_module
from cairn.dispatcher.protocol.client import ApiResult
from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.session import SessionStream, build_messages, redact
from cairn.dispatcher.workers.usage import Usage
from cairn.server import db
from cairn.server.app import app
from cairn.server.routers import sessions as sessions_router
from cairn.server.services import utcnow_before


@pytest.fixture(autouse=True)
def _no_upload_backoff(monkeypatch) -> None:
    # Retry behaviour is under test; the wall-clock sleep between attempts is not.
    monkeypatch.setattr(session_module, "UPLOAD_RETRY_BACKOFF_SECONDS", 0)


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "test", "origin": "starting point", "goal": "finish", "hints": []},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


DECIDE_SUMMARY = "决策下一步（1 个待办步骤）"


def _session_payload(**overrides) -> dict:
    payload = {
        "worker": "pi-main",
        "worker_type": "pi",
        "phase": "decide",
        "summary": DECIDE_SUMMARY,
        "status": "success",
        "returncode": 0,
        "duration_ms": 1234,
        "native_session": "session-1",
        "messages": [
            {"role": "user", "content": "read the graph"},
            {"role": "assistant", "content": "added step s002"},
        ],
    }
    payload.update(overrides)
    return payload


def test_create_and_read_session(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(f"/projects/{project_id}/sessions", json=_session_payload())
    assert response.status_code == 201
    created = response.json()
    assert created["id"] == "x001"
    assert created["phase"] == "decide"
    assert created["summary"] == DECIDE_SUMMARY
    assert created["timed_out"] is False
    assert [m["seq"] for m in created["messages"]] == [0, 1]
    assert created["messages"][1]["role"] == "assistant"

    response = client.post(f"/projects/{project_id}/sessions", json=_session_payload())
    assert response.json()["id"] == "x002"

    listed = client.get(f"/projects/{project_id}/sessions")
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()] == ["x001", "x002"]
    assert listed.json()[0]["summary"] == DECIDE_SUMMARY

    detail = client.get(f"/projects/{project_id}/sessions/x001")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["content"] == "read the graph"

    project = client.get(f"/projects/{project_id}").json()
    assert [s["id"] for s in project["sessions"]] == ["x001", "x002"]
    assert project["sessions"][0]["summary"] == DECIDE_SUMMARY


def test_session_unknown_ids_return_404(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.get(f"/projects/{project_id}/sessions/x001").status_code == 404
    assert client.get("/projects/proj_999/sessions").status_code == 404
    assert client.post("/projects/proj_999/sessions", json=_session_payload()).status_code == 404
    assert client.patch(f"/projects/{project_id}/sessions/x001", json={"status": "success"}).status_code == 404
    assert (
        client.post(f"/projects/{project_id}/sessions/x001/messages", json={"messages": []}).status_code == 404
    )
    assert client.post(f"/projects/{project_id}/sessions/x001/heartbeat").status_code == 404


def test_live_session_appends_messages_and_finishes(client: TestClient) -> None:
    project_id = _create_project(client)
    created = client.post(f"/projects/{project_id}/sessions", json=_session_payload(status="running")).json()
    assert created["status"] == "running"
    assert created["message_count"] == 2
    session_id = created["id"]

    appended = client.post(
        f"/projects/{project_id}/sessions/{session_id}/messages",
        json={"messages": [{"role": "tool_call", "content": "Bash\nls -la"}]},
    )
    assert appended.status_code == 200
    assert [message["seq"] for message in appended.json()["messages"]] == [0, 1, 2]
    assert appended.json()["messages"][-1] == {
        "seq": 2,
        "role": "tool_call",
        "content": "Bash\nls -la",
        "raw": None,
    }
    assert appended.json()["message_count"] == 3

    beat = client.post(f"/projects/{project_id}/sessions/{session_id}/heartbeat")
    assert beat.status_code == 200
    assert beat.json()["status"] == "running"

    finished = client.patch(
        f"/projects/{project_id}/sessions/{session_id}",
        json={"status": "success", "returncode": 0, "duration_ms": 1200, "native_session": "native-9"},
    )
    assert finished.status_code == 200
    assert finished.json()["status"] == "success"
    assert finished.json()["native_session"] == "native-9"
    assert finished.json()["duration_ms"] == 1200

    detail = client.get(f"/projects/{project_id}/sessions/{session_id}").json()
    assert detail["status"] == "success"
    assert detail["message_count"] == 3
    assert len(detail["messages"]) == 3

    listed = client.get(f"/projects/{project_id}/sessions").json()
    assert listed[0]["message_count"] == 3


def test_stale_running_session_is_expired(client: TestClient) -> None:
    """A dispatcher that dies mid-run must not leave an eternal `running` session."""
    project_id = _create_project(client)
    session_id = client.post(
        f"/projects/{project_id}/sessions", json=_session_payload(status="running")
    ).json()["id"]

    with db.get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = ? WHERE id = ?",
            (utcnow_before(600), session_id),
        )

    detail = client.get(f"/projects/{project_id}/sessions/{session_id}").json()
    assert detail["status"] == "failed"
    assert "dispatcher stopped" in detail["error"]

    listed = client.get(f"/projects/{project_id}/sessions").json()
    assert listed[0]["status"] == "failed"

    # A running session that keeps heartbeating stays alive.
    fresh_id = client.post(
        f"/projects/{project_id}/sessions", json=_session_payload(status="running")
    ).json()["id"]
    client.post(f"/projects/{project_id}/sessions/{fresh_id}/heartbeat")
    assert client.get(f"/projects/{project_id}/sessions/{fresh_id}").json()["status"] == "running"


def test_session_validation_rejects_bad_role(client: TestClient) -> None:
    project_id = _create_project(client)
    payload = _session_payload(messages=[{"role": "robot", "content": "hi"}])
    assert client.post(f"/projects/{project_id}/sessions", json=payload).status_code == 422


def test_sessions_are_deleted_with_project(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(f"/projects/{project_id}/sessions", json=_session_payload())
    assert client.delete(f"/projects/{project_id}").status_code == 204

    with db.get_conn() as conn:
        sessions_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        messages_count = conn.execute("SELECT COUNT(*) FROM session_messages").fetchone()[0]
    assert sessions_count == 0
    assert messages_count == 0


def test_redact_masks_credentials() -> None:
    header = "Auth" + "orization: " + "Bea" + "rer abcdefgh12345"
    text = "key=sk-abcdefghijklmnop API_KEY=supersecret " + header
    masked = redact(text)
    assert "sk-abcdefghijklmnop" not in masked
    assert "supersecret" not in masked
    assert "abcdefgh12345" not in masked


def test_build_messages_prefers_json_events() -> None:
    event = (
        '{"type": "turn_end", "role": "assistant", '
        '"content": [{"type": "text", "text": "step created"}]}'
    )
    stdout = f"{event}\nnot json"
    result = ProcessResult(returncode=0, stdout=stdout, stderr="")
    messages = build_messages("do the thing", result)
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[1]["content"] == "step created"


def test_build_messages_falls_back_to_raw_output() -> None:
    result = ProcessResult(returncode=1, stdout="boom output", stderr="trace")
    messages = build_messages("prompt", result)
    assert messages[0] == {"role": "user", "content": "prompt"}
    assert messages[1]["role"] == "tool"
    assert "boom output" in messages[1]["content"]


def test_build_messages_expands_claude_stream_json() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "system", "subtype": "init", "session_id": "s1"}),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "先看看工作目录"},
                            {"type": "text", "text": "我先列出文件"},
                            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}},
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "a.txt"}]}
                        ],
                    },
                }
            ),
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
            json.dumps({"type": "result", "subtype": "success", "result": '{"accepted":true,"data":{}}'}),
        ]
    )

    messages = build_messages("do the thing", ProcessResult(returncode=0, stdout=stdout, stderr=""))

    assert [m["role"] for m in messages] == [
        "user",
        "thinking",
        "assistant",
        "tool_call",
        "tool_result",
        "assistant",
    ]
    assert messages[0]["content"] == "do the thing"
    assert messages[3]["content"] == "Bash\nls -la"
    assert messages[4]["content"] == "a.txt"
    # The trailing `result` event repeats the last answer and must not duplicate it.
    assert messages[-1]["content"] == '{"accepted":true,"data":{}}'


def test_build_messages_expands_codex_json_items() -> None:
    stdout = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            json.dumps({"type": "turn.started"}),
            json.dumps({"type": "item.completed", "item": {"type": "reasoning", "text": "need to list files"}}),
            json.dumps(
                {
                    "type": "item.started",
                    "item": {"type": "command_execution", "command": "ls", "status": "in_progress"},
                }
            ),
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "command_execution",
                        "command": "ls",
                        "aggregated_output": "a.txt",
                        "exit_code": 0,
                        "status": "completed",
                    },
                }
            ),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": '{"accepted":true}'}}),
            json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12}}),
        ]
    )

    messages = build_messages("go", ProcessResult(returncode=0, stdout=stdout, stderr=""))

    assert [m["role"] for m in messages] == [
        "user",
        "thinking",
        "tool_call",
        "tool_result",
        "assistant",
    ]
    assert messages[2]["content"] == "bash\nls"
    assert messages[3]["content"] == "[exit 0]\na.txt"


def test_build_messages_truncates_long_tool_output() -> None:
    stdout = json.dumps(
        {
            "type": "item.completed",
            "item": {"type": "command_execution", "command": "cat big", "aggregated_output": "x" * 20000},
        }
    )
    messages = build_messages("go", ProcessResult(returncode=0, stdout=stdout, stderr=""))
    tool_output = messages[-1]
    assert tool_output["role"] == "tool_result"
    assert len(tool_output["content"]) < 20000


# ------------------------------------------------------------------ live streaming


class _SessionClient:
    def __init__(self, *, create_status: int = 201) -> None:
        self.create_status = create_status
        self.created: list[dict] = []
        self.appended: list[list[dict]] = []
        self.finished: list[dict] = []
        self.heartbeats: list[int] = []

    def create_session(self, _project_id: str, payload: dict) -> ApiResult:
        self.created.append(payload)
        if self.create_status != 201:
            return ApiResult(self.create_status, text="unavailable")
        return ApiResult(201, {"id": "x001"})

    def append_session_messages(self, _project_id: str, _session_id: str, messages: list[dict]) -> ApiResult:
        self.appended.append(messages)
        return ApiResult(200, {})

    def finish_session(self, _project_id: str, _session_id: str, payload: dict) -> ApiResult:
        self.finished.append(payload)
        return ApiResult(200, {})

    def session_heartbeat(self, _project_id: str, _session_id: str) -> ApiResult:
        self.heartbeats.append(1)
        return ApiResult(200, {})


def _wait_for(predicate, timeout: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_session_stream_appends_while_the_worker_runs() -> None:
    client = _SessionClient()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        step_id="s001",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()

    assert stream.session_id == "x001"
    assert client.created[0]["status"] == "running"
    assert client.created[0]["messages"] == [{"role": "user", "content": "solve it"}]

    stream.feed('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}\n')
    assert _wait_for(lambda: bool(client.appended))
    assert [message["role"] for message in client.appended[0]] == ["assistant"]

    stream.feed('{"type":"item.completed","item":{"type":"command_execution","command":"ls","aggregated_output":"a.txt"}}\n')
    assert _wait_for(lambda: sum(len(batch) for batch in client.appended) >= 3)

    stream.finish(status="success", result=ProcessResult(0, "", ""), duration_ms=42, native_session="native-1")
    assert client.finished[0]["status"] == "success"
    assert client.finished[0]["duration_ms"] == 42
    assert client.finished[0]["native_session"] == "native-1"
    # finish() is idempotent: a second call must not write again.
    stream.finish(status="failed")
    assert len(client.finished) == 1


def test_session_stream_falls_back_to_a_single_upload() -> None:
    client = _SessionClient(create_status=503)
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="decide",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()
    assert stream.session_id is None

    stream.feed('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"answer"}]}}\n')
    stream.finish(status="success", result=ProcessResult(0, "", ""), duration_ms=7)

    assert client.appended == []
    fallback = client.created[-1]
    assert fallback["status"] == "success"
    assert [message["role"] for message in fallback["messages"]] == ["user", "assistant"]
    assert fallback["messages"][0]["content"] == "solve it"


def test_session_stream_retries_a_failed_append() -> None:
    class Flaky(_SessionClient):
        def __init__(self) -> None:
            super().__init__()
            self.failures = 1

        def append_session_messages(self, pid: str, sid: str, messages: list[dict]) -> ApiResult:
            if self.failures:
                self.failures -= 1
                return ApiResult(500, text="boom")
            return super().append_session_messages(pid, sid, messages)

    client = Flaky()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()
    stream.feed('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}\n')

    assert _wait_for(lambda: bool(client.appended))
    assert client.appended[0][0]["content"] == "working"
    stream.finish(status="success", result=ProcessResult(0, "", ""))


def test_session_stream_heartbeats_a_quiet_worker() -> None:
    client = _SessionClient()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        prompt="solve it",
        flush_interval=0.02,
        heartbeat_interval=0.05,
    )
    stream.start()
    assert _wait_for(lambda: bool(client.heartbeats))
    stream.finish(status="success", result=ProcessResult(0, "", ""), duration_ms=5)

    assert client.appended == []
    assert len(client.finished) == 1


def test_session_stream_keeps_raw_output_when_events_are_missing() -> None:
    client = _SessionClient()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()
    stream.feed("plain text only\n")
    stream.finish(status="failed", result=ProcessResult(1, "plain text only", "boom"), duration_ms=3)

    assert [message["role"] for message in client.appended[0]] == ["tool", "tool"]
    assert "boom" in client.appended[0][-1]["content"]


def _sse_events(client: TestClient, url: str) -> list[tuple[str, dict | None]]:
    with client.stream("GET", url) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        events: list[tuple[str, dict | None]] = []
        name = None
        for line in response.iter_lines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: ") :])))
    return events


def test_stream_finished_session_snapshot_then_end(client: TestClient) -> None:
    project_id = _create_project(client)
    created = client.post(f"/projects/{project_id}/sessions", json=_session_payload()).json()

    events = _sse_events(
        client, f"/projects/{project_id}/sessions/{created['id']}/stream"
    )

    assert [name for name, _ in events] == ["snapshot", "end"]
    assert [m["seq"] for m in events[0][1]["messages"]] == [0, 1]
    assert events[0][1]["session"]["status"] == "success"
    assert events[1][1]["session"]["status"] == "success"


def _parse_sse(chunks: list[str]) -> list[tuple[str, dict]]:
    """Frames from raw SSE chunks; keep-alive comments carry no event and are skipped."""
    events: list[tuple[str, dict]] = []
    name = None
    for chunk in chunks:
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[len("event: ") :]
            elif line.startswith("data: "):
                events.append((name, json.loads(line[len("data: ") :])))
    return events


def test_stream_running_session_emits_only_new_messages(client: TestClient) -> None:
    project_id = _create_project(client)
    session = client.post(
        f"/projects/{project_id}/sessions",
        json=_session_payload(
            status="running",
            returncode=None,
            duration_ms=None,
            messages=[{"role": "user", "content": "read the graph"}],
        ),
    ).json()

    async def drive_stream() -> list[str]:
        # TestClient buffers a whole response, so a live stream cannot be read through
        # HTTP; the SSE body generator is driven directly instead.
        stream = sessions_router.session_event_stream(project_id, session["id"])
        chunks = [await anext(stream)]
        assert "event: snapshot" in chunks[0]
        # The transcript grows while the stream is still open.
        client.post(
            f"/projects/{project_id}/sessions/{session['id']}/messages",
            json={"messages": [{"role": "assistant", "content": "adding s002"}]},
        )
        client.patch(
            f"/projects/{project_id}/sessions/{session['id']}",
            json={"status": "success", "returncode": 0, "duration_ms": 10},
        )
        try:
            while True:
                chunks.append(await anext(stream))
        except StopAsyncIteration:
            pass
        finally:
            await stream.aclose()
        return chunks

    results = _parse_sse(asyncio.run(drive_stream()))

    names = [name for name, _ in results]
    assert names[0] == "snapshot"
    assert "messages" in names
    assert names[-1] == "end"

    snapshot = results[0][1]
    assert [m["seq"] for m in snapshot["messages"]] == [0]
    streamed = next(payload for name, payload in results if name == "messages")
    assert [m["seq"] for m in streamed["messages"]] == [1]
    assert streamed["messages"][0]["content"] == "adding s002"
    assert results[-1][1]["session"]["status"] == "success"


def test_stream_unknown_session_returns_404(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.get(f"/projects/{project_id}/sessions/x999/stream")
    assert response.status_code == 404


# ------------------------------------------------------------------ regressions


def test_create_session_requires_explicit_status(client: TestClient) -> None:
    project_id = _create_project(client)
    payload = _session_payload()
    del payload["status"]
    assert client.post(f"/projects/{project_id}/sessions", json=payload).status_code == 422


def test_finished_session_rejects_appends_and_heartbeats_do_not_touch(client: TestClient) -> None:
    project_id = _create_project(client)
    created = client.post(f"/projects/{project_id}/sessions", json=_session_payload()).json()
    session_id = created["id"]
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE sessions SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (session_id,),
        )

    appended = client.post(
        f"/projects/{project_id}/sessions/{session_id}/messages",
        json={"messages": [{"role": "assistant", "content": "late"}]},
    )
    assert appended.status_code == 409

    beat = client.post(f"/projects/{project_id}/sessions/{session_id}/heartbeat")
    assert beat.status_code == 200
    assert beat.json()["status"] == "success"

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT updated_at FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
    assert row["updated_at"] == "2000-01-01T00:00:00Z"


def test_stream_empty_running_session_sends_one_snapshot(
    client: TestClient, monkeypatch
) -> None:
    monkeypatch.setattr(sessions_router, "STREAM_POLL_SECONDS", 0.01)
    project_id = _create_project(client)
    session = client.post(
        f"/projects/{project_id}/sessions",
        json=_session_payload(
            status="running", returncode=None, duration_ms=None, messages=[]
        ),
    ).json()

    async def drive_stream(ticks: int) -> list[str]:
        # An open SSE connection is buffered by TestClient; drive the body directly so
        # the keep-alive cadence can be observed without waiting for the idle limit.
        stream = sessions_router.session_event_stream(project_id, session["id"])
        chunks: list[str] = []
        try:
            while sum(chunk == ": keep-alive\n\n" for chunk in chunks) < ticks:
                chunks.append(await anext(stream))
        finally:
            await stream.aclose()
        return chunks

    chunks = asyncio.run(drive_stream(ticks=5))

    assert sum(chunk.count("event: snapshot") for chunk in chunks) == 1
    assert chunks[0].startswith("event: snapshot")


def test_session_stream_drops_messages_when_server_closed_the_session() -> None:
    class Closed(_SessionClient):
        def append_session_messages(
            self, pid: str, sid: str, messages: list[dict]
        ) -> ApiResult:
            self.appended.append(messages)
            return ApiResult(409, text="Session is not running")

    client = Closed()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()
    stream.feed('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"working"}]}}\n')

    assert _wait_for(lambda: bool(client.appended))
    time.sleep(0.2)  # several flush intervals: a retry loop would show up here
    assert len(client.appended) == 1

    stream.finish(status="failed")
    assert len(client.finished) == 1


def test_session_stream_records_usage_on_finish() -> None:
    client = _SessionClient()
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        prompt="solve it",
        flush_interval=0.02,
    )
    stream.start()
    assert stream.session_id == "x001"

    stream.finish(
        status="success",
        usage=Usage(input_tokens=120, output_tokens=45, cache_read_tokens=30, cache_write_tokens=10),
    )

    assert len(client.finished) == 1
    payload = client.finished[0]
    assert payload["input_tokens"] == 120
    assert payload["output_tokens"] == 45
    assert payload["cache_read_tokens"] == 30
    assert payload["cache_write_tokens"] == 10
    assert payload["total_tokens"] == 205


def test_session_usage_round_trip_and_project_totals(client: TestClient) -> None:
    project_id = _create_project(client)

    created = client.post(
        f"/projects/{project_id}/sessions",
        json=_session_payload(
            status="running",
            input_tokens=100,
            output_tokens=40,
            cache_read_tokens=20,
            cache_write_tokens=5,
            total_tokens=165,
        ),
    ).json()
    assert created["input_tokens"] == 100
    assert created["total_tokens"] == 165

    finished = client.patch(
        f"/projects/{project_id}/sessions/{created['id']}",
        json={
            "status": "success",
            "returncode": 0,
            "input_tokens": 120,
            "output_tokens": 40,
            "cache_read_tokens": 20,
            "cache_write_tokens": 5,
            "total_tokens": 185,
        },
    ).json()
    assert finished["input_tokens"] == 120
    assert finished["total_tokens"] == 185

    client.post(
        f"/projects/{project_id}/sessions",
        json=_session_payload(phase="summary", input_tokens=10, output_tokens=2, total_tokens=12),
    )

    usage = client.get(f"/projects/{project_id}/usage")
    assert usage.status_code == 200
    payload = usage.json()
    assert payload["project_id"] == project_id
    assert payload["sessions"] == 2
    assert payload["input_tokens"] == 130
    assert payload["output_tokens"] == 42
    assert payload["cache_read_tokens"] == 20
    assert payload["cache_write_tokens"] == 5
    assert payload["total_tokens"] == 197
    assert payload["by_phase"]["summary"]["total_tokens"] == 12
    assert payload["by_phase"]["decide"]["total_tokens"] == 185
    assert payload["by_worker"]["pi-main"]["sessions"] == 2
    assert client.get("/projects/proj_999/usage").status_code == 404


class _FlakyAppendClient(_SessionClient):
    """Fails the first few appends with a transient 503, then succeeds."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining_failures = failures

    def append_session_messages(self, project_id: str, session_id: str, messages: list[dict]) -> ApiResult:
        self.appended.append(messages)
        if self.remaining_failures > 0:
            self.remaining_failures -= 1
            return ApiResult(503, text="temporarily unavailable")
        return ApiResult(200, {})


def test_finish_retries_transient_append_failures() -> None:
    client = _FlakyAppendClient(failures=2)
    stream = SessionStream(
        client,
        "proj_001",
        worker="pi-main",
        worker_type="pi",
        phase="execute",
        step_id="s001",
        prompt="solve it",
        flush_interval=10.0,
    )
    stream.start()
    stream.feed('{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"done"}]}}\n')

    stream.finish(status="success", result=ProcessResult(0, "", ""))

    # Two rejected attempts plus the successful one; the tail still lands.
    assert len(client.appended) == 3
    assert client.appended[-1][0]["content"] == "done"
