"""Cargo declarations and opt-in frozen resolver evidence; never build a crate."""

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tomllib

from .core import catalogue


# Crate and Arch namespaces are independent. Only reviewed identities map here.
CRATE_CAPABILITIES = {
    'x11': 'libx11', 'x11-dl': 'libx11', 'x11rb': 'libxcb',
    'x11rb-protocol': 'libxcb', 'xcb': 'libxcb', 'xcb-sys': 'libxcb',
    'glutin_glx_sys': 'libglx', 'jack': 'jack', 'jack-sys': 'jack',
}
DEPENDENCY_SECTIONS = ('dependencies', 'build-dependencies', 'dev-dependencies')
# This variable only selects an Apple deployment version. Keep the review
# bounded to its numeric form; Cargo still applies configuration precedence.
REVIEWED_CONFIG_ENV = {'MACOSX_DEPLOYMENT_TARGET': re.compile(r'[0-9]+(?:\.[0-9]+){0,2}')}


def _table(value, context):
    if not isinstance(value, dict):
        raise ValueError(f'{context}: expected a TOML table')
    return value


def _read(path):
    try:
        return tomllib.loads(path.read_text())
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f'{path}: {error}') from error


def _files(source):
    """Bound source discovery to the selected tree, omitting generated trees."""
    for directory, children, files in os.walk(source):
        children[:] = sorted(name for name in children if name not in {'.git', 'target', 'pkg', '.venv', 'node_modules'})
        for name in sorted(files):
            if name in {'Cargo.toml', 'Cargo.lock'} or Path(directory).name == '.cargo' and name in {'config', 'config.toml'}:
                yield Path(directory) / name


def _label(path, root):
    return str(path.relative_to(root))


def capture(source):
    """Capture TOML declarations, features and input hashes without invoking Cargo."""
    source = Path(source).resolve()
    manifest = source if source.is_file() else source / 'Cargo.toml'
    root = manifest.parent
    documents = {path: _read(path) for path in _files(root) if path.name == 'Cargo.toml'}
    if manifest not in documents:
        documents[manifest] = _read(manifest)
    top = documents[manifest]
    workspace = _table(top.get('workspace', {}), manifest)
    inherited_package = _table(workspace.get('package', {}), f'{manifest}: workspace.package')
    _table(workspace.get('dependencies', {}), f'{manifest}: workspace.dependencies')
    packages, scripts = [], set()
    for file, document in sorted(documents.items()):
        if 'package' not in document:
            continue
        context = _label(file, root)
        package = _table(document['package'], context + ': package')
        # Nested independent workspaces and explicit package.workspace values
        # must not borrow the selected root workspace's declarations.
        candidates = [file.parent / 'Cargo.toml'] + [parent / 'Cargo.toml' for parent in file.parent.parents if parent.is_relative_to(root)]
        if 'workspace' in package:
            if not isinstance(package['workspace'], str):
                raise ValueError(f'{context}: package.workspace must be a path')
            candidates = [(file.parent / package['workspace'] / 'Cargo.toml').resolve()]
        local_workspace = next((documents[path]['workspace'] for path in candidates
                                if path in documents and 'workspace' in documents[path]), {})
        local_workspace = _table(local_workspace, context + ': workspace')
        local_package = _table(local_workspace.get('package', {}), context + ': workspace.package')
        local_dependencies = _table(local_workspace.get('dependencies', {}), context + ': workspace.dependencies')
        name = package.get('name')
        if not isinstance(name, str) or not name:
            raise ValueError(f'{context}: package.name must be a string')
        version = package.get('version')
        if isinstance(version, dict) and version.get('workspace') is True:
            version = local_package.get('version')
        if version is not None and not isinstance(version, str):
            raise ValueError(f'{context}: package.version must be a string or workspace inheritance')
        features = _table(document.get('features', {}), context + ': features')
        if not all(isinstance(values, list) and all(isinstance(value, str) for value in values) for values in features.values()):
            raise ValueError(f'{context}: feature declarations must be lists of strings')
        dependencies = []

        def declarations(table, target=None):
            for section in DEPENDENCY_SECTIONS:
                entries = _table(table.get(section, {}), f'{context}: {section}')
                for alias, raw in sorted(entries.items()):
                    if isinstance(raw, str):
                        value = {'version': raw}
                    elif isinstance(raw, dict):
                        value = dict(raw)
                    else:
                        raise ValueError(f'{context}: dependency {alias} must be a version or table')
                    for field in ('optional', 'default-features', 'workspace'):
                        if field in value and not isinstance(value[field], bool):
                            raise ValueError(f'{context}: dependency {alias} {field} must be a boolean')
                    if 'features' in value and (not isinstance(value['features'], list)
                            or not all(isinstance(item, str) for item in value['features'])):
                        raise ValueError(f'{context}: dependency {alias} features must be a list of strings')
                    inherited = value.get('workspace') is True
                    unresolved = False
                    if inherited:
                        shared = local_dependencies.get(alias)
                        if isinstance(shared, str):
                            shared = {'version': shared}
                        if isinstance(shared, dict):
                            shared = dict(shared)
                            # Workspace feature declarations are additive.
                            extra = value.get('features', [])
                            value = {**shared, **value}
                            if extra:
                                if not isinstance(extra, list) or not isinstance(shared.get('features', []), list):
                                    raise ValueError(f'{context}: dependency {alias} features must be a list')
                                value['features'] = list(dict.fromkeys(shared.get('features', []) + extra))
                        else:
                            unresolved = True
                    actual = value.get('package', alias)
                    if not isinstance(actual, str):
                        raise ValueError(f'{context}: dependency {alias} package must be a string')
                    dependencies.append({'alias': alias, 'package': actual, 'kind': section,
                                         'target': target, 'optional': value.get('optional', False),
                                         'workspace_inherited': inherited, 'unresolved': unresolved,
                                         'declaration': value})
        declarations(document)
        for target, table in sorted(_table(document.get('target', {}), context + ': target').items()):
            declarations(_table(table, f'{context}: target {target}'), target)
        build = package.get('build')
        if build not in (None, False, True) and not isinstance(build, str):
            raise ValueError(f'{context}: package.build must be a path or boolean')
        build_path = file.parent / (build if isinstance(build, str) else 'build.rs')
        if build is not False and build_path.is_file():
            resolved_script = build_path.resolve()
            if resolved_script.is_relative_to(root):
                scripts.add(_label(resolved_script, root))
        packages.append({'name': name, 'version': version, 'manifest': context,
                         'features': features, 'dependencies': dependencies,
                         'build': build, 'links': package.get('links')})
    top_package = next((item for item in packages if item['manifest'] == _label(manifest, root)), None)
    version = top_package['version'] if top_package else inherited_package.get('version')
    origin = 'package.version' if top_package else 'workspace.package.version'
    if not version:
        version, origin = 'unknown', 'unknown'
    project = top_package['name'] if top_package else root.name
    inputs = set(_files(root)) | set(documents)
    inputs.update(root / name for name in scripts)
    hashes = {_label(path, root): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(inputs)}
    data = catalogue('cargo', project, version, {}, sources=hashes, version_origin=origin,
                     cargo={'manifest': _label(manifest, root), 'workspace': workspace,
                            'manifests': sorted(_label(path, root) for path in documents),
                            'packages': packages, 'build_scripts': sorted(scripts),
                            'lockfiles': sorted(_label(path, root) for path in inputs if path.name == 'Cargo.lock')})
    data['notes'] = ['Cargo feature declarations are separate from generic build options; declaration presence does not establish activation.']
    return data


def _resolved(metadata, selectors, manifest):
    """Validate native shapes before traversing non-dev, target-filtered edges."""
    if not isinstance(metadata, dict):
        raise ValueError('Cargo metadata must be an object')
    values = metadata.get('packages')
    if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
        raise ValueError('Cargo metadata packages must be objects')
    packages = {}
    for value in values:
        if not all(isinstance(value.get(key), str) for key in ('id', 'name', 'version', 'manifest_path')):
            raise ValueError('Cargo metadata package identity is incomplete')
        if value['id'] in packages:
            raise ValueError('Cargo metadata contains duplicate package IDs')
        packages[value['id']] = value
    members = metadata.get('workspace_members')
    if not isinstance(members, list) or not all(isinstance(value, str) and value in packages for value in members):
        raise ValueError('Cargo metadata workspace_members are invalid')
    resolve = metadata.get('resolve')
    if not isinstance(resolve, dict) or not isinstance(resolve.get('nodes'), list):
        raise ValueError('Cargo metadata has no resolver nodes')
    nodes = {}
    for node in resolve['nodes']:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or node['id'] not in packages
                or not isinstance(node.get('features'), list) or not all(isinstance(item, str) for item in node['features'])
                or not isinstance(node.get('deps'), list)):
            raise ValueError('Cargo metadata resolver node is invalid')
        for edge in node['deps']:
            if (not isinstance(edge, dict) or not isinstance(edge.get('name'), str) or edge.get('pkg') not in packages
                    or not isinstance(edge.get('dep_kinds'), list) or not edge['dep_kinds']):
                raise ValueError('Cargo metadata dependency edge is invalid')
            for kind in edge['dep_kinds']:
                if (not isinstance(kind, dict) or kind.get('kind') not in (None, 'normal', 'build', 'dev')
                        or kind.get('target') is not None and not isinstance(kind['target'], str)):
                    raise ValueError('Cargo metadata dependency kind is invalid')
        if node['id'] in nodes:
            raise ValueError('Cargo metadata contains duplicate resolver node IDs')
        nodes[node['id']] = node
    if not all(edge['pkg'] in nodes for node in nodes.values() for edge in node['deps']):
        raise ValueError('Cargo metadata is missing dependency resolver nodes')
    selected = []
    if selectors:
        for selector in selectors:
            matches = [identity for identity in members if selector in
                       (identity, packages[identity]['name'], packages[identity]['name'] + '@' + packages[identity]['version'])]
            if len(matches) != 1:
                raise LookupError(f'unknown or ambiguous workspace package selection: {selector}')
            selected.extend(matches)
    else:
        # A member manifest defaults to its package; virtual roots use Cargo defaults.
        workspace_root = metadata.get('workspace_root')
        if not isinstance(workspace_root, str):
            raise ValueError('Cargo metadata workspace_root is invalid')
        at_workspace_root = manifest.resolve() == (Path(workspace_root) / 'Cargo.toml').resolve()
        selected = [] if at_workspace_root else [identity for identity in members if Path(packages[identity]['manifest_path']).resolve() == manifest.resolve()]
        if not selected:
            selected = metadata.get('workspace_default_members', members)
            if not isinstance(selected, list) or not all(identity in members for identity in selected):
                raise ValueError('Cargo metadata default workspace members are invalid')
    selected = sorted(set(selected))
    if not all(identity in nodes for identity in selected):
        raise ValueError('Cargo metadata is missing selected resolver nodes')
    reached = {'runtime': set(), 'build': set()}
    pending = [(identity, 'runtime') for identity in selected]
    edges = []
    while pending:
        identity, scope = pending.pop()
        if identity in reached[scope]:
            continue
        reached[scope].add(identity)
        for edge in nodes[identity]['deps']:
            for kind in edge['dep_kinds']:
                if kind['kind'] == 'dev':
                    continue
                child_scope = 'build' if scope == 'build' or kind['kind'] == 'build' else 'runtime'
                edges.append({'from': identity, 'alias': edge['name'], 'package': edge['pkg'],
                              'kind': kind['kind'] or 'normal', 'target': kind.get('target'), 'scope': child_scope})
                pending.append((edge['pkg'], child_scope))
    def record(identity):
        value = packages[identity]
        return {key: value.get(key) for key in ('id', 'name', 'version', 'source', 'manifest_path')}
    return {'selected_ids': selected, 'selected_packages': sorted(packages[value]['name'] for value in selected),
            'runtime': [record(value) for value in sorted(reached['runtime'])],
            'build': [record(value) for value in sorted(reached['build'])], 'edges': edges,
            'features': {value: sorted(nodes[value]['features']) for value in sorted(reached['runtime'] | reached['build'])}}


def _toolchain_environment(environment, root):
    """Rustup proxies must use an installed default, ignoring source overrides."""
    executable = shutil.which('cargo')
    compiler = shutil.which('rustc', path=os.defpath) or shutil.which('rustc')
    if not compiler:
        raise ValueError('An installed Rust compiler is required for metadata platform filtering')
    proxy = next((Path(path).resolve() for path in (executable, compiler)
                  if path and Path(path).resolve().name == 'rustup'), None)
    if proxy:
        process = subprocess.run([str(proxy), 'toolchain', 'list'],
                                 cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        toolchain = next((line.split()[0] for line in process.stdout.splitlines()
                          if re.search(r'\([^)]*\bdefault\b[^)]*\)', line)), None)
        if process.returncode or not toolchain:
            raise ValueError('Rustup has no installed default toolchain; automatic installation is disabled')
        environment['RUSTUP_TOOLCHAIN'] = toolchain
        process = subprocess.run([str(proxy), 'which', '--toolchain', toolchain, 'rustc'],
                                 cwd=root, env=environment, text=True, capture_output=True, timeout=30)
        compiler = process.stdout.strip()
        if process.returncode or not Path(compiler).is_absolute() or not Path(compiler).is_file():
            raise ValueError('Cannot locate the installed default Rust compiler')
    # Cargo metadata queries rustc for target information. Source-configured
    # compilers/wrappers must never become an implicit project execution path.
    environment.update(RUSTC=str(Path(compiler).resolve()), RUSTC_WRAPPER='', RUSTC_WORKSPACE_WRAPPER='',
                       CARGO_BUILD_RUSTC=str(Path(compiler).resolve()), CARGO_BUILD_RUSTC_WRAPPER='',
                       CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER='')
    return environment


def _configuration_inputs(root, environment):
    """Inspect every Cargo config search location before spawning native tools."""
    directories = [root, *root.parents]
    cargo_home = Path(environment.get('CARGO_HOME', str(Path.home() / '.cargo')))
    if not cargo_home.is_absolute():
        cargo_home = root / cargo_home
    locations = [directory / '.cargo' for directory in directories] + [cargo_home]
    result = {}
    for location in locations:
        path = next((location / name for name in ('config', 'config.toml') if (location / name).is_file()), None)
        if path is None:
            continue
        data = _read(path)
        for name, value in _table(data.get('env', {}), f'{path}: env').items():
            if name not in REVIEWED_CONFIG_ENV:
                raise ValueError(f'{path}: native metadata resolution rejects unreviewed [env] variable {name}; only numeric MACOSX_DEPLOYMENT_TARGET is permitted')
            if isinstance(value, dict):
                if (set(value) - {'value', 'force', 'relative'}
                        or not isinstance(value.get('force', False), bool)
                        or not isinstance(value.get('relative', False), bool)):
                    raise ValueError(f'{path}: invalid [env] {name} configuration')
                if value.get('relative', False):
                    raise ValueError(f'{path}: [env] {name} cannot use relative=true for a deployment version')
                # Cargo merges environment tables across its configuration
                # search paths. A force-only table may inherit a value; Cargo
                # must diagnose an absent value after performing that merge.
                if 'value' not in value:
                    continue
                value = value['value']
            if (not isinstance(value, str) or len(value) > 32
                    or not REVIEWED_CONFIG_ENV[name].fullmatch(value)):
                raise ValueError(f'{path}: [env] {name} must be a numeric deployment version')
        result[str(path.resolve())] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def audit_source(source, policy, *, resolve=False, features=(), no_default_features=False,
                 all_features=False, packages=(), target='x86_64-unknown-linux-gnu',
                 cargo_home=None, manifest=None):
    """Report declarations, optionally supplementing with frozen Cargo metadata."""
    source = Path(source).resolve()
    source_manifest = source if source.is_file() else source / 'Cargo.toml'
    data = capture(source)
    root = source_manifest.parent
    chosen = Path(manifest) if manifest is not None else source_manifest
    if not chosen.is_absolute():
        chosen = root / chosen
    chosen = chosen.resolve()
    if not chosen.is_relative_to(root) or not chosen.is_file():
        raise ValueError('Cargo manifest must be an existing file within the selected source tree')
    result = {'backend': 'cargo', 'project': data['project'], 'version': data['version'],
              'evidence': {'sources': data['sources'], 'declarations': data['cargo']}, 'findings': []}

    def finding(severity, code, message):
        result['findings'].append({'severity': severity, 'code': code, 'message': message})

    if data['version_origin'] == 'unknown':
        finding('warning', 'source.identity', 'Cargo workspace has no declared release version; package recipe identity must be reviewed')
    for package in data['cargo']['packages']:
        for dependency in package['dependencies']:
            capability = CRATE_CAPABILITIES.get(dependency['package'])
            kind = policy.dependency(capability) if capability else None
            if kind:
                scope = dependency['kind'] + (' optional' if dependency['optional'] else '')
                if dependency['target']:
                    scope += ' target=' + dependency['target']
                finding('warning', 'source.' + kind,
                        f'{package["manifest"]}: {scope} declaration {dependency["alias"]} (crate {dependency["package"]}); activation is unverified')
            if dependency['unresolved']:
                finding('warning', 'source.coverage', f'{package["manifest"]}: dependency {dependency["alias"]} inherits an unavailable workspace declaration')
    finding('warning', 'source.coverage',
            'Cargo declarations include optional, inactive, development and other-target dependencies. Known crate mappings cannot prove arbitrary wrappers, static protocols, dlopen or native build-script output; feature names alone do not establish a forbidden capability.')
    if not resolve:
        return result
    command = ['cargo', 'metadata', '--format-version=1', '--frozen', '--filter-platform', target,
               '--manifest-path', str(chosen)]
    if no_default_features:
        command.append('--no-default-features')
    if all_features:
        command.append('--all-features')
    if features:
        command.extend(['--features', ','.join(features)])
    environment = dict(os.environ, RUSTUP_AUTO_INSTALL='0', CARGO_NET_OFFLINE='true',
                       CARGO_ENCODED_RUSTFLAGS='')
    environment.pop('RUSTC_BOOTSTRAP', None)
    if cargo_home is not None:
        environment['CARGO_HOME'] = str(Path(cargo_home).resolve())
    result['evidence']['request'] = {'command': command, 'packages': list(packages), 'target': target,
                                     'features': list(features), 'no_default_features': no_default_features,
                                     'all_features': all_features, 'manifest': str(chosen),
                                     'compiler_query': {
                                         'rustflags': 'suppressed', 'bootstrap': 'removed',
                                         'reviewed_environment': sorted(REVIEWED_CONFIG_ENV),
                                         'configuration_semantics': 'Cargo retains merging and force precedence; reviewed environment entries are not rewritten.'}}
    try:
        try:
            configs = _configuration_inputs(root, environment)
        except ValueError as error:
            finding('error', 'cargo.config', str(error))
            return result
        result['evidence']['request']['configurations'] = configs
        environment = _toolchain_environment(environment, root)
        result['evidence']['request']['compiler'] = environment['RUSTC']
        if 'RUSTUP_TOOLCHAIN' in environment:
            result['evidence']['request']['toolchain'] = environment['RUSTUP_TOOLCHAIN']
        process = subprocess.run(command, cwd=root, env=environment, text=True, capture_output=True, timeout=120)
        # --frozen is Cargo's mutation guard; hashes make unexpected input drift visible.
        after = capture(source)['sources']
        if after != data['sources']:
            finding('error', 'cargo.changed', 'Cargo source or lockfile inputs changed during frozen metadata resolution; evidence is unusable')
            return result
        if _configuration_inputs(root, environment) != configs:
            finding('error', 'cargo.changed', 'Cargo configuration changed during metadata resolution; evidence is unusable')
            return result
        if process.returncode:
            finding('error', 'cargo.metadata', 'Frozen Cargo metadata failed (lockfile, installed toolchain and cached dependencies are required): ' + process.stderr.strip())
            return result
        resolved = _resolved(json.loads(process.stdout), packages, chosen)
        result['evidence']['resolved'] = resolved
        for scope in ('runtime', 'build'):
            for package in resolved[scope]:
                capability = CRATE_CAPABILITIES.get(package['name'])
                kind = policy.dependency(capability) if capability else None
                if kind:
                    finding('warning', 'cargo.' + kind,
                            f'{scope} resolver graph reaches crate {package["name"]} {package["version"]} ({package["id"]}); selected build activation requires review')
        finding('warning', 'cargo.scope',
                'Cargo metadata reports a workspace resolver feature union, including development and host/target contexts; graph traversal excludes dev edges and separates build dependencies, but cannot undo feature unification or prove the exact selected build. Compiler query rustflags are suppressed and inherited RUSTC_BOOTSTRAP is removed to prevent plugin execution; custom cfg activation from build flags is unobserved and target-conditional dependencies may differ. Resolved feature names are review evidence, not capability policy. No build scripts were executed.')
    except LookupError as error:
        finding('error', 'cargo.selection', str(error))
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, subprocess.TimeoutExpired) as error:
        finding('error', 'cargo.metadata', f'Cargo metadata evidence is unavailable or malformed: {error}')
    return result
