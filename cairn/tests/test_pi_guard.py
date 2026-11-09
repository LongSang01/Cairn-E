"""Wiring of the pi command-guard extension (paths, config, write order)."""

from __future__ import annotations

import json

from cairn.dispatcher.config import ToolGuardConfig, WorkerConfig
from cairn.dispatcher.workers import pi_guard


class _RecordingBackend:
    def __init__(self) -> None:
        self.writes: list[tuple[str, str]] = []

    def write_text_file(self, container_name: str, path: str, content: str) -> None:
        self.writes.append((path, content))


def _worker() -> WorkerConfig:
    return WorkerConfig.model_validate(
        {
            "name": "pi-test",
            "type": "pi",
            "task_types": ["execute"],
            "max_running": 1,
            "priority": 0,
            "env": {
                "PI_MODEL": "m",
                "PI_BASE_URL": "http://api",
                "PI_API_KEY": "k",
                "PI_PROVIDER_API": "openai-completions",
            },
        }
    )


def test_install_writes_config_before_the_extension() -> None:
    """pi aborts the run when an extension cannot load, so config must land first."""
    backend = _RecordingBackend()

    pi_guard.install(
        backend,
        "c1",
        _worker(),
        "proj_001",
        ToolGuardConfig(enabled=True),
        session_id="s001",
    )

    paths = [path for path, _content in backend.writes]
    assert paths == [
        "/tmp/cairn-pi/pi-test/cairn-guard.json",
        "/tmp/cairn-pi/pi-test/extensions/cairn-guard.ts",
    ]


def test_install_stamps_the_project_and_api_url(monkeypatch) -> None:
    monkeypatch.delenv("CAIRN_WORKER_TOKEN", raising=False)
    backend = _RecordingBackend()

    pi_guard.install(
        backend,
        "c1",
        _worker(),
        "proj_042",
        ToolGuardConfig(enabled=True, api_url="http://127.0.0.1:8001/", fail_action="deny"),
        session_id="s042",
    )

    config = json.loads(backend.writes[0][1])
    assert config == {
        "api_url": "http://127.0.0.1:8001",
        "project_id": "proj_042",
        "worker": "pi-test",
        "session_id": "s042",
        "fail_action": "deny",
        "worker_token": "",
    }
    extension = backend.writes[1][1]
    assert 'pi.on("tool_call"' in extension
    assert "session: CFG.session_id" in extension


def test_extension_sends_the_full_call_plus_script_snapshots() -> None:
    """The reviewer cannot see inside the container, so the extension ships evidence."""
    extension = pi_guard.EXTENSION_SOURCE

    assert "arguments: event.input || { command }" in extension
    assert "working_directory: cwd" in extension
    assert "script_files: captureScripts(command, cwd)" in extension
    assert "/internal/projects/${CFG.project_id}/tool-guard/check" in extension
    assert '"x-cairn-worker-token": CFG.worker_token' in extension
    assert "const MAX_SCRIPT_BYTES = 65536;" in extension
    assert "const MAX_SCRIPTS = 5;" in extension


def test_extension_marks_an_unreadable_script_as_incomplete() -> None:
    extension = pi_guard.EXTENSION_SOURCE

    assert "const truncated = raw.length > MAX_SCRIPT_BYTES;" in extension
    assert "complete: !truncated" in extension
    assert 'content: "", complete: false, truncated: false' in extension


def test_local_mode_still_loads_the_command_guard() -> None:
    """Local mode is not trusted: without `-e` the extension is never discovered."""
    from cairn.dispatcher.workers.adapters.pi import PiDriver

    argv = PiDriver(local=True).build_execute(_worker(), "prompt", None).argv

    assert argv[0] == "/bin/sh"
    assert "/tmp/cairn-pi/pi-test/extensions/cairn-guard.ts" in argv
    script = argv[2]
    assert 'if [ -f "$guard" ]' in script
    assert 'exec pi --no-extensions -e "$guard" "$@"' in script
    assert 'exec pi --no-extensions "$@"' in script
