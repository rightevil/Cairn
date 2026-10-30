from pathlib import Path
import json
import os
import socket
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from cairn.desktop import DesktopRuntime, DesktopWindow, SingleInstance
from cairn.runtime_entry import module_command


def test_frozen_runtime_entry(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(tmp_path / 'Cairn.exe'))
    command = module_command('cairn.dispatcher.terminal_bridge', 'hook')
    assert command == [str(tmp_path / 'cairn-runtime.exe'), '-m', 'cairn.dispatcher.terminal_bridge', 'hook']
    with pytest.raises(ValueError):
        module_command('untrusted.module')


def test_close_to_tray_and_explicit_exit():
    calls = []
    finished = threading.Event()
    window = SimpleNamespace(hide=lambda: calls.append('hide'), show=lambda: calls.append('show'),
                             restore=lambda: calls.append('restore'), destroy=finished.set)
    runtime = SimpleNamespace(shutdown=lambda: calls.append('shutdown'))
    tray = SimpleNamespace(stop=lambda: calls.append('stop'))
    controller = DesktopWindow(window, tray, runtime)
    assert controller.closing() is False
    assert calls == ['hide']
    controller.restore()
    assert calls == ['hide', 'show', 'restore']
    controller.exit()
    assert finished.wait(3)
    assert calls[-2:] == ['shutdown', 'stop']
    assert controller.closing() is True
    controller.exit()
    assert calls.count('shutdown') == 1


@pytest.mark.skipif(os.name != 'nt', reason='Windows tray instance event')
def test_second_instance_signals_restore_and_releases_lock(tmp_path):
    owner = SingleInstance(tmp_path)
    other = SingleInstance(tmp_path)
    stop = threading.Event()
    restored = threading.Event()
    assert owner.acquire()
    thread = threading.Thread(target=owner.watch, args=(stop, restored.set))
    thread.start()
    try:
        assert other.acquire() is False
        assert restored.wait(3)
    finally:
        stop.set(); thread.join(); owner.close(); other.close()
    assert other.acquire()
    other.close()


def test_exit_failure_keeps_tray_available():
    failed = threading.Event()
    def shutdown():
        raise OSError('test shutdown failure')
    tray = SimpleNamespace(notify=lambda *args: failed.set())
    controller = DesktopWindow(None, tray, SimpleNamespace(shutdown=shutdown))
    controller.exit()
    assert failed.wait(3)
    assert not controller.exiting.is_set()


def config(tmp_path, server):
    path = tmp_path / 'dispatch.yaml'
    path.write_text(f'''server: {server}
worker_token_file: data/cairn.worker-token
runtime:
  execution: local
  interval: 1
  prompt_group: mock
  max_workers: 1
  max_project_workers: 1
  max_running_projects: 1
  healthcheck_timeout: 5
tasks:
  bootstrap: {{timeout: 10, conclude_timeout: 5}}
  reason: {{timeout: 10}}
  explore: {{timeout: 10, conclude_timeout: 5}}
workers:
  - name: mock
    type: mock
    task_types: [bootstrap, reason, explore]
    max_running: 1
    priority: 0
''', encoding='utf-8')
    return path


def test_desktop_rejects_remote_endpoint(tmp_path):
    with pytest.raises(ValueError, match='本机'):
        DesktopRuntime(config(tmp_path, 'http://example.com:8000'))


def test_real_server_and_dispatcher_start_and_graceful_shutdown(tmp_path):
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    path = config(tmp_path, f'http://127.0.0.1:{port}')
    script = '''
from pathlib import Path
import sys, requests
from cairn.desktop import DesktopRuntime
r = DesktopRuntime(Path(sys.argv[1]))
try:
    r.start()
    assert requests.get(r.url + '/openapi.json', timeout=5).status_code == 200
    assert r.dispatch_thread.is_alive()
finally:
    r.shutdown()
assert not r.server_thread.is_alive()
assert not r.dispatch_thread.is_alive()
print('stopped')
'''
    result = subprocess.run([sys.executable, '-c', script, str(path)], capture_output=True, text=True, timeout=30,
                            env={**os.environ, 'NO_PROXY': 'localhost,127.0.0.1'})
    assert result.returncode == 0, result.stderr
    assert 'stopped' in result.stdout
    assert (tmp_path / 'data/cairn.db').exists()


def test_dispatcher_stop_event_cancels_owned_tasks_without_dispatching():
    from cairn.dispatcher.scheduler.loop import DispatcherLoop
    stop = threading.Event(); stop.set()
    calls = []
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.futures = {'task': SimpleNamespace(cancellation=SimpleNamespace(cancel=calls.append))}
    loop.run_startup_healthchecks = lambda: None
    loop.close = lambda: calls.append('closed')
    loop.run(stop_event=stop)
    assert calls == ['Dispatcher shutting down', 'closed']
