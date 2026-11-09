"""Look up a row or raise the right HTTP error before a route writes."""

from __future__ import annotations

import sqlite3

from fastapi import HTTPException

from cairn.contracts.models import TERMINAL_STEP_STATUSES

from .constants import PROJECT_COLUMNS, SESSION_COLUMNS
from .leases import expire_steps


def get_project_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT {PROJECT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return row


def check_project_active(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "active":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_hint_writable(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] not in ("active", "stopped", "completed"):
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def check_project_completed(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = get_project_or_404(conn, project_id)
    if row["status"] != "completed":
        raise HTTPException(403, f"Project is {row['status']}")
    return row


def validate_facts_exist(conn: sqlite3.Connection, project_id: str, fact_ids: list[str]) -> None:
    for fid in fact_ids:
        row = conn.execute(
            "SELECT 1 FROM facts WHERE id = ? AND project_id = ?", (fid, project_id)
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Fact {fid} not found")


def validate_goal_exists(conn: sqlite3.Connection, project_id: str, goal_id: str) -> None:
    row = conn.execute(
        "SELECT 1 FROM goals WHERE id = ? AND project_id = ?", (goal_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, f"Goal {goal_id} not found")


def validate_creator_worker(creator: str, worker: str | None) -> None:
    if worker is not None and worker != creator:
        raise HTTPException(400, "worker must be null or equal to creator")


def get_step_or_404(conn: sqlite3.Connection, project_id: str, step_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM steps WHERE id = ? AND project_id = ?",
        (step_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Step not found")
    return row


def get_claimable_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> sqlite3.Row:
    expire_steps(conn, project_id)
    row = get_step_or_404(conn, project_id, step_id)
    if row["status"] in TERMINAL_STEP_STATUSES:
        raise HTTPException(409, f"Step already {row['status']}")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return row


def get_releasable_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> sqlite3.Row:
    expire_steps(conn, project_id)
    row = get_step_or_404(conn, project_id, step_id)
    if row["status"] in TERMINAL_STEP_STATUSES:
        raise HTTPException(409, f"Step already {row['status']}")
    if row["worker"] is None:
        return row
    if row["worker"] != worker:
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return row


def get_root_goal_or_404(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM goals WHERE project_id = ? AND parent_id IS NULL ORDER BY rowid LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(409, "Project is missing its root goal")
    return row


def get_goal_or_404(conn: sqlite3.Connection, project_id: str, goal_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM goals WHERE id = ? AND project_id = ?", (goal_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Goal not found")
    return row


def get_completion_step_or_409(conn: sqlite3.Connection, project_id: str) -> sqlite3.Row:
    rows = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? AND kind = 'completion'", (project_id,)
    ).fetchall()
    if not rows:
        raise HTTPException(409, "Completed project is missing its completion step")
    if len(rows) != 1:
        raise HTTPException(409, "Completed project has multiple completion steps")
    return rows[0]


def get_session_or_404(conn: sqlite3.Connection, project_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions s WHERE s.id = ? AND s.project_id = ?",
        (session_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    return row


def get_question_or_404(conn: sqlite3.Connection, project_id: str, question_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM questions WHERE id = ? AND project_id = ?", (question_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Question not found")
    return row


def get_document_or_404(conn: sqlite3.Connection, project_id: str, document_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM documents WHERE id = ? AND project_id = ?", (document_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Document not found")
    return row
