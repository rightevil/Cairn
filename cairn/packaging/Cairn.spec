from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata
from cairn.runtime_entry import INTERNAL_MODULES

root = Path(SPECPATH).parent
data = collect_data_files('cairn') + collect_data_files('claude_agent_sdk')
data += [(str(p), str(p.parent.relative_to(root / 'src'))) for p in (root / 'src/cairn/dispatcher/skills').rglob('*.py')]
data += copy_metadata('cairn') + copy_metadata('claude-agent-sdk')
hidden = collect_submodules('cairn') + sorted(INTERNAL_MODULES) + ['jinja2', 'jinja2.ext', 'tomllib', 'webview.platforms.winforms', 'uvicorn.logging',
    'uvicorn.loops.auto', 'uvicorn.protocols.http.auto', 'uvicorn.protocols.websockets.auto',
    'uvicorn.lifespan.on', 'pystray._win32']
a = Analysis([str(root / 'packaging/desktop_entry.py')], pathex=[str(root / 'src')],
    datas=data, hiddenimports=hidden, excludes=['PyQt5', 'PyQt6', 'PySide2', 'PySide6', 'tkinter', 'pytest'])
pyz = PYZ(a.pure)
icon = str(root / 'src/cairn/server/static/assets/cairn.ico')
gui = EXE(pyz, a.scripts, [], exclude_binaries=True, name='Cairn', console=False, upx=False, icon=icon)
# Both launchers share libraries; hooks/MCP retain stdin/stdout without opening a window in the GUI.
runtime_scripts = a.scripts[:-1] + [('runtime_entry', str(root / 'packaging/runtime_entry.py'), 'PYSOURCE')]
runtime = EXE(pyz, runtime_scripts, [], exclude_binaries=True, name='cairn-runtime', console=True, upx=False, icon=icon)
coll = COLLECT(gui, runtime, a.binaries, a.datas, strip=False, upx=False, name='Cairn')
