from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = Path.home() / ".local" / "share" / "cairn" / "cairn.db"

_db_path: Path | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS settings (
    step_timeout INTEGER NOT NULL DEFAULT 20,
    decide_timeout INTEGER NOT NULL DEFAULT 20
);

INSERT OR IGNORE INTO settings (rowid) VALUES (1);

CREATE TABLE IF NOT EXISTS projects (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    decide_worker TEXT,
    decide_trigger TEXT,
    decide_started_at TEXT,
    decide_last_heartbeat_at TEXT
);

CREATE TABLE IF NOT EXISTS facts (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title TEXT,
    description TEXT NOT NULL,
    step_id TEXT,
    created_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS goals (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    parent_id TEXT,
    description TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS steps (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    goal_id TEXT,
    kind TEXT NOT NULL DEFAULT 'action',
    title TEXT,
    description TEXT NOT NULL,
    rationale TEXT,
    priority INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',
    creator TEXT NOT NULL,
    worker TEXT,
    last_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    concluded_at TEXT,
    result_fact_id TEXT,
    note TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS step_sources (
    step_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    fact_id TEXT NOT NULL,
    PRIMARY KEY (step_id, project_id, fact_id),
    FOREIGN KEY (step_id, project_id) REFERENCES steps(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS findings (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id TEXT,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'info',
    description TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS documents (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    step_id TEXT,
    fact_id TEXT,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE INDEX IF NOT EXISTS documents_by_project ON documents (project_id, created_at);

CREATE TABLE IF NOT EXISTS hints (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    creator TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    worker TEXT NOT NULL,
    worker_type TEXT NOT NULL,
    phase TEXT NOT NULL,
    step_id TEXT,
    summary TEXT,
    native_session TEXT,
    status TEXT NOT NULL DEFAULT 'failed',
    returncode INTEGER,
    timed_out INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS session_messages (
    session_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    raw TEXT,
    PRIMARY KEY (session_id, project_id, seq),
    FOREIGN KEY (session_id, project_id) REFERENCES sessions(id, project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS questions (
    id TEXT NOT NULL,
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    answer TEXT,
    error TEXT,
    worker TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    updated_at TEXT,
    answered_at TEXT,
    duration_ms INTEGER,
    PRIMARY KEY (id, project_id)
);

CREATE TABLE IF NOT EXISTS counters (
    name TEXT PRIMARY KEY,
    value INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO counters (name, value) VALUES ('project', 0);

CREATE TABLE IF NOT EXISTS scoped_counters (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (project_id, kind)
);

CREATE INDEX IF NOT EXISTS sessions_by_project ON sessions (project_id, created_at);
CREATE INDEX IF NOT EXISTS findings_by_project ON findings (project_id, created_at);
"""

# Columns are additive so an existing Cairn 0.x database keeps working: create the new
# names, copy whatever the old version stored, and leave the old columns behind.
_LEGACY_COLUMN_COPIES: tuple[tuple[str, str, str], ...] = (
    ("settings", "step_timeout", "intent_timeout"),
    ("settings", "decide_timeout", "reason_timeout"),
    ("projects", "decide_worker", "reason_worker"),
    ("projects", "decide_trigger", "reason_trigger"),
    ("projects", "decide_started_at", "reason_started_at"),
    ("projects", "decide_last_heartbeat_at", "reason_last_heartbeat_at"),
)


def configure(path: Path) -> None:
    global _db_path
    if _db_path is not None:
        return
    _db_path = path
    _db_path.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        _migrate_legacy_intents(conn)


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    for table, new_column, legacy_column in _LEGACY_COLUMN_COPIES:
        columns = _table_columns(conn, table)
        if new_column in columns:
            continue
        if table == "settings":
            conn.execute(f"ALTER TABLE settings ADD COLUMN {new_column} INTEGER NOT NULL DEFAULT 20")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {new_column} TEXT")
        if legacy_column in columns:
            conn.execute(f"UPDATE {table} SET {new_column} = {legacy_column}")

    session_columns = _table_columns(conn, "sessions")
    for column, ddl in (
        ("native_session", "TEXT"),
        ("summary", "TEXT"),
        ("error", "TEXT"),
        ("duration_ms", "INTEGER"),
        ("updated_at", "TEXT"),
        ("input_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("output_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_read_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("cache_write_tokens", "INTEGER NOT NULL DEFAULT 0"),
        ("total_tokens", "INTEGER NOT NULL DEFAULT 0"),
    ):
        if column not in session_columns:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} {ddl}")
    # Sessions created before `updated_at` existed have no heartbeat history.
    conn.execute("UPDATE sessions SET updated_at = created_at WHERE updated_at IS NULL")

    project_columns = _table_columns(conn, "projects")
    for column, ddl in (
        ("context_summary", "TEXT"),
        ("context_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("context_step_summary", "TEXT"),
        ("context_step_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("context_finding_summary", "TEXT"),
        ("context_finding_summary_facts", "INTEGER NOT NULL DEFAULT 0"),
        ("recall_plan", "TEXT"),
    ):
        if column not in project_columns:
            conn.execute(f"ALTER TABLE projects ADD COLUMN {column} {ddl}")

    fact_columns = _table_columns(conn, "facts")
    if "step_id" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN step_id TEXT")
    if "created_at" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN created_at TEXT")
    if "title" not in fact_columns:
        conn.execute("ALTER TABLE facts ADD COLUMN title TEXT")

    step_columns = _table_columns(conn, "steps")
    if "title" not in step_columns:
        conn.execute("ALTER TABLE steps ADD COLUMN title TEXT")

    # Findings gained a severity grade; older rows have no opinion and stay `info`.
    finding_columns = _table_columns(conn, "findings")
    if "severity" not in finding_columns:
        conn.execute("ALTER TABLE findings ADD COLUMN severity TEXT NOT NULL DEFAULT 'info'")

    # A database created by Cairn 0.x has no goals table content: the root goal lived in a
    # reserved fact named "goal". Promote it to a real Goal node, then drop the fact.
    if "intents" not in _token_tables(conn):
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO goals (id, project_id, parent_id, description, status, creator, created_at)
        SELECT 'g001', p.id, NULL, f.description, 'active', 'legacy', p.created_at
        FROM projects p
        JOIN facts f ON f.project_id = p.id AND f.id = 'goal'
        WHERE NOT EXISTS (SELECT 1 FROM goals g WHERE g.project_id = p.id)
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT project_id, 'goal', 1 FROM goals
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT project_id, 'step', COUNT(*) FROM intents GROUP BY project_id
        """
    )
    conn.execute(
        """
        INSERT OR IGNORE INTO scoped_counters (project_id, kind, value)
        SELECT id, 'finding', 0 FROM projects
        """
    )
    conn.execute("DELETE FROM facts WHERE id = 'goal'")


def _token_tables(conn: sqlite3.Connection) -> set[str]:
    return {
        row["name"]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }


def _migrate_legacy_intents(conn: sqlite3.Connection) -> None:
    if "intents" in _token_tables(conn) and not conn.execute("SELECT 1 FROM steps LIMIT 1").fetchone():
        _migrate_intents(conn)


def _migrate_intents(conn: sqlite3.Connection) -> None:
    """Copy Cairn 0.x intents into steps so old projects keep their history."""
    rows = conn.execute("SELECT * FROM intents").fetchall()
    if not rows:
        return
    for row in rows:
        if row["to_fact_id"] == "goal":
            status, result_fact_id, kind, note = "done", None, "completion", "legacy completion"
        elif row["to_fact_id"] is not None:
            status, result_fact_id, kind, note = "done", row["to_fact_id"], "action", None
        elif row["worker"] is not None:
            status, result_fact_id, kind, note = "running", None, "action", None
        else:
            status, result_fact_id, kind, note = "pending", None, "action", None
        conn.execute(
            """
            INSERT OR IGNORE INTO steps (
                id, project_id, goal_id, kind, description, rationale, priority, status,
                creator, worker, last_heartbeat_at, created_at, concluded_at, result_fact_id, note
            ) VALUES (?, ?, 'g001', ?, ?, NULL, 0, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row["id"],
                row["project_id"],
                kind,
                row["description"],
                status,
                row["creator"],
                row["worker"] if status == "running" else None,
                row["last_heartbeat_at"],
                row["created_at"],
                row["concluded_at"],
                result_fact_id,
                note,
            ),
        )
        for source in conn.execute(
            "SELECT fact_id FROM intent_sources WHERE intent_id = ? AND project_id = ?",
            (row["id"], row["project_id"]),
        ).fetchall():
            conn.execute(
                "INSERT OR IGNORE INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (row["id"], row["project_id"], source["fact_id"]),
            )


@contextmanager
def get_conn() -> Generator[sqlite3.Connection, None, None]:
    assert _db_path is not None
    conn = sqlite3.connect(str(_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
