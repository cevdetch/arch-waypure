"""Read Python metadata without running projects or build backend hooks.

Requirements are Python distribution names, not import names or Arch package
names. Static declarations are lower bounds when metadata is dynamic. Build
backend hook requirements are always unknown to this static reader.
"""

import ast
import configparser
from email import policy as email_policy
from email.parser import Parser
from pathlib import Path
import re
import tomllib

from .core import catalogue, digest

try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
    from packaging.version import Version
except ImportError:  # Keep the existing tools usable without a new dependency.
    Requirement = SpecifierSet = Version = None

# Deliberately tiny: PyPI names cannot be mechanically mapped to Arch packages.
_INTERFACES = {'python-xlib': 'python-xlib', 'xcffib': 'python-xcffib', 'jack-client': 'jack'}
_NAME = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z')
_MAX_SOURCE = 1024 * 1024


def _normal(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _strings(value, label):
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise ValueError(f'{label}: expected an array of nonempty strings')
    return list(value)


def _table(value, label):
    if not isinstance(value, dict):
        raise ValueError(f'{label}: expected a table')
    return value


def _string(value, label):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f'{label}: expected a nonempty string')
    return value


def _read(path):
    if path.stat().st_size > _MAX_SOURCE:
        raise ValueError(f'{path.name}: metadata source exceeds {_MAX_SOURCE} bytes')
    try:
        return path.read_text(encoding='utf-8')
    except UnicodeError as error:
        raise ValueError(f'{path.name}: metadata is not UTF-8') from error


def _setup_literals(text):
    """Extract direct setup keyword literals; never evaluate names or calls."""
    try:
        tree = ast.parse(text, filename='setup.py')
    except (SyntaxError, RecursionError) as error:
        raise ValueError(f'setup.py: cannot parse metadata: {error}') from error
    nodes = list(ast.walk(tree))
    if len(nodes) > 100000:
        raise ValueError('setup.py: AST exceeds static inspection limit')
    values, unresolved = {}, set()
    recognized = {'name', 'version', 'install_requires', 'extras_require', 'setup_requires', 'python_requires'}
    # Restrict evidence to direct module-level calls of imported packaging APIs.
    # A method named setup(), a call inside a function, or a conditional call
    # cannot establish project metadata without execution.
    functions, modules = set(), set()
    for statement in tree.body:
        if isinstance(statement, ast.ImportFrom) and statement.module in {'setuptools', 'distutils.core'}:
            functions.update(alias.asname or alias.name for alias in statement.names if alias.name == 'setup')
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                if alias.name in {'setuptools', 'distutils.core'}:
                    modules.add(alias.asname or alias.name)
    def dotted(node):
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            base = dotted(node.value)
            return base + '.' + node.attr if base else None
        return None
    rebound = {node.id for node in nodes if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)}
    rebound.update(node.name for node in nodes if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    import_bindings = {}
    for node in nodes:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == '*':
                    rebound.update(functions | {name.split('.')[0] for name in modules})
                else:
                    name = alias.asname or (alias.name.split('.')[0] if isinstance(node, ast.Import) else alias.name)
                    import_bindings[name] = import_bindings.get(name, 0) + 1
    rebound.update(name for name, count in import_bindings.items() if count > 1)
    functions.difference_update(rebound)
    modules = {name for name in modules if name.split('.')[0] not in rebound}
    calls = [statement.value for statement in tree.body if isinstance(statement, ast.Expr)
             and isinstance(statement.value, ast.Call) and
             (dotted(statement.value.func) in functions or
              dotted(statement.value.func) in {name + '.setup' for name in modules})]
    for call in calls:
        for keyword in call.keywords:
            if keyword.arg is None:
                unresolved.update(recognized)
            elif keyword.arg in recognized:
                try:
                    value = ast.literal_eval(keyword.value)
                except (ValueError, TypeError, SyntaxError, RecursionError, MemoryError):
                    unresolved.add(keyword.arg)
                else:
                    if keyword.arg in values and values[keyword.arg] != value:
                        unresolved.add(keyword.arg)
                        previous = values[keyword.arg]
                        if isinstance(previous, (list, tuple)) and isinstance(value, (list, tuple)):
                            values[keyword.arg] = list(previous) + [item for item in value if item not in previous]
                        elif keyword.arg == 'extras_require' and isinstance(previous, dict) and isinstance(value, dict):
                            merged = dict(previous)
                            for extra, entries in value.items():
                                if extra not in merged:
                                    merged[extra] = entries
                                elif isinstance(merged[extra], (list, tuple)) and isinstance(entries, (list, tuple)):
                                    merged[extra] = list(merged[extra]) + [item for item in entries if item not in merged[extra]]
                            values[keyword.arg] = merged
                    else:
                        values[keyword.arg] = value
    return values, unresolved, bool(calls)


def capture(source: Path):
    """Return a Python catalogue of static source declarations and coverage.

    ``version='unknown'`` denotes unresolved identity. No setup module, backend,
    imports, file/attr directive, or project-specified command is executed.
    """
    source = Path(source)
    if not source.is_dir():
        raise ValueError(f'{source}: expected a Python source directory')
    texts = {name: _read(source / name) for name in ('pyproject.toml', 'setup.cfg', 'setup.py')
             if (source / name).is_file()}
    if not texts:
        raise ValueError(f'{source}: no Python metadata source found')
    result = {'build_requirements': [], 'runtime_requirements': [], 'optional_dependencies': {},
              'dependency_groups': {}, 'dynamic_metadata': [], 'requires_python': None,
              'build_backend': None, 'backend_path': [], 'source_kind': '+'.join(texts)}
    identity, dynamic, notes = {}, set(), []
    uncertain = set()
    runtime_complete = False
    setup_found = False
    if 'setup.py' in texts:
        literal, unresolved, setup_found = _setup_literals(texts['setup.py'])
        mapping = {'name': 'name', 'version': 'version', 'install_requires': 'dependencies',
                   'extras_require': 'optional-dependencies', 'setup_requires': 'build-requirements',
                   'python_requires': 'requires-python'}
        dynamic.update(mapping[key] for key in unresolved)
        uncertain.update(mapping[key] for key in unresolved)
        for key in ('name', 'version'):
            if key in literal and key not in unresolved:
                identity[key] = _string(literal[key], f'setup.py {key}')
        for key, target in (('install_requires', 'runtime_requirements'), ('setup_requires', 'build_requirements')):
            if key in literal:
                result[target] = _strings(list(literal[key]) if isinstance(literal[key], tuple) else literal[key], f'setup.py {key}')
        if 'extras_require' in literal:
            result['optional_dependencies'] = {_string(key, 'setup.py extra name'): _strings(list(value) if isinstance(value, tuple) else value, f'setup.py extra {key}') for key, value in
                                                _table(literal['extras_require'], 'setup.py extras_require').items()}
        if 'python_requires' in literal:
            result['requires_python'] = _string(literal['python_requires'], 'setup.py python_requires')
        # Literal AST reads may miss aliases, control flow, modifications and hooks.
        dynamic.add('backend-requirements')
        notes.append('setup.py metadata is bounded literal evidence; control flow and computed metadata are not executed.')
        if not setup_found:
            dynamic.update(('dependencies', 'version'))
            notes.append('Custom setup.py does not expose a literal setup() declaration.')
    if 'setup.cfg' in texts:
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(texts['setup.cfg'])
        except configparser.Error as error:
            raise ValueError(f'setup.cfg: {error}') from error
        def cfg_value(section, name):
            return parser.get(section, name, fallback=None)
        def cfg_requirements(value, label):
            if value.lstrip().startswith(('file:', 'attr:')):
                dynamic.add(label)
                return None
            return [line.strip() for line in value.splitlines() if line.strip() and not line.lstrip().startswith('#')]
        for key in ('name', 'version'):
            value = cfg_value('metadata', key)
            if value is not None:
                if value.lstrip().startswith(('attr:', 'file:')):
                    dynamic.add(key)
                    identity.pop(key, None)
                else:
                    identity[key] = _string(value, f'setup.cfg {key}')
        for key, target, label in (('install_requires', 'runtime_requirements', 'dependencies'),
                                   ('setup_requires', 'build_requirements', 'build-requirements')):
            value = cfg_value('options', key)
            if value is not None:
                items = cfg_requirements(value, label)
                if items is not None:
                    result[target] = items
                    uncertain.discard(label)
        if parser.has_section('options.extras_require'):
            for key, value in parser.items('options.extras_require'):
                items = cfg_requirements(value, 'optional-dependencies')
                if items is not None:
                    result['optional_dependencies'][key] = items
        value = cfg_value('options', 'python_requires')
        if value is not None:
            result['requires_python'] = _string(value, 'setup.cfg python_requires')
        notes.append('setup.cfg declarations do not establish absence of backend-supplied requirements.')
    if 'pyproject.toml' in texts:
        try:
            data = tomllib.loads(texts['pyproject.toml'])
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f'pyproject.toml: {error}') from error
        if 'build-system' in data:
            build = _table(data['build-system'], 'build-system')
            result['build_requirements'] = _strings(build.get('requires'), 'build-system.requires')
            uncertain.discard('build-requirements')
            if 'build-backend' in build:
                result['build_backend'] = _string(build['build-backend'], 'build-system.build-backend')
            if 'backend-path' in build:
                result['backend_path'] = _strings(build['backend-path'], 'build-system.backend-path')
        if 'project' in data:
            project = _table(data['project'], 'project')
            declared_dynamic = set(_strings(project.get('dynamic', []), 'project.dynamic'))
            appendable = {'authors', 'maintainers', 'classifiers', 'keywords', 'urls', 'scripts',
                          'gui-scripts', 'entry-points', 'dependencies', 'optional-dependencies',
                          'license-files', 'import-names', 'import-namespaces'}
            for key in declared_dynamic & project.keys():
                if key not in appendable or not isinstance(project[key], (list, dict)):
                    raise ValueError(f'project.{key}: scalar metadata cannot be both static and dynamic')
            # PEP 621 is authoritative for fields not delegated to the backend.
            # A non-dynamic omission means absent, not inherited legacy metadata.
            authoritative = {'name', 'version', 'dependencies', 'optional-dependencies', 'requires-python'} - declared_dynamic
            dynamic.difference_update(authoritative)
            uncertain.difference_update(authoritative)
            if 'dependencies' not in project and 'dependencies' not in declared_dynamic:
                result['runtime_requirements'] = []
            if 'optional-dependencies' not in project and 'optional-dependencies' not in declared_dynamic:
                result['optional_dependencies'] = {}
            if 'requires-python' not in project and 'requires-python' not in declared_dynamic:
                result['requires_python'] = None
            dynamic.update(declared_dynamic)
            for key in ('name', 'version'):
                if key in project:
                    identity[key] = _string(project[key], f'project.{key}')
                elif key in declared_dynamic:
                    identity.pop(key, None)
            if 'name' in declared_dynamic:
                raise ValueError('project.name cannot be dynamic')
            for key, target in (('dependencies', 'runtime_requirements'),):
                if key in project:
                    result[target] = _strings(project[key], f'project.{key}')
                    uncertain.discard(key)
            if 'optional-dependencies' in project:
                extras = _table(project['optional-dependencies'], 'project.optional-dependencies')
                result['optional_dependencies'] = {key: _strings(value, f'project.optional-dependencies.{key}')
                                                   for key, value in extras.items()}
                uncertain.discard('optional-dependencies')
            if 'requires-python' in project:
                result['requires_python'] = _string(project['requires-python'], 'project.requires-python')
            # PEP 808 permits static list/table entries plus dynamic append-only entries.
            runtime_complete = 'dependencies' not in dynamic and ('dependencies' in project or 'setup.py' not in texts)
            if 'dependencies' in declared_dynamic or 'optional-dependencies' in declared_dynamic:
                notes.append('Static dynamic list/table entries are lower bounds; the backend may append entries.')
        else:
            dynamic.update(('dependencies', 'version'))
        if 'dependency-groups' in data:
            groups = _table(data['dependency-groups'], 'dependency-groups')
            for name, items in groups.items():
                if not isinstance(items, list):
                    raise ValueError(f'dependency-groups.{name}: expected an array')
                for item in items:
                    if not (isinstance(item, str) and item.strip() or isinstance(item, dict) and
                            set(item) == {'include-group'} and isinstance(item['include-group'], str) and item['include-group']):
                        raise ValueError(f'dependency-groups.{name}: invalid requirement/include-group')
                result['dependency_groups'][name] = items
    if not runtime_complete:
        notes.append('Runtime declarations are partial; an empty list does not prove absence of dependencies.')
    result['dynamic_metadata'] = sorted(dynamic)
    result['uncertain_metadata'] = sorted(uncertain)
    result['metadata_coverage'] = {'runtime_complete': runtime_complete, 'build_complete': False,
                                   'optional_complete': runtime_complete and 'optional-dependencies' not in dynamic,
                                   'execution': 'none', 'requirements': 'static lower-bound evidence'}
    notes.append('Build backend hook requirements are unknown; no backend or project code was executed.')
    version = identity.get('version', 'unknown')
    if 'version' in dynamic:
        version = 'unknown'
    data = catalogue('python', identity.get('name', source.name), version, {},
                     {name: digest(text) for name, text in texts.items()}, **result)
    data['notes'] = notes
    return data


def _finding(code, severity, message, **fields):
    return {'code': code, 'severity': severity, 'message': message, **fields}


def _requirement(value):
    if Requirement is not None:
        req = Requirement(value)
        return req.name, req.marker, req
    # This fallback identifies names only; it does not claim PEP 508 validation.
    match = re.match(r'\s*([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(.*)\Z', value)
    if not match or (match[2].strip() and not re.match(r'\s*(?:\[|[<>=!~;(@])', match[2])):
        raise ValueError('invalid requirement')
    return match[1], value.partition(';')[2] or None, None


def _applicable(marker, extras, environment):
    if marker is None:
        return True
    if Requirement is None:
        return None
    text = str(marker)
    unquoted = re.sub(r"'[^']*'|\"[^\"]*\"", '', text)
    variables = set(re.findall(r'\b[a-zA-Z_][a-zA-Z_0-9]*\b', unquoted)) - {'and', 'or', 'in', 'not'}
    supplied = dict(environment or {})
    if variables - set(supplied) - {'extra'}:
        return None
    try:
        # A marker involving extra is positive only for an explicitly selected extra.
        values = extras if 'extra' in variables and extras else ('',)
        results = [marker.evaluate(environment={**supplied, 'extra': extra}) for extra in values]
        if 'extra' in variables and not extras:
            return None
        return any(results)
    except (ValueError, KeyError, TypeError):
        return None


def _audit_requirements(items, policy, *, extras=(), environment=None):
    findings = []
    for value, scope, optional in items:
        try:
            name, marker, _ = _requirement(value)
        except (ValueError, TypeError) as error:
            findings.append(_finding('python.requirement', 'error', f'{scope}: invalid requirement {value!r}: {error}', requirement=value))
            continue
        applicable = _applicable(marker, extras, environment)
        if applicable is False:
            continue
        arch = _INTERFACES.get(_normal(name))
        if arch is None:
            findings.append(_finding('python.unmapped', 'warning', f'{scope}: {value}: no explicit Python distribution-to-Arch policy mapping; dependency retained.', requirement=value))
            continue
        kind = policy.dependency(arch)
        if kind:
            severity = 'warning' if optional or applicable is None else 'error'
            findings.append(_finding(f'python.{kind}', severity, f'{scope}: {value} declares {arch} ({kind}); ' +
                                     ('Conditional, optional or uncertain declaration applicability is unresolved.' if severity == 'warning' else 'applicable requirement.'),
                                     requirement=value))
    return findings


def _parser_findings():
    return [] if Requirement is not None else [_finding('python.parser', 'warning',
                'Optional packaging library unavailable: requirement syntax, version and marker coverage is partial; markers remain unresolved.')]


def audit_source(source, policy, *, extras=(), environment=None):
    """Audit static requirements; environment is explicit, never the host default."""
    if not isinstance(extras, (tuple, list, set)) or not all(isinstance(extra, str) for extra in extras):
        raise ValueError('extras must be a collection of names')
    if environment is not None and (not isinstance(environment, dict) or
                                   not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items())):
        raise ValueError('environment must be a string-to-string marker environment')
    data = capture(source)
    selected = {_normal(extra) for extra in extras}
    uncertain = set(data['uncertain_metadata'])
    items = [(item, 'runtime', 'dependencies' in uncertain) for item in data['runtime_requirements']]
    items.extend((item, 'build-system', 'build-requirements' in uncertain) for item in data['build_requirements'])
    for extra, requirements in data['optional_dependencies'].items():
        items.extend((item, f'extra {extra}', _normal(extra) not in selected or 'optional-dependencies' in uncertain) for item in requirements)
    for group, requirements in data['dependency_groups'].items():
        items.extend((item, f'dependency group {group}', True) for item in requirements if isinstance(item, str))
    findings = _parser_findings()
    findings.extend(_audit_requirements(items, policy, extras=tuple(selected), environment=environment))
    findings.append(_finding('source.coverage', 'warning', 'Python metadata coverage is static lower-bound evidence; build backend hook requirements are unknown.' +
                            (' Runtime requirements are incomplete.' if not data['metadata_coverage']['runtime_complete'] else '')))
    return {'backend': 'python', 'project': data['project'], 'version': data['version'],
            'evidence': {key: data[key] for key in ('sources', 'source_kind', 'build_requirements', 'runtime_requirements',
                        'optional_dependencies', 'dependency_groups', 'dynamic_metadata', 'uncertain_metadata', 'metadata_coverage', 'requires_python')},
            'findings': findings}


def audit_distribution(text: str, label: str, policy):
    """Audit installed METADATA/PKG-INFO text; malformed identity is an error."""
    if not isinstance(text, str) or not isinstance(label, str):
        raise ValueError('distribution metadata and label must be text')
    findings = _parser_findings()
    message = Parser(policy=email_policy.compat32).parsestr(text)
    if message.defects or '\x00' in text:
        findings.append(_finding('python.metadata', 'error', f'{label}: malformed metadata header structure'))
    def values(name):
        return [' '.join(value.splitlines()).strip() for value in message.get_all(name, [])]
    for field in ('Metadata-Version', 'Name', 'Version'):
        entries = values(field)
        if len(entries) != 1 or not entries[0]:
            findings.append(_finding('python.identity', 'error', f'{label}: expected one nonempty {field} header; found {len(entries)}'))
    names, versions, metadata_versions = values('Name'), values('Version'), values('Metadata-Version')
    if names and not _NAME.fullmatch(names[0]):
        findings.append(_finding('python.identity', 'error', f'{label}: invalid distribution Name {names[0]!r}'))
    if metadata_versions and not re.fullmatch(r'[1-9]\d*\.\d+', metadata_versions[0]):
        findings.append(_finding('python.metadata', 'error', f'{label}: invalid Metadata-Version {metadata_versions[0]!r}'))
    if versions:
        try:
            if Version is not None:
                Version(versions[0])
            elif not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.!+_-]*', versions[0]):
                raise ValueError('invalid version')
        except ValueError:
            findings.append(_finding('python.identity', 'error', f'{label}: invalid Version {versions[0]!r}'))
    for field in ('Requires-Python',):
        entries = values(field)
        if len(entries) > 1:
            findings.append(_finding('python.metadata', 'error', f'{label}: duplicate {field}'))
        for entry in entries:
            try:
                if SpecifierSet is not None:
                    SpecifierSet(entry)
                elif not entry:
                    raise ValueError('empty specifier')
            except ValueError:
                findings.append(_finding('python.metadata', 'error', f'{label}: invalid {field} {entry!r}'))
    for extra in values('Provides-Extra'):
        if not _NAME.fullmatch(extra):
            findings.append(_finding('python.metadata', 'error', f'{label}: invalid Provides-Extra {extra!r}'))
    for field in values('Dynamic'):
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9-]*', field) or field.lower() in {'name', 'version', 'metadata-version', 'dynamic'}:
            findings.append(_finding('python.metadata', 'error', f'{label}: invalid Dynamic field {field!r}'))
    if values('Dynamic'):
        findings.append(_finding('python.coverage', 'warning', f'{label}: dynamic metadata fields remain unknown: {values("Dynamic")}'))
    findings.extend(_audit_requirements([(value, label, False) for value in values('Requires-Dist')], policy))
    return findings
