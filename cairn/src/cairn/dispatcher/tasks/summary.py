"""Background folding of graph history into the project's rolling summaries.

A summarization is one extra model call, so Decide never waits for it: the run records
what is about to leave the recent window, the scheduler runs the call on its own small
pool, and the next Decide / Execute / question run picks the merged result up from the
server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.context import split_summary_sections, summarize_prompt
from cairn.dispatcher.prompting import load_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.tasks.common import run_worker_process
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger(__name__)
MAX_SUMMARY_CHARS = 4000


@dataclass(slots=True)
class SummaryRequest:
    """History that should be folded into the persisted summaries.

    ``*_covered`` is the number of items the channel will cover once this request lands;
    the server keeps the higher value so a slow request can never roll a channel back.
    """

    project_id: str
    worker: str
    facts: list[dict[str, Any]] = field(default_factory=list)
    facts_covered: int = 0
    previous_facts: str = ""
    steps: list[dict[str, Any]] = field(default_factory=list)
    steps_covered: int = 0
    previous_steps: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    findings_covered: int = 0
    previous_findings: str = ""

    def __bool__(self) -> bool:
        return bool(self.facts or self.steps or self.findings)


def build_summary_prompt(group: str, request: SummaryRequest) -> str:
    """Render the summary prompt; a placeholder-free template (mock group) is used as-is."""
    try:
        template = load_prompt(group, "summary.md")
    except (FileNotFoundError, OSError):
        template = ""
    if template and "{facts}" not in template:
        return template
    return summarize_prompt(
        _previous_block(request),
        request.facts,
        steps=request.steps,
        findings=request.findings,
    )


def run_summary_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ExecutionBackend,
    worker: WorkerConfig,
    request: SummaryRequest,
) -> str:
    """Fold the pending history into the stored summaries; returns an outcome label."""
    if not request:
        return "empty"
    driver = get_driver(worker.type)
    timeout = config.runtime.context_budget.summary_timeout
    prompt = build_summary_prompt(config.runtime.prompt_group, request)
    LOG.info(
        "summarizing graph history project=%s worker=%s facts=%s steps=%s findings=%s timeout=%ss",
        request.project_id,
        worker.name,
        len(request.facts),
        len(request.steps),
        len(request.findings),
        timeout,
    )
    try:
        container_name = container_manager.ensure_running(request.project_id)
        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="summary",
            timeout_seconds=timeout,
        )
    except Exception:  # a failed summary must never break the loop
        LOG.warning("graph summarization failed project=%s", request.project_id, exc_info=True)
        return "failed"
    if result.cancelled or result.timed_out or result.returncode != 0:
        LOG.warning(
            "graph summarization gave up project=%s worker=%s timed_out=%s code=%s",
            request.project_id,
            worker.name,
            result.timed_out,
            result.returncode,
        )
        return "failed"

    text = driver.extract_response_text(result.stdout, result.stderr).strip()[:MAX_SUMMARY_CHARS]
    if not text:
        return "empty"
    sections = split_summary_sections(text)
    try:
        client.save_context_summary(
            request.project_id,
            summary=sections["facts"],
            facts_covered=request.facts_covered if sections["facts"] else 0,
            step_summary=sections["steps"],
            steps_covered=request.steps_covered if sections["steps"] else 0,
            finding_summary=sections["findings"],
            findings_covered=request.findings_covered if sections["findings"] else 0,
        )
    except Exception:  # the next run will simply ask again
        LOG.warning("context summary write failed project=%s", request.project_id, exc_info=True)
        return "failed"
    LOG.info(
        "graph summary stored project=%s facts=%s steps=%s findings=%s",
        request.project_id,
        request.facts_covered if sections["facts"] else 0,
        request.steps_covered if sections["steps"] else 0,
        request.findings_covered if sections["findings"] else 0,
    )
    return "summarized"


def _previous_block(request: SummaryRequest) -> str:
    parts = []
    if request.previous_facts:
        parts.append(f"Facts:\n{request.previous_facts}")
    if request.previous_steps:
        parts.append(f"Steps:\n{request.previous_steps}")
    if request.previous_findings:
        parts.append(f"Findings:\n{request.previous_findings}")
    return "\n\n".join(parts) or "(none)"
