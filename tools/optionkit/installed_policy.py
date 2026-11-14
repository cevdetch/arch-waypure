"""Optional installed runtime evidence, separate from planned local recipes."""

from collections import deque
from functools import lru_cache

from .alpm import BackendUnavailable
from .dependencies import split_dependency, version_satisfies


class InstalledGraph:
    def __init__(self, packages):
        self.packages = packages
        self.providers = {}
        for package in packages.values():
            for value in package.provides:
                name, operator, version = split_dependency(value)
                if operator not in {None, '='}:
                    raise ValueError(f'Invalid installed provision: {value!r}')
                self.providers.setdefault(name, []).append((package.name, version))
        self.matches = lru_cache(maxsize=4096)(version_satisfies)

    def paths(self, label, dependencies, policy):
        queue = deque((dependency, [label], True) for dependency in dependencies)
        seen, violations, uncertain = set(), set(), set()
        while queue:
            dependency, chain, certain = queue.popleft()
            try:
                name, operator, version = split_dependency(dependency)
                concrete = self.packages.get(name)
                candidates = []
                if concrete and self.matches(concrete.version, operator, version):
                    candidates = [name]
                else:
                    candidates = sorted({candidate for candidate, provided in self.providers.get(name, [])
                                         if self.matches(provided, operator, version)})
            except (ValueError, BackendUnavailable) as error:
                uncertain.add(f'Installed: cannot resolve {dependency!r}: {error}')
                continue
            if not candidates:
                uncertain.add(f'Installed: no compatible provider for {dependency} (via {" -> ".join(chain)})')
                continue
            if len(candidates) > 1:
                uncertain.add(f'Installed: Multiple compatible providers for {dependency}; paths are conditional: '
                              + ', '.join(candidates))
            certain = certain and len(candidates) == 1
            for candidate in candidates:
                package = self.packages[candidate]
                path = chain + [package.name + '@' + package.version]
                for _, kind, field, value in policy.metadata({'pkgname': [package.name],
                                                               'provides': package.provides,
                                                               'depends': [dependency]}):
                    message = f'{kind.upper()} installed: {" -> ".join(path)} ({field}: {value})'
                    violations.add((message, 'error' if certain else 'warning'))
                if (candidate, certain) in seen:
                    continue
                seen.add((candidate, certain))
                queue.extend((value, path, certain) for value in package.depends)
        return sorted(violations), sorted(uncertain)
