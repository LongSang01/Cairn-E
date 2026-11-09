import os
from pathlib import Path

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db

_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS


@click.group()
def main():
    """Cairn - Fact-graph based collaborative exploration protocol."""


@main.command()
@click.option("--host", default="127.0.0.1", show_default=True, help="Bind host")
@click.option("--port", default=8000, show_default=True, help="Bind port")
@click.option(
    "--db-path",
    type=click.Path(),
    default=str(db.DEFAULT_DB),
    show_default=True,
    help="SQLite database path",
)
@click.option("--log-level", default="info", show_default=True, help="Uvicorn log level")
@click.option(
    "--log-format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    envvar="CAIRN_LOG_FORMAT",
    help="Server log format",
)
@click.option(
    "--access-log/--no-access-log",
    default=True,
    show_default=True,
    help="Enable Uvicorn access log",
)
@click.option(
    "--web-config",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    envvar="CAIRN_WEB_CONFIG",
    help="dispatch.yaml whose web.auth protects the UI (default: ./dispatch.yaml if present)",
)
@click.option(
    "--allow-insecure",
    is_flag=True,
    help="Allow binding a non-loopback host while web.auth is not configured",
)
def serve(
    host: str,
    port: int,
    db_path: str,
    log_level: str,
    log_format: str,
    access_log: bool,
    web_config: Path | None,
    allow_insecure: bool,
):
    """Start the Cairn API server."""
    from cairn.server.auth import WEB_CONFIG_ENV, load_web_auth

    config_path = web_config
    if config_path is None and Path("dispatch.yaml").exists():
        config_path = Path("dispatch.yaml")
    auth = None
    if config_path is not None:
        try:
            auth = load_web_auth(config_path)
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
    if auth is None and not _is_loopback(host) and not allow_insecure:
        raise click.ClickException(
            f"refusing to bind {host} without authentication: add web.auth to "
            f"{config_path or 'dispatch.yaml'} or pass --allow-insecure"
        )
    if config_path is not None:
        # The middleware resolves the file on its own; hand it the same path the CLI
        # validated instead of duplicating the default lookup.
        os.environ[WEB_CONFIG_ENV] = str(config_path)
    db.configure(Path(db_path))
    configure_logging(log_level.upper(), json_format=log_format == "json")
    # Imported here so `cairn dispatch` never loads the web stack.
    from cairn.server.app import app

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level.lower(),
        access_log=access_log,
    )


@main.command()
@click.option(
    "--config",
    "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Dispatcher config path",
)
@click.option("--once", is_flag=True, help="Run one scheduling iteration and exit")
@click.option(
    "--startup-healthcheck-only",
    is_flag=True,
    help="Run startup worker healthchecks and exit",
)
@click.option("--log-level", default="INFO", show_default=True, help="Log level")
@click.option(
    "--log-format",
    type=click.Choice(["text", "json"]),
    default="text",
    show_default=True,
    envvar="CAIRN_LOG_FORMAT",
    help="Log format",
)
def dispatch(
    config_path: Path,
    once: bool,
    startup_healthcheck_only: bool,
    log_level: str,
    log_format: str,
):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only, json_format=log_format == "json")
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
