"""Typed language metadata stays separate from native configure flag arrays."""

import importlib


BACKENDS = {'cargo', 'python'}


def module(backend):
    if backend not in BACKENDS:
        raise ValueError(f'{backend}: source-check supports Cargo and Python')
    return importlib.import_module('.' + backend + '_source', __package__)


def capture(source, backend):
    return module(backend).capture(source)


def audit(source, backend, policy, **selection):
    if backend == 'python':
        if any(selection.get(name) for name in ('resolve', 'features', 'no_default_features', 'all_features', 'packages', 'cargo_home', 'manifest')):
            raise ValueError('Cargo selections do not apply to Python source checks')
        if selection.get('target', 'x86_64-unknown-linux-gnu') != 'x86_64-unknown-linux-gnu':
            raise ValueError('Cargo target selection does not apply to Python source checks')
        options = {'extras': selection.get('extras', ())}
    else:
        if selection.get('extras'):
            raise ValueError('Python extras do not apply to Cargo source checks')
        options = {name: selection[name] for name in
                   ('resolve', 'features', 'no_default_features', 'all_features', 'packages', 'target', 'cargo_home', 'manifest')
                   if name in selection and selection[name] is not None}
    return module(backend).audit_source(source, policy, **options)
