from fastapi import APIRouter

from cairn.contracts.models import (
    Document,
    DocumentMeta,
)
from cairn.server.db import get_conn
from cairn.server.models import CreateDocumentRequest
from cairn.server.services.documents import (
    build_documents,
    document_to_model,
    insert_document,
)
from cairn.server.services.guards import (
    check_project_active,
    get_document_or_404,
    get_project_or_404,
)

router = APIRouter(tags=["documents"])


@router.get("/projects/{project_id}/documents", response_model=list[DocumentMeta])
def list_documents(project_id: str):
    """Headers only: the graph and this list never carry document bodies."""
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return build_documents(conn, project_id)


@router.get("/projects/{project_id}/documents/{document_id}", response_model=Document)
def get_document(project_id: str, document_id: str):
    with get_conn() as conn:
        get_project_or_404(conn, project_id)
        return document_to_model(get_document_or_404(conn, project_id, document_id))


@router.post("/projects/{project_id}/documents", response_model=Document, status_code=201)
def create_document(project_id: str, body: CreateDocumentRequest):
    with get_conn() as conn:
        check_project_active(conn, project_id)
        return insert_document(
            conn,
            project_id,
            title=body.title,
            content=body.content,
            fact_id=body.fact_id,
            step_id=body.step_id,
        )
