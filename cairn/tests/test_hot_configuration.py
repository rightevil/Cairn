import copy
import hashlib
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import DispatchConfig
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from test_desktop import config


def test_provider_routes_protocol_credentials_and_model(tmp_path):
    path = config(tmp_path, 'http://127.0.0.1:8000')
    import yaml
    data = yaml.safe_load(path.read_text())
    data['providers'] = [{'id':'shared','name':'Gateway','protocol':'anthropic','base_url':'http://localhost:9000',
                          'api_key':'test-private','models':['strong-model','small-model']}]
    worker = data['workers'][0]
    worker.update(type='claudecode', provider='shared', model='strong-model')
    result = DispatchConfig.model_validate(data)
    assert result.workers[0].env['ANTHROPIC_AUTH_TOKEN'] == 'test-private'
    assert result.workers[0].env['ANTHROPIC_BASE_URL'] == 'http://localhost:9000'
    assert result.workers[0].env['ANTHROPIC_MODEL'] == 'strong-model'
    data['workers'][0]['type'] = 'codex'
    with pytest.raises(ValueError):
        DispatchConfig.model_validate(data)
    data['providers'][0]['protocol'] = 'openai'
    result = DispatchConfig.model_validate(data)
    assert result.workers[0].env['OPENAI_API_KEY'] == 'test-private'
    assert result.workers[0].env['CODEX_MODEL'] == 'strong-model'


def test_hot_reload_keeps_running_task_snapshot_and_resizes_capacity(tmp_path):
    loop = DispatcherLoop(config(tmp_path, 'http://127.0.0.1:8000'))
    old = loop.config
    owned = set()
    loop.development = SimpleNamespace(config=old, owned=owned)
    bound = loop.development
    started, release = threading.Event(), threading.Event()
    def running():
        started.set(); release.wait(3)
        return bound.config.runtime.max_workers
    task = loop.executor.submit(running)
    assert started.wait(3)
    candidate = copy.deepcopy(old)
    candidate.runtime.max_workers = 2
    candidate.runtime.interval = 60
    try:
        loop.reload_config(candidate)
        assert loop.config is candidate and loop.development.config is candidate
        assert loop.development is not bound and loop.development.owned is owned
        assert loop.wake.is_set()
        assert loop.executor.submit(lambda: loop.development.config.runtime.max_workers).result(3) == 2
        release.set()
        assert task.result(3) == 1
        assert len(loop.retired_executors) == 1
    finally:
        release.set(); loop.close()


def test_bundled_skill_hashes_setup_and_po_rendering(tmp_path):
    from cairn.dispatcher.design_skills import prepare, run_script, BMAD_SKILLS
    bundle = Path(__file__).parents[1] / 'src/cairn/dispatcher/skills/designgod'
    manifest = json.loads((bundle/'references/sources.json').read_text(encoding='utf-8'))
    assert all(hashlib.sha256((bundle/name).read_bytes()).hexdigest() == digest for name,digest in manifest['sha256'].items())
    prepare(tmp_path, None)

    for name in BMAD_SKILLS:
        original = (bundle/'references/upstream/bmad'/name/'ENTRY.md').read_text(encoding='utf-8')
        native = (tmp_path/'.claude/skills'/name/'SKILL.md').read_text(encoding='utf-8')
        assert native.endswith(original[original.index('---',3)+3:])
    assert (tmp_path/'.claude/skills/grilling/SKILL.md').read_bytes() == (bundle/'references/upstream/grilling/ENTRY.md').read_bytes()
    rendered = run_script(tmp_path,'render_skill.py',['--project-root',str(tmp_path),'--skill',str(tmp_path/'.claude/skills/bmad-agent-po')])
    assert 'HALT:' not in rendered
    assert 'Cairn host mapping' in rendered and 'spec' in rendered and 'slice' in rendered
    prepare(tmp_path, None)


def test_live_apply_recovers_failed_dispatcher_without_restarting_api(tmp_path, monkeypatch):
    from cairn.desktop import DesktopRuntime
    import cairn.dispatcher.scheduler.loop as module
    path = config(tmp_path, 'http://127.0.0.1:8000')
    runtime = DesktopRuntime(path)
    runtime.failure = 'startup failed'
    runtime.dispatch_thread = SimpleNamespace(is_alive=lambda: False)
    started = threading.Event()
    class Replacement:
        def __init__(self, *args, **kwargs):
            self.wake = threading.Event()
        def run(self, stop_event):
            started.set(); stop_event.wait(3)
    monkeypatch.setattr(module, 'DispatcherLoop', Replacement)
    runtime.reload(path, {})
    assert started.wait(3)
    assert runtime.failure is None
    assert runtime.server is None, 'Recovery must not recreate or replace the API server'
    runtime.shutdown()
