from fastapi import APIRouter, HTTPException, Request, Response

from cairn.server.db import get_conn
from cairn.server.http_cache import cache_headers
from cairn.server.models import (
    CompleteRequest,
    CompleteResponse,
    Constraint,
    CreateProjectRequest,
    DecideClaimRequest,
    Fact,
    Goal,
    HeartbeatRequest,
    Hint,
    ProjectDetail,
    ProjectMeta,
    ProjectSummary,
    ReopenRequest,
    ReopenResponse,
    ScopeEntry,
    Steer,
    UpdateProjectStatusRequest,
    UpdateProjectTitleRequest,
    UpdateTokenBudgetRequest,
)
from cairn.server.services import (
    build_findings,
    build_goals,
    build_sessions,
    build_steps,
    check_project_active,
    check_project_completed,
    clear_project_decide,
    enforce_token_budget,
    expire_decide_leases,
    expire_steps,
    get_completion_step_or_409,
    get_project_or_404,
    get_root_goal_or_404,
    goal_to_model,
    next_fact_id,
    next_goal_id,
    next_hint_id,
    next_project_id,
    next_step_id,
    project_decide_from_row,
    project_meta_from_row,
    step_to_model,
    utcnow,
    validate_facts_exist,
)

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectSummary])
def list_projects(request: Request, response: Response):
    with get_conn() as conn:
        expire_steps(conn)
        expire_decide_leases(conn)
        rows = conn.execute("""
            SELECT p.id, p.title, p.status, p.created_at,
                p.decide_worker, p.decide_trigger, p.decide_started_at, p.decide_last_heartbeat_at,
                (SELECT COUNT(*) FROM facts WHERE project_id = p.id) AS fact_count,
                (SELECT COUNT(*) FROM goals WHERE project_id = p.id) AS goal_count,
                (SELECT COUNT(*) FROM goals WHERE project_id = p.id AND status = 'active') AS open_goal_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id) AS step_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id AND status = 'running') AS running_step_count,
                (SELECT COUNT(*) FROM steps WHERE project_id = p.id AND status = 'pending') AS pending_step_count,
                (SELECT COUNT(*) FROM findings WHERE project_id = p.id) AS finding_count,
                (SELECT COUNT(*) FROM hints WHERE project_id = p.id) AS hint_count
            FROM projects p
            ORDER BY p.created_at
        """).fetchall()
        summaries = [
            ProjectSummary(
                id=row["id"],
                title=row["title"],
                status=row["status"],
                created_at=row["created_at"],
                decide=project_decide_from_row(row),
                fact_count=row["fact_count"],
                goal_count=row["goal_count"],
                open_goal_count=row["open_goal_count"],
                step_count=row["step_count"],
                running_step_count=row["running_step_count"],
                pending_step_count=row["pending_step_count"],
                finding_count=row["finding_count"],
                hint_count=row["hint_count"],
            )
            for row in rows
        ]

    headers, not_modified = cache_headers(request, summaries)
    if not_modified is not None:
        return not_modified
    response.headers.update(headers)
    return summaries


@router.post("/projects", response_model=ProjectDetail, status_code=201)
def create_project(body: CreateProjectRequest):
    with get_conn() as conn:
        pid = next_project_id(conn)
        now = utcnow()

        conn.execute(
            """
            INSERT INTO projects (
                id, title, status, created_at,
                decide_worker, decide_trigger, decide_started_at, decide_last_heartbeat_at
            ) VALUES (?, ?, 'active', ?, NULL, NULL, NULL, NULL)
            """,
            (pid, body.title, now),
        )
        # origin is the initial world state: a fact, not a special case.
        conn.execute(
            "INSERT INTO facts (id, project_id, description, step_id, created_at) VALUES (?, ?, ?, NULL, ?)",
            ("origin", pid, body.origin, now),
        )
        # goal is the termination condition: a Goal node, not a fact.
        gid = next_goal_id(conn, pid)
        conn.execute(
            """
            INSERT INTO goals (id, project_id, parent_id, description, status, creator, created_at, closed_at)
            VALUES (?, ?, NULL, ?, 'active', 'system', ?, NULL)
            """,
            (gid, pid, body.goal, now),
        )

        hints = []
        if body.hints:
            for h in body.hints:
                hid = next_hint_id(conn, pid)
                conn.execute(
                    "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
                    (hid, pid, h.content, h.creator, now),
                )
                hints.append(Hint(id=hid, content=h.content, creator=h.creator, created_at=now))

        return ProjectDetail(
            project=ProjectMeta(
                id=pid, title=body.title, status="active", created_at=now, decide=None
            ),
            facts=[Fact(id="origin", description=body.origin)],
            goals=[
                Goal(
                    id=gid,
                    parent=None,
                    description=body.goal,
                    status="active",
                    creator="system",
                    created_at=now,
                )
            ],
            steps=[],
            findings=[],
            hints=hints,
        )


@router.get("/projects/{project_id}", response_model=ProjectDetail)
def get_project(project_id: str, request: Request, response: Response):
    with get_conn() as conn:
        expire_steps(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)

        facts = conn.execute(
            "SELECT id, title, description, step_id FROM facts WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        hints = conn.execute(
            "SELECT * FROM hints WHERE project_id = ? ORDER BY created_at",
            (project_id,),
        ).fetchall()
        constraints = conn.execute(
            "SELECT * FROM constraints WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        steers = conn.execute(
            "SELECT * FROM steers WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        scope_entries = conn.execute(
            "SELECT * FROM project_scope WHERE project_id = ? ORDER BY id",
            (project_id,),
        ).fetchall()

        detail = ProjectDetail(
            project=project_meta_from_row(row),
            facts=[Fact(**dict(f)) for f in facts],
            goals=build_goals(conn, project_id),
            steps=build_steps(conn, project_id),
            findings=build_findings(conn, project_id),
            hints=[Hint(**dict(h)) for h in hints],
            constraints=[Constraint(**dict(c)) for c in constraints],
            steers=[Steer(**dict(s)) for s in steers],
            scope=[ScopeEntry(**dict(s)) for s in scope_entries],
            sessions=build_sessions(conn, project_id),
        )

    headers, not_modified = cache_headers(request, detail)
    if not_modified is not None:
        return not_modified
    response.headers.update(headers)
    return detail


@router.delete("/projects/{project_id}", status_code=204)
def delete_project(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        # SQLite cannot add a FK to an existing column (migration 006 added
        # tool_rules.project_id by ALTER TABLE), so cascade these by hand.
        conn.execute("DELETE FROM tool_rules WHERE project_id = ?", (project_id,))
        conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))


@router.put("/projects/{project_id}/title", response_model=ProjectMeta)
def update_project_title(project_id: str, body: UpdateProjectTitleRequest):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute("UPDATE projects SET title = ? WHERE id = ?", (body.title, project_id))
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.put("/projects/{project_id}/budget", response_model=ProjectMeta)
def update_project_budget(project_id: str, body: UpdateTokenBudgetRequest):
    """Set (or clear) the token budget; the status follows immediately."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        conn.execute(
            "UPDATE projects SET token_budget = ? WHERE id = ?", (body.token_budget, project_id)
        )
        # Lowering it may block right away; raising it may unblock a budget block.
        enforce_token_budget(conn, project_id)
        return project_meta_from_row(get_project_or_404(conn, project_id))


@router.put("/projects/{project_id}/status", response_model=ProjectMeta)
def update_project_status(project_id: str, body: UpdateProjectStatusRequest):
    with get_conn() as conn:
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_status = row["status"]
        if current_status == "completed":
            raise HTTPException(409, "Completed projects cannot change status")
        if current_status == body.status:
            return project_meta_from_row(row)

        conn.execute("UPDATE projects SET status = ? WHERE id = ?", (body.status, project_id))
        if body.status == "stopped":
            conn.execute(
                "UPDATE steps SET worker = NULL, status = 'pending' WHERE project_id = ? AND status = 'running'",
                (project_id,),
            )
            clear_project_decide(conn, project_id)
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/decide/claim", response_model=ProjectMeta)
def claim_project_decide(project_id: str, body: DecideClaimRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["decide_worker"]
        if current_worker is not None and current_worker != body.worker:
            raise HTTPException(409, f"Project decide is currently claimed by {current_worker}")
        if current_worker == body.worker:
            return project_meta_from_row(row)

        now = utcnow()
        conn.execute(
            """
            UPDATE projects
            SET decide_worker = ?,
                decide_trigger = ?,
                decide_started_at = ?,
                decide_last_heartbeat_at = ?
            WHERE id = ?
            """,
            (body.worker, body.trigger, now, now, project_id),
        )
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/decide/heartbeat", response_model=ProjectMeta)
def heartbeat_project_decide(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["decide_worker"]
        if current_worker is None:
            raise HTTPException(409, "Project decide is not currently claimed")
        if current_worker != body.worker:
            raise HTTPException(409, f"Project decide is currently claimed by {current_worker}")

        conn.execute(
            "UPDATE projects SET decide_last_heartbeat_at = ? WHERE id = ?",
            (utcnow(), project_id),
        )
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/decide/release", response_model=ProjectMeta)
def release_project_decide(project_id: str, body: HeartbeatRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        row = get_project_or_404(conn, project_id)
        current_worker = row["decide_worker"]
        if current_worker is None:
            return project_meta_from_row(row)
        if current_worker != body.worker:
            raise HTTPException(409, f"Project decide is currently claimed by {current_worker}")

        clear_project_decide(conn, project_id)
        updated = get_project_or_404(conn, project_id)
        return project_meta_from_row(updated)


@router.post("/projects/{project_id}/complete", response_model=CompleteResponse)
def complete_project(project_id: str, body: CompleteRequest):
    """Decide declares the root goal achieved. Recorded as a completion Step."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        expire_decide_leases(conn, project_id)
        validate_facts_exist(conn, project_id, body.from_)
        root = get_root_goal_or_404(conn, project_id)
        if root["status"] != "active":
            raise HTTPException(409, f"Root goal is already {root['status']}")
        open_steps = conn.execute(
            "SELECT COUNT(*) AS n FROM steps"
            " WHERE project_id = ? AND kind = 'action' AND status IN ('pending', 'running')",
            (project_id,),
        ).fetchone()["n"]
        if open_steps:
            raise HTTPException(409, f"{open_steps} step(s) are still open")
        active_sub_goals = conn.execute(
            "SELECT COUNT(*) AS n FROM goals"
            " WHERE project_id = ? AND parent_id IS NOT NULL AND status = 'active'",
            (project_id,),
        ).fetchone()["n"]
        if active_sub_goals:
            raise HTTPException(409, f"{active_sub_goals} sub-goal(s) are still active")

        now = utcnow()
        sid = next_step_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO steps (
                id, project_id, goal_id, kind, description, rationale, priority, status,
                creator, worker, last_heartbeat_at, created_at, concluded_at, result_fact_id, note
            ) VALUES (?, ?, ?, 'completion', ?, NULL, 0, 'done', ?, ?, ?, ?, ?, NULL, 'completion')
            """,
            (
                sid,
                project_id,
                root["id"],
                body.description,
                body.worker,
                body.worker,
                now,
                now,
                now,
            ),
        )
        for fid in body.from_:
            conn.execute(
                "INSERT INTO step_sources (step_id, project_id, fact_id) VALUES (?, ?, ?)",
                (sid, project_id, fid),
            )
        conn.execute(
            "UPDATE goals SET status = 'achieved', closed_at = ? WHERE id = ? AND project_id = ?",
            (now, root["id"], project_id),
        )
        conn.execute("UPDATE projects SET status = 'completed' WHERE id = ?", (project_id,))
        clear_project_decide(conn, project_id)

        updated_project = get_project_or_404(conn, project_id)
        goal_row = get_root_goal_or_404(conn, project_id)
        step_row = conn.execute(
            "SELECT * FROM steps WHERE id = ? AND project_id = ?", (sid, project_id)
        ).fetchone()
        return CompleteResponse(
            project=project_meta_from_row(updated_project),
            goal=goal_to_model(goal_row),
            step=step_to_model(conn, step_row, project_id),
        )


@router.post("/projects/{project_id}/reopen", response_model=ReopenResponse)
def reopen_project(project_id: str, body: ReopenRequest):
    with get_conn() as conn:
        expire_decide_leases(conn, project_id)
        check_project_completed(conn, project_id)
        completion = get_completion_step_or_409(conn, project_id)

        source_rows = conn.execute(
            "SELECT fact_id FROM step_sources WHERE step_id = ? AND project_id = ? ORDER BY rowid",
            (completion["id"], project_id),
        ).fetchall()
        source_ids = [row["fact_id"] for row in source_rows]
        if not source_ids:
            raise HTTPException(409, "Completion step is missing its source facts")

        now = utcnow()
        fact_id = next_fact_id(conn, project_id)
        root = get_root_goal_or_404(conn, project_id)

        # The completion decision is withdrawn; the external feedback becomes a fact, so the
        # next Decide run sees a changed graph and plans again.
        conn.execute(
            "DELETE FROM steps WHERE id = ? AND project_id = ?",
            (completion["id"], project_id),
        )
        conn.execute(
            "INSERT INTO facts (id, project_id, description, step_id, created_at) VALUES (?, ?, ?, NULL, ?)",
            (fact_id, project_id, body.description, now),
        )
        conn.execute(
            "UPDATE goals SET status = 'active', closed_at = NULL WHERE id = ? AND project_id = ?",
            (root["id"], project_id),
        )
        clear_project_decide(conn, project_id)
        conn.execute("UPDATE projects SET status = 'active' WHERE id = ?", (project_id,))

        updated_project = get_project_or_404(conn, project_id)
        goal_row = get_root_goal_or_404(conn, project_id)
        return ReopenResponse(
            project=project_meta_from_row(updated_project),
            fact=Fact(id=fact_id, description=body.description),
            goal=goal_to_model(goal_row),
        )
