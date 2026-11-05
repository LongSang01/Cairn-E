"""Shared task helpers: Persisting the outcome of an Execute run in one server write."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from cairn.dispatcher.protocol.client import CairnClient

from .leases import best_effort_release

LOG = logging.getLogger(__name__)

@dataclass(slots=True)
class StepWriteResult:
    status: str
    fact_ids: list[str]


def write_step_result(
    client: CairnClient,
    project_id: str,
    step_id: str,
    worker_name: str,
    result: dict,
    *,
    source: str,
    phase_ms: int,
    total_ms: int | None = None,
) -> StepWriteResult:
    """Persist submit_fact / submit_finding plus the step closure in a single write."""
    response = client.conclude_step(
        project_id,
        step_id,
        worker_name,
        facts=result["facts"],
        findings=result["findings"],
        status=result["status"],
        note=result.get("note"),
    )
    if response.ok:
        fact_ids: list[str] = []
        if isinstance(response.data, dict):
            for fact in response.data.get("facts") or []:
                if isinstance(fact, dict) and isinstance(fact.get("id"), str):
                    fact_ids.append(fact["id"])
        LOG.info(
            "step concluded project=%s step=%s worker=%s source=%s status=%s facts=%s findings=%s "
            "phase_ms=%s total_ms=%s",
            project_id,
            step_id,
            worker_name,
            source,
            result["status"],
            fact_ids,
            len(result["findings"]),
            phase_ms,
            total_ms,
        )
        return StepWriteResult(status="success", fact_ids=fact_ids)
    if response.status_code == 403:
        LOG.info(
            "project became inactive during step conclude project=%s step=%s worker=%s",
            project_id,
            step_id,
            worker_name,
        )
    else:
        LOG.warning(
            "step conclude write failed project=%s step=%s worker=%s status=%s body=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
            response.text,
        )
    best_effort_release(client, project_id, step_id, worker_name)
    return StepWriteResult(status="failed", fact_ids=[])
