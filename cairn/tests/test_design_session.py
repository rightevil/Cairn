import asyncio
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk as sdk
import pytest

from cairn.dispatcher import design_session, design_skills
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server.development_models import PlannedWork
from test_development_runtime import repository


def test_design_artifact_export_updates_owned_files_and_preserves_user_edits(tmp_path, repository):
    from cairn.dispatcher.runtime.design_artifacts import export_artifacts
    from cairn.dispatcher.runtime.git_workspace import git
    source = tmp_path / 'artifacts'
    source.mkdir()
    spec = source / 'spec.md'
    spec.write_text('First specification', encoding='utf-8')
    root = export_artifacts(source, repository)
    assert (root / 'spec.md').read_text() == 'First specification'
    assert not git(repository, 'status', '--porcelain')
    spec.write_text('Second specification', encoding='utf-8')
    export_artifacts(source, repository)
    assert (root / 'spec.md').read_text() == 'Second specification'
    (root / 'spec.md').write_text('User edits', encoding='utf-8')
    with pytest.raises(RuntimeError, match='preserving'):
        export_artifacts(source, repository)
    assert (root / 'spec.md').read_text() == 'User edits'


def test_design_tools_cannot_write_repository_or_read_unrelated_files(tmp_path):
    workspace, snapshot = tmp_path / 'design', tmp_path / 'snapshot'
    allowed = design_session.tool_path_allowed
    assert allowed(workspace, snapshot, 'Write', {'file_path': str(workspace / 'artifacts/spec.md')})
    assert allowed(workspace, snapshot, 'Edit', {'file_path': 'artifacts/spec.md'})
    assert allowed(workspace, snapshot, 'Read', {'file_path': str(snapshot / 'app.py')})
    assert not allowed(workspace, snapshot, 'Write', {'file_path': str(snapshot / 'app.py')})
    assert not allowed(workspace, snapshot, 'Write', {'file_path': 'artifacts/../../outside'})
    assert not allowed(workspace, snapshot, 'Read', {'file_path': str(tmp_path / 'secret')})
    assert not allowed(workspace, snapshot, 'Bash', {'command': 'anything'})
    assert not allowed(workspace, snapshot, 'AskUserQuestion', {})
    assert allowed(workspace, snapshot, 'Skill', {'skill': 'grilling'})


def test_bmad_scripts_are_argv_only_and_workspace_confined(tmp_path, monkeypatch):
    captured = []
    monkeypatch.setattr(design_skills, 'command', lambda argv, cwd: captured.append(argv) or SimpleNamespace(stdout='{}'))
    assert design_skills.run_script(tmp_path, 'resolve_config.py', ['--project-root', str(tmp_path)]) == '{}'
    assert any(arg.endswith('resolve_config.py') for arg in captured[0])
    with pytest.raises(ValueError):
        design_skills.run_script(tmp_path, 'setup.py', [])
    with pytest.raises(ValueError):
        design_skills.run_script(tmp_path, 'memlog.py', ['append', '--workspace', str(tmp_path.parent)])
    with pytest.raises(ValueError):
        design_skills.run_script(tmp_path, 'memlog.py', ['--workspace=' + str(tmp_path.parent)])
    with pytest.raises(ValueError):
        design_skills.run_script(tmp_path, 'render_skill.py', ['--skill', str(tmp_path / 'artifacts/forged-skill')])
    with pytest.raises(ValueError):
        design_skills.run_script(tmp_path, 'render_skill.py', ['--overrides', str(tmp_path / 'artifacts/forged.toml')])


def test_resumed_sessions_refresh_host_rules_without_reinstalling_pinned_skills(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(design_skills, 'git', lambda *args: 'pinned')
    (tmp_path / 'skill-versions.json').write_text(json.dumps({'bmad': 'pinned', 'grilling': 'pinned'}))
    skill = tmp_path / '.claude/skills/cairn-design/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Old rules')
    design_skills.prepare(tmp_path, SimpleNamespace(bmad_repository='unused', grilling_repository='unused'))
    assert '设计者自检' in skill.read_text(encoding='utf-8')
    assert '冻结当前阶段并送独立评审' in skill.read_text(encoding='utf-8')
    assert json.loads((tmp_path / 'skill-versions.json').read_text())['bmad'] == 'pinned'


@pytest.mark.parametrize('resume,content', [(None, 'Design the feature'), ('existing-session', '/bmad-architecture')])
@pytest.mark.parametrize('text_index,partial,turns,bundled', [(0, 'Question', 1, False), (1, 'Question', 1, False), (3, 'Quest', 1, False), (1, 'Question', 2, False), (1, 'Fir', 1, True)])
def test_sdk_turn_automatically_orchestrates_and_resumes_with_persisted_questions(tmp_path, monkeypatch, resume, content, text_index, partial, turns, bundled):
    workspace = tmp_path / 'project' / 'design-session'
    skill = workspace / '.claude/skills/cairn-design/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Invoke the real BMAD and grilling Skills.')
    directory = tmp_path / 'runs' / 'run'
    directory.mkdir(parents=True)
    captured, events = {}, []
    monkeypatch.setattr(design_session, 'prepare', lambda *args: None)
    monkeypatch.setattr(design_session, 'export_artifacts', lambda *args: None)
    monkeypatch.setattr(design_session, 'git', lambda *args: 'a' * 40)
    from cairn.dispatcher import development
    monkeypatch.setattr(development, 'executable_argv', lambda *args: ['claude.exe'])

    def call(pid, action, payload=None):
        if action.startswith('design?input_id='):
            return {'draft': {'session': resume, 'baseline': '', 'version': 0}, 'messages': [
                {'id': 1, 'kind': 'user', 'data': {'content': content, 'answers': {'Q1': 'Explicit choice'}}}]}
        if action.endswith('/inputs'):
            return {'items': [], 'closed': True}
        events.append(payload)
        return {'version': 1}

    monkeypatch.setattr(sdk, 'create_sdk_mcp_server', lambda name, tools: {'type': 'sdk', 'name': name, 'tools': tools})

    class Client:
        def __init__(self, options, transport):
            captured['options'] = options
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def get_server_info(self):
            return {'commands': [{'name': 'bmad-prd'}]}
        async def query(self, prompt):
            captured['prompt'] = prompt
        async def receive_messages(self):
            tools = {t.name: t for t in captured['options'].mcp_servers['cairn']['tools']}
            await tools['ask_questions'].handler({'questions': [{'id': 'Q2', 'text': 'Next decision'}]})
            guard = captured['options'].hooks['PreToolUse'][0].hooks[0]
            captured['deny'] = await guard({'tool_name': 'Bash', 'tool_input': {'command': 'exit'}}, '', {})
            await guard({'tool_name': 'Skill', 'tool_input': {'skill': 'bmad-prd'}}, '', {})
            for number in range(turns):
                yield sdk.StreamEvent('1', 'test', {'type': 'message_start'})
                yield sdk.StreamEvent('2', 'test', {'type': 'content_block_start', 'index': text_index, 'content_block': {'type': 'text'}})
                yield sdk.StreamEvent('3', 'test', {'type': 'content_block_delta', 'index': text_index, 'delta': {'type': 'text_delta', 'text': partial}})
                if bundled:
                    yield sdk.StreamEvent('4', 'test', {'type': 'content_block_start', 'index': 3, 'content_block': {'type': 'text'}})
                    yield sdk.StreamEvent('5', 'test', {'type': 'content_block_delta', 'index': 3, 'delta': {'type': 'text_delta', 'text': 'Sec'}})
                    yield sdk.AssistantMessage([sdk.TextBlock('First'), sdk.TextBlock('Second')], 'test')
                else:
                    yield sdk.AssistantMessage([sdk.TextBlock('Question')], 'test')
            yield sdk.ResultMessage('success', 1, 1, False, 1, resume or 'new-session', result='Done')
        async def interrupt(self):
            captured['interrupt'] = True

    monkeypatch.setattr(sdk, 'ClaudeSDKClient', Client)
    project = SimpleNamespace(project=SimpleNamespace(id='project', development=SimpleNamespace(repository=str(tmp_path / 'repository'))), hints=[])
    dispatcher = SimpleNamespace(directory=tmp_path, config=SimpleNamespace(design=None), call=call)
    worker = SimpleNamespace(env={'ANTHROPIC_MODEL': 'strong'}, model=None)
    intent = SimpleNamespace(work=SimpleNamespace(result={'message_id': 1, 'revision_feedback': {'summary': 'Fix the write conflict', 'version': 97}}))
    output, session = asyncio.run(design_session.run_turn(dispatcher, project, intent, worker,
        {'id': 'run', 'token': 'lease'}, directory, tmp_path / 'snapshot', TaskCancellation()))
    assert output['kind'] == 'design'
    options = captured['options']
    assert options.resume == resume and options.setting_sources == ['project']
    assert options.model == 'strong' and 'Skill' in options.tools and 'Bash' not in options.tools
    assert captured['prompt'].startswith(content if content.startswith('/') else '/cairn-design')
    assert captured['deny']['hookSpecificOutput']['permissionDecision'] == 'deny'
    text_events = [e['data'] for e in events if e['kind'] == 'text']
    assert len(text_events) == (4 if bundled else turns * (1 if partial == 'Question' else 2))
    rendered = {}
    for event in text_events:
        key = event['block']
        rendered[key] = event['text'] if event.get('mode') == 'snapshot' else rendered.get(key, '') + event['text']
    assert list(rendered.values()) == (['First', 'Second'] if bundled else ['Question'] * turns)
    assert any(e['kind'] == 'questions' for e in events)
    assert any(e['kind'] == 'activity' and e['data']['skill'] == 'bmad-prd' for e in events)
    assert 'Explicit choice' in (workspace / 'context.json').read_text()
    assert 'Fix the write conflict' in (workspace / 'context.json').read_text()
    assert session == resume if resume else bool(session)


def test_frozen_review_tools_enforce_artifact_only_writes(tmp_path):
    from cairn.dispatcher.frozen_review import review_tool_allowed as allowed
    snapshot, reports = tmp_path / 'snapshot', tmp_path / 'run/reports'
    context = (tmp_path / 'run/context.json').resolve()
    assert allowed(snapshot,reports,{context},'Read',{'file_path':str(context)})
    assert allowed(snapshot,reports,{context},'Read',{'file_path':str(snapshot / 'app.py')})
    assert allowed(snapshot,reports,{context},'Write',{'file_path':str(reports / 'proposed.patch')})
    assert not allowed(snapshot,reports,{context},'Edit',{'file_path':str(snapshot / 'app.py')})
    assert not allowed(snapshot,reports,{context},'Write',{'file_path':str(context)})
    assert not allowed(snapshot,reports,{context},'Write',{'file_path':str(reports / '../context.json')})
    assert not allowed(snapshot,reports,{context},'Read',{'file_path':str(tmp_path / 'design-session/artifacts/spec.md')})
    for tool in ['Bash','Agent','Skill','mcp__cairn__save_draft']:
        assert not allowed(snapshot,reports,{context},tool,{})
