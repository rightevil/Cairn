"""Build a portable Windows folder/ZIP; private runtime configuration is never collected."""
from pathlib import Path
import hashlib
import shutil
import subprocess
import sys
import tomllib


def main():
    root = Path(__file__).resolve().parents[1]
    from icons import build
    build()
    # Build in staging so local databases, settings and backups in dist are never deleted.
    staging = root / 'build/portable'
    subprocess.run([sys.executable, '-m', 'PyInstaller', '--noconfirm',
                    '--distpath', str(staging), str(root / 'packaging/Cairn.spec')], cwd=root, check=True)
    output = staging / 'Cairn'
    shutil.copy2(root.parent / 'dispatch.example.yaml', output / 'dispatch.example.yaml')
    (output / '使用说明.txt').write_text(
        'Cairn 免安装桌面版\n\n解压完整目录后双击 Cairn.exe，请保留 _internal 和 cairn-runtime.exe。\n'
        '首次使用会自动打开设置页，添加供应商（协议、地址、密钥、模型）并为 Agent 分配供应商和角色。\n'
        'Windows 需要 Microsoft Edge WebView2 Runtime；Git、Agent CLI 沿用本机安装。designgod 工作流已内置。\n'
        '关闭窗口后继续在托盘运行；单击托盘图标恢复；右键菜单可打开或退出 Cairn。\n'
        '数据和日志默认位于配置对应的 .cairn/data；更换版本请保留数据和本地配置。\n'
        '工作台左下角“运行设置”保存并应用即可热加载；当前任务保持原配置，新任务使用新配置，无需重启程序。\n'
        'dispatch.yaml 和 desktop.local.json 含本机配置与密钥，请勿分享。\n',
        encoding='utf-8-sig')
    # Build a clean distribution, even if an earlier local run added credentials or databases.
    files = ['Cairn.exe', 'cairn-runtime.exe', '_internal', 'dispatch.example.yaml', '使用说明.txt']
    import zipfile
    version = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['version']
    (root / 'dist').mkdir(exist_ok=True)
    package = root / 'dist' / f'Cairn-{version}-windows-x64.zip'
    with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name in files:
            path = output / name
            for item in path.rglob('*') if path.is_dir() else [path]:
                if item.is_file():
                    archive.write(item, Path('Cairn') / item.relative_to(output))
    with package.open('rb') as source:
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    package.with_suffix('.zip.sha256').write_text(f'{digest}  {package.name}\n', encoding='utf-8', newline='\n')
    shutil.copytree(output, root / 'dist/Cairn', dirs_exist_ok=True)


if __name__ == '__main__':
    main()
