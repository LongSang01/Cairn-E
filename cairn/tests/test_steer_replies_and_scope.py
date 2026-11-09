"""Chat replies, in-flight steer delivery and task-level scope."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from cairn.contracts.models import Steer
from cairn.dispatcher.constraints import render_scope_block
from cairn.dispatcher.output_validation import validate_decide_payload
from cairn.dispatcher.tasks.decide import _settle_steers
from cairn.dispatcher.workers.pi_guard import EXTENSION_SOURCE
from cairn.server import db


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects", json={"title": "eng", "origin": "starting point", "goal": "finish"}
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _steer(client: TestClient, project_id: str, text: str) -> str:
    response = client.post(
        f"/projects/{project_id}/steers", json={"text": text, "creator": "Human"}
    )
    assert response.status_code == 201
    return response.json()["id"]


def _check(client: TestClient, project_id: str, command: str, worker: str = "worker-a"):
    return client.post(
        f"/projects/{project_id}/tool-guard/check",
        json={"tool": "bash", "command": command, "worker": worker},
    ).json()


# --- chat replies ---------------------------------------------------------------


def test_steer_can_be_answered_once_and_replied_text_is_stable(client: TestClient) -> None:
    pid = _project(client)
    sid = _steer(client, pid, "不要碰 10.0.0.9")
    assert client.get(f"/projects/{pid}/steers").json()[0]["answer"] is None

    first = client.post(
        f"/projects/{pid}/steers/{sid}/answer", json={"text": "已停止该目标的探测。"}
    )
    assert first.status_code == 200
    assert first.json()["answer"] == "已停止该目标的探测。"
    assert first.json()["answered_at"] is not None

    # Retried Decide runs must not overwrite the first answer.
    second = client.post(f"/projects/{pid}/steers/{sid}/answer", json={"text": "另一句回复"})
    assert second.json()["answer"] == "已停止该目标的探测。"
    assert (
        client.post(f"/projects/{pid}/steers/missing/answer", json={"text": "x"}).status_code == 404
    )


def test_validation_accepts_steer_replies_in_any_payload_kind() -> None:
    payload = {
        "accepted": True,
        "data": {"steers": {"reply": [{"id": "st001", "text": "已停止。"}]}},
    }
    kind, _, lookups = validate_decide_payload(
        payload,
        open_steps_empty=True,
        max_steps=3,
        fact_ids=["f001"],
        goal_ids=["g001"],
        active_sub_goal_ids=[],
        step_ids=[],
    )
    assert kind == "noop"
    assert lookups["steer_replies"] == [{"id": "st001", "text": "已停止。"}]


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def answer_steer(self, project_id, steer_id, *, text):
        self.calls.append((project_id, steer_id, text))
        return type("R", (), {"ok": True, "status_code": 200})()


def test_settle_steers_prefers_model_reply_and_falls_back_to_facts() -> None:
    client = _FakeClient()
    steers = [
        Steer(id="st001", text="停止探测", creator="Human", created_at="now"),
        Steer(id="st002", text="只做只读", creator="Human", created_at="now"),
    ]
    _settle_steers(
        client,
        "proj_001",
        steers,
        {"steer_replies": [{"id": "st001", "text": "已取消 s003。"}]},
        applied={"constraints_added": 1, "steps_updated": 2},
    )
    assert client.calls[0][2] == "已取消 s003。"
    assert "新增约束 1" in client.calls[1][2]
    assert "调整步骤 2" in client.calls[1][2]
    assert "已完成" not in client.calls[1][2]


# --- in-flight delivery ---------------------------------------------------------


def test_steer_is_delivered_once_per_worker_at_its_next_tool_call(client: TestClient) -> None:
    pid = _project(client)
    _steer(client, pid, "停止对 10.0.0.9 的所有测试")

    first = _check(client, pid, "nmap -sV 10.0.0.5", worker="worker-a")
    assert first["decision"] == "allow"
    assert len(first["steers"]) == 1
    assert first["steers"][0]["text"] == "停止对 10.0.0.9 的所有测试"

    # The receipt prevents the same instruction from blocking every later call.
    assert _check(client, pid, "whoami", worker="worker-a") == {"decision": "allow"}
    # Another worker still has to see it.
    other = _check(client, pid, "whoami", worker="worker-b")
    assert len(other["steers"]) == 1
    # A new steer is delivered even to a worker that already saw the first one.
    _steer(client, pid, "只做只读收集")
    again = _check(client, pid, "whoami", worker="worker-a")
    assert [s["text"] for s in again["steers"]] == ["只做只读收集"]


def test_pi_extension_injects_steers_by_blocking_one_call() -> None:
    assert "verdict.steers" in EXTENSION_SOURCE
    assert "Cairn 用户新指令" in EXTENSION_SOURCE
    assert "if (steerText) return blocked(steerText);" in EXTENSION_SOURCE


# --- task-level scope -----------------------------------------------------------


def test_scope_crud_and_cidr_gate(client: TestClient) -> None:
    pid = _project(client)
    for body in (
        {"kind": "cidr", "value": "10.0.0.0/24"},
        {"kind": "domain", "value": "example.com"},
        {"kind": "ip", "value": "192.168.1.10"},
    ):
        assert client.post(f"/projects/{pid}/scope", json=body).status_code == 201
    entries = client.get(f"/projects/{pid}/scope").json()["entries"]
    assert len(entries) == 3
    assert client.get(f"/projects/{pid}").json()["scope"] == entries

    assert _check(client, pid, "nmap -sV 10.0.0.5") == {"decision": "allow"}
    assert _check(client, pid, "curl -i https://api.example.com/v1") == {"decision": "allow"}
    denied = _check(client, pid, "nmap -sV 10.0.1.5")
    assert denied["decision"] == "deny"
    assert "不在本次测试范围" in denied["reason"]
    # Local commands and file paths are never scope-blocked.
    assert _check(client, pid, "cat /var/log/app.log") == {"decision": "allow"}


def test_scope_check_reports_hosts_and_gate_state(client: TestClient) -> None:
    pid = _project(client)
    before = client.post(f"/projects/{pid}/scope/check", json={"command": "ping 8.8.8.8"}).json()
    assert before["gate"] is False
    assert before["decision"] == "allow"
    assert before["hosts"] == ["8.8.8.8"]

    client.post(f"/projects/{pid}/scope", json={"kind": "ip", "value": "8.8.8.8"})
    after = client.post(
        f"/projects/{pid}/scope/check", json={"command": "ping 8.8.8.8 && curl 9.9.9.9"}
    ).json()
    assert after["gate"] is True
    assert after["decision"] == "deny"
    assert after["out_of_scope"] == ["9.9.9.9"]


def test_scope_is_project_scoped_and_validated(client: TestClient) -> None:
    a, b = _project(client), _project(client)
    entry = client.post(f"/projects/{a}/scope", json={"kind": "domain", "value": "example.com"})
    assert entry.status_code == 201
    assert _check(client, a, "curl 9.9.9.9")["decision"] == "deny"
    assert _check(client, b, "curl 9.9.9.9") == {"decision": "allow"}
    assert (
        client.post(f"/projects/{a}/scope", json={"kind": "cidr", "value": "broken"}).status_code
        == 422
    )
    assert (
        client.post(
            f"/projects/{a}/scope", json={"kind": "domain", "value": "example.com"}
        ).status_code
        == 409
    )
    assert client.delete(f"/projects/{a}/scope/{entry.json()['id']}").status_code == 204
    assert _check(client, a, "curl 9.9.9.9") == {"decision": "allow"}


def test_scope_and_tool_rule_gates_both_apply(client: TestClient) -> None:
    pid = _project(client)
    client.post(f"/projects/{pid}/scope", json={"kind": "domain", "value": "example.com"})
    client.post(
        f"/projects/{pid}/tool-rules",
        json={
            "target": "host",
            "match_type": "exact",
            "pattern": "api.example.com",
            "action": "allow",
        },
    )
    assert _check(client, pid, "curl https://api.example.com/v1") == {"decision": "allow"}
    # Allowed by the scope domain suffix but not by the narrower tool rule.
    assert _check(client, pid, "curl https://www.example.com/")["decision"] == "deny"


def test_render_scope_block_tells_the_planner_the_gate_is_on() -> None:
    assert "未设范围白名单" in render_scope_block([])
    block = render_scope_block([{"kind": "cidr", "value": "10.0.0.0/24"}])
    assert "10.0.0.0/24" in block
    assert "会被平台拦截" in block


def test_migration_007_adds_reply_columns_to_existing_steers_table(client: TestClient) -> None:
    pid = _project(client)
    with db.get_conn() as conn:
        columns = db._table_columns(conn, "steers")
    assert {"answer", "answered_at"} <= set(columns)
    assert client.get(f"/projects/{pid}/steers").json() == []


# --- cleanup regressions --------------------------------------------------------


def test_delivery_receipt_is_scoped_by_session_and_skips_answered_steers(
    client: TestClient,
) -> None:
    pid = _project(client)
    _steer(client, pid, "只做只读收集")

    first = client.post(
        f"/projects/{pid}/tool-guard/check",
        json={"tool": "bash", "command": "whoami", "worker": "w1", "session": "s001"},
    ).json()
    assert len(first["steers"]) == 1
    # Same worker + same run: no repeat.
    again = client.post(
        f"/projects/{pid}/tool-guard/check",
        json={"tool": "bash", "command": "whoami", "worker": "w1", "session": "s001"},
    )
    assert again.json() == {"decision": "allow"}
    # A new run of the same worker has a new session and still sees an unanswered
    # instruction (the pi extension now stamps CFG.session_id per run).
    new_run = client.post(
        f"/projects/{pid}/tool-guard/check",
        json={"tool": "bash", "command": "whoami", "worker": "w1", "session": "s002"},
    ).json()
    assert len(new_run["steers"]) == 1

    sid = client.get(f"/projects/{pid}/steers").json()[0]["id"]
    client.post(f"/projects/{pid}/steers/{sid}/answer", json={"text": "已落实。"})
    answered = client.post(
        f"/projects/{pid}/tool-guard/check",
        json={"tool": "bash", "command": "whoami", "worker": "w1", "session": "s003"},
    ).json()
    assert "steers" not in answered


def test_answer_steer_is_idempotent_under_sequential_and_concurrent_retries(
    client: TestClient,
) -> None:
    pid = _project(client)
    sid = _steer(client, pid, "停止探测")
    url = f"/projects/{pid}/steers/{sid}/answer"

    def post(text: str):
        return client.post(url, json={"text": text})

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(post, ["第一句", "第二句"]))
    assert all(response.status_code == 200 for response in results)
    answers = {response.json()["answer"] for response in results}
    # The conditional UPDATE keeps whichever row landed first; both concurrent
    # callers must observe that same answer, and no later write may win.
    assert len(answers) == 1
    final = client.get(f"/projects/{pid}/steers").json()[0]["answer"]
    assert final in answers
    assert client.post(url, json={"text": "再来一次"}).json()["answer"] == final


def test_planner_cannot_declare_an_allow_constraint(client: TestClient) -> None:
    pid = _project(client)
    assert (
        client.post(
            f"/projects/{pid}/constraints",
            json={"text": "扩大范围", "kind": "allow", "origin": "planner"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"/projects/{pid}/constraints",
            json={"text": "收紧范围", "kind": "deny", "origin": "planner"},
        ).status_code
        == 201
    )


def test_platform_host_allow_rule_does_not_gate_every_project(client: TestClient) -> None:
    a, b = _project(client), _project(client)
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tool_rules (
                target, match_type, pattern, action, priority, message,
                timeout_seconds, timeout_action, enabled, project_id
            ) VALUES ('host', 'exact', 'example.com', 'allow', 50, NULL, 60, 'deny', 1, NULL)
            """
        )
    assert _check(client, a, "curl https://other.example.org/") == {"decision": "allow"}
    assert _check(client, b, "curl https://other.example.org/") == {"decision": "allow"}
    client.post(
        f"/projects/{a}/tool-rules",
        json={"target": "host", "match_type": "exact", "pattern": "example.com", "action": "allow"},
    )
    # Only project a opted into an allowlist.
    assert _check(client, a, "curl https://other.example.org/")["decision"] == "deny"
    assert _check(client, b, "curl https://other.example.org/") == {"decision": "allow"}


def test_ipv6_targets_are_visible_to_the_scope_gate(client: TestClient) -> None:
    pid = _project(client)
    client.post(f"/projects/{pid}/scope", json={"kind": "cidr", "value": "2001:db8::/32"})
    assert _check(client, pid, "curl http://[2001:db8::1]/") == {"decision": "allow"}
    denied = _check(client, pid, "curl http://[2001:dead::1]/")
    assert denied["decision"] == "deny"


def test_scope_check_is_a_pure_dry_run(client: TestClient) -> None:
    pid = _project(client)
    client.post(f"/projects/{pid}/scope", json={"kind": "domain", "value": "example.com"})
    result = client.post(f"/projects/{pid}/scope/check", json={"command": "curl 9.9.9.9"}).json()
    assert result["decision"] == "deny"
    assert result["out_of_scope"] == ["9.9.9.9"]
    assert result["gate"] is True
    # No approval row and no audit row: a dry run never changes server state.
    with db.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM tool_approvals").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM tool_audit").fetchone()["n"] == 0
    assert client.post(f"/projects/{pid}/scope/check", json={}).status_code == 422


def test_deleting_a_project_removes_its_task_rules(client: TestClient) -> None:
    pid = _project(client)
    client.post(
        f"/projects/{pid}/tool-rules",
        json={"target": "command", "match_type": "contains", "pattern": "nmap", "action": "deny"},
    )
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM tool_rules WHERE project_id = ?", (pid,)
            ).fetchone()["n"]
            == 1
        )
    assert client.delete(f"/projects/{pid}").status_code in (200, 204)
    with db.get_conn() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS n FROM tool_rules WHERE project_id = ?", (pid,)
            ).fetchone()["n"]
            == 0
        )


def test_exports_carry_constraints_steers_replies_and_scope(client: TestClient) -> None:
    pid = _project(client)
    client.post(f"/projects/{pid}/constraints", json={"text": "不要增删改查"})
    sid = _steer(client, pid, "停止对 10.0.0.9 的测试")
    client.post(f"/projects/{pid}/steers/{sid}/answer", json={"text": "已停止。"})
    client.post(f"/projects/{pid}/scope", json={"kind": "cidr", "value": "10.0.0.0/24"})

    exported = client.get(f"/projects/{pid}/export?format=yaml")
    assert exported.status_code == 200
    body = exported.text
    assert "不要增删改查" in body
    assert "停止对 10.0.0.9 的测试" in body
    assert "已停止。" in body
    assert "10.0.0.0/24" in body

    timeline = client.get(f"/projects/{pid}/export?format=timeline").text
    assert "CONSTRAINT FORBIDDEN" in timeline
    assert "STEER ANSWER" in timeline
    assert "SCOPE ENTRY" in timeline
