"""Scoped counters: allocate the next id for a row in one project."""

from __future__ import annotations

import sqlite3


def next_project_id(conn: sqlite3.Connection) -> str:
    conn.execute("UPDATE counters SET value = value + 1 WHERE name = 'project'")
    row = conn.execute("SELECT value FROM counters WHERE name = 'project'").fetchone()
    return f"proj_{row['value']:03d}"

def _next_scoped_id(
    conn: sqlite3.Connection, kind: str, prefix: str, project_id: str
) -> str:
    conn.execute(
        "INSERT OR IGNORE INTO scoped_counters (project_id, kind, value) VALUES (?, ?, 0)",
        (project_id, kind),
    )
    conn.execute(
        "UPDATE scoped_counters SET value = value + 1 WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    )
    row = conn.execute(
        "SELECT value FROM scoped_counters WHERE project_id = ? AND kind = ?",
        (project_id, kind),
    ).fetchone()
    assert row is not None
    return f"{prefix}{row['value']:03d}"

def next_fact_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "fact", "f", project_id)

def next_step_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "step", "s", project_id)

def next_goal_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "goal", "g", project_id)

def next_hint_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "hint", "h", project_id)

def next_finding_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "finding", "d", project_id)

def next_document_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "document", "doc", project_id)

def next_session_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "session", "x", project_id)

def next_session_message_seq(conn: sqlite3.Connection, project_id: str, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM session_messages WHERE project_id = ? AND session_id = ?",
        (project_id, session_id),
    ).fetchone()
    return int(row[0])

def next_question_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "question", "q", project_id)
