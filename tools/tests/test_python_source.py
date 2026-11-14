"""Python metadata is evidence, never permission to execute a project."""

from pathlib import Path
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))
from optionkit.runtime_policy import load_policy
from optionkit import python_source


class PythonSourceCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        self.policy = load_policy(TOOLS / 'waypure-policy.toml')

    def write(self, name, text):
        (self.source / name).write_text(text)

    def capture(self):
        from optionkit.python_source import capture
        return capture(self.source)

    def audit(self, **kwargs):
        from optionkit.python_source import audit_source
        return audit_source(self.source, self.policy, **kwargs)

    def test_static_pyproject_preserves_requirements_extras_groups_and_hashes(self):
        self.write('pyproject.toml', '''[build-system]
requires = ["setuptools>=70", "wheel"]
build-backend = "setuptools.build_meta"
[project]
name = "Demo"
version = "1.2"
requires-python = ">=3.12"
dependencies = ["requests>=2", "python-xlib; sys_platform == 'linux'"]
[project.optional-dependencies]
x11 = ["xcffib>=1"]
[dependency-groups]
test = ["pytest", {include-group = "lint"}]
lint = ["ruff"]
''')
        data = self.capture()
        self.assertEqual((data['backend'], data['project'], data['version']), ('python', 'Demo', '1.2'))
        self.assertEqual(data['options'], {})
        self.assertEqual(data['runtime_requirements'], ['requests>=2', "python-xlib; sys_platform == 'linux'"])
        self.assertEqual(data['build_requirements'], ['setuptools>=70', 'wheel'])
        self.assertEqual(data['optional_dependencies'], {'x11': ['xcffib>=1']})
        self.assertEqual(data['dependency_groups']['test'][1], {'include-group': 'lint'})
        self.assertEqual(data['requires_python'], '>=3.12')
        self.assertEqual(len(data['sources']['pyproject.toml']), 64)

    def test_dynamic_static_entries_are_lower_bounds_and_calibre_custom_setup_is_unknown(self):
        self.write('pyproject.toml', '''[project]
name = "calibre"
requires-python = ">=3.11"
dynamic = ["version", "dependencies"]
dependencies = ["xcffib"]
''')
        self.write('setup.py', f"from pathlib import Path\nPath({str(self.source / 'EXECUTED')!r}).touch()\nraise RuntimeError('custom build')\n")
        data = self.capture()
        self.assertEqual(data['version'], 'unknown')
        self.assertIn('dependencies', data['dynamic_metadata'])
        self.assertEqual(data['runtime_requirements'], ['xcffib'])
        self.assertFalse(data['metadata_coverage']['runtime_complete'])
        self.assertFalse(data['metadata_coverage']['build_complete'])
        self.assertFalse((self.source / 'EXECUTED').exists())
        self.assertTrue(any(f['code'] == 'source.coverage' for f in self.audit()['findings']))

    def test_setup_cfg_declarative_metadata_and_literal_setup_py_without_execution(self):
        self.write('setup.cfg', '''[metadata]
name = demo
version = 2.1
[options]
python_requires = >=3.10
install_requires =
    requests>=2
    JACK-Client; sys_platform == "linux"
[options.extras_require]
x11 =
    python-xlib
''')
        self.write('setup.py', f"from pathlib import Path\nPath({str(self.source / 'EXECUTED')!r}).touch()\nfrom setuptools import setup\nsetup()\n")
        data = self.capture()
        self.assertEqual(data['project'], 'demo')
        self.assertEqual(data['version'], '2.1')
        self.assertEqual(data['runtime_requirements'][0], 'requests>=2')
        self.assertEqual(data['optional_dependencies']['x11'], ['python-xlib'])
        self.assertFalse((self.source / 'EXECUTED').exists())
        self.write('setup.cfg', '[metadata]\nname = demo\n')
        self.write('setup.py', "raise RuntimeError('never run')\nfrom setuptools import setup\nsetup(name='literal', version='3', install_requires=['xcffib'], extras_require={'audio': ['JACK-Client']})\n")
        data = self.capture()
        self.assertEqual(data['runtime_requirements'], ['xcffib'])
        self.assertEqual(data['optional_dependencies'], {'audio': ['JACK-Client']})

    def test_dynamic_setup_py_and_attr_file_directives_remain_unknown(self):
        self.write('setup.cfg', '[metadata]\nname = demo\nversion = attr: demo.VERSION\n[options]\ninstall_requires = file: requirements.txt\n')
        self.write('setup.py', "from setuptools import setup\nsetup(install_requires=get_requirements())\n")
        data = self.capture()
        self.assertEqual(data['version'], 'unknown')
        self.assertFalse(data['metadata_coverage']['runtime_complete'])
        self.assertIn('dependencies', data['dynamic_metadata'])

    def test_conditional_optional_and_unknown_mappings_are_conservative(self):
        self.write('pyproject.toml', '''[project]
name = "demo"
version = "1"
dependencies = ["python-xlib", "python-xcffib", "xcffib; sys_platform == 'linux'", "JACK-Client; sys_platform == 'win32'", "requests @ https://example.test/requests.whl"]
[project.optional-dependencies]
x11 = ["xcffib"]
''')
        findings = self.audit()['findings']
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'error' and 'python-xlib' in f['message'] for f in findings))
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'warning' and 'sys_platform' in f['message'] for f in findings))
        self.assertFalse(any(f['code'] == 'python.x11' and f.get('requirement') == 'python-xcffib' for f in findings))
        selected = self.audit(extras=('x11',), environment={'sys_platform': 'linux'})['findings']
        if python_source.Requirement is not None:
            self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'error' and "xcffib;" in f['message'] for f in selected))
            self.assertFalse(any(f['code'] == 'python.pipewire' for f in selected))
        else:
            self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'warning' and "xcffib;" in f['message'] for f in selected))
            self.assertTrue(any(f['code'] == 'python.pipewire' and f['severity'] == 'warning' for f in selected))
            self.assertTrue(any(f['code'] == 'python.parser' for f in selected))
        self.assertTrue(any('requests @ https://' in f['message'] for f in selected if f['code'] == 'python.unmapped'))

    def test_unrelated_import_rebinding_cannot_supply_setup_identity(self):
        for imports, call in (("from setuptools import setup\nfrom unrelated import setup", 'setup'),
                              ("from setuptools import setup as install\nfrom unrelated import setup as install", 'install'),
                              ("import setuptools as backend\nimport unrelated as backend", 'backend.setup')):
            with self.subTest(imports=imports):
                self.write('setup.py', imports + f"\n{call}(name='fake', version='1', install_requires=['python-xlib'])\n")
                data = self.capture()
                self.assertEqual(data['version'], 'unknown')
                self.assertNotEqual(data['project'], 'fake')
                self.assertFalse(data['runtime_requirements'])
                self.assertFalse(any(f['severity'] == 'error' for f in self.audit()['findings']))

    def test_pep621_omitted_static_fields_override_legacy_declarations(self):
        self.write('setup.cfg', '[metadata]\nname = old\nversion = 1\n[options]\ninstall_requires = python-xlib\npython_requires = >=3.7\n[options.extras_require]\nx11 = xcffib\n')
        self.write('pyproject.toml', '[project]\nname = "modern"\nversion = "2"\n')
        data = self.capture()
        self.assertEqual(data['runtime_requirements'], [])
        self.assertEqual(data['optional_dependencies'], {})
        self.assertIsNone(data['requires_python'])
        self.assertTrue(data['metadata_coverage']['runtime_complete'])
        self.assertFalse(any(f['code'] in {'python.x11', 'python.pipewire'} for f in self.audit()['findings']))

    def test_conflicting_setup_requirements_are_uncertain_union_evidence(self):
        self.write('setup.py', "from setuptools import setup\nsetup(name='demo', version='1', install_requires=['python-xlib'])\nsetup(name='demo', version='1', install_requires=['JACK-Client'])\n")
        data = self.capture()
        self.assertEqual(set(data['runtime_requirements']), {'python-xlib', 'JACK-Client'})
        self.assertIn('dependencies', data['dynamic_metadata'])
        findings = self.audit()['findings']
        policy = [f for f in findings if f['code'] in {'python.x11', 'python.pipewire'}]
        self.assertEqual(len(policy), 2)
        self.assertTrue(all(f['severity'] == 'warning' for f in policy), findings)

    def test_authoritative_pyproject_lists_override_ambiguous_setup_evidence(self):
        self.write('setup.py', "from setuptools import setup\nsetup(install_requires=['requests'], setup_requires=['requests'])\nsetup(install_requires=['JACK-Client'], setup_requires=['JACK-Client'])\n")
        self.write('pyproject.toml', '[build-system]\nrequires = ["python-xlib"]\n[project]\nname = "modern"\nversion = "2"\ndynamic = ["dependencies"]\ndependencies = ["xcffib"]\n')
        data = self.capture()
        self.assertEqual(data['runtime_requirements'], ['xcffib'])
        findings = self.audit()['findings']
        policy = [f for f in findings if f['code'] == 'python.x11']
        self.assertEqual(len(policy), 2)
        self.assertTrue(all(f['severity'] == 'error' for f in policy), findings)

    def test_setup_literal_tuple_requirements_and_invalid_extra_keys(self):
        self.write('setup.py', "from setuptools import setup\nsetup(name='demo', version='1', install_requires=('xcffib',), extras_require={'audio': ('JACK-Client',)})\n")
        data = self.capture()
        self.assertEqual(data['runtime_requirements'], ['xcffib'])
        self.assertEqual(data['optional_dependencies'], {'audio': ['JACK-Client']})
        self.write('setup.py', "from setuptools import setup\nsetup(name='demo', version='1', extras_require={3: ['xcffib']})\n")
        with self.assertRaises(ValueError):
            self.capture()

    def test_pep440_epoch_is_preserved(self):
        self.write('pyproject.toml', '[project]\nname = "demo"\nversion = "1!2.0"\n')
        self.assertEqual(self.capture()['version'], '1!2.0')

    def test_dynamic_scalar_overlap_and_missing_build_requires_are_invalid(self):
        for text in ('[project]\nname = "demo"\nversion = "1"\ndynamic = ["version"]', '[build-system]\nbuild-backend = "custom.backend"'):
            with self.subTest(text=text):
                self.write('pyproject.toml', text)
                with self.assertRaises(ValueError):
                    self.capture()

    def test_lookalike_and_scoped_setup_calls_are_unresolved(self):
        self.write('setup.py', "class Other: pass\nobj = Other()\nobj.setup(name='fake', version='1', install_requires=['python-xlib'])\ndef unused():\n    setup(name='also-fake', version='2')\n")
        data = self.capture()
        self.assertEqual(data['version'], 'unknown')
        self.assertFalse(data['runtime_requirements'])
        self.assertFalse(data['metadata_coverage']['runtime_complete'])

    def test_fallback_retains_requirement_but_reports_parser_and_marker_limits(self):
        from unittest.mock import patch
        from optionkit import python_source
        self.write('pyproject.toml', '[project]\nname = "demo"\nversion = "1"\ndependencies = ["xcffib; sys_platform == \'linux\'"]\n')
        with patch.object(python_source, 'Requirement', None):
            findings = self.audit(environment={'sys_platform': 'linux'})['findings']
        self.assertTrue(any(f['code'] == 'python.parser' for f in findings))
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'warning' for f in findings))

    def test_invalid_native_structures_raise_value_error(self):
        for text in ('project = []', '[project]\nname = "demo"\ndependencies = "xcffib"', '[project]\nname = "demo"\ndynamic = [7]', '[dependency-groups]\ntest = [7]'):
            with self.subTest(text=text):
                self.write('pyproject.toml', text)
                with self.assertRaises(ValueError):
                    self.capture()


class PythonDistributionCase(unittest.TestCase):
    def audit(self, text):
        from optionkit.python_source import audit_distribution
        return audit_distribution(text, 'usr/lib/python3.14/site-packages/demo.dist-info/METADATA', load_policy(TOOLS / 'waypure-policy.toml'))

    def test_distribution_metadata_preserves_folded_requirements_markers_extras_and_urls(self):
        findings = self.audit('Metadata-Version: 2.4\nName: Demo\nVersion: 1.0\nRequires-Dist: python-xlib\nRequires-Dist: xcffib;\n extra == "x11"\nRequires-Dist: requests @ https://example.test/pkg.whl\nProvides-Extra: x11\n\nDescription\n')
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'error' for f in findings))
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'warning' and 'extra' in f['message'] for f in findings))
        self.assertTrue(any('https://example.test/pkg.whl' in f['message'] for f in findings))

    def test_malformed_identity_headers_requirements_and_version_are_errors(self):
        for text in ('Metadata-Version: 2.4\nVersion: 1\n', 'Metadata-Version: 2.4\nName: demo\nName: other\nVersion: 1\n', 'Metadata-Version: 2.4\nName: demo\nVersion: spaces in version\n', 'Metadata-Version: 2.4\nName: demo\nVersion: 1\nRequires-Dist: invalid ???\n', 'garbage\nName: demo\nVersion: 1\n'):
            with self.subTest(text=text):
                self.assertTrue(any(f['severity'] == 'error' for f in self.audit(text)))

    def test_core_metadata_structure_and_dynamic_identity_are_validated(self):
        for suffix in ('Dynamic: Name\n', 'Requires-Python: >=3\nRequires-Python: >=2\n'):
            with self.subTest(suffix=suffix):
                findings = self.audit('Metadata-Version: 2.4\nName: demo\nVersion: 1\n' + suffix)
                self.assertTrue(any(f['severity'] == 'error' for f in findings), findings)

    @unittest.skipUnless(python_source.SpecifierSet is not None, 'strict specifier validation requires optional packaging')
    def test_packaging_rejects_invalid_requires_python_specifiers(self):
        findings = self.audit('Metadata-Version: 2.4\nName: demo\nVersion: 1\nRequires-Python: not-python\n')
        self.assertTrue(any(f['code'] == 'python.metadata' and f['severity'] == 'error' for f in findings), findings)

    def test_distribution_fallback_reports_partial_syntax_and_retains_conditional_requirements(self):
        from unittest.mock import patch
        with patch.multiple(python_source, Requirement=None, SpecifierSet=None, Version=None):
            findings = self.audit('Metadata-Version: 2.4\nName: demo\nVersion: 1\nRequires-Python: not-python\nRequires-Dist: xcffib; sys_platform == "linux"\n')
            malformed_identity = self.audit('Metadata-Version: 2.4\nVersion: 1\n')
        self.assertTrue(any(f['code'] == 'python.parser' and f['severity'] == 'warning' for f in findings), findings)
        self.assertTrue(any(f['code'] == 'python.x11' and f['severity'] == 'warning' and f['requirement'] == 'xcffib; sys_platform == "linux"' for f in findings), findings)
        self.assertFalse(any(f['severity'] == 'error' for f in findings), findings)
        self.assertTrue(any(f['code'] == 'python.identity' and f['severity'] == 'error' for f in malformed_identity), malformed_identity)

    def test_non_text_distribution_structure_is_rejected(self):
        with self.assertRaises(ValueError):
            self.audit({})


if __name__ == '__main__':
    unittest.main()
