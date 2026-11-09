from fastapi import APIRouter

from cairn.contracts.models import Hint
from cairn.server.db import get_conn
from cairn.server.models import CreateHintRequest
from cairn.server.services.documents import utcnow
from cairn.server.services.guards import check_project_hint_writable
from cairn.server.services.ids import next_hint_id

router = APIRouter(tags=["hints"])


@router.post(
    "/projects/{project_id}/hints",
    response_model=Hint,
    status_code=201,
)
def create_hint(project_id: str, body: CreateHintRequest):
    with get_conn() as conn:
        check_project_hint_writable(conn, project_id)

        now = utcnow()
        hid = next_hint_id(conn, project_id)
        conn.execute(
            "INSERT INTO hints (id, project_id, content, creator, created_at) VALUES (?, ?, ?, ?, ?)",
            (hid, project_id, body.content, body.creator, now),
        )
        return Hint(id=hid, content=body.content, creator=body.creator, created_at=now)
