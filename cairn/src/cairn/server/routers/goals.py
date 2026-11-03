from fastapi import APIRouter, HTTPException

from cairn.server.db import get_conn
from cairn.server.models import CloseGoalRequest, CreateGoalRequest, Goal
from cairn.server.services import (
    check_project_active,
    get_goal_or_404,
    get_root_goal_or_404,
    goal_to_model,
    next_goal_id,
    utcnow,
    validate_goal_exists,
)

router = APIRouter(tags=["goals"])


@router.post("/projects/{project_id}/goals", response_model=Goal, status_code=201)
def create_goal(project_id: str, body: CreateGoalRequest):
    """Decide adds a sub goal. Omitting parent attaches it to the root goal."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        root = get_root_goal_or_404(conn, project_id)
        parent = body.parent or root["id"]
        if parent != root["id"]:
            validate_goal_exists(conn, project_id, parent)

        now = utcnow()
        gid = next_goal_id(conn, project_id)
        conn.execute(
            """
            INSERT INTO goals (id, project_id, parent_id, description, status, creator, created_at, closed_at)
            VALUES (?, ?, ?, ?, 'active', ?, ?, NULL)
            """,
            (gid, project_id, parent, body.description, body.creator, now),
        )
        row = get_goal_or_404(conn, project_id, gid)
        return goal_to_model(row)


@router.post("/projects/{project_id}/goals/{goal_id}/close", response_model=Goal)
def close_goal(project_id: str, goal_id: str, body: CloseGoalRequest):
    """Decide achieves or drops a sub goal. The root goal is closed by /complete."""
    with get_conn() as conn:
        check_project_active(conn, project_id)
        row = get_goal_or_404(conn, project_id, goal_id)
        if row["parent_id"] is None:
            raise HTTPException(409, "The root goal is closed by completing the project")
        if row["status"] != "active":
            if row["status"] == body.status:
                return goal_to_model(row)
            raise HTTPException(409, f"Goal is already {row['status']}")

        now = utcnow()
        conn.execute(
            "UPDATE goals SET status = ?, closed_at = ? WHERE id = ? AND project_id = ?",
            (body.status, now, goal_id, project_id),
        )
        updated = get_goal_or_404(conn, project_id, goal_id)
        return goal_to_model(updated)
