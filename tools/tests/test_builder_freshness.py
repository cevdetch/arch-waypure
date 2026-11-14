"""Builder freshness coverage migrated from ~/.local/share/waypure-build/tests."""

import shutil
import unittest
from builder_test_support import BuilderCase
from pathlib import Path

def _info(wb, dirname, version='1.0-1', names=None, Deps=()):
    d = Path('/repo') / dirname
    return wb.PackageInfo(dir=d, pkgnames=list(names or [dirname]), version=version, depends=list(Deps), makedepends=[], checkdepends=[], provides=[])

def _patch_run(monkeypatch, wb, pacman=None, vercmp=None):
    pacman = pacman or {}
    vercmp = vercmp or {}
    which=shutil.which
    monkeypatch.setattr(shutil,'which',lambda command,*a,**kw:
                        command if command in {'pacman','vercmp'} else which(command,*a,**kw))

    class Proc:

        def __init__(self, returncode=0, stdout=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = ''

    def fake_run(cmd, **kw):
        if cmd[:2] == ['pacman', '-Q']:
            return Proc(0, ''.join(f'{name} {version}\n' for name,version in pacman.items()))
        if cmd[0] == 'vercmp':
            return Proc(0, str(vercmp.get((cmd[1], cmd[2]), 0)))
        raise AssertionError(f'unexpected command: {cmd}')
    monkeypatch.setattr(wb.subprocess, 'run', fake_run)

class BuilderFreshnessCase(BuilderCase):

    def test_new_archive_from_another_version_cannot_make_current_recipe_fresh(self):
        self.monkeypatch.setattr(self.wb, 'installed_version', lambda name: '1.0-1')
        self.monkeypatch.setattr(self.wb, 'compare_versions', lambda a, b: 0)
        package = self.root/'demo'
        package.mkdir()
        (package/'PKGBUILD').write_text('pkgver=1.0\n')
        (package/'demo-0.9-1-x86_64.pkg.tar.zst').write_text('old release')
        info = self.wb.PackageInfo(dir=package, pkgnames=['demo'], version='1.0-1')
        self.assertIsNotNone(self.wb.dirty_reason(info))

    def test_split_package_needs_a_current_archive_for_every_output(self):
        self.monkeypatch.setattr(self.wb, 'installed_version', lambda name: '1.0-1')
        self.monkeypatch.setattr(self.wb, 'compare_versions', lambda a, b: 0)
        package = self.root/'demo'
        package.mkdir()
        (package/'PKGBUILD').write_text('pkgver=1.0\n')
        (package/'demo-1.0-1-x86_64.pkg.tar.zst').write_text('main split')
        info = self.wb.PackageInfo(dir=package, pkgnames=['demo', 'demo-libs'], version='1.0-1')
        self.assertIsNotNone(self.wb.dirty_reason(info))

    def test_missing_package_is_dirty(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _patch_run(monkeypatch, wb)
        self.assertEqual(wb.dirty_reason(_info(wb, 'a')), 'a not installed')

    def test_older_installed_is_dirty(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        _patch_run(monkeypatch, wb, pacman={'a': '1.0-1'}, vercmp={('1.0-1', '1.1-1'): -1})
        info = _info(wb, 'a', version='1.1-1')
        self.assertIn('1.0-1 < 1.1-1', wb.dirty_reason(info))

    def test_newer_installed_is_fresh(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _patch_run(monkeypatch, wb, pacman={'a': '2.0-1'}, vercmp={('2.0-1', '1.0-1'): 1})
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'PKGBUILD').write_text('x\n')
        (d / 'a-1.0-1-x86_64.pkg.tar.zst').write_text('x\n')
        import os
        old = os.stat(d / 'a-1.0-1-x86_64.pkg.tar.zst').st_mtime
        os.utime(d / 'PKGBUILD', (old - 10, old - 10))
        info = wb.PackageInfo(dir=d, pkgnames=['a'], version='1.0-1')
        self.assertIs(wb.dirty_reason(info), None)

    def test_split_package_checks_every_name(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        _patch_run(monkeypatch, wb, pacman={'foo': '1.0-1'}, vercmp={('1.0-1', '1.0-1'): 0})
        info = _info(wb, 'foo', names=['foo', 'foo-lib'])
        self.assertEqual(wb.dirty_reason(info), 'foo-lib not installed')

    def test_mtime_newer_sources_are_dirty(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _patch_run(monkeypatch, wb, pacman={'a': '1.0-1'}, vercmp={('1.0-1', '1.0-1'): 0})
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'a-1.0-1-x86_64.pkg.tar.zst').write_text('x\n')
        (d / 'PKGBUILD').write_text('x\n')
        import os
        os.utime(d / 'a-1.0-1-x86_64.pkg.tar.zst', (1, 1))
        info = wb.PackageInfo(dir=d, pkgnames=['a'], version='1.0-1')
        self.assertEqual(wb.dirty_reason(info), 'sources newer than artifact')

    def test_propagate_dirty_covers_transitive_rdeps(self):
        wb = self.wb
        lib = _info(wb, 'lib')
        mid = _info(wb, 'mid', Deps=['lib'])
        top = _info(wb, 'top', Deps=['mid'])
        tiers = wb.topo_tiers([top, mid, lib])
        grown = wb.propagate_dirty(tiers, {lib.dir})
        self.assertEqual(grown, {lib.dir, mid.dir, top.dir})
