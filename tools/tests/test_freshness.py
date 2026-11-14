"""Native build logs and signatures must not be mistaken for package archives."""

from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from optionkit.freshness import package_artifacts
from optionkit.versions import archives


class ArchiveDiscoveryCase(unittest.TestCase):
    def test_only_complete_package_extensions_are_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            accepted = {'demo-1.0-1-any.pkg.tar'+suffix for suffix in ('', '.zst', '.xz', '.gz', '.bz2')}
            rejected = {'demo-1.0-1-any.pkg.tar.zst-namcap.log', 'demo-1.0-1-any.pkg.tar.zst.sig',
                        'demo-1.0-1-any.pkg.tar.xz.log', 'demo-1.0-1-any.pkg.tar-backup'}
            for name in accepted | rejected:
                (root/name).touch()
            self.assertEqual({path.name for path in package_artifacts(root, 'demo', '1.0-1')}, accepted)
            self.assertEqual({path.name for path in archives(root)}, accepted)
