"""ManagedProcess: container exec lifecycle and streamed output decoding."""

from __future__ import annotations

from typing import Any

from cairn.dispatcher.runtime.process import ManagedProcess


class _FakeApi:
    def __init__(
        self,
        chunks: list[tuple[bytes | None, bytes | None] | bytes],
        *,
        exit_code: int = 0,
        running: bool = False,
        pid: int = 123,
    ) -> None:
        self.chunks = chunks
        self.exit_code = exit_code
        self.running = running
        self.pid = pid
        self.exec_calls: list[tuple[str, list[str], dict[str, Any]]] = []

    def exec_create(self, container_id: str, cmd: list[str], **kw: Any) -> dict[str, str]:
        self.exec_calls.append((container_id, cmd, kw))
        return {"Id": "exec-1"}

    def exec_start(self, exec_id: str, **kw: Any) -> Any:
        return iter(self.chunks)

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"Running": self.running, "Pid": self.pid, "ExitCode": self.exit_code}


class _FakeContainer:
    def __init__(self, api: _FakeApi) -> None:
        self.id = "c1"
        self.name = "c1"
        self.client = type("Client", (), {"api": api})()
        self.kill_calls: list[list[str]] = []

    def exec_run(self, cmd: list[str], **kw: Any) -> Any:
        self.kill_calls.append(cmd)
        return type("Result", (), {"exit_code": 0})()


def test_split_chunk_and_decode() -> None:
    assert ManagedProcess._split_chunk((b"out", b"err")) == ("out", "err")
    assert ManagedProcess._split_chunk(b"only") == ("only", "")
    assert ManagedProcess._split_chunk(None) == ("", "")
    assert ManagedProcess._decode(None) == ""
    assert ManagedProcess._decode("plain") == "plain"
    assert ManagedProcess._decode(b"\xff") == "\ufffd"


def test_start_and_communicate_collects_streamed_output() -> None:
    api = _FakeApi([(b"hello\n", None), (None, b"warn\n")])
    container = _FakeContainer(api)
    proc = ManagedProcess(container, ["echo", "hi"], {"FOO": "bar"})
    emitted: list[str] = []
    proc.on_stdout = emitted.append

    proc.start()
    result = proc.communicate(timeout=2)

    assert result.returncode == 0
    assert result.stdout == "hello\n"
    assert result.stderr == "warn\n"
    assert emitted == ["hello\n"]
    assert not result.timed_out
    assert not result.cancelled
    # exec_create received the command and environment.
    _, cmd, kw = api.exec_calls[0]
    assert cmd == ["echo", "hi"]
    assert kw["environment"] == {"FOO": "bar"}


def test_kill_skips_when_exec_is_not_running() -> None:
    api = _FakeApi([], running=False)
    container = _FakeContainer(api)
    proc = ManagedProcess(container, ["x"], {})
    proc.start()
    proc.communicate(timeout=2)

    proc.kill()

    assert container.kill_calls == []


def test_kill_sends_sigkill_when_exec_is_running() -> None:
    api = _FakeApi([], running=True, pid=42)
    container = _FakeContainer(api)
    proc = ManagedProcess(container, ["sleep", "99"], {})
    proc.start()
    proc.communicate(timeout=2)

    proc.kill()

    assert container.kill_calls == [["kill", "-KILL", "42"]]


def test_cancel_records_reason_and_is_idempotent() -> None:
    api = _FakeApi([], running=True, pid=7)
    container = _FakeContainer(api)
    proc = ManagedProcess(container, ["sleep", "99"], {})
    proc.start()
    proc.communicate(timeout=2)

    proc.cancel("first")
    proc.cancel("second")

    assert proc._cancel_reason == "first"
