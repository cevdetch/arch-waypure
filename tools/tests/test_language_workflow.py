"""Cargo/Python reviews use typed evidence, not invented configure switches."""

import json
import io
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import tarfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))

from optionkit import adapters
from optionkit.core import catalogue, compare, load, record_catalogue


class LanguageWorkflowCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.package = self.root / 'demo'
        self.source = self.package / 'src/demo-1.0'
        self.source.mkdir(parents=True)
        (self.package / 'PKGBUILD').write_text('pkgname=demo-waypure\npkgver=1.0\npkgrel=1\nprovides=(demo)\nconflicts=(demo)\n')
        (self.package / '.SRCINFO').write_text('pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\npkgname = demo-waypure\n\tprovides = demo\n\tconflicts = demo\n')

    def tool(self, name, *args):
        return subprocess.run([str(TOOLS / name), *map(str, args)], cwd=self.package,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                              text=True, capture_output=True)

    def python_source(self):
        (self.source / 'pyproject.toml').write_text('[project]\nname="demo"\nversion="1.0"\ndependencies=["requests>=2"]\n[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n')

    def cargo_source(self):
        (self.source / 'Cargo.toml').write_text('[package]\nname="demo"\nversion="1.0"\n[features]\ndefault=[]\nwayland=[]\n')

    def test_python_epoch_catalogue_can_be_saved_and_loaded(self):
        for backend, version in (('python', '1!2.0'), ('cmake', '1.0_sdk')):
            data = catalogue(backend, 'demo', version, {})
            path, _ = record_catalogue(data, self.root)
            self.assertEqual(load(path)['version'], version)

    def test_malformed_language_payloads_are_rejected_on_load(self):
        malformed = [('cargo', {'cargo': []}),
                     ('cargo', {'cargo': {'packages': [42]}}),
                     ('cargo', {'cargo': {'packages': [{'manifest': []}]}}),
                     ('python', {'runtime_requirements': 'requests'}),
                     ('python', {'optional_dependencies': {'extra': 'requests'}})]
        for backend, payload in malformed:
            with self.subTest(payload=payload):
                path = self.root / 'malformed.json.log'
                path.write_text(json.dumps(catalogue(backend, 'demo', '1.0', {}, **payload)))
                with self.assertRaises(ValueError):
                    load(path)

    def test_language_source_detection_and_snapshots(self):
        self.python_source()
        with (self.source / 'pyproject.toml').open('a') as stream:
            stream.write('[dependency-groups]\ntest=["pytest", {include-group="lint"}]\nlint=["ruff"]\n')
        self.assertEqual(adapters.detect(self.source), 'python')
        output = self.root / 'python.json.log'
        result = self.tool('waypure-options', 'snapshot', self.source, '-o', output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('requests>=2', load(output)['runtime_requirements'])
        self.assertIn({'include-group': 'lint'}, load(output)['dependency_groups']['test'])
        (self.source / 'pyproject.toml').unlink()
        self.cargo_source()
        self.assertEqual(adapters.detect(self.source), 'cargo')
        result = self.tool('waypure-options', 'snapshot', self.source, '-o', self.root / 'cargo.json.log')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(load(self.root / 'cargo.json.log')['backend'], 'cargo')

    def test_language_review_and_check_do_not_require_configure_arrays(self):
        self.python_source()
        result = self.tool('waypure-options', 'review')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.package / 'options/1.0.source.json.log').is_file())
        self.assertFalse((self.package / 'options/1.0.options.sh.log').exists())
        result = self.tool('waypure-options', 'check', '1.0')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        result = self.tool('waypure-options', 'render', '1.0')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source-check', result.stderr)

    def test_source_check_and_repository_audit_expose_python_source_evidence(self):
        self.python_source()
        result = self.tool('waypure-options', 'source-check', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['backend'], 'python')
        result = self.tool('waypure-audit', '--repo', self.root, 'demo', '--json')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        row = json.loads(result.stdout)['packages'][0]
        self.assertIn(row['checks']['source'], {'PASS', 'WARN'})
        self.assertEqual(row['source']['backend'], 'python')
        self.assertFalse(any(item['code'] == 'options.unsupported' for item in row['findings']))

    def test_python_requirement_changes_appear_in_release_diffs(self):
        before = catalogue('python', 'demo', '1.0', {}, runtime_requirements=['requests>=2'])
        after = catalogue('python', 'demo', '1.1', {}, runtime_requirements=['httpx>=1'])
        text = compare(before, after)
        self.assertIn('requests>=2', text)
        self.assertIn('httpx>=1', text)

    def test_cargo_feature_changes_appear_in_release_diffs(self):
        before = catalogue('cargo', 'demo', '1.0', {}, cargo={'packages': [{'name': 'demo', 'manifest': 'Cargo.toml', 'features': {'default': []}, 'dependencies': []}]})
        after = catalogue('cargo', 'demo', '1.1', {}, cargo={'packages': [{'name': 'demo', 'manifest': 'Cargo.toml', 'features': {'default': ['wayland'], 'wayland': []}, 'dependencies': []}]})
        self.assertIn('wayland', compare(before, after))

    def test_cargo_workspace_identity_and_source_changes_appear_in_diffs(self):
        before = catalogue('cargo', 'demo', '1.0', {}, sources={'Cargo.lock': 'old'},
                           cargo={'workspace': {'resolver': '2'}, 'packages': [{'manifest': 'Cargo.toml', 'name': 'demo'}]})
        after = catalogue('cargo', 'demo', '1.1', {}, sources={'Cargo.lock': 'new'},
                          cargo={'workspace': {'resolver': '3'}, 'packages': [{'manifest': 'Cargo.toml', 'name': 'application'}]})
        result = compare(before, after)
        for value in ('workspace', 'resolver', 'application', 'Cargo.lock'):
            self.assertIn(value, result)

    def test_cached_language_catalogues_without_source_do_not_enter_option_checks(self):
        for backend in ('python', 'cargo'):
            with self.subTest(backend=backend):
                record_catalogue(catalogue(backend, 'demo', '1.0', {}), self.package / 'options')
                result = self.tool('waypure-audit', '--repo', self.root, 'demo', '--json')
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                row = json.loads(result.stdout)['packages'][0]
                self.assertEqual(row['checks']['options'], 'SKIP')
                self.assertEqual(row['checks']['source'], 'MISSING')
                self.assertTrue(any(item['code'] == 'source.missing' for item in row['findings']))

    def test_virtual_workspace_reviews_keep_package_identity_across_releases(self):
        (self.source / 'Cargo.toml').write_text('[workspace]\nmembers=[]\n')
        result = self.tool('waypure-options', 'review')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        next_source = self.package / 'src/demo-1.1'
        next_source.mkdir()
        (next_source / 'Cargo.toml').write_text('[workspace]\nmembers=[]\nresolver="3"\n')
        path = self.package / 'PKGBUILD'
        path.write_text(path.read_text().replace('pkgver=1.0', 'pkgver=1.1'))
        result = self.tool('waypure-options', 'review')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('1.0 -> 1.1', result.stdout)
        self.assertIn('workspace', result.stdout)
        result = self.tool('waypure-options', 'check', '1.1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_virtual_workspace_snapshots_accept_explicit_stable_identity(self):
        (self.source / 'Cargo.toml').write_text('[workspace]\nmembers=[]\n')
        output = self.root / 'workspace.json.log'
        result = self.tool('waypure-options', 'snapshot', self.source, '--version', '1.0',
                           '--project', 'upstream-workspace', '-o', output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = load(output)
        self.assertEqual(data['project'], 'upstream-workspace')
        result = self.tool('waypure-options', 'check', output)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_source_check_rejects_inapplicable_selection(self):
        self.python_source()
        result = self.tool('waypure-options', 'source-check', '--resolve')
        self.assertNotEqual(result.returncode, 0)

    def test_source_check_rejects_wrong_release(self):
        self.python_source()
        path = self.source / 'pyproject.toml'
        path.write_text(path.read_text().replace('version="1.0"', 'version="2.0"'))
        result = self.tool('waypure-options', 'source-check', '--source', self.source)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('version', result.stderr.lower())

    def test_archive_audits_installed_python_distribution_metadata(self):
        archive = self.package / 'demo-waypure-1.0-1-x86_64.pkg.tar'
        entries = {'.PKGINFO': b'pkgname = demo-waypure\npkgver = 1.0-1\n',
                   'usr/lib/python3.14/site-packages/demo-1.0.dist-info/METADATA':
                       b'Metadata-Version: 2.4\nName: demo\nName: wrong\nVersion: 1.0\nRequires-Dist: python-xlib\n'}
        with tarfile.open(archive, 'w') as stream:
            for name, content in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(content)
                stream.addfile(member, io.BytesIO(content))
        result = self.tool('waypure-audit', '--repo', self.root, 'demo', '--metadata-only', '--artifacts', '--json')
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        row = json.loads(result.stdout)['packages'][0]
        self.assertEqual(row['checks']['artifacts'], 'FAIL')
        codes = {item['code'] for item in row['findings']}
        self.assertIn('artifact.python.identity', codes)
        self.assertIn('artifact.python.x11', codes)
