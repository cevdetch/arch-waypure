"""Shared entry point for the specialized Qt and FFmpeg inspection commands."""

import argparse
from pathlib import Path
import sys


def parser_for(collector, qt):
    parser = argparse.ArgumentParser(description=collector.__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command')
    scanner = commands.add_parser('scan', help='capture extracted releases under a package source directory')
    scanner.add_argument('directory', type=Path, nargs='?')
    snapshot = commands.add_parser('snapshot', help='capture source options without configuring a build')
    snapshot.add_argument('source', type=Path)
    snapshot.add_argument('-o', '--output', type=Path, required=True)
    diff = commands.add_parser('diff', help='compare release options and source rules')
    diff.add_argument('old', help='release version or JSON path')
    diff.add_argument('new', help='release version or JSON path')
    if qt:
        diff.add_argument('--all', action='store_true', help='include CPU and other-platform choices')
    descriptions = {'render': 'print a commented CMake options array' if qt else 'print commented Bash array entries',
                    'check': 'check PKGBUILD feature choices and implied OFFs' if qt else 'check selected PKGBUILD flags against an inventory'}
    if qt:
        descriptions['explain'] = "explain a feature's source rules and observed state"
    for name, description in descriptions.items():
        command = commands.add_parser(name, help=description)
        command.add_argument('inventory', help='release version or JSON path')
        if name == 'render':
            command.add_argument('--added-since', help='only new choices, plus existing selections')
            command.add_argument('--pkgbuild', type=Path, help='keep active choices and comments')
            command.add_argument('--entries-only', action='store_true', help='omit the array wrapper')
            if qt:
                command.add_argument('--all', action='store_true')
                command.add_argument('--prune-redundant', action='store_true',
                                     help='comment out OFFs proved unnecessary together')
                command.set_defaults(build=None)
            else:
                scope = command.add_mutually_exclusive_group()
                scope.add_argument('--all', action='store_true', default=True,
                                   help='show every advertised option (default)')
                scope.add_argument('--features-only', dest='all', action='store_false',
                                   help='hide directory/toolchain arguments and help options')
        elif qt:
            command.add_argument('--pkgbuild', type=Path, help="defaults to the current package's PKGBUILD")
            command.add_argument('--build', type=Path, help='existing build directory or CMakeCache.txt; read only')
            if name == 'explain':
                command.add_argument('feature')
        else:
            command.add_argument('pkgbuild', type=Path, nargs='?')
    return parser


def main(backend):
    if backend == 'qt6':
        import tarfile
        from . import qt6 as collector
        errors = (OSError, ValueError, tarfile.TarError)
    elif backend == 'ffmpeg':
        import subprocess
        from . import ffmpeg as collector
        errors = (OSError, ValueError, subprocess.SubprocessError)
    else:
        raise ValueError(f'unsupported collector: {backend}')
    qt = backend == 'qt6'
    parser = parser_for(collector, qt)
    args = parser.parse_args()
    inventory_path = collector.inventory_path if qt else collector.resolve_inventory
    try:
        if args.command in (None, 'scan'):
            collector.scan(getattr(args, 'directory', None))
        elif args.command == 'snapshot':
            if not qt and args.output.exists():
                raise ValueError(f'{args.output} already exists; choose a new filename to preserve the baseline')
            collector.write_snapshot(collector.capture(args.source), args.output)
        elif args.command == 'diff':
            before = collector.load(inventory_path(args.old))
            after = collector.load(inventory_path(args.new))
            if qt:
                collector.compare(before, after, args.all)
            else:
                collector.compare(before, after)
        else:
            data = collector.load(inventory_path(args.inventory))
            if qt:
                selected, cache = collector.selected_context(args, data)
                if args.command == 'render':
                    previous = collector.load(inventory_path(args.added_since)) if args.added_since else None
                    collector.render(data, selected, args.all, previous, args.entries_only, args.prune_redundant)
                elif args.command == 'check':
                    return int(collector.check(data, selected, cache))
                else:
                    collector.explain(data, args.feature, selected, cache)
            elif args.command == 'check':
                collector.check_selected(data, args.pkgbuild or collector.package_root() / 'PKGBUILD')
            else:
                collector.render(data,
                                 collector.load(inventory_path(args.added_since)) if args.added_since else None,
                                 args.all, collector.selected_flags(args.pkgbuild) if args.pkgbuild else None,
                                 args.entries_only, collector.pkgbuild_annotations(args.pkgbuild) if args.pkgbuild else None)
    except errors as error:
        if qt:
            print(f'qt6-options: {error}', file=sys.stderr)
            return 1
        parser.exit(1, f'error: {error}\n')
    return 0
