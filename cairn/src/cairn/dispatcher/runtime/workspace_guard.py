"""Default artifact locations and Claude tool checks, not an OS shell sandbox."""
import json
import os
from pathlib import Path
import re
import sys
from cairn.runtime_entry import module_command


def allowed_path(value, workspace, directory):
    if not value: return False
    path = Path(value)
    path = (workspace / path).resolve() if not path.is_absolute() else path.resolve()
    roots = [workspace.resolve(), directory.resolve() / 'artifacts', directory.resolve() / 'tmp']
    if path == directory.resolve() / 'progress.json': return True
    return any(path.is_relative_to(root) and '.git' not in path.relative_to(root).parts for root in roots)


def check(value, workspace, directory):
    name, args = value.get('tool_name'), value.get('tool_input', {})
    if name in ('Write', 'Edit', 'NotebookEdit'):
        return allowed_path(args.get('file_path') or args.get('notebook_path') or '', workspace, directory)
    if name != 'Bash': return True
    command = args.get('command', '')
    if re.search(r'(?:^|[/\\\s\'"(=])\.\.[/\\]', command): return False
    # Reject explicit external paths, including those embedded in Python/Node code.
    paths = re.finditer(r'''['"](?P<quoted>(?:[A-Za-z]:[/\\]|/)[^'"<>|;)]*)['"]|(?P<plain>[A-Za-z]:[/\\][^\s'"<>|;)]*|(?<=[\s'"(=])/[^\s'"<>|;)]*)''', command)
    for match in paths:
        path = match.group('quoted') or match.group('plain')
        if not allowed_path(path, workspace, directory): return False
    return True


def prepare(workspace, directory):
    for name in ('tmp', 'artifacts'): (directory/name).mkdir(exist_ok=True)
    config = directory/'workspace-guard.json'
    config.write_text(json.dumps({'workspace':str(workspace),'directory':str(directory)}), encoding='utf-8')
    argv = module_command('cairn.dispatcher.runtime.workspace_guard')
    settings = directory/'workspace-settings.json'
    settings.write_text(json.dumps({'hooks': {'PreToolUse': [{'hooks': [{'type':'command','command':argv[0],
        'args':argv[1:], 'timeout':10,'onFailure':'block'}]}]}}), encoding='utf-8')
    return {'TMP':str(directory/'tmp'),'TEMP':str(directory/'tmp'),'TMPDIR':str(directory/'tmp'),
        'CAIRN_ARTIFACT_DIR':str(directory/'artifacts'), 'CAIRN_WORKSPACE_GUARD':str(config),
        'CAIRN_WORKSPACE_SETTINGS':str(settings), 'CAIRN_WRITABLE_ROOTS':json.dumps([str(directory/'artifacts'),str(directory/'tmp')])}


if __name__ == '__main__':
    try:
        config = json.loads(Path(os.environ['CAIRN_WORKSPACE_GUARD']).read_text(encoding='utf-8'))
        permit = check(json.loads(sys.stdin.buffer.read()), Path(config['workspace']), Path(config['directory']))
        print(json.dumps({'hookSpecificOutput':{'hookEventName':'PreToolUse','permissionDecision':'allow' if permit else 'deny',
            'permissionDecisionReason':'Write source only in this worktree; temporary output goes in CAIRN_ARTIFACT_DIR or TMPDIR. External paths and traversal are refused.'}}))
    except Exception:
        print('Cairn workspace guard unavailable; refusing tool execution.', file=sys.stderr)
        sys.exit(2)
