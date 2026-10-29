"""Persist readable messages separately from full-screen PTY repaint bytes."""
import json
from pathlib import Path
import uuid


def messages(path, session):
    result = []
    with path.open(encoding='utf-8',errors='replace') as stream:
        for index, line in enumerate(stream):
            try: item = json.loads(line)
            except ValueError: continue
            if not isinstance(item,dict): continue
            if item.get('sessionId', session) != session or item.get('isSidechain'): continue
            role = item.get('type')
            if role not in ('user', 'assistant'): continue
            message = item.get('message') or {}
            if not isinstance(message,dict): continue
            content = message.get('content', [])
            blocks = [{'type':'text','text':content}] if isinstance(content,str) else content if isinstance(content,list) else []
            text = []
            for block in blocks if isinstance(blocks,list) else []:
                if not isinstance(block,dict): continue
                if block.get('type') == 'text': text.append(str(block.get('text','')))
                elif block.get('type') == 'tool_use':
                    args = block.get('input') if isinstance(block.get('input'),dict) else {}
                    target = next((args.get(k) for k in ('file_path','path','command','pattern','skill','url') if args.get(k)), '')
                    text.append('[工具] ' + str(block.get('name','')) + (' · ' + str(target) if target else ''))
                elif block.get('type') == 'tool_result':
                    values = block.get('content', '')
                    if isinstance(values,list): values = '\n'.join(v.get('text','[非文本附件]') for v in values if isinstance(v,dict))
                    text.append('[工具结果]\n' + str(values))
            body = '\n'.join(text).strip()
            label = '系统' if item.get('isMeta') else '工具' if any(isinstance(b,dict) and b.get('type') == 'tool_result' for b in blocks) else '你' if role == 'user' else 'Agent'
            if body: result.append({'id':str(item.get('uuid') or index), 'role':label, 'text':body})
    return result


def source(directory, session, profile=None):
    try: uuid.UUID(session)
    except (ValueError, TypeError, AttributeError): return None
    roots = [directory.parent/'claude-profiles'/session, Path.home()/'.claude']
    if profile: roots.insert(0,Path(profile))
    for root in roots:
        projects = (root/'projects').resolve()
        for path in projects.glob('*/'+session+'.jsonl'):
            resolved = path.resolve()
            if resolved.is_relative_to(projects) and resolved.is_file(): return resolved
    return None


def archive(config, event):
    from cairn.dispatcher.development import atomic_json
    directory = Path(config['directory'])
    session = config.get('session') or event.get('session_id')
    path = source(directory,session,config.get('profile_root'))
    if path:
        atomic_json(directory/'conversation.json', {'session':session,'entries':messages(path,session)})
