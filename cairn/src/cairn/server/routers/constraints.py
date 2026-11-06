"""Constraints: the authorization boundary Decide and Execute must obey.

A constraint is not a hint. A hint is free judgement the graph absorbs; a
constraint outranks the goal and is rendered ahead of the graph in every prompt.
Like ARTEX's task constraints, nothing here is enforced at the tool level: the
tool guard keeps reading its own rules so a constraint (or a prompt injection
inside one) can never rewrite what a command is allowed to do.
"""

from fastapi import APIRouter, HTTPException

from cairn.contracts.models import Constraint
from cairn.server.db import get_conn
from cairn.server.models import CreateConstraintRequest
from cairn.server.services import (
    check_project_hint_writable,
    get_project_or_404,
    next_constraint_id,
    utcnow,
)

router = APIRouter(tags=["constraints"])


def _constraint_from_row(row) -> Constraint:
    return Constraint(
        id=row["id"],
        kind=row["kind"],
        text=row["text"],
        origin=row["origin"],
        created_at=row["created_at"],
    )


@router.get("/projects/{project_id}/constraints", response_model=list[Constraint])
def list_constraints(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM constraints WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        return [_constraint_from_row(row) for row in rows]


@router.post(
    "/projects/{project_id}/constraints",
    response_model=Constraint,
    status_code=201,
)
def create_constraint(project_id: str, body: CreateConstraintRequest):
    """Add one rule. Edits take effect on the next Decide / Execute run.

    Nothing is pushed to a running worker on purpose: its prompt is already
    built, and interrupting a run to inject policy would drop the work it is
    about to submit. The next run re-reads the project and sees the rule.
    """
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        cid = next_constraint_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO constraints (id, project_id, kind, text, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cid, project_id, body.kind, body.text, body.origin, now),
        )
        return Constraint(
            id=cid, kind=body.kind, text=body.text, origin=body.origin, created_at=now
        )


@router.delete("/projects/{project_id}/constraints/{constraint_id}", status_code=204)
def delete_constraint(project_id: str, constraint_id: str):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)
        cursor = conn.execute(
            "DELETE FROM constraints WHERE id = ? AND project_id = ?",
            (constraint_id, project_id),
        )
        if cursor.rowcount == 0:
            raise HTTPException(404, f"Constraint {constraint_id} not found")
        return None
