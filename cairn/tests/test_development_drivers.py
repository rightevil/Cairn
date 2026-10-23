import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import WorkerConfig
from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver
from cairn.dispatcher.workers.adapters.codex import CodexDriver


def worker(kind="claudecode", **kwargs):
    return WorkerConfig(name="role-worker", type=kind, roles=["implementer"], max_running=1, priority=0, **kwargs)


def test_strong_roles_cannot_silently_use_standard_tier():
    with pytest.raises(ValidationError, match="strong model tier"):
        WorkerConfig(name="planner", type="claudecode", roles=["planner"], max_running=1, priority=0)


def test_claude_structured_result_failure_and_session():
    driver = ClaudeCodeDriver()
    role = worker(model="configured-standard-model")
    argv = driver.build_execute(role, "prompt", "session-1").argv
    assert ["--output-format", "json"] == argv[argv.index("--output-format"):argv.index("--output-format") + 2]
    assert role.env["ANTHROPIC_MODEL"] == "configured-standard-model"
    raw = json.dumps({"type": "result", "session_id": "session-1", "result": '{"kind":"implemented","summary":"done"}'})
    assert driver.extract_session(None, raw, "") == "session-1"
    assert json.loads(driver.extract_response_text(raw, ""))["kind"] == "implemented"
    with pytest.raises(ValueError, match="reported an error"):
        driver.extract_response_text(json.dumps({"type": "result", "is_error": True, "result": "quota exceeded"}), "")


def test_local_codex_provider_and_model_survive_execute_and_resume():
    role = worker("codex", model="configured-model", env={"CODEX_BASE_URL": "http://localhost:9999/v1", "OPENAI_API_KEY": "not-a-real-key"})
    driver = CodexDriver(local=True)
    execute = driver.build_execute(role, "prompt", None).argv
    resume = driver.build_conclude(role, "continue", "session-2")
    for argv in [execute, resume]:
        assert "--json" in argv
        assert argv[argv.index("--model") + 1] == "configured-model"
        assert 'model_provider="cairn"' in argv
        assert 'model_providers.cairn.base_url="http://localhost:9999/v1"' in argv


def test_codex_events_recover_session_and_final_message_without_human_log_regex():
    driver = CodexDriver(local=True)
    raw = "\n".join(json.dumps(event) for event in [
        {"type": "thread.started", "thread_id": "thread-123"},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "final answer"}},
        {"type": "turn.completed", "usage": {"input_tokens": 42}},
    ])
    assert driver.extract_session(None, raw, "") == "thread-123"
    assert driver.extract_response_text(raw, "") == "final answer"
    with pytest.raises(ValueError):
        driver.extract_response_text(json.dumps({"type": "turn.failed", "error": {"message": "429"}}), "")


def test_credentials_resolve_from_named_environment_without_global_config_mutation(monkeypatch):
    monkeypatch.setenv("MAGPIE_TEST_TOKEN", "local-test-only")
    role = worker(env_from={"ANTHROPIC_AUTH_TOKEN": "MAGPIE_TEST_TOKEN"})
    assert role.env["ANTHROPIC_AUTH_TOKEN"] == "local-test-only"


def test_loopback_server_bypasses_host_proxy_and_remote_servers_keep_it():
    from cairn.dispatcher.protocol.client import CairnClient
    for url, trust_env in [("http://127.0.0.1:8000", False), ("http://localhost:8000", False), ("https://example.org", True)]:
        client = CairnClient(url)
        assert client._session().trust_env is trust_env
        client.close()


def test_real_capacity_reset_information_is_used_without_a_project_budget():
    from cairn.dispatcher.development import capacity_retry_at, classify_failure
    assert classify_failure("429 quota exceeded")[0] == "capacity"
    assert capacity_retry_at('429 {"reset_at":"2099-01-01T00:00:00Z"}') == "2099-01-01T00:00:00Z"
    assert capacity_retry_at("Retry-After: 60") is not None
    assert capacity_retry_at("unknown reset time") is None


def test_development_example_loads_both_tiers_from_magpie_environment(monkeypatch):
    from cairn.dispatcher.config import DispatchConfig
    for key, value in {"MAGPIE_ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "MAGPIE_API_KEY": "test-only",
                       "MAGPIE_STRONG_MODEL": "strong-model", "MAGPIE_STANDARD_MODEL": "standard-model"}.items():
        monkeypatch.setenv(key, value)
    config = DispatchConfig.load(Path(__file__).resolve().parents[2] / "dispatch.example.yaml")
    assert config.workers[0].env["ANTHROPIC_MODEL"] == "strong-model"
    assert config.workers[1].env["ANTHROPIC_MODEL"] == "standard-model"
    assert Path(config.local.workspace_root).is_absolute()
    assert config.runtime.prompt_group == "development"


def test_pi_local_provider_uses_native_argv_and_private_model_file(tmp_path, monkeypatch):
    from cairn.dispatcher.workers.adapters.pi import PiDriver
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path))
    role = worker("pi", env={"PI_BASE_URL": "http://localhost:9999/v1", "PI_API_KEY": "test-only", "PI_MODEL": "model",
                             "PI_PROVIDER_API": "openai-completions"})
    argv = PiDriver(local=True).build_execute(role, "prompt", None).argv
    assert argv[0] == "pi" and "/bin/sh" not in argv and "--provider" in argv
    model_file = Path(role.env["PI_CODING_AGENT_DIR"]) / "models.json"
    assert json.loads(model_file.read_text())["providers"]["cairn"]["baseUrl"] == "http://localhost:9999/v1"


@pytest.mark.parametrize("native", [True, False])
def test_windows_claude_npm_shim_resolves_native_and_legacy_installs(tmp_path, monkeypatch, native):
    from cairn.dispatcher import development
    shim = tmp_path / "claude.CMD"
    package = tmp_path / "node_modules/@anthropic-ai/claude-code"
    package.mkdir(parents=True)
    legacy = package / "cli.js"
    legacy.touch()
    executable = package / "bin/claude.exe"
    if native:
        executable.parent.mkdir()
        executable.touch()
    monkeypatch.setattr(development, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(development.shutil, "which", lambda name: str(shim) if name == "claude" else "node.exe")
    arguments = ["claude", "-p", "--", '中文 prompt & %PATH% "quoted"']
    resolved = development.executable_argv(worker(), arguments)
    assert resolved == ([str(executable)] if native else ["node.exe", str(legacy)]) + arguments[1:]


def test_unrecognized_windows_cli_install_fails_at_startup(tmp_path, monkeypatch):
    from cairn.dispatcher import development
    from cairn.dispatcher.scheduler.loop import DispatcherLoop
    shim = tmp_path / "claude.cmd"
    monkeypatch.setattr(development, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(development.shutil, "which", lambda name: str(shim))
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.config = SimpleNamespace(workers=[worker()])
    with pytest.raises(RuntimeError, match="Cannot resolve native CLI"):
        loop._run_local_binary_check()


def test_explicit_native_cli_command_keeps_the_original_prompt():
    from cairn.dispatcher.development import executable_argv
    role = worker(command=["/custom/claude.exe"])
    assert executable_argv(role, ["claude", "-p", "--", "prompt"]) == ["/custom/claude.exe", "-p", "--", "prompt"]


def test_cli_probe_runs_the_resolved_native_prefix(monkeypatch):
    from cairn.dispatcher.scheduler import loop
    observed = []
    monkeypatch.setattr(loop.shutil, "which", lambda binary: binary)
    monkeypatch.setattr(loop.subprocess, "run", lambda argv, **kw: observed.append(argv) or SimpleNamespace(returncode=0))
    path, runnable = loop.DispatcherLoop._probe_local_cli(("node.exe", "/CLI with spaces/cli.js"))
    assert runnable and path == "node.exe"
    assert observed == [["node.exe", "/CLI with spaces/cli.js", "--help"]]


@pytest.mark.parametrize("model", ["group/cairn-strong", "MiniMax-M3", "workbuddy/deepseek-v4.1-flash"])
def test_literal_yaml_provider_values_do_not_require_environment_variables(tmp_path, monkeypatch, model):
    import yaml
    from cairn.dispatcher.config import DispatchConfig
    path = Path(__file__).resolve().parents[2] / "dispatch.example.yaml"
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    for entry in payload["workers"]:
        for name in entry.pop("env_from").values():
            monkeypatch.delenv(name, raising=False)
        entry["env"] = {"ANTHROPIC_BASE_URL": "http://127.0.0.1:3425", "ANTHROPIC_AUTH_TOKEN": "local-test-only",
                        "ANTHROPIC_MODEL": model}
    direct = tmp_path / "direct.yaml"
    direct.write_text(yaml.safe_dump(payload), encoding="utf-8")
    config = DispatchConfig.load(direct)
    assert all(w.env["ANTHROPIC_MODEL"] == model for w in config.workers)
    assert all(w.env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:3425" for w in config.workers)


def test_literal_env_from_has_an_actionable_error_without_echoing_its_value(monkeypatch):
    monkeypatch.delenv("http://127.0.0.1:3425", raising=False)
    with pytest.raises(ValidationError) as error:
        worker(env_from={"ANTHROPIC_BASE_URL": "http://127.0.0.1:3425"})
    message = error.value.errors(include_input=False)[0]["msg"]
    assert "env.ANTHROPIC_BASE_URL" in message and "not env_from" in message
    assert "http://127.0.0.1:3425" not in message
