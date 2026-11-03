from __future__ import annotations

from fastapi.testclient import TestClient
import pytest

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
        json={
            "title": "test",
            "origin": "starting point",
            "goal": "finish",
            "hints": [{"content": "initial clue", "creator": "human"}],
        },
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["facts"] == [{"id": "origin", "description": "starting point"}]
    assert payload["goals"][0]["id"] == "g001"
    assert payload["goals"][0]["parent"] is None
    return payload["project"]["id"]


def test_project_workflow_creates_goal_step_finding_completes_and_reopens(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/goals",
        json={"description": "stage one", "creator": "decider"},
    )
    assert response.status_code == 201
    assert response.json()["id"] == "g002"
    assert response.json()["parent"] == "g001"

    response = client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "goal": "g002",
            "description": "investigate",
            "priority": 5,
            "creator": "decider",
            "worker": None,
        },
    )
    assert response.status_code == 201
    assert response.json()["id"] == "s001"
    assert response.json()["from"] == ["origin"]
    assert response.json()["priority"] == 5

    response = client.post(f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "executor"})
    assert response.status_code == 200
    assert response.json()["status"] == "running"

    response = client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [{"description": "new fact"}],
            "findings": [{"kind": "flaw", "description": "a product"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["facts"] == [{"id": "f001", "description": "new fact"}]
    assert payload["findings"][0]["kind"] == "flaw"
    assert payload["findings"][0]["step"] == "s001"
    assert payload["step"]["status"] == "done"
    assert payload["step"]["to"] == "f001"

    response = client.post(f"/projects/{project_id}/goals/g002/close", json={"status": "achieved", "worker": "decider"})
    assert response.status_code == 200
    assert response.json()["status"] == "achieved"

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "solved", "worker": "decider"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "completed"
    assert payload["goal"]["status"] == "achieved"
    assert payload["step"]["kind"] == "completion"

    detail = client.get(f"/projects/{project_id}").json()
    assert [f["id"] for f in detail["facts"]] == ["origin", "f001"]
    assert [d["kind"] for d in detail["findings"]] == ["flaw"]

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction"}
    assert payload["goal"]["status"] == "active"

    detail = client.get(f"/projects/{project_id}").json()
    assert all(step["kind"] != "completion" for step in detail["steps"])


def test_stopping_project_releases_claims_and_decide_but_keeps_hints_writable(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/decide/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    response = client.put(f"/projects/{project_id}/status", json={"status": "stopped"})
    assert response.status_code == 200
    assert response.json()["decide"] is None

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["steps"][0]["worker"] is None
    assert detail["steps"][0]["status"] == "pending"
    assert client.post(
        f"/projects/{project_id}/hints",
        json={"content": "manual note", "creator": "human"},
    ).status_code == 201
    assert client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "blocked", "creator": "decider", "worker": None},
    ).status_code == 403


def test_step_creation_rejects_unknown_fact_and_mismatched_initial_worker(client: TestClient) -> None:
    project_id = _create_project(client)

    assert client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["f999"], "description": "invalid", "creator": "decider", "worker": None},
    ).status_code == 404
    assert client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "invalid", "creator": "decider", "worker": "executor"},
    ).status_code == 400


def test_settings_and_export_are_backed_by_the_same_database(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.put("/settings", json={"step_timeout": 30, "decide_timeout": 45})
    assert response.status_code == 200
    assert client.get("/settings").json() == {"step_timeout": 30, "decide_timeout": 45}

    exported = client.get(f"/projects/{project_id}/export?format=yaml")
    assert exported.status_code == 200
    assert "starting point" in exported.text
    assert "finish" in exported.text
    assert "goals:" in exported.text
    assert client.get(f"/projects/{project_id}/export?format=invalid").status_code == 400


def test_decide_update_step_can_reprioritize_and_abandon(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "work", "creator": "decider"},
    )

    response = client.post(f"/projects/{project_id}/steps/s001/update", json={"priority": 9})
    assert response.status_code == 200
    assert response.json()["priority"] == 9

    response = client.post(
        f"/projects/{project_id}/steps/s001/update",
        json={"status": "abandoned", "note": "exhausted"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "abandoned"
    assert response.json()["note"] == "exhausted"

    assert client.post(f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "w"}).status_code == 409


def test_expired_step_and_decide_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "work", "creator": "worker-a", "worker": "worker-a"},
    )
    client.post(
        f"/projects/{project_id}/decide/claim",
        json={"worker": "worker-a", "trigger": "initial"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE steps SET last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE project_id = ?",
            (project_id,),
        )
        conn.execute(
            "UPDATE projects SET decide_last_heartbeat_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (project_id,),
        )

    response = client.post(f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "worker-b"})
    assert response.status_code == 200
    assert response.json()["worker"] == "worker-b"

    response = client.post(
        f"/projects/{project_id}/decide/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )
    assert response.status_code == 200
    assert response.json()["decide"]["worker"] == "worker-b"


def test_live_decide_lease_rejects_competing_worker(client: TestClient) -> None:
    project_id = _create_project(client)
    assert client.post(
        f"/projects/{project_id}/decide/claim",
        json={"worker": "worker-a", "trigger": "initial"},
    ).status_code == 200

    response = client.post(
        f"/projects/{project_id}/decide/claim",
        json={"worker": "worker-b", "trigger": "facts:2->3"},
    )

    assert response.status_code == 409
    assert "worker-a" in response.json()["detail"]


def test_root_goal_cannot_be_closed_directly(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/goals/g001/close",
        json={"status": "achieved", "worker": "decider"},
    )

    assert response.status_code == 409


def test_project_summary_exposes_fgs_counts(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(f"/projects/{project_id}/steps", json={"from": ["origin"], "description": "w", "creator": "d"})
    client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={"worker": "d", "facts": [{"description": "f"}], "findings": [{"kind": "k", "description": "d"}]},
    )

    summary = client.get("/projects").json()[0]
    assert summary["fact_count"] == 2
    assert summary["goal_count"] == 1
    assert summary["open_goal_count"] == 1
    assert summary["step_count"] == 1
    assert summary["pending_step_count"] == 0
    assert summary["finding_count"] == 1
    assert summary["hint_count"] == 1
