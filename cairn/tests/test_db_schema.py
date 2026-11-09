from __future__ import annotations

import sqlite3
import threading

import pytest

from cairn.server import db

# The schema is created in one shot: there is no upgrade path from Cairn 0.x, so the
# only thing worth pinning is that a fresh database carries every table, column and
# index the runtime reads.


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
        assert {"asset", "batch"} <= _columns(conn, "steps")


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
