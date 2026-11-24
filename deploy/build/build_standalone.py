#!/usr/bin/env python3
"""Build a standalone Relay executable using PyInstaller.

Usage:
    python deploy/build/build_standalone.py
    python deploy/build/build_standalone.py --bundle-aria2
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def main() -> None:
    parser = argparse.ArgumentParser(description='Build a standalone Relay binary with PyInstaller')
    parser.add_argument('--name', default='relay', help='output binary name (default: relay)')
    parser.add_argument('--onedir', action='store_true', help='build a directory bundle instead of a single file')
    parser.add_argument('--bundle-aria2', action='store_true',
                        help='embed the local aria2c binary inside the standalone build')
    parser.add_argument('--aria2-path', default='', help='explicit path to aria2c when bundling')
    args = parser.parse_args()

    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        sys.exit(
            'PyInstaller is not installed. Install build extras first:\n'
            '  pip install -e ".[build]"   # or: uv pip install -e ".[build]"'
        )

    entry = ROOT / 'deploy' / 'build' / '_entrypoint.py'
    static_src = ROOT / 'relay' / 'static'
    sep = ';' if os.name == 'nt' else ':'

    cmd = [
        sys.executable, '-m', 'PyInstaller',
        '--noconfirm',
        '--clean',
        '--name', args.name,
        '--onedir' if args.onedir else '--onefile',
        '--add-data', f'{static_src}{sep}relay/static',
        '--hidden-import', 'waitress',
    ]

    if args.bundle_aria2:
        aria2 = args.aria2_path or shutil.which('aria2c')
        if not aria2 or not Path(aria2).is_file():
            sys.exit('Could not find aria2c to bundle. Pass --aria2-path or install aria2.')
        cmd.extend(['--add-binary', f'{aria2}{sep}.'])
        print(f'Bundling aria2c from: {aria2}')

    cmd.append(str(entry))
    print('Running:', ' '.join(cmd))
    subprocess.run(cmd, cwd=str(ROOT), check=True)
    ext = '.exe' if os.name == 'nt' else ''
    artifact = ROOT / 'dist' / (args.name if args.onedir else args.name + ext)
    print(f'\nBuilt standalone artifact: {artifact}')


if __name__ == '__main__':
    main()
