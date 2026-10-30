"""Check release versions, archive hygiene and the frozen BMAD runtime before publishing."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import tempfile
import tomllib
import zipfile

from cairn import __version__


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--versions-only', action='store_true')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))
    lock = tomllib.loads((root / 'uv.lock').read_text(encoding='utf-8'))
    cz = tomllib.loads((root.parent / '.cz.toml').read_text(encoding='utf-8'))['tool']['commitizen']
    version = project['project']['version']
    assert version == __version__ == cz['version'], 'Version sources disagree'
    assert [p['version'] for p in lock['package'] if p['name'] == 'cairn'] == [version], 'Lock version disagrees'
    if os.environ.get('GITHUB_REF_TYPE') == 'tag':
        assert os.environ['GITHUB_REF_NAME'] == f'v{version}', 'Release tag disagrees with package version'
    print(f'Versions agree: {version}')
    if args.versions_only:
        return

    package = root / 'dist' / f'Cairn-{version}-windows-x64.zip'
    with package.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    assert package.with_suffix('.zip.sha256').read_bytes() == f'{digest}  {package.name}\n'.encode('utf-8')
    with tempfile.TemporaryDirectory(prefix='cairn-release-') as temporary:
        directory = Path(temporary)
        with zipfile.ZipFile(package) as archive:
            names = [PurePosixPath(name) for name in archive.namelist()]
            assert {name.parts[:2] for name in names} == {
                ('Cairn', name) for name in ('Cairn.exe', 'cairn-runtime.exe', '_internal', 'dispatch.example.yaml', '使用说明.txt')
            }, 'Unexpected files in portable package'
            assert not any('..' in name.parts or {'dispatch.yaml', 'desktop.local.json', '.cairn'} & set(name.parts) for name in names)
            archive.extractall(directory)
        app = directory / 'Cairn'
        bundle = app / '_internal/cairn/dispatcher/skills/designgod'
        manifest = json.loads((bundle / 'references/sources.json').read_text(encoding='utf-8'))
        for name, expected in manifest['sha256'].items():
            assert hashlib.sha256((bundle / name).read_bytes()).hexdigest() == expected, name
        metadata, = (app / '_internal').glob('cairn-*.dist-info/METADATA')
        assert f'\nVersion: {version}\n' in metadata.read_text(encoding='utf-8')

        def run(*arguments):
            result = subprocess.run([str(app / 'cairn-runtime.exe'), *map(str, arguments)],
                                    cwd=directory, capture_output=True, encoding='utf-8', errors='replace', timeout=60)
            assert result.returncode == 0, result.stdout + result.stderr
            return result.stdout

        assert 'Usage:' in run('--help')
        workspace = directory / 'design'
        skills = workspace / '.claude/skills'
        shutil.copytree(bundle / 'references/upstream/bmad', skills)
        for entry in skills.glob('*/ENTRY.md'):
            entry.rename(entry.with_name('SKILL.md'))
        run('-m', 'cairn.dispatcher.bmad_runner', skills / 'bmad/scripts/setup.py',
            '--project-root', workspace, '--skill', skills / 'bmad', '--root', skills)
        (workspace / 'artifacts').mkdir(exist_ok=True)
        (workspace / '_bmad/config.toml').write_text(
            f'[core]\nproject_name = "Cairn smoke test"\noutput_folder = "{workspace.as_posix()}/artifacts"\nactive_initiative = "design"\n', encoding='utf-8')
        rendered = run('-m', 'cairn.dispatcher.bmad_runner', workspace / '_bmad/scripts/render_skill.py',
                       '--project-root', workspace, '--skill', skills / 'bmad-agent-po')
        assert rendered.strip() and 'HALT:' not in rendered, rendered
    print('Portable archive, metadata, bundled skill hashes and frozen runtime passed')


if __name__ == '__main__':
    main()
