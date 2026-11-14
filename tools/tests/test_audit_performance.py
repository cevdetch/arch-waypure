"""Bound repeated audit work without machine-dependent timing assertions."""

import contextlib
from collections import Counter
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from optionkit import repository


class AuditPerformanceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.recipes = []
        for index in range(8):
            directory = self.root / f'pkg{index}'
            directory.mkdir()
            name = directory.name
            (directory / 'PKGBUILD').write_text(
                f'pkgname={name}-waypure\npkgver=1.0\npkgrel=1\n'
                f'provides=({name})\nconflicts=({name})\n')
            (directory / '.SRCINFO').write_text(
                f'pkgbase = {name}-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n'
                f'pkgname = {name}-waypure\n\tprovides = {name}\n\tconflicts = {name}\n')
            self.recipes.append(directory)

    def audit(self):
        output = io.StringIO()
        with patch.object(repository, 'sync_versions', return_value={}):
            with patch.object(sys, 'argv', ['waypure-audit', '--repo', str(self.root), '--metadata-only', '--json']):
                with contextlib.redirect_stdout(output):
                    result = repository.main()
        return result, json.loads(output.getvalue())

    def test_each_metadata_input_is_read_once_per_command(self):
        reads = Counter()
        read_text = Path.read_text

        def counted(path, *args, **kwargs):
            if path.name in {'PKGBUILD', '.SRCINFO'}:
                reads[path] += 1
            return read_text(path, *args, **kwargs)

        with patch.object(Path, 'read_text', counted):
            result, report = self.audit()
        self.assertEqual(result, 0, report)
        for directory in self.recipes:
            for name in ('PKGBUILD', '.SRCINFO'):
                self.assertEqual(reads[directory / name], 1, (directory, name, reads))

    def test_separate_commands_see_changed_metadata_and_dependency_paths(self):
        result, report = self.audit()
        self.assertEqual(result, 0, report)
        metadata = self.recipes[0] / '.SRCINFO'
        metadata.write_text(metadata.read_text() + '\tdepends = pkg1\n')
        other = self.recipes[1] / '.SRCINFO'
        other.write_text(other.read_text() + '\tdepends = libx11\n')
        result, report = self.audit()
        self.assertEqual(result, 1)
        paths = [item['message'] for item in report['packages'][0]['findings'] if item['code'] == 'dependency.path']
        self.assertIn('X11: pkg0 -> pkg1 -> libx11', paths)

    def test_arch_name_still_falls_back_to_directory_without_a_recipe(self):
        from optionkit.arch import pkgbase
        source = self.root / 'standalone-source'
        source.mkdir()
        self.assertEqual(pkgbase(source, {}), 'standalone-source')

    def test_invalid_metadata_does_not_require_reading_the_recipe(self):
        (self.recipes[0] / '.SRCINFO').write_text('incomplete metadata\n')
        (self.recipes[0] / 'PKGBUILD').write_bytes(b'\xff')
        result, report = self.audit()
        self.assertEqual(result, 1)
        self.assertIn('srcinfo.missing', [item['code'] for item in report['packages'][0]['findings']])
