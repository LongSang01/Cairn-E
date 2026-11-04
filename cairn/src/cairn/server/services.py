from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from cairn.server.models import (
    ContextSummary,
    Finding,
    Goal,
    ProjectDecide,
    ProjectMeta,
    Question,
    Session,
    SessionMessage,
    Step,
)

PROJECT_COLUMNS = (
    "id, title, status, created_at, decide_worker, decide_trigger, "
    "decide_started_at, decide_last_heartbeat_at"
)

# Session rows carry their message count so the list view can show live progress.
SESSION_COLUMNS = (
    "s.*, (SELECT COUNT(*) FROM session_messages m "
    "WHERE m.project_id = s.project_id AND m.session_id = s.id) AS message_count"
)

# A running session is heartbeated every few seconds; silence means the dispatcher died.
SESSION_STALE_SECONDS = 60


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def utcnow_before(seconds: int) -> str:
    moment = datetime.now(UTC) - timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


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


def next_session_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "session", "x", project_id)


def next_session_message_seq(conn: sqlite3.Connection, project_id: str, session_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(seq) + 1, 0) FROM session_messages WHERE project_id = ? AND session_id = ?",
        (project_id, session_id),
    ).fetchone()
    return int(row[0])


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


def validate_facts_exist(
    conn: sqlite3.Connection, project_id: str, fact_ids: list[str]
) -> None:
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


def get_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str
) -> sqlite3.Row:
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
    if row["status"] in ("done", "abandoned"):
        raise HTTPException(409, f"Step already {row['status']}")
    if row["worker"] is not None and row["worker"] != worker:
        raise HTTPException(409, f"Step is currently claimed by {row['worker']}")
    return row


def get_releasable_step_or_404(
    conn: sqlite3.Connection, project_id: str, step_id: str, worker: str
) -> sqlite3.Row:
    expire_steps(conn, project_id)
    row = get_step_or_404(conn, project_id, step_id)
    if row["status"] in ("done", "abandoned"):
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


def get_goal_or_404(
    conn: sqlite3.Connection, project_id: str, goal_id: str
) -> sqlite3.Row:
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


def goal_to_model(row: sqlite3.Row) -> Goal:
    return Goal(
        id=row["id"],
        parent=row["parent_id"],
        description=row["description"],
        status=row["status"],
        creator=row["creator"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
    )


def build_goals(conn: sqlite3.Connection, project_id: str) -> list[Goal]:
    rows = conn.execute(
        "SELECT * FROM goals WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [goal_to_model(row) for row in rows]


def step_to_model(conn: sqlite3.Connection, row: sqlite3.Row, project_id: str) -> Step:
    sources = conn.execute(
        "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
        (row["id"], project_id),
    ).fetchall()
    return Step(
        id=row["id"],
        from_=[s["fact_id"] for s in sources],
        to=row["result_fact_id"],
        goal=row["goal_id"],
        kind=row["kind"],
        description=row["description"],
        rationale=row["rationale"],
        priority=row["priority"],
        status=row["status"],
        creator=row["creator"],
        worker=row["worker"],
        last_heartbeat_at=row["last_heartbeat_at"],
        created_at=row["created_at"],
        concluded_at=row["concluded_at"],
        note=row["note"],
    )


def build_steps(conn: sqlite3.Connection, project_id: str) -> list[Step]:
    rows = conn.execute(
        "SELECT * FROM steps WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [step_to_model(conn, row, project_id) for row in rows]


def build_findings(conn: sqlite3.Connection, project_id: str) -> list[Finding]:
    rows = conn.execute(
        "SELECT * FROM findings WHERE project_id = ? ORDER BY created_at, rowid",
        (project_id,),
    ).fetchall()
    return [
        Finding(
            id=row["id"],
            kind=row["kind"],
            description=row["description"],
            step=row["step_id"],
            creator=row["creator"],
            created_at=row["created_at"],
        )
        for row in rows
    ]


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


def session_to_model(row: sqlite3.Row) -> Session:
    return Session(
        id=row["id"],
        worker=row["worker"],
        worker_type=row["worker_type"],
        phase=row["phase"],
        step=row["step_id"],
        native_session=row["native_session"],
        status=row["status"],
        returncode=row["returncode"],
        timed_out=bool(row["timed_out"]),
        duration_ms=row["duration_ms"],
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
    conn: sqlite3.Connection, project_id: str, session_id: str
) -> list[SessionMessage]:
    rows = conn.execute(
        "SELECT seq, role, content, raw FROM session_messages "
        "WHERE project_id = ? AND session_id = ? ORDER BY seq",
        (project_id, session_id),
    ).fetchall()
    return [
        SessionMessage(seq=r["seq"], role=r["role"], content=r["content"], raw=r["raw"])
        for r in rows
    ]


def get_session_or_404(conn: sqlite3.Connection, project_id: str, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        f"SELECT {SESSION_COLUMNS} FROM sessions s WHERE s.id = ? AND s.project_id = ?",
        (session_id, project_id),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Session not found")
    return row


# ------------------------------------------------------------------------- questions

# A pending question nobody claimed, or a running one whose dispatcher died.
QUESTION_PENDING_TTL_SECONDS = 300
QUESTION_RUNNING_TTL_SECONDS = 900


def next_question_id(conn: sqlite3.Connection, project_id: str) -> str:
    return _next_scoped_id(conn, "question", "q", project_id)


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


def get_question_or_404(conn: sqlite3.Connection, project_id: str, question_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM questions WHERE id = ? AND project_id = ?", (question_id, project_id)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Question not found")
    return row


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


# -------------------------------------------------------------------- context summary


_CONTEXT_COLUMNS = (
    "context_summary, context_summary_facts,"
    " context_step_summary, context_step_summary_facts,"
    " context_finding_summary, context_finding_summary_facts"
)


def get_context_summary(conn: sqlite3.Connection, project_id: str) -> ContextSummary:
    row = conn.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")
    return _context_summary_from_row(row)


def save_context_summary(
    conn: sqlite3.Connection,
    project_id: str,
    summary: str,
    facts_covered: int,
    *,
    step_summary: str = "",
    steps_covered: int = 0,
    finding_summary: str = "",
    findings_covered: int = 0,
) -> ContextSummary:
    """Merge summaries in, letting each channel only move forward.

    Summaries are written by background tasks, so a slow one must never clobber a newer
    one: a channel is replaced only when its coverage count does not go backwards.
    """
    row = conn.execute(
        f"SELECT {_CONTEXT_COLUMNS} FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Project not found")

    facts_text, facts_count = _merge_channel(
        row["context_summary"], row["context_summary_facts"], summary, facts_covered
    )
    steps_text, steps_count = _merge_channel(
        row["context_step_summary"], row["context_step_summary_facts"], step_summary, steps_covered
    )
    findings_text, findings_count = _merge_channel(
        row["context_finding_summary"],
        row["context_finding_summary_facts"],
        finding_summary,
        findings_covered,
    )
    merged = ContextSummary(
        summary=facts_text,
        facts_covered=facts_count,
        step_summary=steps_text,
        steps_covered=steps_count,
        finding_summary=findings_text,
        findings_covered=findings_count,
    )
    conn.execute(
        "UPDATE projects SET context_summary = ?, context_summary_facts = ?,"
        " context_step_summary = ?, context_step_summary_facts = ?,"
        " context_finding_summary = ?, context_finding_summary_facts = ? WHERE id = ?",
        (
            merged.summary,
            merged.facts_covered,
            merged.step_summary,
            merged.steps_covered,
            merged.finding_summary,
            merged.findings_covered,
            project_id,
        ),
    )
    return merged


def _merge_channel(
    current: str | None, current_covered: int | None, new: str, covered: int
) -> tuple[str, int]:
    """Keep whichever side covers more history; a stale summary must not clobber a newer one."""
    previous_text = current or ""
    previous_covered = int(current_covered or 0)
    if not new or covered < previous_covered:
        return previous_text, previous_covered
    return new, covered


def _context_summary_from_row(row: sqlite3.Row) -> ContextSummary:
    return ContextSummary(
        summary=row["context_summary"] or "",
        facts_covered=int(row["context_summary_facts"] or 0),
        step_summary=row["context_step_summary"] or "",
        steps_covered=int(row["context_step_summary_facts"] or 0),
        finding_summary=row["context_finding_summary"] or "",
        findings_covered=int(row["context_finding_summary_facts"] or 0),
    )
