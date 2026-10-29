import json
import uuid

from cairn.dispatcher.runtime.terminal_history import archive, messages
from cairn.server import db
from test_design import create_design, send, ready, freeze, review
from test_development_api import http, OPERATOR, claim
from test_development_runtime import repository


def test_history_preserves_messages_and_tools_but_excludes_thinking(tmp_path):
    session = str(uuid.uuid4())
    path = tmp_path/'messages.jsonl'
    records = [
        {'uuid':'user','type':'user','sessionId':session,'message':{'content':'Actual user input'}},
        {'uuid':'agent','type':'assistant','sessionId':session,'message':{'content':[
            {'type':'thinking','thinking':'Private model reasoning'},
            {'type':'text','text':'Readable assistant reply'},
            {'type':'tool_use','name':'Read','input':{'file_path':'artifacts/spec.md'}}]}},
        {'uuid':'tool','type':'user','sessionId':session,'message':{'content':[{'type':'tool_result','content':[{'type':'text','text':'Tool response'},{'type':'image','data':'do not copy binary'}]}]}},
        {'uuid':'other','type':'assistant','sessionId':'another-session','message':{'content':'Unrelated session'}},
        {'uuid':'child','type':'assistant','isSidechain':True,'message':{'content':'Child session'}},
    ]
    path.write_text('\n'.join(json.dumps(item) for item in records)+'\nnull\n{"partial":',encoding='utf-8')
    items = messages(path,session)
    assert len(items) == 3
    assert items[0]['text'] == 'Actual user input'
    assert 'Readable assistant reply' in items[1]['text'] and 'artifacts/spec.md' in items[1]['text']
    assert 'Tool response' in items[2]['text']
    assert items[2]['role'] == '工具'
    assert not any(secret in str(items) for secret in ('Private model reasoning','Unrelated session','Child session','do not copy binary'))


def test_ended_history_paginates_from_start_and_survives_source_removal(http,repository):
    project = create_design(http,repository); intent = send(http,project); run = claim(http,project,intent).json()
    session = str(uuid.uuid4())
    directory = repository/'.cairn/workspaces'/project/'runs'/run['id']; directory.mkdir(parents=True)
    log = directory/'terminal.log'; log.write_text('Last TUI frame only')
    source = directory.parent/'claude-profiles'/session/'projects/test'/(session+'.jsonl'); source.parent.mkdir(parents=True)
    source.write_text('\n'.join(json.dumps({'uuid':str(i),'type':'assistant','sessionId':session,
        'message':{'content':[{'type':'text','text':'Saved conversation message '+str(i)}]}}) for i in range(420)),encoding='utf-8')
    with db.get_conn() as conn:
        conn.execute("UPDATE runs SET state='success',session=?,streams=? WHERE id=?",(session,json.dumps({'terminal':str(log)}),run['id']))
    endpoint = f'/projects/{project}/development/runs/{run["id"]}/terminal/history'
    assert http.get(endpoint).status_code == 401
    page = http.get(endpoint,headers=OPERATOR).json()
    assert page['total'] == 420 and page['before'] == 220 and page['source'] == 'claude'
    assert page['entries'][-1]['text'].endswith('419')
    earlier = http.get(endpoint+'?before=220',headers=OPERATOR).json()
    assert earlier['entries'][0]['index'] == 20 and earlier['before'] == 20
    first = http.get(endpoint+'?before=20',headers=OPERATOR).json()
    assert first['entries'][0]['text'].endswith('0') and first['before'] is None
    assert http.get(endpoint+'?before=-1',headers=OPERATOR).status_code == 422
    archive({'directory':str(directory),'session':session},{})
    source.unlink()
    replay = http.get(endpoint,headers=OPERATOR).json()
    assert replay['source'] == 'archive' and replay['entries'] == page['entries']
    other = create_design(http,repository)
    assert http.get(endpoint.replace(project,other),headers=OPERATOR).status_code == 404


def test_design_and_review_fallbacks_are_readable_and_never_start_a_run(http,repository):
    project,_,_,_ = ready(http,repository)
    frozen = freeze(http,project); run = review(http,project,frozen)
    response = http.get(f'/projects/{project}/development/runs/{run["id"]}/terminal/history',headers=OPERATOR)
    assert response.status_code == 200, response.text
    assert response.json()['source'] == 'saved_review'
    assert response.json()['entries'][0]['text'] == 'Independent review'
    assert not any(r['state']=='running' for r in http.get(f'/projects/{project}/development',headers=OPERATOR).json()['runs'])
