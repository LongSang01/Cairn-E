"""Side questions: one read-only answer from a graph snapshot.

The answer lands in the `questions` table only. Nothing is written into the graph, no
step is claimed, and the run does not take a decide/execute slot — a question can be
answered while the project is mid-run, and cancelling or clearing it never touches the
task state.
"""

from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.backend import ExecutionBackend
from cairn.dispatcher.tasks.common import (
    budgeted_graph_yaml,
    did_timeout,
    preview,
    prompt_overhead_tokens,
    run_worker_process,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail, Question

LOG = logging.getLogger(__name__)
MAX_ANSWER_CHARS = 20_000


def load_question_prompt(group: str) -> str:
    """Custom prompt groups may not ship question.md; the default group always does."""
    try:
        return load_prompt(group, "question.md")
    except (FileNotFoundError, OSError):
        if group == "default":
            raise
        return load_prompt("default", "question.md")


def run_question_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ExecutionBackend,
    project: ProjectDetail,
    question: Question,
    export_yaml: str,
    worker: WorkerConfig,
) -> str:
    driver = get_driver(worker.type, config.runtime.execution)
    project_id = project.project.id
    started = time.perf_counter()

    # The question may have been cancelled while it sat in the queue.
    try:
        current = client.get_question(project_id, question.id)
    except Exception:  # noqa: BLE001 - a vanished question is a cancelled question
        LOG.info("question disappeared before start project=%s question=%s", project_id, question.id)
        return "cancelled"
    if current.status != "running":
        LOG.info(
            "question skipped project=%s question=%s status=%s",
            project_id,
            question.id,
            current.status,
        )
        return "cancelled"

    try:
        template = load_question_prompt(config.runtime.prompt_group)
        blocks = {"question": question.content, "graph_yaml": ""}
        if "{graph_yaml}" in template:
            container_name = container_manager.ensure_running(project_id)
            blocks["graph_yaml"] = write_graph_snapshot_reference(
                container_manager,
                container_name,
                budgeted_graph_yaml(
                    export_yaml.strip(),
                    client=client,
                    project_id=project_id,
                    config=config,
                    worker=worker,
                    phase="question",
                    overhead_tokens=prompt_overhead_tokens(template, blocks),
                ),
                phase="question",
            )
        else:
            container_name = container_manager.container_name(project_id)

        prompt = render_prompt(template, blocks)
        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        result = run_worker_process(
            container_manager,
            container_name,
            worker,
            command.argv,
            phase="question",
            timeout_seconds=config.tasks.question.timeout,
        )
        duration_ms = int((time.perf_counter() - started) * 1000)

        if did_timeout(result):
            LOG.warning(
                "question timed out project=%s question=%s worker=%s timeout=%ss",
                project_id,
                question.id,
                worker.name,
                config.tasks.question.timeout,
            )
            _answer(client, project_id, question.id, worker.name, duration_ms, error="timed out")
            return "failed"
        if result.returncode != 0:
            LOG.warning(
                "question command failed project=%s question=%s worker=%s code=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                question.id,
                worker.name,
                result.returncode,
                preview(result.stdout),
                preview(result.stderr),
            )
            _answer(
                client,
                project_id,
                question.id,
                worker.name,
                duration_ms,
                error=f"worker exited with code {result.returncode}",
            )
            return "failed"

        text = driver.extract_response_text(result.stdout, result.stderr).strip()
        if not text:
            _answer(
                client,
                project_id,
                question.id,
                worker.name,
                duration_ms,
                error="worker returned no text",
            )
            return "failed"
        _answer(
            client,
            project_id,
            question.id,
            worker.name,
            duration_ms,
            answer=text[:MAX_ANSWER_CHARS],
        )
        LOG.info(
            "question answered project=%s question=%s worker=%s duration_ms=%s chars=%s",
            project_id,
            question.id,
            worker.name,
            duration_ms,
            len(text),
        )
        return "success"
    except Exception as exc:  # noqa: BLE001 - never break the dispatcher loop
        LOG.exception("question task crashed project=%s question=%s", project_id, question.id)
        _answer(
            client,
            project_id,
            question.id,
            worker.name,
            int((time.perf_counter() - started) * 1000),
            error=f"question task crashed: {exc}",
        )
        return "failed"


def _answer(
    client: CairnClient,
    project_id: str,
    question_id: str,
    worker_name: str,
    duration_ms: int,
    *,
    answer: str | None = None,
    error: str | None = None,
) -> None:
    try:
        response = client.answer_question(
            project_id,
            question_id,
            status="answered" if answer is not None else "failed",
            answer=answer,
            error=error,
            worker=worker_name,
            duration_ms=duration_ms,
        )
    except Exception:  # noqa: BLE001
        LOG.warning("question answer upload crashed question=%s", question_id, exc_info=True)
        return
    if response.status_code == 409:
        # The human cancelled it while the worker was running: drop the answer.
        LOG.info("question was cancelled before its answer landed question=%s", question_id)
        return
    if not response.ok:
        LOG.warning(
            "question answer upload failed question=%s status=%s body=%s",
            question_id,
            response.status_code,
            response.text,
        )
