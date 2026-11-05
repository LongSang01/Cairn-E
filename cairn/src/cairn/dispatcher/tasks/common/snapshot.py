"""Shared task helpers: Graph snapshot files, prompt budgeting and rolling summaries."""

from __future__ import annotations

import logging
import uuid

from cairn.contracts.models import ContextSummary
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.context import estimate_tokens, prepare_snapshot
from cairn.dispatcher.prompting import render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend

LOG = logging.getLogger(__name__)


def prompt_overhead_tokens(template: str, blocks: dict[str, str]) -> int:
    """Tokens the prompt spends on everything except the snapshot itself."""
    return estimate_tokens(render_prompt(template, {**blocks, "graph_yaml": ""}))


def load_stored_summary(client: CairnClient, project_id: str, *, enabled: bool) -> ContextSummary:
    """The rolling summaries the last run persisted, with how much history they cover.

    Budget bookkeeping must never break a run, so an unavailable summary degrades to
    "no summary" and the snapshot falls back to its deterministic form.
    """
    if not enabled:
        return ContextSummary()
    try:
        return client.get_context_summary(project_id)
    except Exception:
        # Budgeting must never break a run.
        LOG.warning("context summary unavailable project=%s", project_id, exc_info=True)
        return ContextSummary()


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
    stored = load_stored_summary(client, project_id, enabled=config.runtime.context_budget.enabled)
    view = prepare_snapshot(
        graph_yaml,
        config=config.runtime.context_budget,
        worker=worker,
        overhead_tokens=overhead_tokens,
        phase=phase,
        summary=stored.summary,
        summary_facts=stored.facts_covered,
        step_summary=stored.step_summary,
        summary_steps=stored.steps_covered,
        finding_summary=stored.finding_summary,
        summary_findings=stored.findings_covered,
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
    from . import GRAPH_SNAPSHOT_ROOT

    path = f"{GRAPH_SNAPSHOT_ROOT}/{phase}-{uuid.uuid4().hex[:12]}/graph.yaml"
    container_manager.write_text_file(container_name, path, graph_yaml)
    return (
        "The FGS graph YAML snapshot is stored in this file inside the current container:\n\n"
        f"{path}\n\n"
        "Before using the graph, read the entire file and treat its contents as the YAML snapshot "
        "for this Graph section."
    )
