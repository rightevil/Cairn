"""Opt-in integration: actual local Claude CLI, deterministic localhost model stub."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import DesignConfig, WorkerConfig
from cairn.dispatcher.design_session import run_turn
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from test_development_runtime import repository

pytestmark = pytest.mark.skipif(os.environ.get('CAIRN_NATIVE_CLAUDE_TEST') != '1', reason='Opt-in native Claude integration')


def test_actual_claude_invokes_skills_and_persists_question(repository, tmp_path):
    calls, events = [], []
    responses = [('Skill', {'skill': 'cairn-design'}), ('Skill', {'skill': 'bmad-product-brief'}),
                 ('Skill', {'skill': 'grilling'}), ('mcp__cairn__ask_questions', {'questions': [
                     {'id': 'Q1', 'text': 'Who is the first user?', 'recommendation': 'Internal team', 'options': ['Internal team', 'External users']}]} )]

    class Model(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                data = b'{"input_tokens":100}'
                self.send_response(200); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)
                return
            index = len(calls)
            calls.append([tool['name'] for tool in body.get('tools', [])])
            tool = responses[index] if index < len(responses) else None
            message = {'id': f'msg_{index}', 'type': 'message', 'role': 'assistant', 'model': body.get('model'), 'content': [],
                       'stop_reason': None, 'stop_sequence': None, 'usage': {'input_tokens': 100, 'output_tokens': 0}}
            block = {'type': 'tool_use', 'id': f'tool_{index}', 'name': tool[0], 'input': {}} if tool else {'type': 'text', 'text': ''}
            frames = [('message_start', {'type': 'message_start', 'message': message}),
                      ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': block}),
                      ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta':
                          {'type': 'input_json_delta', 'partial_json': json.dumps(tool[1])} if tool else {'type': 'text_delta', 'text': 'Please answer the saved question.'}}),
                      ('content_block_stop', {'type': 'content_block_stop', 'index': 0}),
                      ('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use' if tool else 'end_turn', 'stop_sequence': None}, 'usage': {'output_tokens': 20}}),
                      ('message_stop', {'type': 'message_stop'})]
            data = ''.join('event: ' + kind + '\ndata: ' + json.dumps(payload) + '\n\n' for kind, payload in frames).encode()
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(data))); self.end_headers(); self.wfile.write(data)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Model)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    worker = WorkerConfig(name='native-test', type='claudecode', tier='strong', roles=['planner'], max_running=1, priority=0,
        env={'ANTHROPIC_BASE_URL': f'http://127.0.0.1:{server.server_port}', 'ANTHROPIC_AUTH_TOKEN': 'local-test-only',
             'ANTHROPIC_API_KEY': '', 'ANTHROPIC_MODEL': 'claude-sonnet-4-6', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC': '1',
             'HTTP_PROXY': '', 'HTTPS_PROXY': '', 'ALL_PROXY': '', 'NO_PROXY': '127.0.0.1,localhost'})
    saved_session = [None]
    current_input = [1]
    def call(pid, action, payload=None):
        if action.startswith('design?'):
            return {'draft': {'session': saved_session[0], 'baseline': '', 'version': 0}, 'messages': [
                {'id': current_input[0], 'kind': 'user', 'data': {'content': 'Help design a feature' if current_input[0] == 1 else 'The first user is the internal team', 'answers': {}}}]}
        events.append((action, payload))
        return {'version': 1}
    dispatcher = SimpleNamespace(directory=tmp_path, config=SimpleNamespace(design=DesignConfig(
        bmad_repository=os.environ.get('CAIRN_BMAD_REPOSITORY', str(Path(__file__).resolve().parents[3] / 'BMAD-METHOD')),
        grilling_repository=os.environ.get('CAIRN_GRILLING_REPOSITORY', str(Path(__file__).resolve().parents[3] / 'skills')))), call=call)
    directory = tmp_path / 'runs' / 'native-run'
    directory.mkdir(parents=True)
    try:
        output, session = asyncio.run(asyncio.wait_for(run_turn(dispatcher,
            SimpleNamespace(project=SimpleNamespace(id='native-test'), hints=[]),
            SimpleNamespace(work=SimpleNamespace(result={'message_id': 1})), worker,
            {'id': 'native-run', 'token': 'local-lease'}, directory, repository, TaskCancellation()), 60))
        saved_session[0], current_input[0] = session, 2
        resumed_directory = tmp_path / 'runs' / 'resumed-run'
        resumed_directory.mkdir()
        resumed, resumed_session = asyncio.run(asyncio.wait_for(run_turn(dispatcher,
            SimpleNamespace(project=SimpleNamespace(id='native-test'), hints=[]),
            SimpleNamespace(work=SimpleNamespace(result={'message_id': 2})), worker,
            {'id': 'resumed-run', 'token': 'next-lease'}, resumed_directory, repository, TaskCancellation()), 60))
        assert resumed['kind'] == 'design' and resumed_session == session
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert output['kind'] == 'design' and session
    assert any(payload.get('pid') for action, payload in events if action.endswith('/heartbeat'))
    activities = [p['data'].get('skill') for _, p in events if p.get('kind') == 'activity']
    assert {'cairn-design', 'bmad-product-brief', 'grilling'} <= set(activities)
    assert any(p.get('kind') == 'questions' and p['data']['questions'][0]['id'] == 'Q1' for _, p in events)
    assert calls and any('mcp__cairn__ask_questions' in tools for tools in calls)
