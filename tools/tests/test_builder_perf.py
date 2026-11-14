"""Builder perf coverage migrated from ~/.local/share/waypure-build/tests."""

import shutil
import unittest
from builder_test_support import BuilderCase
import json

def _pkgbuild_text(name='dummy-waypure', ver='1.0', rel='1'):
    return f'pkgname={name}\npkgver={ver}\npkgrel={rel}\npkgdesc="d"\narch=("x86_64")\nurl="https://example.com"\nlicense=("MIT")\ndepends=(glibc)\nprovides=(dummy)\nsource=()\nsha256sums=()\npackage() {{ :; }}\n'

def _use_cache(monkeypatch, tmp_path):
    monkeypatch.setenv('WAYPURE_CACHE', str(tmp_path / 'cache.json'))

class BuilderPerfCase(BuilderCase):

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_cache_hit_skips_makepkg(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _use_cache(monkeypatch, tmp_path)
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'PKGBUILD').write_text(_pkgbuild_text())
        info = wb.parse_srcinfo(d)
        self.assertEqual(info.pkgnames, ['dummy-waypure'])

        def boom(*a, **k):
            raise AssertionError('makepkg must not run on cache hit')
        monkeypatch.setattr(wb.subprocess, 'run', boom)
        info2 = wb.parse_srcinfo(d)
        self.assertEqual(info2.pkgnames, ['dummy-waypure'])
        self.assertEqual(info2.version, info.version)

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_cache_invalidates_on_pkgbuild_change(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        import os
        import time
        _use_cache(monkeypatch, tmp_path)
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'PKGBUILD').write_text(_pkgbuild_text(rel='1'))
        self.assertEqual(wb.parse_srcinfo(d).version, '1.0-1')
        (d / 'PKGBUILD').write_text(_pkgbuild_text(rel='2'))
        os.utime(d / 'PKGBUILD', (time.time() + 5, time.time() + 5))
        self.assertEqual(wb.parse_srcinfo(d).version, '1.0-2')

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_corrupt_cache_ignored(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _use_cache(monkeypatch, tmp_path)
        (tmp_path / 'cache.json').write_text('not json{{')
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'PKGBUILD').write_text(_pkgbuild_text())
        self.assertEqual(wb.parse_srcinfo(d).pkgnames, ['dummy-waypure'])

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_cache_prunes_deleted_dirs(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        _use_cache(monkeypatch, tmp_path)
        d = tmp_path / 'a'
        d.mkdir()
        (d / 'PKGBUILD').write_text(_pkgbuild_text())
        wb.parse_srcinfo(d)
        cache_file = tmp_path / 'cache.json'
        data = json.loads(cache_file.read_text())
        data['entries']['/nonexistent-dir-xyz'] = {'mtime_ns': 1, 'size': 1, 'text': 'pkgbase = x\n'}
        cache_file.write_text(json.dumps(data))
        wb.parse_srcinfo(d)
        data = json.loads(cache_file.read_text())
        self.assertNotIn('/nonexistent-dir-xyz', data['entries'])

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_parse_all_reports_progress_in_order(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        for name in ('b', 'a'):
            d = tmp_path / name
            d.mkdir()
            (d / 'PKGBUILD').write_text(_pkgbuild_text(name=f'{name}-waypure'))
        _use_cache(monkeypatch, tmp_path)
        infos = wb.parse_all(wb.find_pkgbuild_dirs(tmp_path), progress=True)
        self.assertEqual([i.dir.name for i in infos], ['a', 'b'])
        err = capsys.readouterr().err
        self.assertTrue('parsing a' in err and 'parsing b' in err)
