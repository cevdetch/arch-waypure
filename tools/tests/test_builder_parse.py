"""Builder parse coverage migrated from ~/.local/share/waypure-build/tests."""

import shutil
import unittest
from builder_test_support import BuilderCase
SINGLE = 'pkgbase = cairo-waypure\n\tpkgver = 1.18.6\n\tpkgrel = 1\n\tpkgname = cairo-waypure\n\tdepends = fontconfig\n\tmakedepends = meson\n\tprovides = cairo\n'

class BuilderParseCase(BuilderCase):

    def test_find_pkgbuild_dirs_sorted(self):
        wb = self.wb
        tmp_path = self.root
        (tmp_path / 'b' / 'PKGBUILD').parent.mkdir(parents=True)
        (tmp_path / 'b' / 'PKGBUILD').write_text('pkgname=b\n')
        (tmp_path / 'a' / 'PKGBUILD').parent.mkdir(parents=True)
        (tmp_path / 'a' / 'PKGBUILD').write_text('pkgname=a\n')
        (tmp_path / 'empty').mkdir()
        self.assertEqual(wb.find_pkgbuild_dirs(tmp_path), [tmp_path / 'a', tmp_path / 'b'])

    def test_parse_single(self):
        wb = self.wb
        tmp_path = self.root
        info = wb.parse_srcinfo_text(SINGLE, tmp_path)
        self.assertEqual(info.pkgnames, ['cairo-waypure'])
        self.assertEqual(info.version, '1.18.6-1')
        self.assertEqual(info.depends, ['fontconfig'])
        self.assertEqual(info.makedepends, ['meson'])
        self.assertEqual(info.provides, ['cairo'])

    def test_parse_split_and_epoch(self):
        wb = self.wb
        tmp_path = self.root
        text = SINGLE.replace('pkgrel = 1', 'pkgrel = 2\nepoch = 1').replace('pkgname = cairo-waypure', 'pkgname = cairo-waypure\n\tpkgname = cairo-docs-waypure')
        info = wb.parse_srcinfo_text(text, tmp_path)
        self.assertEqual(info.pkgnames, ['cairo-waypure', 'cairo-docs-waypure'])
        self.assertEqual(info.version, '1:1.18.6-2')

    def test_parse_incomplete_raises(self):
        wb = self.wb
        tmp_path = self.root
        with self.assertRaises(wb.SrcinfoError):
            wb.parse_srcinfo_text('pkgbase = x\n', tmp_path)

    def test_x86_64_qualified_dependencies_and_provisions_are_included(self):
        info=self.wb.parse_srcinfo_text(SINGLE+'\tdepends_x86_64 = libcpu>=2\n'
            +'\tmakedepends_x86_64 = compiler\n\tcheckdepends_x86_64 = tester\n'
            +'\tprovides_x86_64 = cpu-library=2\n\tdepends_aarch64 = other-cpu\n'
            +'\tdepends = \n\tprovides = \n',self.root)
        self.assertEqual(info.depends,['fontconfig','libcpu>=2'])
        self.assertEqual(info.makedepends,['meson','compiler'])
        self.assertEqual(info.checkdepends,['tester'])
        self.assertEqual(info.provides,['cairo','cpu-library=2'])

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_parse_srcinfo_runs_makepkg(self):
        wb = self.wb
        tmp_path = self.root
        (tmp_path / 'PKGBUILD').write_text('pkgname=dummy-waypure\npkgver=1.0\npkgrel=1\npkgdesc="d"\narch=("x86_64")\nurl="https://example.com"\nlicense=("MIT")\ndepends=(glibc)\nprovides=(dummy)\nsource=()\nsha256sums=()\npackage() { :; }\n')
        info = wb.parse_srcinfo(tmp_path)
        self.assertEqual(info.pkgnames, ['dummy-waypure'])
        self.assertEqual(info.version, '1.0-1')
        self.assertEqual(info.provides, ['dummy'])
