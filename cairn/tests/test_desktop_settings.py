import copy
import json
import os
from pathlib import Path

import yaml
import pytest

from cairn.desktop_settings import DesktopSettings
from test_desktop import config


def settings(tmp_path):
    path = config(tmp_path, 'http://127.0.0.1:8000')
    data = yaml.safe_load(path.read_text(encoding='utf-8'))
    data['workers'][0]['type'] = 'claudecode'
    data['workers'][0]['env'] = {'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9000',
        'ANTHROPIC_AUTH_TOKEN': 'worker-private', 'ANTHROPIC_MODEL': 'model-a', 'CUSTOM_TOKEN': 'preserve-private'}
    data['local'] = {'workspace_root': 'workspaces', 'completed_action': 'keep'}
    path.write_text(yaml.safe_dump(data), encoding='utf-8')
    (tmp_path / 'desktop.local.json').write_text(json.dumps({'config': str(path),
        'environment': {'TEST_API_KEY': 'private-key'}}), encoding='utf-8')
    return DesktopSettings(tmp_path)


def test_provider_migration_masks_secrets_and_preserves_other_fields(tmp_path):
    store = settings(tmp_path)
    value = store.get_settings()
    assert value['ok']
    for secret in ('worker-private', 'preserve-private', 'private-key'):
        assert secret not in json.dumps(value)
    assert value['providers'][0]['base_url'] == 'http://127.0.0.1:9000'
    assert value['workers'][0]['provider'] == value['providers'][0]['id']
    value['workers'][0]['name'] = 'renamed'
    value['runtime']['max_workers'] = 2
    original_env = dict(os.environ)
    result = store.save_settings(value)
    assert result['ok'], result
    assert dict(os.environ) == original_env
    saved = yaml.safe_load(store.config_path.read_text(encoding='utf-8'))
    assert saved['providers'][0]['api_key'] == 'worker-private'
    assert saved['workers'][0]['env'] == {'CUSTOM_TOKEN': 'preserve-private'}
    assert saved['workers'][0]['task_types'] == ['bootstrap', 'reason', 'explore']
    assert saved['local']['completed_action'] == 'keep'
    assert saved['runtime']['max_workers'] == 2
    assert saved['design'] is None
    assert list((tmp_path / '.cairn/settings-backups').glob('*.yaml'))


def test_update_and_clear_provider_key_without_returning_it(tmp_path):
    store = settings(tmp_path)
    value = store.get_settings()
    value['providers'][0]['api_key'] = 'replacement-private'
    assert store.save_settings(value)['ok']
    value = store.get_settings()
    assert 'replacement-private' not in json.dumps(value)
    value['providers'][0]['clear_key'] = True
    assert store.save_settings(value)['ok']
    assert yaml.safe_load(store.config_path.read_text(encoding='utf-8'))['providers'][0]['api_key'] == ''


def test_invalid_configuration_and_stale_edits_do_not_write(tmp_path):
    store = settings(tmp_path)
    before = store.config_path.read_bytes()
    value = store.get_settings()
    for url in ('file:///private', 'http://user:private@example.com', 'http://localhost:99999'):
        value['providers'][0]['base_url'] = url
        assert not store.save_settings(value)['ok']
        assert store.config_path.read_bytes() == before
    value = store.get_settings()
    value['runtime']['max_workers'] = 0
    assert not store.save_settings(value)['ok']
    assert store.config_path.read_bytes() == before
    value = store.get_settings()
    store.config_path.write_bytes(before + b'\n# external change\n')
    assert not store.save_settings(value)['ok']
    assert store.config_path.read_bytes().endswith(b'# external change\n')


def test_apply_failure_rolls_back_both_files(tmp_path):
    store = settings(tmp_path)
    value = store.get_settings()
    before = store.config_path.read_bytes(), store.local_path.read_bytes()
    def fail(*args):
        raise RuntimeError('test application failure')
    store.apply_config = fail
    value['providers'][0]['api_key'] = 'candidate-key'
    assert not store.save_settings(value)['ok']
    assert (store.config_path.read_bytes(), store.local_path.read_bytes()) == before


def test_first_run_uses_example_without_writing_until_save(tmp_path):
    template_dir = tmp_path / 'template'; template_dir.mkdir()
    source = config(template_dir, 'http://127.0.0.1:8000')
    (tmp_path / 'dispatch.example.yaml').write_bytes(source.read_bytes())
    store = DesktopSettings(tmp_path)
    value = store.get_settings()
    assert value['ok'] and not store.config_path.exists()
    applied = []
    store.apply_config = lambda *args: applied.append(args)
    assert store.save_settings(value)['ok']
    assert store.config_path.is_file() and len(applied) == 1


def test_common_legacy_provider_is_shared_by_models(tmp_path):
    store = settings(tmp_path)
    data = store.raw()
    other = copy.deepcopy(data['workers'][0]); other['name'] = 'other'
    other['env']['ANTHROPIC_MODEL'] = 'model-b'; data['workers'].append(other)
    store.config_path.write_text(yaml.safe_dump(data), encoding='utf-8')
    before = store.config_path.read_bytes()
    value = store.get_settings()
    assert len(value['providers']) == 1
    assert set(value['providers'][0]['models_text'].splitlines()) == {'model-a', 'model-b'}
    assert store.config_path.read_bytes() == before


def test_window_icon_contains_multiple_resolutions():
    Image = pytest.importorskip('PIL.Image')
    root = Path(__file__).parents[1] / 'src/cairn/server/static/assets'
    assert {(16,16),(32,32),(48,48),(256,256)} <= Image.open(root/'cairn.ico').ico.sizes()


def test_first_run_bridge_starts_and_reloads_without_restarting_app(tmp_path, monkeypatch):
    import sys, threading
    from types import SimpleNamespace
    import cairn.desktop as desktop
    pytest.importorskip('PIL.Image')
    template = tmp_path/'template'; template.mkdir()
    (tmp_path/'dispatch.example.yaml').write_bytes(config(template,'http://127.0.0.1:8000').read_bytes())
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'Cairn.exe'))
    monkeypatch.setattr(sys,'argv',['Cairn.exe'])
    monkeypatch.setenv('NO_PROXY','127.0.0.1')
    monkeypatch.setattr(desktop.logging,'basicConfig',lambda **kw:None)
    windows, calls = [], []
    class Event:
        def __iadd__(self, callback): return self
        def __isub__(self, callback): return self
    class Window:
        def __init__(self):
            self.api={};self.events=SimpleNamespace(closing=Event());self.closed=threading.Event();self.hidden=False
        def expose(self,*functions): self.api.update({f.__name__:f for f in functions})
        def hide(self): self.hidden=True
        def show(self): self.hidden=False
        def restore(self): pass
        def destroy(self): self.closed.set()
    def create(*a,**kw):
        result=Window();windows.append(result);return result
    class Instance:
        def __init__(self,path): path.mkdir(parents=True,exist_ok=True)
        def acquire(self):return True
        def watch(self,stop,restore):stop.wait()
        def close(self):pass
    class Runtime:
        def __init__(self,path,*a,**kw):
            if not path.exists():raise FileNotFoundError()
            self.url='http://127.0.0.1:8000';self.failure=None
        def start(self):calls.append('start')
        def shutdown(self):pass
        def reload(self,*a):calls.append('reload')
    tray=SimpleNamespace(run_detached=lambda:None,stop=lambda:None)
    def start(callback,**kw):
        callback()
        api=windows[0].api
        assert set(api)=={'get_settings','save_settings'}
        assert api['save_settings'](api['get_settings']())['ok']
        assert len(windows)==2 and windows[0].hidden
        assert set(windows[1].api)=={'open_settings'}
        assert api['save_settings'](api['get_settings']())['ok']
        assert len(windows)==2 and calls==['start','reload']
        tray.menu[1][1]()
        assert windows[1].closed.wait(3)
    monkeypatch.setitem(sys.modules,'webview',SimpleNamespace(create_window=create,start=start))
    monkeypatch.setitem(sys.modules,'pystray',SimpleNamespace(Icon=lambda *a:tray,Menu=lambda *a:a,MenuItem=lambda *a,**kw:a))
    monkeypatch.setattr(desktop,'SingleInstance',Instance)
    monkeypatch.setattr(desktop,'DesktopRuntime',Runtime)
    desktop.main()
