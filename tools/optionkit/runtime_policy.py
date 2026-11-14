"""Known forbidden package/ELF families, separate from native build flags."""

from dataclasses import dataclass
import fnmatch
import posixpath
import re
import tomllib


@dataclass(frozen=True)
class RuntimePolicy:
    rules: tuple
    paths: tuple = ()

    def dependency(self, value):
        # Optional dependency descriptions use ': '; SONAME namespaces do not.
        value = value.partition(': ')[0]
        name = re.split(r'[<>=]', value, maxsplit=1)[0].strip()
        # makepkg accepts both libX11.so=6-64 and lib:libX11.so.6 forms.
        library = name.split(':', 1)[-1]
        package = name.lower().removeprefix('lib32-').removesuffix('-waypure')
        for kind, dependencies, libraries in self.rules:
            if dependencies.match(package) or libraries.match(library):
                return kind
        return None

    def library(self, name):
        # DT_NEEDED may contain absolute, relative or $ORIGIN-prefixed paths.
        name = name.rsplit('/', 1)[-1]
        return next((kind for kind, _, libraries in self.rules if libraries.match(name)), None)

    def metadata(self, fields):
        """Yield policy evidence for output capabilities and dependency fields."""
        for key, values in fields.items():
            field = key.removesuffix('_x86_64')
            if field not in {'pkgname', 'provides', 'depends', 'makedepends', 'checkdepends', 'optdepends'}:
                continue
            for value in values:
                if kind := self.dependency(value):
                    yield ('warning' if field == 'optdepends' else 'error', kind, key, value)

    def shipped_path(self, name):
        # Archive paths describe the installed filesystem, whose root clamps '..'.
        name = posixpath.normpath('/' + name.lstrip('/')).lstrip('/')
        return next((kind for kind, patterns in self.paths if patterns.match(name)), None)


def load_policy(path):
    data = tomllib.loads(path.read_text())

    def patterns(table, section, prefix):
        label = prefix + section + '.forbidden'
        values = table.get(section, {})
        if not isinstance(values, dict):
            raise ValueError(f'{path}: {prefix}{section} must be a table')
        values = values.get('forbidden')
        if not isinstance(values, list) or not all(isinstance(value, str) and value for value in values):
            raise ValueError(f'{path}: {label} must be a list of names/patterns')
        if section == 'dependencies':
            values = [value.lower() for value in values]
        # Compile once, rather than walking every glob for each ELF/dependency.
        return re.compile('|'.join(fnmatch.translate(value) for value in values) or r'(?!)')

    tables = [('x11', data, '')]
    if 'pipewire' in data:
        if not isinstance(data['pipewire'], dict):
            raise ValueError(f'{path}: pipewire must be a table')
        tables.append(('pipewire', data['pipewire'], 'pipewire.'))
    return RuntimePolicy(tuple((kind, patterns(table, 'dependencies', prefix),
                               patterns(table, 'libraries', prefix))
                              for kind, table, prefix in tables),
                         tuple((kind, patterns(table, 'paths', prefix))
                               for kind, table, prefix in tables if 'paths' in table))
