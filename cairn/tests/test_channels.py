"""Channel separation: the worker's `/internal/*` and the UI's write endpoints.

The worker can reach loopback, so it can call any endpoint the server exposes. What it
must not be able to do is rewrite its own rules, approve its own pending command, or
widen the authorized scope — those are the levers that turn the reviewer into advice.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from cairn.server import db

WORKER_HEADER = "x-cairn-worker-token"
UI_HEADER = "x-cairn-ui-token"


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects", json={"title": "eng", "origin": "starting point", "goal": "finish"}
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _check(client: TestClient, project_id: str, headers: dict[str, str] | None = None):
    return client.post(
        f"/internal/projects/{project_id}/tool-guard/check",
        json={"tool": "bash", "command": "nmap -sV 10.0.0.5", "worker": "local-pi"},
        headers=headers or {},
    )


def test_the_worker_channel_is_open_when_no_token_is_configured(client, monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_WORKER_TOKEN", raising=False)
    project_id = _project(client)

    assert _check(client, project_id).status_code == 200


def test_the_worker_channel_rejects_a_missing_or_wrong_token(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _project(client)

    assert _check(client, project_id).status_code == 401
    assert _check(client, project_id, {WORKER_HEADER: "guess"}).status_code == 401


def test_the_worker_channel_accepts_the_configured_token(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _project(client)

    response = _check(client, project_id, {WORKER_HEADER: "worker-secret"})

    assert response.status_code == 200
    assert response.json()["decision"] in ("allow", "deny", "ask")


def test_the_legacy_check_path_obeys_the_same_token(client, monkeypatch) -> None:
    """An extension written by an older dispatcher must not be a way around the token."""
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _project(client)

    response = client.post(
        f"/projects/{project_id}/tool-guard/check",
        json={"tool": "bash", "command": "whoami"},
    )

    assert response.status_code == 401


def test_writing_tool_rules_requires_the_ui_channel(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")
    project_id = _project(client)
    body = {"target": "command", "match_type": "contains", "pattern": "curl", "action": "allow"}

    assert client.post(f"/projects/{project_id}/tool-rules", json=body).status_code == 403
    allowed = client.post(
        f"/projects/{project_id}/tool-rules", json=body, headers={UI_HEADER: "ui-secret"}
    )
    assert allowed.status_code == 201


def test_deciding_an_approval_requires_the_ui_channel(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")
    project_id = _project(client)
    with db.get_conn() as conn:
        conn.execute(
            "INSERT INTO tool_approvals (id, project_id, tool, command, digest, status,"
            " created_at) VALUES ('appr_x', ?, 'bash', 'rm -rf /data', 'd', 'pending',"
            " '2026-01-01T00:00:00Z')",
            (project_id,),
        )

    assert (
        client.post(
            f"/projects/{project_id}/tool-approvals/appr_x/decide", json={"approve": True}
        ).status_code
        == 403
    )
    ok = client.post(
        f"/projects/{project_id}/tool-approvals/appr_x/decide",
        json={"approve": True},
        headers={UI_HEADER: "ui-secret"},
    )
    assert ok.status_code == 200


def test_editing_the_scope_requires_the_ui_channel(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")
    project_id = _project(client)
    body = {"kind": "cidr", "value": "10.0.0.0/8"}

    assert client.post(f"/projects/{project_id}/scope", json=body).status_code == 403
    allowed = client.post(
        f"/projects/{project_id}/scope", json=body, headers={UI_HEADER: "ui-secret"}
    )
    assert allowed.status_code == 201


def test_read_only_endpoints_stay_open_to_the_worker(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _project(client)

    response = client.get(
        f"/internal/projects/{project_id}/tool-approvals/appr_missing",
        headers={WORKER_HEADER: "worker-secret"},
    )

    assert response.status_code == 404
