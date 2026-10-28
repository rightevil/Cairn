"""Opt-in integration: actual local Claude CLI, deterministic localhost model stub."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from cairn.dispatcher.config import DesignConfig, WorkerConfig
from cairn.dispatcher.design_session import run_turn
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from test_development_runtime import repository

pytestmark = pytest.mark.skipif(os.environ.get('CAIRN_NATIVE_CLAUDE_TEST') != '1', reason='Opt-in native Claude integration')


def test_actual_claude_does_not_multiply_gateway_retries(repository):
    import subprocess
    from cairn.dispatcher.development import executable_argv, classify_failure
    from cairn.dispatcher.workers.adapters.claudecode import ClaudeCodeDriver

    calls = []
    class FailedGateway(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length', 0)))
            if 'count_tokens' in self.path:
                code, payload = 200, {'input_tokens': 10}
            else:
                calls.append(self.path)
                code, payload = 502, {'type':'error','error':{'type':'api_error','message':'getaddrinfo ENOTFOUND upstream.example.org'}}
            body = json.dumps(payload).encode()
            self.send_response(code); self.send_header('Content-Type','application/json')
            self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)

    server = ThreadingHTTPServer(('127.0.0.1', 0), FailedGateway)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    role = WorkerConfig(name='native-retry-test', type='claudecode', roles=['implementer'], max_running=1, priority=0,
        env={'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{server.server_port}', 'ANTHROPIC_AUTH_TOKEN':'local-test-only',
             'ANTHROPIC_API_KEY':'', 'ANTHROPIC_MODEL':'claude-sonnet-4-6', 'CLAUDE_CODE_MAX_RETRIES':'0',
             'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'',
             'NO_PROXY':'127.0.0.1,localhost'})
    driver = ClaudeCodeDriver()
    try:
        argv = executable_argv(role, driver.build_execute(role, 'Return a result.', driver.prepare_session()).argv)
        process = subprocess.run(argv, cwd=repository, env={**os.environ, **role.env}, capture_output=True,
                                 text=True, encoding='utf-8', errors='replace', timeout=30)
        assert len(calls) == 1 and calls[0].split('?', 1)[0] == '/v1/messages', f'Unexpected gateway requests: {calls}'
        assert classify_failure(process.stdout+'\n'+process.stderr)[0] == 'infrastructure'
    finally:
        server.shutdown(); server.server_close()


@pytest.mark.parametrize("guidance_phase", ["tool", "text", "boundary"])
def test_actual_claude_invokes_skills_and_persists_question(repository, tmp_path, guidance_phase):
    calls, events = [], []
    guidance = {"available":False, "delivered":False, "observed":False}
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
            if 'LOCAL_GUIDANCE' in json.dumps(body): guidance['observed'] = True
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
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.send_header('Content-Length', str(len(data))); self.end_headers()
            if guidance_phase == 'text' and index == len(responses):
                split = data.index(b'event: content_block_stop')
                self.wfile.write(data[:split]); self.wfile.flush()
                guidance['available'] = True
                time.sleep(.5)
                self.wfile.write(data[split:])
            else:
                self.wfile.write(data)

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
        if action.endswith('/inputs'):
            if current_input[0] == 1 and not guidance['delivered'] and (
                    payload['action'] == 'take' and guidance['available'] or payload['action'] == 'close' and guidance_phase == 'boundary'):
                guidance['delivered'] = True
                return {'items':[{'id':20,'data':{'content':'LOCAL_GUIDANCE: preserve the agreed scope'}}], 'closed':False}
            return {'items': [], 'closed': payload['action'] == 'close'}
        if guidance_phase == 'tool' and payload.get('kind') == 'questions' and current_input[0] == 1:
            guidance['available'] = True
            time.sleep(.5)
        events.append((action, payload))
        return {'version': 1}
    dispatcher = SimpleNamespace(directory=tmp_path, config=SimpleNamespace(design=DesignConfig(
        bmad_repository=os.environ.get('CAIRN_BMAD_REPOSITORY', str(Path(__file__).resolve().parents[3] / 'BMAD-METHOD')),
        grilling_repository=os.environ.get('CAIRN_GRILLING_REPOSITORY', str(Path(__file__).resolve().parents[3] / 'skills')))), call=call)
    directory = tmp_path / 'runs' / 'native-run'
    directory.mkdir(parents=True)
    try:
        output, session = asyncio.run(asyncio.wait_for(run_turn(dispatcher,
            SimpleNamespace(project=SimpleNamespace(id='native-test', development=SimpleNamespace(repository=str(repository))), hints=[]),
            SimpleNamespace(work=SimpleNamespace(result={'message_id': 1})), worker,
            {'id': 'native-run', 'token': 'local-lease'}, directory, repository, TaskCancellation()), 60))
        saved_session[0], current_input[0] = session, 2
        resumed_directory = tmp_path / 'runs' / 'resumed-run'
        resumed_directory.mkdir()
        resumed, resumed_session = asyncio.run(asyncio.wait_for(run_turn(dispatcher,
            SimpleNamespace(project=SimpleNamespace(id='native-test', development=SimpleNamespace(repository=str(repository))), hints=[]),
            SimpleNamespace(work=SimpleNamespace(result={'message_id': 2})), worker,
            {'id': 'resumed-run', 'token': 'next-lease'}, resumed_directory, repository, TaskCancellation()), 60))
        assert resumed['kind'] == 'design' and resumed_session == session
    finally:
        (tmp_path / 'guidance-state.json').write_text(json.dumps(guidance))
        server.shutdown(); server.server_close(); thread.join(timeout=3)
    assert guidance['delivered'] and guidance['observed'], 'Guidance must reach the actual native CLI model stream'
    assert output['kind'] == 'design' and session
    assert any(payload.get('pid') for action, payload in events if action.endswith('/heartbeat'))
    activities = [p['data'].get('skill') for _, p in events if p.get('kind') == 'activity']
    assert {'cairn-design', 'bmad-product-brief', 'grilling'} <= set(activities)
    assert any(p.get('kind') == 'questions' and p['data']['questions'][0]['id'] == 'Q1' for _, p in events)
    assert calls and any('mcp__cairn__ask_questions' in tools for tools in calls)


@pytest.mark.parametrize("bounded", [False, True])
def test_actual_review_cli_denies_source_edits_but_can_write_own_report(repository,tmp_path,bounded):
    from cairn.dispatcher.frozen_review import run_review, ReviewChecks
    from cairn.dispatcher.runtime.git_workspace import GitWorkspace
    from cairn.server.development_models import DevelopmentProject
    import sys
    run_dir=tmp_path/'review-run'; run_dir.mkdir()
    context=run_dir/'context.json'; context.write_text('{}')
    original=(repository/'README.md').read_bytes()
    report=run_dir/'reports'/'review.md'
    requests=[]
    class Reviewer(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if 'count_tokens' in self.path:
                data=b'{"input_tokens":100}'
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            index=len(requests); requests.append(body)
            tools=[('Write',{'file_path':str(repository/'README.md'),'content':'Unauthorized overwrite'}),
                   ('Write',{'file_path':str(report),'content':'# Independent findings'})]
            if bounded:
                tools += [('mcp__cairn_review__run_check', {'index':0}), ('mcp__cairn_review__run_check', {'index':0})]
            chosen=tools[index] if index<len(tools) else None
            result=json.dumps({'kind':'review','verdict':'request_changes','summary':'Independent findings recorded'})
            block={'type':'tool_use','id':f'tool_{index}','name':chosen[0],'input':{}} if chosen else {'type':'text','text':''}
            frames=[('message_start',{'type':'message_start','message':{'id':f'msg_{index}','type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':100,'output_tokens':0}}}),
                ('content_block_start',{'type':'content_block_start','index':0,'content_block':block}),
                ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps(chosen[1])} if chosen else {'type':'text_delta','text':result}}),
                ('content_block_stop',{'type':'content_block_stop','index':0}),
                ('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use' if chosen else 'end_turn','stop_sequence':None},'usage':{'output_tokens':20}}),
                ('message_stop',{'type':'message_stop'})]
            data=''.join('event: '+name+'\ndata: '+json.dumps(data)+'\n\n' for name,data in frames).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Reviewer)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    worker=WorkerConfig(name='review',type='claudecode',roles=['architect'],tier='strong',max_running=1,priority=0,
        env={'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{server.server_port}','ANTHROPIC_AUTH_TOKEN':'local-test-only','ANTHROPIC_API_KEY':'',
             'ANTHROPIC_MODEL':'claude-sonnet-4-6','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','NO_PROXY':'127.0.0.1,localhost'})
    dispatcher=SimpleNamespace(call=lambda *args: {})
    project=SimpleNamespace(project=SimpleNamespace(id='test',development=SimpleNamespace(repository=str(repository))))
    checks = ReviewChecks(GitWorkspace(DevelopmentProject(repository=str(repository),design='Review implementation'),tmp_path/'data','test'),
        repository, [[sys.executable,'-c',"import os;from pathlib import Path;p=Path(os.environ['CAIRN_ARTIFACT_DIR'])/'count';p.write_text(p.read_text()+'x' if p.exists() else 'x')"]], run_dir, TaskCancellation()) if bounded else None
    try:
        result,session=asyncio.run(asyncio.wait_for(run_review(dispatcher,project,worker,{'id':'review-run','token':'local'},run_dir,repository,
            'Read the context and report findings as JSON: '+str(context),context,TaskCancellation(),checks=checks),40))
    finally:
        server.shutdown();server.server_close();thread.join(timeout=3)
    assert json.loads(result)['verdict']=='request_changes' and session
    assert (repository/'README.md').read_bytes()==original
    assert report.read_text()=='# Independent findings'
    assert all('Bash' not in [t['name'] for t in body['tools']] for body in requests)
    if bounded:
        assert (run_dir/'review-check-0'/'count').read_text() == 'x'
        assert len(checks.results) == 1
        assert any('"reused": true' in json.dumps(body) .replace('\\"','"') for body in requests)
