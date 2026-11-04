import asyncio
import time

from fastapi import APIRouter, HTTPException
from starlette.concurrency import run_in_threadpool

from cairn.server.db import get_conn
from cairn.server.models import (
    AppendSessionMessagesRequest,
    CreateSessionRequest,
    FinishSessionRequest,
    Session,
    SessionDetail,
)
from cairn.server.services import (
    build_session_messages,
    build_sessions,
    expire_stale_sessions,
    get_project_or_404,
    get_session_or_404,
    next_session_id,
    next_session_message_seq,
    session_to_model,
    utcnow,
)
from cairn.server.sse import sse_event, sse_response

router = APIRouter(tags=["sessions"])

STREAM_POLL_SECONDS = 0.5
STREAM_IDLE_LIMIT_SECONDS = 300.0


@router.get("/projects/{project_id}/sessions", response_model=list[Session])
def list_sessions(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_sessions(conn, project_id)


@router.get("/projects/{project_id}/sessions/{session_id}", response_model=SessionDetail)
def get_session(project_id: str, session_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_stale_sessions(conn, project_id)
        row = get_session_or_404(conn, project_id, session_id)
        return SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(conn, project_id, session_id),
        )


@router.get("/projects/{project_id}/sessions/{session_id}/stream")
def stream_session(project_id: str, session_id: str):
    """Server-sent events for one session: the full transcript, then every new message.

    The first event is always a `snapshot` so a client that connects mid-run does not
    need a separate request. After that each new message arrives as a `messages` event
    carrying only the messages the client has not seen yet.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_stale_sessions(conn, project_id)
        get_session_or_404(conn, project_id, session_id)

    def poll() -> tuple[dict, list[dict]] | None:
        """Read one transcript snapshot off the event loop (sqlite3 is blocking)."""
        with get_conn() as conn:
            expire_stale_sessions(conn, project_id)
            try:
                row = get_session_or_404(conn, project_id, session_id)
            except HTTPException:
                return None
            messages = build_session_messages(conn, project_id, session_id)
            return session_to_model(row).model_dump(), [m.model_dump() for m in messages]

    async def generate():
        sent = 0
        snapshot_sent = False
        last_change = time.monotonic()
        while True:
            result = await run_in_threadpool(poll)
            if result is None:
                yield sse_event("gone", {"id": session_id})
                return
            session, messages = result

            if not snapshot_sent:
                # One snapshot per connection, even while the transcript is still
                # empty; later polls only carry deltas.
                last_change = time.monotonic()
                yield sse_event("snapshot", {"session": session, "messages": messages})
                snapshot_sent = True
            else:
                fresh = messages[sent:]
                if fresh:
                    last_change = time.monotonic()
                    yield sse_event("messages", {"session": session, "messages": fresh})
            sent = len(messages)

            if session["status"] != "running":
                yield sse_event("end", {"session": session})
                return
            if time.monotonic() - last_change > STREAM_IDLE_LIMIT_SECONDS:
                # The dispatcher heartbeats every few seconds; a long silence means the
                # stream lost its writer, so stop holding the connection open.
                yield sse_event("end", {"session": session})
                return

            yield ": keep-alive\n\n"
            await asyncio.sleep(STREAM_POLL_SECONDS)

    return sse_response(generate())


@router.post(
    "/projects/{project_id}/sessions",
    response_model=SessionDetail,
    status_code=201,
)
def create_session(project_id: str, body: CreateSessionRequest):
    """Called by the dispatcher: record one model conversation for a Decide/Execute run.

    A live run creates the session with status `running` before the worker starts, then
    appends messages as they arrive and finalizes it with PATCH.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        sid = next_session_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO sessions (
                id, project_id, worker, worker_type, phase, step_id, native_session,
                status, returncode, timed_out, duration_ms, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                now,
            ),
        )
        _insert_messages(conn, project_id, sid, body.messages, start=0)
        row = get_session_or_404(conn, project_id, sid)
        return SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(conn, project_id, sid),
        )


@router.post("/projects/{project_id}/sessions/{session_id}/messages", response_model=SessionDetail)
def append_session_messages(project_id: str, session_id: str, body: AppendSessionMessagesRequest):
    """Append transcript entries to a session that is still running.

    A session that already reached a final status is closed: appending would
    extend a transcript the run summary was already computed from.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_session_or_404(conn, project_id, session_id)
        if row["status"] != "running":
            raise HTTPException(409, "Session is not running")
        start = next_session_message_seq(conn, project_id, session_id)
        _insert_messages(conn, project_id, session_id, body.messages, start=start)
        _touch(conn, project_id, session_id)
        row = get_session_or_404(conn, project_id, session_id)
        return SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(conn, project_id, session_id),
        )


@router.post("/projects/{project_id}/sessions/{session_id}/heartbeat", response_model=Session)
def heartbeat_session(project_id: str, session_id: str):
    """Keep a running session alive while the worker is busy and produces no output.

    Beating a finished session is a no-op: its `updated_at` must stay at the real
    end time so the list order and staleness checks keep meaning something.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = get_session_or_404(conn, project_id, session_id)
        if row["status"] == "running":
            _touch(conn, project_id, session_id)
            row = get_session_or_404(conn, project_id, session_id)
        return session_to_model(row)


@router.patch("/projects/{project_id}/sessions/{session_id}", response_model=Session)
def finish_session(project_id: str, session_id: str, body: FinishSessionRequest):
    """Close a live session with the run's real outcome."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        get_session_or_404(conn, project_id, session_id)
        conn.execute(
            """
            UPDATE sessions
            SET status = ?, returncode = ?, timed_out = ?, duration_ms = ?, error = ?,
                native_session = COALESCE(?, native_session), updated_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                body.status,
                body.returncode,
                1 if body.timed_out else 0,
                body.duration_ms,
                body.error,
                body.native_session,
                utcnow(),
                session_id,
                project_id,
            ),
        )
        return session_to_model(get_session_or_404(conn, project_id, session_id))


def _touch(conn, project_id: str, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ? AND project_id = ?",
        (utcnow(), session_id, project_id),
    )


def _insert_messages(conn, project_id: str, session_id: str, messages, *, start: int) -> None:
    if not messages:
        return
    conn.executemany(
        "INSERT INTO session_messages (session_id, project_id, seq, role, content, raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (session_id, project_id, start + index, message.role, message.content, message.raw)
            for index, message in enumerate(messages)
        ],
    )
