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


@pytest.mark.parametrize('resume', [None, 'existing-session'])
def test_native_turn_prepares_authoritative_context_and_resumes(tmp_path, monkeypatch, resume):
    from cairn.dispatcher import design_terminal
    captured = {}
    monkeypatch.setattr(design_session, 'prepare', lambda *args: None)
    monkeypatch.setattr(design_session, 'export_artifacts', lambda *args: None)
    monkeypatch.setattr(design_session, 'git', lambda *args: 'a'*40)
    async def console(*args, **kwargs):
        captured.update(kwargs)
        return 'Saved', kwargs['session']
    monkeypatch.setattr(design_terminal,'run_console',console)
    def call(pid, action, payload=None):
        return {'draft':{'session':resume,'baseline':''}, 'messages':[{'id':1,'kind':'user','data':{'content':'Explicit choice'}}]} if action.startswith('design?') else {}
    project=SimpleNamespace(project=SimpleNamespace(id='project',development=SimpleNamespace(repository=str(tmp_path/'repository'))),hints=[])
    dispatcher=SimpleNamespace(directory=tmp_path,config=SimpleNamespace(design=None),call=call)
    directory=tmp_path/'run';directory.mkdir()
    result,session=asyncio.run(design_session.run_turn(dispatcher,project,SimpleNamespace(work=SimpleNamespace(result={'message_id':1,'revision_feedback':{'summary':'Fix conflict'}})),
        None,{'id':'run','token':'lease'},directory,tmp_path/'snapshot',TaskCancellation()))
    assert result['kind']=='design' and (session==resume if resume else bool(session))
    assert captured['message_id']==1 and 'Explicit choice' in captured['prompt']
    saved=(captured['workspace']/'context.json').read_text()
    assert 'Fix conflict' in saved and 'native_work_schema' in saved


def test_native_hooks_fail_closed_and_require_operator_input(tmp_path,monkeypatch):
    import hashlib
    from cairn.dispatcher.terminal_bridge import hook
    monkeypatch.setattr('cairn.dispatcher.terminal_bridge.CairnClient',lambda *a,**kw: SimpleNamespace(development=lambda *a:SimpleNamespace(ok=True)))
    config={'review':False,'workspace':str(tmp_path/'workspace'),'snapshot':str(tmp_path/'snapshot'),
        'directory':str(tmp_path),'prompt_hash':hashlib.sha256(b'first').hexdigest(),'message_id':1,
        'server':'http://localhost','worker_token':'local','project':'project','run':'run','token':'lease'}
    denied=hook(config,{'hook_event_name':'PreToolUse','tool_name':'Bash','tool_input':{'command':'echo unsafe'}})
    assert denied['hookSpecificOutput']['permissionDecision']=='deny'
    initial=hook(config,{'hook_event_name':'UserPromptSubmit','prompt':'first'})
    assert '#1' in initial['hookSpecificOutput']['additionalContext']
    assert hook(config,{'hook_event_name':'UserPromptSubmit','prompt':'forged'})['decision']=='block'
    hook(config,{'hook_event_name':'Stop','last_assistant_message':'Done'})
    import json
    assert json.loads((tmp_path/'terminal-state.json').read_text())['state']=='idle'
    review={**config,'review':True,'readable':[]}
    assert hook(review,{'hook_event_name':'UserPromptSubmit','prompt':'change scope'})['decision']=='block'


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
