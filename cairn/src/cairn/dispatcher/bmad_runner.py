"""Run bundled BMAD scripts with Cairn's interpreter, including frozen builds."""
from pathlib import Path
import runpy
import sys


def main():
    script = Path(sys.argv[1]).resolve()
    if script.name not in {'setup.py', 'resolve_config.py', 'resolve_customization.py', 'render_skill.py', 'memlog.py'}:
        raise ValueError('Unsupported BMAD runtime script')
    sys.argv = [str(script), *sys.argv[2:]]
    sys.path.insert(0, str(script.parent))
    runpy.run_path(str(script), run_name='__main__')


if __name__ == '__main__':
    main()
