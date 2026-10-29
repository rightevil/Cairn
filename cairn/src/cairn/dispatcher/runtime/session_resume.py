"""Resume only the same task checkout, candidate, role and runtime policy."""
import hashlib
import json
from pathlib import Path

from cairn.dispatcher.runtime.git_workspace import git


def session_identity(worker, intent, workspace):
    profile = {'type':worker.type, 'command':worker.command, 'model':worker.model,
        'routing':{k:v for k,v in worker.env.items() if k.endswith(('MODEL','BASE_URL'))}, 'policy':1}
    return {'worker':hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest(),
        'intent':intent.id,'revision':intent.work.revision,'design_revision':intent.work.design_revision,
        'workspace':str(workspace.resolve()), 'head':git(workspace,'rev-parse','HEAD')}


def compatible_session(runs_root, runs, current, identity):
    for run in runs:
        if run['id'] == current: continue
        if run['state'] in ('running','success','superseded'): return None
        path = runs_root/run['id']/'session-context.json'
        try: saved = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError): return None
        session = saved.pop('session',None)
        if session and saved == identity: return session
        # A changed latest context must not fall back to an even older chat.
        return None
    return None
