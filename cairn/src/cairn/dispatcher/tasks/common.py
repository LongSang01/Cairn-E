from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import ProcessResult

PROCESS_COMMUNICATE_GRACE_SECONDS = 15
LOG_PREVIEW_LIMIT = 1200
GRAPH_SNAPSHOT_ROOT = "/tmp/cairn-prompts"
LOG = logging.getLogger(__name__)


@dataclass(slots=True)
class StepWriteResult:
    status: str
    fact_ids: list[str]


def preview(text: str, limit: int = LOG_PREVIEW_LIMIT) -> str:
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "..."


def did_timeout(result: ProcessResult) -> bool:
    return not result.cancelled and (result.timed_out or result.returncode in (124, 137))


def cancel_reason(result: ProcessResult, cancellation: TaskCancellation | None = None) -> str | None:
    if result.cancelled:
        return result.cancel_reason or "cancelled"
    if cancellation is not None:
        return cancellation.reason
    return None


def communicate_timeout(timeout_seconds: int, grace_seconds: int = PROCESS_COMMUNICATE_GRACE_SECONDS) -> int:
    return timeout_seconds + grace_seconds


def task_healthcheck_enabled(config: DispatchConfig) -> bool:
    if config.runtime.execution == "local":
        return False
    return config.runtime.worker_healthcheck == "startup_and_task"


def write_graph_snapshot_reference(
    container_manager: ContainerManager,
    container_name: str,
    graph_yaml: str,
    *,
    phase: str,
) -> str:
    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The FGS graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )


def run_worker_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
) -> ProcessResult:
    LOG.info(
        "starting container exec container=%s worker=%s phase=%s timeout=%ss",
        container_name,
        worker.name,
        phase,
        timeout_seconds,
    )
    process = container_manager.build_exec_process(
        container_name,
        dict(worker.env),
        argv,
        timeout_seconds=timeout_seconds,
    )
    process.start()
    if lease is not None:
        lease.attach_process(process)
    if cancellation is not None:
        cancellation.attach_process(process)
    try:
        return process.communicate(timeout=communicate_timeout(timeout_seconds))
    finally:
        if lease is not None:
            lease.attach_process(None)
        if cancellation is not None:
            cancellation.attach_process(None)


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


def best_effort_release(client: CairnClient, project_id: str, step_id: str, worker_name: str) -> None:
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
