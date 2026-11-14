"""One local runtime provider index per audit command, preserving split outputs."""

from collections import deque
from functools import lru_cache

from .alpm import BackendUnavailable
from .dependencies import split_dependency, version_satisfies
from .metadata import entry_values, package_fields


def satisfies(available, operator, requested):
    try:
        return version_satisfies(available, operator, requested)
    except BackendUnavailable:
        return None


class LocalGraph:
    def __init__(self, infos):
        self.outputs = {}
        providers, nodes = self.providers, self.nodes = {}, {}
        for info in infos.values():
            outputs = package_fields(info.source_text)
            self.outputs[info.dir] = outputs
            for output, entry in outputs.items():
                node = (info.dir, output)
                nodes[node] = entry
                label = info.dir.name + (":" + output if len(outputs) > 1 else "")
                provides = [(output, info.version)]
                for value in entry_values(entry, "provides"):
                    name, separator, version = value.partition("=")
                    provides.append((name, version if separator else None))
                for name, version in set(provides):
                    providers.setdefault(name, []).append((node, version, label))
        self.starts = {}
        for node in nodes:
            self.starts.setdefault(node[0], []).append(node)
        self.satisfies = lru_cache(maxsize=4096)(satisfies)

    def paths(self, start, policy):
        queue = deque((node, [start.name], True) for node in self.starts.get(start, ()))
        seen, violations, uncertain = set(), set(), set()
        while queue:
            node, chain, certain = queue.popleft()
            if (node, certain) in seen:
                continue
            seen.add((node, certain))
            for dependency in entry_values(self.nodes[node], "depends"):
                try:
                    name, operator, required = split_dependency(dependency)
                except ValueError:
                    uncertain.add(f"Cannot resolve dependency {dependency!r}")
                    continue
                if kind := policy.dependency(name):
                    violations.add((kind.upper() + ': ' + " -> ".join(chain + [name]), "error" if certain else "warning"))
                candidates = {}
                for candidate, version, label in self.providers.get(name, []):
                    matches = self.satisfies(version, operator, required)
                    if matches is None:
                        uncertain.add(f"Cannot prove {candidate[1]} satisfies {dependency}; vercmp unavailable")
                    elif matches:
                        candidates[candidate] = label
                if len(candidates) > 1:
                    uncertain.add(f"Multiple compatible local providers for {dependency}; paths are conditional")
                for candidate, label in candidates.items():
                    queue.append((candidate, chain + [label], certain and len(candidates) == 1))
        return sorted(violations), sorted(uncertain)
