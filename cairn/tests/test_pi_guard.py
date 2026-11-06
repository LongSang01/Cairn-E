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
        backend, "c1", _worker(), "proj_001", ToolGuardConfig(enabled=True),
        session_id="s001",
    )

    paths = [path for path, _content in backend.writes]
    assert paths == [
        "/tmp/cairn-pi/pi-test/cairn-guard.json",
        "/tmp/cairn-pi/pi-test/extensions/cairn-guard.ts",
    ]


def test_install_stamps_the_project_and_api_url() -> None:
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
    }
    extension = backend.writes[1][1]
    assert 'pi.on("tool_call"' in extension
    assert "session: CFG.session_id" in extension
