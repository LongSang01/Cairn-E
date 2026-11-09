from __future__ import annotations

import json

import pytest
import yaml
from fastapi.testclient import TestClient

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
    assert payload["facts"] == [{"id": "origin", "description": "starting point", "title": None}]
    assert payload["goals"][0]["id"] == "g001"
    assert payload["goals"][0]["parent"] is None
    return payload["project"]["id"]


def test_project_workflow_creates_goal_step_finding_completes_and_reopens(
    client: TestClient,
) -> None:
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
            "asset": "  10.0.0.1:8080  ",
            "priority": 5,
            "creator": "decider",
            "worker": None,
        },
    )
    assert response.status_code == 201
    assert response.json()["id"] == "s001"
    assert response.json()["from"] == ["origin"]
    assert response.json()["asset"] == "10.0.0.1:8080"
    assert response.json()["priority"] == 5

    response = client.post(
        f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "executor"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"

    response = client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [{"description": "new fact"}],
            "findings": [{"kind": "flaw", "severity": "high", "description": "a product"}],
        },
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["facts"] == [{"id": "f001", "description": "new fact", "title": None}]
    assert payload["findings"][0]["kind"] == "flaw"
    assert payload["findings"][0]["severity"] == "high"
    assert payload["findings"][0]["step"] == "s001"
    assert payload["step"]["status"] == "done"
    assert payload["step"]["to"] == "f001"

    response = client.post(
        f"/projects/{project_id}/goals/g002/close", json={"status": "achieved", "worker": "decider"}
    )
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
    assert [d["severity"] for d in detail["findings"]] == ["high"]

    summary = client.get(f"/projects/{project_id}/findings/summary")
    assert summary.status_code == 200
    assert summary.json()["total"] == 1
    assert summary.json()["by_severity"] == {"high": 1}
    assert summary.json()["by_kind"] == {"flaw": 1}
    assert "findings" not in summary.json()

    report = client.get(f"/projects/{project_id}/export", params={"format": "findings"})
    assert report.status_code == 200
    assert "## High (1)" in report.text
    assert "**d001** [flaw]" in report.text

    response = client.post(
        f"/projects/{project_id}/reopen",
        json={"description": "human correction", "creator": "human"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["project"]["status"] == "active"
    assert payload["fact"] == {"id": "f002", "description": "human correction", "title": None}
    assert payload["goal"]["status"] == "active"

    detail = client.get(f"/projects/{project_id}").json()
    assert all(step["kind"] != "completion" for step in detail["steps"])


def test_complete_rejects_until_steps_and_sub_goals_are_closed(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/goals",
        json={"description": "stage one", "creator": "decider"},
    )
    assert response.status_code == 201
    sub_id = response.json()["id"]
    response = client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "goal": sub_id,
            "description": "investigate",
            "priority": 5,
            "creator": "decider",
            "worker": None,
        },
    )
    assert response.status_code == 201

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "premature", "worker": "decider"},
    )
    assert response.status_code == 409
    assert "still open" in response.text

    response = client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={"worker": "executor", "status": "done", "facts": [{"description": "result"}]},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "premature", "worker": "decider"},
    )
    assert response.status_code == 409
    assert "sub-goal" in response.text

    response = client.post(
        f"/projects/{project_id}/goals/{sub_id}/close",
        json={"status": "achieved", "worker": "decider"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "goal met", "worker": "decider"},
    )
    assert response.status_code == 200


def test_completion_waits_for_a_running_step_instead_of_dropping_it(client: TestClient) -> None:
    """The proj_008 finale: the last verification must conclude before the project ends.

    Decide wanted to abandon the running final verification and complete in one payload.
    The abandon is refused, so the executor lands its fact first and the project completes
    on the next round with the graph intact.
    """
    project_id = _create_project(client)
    sub_id = client.post(
        f"/projects/{project_id}/goals", json={"description": "verify", "creator": "decider"}
    ).json()["id"]
    for description, worker in (("redundant", None), ("final verification", "executor")):
        response = client.post(
            f"/projects/{project_id}/steps",
            json={
                "from": ["origin"],
                "goal": sub_id,
                "description": description,
                "creator": worker or "decider",
                "worker": worker,
            },
        )
        assert response.status_code == 201

    # A running step cannot be abandoned, so the project cannot complete yet.
    assert (
        client.post(
            f"/projects/{project_id}/steps/s002/update",
            json={"status": "abandoned", "note": "looks duplicated"},
        ).status_code
        == 409
    )
    assert (
        client.post(
            f"/projects/{project_id}/complete",
            json={"from": ["origin"], "description": "goal met", "worker": "decider"},
        ).status_code
        == 409
    )

    # The executor closes it with the fact it just confirmed; the pending step is abandonable.
    response = client.post(
        f"/projects/{project_id}/steps/s002/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [{"description": "rdp reachable with nihaoa/nihao123"}],
        },
    )
    assert response.status_code == 200
    assert response.json()["step"]["status"] == "done"
    assert response.json()["facts"][0]["description"] == "rdp reachable with nihaoa/nihao123"

    response = client.post(
        f"/projects/{project_id}/steps/s001/update",
        json={"status": "abandoned", "note": "covered by the verification fact"},
    )
    assert response.status_code == 200
    response = client.post(
        f"/projects/{project_id}/goals/{sub_id}/close",
        json={"status": "achieved", "worker": "decider"},
    )
    assert response.status_code == 200

    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["f001"], "description": "goal met", "worker": "decider"},
    )
    assert response.status_code == 200
    assert response.json()["project"]["status"] == "completed"


def test_step_update_with_only_a_note_stores_guidance(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "probe", "creator": "decider", "worker": None},
    )

    response = client.post(
        f"/projects/{project_id}/steps/s001/update",
        json={"note": "wait for the lockout window first"},
    )

    assert response.status_code == 200
    assert response.json()["note"] == "wait for the lockout window first"
    assert response.json()["priority"] == 0
    assert response.json()["status"] == "pending"


def test_titles_documents_and_recall_round_trip(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "description": "probe 6379",
            "title": "probe redis",
            "creator": "decider",
            "worker": None,
        },
    )
    assert response.status_code == 201
    assert response.json()["title"] == "probe redis"

    response = client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [
                {
                    "title": "redis reachable",
                    "description": "port 6379 answers PING",
                    "documents": [{"title": "nmap", "content": "PORT 6379 open"}],
                }
            ],
            "findings": [],
        },
    )
    assert response.status_code == 200
    assert response.json()["facts"][0]["title"] == "redis reachable"

    documents = client.get(f"/projects/{project_id}/documents").json()
    assert [document["title"] for document in documents] == ["nmap"]
    document_id = documents[0]["id"]
    assert (
        client.get(f"/projects/{project_id}/documents/{document_id}").json()["content"]
        == "PORT 6379 open"
    )

    # Recall reaches the fact through its title and links the stored evidence.
    recall = client.get(f"/projects/{project_id}/recall", params={"q": "redis"}).json()
    assert recall["facts"][0]["id"] == "f001"
    assert recall["facts"][0]["title"] == "redis reachable"
    assert recall["facts"][0]["doc_ids"] == [document_id]

    plan = {"queries": ["redis"], "doc_ids": [{"id": document_id, "from": 4_000}]}
    assert client.put(f"/projects/{project_id}/recall-plan", json=plan).json() == plan
    assert client.get(f"/projects/{project_id}/recall-plan").json() == plan

    # A bare id is not a valid read request: the plan carries structured entries.
    bare = client.put(
        f"/projects/{project_id}/recall-plan", json={"queries": [], "doc_ids": [document_id]}
    )
    assert bare.status_code == 422

    export = yaml.safe_load(client.get(f"/projects/{project_id}/export?format=yaml").text)
    facts = {fact["id"]: fact for fact in export["facts"]}
    assert facts["f001"]["title"] == "redis reachable"
    assert facts["f001"]["doc_ids"] == [document_id]
    assert export["steps"][0]["title"] == "probe redis"
    assert export["documents"][0]["id"] == document_id


def test_stopping_project_releases_claims_and_decide_but_keeps_hints_writable(
    client: TestClient,
) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "description": "work",
            "creator": "worker-a",
            "worker": "worker-a",
        },
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
    assert (
        client.post(
            f"/projects/{project_id}/hints",
            json={"content": "manual note", "creator": "human"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/projects/{project_id}/steps",
            json={
                "from": ["origin"],
                "description": "blocked",
                "creator": "decider",
                "worker": None,
            },
        ).status_code
        == 403
    )


def test_step_creation_rejects_unknown_fact_and_mismatched_initial_worker(
    client: TestClient,
) -> None:
    project_id = _create_project(client)

    assert (
        client.post(
            f"/projects/{project_id}/steps",
            json={"from": ["f999"], "description": "invalid", "creator": "decider", "worker": None},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/projects/{project_id}/steps",
            json={
                "from": ["origin"],
                "description": "invalid",
                "creator": "decider",
                "worker": "executor",
            },
        ).status_code
        == 400
    )


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

    assert (
        client.post(
            f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "w"}
        ).status_code
        == 409
    )


def test_running_step_cannot_be_abandoned_by_decide(client: TestClient) -> None:
    """A claimed step belongs to its executor: the mid-run result must not be dropped.

    proj_008: Decide abandoned the final verification while its executor was running,
    the run finished with exit code 0, its heartbeat hit 409 and the fact was never
    recorded, so the project could not be completed.
    """
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "description": "verify",
            "creator": "executor",
            "worker": "executor",
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "running"

    response = client.post(
        f"/projects/{project_id}/steps/s001/update",
        json={"status": "abandoned", "note": "duplicated"},
    )
    assert response.status_code == 409
    assert "running" in response.text

    # The owner still closes it with its result, which is what lets the graph move on.
    response = client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={"worker": "executor", "status": "done", "facts": [{"description": "verified"}]},
    )
    assert response.status_code == 200
    assert response.json()["step"]["status"] == "done"

    # A stale claim is reclaimed first, so an orphaned running step stays abandonable.
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "orphan", "creator": "w", "worker": "w"},
    )
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE steps SET last_heartbeat_at = '2000-01-01T00:00:00Z'"
            " WHERE id = 's002' AND project_id = ?",
            (project_id,),
        )
    response = client.post(
        f"/projects/{project_id}/steps/s002/update",
        json={"status": "abandoned", "note": "lease expired"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "abandoned"


def test_expired_step_and_decide_leases_can_be_reclaimed(client: TestClient) -> None:
    project_id = _create_project(client)
    client.post(
        f"/projects/{project_id}/steps",
        json={
            "from": ["origin"],
            "description": "work",
            "creator": "worker-a",
            "worker": "worker-a",
        },
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

    response = client.post(
        f"/projects/{project_id}/steps/s001/heartbeat", json={"worker": "worker-b"}
    )
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
    assert (
        client.post(
            f"/projects/{project_id}/decide/claim",
            json={"worker": "worker-a", "trigger": "initial"},
        ).status_code
        == 200
    )

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
    client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "w", "creator": "d"},
    )
    client.post(
        f"/projects/{project_id}/steps/s001/conclude",
        json={
            "worker": "d",
            "facts": [{"description": "f"}],
            "findings": [{"kind": "k", "description": "d"}],
        },
    )

    summary = client.get("/projects").json()[0]
    assert summary["fact_count"] == 2
    assert summary["goal_count"] == 1
    assert summary["open_goal_count"] == 1
    assert summary["step_count"] == 1
    assert summary["pending_step_count"] == 0
    assert summary["finding_count"] == 1
    assert summary["hint_count"] == 1


# --------------------------------------------------------------------- side questions


def _ask(client: TestClient, project_id: str, content: str = "what is this CVE?") -> dict:
    response = client.post(f"/projects/{project_id}/questions", json={"content": content})
    assert response.status_code == 201, response.text
    return response.json()


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


def test_question_can_be_claimed_then_answered_once(client: TestClient) -> None:
    project_id = _create_project(client)
    question = _ask(client, project_id, "  what is this CVE?  ")

    assert question["id"] == "q001"
    assert question["content"] == "what is this CVE?"
    assert question["status"] == "pending"
    assert question["answer"] is None
    assert question["started_at"] is None
    assert [q["id"] for q in client.get(f"/projects/{project_id}/questions").json()] == ["q001"]
    assert (
        client.get(f"/projects/{project_id}/questions/q001").json()["content"]
        == question["content"]
    )
    assert client.get(f"/projects/{project_id}/questions/q999").status_code == 404

    # An unanswered question has no worker that could report a result yet.
    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={"status": "answered", "answer": "CVE-2024-1 is a demo"},
    )
    assert response.status_code == 409

    response = client.post(
        f"/projects/{project_id}/questions/q001/claim", json={"worker": "pi-main"}
    )
    assert response.status_code == 200
    claimed = response.json()
    assert claimed["status"] == "running"
    assert claimed["worker"] == "pi-main"
    assert claimed["started_at"] is not None

    response = client.post(
        f"/projects/{project_id}/questions/q001/claim", json={"worker": "worker-b"}
    )
    assert response.status_code == 409

    # Only the claiming dispatcher may answer; anyone else is rejected.
    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={"status": "answered", "answer": "hijacked", "worker": "worker-b"},
    )
    assert response.status_code == 409
    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={"status": "answered", "answer": "anonymous"},
    )
    assert response.status_code == 409
    assert client.get(f"/projects/{project_id}/questions/q001").json()["status"] == "running"

    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={
            "status": "answered",
            "answer": "CVE-2024-1 is a demo",
            "worker": "pi-main",
            "duration_ms": 1234,
        },
    )
    assert response.status_code == 200
    answered = response.json()
    assert answered["status"] == "answered"
    assert answered["answer"] == "CVE-2024-1 is a demo"
    assert answered["duration_ms"] == 1234
    assert answered["answered_at"] is not None

    assert (
        client.post(
            f"/projects/{project_id}/questions/q001/answer",
            json={"status": "answered", "answer": "again"},
        ).status_code
        == 409
    )
    assert client.post(f"/projects/{project_id}/questions/q001/cancel").status_code == 409


def test_failed_answer_requires_an_error_and_closes_the_question(client: TestClient) -> None:
    project_id = _create_project(client)
    _ask(client, project_id)
    client.post(f"/projects/{project_id}/questions/q001/claim", json={"worker": "pi-main"})

    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={"status": "failed", "worker": "pi-main"},
    )
    assert response.status_code == 422

    response = client.post(
        f"/projects/{project_id}/questions/q001/answer",
        json={"status": "failed", "error": "worker timed out", "worker": "pi-main"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "failed"
    assert payload["error"] == "worker timed out"
    assert payload["answer"] is None


def test_cancel_and_clear_only_touch_settled_questions(client: TestClient) -> None:
    project_id = _create_project(client)
    _ask(client, project_id, "first")
    _ask(client, project_id, "second")

    response = client.post(f"/projects/{project_id}/questions/q001/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    assert response.json()["answered_at"] is not None

    # A pending question is still waiting for its worker, so clearing keeps it.
    assert client.delete(f"/projects/{project_id}/questions").json() == {"deleted": 1}
    remaining = client.get(f"/projects/{project_id}/questions").json()
    assert [(q["id"], q["status"]) for q in remaining] == [("q002", "pending")]

    client.post(f"/projects/{project_id}/questions/q002/cancel")
    assert client.delete(f"/projects/{project_id}/questions").json() == {"deleted": 1}
    assert client.get(f"/projects/{project_id}/questions").json() == []


def test_question_content_must_not_be_blank(client: TestClient) -> None:
    project_id = _create_project(client)

    assert (
        client.post(f"/projects/{project_id}/questions", json={"content": "   "}).status_code == 422
    )
    assert client.get("/projects/proj_999/questions").status_code == 404


def test_pending_questions_span_projects_and_honour_the_limit(client: TestClient) -> None:
    first = _create_project(client)
    second = _create_project(client)
    _ask(client, first, "oldest")
    _ask(client, second, "newest")

    pending = client.get("/questions/pending", params={"limit": 1}).json()
    assert [q["content"] for q in pending] == ["oldest"]
    assert [q["project_id"] for q in client.get("/questions/pending").json()] == [first, second]

    assert client.get("/questions/pending", params={"limit": 0}).status_code == 422
    assert client.get("/questions/pending", params={"limit": 51}).status_code == 422


def test_completed_project_rejects_new_questions(client: TestClient) -> None:
    project_id = _create_project(client)
    response = client.post(
        f"/projects/{project_id}/complete",
        json={"from": ["origin"], "description": "solved", "worker": "decider"},
    )
    assert response.status_code == 200

    response = client.post(f"/projects/{project_id}/questions", json={"content": "late"})
    assert response.status_code == 403


def test_question_stream_snapshots_history_and_ends_when_idle(client: TestClient) -> None:
    project_id = _create_project(client)
    _ask(client, project_id, "already settled")
    client.post(f"/projects/{project_id}/questions/q001/cancel")

    events = _sse_events(client, f"/projects/{project_id}/questions/stream")

    assert [name for name, _ in events] == ["snapshot", "end"]
    assert [q["status"] for q in events[0][1]["questions"]] == ["cancelled"]


# -------------------------------------------------------------- rolling graph summary


def test_context_summary_round_trip(client: TestClient) -> None:
    project_id = _create_project(client)

    response = client.get(f"/projects/{project_id}/context")
    assert response.status_code == 200
    assert response.json() == {
        "summary": "",
        "facts_covered": 0,
        "step_summary": "",
        "steps_covered": 0,
        "finding_summary": "",
        "findings_covered": 0,
    }

    stored = {
        "summary": "- db reachable (origin)",
        "facts_covered": 1,
        "step_summary": "- sqlmap against /login found nothing (s001)",
        "steps_covered": 1,
        "finding_summary": "- openssh 8.2 on 22/tcp (d001)",
        "findings_covered": 1,
    }
    response = client.put(f"/projects/{project_id}/context", json=stored)
    assert response.status_code == 200
    assert response.json() == stored
    assert client.get(f"/projects/{project_id}/context").json() == stored

    # A slow background summary must not roll a channel back to older coverage.
    response = client.put(
        f"/projects/{project_id}/context",
        json={"summary": "- stale text", "facts_covered": 0, "step_summary": "- stale"},
    )
    assert response.status_code == 200
    assert response.json() == stored

    # Newer coverage replaces the channel, and channels advance independently.
    response = client.put(
        f"/projects/{project_id}/context",
        json={"summary": "- db reachable, ssh open (origin)", "facts_covered": 2},
    )
    assert response.json()["summary"] == "- db reachable, ssh open (origin)"
    assert response.json()["facts_covered"] == 2
    assert response.json()["step_summary"] == stored["step_summary"]

    assert client.get("/projects/proj_999/context").status_code == 404
    assert (
        client.put(
            "/projects/proj_999/context", json={"summary": "- x", "facts_covered": 1}
        ).status_code
        == 404
    )


def test_healthz_reports_database_and_schema_version(client: TestClient) -> None:
    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["database"] == "ok"
    assert payload["schema_version"] >= 1


def test_project_detail_supports_conditional_get(client: TestClient) -> None:
    project_id = _create_project(client)

    first = client.get(f"/projects/{project_id}")
    assert first.status_code == 200
    tag = first.headers["etag"]
    assert tag.startswith('"')
    assert first.headers["cache-control"] == "no-cache"

    unchanged = client.get(f"/projects/{project_id}", headers={"If-None-Match": tag})
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == tag

    # A weak validator and a wildcard also match the current representation.
    assert (
        client.get(f"/projects/{project_id}", headers={"If-None-Match": f"W/{tag}"}).status_code
        == 304
    )
    assert client.get(f"/projects/{project_id}", headers={"If-None-Match": "*"}).status_code == 304

    # Mutating the graph changes the tag, so the stale copy is no longer reused.
    client.post(f"/projects/{project_id}/hints", json={"content": "new hint", "creator": "human"})
    changed = client.get(f"/projects/{project_id}", headers={"If-None-Match": tag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != tag


def test_project_list_supports_conditional_get(client: TestClient) -> None:
    _create_project(client)

    first = client.get("/projects")
    assert first.status_code == 200
    tag = first.headers["etag"]

    assert client.get("/projects", headers={"If-None-Match": tag}).status_code == 304

    _create_project(client)
    changed = client.get("/projects", headers={"If-None-Match": tag})
    assert changed.status_code == 200
    assert changed.headers["etag"] != tag


def test_an_exclusive_step_round_trips_and_requires_an_asset(client: TestClient) -> None:
    project_id = _create_project(client)

    created = client.post(
        f"/projects/{project_id}/steps",
        json={
            "description": "spray the login",
            "creator": "human",
            "from": ["origin"],
            "asset": "10.0.0.1:8080/login",
            "exclusive": True,
        },
    )
    assert created.status_code == 201
    assert created.json()["exclusive"] is True

    detail = client.get(f"/projects/{project_id}").json()
    assert detail["steps"][0]["exclusive"] is True

    # The YAML snapshot handed to a worker carries the flag too, so the graph it
    # reads matches what the scheduler decided.
    export = yaml.safe_load(client.get(f"/projects/{project_id}/export?format=yaml").text)
    assert export["steps"][0]["exclusive"] is True

    rejected = client.post(
        f"/projects/{project_id}/steps",
        json={"description": "spray", "creator": "human", "from": ["origin"], "exclusive": True},
    )
    assert rejected.status_code == 422
