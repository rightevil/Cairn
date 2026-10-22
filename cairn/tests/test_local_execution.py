from __future__ import annotations

import os
import sys
import json
import subprocess
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from cairn.dispatcher.config import DispatchConfig, LocalConfig, WorkerConfig
from cairn.dispatcher.runtime.local_backend import LocalBackend
from cairn.dispatcher.runtime.local_process import LocalProcess
from cairn.dispatcher.scheduler import loop as loop_module
from cairn.dispatcher.tasks import common, explore
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.workers.adapters.codex import CodexDriver
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.dispatcher.workers.registry import get_driver

from conftest import FakeClient, make_config, make_intent, make_project


REPO_ROOT = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- LocalProcess


def test_local_process_captures_stdout_and_exit_code() -> None:
    process = LocalProcess(
        [sys.executable, "-c", "import sys; print('hello'); sys.exit(3)"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert result.stdout.strip() == "hello"
    assert result.returncode == 3
    assert not result.timed_out


def test_local_process_inherits_cwd(tmp_path: Path) -> None:
    process = LocalProcess(
        [sys.executable, "-c", "import os; print(os.getcwd())"],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert Path(result.stdout.strip()).resolve() == tmp_path.resolve()


def test_local_process_times_out_and_kills_within_grace() -> None:
    process = LocalProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=1,
        term_grace_seconds=2,
    )
    process.start()
    started = time.monotonic()
    result = process.communicate(timeout=30)
    elapsed = time.monotonic() - started

    assert result.timed_out
    assert elapsed < 10  # killed on its own timeout, not the 30s outer backstop


def test_local_process_kill_terminates_child_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "child.pid"
    script = ("import subprocess,sys,time; from pathlib import Path; "
              "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)']); "
              f"Path({str(pid_file)!r}).write_text(str(child.pid)); time.sleep(30)")
    process = LocalProcess(
        [sys.executable, "-c", script],
        cwd=str(tmp_path),
        env=dict(os.environ),
        timeout_seconds=1,
        term_grace_seconds=2,
    )
    process.start()
    result = process.communicate(timeout=30)

    assert result.timed_out
    child_pid = int(pid_file.read_text().strip())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            _assert_process_exists(child_pid)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError(f"child process {child_pid} survived the group kill")


def test_local_process_cancel_records_reason() -> None:
    process = LocalProcess(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=os.getcwd(),
        env=dict(os.environ),
        timeout_seconds=30,
        term_grace_seconds=2,
    )
    process.start()
    process.cancel("project stopped")
    result = process.communicate(timeout=30)

    assert result.cancelled
    assert result.cancel_reason == "project stopped"


# --------------------------------------------------------------------------- LocalBackend


def test_local_backend_creates_isolated_project_dir(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    handle = backend.ensure_running("proj_001")

    assert Path(handle) == tmp_path / "proj_001"
    assert Path(handle).is_dir()
    assert backend.container_name("proj_001") == str(tmp_path / "proj_001")


def test_local_backend_merges_host_env_with_worker_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CAIRN_HOST_VAR", "host")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    handle = backend.ensure_running("proj_001")

    process = backend.build_exec_process(
        handle,
        {"CAIRN_WORKER_VAR": "worker"},
        [sys.executable, "-c", "import os; print(os.environ['CAIRN_HOST_VAR']+'-'+os.environ['CAIRN_WORKER_VAR'],end='')"],
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    assert result.stdout == "host-worker"


def test_local_common_env_reaches_worker_subprocess(tmp_path: Path, monkeypatch) -> None:
    # common_env (e.g. an outbound proxy) merges into every worker's env and must survive
    # all the way to the host subprocess in local mode.
    payload = _local_payload()
    payload["local"] = {"workspace_root": str(tmp_path)}
    payload["common_env"] = {
        "https_proxy": "http://127.0.0.1:7897",
        "http_proxy": "http://127.0.0.1:7897",
        "all_proxy": "http://127.0.0.1:7897",
    }
    config = DispatchConfig.model_validate(payload)
    worker = next(w for w in config.workers if w.type == "claudecode")
    assert worker.env["https_proxy"] == "http://127.0.0.1:7897"

    assert config.local is not None
    backend = LocalBackend(config.local)
    handle = backend.ensure_running("proj_001")
    process = backend.build_exec_process(
        handle,
        dict(worker.env),
        [sys.executable, "-c", "import os; print('|'.join(os.environ[k] for k in ['https_proxy','http_proxy','all_proxy']),end='')"],
        timeout_seconds=10,
    )
    process.start()
    result = process.communicate(timeout=20)

    proxy = "http://127.0.0.1:7897"
    assert result.stdout == f"{proxy}|{proxy}|{proxy}"


def test_local_backend_write_text_file_writes_to_host(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    target = tmp_path / "snapshots" / "graph.yaml"

    backend.write_text_file("ignored", str(target), "facts: []\n")

    assert target.read_text() == "facts: []\n"


def test_local_backend_keep_leaves_dir_and_reports_no_cleanup(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path), completed_action="keep"))
    handle = backend.ensure_running("proj_001")

    assert backend.needs_completed_cleanup("proj_001") is False
    assert backend.cleanup_completed("proj_001") is True
    assert Path(handle).is_dir()


def test_local_backend_remove_deletes_dir_on_completion(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path), completed_action="remove"))
    handle = backend.ensure_running("proj_001")

    assert backend.needs_completed_cleanup("proj_001") is True
    assert backend.cleanup_completed("proj_001") is True
    assert not Path(handle).exists()


def test_local_backend_stopped_cleanup_is_noop(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path)))
    backend.ensure_running("proj_001")

    assert backend.needs_stopped_cleanup("proj_001") is False
    assert backend.cleanup_stopped("proj_001") is True


# --------------------------------------------------------------------------- config


def _local_payload() -> dict:
    return {
        "server": "http://127.0.0.1:8000",
        "runtime": {
            "execution": "local",
            "worker_healthcheck": "disabled",
            "interval": 3,
            "max_workers": 2,
            "max_running_projects": 1,
            "max_project_workers": 2,
            "healthcheck_timeout": 5,
            "prompt_group": "default",
        },
        "tasks": {
            "bootstrap": {"timeout": 10, "conclude_timeout": 5},
            "reason": {"timeout": 10, "max_intents": 3},
            "explore": {"timeout": 10, "conclude_timeout": 5},
        },
        "workers": [
            {"name": "local-claude", "type": "claudecode", "task_types": ["explore"], "max_running": 1, "priority": 0},
            {"name": "local-codex", "type": "codex", "task_types": ["explore"], "max_running": 1, "priority": 1},
            {"name": "local-pi", "type": "pi", "task_types": ["reason"], "max_running": 1, "priority": 2},
        ],
    }


def test_local_execution_needs_no_container_or_worker_env() -> None:
    config = DispatchConfig.model_validate(_local_payload())

    assert config.container is None
    assert config.local is not None
    assert config.local.completed_action == "keep"
    assert all(worker.env == {} for worker in config.workers)


def test_local_workspace_root_is_optional_and_defaults_null() -> None:
    payload = _local_payload()
    payload["local"] = {"completed_action": "remove"}
    config = DispatchConfig.model_validate(payload)

    assert config.local is not None
    assert config.local.workspace_root is None
    assert config.local.completed_action == "remove"


def test_container_execution_requires_container_block() -> None:
    payload = make_config().model_dump()
    payload["container"] = None

    with pytest.raises(ValidationError, match="container config is required"):
        DispatchConfig.model_validate(payload)


def test_container_execution_still_requires_worker_env() -> None:
    payload = _local_payload()
    payload["runtime"]["execution"] = "container"
    payload["runtime"]["worker_healthcheck"] = "startup_only"
    payload["container"] = {"image": "img", "network_mode": "host", "completed_action": "stop"}

    with pytest.raises(ValidationError, match="missing env keys"):
        DispatchConfig.model_validate(payload)


def test_shipped_local_example_config_is_valid(monkeypatch) -> None:
    for key, value in {"MAGPIE_ANTHROPIC_BASE_URL": "http://127.0.0.1:9999", "MAGPIE_API_KEY": "test-only",
                       "MAGPIE_STRONG_MODEL": "strong-model", "MAGPIE_STANDARD_MODEL": "standard-model"}.items():
        monkeypatch.setenv(key, value)
    config = DispatchConfig.load(REPO_ROOT / "dispatch.example.yaml")

    assert config.runtime.execution == "local"
    assert config.container is None
    assert config.local is not None
    assert config.local.completed_action == "keep"


# --------------------------------------------------------------------------- startup CLI check


def _bare_loop(config: DispatchConfig) -> loop_module.DispatcherLoop:
    loop = loop_module.DispatcherLoop.__new__(loop_module.DispatcherLoop)
    loop.config = config
    return loop


def test_local_cli_check_passes_when_cli_present() -> None:
    payload = _local_payload()
    payload["workers"] = [{"name": "m", "type": "mock", "task_types": ["reason"], "max_running": 1, "priority": 0}]
    config = DispatchConfig.model_validate(payload)

    _bare_loop(config)._run_local_binary_check()  # mock -> python3 --help runs; must not raise


def test_local_cli_check_exits_when_no_cli_installed(monkeypatch) -> None:
    monkeypatch.setattr(loop_module.shutil, "which", lambda _binary: None)
    config = DispatchConfig.model_validate(_local_payload())

    with pytest.raises(RuntimeError, match="none of the configured worker CLIs"):
        _bare_loop(config)._run_local_binary_check()


# --------------------------------------------------------------------------- drivers


def _bare_worker(worker_type: str) -> WorkerConfig:
    return WorkerConfig.model_validate(
        {"name": worker_type, "type": worker_type, "task_types": ["explore"], "max_running": 1, "priority": 0}
    )


def test_codex_local_driver_omits_provider_injection() -> None:
    worker = _bare_worker("codex")
    argv = CodexDriver(local=True).build_execute(worker, "PROMPT", None).argv

    assert argv == ["codex", "exec", "--dangerously-bypass-approvals-and-sandbox", "--", "PROMPT"]
    assert not any("model_providers" in part for part in argv)
    assert "--model" not in argv

    conclude = CodexDriver(local=True).build_conclude(worker, "PROMPT", "sess-1")
    assert conclude[:4] == ["codex", "exec", "resume", "sess-1"]
    assert conclude[-2:] == ["--", "PROMPT"]
    assert not any("model_providers" in part for part in conclude)


def test_pi_local_driver_omits_models_json_and_provider() -> None:
    worker = _bare_worker("pi")
    argv = PiDriver(local=True).build_execute(worker, "PROMPT", None).argv

    assert argv[0] == "pi"
    assert "/bin/sh" not in argv
    assert "--provider" not in argv
    assert "--model" not in argv
    assert argv[-2:] == ["-p", "PROMPT"]


def test_get_driver_selects_local_or_container_variant() -> None:
    assert get_driver("codex", "local").local is True
    assert get_driver("codex").local is False
    assert get_driver("pi", "local").local is True
    # claudecode and mock are shared instances across both modes
    assert get_driver("claudecode", "local") is get_driver("claudecode")
    assert get_driver("mock", "local") is get_driver("mock")


# --------------------------------------------------------------------------- end to end


def _local_config_for_worker(name: str, worker_type: str) -> DispatchConfig:
    return DispatchConfig.model_validate(
        {
            "server": "in-process",
            "runtime": {
                "execution": "local",
                "worker_healthcheck": "disabled",
                "interval": 60,
                "max_workers": 1,
                "max_running_projects": 1,
                "max_project_workers": 1,
                "healthcheck_timeout": 5,
                "prompt_group": "default",
            },
            "tasks": {
                "bootstrap": {"timeout": 30, "conclude_timeout": 10},
                "reason": {"timeout": 30, "max_intents": 3},
                "explore": {"timeout": 30, "conclude_timeout": 10},
            },
            "workers": [
                {"name": name, "type": worker_type, "task_types": ["explore"], "max_running": 1, "priority": 0}
            ],
        }
    )


def _install_fake_cli(tmp_path: Path, monkeypatch, name: str, body: str) -> None:
    # Only replace the executable lookup; driver argv and the real subprocess path remain exercised.
    import shlex
    output = shlex.split(body)[1]
    script = tmp_path / (name + ".py")
    script.write_text("print(" + repr(output) + ")", encoding="utf-8")
    original = subprocess.Popen

    def launch(command, *args, **kwargs):
        if command[0] == name:
            command = [sys.executable, str(script), *command[1:]]
        return original(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", launch)


def test_explore_runs_real_local_cli_end_to_end(tmp_path: Path, monkeypatch) -> None:
    # A fake `claude` on PATH stands in for the real CLI: the whole local path is exercised
    # for real — driver argv -> LocalBackend -> LocalProcess subprocess -> stdout parsing.
    _install_fake_cli(
        tmp_path,
        monkeypatch,
        "claude",
        "echo '{\"accepted\":true,\"data\":{\"description\":\"local fake fact\"}}'",
    )

    config = _local_config_for_worker("test-worker", "claudecode")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)

    outcome = explore.run_explore_task(
        config,
        client,
        backend,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "success"
    assert client.concluded == [("proj_001", "i001", "test-worker", "local fake fact")]
    # graph snapshot was materialised on the host under the patched root
    snapshot_root = Path(backend.snapshot_root)
    assert any(p.name == "graph.yaml" for p in snapshot_root.rglob("*"))


def test_explore_local_cli_rejection_releases_intent(tmp_path: Path, monkeypatch) -> None:
    _install_fake_cli(
        tmp_path,
        monkeypatch,
        "claude",
        "echo '{\"accepted\":false,\"reason\":\"policy_refusal\"}'",
    )

    config = _local_config_for_worker("test-worker", "claudecode")
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "work")))
    intent = make_intent()
    project = make_project(intents=[intent])
    client = FakeClient(project)

    outcome = explore.run_explore_task(
        config,
        client,
        backend,
        project,
        "facts:\n- id: f001\n",
        intent,
        config.workers[0],
        TaskCancellation(),
    )

    assert outcome == "rejected"
    assert client.concluded == []
    assert client.released == [("proj_001", "i001", "test-worker")]


def _assert_process_exists(pid: int) -> None:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.OpenProcess.restype = wintypes.HANDLE
        api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = api.OpenProcess(0x1000, False, pid)
        if not handle:
            raise ProcessLookupError(pid)
        try:
            code = wintypes.DWORD()
            assert api.GetExitCodeProcess(handle, ctypes.byref(code))
            if code.value != 259:
                raise ProcessLookupError(pid)
        finally:
            api.CloseHandle(handle)
    else:
        os.kill(pid, 0)
        status = Path(f"/proc/{pid}/stat")
        if status.exists() and status.read_text().split(") ", 1)[1].startswith("Z"):
            raise ProcessLookupError(pid)


def test_local_snapshot_default_supports_unicode_and_spaces(tmp_path: Path) -> None:
    backend = LocalBackend(LocalConfig(workspace_root=str(tmp_path / "开发 workspace")))
    workdir = backend.ensure_running("proj_001")
    reference = common.write_graph_snapshot_reference(backend, workdir, "facts: []\n", phase="reason")
    files = list(Path(backend.snapshot_root).rglob("graph.yaml"))
    assert len(files) == 1
    assert files[0].is_absolute()
    assert files[0].read_text(encoding="utf-8") == "facts: []\n"
    assert str(files[0]).replace("\\", "/") in reference.replace("\\", "/")


def test_long_output_is_persisted_while_memory_keeps_start_and_final_result(tmp_path: Path) -> None:
    from cairn.dispatcher.runtime.local_process import CAPTURE_LIMIT, READ_CHUNK_SIZE
    process = LocalProcess([sys.executable, "-c",
        "import sys; print('SESSION-HEADER',flush=True); sys.stdout.write('x'*6000000); print('\\nFINAL-RESULT',flush=True)"],
        str(tmp_path), dict(os.environ), timeout_seconds=20)
    process.stdout_path, process.stderr_path = tmp_path / "stdout.log", tmp_path / "stderr.log"
    process.start()
    result = process.communicate(timeout=20)
    assert result.returncode == 0
    assert process.stdout_path.stat().st_size > 6_000_000
    assert len(result.stdout) < CAPTURE_LIMIT + READ_CHUNK_SIZE + 100
    assert result.stdout.startswith("SESSION-HEADER")
    assert result.stdout.rstrip().endswith("FINAL-RESULT")


def test_small_live_output_is_flushed_before_process_exits(tmp_path: Path) -> None:
    process = LocalProcess([sys.executable, "-c", "import time; print('ready',flush=True); time.sleep(30)"],
        str(tmp_path), dict(os.environ), timeout_seconds=20)
    process.stdout_path = tmp_path / "stdout.log"
    process.start()
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if process.stdout_path.exists() and "ready" in process.stdout_path.read_text(encoding="utf-8"):
                break
            time.sleep(0.02)
        else:
            pytest.fail("Live output stayed buffered until process exit")
    finally:
        process.cancel("test finished")
        process.communicate(timeout=10)
