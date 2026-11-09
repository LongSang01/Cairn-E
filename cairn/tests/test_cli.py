"""CLI entry points: serve/dispatch wiring and error mapping."""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from cairn import cli


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "dispatch.yaml"
    path.write_text("server: http://cairn-server:8000\n", encoding="utf-8")
    return path


def test_main_help_lists_commands() -> None:
    result = CliRunner().invoke(cli.main, ["--help"])

    assert result.exit_code == 0
    assert "serve" in result.output
    assert "dispatch" in result.output


def test_serve_configures_db_and_delegates_to_uvicorn(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli.db, "configure", lambda p: captured.setdefault("db", p))
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **kw: captured.setdefault("log", kw))
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: captured.setdefault("uvicorn", kw))

    result = CliRunner().invoke(
        cli.serve,
        ["--host", "0.0.0.0", "--port", "9001", "--log-format", "json", "--no-access-log"],
    )

    assert result.exit_code == 0, result.output
    assert "db" in captured
    assert captured["uvicorn"]["host"] == "0.0.0.0"
    assert captured["uvicorn"]["port"] == 9001
    assert captured["uvicorn"]["access_log"] is False
    assert captured["log"]["json_format"] is True


class _FakeLoop:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.once: bool | None = None
        self.health_only = False

    def run(self, once: bool = False) -> None:
        self.once = once

    def run_startup_healthchecks_only(self) -> None:
        self.health_only = True


def test_dispatch_once_runs_one_iteration(monkeypatch, tmp_path: Path) -> None:
    loop = _FakeLoop(tmp_path)
    monkeypatch.setattr(cli, "DispatcherLoop", lambda path: loop)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **kw: None)

    result = CliRunner().invoke(cli.dispatch, ["--config", str(_config(tmp_path)), "--once"])

    assert result.exit_code == 0, result.output
    assert loop.once is True
    assert loop.health_only is False


def test_dispatch_startup_healthcheck_only(monkeypatch, tmp_path: Path) -> None:
    loop = _FakeLoop(tmp_path)
    monkeypatch.setattr(cli, "DispatcherLoop", lambda path: loop)
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **kw: None)

    result = CliRunner().invoke(
        cli.dispatch, ["--config", str(_config(tmp_path)), "--startup-healthcheck-only"]
    )

    assert result.exit_code == 0, result.output
    assert loop.health_only is True


def test_dispatch_runtime_error_becomes_click_exception(monkeypatch, tmp_path: Path) -> None:
    class _BoomLoop(_FakeLoop):
        def run(self, once: bool = False) -> None:
            raise RuntimeError("bad config")

    monkeypatch.setattr(cli, "DispatcherLoop", lambda path: _BoomLoop(path))
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **kw: None)

    result = CliRunner().invoke(cli.dispatch, ["--config", str(_config(tmp_path)), "--once"])

    assert result.exit_code != 0
    assert "bad config" in result.output
