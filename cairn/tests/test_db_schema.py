from __future__ import annotations

import sqlite3
import threading

import pytest

from cairn.server import db

# The schema is created in one shot. A file from an older build is patched in place by
# `_patch_legacy_columns`, so two things are worth pinning: a fresh database carries
# every table, column and index the runtime reads, and an old one is repaired instead
# of failing on its first query.


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _configured(tmp_path, monkeypatch, name: str = "fresh.db"):
    monkeypatch.setattr(db, "_db_path", None)
    path = tmp_path / name
    db.configure(path)
    return path


def test_fresh_database_has_the_full_schema(tmp_path, monkeypatch) -> None:
    _configured(tmp_path, monkeypatch)

    with db.get_conn() as conn:
        tables = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert {
            "projects",
            "facts",
            "goals",
            "steps",
            "step_sources",
            "findings",
            "finding_evidence",
            "http_exchanges",
            "documents",
            "hints",
            "constraints",
            "steers",
            "steer_deliveries",
            "project_scope",
            "sessions",
            "session_messages",
            "questions",
            "tool_rules",
            "tool_approvals",
            "tool_audit",
            "tool_review_cache",
        } <= tables

        indexes = {
            row["name"]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert {
            "idx_sessions_client_key",
            "idx_session_messages_client_msg_id",
            "tool_rules_by_project",
            "tool_review_cache_by_expiry",
        } <= indexes

        project_columns = _columns(conn, "projects")
        assert {
            "decide_worker",
            "decide_trigger",
            "decide_started_at",
            "decide_last_heartbeat_at",
            "token_budget",
            "blocked_reason",
            "context_summary",
            "context_summary_facts",
            "context_step_summary",
            "context_step_summary_facts",
            "context_finding_summary",
            "context_finding_summary_facts",
            "recall_plan",
        } <= project_columns

        assert {"client_key"} <= _columns(conn, "sessions")
        assert {"client_msg_id"} <= _columns(conn, "session_messages")
        assert {"reason"} <= _columns(conn, "tool_audit")
        assert {"severity", "status"} <= _columns(conn, "findings")
        assert {"asset", "exclusive", "batch"} <= _columns(conn, "steps")


def test_builtin_tool_rules_are_seeded_once(tmp_path, monkeypatch) -> None:
    path = _configured(tmp_path, monkeypatch)

    with db.get_conn() as conn:
        seeded = conn.execute("SELECT COUNT(*) AS n FROM tool_rules").fetchone()["n"]

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        again = conn.execute("SELECT COUNT(*) AS n FROM tool_rules").fetchone()["n"]

    assert seeded == 4
    assert again == seeded


def test_get_conn_reuses_one_connection_per_thread(tmp_path, monkeypatch) -> None:
    _configured(tmp_path, monkeypatch, "reuse.db")

    with db.get_conn() as first:
        pass
    with db.get_conn() as second:
        pass

    assert first is second

    from_thread: list[int] = []

    def read() -> None:
        with db.get_conn() as conn:
            from_thread.append(id(conn))

    thread = threading.Thread(target=read)
    thread.start()
    thread.join()

    assert from_thread and from_thread[0] != id(first)


def test_get_conn_refuses_nesting(tmp_path, monkeypatch) -> None:
    _configured(tmp_path, monkeypatch, "nested.db")

    with db.get_conn() as outer:
        outer.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(RuntimeError, match="must not be nested"), db.get_conn():
            pass
        outer.execute("INSERT INTO t VALUES (1)")
    with db.get_conn() as conn:
        assert [row["x"] for row in conn.execute("SELECT x FROM t")] == [1]


def _schema_from_an_older_build() -> str:
    """The current DDL minus the columns added afterwards: what an old file contains."""
    dropped = ("exclusive INTEGER", "approval_id TEXT", "tool_audit_by_approval")
    return "\n".join(
        line for line in db.SCHEMA.splitlines() if not any(token in line for token in dropped)
    )


def test_an_old_database_is_patched_instead_of_failing_on_the_first_query(
    tmp_path, monkeypatch
) -> None:
    """A file written before `exclusive`/`approval_id` existed must keep working.

    Without the patch every project read raised ``IndexError: No item with that key``
    and every step insert ``no such column``, while /healthz kept claiming the new
    version.
    """
    path = tmp_path / "legacy.db"
    legacy = sqlite3.connect(path)
    legacy.executescript(_schema_from_an_older_build())
    legacy.commit()
    legacy.close()

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        assert "exclusive" in _columns(conn, "steps")
        assert "approval_id" in _columns(conn, "tool_audit")
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

        # The columns are usable, not just present.
        conn.execute(
            "INSERT INTO projects (id, title, status, created_at)"
            " VALUES ('p1', 't', 'active', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "INSERT INTO steps (id, project_id, kind, description, exclusive, status,"
            " creator, created_at) VALUES ('s1', 'p1', 'action', 'x', 1, 'pending',"
            " 'human', '2026-01-01T00:00:00Z')"
        )
        row = conn.execute("SELECT * FROM steps WHERE id = 's1'").fetchone()
        assert row["exclusive"] == 1


def test_a_database_from_a_newer_build_is_refused(tmp_path, monkeypatch) -> None:
    """Half-reading a schema this build cannot know is worse than refusing to start."""
    path = _configured(tmp_path, monkeypatch, "future.db")
    with db.get_conn() as conn:
        conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")

    monkeypatch.setattr(db, "_db_path", None)
    with pytest.raises(RuntimeError, match="schema version"):
        db.configure(path)

    # The rejection leaves the module unconfigured, so the caller can point elsewhere.
    assert db._db_path is None


def test_a_corrected_builtin_rule_reaches_an_existing_database(tmp_path, monkeypatch) -> None:
    """The seeder refreshes shipped targets/patterns; operator knobs stay put."""
    path = _configured(tmp_path, monkeypatch, "rules.db")
    legacy_pattern = r"\.(?:gov|edu)(?:[./:]|$)"
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE tool_rules SET target = 'command', pattern = ?, priority = 70"
            " WHERE message LIKE '%政府/教育%'",
            (legacy_pattern,),
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT target, match_type, pattern, priority, action, timeout_seconds"
            " FROM tool_rules WHERE message LIKE '%政府/教育%'"
        ).fetchone()
    assert row["target"] == "host"
    assert row["match_type"] == "regex"
    assert row["pattern"] == r"\.(?:gov|edu)(?:\.[a-z]{2})?$"
    # Only the shipped fields are rewritten; the operator's tuning survives.
    assert row["priority"] == 70
    assert row["action"] == "deny"
    assert row["timeout_seconds"] == 60


def test_db_busy_counter_counts_lock_failures(tmp_path, monkeypatch) -> None:
    path = _configured(tmp_path, monkeypatch, "busy.db")
    before = db.db_busy_total()

    with db.get_conn() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        other = sqlite3.connect(path, timeout=0.05, factory=db._InstrumentedConnection)
        other.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError):
            other.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            other.execute("SELECT * FROM missing_table")
        other.close()

    assert db.db_busy_total() == before + 1
