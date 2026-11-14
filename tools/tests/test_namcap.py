"""Explicit native lint handling, including namcap's zero-exit error findings."""

import importlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch


TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))


class NamcapCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root/'PKGBUILD'
        self.path.write_text('pkgname=demo\n')

    def run_lint(self, **kwargs):
        module = importlib.import_module('optionkit.linting')
        return module.run_namcap(self.path, **kwargs)

    def command(self, text, status=0):
        executable = self.root/'namcap'
        executable.write_text('#!' + sys.executable + '\nimport sys\nprint(' + repr(text) + ')\nsys.exit(' + str(status) + ')\n')
        executable.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}))

    def test_errors_are_detected_even_when_process_succeeds(self):
        self.command('demo E: synthetic error\ndemo W: synthetic warning')
        result = self.run_lint()
        self.assertEqual(result['status'], 'FAIL')
        self.assertEqual([item['severity'] for item in result['findings']], ['error', 'warning'])

    def test_native_information_is_requested_and_preserved(self):
        executable = self.root/'namcap'
        executable.write_text('#!' + sys.executable + '\nimport sys\nif "-i" in sys.argv:\n    print("demo I: synthetic information")\n')
        executable.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}))
        result = self.run_lint()
        self.assertEqual(result['status'], 'PASS')
        self.assertEqual([item['severity'] for item in result['findings']], ['note'])

    def test_missing_requested_tool_is_not_a_pass(self):
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.root)}))
        result = self.run_lint()
        self.assertEqual(result['status'], 'MISSING')
        self.assertEqual(result['findings'][0]['severity'], 'error')

    def test_nonzero_exit_without_structured_findings_is_an_error(self):
        self.command('cannot load requested archive', 2)
        result = self.run_lint()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any(item['severity'] == 'error' for item in result['findings']))

    def test_unrecognized_nonempty_output_does_not_claim_clean_lint(self):
        self.command('unexpected diagnostic format')
        self.assertEqual(self.run_lint()['status'], 'WARN')

    def test_native_timeout_is_reported(self):
        executable = self.root/'namcap'
        executable.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(10)\n')
        executable.chmod(0o755)
        self.enterContext(patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ['PATH']}))
        result = self.run_lint(timeout=0.1)
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any(item['code'] == 'namcap.timeout' for item in result['findings']))

    @unittest.skipUnless(shutil.which('namcap', path=os.defpath), 'system namcap needed')
    def test_real_namcap_reports_missing_license_files(self):
        self.path = self.root/'demo-waypure-1.0-1-any.pkg.tar'
        metadata = b'pkgname = demo-waypure\npkgver = 1.0-1\npkgdesc = Synthetic fixture\nurl = https://example.invalid\narch = any\nlicense = MIT\nsize = 0\n'
        with tarfile.open(self.path, 'w') as archive:
            entry = tarfile.TarInfo('.PKGINFO')
            entry.size = len(metadata)
            archive.addfile(entry, io.BytesIO(metadata))
        # Arch's namcap wrapper finds python3 through PATH. Keep uv's test
        # interpreter separate from the host interpreter owning that module.
        with patch.dict(os.environ, {'PATH': os.defpath}):
            result = self.run_lint()
        self.assertEqual(result['status'], 'FAIL')
        self.assertTrue(any(item['severity'] == 'error' and 'license' in item['message'].lower() for item in result['findings']), result)
