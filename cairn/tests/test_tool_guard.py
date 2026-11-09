"""Command guard: rule evaluation, human approval and the audit trail."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cairn.server import db


def _project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "eng", "origin": "starting point", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _check(client: TestClient, project_id: str, command: str, tool: str = "bash"):
    return client.post(
        f"/internal/projects/{project_id}/tool-guard/check",
        json={"tool": tool, "command": command, "worker": "local-pi"},
    )


def _add_rule(
    *,
    action: str,
    pattern: str,
    match_type: str = "contains",
    target: str = "command",
    priority: int = 50,
    timeout_seconds: int = 60,
    timeout_action: str = "deny",
) -> int:
    with db.get_conn() as conn:
        cursor = conn.execute(
            """
            INSERT INTO tool_rules (
                target, match_type, pattern, action, priority, message,
                timeout_seconds, timeout_action, enabled
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
            """,
            (
                target,
                match_type,
                pattern,
                action,
                priority,
                "测试规则",
                timeout_seconds,
                timeout_action,
            ),
        )
        return int(cursor.lastrowid or 0)


def test_ordinary_command_is_allowed_and_audited(client: TestClient) -> None:
    project_id = _project(client)

    response = _check(client, project_id, "nmap -sV 10.0.0.5")

    assert response.status_code == 200
    assert response.json() == {"decision": "allow"}

    audit = client.get(f"/projects/{project_id}/tool-audit").json()["entries"]
    assert audit[0]["decision"] == "allow"
    assert audit[0]["decision_source"] == "default"
    assert audit[0]["digest"]


def test_builtin_deny_rule_blocks_and_names_the_platform(client: TestClient) -> None:
    project_id = _project(client)

    response = _check(client, project_id, "rm -rf /")

    assert response.status_code == 200
    body = response.json()
    assert body["decision"] == "deny"
    # The wording must not read like the target defended itself.
    assert body["reason"].startswith("【Cairn 平台管控·非目标防御】")
    # The human-readable message, not the numeric priority, must reach the reason.
    assert "递归删除" in body["reason"]


def test_seeded_rules_keep_priority_and_message_apart(client: TestClient) -> None:
    _project(client)

    with db.get_conn() as conn:
        rows = conn.execute("SELECT priority, message FROM tool_rules").fetchall()

    assert rows
    for row in rows:
        assert isinstance(row["priority"], int)
        assert row["message"] and not str(row["message"]).isdigit()


def test_scope_rule_blocks_government_domains(client: TestClient) -> None:
    project_id = _project(client)

    body = _check(client, project_id, "curl -s http://www.example.gov.cn/").json()

    assert body["decision"] == "deny"


def test_ask_rule_parks_the_command_until_a_human_decides(client: TestClient) -> None:
    project_id = _project(client)
    _add_rule(action="ask", pattern="sqlmap", priority=200)

    body = _check(client, project_id, "sqlmap -u http://target/login.php").json()
    assert body["decision"] == "ask"
    assert body["timeout_action"] == "deny"
    approval_id = body["approval_id"]

    pending = client.get(f"/projects/{project_id}/tool-approvals").json()["approvals"]
    assert [item["id"] for item in pending] == [approval_id]

    decided = client.post(
        f"/projects/{project_id}/tool-approvals/{approval_id}/decide",
        json={"approve": False, "actor": "alice", "note": "越界"},
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "denied"
    assert decided.json()["decided_by"] == "alice"

    audit = client.get(f"/projects/{project_id}/tool-audit").json()["entries"]
    manual = [item for item in audit if item["decision_source"] == "manual"]
    assert manual and manual[0]["actor"] == "alice"
    assert manual[0]["decision"] == "deny"


def test_approving_lets_the_command_through(client: TestClient) -> None:
    project_id = _project(client)
    _add_rule(action="ask", pattern="hydra", priority=200)

    approval_id = _check(client, project_id, "hydra -l admin -P pass.txt ssh://target").json()[
        "approval_id"
    ]
    decided = client.post(
        f"/projects/{project_id}/tool-approvals/{approval_id}/decide",
        json={"approve": True, "actor": "bob"},
    )

    assert decided.json()["status"] == "allowed"


def test_stale_approval_expires_to_the_rule_action(client: TestClient) -> None:
    project_id = _project(client)
    _add_rule(action="ask", pattern="nikto", timeout_seconds=1)
    approval_id = _check(client, project_id, "nikto -host target").json()["approval_id"]

    # Age the row instead of sleeping: the next check sweeps the queue.
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE tool_approvals SET created_at = '2000-01-01T00:00:00Z' WHERE id = ?",
            (approval_id,),
        )

    _check(client, project_id, "nmap -sV target")

    row = client.get(f"/projects/{project_id}/tool-approvals/{approval_id}").json()
    assert row["status"] == "timeout"
    audit = client.get(f"/projects/{project_id}/tool-audit").json()["entries"]
    assert any(item["decision_source"] == "timeout" for item in audit)


def test_duplicate_decision_is_rejected(client: TestClient) -> None:
    project_id = _project(client)
    _add_rule(action="ask", pattern="wfuzz")
    approval_id = _check(client, project_id, "wfuzz -c -w list.txt http://target/FUZZ").json()[
        "approval_id"
    ]

    first = client.post(
        f"/projects/{project_id}/tool-approvals/{approval_id}/decide", json={"approve": True}
    )
    second = client.post(
        f"/projects/{project_id}/tool-approvals/{approval_id}/decide", json={"approve": False}
    )

    assert first.status_code == 200
    assert second.status_code == 409


def test_a_forbidden_tld_in_a_file_name_is_not_a_forbidden_host(client: TestClient) -> None:
    project_id = _project(client)

    body = _check(client, project_id, "curl -s http://example.com/files/report.edu.txt").json()

    assert body["decision"] == "allow"


def test_a_government_host_is_denied_without_the_scheme(client: TestClient) -> None:
    project_id = _project(client)

    body = _check(client, project_id, "ping www.mit.edu").json()

    assert body["decision"] == "deny"


def test_an_ask_rule_is_audited_before_a_human_decides(client: TestClient) -> None:
    project_id = _project(client)
    _add_rule(action="ask", pattern="sqlmap", priority=200)

    assert (
        _check(client, project_id, "sqlmap -u http://target/login.php").json()["decision"] == "ask"
    )

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT decision, decision_source, approval_id FROM tool_audit WHERE project_id = ?"
            " ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
    assert row["decision"] == "ask"
    assert row["decision_source"] == "rule"
    assert row["approval_id"]


def test_the_audit_links_the_ask_to_the_human_verdict(client: TestClient) -> None:
    """Two rows describe one command; the approval id is what pairs them up."""
    project_id = _project(client)
    _add_rule(action="ask", pattern="sqlmap", priority=200)

    approval_id = _check(client, project_id, "sqlmap -u http://target/x").json()["approval_id"]
    decided = client.post(
        f"/projects/{project_id}/tool-approvals/{approval_id}/decide",
        json={"approve": False, "actor": "alice"},
    )
    assert decided.status_code == 200

    entries = client.get(f"/projects/{project_id}/tool-audit").json()["entries"]
    linked = [item for item in entries if item["approval_id"] == approval_id]
    assert [item["decision"] for item in linked] == ["deny", "ask"]
    assert {item["decision_source"] for item in linked} == {"rule", "manual"}


def test_a_url_after_an_equals_sign_is_still_a_target(client: TestClient) -> None:
    """`--url=https://x.gov` is how many tools are actually invoked."""
    project_id = _project(client)

    body = _check(client, project_id, "curl --url=https://www.example.gov.cn/").json()

    assert body["decision"] == "deny"


def test_an_equals_sign_only_opens_a_url_not_a_file_name() -> None:
    from cairn.server.services.hosts import host_candidates

    assert host_candidates("curl --url=https://www.example.gov/") == ["www.example.gov"]
    assert host_candidates("curl --output=report.txt http://example.com/") == ["example.com"]


def test_expire_due_still_sweeps_a_row_with_a_short_budget(client: TestClient) -> None:
    from cairn.server.services.tool_guard import expire_due

    project_id = _project(client)
    _add_rule(action="ask", pattern="sqlmap", priority=200, timeout_seconds=5)
    assert _check(client, project_id, "sqlmap -u http://target/x").json()["decision"] == "ask"

    with db.get_conn() as conn:
        conn.execute("UPDATE tool_approvals SET created_at = '2000-01-01T00:00:00Z'")
        assert expire_due(conn) == 1
