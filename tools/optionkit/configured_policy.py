"""Read existing configured evidence without configuring or executing a build."""

import json
from pathlib import Path
import shlex

from .analysis import read_observed
from .core import dynamic, same


def audit_build(path, data, selected, desired, policy):
    try:
        observed = read_observed(path, data)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'Invalid configured option evidence: {error}') from error
    result = {'path': str(path), 'backend': data['backend'], 'observed': observed, 'findings': []}
    seen = set()

    def finding(severity, code, message):
        key = severity, code, message
        if key not in seen:
            result['findings'].append({'severity': severity, 'code': code, 'message': message})
            seen.add(key)

    for collection, code, severity in ((selected, 'build.drift', 'warning'), (desired, 'build.policy', 'error')):
        for name, item in collection.items():
            if dynamic(item['value']):
                continue
            if name not in observed:
                finding('warning', 'build.unobserved', f'{name}: no configured value for requested {item["value"]}')
            elif not same(item['value'], observed[name], data.get('options', {}).get(name)):
                finding(severity, code, f'{name}: requested {item["value"]}, configured {observed[name]}')

    def links(arguments, context, severity='error'):
        if not isinstance(arguments, list) or not all(isinstance(value, str) for value in arguments):
            raise ValueError(f'{context}: expected a list of link arguments')
        tokens = [(token, argument) for argument in arguments if argument != '-Xlinker'
                  for token in (argument[4:].split(',') if argument.startswith('-Wl,') else [argument])]
        position = 0
        while position < len(tokens):
            argument, original = tokens[position]
            position += 1
            if argument in {'-rpath', '--rpath', '-rpath-link', '--rpath-link', '-R', '-soname', '--soname',
                            '-o', '-Map', '--version-script', '-T', '-L', '-I', '--dynamic-linker', '--sysroot'}:
                position += 1
                continue
            if argument in {'-l', '--library'} and position < len(tokens):
                value, following = tokens[position]
                position += 1
                argument = '-l' + value
                original = original + ' ' + following
            elif argument.startswith('--library='):
                argument = '-l' + argument.partition('=')[2]
            library = argument
            if argument.startswith('-l:'):
                library = argument[3:]
            elif argument.startswith('-l'):
                library = 'lib' + argument[2:] + '.so'
            elif argument.startswith('-'):
                continue
            kind = policy.library(library)
            if not kind and library.endswith('.a'):
                kind = policy.shipped_path('usr/lib/' + library.rsplit('/', 1)[-1])
            if kind:
                finding(severity, f'build.{kind}', f'{context}: configured link input {original}')

    directory = path if path.is_dir() else path.parent

    def record_list(values, label):
        if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
            raise ValueError(f'{label}: expected a list of native records')
        return values

    def records(file):
        return record_list(json.loads(file.read_text()), file)

    try:
        if data['backend'] == 'meson':
            info = directory / 'meson-info' if path.is_dir() else directory
            dependencies, targets = info / 'intro-dependencies.json', info / 'intro-targets.json'
            if dependencies.is_file():
                for entry in records(dependencies):
                    # A detected probe can be unused; target links carry stronger evidence.
                    if kind := policy.dependency(entry['name']):
                        finding('warning', f'build.{kind}', f'Meson detected dependency {entry["name"]}; review target use')
                    links(entry.get('link_args', []), 'Meson dependency ' + entry['name'], 'warning')
            else:
                finding('warning', 'build.coverage', 'Meson configured dependency evidence is unavailable')
            if targets.is_file():
                for target in records(targets):
                    for source in record_list(target.get('target_sources', []), 'Meson target sources'):
                        if 'linker' in source:
                            links(source['parameters'], 'Meson target ' + target['name'])
            else:
                finding('warning', 'build.coverage', 'Meson configured target links are unavailable; detected probes alone do not prove target use')
        elif data['backend'] in {'cmake', 'qt6'}:
            if not observed.get('CMAKE_PROJECT_NAME') or not observed.get('CMAKE_PROJECT_VERSION'):
                finding('warning', 'build.identity', 'CMake cache does not record both project and version; release identity is incomplete')
            reply = directory / '.cmake/api/v1/reply'
            indices = list(reply.glob('index-*.json'))
            if not indices:
                finding('warning', 'build.coverage', 'CMake file-api codemodel is unavailable; cache values alone do not establish configured links')
            else:
                def read_reply(name):
                    file = (reply / name).resolve()
                    if not file.is_relative_to(reply.resolve()):
                        raise ValueError('unsafe CMake file-api reference: ' + name)
                    return json.loads(file.read_text())

                index_file = max(indices, key=lambda file: file.stat().st_mtime_ns)
                cache = directory / 'CMakeCache.txt'
                stale = index_file.stat().st_mtime_ns < cache.stat().st_mtime_ns
                if stale:
                    finding('warning', 'build.stale', 'CMake file-api reply predates CMakeCache.txt; reconfigure before relying on target links')
                index = json.loads(index_file.read_text())
                models = [item for item in index['objects'] if item['kind'] == 'codemodel' and item['version']['major'] == 2]
                if not models:
                    finding('warning', 'build.coverage', 'CMake reply has no supported codemodel-v2')
                for model in models:
                    model = read_reply(model['jsonFile'])
                    if Path(model['paths']['build']).resolve() != directory.resolve():
                        raise ValueError('CMake codemodel build directory differs from the selected build')
                    home = observed.get('CMAKE_HOME_DIRECTORY')
                    if home and Path(model['paths']['source']).resolve() != Path(home).resolve():
                        raise ValueError('CMake codemodel source differs from the build cache')
                    for configuration in record_list(model['configurations'], 'CMake configurations'):
                        for reference in record_list(configuration.get('targets', []), 'CMake target references'):
                            target = read_reply(reference['jsonFile'])
                            fragments = record_list(target.get('link', {}).get('commandFragments', []), 'CMake link fragments')
                            arguments = [argument for fragment in fragments if fragment['role'] in {'libraries', 'flags'}
                                         for argument in shlex.split(fragment['fragment'])]
                            links(arguments, 'CMake target ' + target['name'], 'warning' if stale else 'error')
        finding('note', 'build.scope', 'Configured observations describe one build, not source-rule guarantees or proof against static features/dlopen; review package-specific feature graphs')
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError(f'Invalid configured dependency/target evidence: {error}') from error
    return result
