"""Entrypoint commands shared by source runs and the frozen runtime helper."""
from pathlib import Path
import runpy
import sys


INTERNAL_MODULES = {
    'cairn.dispatcher.bmad_runner',
    'cairn.dispatcher.terminal_bridge',
    'cairn.dispatcher.collaboration_tools',
    'cairn.dispatcher.runtime.workspace_guard',
}


def module_command(module, *args):
    if module not in INTERNAL_MODULES:
        raise ValueError('Unknown Cairn runtime module')
    executable = str(Path(sys.executable).with_name('cairn-runtime.exe')) if getattr(sys, 'frozen', False) else sys.executable
    return [executable, '-m', module, *args]


def main():
    if len(sys.argv) > 2 and sys.argv[1] == '-m':
        module = sys.argv[2]
        if module not in INTERNAL_MODULES:
            raise ValueError('Unknown Cairn runtime module')
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name='__main__')
    else:
        from cairn.cli import main as cli
        cli()


if __name__ == '__main__':
    main()
