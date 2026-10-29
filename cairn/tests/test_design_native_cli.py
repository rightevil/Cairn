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
    dispatcher=SimpleNamespace(call=lambda *args: {}, config=SimpleNamespace(server='http://127.0.0.1:1'), client=SimpleNamespace(_worker_token='local-worker'))
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


def test_native_design_mcp_and_keyboard_guidance_resume(repository,tmp_path):
    from cairn.dispatcher.design_terminal import run_console
    from cairn.dispatcher.development import atomic_json
    from cairn.dispatcher.runtime.git_workspace import git
    import uuid
    workspace=tmp_path/'design'; workspace.mkdir()
    skill=workspace/'.claude/skills/cairn-design/SKILL.md'; skill.parent.mkdir(parents=True)
    skill.write_text('Use Cairn tools. Never approve a design.')
    (workspace/'context.json').write_text(json.dumps({'draft':{'session':None}}))
    run_dir=tmp_path/'run';run_dir.mkdir()
    events=[]; prompts=[]; calls=[]
    class Gateway(BaseHTTPRequestHandler):
        def log_message(self,*args): pass
        def do_POST(self):
            body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if '/development/' in self.path:
                events.append((self.path,body))
                data=json.dumps({'id':2,'version':1}).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            if 'count_tokens' in self.path:
                data=b'{"input_tokens":100}'
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data);return
            calls.append(body)
            first=bool(body.get('tools')) and not any(b.get('tools') for b in calls[:-1])
            block={'type':'tool_use','id':'ask','name':'mcp__cairn__ask_questions','input':{}} if first else {'type':'text','text':''}
            frames=[('message_start',{'type':'message_start','message':{'id':'msg'+str(len(calls)),'type':'message','role':'assistant','model':body['model'],'content':[],'stop_reason':None,'stop_sequence':None,'usage':{'input_tokens':100,'output_tokens':0}}}),
                ('content_block_start',{'type':'content_block_start','index':0,'content_block':block}),
                ('content_block_delta',{'type':'content_block_delta','index':0,'delta':{'type':'input_json_delta','partial_json':json.dumps({'questions':[{'id':'Q1','text':'Which audience?'}]})} if first else {'type':'text_delta','text':'Saved. Waiting for you.'}}),
                ('content_block_stop',{'type':'content_block_stop','index':0}),
                ('message_delta',{'type':'message_delta','delta':{'stop_reason':'tool_use' if first else 'end_turn','stop_sequence':None},'usage':{'output_tokens':20}}),('message_stop',{'type':'message_stop'})]
            data=''.join('event: '+n+'\ndata: '+json.dumps(v)+'\n\n' for n,v in frames).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
    server=ThreadingHTTPServer(('127.0.0.1',0),Gateway)
    threading.Thread(target=server.serve_forever,daemon=True).start()
    base=f'http://127.0.0.1:{server.server_port}'
    worker=WorkerConfig(name='native',type='claudecode',roles=['planner'],tier='strong',max_running=1,priority=0,
        env={'ANTHROPIC_BASE_URL':base,'ANTHROPIC_AUTH_TOKEN':'local-test-only','ANTHROPIC_API_KEY':'','ANTHROPIC_MODEL':'claude-sonnet-4-6',
            'HTTP_PROXY':'','HTTPS_PROXY':'','ALL_PROXY':'','NO_PROXY':'127.0.0.1,localhost'})
    dispatcher=SimpleNamespace(config=SimpleNamespace(server=base),client=SimpleNamespace(_worker_token='local-worker'),call=lambda *a: {})
    project=SimpleNamespace(project=SimpleNamespace(id='test',development=SimpleNamespace(repository=str(repository))))
    async def operate():
        seen=None; sent=False
        for _ in range(300):
            await asyncio.sleep(.1)
            state_file=run_dir/'terminal-state.json'
            if not state_file.exists(): continue
            state=json.loads(state_file.read_text())
            if state['state']!='idle': continue
            if not sent:
                seen=state['time']; sent=True
                atomic_json(run_dir/'operator-input.json',{'nonce':'local-input','time':time.time()})
                (run_dir/'terminal-input.jsonl').write_text(json.dumps({'type':'input','data':'LOCAL_GUIDANCE'})+'\n'+json.dumps({'type':'input','data':'\r'})+'\n')
            elif state['time']!=seen:
                with (run_dir/'terminal-input.jsonl').open('a') as f:f.write(json.dumps({'type':'close'})+'\n')
                return
        raise AssertionError('Native terminal never completed both interactive turns')
    async def scenario():
        task=asyncio.create_task(operate())
        try:
            return await asyncio.wait_for(run_console(dispatcher,project,worker,{'id':'run','token':'local'},run_dir,repository,
                TaskCancellation(),workspace=workspace,prompt='Design the feature.',session=str(uuid.uuid4()),message_id=1),35)
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    try: result,session=asyncio.run(scenario())
    finally:
        server.shutdown();server.server_close()
        (tmp_path/'model-debug.json').write_text(json.dumps({'calls':calls,'events':events}),encoding='utf-8')
    assert 'Saved' in result and session
    assert any(body.get('kind')=='questions' for _,body in events)
    assert any(body.get('content')=='LOCAL_GUIDANCE' for path,body in events if path.endswith('/terminal/input'))
    assert any('LOCAL_GUIDANCE' in json.dumps(body) for body in calls)
