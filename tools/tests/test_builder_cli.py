"""Builder cli coverage migrated from ~/.local/share/waypure-build/tests."""

import shutil
import unittest
from builder_test_support import BuilderCase
from pathlib import Path

def _repo(tmp_path, spec):
    for dirname, text in spec.items():
        d = tmp_path / dirname
        d.mkdir()
        (d / 'PKGBUILD').write_text(text)
    return tmp_path

def _patch_wb(monkeypatch, wb, versions, makepkg_trace=None):

    which=shutil.which
    monkeypatch.setattr(shutil,'which',lambda command,*a,**kw:
                        command if command in {'pacman','vercmp'} else which(command,*a,**kw))

    class Proc:

        def __init__(self, returncode=0, stdout=''):
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd, **kw):
        if cmd == ['makepkg', '--version']:
            return Proc(0, 'makepkg (pacman) 7.1.0\n')
        if cmd[:2] == ['makepkg', '--printsrcinfo']:
            import re
            pkgdir = Path(str(kw.get('cwd')))
            text = (pkgdir / 'PKGBUILD').read_text()
            get = lambda k: re.search(f'^{k}=(.*)$', text, re.M).group(1)
            names = re.findall('^pkgname=(.*)$', text, re.M)
            lines = [f'pkgbase = {names[0]}', f"\tpkgver = {get('pkgver')}", f"\tpkgrel = {get('pkgrel')}"]
            lines += [f'\tpkgname = {n}' for n in names]
            for dep in re.findall('^depends=(.*)$', text, re.M):
                lines.append(f'\tdepends = {dep}')
            return Proc(0, '\n'.join(lines) + '\n')
        if cmd[:2] == ['pacman', '-Q']:
            return Proc(0, ''.join(f'{name} {version}\n' for name,version in versions.items()))
        if cmd[0] == 'vercmp':
            a, b = (cmd[1], cmd[2])
            return Proc(0, str((a > b) - (a < b)))
        if cmd[0] == 'makepkg':
            if makepkg_trace is not None:
                makepkg_trace.append(list(cmd))
            d = Path(str(kw.get('cwd')))
            (d / 'built.marker').write_text('built\n')
            return Proc(0, '')
        raise AssertionError(f'unexpected: {cmd}')
    monkeypatch.setattr(wb.subprocess, 'run', fake_run)

def _three_pkg_repo(tmp_path):
    _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n', 'app': 'pkgname=app\npkgver=1.0\npkgrel=1\ndepends=lib\n', 'solo': 'pkgname=solo\npkgver=1.0\npkgrel=1\n'})

class BuilderCliCase(BuilderCase):

    def test_duplicate_outputs_stop_all_modes_before_building(self):
        _repo(self.root,{'a':'pkgname=same\npkgver=1.0\npkgrel=1\n',
                         'b':'pkgname=same\npkgver=2.0\npkgrel=1\n'})
        _patch_wb(self.monkeypatch,self.wb,{})
        for arguments in (['--order-only'],['--check'],[]):
            with self.subTest(arguments=arguments):
                self.assertEqual(self.wb.main(['--repo',str(self.root),*arguments]),1)
                self.assertIn('Duplicate package output same',self.capsys.readouterr().err)
                self.assertFalse(any(self.root.glob('*/built.marker')))

    def test_ignore_removes_duplicate_provider_before_validation(self):
        _repo(self.root,{'a':'pkgname=same\npkgver=1.0\npkgrel=1\n',
                         'b':'pkgname=same\npkgver=2.0\npkgrel=1\n',
                         'app':'pkgname=app\npkgver=1.0\npkgrel=1\ndepends=same\n'})
        _patch_wb(self.monkeypatch,self.wb,{})
        self.assertEqual(self.wb.main(['--repo',str(self.root),'--order-only','--ignore','b']),0)
        output=self.capsys.readouterr().out
        self.assertIn('tier 0: a',output)
        self.assertIn('tier 1: app',output)

    def test_unsatisfied_local_version_stops_before_makepkg_build(self):
        _repo(self.root,{'library':'pkgname=library\npkgver=1.0\npkgrel=1\n',
                         'app':'pkgname=app\npkgver=1.0\npkgrel=1\ndepends=library>=2.0\n'})
        _patch_wb(self.monkeypatch,self.wb,{})
        self.assertEqual(self.wb.main(['--repo',str(self.root)]),1)
        self.assertIn('no local provider satisfies library>=2.0',self.capsys.readouterr().err)
        self.assertFalse(any(self.root.glob('*/built.marker')))

    def test_namcap_errors_stop_a_requested_build_before_makepkg(self):
        import os
        import sys
        package = self.root/'demo'
        package.mkdir()
        (package/'PKGBUILD').write_text('pkgname=demo\n')
        for name, body in {
            'namcap': "print('demo E: synthetic lint error')\n",
            'makepkg': "from pathlib import Path\nPath('built.marker').touch()\n",
        }.items():
            executable = self.root/name
            executable.write_text('#!' + sys.executable + '\n' + body)
            executable.chmod(0o755)
        self.monkeypatch.setenv('PATH', str(self.root) + os.pathsep + os.environ['PATH'])
        info = self.wb.PackageInfo(dir=package, pkgnames=['demo'], version='1.0-1')
        self.assertFalse(self.wb.build_dir(info, [], True))
        self.assertFalse((package/'built.marker').exists())

    def test_stale_artifact_rebuild_is_forced(self):
        package = self.root/'demo'
        package.mkdir()
        (package/'demo-1.0-1-x86_64.pkg.tar').write_text('stale')
        (package/'PKGBUILD').write_text('pkgname=demo\npkgver=1.0\npkgrel=1\n')
        trace = []
        _patch_wb(self.monkeypatch, self.wb, {'demo': '1.0-1'}, makepkg_trace=trace)
        self.assertEqual(self.wb.main(['--repo', str(self.root)]), 0)
        self.assertIn('-f', trace[0])

    def test_fresh_artifact_install_only_keeps_plain_makepkg(self):
        package = self.root/'demo'
        package.mkdir()
        (package/'PKGBUILD').write_text('pkgname=demo\npkgver=1.0\npkgrel=1\n')
        (package/'demo-1.0-1-x86_64.pkg.tar').write_text('fresh')
        trace = []
        _patch_wb(self.monkeypatch, self.wb, {}, makepkg_trace=trace)
        self.assertEqual(self.wb.main(['--repo', str(self.root)]), 0)
        self.assertNotIn('-f', trace[0])

    def test_needs_force_decisions(self):
        package = self.root/'demo'
        package.mkdir()
        stale = package/'demo-1.0-1-x86_64.pkg.tar'
        stale.write_text('stale')
        newer = package/'PKGBUILD'
        newer.write_text('pkgname=demo\npkgver=1.0\npkgrel=1\n')
        info = self.wb.PackageInfo(dir=package, pkgnames=['demo'], version='1.0-1')
        for reason in ('forced', 'dependency rebuilt', 'sources newer than artifact'):
            self.assertTrue(self.wb.needs_force(info, reason), reason)
        self.assertTrue(self.wb.needs_force(info, 'demo not installed'))
        split = self.wb.PackageInfo(dir=package, pkgnames=['demo', 'demo-subs'], version='1.0-1')
        self.assertTrue(self.wb.needs_force(split, 'demo-subs not installed'))
        stale.touch()
        self.assertFalse(self.wb.needs_force(info, 'demo not installed'))

    def test_namcap_archive_error_is_reported_after_makepkg(self):
        import os
        import sys
        package = self.root/'demo'
        package.mkdir()
        (package/'PKGBUILD').write_text('pkgname=demo\n')
        trace = self.root/'lint-inputs'
        native = self.root/'namcap'
        native.write_text('#!' + sys.executable + '\nfrom pathlib import Path\nimport sys\n'
                          + 'path = Path(sys.argv[-1])\nwith open(' + repr(str(trace)) + ", 'a') as out: out.write(path.name + '\\n')\n"
                          + "if path.name != 'PKGBUILD': print('demo E: synthetic archive license error')\n")
        native.chmod(0o755)
        makepkg = self.root/'makepkg'
        makepkg.write_text('#!' + sys.executable + "\nfrom pathlib import Path\nPath('demo-1.0-1-x86_64.pkg.tar').write_text('synthetic archive')\n")
        makepkg.chmod(0o755)
        self.monkeypatch.setenv('PATH', str(self.root) + os.pathsep + os.environ['PATH'])
        info = self.wb.PackageInfo(dir=package, pkgnames=['demo'], version='1.0-1')
        self.assertFalse(self.wb.build_dir(info, [], True))
        self.assertEqual(trace.read_text().splitlines(), ['PKGBUILD', 'demo-1.0-1-x86_64.pkg.tar'])

    def test_order_only_prints_leaves_first(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n', 'app': 'pkgname=app\npkgver=1.0\npkgrel=1\ndepends=lib\n'})
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only']), 0)
        out = capsys.readouterr().out
        self.assertTrue(out.index('lib') < out.index('app'))

    def test_check_reports_dirty_with_reason(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n'})
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--check']), 0)
        out = capsys.readouterr().out
        self.assertIn('DIRTY lib: lib not installed', out)

    def test_build_installs_in_order_then_skips(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n', 'app': 'pkgname=app\npkgver=1.0\npkgrel=1\ndepends=lib\n'})
        versions = {}
        _patch_wb(monkeypatch, wb, versions)
        self.assertEqual(wb.main(['--repo', str(tmp_path)]), 0)
        self.assertTrue((tmp_path / 'lib' / 'built.marker').is_file())
        self.assertTrue((tmp_path / 'app' / 'built.marker').is_file())

    def test_from_unknown_name_errors(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n'})
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--from', 'nosuchpkg']), 1)
        err = capsys.readouterr().err
        self.assertIn('nosuchpkg', err)

    def test_ignore_drops_package_and_warns_rdeps(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _three_pkg_repo(tmp_path)
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only', '--ignore', 'lib']), 0)
        out = capsys.readouterr()
        self.assertIn('tier 0: app solo', out.out)
        self.assertNotIn('lib', out.out)
        self.assertTrue('app' in out.err and 'lib' in out.err and ('external' in out.err))

    def test_ignore_brace_syntax(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _three_pkg_repo(tmp_path)
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only', '--ignore', '{lib,solo}']), 0)
        out = capsys.readouterr().out
        self.assertIn('tier 0: app', out)
        self.assertTrue('lib' not in out and 'solo' not in out)

    def test_ignore_all_errors(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'lib': 'pkgname=lib\npkgver=1.0\npkgrel=1\n'})
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only', '--ignore', 'lib']), 1)
        self.assertIn('ignored', capsys.readouterr().err)

    def test_ignore_by_pkgname_drops_dir(self):
        wb = self.wb
        monkeypatch = self.monkeypatch
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'tool': 'pkgname=toolkit\npkgver=1.0\npkgrel=1\n', 'user': 'pkgname=user\npkgver=1.0\npkgrel=1\ndepends=toolkit\n'})
        _patch_wb(monkeypatch, wb, {})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only', '--ignore', 'toolkit']), 0)
        out = capsys.readouterr()
        self.assertIn('tier 0: user', out.out)
        self.assertNotIn('toolkit', out.out)
        self.assertTrue('user' in out.err and 'toolkit' in out.err)

    @unittest.skipUnless(shutil.which('makepkg'), 'makepkg needed')
    def test_ignore_skips_unparsable_dir(self):
        wb = self.wb
        tmp_path = self.root
        capsys = self.capsys
        _repo(tmp_path, {'good': 'pkgname=good\npkgver=1.0\npkgrel=1\n', 'stub': 'this is not a valid PKGBUILD (((\n'})
        self.assertEqual(wb.main(['--repo', str(tmp_path), '--order-only', '--ignore', 'stub']), 0)
        self.assertIn('tier 0: good', capsys.readouterr().out)
