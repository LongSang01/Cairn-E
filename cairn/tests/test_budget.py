"""Token budget gate: crossing it blocks the project, raising it unblocks."""

from __future__ import annotations

from fastapi.testclient import TestClient


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "eng", "origin": "starting point", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _report_usage(client: TestClient, project_id: str, tokens: int) -> None:
    created = client.post(
        f"/projects/{project_id}/sessions",
        json={
            "worker": "executor",
            "worker_type": "pi",
            "phase": "execute",
            "status": "running",
        },
    )
    assert created.status_code == 201
    session_id = created.json()["id"]
    finished = client.patch(
        f"/projects/{project_id}/sessions/{session_id}",
        json={
            "worker": "executor",
            "status": "success",
            "input_tokens": tokens,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": tokens,
        },
    )
    assert finished.status_code == 200


def test_crossing_the_budget_blocks_the_project(client: TestClient) -> None:
    project_id = _project(client)
    budget = client.put(f"/projects/{project_id}/budget", json={"token_budget": 100})
    assert budget.status_code == 200
    assert budget.json()["token_budget"] == 100

    _report_usage(client, project_id, 150)

    detail = client.get(f"/projects/{project_id}").json()["project"]
    assert detail["status"] == "blocked"
    assert detail["blocked_reason"].startswith("token budget exceeded")


def test_staying_under_the_budget_keeps_it_active(client: TestClient) -> None:
    project_id = _project(client)
    client.put(f"/projects/{project_id}/budget", json={"token_budget": 1000})

    _report_usage(client, project_id, 10)

    assert client.get(f"/projects/{project_id}").json()["project"]["status"] == "active"


def test_raising_the_budget_unblocks(client: TestClient) -> None:
    project_id = _project(client)
    client.put(f"/projects/{project_id}/budget", json={"token_budget": 100})
    _report_usage(client, project_id, 150)

    raised = client.put(f"/projects/{project_id}/budget", json={"token_budget": 1000})

    assert raised.status_code == 200
    assert raised.json()["status"] == "active"
    assert raised.json()["blocked_reason"] is None


def test_clearing_the_budget_does_not_fight_a_manual_stop(client: TestClient) -> None:
    project_id = _project(client)
    client.put(f"/projects/{project_id}/status", json={"status": "stopped"})

    cleared = client.put(f"/projects/{project_id}/budget", json={"token_budget": None})

    assert cleared.status_code == 200
    assert cleared.json()["status"] == "stopped"


def test_a_negative_budget_is_rejected(client: TestClient) -> None:
    project_id = _project(client)

    response = client.put(f"/projects/{project_id}/budget", json={"token_budget": -5})

    assert response.status_code == 422
