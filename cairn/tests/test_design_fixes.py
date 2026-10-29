import json
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace

from cairn.server import db, development_store as store
from cairn.dispatcher.models import RunningTask
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.dispatcher.scheduler.loop import DispatcherLoop
from test_design import create_design, url, agent, ready, freeze
from test_development_api import http, OPERATOR, WORKER, claim
from test_development_runtime import repository


def test_connect_is_atomic_without_fabricated_user_input_and_mode_is_explicit(http, repository):
    project = create_design(http, repository)
    assert http.post(url(project, '/connect')).status_code == 401
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: http.post(url(project, '/connect'), headers=OPERATOR).json(), range(2)))
    assert results[0]['intent_id'] == results[1]['intent_id']
    iid = results[0]['intent_id']
    data = http.get(url(project), headers=OPERATOR).json()
    assert data['messages'] == [] and data['draft']['design_mode'] is None
    run = claim(http, project, iid).json()
    endpoint = url(project, '/runs/' + run['id'] + '/inputs')
    assert http.post(endpoint, headers=WORKER, json={'token':run['token'],'action':'start'}).status_code == 200
    assert agent(http, project, run, 'draft', {'version':0,'documents':{'brief':'premature'}}).status_code == 409
    assert http.post(url(project, '/mode'), headers=OPERATOR, json={'version':0,'mode':'auto'}).status_code == 422
    response = http.post(url(project, '/mode'), headers=OPERATOR, json={'version':0,'mode':'coaching'})
    assert response.json()['design_mode'] == 'coaching'
    assert http.post(url(project, '/mode'), headers=OPERATOR, json={'version':0,'mode':'quick'}).status_code == 409
    batch = http.post(endpoint, headers=WORKER, json={'token':run['token'],'action':'take'}).json()['items']
    assert batch and '教练路径' in batch[0]['data']['content']
    assert len(http.get(f'/projects/{project}').json()['intents']) == 1


def test_mode_quote_cannot_infer_negative_or_fabricate_consent(http, repository):
    project = create_design(http, repository)
    sent = http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'mode','content':'不要快速路径，我选择教练路径'}).json()
    run = claim(http,project,sent['intent_id']).json()
    assert agent(http,project,run,'mode',{'mode':'quick','quote':'不要快速路径','message_id':sent['id']},eid='negative').status_code == 422
    assert agent(http,project,run,'mode',{'mode':'quick','quote':'快速路径','message_id':sent['id']},eid='clipped-negative').status_code == 422
    assert agent(http,project,run,'mode',{'mode':'quick','quote':'我选择快速路径','message_id':sent['id']},eid='fake').status_code == 422
    assert agent(http,project,run,'mode',{'mode':'coaching','quote':'我选择教练路径','message_id':sent['id']},eid='explicit').status_code == 200


def test_changes_names_actual_frozen_baseline(http, repository):
    project, _, _, _ = ready(http, repository)
    frozen = freeze(http,project)
    state = http.get(url(project), headers=OPERATOR).json()
    assert state['review']['comparison'] == {'from_version':frozen['version'],'to_version':frozen['version']}
    assert all(not values for values in state['review']['changes'].values())
    assert http.post(url(project,'/connect'), headers=OPERATOR).json()['state'] == 'frozen'


def test_workspace_guard_resolves_paths_and_configures_owned_temporary_output(tmp_path):
    from cairn.dispatcher.runtime.workspace_guard import prepare, check
    workspace, run = tmp_path/'worktree', tmp_path/'run'
    workspace.mkdir(); run.mkdir()
    env = prepare(workspace,run)
    assert env['TEMP'] == str(run/'tmp') and env['TMPDIR'] == env['TEMP']
    def call(name, value): return check({'tool_name':name,'tool_input':value},workspace,run)
    assert call('Write', {'file_path':str(workspace/'src.js')})
    assert call('Write', {'file_path':str(run/'artifacts/debug.js')})
    assert not call('Write', {'file_path':str(tmp_path/'outside.js')})
    assert not call('Edit', {'file_path':'sub/../../outside.js'})
    assert not call('Write', {'file_path':'.git/config'})
    assert not call('Bash', {'command':'python -c "open('+repr(str(tmp_path/'outside.js'))+',\'w\').write(\'bad\')"'})
    assert not call('Bash', {'command':"python -c \"open('sub/../../outside.js','w').write('bad')\""})
    assert call('Bash', {'command':'node --test test/unit.test.mjs'})
    assert call('Bash', {'command':'node "'+str(workspace/'file with spaces.mjs')+'"'})


def test_resume_rejects_changed_revision_profile_or_checkout(repository, tmp_path):
    from cairn.dispatcher.runtime.session_resume import session_identity, compatible_session
    worker = SimpleNamespace(type='claudecode', command=[], model='model', env={})
    intent = SimpleNamespace(id='task', work=SimpleNamespace(revision=1, design_revision=1))
    identity = session_identity(worker,intent,repository)
    (tmp_path/'old').mkdir()
    (tmp_path/'old/session-context.json').write_text(json.dumps({**identity,'session':'prior-chat'}))
    runs = [{'id':'old','state':'infrastructure'}]
    assert compatible_session(tmp_path,runs,'new',identity) == 'prior-chat'
    assert compatible_session(tmp_path,runs,'new',{**identity,'revision':2}) is None
    assert compatible_session(tmp_path,runs,'new',{**identity,'head':'different'}) is None
    assert compatible_session(tmp_path,[{'id':'old','state':'success'}],'new',identity) is None


def test_interactive_design_does_not_consume_implementation_worker_slots():
    loop = DispatcherLoop.__new__(DispatcherLoop)
    loop.futures = {Future():RunningTask('project','development.design','strong',TaskCancellation()),
        Future():RunningTask('project','development.implementer','standard',TaskCancellation())}
    assert loop._active_task_count() == 1
    assert loop._worker_counts() == {'standard':1}
    assert loop._project_running_task_count('project') == 1


def test_frozen_stage_review_uses_specific_input_not_development_logs(repository, tmp_path, monkeypatch):
    import asyncio
    from cairn.dispatcher.frozen_review import run_review
    directory = tmp_path/'review'; directory.mkdir()
    (directory/'work-index.json').write_text('{}')
    context = directory/'context.json'
    context.write_text(json.dumps({'subject':{'id':'candidate','work':{'result':{'design_snapshot':{
        'stage':'requirements','version':6,'documents':{'prd':'Complete specification'}}}}},
        'raw_evidence':[{'path':str(directory/'stdout.log')}]}))
    captured = {}
    async def console(*args, **kwargs): captured.update(kwargs); return '{}','review'
    monkeypatch.setattr('cairn.dispatcher.design_terminal.run_console', console)
    project = SimpleNamespace(project=SimpleNamespace(development=SimpleNamespace(repository=str(repository))))
    asyncio.run(run_review(None,project,None,{},directory,repository,'generic JSON Schema',context,TaskCancellation()))
    assert 'JSON Schema' not in captured['prompt']
    assert captured['readable'] == {(directory/'design-review-context.json').resolve()}
    frozen = json.loads((directory/'design-review-context.json').read_text())
    assert frozen['frozen_design']['documents']['prd'] == 'Complete specification'
    assert 'raw_evidence' not in frozen


def test_role_limits_can_be_set_in_launcher_environment(tmp_path, monkeypatch):
    import pytest
    from conftest import make_config
    from cairn.dispatcher.config import DispatchConfig
    path = tmp_path/'dispatch.yaml'; path.write_text(json.dumps(make_config().model_dump(mode='json')))
    monkeypatch.setenv('CAIRN_MAX_IMPLEMENTERS','2'); monkeypatch.setenv('CAIRN_MAX_REVIEWERS','1')
    config = DispatchConfig.load(path)
    assert config.runtime.max_implementers == 2 and config.runtime.max_reviewers == 1
    monkeypatch.setenv('CAIRN_MAX_REVIEWERS','0')
    with pytest.raises(ValueError): DispatchConfig.load(path)
