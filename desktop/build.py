"""用 PyInstaller 构建本地客户端。macOS 输出 .app，Windows 输出 .exe。"""
from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist-desktop'
SEPARATOR = ';' if sys.platform == 'win32' else ':'


def add_data(source: str, destination: str) -> list[str]:
    return ['--add-data', f'{ROOT / source}{SEPARATOR}{destination}']


def main() -> None:
    if DIST.exists():
        shutil.rmtree(DIST)
    command = [
        sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
        # Keep the generated filename ASCII-only.  GitHub's Windows runner
        # and some unzip tools do not consistently preserve non-ASCII binary
        # names, even though the application UI itself is fully Chinese.
        '--name', 'FishCloudLocal', '--paths', str(ROOT),
        '--distpath', str(DIST), '--workpath', str(ROOT / 'build-desktop'),
        '--specpath', str(ROOT / 'build-desktop'),
    ]
    if platform.system() == 'Darwin':
        # An app bundle is the normal macOS delivery format.  Building a
        # directory bundle also prevents the raw helper executable from being
        # shown next to the application in the DMG.
        command.extend(['--onedir', '--windowed'])
    else:
        command.append('--onefile')
    for source, destination in [
        ('static', 'static'),
        ('global_config.yml', '.'),
        ('announcement.json', '.'),
    ]:
        command.extend(add_data(source, destination))
    command.extend([
        '--collect-submodules', 'utils',
        '--collect-submodules', 'fastapi',
        '--collect-submodules', 'uvicorn',
        '--collect-submodules', 'playwright',
        '--hidden-import', 'Start',
        str(ROOT / 'desktop_launcher.py'),
    ])
    subprocess.run(command, check=True, cwd=ROOT)
    print(f'本地客户端已构建到: {DIST}')


if __name__ == '__main__':
    main()
