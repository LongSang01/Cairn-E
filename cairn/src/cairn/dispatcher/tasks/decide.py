from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

from cairn.contracts.models import ContextSummary, ProjectDetail, RecallPlan
from cairn.dispatcher.config import ContextBudgetConfig, DispatchConfig, WorkerConfig
from cairn.dispatcher.context import GraphView, prepare_snapshot
from cairn.dispatcher.models import StepFailure
from cairn.dispatcher.output_validation import parse_json_output, validate_decide_payload
from cairn.dispatcher.prompting import (
    format_fact_ids,
    format_open_steps,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.session import SessionStream, start_session
from cairn.dispatcher.tasks.common import (
    best_effort_release_decide,
    cancel_reason,
    did_timeout,
    driver_usage,
    load_stored_summary,
    preview,
    prompt_overhead_tokens,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.tasks.summary import SummaryRequest, run_summary_task
from cairn.dispatcher.workers.registry import get_driver

LOG = logging.getLogger(__name__)


def run_decide_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ExecutionBackend,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
    *,
    summary_sink: Callable[[SummaryRequest], bool] | None = None,
    step_failures: dict[str, StepFailure] | None = None,
) -> str:
    """Decide: read the whole FGS graph, evolve it, never touch the world.

    ``summary_sink`` lets the scheduler fold expired history into the stored summaries
    after this run finishes instead of blocking it with a second model call.

    ``step_failures`` is the scheduler's per-step record of consecutive failed Execute
    runs (timeouts included). It is attached to ``open_steps`` so the model can change
    route or abandon a step that keeps failing instead of re-running it blindly.
    """
    driver = get_driver(worker.type, config.runtime.execution)
    project_id = project.project.id
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_decide(client, project_id, worker.name, config.runtime.interval)
    lease.start()
    stream: SessionStream | None = None
    try:
        container_name = container_manager.ensure_running(project_id)

        if task_healthcheck_enabled(config):
            health = _healthcheck(
                driver, worker, healthcheck_timeout, project_id, cancellation, lease
            )
            if health is not None:
                return health

        failures = step_failures or {}
        open_steps: list[dict[str, Any]] = []
        for step in project.steps:
            if step.kind != "action" or step.status not in ("pending", "running"):
                continue
            entry: dict[str, Any] = {
                "id": step.id,
                "from": step.from_,
                "goal": step.goal,
                "title": step.title,
                "description": step.description,
                "rationale": step.rationale,
                "priority": step.priority,
                "status": step.status,
                "worker": step.worker,
                "note": step.note,
            }
            failure = failures.get(step.id)
            if failure is not None and failure.count > 0:
                entry["failures"] = {
                    "count": failure.count,
                    "budget_exhausted": failure.budget_exhausted,
                    "error": failure.error,
                    "noop": failure.noop,
                    "last_error": failure.reason,
                    "last_at": failure.at,
                }
            open_steps.append(entry)
        fact_ids = [fact.id for fact in project.facts]
        goal_ids = [goal.id for goal in project.goals if goal.status == "active"]
        active_sub_goal_ids = [
            goal.id for goal in project.goals if goal.parent is not None and goal.status == "active"
        ]
        step_ids = [str(step["id"]) for step in open_steps]
        # Only a pending step is abandonable from here: a running step is closed by
        # its own executor, and abandoning it would throw that result away.
        pending_step_ids = [str(step["id"]) for step in open_steps if step["status"] == "pending"]

        template = load_prompt(config.runtime.prompt_group, "decide.md")
        recall_block, document_block = _consume_recall_plan(client, project_id)
        blocks = {
            "fact_ids": format_fact_ids(fact_ids),
            "open_steps": format_open_steps(open_steps),
            "max_steps": str(config.tasks.decide.max_steps),
            "recall": recall_block,
            "documents": document_block,
        }
        # The snapshot gets whatever budget the template and its JSON blocks leave.
        overhead_tokens = prompt_overhead_tokens(template, blocks)
        view = _prepare_graph_view(
            client,
            config,
            project_id,
            export_yaml.strip(),
            worker,
            overhead_tokens=overhead_tokens,
            phase="decide",
            container_manager=container_manager,
            summary_sink=summary_sink,
        )
        prompt = render_prompt(
            template,
            {
                **blocks,
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    view.yaml,
                    phase="decide",
                ),
            },
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        summary = (
            f"决策下一步（{len(open_steps)} 个待办步骤）"
            if open_steps
            else "决策下一步（当前没有待办步骤）"
        )
        stream = start_session(
            client,
            project_id,
            worker=worker.name,
            worker_type=worker.type,
            phase="decide",
            summary=summary,
            prompt=prompt,
            native_session=session,
        )
        execute_started = time.perf_counter()
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="decide",
            timeout_seconds=config.tasks.decide.timeout,
            lease=lease,
            cancellation=cancellation,
            on_stdout=stream.feed,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        total_ms = int((time.perf_counter() - task_started) * 1000)
        session = driver.extract_session(session, result.stdout, result.stderr)
        usage = driver_usage(driver, result.stdout, result.stderr)

        def record(status: str, error: str | None = None) -> None:
            stream.finish(
                status=status,
                result=result,
                error=error,
                duration_ms=execute_ms,
                native_session=session,
                usage=usage,
            )

        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info(
                "decide cancelled project=%s worker=%s reason=%s execute_ms=%s",
                project_id,
                worker.name,
                cancelled,
                execute_ms,
            )
            record("cancelled", cancelled)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during decide project=%s worker=%s status=%s execute_ms=%s",
                project_id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            record("failed", f"heartbeat lost: {lease.failure.status_code}")
            return "failed"
        if did_timeout(result):
            LOG.warning(
                "decide timed out project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            record("failed", "timed out")
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "decide command failed project=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                result.returncode,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            record("failed", f"exit code {result.returncode}")
            return "failed"

        try:
            model_output = driver.extract_response_text(result.stdout, result.stderr)
            payload = parse_json_output(model_output)
            kind, data, lookups = validate_decide_payload(
                payload,
                open_steps_empty=not open_steps,
                max_steps=config.tasks.decide.max_steps,
                fact_ids=fact_ids,
                goal_ids=goal_ids,
                active_sub_goal_ids=active_sub_goal_ids,
                step_ids=step_ids,
                pending_step_ids=pending_step_ids,
            )
        except Exception as exc:  # noqa: BLE001 - every parse failure must close the task cleanly
            LOG.warning(
                "decide parse failed project=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                worker.name,
                exc,
                execute_ms,
                total_ms,
                preview(result.stdout),
                preview(result.stderr),
            )
            record("failed", f"parse failed: {exc}")
            return "failed"

        if kind == "rejected":
            LOG.warning(
                "decide rejected project=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
                preview(result.stdout),
            )
            record("rejected")
            return "rejected"

        # A no-op is the legal "wait" answer while steps are open: it carries no graph
        # data, so it has to be handled before anything indexes into `data`. Persist
        # what the run wants looked up next, then close the task cleanly.
        if lookups["recall"] or lookups["doc_ids"]:
            _store_lookups(client, project_id, lookups)

        if kind == "noop":
            LOG.info(
                "decide finished without graph change project=%s worker=%s execute_ms=%s total_ms=%s",
                project_id,
                worker.name,
                execute_ms,
                total_ms,
            )
            record("success")
            return "success"

        if data is None:  # pragma: no cover - validation never returns no data for actions
            LOG.error(
                "decide returned no data project=%s worker=%s kind=%s",
                project_id,
                worker.name,
                kind,
            )
            record("failed", f"no data for kind {kind}")
            return "failed"

        if kind == "complete":
            for goal in data.get("goal_close") or ():
                closed = client.close_goal(
                    project_id,
                    goal["id"],
                    status=goal["status"],
                    worker=worker.name,
                    note=goal["note"],
                )
                if not closed.ok:
                    _log_write_failure("goal close", project_id, worker.name, closed)
            for step in data.get("step_update") or ():
                updated = client.update_step(
                    project_id,
                    step["id"],
                    priority=step["priority"],
                    status=step["status"],
                    note=step["note"],
                )
                if not updated.ok:
                    _log_write_failure("step update", project_id, worker.name, updated)
            response = client.complete(project_id, data["from"], data["description"], worker.name)
            if response.status_code == 403:
                LOG.info(
                    "project became inactive during decide complete project=%s worker=%s",
                    project_id,
                    worker.name,
                )
                record("success")
                return "success"
            if not response.ok:
                LOG.warning(
                    "decide complete write failed project=%s worker=%s status=%s body=%s",
                    project_id,
                    worker.name,
                    response.status_code,
                    response.text,
                )
                record("failed", f"complete write failed: {response.status_code}")
                return "failed"
            LOG.info(
                "project completed project=%s worker=%s from=%s execute_ms=%s total_ms=%s",
                project_id,
                worker.name,
                data["from"],
                execute_ms,
                total_ms,
            )
            record("success")
            return "success"

        applied = _apply_actions(client, project_id, worker.name, data)
        LOG.info(
            "decide finished project=%s worker=%s applied=%s goals_added=%s goals_closed=%s steps_added=%s steps_updated=%s "
            "execute_ms=%s total_ms=%s",
            project_id,
            worker.name,
            applied["total"],
            applied["goals_added"],
            applied["goals_closed"],
            applied["steps_added"],
            applied["steps_updated"],
            execute_ms,
            total_ms,
        )
        if applied["total"] == 0:
            LOG.warning(
                "decide applied no graph change project=%s worker=%s", project_id, worker.name
            )
            record("failed", "applied no graph change")
            return "failed"
        record("success")
        return "success"
    except Exception:
        LOG.exception("decide task crashed project=%s worker=%s", project_id, worker.name)
        if stream is not None:
            stream.finish(status="failed", error="task crashed")
        return "failed"
    finally:
        lease.stop()
        best_effort_release_decide(client, project_id, worker.name)


def _apply_actions(
    client: CairnClient, project_id: str, worker_name: str, data: dict
) -> dict[str, int]:
    applied = {
        "goals_added": 0,
        "goals_closed": 0,
        "steps_added": 0,
        "steps_updated": 0,
        "total": 0,
    }

    for goal in data["goals"]["add"]:
        response = client.create_goal(
            project_id,
            description=goal["description"],
            creator=worker_name,
            parent=goal["parent"],
        )
        if response.ok:
            applied["goals_added"] += 1
            LOG.info(
                "decide added goal project=%s worker=%s goal=%s",
                project_id,
                worker_name,
                goal["description"],
            )
        else:
            _log_write_failure("goal add", project_id, worker_name, response)

    for goal in data["goals"]["close"]:
        response = client.close_goal(
            project_id,
            goal["id"],
            status=goal["status"],
            worker=worker_name,
            note=goal["note"],
        )
        if response.ok:
            applied["goals_closed"] += 1
            LOG.info(
                "decide closed goal project=%s worker=%s goal=%s status=%s",
                project_id,
                worker_name,
                goal["id"],
                goal["status"],
            )
        else:
            _log_write_failure("goal close", project_id, worker_name, response)

    for step in data["steps"]["add"]:
        response = client.create_step(
            project_id,
            from_ids=step["from"],
            description=step["description"],
            creator=worker_name,
            title=step.get("title"),
            goal=step["goal"],
            rationale=step["rationale"],
            priority=step["priority"],
        )
        if response.ok:
            applied["steps_added"] += 1
            LOG.info(
                "decide added step project=%s worker=%s from=%s priority=%s description=%s",
                project_id,
                worker_name,
                step["from"],
                step["priority"],
                step["description"],
            )
        else:
            _log_write_failure("step add", project_id, worker_name, response)

    for step in data["steps"]["update"]:
        response = client.update_step(
            project_id,
            step["id"],
            priority=step["priority"],
            status=step["status"],
            note=step["note"],
        )
        if response.ok:
            applied["steps_updated"] += 1
            LOG.info(
                "decide updated step project=%s worker=%s step=%s priority=%s status=%s",
                project_id,
                worker_name,
                step["id"],
                step["priority"],
                step["status"],
            )
        else:
            _log_write_failure("step update", project_id, worker_name, response)

    applied["total"] = (
        applied["goals_added"]
        + applied["goals_closed"]
        + applied["steps_added"]
        + applied["steps_updated"]
    )
    return applied


def _log_write_failure(action: str, project_id: str, worker_name: str, response) -> None:
    level = logging.INFO if response.status_code in (403, 404, 409) else logging.WARNING
    LOG.log(
        level,
        "decide %s failed project=%s worker=%s status=%s body=%s",
        action,
        project_id,
        worker_name,
        response.status_code,
        response.text,
    )


def _prepare_graph_view(
    client: CairnClient,
    config: DispatchConfig,
    project_id: str,
    graph_yaml: str,
    worker: WorkerConfig,
    *,
    overhead_tokens: int,
    phase: str,
    container_manager: ExecutionBackend,
    summary_sink: Callable[[SummaryRequest], bool] | None = None,
) -> GraphView:
    """Fit the graph into the worker's context budget.

    Summarization is never awaited on the Decide path: the pending history is handed to
    ``summary_sink`` (the scheduler's background pool) and this run keeps using the
    previous summary, so a Decide stays one model call. Without a sink the call runs
    inline, which only happens when the task is invoked directly.
    """
    budget_config = config.runtime.context_budget
    stored = load_stored_summary(client, project_id, enabled=budget_config.enabled)
    view = _snapshot(graph_yaml, budget_config, worker, overhead_tokens, phase, stored)
    LOG.info(
        "graph snapshot project=%s phase=%s worker=%s budget_tokens=%s snapshot_tokens=%s compacted=%s "
        "truncated=%s older=%s/%s/%s covered=%s/%s/%s",
        project_id,
        phase,
        worker.name,
        view.budget_tokens,
        view.tokens,
        view.compacted,
        view.truncated,
        len(view.old_facts),
        len(view.old_steps),
        len(view.old_findings),
        stored.facts_covered,
        stored.steps_covered,
        stored.findings_covered,
    )
    request = _summary_request(view, stored, project_id=project_id, worker=worker)
    if request is None or budget_config.summary_max_calls <= 0:
        return view

    if summary_sink is not None:
        if summary_sink(request):
            LOG.info(
                "graph history queued for summarization project=%s worker=%s facts=%s steps=%s findings=%s",
                project_id,
                worker.name,
                len(request.facts),
                len(request.steps),
                len(request.findings),
            )
        else:
            LOG.info("graph summary already running project=%s", project_id)
        return view

    # No scheduler attached (direct invocation): keep the inline behaviour.
    if run_summary_task(config, client, container_manager, worker, request) != "summarized":
        return view
    fresh = load_stored_summary(client, project_id, enabled=budget_config.enabled)
    return _snapshot(graph_yaml, budget_config, worker, overhead_tokens, phase, fresh)


def _snapshot(
    graph_yaml: str,
    budget_config: ContextBudgetConfig,
    worker: WorkerConfig,
    overhead_tokens: int,
    phase: str,
    stored: ContextSummary,
) -> GraphView:
    return prepare_snapshot(
        graph_yaml,
        config=budget_config,
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


def _summary_request(
    view: GraphView, stored: ContextSummary, *, project_id: str, worker: WorkerConfig
) -> SummaryRequest | None:
    """The history this snapshot could not carry, or ``None`` when nothing is pending."""
    request = SummaryRequest(
        project_id=project_id,
        worker=worker.name,
        facts=view.old_facts[stored.facts_covered :],
        facts_covered=len(view.old_facts),
        previous_facts=stored.summary,
        steps=view.old_steps[stored.steps_covered :],
        steps_covered=len(view.old_steps),
        previous_steps=stored.step_summary,
        findings=view.old_findings[stored.findings_covered :],
        findings_covered=len(view.old_findings),
        previous_findings=stored.finding_summary,
    )
    return request or None


def _healthcheck(
    driver, worker: WorkerConfig, timeout: int, project_id: str, cancellation, lease
) -> str | None:
    LOG.info(
        "checking worker health project=%s worker=%s timeout=%ss", project_id, worker.name, timeout
    )
    health = driver.check_health(worker, timeout=timeout)
    if cancellation.is_cancelled:
        LOG.info(
            "decide cancelled during healthcheck project=%s worker=%s reason=%s",
            project_id,
            worker.name,
            cancellation.reason,
        )
        return "cancelled"
    if lease.failure is not None:
        LOG.warning(
            "heartbeat lost during decide healthcheck project=%s worker=%s status=%s",
            project_id,
            worker.name,
            lease.failure.status_code,
        )
        return "failed"
    if not health.ok:
        LOG.warning(
            "worker unhealthy project=%s worker=%s status=%s detail=%s",
            project_id,
            worker.name,
            health.status,
            health.detail,
        )
        return "unhealthy"
    return None


RECALL_BLOCK_CHARS = 4000
DOCUMENT_BLOCK_CHARS = 4000
# A truncated document keeps both ends: the conclusion usually sits at the bottom.
DOCUMENT_HEAD_RATIO = 0.6


def _consume_recall_plan(client: CairnClient, project_id: str) -> tuple[str, str]:
    """Render the lookups the previous Decide asked for, then clear them.

    Each request is injected exactly once: leaving it in place would re-spend prompt
    tokens on results the model has already seen.
    """
    try:
        plan = client.get_recall_plan(project_id)
    except Exception:
        LOG.warning("recall plan unavailable project=%s", project_id, exc_info=True)
        return "(none)", "(none)"
    if not plan.queries and not plan.doc_ids:
        return "(none)", "(none)"

    recall_text = _render_recall(client, project_id, plan)
    document_text = _render_documents(client, project_id, plan)
    try:
        client.save_recall_plan(project_id, queries=[], doc_ids=[])
    except Exception:
        LOG.warning("recall plan reset failed project=%s", project_id, exc_info=True)
    return recall_text, document_text


def _render_recall(client: CairnClient, project_id: str, plan: RecallPlan) -> str:
    lines: list[str] = []
    used = 0
    for query in plan.queries:
        try:
            result = client.recall(project_id, query, limit=5)
        except Exception:
            LOG.warning("recall failed project=%s query=%s", project_id, query, exc_info=True)
            continue
        lines.append(f"### {query}")
        for hit in list(result.facts) + list(result.steps):
            line = f"- {hit.kind} {hit.id} [{hit.score}]: {hit.title or hit.description[:80]}"
            if hit.status:
                line += f" ({hit.status})"
            if hit.doc_ids:
                line += f" docs={','.join(hit.doc_ids)}"
            if used + len(line) + 1 > RECALL_BLOCK_CHARS:
                lines.append("… (more matches skipped)")
                LOG.info(
                    "recall injected project=%s queries=%s hits=%s truncated=true",
                    project_id,
                    plan.queries,
                    len(lines),
                )
                return "\n".join(lines)
            lines.append(line)
            used += len(line) + 1
    LOG.info(
        "recall injected project=%s queries=%s hits=%s truncated=false",
        project_id,
        plan.queries,
        len(lines),
    )
    return "\n".join(lines) or "(none)"


def _slice_document(content: str, offset: int, room: int, document_id: str) -> str:
    """Fit one document into `room` characters, keeping both ends when it overflows.

    The head alone is not enough — conclusions usually land at the end — so the body is
    split into a leading and a trailing slice. The marker names the exact offset to
    resume from, which makes truncation "not read yet" instead of "lost forever".
    """
    start = max(0, min(offset, len(content)))
    remaining = content[start:]
    if len(remaining) <= room:
        return remaining

    head_len = max(1, int(room * DOCUMENT_HEAD_RATIO))
    tail_len = max(0, room - head_len)
    marker = ""
    for _ in range(3):
        skipped = max(0, len(remaining) - head_len - tail_len)
        resume_from = start + head_len
        marker = (
            f"… ({skipped} characters omitted; next round request "
            f'{{"id": "{document_id}", "from": {resume_from}}}) …'
        )
        excess = head_len + tail_len + len(marker) + 2 - room
        if excess <= 0:
            break
        head_len = max(1, head_len - excess)
        tail_len = max(0, room - head_len - len(marker) - 2)

    suffix = "\n… (truncated; read on with a larger offset)"
    if head_len < room * 0.25 or room <= len(marker) + 24:
        # Too little budget for both ends (or barely over the limit): one plain cut
        # wastes far less text than a one-character head plus a long marker.
        if room <= len(suffix):
            return remaining[:room]
        return remaining[: room - len(suffix)] + suffix
    head = remaining[:head_len]
    if tail_len <= 0:
        return f"{head}\n{marker}"
    return f"{head}\n{marker}\n{remaining[len(remaining) - tail_len :]}"


def _render_documents(client: CairnClient, project_id: str, plan: RecallPlan) -> str:
    """Fetch the requested documents, fitting each into the shared injection budget."""
    blocks: list[str] = []
    used = 0
    for request in plan.doc_ids:
        try:
            document = client.get_document(project_id, request.id)
        except Exception:
            LOG.warning(
                "document fetch failed project=%s document=%s",
                project_id,
                request.id,
                exc_info=True,
            )
            continue
        header = f"### {document.id} {document.title}\n"
        room = DOCUMENT_BLOCK_CHARS - used - len(header)
        if room <= 40:
            blocks.append("… (more documents skipped; ask for them again next round)")
            break
        body = _slice_document(document.content, request.offset, room, document.id)
        block = header + body
        blocks.append(block)
        used += len(block) + 2  # the blank line between blocks
    LOG.info(
        "documents injected project=%s reads=%s",
        project_id,
        [(request.id, request.offset) for request in plan.doc_ids],
    )
    return "\n\n".join(blocks) or "(none)"


def _store_lookups(client: CairnClient, project_id: str, lookups: dict) -> None:
    try:
        client.save_recall_plan(project_id, queries=lookups["recall"], doc_ids=lookups["doc_ids"])
    except Exception:
        LOG.warning("recall plan write failed project=%s", project_id, exc_info=True)
