from __future__ import annotations

import abc
import math
import re
import uuid
from dataclasses import dataclass
from typing import Any

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.health import HealthResult


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


@dataclass(slots=True)
class Usage:
    """Token counters one worker run reported, normalised across CLI dialects."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reported_total: int = 0

    @property
    def total_tokens(self) -> int:
        # Some CLIs report a total that also counts reasoning tokens; trust it when it
        # is at least the sum of the parts we know, otherwise fall back to the parts.
        measured = (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_write_tokens
        )
        return max(measured, self.reported_total)

    def __bool__(self) -> bool:
        return bool(self.total_tokens)

    def payload(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
        }


_INPUT_KEYS = ("input_tokens", "prompt_tokens", "input", "inputTokens")
_OUTPUT_KEYS = ("output_tokens", "completion_tokens", "output", "outputTokens")
_CACHE_READ_KEYS = (
    "cache_read_tokens",
    "cache_read_input_tokens",
    "cached_input_tokens",
    "cacheReadInputTokens",
    "cacheRead",
    "cache_read",
)
_CACHE_WRITE_KEYS = (
    "cache_write_tokens",
    "cache_creation_input_tokens",
    "cacheCreationInputTokens",
    "cacheWrite",
    "cache_write",
)
_TOTAL_KEYS = ("total_tokens", "totalTokens", "total")


def _counter(container: dict[str, Any], keys: tuple[str, ...]) -> int:
    for key in keys:
        value = container.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        # JSON allows Infinity/NaN; neither is a token count.
        if math.isfinite(value) and value >= 0:
            return int(value)
    return 0


def normalize_usage(raw: Any, *, cache_in_input: bool = False) -> Usage | None:
    """Read a usage payload in any of the shapes agent CLIs emit.

    Unknown or malformed payloads return None so a run without usage metadata simply
    records no tokens instead of inventing numbers.

    Set ``cache_in_input`` for dialects where cached prompt tokens are a subset of
    ``input_tokens`` (OpenAI). The shared counters keep the two disjoint so a project
    total never counts the same cached tokens twice.
    """
    if not isinstance(raw, dict):
        return None
    usage = Usage(
        input_tokens=_counter(raw, _INPUT_KEYS),
        output_tokens=_counter(raw, _OUTPUT_KEYS),
        cache_read_tokens=_counter(raw, _CACHE_READ_KEYS),
        cache_write_tokens=_counter(raw, _CACHE_WRITE_KEYS),
        reported_total=_counter(raw, _TOTAL_KEYS),
    )
    if cache_in_input:
        usage.input_tokens = max(0, usage.input_tokens - usage.cache_read_tokens)
    return usage if usage else None


def add_usage(left: Usage | None, right: Usage | None) -> Usage | None:
    if left is None:
        return right
    if right is None:
        return left
    return Usage(
        input_tokens=left.input_tokens + right.input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        cache_read_tokens=left.cache_read_tokens + right.cache_read_tokens,
        cache_write_tokens=left.cache_write_tokens + right.cache_write_tokens,
        reported_total=left.reported_total + right.reported_total,
    )


def usage_payload(usage: Usage | None) -> dict[str, int]:
    return usage.payload() if usage is not None else {}


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def local_binary(self) -> str | None:
        """Executable this driver invokes in local mode, checked on PATH at startup.

        None means the driver has no host binary to verify (or is not used locally).
        """
        return None

    def prepare_session(self) -> str | None:
        return None

    @abc.abstractmethod
    def check_health(self, worker: WorkerConfig, *, timeout: float) -> HealthResult:
        """Verify this worker's LLM config is usable, in-process (no container, no curl)."""
        raise NotImplementedError

    def describe_health(self, worker: WorkerConfig) -> str:
        return "in-process API ping"

    @abc.abstractmethod
    def build_execute(self, worker: WorkerConfig, prompt: str, session: str | None) -> DriverResult:
        raise NotImplementedError

    @abc.abstractmethod
    def build_conclude(self, worker: WorkerConfig, prompt: str, session: str) -> list[str]:
        raise NotImplementedError

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        return session

    def extract_response_text(self, stdout: str, stderr: str) -> str:
        return stdout

    def extract_usage(self, stdout: str, stderr: str) -> Usage | None:
        """Token usage the CLI printed, if it reports any. Absent means unmeasured."""
        return None


class SeedSessionDriver(WorkerDriver):
    def prepare_session(self) -> str | None:
        return str(uuid.uuid4())


class RegexSessionDriver(WorkerDriver):
    session_pattern = re.compile(r"session id:\s*([0-9a-fA-F-]+)")

    def extract_session(self, session: str | None, stdout: str, stderr: str) -> str | None:
        if session:
            return session
        match = self.session_pattern.search(stderr)
        if match:
            return match.group(1)
        return None
