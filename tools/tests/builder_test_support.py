"""Isolated unittest fixtures for the builder tests migrated from ~/.local/share/waypure-build."""

import contextlib
import importlib.machinery
import importlib.util
import io
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
loader = importlib.machinery.SourceFileLoader('waypure_builder_test', str(TOOLS/'waypure-build'))
spec = importlib.util.spec_from_loader(loader.name, loader)
builder = importlib.util.module_from_spec(spec)
sys.modules[loader.name] = builder
loader.exec_module(builder)


class Patches:
    def __init__(self, case):
        self.case = case

    def setattr(self, target, name, value):
        change = patch.object(target, name, value)
        change.start()
        self.case.addCleanup(change.stop)

    def setenv(self, name, value):
        change = patch.dict(os.environ, {name: value})
        change.start()
        self.case.addCleanup(change.stop)


class Captures:
    def __init__(self, case):
        self.out, self.err = io.StringIO(), io.StringIO()
        case.enterContext(contextlib.redirect_stdout(self.out))
        case.enterContext(contextlib.redirect_stderr(self.err))

    def readouterr(self):
        result = SimpleNamespace(out=self.out.getvalue(), err=self.err.getvalue())
        for stream in (self.out, self.err):
            stream.seek(0)
            stream.truncate()
        return result


class BuilderCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.wb = builder
        self.monkeypatch = Patches(self)
        self.monkeypatch.setenv('WAYPURE_CACHE', str(self.root/'cache.json'))
        self.monkeypatch.setenv('WAYPURE_ALPM_BACKEND', 'cli')
        from optionkit.alpm import reset_backend
        reset_backend()
        self.addCleanup(reset_backend)
        self.monkeypatch.setattr(builder, '_MAKEPKG_VERSION', None)
        self.capsys = Captures(self)
