"""Prometheus-style metrics for judging WAL and busy_timeout tuning.

Every series is a cheap aggregate over SQLite plus a process counter, so
scraping /metrics frequently is safe on the single-file database.
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Response

from cairn import __version__
from cairn.server import db

router = APIRouter(tags=["observability"])

_START_TIME = time.time()

_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _format_gauge(name: str, help_: str, samples: list[tuple[dict[str, str], float]]) -> list[str]:
    lines = [f"# HELP {name} {help_}", f"# TYPE {name} gauge"]
    for labels, value in samples:
        label_str = ",".join(f'{key}="{val}"' for key, val in sorted(labels.items()))
        lines.append(f"{name}{{{label_str}}} {value}" if label_str else f"{name} {value}")
    return lines


_SESSIONS_BY_STATUS = "SELECT status, COUNT(*) AS n FROM sessions GROUP BY status"
_STEPS_BY_STATUS = "SELECT status, COUNT(*) AS n FROM steps GROUP BY status"


def _status_counts(query: str) -> list[tuple[dict[str, str], float]]:
    with db.get_conn() as conn:
        rows = conn.execute(query).fetchall()
    return [({"status": row["status"]}, float(row["n"])) for row in rows]


def collect() -> str:
    """Render the current metrics in the Prometheus text exposition format."""
    lines: list[str] = []
    lines += _format_gauge(
        "cairn_build_info",
        "Build information.",
        [({"version": __version__}, 1.0)],
    )
    lines += _format_gauge(
        "cairn_process_start_time_seconds",
        "Unix time the server process started.",
        [({}, float(int(_START_TIME)))],
    )
    lines += _format_gauge(
        "cairn_db_busy_total",
        "Hard SQLite lock failures (busy_timeout exhausted) since start.",
        [({}, float(db.db_busy_total()))],
    )
    try:
        with db.get_conn() as conn:
            projects = conn.execute("SELECT COUNT(*) AS n FROM projects").fetchone()["n"]
        reachable = 1.0
        schema_version = db.SCHEMA_VERSION
    except Exception:  # noqa: BLE001 - a broken DB must not break the scrape
        schema_version = 0
        projects = 0
        reachable = 0.0
    lines += _format_gauge("cairn_db_reachable", "1 when the database answers.", [({}, reachable)])
    lines += _format_gauge(
        "cairn_schema_version", "Schema version the binary writes.", [({}, float(schema_version))]
    )
    lines += _format_gauge("cairn_projects", "Projects currently stored.", [({}, float(projects))])
    if reachable:
        lines += _format_gauge(
            "cairn_sessions", "Sessions by status.", _status_counts(_SESSIONS_BY_STATUS)
        )
        lines += _format_gauge(
            "cairn_steps",
            "Steps by status (pending steps form the dispatch queue).",
            _status_counts(_STEPS_BY_STATUS),
        )
    return "\n".join(lines) + "\n"


@router.get("/metrics", summary="Prometheus-style metrics")
def metrics() -> Response:
    return Response(content=collect(), media_type=_MEDIA_TYPE)
