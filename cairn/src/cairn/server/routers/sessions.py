import asyncio
import time
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, Query, Request, Response
from starlette.concurrency import run_in_threadpool

from cairn.server.db import get_conn
from cairn.server.http_cache import cache_headers
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
def list_sessions(request: Request, response: Response, project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        sessions = build_sessions(conn, project_id)
    headers, not_modified = cache_headers(request, sessions)
    response.headers.update(headers)
    return not_modified or sessions


@router.get("/projects/{project_id}/sessions/{session_id}", response_model=SessionDetail)
def get_session(
    request: Request,
    response: Response,
    project_id: str,
    session_id: str,
    since_seq: int | None = Query(default=None, ge=0),
):
    """One session with its transcript.

    `since_seq` keeps long sessions cheap to re-poll: pass the highest seq already
    seen and the response carries only newer messages (an empty list when caught up).
    Omit it for the full transcript.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_stale_sessions(conn, project_id)
        row = get_session_or_404(conn, project_id, session_id)
        detail = SessionDetail(
            **session_to_model(row).model_dump(),
            messages=build_session_messages(
                conn, project_id, session_id, since_seq=since_seq
            ),
        )
    headers, not_modified = cache_headers(request, detail)
    response.headers.update(headers)
    return not_modified or detail


@router.get("/projects/{project_id}/sessions/{session_id}/stream")
def stream_session(
    request: Request,
    project_id: str,
    session_id: str,
    since_seq: int | None = Query(default=None, ge=0),
):
    """Server-sent events for one session: the full transcript, then every new message.

    The first event is always a `snapshot` so a client that connects mid-run does not
    need a separate request. After that each new message arrives as a `messages` event
    carrying only the messages the client has not seen yet.

    A reconnecting client resumes instead of re-downloading: every event's `id:` is the
    newest message seq it carries, and a client that sends that id back as
    `Last-Event-ID` (or `?since_seq=`) continues with only newer messages and no
    snapshot.
    """
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        expire_stale_sessions(conn, project_id)
        get_session_or_404(conn, project_id, session_id)

    resume_from = _resume_from(request, since_seq)
    return sse_response(
        session_event_stream(project_id, session_id, last_event_id=resume_from)
    )


def _resume_from(request: Request, since_seq: int | None) -> int | None:
    """The client's resume token: its `Last-Event-ID` header wins over `since_seq`.

    A non-integer header (some proxies mangle it) falls back to a fresh snapshot,
    which is always safe — it just re-sends what the client already had.
    """
    raw = request.headers.get("last-event-id")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            return None
    return since_seq


async def session_event_stream(
    project_id: str, session_id: str, *, last_event_id: int | None = None
) -> AsyncIterator[str]:
    """The SSE body for one session, kept separate so it can be driven without HTTP.

    ``TestClient`` buffers a whole response, so an open stream cannot be observed while
    it is still polling; tests consume this generator directly instead. Every event is
    tagged with the newest message seq it carries, so a dropped connection resumes from
    `last_event_id` instead of replaying the whole transcript.
    """

    def poll(since: int | None) -> tuple[dict, list[dict]] | None:
        """Read one transcript slice off the event loop (sqlite3 is blocking)."""
        with get_conn() as conn:
            expire_stale_sessions(conn, project_id)
            try:
                row = get_session_or_404(conn, project_id, session_id)
            except HTTPException:
                return None
            messages = build_session_messages(conn, project_id, session_id, since_seq=since)
            return session_to_model(row).model_dump(), [m.model_dump() for m in messages]

    # A resumed connection rendered its snapshot before the network dropped: it
    # continues from the last id it saw and never gets a second snapshot.
    resuming = last_event_id is not None
    snapshot_sent = resuming
    sent_seq = last_event_id
    last_change = time.monotonic()
    while True:
        result = await run_in_threadpool(poll, sent_seq if snapshot_sent else None)
        if result is None:
            yield sse_event("gone", {"id": session_id})
            return
        session, messages = result

        if not snapshot_sent:
            # One snapshot per connection, even while the transcript is still
            # empty; later polls only carry deltas. Its id is the newest seq so a
            # reconnect resumes exactly after this point (-1 when empty, which
            # replays everything on resume).
            last_change = time.monotonic()
            newest = messages[-1]["seq"] if messages else -1
            yield sse_event(
                "snapshot", {"session": session, "messages": messages}, event_id=newest
            )
            snapshot_sent = True
            sent_seq = newest
        elif messages:
            last_change = time.monotonic()
            yield sse_event(
                "messages",
                {"session": session, "messages": messages},
                event_id=messages[-1]["seq"],
            )
            sent_seq = messages[-1]["seq"]

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
        now = utcnow()
        existing = None
        if body.client_key is not None:
            existing = conn.execute(
                "SELECT * FROM sessions WHERE project_id = ? AND client_key = ?",
                (project_id, body.client_key),
            ).fetchone()
        if existing is not None:
            # The same upload already landed — a retried create, or the end-of-run
            # fallback after a lost live create. Converge onto that session instead
            # of recording the run twice: new messages merge in, and a real outcome
            # finalizes a row that is still `running`.
            sid = existing["id"]
            _insert_messages(
                conn,
                project_id,
                sid,
                body.messages,
                start=next_session_message_seq(conn, project_id, sid),
            )
            if existing["status"] == "running" and body.status != "running":
                _finalize_session(conn, project_id, sid, body)
            row = get_session_or_404(conn, project_id, sid)
            return SessionDetail(
                **session_to_model(row).model_dump(),
                messages=build_session_messages(conn, project_id, sid),
            )
        sid = next_session_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO sessions (
                id, project_id, worker, worker_type, phase, step_id, summary, native_session,
                status, returncode, timed_out, duration_ms,
                input_tokens, output_tokens, cache_read_tokens, cache_write_tokens, total_tokens,
                error, client_key, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                sid,
                project_id,
                body.worker,
                body.worker_type,
                body.phase,
                body.step,
                body.summary,
                body.native_session,
                body.status,
                body.returncode,
                1 if body.timed_out else 0,
                body.duration_ms,
                body.input_tokens,
                body.output_tokens,
                body.cache_read_tokens,
                body.cache_write_tokens,
                body.total_tokens,
                body.error,
                body.client_key,
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
        _finalize_session(conn, project_id, session_id, body)
        return session_to_model(get_session_or_404(conn, project_id, session_id))


def _touch(conn, project_id: str, session_id: str) -> None:
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE id = ? AND project_id = ?",
        (utcnow(), session_id, project_id),
    )


def _finalize_session(conn, project_id: str, session_id: str, body) -> None:
    """Record a run's real outcome on the session row.

    Shared by PATCH /sessions/{id} and the idempotent create path, where a retried
    end-of-run upload finalizes the `running` row its first attempt created.
    """
    conn.execute(
        """
        UPDATE sessions
        SET status = ?, returncode = ?, timed_out = ?, duration_ms = ?, error = ?,
            input_tokens = ?, output_tokens = ?, cache_read_tokens = ?,
            cache_write_tokens = ?, total_tokens = ?,
            native_session = COALESCE(?, native_session), updated_at = ?
        WHERE id = ? AND project_id = ?
        """,
        (
            body.status,
            body.returncode,
            1 if body.timed_out else 0,
            body.duration_ms,
            body.error,
            body.input_tokens,
            body.output_tokens,
            body.cache_read_tokens,
            body.cache_write_tokens,
            body.total_tokens,
            body.native_session,
            utcnow(),
            session_id,
            project_id,
        ),
    )


def _insert_messages(conn, project_id: str, session_id: str, messages, *, start: int) -> None:
    """Insert transcript entries, skipping client ids that already landed.

    Uploads are retried after network failures: a message carrying a `msg_id` must
    land exactly once no matter how many times its batch is re-sent. Messages without
    a `msg_id` have nothing to dedupe on and are always inserted.
    """
    seq = start
    for message in messages:
        if message.msg_id is not None:
            duplicate = conn.execute(
                "SELECT 1 FROM session_messages WHERE session_id = ? AND client_msg_id = ?",
                (session_id, message.msg_id),
            ).fetchone()
            if duplicate is not None:
                continue
        conn.execute(
            "INSERT INTO session_messages (session_id, project_id, seq, role, content, raw, client_msg_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (session_id, project_id, seq, message.role, message.content, message.raw, message.msg_id),
        )
        seq += 1
