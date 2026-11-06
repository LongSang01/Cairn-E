"""HTTP evidence extraction, finding status gating and the SRC-style report."""

from __future__ import annotations

from fastapi.testclient import TestClient

from cairn.server.services import parse_http_exchanges

RAW_CURL = """GET /login.php HTTP/1.1
Host: target.local
User-Agent: curl/8.5.0

HTTP/1.1 200 OK
Content-Type: text/html

<html><body>login</body></html>
"""

VERBOSE_CURL = """* Trying 10.0.0.5:80...
> GET /admin HTTP/1.1
> Host: target.local
>
< HTTP/1.1 302 Found
< Location: /login
<
"""

REQUESTS_DUMP = """url = 'http://target.local/api/users'
r = session.get(url)
print(r)
<Response [401]>
"""


def _create_project(client: TestClient) -> str:
    response = client.post(
        "/projects",
        json={"title": "eng", "origin": "starting point", "goal": "finish"},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def _create_running_step(client: TestClient, project_id: str) -> str:
    response = client.post(
        f"/projects/{project_id}/steps",
        json={"from": ["origin"], "description": "probe login", "creator": "decider"},
    )
    assert response.status_code == 201
    step_id = response.json()["id"]
    response = client.post(
        f"/projects/{project_id}/steps/{step_id}/heartbeat", json={"worker": "executor"}
    )
    assert response.status_code == 200
    assert response.json()["status"] == "running"
    return step_id


def _start_session(client: TestClient, project_id: str, step_id: str) -> str:
    response = client.post(
        f"/projects/{project_id}/sessions",
        json={
            "worker": "executor",
            "worker_type": "pi",
            "phase": "execute",
            "step": step_id,
            "status": "running",
            "messages": [{"role": "user", "content": "probe the login form"}],
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


def _append_tool_output(client: TestClient, project_id: str, session_id: str, output: str) -> None:
    response = client.post(
        f"/projects/{project_id}/sessions/{session_id}/messages",
        json={"messages": [{"role": "tool_result", "content": output}]},
    )
    assert response.status_code == 200


def test_parse_raw_request_response_pair() -> None:
    exchanges = parse_http_exchanges(RAW_CURL)

    assert len(exchanges) == 1
    exchange = exchanges[0]
    assert exchange.method == "GET"
    # The request line only has the path; the Host header fills in the origin.
    assert exchange.url == "http://target.local/login.php"
    assert exchange.status == 200
    assert "GET /login.php HTTP/1.1" in exchange.request
    assert "<html><body>login</body></html>" in exchange.response
    assert len(exchange.digest) == 64


def test_parse_verbose_curl_markers() -> None:
    exchanges = parse_http_exchanges(VERBOSE_CURL)

    assert len(exchanges) == 1
    assert exchanges[0].method == "GET"
    assert exchanges[0].url == "http://target.local/admin"
    assert exchanges[0].status == 302


def test_parse_requests_repr_falls_back_to_nearby_url() -> None:
    exchanges = parse_http_exchanges(REQUESTS_DUMP)

    assert len(exchanges) == 1
    assert exchanges[0].status == 401
    assert exchanges[0].url == "http://target.local/api/users"


def test_plain_text_yields_no_exchange() -> None:
    assert parse_http_exchanges("nothing to see here\njust logs") == []


def test_confirmed_finding_gets_frozen_exchange_and_report(client: TestClient) -> None:
    project_id = _create_project(client)
    step_id = _create_running_step(client, project_id)
    session_id = _start_session(client, project_id, step_id)
    _append_tool_output(client, project_id, session_id, RAW_CURL)

    response = client.post(
        f"/projects/{project_id}/steps/{step_id}/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "findings": [
                {
                    "kind": "sqli",
                    "severity": "high",
                    "status": "confirmed",
                    "description": "' OR 1=1-- bypasses the login form",
                    "evidence": [{"kind": "exchange", "ref": "http://target.local/login.php"}],
                }
            ],
        },
    )

    assert response.status_code == 200
    finding = response.json()["findings"][0]
    assert finding["status"] == "confirmed"
    assert finding["evidence"][0]["kind"] == "exchange"
    # The positional id is what gets stored, not the URL the worker wrote.
    assert finding["evidence"][0]["ref"] == f"{session_id}:1:0"
    assert finding["evidence"][0]["digest"]

    report = client.get(f"/projects/{project_id}/export", params={"format": "report"})
    assert report.status_code == 200
    assert report.headers["content-type"].startswith("text/markdown")
    body = report.text
    assert "confirmed 1 / tentative 0" in body
    assert "GET /login.php HTTP/1.1" in body
    assert "HTTP/1.1 200 OK" in body


def test_confirm_without_evidence_is_downgraded(client: TestClient) -> None:
    project_id = _create_project(client)
    step_id = _create_running_step(client, project_id)

    response = client.post(
        f"/projects/{project_id}/steps/{step_id}/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "findings": [
                {
                    "kind": "weakness",
                    "severity": "low",
                    "status": "confirmed",
                    "description": "smells like a weak CSP header",
                }
            ],
        },
    )

    assert response.status_code == 200
    finding = response.json()["findings"][0]
    assert finding["status"] == "tentative"
    assert finding["evidence"] == []

    report = client.get(f"/projects/{project_id}/export", params={"format": "report"})
    assert "confirmed 0 / tentative 1" in report.text
    assert "待验证条目（tentative）" in report.text


def test_document_evidence_is_accepted(client: TestClient) -> None:
    project_id = _create_project(client)
    step_id = _create_running_step(client, project_id)

    response = client.post(
        f"/projects/{project_id}/steps/{step_id}/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "facts": [
                {
                    "description": "captured a config leak",
                    "documents": [{"title": "dump", "content": "SECRET=1"}],
                }
            ],
            "findings": [
                {
                    "kind": "exposure",
                    "severity": "medium",
                    "status": "confirmed",
                    "description": "config leaked",
                    "evidence": [{"kind": "document", "ref": "doc001"}],
                }
            ],
        },
    )

    assert response.status_code == 200
    finding = response.json()["findings"][0]
    assert finding["status"] == "confirmed"
    assert finding["evidence"][0]["kind"] == "document"

    report = client.get(f"/projects/{project_id}/export", params={"format": "report"})
    assert "SECRET=1" in report.text


def test_unknown_evidence_reference_is_dropped(client: TestClient) -> None:
    project_id = _create_project(client)
    step_id = _create_running_step(client, project_id)

    response = client.post(
        f"/projects/{project_id}/steps/{step_id}/conclude",
        json={
            "worker": "executor",
            "status": "done",
            "findings": [
                {
                    "kind": "weakness",
                    "severity": "low",
                    "status": "confirmed",
                    "description": "nothing actually captured",
                    "evidence": [
                        {"kind": "document", "ref": "d999"},
                        {"kind": "exchange", "ref": "http://nowhere.local/x"},
                        {"kind": "seq", "ref": "9999"},
                    ],
                }
            ],
        },
    )

    assert response.status_code == 200
    finding = response.json()["findings"][0]
    assert finding["status"] == "tentative"
    assert finding["evidence"] == []
