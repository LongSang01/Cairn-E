"""Task-level tool rules and the host allowlist (ARTEX task_intercept_rules / asset gate)."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cairn.server import db


def _project(client: TestClient, title: str = "eng") -> str:
    response = client.post(
        "/projects", json={"title": title, "origin": "starting point", "goal": "finish"}
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _check(client: TestClient, project_id: str, command: str):
    return client.post(
        f"/projects/{project_id}/tool-guard/check",
        json={"tool": "bash", "command": command, "worker": "local-pi"},
    )


def _rule(
    client: TestClient,
    project_id: str,
    *,
    target: str = "command",
    action: str = "deny",
    pattern: str = "nmap",
    match_type: str = "contains",
    priority: int = 50,
) -> dict:
    response = client.post(
        f"/projects/{project_id}/tool-rules",
        json={
            "target": target,
            "match_type": match_type,
            "pattern": pattern,
            "action": action,
            "priority": priority,
            "message": "任务级规则",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_project_rule_only_applies_to_its_project(client: TestClient) -> None:
    a, b = _project(client, "a"), _project(client, "b")
    _rule(client, a, pattern="nmap")

    assert _check(client, a, "nmap -sV 10.0.0.5").json()["decision"] == "deny"
    assert _check(client, b, "nmap -sV 10.0.0.5").json() == {"decision": "allow"}
    assert len(client.get(f"/projects/{a}/tool-rules").json()["rules"]) == 1
    assert client.get(f"/projects/{b}/tool-rules").json() == {"rules": []}


def test_global_deny_cannot_be_lifted_by_a_project_allow(client: TestClient) -> None:
    pid = _project(client)
    # Seed a platform-wide deny; a task-level allow must not become a bypass.
    with db.get_conn() as conn:
        conn.execute(
            """
            INSERT INTO tool_rules (
                target, match_type, pattern, action, priority, message,
                timeout_seconds, timeout_action, enabled, project_id
            ) VALUES ('command', 'contains', 'hydra', 'deny', 100, '平台禁止', 60, 'deny', 1, NULL)
            """
        )
    _rule(client, pid, action="allow", pattern="hydra", priority=999)

    assert _check(client, pid, "hydra -l admin -P words.txt").json()["decision"] == "deny"


def test_host_allowlist_gates_out_of_scope_commands(client: TestClient) -> None:
    pid = _project(client)
    _rule(client, pid, target="host", action="allow", pattern="example.com", match_type="exact")
    _rule(client, pid, target="host", action="allow", pattern="10.0.0.5", match_type="exact")

    assert _check(client, pid, "curl -i https://example.com/login").json() == {
        "decision": "allow"
    }
    assert _check(client, pid, "nmap -sV 10.0.0.5").json() == {"decision": "allow"}

    out = _check(client, pid, "curl -i https://out-of-scope.example.org/").json()
    assert out["decision"] == "deny"
    assert "不在本次测试范围" in out["reason"]
    # A command with no host at all is ordinary local reconnaissance.
    assert _check(client, pid, "whoami").json() == {"decision": "allow"}
    # File names that look like domains must not be mistaken for out-of-scope hosts.
    assert _check(client, pid, "cat /var/log/app.log").json() == {"decision": "allow"}
    assert _check(client, pid, "ls ./report.json").json() == {"decision": "allow"}


def test_no_allowlist_means_no_host_gating(client: TestClient) -> None:
    pid = _project(client)
    _rule(client, pid, target="host", action="deny", pattern="bad.example.com")
    assert _check(client, pid, "curl -i https://other.example.org/").json() == {
        "decision": "allow"
    }
    assert _check(client, pid, "curl -i https://bad.example.com/").json()["decision"] == "deny"


def test_project_rule_crud_and_global_rules_are_untouchable(client: TestClient) -> None:
    pid = _project(client)
    rule = _rule(client, pid, pattern="sqlmap")
    assert rule["project_id"] == pid
    assert _check(client, pid, "sqlmap -u http://x --dump").json()["decision"] == "deny"

    assert client.delete(f"/projects/{pid}/tool-rules/{rule['id']}").status_code == 204
    assert _check(client, pid, "sqlmap -u http://x --dump").json() == {"decision": "allow"}
    assert client.delete(f"/projects/{pid}/tool-rules/{rule['id']}").status_code == 404

    with db.get_conn() as conn:
        global_id = conn.execute(
            "SELECT id FROM tool_rules WHERE project_id IS NULL LIMIT 1"
        ).fetchone()["id"]
    # Deleting a platform-wide rule through a project route must never work.
    assert client.delete(f"/projects/{pid}/tool-rules/{global_id}").status_code == 404
    with db.get_conn() as conn:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM tool_rules WHERE id = ?", (global_id,)
        ).fetchone()["n"] == 1


def test_invalid_rule_request_is_rejected(client: TestClient) -> None:
    pid = _project(client)
    for body in (
        {"target": "unknown", "match_type": "contains", "pattern": "x", "action": "deny"},
        {"target": "command", "match_type": "contains", "pattern": "  ", "action": "deny"},
        {"target": "command", "match_type": "contains", "pattern": "x", "action": "block"},
    ):
        assert client.post(f"/projects/{pid}/tool-rules", json=body).status_code == 422
