from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

from cairn.dispatcher.runtime.process import ProcessResult
from cairn.dispatcher.session import build_messages, redact
from cairn.server import db
from cairn.server.app import app


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


def _session_payload(**overrides) -> dict:
    payload = {
        "worker": "pi-main",
        "worker_type": "pi",
        "phase": "decide",
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
    assert created["timed_out"] is False
    assert [m["seq"] for m in created["messages"]] == [0, 1]
    assert created["messages"][1]["role"] == "assistant"

    response = client.post(f"/projects/{project_id}/sessions", json=_session_payload())
    assert response.json()["id"] == "x002"

    listed = client.get(f"/projects/{project_id}/sessions")
    assert listed.status_code == 200
    assert [s["id"] for s in listed.json()] == ["x001", "x002"]

    detail = client.get(f"/projects/{project_id}/sessions/x001")
    assert detail.status_code == 200
    assert detail.json()["messages"][0]["content"] == "read the graph"

    project = client.get(f"/projects/{project_id}").json()
    assert [s["id"] for s in project["sessions"]] == ["x001", "x002"]


def test_session_unknown_ids_return_404(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.get(f"/projects/{project_id}/sessions/x001").status_code == 404
    assert client.get("/projects/proj_999/sessions").status_code == 404
    assert client.post("/projects/proj_999/sessions", json=_session_payload()).status_code == 404


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
    stdout = "\n".join(
        [
            '{"type": "turn_end", "role": "assistant", '
            '"content": [{"type": "text", "text": "step created"}]}',
            "not json",
        ]
    )
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
