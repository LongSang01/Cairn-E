"""Side questions: a read-only Q&A channel beside the graph.

A question is a snapshot query. It never creates facts, steps or hints, it is answered by
the dispatcher outside the decide/execute slots, and clearing the history only deletes
rows from the `questions` table.
"""

import time

from fastapi import APIRouter, HTTPException, Query

from cairn.server.db import get_conn
from cairn.server.models import (
    AnswerQuestionRequest,
    ClaimQuestionRequest,
    CreateQuestionRequest,
    Question,
)
from cairn.server.services import (
    build_questions,
    get_project_or_404,
    get_question_or_404,
    list_pending_questions,
    next_question_id,
    question_to_model,
    utcnow,
)
from cairn.server.sse import sse_event, sse_response

router = APIRouter(tags=["questions"])

TERMINAL_STATUSES = ("answered", "failed", "cancelled")
ACTIVE_STATUSES = ("pending", "running")
QUESTION_STREAM_INTERVAL_SECONDS = 1.0
QUESTION_STREAM_MAX_SECONDS = 900


@router.get("/projects/{project_id}/questions/stream")
def stream_questions(project_id: str):
    """Server-sent events for the side channel.

    The initial `snapshot` carries the whole history; later `questions` events carry the
    full list again (it is small) whenever something changed. The stream ends once
    nothing is pending or running, so an idle tab holds no connection.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)

    def generate():
        deadline = time.monotonic() + QUESTION_STREAM_MAX_SECONDS
        snapshot_sent = False
        last: list[dict] | None = None
        while True:
            try:
                with get_conn() as conn:
                    get_project_or_404(conn, project_id)
                    questions = build_questions(conn, project_id)
            except HTTPException:
                yield sse_event("gone", {"project": project_id})
                return
            payload = [question.model_dump() for question in questions]
            if not snapshot_sent:
                yield sse_event("snapshot", {"questions": payload})
                snapshot_sent = True
                last = payload
            elif payload != last:
                yield sse_event("questions", {"questions": payload})
                last = payload
            if not any(question["status"] in ACTIVE_STATUSES for question in payload):
                yield sse_event("end", {"questions": payload})
                return
            if time.monotonic() > deadline:
                yield sse_event("end", {"questions": payload})
                return
            yield ": keep-alive\n\n"
            time.sleep(QUESTION_STREAM_INTERVAL_SECONDS)

    return sse_response(generate())


@router.get("/questions/pending", response_model=list[Question])
def pending_questions(limit: int = Query(default=5, ge=1, le=50)):
    """Internal: what the dispatcher should pick up next, across all projects."""
    with get_conn() as conn:
        return list_pending_questions(conn, limit=limit)


@router.get("/projects/{project_id}/questions", response_model=list[Question])
def list_questions(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_questions(conn, project_id)


# Declared after `/questions/stream` so that literal path is matched first.
@router.get("/projects/{project_id}/questions/{question_id}", response_model=Question)
def get_question(project_id: str, question_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return question_to_model(get_question_or_404(conn, project_id, question_id))


@router.post("/projects/{project_id}/questions", response_model=Question, status_code=201)
def ask_question(project_id: str, body: CreateQuestionRequest):
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        if project["status"] == "completed":
            raise HTTPException(403, "Project is completed; its worker container is gone")
        question_id = next_question_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO questions (id, project_id, content, status, created_at, updated_at)
            VALUES (?, ?, ?, 'pending', ?, ?)
            """,
            (question_id, project_id, body.content, now, now),
        )
        return question_to_model(get_question_or_404(conn, project_id, question_id))


@router.delete("/projects/{project_id}/questions", response_model=dict[str, int])
def clear_questions(project_id: str):
    """Clear the side-channel history. Pending/running answers are left alone."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        cursor = conn.execute(
            "DELETE FROM questions WHERE project_id = ? AND status IN (?, ?, ?)",
            (project_id, *TERMINAL_STATUSES),
        )
        return {"deleted": cursor.rowcount}


@router.post("/projects/{project_id}/questions/{question_id}/cancel", response_model=Question)
def cancel_question(project_id: str, question_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_question_or_404(conn, project_id, question_id)
        if row["status"] not in ACTIVE_STATUSES:
            raise HTTPException(409, f"Question already {row['status']}")
        conn.execute(
            "UPDATE questions SET status = 'cancelled', answered_at = ?, updated_at = ? "
            "WHERE id = ? AND project_id = ?",
            (utcnow(), utcnow(), question_id, project_id),
        )
        return question_to_model(get_question_or_404(conn, project_id, question_id))


@router.post("/projects/{project_id}/questions/{question_id}/claim", response_model=Question)
def claim_question(project_id: str, question_id: str, body: ClaimQuestionRequest):
    """Internal: the dispatcher takes ownership before running the worker."""
    with get_conn() as conn:
        project = get_project_or_404(conn, project_id)
        if project["status"] == "completed":
            raise HTTPException(403, "Project is completed")
        row = get_question_or_404(conn, project_id, question_id)
        if row["status"] != "pending":
            raise HTTPException(409, f"Question is {row['status']}")
        now = utcnow()
        conn.execute(
            """
            UPDATE questions
            SET status = 'running', worker = ?, started_at = ?, updated_at = ?
            WHERE id = ? AND project_id = ? AND status = 'pending'
            """,
            (body.worker, now, now, question_id, project_id),
        )
        updated = get_question_or_404(conn, project_id, question_id)
        if updated["status"] != "running" or updated["worker"] != body.worker:
            raise HTTPException(409, "Question was claimed by another dispatcher")
        return question_to_model(updated)


@router.post("/projects/{project_id}/questions/{question_id}/answer", response_model=Question)
def answer_question(project_id: str, question_id: str, body: AnswerQuestionRequest):
    """Internal: store the answer (or the failure) and close the question."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_question_or_404(conn, project_id, question_id)
        # Only a claimed question has a worker that is allowed to answer it.
        if row["status"] != "running":
            raise HTTPException(409, f"Question is {row['status']}")
        now = utcnow()
        conn.execute(
            """
            UPDATE questions
            SET status = ?, answer = ?, error = ?, worker = COALESCE(?, worker),
                answered_at = ?, updated_at = ?, duration_ms = COALESCE(?, duration_ms)
            WHERE id = ? AND project_id = ?
            """,
            (
                body.status,
                (body.answer or "").strip() or None,
                (body.error or "").strip() or None,
                body.worker,
                now,
                now,
                body.duration_ms,
                question_id,
                project_id,
            ),
        )
        return question_to_model(get_question_or_404(conn, project_id, question_id))
