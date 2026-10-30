"""Dispatcher-owned native Claude TUI. HTTP transports bytes, never launches a shell."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import queue
import threading
import time
import uuid
from cairn.runtime_entry import module_command

from cairn.dispatcher.development import atomic_json, executable_argv
from cairn.dispatcher.runtime.pty_process import ConsoleProcess
from cairn.dispatcher.runtime.process_identity import process_identity


async def run_console(dispatcher, project, worker, run, directory, snapshot, cancellation, *,
                      workspace, prompt, session, message_id=None, readable=(), review=False):
    pid = project.project.id
    private = {'server':dispatcher.config.server,'worker_token':dispatcher.client._worker_token,
        'project':pid,'run':run['id'],'token':run['token'],'directory':str(directory),
        'workspace':str(workspace),'snapshot':str(snapshot),'repository':project.project.development.repository,
        'review':review,'session':session,'readable':[str(p) for p in readable],'message_id':message_id,
        'prompt_hash':hashlib.sha256(prompt.encode()).hexdigest() if prompt else None}
    atomic_json(directory/'terminal-private.json',private)
    hook_argv = module_command('cairn.dispatcher.terminal_bridge', 'hook')
    hook = {'type':'command','command':hook_argv[0],'args':hook_argv[1:],
            'timeout':15,'onFailure':'block'}
    settings = {'theme':'light','skipDangerousModePermissionPrompt':True,
        'hooks':{name:[{'hooks':[hook]}] for name in ('PreToolUse','UserPromptSubmit','Stop','StopFailure','SessionEnd')}}
    atomic_json(directory/'terminal-settings.json',settings)
    server_argv = module_command('cairn.dispatcher.terminal_bridge', 'serve')
    servers = {'cairn':{'command':server_argv[0], 'args':server_argv[1:]}}
    atomic_json(directory/'terminal-mcp.json',{'mcpServers':servers})
    argv = executable_argv(worker,['claude'])
    if len(argv)%2 != 1 or any(a not in ('--model','--fallback-model','--effort') for a in argv[1::2]):
        raise RuntimeError('Design terminal requires native Claude with only model/effort command options')
    argv += ['--settings',str(directory/'terminal-settings.json'),'--setting-sources','',
        '--strict-mcp-config','--mcp-config',str(directory/'terminal-mcp.json'),
        '--dangerously-skip-permissions','--tools','Read,Glob,Grep,Write,Edit'+('' if review else ',Skill,WebSearch,WebFetch'),
        '--session-id',session]
    if worker.model and '--model' not in argv: argv += ['--model',worker.model]
    system = directory/'terminal-system.md'
    system.write_text(('Frozen independent review. Only read the supplied snapshot, evidence and context. Write reports '
        'only under '+str(directory/'reports')+'. No implementation edits, shell, copying, publication or product decisions. '
        'Register the verdict with mcp__cairn__submit_review; then give a readable Chinese report. '
        'Do not print internal schemas or claim future tests ran.' if review else
        (workspace/'.claude/skills/cairn-design/SKILL.md').read_text(encoding='utf-8')),encoding='utf-8')
    argv += ['--append-system-prompt-file',str(system)]
    if not review:
        draft = json.loads((workspace/'context.json').read_text(encoding='utf-8'))['draft']
        if draft.get('session'):
            argv[argv.index('--session-id')] = '--resume'
    if prompt: argv.append(prompt)
    env = {**worker.env,'CAIRN_TERMINAL_CONTEXT':str(directory/'terminal-private.json'),
        'CLAUDE_CODE_MAX_RETRIES':worker.env.get('CLAUDE_CODE_MAX_RETRIES',os.environ.get('CLAUDE_CODE_MAX_RETRIES','0')),
        'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
    if any(worker.env.get(k) or os.environ.get(k) for k in ('ANTHROPIC_AUTH_TOKEN','ANTHROPIC_API_KEY')):
        # Dedicated native preferences avoid changing the operator's global Claude setup.
        profile = directory.parent/'claude-profiles'/session
        profile.mkdir(parents=True,exist_ok=True)
        prefs = profile/'.claude.json'
        if not prefs.exists(): atomic_json(prefs,{'hasCompletedOnboarding':True,'theme':'light',
            'projects':{(workspace if not review else snapshot).resolve().as_posix():{'hasTrustDialogAccepted':True,'projectOnboardingSeenCount':1}}})
        env['CLAUDE_CONFIG_DIR']=str(profile)
    private['profile_root'] = env.get('CLAUDE_CONFIG_DIR') or os.environ.get('CLAUDE_CONFIG_DIR') or str(Path.home()/'.claude')
    atomic_json(directory/'terminal-private.json',private)
    for name in ('stdout.log','stderr.log','terminal.log'): (directory/name).touch()
    (directory/'reports').mkdir(exist_ok=True)
    atomic_json(directory/'terminal-state.json',{'state':'starting','time':time.time()})
    console = ConsoleProcess(argv,workspace if not review else snapshot,env)
    chunks = queue.Queue(maxsize=128)
    stop_read = threading.Event()
    def enqueue(chunk):
        while not stop_read.is_set():
            try: chunks.put(chunk, timeout=.1); return
            except queue.Full: pass
    def read():
        try:
            while console.alive():
                chunk = console.read()
                if not chunk: break
                enqueue(chunk)
        except (EOFError,OSError): pass
        finally: enqueue(None)
    reader = threading.Thread(target=read,daemon=True)
    try:
        identity = process_identity(console.pid)
        atomic_json(directory/'process.json',{'pid':console.pid,'identity':identity})
        current = await asyncio.to_thread(dispatcher.call,pid,f"runs/{run['id']}/heartbeat",{
            'token':run['token'],'pid':console.pid,'process_identity':identity,'session':session,
            'streams':{name:str(directory/(name+'.log')) for name in ('stdout','stderr','terminal')}})
        if current.get('cancel'): raise RuntimeError('Design lease closed before terminal attachment')
        reader.start()
        offset = 0
        last_text = ''
        queued = []
        while not cancellation.is_cancelled:
            batch = []
            for _ in range(32):
                if chunks.empty(): break
                chunk = chunks.get_nowait()
                if chunk is None: break
                batch.append(chunk)
            if batch:
                text = ''.join(batch)
                with (directory/'terminal.log').open('a',encoding='utf-8',newline='') as log: log.write(text)
                console.answer_queries(text)
            state = json.loads((directory/'terminal-state.json').read_text(encoding='utf-8'))
            if not prompt and state['state'] == 'starting' and console.ready:
                state = {'state':'idle','time':time.time()}
                atomic_json(directory/'terminal-state.json',state)
            if state['state']=='error':
                raise RuntimeError('Claude upstream request failed: '+str(state.get('error')).replace('_',' ')+' '+state.get('text',''))
            if state['state']=='idle' and state.get('text') != last_text:
                last_text = state.get('text','')
                if review:
                    result_file = directory/'review-result.json'
                    return (result_file.read_text(encoding='utf-8') if result_file.exists() else last_text),session
                if last_text:
                    from cairn.dispatcher.terminal_bridge import emit
                    await asyncio.to_thread(emit,private,'text',{'block':run['id']+':'+str(state['time']),'text':last_text})
            control = directory/'terminal-input.jsonl'
            if control.exists():
                with control.open(encoding='utf-8') as stream:
                    stream.seek(offset)
                    for line in stream:
                        value=json.loads(line)
                        if value['type']=='input': console.write(value['data'])
                        elif value['type']=='resize': console.resize(value['rows'],value['cols'])
                        elif value['type']=='close':
                            if state['state']!='idle': raise RuntimeError('Design became busy before close')
                            return last_text,session
                        if value['type'] in ('input','resize') and value.get('created_at'):
                            from cairn.dispatcher.runtime.terminal_metrics import record
                            record(directory,value['type']+'_to_pty',(time.time()-value['created_at'])*1000)
                    offset=stream.tell()
            if not review and state['state']=='idle':
                batch=await asyncio.to_thread(dispatcher.call,pid,f"design/runs/{run['id']}/inputs",{
                    'token':run['token'],'action':'take','message_ids':[]})
                queued.extend(batch.get('items',[]))
                if queued:
                    item=queued.pop(0)
                    # Already authenticated, persisted card answers; the hook still checks an input ticket.
                    atomic_json(directory/'operator-input.json',{'nonce':str(uuid.uuid4()),'time':time.time(),'message_id':item['id']})
                    atomic_json(directory/'terminal-state.json',{'state':'working','time':time.time()})
                    console.write('\x1b[200~'+item['data']['content']+'\x1b[201~\r')
            if not console.alive(): raise RuntimeError('Native Claude terminal exited before a completed design turn')
            await asyncio.sleep(.05)
        raise RuntimeError('Design terminal interrupted; persisted draft and inputs retained')
    finally:
        stop_read.set()
        console.close()
        if reader.is_alive(): reader.join(timeout=2)
        from cairn.dispatcher.runtime.terminal_history import archive
        try: await asyncio.to_thread(archive,private,{'session_id':session})
        except (OSError,ValueError) as exc:
            logging.getLogger(__name__).warning('Conversation archive unavailable run=%s error=%s',run['id'],exc)
