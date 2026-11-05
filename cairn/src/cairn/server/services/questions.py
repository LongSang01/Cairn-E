"""Side-question list models and the dispatcher's pending queue."""

from __future__ import annotations

import sqlite3

from cairn.contracts.models import Question

from .leases import expire_questions


def question_to_model(row: sqlite3.Row) -> Question:
    return Question(
        id=row["id"],
        project_id=row["project_id"],
        content=row["content"],
        status=row["status"],
        answer=row["answer"],
        error=row["error"],
        worker=row["worker"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        answered_at=row["answered_at"],
        duration_ms=row["duration_ms"],
    )

def build_questions(conn: sqlite3.Connection, project_id: str) -> list[Question]:
    expire_questions(conn, project_id)
    rows = conn.execute(
        "SELECT * FROM questions WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [question_to_model(row) for row in rows]

def list_pending_questions(conn: sqlite3.Connection, limit: int = 5) -> list[Question]:
    """Oldest pending questions across all projects: what the dispatcher should answer."""
    expire_questions(conn)
    rows = conn.execute(
        """
        SELECT * FROM questions q
        WHERE q.status = 'pending'
          AND EXISTS (SELECT 1 FROM projects p WHERE p.id = q.project_id)
        ORDER BY q.created_at, q.rowid
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [question_to_model(row) for row in rows]
