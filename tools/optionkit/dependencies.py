"""Dependency syntax and version predicates shared by local metadata readers."""

import re

from .alpm import compare_versions


def bare_name(dep):
    return re.split(r"[<>=]", dep, maxsplit=1)[0].strip()


def split_dependency(text):
    match = re.fullmatch(r'([^<>=\s]+)(?:(<=|>=|=|<|>)([^<>=\s]+))?', text.strip())
    if match is None:
        raise ValueError(f'Invalid dependency: {text!r}')
    return match.groups()


def version_satisfies(available, operator, required):
    if operator is None:
        return True
    if available is None:
        return False
    comparison = 0 if available == required else compare_versions(available, required)
    return {'=': comparison == 0, '>': comparison > 0, '>=': comparison >= 0,
            '<': comparison < 0, '<=': comparison <= 0}[operator]
