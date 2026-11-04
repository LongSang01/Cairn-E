from __future__ import annotations

from typing import ClassVar

import requests

from cairn.dispatcher.protocol.client import CairnClient
from cairn.dispatcher.runtime.startup_healthcheck import (
    StartupHealthcheckResult,
    format_failure_summary,
)


def test_client_request_failure_returns_status_zero() -> None:
    class Session:
        def request(self, *_args, **_kwargs):
            raise requests.ConnectionError("offline")

    client = CairnClient("http://server/")
    client._local.session = Session()

    result = client.create_step(
        "proj_001",
        from_ids=["f001"],
        description="investigate",
        creator="decider",
    )

    assert result.status_code == 0
    assert result.text == "offline"


def test_session_heartbeat_posts_without_a_body() -> None:
    class Response:
        status_code = 200
        headers: ClassVar[dict[str, str]] = {"content-type": "application/json"}
        text = "{}"

        def json(self):
            return {}

    class Session:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, dict | None]] = []

        def request(self, method: str, url: str, json=None, timeout=0):
            self.calls.append((method, url, json))
            return Response()

    session = Session()
    client = CairnClient("http://server/")
    client._local.session = session

    result = client.session_heartbeat("proj_001", "x001")

    assert result.ok
    assert session.calls == [
        ("POST", "http://server/projects/proj_001/sessions/x001/heartbeat", None)
    ]


def test_startup_healthcheck_failure_summary_includes_worker_details() -> None:
    results = [
        StartupHealthcheckResult(
            worker_name="worker-a",
            ok=False,
            status=401,
            duration_ms=12,
            detail="unauthorized",
            endpoint="POST http://api/v1/messages",
        ),
        StartupHealthcheckResult(
            worker_name="worker-b",
            ok=True,
            status=200,
            duration_ms=8,
            detail="",
            endpoint="POST http://api/v1/messages",
        ),
    ]

    summary = format_failure_summary(results)

    assert summary == (
        "startup healthchecks failed for all workers: worker-a(http=401, detail=unauthorized)"
    )
