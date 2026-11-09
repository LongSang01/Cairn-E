from __future__ import annotations

import abc
from dataclasses import dataclass

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.health import HealthResult
from cairn.dispatcher.workers.usage import Usage


@dataclass(slots=True)
class DriverResult:
    argv: list[str]
    session: str | None = None


class WorkerDriver(abc.ABC):
    type_name: str

    def supports_conclude(self) -> bool:
        return True

    def local_binary(self) -> str | None:
        """Executable this driver invokes in local mode, checked on PATH at startup.

        None means the driver has no host binary to verify (or is not used locally).
        """
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

    def prepare_session(self) -> str | None:
        """A per-run session id the CLI will accept; None means the CLI assigns one."""
        return None
