from __future__ import annotations

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

    result = client.create_intent("proj_001", ["f001"], "investigate", "reasoner")

    assert result.status_code == 0
    assert result.text == "offline"


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


def test_dispatch_config_error_is_readable_and_does_not_print_secret_inputs(tmp_path, monkeypatch):
    import yaml
    from click.testing import CliRunner
    from cairn.cli import main
    from conftest import make_config
    monkeypatch.setattr("cairn.cli.configure_logging", lambda *args, **kwargs: None)
    monkeypatch.delenv("http://127.0.0.1:3425", raising=False)
    payload = make_config().model_dump()
    payload["workers"][0]["env"]["ANTHROPIC_AUTH_TOKEN"] = "do-not-print-private-key"
    payload["workers"][0]["env_from"] = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3425"}
    path = tmp_path / "dispatch.yaml"
    path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    result = CliRunner().invoke(main, ["dispatch", "--config", str(path)])
    assert result.exit_code == 1
    assert "env.ANTHROPIC_BASE_URL" in result.output and "workers.0" in result.output
    assert "do-not-print-private-key" not in result.output
    assert "input_value=" not in result.output and "Traceback" not in result.output
