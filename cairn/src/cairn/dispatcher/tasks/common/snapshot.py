"""Shared task helpers: Graph snapshot files, prompt budgeting and rolling summaries."""

from __future__ import annotations

import logging
import uuid
from typing import Any

import yaml

from cairn.contracts.models import ContextSummary, ProjectDetail, Step
from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.context import estimate_tokens, prepare_snapshot
from cairn.dispatcher.prompting import render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend

LOG = logging.getLogger(__name__)

# A conclusion only needs enough of a source fact to recognise it, not its full text.
STEP_SCOPE_FACT_CHARS = 400


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


def step_scope_yaml(project: ProjectDetail, step: Step) -> str:
    """The conclusion's context: this step plus the facts it started from.

    The settlement round runs in the same worker session, so the model already holds
    everything it observed. Pasting the whole graph on top of that costs tokens without
    adding evidence: a graph with 27 facts pushed conclude into minutes and still wrote
    nothing back, while the transcript is what actually proves the result.
    """
    facts = {fact.id: fact for fact in project.facts}
    data: dict[str, Any] = {"step": {"id": step.id, "description": step.description}}
    if step.rationale:
        data["step"]["rationale"] = step.rationale
    sources = [facts[fid] for fid in step.from_ if fid in facts]
    if sources:
        data["step"]["from"] = [
            {"id": fact.id, "statement": _clip(fact.title or fact.description)} for fact in sources
        ]
    root_goal = next((goal for goal in project.goals if goal.parent is None), None)
    if root_goal is not None:
        data["goal"] = _clip(root_goal.description)
    return yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False)


def _clip(text: str) -> str:
    if len(text) <= STEP_SCOPE_FACT_CHARS:
        return text
    return text[: STEP_SCOPE_FACT_CHARS - 1] + "…"


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
