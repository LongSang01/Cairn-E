"""Build session and transcript models from database rows."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import Session, SessionMessage

from .constants import SESSION_COLUMNS
from .leases import expire_stale_sessions


def session_to_model(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        worker=row["worker"],
        worker_type=row["worker_type"],
        phase=row["phase"],
        step=row["step_id"],
        summary=row["summary"] or "",
        native_session=row["native_session"],
        status=row["status"],
        returncode=row["returncode"],
        timed_out=bool(row["timed_out"]),
        duration_ms=row["duration_ms"],
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        cache_read_tokens=int(row["cache_read_tokens"] or 0),
        cache_write_tokens=int(row["cache_write_tokens"] or 0),
        total_tokens=int(row["total_tokens"] or 0),
        error=row["error"],
        created_at=row["created_at"],
        message_count=row["message_count"],
    )


def build_sessions(conn: sqlite3.Connection, project_id: str) -> list[Session]:
    expire_stale_sessions(conn, project_id)
    rows = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions s WHERE s.project_id = ? "
        "ORDER BY s.created_at, s.rowid",
        (project_id,),
    ).fetchall()
    return [session_to_model(row) for row in rows]


def build_session_messages(
    conn: sqlite3.Connection,
    project_id: str,
    session_id: str,
    *,
    since_seq: int | None = None,
) -> list[SessionMessage]:
    """Full transcript, or only messages newer than `since_seq` (0-based seq)."""
    query_all = (
        "SELECT seq, role, content, raw FROM session_messages "
        "WHERE project_id = ? AND session_id = ? ORDER BY seq"
    )
    query_since = (
        "SELECT seq, role, content, raw FROM session_messages "
        "WHERE project_id = ? AND session_id = ? AND seq > ? ORDER BY seq"
    )
    params: tuple[object, ...]
    if since_seq is None:
        params = (project_id, session_id)
        rows = conn.execute(query_all, params).fetchall()
    else:
        params = (project_id, session_id, since_seq)
        rows = conn.execute(query_since, params).fetchall()
    return [
        SessionMessage(seq=r["seq"], role=r["role"], content=r["content"], raw=r["raw"])
        for r in rows
    ]
