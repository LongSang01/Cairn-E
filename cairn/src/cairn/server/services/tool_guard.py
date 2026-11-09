"""Command-level guard: allow / deny / ask before a worker runs a shell command.

The worker is an agent CLI in a container; Cairn never runs the command itself, so
the only place an interception can happen is inside that CLI. A pi extension posts
each `bash` call here and blocks on the answer, which makes this module the single
source of truth for the policy, for the human approval queue and for the audit log.

Rules are evaluated by descending priority, first match wins; with no match the
default is `allow`. That mirrors the ARTEX behaviour: only actions that no
legitimate task needs are denied out of the box, because over-blocking quietly
breaks the testing the platform exists for.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from uuid import uuid4

from .hosts import host_candidates
from .scope import host_in_scope, load_scope
from .steer_delivery import claim_steers
from .timeutil import utcnow

# Wording matters: an agent that reads a block as "the target defended itself" will
# retry with a bypass. This says the platform did it.
BLOCK_PREFIX = "【Cairn 平台管控·非目标防御】"
DEFAULT_ACTION = "allow"
ACTIONS = ("allow", "deny", "ask")


@dataclass(slots=True)
class GuardDecision:
    action: str
    reason: str | None
    rule_id: int | None
    source: str
    timeout_seconds: int = 60
    timeout_action: str = "deny"


def command_digest(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", "replace")).hexdigest()


def _matches(match_type: str, pattern: str, value: str) -> bool:
    if match_type == "exact":
        return value.strip() == pattern
    if match_type == "prefix":
        return value.strip().startswith(pattern)
    if match_type == "contains":
        return pattern in value
    if match_type == "regex":
        try:
            return re.search(pattern, value) is not None
        except re.error:
            # A broken rule must never take the guard down with it.
            return False
    return False


def _row_matches(row, tool: str, command: str) -> bool:
    target = row["target"]
    if target == "host":
        return any(
            _matches(row["match_type"], row["pattern"], host) for host in host_candidates(command)
        )
    if target not in ("tool", "command", "*"):
        return False
    value = command if target == "command" else tool
    return _matches(row["match_type"], row["pattern"], value)


def _decision_from(row) -> GuardDecision:
    action = row["action"] if row["action"] in ACTIONS else DEFAULT_ACTION
    message = row["message"] or f"命中规则 {row['match_type']}:{row['pattern']}"
    return GuardDecision(
        action=action,
        reason=f"{BLOCK_PREFIX}此调用被平台拦截。原因：{message}",
        rule_id=row["id"],
        source="rule",
        timeout_seconds=int(row["timeout_seconds"] or 60),
        timeout_action=row["timeout_action"] or "deny",
    )


def evaluate(
    conn: sqlite3.Connection, tool: str, command: str, project_id: str | None = None
) -> GuardDecision:
    """Global (platform-owned) rules plus this project's task-level rules.

    Precedence mirrors ARTEX's asset gate: a platform-wide deny/ask is a floor a
    project rule can never lift; project rules otherwise win over global allows;
    and `target=host, action=allow` rules form an optional allowlist — a command
    naming a host outside it is denied. With no allowlist nothing is gated, so
    adding one rule cannot accidentally block the whole task.
    """
    rows = conn.execute(
        "SELECT * FROM tool_rules WHERE enabled = 1 "
        "AND (project_id IS NULL OR project_id = ?) "
        "ORDER BY priority DESC, id ASC",
        (project_id,),
    ).fetchall()
    global_hard = [
        row for row in rows if row["project_id"] is None and row["action"] in ("deny", "ask")
    ]
    project_rules = [row for row in rows if row["project_id"] is not None]
    global_soft = [
        row for row in rows if row["project_id"] is None and row["action"] not in ("deny", "ask")
    ]

    # 1. Platform-wide prohibitions cannot be overridden by a project allow.
    for row in global_hard:
        if _row_matches(row, tool, command):
            return _decision_from(row)

    # 2. Optional host allowlists: task-level `project_scope` and tool rules with
    #    target=host, action=allow. Both gates must pass when both are configured.
    #    Explicit host deny/ask rules are handled in steps 1/3.
    # Only project rules open an allowlist. A platform-wide `host/allow` rule is
    # an ordinary permission, not a gate: letting it open the gate here would
    # deny unlisted hosts in every project on the platform.
    allow_hosts = [
        row for row in project_rules if row["target"] == "host" and row["action"] == "allow"
    ]
    scope = load_scope(conn, project_id) if project_id else []
    if allow_hosts or scope:
        for host in host_candidates(command):
            rule_ok = not allow_hosts or any(
                _matches(row["match_type"], row["pattern"], host) for row in allow_hosts
            )
            scope_ok = not scope or host_in_scope(host, scope)
            if not rule_ok or not scope_ok:
                return GuardDecision(
                    action="deny",
                    reason=f"{BLOCK_PREFIX}此调用被平台拦截。原因：目标 {host} 不在本次测试范围",
                    rule_id=None,
                    source="scope",
                )

    # 3. Project rules win over platform-wide allow rules.
    for row in project_rules:
        if row["target"] == "host" and row["action"] == "allow":
            continue  # a whitelist entry is not a verdict on its own
        if _row_matches(row, tool, command):
            return _decision_from(row)

    for row in global_soft:
        if _row_matches(row, tool, command):
            return _decision_from(row)

    return GuardDecision(action=DEFAULT_ACTION, reason=None, rule_id=None, source="default")


def _audit(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    session_id: str | None,
    worker: str | None,
    tool: str,
    command: str,
    decision: str,
    decision_source: str,
    rule_id: int | None,
    actor: str | None,
    enforced: bool = True,
) -> None:
    conn.execute(
        """
        INSERT INTO tool_audit (
            project_id, session_id, worker, tool, command, digest, decision,
            decision_source, rule_id, actor, enforced, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            session_id,
            worker,
            tool,
            command,
            command_digest(command),
            decision,
            decision_source,
            rule_id,
            actor,
            1 if enforced else 0,
            utcnow(),
        ),
    )


def check(
    conn: sqlite3.Connection,
    *,
    project_id: str,
    tool: str,
    command: str,
    worker: str | None = None,
    session_id: str | None = None,
    actor: str | None = None,
) -> dict[str, object]:
    """Evaluate one command; `ask` parks it in the approval queue.

    The response may also carry undelivered steers. The pi extension blocks that
    one tool call and returns them to the model, which is how a message reaches
    a worker that is already running rather than only the next Decide round.
    """
    decision = evaluate(conn, tool, command, project_id=project_id)
    pending_steers = claim_steers(conn, project_id=project_id, worker=worker, session_id=session_id)
    extra = {"steers": pending_steers} if pending_steers else {}
    if decision.action == "allow":
        _audit(
            conn,
            project_id=project_id,
            session_id=session_id,
            worker=worker,
            tool=tool,
            command=command,
            decision="allow",
            decision_source=decision.source,
            rule_id=decision.rule_id,
            actor=actor,
        )
        return {"decision": "allow", **extra}

    if decision.action == "deny":
        _audit(
            conn,
            project_id=project_id,
            session_id=session_id,
            worker=worker,
            tool=tool,
            command=command,
            decision="deny",
            decision_source=decision.source,
            rule_id=decision.rule_id,
            actor=actor,
        )
        return {"decision": "deny", "reason": decision.reason, **extra}

    approval_id = f"appr_{uuid4().hex[:12]}"
    now = utcnow()
    conn.execute(
        """
        INSERT INTO tool_approvals (
            id, project_id, session_id, worker, tool, command, digest, status, reason,
            rule_id, created_at, expires_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, NULL)
        """,
        (
            approval_id,
            project_id,
            session_id,
            worker,
            tool,
            command,
            command_digest(command),
            decision.reason,
            decision.rule_id,
            now,
        ),
    )
    return {
        "decision": "ask",
        "approval_id": approval_id,
        "reason": decision.reason,
        "timeout_seconds": decision.timeout_seconds,
        "timeout_action": decision.timeout_action,
        **extra,
    }


def get_approval(conn: sqlite3.Connection, project_id: str, approval_id: str):
    return conn.execute(
        "SELECT * FROM tool_approvals WHERE project_id = ? AND id = ?",
        (project_id, approval_id),
    ).fetchone()


def pending_approvals(conn: sqlite3.Connection, project_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tool_approvals WHERE project_id = ? AND status = 'pending'"
        " ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()


def decide(
    conn: sqlite3.Connection,
    project_id: str,
    approval_id: str,
    *,
    approve: bool,
    actor: str | None,
    note: str | None = None,
) -> sqlite3.Row:
    """Human verdict: record it, audit it, and let the extension unblock."""
    row = get_approval(conn, project_id, approval_id)
    if row is None:
        raise LookupError(approval_id)
    if row["status"] != "pending":
        raise ValueError(row["status"])

    status = "allowed" if approve else "denied"
    conn.execute(
        "UPDATE tool_approvals SET status = ?, decision_source = 'manual', decided_by = ?,"
        " decided_at = ?, reason = COALESCE(?, reason) WHERE id = ?",
        (status, actor, utcnow(), note, approval_id),
    )
    _audit(
        conn,
        project_id=project_id,
        session_id=row["session_id"],
        worker=row["worker"],
        tool=row["tool"],
        command=row["command"],
        decision="allow" if approve else "deny",
        decision_source="manual",
        rule_id=row["rule_id"],
        actor=actor,
    )
    return get_approval(conn, project_id, approval_id)


def expire_due(conn: sqlite3.Connection, *, timeout_seconds: int = 60) -> int:
    """Turn stale pending approvals into their rule's timeout action.

    Called on every guard check, so no background task is needed: the next command
    in the project sweeps the queue.
    """
    now = utcnow()
    rows = conn.execute(
        "SELECT * FROM tool_approvals WHERE status = 'pending' ORDER BY created_at"
    ).fetchall()
    expired = 0
    for row in rows:
        created = row["created_at"]
        limit = timeout_seconds
        if row["rule_id"] is not None:
            rule = conn.execute(
                "SELECT timeout_seconds FROM tool_rules WHERE id = ?", (row["rule_id"],)
            ).fetchone()
            if rule is not None:
                limit = int(rule["timeout_seconds"] or timeout_seconds)
        if _age_seconds(created, now) < limit:
            continue
        action = "deny"
        if row["rule_id"] is not None:
            rule = conn.execute(
                "SELECT timeout_action FROM tool_rules WHERE id = ?", (row["rule_id"],)
            ).fetchone()
            if rule is not None and rule["timeout_action"] == "allow":
                action = "allow"
        conn.execute(
            "UPDATE tool_approvals SET status = 'timeout', decision_source = 'timeout',"
            " decided_at = ?, reason = COALESCE(reason, '审批超时') WHERE id = ?",
            (now, row["id"]),
        )
        _audit(
            conn,
            project_id=row["project_id"],
            session_id=row["session_id"],
            worker=row["worker"],
            tool=row["tool"],
            command=row["command"],
            decision=action,
            decision_source="timeout",
            rule_id=row["rule_id"],
            actor=None,
        )
        expired += 1
    return expired


def list_audit(conn: sqlite3.Connection, project_id: str, *, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM tool_audit WHERE project_id = ? ORDER BY id DESC LIMIT ?",
        (project_id, limit),
    ).fetchall()


def _age_seconds(created_at: str, now: str) -> float:
    from datetime import datetime

    try:
        start = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        end = datetime.fromisoformat(now.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    return (end - start).total_seconds()
