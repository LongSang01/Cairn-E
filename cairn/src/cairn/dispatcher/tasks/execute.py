from __future__ import annotations

import logging
import time

from cairn.dispatcher.config import DispatchConfig, WorkerConfig
from cairn.dispatcher.contracts import parse_json_output, validate_execute_payload
from cairn.dispatcher.prompting import load_prompt, render_prompt
from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.session import record_session
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.runtime.containers import ContainerManager
from cairn.dispatcher.runtime.heartbeat import HeartbeatLease
from cairn.dispatcher.tasks.common import (
    best_effort_release,
    cancel_reason,
    did_timeout,
    preview,
    project_allows_conclude_fallback,
    run_worker_process,
    task_healthcheck_enabled,
    write_graph_snapshot_reference,
    write_step_result,
)
from cairn.dispatcher.workers.registry import get_driver
from cairn.server.models import ProjectDetail, Step

LOG = logging.getLogger(__name__)


def run_execute_task(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    project: ProjectDetail,
    export_yaml: str,
    step: Step,
    worker: WorkerConfig,
    cancellation: TaskCancellation,
) -> str:
    """Execute: one claimed step, world tools, and submit_fact / submit_finding."""
    driver = get_driver(worker.type, config.runtime.execution)
    project_id = project.project.id
    task_started = time.perf_counter()
    healthcheck_timeout = config.runtime.healthcheck_timeout
    lease = HeartbeatLease.for_step(client, project_id, step.id, worker.name, config.runtime.interval)
    lease.start()
    try:
        container_name = container_manager.ensure_running(project_id)

        if task_healthcheck_enabled(config):
            LOG.info(
                "checking worker health project=%s step=%s worker=%s timeout=%ss",
                project_id,
                step.id,
                worker.name,
                healthcheck_timeout,
            )
            health = driver.check_health(worker, timeout=healthcheck_timeout)
            if cancellation.is_cancelled:
                LOG.info(
                    "execute cancelled during healthcheck project=%s step=%s worker=%s reason=%s",
                    project_id,
                    step.id,
                    worker.name,
                    cancellation.reason,
                )
                best_effort_release(client, project_id, step.id, worker.name)
                return "cancelled"
            if lease.failure is not None:
                LOG.warning(
                    "heartbeat lost during execute healthcheck project=%s step=%s worker=%s status=%s",
                    project_id,
                    step.id,
                    worker.name,
                    lease.failure.status_code,
                )
                best_effort_release(client, project_id, step.id, worker.name)
                return "failed"
            if not health.ok:
                LOG.warning(
                    "worker unhealthy project=%s step=%s worker=%s status=%s detail=%s",
                    project_id,
                    step.id,
                    worker.name,
                    health.status,
                    health.detail,
                )
                best_effort_release(client, project_id, step.id, worker.name)
                return "unhealthy"

        prompt = render_prompt(
            load_prompt(config.runtime.prompt_group, "execute.md"),
            {
                "graph_yaml": write_graph_snapshot_reference(
                    container_manager,
                    container_name,
                    export_yaml.strip(),
                    phase="execute",
                ),
                "step_id": step.id,
                "step_description": step.description,
            },
        )

        session = driver.prepare_session()
        execute = driver.build_execute(worker, prompt, session)
        session = execute.session
        execute_started = time.perf_counter()
        first = _run_process(
            container_manager,
            container_name,
            worker,
            execute.argv,
            phase="execute",
            timeout=config.tasks.execute.timeout,
            lease=lease,
            cancellation=cancellation,
        )
        execute_ms = int((time.perf_counter() - execute_started) * 1000)
        session = driver.extract_session(session, first.stdout, first.stderr)

        def record(status: str, error: str | None = None) -> None:
            record_session(
                client,
                project_id,
                worker=worker.name,
                worker_type=worker.type,
                phase="execute",
                status=status,
                result=first,
                step_id=step.id,
                native_session=session,
                prompt=prompt,
                duration_ms=execute_ms,
                error=error,
            )

        cancelled = cancel_reason(first, cancellation)
        if cancelled is not None:
            LOG.info(
                "execute cancelled project=%s step=%s worker=%s reason=%s execute_ms=%s",
                project_id,
                step.id,
                worker.name,
                cancelled,
                execute_ms,
            )
            record("cancelled", cancelled)
            best_effort_release(client, project_id, step.id, worker.name)
            return "cancelled"
        if lease.failure is not None:
            LOG.warning(
                "heartbeat lost during execute project=%s step=%s worker=%s status=%s execute_ms=%s",
                project_id,
                step.id,
                worker.name,
                lease.failure.status_code,
                execute_ms,
            )
            record("failed", f"heartbeat lost: {lease.failure.status_code}")
            best_effort_release(client, project_id, step.id, worker.name)
            return "failed"

        if not did_timeout(first) and first.returncode == 0:
            try:
                model_output = driver.extract_response_text(first.stdout, first.stderr)
                payload = parse_json_output(model_output)
                kind, data = validate_execute_payload(payload)
            except Exception as exc:
                LOG.warning(
                    "execute parse failed project=%s step=%s worker=%s error=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                    project_id,
                    step.id,
                    worker.name,
                    exc,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                    preview(first.stderr),
                )
                record("failed", f"parse failed: {exc}")
                return _try_conclude_fallback(
                    config,
                    client,
                    container_manager,
                    container_name,
                    worker,
                    driver,
                    project_id,
                    step,
                    export_yaml,
                    session,
                    lease,
                    cancellation,
                )
            if kind == "rejected":
                LOG.warning(
                    "execute rejected project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s",
                    project_id,
                    step.id,
                    worker.name,
                    execute_ms,
                    int((time.perf_counter() - task_started) * 1000),
                    preview(first.stdout),
                )
                record("rejected")
                best_effort_release(client, project_id, step.id, worker.name)
                return "rejected"
            status = write_step_result(
                client,
                project_id,
                step.id,
                worker.name,
                data,
                source="execute",
                phase_ms=execute_ms,
                total_ms=int((time.perf_counter() - task_started) * 1000),
            ).status
            record("success")
            return status

        if did_timeout(first):
            LOG.warning(
                "execute timed out project=%s step=%s worker=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
                project_id,
                step.id,
                worker.name,
                execute_ms,
                int((time.perf_counter() - task_started) * 1000),
                preview(first.stdout),
                preview(first.stderr),
            )
            record("failed", "timed out")
            return _try_conclude_fallback(
                config,
                client,
                container_manager,
                container_name,
                worker,
                driver,
                project_id,
                step,
                export_yaml,
                session,
                lease,
                cancellation,
            )
        LOG.warning(
            "execute command failed project=%s step=%s worker=%s code=%s execute_ms=%s total_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            step.id,
            worker.name,
            first.returncode,
            execute_ms,
            int((time.perf_counter() - task_started) * 1000),
            preview(first.stdout),
            preview(first.stderr),
        )
        record("failed", f"exit code {first.returncode}")
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    except Exception:
        LOG.exception("execute task crashed project=%s step=%s worker=%s", project_id, step.id, worker.name)
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    finally:
        lease.stop()


def _try_conclude_fallback(
    config: DispatchConfig,
    client: CairnClient,
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    driver,
    project_id: str,
    step: Step,
    export_yaml: str,
    session: str | None,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
) -> str:
    if not driver.supports_conclude() or not session:
        LOG.info(
            "conclude fallback unavailable project=%s step=%s worker=%s supports_conclude=%s has_session=%s",
            project_id,
            step.id,
            worker.name,
            driver.supports_conclude(),
            bool(session),
        )
        _record_conclude(
            client,
            project_id,
            worker,
            step.id,
            prompt=None,
            result=None,
            status="failed",
            native_session=session,
            error="conclude fallback unavailable",
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if lease.failure is not None:
        LOG.warning("conclude fallback skipped because heartbeat already lost project=%s step=%s worker=%s", project_id, step.id, worker.name)
        _record_conclude(
            client, project_id, worker, step.id, prompt=None, result=None,
            status="failed", native_session=session, error="heartbeat lost before conclude",
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if cancellation.is_cancelled:
        LOG.info(
            "conclude fallback skipped because task was cancelled project=%s step=%s worker=%s reason=%s",
            project_id,
            step.id,
            worker.name,
            cancellation.reason,
        )
        _record_conclude(
            client, project_id, worker, step.id, prompt=None, result=None,
            status="cancelled", native_session=session, error=cancellation.reason,
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "cancelled"

    if not project_allows_conclude_fallback(client, project_id, worker_name=worker.name, step_id=step.id):
        _record_conclude(
            client, project_id, worker, step.id, prompt=None, result=None,
            status="failed", native_session=session, error="project is not active",
        )
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"

    container_name = container_manager.ensure_running(project_id)

    prompt = render_prompt(
        load_prompt(config.runtime.prompt_group, "execute_conclude.md"),
        {
            "graph_yaml": write_graph_snapshot_reference(
                container_manager,
                container_name,
                export_yaml.strip(),
                phase="execute_conclude",
            ),
            "step_id": step.id,
            "step_description": step.description,
        },
    )
    conclude_argv = driver.build_conclude(worker, prompt, session)
    LOG.info("starting conclude fallback project=%s step=%s worker=%s", project_id, step.id, worker.name)
    conclude_started = time.perf_counter()
    result = _run_process(
        container_manager,
        container_name,
        worker,
        conclude_argv,
        phase="execute_conclude",
        timeout=config.tasks.execute.conclude_timeout,
        lease=lease,
        cancellation=cancellation,
    )
    conclude_ms = int((time.perf_counter() - conclude_started) * 1000)
    cancelled = cancel_reason(result, cancellation)

    def record(status: str, error: str | None = None) -> None:
        _record_conclude(
            client,
            project_id,
            worker,
            step.id,
            prompt=prompt,
            result=result,
            status=status,
            native_session=session,
            error=error,
            duration_ms=conclude_ms,
        )
    if cancelled is not None:
        LOG.info(
            "conclude cancelled project=%s step=%s worker=%s reason=%s conclude_ms=%s",
            project_id,
            step.id,
            worker.name,
            cancelled,
            conclude_ms,
        )
        record("cancelled", cancelled)
        best_effort_release(client, project_id, step.id, worker.name)
        return "cancelled"
    if lease.failure is not None:
        record("failed", f"heartbeat lost: {lease.failure.status_code}")
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if result.timed_out or result.returncode != 0:
        LOG.warning(
            "conclude failed project=%s step=%s worker=%s code=%s timed_out=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            step.id,
            worker.name,
            result.returncode,
            result.timed_out,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        record("failed", "timed out" if result.timed_out else f"exit code {result.returncode}")
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    try:
        model_output = driver.extract_response_text(result.stdout, result.stderr)
        payload = parse_json_output(model_output)
        kind, data = validate_execute_payload(payload)
    except Exception as exc:
        LOG.warning(
            "conclude parse failed project=%s step=%s worker=%s error=%s conclude_ms=%s stdout_preview=%s stderr_preview=%s",
            project_id,
            step.id,
            worker.name,
            exc,
            conclude_ms,
            preview(result.stdout),
            preview(result.stderr),
        )
        record("failed", f"parse failed: {exc}")
        best_effort_release(client, project_id, step.id, worker.name)
        return "failed"
    if kind == "rejected":
        LOG.warning(
            "conclude rejected project=%s step=%s worker=%s conclude_ms=%s stdout_preview=%s",
            project_id,
            step.id,
            worker.name,
            conclude_ms,
            preview(result.stdout),
        )
        record("rejected")
        best_effort_release(client, project_id, step.id, worker.name)
        return "rejected"
    status = write_step_result(
        client,
        project_id,
        step.id,
        worker.name,
        data,
        source="execute_conclude",
        phase_ms=conclude_ms,
    ).status
    record("success")
    return status


def _record_conclude(
    client: CairnClient,
    project_id: str,
    worker: WorkerConfig,
    step_id: str,
    *,
    prompt: str | None,
    result,
    status: str,
    native_session: str | None,
    error: str | None = None,
    duration_ms: int | None = None,
) -> None:
    record_session(
        client,
        project_id,
        worker=worker.name,
        worker_type=worker.type,
        phase="execute_conclude",
        status=status,
        result=result,
        step_id=step_id,
        native_session=native_session,
        prompt=prompt,
        duration_ms=duration_ms,
        error=error,
    )


def _run_process(
    container_manager: ContainerManager,
    container_name: str,
    worker: WorkerConfig,
    argv: list[str],
    *,
    phase: str,
    timeout: int,
    lease: HeartbeatLease,
    cancellation: TaskCancellation,
):
    return run_worker_process(
        container_manager,
        container_name,
        worker,
        argv,
        phase=phase,
        timeout_seconds=timeout,
        lease=lease,
        cancellation=cancellation,
    )
