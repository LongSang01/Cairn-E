from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_decide_payload
from cairn.dispatcher.prompting import (
    format_fact_ids,
    format_open_steps,
    load_prompt,
    render_prompt,
)
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.session import SessionStream, start_session
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release_decide,
    cancel_reason,
    did_timeout,
    preview,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail

LOG = logging.getLogger(__name__)


def run_decide_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Decide: read the whole FGS graph, evolve it, never touch the world."""
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
            health = _healthcheck(driver, worker, healthcheck_timeout, project_id, cancellation, lease)
            if health is not None:
                return health

        open_steps = [
            {
                "id": step.id,
                "from": step.from_,
                "goal": step.goal,
                "description": step.description,
                "rationale": step.rationale,
                "priority": step.priority,
                "status": step.status,
                "worker": step.worker,
            }
            for step in project.steps
            if step.kind == "action" and step.status in ("pending", "running")
        ]
        fact_ids = [fact.id for fact in project.facts]
        goal_ids = [goal.id for goal in project.goals if goal.status == "active"]
        step_ids = [step["id"] for step in open_steps]

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "decide.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="decide",
                ),
                "fact_ids": format_fact_ids(fact_ids),
                "open_steps": format_open_steps(open_steps),
                "max_steps": str(config.tasks.decide.max_steps),
            },
        )

        session = driver.prepare_session()
        command = driver.build_execute(worker, prompt, session)
        stream = start_session(
            client,
            project_id,
            worker=worker.name,
            worker_type=worker.type,
            phase="decide",
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

        def record(status: str, error: str | None = None) -> None:
            stream.finish(
                status=status,
                result=result,
                error=error,
                duration_ms=execute_ms,
                native_session=session,
            )

        cancelled = cancel_reason(result, cancellation)
        if cancelled is not None:
            LOG.info("decide cancelled project=%s worker=%s reason=%s execute_ms=%s", project_id, worker.name, cancelled, execute_ms)
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
            kind, data = validate_decide_payload(
                payload,
                open_steps_empty=not open_steps,
                max_steps=config.tasks.decide.max_steps,
                fact_ids=fact_ids,
                goal_ids=goal_ids,
                step_ids=step_ids,
            )
        except Exception as exc:
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

        if kind == "complete":
            response = client.complete(project_id, data["from"], data["description"], worker.name)
            if response.status_code == 403:
                LOG.info("project became inactive during decide complete project=%s worker=%s", project_id, worker.name)
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
            LOG.warning("decide applied no graph change project=%s worker=%s", project_id, worker.name)
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
            LOG.info("decide added goal project=%s worker=%s goal=%s", project_id, worker_name, goal["description"])
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
            LOG.info("decide closed goal project=%s worker=%s goal=%s status=%s", project_id, worker_name, goal["id"], goal["status"])
        else:
            _log_write_failure("goal close", project_id, worker_name, response)

    for step in data["steps"]["add"]:
        response = client.create_step(
            project_id,
            from_ids=step["from"],
            description=step["description"],
            creator=worker_name,
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

    applied["total"] = applied["goals_added"] + applied["goals_closed"] + applied["steps_added"] + applied["steps_updated"]
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


def _healthcheck(driver, worker: WorkerConfig, timeout: int, project_id: str, cancellation, lease) -> str | None:
    LOG.info("checking worker health project=%s worker=%s timeout=%ss", project_id, worker.name, timeout)
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
