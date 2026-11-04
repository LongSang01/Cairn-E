from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.context import estimate_tokens, prepare_snapshot
from cairn.dispatcher.prompting import render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.runtime.process import OutputCallback, ProcessResult

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


def prompt_overhead_tokens(template: str, blocks: dict[str, str]) -> int:
    """Tokens the prompt spends on everything except the snapshot itself."""
    return estimate_tokens(render_prompt(template, {**blocks, "graph_yaml": ""}))


def load_stored_summary(client: CairnClient, project_id: str, *, enabled: bool) -> tuple[str, int]:
    """The rolling summary the last Decide run persisted, plus how many facts it covers.

    Budget bookkeeping must never break a run, so an unavailable summary degrades to
    "no summary" and the snapshot falls back to its deterministic form.
    """
    if not enabled:
        return "", 0
    try:
        stored = client.get_context_summary(project_id)
    except Exception:  # noqa: BLE001 - budgeting must never break a run
        LOG.warning("context summary unavailable project=%s", project_id, exc_info=True)
        return "", 0
    return stored.summary, stored.facts_covered


def budgeted_graph_yaml(
    graph_yaml: str,
    *,
    client: CairnClient,
    project_id: str,
    config: DispatchConfig,
    worker: WorkerConfig,
    phase: str,
    overhead_tokens: int,
) -> str:
    """Compact the snapshot down to the worker's remaining context budget."""
    summary, covered = load_stored_summary(
        client, project_id, enabled=config.runtime.context_budget.enabled
    )
    view = prepare_snapshot(
        graph_yaml,
        config=config.runtime.context_budget,
        worker=worker,
        overhead_tokens=overhead_tokens,
        phase=phase,
        summary=summary,
        summary_facts=covered,
    )
    if view.compacted:
        LOG.info(
            "graph snapshot compacted phase=%s worker=%s budget_tokens=%s snapshot_tokens=%s truncated=%s",
            phase,
            worker.name,
            view.budget_tokens,
            view.tokens,
            view.truncated,
        )
    return view.yaml


def write_graph_snapshot_reference(
    container_manager: ExecutionBackend,
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
    container_manager: ExecutionBackend,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout_seconds: int,
    lease: HeartbeatLease | None = None,
    cancellation: TaskCancellation | None = None,
    on_stdout: OutputCallback | None = None,
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
    if on_stdout is not None:
        # Set before start() so no chunk is missed.
        process.on_stdout = on_stdout
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
