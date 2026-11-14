"""A complete tar prefix is not proof that its decompressor succeeded."""

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

from optionkit.artifacts import audit_artifact
from optionkit.runtime_policy import load_policy


class ArtifactStreamCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.policy = load_policy(TOOLS / 'waypure-policy.toml')
        self.path = self.root / 'demo.pkg.tar.zst'

    def tar_bytes(self, optional=True):
        output = io.BytesIO()
        data = b'pkgname = demo\npkgver = 1.0-1\n'
        if optional:
            data += b'optdepend = jack: optional audio\n'
        with tarfile.open(fileobj=output, mode='w') as archive:
            member = tarfile.TarInfo('.PKGINFO')
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        return output.getvalue()

    def test_nonzero_decoder_status_is_not_hidden_by_an_optional_dependency_warning(self):
        self.path.write_bytes(self.tar_bytes())
        binary = self.root / 'zstd'
        binary.write_text('#!' + sys.executable + '\nimport sys\n'
                          'sys.stdout.buffer.write(open(sys.argv[-1], "rb").read())\n'
                          'sys.stdout.buffer.flush()\nsys.exit(7)\n')
        binary.chmod(0o755)
        with patch.dict(os.environ, {'PATH': str(self.root) + os.pathsep + os.environ.get('PATH', '')}):
            result = audit_artifact(self.path, self.policy)
        self.assertTrue(any(item['severity'] == 'warning' for item in result['findings']), result)
        self.assertTrue(any(item['code'] == 'archive.read' and item['severity'] == 'error'
                            and '7' in item['message'] for item in result['findings']), result)

    @unittest.skipUnless(shutil.which('zstd'), 'zstd needed')
    def test_valid_padded_zstd_stream_is_drained_without_causing_sigpipe(self):
        packed = subprocess.run(['zstd', '-q', '--stdout'],
                                input=self.tar_bytes(False) + b'\0' * (2 * 1024 * 1024),
                                capture_output=True, check=True).stdout
        self.path.write_bytes(packed)
        result = audit_artifact(self.path, self.policy)
        self.assertEqual(result['findings'], [], result)

    @unittest.skipUnless(shutil.which('zstd'), 'zstd needed')
    def test_checksum_failure_after_tar_end_is_an_error_even_with_existing_warning(self):
        packed = bytearray(subprocess.run(['zstd', '-q', '--stdout', '--check'],
                                          input=self.tar_bytes(), capture_output=True, check=True).stdout)
        packed[-1] ^= 1
        self.path.write_bytes(packed)
        result = audit_artifact(self.path, self.policy)
        self.assertTrue(any(item['code'] == 'archive.read' and item['severity'] == 'error'
                            for item in result['findings']), result)

    def metadata_archive(self, name, *, link_type=None):
        path = self.root / 'python.pkg.tar'
        metadata = b'Metadata-Version: 2.4\nName: demo\nVersion: 1.0\nRequires-Dist: python-xlib\n'
        with tarfile.open(path, 'w') as archive:
            package = b'pkgname = demo\npkgver = 1.0-1\n'
            item = tarfile.TarInfo('.PKGINFO')
            item.size = len(package)
            archive.addfile(item, io.BytesIO(package))
            if link_type is None:
                item = tarfile.TarInfo(name)
                item.size = len(metadata)
                archive.addfile(item, io.BytesIO(metadata))
            else:
                target = 'usr/share/demo/distribution.txt'
                item = tarfile.TarInfo(name)
                item.type = link_type
                item.linkname = ('../../../../share/demo/distribution.txt'
                                 if link_type == tarfile.SYMTYPE else target)
                archive.addfile(item)
                item = tarfile.TarInfo(target)
                item.size = len(metadata)
                archive.addfile(item, io.BytesIO(metadata))
        return path

    def test_flat_installed_egg_info_dependencies_are_audited(self):
        path = self.metadata_archive('usr/lib/python3.14/site-packages/demo-1.0.egg-info')
        result = audit_artifact(path, self.policy)
        self.assertTrue(any(item['code'] == 'artifact.python.x11' and item['severity'] == 'error'
                            for item in result['findings']), result)

    def test_regular_directory_distribution_metadata_dependencies_are_audited(self):
        for name in ('usr/lib/python3.14/site-packages/demo-1.0.dist-info/METADATA',
                     'usr/lib/python3.14/site-packages/demo-1.0.egg-info/PKG-INFO'):
            with self.subTest(name=name):
                result = audit_artifact(self.metadata_archive(name), self.policy)
                self.assertTrue(any(item['code'] == 'artifact.python.x11' and item['severity'] == 'error'
                                    for item in result['findings']), result)

    def test_linked_installed_metadata_reports_unknown_coverage_without_following_links(self):
        for link_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
            for name in ('usr/lib/python3.14/site-packages/demo-1.0.dist-info/METADATA',
                         'usr/lib/python3.14/site-packages/demo-1.0.egg-info/PKG-INFO',
                         'usr/lib/python3.14/site-packages/demo-1.0.egg-info'):
                with self.subTest(link_type=link_type, name=name):
                    result = audit_artifact(self.metadata_archive(name, link_type=link_type), self.policy)
                    self.assertEqual([item['code'] for item in result['findings']], ['artifact.python.coverage'])
                    self.assertEqual(result['findings'][0]['severity'], 'warning')
                    self.assertIn(name, result['findings'][0]['message'])
