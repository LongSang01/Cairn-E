from fastapi import APIRouter

from cairn.server.db import get_conn
from cairn.server.models import (
    ConcludeStepRequest,
    ConcludeStepResponse,
    CreateStepRequest,
    Fact,
    Finding,
    HeartbeatRequest,
    Step,
    UpdateStepRequest,
)
from cairn.server.services import (
    check_project_active,
    get_claimable_step_or_404,
    get_releasable_step_or_404,
    get_step_or_404,
    next_fact_id,
    next_finding_id,
    next_step_id,
    step_to_model,
    utcnow,
    validate_creator_worker,
    validate_facts_exist,
    validate_goal_exists,
)

router = APIRouter(tags=["steps"])


def _refresh_step(conn, project_id: str, step_id: str) -> Step:
    row = conn.execute(
        "SELECT * FROM steps WHERE id = ? AND project_id = ?", (step_id, project_id)
    ).fetchone()
    return step_to_model(conn, row, project_id)


@router.post("/projects/{project_id}/steps", response_model=Step, status_code=201)
def create_step(project_id: str, body: CreateStepRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        if body.goal is not None:
            validate_goal_exists(conn, project_id, body.goal)
        validate_creator_worker(body.creator, body.worker)

        now = utcnow()
        sid = next_step_id(conn, project_id)
        claimed = body.worker is not None
        conn.execute(
            """
            INSERT INTO steps (
                id, project_id, goal_id, kind, description, rationale, priority, status,
                creator, worker, last_heartbeat_at, created_at, concluded_at, result_fact_id, note
            ) VALUES (?, ?, ?, 'action', ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)
            """,
            (
                sid,
                project_id,
                body.goal,
                body.description,
                body.rationale,
                body.priority,
                "running" if claimed else "pending",
                body.creator,
                body.worker,
                now if claimed else None,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (sid, project_id, fid),
            )
        return _refresh_step(conn, project_id, sid)


@router.post("/projects/{project_id}/steps/{step_id}/heartbeat", response_model=Step)
def heartbeat(project_id: str, step_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_step_or_404(conn, project_id, step_id, body.worker)

        conn.execute(
            """
            UPDATE steps SET worker = ?, status = 'running', last_heartbeat_at = ?
            WHERE id = ? AND project_id = ?
            """,
            (body.worker, utcnow(), step_id, project_id),
        )
        return _refresh_step(conn, project_id, step_id)


@router.post("/projects/{project_id}/steps/{step_id}/release", response_model=Step)
def release(project_id: str, step_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_releasable_step_or_404(conn, project_id, step_id, body.worker)

        if row["worker"] == body.worker:
            conn.execute(
                "UPDATE steps SET worker = NULL, status = 'pending' WHERE id = ? AND project_id = ?",
                (step_id, project_id),
            )
        return _refresh_step(conn, project_id, step_id)


@router.post("/projects/{project_id}/steps/{step_id}/update", response_model=Step)
def update_step(project_id: str, step_id: str, body: UpdateStepRequest):
    """Decide's reprioritize / abandon operation. Steps are never deleted."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_step_or_404(conn, project_id, step_id)
        if row["status"] in ("done", "abandoned"):
            return step_to_model(conn, row, project_id)

        now = utcnow()
        if body.priority is not None:
            conn.execute(
                "UPDATE steps SET priority = ? WHERE id = ? AND project_id = ?",
                (body.priority, step_id, project_id),
            )
        if body.status == "abandoned":
            conn.execute(
                """
                UPDATE steps
                SET status = 'abandoned', worker = NULL, concluded_at = ?, note = ?
                WHERE id = ? AND project_id = ?
                """,
                (now, body.note, step_id, project_id),
            )
        return _refresh_step(conn, project_id, step_id)


@router.post(
    "/projects/{project_id}/steps/{step_id}/conclude",
    response_model=ConcludeStepResponse,
)
def conclude(project_id: str, step_id: str, body: ConcludeStepRequest):
    """Execute's submit_fact / submit_finding and step closure in one atomic write."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        get_claimable_step_or_404(conn, project_id, step_id, body.worker)

        now = utcnow()
        facts: list[Fact] = []
        findings: list[Finding] = []

        for submitted in body.submitted_facts():
            fid = next_fact_id(conn, project_id)
            conn.execute(
                "INSERT INTO facts (id, project_id, description, step_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (fid, project_id, submitted.description, step_id, now),
            )
            facts.append(Fact(id=fid, description=submitted.description))

        for submitted in body.findings or []:
            did = next_finding_id(conn, project_id)
            conn.execute(
                """
                INSERT INTO findings (id, project_id, step_id, kind, description, creator, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (did, project_id, step_id, submitted.kind, submitted.description, body.worker, now),
            )
            findings.append(
                Finding(
                    id=did,
                    kind=submitted.kind,
                    description=submitted.description,
                    step=step_id,
                    creator=body.worker,
                    created_at=now,
                )
            )

        conn.execute(
            """
            UPDATE steps
            SET status = ?, worker = ?, last_heartbeat_at = ?, concluded_at = ?,
                result_fact_id = ?, note = ?
            WHERE id = ? AND project_id = ?
            """,
            (
                body.status,
                body.worker,
                now,
                now,
                facts[0].id if facts else None,
                body.note,
                step_id,
                project_id,
            ),
        )
        return ConcludeStepResponse(
            facts=facts,
            findings=findings,
            step=_refresh_step(conn, project_id, step_id),
        )
