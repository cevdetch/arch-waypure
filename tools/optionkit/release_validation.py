"""Explicit release builds in fresh devtools chroots, with local evidence receipts.

This is a validation boundary, not a dependency solver: native makepkg/pacman
resolve dependencies. Local archives require explicit --install or --bootstrap-install.
"""

import argparse
import ast
from datetime import datetime, timezone
import hashlib
from importlib.machinery import BYTECODE_SUFFIXES, EXTENSION_SUFFIXES
import json
import os
from pathlib import Path, PurePosixPath
import posixpath
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import urlsplit
import uuid

from .alpm import get_backend
from .artifacts import audit_artifact, finish_decoder
from .dependencies import split_dependency, version_satisfies
from .freshness import is_package_archive
from .linting import run_namcap
from .literals import array_body, words
from .metadata import SrcinfoError, entry_values, fields, package_fields, parse_srcinfo_text
from .pkgbuild import LiteralRecipe
from .repository import recipe_arrays
from .runtime_policy import load_policy


RECEIPT = '.waypure-validation.json.log'
UPDATE_ASSURANCE = 'update requested; native devtools update success is not independently verified'
ARCHBUILD_UPDATE_ASSURANCE = 'native archbuild checks the pristine baseline update; before/after snapshots and actual build inputs are recorded'
CHECKED_UPDATE_ASSURANCE = 'validator checks the pristine baseline update; before/after snapshots and actual build inputs are recorded'
SCOPE = ('Build success demonstrates dependency sufficiency in the recorded baseline and working '
         'copy with the explicit local inputs. It does not prove dependency '
         'necessity or minimality. No dependency pruning is performed.')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def snapshot(directory):
    """Hash recipe inputs and downloaded sources, excluding generated build products."""
    paths = {path.name: path for path in directory.iterdir()
             if path.is_file() and '.pkg.tar' not in path.name and not path.name.endswith('.log')}
    # Declared inputs remain inputs even when their names resemble generated
    # logs/archives. Aliases identify the filename makepkg downloads into SRCDEST.
    for key, values in fields((directory/'.SRCINFO').read_text()).items():
        if key == 'source' or key.startswith('source_'):
            for value in values:
                name = value.split('::', 1)[0] if '::' in value else value
                if '://' in name:
                    name = Path(urlsplit(name).path).name
                path = directory/name
                if path.is_file():
                    paths[name] = path
    return {name: digest(path) for name, path in sorted(paths.items())}


def output_inventory(directory):
    """Freeze every package/signature path and hash before host checks resume."""
    result = {}
    for path in sorted(directory.glob('*.pkg.tar*')):
        if not (is_package_archive(path) or path.suffix == '.sig' and is_package_archive(path.with_suffix(''))):
            continue
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'invalid output archive: {path}')
        result[path.name] = digest(path)
    return result


def require_outputs_unchanged(directory, expected):
    if output_inventory(directory) != expected:
        raise ValueError(f'{directory}: package output inventory changed after chroot build')


def require_context_unchanged(plan_data, expected_baseline):
    if baseline(Path(plan_data['chroot']), update_assurance=plan_data['update_assurance']) != expected_baseline:
        raise ValueError('chroot baseline changed during validation')
    for item in plan_data['install_archives']:
        if digest(Path(item['path'])) != item['sha256']:
            raise ValueError(f'injected archive changed during validation: {item["path"]}')
    if digest(Path(plan_data['policy']['path'])) != plan_data['policy']['sha256']:
        raise ValueError('runtime policy changed during validation')


def require_clean(findings, context):
    errors = [item['message'] for item in findings if item['severity'] == 'error']
    if errors:
        raise ValueError(context + ': ' + '; '.join(errors))


def inspect_recipe(directory, policy):
    text = (directory/'.SRCINFO').read_text()
    info = parse_srcinfo_text(text, directory)
    recorded = fields(text)
    for key in ('pkgbase', 'pkgver', 'pkgrel'):
        if len(recorded.get(key, [])) != 1:
            raise ValueError(f'{directory}: expected one .SRCINFO {key}')
    if len(recorded.get('epoch', [])) > 1 or len(info.pkgnames) != len(set(info.pkgnames)):
        raise ValueError(f'{directory}: ambiguous .SRCINFO identity/version')
    recipe = LiteralRecipe.read(directory/'PKGBUILD')
    for key in ('pkgver', 'pkgrel', 'epoch'):
        actual = recipe.scalar(key)
        if key == 'epoch' and not re.search(r'(?m)^\s*epoch\s*=', recipe.text):
            actual = '0'
        if actual is None or actual != recorded.get(key, ['0'])[0]:
            raise ValueError(f'{directory}: ambiguous or mismatched recipe/.SRCINFO {key}')
    name = recipe.scalar('pkgname')
    names = [name] if name else None
    declaration = re.search(r'(?m)^pkgname=', recipe.text)
    if names is None and declaration and recipe.bindings.unique('pkgname') and declaration.start() < recipe.bindings.scope_limit:
        body = array_body(recipe.text[declaration.end():])
        if body is not None:
            names = words(body, lambda reference: recipe.bindings.get(reference, declaration.start()))
    if not names or len(names) != len(set(names)) or set(names) != set(info.pkgnames):
        raise ValueError(f'{directory}: ambiguous or mismatched recipe/.SRCINFO output identity')
    base = recipe.scalar('pkgbase')
    if base is None and not re.search(r'(?m)^\s*pkgbase\s*=', recipe.text):
        base = names[0]
    if base != recorded['pkgbase'][0]:
        raise ValueError(f'{directory}: mismatched recipe/.SRCINFO pkgbase')
    findings = [{'severity': severity, 'message': f'{kind}: {key} = {value}'}
                for severity, kind, key, value in policy.metadata(recorded)]
    dependencies = info.depends + info.makedepends + info.checkdepends
    for key, values, _ in recipe_arrays(recipe):
        if values is not None:
            if key.removesuffix('_x86_64') in {'depends', 'makedepends', 'checkdepends'}:
                dependencies.extend(values)
            findings.extend({'severity': severity, 'message': f'{kind}: recipe {key} = {value}'}
                            for severity, kind, _, value in policy.metadata({key: values}))
    for dependency in dependencies:
        name, _, _ = split_dependency(dependency)
        if name.endswith('-waypure') and (name not in info.pkgnames or dependency != name+'='+info.version):
            raise ValueError(f'{directory}: {dependency}: use official dependency names; only exact sibling pins are allowed')
    for key, sources in recorded.items():
        if key == 'source' or key.startswith('source_'):
            hashes = recorded.get(key.replace('source', 'sha256sums', 1), [])
            if len(sources) != len(hashes):
                raise ValueError(f'{directory}: {key}: source/SHA256 checksum count differs')
            for checksum in hashes:
                if checksum == 'SKIP' and re.search(r'(?:^|[.\-])rc\d*', recorded['pkgver'][0], re.I):
                    findings.append({'severity': 'warning', 'message': f'{key}: release-candidate source checksum is SKIP'})
                elif not re.fullmatch(r'[0-9a-fA-F]{64}', checksum):
                    raise ValueError(f'{directory}: {key}: trusted source requires a real SHA256 checksum')
    require_clean(findings, str(directory))
    return info, base, [item['message'] for item in findings if item['severity'] != 'error']


def baseline(chroot, *, update_assurance=UPDATE_ASSURANCE):
    root = chroot/'root'
    if not root.is_dir():
        raise ValueError(f'missing clean chroot baseline: {root}; create it separately with mkarchroot')
    marker = root/'.arch-chroot'
    if not marker.is_file() or not re.fullmatch(r'v\d+', marker.read_text().strip()):
        raise ValueError(f'incomplete Arch chroot: missing or invalid {marker}; initialize a fresh chroot separately with devtools')
    chroot_version = marker.read_text().strip()
    native_library = Path('/usr/share/devtools/lib/archroot.sh')
    if native_library.is_file():
        expected = re.search(r'''(?m)^CHROOT_VERSION=['"]?(v\d+)''', native_library.read_text())
        if expected and chroot_version != expected[1]:
            raise ValueError(f'outdated Arch chroot: {marker} is {chroot_version}, installed devtools requires {expected[1]}; recreate it separately before validation')
    for name in ('usr/bin/bash', 'usr/bin/pacman'):
        path = root/name
        if not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError(f'incomplete Arch chroot: missing executable {path}; initialize a fresh chroot separately with devtools')
    evidence, packages = {'.arch-chroot': digest(marker)}, []
    for name in ('etc/pacman.conf', 'etc/makepkg.conf'):
        path = root/name
        if not path.is_file():
            raise ValueError(f'incomplete chroot baseline: missing {path}')
        evidence[name] = digest(path)
    configs = [root/'etc/waypure-build-profile.json']
    for pattern in ('etc/makepkg.conf.d/**/*.conf', 'etc/pacman.conf.d/**/*.conf', 'etc/pacman.d/*'):
        configs.extend(root.glob(pattern))
    for path in sorted(set(configs)):
        if path.is_file():
            evidence[str(path.relative_to(root))] = digest(path)
    database = root/'var/lib/pacman/local'
    if not database.is_dir():
        raise ValueError(f'incomplete chroot baseline: missing {database}')
    for path in sorted(database.glob('*/desc')):
        text = path.read_text()
        name = re.search(r'(?m)^%NAME%\n([^\n]+)', text)
        version = re.search(r'(?m)^%VERSION%\n([^\n]+)', text)
        if not name or not version:
            raise ValueError(f'invalid baseline package record: {path}')
        packages.append({'name': name[1], 'version': version[1]})
        evidence[str(path.relative_to(root))] = digest(path)
    if not packages:
        raise ValueError(f'empty baseline package database: {database}')
    return {'root': str(root), 'packages': packages, 'sha256': evidence, 'devtools_version': chroot_version,
            'update': update_assurance}


def archive_metadata(path, *, include_files=False, include_pkgconfig=False, include_contents=()):
    """Read bounded metadata and explicitly requested archive contents."""
    process = None
    try:
        if path.name.endswith('.zst'):
            process = subprocess.Popen(['zstd', '--decompress', '--stdout', '--', str(path)],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            archive = tarfile.open(fileobj=process.stdout, mode='r|')
        else:
            archive = tarfile.open(path, mode='r|*')
        result, seen = {}, set()
        with archive:
            for item in archive:
                name = item.name.removeprefix('./')
                data = None
                if include_files or include_pkgconfig or include_contents:
                    canonical = PurePosixPath(item.name)
                    if canonical.is_absolute() or '..' in canonical.parts:
                        raise ValueError(f'{path}: unsafe archive member {item.name!r}')
                    name = canonical.as_posix()
                    if name in seen:
                        raise ValueError(f'{path}: duplicate archive member {name}')
                    seen.add(name)
                    if not item.isdir():
                        result.setdefault('files', {})[name] = {
                            'type': 'file' if item.isfile() else 'symlink' if item.issym() else 'other',
                            'target': item.linkname}
                if name in include_contents and item.isfile() and item.size <= 2 * 1024 * 1024:
                    data = archive.extractfile(item).read()
                    result.setdefault('contents', {})[name] = data
                if (include_pkgconfig and item.isfile() and item.size <= 2 * 1024 * 1024
                        and PurePosixPath(name).parent.as_posix() in
                        {'usr/lib/pkgconfig', 'usr/lib32/pkgconfig', 'usr/share/pkgconfig'}
                        and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.+-]*\.pc', PurePosixPath(name).name)):
                    result.setdefault('pkgconfig', {})[name] = data if data is not None else archive.extractfile(item).read()
                if name not in {'.PKGINFO', '.BUILDINFO'}:
                    continue
                if name in result or not item.isfile() or item.size > 2 * 1024 * 1024:
                    raise ValueError(f'{path}: invalid or duplicate {name}')
                result[name] = fields((data if data is not None else archive.extractfile(item).read()).decode())
        if process and finish_decoder(process):
            raise ValueError(f'{path}: decompression failed')
        return result
    finally:
        if process:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()


def internal_python_dependencies(lint, path, audit):
    """Split known mixed findings and prove Python imports without importing code."""
    header = re.compile(re.escape(path.name) + r': Dependency \S+ detected and not included \((.*)\)')
    reason = re.compile(r'(?:python modules|libraries|pkg-config files) \[.*?\] needed in files \[.*?\](?=, (?:python modules|libraries|pkg-config files) |$)')
    findings = []
    for finding in lint['findings']:
        match = header.fullmatch(finding['message']) if finding['severity'] == 'error' else None
        parts = [part[0] for part in reason.finditer(match[1])] if match else []
        # Keep unfamiliar/malformed mixed findings intact and fatal. Splitting
        # known reasons does not resolve any edge; each still needs its proof.
        if len(parts) > 1 and match[1] == ', '.join(parts):
            prefix = finding['message'][:match.start(1)]
            findings.extend({**finding, 'message': prefix + part + ')'} for part in parts)
        else:
            findings.append(finding)
    lint['findings'] = findings
    pattern = re.compile(re.escape(path.name) +
                         r': Dependency \S+ detected and not included \(python modules (\[.*\]) needed in files (\[.*\])\)')
    site = f'usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
    interpreter = Path(sys.executable)
    for finding in findings:
        match = pattern.fullmatch(finding['message']) if finding['severity'] == 'error' else None
        if not match:
            continue
        try:
            modules, consumers = (ast.literal_eval(value) for value in match.groups())
            if not all(isinstance(items, list) and items and all(isinstance(x, str) for x in items)
                       for items in (modules, consumers)) or not all(re.fullmatch(r'[A-Za-z_]\w*', m) for m in modules):
                continue
            choices = {m: [f'{site}/{m}.py', f'{site}/{m}/__init__.py',
                           *(f'{site}/{m}{suffix}' for suffix in EXTENSION_SUFFIXES),
                           *(f'{site}/{m}/__init__{suffix}' for suffix in EXTENSION_SUFFIXES),
                           *(f'{site}/{m}{suffix}' for suffix in BYTECODE_SUFFIXES),
                           *(f'{site}/{m}/__init__{suffix}' for suffix in BYTECODE_SUFFIXES)] for m in modules}
            wanted = set(consumers) | {p for paths in choices.values() for p in paths}
            metadata = archive_metadata(path, include_files=True, include_contents=wanted)
            files, contents = metadata.get('files', {}), metadata.get('contents', {})
            # Count competing paths before filtering unsupported entries: a
            # symlink or oversized package initializer can shadow module.py.
            providers = {m: [p for p in paths if p in files] for m, paths in choices.items()}
            if not all(len(paths) == 1 for paths in providers.values()) or any(f'{site}/{m}' in files for m in modules):
                continue
            providers = {m: paths[0] for m, paths in providers.items()}
            if any(p.endswith(tuple(BYTECODE_SUFFIXES)) for p in providers.values()):
                continue
            names = set(consumers) | set(providers.values())
            if not all(files.get(p, {}).get('type') == 'file' and p in contents for p in names):
                continue
            if any(parent.as_posix() in files for name in names for parent in PurePosixPath(name).parents):
                continue
            trees = {p: ast.parse(contents[p]) for p in names
                     if p in consumers or p.endswith('.py')}
            imports = {p: {alias.name for node in ast.walk(trees[p]) if isinstance(node, ast.Import)
                           for alias in node.names} |
                          {node.module for node in ast.walk(trees[p])
                           if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module}
                       for p in consumers}
            if not all(set(modules) & values for values in imports.values()):
                continue
            if not set(modules) <= set().union(*imports.values()):
                continue
            with interpreter.open('rb') as stream:
                elf_header = stream.read(20)
            abi = (elf_header[4:6] + elf_header[18:20]).hex() if elf_header[:4] == b'\x7fELF' else None
            with tempfile.TemporaryDirectory(prefix='waypure-python-import-') as directory:
                valid = True
                for module, provider in providers.items():
                    if not provider.endswith('.so'):
                        continue
                    if not abi or audit['elf_objects'].get(provider, {}).get('abi') != abi:
                        valid = False
                        break
                    probe = Path(directory)/'extension.so'
                    probe.write_bytes(contents[provider])
                    symbols = subprocess.run(['readelf', '--dyn-syms', '--wide', str(probe)],
                                             capture_output=True, text=True, timeout=5)
                    if symbols.returncode or not any(
                            len(parts := line.split()) == 8 and parts[3:6] == ['FUNC', 'GLOBAL', 'DEFAULT']
                            and parts[6] != 'UND' and parts[7] == 'PyInit_' + module
                            for line in symbols.stdout.splitlines()):
                        valid = False
                        break
                if not valid:
                    continue
        except (OSError, SyntaxError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            continue
        finding.update(severity='note', code='namcap.internal-python', resolved_by=sorted(providers.values()))
        finding['message'] += ' (Python imports, module paths and extension ABI/entry points verified in this archive)'
    levels = {item['severity'] for item in lint['findings']}
    lint['status'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'


def internal_library_dependencies(lint, path, audit, *, companions=(), row=None, policy=None):
    """Prove ELF edges in this archive or an exactly declared current sibling."""
    pattern = re.compile(re.escape(path.name) +
                         r': Dependency \S+ detected and not included \(libraries (\[.*\]) needed in files (\[.*\])\)')
    candidates = [(finding, pattern.fullmatch(finding['message'])) for finding in lint['findings']
                  if finding['severity'] == 'error']
    candidates = [(finding, match) for finding, match in candidates if match]
    if not candidates:
        return
    metadata = archive_metadata(path, include_files=True)
    files = metadata.get('files', {})
    objects = audit['elf_objects']
    sources = [(path, files, objects)]
    identity = metadata.get('.PKGINFO', {})
    if row and policy is not None and identity.get('pkgbase') == [row['pkgbase']]:
        for companion in companions:
            other = archive_metadata(companion, include_files=True)
            package = other.get('.PKGINFO', {})
            names = package.get('pkgname', [])
            if (len(names) != 1 or names[0] not in row['pkgnames']
                    or names == identity.get('pkgname')
                    or package.get('pkgbase') != [row['pkgbase']]
                    or package.get('pkgver') != [row['version']]
                    or identity.get('pkgver') != [row['version']]
                    or names[0] + '=' + row['version'] not in identity.get('depend', [])
                    or any(item.get('.BUILDINFO', {}).get('pkgbuild_sha256sum') != [row['inputs']['PKGBUILD']]
                           for item in (metadata, other))):
                continue
            provider_audit = audit_artifact(companion, policy)
            require_clean(provider_audit['findings'], str(companion))
            sources.append((companion, other.get('files', {}), provider_audit['elf_objects']))

    def provided(library, source, files, objects):
        if PurePosixPath(library).parent.as_posix() not in {'usr/lib', 'usr/lib32'}:
            return None
        destination, visited = library, set()
        while destination in files and destination not in visited:
            visited.add(destination)
            item = files[destination]
            if item['type'] == 'file':
                obj = objects.get(destination)
                if obj and obj['sonames'] == [PurePosixPath(library).name]:
                    return source, destination, obj
                return None
            if item['type'] != 'symlink':
                return None
            target = item['target']
            # Prove only same-directory SONAME links. Normalizing e.g.
            # missing/../lib.so would hide a broken filesystem lookup.
            if not target or '/' in target or target in {'.', '..'}:
                return None
            destination = posixpath.join(posixpath.dirname(destination), target)
        return None

    for finding, match in candidates:
        try:
            libraries, consumers = (ast.literal_eval(value) for value in match.groups())
        except (SyntaxError, ValueError):
            continue
        if not all(isinstance(items, list) and items and all(isinstance(x, str) for x in items)
                   for items in (libraries, consumers)):
            continue
        providers = {}
        for library in libraries:
            matches = [provided(library, *source) for source in sources if library in source[1]]
            # A conflicting or broken path must not be hidden by another output.
            providers[library] = matches[0] if len(matches) == 1 else None
        if not all(providers.values()) or not all(consumer in objects for consumer in consumers):
            continue
        if not all(any(PurePosixPath(library).name in objects[consumer]['needed'] for consumer in consumers)
                   for library in libraries):
            continue
        if not all(any(PurePosixPath(library).name in objects[consumer]['needed'] for library in libraries)
                   for consumer in consumers):
            continue
        if not all(objects[consumer]['abi'] == provider[2]['abi']
                   for library, provider in providers.items() for consumer in consumers
                   if PurePosixPath(library).name in objects[consumer]['needed']):
            continue
        sibling = any(provider[0] != path for provider in providers.values())
        finding.update(severity='note', code='namcap.internal-library',
                       resolved_by=sorted({str(provider[0]) + ':' + provider[1] if sibling else provider[1]
                                           for provider in providers.values()}))
        finding['message'] += (' (ELF dependency and matching library verified in exactly declared current sibling archives)'
                               if sibling else ' (ELF dependency and matching library verified in this archive)')
    levels = {item['severity'] for item in lint['findings']}
    lint['status'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'


def internal_pkgconfig_dependencies(lint, path):
    """Prove reported pkg-config edges within this archive, isolated from host files."""
    pattern = re.compile(re.escape(path.name) +
                         r': Dependency \S+ detected and not included \(pkg-config files (\[.*\]) needed in files (\[.*\])\)')
    candidates = [(finding, pattern.fullmatch(finding['message'])) for finding in lint['findings']
                  if finding['severity'] == 'error']
    candidates = [(finding, match) for finding, match in candidates if match]
    executable = shutil.which('pkg-config')
    if not candidates or not executable:
        return
    metadata = archive_metadata(path, include_pkgconfig=True)
    contents = metadata.get('pkgconfig', {})
    files = metadata.get('files', {})
    for finding, match in candidates:
        try:
            providers, consumers = (ast.literal_eval(value) for value in match.groups())
        except (SyntaxError, ValueError):
            continue
        if not all(isinstance(items, list) and items and all(isinstance(x, str) for x in items)
                   for items in (providers, consumers)):
            continue
        if not all(name in contents and files[name]['type'] == 'file' for name in providers + consumers):
            continue
        directories = {str(PurePosixPath(name).parent) for name in providers + consumers}
        if {'usr/lib/pkgconfig', 'usr/lib32/pkgconfig'} <= directories:
            continue
        view = {'usr/share/pkgconfig', 'usr/lib32/pkgconfig' if 'usr/lib32/pkgconfig' in directories
                else 'usr/lib/pkgconfig'}
        available = {name: data for name, data in contents.items() if str(PurePosixPath(name).parent) in view}
        basenames = [PurePosixPath(name).name for name in available]
        if len(basenames) != len(set(basenames)):
            continue
        try:
            with tempfile.TemporaryDirectory(prefix='waypure-pkgconfig-') as directory:
                for name, data in available.items():
                    (Path(directory)/PurePosixPath(name).name).write_bytes(data)
                environment = {'PATH': os.defpath, 'LC_ALL': 'C', 'PKG_CONFIG_LIBDIR': directory,
                               'PKG_CONFIG_PATH': ''}

                def query(*arguments):
                    return subprocess.run([executable, '--no-uninstalled', '--maximum-traverse-depth', '1', *arguments],
                                          env=environment, capture_output=True, text=True, timeout=5)

                edges = {}
                for consumer in consumers:
                    result = query('--print-requires', '--print-requires-private', PurePosixPath(consumer).stem)
                    if result.returncode:
                        break
                    edges[consumer] = result.stdout.splitlines()
                if len(edges) != len(set(consumers)):
                    continue
                modules = {PurePosixPath(name).stem for name in providers}
                relevant = {consumer: [requirement for requirement in requirements
                                      if requirement.split() and requirement.split()[0] in modules]
                            for consumer, requirements in edges.items()}
                if not all(relevant.values()):
                    continue
                requirements = {requirement for values in relevant.values() for requirement in values}
                if {requirement.split()[0] for requirement in requirements} != modules:
                    continue
                if any(query('--exists', requirement).returncode for requirement in requirements):
                    continue
        except (OSError, UnicodeError, subprocess.TimeoutExpired):
            continue
        finding.update(severity='note', code='namcap.internal-pkgconfig', resolved_by=sorted(set(providers)))
        finding['message'] += ' (pkg-config requirements and versions verified within this archive)'
    levels = {item['severity'] for item in lint['findings']}
    lint['status'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'


def debug_companion_links(lint, path, companions, row):
    """Resolve only build-ID links whose exact target ships in this build's primary outputs."""
    pattern = re.compile(re.escape(path.name)+r': Symlink \((usr/lib/debug/\.build-id/[0-9a-f]{2}/[0-9a-f]{6,})\) points to non-existing (.+)')
    candidates = [(finding, pattern.fullmatch(finding['message'])) for finding in lint['findings']
                  if finding['severity'] == 'error']
    candidates = [(finding, match) for finding, match in candidates if match]
    if not candidates:
        return
    links = archive_metadata(path, include_files=True)['files']
    files = {}
    for companion in companions:
        metadata = archive_metadata(companion, include_files=True)
        identity = metadata.get('.PKGINFO', {})
        if (identity.get('pkgname', []) not in [[name] for name in row['pkgnames']]
                or identity.get('pkgver') != [row['version']]
                or metadata.get('.BUILDINFO', {}).get('pkgbuild_sha256sum') != [row['inputs']['PKGBUILD']]):
            continue
        for name, item in metadata.get('files', {}).items():
            if item['type'] == 'file':
                files[name] = str(companion)
    for finding, match in candidates:
        name, target = match.groups()
        if links.get(name) != {'type': 'symlink', 'target': target}:
            continue
        destination = posixpath.normpath(posixpath.join(posixpath.dirname(name), target)).lstrip('/')
        if destination == '..' or destination.startswith('../') or destination not in files:
            continue
        finding.update(severity='note', code='namcap.debug-companion', resolved_by=files[destination])
        finding['message'] += ' (target verified in current companion archive)'
    levels = {item['severity'] for item in lint['findings']}
    lint['status'] = 'FAIL' if 'error' in levels else 'WARN' if 'warning' in levels else 'PASS'


def inspect_installs(paths, infos, policy, bootstrap_paths=()):
    result, capabilities, filenames = [], {}, set()
    output_names = {name for info in infos for name in info.pkgnames}
    for path, bootstrap in ([(path, False) for path in paths]
                            + [(path, True) for path in bootstrap_paths]):
        path = path.resolve(strict=True)
        if path.name in filenames:
            raise ValueError(f'duplicate injected archive filename: {path.name}')
        filenames.add(path.name)
        archive = archive_metadata(path)
        metadata = archive.get('.PKGINFO', {})
        for field in ('pkgname', 'pkgver', 'arch'):
            if len(metadata.get(field, [])) != 1:
                raise ValueError(f'{path}: missing or ambiguous {field}')
        identity = get_backend().archive(path)  # Cached local ALPM read; no sync/network.
        if identity.name != metadata['pkgname'][0] or identity.version != metadata['pkgver'][0]:
            raise ValueError(f'{path}: archive/ALPM identity mismatch')
        if metadata['arch'][0] not in {'any', 'x86_64'}:
            raise ValueError(f'{path}: incompatible archive architecture')
        if identity.name in output_names and not bootstrap:
            raise ValueError(f'{path}: injected package conflicts with selected output {identity.name}')
        if bootstrap and identity.name not in output_names:
            raise ValueError(f'{path}: --bootstrap-install must name a selected output package')
        provenance = {}
        if bootstrap:
            hashes = archive.get('.BUILDINFO', {}).get('pkgbuild_sha256sum', [])
            if len(hashes) != 1 or not re.fullmatch(r'[0-9a-fA-F]{64}', hashes[0]):
                raise ValueError(f'{path}: bootstrap .BUILDINFO requires one valid pkgbuild_sha256sum')
            provenance = {'role': 'bootstrap', 'pkgbuild_sha256sum': hashes[0]}
        audit = audit_artifact(path, policy)
        require_clean(audit['findings'], str(path))
        supplied = {identity.name: identity.version}
        for value in metadata.get('provides', []):
            name, operator, version = split_dependency(value)
            if operator not in {None, '='}:
                raise ValueError(f'{path}: invalid provides {value}')
            if name in supplied and supplied[name] != version:
                raise ValueError(f'{path}: conflicting provided capability {name}')
            supplied[name] = version
        for name, version in supplied.items():
            if name in capabilities:
                raise ValueError(f'duplicate injected package/capability: {name}')
            capabilities[name] = version
        result.append({'path': str(path), 'pkgname': identity.name, 'pkgver': identity.version,
                       'sha256': digest(path), 'provides': metadata.get('provides', []),
                       'arch': metadata['arch'][0], **provenance})
    for info in infos:
        for dependency in info.depends + info.makedepends + info.checkdepends:
            name, operator, required = split_dependency(dependency)
            if name in capabilities and not version_satisfies(capabilities[name], operator, required):
                raise ValueError(f'injected {name} cannot satisfy {dependency}')
    return result


def executable_path(name):
    path = shutil.which(name)
    if path is None:
        raise ValueError(f'required executable unavailable: {name}')
    # Do not resolve symlinks: archbuild derives its configuration from argv[0].
    return os.path.abspath(path)


def plan(packages, *, repo, chroot, installs=(), bootstrap_installs=(), policy_path=None, builder='makechrootpkg', auth=None, update_baseline=False):
    """Return a concrete read-only plan. Does not evaluate recipes or run builds."""
    policy_path = policy_path or Path(__file__).resolve().parents[1]/'waypure-policy.toml'
    policy = load_policy(policy_path)
    recipes = [inspect_recipe(path, policy) for path in packages]
    inputs = inspect_installs(installs, [recipe[0] for recipe in recipes], policy, bootstrap_installs)
    if builder == 'extra-x86_64-build' and chroot.name != 'extra-x86_64':
        raise ValueError('--builder extra-x86_64-build requires --chroot to end in extra-x86_64 (the directory containing root/)')
    if update_baseline and (builder != 'makechrootpkg' or auth is None):
        raise ValueError('--update-baseline requires --builder makechrootpkg and --auth doas or sudo')
    update_assurance = ARCHBUILD_UPDATE_ASSURANCE if builder == 'extra-x86_64-build' else UPDATE_ASSURANCE
    if update_baseline:
        update_assurance = CHECKED_UPDATE_ASSURANCE
    context = baseline(chroot, update_assurance=update_assurance)
    build_user = pwd.getpwuid(os.getuid()).pw_name
    rows = []
    for directory, (info, base, warnings) in zip(packages, recipes):
        output = directory/('.waypure-validation-'+uuid.uuid4().hex+'.log')
        if builder == 'extra-x86_64-build':
            command = [builder, '-r', str(chroot.parent), '--', '-U', build_user]
        else:
            command = [builder, '-c', *([] if update_baseline else ['-u']), '-r', str(chroot)]
            if auth:
                command += ['-U', build_user]
        for archive in inputs:
            command += ['-I', archive['path']]
        command += ['--', '--force', '--check']
        environment = {'PKGDEST': str(output), 'LOGDEST': str(output),
                       'SRCPKGDEST': str(output), 'SRCDEST': str(directory)}
        if auth:
            # doas resets the environment; explicit assignments also make sudo
            # independent of its environment-preservation policy. Keep wrapper
            # basenames intact: archbuild selects its configuration from $0.
            forwarded = {key: os.environ[key] for key in
                         ('SOURCE_DATE_EPOCH', 'GNUPGHOME', 'SSH_AUTH_SOCK', 'NPROC', 'MAKEFLAGS', 'PACKAGER')
                         if key in os.environ}
            command[0] = executable_path(builder)
            command = [executable_path(auth), *(['--'] if auth == 'sudo' else []), executable_path('env'),
                       'SUDO_USER='+build_user, *[key+'='+value for key,value in {**forwarded, **environment}.items()], *command]
        update_commands = {}
        if update_baseline:
            prefix = [executable_path(auth), *(['--'] if auth == 'sudo' else []),
                      executable_path('flock'), str(chroot/'root.lock'),
                      executable_path('arch-nspawn'), str(chroot/'root')]
            update_commands = {'update_command': [*prefix, 'pacman', '-Syuu', '--noconfirm'],
                               'target_command': [*prefix, 'gcc', '-march=native', '-Q', '--help=target']}
        rows.append({'package': directory.name, 'directory': str(directory), 'pkgbase': base,
                     'pkgnames': info.pkgnames, 'version': info.version,
                     'output_architectures': {name: entry_values(entry, 'arch')
                                              for name, entry in package_fields(info.source_text).items()},
                     'source_command': ['makepkg', '--verifysource', '--nodeps'],
                     'metadata_command': ['makepkg', '--printsrcinfo'],
                     'build_command': command, 'output_directory': str(output),
                     'environment': environment, 'builder': builder, 'auth': auth or 'devtools',
                     'receipt': str(directory/RECEIPT), 'warnings': warnings, 'update_assurance': update_assurance,
                     'inputs': snapshot(directory), **update_commands})
    return {'status': 'PLAN', 'repo': str(repo), 'chroot': str(chroot), 'baseline': context, 'builder': builder, 'auth': auth or 'devtools',
            'policy': {'path': str(policy_path), 'sha256': digest(policy_path)},
            'install_archives': inputs, 'packages': rows, 'scope': SCOPE+' '+update_assurance+'.', 'update_assurance': update_assurance,
            'notes': ['Planning has not validated a clean-chroot build.',
                      'Selected recipes execute sequentially. No automatic cycle resolution or local input selection. Explicit bootstrap archives seed the working chroot only; fresh forced outputs are still required.',
                      ('Archbuild updates the pristine baseline; before/after snapshots are recorded. Its default -n installs outputs only in the working chroot; -C compares against sync packages and may skip unpublished outputs.'
                       if builder == 'extra-x86_64-build' else
                       'Validator checks a locked pristine baseline update before each fresh working copy; no unchecked working-copy update is requested.'
                       if update_baseline else 'Native makechrootpkg may not propagate update failure; this validator does not independently verify update success.'),
                      'Trusted PKGBUILDs execute on the host for metadata/source verification and native lint; builds and dependency installation occur in the working chroot only.',
                      'The validator runs as the normal user. Only its native chroot commands are elevated. Installed devtools still uses sudo internally for user switching, including with --auth doas; no host packages are installed.']}


def native(command, directory, environment, *, capture=False, logfile=None):
    if logfile is not None:
        with logfile.open('x') as stream:
            result = subprocess.run(command, cwd=directory, env=environment, text=True,
                                    stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            with logfile.open('rb') as stream:
                stream.seek(max(0, logfile.stat().st_size-16000))
                tail = stream.read().decode(errors='replace')
            raise ValueError(f'{shlex.join(command)} exited {result.returncode}; log: {logfile}\n{tail}')
        return None
    result = subprocess.run(command, cwd=directory, env=environment, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    if result.returncode:
        raise ValueError(f'{shlex.join(command)} exited {result.returncode}' +
                         (': ' + result.stderr.strip() if capture and result.stderr else ''))
    return result.stdout if capture else None


def check_generated_metadata(row, environment):
    directory = Path(row['directory'])
    actual = native(row['metadata_command'], directory, environment, capture=True)
    recorded = (directory/'.SRCINFO').read_text()
    if fields(actual) != fields(recorded) or package_fields(actual) != package_fields(recorded):
        raise ValueError(f'{directory}: generated recipe metadata differs from .SRCINFO; regenerate before validation')


def prepared_source_evidence(directory, policy):
    """Optional static language evidence; never run Cargo resolution or backends."""
    from . import adapters, language_source
    from .cli import source_path
    from .policies import load_config

    try:
        source = source_path(directory, load_config(directory),
                             version=LiteralRecipe.read(directory/'PKGBUILD').scalar('pkgver'))
        backend = adapters.detect(source)
    except (OSError, ValueError) as error:
        return None, ['Prepared source evidence unavailable: '+str(error)]
    if backend not in language_source.BACKENDS:
        return None, []
    evidence = language_source.audit(source, backend, policy)
    require_clean(evidence['findings'], 'prepared language source')
    evidence['directory'] = str(source.resolve())
    evidence['scope'] = ('Static supporting evidence from the prepared source tree; does not establish '
                         'Cargo feature activation, backend hook dependencies, or that this tree was selected by the chroot build.')
    return evidence, [item['message'] for item in evidence['findings'] if item['severity'] == 'warning']


def validate(plan_data):
    """Execute the plan; commit one atomic success receipt only after every gate."""
    for row in plan_data['packages']:
        Path(row['receipt']).unlink(missing_ok=True)
    require_normal_user()
    required = {'makepkg', 'makechrootpkg', 'namcap', 'readelf', plan_data['builder']}
    if any('update_command' in row for row in plan_data['packages']):
        required.update(('arch-nspawn', 'flock'))
    if plan_data['auth'] != 'devtools':
        required.update((plan_data['auth'], 'env'))
    for tool in sorted(required):
        if not shutil.which(tool):
            raise ValueError(f'required executable unavailable: {tool}')
    policy_path = Path(plan_data['policy']['path'])
    policy = load_policy(policy_path)
    receipts = []
    inventories = {}
    context = plan_data['baseline']
    for row in plan_data['packages']:
        directory, output = Path(row['directory']), Path(row['output_directory'])
        receipt = Path(row['receipt'])
        output.mkdir()  # Exclusive run staging; makepkg preflight also creates destination directories.
        environment = {**os.environ, **row['environment'], 'LC_ALL': 'C'}
        source_evidence, source_warnings = prepared_source_evidence(directory, policy)
        warnings = list(row['warnings']) + source_warnings
        lint_results = [run_namcap(directory/'PKGBUILD')]
        require_clean(lint_results[0]['findings'], 'recipe namcap')
        check_generated_metadata(row, environment)
        # devtools' build defaults contain --skipinteg. Verify explicitly using
        # the same SRCDEST before accepting that build; do not pass skip flags.
        native(row['source_command'], directory, environment, capture=True)
        check_generated_metadata(row, environment)
        inputs = snapshot(directory)
        for name, expected in row['inputs'].items():
            if inputs.get(name) != expected:
                raise ValueError(f'{directory}: input changed during preflight: {name}')
        require_context_unchanged(plan_data, context)
        if output_inventory(output):
            raise ValueError(f'{output}: unexpected package output before chroot build')
        baseline_before_build = context
        update_evidence = {}
        if 'update_command' in row:
            update_log = output/'baseline-update.log'
            native(row['update_command'], directory, environment, logfile=update_log)
            target_text = native(row['target_command'], directory, environment, capture=True)
            target = dict(re.findall(r'(?m)^\s*-(march|mtune)=\s+(\S+)', target_text))
            if set(target) != {'march', 'mtune'} or 'native' in target.values():
                raise ValueError('native compiler target did not resolve to an explicit CPU')
            (output/'native-target.log').write_text(target_text)
            context = baseline(Path(plan_data['chroot']), update_assurance=plan_data['update_assurance'])
            update_evidence = {'update_log': str(update_log), 'compiler_native_target': target}
        build_log = output/'build.log'
        native(row['build_command'], directory, environment, logfile=build_log)
        built_outputs = output_inventory(output)
        if plan_data['builder'] == 'extra-x86_64-build':
            # Only archbuild's privileged native invocation may transition the
            # pristine baseline. Snapshot before any host recipe evaluation.
            context = baseline(Path(plan_data['chroot']), update_assurance=plan_data['update_assurance'])
        check_generated_metadata(row, environment)
        require_outputs_unchanged(output, built_outputs)
        if snapshot(directory) != inputs:
            raise ValueError(f'{directory}: recipe/source inputs changed during build')
        require_context_unchanged(plan_data, context)
        artifacts, seen = [], set()
        for name, archive_hash in built_outputs.items():
            path = output/name
            if path.name.endswith('.sig'):
                continue
            if path.is_symlink() or not path.is_file():
                raise ValueError(f'invalid output archive: {path}')
            audit = audit_artifact(path, policy)
            require_clean(audit['findings'], str(path))
            metadata = archive_metadata(path)
            pkginfo = metadata.get('.PKGINFO', {})
            auxiliary_debug = (audit['pkgname'] == row['pkgbase']+'-debug'
                               and 'pkgtype=debug' in pkginfo.get('xdata', [])
                               and audit['pkgname'] not in row['pkgnames'])
            if (audit['pkgname'] not in row['pkgnames'] and not auxiliary_debug) or audit['pkgver'] != row['version']:
                raise ValueError(f'{path}: unexpected output identity/version')
            if audit['pkgname'] in seen:
                raise ValueError(f'duplicate output archive for {audit["pkgname"]}')
            if audit.get('pkgbase') not in {None, row['pkgbase']}:
                raise ValueError(f'{path}: unexpected output pkgbase')
            seen.add(audit['pkgname'])
            architectures = pkginfo.get('arch', [])
            expected_architectures = (set(value for values in row['output_architectures'].values() for value in values)
                                      if auxiliary_debug else row['output_architectures'][audit['pkgname']])
            if len(architectures) != 1 or architectures[0] not in expected_architectures:
                raise ValueError(f'{path}: output architecture differs from .SRCINFO')
            buildinfo = metadata.get('.BUILDINFO', {})
            provenance = buildinfo.get('pkgbuild_sha256sum', [])
            if provenance and provenance != [inputs['PKGBUILD']]:
                raise ValueError(f'{path}: .BUILDINFO PKGBUILD provenance mismatch')
            if not provenance:
                warnings.append(f'{path.name}: .BUILDINFO pkgbuild_sha256sum unavailable; source identity relies on invocation/input snapshots')
            lint = run_namcap(path)
            internal_python_dependencies(lint, path, audit)
            companions = [output/name for name in built_outputs
                          if name != path.name and not name.endswith('.sig')]
            internal_library_dependencies(lint, path, audit, companions=companions, row=row, policy=policy)
            internal_pkgconfig_dependencies(lint, path)
            if auxiliary_debug:
                companions = [output/name for name in built_outputs
                              if name != path.name and not name.endswith('.sig')]
                debug_companion_links(lint, path, companions, row)
            lint_results.append(lint)
            require_clean(lint['findings'], 'archive namcap')
            if digest(path) != archive_hash:
                raise ValueError(f'{path}: output archive changed during validation')
            artifacts.append({**audit, 'sha256': archive_hash, 'buildinfo': buildinfo, 'auxiliary_debug': auxiliary_debug,
                              'pkgbuild_sha256sum': provenance[0] if provenance else None})
        if not set(row['pkgnames']).issubset(seen):
            raise ValueError('missing current build output(s): ' + ', '.join(sorted(set(row['pkgnames'])-seen)))
        warnings.extend(item['message'] for lint in lint_results for item in lint['findings']
                        if item['severity'] == 'warning')
        warnings.extend(item['message'] for artifact in artifacts for item in artifact['findings']
                        if item['severity'] == 'warning')
        current_source, _ = prepared_source_evidence(directory, policy)
        if current_source != source_evidence:
            raise ValueError(f'{directory}: prepared source evidence changed during validation')
        # Recheck after native lint (which also evaluates trusted recipes).
        if snapshot(directory) != inputs:
            raise ValueError(f'{directory}: inputs changed during final validation')
        require_context_unchanged(plan_data, context)
        require_outputs_unchanged(output, built_outputs)
        record = {**row, 'status': 'PASS', 'completed_at': datetime.now(timezone.utc).isoformat(),
                  'inputs': inputs, 'artifacts': artifacts, 'namcap': lint_results, 'build_log': str(build_log),
                  'warnings': warnings, 'source_evidence': source_evidence, 'baseline': context,
                  'baseline_before_build': baseline_before_build,
                  **update_evidence,
                  'install_archives': plan_data['install_archives'], 'policy': plan_data['policy'],
                  'scope': plan_data['scope']}
        receipts.append(record)
        inventories[row['directory']] = built_outputs
    # Later selected recipes execute trusted host hooks too. Defer every
    # receipt until their earlier inputs and outputs have been rechecked.
    for record in receipts:
        directory = Path(record['directory'])
        if snapshot(directory) != record['inputs']:
            raise ValueError(f'{directory}: inputs changed during later package validation')
        require_outputs_unchanged(Path(record['output_directory']), inventories[record['directory']])
    require_context_unchanged(plan_data, context)
    for record in receipts:
        receipt = Path(record['receipt'])
        temporary = receipt.with_name(receipt.name+'.'+uuid.uuid4().hex+'.log')
        try:
            with temporary.open('x') as stream:
                json.dump(record, stream, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(receipt)
        finally:
            temporary.unlink(missing_ok=True)
    return {**plan_data, 'status': 'PASS', 'packages': receipts, 'baseline_after_validation': context}


def require_normal_user():
    if os.geteuid() == 0:
        raise ValueError('run waypure-validate as your normal user; host makepkg cannot run as root. Use --auth doas or --auth sudo to elevate only the native chroot commands')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Validate trusted release recipes in fresh clean-chroot copies. Build success proves sufficiency, not dependency minimality.')
    parser.add_argument('packages', nargs='+', help='selected package directories, relative to --repo')
    parser.add_argument('--repo', type=Path, default=Path.cwd())
    parser.add_argument('--chroot', type=Path, help='devtools chroot parent containing root/ (required for execution)')
    parser.add_argument('--builder', choices=('makechrootpkg', 'extra-x86_64-build'), default='makechrootpkg', help='native devtools builder; archbuild records its pristine baseline update')
    parser.add_argument('--auth', choices=('doas', 'sudo'), help='elevate only native chroot commands; default uses devtools authentication')
    parser.add_argument('--update-baseline', action='store_true', help='check a locked baseline update before each fresh makechrootpkg copy; requires --auth')
    parser.add_argument('--install', type=Path, action='append', default=[], metavar='ARCHIVE', help='explicit compatible local archive to inject into the working chroot; repeatable')
    parser.add_argument('--bootstrap-install', type=Path, action='append', default=[], metavar='ARCHIVE', help='explicit prior output archive for a bootstrap cycle; requires .BUILDINFO source provenance and a fresh forced build')
    parser.add_argument('--plan', action='store_true', help='read-only concrete plan; never evaluates PKGBUILDs or runs builds')
    parser.add_argument('--json', action='store_true', help='machine-readable plan or final report')
    parser.add_argument('--policy', type=Path, default=Path(__file__).resolve().parents[1]/'waypure-policy.toml')
    args = parser.parse_args(argv)
    try:
        repo = args.repo.resolve(strict=True)
        packages = []
        for name in args.packages:
            directory = (repo/name).resolve(strict=True)
            if directory.parent != repo or not (directory/'PKGBUILD').is_file():
                raise ValueError(f'not a direct package directory in {repo}: {directory}')
            if directory in packages:
                raise ValueError(f'duplicate selected recipe: {directory}')
            packages.append(directory)
        if not args.plan:
            # Invalidate before any recipe/tool/root checks, so failed attempts
            # never leave an older success presented as the latest validation.
            for directory in packages:
                (directory/RECEIPT).unlink(missing_ok=True)
            require_normal_user()
            if args.chroot is None:
                raise ValueError('--chroot is required for execution')
        chroot = (args.chroot or Path('/var/lib/archbuild/extra-x86_64')).resolve()
        report = plan(packages, repo=repo, chroot=chroot, installs=args.install, bootstrap_installs=args.bootstrap_install, policy_path=args.policy.resolve(), builder=args.builder, auth=args.auth, update_baseline=args.update_baseline)
        if not args.plan:
            report = validate(report)
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            print(report['status'] + ': ' + report['scope'])
            print('Baseline: ' + report['baseline']['root'])
            context = report.get('baseline_after_validation', report['baseline'])
            print('Baseline packages: ' + ', '.join(p['name']+'='+p['version'] for p in context['packages']))
            for row in report['packages']:
                print(row['package'] + ' ' + row['version'] + ': cwd=' + row['directory'])
                print('  env ' + shlex.join([key+'='+value for key,value in row['environment'].items()]))
                print('  ' + shlex.join(row['source_command']))
                if 'update_command' in row:
                    print('  ' + shlex.join(row['update_command']))
                print('  ' + shlex.join(row['build_command']))
                if 'compiler_native_target' in row:
                    print('  Native compiler target: ' + ', '.join(key+'='+value for key,value in row['compiler_native_target'].items()))
                if not args.plan:
                    print('  receipt: ' + row['receipt'])
                for warning in row['warnings']:
                    print('  WARNING: ' + warning)
            for item in report['install_archives']:
                print(('Bootstrap input: ' if item.get('role') == 'bootstrap' else 'Local input: ')+item['path']+' '+item['pkgname']+'='+item['pkgver']+' SHA256='+item['sha256'])
            if args.plan:
                print('Planning only; no clean-chroot validation has run.')
        return 0
    except (OSError, ValueError, SrcinfoError, tarfile.TarError, subprocess.SubprocessError) as error:
        parser.exit(2, 'waypure-validate: ' + str(error) + '\n')


if __name__ == '__main__':
    raise SystemExit(main())
