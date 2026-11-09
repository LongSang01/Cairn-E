"""Tick ordering, error containment and startup checks of ``DispatcherLoop``."""

from __future__ import annotations

import shutil
import subprocess
import time
from types import SimpleNamespace

import pytest
import requests
from conftest import bare_loop, make_config

from cairn.contracts.models import Settings
from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.runtime.startup_healthcheck import StartupHealthcheckResult
from cairn.dispatcher.scheduler.loop import DispatcherLoop

TICK_STAGES = [
    "_reap_futures",
    "_reap_cleanup_futures",
    "_initialize_decide_checkpoints",
    "_refresh_runtime_projects",
    "_cancel_inactive_tasks",
    "_queue_container_cleanups",
    "_reap_summary_futures",
    "_dispatch_questions",
    "_dispatch_available",
]


class _FakePool:
    def __init__(self, name: str, calls: list[str]) -> None:
        self.name = name
        self.calls = calls

    def shutdown(self, wait: bool = True) -> None:
        self.calls.append(f"shutdown:{self.name}:{wait}")


class _FakeContainers:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def container_name(self, project_id: str) -> str:
        return f"container-{project_id}"

    def close(self) -> None:
        self.calls.append("close:containers")


class _FakeClient:
    def __init__(self, calls: list[str], settings: Settings) -> None:
        self.calls = calls
        self.settings = settings

    def list_projects(self) -> list:
        self.calls.append("list_projects")
        return []

    def get_settings(self) -> Settings:
        return self.settings

    def close(self) -> None:
        self.calls.append("close:client")


def _stub_loop(
    *, settings: Settings | None = None, runtime: dict | None = None
) -> tuple[DispatcherLoop, list[str]]:
    """A loop whose collaborators are recorders, so ``run`` can be inspected offline."""
    calls: list[str] = []
    loop = bare_loop()
    loop.config = make_config(runtime)
    loop._settings_checked = False
    loop._startup_healthchecks_checked = False
    loop.executor = _FakePool("executor", calls)
    loop.cleanup_executor = _FakePool("cleanup_executor", calls)
    loop.question_executor = _FakePool("question_executor", calls)
    loop.summary_executor = _FakePool("summary_executor", calls)
    loop.container_manager = _FakeContainers(calls)
    loop.client = _FakeClient(calls, settings or Settings(step_timeout=600, decide_timeout=600))
    return loop, calls


def _record_stage(loop: DispatcherLoop, calls: list[str], name: str) -> None:
    setattr(loop, name, lambda *_args, **_kwargs: calls.append(name))


def test_run_once_walks_one_tick_in_order_then_closes_every_collaborator() -> None:
    loop, calls = _stub_loop()
    _record_stage(loop, calls, "run_startup_healthchecks")
    _record_stage(loop, calls, "_validate_server_settings")
    for stage in TICK_STAGES:
        _record_stage(loop, calls, stage)

    loop.run(once=True)

    assert calls == [
        "run_startup_healthchecks",
        "_validate_server_settings",
        "_reap_futures",
        "_reap_cleanup_futures",
        "list_projects",
        "_initialize_decide_checkpoints",
        "_refresh_runtime_projects",
        "_cancel_inactive_tasks",
        "_queue_container_cleanups",
        "_reap_summary_futures",
        "_dispatch_questions",
        "_dispatch_available",
        "shutdown:executor:True",
        "shutdown:cleanup_executor:True",
        "shutdown:question_executor:True",
        "shutdown:summary_executor:True",
        "close:containers",
        "close:client",
    ]
    assert loop._settings_checked is True


def test_a_server_request_error_retries_after_the_interval(monkeypatch) -> None:
    loop, calls = _stub_loop(runtime={"interval": 5})
    _record_stage(loop, calls, "run_startup_healthchecks")
    _record_stage(loop, calls, "_validate_server_settings")
    for stage in TICK_STAGES:
        _record_stage(loop, calls, stage)
    attempts = {"count": 0}

    def list_projects() -> list:
        attempts["count"] += 1
        calls.append("list_projects")
        if attempts["count"] == 1:
            raise requests.ConnectionError("server down")
        raise SystemExit("stop the loop")

    loop.client.list_projects = list_projects
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(f"sleep:{seconds}"))

    with pytest.raises(SystemExit):
        loop.run()

    assert attempts["count"] == 2
    assert "sleep:5" in calls
    assert "close:client" in calls


def test_an_unexpected_tick_failure_is_contained_and_retried(monkeypatch) -> None:
    loop, calls = _stub_loop(runtime={"interval": 7})
    _record_stage(loop, calls, "run_startup_healthchecks")
    _record_stage(loop, calls, "_validate_server_settings")
    for stage in TICK_STAGES:
        _record_stage(loop, calls, stage)
    attempts = {"count": 0}

    def reap() -> None:
        attempts["count"] += 1
        calls.append("_reap_futures")
        if attempts["count"] == 1:
            raise ValueError("bug in a mixin")
        raise SystemExit("stop the loop")

    loop._reap_futures = reap
    monkeypatch.setattr(time, "sleep", lambda seconds: calls.append(f"sleep:{seconds}"))

    with pytest.raises(SystemExit):
        loop.run()

    assert attempts["count"] == 2
    assert "sleep:7" in calls


def test_once_mode_lets_a_server_error_escape() -> None:
    loop, calls = _stub_loop()
    _record_stage(loop, calls, "run_startup_healthchecks")
    _record_stage(loop, calls, "_validate_server_settings")

    def list_projects() -> list:
        raise requests.ConnectionError("server down")

    loop.client.list_projects = list_projects

    with pytest.raises(requests.ConnectionError):
        loop.run(once=True)

    assert "close:client" in calls


def test_validate_server_settings_rejects_a_timeout_at_or_below_the_interval() -> None:
    loop, _ = _stub_loop(
        settings=Settings(step_timeout=5, decide_timeout=600), runtime={"interval": 5}
    )

    with pytest.raises(RuntimeError, match="step_timeout=5s must be greater than"):
        loop._validate_server_settings()


def test_validate_server_settings_warns_when_the_heartbeat_slack_is_thin(caplog) -> None:
    loop, _ = _stub_loop(
        settings=Settings(step_timeout=9, decide_timeout=600), runtime={"interval": 5}
    )

    with caplog.at_level("WARNING"):
        loop._validate_server_settings()

    assert "server step_timeout is tight" in caplog.text
    assert "heartbeat slack is only 4s" in caplog.text


def test_validate_server_settings_logs_a_comfortable_slack(caplog) -> None:
    loop, _ = _stub_loop(
        settings=Settings(step_timeout=60, decide_timeout=90), runtime={"interval": 5}
    )

    with caplog.at_level("INFO"):
        loop._validate_server_settings()

    assert "server setting validated step_timeout=60s interval=5s" in caplog.text
    assert "server setting validated decide_timeout=90s interval=5s" in caplog.text


def _healthcheck(name: str, ok: bool) -> StartupHealthcheckResult:
    return StartupHealthcheckResult(
        worker_name=name,
        ok=ok,
        status=200 if ok else 500,
        duration_ms=3,
        detail="" if ok else "boom",
        endpoint="http://worker",
    )


def test_startup_healthchecks_pass_when_any_worker_is_healthy(monkeypatch) -> None:
    loop, _ = _stub_loop()
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.loop.core.run_startup_healthchecks",
        lambda _config, *, show_commands: [_healthcheck("w1", True), _healthcheck("w2", False)],
    )

    loop._run_startup_healthchecks(show_commands=False)


def test_startup_healthchecks_fail_when_every_worker_is_unhealthy(monkeypatch) -> None:
    loop, _ = _stub_loop()
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.loop.core.run_startup_healthchecks",
        lambda _config, *, show_commands: [_healthcheck("w1", False)],
    )

    with pytest.raises(RuntimeError, match=r"startup healthchecks failed for all workers: w1"):
        loop._run_startup_healthchecks(show_commands=False)


def test_startup_healthchecks_only_forces_the_diagnostic_and_closes() -> None:
    loop, calls = _stub_loop()
    loop.config.runtime.worker_healthcheck = "disabled"
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append(
        f"healthchecks:{show_commands}"
    )

    loop.run_startup_healthchecks_only()

    assert "healthchecks:True" in calls
    assert "close:client" in calls


def test_startup_healthchecks_are_not_repeated_once_checked() -> None:
    loop, calls = _stub_loop()
    loop._run_startup_healthchecks = lambda *, show_commands: calls.append("healthchecks")

    loop.run_startup_healthchecks()
    loop.run_startup_healthchecks(force=True)

    assert calls.count("healthchecks") == 1


def test_local_mode_fails_when_no_worker_cli_is_installed(monkeypatch) -> None:
    loop, _ = _stub_loop(runtime={"execution": "local"})
    monkeypatch.setattr(shutil, "which", lambda _binary: None)

    with pytest.raises(RuntimeError, match="none of the configured worker CLIs are installed"):
        loop._run_local_binary_check()


def test_local_mode_warns_about_missing_clis_but_keeps_the_available_ones(
    monkeypatch, caplog
) -> None:
    loop, _ = _stub_loop(runtime={"execution": "local"})
    loop.config.workers = [
        *loop.config.workers,
        WorkerConfig(name="pi-worker", type="pi", task_types=["decide"], max_running=1, priority=0),
    ]
    monkeypatch.setattr(
        shutil, "which", lambda binary: "/usr/bin/python3" if binary == "python3" else None
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0))

    with caplog.at_level("WARNING"):
        loop._run_local_binary_check()

    assert "Missing CLIs, their workers cannot run: pi" in caplog.text
    assert "Local mode uses each CLI's own host config" in caplog.text


def test_probe_local_cli_distinguishes_missing_unrunnable_and_crashing(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _binary: None)
    assert DispatcherLoop._probe_local_cli("pi") == (None, False)

    monkeypatch.setattr(shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1))
    assert DispatcherLoop._probe_local_cli("pi") == ("/usr/bin/pi", False)

    def boom(*_args, **_kwargs):
        raise OSError("no exec")

    monkeypatch.setattr(subprocess, "run", boom)
    assert DispatcherLoop._probe_local_cli("pi") == ("/usr/bin/pi", False)


def test_local_mode_skips_the_cli_check_when_no_worker_reports_a_binary(monkeypatch) -> None:
    loop, _ = _stub_loop(runtime={"execution": "local"})
    loop.config.workers = []
    monkeypatch.setattr(
        "cairn.dispatcher.scheduler.loop.core.get_driver",
        lambda *_args: SimpleNamespace(local_binary=lambda: None),
    )

    loop._run_local_binary_check()
