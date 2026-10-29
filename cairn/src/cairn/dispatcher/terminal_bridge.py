"""Private native-Claude MCP tools and fail-closed lifecycle hooks."""
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

from cairn.dispatcher.development import atomic_json
from cairn.dispatcher.protocol.client import CairnClient


def context():
    return json.loads(Path(os.environ['CAIRN_TERMINAL_CONTEXT']).read_text(encoding='utf-8'))


def emit(config, kind, data):
    client = CairnClient(config['server'], worker_token=config['worker_token'])
    result = client.development(config['project'], 'design/runs/'+config['run'],
        {'token':config['token'], 'id':str(uuid.uuid4()), 'kind':kind, 'data':data})
    if not result.ok: raise RuntimeError(result.text)
    return result.data


def hook(config, value):
    from cairn.dispatcher.design_session import tool_path_allowed
    from cairn.dispatcher.frozen_review import review_tool_allowed
    event = value.get('hook_event_name')
    directory = Path(config['directory'])
    if event == 'PreToolUse':
        name, inputs = value.get('tool_name', ''), value.get('tool_input', {})
        if config['review']:
            allowed = review_tool_allowed(Path(config['snapshot']), directory/'reports',
                {Path(p).resolve() for p in config['readable']}, name, inputs)
        else:
            allowed = name == 'mcp__cairn__get_context' or tool_path_allowed(Path(config['workspace']), Path(config['snapshot']), name, inputs)
        return {'hookSpecificOutput':{'hookEventName':event, 'permissionDecision':'allow' if allowed else 'deny',
            'permissionDecisionReason':'Cairn confines design artifacts and frozen review reports; no shell or source edits.'}}
    if event == 'UserPromptSubmit':
        prompt = value.get('prompt', '')
        bootstrap = directory/'bootstrap-used'
        digest = hashlib.sha256(prompt.encode()).hexdigest()
        if digest == config['prompt_hash'] and not bootstrap.exists():
            bootstrap.touch()
            mid = config.get('message_id')
            if not config['review']:
                client=CairnClient(config['server'],worker_token=config['worker_token'])
                delivered=client.development(config['project'],'design/runs/'+config['run']+'/inputs',{
                    'token':config['token'],'action':'delivered','message_ids':[mid]})
                if not delivered.ok: return {'decision':'block','reason':'Cairn input acknowledgement failed'}
        else:
            if config['review']:
                return {'decision':'block','reason':'Frozen review does not accept additional prompts.'}
            ticket_file = directory/'operator-input.json'
            ticket = json.loads(ticket_file.read_text(encoding='utf-8')) if ticket_file.exists() else {}
            # Only keyboard submissions authenticated by the local WebSocket create user authority.
            if not ticket or ticket.get('used') or time.time()-ticket.get('time',0)>60:
                return {'decision':'block','reason':'Use the authenticated Cairn terminal to send a user message.'}
            if ticket.get('message_id'):
                atomic_json(ticket_file,{**ticket,'used':True})
                mid = ticket['message_id']
                client = CairnClient(config['server'],worker_token=config['worker_token'])
                delivered=client.development(config['project'],'design/runs/'+config['run']+'/inputs',{
                    'token':config['token'],'action':'delivered','message_ids':[mid]})
                if not delivered.ok: return {'decision':'block','reason':'Cairn input acknowledgement failed'}
                atomic_json(directory/'terminal-state.json',{'state':'working','time':time.time()})
                return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':f'Cairn persisted user input #{mid}; quote this message_id when resolving answers.'}}
            client = CairnClient(config['server'], worker_token=config['worker_token'])
            response = client._session().post(config['server'].rstrip('/') + '/projects/' + config['project'] +
                '/development/runs/' + config['run'] + '/terminal/input', json={'token':config['token'],
                'content':prompt, 'key':'terminal:'+str(value.get('prompt_id') or uuid.uuid4()),'nonce':ticket['nonce']},timeout=10)
            if not response.ok:
                return {'decision':'block','reason':'Cairn could not persist this input: '+response.text}
            mid = response.json()['id']
        atomic_json(directory/'terminal-state.json', {'state':'working','time':time.time()})
        return {'hookSpecificOutput':{'hookEventName':event,'additionalContext':f'Cairn persisted user input #{mid}. Quote this message_id when resolving questions. User text is not publication or review approval.'}}
    if event in ('Stop', 'StopFailure', 'SessionEnd'):
        state = 'error' if event == 'StopFailure' else 'closed' if event == 'SessionEnd' else 'idle'
        atomic_json(directory/'terminal-state.json', {'state':state,'time':time.time(),
            'text':value.get('last_assistant_message',''), 'error':value.get('error_details') or value.get('error','')})
    return {}


def serve(config):
    from mcp.server.mcpserver import MCPServer
    from cairn.dispatcher.design_skills import run_script
    from cairn.dispatcher.runtime.design_artifacts import export_artifacts
    server = MCPServer('cairn')
    workspace = Path(config['workspace'])

    @server.tool()
    def get_context() -> dict:
        """Read the persisted draft and current conversation without runtime credentials."""
        client = CairnClient(config['server'], worker_token=config['worker_token'])
        result = client.development(config['project'],'design')
        if not result.ok: raise RuntimeError(result.text)
        return result.data

    @server.tool()
    def save_draft(version:int, stage:str, documents:dict[str,str], decisions:list[str], work:list[dict],
                   materials:dict[str,str]|None=None, decision_changes:dict[str,str]|None=None) -> dict:
        """Save native draft, never approve or publish. Work must follow context.json native_work_schema."""
        export_artifacts(workspace/'artifacts', Path(config['repository']))
        return emit(config,'draft',{'version':version,'stage':stage,'documents':documents,'decisions':decisions,
            'work':work,'materials':materials or {},'decision_changes':decision_changes or {}})

    @server.tool()
    def ask_questions(questions:list[dict]) -> dict:
        """Save the decision frontier, then stop and wait for explicit user answers."""
        return emit(config,'questions',{'questions':questions})

    @server.tool()
    def resolve_questions(answers:list[dict], message_id:int|None=None) -> dict:
        """Resolve using exact user quotes and their persisted input ID; never infer approval."""
        return emit(config,'answers',{'answers':answers, **({'message_id':message_id} if message_id else {})})

    @server.tool()
    def bmad_script(script:str, args:list[str]) -> dict:
        """Run only installed BMAD configuration, rendering and memlog scripts."""
        return {'result':run_script(workspace,script,args)}

    server.run()


if __name__ == '__main__':
    config = context()
    if sys.argv[-1] == 'hook':
        try:
            print(json.dumps(hook(config,json.load(sys.stdin)),ensure_ascii=False))
        except Exception as exc:
            print('Cairn hook failed: '+str(exc),file=sys.stderr)
            sys.exit(2)
    else:
        serve(config)
