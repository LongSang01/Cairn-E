from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import CreateSessionRequest, Session, SessionDetail
from cairn.server.services import (
    build_session_messages,
    build_sessions,
    get_project_or_404,
    get_session_or_404,
    next_session_id,
    session_to_model,
    utcnow,
)

router = APIRouter(tags=["sessions"])


@router.get("/projects/{project_id}/sessions", response_model=list[Session])
def list_sessions(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_sessions(conn, project_id)


@router.get("/projects/{project_id}/sessions/{session_id}", response_model=SessionDetail)
def get_session(project_id: str, session_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_session_or_404(conn, project_id, session_id)
        return SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(conn, project_id, session_id),
        )


@router.post(
    "/projects/{project_id}/sessions",
    response_model=SessionDetail,
    status_code=201,
)
def create_session(project_id: str, body: CreateSessionRequest):
    """Called by the dispatcher: record one model conversation for a Decide/Execute run."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        sid = next_session_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO sessions (
                id, project_id, worker, worker_type, phase, step_id, native_session,
                status, returncode, timed_out, duration_ms, error, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                project_id,
                body.worker,
                body.worker_type,
                body.phase,
                body.step,
                body.native_session,
                body.status,
                body.returncode,
                1 if body.timed_out else 0,
                body.duration_ms,
                body.error,
                now,
            ),
        )
        conn.executemany(
            "INSERT INTO session_messages (session_id, project_id, seq, role, content, raw) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (sid, project_id, index, message.role, message.content, message.raw)
                for index, message in enumerate(body.messages)
            ],
        )
        row = get_session_or_404(conn, project_id, sid)
        return SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(conn, project_id, sid),
        )
