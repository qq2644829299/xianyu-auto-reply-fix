"""Create the Windows download archive without PowerShell path quirks."""
from __future__ import annotations

import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / 'dist-desktop'
RELEASE = ROOT / 'release'
ARCHIVE = RELEASE / 'FishCloudLocal-windows.zip'


def main() -> None:
    executable = DIST / 'FishCloudLocal.exe'
    if not executable.is_file():
        raise FileNotFoundError(f'Windows client was not produced: {executable}')
    RELEASE.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(ARCHIVE, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.write(executable, executable.name)
    print(f'Windows archive created: {ARCHIVE}')


if __name__ == '__main__':
    main()
