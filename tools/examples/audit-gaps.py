#!/usr/bin/env python3
"""Create inspectable synthetic packages demonstrating audit checks; never build/install them."""

import argparse
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from optionkit.linting import run_namcap
from optionkit.repository import audit_package, load_policy, srcinfo
from optionkit.terminal import Terminal


def fixture(root, label, architecture='x86_64'):
    directory = root/label
    directory.mkdir()
    source = directory/'input.txt'
    source.write_text('Synthetic audit fixture.\n')
    checksum = hashlib.sha256(source.read_bytes()).hexdigest()
    (directory/'PKGBUILD').write_text(
        "pkgname=demo-waypure\npkgver=1.0\npkgrel=1\npkgdesc='Synthetic audit fixture'\n"
        "url=https://example.invalid\narch=('" + architecture + "')\nlicense=('MIT')\n"
        "provides=('demo')\nconflicts=('demo')\nsource=('input.txt')\n"
        "sha256sums=('" + checksum + "')\npackage() { return 1; }\n")
    (directory/'.SRCINFO').write_text(
        'pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tarch = ' + architecture + '\n'
        '\tlicense = MIT\n\tprovides = demo\n\tconflicts = demo\n\tsource = input.txt\n'
        '\tsha256sums = ' + checksum + '\npkgname = demo-waypure\n')
    return directory


def archive(directory, extra=None, architecture='x86_64'):
    path = directory/('demo-waypure-1.0-1-' + architecture + '.pkg.tar')
    metadata = (b'pkgname = demo-waypure\npkgbase = demo-waypure\npkgver = 1.0-1\n'
                b'pkgdesc = Synthetic audit fixture\nurl = https://example.invalid\n'
                b'arch = ' + architecture.encode() + b'\nlicense = MIT\nsize = 0\n')
    with tarfile.open(path, 'w') as output:
        for name, data in {'.PKGINFO': metadata, **(extra or {})}.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            member.uname = member.gname = 'root'
            output.addfile(member, io.BytesIO(data))
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, help='new directory for synthetic fixtures; default: a fresh directory in /tmp')
    parser.add_argument('--namcap', action='store_true', help='also demonstrate native license-file lint on a generated archive')
    args = parser.parse_args()
    if args.directory:
        root = args.directory.resolve()
        try:
            root.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            parser.error('--directory must name a new directory')
    else:
        root = Path(tempfile.mkdtemp(prefix='waypure-audit-examples-'))
    terminal = Terminal('auto')
    policy = load_policy(TOOLS/'waypure-policy.toml')
    print('SYNTHETIC EXAMPLES: generated demo-waypure archives, not findings in real packages.')
    print('The dummy library contains one test function and no X11 implementation.')
    print('Fixtures kept for inspection:', root)
    print('Results below show the relevant recipe/artifact checks; these invented packages lack Arch evidence.\n')
    cases = []
    baseline = fixture(root, 'baseline')
    cases.append((baseline, False, 'recipe', 'PASS', 'matching metadata'))
    mismatch = fixture(root, 'license-mismatch')
    metadata = mismatch/'.SRCINFO'
    metadata.write_text(metadata.read_text().replace('license = MIT', 'license = BSD-2-Clause'))
    cases.append((mismatch, False, 'recipe', 'FAIL', 'PKGBUILD declares MIT; .SRCINFO records BSD-2-Clause'))
    counts = fixture(root, 'checksum-count')
    (counts/'second.patch').write_text('Synthetic second source.\n')
    recipe = counts/'PKGBUILD'
    recipe.write_text(recipe.read_text().replace("source=('input.txt')", "source=('input.txt' 'second.patch')"))
    metadata = counts/'.SRCINFO'
    metadata.write_text(metadata.read_text().replace('\tsha256sums =', '\tsource = second.patch\n\tsha256sums ='))
    cases.append((counts, False, 'recipe', 'FAIL', 'two source files; one checksum'))
    stale = fixture(root, 'stale-archive')
    path = archive(stale)
    os.utime(path, (1, 1))
    cases.append((stale, True, 'artifacts', 'STALE', 'correct version; archive older than recipe/metadata'))
    if shutil.which('cc') and shutil.which('readelf'):
        bundled = fixture(root, 'bundled-x11')
        source = bundled/'synthetic.c'
        source.write_text('int synthetic_audit_example(void) { return 0; }\n')
        library = bundled/'dummy.so'
        subprocess.run(['cc', '-shared', '-fPIC', '-nostdlib', '-Wl,-soname,libX11.so.6', str(source), '-o', str(library)], check=True)
        archive(bundled, {'usr/lib/renamed.so': library.read_bytes()})
        cases.append((bundled, True, 'artifacts', 'FAIL', 'dummy ELF renamed.so has SONAME libX11.so.6 and no DT_NEEDED'))
    else:
        print('SKIP bundled-x11: this example needs cc and readelf.\n')
    failed = False
    for directory, artifacts, check, expected, description in cases:
        info = srcinfo(directory)[0]
        row = audit_package(directory, policy, {directory: info}, {}, artifacts=artifacts, metadata_only=True)
        actual = row['checks'][check]
        print(terminal.paint(actual, actual), directory.name + ':', description)
        print(' ', check + ':', actual, '(expected ' + expected + ')')
        print('  Recipe:', directory/'PKGBUILD')
        print('  Metadata:', directory/'.SRCINFO')
        for result in row['artifacts']:
            print('  Archive:', result['path'])
        for finding in row['findings']:
            if finding['code'] == 'arch.missing':
                continue
            print(' ', finding['severity'].upper(), '[' + finding['code'] + ']', finding['message'])
        print()
        failed |= actual != expected
    if args.namcap:
        licenses = fixture(root, 'license-files', architecture='any')
        path = archive(licenses, {'usr/share/licenses/wrong-name/COPYING': b'Synthetic placeholder\n'}, architecture='any')
        result = run_namcap(path)
        print('NATIVE NAMCAP:', result['status'], path)
        for finding in result['findings']:
            print(' ', finding['severity'].upper(), finding['message'])
        failed |= not any('license' in item['message'].lower() and item['severity'] == 'error' for item in result['findings'])
    print('Inspect a fixture with:')
    print(' ', TOOLS/'waypure-audit', '--repo', root, 'bundled-x11 --metadata-only --artifacts')
    print('The CLI may also report missing real Arch evidence for these invented packages.')
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
