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
           {'design_baseline': draft['baseline'], 'checks_passed': evidence}, session=session)
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
    assert http.post(url(project, '/messages'), headers=OPERATOR, json={'id': 'other', 'content': 'Another'}).status_code == 409


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


def test_reopen_preserves_design_and_interrupt_fences_late_writes(http, repository):
    project, _, _, _ = ready(http, repository)
    draft = freeze(http, project)
    reopened = http.post(url(project, '/reopen'), headers=OPERATOR, json={'version': draft['version']})
    assert reopened.status_code == 200, reopened.text
    assert reopened.json()['documents'] == draft['documents'] and not reopened.json()['candidate']
    run = claim(http, project, send(http, project, 'Revise', 'revision')).json()
    assert http.post(url(project, '/interrupt'), headers=OPERATOR).status_code == 200
    assert agent(http, project, run, 'text', {'text': 'Late answer'}).status_code == 409
    finish(http, project, run, {'kind': 'design', 'summary': 'Cancelled'}, outcome='cancelled')
    send(http, project, 'Continue from saved draft', 'continue')


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
