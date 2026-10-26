import asyncio
from pathlib import Path
from types import SimpleNamespace

import claude_agent_sdk as sdk
import pytest

from cairn.dispatcher import design_session, design_skills
from cairn.dispatcher.runtime.cancellation import TaskCancellation
from cairn.server.development_models import PlannedWork


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


@pytest.mark.parametrize('resume,content', [(None, 'Design the feature'), ('existing-session', '/bmad-architecture')])
def test_sdk_turn_automatically_orchestrates_and_resumes_with_persisted_questions(tmp_path, monkeypatch, resume, content):
    workspace = tmp_path / 'project' / 'design-session'
    skill = workspace / '.claude/skills/cairn-design/SKILL.md'
    skill.parent.mkdir(parents=True)
    skill.write_text('Invoke the real BMAD and grilling Skills.')
    directory = tmp_path / 'runs' / 'run'
    directory.mkdir(parents=True)
    captured, events = {}, []
    monkeypatch.setattr(design_session, 'prepare', lambda *args: None)
    monkeypatch.setattr(design_session, 'git', lambda *args: 'a' * 40)
    from cairn.dispatcher import development
    monkeypatch.setattr(development, 'executable_argv', lambda *args: ['claude.exe'])

    def call(pid, action, payload=None):
        if action.startswith('design?input_id='):
            return {'draft': {'session': resume, 'baseline': '', 'version': 0}, 'messages': [
                {'id': 1, 'kind': 'user', 'data': {'content': content, 'answers': {'Q1': 'Explicit choice'}}}]}
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
        async def receive_response(self):
            tools = {t.name: t for t in captured['options'].mcp_servers['cairn']['tools']}
            await tools['ask_questions'].handler({'questions': [{'id': 'Q2', 'text': 'Next decision'}]})
            guard = captured['options'].hooks['PreToolUse'][0].hooks[0]
            captured['deny'] = await guard({'tool_name': 'Bash', 'tool_input': {'command': 'exit'}}, '', {})
            await guard({'tool_name': 'Skill', 'tool_input': {'skill': 'bmad-prd'}}, '', {})
            yield sdk.StreamEvent('1', 'test', {'type': 'message_start'})
            yield sdk.StreamEvent('2', 'test', {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': 'Question'}})
            yield sdk.AssistantMessage([sdk.TextBlock('Question')], 'test')
            yield sdk.ResultMessage('success', 1, 1, False, 1, resume or 'new-session', result='Done')
        async def interrupt(self):
            captured['interrupt'] = True

    monkeypatch.setattr(sdk, 'ClaudeSDKClient', Client)
    project = SimpleNamespace(project=SimpleNamespace(id='project'), hints=[])
    dispatcher = SimpleNamespace(directory=tmp_path, config=SimpleNamespace(design=None), call=call)
    worker = SimpleNamespace(env={'ANTHROPIC_MODEL': 'strong'}, model=None)
    intent = SimpleNamespace(work=SimpleNamespace(result={'message_id': 1}))
    output, session = asyncio.run(design_session.run_turn(dispatcher, project, intent, worker,
        {'id': 'run', 'token': 'lease'}, directory, tmp_path / 'snapshot', TaskCancellation()))
    assert output['kind'] == 'design'
    options = captured['options']
    assert options.resume == resume and options.setting_sources == ['project']
    assert options.model == 'strong' and 'Skill' in options.tools and 'Bash' not in options.tools
    assert captured['prompt'].startswith(content if content.startswith('/') else '/cairn-design')
    assert captured['deny']['hookSpecificOutput']['permissionDecision'] == 'deny'
    assert len([e for e in events if e['kind'] == 'text']) == 1
    assert any(e['kind'] == 'questions' for e in events)
    assert any(e['kind'] == 'activity' and e['data']['skill'] == 'bmad-prd' for e in events)
    assert 'Explicit choice' in (workspace / 'context.json').read_text()
    assert session == resume if resume else bool(session)
