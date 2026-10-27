import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from cairn.dispatcher.runtime.git_workspace import git
from cairn.server import db, development_store as store
from cairn.server.development_models import WorkPackage
from test_development_api import OPERATOR, WORKER, claim, http
from test_development_runtime import repository


def create_design(http, repository):
    response = http.post('/projects', headers=OPERATOR, json={
        'title': 'Design', 'origin': 'Existing repository', 'goal': 'A useful feature',
        'development': {'phase': 'design', 'repository': str(repository), 'design': 'Initial idea',
                        'delivery': 'local', 'checks': [['git', 'diff', '--check']]}})
    assert response.status_code == 201, response.text
    assert response.json()['intents'] == []
    return response.json()['project']['id']


def url(project, suffix=''):
    return f'/projects/{project}/development/design' + suffix


def send(http, project, content='Design this feature', mid='message-1', answers=None):
    response = http.post(url(project, '/messages'), headers=OPERATOR,
                         json={'id': mid, 'content': content, 'answers': answers or {}})
    assert response.status_code == 201, response.text
    return response.json()['intent_id']


def agent(http, project, run, kind, data, eid=None):
    return http.post(url(project, '/runs/' + run['id']), headers=WORKER, json={
        'token': run['token'], 'id': eid or kind, 'kind': kind, 'data': data})


def finish(http, project, run, output, snapshot=None, session='author', outcome='success'):
    response = http.post(f"/projects/{project}/development/runs/{run['id']}/finish", headers=WORKER, json={
        'token': run['token'], 'outcome': outcome, 'session': session,
        'result': {'output': output, 'snapshot': snapshot or {}},
        'evidence': [{'kind': 'log', 'path': 'test-fixture', 'sha256': 'a' * 64}]})
    assert response.status_code == 200, response.text
    response = http.post(f'/projects/{project}/development/advance', headers=WORKER, json={})
    assert response.status_code == 200, response.text


def ready(http, repository):
    project = create_design(http, repository)
    intent = send(http, project)
    run = claim(http, project, intent).json()
    baseline = git(repository, 'rev-parse', 'HEAD')
    assert agent(http, project, run, 'session', {'session': 'author', 'baseline': baseline}).status_code == 200
    task = {'key': 'FEATURE-1', 'description': 'Implement the feature', 'scope': 'Existing app only',
            'acceptance': ['Feature follows requirements'], 'spec_refs': ['requirements'],
            'checks': [['git', 'diff', '--check']], 'dependencies': []}
    response = agent(http, project, run, 'draft', {'version': 0, 'stage': 'plan',
        'documents': {'requirements': 'The confirmed feature requirements', 'architecture': 'Keep the current architecture'},
        'decisions': ['Preserve existing behavior'], 'work': [task]})
    assert response.status_code == 200, response.text
    finish(http, project, run, {'kind': 'design', 'summary': 'Draft saved'})
    return project, intent, run, task


def freeze(http, project):
    draft = http.get(url(project), headers=OPERATOR).json()['draft']
    response = http.post(url(project, '/candidate'), headers=OPERATOR, json={'version': draft['version']})
    assert response.status_code == 200, response.text
    return response.json()


def review(http, project, draft, *, session='independent', verdict='approve', evidence=True):
    work = http.get(f'/projects/{project}').json()['intents']
    iid = next(i['id'] for i in work if i['work']['role'] == 'architect' and i['work']['state'] == 'ready')
    run = claim(http, project, iid, role='architect', worker='reviewer').json()
    finish(http, project, run, {'kind': 'review', 'summary': 'Independent review', 'verdict': verdict},
           {'design_baseline': draft['baseline'], 'checks_passed': evidence, 'check_scope': 'design_baseline'}, session=session)
    return run


def test_design_has_no_automatic_planner_and_rejects_execution_claims(http, repository):
    project = create_design(http, repository)
    http.post(f'/projects/{project}/development/advance', headers=WORKER)
    assert http.get(f'/projects/{project}').json()['intents'] == []
    with db.get_conn() as conn:
        iid = store.add_intent(conn, project, 'Bypass approval', WorkPackage(revision=1))
    assert claim(http, project, iid, role='implementer').status_code == 409
    assert http.post(url(project, '/messages'), headers=WORKER, json={'id': 'x', 'content': 'Start'}).status_code == 401


def test_message_retries_are_idempotent_and_cannot_change_content(http, repository):
    project = create_design(http, repository)
    body = {'id': 'same', 'content': 'Design'}
    first = http.post(url(project, '/messages'), headers=OPERATOR, json=body)
    assert first.status_code == 201
    assert http.post(url(project, '/messages'), headers=OPERATOR, json=body).status_code == 201
    assert http.post(url(project, '/messages'), headers=OPERATOR, json={**body, 'content': 'Changed'}).status_code == 409
    assert len(http.get(f'/projects/{project}').json()['intents']) == 1
    assert http.post(url(project, '/messages'), headers=OPERATOR, json={'id': 'other', 'content': 'Another'}).status_code == 201
    assert len(http.get(f'/projects/{project}').json()['intents']) == 1


def test_frontier_questions_require_explicit_operator_answers(http, repository):
    project = create_design(http, repository)
    run = claim(http, project, send(http, project)).json()
    q = {'id': 'Q1', 'text': 'Who is the user?', 'recommendation': 'Internal team'}
    assert agent(http, project, run, 'questions', {'questions': [q]}).status_code == 200
    assert agent(http, project, run, 'questions', {'questions': [{**q, 'id': 'Q2', 'depends_on': ['Q1']}]}, 'dependent').status_code == 409
    assert agent(http, project, run, 'questions', {'questions': [{**q, 'id': 'Q3', 'answer': 'default'}]}, 'fabricate').status_code == 409
    finish(http, project, run, {'kind': 'design', 'summary': 'Waiting for user'})
    assert http.post(url(project, '/candidate'), headers=OPERATOR, json={'version': 1}).status_code == 422
    send(http, project, 'The internal team', 'answer', {'Q1': 'Internal team'})
    state = http.get(url(project), headers=OPERATOR).json()
    assert state['draft']['questions'][0]['answer'] == 'Internal team'
    assert state['draft']['version'] == 2


def test_card_answers_save_one_question_at_a_time_and_resume_only_after_the_last(http, repository):
    project = create_design(http, repository)
    run = claim(http, project, send(http, project)).json()
    questions = [{'id': 'Q1', 'text': 'First choice?'}, {'id': 'Q2', 'text': 'Second choice?'}]
    assert agent(http, project, run, 'questions', {'questions': questions}).status_code == 200
    finish(http, project, run, {'kind': 'design', 'summary': 'Waiting for answers'})
    first = http.post(url(project, '/messages'), headers=OPERATOR, json={
        'id': 'card-one', 'content': 'Q1: First answer', 'answers': {'Q1': 'First answer'}})
    assert first.status_code == 201 and first.json()['intent_id'] is None
    assert first.json()['remaining_questions'] == ['Q2']
    draft = http.get(url(project), headers=OPERATOR).json()['draft']
    assert [q['answer'] for q in draft['questions']] == ['First answer', None]
    assert len(http.get(f'/projects/{project}').json()['intents']) == 1
    assert http.post(url(project, '/messages'), headers=OPERATOR, json={
        'id': 'card-one', 'content': 'Q1: First answer', 'answers': {'Q1': 'First answer'}}).status_code == 201
    final = http.post(url(project, '/messages'), headers=OPERATOR, json={
        'id': 'card-two', 'content': 'Q2: Second answer', 'answers': {'Q2': 'Second answer'}})
    assert final.status_code == 201 and final.json()['intent_id']
    assert len(http.get(f'/projects/{project}').json()['intents']) == 2
    draft = http.get(url(project), headers=OPERATOR).json()['draft']
    assert [q['answer'] for q in draft['questions']] == ['First answer', 'Second answer']


def test_native_release_requires_review_and_exact_confirmation_then_is_atomic_idempotent(http, repository):
    project, _, _, _ = ready(http, repository)
    draft = freeze(http, project)
    payload = {'version': draft['version'], 'digest': draft['candidate']['digest']}
    assert http.post(url(project, '/publish'), headers=OPERATOR, json=payload).status_code == 409
    review(http, project, draft)
    before = http.get(f'/projects/{project}').json()
    assert not any(i['work']['role'] == 'implementer' for i in before['intents'])
    assert http.post(url(project, '/publish'), headers=OPERATOR, json={**payload, 'digest': 'wrong'}).status_code == 409
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: http.post(url(project, '/publish'), headers=OPERATOR, json=payload), range(2)))
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    data = http.get(f'/projects/{project}').json()
    tasks = [i for i in data['intents'] if i['work']['role'] == 'implementer']
    assert len(tasks) == 1 and tasks[0]['work']['spec_refs'] == ['requirements']
    assert data['project']['development']['phase'] == 'execution'
    assert claim(http, project, tasks[0]['id'], role='implementer').status_code == 200


def test_document_stage_can_be_reviewed_without_tasks_then_continue_without_publishing(http, repository):
    project = create_design(http, repository)
    run = claim(http, project, send(http, project)).json()
    baseline = git(repository, 'rev-parse', 'HEAD')
    assert agent(http, project, run, 'session', {'session': 'author', 'baseline': baseline}).status_code == 200
    assert agent(http, project, run, 'draft', {'version': 0, 'stage': 'requirements',
        'documents': {'prd': 'The whole PRD', 'addendum': 'All numeric rules'}, 'work': []}).status_code == 200
    finish(http, project, run, {'kind': 'design', 'summary': 'Requirements ready'})
    assert http.get(url(project), headers=OPERATOR).json()['readiness']['ready']
    draft = freeze(http, project)
    assert draft['candidate']['kind'] == 'stage'
    payload = {'version': draft['version'], 'digest': draft['candidate']['digest']}
    assert http.post(url(project, '/continue'), headers=OPERATOR, json=payload).status_code == 409

    review(http, project, draft)
    assert http.post(url(project, '/publish'), headers=OPERATOR, json=payload).status_code == 409
    response = http.post(url(project, '/continue'), headers=OPERATOR, json=payload)
    assert response.status_code == 200, response.text
    continued = response.json()
    assert continued['stage'] == 'ux' and not continued['candidate']
    assert continued['session'] == 'author' and continued['documents'] == draft['documents']
    assert continued['stage_reviews'][0]['review'] == 'approve'
    project_state = http.get(f'/projects/{project}').json()
    assert project_state['project']['development']['phase'] == 'design'
    assert not any(i['work']['role'] == 'implementer' for i in project_state['intents'])
    assert sum(i['work']['purpose'] == 'design' and i['work']['state'] == 'ready' for i in project_state['intents']) == 1
    assert http.post(url(project, '/continue'), headers=OPERATOR, json=payload).status_code == 409


def test_review_progress_exposes_real_run_and_preserves_the_report_after_reopening(http, repository):
    project, _, _, _ = ready(http, repository)
    frozen = freeze(http, project)
    status = http.get(url(project), headers=OPERATOR).json()['review']
    assert status['current'] and status['version'] == frozen['version']
    assert status['run'] is None and status['verdict'] == 'pending'
    run = claim(http, project, status['intent_id'], role='architect', worker='independent').json()
    current = http.get(url(project), headers=OPERATOR).json()['review']
    assert current['run']['id'] == run['id'] and current['attempts'] == 1
    assert 'token_hash' not in current['run'] and run['token'] not in str(current)
    response = http.post(f"/projects/{project}/development/runs/{run['id']}/heartbeat", headers=WORKER,
        json={'token': run['token'], 'checkpoint': '正在执行设计基线检查'})
    assert response.status_code == 200
    assert http.get(url(project), headers=OPERATOR).json()['review']['run']['checkpoint'] == '正在执行设计基线检查'
    finish(http, project, run, {'kind': 'review', 'summary': 'Fix the parallel write conflict', 'verdict': 'request_changes'}, session='independent')
    result = http.get(url(project), headers=OPERATOR).json()['review']
    assert result['verdict'] == 'request_changes' and result['summary'] == 'Fix the parallel write conflict'
    assert http.post(url(project, '/reopen'), headers=OPERATOR, json={'version': frozen['version']}).status_code == 200
    archived = http.get(url(project), headers=OPERATOR).json()['review']
    assert not archived['current'] and archived['summary'] == result['summary']


@pytest.mark.parametrize('mutation', ['baseline', 'dirty', 'controls'])
def test_publish_rejects_stale_code_or_controls(http, repository, mutation):
    project, _, _, _ = ready(http, repository)
    draft = freeze(http, project)
    review(http, project, draft)
    if mutation == 'baseline':
        git(repository, 'commit', '--allow-empty', '-m', 'Changed baseline')
    elif mutation == 'dirty':
        (repository / 'README.md').write_text('Uncommitted change')
    else:
        http.post(f'/projects/{project}/development/controls', headers=OPERATOR, json={'kind': 'directive', 'content': 'Different scope'})
    result = http.post(url(project, '/publish'), headers=OPERATOR, json={'version': draft['version'], 'digest': draft['candidate']['digest']})
    assert result.status_code == 409, result.text
    assert http.get(f'/projects/{project}').json()['project']['development']['phase'] == 'design'


@pytest.mark.parametrize('session,evidence', [('author', True), ('independent', False)])
def test_review_cannot_self_approve_or_claim_unrun_checks(http, repository, session, evidence):
    project, _, _, _ = ready(http, repository)
    draft = freeze(http, project)
    review(http, project, draft, session=session, evidence=evidence)
    current = http.get(url(project), headers=OPERATOR).json()['draft']
    assert current['candidate']['review'] == 'pending'


def test_independent_design_runtime_checks_baseline_without_running_future_tests(http, repository, tmp_path, monkeypatch):
    import hashlib
    import sys
    from types import SimpleNamespace
    from cairn.dispatcher.config import LocalConfig, WorkerConfig
    from cairn.dispatcher.development import DevelopmentDispatcher
    from cairn.dispatcher.runtime.cancellation import TaskCancellation
    from cairn.dispatcher.runtime.local_backend import LocalBackend
    from test_development_runtime import Client
    project, _, author, _ = ready(http, repository)
    from cairn.server.design import load, save
    marker = tmp_path / 'future-test-ran'
    with db.get_conn() as conn:
        evidence = tmp_path / 'runtime' / project / 'runs' / author['id'] / 'stdout.log'
        evidence.parent.mkdir(parents=True)
        evidence.write_bytes(b'Design turn saved')
        conn.execute('UPDATE runs SET evidence=? WHERE id=?', (json.dumps([{'kind': 'log', 'path': str(evidence),
            'sha256': hashlib.sha256(evidence.read_bytes()).hexdigest()}]), author['id']))
        draft = load(conn, project)
        draft.work[0].checks = [[sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()']]
        save(conn, project, draft)
    freeze(http, project)
    work = http.get(f'/projects/{project}').json()['intents']
    iid = next(i['id'] for i in work if i['work']['role'] == 'architect')
    run = claim(http, project, iid, role='architect').json()
    from cairn.dispatcher import frozen_review
    async def controlled_review(dispatcher, project, worker, run, directory, *args):
        (directory / 'reports').mkdir()
        (directory / 'stdout.log').write_text('Independent review output')
        (directory / 'stderr.log').write_text('')
        return json.dumps({'kind':'review','verdict':'approve','summary':'Reviewed frozen documents'}), 'independent-runtime'
    monkeypatch.setattr(frozen_review, 'run_review', controlled_review)
    local = LocalConfig(workspace_root=str(tmp_path / 'runtime'))
    client = Client(http)
    loop = SimpleNamespace(client=client, config=SimpleNamespace(server='in-process', local=local,
        runtime=SimpleNamespace(interval=1), tasks=SimpleNamespace(development_timeout=10)), container_manager=LocalBackend(local))
    dispatcher = DevelopmentDispatcher(loop)
    detail = client.get_project(project)
    intent = next(i for i in detail.intents if i.id == iid)
    worker = WorkerConfig(name='independent', type='claudecode', roles=['architect'], tier='strong',
        command=[sys.executable], max_running=1, priority=0)
    assert dispatcher.execute(detail, intent, worker, run, TaskCancellation()) == 'success'
    assert not marker.exists(), 'Design review must not execute planned implementation checks'
    http.post(f'/projects/{project}/development/advance', headers=WORKER)
    current = http.get(url(project), headers=OPERATOR).json()['draft']
    assert current['candidate']['review'] == 'approve'
    records = client.development(project).data['runs']
    result = next(r for r in records if r['id'] == run['id'])
    assert result['result']['snapshot']['check_scope'] == 'design_baseline'
    assert any(e.get('command') == ['git', 'diff', '--check'] for e in result['evidence'])


def test_reopen_preserves_design_and_interrupt_fences_late_writes(http, repository):
    project, _, _, _ = ready(http, repository)
    draft = freeze(http, project)
    reopened = http.post(url(project, '/reopen'), headers=OPERATOR, json={'version': draft['version']})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()['documents'] == draft['documents'] and not reopened.json()['candidate']
    tasks = http.get(f'/projects/{project}').json()['intents']
    iid = next(i['id'] for i in tasks if i['work']['purpose'] == 'design' and i['work']['state'] == 'ready')
    run = claim(http, project, iid).json()
    assert http.post(url(project, '/interrupt'), headers=OPERATOR).status_code == 200
    assert agent(http, project, run, 'text', {'text': 'Late answer'}).status_code == 409
    finish(http, project, run, {'kind': 'design', 'summary': 'Cancelled'}, outcome='cancelled')
    send(http, project, 'Continue from saved draft', 'continue')


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('baseline_changed', [False, True])
def test_review_revision_restores_context_queues_once_and_requires_new_review(http, repository, legacy, baseline_changed):
    project, _, _, task = ready(http, repository)
    frozen = freeze(http, project)
    review(http, project, frozen, verdict='request_changes')
    from cairn.server.design import load, save
    if legacy:
        with db.get_conn() as conn:
            draft = load(conn, project)
            subject = draft.candidate['subject']
            draft.candidate, draft.baseline, draft.session = {}, '', None
            draft.version += 1
            save(conn, project, draft)
            from cairn.server.development_workflow import all_work
            for iid, value in all_work(conn, project):
                if iid == subject or value.subject == subject:
                    value.state = 'superseded'
                    store.save_work(conn, project, iid, value)
    if baseline_changed:
        git(repository, 'commit', '--allow-empty', '-m', 'New baseline')
    previous = http.get(url(project), headers=OPERATOR).json()['draft']
    payload = {'version': previous['version'], 'digest': frozen['candidate']['digest']}
    response = http.post(url(project, '/reopen'), headers=OPERATOR, json=payload)
    assert response.status_code == 200, response.text
    draft = response.json()
    assert draft['session'] == frozen['session']
    assert draft['baseline'] == git(repository, 'rev-parse', 'HEAD')
    assert draft['documents'] == frozen['documents'] and draft['work'] == frozen['work']
    assert http.post(url(project, '/reopen'), headers=OPERATOR, json=payload).status_code == 200
    tasks = http.get(f'/projects/{project}').json()['intents']
    queued = [i for i in tasks if i['work']['purpose'] == 'design' and i['work']['state'] == 'ready']
    assert len(queued) == 1
    feedback = queued[0]['work']['result']['revision_feedback']
    assert feedback['summary'] == 'Independent review' and feedback['verdict'] == 'request_changes'
    assert feedback['output']['verdict'] == 'request_changes'
    assert feedback['version'] == frozen['version'] and feedback['baseline_changed'] == baseline_changed
    status = http.get(url(project), headers=OPERATOR).json()
    assert status['review']['revision']['state'] == 'ready' and not status['readiness']['ready']
    run = claim(http, project, queued[0]['id']).json()
    assert agent(http, project, run, 'session', {'session': draft['session'], 'baseline': draft['baseline']}).status_code == 200
    response = agent(http, project, run, 'draft', {'version': draft['version'], 'stage': 'plan',
        'documents': draft['documents'], 'decisions': draft['decisions'] + ['Addressed each review finding'], 'work': [task]})
    assert response.status_code == 200, response.text
    finish(http, project, run, {'kind': 'design', 'summary': 'Revision saved'})
    again = freeze(http, project)
    assert again['candidate']['review'] == 'pending' and again['version'] > frozen['version']


def test_revision_rejects_a_stale_review_digest(http, repository):
    project, _, _, _ = ready(http, repository)
    frozen = freeze(http, project)
    response = http.post(url(project, '/reopen'), headers=OPERATOR, json={'version': frozen['version'], 'digest': 'wrong'})
    assert response.status_code == 409
    assert http.get(url(project), headers=OPERATOR).json()['draft']['candidate'] == frozen['candidate']


@pytest.mark.parametrize('fault', ['cycle', 'missing_ref', 'missing_dependency', 'no_checks'])
def test_frozen_native_plan_is_deterministically_validated(http, repository, fault):
    project, _, _, task = ready(http, repository)
    with db.get_conn() as conn:
        from cairn.server.design import load, save
        draft = load(conn, project)
        if fault == 'cycle':
            draft.work[0].dependencies = [task['key']]
        elif fault == 'missing_ref':
            draft.work[0].spec_refs = ['missing']
        elif fault == 'missing_dependency':
            draft.work[0].dependencies = ['unknown']
        else:
            draft.work[0].checks = []
            config = store.project(conn, project)
            config.checks = []
            store.save_project(conn, project, config)
        save(conn, project, draft)
    response = http.post(url(project, '/candidate'), headers=OPERATOR, json={'version': 1})
    assert response.status_code == 422, response.text


def test_agent_update_idempotence_and_version_fence(http, repository):
    project = create_design(http, repository)
    run = claim(http, project, send(http, project)).json()
    data = {'version': 0, 'documents': {'requirements': 'Feature'}}
    assert agent(http, project, run, 'draft', data).status_code == 200
    assert agent(http, project, run, 'draft', data).status_code == 200
    assert agent(http, project, run, 'draft', {**data, 'documents': {}}, 'stale').status_code == 409
    assert agent(http, project, {**run, 'token': 'wrong'}, 'text', {'text': 'No access'}).status_code == 403


def test_reference_sections_and_unique_legacy_file_ids_share_submit_readiness(http, repository):
    project, _, _, _ = ready(http, repository)
    from cairn.server.design import load, save
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.documents['prd-file'] = 'artifacts/design/product/product.md'
        source = repository / '.cairn/artifacts/design/product/product.md'
        source.parent.mkdir(parents=True)
        source.write_text('# Product\nFR-2: A confirmed behavior', encoding='utf-8')
        draft.work[0].spec_refs = ['requirements#FR-1', 'product#FR-2']
        save(conn, project, draft)
    assert http.get(url(project), headers=OPERATOR).json()['readiness']['ready']
    frozen = freeze(http, project)
    assert frozen['work'][0]['spec_refs'] == ['requirements#FR-1', 'prd-file#FR-2']
    assert 'FR-2: A confirmed behavior' in frozen['documents']['prd-file']
    assert frozen['document_sources']['prd-file'] == 'artifacts/design/product/product.md'


def test_document_preview_loads_body_and_freeze_keeps_an_immutable_snapshot(http, repository):
    project, _, _, _ = ready(http, repository)
    from cairn.server.design import load, save
    source = repository / '.cairn/artifacts/spec.md'
    source.parent.mkdir(parents=True)
    source.write_text('# Specification\nActual requirements', encoding='utf-8')
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.documents['requirements'] = 'artifacts/spec.md'
        save(conn, project, draft)
    state = http.get(url(project), headers=OPERATOR).json()
    assert 'Actual requirements' in state['document_views']['requirements']
    assert state['readiness']['ready']
    frozen = freeze(http, project)
    source.write_text('Changed after freeze', encoding='utf-8')
    state = http.get(url(project), headers=OPERATOR).json()
    assert state['document_views']['requirements'] == frozen['documents']['requirements']
    assert 'Changed after freeze' not in state['document_views']['requirements']


@pytest.mark.parametrize('path', ['artifacts/missing.md', 'artifacts/../../secret.md'])
def test_unreadable_document_references_block_readiness_without_exposing_files(http, repository, path):
    project, _, _, _ = ready(http, repository)
    from cairn.server.design import load, save
    (repository / 'secret.md').write_text('Private contents', encoding='utf-8')
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.documents['requirements'] = path
        save(conn, project, draft)
    state = http.get(url(project), headers=OPERATOR).json()
    assert not state['readiness']['ready'] and state['document_errors']
    assert 'Private contents' not in str(state)


@pytest.mark.parametrize('reference', ['unknown#FR-1', 'requirements#', 'product#FR-2'])
def test_invalid_and_ambiguous_references_are_reported_before_submission(http, repository, reference):
    project, _, _, _ = ready(http, repository)
    from cairn.server.design import load, save
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.documents.update({'one': 'artifacts/a/product.md', 'two': 'artifacts/b/product.md'})
        draft.work[0].spec_refs = [reference]
        save(conn, project, draft)
    status = http.get(url(project), headers=OPERATOR).json()['readiness']
    assert not status['ready']
    assert any('FEATURE-1' in issue and reference in issue for issue in status['issues'])
    rejected = http.post(url(project, '/candidate'), headers=OPERATOR, json={'version': 1})
    assert rejected.status_code == 422
    assert reference in rejected.json()['detail']


def test_free_text_answers_must_quote_new_user_input_not_agent_defaults(http, repository):
    project = create_design(http, repository)
    run = claim(http, project, send(http, project)).json()
    assert agent(http, project, run, 'questions', {'questions': [{'id': 'Q1', 'text': 'Who uses it?'}]}).status_code == 200
    assert agent(http, project, run, 'answers', {'answers': [{'id': 'Q1', 'quote': 'Design'}]}).status_code == 409
    finish(http, project, run, {'kind': 'design', 'summary': 'Waiting for answer'})
    next_run = claim(http, project, send(http, project, 'The internal team will use it', 'answer-text')).json()
    assert agent(http, project, next_run, 'answers', {'answers': [{'id': 'Q1', 'quote': 'External users'}]}, 'invented').status_code == 409
    assert agent(http, project, next_run, 'answers', {'answers': [{'id': 'Q1', 'quote': 'internal team'}]}).status_code == 200
    q = http.get(url(project), headers=OPERATOR).json()['draft']['questions'][0]
    assert q['answer'] == 'internal team' and q['answer_message_id']


def test_materials_freeze_before_review_and_materialize_without_manual_copy(http, repository, tmp_path):
    from cairn.server.design import load, save
    from cairn.dispatcher.runtime.design_artifacts import export_artifacts
    from cairn.server.design_materials import materialize_materials
    project, _, _, _ = ready(http, repository)
    source = tmp_path / 'artifacts'
    source.mkdir()
    (source / 'key-deployment.html').write_text('<h1>Approved prototype</h1>')
    export_artifacts(source, repository)
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.documents['ux'] = 'Use key-deployment.html in test/fixtures/ux/ before implementation.'
        save(conn, project, draft)
    draft = freeze(http, project)
    materials = draft['prepared_materials']
    assert len(materials) == 1 and materials[0]['target'] == 'test/fixtures/ux/key-deployment.html'
    assert not (repository / materials[0]['target']).exists()
    (repository / '.cairn/artifacts/key-deployment.html').write_text('Later edits')
    workspace = tmp_path / 'private-worktree'
    workspace.mkdir()
    materialize_materials(repository, workspace, materials)
    target = workspace / materials[0]['target']
    assert target.read_text() == '<h1>Approved prototype</h1>'
    target.write_text('User edits')
    with pytest.raises(ValueError, match='Preserving'):
        materialize_materials(repository, workspace, materials)
    assert target.read_text() == 'User edits'


def test_materials_readiness_reports_missing_source_and_rejects_escape(http, repository):
    from cairn.server.design import load, save
    project, _, _, _ = ready(http, repository)
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.materials = {'test/fixtures/ux/a.html':'artifacts/missing.html'}
        save(conn, project, draft)
    state = http.get(url(project), headers=OPERATOR).json()
    assert not state['readiness']['ready'] and any('missing.html' in s for s in state['readiness']['issues'])
    with db.get_conn() as conn:
        draft = load(conn, project)
        draft.materials = {'../outside.html':'artifacts/missing.html'}
        save(conn, project, draft)
    assert any('越界' in s for s in http.get(url(project), headers=OPERATOR).json()['readiness']['issues'])


def test_live_guidance_uses_one_writer_and_survives_result_boundary(http, repository):
    project = create_design(http, repository)
    iid = send(http, project)
    run = claim(http, project, iid).json()
    endpoint = url(project, '/runs/' + run['id'] + '/inputs')
    def delivery(action, ids=None):
        response = http.post(endpoint, headers=WORKER, json={'token':run['token'],'action':action,'message_ids':ids or []})
        assert response.status_code == 200, response.text
        return response.json()
    delivery('start')
    response = http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'guide-1','content':'Preserve scope'})
    assert response.status_code == 201 and response.json()['intent_id'] == iid
    guide = response.json()['id']
    assert len(http.get(f'/projects/{project}').json()['intents']) == 1
    batch = delivery('take')['items']
    assert [m['id'] for m in batch] == [guide]
    assert not delivery('take')['items'], 'A taken message must not be delivered twice'
    delivery('delivered',[guide])
    assert delivery('close')['closed']
    late = http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'guide-2','content':'Late guidance'}).json()
    assert late['intent_id'] != iid
    assert claim(http,project,late['intent_id']).status_code == 409, 'Do not spawn another writer beside a live session'
    finish(http,project,run,{'kind':'design','summary':'Saved'})
    assert claim(http,project,late['intent_id']).status_code == 200
    states = http.get(url(project),headers=OPERATOR).json()['deliveries']
    assert states[str(guide)] == 'delivered' and states[str(late['id'])] == 'queued'


def test_uncertain_guidance_is_not_silently_replayed_after_failure(http, repository):
    project = create_design(http,repository)
    iid = send(http,project)
    run = claim(http,project,iid).json()
    guide = http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'uncertain','content':'Direction'}).json()['id']
    endpoint = url(project,'/runs/'+run['id']+'/inputs')
    http.post(endpoint,headers=WORKER,json={'token':run['token'],'action':'start'})
    http.post(endpoint,headers=WORKER,json={'token':run['token'],'action':'take'})
    finish(http,project,run,{},outcome='failed')
    assert http.get(url(project),headers=OPERATOR).json()['deliveries'][str(guide)] == 'uncertain'
    send(http,project,mid='resume',content='Resume the saved design')
    with db.get_conn() as conn:
        row = conn.execute('SELECT intent_id,state FROM design_inputs WHERE message_id=?',(guide,)).fetchone()
        assert row['intent_id'] == iid and row['state'] == 'uncertain'


def test_actual_session_groups_do_not_merge_agents_by_worker_name(http, repository):
    project,_,_,_ = ready(http,repository)
    draft = freeze(http,project)
    review(http,project,draft,verdict='request_changes')
    data=http.get(f'/projects/{project}/development',headers=OPERATOR).json()
    assert len(data['sessions']) == 2
    assert sum(s['main'] for s in data['sessions']) == 1
    assert {s['session'] for s in data['sessions']} == {'author','independent'}


def test_revision_cannot_remove_confirmed_decision_without_new_user_answer(http,repository):
    project,_,_,task=ready(http,repository)
    frozen=freeze(http,project)
    review(http,project,frozen,verdict='request_changes')
    draft=http.post(url(project,'/reopen'),headers=OPERATOR,json={'version':frozen['version']}).json()
    iid=next(i['id'] for i in http.get(f'/projects/{project}').json()['intents'] if i['work']['purpose']=='design' and i['work']['state']=='ready')
    run=claim(http,project,iid).json()
    response=agent(http,project,run,'draft',{'version':draft['version'],'decisions':[]})
    assert response.status_code==409 and 'confirmed decision' in response.text
    response=agent(http,project,run,'questions',{'questions':[{'id':'change-decision','text':'Replace the prior decision?'}]})
    assert response.status_code==200
    answer=http.post(url(project,'/messages'),headers=OPERATOR,json={'id':'explicit-decision','content':'Replace the prior decision','answers':{'change-decision':'Yes, replace it'}})
    assert answer.status_code==201
    current=http.get(url(project),headers=OPERATOR).json()['draft']
    response=agent(http,project,run,'draft',{'version':current['version'],'decisions':['New explicitly approved decision'],
        'decision_changes':{'Preserve existing behavior':'change-decision'}},eid='changed-decision')
    assert response.status_code==200,response.text
