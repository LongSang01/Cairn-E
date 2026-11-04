from __future__ import annotations

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.base import (
    DriverResult,
    SeedSessionDriver,
    Usage,
    add_usage,
    normalize_usage,
)
from cairn.dispatcher.workers.health import HealthResult, http_ping, proxies_from_env
from cairn.dispatcher.workers.stream import iter_json_events, text_from_content

ANTHROPIC_VERSION = "2023-06-01"
# Streaming JSON is what makes the transcript complete: plain `-p` only prints the
# final answer, so tool calls and their results would never reach the sessions view.
STREAM_FLAGS = ["--output-format", "stream-json", "--verbose"]


class ClaudeCodeDriver(SeedSessionDriver):
    type_name = "claudecode"

    def local_binary(self) -> str | None:
        return "claude"

    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        env = worker.env
        return http_ping(
            f"{env['ANTHROPIC_BASE_URL']}/v1/messages",
            headers={
                "Authorization": f"Bearer {env['ANTHROPIC_AUTH_TOKEN']}",
                "anthropic-version": ANTHROPIC_VERSION,
                "content-type": "application/json",
            },
            json_body={
                "model": env["ANTHROPIC_MODEL"],
                "max_tokens": 10,
                "messages": [{"role": "user", "content": "ping"}],
            },
            timeout=timeout,
            proxies=proxies_from_env(env),
        )

    def describe_health(self, worker: WorkerConfig) -> str:
        return f"POST {worker.env['ANTHROPIC_BASE_URL']}/v1/messages (model={worker.env['ANTHROPIC_MODEL']})"

    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        assert session is not None
        return DriverResult(
            argv=[
                "claude",
                "--session-id",
                session,
                "--dangerously-skip-permissions",
                *STREAM_FLAGS,
                "-p",
                "--",
                prompt,
            ],
            session=session,
        )

    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        return [
            "claude",
            "-r",
            session,
            "--dangerously-skip-permissions",
            *STREAM_FLAGS,
            "-p",
            "--",
            prompt,
        ]

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        """Pick the model's final answer out of the stream-json events."""
        assistant_text = ""
        result_text: str | None = None
        for event in iter_json_events(stdout):
            kind = event.get("type")
            if kind == "result" and isinstance(event.get("result"), str):
                result_text = event["result"]
            elif kind == "assistant":
                message = event.get("message")
                if isinstance(message, dict):
                    text = text_from_content(message.get("content")).strip()
                    if text:
                        assistant_text = text
        if result_text is not None and result_text.strip():
            return result_text
        if assistant_text:
            return assistant_text
        # Plain text mode (older CLI, or stream-json unavailable): stdout is the answer.
        return stdout

    def extract_usage(self, stdout: str, stderr: str) -> Usage | None:
        """The `result` event carries the run's cumulative usage."""
        usage: Usage | None = None
        for event in iter_json_events(stdout):
            if event.get("type") != "result":
                continue
            found = normalize_usage(event.get("usage"))
            if found is None:
                # Newer CLIs break the same totals down per model.
                model_usage = event.get("modelUsage")
                if isinstance(model_usage, dict):
                    for entry in model_usage.values():
                        found = add_usage(found, normalize_usage(entry))
            usage = add_usage(usage, found)
        return usage
