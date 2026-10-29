import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cairn.server import db
from cairn.server.app import app
from test_design import create_design,send,url
from test_development_api import http,OPERATOR,WORKER,claim
from test_development_runtime import repository


def test_native_terminal_local_auth_ownership_input_and_idle_close(http,repository):
    project=create_design(http,repository)
    intent=send(http,project)
    run=claim(http,project,intent).json()
    directory=repository/'.cairn/workspaces'/project/'runs'/run['id'];directory.mkdir(parents=True)
    terminal=directory/'terminal.log';terminal.write_text('Native CLI output',encoding='utf-8')
    (directory/'terminal-state.json').write_text(json.dumps({'state':'idle','time':time.time()}))
    endpoint=f'/projects/{project}/development/runs/{run["id"]}/terminal'
    with db.get_conn() as conn:
        conn.execute('UPDATE runs SET streams=? WHERE id=?',(json.dumps({'terminal':str(terminal)}),run['id']))
        conn.execute("UPDATE design_inputs SET state='delivered',run_id=? WHERE intent_id=?",(run['id'],intent))
    browser=TestClient(app,base_url='http://127.0.0.1',client=('127.0.0.1',50000))
    browser.get('/')
    with pytest.raises(WebSocketDisconnect):
        with browser.websocket_connect("ws://127.0.0.1"+endpoint,headers={'origin':'http://evil.example'}):pass
    with browser.websocket_connect("ws://127.0.0.1"+endpoint,headers={'origin':'http://127.0.0.1'}) as ws:
        assert ws.receive_json()=={'type':'ready','readonly':False}
        output = ws.receive_json()
        assert output['data']=='Native CLI output'
        ws.send_json({'type':'ack','seq':output['seq']})
        with browser.websocket_connect("ws://127.0.0.1"+endpoint) as viewer:
            assert viewer.receive_json()['readonly'] is True
        ws.send_json({'type':'input','data':'Direction\r'})
        for _ in range(100):
            if (directory/'operator-input.json').exists():break
            time.sleep(.01)
        ticket=json.loads((directory/'operator-input.json').read_text())
        payload={'token':run['token'],'content':'Direction','key':'native-input','nonce':'forged'}
        assert http.post(endpoint+'/input',headers=WORKER,json=payload).status_code==409
        payload['nonce']=ticket['nonce']
        assert http.post(endpoint+'/input',headers=WORKER,json=payload).status_code==200
        assert http.post(endpoint+'/close',headers=OPERATOR,json={}).status_code==409
        (directory/'terminal-state.json').write_text(json.dumps({'state':'idle','time':time.time()+1}))
        assert http.post(endpoint+'/close',headers=OPERATOR,json={}).status_code==200
        late=http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'after-close','content':'Keep this for the next conversation'})
        assert late.status_code == 201 and late.json()['intent_id'] != intent
        assert http.post(endpoint+'/input',headers=WORKER,json={**payload,'key':'after-close-native'}).status_code == 409
        stats=http.get(endpoint+'/metrics',headers=OPERATOR).json()
        assert stats['samples']['server_to_paint_ack']['count'] >= 1
    assert 'Direction' in http.get(url(project),headers=OPERATOR).text
    assert any(json.loads(line)['type']=='close' for line in (directory/'terminal-input.jsonl').read_text().splitlines())
