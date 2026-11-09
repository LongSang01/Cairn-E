"""In-flight instructions to the planner (ARTEX's chat / steer_work equivalent).

Cairn's Decide is its planner: a message lands here, the next Decide run sees it
in its prompt, and its *actions* are the graph change (a new constraint, a step
note, a cancelled running step). The planner also writes a short textual reply
onto the same row via ``/steers/{id}/answer``. Messages are append-only so a
worker that starts later still sees the whole instruction history.
"""

from fastapi import APIRouter, HTTPException

from cairn.contracts.models import Steer
from cairn.server.db import get_conn
from cairn.server.models import AnswerSteerRequest, CreateSteerRequest
from cairn.server.services.documents import utcnow
from cairn.server.services.guards import (
    check_project_active,
    get_project_or_404,
)
from cairn.server.services.ids import next_steer_id

router = APIRouter(tags=["steers"])


@router.get("/projects/{project_id}/steers", response_model=list[Steer])
def list_steers(project_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        rows = conn.execute(
            "SELECT * FROM steers WHERE project_id = ? ORDER BY rowid",
            (project_id,),
        ).fetchall()
        return [Steer(**dict(row)) for row in rows]


@router.post(
    "/projects/{project_id}/steers",
    response_model=Steer,
    status_code=201,
)
def create_steer(project_id: str, body: CreateSteerRequest):
    """Append one instruction.

    It reaches the next Decide run (not the one already in flight), and the pi
    guard delivers it at the *next tool call* of a running worker by blocking
    that single call. Neither path kills a run or throws away work in progress.
    """
    with get_conn() as conn:
        check_project_active(conn, project_id)
        sid = next_steer_id(conn, project_id)
        now = utcnow()
        conn.execute(
            """
            INSERT INTO steers (id, project_id, text, creator, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (sid, project_id, body.text, body.creator, now),
        )
        return Steer(id=sid, text=body.text, creator=body.creator, created_at=now)


@router.post(
    "/projects/{project_id}/steers/{steer_id}/answer",
    response_model=Steer,
)
def answer_steer(project_id: str, steer_id: str, body: AnswerSteerRequest):
    """The planner's reply. Idempotent: retrying a run keeps the first answer."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        row = conn.execute(
            "SELECT * FROM steers WHERE id = ? AND project_id = ?",
            (steer_id, project_id),
        ).fetchone()
        if row is None:
            raise HTTPException(404, f"Steer {steer_id} not found")
        # Conditional UPDATE, not check-then-update: two concurrent Decide retries
        # must not overwrite the first answer.
        conn.execute(
            """
            UPDATE steers SET answer = ?, answered_at = ?
            WHERE id = ? AND project_id = ? AND answer IS NULL
            """,
            (body.text, utcnow(), steer_id, project_id),
        )
        row = conn.execute(
            "SELECT * FROM steers WHERE id = ? AND project_id = ?",
            (steer_id, project_id),
        ).fetchone()
        return Steer(**dict(row))
