from __future__ import annotations

import json
import logging
import re

import pytest
from fastapi.testclient import TestClient

from cairn.dispatcher.logging import JsonFormatter
from cairn.logcontext import correlation_id
from cairn.server import db
from cairn.server.app import app
from cairn.server.routers import metrics as metrics_router


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setattr(db, "_db_path", None)
    monkeypatch.setattr(db, "_busy_total", 0)
    db.configure(tmp_path / "cairn.db")
    with TestClient(app) as test_client:
        yield test_client


def test_metrics_exposes_process_and_db_series(client: TestClient) -> None:
    response = client.post(
        "/projects",
        json={"title": "test", "origin": "starting point", "goal": "finish", "hints": []},
    )
    assert response.status_code == 201

    scrape = client.get("/metrics")
    assert scrape.headers["content-type"].startswith("text/plain")
    text = scrape.text
    assert 'cairn_build_info{version="' in text
    assert "cairn_process_start_time_seconds" in text
    assert "cairn_db_reachable 1.0" in text
    assert "cairn_db_busy_total 0.0" in text
    expected_version = float(max(version for version, _ in db._MIGRATIONS))
    assert f"cairn_schema_version {expected_version}" in text
    assert "cairn_projects 1.0" in text
    assert "# TYPE cairn_sessions gauge" in text
    assert "# TYPE cairn_steps gauge" in text


def test_metrics_reports_unreachable_db(monkeypatch) -> None:
    monkeypatch.setattr(db, "_db_path", None)
    text = metrics_router.collect()
    assert "cairn_db_reachable 0.0" in text
    assert "cairn_projects 0.0" in text
    assert "# HELP cairn_sessions" not in text


def test_request_id_generated_and_echoed(client: TestClient) -> None:
    generated = client.get("/healthz")
    assert generated.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{16}", generated.headers["x-request-id"])

    echoed = client.get("/healthz", headers={"X-Request-ID": "nginx-42"})
    assert echoed.headers["x-request-id"] == "nginx-42"

    rejected = client.get("/healthz", headers={"X-Request-ID": "bad id with spaces!"})
    assert re.fullmatch(r"[0-9a-f]{16}", rejected.headers["x-request-id"])


def test_access_log_carries_structured_fields(client: TestClient, caplog) -> None:
    with caplog.at_level(logging.INFO, logger="cairn.server.access"):
        response = client.get("/healthz", headers={"X-Request-ID": "trace-1"})
    assert response.status_code == 200

    entries = [r for r in caplog.records if r.name == "cairn.server.access"]
    assert entries
    entry = entries[-1]
    assert entry.request_id == "trace-1"
    assert entry.method == "GET"
    assert entry.path == "/healthz"
    assert entry.status == 200
    assert isinstance(entry.duration_ms, float)


def test_json_formatter_includes_request_id_and_extras() -> None:
    token = correlation_id.set("rid-9")
    try:
        record = logging.LogRecord(
            "cairn.server.access", logging.INFO, __file__, 1, "hello %s", ("x",), None
        )
        record.custom_field = "v"  # type: ignore[attr-defined]
        payload = json.loads(JsonFormatter().format(record))
    finally:
        correlation_id.reset(token)

    assert payload["msg"] == "hello x"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "cairn.server.access"
    assert payload["request_id"] == "rid-9"
    assert payload["custom_field"] == "v"
    assert payload["ts"].endswith("Z")
