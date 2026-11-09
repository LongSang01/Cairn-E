"""Channel separation: the worker's `/internal/*` and the UI's write endpoints.

The worker can reach loopback, so it can call any endpoint the server exposes. What it
must not be able to do is rewrite its own rules, approve its own pending command, or
widen the authorized scope — those are the levers that turn the reviewer into advice.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from starlette.requests import Request

from cairn.server.channels import require_ui_channel

WORKER_HEADER = "x-cairn-worker-token"


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


def _request(authenticated: bool) -> Request:
    scope = {"type": "http", "method": "POST", "headers": [], "state": {}}
    if authenticated:
        scope["state"]["ui_authenticated"] = True
    return Request(scope)


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


def test_the_ui_channel_rejects_a_request_the_middleware_did_not_mark() -> None:
    with pytest.raises(HTTPException) as excinfo:
        require_ui_channel(_request(authenticated=False))

    assert excinfo.value.status_code == 403


def test_the_ui_channel_accepts_a_request_the_middleware_marked() -> None:
    require_ui_channel(_request(authenticated=True))


def test_read_only_endpoints_stay_open_to_the_worker(client, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _project(client)

    response = client.get(
        f"/internal/projects/{project_id}/tool-approvals/appr_missing",
        headers={WORKER_HEADER: "worker-secret"},
    )

    assert response.status_code == 404
