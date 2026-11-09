"""The server's own Basic Auth, replacing the nginx sidecar that used to sit in front.

The middleware is the only thing standing between the LAN and a project's data, so the
tests cover both directions: what must be rejected (missing/wrong credentials, the
worker's token on a UI endpoint, cross-site writes) and what must stay open (the
liveness probe, the worker channel).
"""

from __future__ import annotations

import base64

from click.testing import CliRunner
from fastapi.testclient import TestClient

from cairn import cli
from cairn.server import auth

AUTH_YAML = """
web:
  auth:
    realm: "Cairn Test"
    users:
      - name: admin
        password: "s3cret"
"""


def _write_config(tmp_path, text: str = AUTH_YAML):
    path = tmp_path / "dispatch.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def _basic(user: str, password: str) -> dict[str, str]:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


def _create_project(client: TestClient, headers: dict[str, str] | None = None) -> str:
    response = client.post(
        "/projects",
        json={"title": "eng", "origin": "starting point", "goal": "finish"},
        headers=headers or {},
    )
    assert response.status_code == 201
    return response.json()["project"]["id"]


def test_the_ui_is_open_when_no_web_auth_is_configured(client: TestClient) -> None:
    assert client.get("/projects").status_code == 200


def test_basic_auth_protects_pages_assets_and_api(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))

    for path in ("/projects", "/static/index.html", "/"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.headers["www-authenticate"] == 'Basic realm="Cairn Test"'

    assert client.get("/projects", headers=_basic("admin", "s3cret")).status_code == 200


def test_a_wrong_password_is_rejected(client: TestClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))

    assert client.get("/projects", headers=_basic("admin", "nope")).status_code == 401
    assert client.get("/projects", headers=_basic("nobody", "s3cret")).status_code == 401


def test_the_health_probe_and_worker_channel_skip_ui_auth(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))

    assert client.get("/healthz").status_code == 200
    # The worker channel has its own token contract; it must not demand Basic Auth.
    assert client.get("/internal/projects/missing/tool-approvals/appr_missing").status_code == 404


def test_a_basic_authenticated_browser_can_change_the_guard(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))
    headers = _basic("admin", "s3cret")
    project_id = _create_project(client, headers)

    response = client.post(
        f"/projects/{project_id}/tool-rules",
        json={"target": "command", "match_type": "contains", "pattern": "curl", "action": "allow"},
        headers=headers,
    )

    assert response.status_code == 201


def test_a_worker_token_cannot_reach_a_ui_endpoint(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))
    monkeypatch.setenv("CAIRN_WORKER_TOKEN", "worker-secret")
    project_id = _create_project(client, _basic("admin", "s3cret"))

    response = client.post(
        f"/projects/{project_id}/tool-rules",
        json={"target": "command", "match_type": "contains", "pattern": "curl", "action": "allow"},
        headers={"x-cairn-worker-token": "worker-secret"},
    )

    assert response.status_code == 401


def test_a_cross_site_write_is_rejected(client: TestClient, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))
    headers = {**_basic("admin", "s3cret"), "Sec-Fetch-Site": "cross-site"}
    project_id = _create_project(client, _basic("admin", "s3cret"))

    response = client.post(
        f"/projects/{project_id}/scope",
        json={"kind": "cidr", "value": "10.0.0.0/8"},
        headers=headers,
    )

    assert response.status_code == 403


def test_a_cross_origin_without_sec_fetch_site_is_rejected(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    """Older browsers only send Origin; the same check has to catch them."""
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))
    project_id = _create_project(client, _basic("admin", "s3cret"))
    headers = {
        **_basic("admin", "s3cret"),
        "Origin": "http://evil.example",
    }

    response = client.post(
        f"/projects/{project_id}/scope",
        json={"kind": "cidr", "value": "10.0.0.0/8"},
        headers=headers,
    )

    assert response.status_code == 403


def test_the_legacy_ui_token_still_authenticates_scripted_clients(
    client: TestClient, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path)))
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")

    assert client.get("/projects").status_code == 401
    assert client.get("/projects", headers={"x-cairn-ui-token": "ui-secret"}).status_code == 200


def test_a_broken_auth_block_fails_closed(client: TestClient, monkeypatch, tmp_path) -> None:
    broken = "web:\n  auth:\n    users:\n      - name: admin\n"
    monkeypatch.setenv(auth.WEB_CONFIG_ENV, str(_write_config(tmp_path, broken)))

    assert client.get("/projects").status_code == 503


def test_load_web_auth_parses_the_block(tmp_path) -> None:
    config = auth.load_web_auth(_write_config(tmp_path))

    assert config is not None
    assert config.realm == "Cairn Test"
    assert config.matches("admin", "s3cret")
    assert not config.matches("admin", "wrong")


def test_load_web_auth_returns_none_without_users(tmp_path) -> None:
    path = _write_config(tmp_path, "web: {}\n")

    assert auth.load_web_auth(path) is None


def test_serve_refuses_a_public_bind_without_auth(tmp_path) -> None:
    """Binding 0.0.0.0 with no credentials would expose the UI to the LAN."""
    config = _write_config(tmp_path, "web: {}\n")

    result = CliRunner().invoke(
        cli.main, ["serve", "--host", "0.0.0.0", "--web-config", str(config)]
    )

    assert result.exit_code != 0
    assert "refusing to bind" in result.output


def test_serve_warns_when_only_the_legacy_ui_token_is_set(monkeypatch, tmp_path) -> None:
    """A token the browser cannot send would silently lock it out of write endpoints."""
    monkeypatch.delenv(auth.WEB_CONFIG_ENV, raising=False)
    monkeypatch.setenv("CAIRN_UI_TOKEN", "ui-secret")
    monkeypatch.setattr(cli.db, "configure", lambda path: None)
    monkeypatch.setattr(cli, "configure_logging", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kwargs: None)

    result = CliRunner().invoke(
        cli.main, ["serve", "--host", "127.0.0.1", "--db-path", str(tmp_path / "cairn.db")]
    )

    assert result.exit_code == 0, result.output
    assert "CAIRN_UI_TOKEN is set without web.auth" in result.output
