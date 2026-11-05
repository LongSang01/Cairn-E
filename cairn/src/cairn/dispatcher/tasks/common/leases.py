"""Shared task helpers: Best-effort release of steps and Decide leases when a task ends."""

from __future__ import annotations

import logging

from cairn.dispatcher.protocol.client import CairnClient

LOG = logging.getLogger(__name__)


def project_allows_conclude_fallback(
    client: CairnClient, project_id: str, *, worker_name: str, step_id: str
) -> bool:
    project = client.get_project(project_id)
    if project.project.status == "active":
        return True
    LOG.info(
        "skip conclude fallback because project is no longer active project=%s step=%s worker=%s status=%s",
        project_id,
        step_id,
        worker_name,
        project.project.status,
    )
    return False


def best_effort_release_decide(client: CairnClient, project_id: str, worker_name: str) -> None:
    response = client.release_decide(project_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "decide release failed project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released decide project=%s worker=%s", project_id, worker_name)
    else:
        LOG.info(
            "decide release skipped project=%s worker=%s status=%s",
            project_id,
            worker_name,
            response.status_code,
        )


def best_effort_release(
    client: CairnClient, project_id: str, step_id: str, worker_name: str
) -> None:
    response = client.release_step(project_id, step_id, worker_name)
    if not response.ok and response.status_code not in (403, 409):
        LOG.warning(
            "release failed project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
    elif response.ok:
        LOG.info("released step project=%s step=%s worker=%s", project_id, step_id, worker_name)
    else:
        LOG.info(
            "release skipped project=%s step=%s worker=%s status=%s",
            project_id,
            step_id,
            worker_name,
            response.status_code,
        )
