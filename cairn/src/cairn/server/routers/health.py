"""Liveness probe for supervisors and container health checks."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from cairn import __version__
from cairn.contracts import CONTRACT_VERSION
from cairn.server.db import get_conn

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(response: Response):
    """Report process liveness plus whether the database is reachable."""
    try:
        with get_conn() as conn:
            conn.execute("SELECT 1").fetchone()
            row = conn.execute(
                "SELECT MAX(version) AS version FROM schema_migrations"
            ).fetchone()
    except Exception:  # noqa: BLE001 - a probe must answer even when the DB is broken
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "database": "unavailable"}
    return {
        "status": "ok",
        "version": __version__,
        "database": "ok",
        "schema_version": row["version"] or 0,
        "contract_version": CONTRACT_VERSION,
    }
