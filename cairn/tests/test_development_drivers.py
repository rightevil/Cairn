import json

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
