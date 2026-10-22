from pathlib import Path
import json
import os

import click
import uvicorn

from cairn.dispatcher.logging import configure_logging
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from cairn.server import db


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
@click.option("--access-log/--no-access-log", default=True, show_default=True, help="Enable Uvicorn access log")
def serve(host: str, port: int, db_path: str, log_level: str, access_log: bool):
    """Start the Cairn API server."""
    db.configure(Path(db_path))
    from cairn.server.access import credential, credential_path
    for role in ("operator", "worker"):
        credential(role)
        location = f"CAIRN_{role.upper()}_TOKEN environment" if os.environ.get(f"CAIRN_{role.upper()}_TOKEN") else str(credential_path(role))
        click.echo(f"Development {role} credential: {location}", err=True)
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
def dispatch(config_path: Path, once: bool, startup_healthcheck_only: bool, log_level: str):
    """Run the Cairn dispatcher."""
    configure_logging(log_level, bare=startup_healthcheck_only)
    loop = DispatcherLoop(config_path)
    try:
        if startup_healthcheck_only:
            loop.run_startup_healthchecks_only()
            return
        loop.run(once=once)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@main.group()
def operation():
    """Reserve and reconcile external actions using the current run's scoped token."""


def operation_request(action: str, payload: dict, method="POST"):
    from cairn.dispatcher.protocol.client import CairnClient
    required = ("CAIRN_SERVER", "CAIRN_PROJECT_ID", "CAIRN_RUN_ID", "CAIRN_RUN_TOKEN")
    if any(not os.environ.get(name) for name in required):
        raise click.ClickException("Operation commands require a Cairn-managed run environment")
    client = CairnClient(os.environ["CAIRN_SERVER"])
    try:
        response = client._request_json(method,
            f"/projects/{os.environ['CAIRN_PROJECT_ID']}/development/runs/{os.environ['CAIRN_RUN_ID']}/operations" + action,
            {**payload, "token": os.environ["CAIRN_RUN_TOKEN"]})
        if not response.ok:
            raise click.ClickException(f"Operation request failed ({response.status_code}): {response.text}")
        click.echo(json.dumps(response.data, ensure_ascii=False))
    finally:
        client.close()


@operation.command("reserve")
@click.option("--key", required=True)
@click.option("--kind", required=True)
@click.option("--request-file", type=click.Path(exists=True, dir_okay=False, path_type=Path), required=True)
def reserve_operation(key: str, kind: str, request_file: Path):
    """Act only if execute=true; query real state before replaying an unknown action."""
    try:
        request = json.loads(request_file.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise click.ClickException(f"Cannot read operation request: {exc}") from exc
    if not isinstance(request, dict):
        raise click.ClickException("Operation request must be a JSON object")
    operation_request("", {"key": key, "kind": kind, "request": request})


@operation.command("record")
@click.option("--key", required=True)
@click.option("--state", type=click.Choice(["unknown", "done", "absent"]), required=True)
@click.option("--resource-id")
@click.option("--evidence", required=True)
def record_operation(key: str, state: str, resource_id: str | None, evidence: str):
    """Record observed resource state; done operations can never be reset for replay."""
    from urllib.parse import quote
    operation_request("/" + quote(key, safe=""), {"state": state, "resource_id": resource_id, "evidence": evidence}, "PUT")
