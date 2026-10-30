"""Provider-oriented desktop settings with validated, transactional live application."""
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile

import yaml

CONNECTIONS = {
    'claudecode': ('ANTHROPIC_BASE_URL', 'ANTHROPIC_AUTH_TOKEN', 'ANTHROPIC_MODEL'),
    'codex': ('CODEX_BASE_URL', 'OPENAI_API_KEY', 'CODEX_MODEL'),
    'pi': ('PI_BASE_URL', 'PI_API_KEY', 'PI_MODEL'),
}


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


class DesktopSettings:
    def __init__(self, home, config_path=None):
        self.home = Path(home)
        self.local_path = self.home / 'desktop.local.json'
        self.local = json.loads(self.local_path.read_text(encoding='utf-8-sig')) if self.local_path.exists() else {}
        path = Path(config_path or self.local.get('config', 'dispatch.yaml'))
        self.config_path = (path if path.is_absolute() else self.home / path).resolve()
        self.apply_config = None

    def raw(self):
        path = self.config_path
        if not path.exists():
            path = self.home / 'dispatch.example.yaml'
            if not path.exists():
                path = Path(__file__).resolve().parents[3] / 'dispatch.example.yaml'
        data = yaml.safe_load(path.read_text(encoding='utf-8-sig'))
        if not isinstance(data, dict):
            raise ValueError('配置文件必须是 YAML 对象。')
        return data

    def revision(self):
        data = self.config_path.read_bytes() if self.config_path.exists() else b''
        local = self.local_path.read_bytes() if self.local_path.exists() else b''
        return hashlib.sha256(data + b'\0' + local).hexdigest()

    def environment(self, data):
        values = dict(self.local.get('environment', {}))
        for worker in data.get('workers', []):
            for name in worker.get('env_from', {}).values():
                values.setdefault(name, os.environ.get(name, ''))
        return values

    def normalized(self):
        data = copy.deepcopy(self.raw())
        providers = data.setdefault('providers', [])
        for provider in providers:
            provider.setdefault('models', [])
        environment = self.environment(data)
        for worker in data.get('workers', []):
            if worker.get('provider') or worker.get('type') not in CONNECTIONS:
                continue
            base, key, model = CONNECTIONS[worker['type']]
            env = {**data.get('common_env', {}), **worker.get('env', {})}
            env.update({k: environment.get(v, '') for k, v in worker.get('env_from', {}).items()})
            if not worker.get('model') and env.get(model):
                worker['model'] = env[model]
            if not env.get(base):
                continue
            protocol = 'openai' if worker['type'] == 'codex' else 'anthropic' if worker['type'] == 'claudecode' else ('anthropic' if 'anthropic' in env.get('PI_PROVIDER_API', '') else 'openai')
            provider = next((p for p in providers if p['base_url'].rstrip('/') == env[base].rstrip('/')
                             and p.get('api_key', '') == env.get(key, '') and p['protocol'] == protocol), None)
            if not provider:
                number = len(providers) + 1
                provider = {'id': 'provider-' + str(number), 'name': '供应商 ' + str(number), 'protocol': protocol,
                            'base_url': env[base], 'api_key': env.get(key, ''), 'models': []}
                providers.append(provider)
            worker['provider'] = provider['id']
            if worker.get('model') and worker['model'] not in provider['models']:
                provider['models'].append(worker['model'])
        return data

    def get_settings(self):
        try:
            if self.local_path.exists():
                self.local = json.loads(self.local_path.read_text(encoding='utf-8-sig'))
            data = self.normalized()
            workers = []
            for worker in data.get('workers', []):
                row = {k: copy.deepcopy(worker.get(k, default)) for k, default in {
                    'name': '', 'type': 'claudecode', 'tier': 'standard', 'roles': [], 'command': [],
                    'model': '', 'max_running': 1, 'priority': 0, 'provider': ''}.items()}
                row['command'] = '\n'.join(row['command'] or [])
                row['original_name'] = worker['name']
                row['provider'] = row['provider'] or ''
                workers.append(row)
            providers = [{**{k: v for k, v in p.items() if k not in ('api_key', 'models')}, 'api_key': '',
                          'has_key': bool(p.get('api_key')), 'original_id': p['id'],
                          'models_text': '\n'.join(p.get('models', []))} for p in data['providers']]
            from cairn.dispatcher.design_skills import bundled_versions
            return {'ok': True, 'path': str(self.config_path), 'revision': self.revision(), 'providers': providers,
                    'runtime': data.get('runtime', {}), 'tasks': data.get('tasks', {}), 'workers': workers,
                    'skills': bundled_versions(), 'warning': getattr(self, 'startup_error', '')}
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
            return {'ok': False, 'error': '无法读取运行配置，请检查本机配置文件。'}

    def save_settings(self, value):
        try:
            if value.get('revision') != self.revision():
                raise ValueError('配置已在外部更新，请重新载入后再保存。')
            data = self.normalized()
            original_providers = {p['id']: p for p in data['providers']}
            providers = []
            for row in value['providers']:
                if not row['name'].strip():
                    raise ValueError('供应商名称不能为空。')
                old = original_providers.get(row.get('original_id', row['id']), {})
                key = row.get('api_key', '') or ('' if row.get('clear_key') else old.get('api_key', ''))
                providers.append({'id': row['id'], 'name': row['name'].strip(), 'protocol': row['protocol'],
                    'base_url': row['base_url'].strip(), 'api_key': key,
                    'models': list(dict.fromkeys(s.strip() for s in row.get('models_text', '').splitlines() if s.strip()))})
            data['providers'] = providers
            runtime = data.setdefault('runtime', {})
            for key in ('max_workers', 'max_project_workers', 'max_running_projects', 'max_implementers', 'max_reviewers', 'interval', 'healthcheck_timeout'):
                number = value['runtime'].get(key)
                runtime[key] = int(number) if number not in ('', None) else None
            runtime['execution'] = 'local'
            data.setdefault('tasks', {})['development_timeout'] = int(value['tasks'].get('development_timeout', 3600))
            data['design'] = None
            original_workers = {w['name']: w for w in data.get('workers', [])}
            workers = []
            for row in value['workers']:
                if not row['name'].strip():
                    raise ValueError('Agent 名称不能为空。')
                worker = copy.deepcopy(original_workers.get(row.get('original_name', row['name']), {}))
                for key in ('name', 'type', 'tier', 'roles'):
                    worker[key] = row[key]
                worker['provider'] = row.get('provider') or None
                for key in ('max_running', 'priority'):
                    worker[key] = int(row[key])
                worker['command'] = [a.strip() for a in row['command'].splitlines() if a.strip()] or None
                worker['model'] = row.get('model') or None
                # Provider settings replace legacy connection mappings; retain unrelated CLI options.
                for field in ('env', 'env_from'):
                    worker[field] = {k: v for k, v in worker.get(field, {}).items()
                                     if k not in {key for keys in CONNECTIONS.values() for key in keys} and k != 'PI_PROVIDER_API'}
                workers.append(worker)
            data['workers'] = workers
            if 'common_env' in data:
                data['common_env'] = {k: v for k, v in data['common_env'].items()
                                      if k not in {key for keys in CONNECTIONS.values() for key in keys} and k != 'PI_PROVIDER_API'}
            environment = self.environment(data)
            from cairn.dispatcher.config import DispatchConfig
            from pydantic import ValidationError
            try:
                DispatchConfig.model_validate(copy.deepcopy(data), context={'environment': {**os.environ, **environment}})
            except ValidationError as exc:
                raise ValueError('配置未通过校验：' + '；'.join('.'.join(map(str, e['loc'])) + ' ' + e['msg']
                                                          for e in exc.errors(include_input=False, include_context=False))) from None
            local = {**self.local, 'config': str(self.config_path), 'environment': environment}
            previous = self.config_path.read_text(encoding='utf-8-sig') if self.config_path.exists() else None
            previous_local = self.local_path.read_text(encoding='utf-8-sig') if self.local_path.exists() else None
            if previous is not None:
                write_atomic(self.home / '.cairn/settings-backups' / (self.revision() + '.yaml'), previous)
            try:
                write_atomic(self.config_path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
                write_atomic(self.local_path, json.dumps(local, ensure_ascii=False, indent=2))
                if self.apply_config:
                    self.apply_config(self.config_path, environment)
            except Exception:
                for path, old in ((self.config_path, previous), (self.local_path, previous_local)):
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        write_atomic(path, old)
                raise
            self.local = local
            self.startup_error = ''
            return {'ok': True, 'revision': self.revision(), 'message': '已保存并生效。正在运行的任务保持原配置，新任务使用新配置。'}
        except (OSError, ValueError, KeyError, TypeError, RuntimeError, yaml.YAMLError) as error:
            message = str(error) if isinstance(error, ValueError) and not isinstance(error, yaml.YAMLError) else '保存或应用失败，已恢复原配置。'
            return {'ok': False, 'error': message}
