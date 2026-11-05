from __future__ import annotations

import sqlite3
import threading
import time

import pytest

from cairn.server import db

LEGACY_PROJECTS = """
CREATE TABLE projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    bootstrap_enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    reason_worker TEXT,
    reason_trigger TEXT,
    reason_started_at TEXT,
    reason_last_heartbeat_at TEXT
)
"""

LEGACY_FACTS = """
CREATE TABLE facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
)
"""

LEGACY_INTENTS = """
CREATE TABLE intents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    to_fact_id TEXT,
    PRIMARY KEY (id, project_id)
)
"""

LEGACY_INTENT_SOURCES = """
CREATE TABLE intent_sources (
    intent_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (intent_id, project_id, fact_id)
)
"""


def _legacy_db(path, *, with_intents: bool) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(LEGACY_PROJECTS)
        conn.execute(LEGACY_FACTS)
        conn.execute(
            "CREATE TABLE hints (id TEXT, project_id TEXT, content TEXT, creator TEXT, created_at TEXT)"
        )
        conn.execute(
            "CREATE TABLE counters (name TEXT PRIMARY KEY, value INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE scoped_counters (project_id TEXT, kind TEXT, value INTEGER, PRIMARY KEY (project_id, kind))"
        )
        conn.execute(
            "CREATE TABLE settings (intent_timeout INTEGER NOT NULL DEFAULT 20, reason_timeout INTEGER NOT NULL DEFAULT 20)"
        )
        conn.execute(
            "INSERT INTO settings (rowid, intent_timeout, reason_timeout) VALUES (1, 31, 47)"
        )
        conn.execute("INSERT INTO counters (name, value) VALUES ('project', 1)")
        conn.execute(
            "INSERT INTO projects (id, title, bootstrap_enabled, created_at, reason_worker, reason_trigger, reason_started_at, reason_last_heartbeat_at)"
            " VALUES ('proj_001', 'legacy', 1, '2026-01-01T00:00:00Z', 'w', 'initial', '2026-01-01T00:00:05Z', '2026-01-01T00:00:06Z')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('origin', 'proj_001', 'start')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('goal', 'proj_001', 'finish')"
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description) VALUES ('f001', 'proj_001', 'known')"
        )
        if with_intents:
            conn.execute(LEGACY_INTENTS)
            conn.execute(LEGACY_INTENT_SOURCES)
            conn.execute(
                "INSERT INTO intents (id, project_id, description, creator, worker, created_at, to_fact_id)"
                " VALUES ('i001', 'proj_001', 'done work', 'reasoner', 'worker-a', '2026-01-01T00:00:02Z', 'f001')"
            )
            conn.execute(
                "INSERT INTO intents (id, project_id, description, creator, worker, created_at, to_fact_id)"
                " VALUES ('i002', 'proj_001', 'goal met', 'reasoner', 'worker-a', '2026-01-01T00:00:03Z', 'goal')"
            )
            conn.execute(
                "INSERT INTO intents (id, project_id, description, creator, worker, created_at, to_fact_id)"
                " VALUES ('i003', 'proj_001', 'pending work', 'reasoner', NULL, '2026-01-01T00:00:04Z', NULL)"
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i001', 'proj_001', 'origin')"
            )
            conn.execute(
                "INSERT INTO intent_sources (intent_id, project_id, fact_id) VALUES ('i003', 'proj_001', 'f001')"
            )


def test_configure_adds_decide_columns_and_copies_legacy_reason_leases(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "legacy.db"
    _legacy_db(path, with_intents=False)

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT decide_worker, decide_trigger, decide_started_at, decide_last_heartbeat_at FROM projects WHERE id = 'proj_001'"
        ).fetchone()
        settings = conn.execute(
            "SELECT step_timeout, decide_timeout FROM settings WHERE rowid = 1"
        ).fetchone()

    assert row["decide_worker"] == "w"
    assert row["decide_trigger"] == "initial"
    assert row["decide_started_at"] == "2026-01-01T00:00:05Z"
    assert row["decide_last_heartbeat_at"] == "2026-01-01T00:00:06Z"
    assert settings["step_timeout"] == 31
    assert settings["decide_timeout"] == 47


def test_configure_promotes_goal_fact_to_goal_node_and_intents_to_steps(
    tmp_path, monkeypatch
) -> None:
    path = tmp_path / "intents.db"
    _legacy_db(path, with_intents=True)

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        goal = conn.execute("SELECT * FROM goals WHERE project_id = 'proj_001'").fetchone()
        facts = [
            row["id"]
            for row in conn.execute("SELECT id FROM facts WHERE project_id = 'proj_001'").fetchall()
        ]
        steps = {
            row["id"]: row
            for row in conn.execute("SELECT * FROM steps WHERE project_id = 'proj_001'").fetchall()
        }
        sources = {
            row["fact_id"]
            for row in conn.execute(
                "SELECT fact_id FROM step_sources WHERE project_id = 'proj_001' AND step_id = 'i003'"
            ).fetchall()
        }

    assert goal["id"] == "g001"
    assert goal["description"] == "finish"
    assert goal["parent_id"] is None
    assert "goal" not in facts
    assert steps["i001"]["status"] == "done"
    assert steps["i001"]["result_fact_id"] == "f001"
    assert steps["i002"]["kind"] == "completion"
    assert steps["i002"]["status"] == "done"
    assert steps["i003"]["status"] == "pending"
    assert sources == {"f001"}


def test_configure_adds_finding_severity_and_session_usage_columns(tmp_path, monkeypatch) -> None:
    path = tmp_path / "pre_grading.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE findings ("
            " id TEXT NOT NULL, project_id TEXT NOT NULL, step_id TEXT, kind TEXT NOT NULL,"
            " description TEXT NOT NULL, creator TEXT NOT NULL, created_at TEXT NOT NULL,"
            " PRIMARY KEY (id, project_id))"
        )
        conn.execute(
            "INSERT INTO findings (id, project_id, step_id, kind, description, creator, created_at)"
            " VALUES ('d001', 'proj_001', 's001', 'flaw', 'old row', 'w', '2026-01-01T00:00:00Z')"
        )
        conn.execute(
            "CREATE TABLE sessions ("
            " id TEXT NOT NULL, project_id TEXT NOT NULL, worker TEXT NOT NULL,"
            " worker_type TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL,"
            " created_at TEXT NOT NULL, PRIMARY KEY (id, project_id))"
        )
        conn.execute(
            "INSERT INTO sessions (id, project_id, worker, worker_type, phase, status, created_at)"
            " VALUES ('x001', 'proj_001', 'w', 'pi', 'execute', 'success', '2026-01-01T00:00:00Z')"
        )

    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        finding = conn.execute("SELECT severity FROM findings WHERE id = 'd001'").fetchone()
        session = conn.execute(
            "SELECT input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,"
            " total_tokens, summary FROM sessions WHERE id = 'x001'"
        ).fetchone()

    assert finding["severity"] == "info"
    assert tuple(session)[:5] == (0, 0, 0, 0, 0)
    assert session["summary"] is None


def test_new_database_records_the_baseline_migration(tmp_path, monkeypatch) -> None:
    path = tmp_path / "fresh.db"
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)

    with db.get_conn() as conn:
        versions = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    # Re-configuring an already migrated database must not apply it twice.
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(path)
    with db.get_conn() as conn:
        again = [row["version"] for row in conn.execute("SELECT version FROM schema_migrations")]

    registered = [version for version, _ in db._MIGRATIONS]
    assert versions == registered
    assert again == registered


def test_get_conn_reuses_one_connection_per_thread(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "reuse.db")

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
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "nested.db")

    with db.get_conn() as outer:
        outer.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(RuntimeError, match="must not be nested"), db.get_conn():
            pass
        outer.execute("INSERT INTO t VALUES (1)")
    with db.get_conn() as conn:
        assert [row["x"] for row in conn.execute("SELECT x FROM t")] == [1]


def test_apply_migrations_serializes_concurrent_start(tmp_path, monkeypatch) -> None:
    runs: list[int] = []
    start_line = threading.Barrier(2)

    def slow_migration(conn: sqlite3.Connection) -> None:
        runs.append(1)
        time.sleep(0.2)

    monkeypatch.setattr(db, "_MIGRATIONS", ((1, slow_migration),))
    path = tmp_path / "mutex.db"
    ddl = (
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )

    def apply() -> None:
        conn = sqlite3.connect(path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.executescript(ddl)
        start_line.wait()
        try:
            db._apply_migrations(conn)
        finally:
            conn.close()

    threads = [threading.Thread(target=apply) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert len(runs) == 1
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == 1
    conn.close()

    again = sqlite3.connect(path, factory=db._InstrumentedConnection)
    again.row_factory = sqlite3.Row
    db._apply_migrations(again)
    again.close()
    assert len(runs) == 1


def test_db_busy_counter_counts_lock_failures(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    db.configure(tmp_path / "busy.db")
    before = db.db_busy_total()

    with db.get_conn() as blocker:
        blocker.execute("BEGIN IMMEDIATE")
        other = sqlite3.connect(
            tmp_path / "busy.db", timeout=0.05, factory=db._InstrumentedConnection
        )
        other.row_factory = sqlite3.Row
        with pytest.raises(sqlite3.OperationalError):
            other.execute("CREATE TABLE t (x INTEGER)")
        with pytest.raises(sqlite3.OperationalError):
            other.execute("SELECT * FROM missing_table")
        other.close()

    assert db.db_busy_total() == before + 1
