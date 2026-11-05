"""Reclaim work whose owner stopped heartbeating (steps, Decide, sessions, questions)."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import ProjectDecide, ProjectMeta

from .constants import (
    QUESTION_PENDING_TTL_SECONDS,
    QUESTION_RUNNING_TTL_SECONDS,
    SESSION_STALE_SECONDS,
)
from .timeutil import utcnow, utcnow_before


def get_step_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT step_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["step_timeout"]

def get_decide_timeout(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT decide_timeout FROM settings WHERE rowid = 1").fetchone()
    return row["decide_timeout"]

def project_decide_from_row(row: sqlite3.Row) -> ProjectDecide | None:
    if row["decide_worker"] is None:
        return None
    return ProjectDecide(
        worker=row["decide_worker"],
        trigger=row["decide_trigger"],
        started_at=row["decide_started_at"],
        last_heartbeat_at=row["decide_last_heartbeat_at"],
    )

def project_meta_from_row(row: sqlite3.Row) -> ProjectMeta:
    return ProjectMeta(
        id=row["id"],
        title=row["title"],
        status=row["status"],
        created_at=row["created_at"],
        decide=project_decide_from_row(row),
    )

def clear_project_decide(conn: sqlite3.Connection, project_id: str) -> None:
    conn.execute(
        """
        UPDATE projects
        SET decide_worker = NULL,
            decide_trigger = NULL,
            decide_started_at = NULL,
            decide_last_heartbeat_at = NULL
        WHERE id = ?
        """,
        (project_id,),
    )

def expire_steps(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_step_timeout(conn)
    now = utcnow()
    query = """
        UPDATE steps
        SET worker = NULL,
            status = 'pending'
        WHERE status = 'running'
          AND worker IS NOT NULL
          AND last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE project_id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)

def expire_decide_leases(conn: sqlite3.Connection, project_id: str | None = None) -> None:
    timeout = get_decide_timeout(conn)
    now = utcnow()
    query = """
        UPDATE projects
        SET decide_worker = NULL,
            decide_trigger = NULL,
            decide_started_at = NULL,
            decide_last_heartbeat_at = NULL
        WHERE decide_worker IS NOT NULL
          AND decide_last_heartbeat_at IS NOT NULL
          AND (julianday(?) - julianday(decide_last_heartbeat_at)) * 86400 > ?
    """
    params: tuple = (now, timeout)
    if project_id is not None:
        query = query.replace("WHERE ", "WHERE id = ? AND ", 1)
        params = (project_id, now, timeout)
    conn.execute(query, params)

def expire_stale_sessions(conn: sqlite3.Connection, project_id: str) -> int:
    """Mark sessions whose dispatcher stopped heartbeating as failed.

    A crashed or killed dispatcher never sends the closing PATCH, so without this the
    row would stay `running` and the UI would poll it forever.
    """
    cursor = conn.execute(
        """
        UPDATE sessions
        SET status = 'failed',
            error = COALESCE(error, 'dispatcher stopped before the run finished'),
            updated_at = ?
        WHERE project_id = ? AND status = 'running'
          AND COALESCE(updated_at, created_at) < ?
        """,
        (utcnow(), project_id, utcnow_before(SESSION_STALE_SECONDS)),
    )
    return cursor.rowcount

def expire_questions(conn: sqlite3.Connection, project_id: str | None = None) -> int:
    """Fail questions nobody is going to answer, so the UI stops waiting forever."""
    now = utcnow()
    scope = " AND project_id = ?" if project_id is not None else ""
    scoped = (project_id,) if project_id is not None else ()
    pending = conn.execute(
        f"""
        UPDATE questions
        SET status = 'failed',
            error = 'no dispatcher picked the question up',
            updated_at = ?
        WHERE status = 'pending' AND created_at < ?{scope}
        """,
        (now, utcnow_before(QUESTION_PENDING_TTL_SECONDS), *scoped),
    )
    running = conn.execute(
        f"""
        UPDATE questions
        SET status = 'failed',
            error = 'dispatcher stopped before answering',
            updated_at = ?
        WHERE status = 'running'
          AND COALESCE(updated_at, started_at, created_at) < ?{scope}
        """,
        (now, utcnow_before(QUESTION_RUNNING_TTL_SECONDS), *scoped),
    )
    return pending.rowcount + running.rowcount
