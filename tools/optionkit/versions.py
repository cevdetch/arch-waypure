"""Compare installed/local package releases with cached official Arch metadata."""

import argparse
import os
from pathlib import Path
import sys

from .alpm import BackendError, OFFICIAL_REPOSITORIES, get_backend, reset_backend
from .freshness import is_package_archive
from .terminal import Terminal, add_color_argument


def archives(root, recursive=False):
    if not root.is_dir():
        return []
    if recursive:
        candidates = root.rglob('*.pkg.tar*')
    else:
        directories = [root, *(path for path in root.iterdir()
                              if path.is_dir() and (path/'PKGBUILD').is_file())]
        candidates = (path for directory in directories for path in directory.glob('*.pkg.tar*'))
    return sorted(path for path in candidates if path.is_file() and is_package_archive(path))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--diff', action='store_true', help='only show release differences')
    parser.add_argument('--repo', type=Path, default=Path(os.environ.get('WAYPURE_DIR', str(Path.home()/'dev/arch/arch-waypure'))))
    parser.add_argument('--recursive', action='store_true', help='also inspect nested archive paths (legacy traversal)')
    add_color_argument(parser)
    args = parser.parse_args()
    reset_backend()
    try:
        backend = get_backend()
        names = (name for name in backend.installed_names() if name.endswith('-waypure'))
        installed = {name.removesuffix('-waypure'): package.version
                     for name,package in backend.installed(names=names).items()}
        local = {}
        for path in archives(args.repo.expanduser(), args.recursive):
            try:
                package = backend.archive(path)
            except BackendError as error:
                print(f'WARNING: {error}', file=sys.stderr)
                continue
            if not package.name.endswith('-waypure'):
                continue
            name = package.name.removesuffix('-waypure')
            if name not in local or backend.compare(package.version, local[name]) > 0:
                local[name] = package.version
        names = installed.keys() | local.keys()
        official = backend.sync_versions(repositories=OFFICIAL_REPOSITORIES, names=names)
        terminal = Terminal(args.color)
        print(terminal.paint(f"{'PKGNAME':<24} {'WAYPURE':<16} {'ARCH':<16} {'SOURCE':<10} STATUS", 'HEADER'))
        print(f"{'-'*24} {'-'*16} {'-'*16} {'-'*10} {'-'*6}")
        for name in sorted(names):
            if name not in official:
                continue
            version = local.get(name, installed.get(name))
            current, arch = version.rsplit('-',1)[0], official[name].rsplit('-',1)[0]
            equal = backend.compare(current, arch) == 0
            if args.diff and equal:
                continue
            source = 'both' if name in installed and name in local else 'local' if name in local else 'installed'
            print(f'{name:<24} {current:<16} {arch:<16} {source:<10} ' + terminal.paint('OK' if equal else 'DIFF', 'PASS' if equal else 'WARN'))
    except BackendError as error:
        parser.exit(2, 'waypure-versions: ' + str(error) + '\n')
    return 0
