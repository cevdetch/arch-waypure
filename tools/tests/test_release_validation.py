"""Release validation contracts exercised with native tool doubles and real archives."""

import hashlib
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import py_compile
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))


class ReleaseValidationCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.repo = self.root/'repo'
        self.package = self.repo/'demo'
        self.package.mkdir(parents=True)
        self.package.joinpath('PKGBUILD').write_text("pkgname=demo-waypure\npkgver=1.0\npkgrel=1\narch=('any')\n")
        self.srcinfo = 'pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tarch = any\npkgname = demo-waypure\n'
        self.package.joinpath('.SRCINFO').write_text(self.srcinfo)
        self.chroot = self.root/'chroot'
        (self.chroot/'root/etc').mkdir(parents=True)
        (self.chroot/'root/etc/pacman.conf').write_text('[options]\nArchitecture = auto\n')
        (self.chroot/'root/etc/makepkg.conf').write_text('CARCH=x86_64\n')
        (self.chroot/'root/.arch-chroot').write_text('v6\n')
        for name in ('bash', 'pacman'):
            executable = self.chroot/'root/usr/bin'/name
            executable.parent.mkdir(parents=True, exist_ok=True)
            executable.write_text('#!/bin/sh\n')
            executable.chmod(0o755)
        database = self.chroot/'root/var/lib/pacman/local/base-devel-1-1'
        database.mkdir(parents=True)
        (database/'desc').write_text('%NAME%\nbase-devel\n\n%VERSION%\n1-1\n')
        self.bin = self.root/'bin'
        self.bin.mkdir()
        self.trace = self.root/'trace.log'
        self.env = {**os.environ, 'PATH': str(self.bin)+os.pathsep+os.environ['PATH'],
                    'TRACE': str(self.trace), 'WAYPURE_ALPM_BACKEND': 'cli',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.executable('makepkg', '''
import json, os, sys
from pathlib import Path
Path(os.environ['PKGDEST']).mkdir(exist_ok=True)
if os.environ.get('PREFLIGHT_OUTPUT'): (Path(os.environ['PKGDEST'])/'old.pkg.tar').write_bytes(b'preexisting')
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['makepkg',*sys.argv[1:]])+'\\n')
if '--printsrcinfo' in sys.argv:
    if os.environ.get('POSTFLIGHT_OUTPUT') and (Path(os.environ['PKGDEST'])/'build.log').exists():
        import io, tarfile
        path=Path(os.environ['PKGDEST'])/'demo-waypure-1.0-1-any.pkg.tar'
        with tarfile.open(path,'w') as archive:
            data=b'pkgname = demo-waypure\\npkgbase = demo-waypure\\npkgver = 1.0-1\\narch = any\\npkgdesc = Host metadata output\\n'
            item=tarfile.TarInfo('.PKGINFO');item.size=len(data);archive.addfile(item,io.BytesIO(data))
    text=Path('.SRCINFO').read_text()
    if os.environ.get('METADATA_DRIFT'): text=text.replace('1.0','2.0')
    if os.environ.get('METADATA_SCOPE_DRIFT'): text=text.replace('\\tdepends = shared\\n','').replace('pkgname = demo-tools-waypure\\n','pkgname = demo-tools-waypure\\n\\tdepends = shared\\n')
    print(text)
if '--verifysource' in sys.argv: sys.exit(int(os.environ.get('VERIFY_STATUS','0')))
''')
        self.executable('namcap', '''
import json, os, sys
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['namcap',*sys.argv[1:]])+'\\n')
if os.environ.get('LINT_ERROR') == 'recipe' and sys.argv[-1].endswith('PKGBUILD'): print('demo E: bad recipe')
if os.environ.get('LINT_ERROR') == 'archive' and not sys.argv[-1].endswith('PKGBUILD'): print('demo E: bad package')
if os.environ.get('INTERNAL_LIBRARY_DIR') and not os.environ.get('MIXED_PKGCONFIG_DEPENDENCY') and not sys.argv[-1].endswith('PKGBUILD'):
    print("demo E: Dependency demo-old detected and not included (libraries ['usr/lib/libfixture.so.1'] needed in files ['usr/bin/demo'])")
if os.environ.get('INTERNAL_PYTHON_DIR') and not sys.argv[-1].endswith('PKGBUILD'):
    reasons=["python modules ['fixture', '_fixture'] needed in files ['usr/bin/demo.py', 'usr/lib/python"+str(sys.version_info.major)+'.'+str(sys.version_info.minor)+"/site-packages/fixture.py']"]
    if os.environ.get('MIXED_INTERNAL_DEPENDENCY'):
        reasons.append("libraries ['usr/lib/libfixture.so.1'] needed in files ['usr/bin/demo']")
    if os.environ.get('REVERSE_INTERNAL_REASONS'): reasons.reverse()
    if os.environ.get('UNKNOWN_INTERNAL_REASON'): reasons.append("programs ['missing'] needed in scripts ['usr/bin/demo.py']")
    print('demo E: Dependency demo-old detected and not included ('+', '.join(reasons)+')')
if os.environ.get('INTERNAL_PKGCONFIG_DIR') and not sys.argv[-1].endswith('PKGBUILD'):
    reasons=["pkg-config files ['usr/lib/pkgconfig/fixture.pc'] needed in files ['usr/lib/pkgconfig/consumer.pc']"]
    if os.environ.get('MIXED_PKGCONFIG_DEPENDENCY'):
        reasons.append("libraries ['usr/lib/libfixture.so.1'] needed in files ['usr/bin/demo']")
    if os.environ.get('REVERSE_INTERNAL_REASONS'): reasons.reverse()
    if os.environ.get('UNKNOWN_INTERNAL_REASON'): reasons.append("programs ['missing'] needed in scripts ['usr/bin/demo']")
    print('demo E: Dependency demo-old detected and not included ('+', '.join(reasons)+')')
if os.environ.get('DEBUG_LINK') and '-debug-1.0-1-' in sys.argv[-1]:
    link=os.environ.get('DEBUG_LINK_NAME','usr/lib/debug/.build-id/ab/0123456789abcdef0123456789abcdef01234567')
    print('demo E: Symlink ('+link+') points to non-existing '+os.environ['DEBUG_LINK'])
if os.environ.get('MUTATE_PRIOR_ARCHIVE') and sys.argv[-1].endswith('demo-waypure-debug-1.0-1-any.pkg.tar'):
    first=__import__('pathlib').Path(sys.argv[-1]).with_name('demo-waypure-1.0-1-any.pkg.tar')
    with first.open('ab') as changed: changed.write(b'changed by later archive lint')
if os.environ.get('ADD_LINT_ARCHIVE') and not sys.argv[-1].endswith('PKGBUILD'):
    __import__('pathlib').Path(sys.argv[-1]).with_name('late.pkg.tar').write_bytes(b'late output')
if os.environ.get('MUTATE_ARCHIVE') and not sys.argv[-1].endswith('PKGBUILD'):
    with open(sys.argv[-1], 'ab') as changed: changed.write(b'changed')
if os.environ.get('MUTATE_CONTEXT') and not sys.argv[-1].endswith('PKGBUILD'):
    with open(os.environ['MUTATE_CONTEXT'], 'ab') as changed: changed.write(b'\\n# changed during archive lint\\n')
if os.environ.get('MUTATE_EARLIER') and sys.argv[-1].endswith('/second/PKGBUILD'):
    earlier=__import__('pathlib').Path(os.environ['EARLIER_PACKAGE'])
    target=earlier/'PKGBUILD' if os.environ['MUTATE_EARLIER']=='recipe' else next(earlier.glob('.waypure-validation-*.log/*.pkg.tar'))
    with target.open('ab') as changed: changed.write(b'\\n# changed by later package\\n')
''')
        self.executable('makechrootpkg', '''
import hashlib, io, json, os, sys, tarfile
from pathlib import Path
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['makechrootpkg',*sys.argv[1:]])+'\\n')
print('native build output')
if os.environ.get('CHROOT_STATUS'): sys.exit(int(os.environ['CHROOT_STATUS']))
if not os.environ.get('NO_OUTPUT'):
    fields=Path('.SRCINFO').read_text().splitlines()
    base=next(line.split(' = ',1)[1] for line in fields if line.strip().startswith('pkgbase = '))
    names=[line.split(' = ',1)[1] for line in fields if line.strip().startswith('pkgname = ')]
    if os.environ.get('INCOMPLETE_SPLIT'): names=names[:1]
    if os.environ.get('UNEXPECTED_OUTPUT'): names.append('surprise')
    if os.environ.get('DEBUG_OUTPUT'): names.append('demo-waypure-debug')
    for name in names:
        name=os.environ.get('OUTPUT_NAME',name)
        version=os.environ.get('OUTPUT_VERSION','1.0-1')
        path=Path(os.environ['PKGDEST'])/(name+'-1.0-1-any.pkg.tar')
        with tarfile.open(path,'w') as archive:
            data=('pkgname = '+name+'\\npkgbase = '+base+'\\npkgver = '+version+'\\narch = any\\n'+os.environ.get('OUTPUT_METADATA','')).encode()
            if name.endswith('-debug') and not os.environ.get('BAD_DEBUG'): data+=b'xdata = pkgtype=debug\\n'
            item=tarfile.TarInfo('.PKGINFO'); item.size=len(data); archive.addfile(item,io.BytesIO(data))
            if not os.environ.get('NO_BUILDINFO'):
                digest=os.environ.get('PROVENANCE_HASH',hashlib.sha256(Path('PKGBUILD').read_bytes()).hexdigest())
                data=('pkgbuild_sha256sum = '+digest+'\\n').encode()
                item=tarfile.TarInfo('.BUILDINFO'); item.size=len(data); archive.addfile(item,io.BytesIO(data))
            if os.environ.get('FORBIDDEN_PATH'):
                item=tarfile.TarInfo('usr/bin/Xwayland'); item.size=1; archive.addfile(item,io.BytesIO(b'x'))
            if os.environ.get('INTERNAL_LIBRARY_DIR'):
                for entry in Path(os.environ['INTERNAL_LIBRARY_DIR']).rglob('*'):
                    if not entry.is_dir(): archive.add(entry,arcname=entry.relative_to(os.environ['INTERNAL_LIBRARY_DIR']).as_posix())
            if os.environ.get('INTERNAL_PKGCONFIG_DIR'):
                for entry in Path(os.environ['INTERNAL_PKGCONFIG_DIR']).rglob('*'):
                    if not entry.is_dir(): archive.add(entry,arcname=entry.relative_to(os.environ['INTERNAL_PKGCONFIG_DIR']).as_posix())
            if os.environ.get('INTERNAL_PYTHON_DIR'):
                for entry in Path(os.environ['INTERNAL_PYTHON_DIR']).rglob('*'):
                    if entry.is_symlink() or not entry.is_dir(): archive.add(entry,arcname=entry.relative_to(os.environ['INTERNAL_PYTHON_DIR']).as_posix())
            if os.environ.get('DEBUG_LINK'):
                if name.endswith('-debug'):
                    item=tarfile.TarInfo(os.environ.get('DEBUG_LINK_NAME','usr/lib/debug/.build-id/ab/0123456789abcdef0123456789abcdef01234567'))
                    item.type=tarfile.SYMTYPE; item.linkname=os.environ['DEBUG_LINK']; archive.addfile(item)
                else:
                    item=tarfile.TarInfo('usr/bin/demo');item.size=4;archive.addfile(item,io.BytesIO(b'demo'))
    if os.environ.get('CHANGE_INPUT'): Path('PKGBUILD').write_text(Path('PKGBUILD').read_text()+'# changed\\n')
''')
        self.executable('pacman', '''
import json, os, sys, tarfile
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['pacman',*sys.argv[1:]])+'\\n')
if sys.argv[1]!='-Qp': sys.exit(91)
with tarfile.open(sys.argv[2]) as archive:
    fields=dict(line.split(' = ',1) for line in archive.extractfile('.PKGINFO').read().decode().splitlines())
print(fields['pkgname'],fields['pkgver'])
''')
        self.executable('vercmp', 'import sys\nprint((sys.argv[1]>sys.argv[2])-(sys.argv[1]<sys.argv[2]))\n')

    def executable(self, name, body):
        path = self.bin/name
        path.write_text('#!'+sys.executable+'\n'+body)
        path.chmod(0o755)

    def archbuild_tools(self):
        chroot = self.root/'extra-x86_64'
        self.chroot.rename(chroot)
        self.chroot = chroot
        self.executable('doas', '''
import json, os, sys
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['doas',*sys.argv[1:]])+'\\n')
clean={'PATH':'/usr/bin:/bin', **{key:os.environ[key] for key in
       ('TRACE','CHROOT_STATUS','MUTATE_BUILD_CONTEXT','MUTATE_CONTEXT') if key in os.environ}}
os.execve(sys.argv[1],sys.argv[1:],clean)
''')
        self.executable('extra-x86_64-build', '''
import json, os, subprocess, sys
from pathlib import Path
with open(os.environ['TRACE'],'a') as out:
    out.write(json.dumps(['extra-x86_64-build',*sys.argv[1:]])+'\\n')
    out.write(json.dumps(['build_environment',os.environ.get('SUDO_USER'),os.environ.get('PKGDEST'),os.environ.get('SRCDEST')])+'\\n')
root=Path(sys.argv[sys.argv.index('-r')+1])/'extra-x86_64'
with (root/'root/etc/makepkg.conf').open('a') as stream: stream.write('# native baseline update\\n')
if os.environ.get('MUTATE_BUILD_CONTEXT'):
    with open(os.environ['MUTATE_BUILD_CONTEXT'],'a') as stream: stream.write('\\n# native context drift\\n')
tail=sys.argv[sys.argv.index('--')+1:]
result=subprocess.run([str(Path(__file__).with_name('makechrootpkg')),'-c','-n','-C','-r',str(root),*tail])
for path in Path(os.environ['PKGDEST']).glob('*.pkg.tar'):
    (Path(os.environ['LOGDEST'])/(path.name+'-namcap.log')).touch()
sys.exit(result.returncode)
''')

    def test_root_execution_is_rejected_before_host_recipe_commands(self):
        from optionkit.release_validation import main
        self.receipt.write_text('{"status":"PASS"}')
        errors=io.StringIO()
        with patch('optionkit.release_validation.os.geteuid',return_value=0), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as raised:
                main(['--repo',str(self.repo),'--chroot',str(self.chroot),'demo'])
        self.assertEqual(raised.exception.code,2)
        self.assertIn('normal user',errors.getvalue())
        self.assertIn('--auth doas',errors.getvalue())
        self.assertFalse(self.receipt.exists())
        self.assertEqual(self.calls(),[])

    def test_incomplete_chroot_is_rejected_before_execution(self):
        for relative in ('.arch-chroot','usr/bin/bash','usr/bin/pacman'):
            with self.subTest(relative=relative):
                path=self.chroot/'root'/relative
                saved=path.read_bytes()
                path.unlink()
                result=self.run_command('--plan','--chroot',self.chroot)
                path.write_bytes(saved)
                path.chmod(0o755)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('incomplete',result.stderr.lower())
                self.assertIn(relative,result.stderr)
                self.assertEqual(self.calls(),[])

    def test_doas_archbuild_plan_is_readonly_and_preserves_explicit_destinations(self):
        self.archbuild_tools()
        result=self.run_command('--plan','--json','--builder','extra-x86_64-build','--auth','doas','--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        row=report['packages'][0]
        command=row['build_command']
        self.assertEqual(command[:2],[str(self.bin/'doas'),shutil.which('env',path=self.env['PATH'])])
        self.assertIn('SUDO_USER='+__import__('pwd').getpwuid(os.getuid()).pw_name,command)
        for key,value in row['environment'].items(): self.assertIn(key+'='+value,command)
        wrapper=command.index(str(self.bin/'extra-x86_64-build'))
        self.assertEqual(command[wrapper+1:wrapper+4],['-r',str(self.chroot.parent),'--'])
        self.assertIn('-U',command)
        self.assertEqual(command[-3:],['--','--force','--check'])
        self.assertEqual(report['builder'],'extra-x86_64-build')
        self.assertEqual(report['auth'],'doas')
        self.assertEqual(self.calls(),[])

    def test_archbuild_updates_are_recorded_and_generate_fresh_receipt(self):
        self.archbuild_tools()
        result=self.run_command('--json','--builder','extra-x86_64-build','--auth','doas','--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        self.assertEqual(receipt['status'],'PASS')
        self.assertEqual(receipt['builder'],'extra-x86_64-build')
        self.assertEqual(receipt['auth'],'doas')
        self.assertNotEqual(receipt['baseline_before_build']['sha256'],receipt['baseline']['sha256'])
        native_environment=next(call for call in self.calls() if call[0]=='build_environment')
        self.assertEqual(native_environment[2],receipt['output_directory'])
        self.assertEqual(native_environment[3],str(self.package))
        self.assertEqual(sum(call[0]=='doas' for call in self.calls()),1)
        self.assertTrue(receipt['artifacts'][0]['sha256'])

    def test_archbuild_failure_or_context_drift_never_creates_receipt(self):
        self.archbuild_tools()
        policy=self.root/'policy.toml'
        policy.write_bytes((TOOLS/'waypure-policy.toml').read_bytes())
        for env in ({'CHROOT_STATUS':'4'},{'MUTATE_BUILD_CONTEXT':str(policy)},
                    {'MUTATE_CONTEXT':str(self.chroot/'root/etc/makepkg.conf')}):
            with self.subTest(env=env):
                self.trace.unlink(missing_ok=True)
                result=self.run_command('--builder','extra-x86_64-build','--auth','doas',
                                        '--chroot',self.chroot,'--policy',policy,env=env)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(self.receipt.exists())
                self.assertTrue(any(call[0]=='extra-x86_64-build' for call in self.calls()),result.stderr)

    def test_archbuild_rejects_mismatched_chroot_name_without_running_commands(self):
        result=self.run_command('--plan','--builder','extra-x86_64-build','--chroot',self.chroot)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('extra-x86_64',result.stderr)
        self.assertEqual(self.calls(),[])

    def checked_update_tools(self):
        self.executable('doas', '''
import os, sys
os.execv(sys.argv[1],sys.argv[1:])
''')
        self.executable('arch-nspawn', '''
import json, os, sys
from pathlib import Path
with open(os.environ['TRACE'],'a') as out: out.write(json.dumps(['arch-nspawn',*sys.argv[1:]])+'\\n')
if sys.argv[2:] == ['pacman','-Syuu','--noconfirm']:
    if os.environ.get('UPDATE_STATUS'): sys.exit(int(os.environ['UPDATE_STATUS']))
    with (Path(sys.argv[1])/'etc/makepkg.conf').open('a') as out: out.write('# checked update\\n')
elif sys.argv[2:] == ['gcc','-march=native','-Q','--help=target']:
    if os.environ.get('TARGET_STATUS'): sys.exit(int(os.environ['TARGET_STATUS']))
    print(os.environ.get('TARGET_RESULT','  -march=                     znver4\\n  -mtune=                     znver4'))
else: sys.exit(92)
''')

    def test_checked_update_plan_is_readonly_and_uses_fresh_copy_without_unchecked_update(self):
        self.checked_update_tools()
        result=self.run_command('--plan','--json','--auth','doas','--chroot',self.chroot,'--update-baseline')
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        row=report['packages'][0]
        self.assertIn('flock',Path(row['update_command'][1]).name)
        self.assertEqual(row['update_command'][-4:],[str(self.chroot/'root'),'pacman','-Syuu','--noconfirm'])
        self.assertIn('-c',row['build_command'])
        self.assertNotIn('-u',row['build_command'])
        self.assertIn('checks',report['update_assurance'])
        self.assertEqual(self.calls(),[])

    def test_failed_checked_update_prevents_build_and_pass_receipt(self):
        self.checked_update_tools()
        self.receipt.write_text('{"status":"PASS"}')
        result=self.run_command('--auth','doas','--chroot',self.chroot,'--update-baseline',env={'UPDATE_STATUS':'7'})
        self.assertNotEqual(result.returncode,0)
        self.assertIn('exited 7',result.stderr)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(any(c[0]=='makechrootpkg' for c in self.calls()))

    def test_checked_update_records_each_baseline_transition_and_native_cpu_target(self):
        self.checked_update_tools()
        self.second_package()
        result=self.run_command('--json','--auth','doas','--chroot',self.chroot,'--update-baseline',packages=('demo','second'))
        self.assertEqual(result.returncode,0,result.stderr)
        first,second=json.loads(result.stdout)['packages']
        self.assertNotEqual(first['baseline_before_build']['sha256'],first['baseline']['sha256'])
        self.assertEqual(first['baseline'],second['baseline_before_build'])
        self.assertEqual(first['compiler_native_target'],{'march':'znver4','mtune':'znver4'})
        self.assertTrue(Path(first['update_log']).is_file())
        calls=self.calls()
        self.assertEqual([c[2] for c in calls if c[0]=='arch-nspawn'],['pacman','gcc','pacman','gcc'])

    def test_failed_or_unresolved_native_query_prevents_build_and_pass(self):
        self.checked_update_tools()
        for env in ({'TARGET_STATUS':'8'}, {'TARGET_RESULT':'  -march= native\n  -mtune= native'},
                    {'TARGET_RESULT':'unrecognized compiler output'}):
            with self.subTest(env=env):
                self.trace.unlink(missing_ok=True)
                result=self.run_command('--auth','doas','--chroot',self.chroot,'--update-baseline',env=env)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(self.receipt.exists())
                self.assertFalse(any(c[0]=='makechrootpkg' for c in self.calls()))

    def test_checked_update_requires_explicit_auth_and_direct_builder(self):
        self.archbuild_tools()
        for extra in ([], ['--auth','doas','--builder','extra-x86_64-build']):
            with self.subTest(extra=extra):
                result=self.run_command('--plan','--chroot',self.chroot,'--update-baseline',*extra)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('--update-baseline requires',result.stderr)
                self.assertEqual(self.calls(),[])

    def test_debug_build_id_link_is_checked_against_current_primary_archive(self):
        result=self.run_command('--chroot',self.chroot,env={'DEBUG_OUTPUT':'1','DEBUG_LINK':'../../../../bin/demo'})
        self.assertEqual(result.returncode,0,result.stderr)
        record=json.loads(self.receipt.read_text())
        findings=[f for lint in record['namcap'] for f in lint['findings'] if f['code']=='namcap.debug-companion']
        self.assertEqual(len(findings),1)
        self.assertEqual(findings[0]['severity'],'note')
        self.assertIn('demo-waypure-1.0-1-any.pkg.tar',findings[0]['resolved_by'])

    def test_missing_or_unrelated_debug_symlinks_still_fail(self):
        for extra in ({'DEBUG_LINK':'../../../../bin/missing'},
                      {'DEBUG_LINK':'../../../../bin/demo','DEBUG_LINK_NAME':'usr/share/bad-link'},
                      {'DEBUG_LINK':'../../../../bin/demo','LINT_ERROR':'archive'}):
            with self.subTest(extra=extra):
                result=self.run_command('--chroot',self.chroot,env={'DEBUG_OUTPUT':'1',**extra})
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(self.receipt.exists())

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_host_owner_of_library_shipped_in_same_archive_is_not_a_dependency(self):
        files = self.internal_library_files()
        result = self.run_command('--chroot', self.chroot, env={'INTERNAL_LIBRARY_DIR': str(files)})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.receipt.read_text())
        resolved = [f for lint in record['namcap'] for f in lint['findings']
                    if f['code'] == 'namcap.internal-library']
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]['severity'], 'note')
        self.assertIn('usr/lib/libfixture.so.1.2', resolved[0]['resolved_by'])

    @unittest.skipUnless(shutil.which('pkg-config'), 'native pkg-config needed')
    def test_host_owner_of_pkgconfig_shipped_in_same_archive_is_not_a_dependency(self):
        files = self.internal_pkgconfig_files()
        result = self.run_command('--chroot', self.chroot, env={'INTERNAL_PKGCONFIG_DIR': str(files)})
        self.assertEqual(result.returncode, 0, result.stderr)
        record = json.loads(self.receipt.read_text())
        resolved = [f for lint in record['namcap'] for f in lint['findings']
                    if f['code'] == 'namcap.internal-pkgconfig']
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]['severity'], 'note')
        self.assertEqual(resolved[0]['resolved_by'], ['usr/lib/pkgconfig/fixture.pc'])

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('cc', 'readelf', 'pkg-config')),
                         'C compiler, readelf and pkg-config needed')
    def test_mixed_internal_pkgconfig_and_library_reasons_are_proven_in_either_order(self):
        for reverse in (False, True):
            with self.subTest(reverse=reverse):
                libraries = self.internal_library_files()
                pkgconfig = self.internal_pkgconfig_files()
                result = self.run_command('--chroot', self.chroot, env={
                    'INTERNAL_LIBRARY_DIR': str(libraries),
                    'INTERNAL_PKGCONFIG_DIR': str(pkgconfig),
                    'MIXED_PKGCONFIG_DEPENDENCY': '1',
                    **({'REVERSE_INTERNAL_REASONS': '1'} if reverse else {}),
                })
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.receipt.read_text())
                resolved = [f for lint in record['namcap'] for f in lint['findings']
                            if f['code'] in {'namcap.internal-library', 'namcap.internal-pkgconfig'}]
                self.assertEqual({f['code'] for f in resolved},
                                 {'namcap.internal-library', 'namcap.internal-pkgconfig'})
                self.assertTrue(all(f['severity'] == 'note' for f in resolved))

    @unittest.skipUnless(all(shutil.which(tool) for tool in ('cc', 'readelf', 'pkg-config')),
                         'C compiler, readelf and pkg-config needed')
    def test_mixed_internal_reasons_keep_any_unproven_dependency_fatal(self):
        for kind in ('missing-library', 'wrong-abi', 'wrong-version', 'unknown', 'other-error'):
            with self.subTest(kind=kind):
                libraries = self.internal_library_files()
                pkgconfig = self.internal_pkgconfig_files()
                extra = {}
                if kind == 'missing-library':
                    (libraries/'usr/lib/libfixture.so.1').unlink()
                elif kind == 'wrong-abi':
                    library = libraries/'usr/lib/libfixture.so.1.2'
                    binary = bytearray(library.read_bytes())
                    binary[18:20] = (183).to_bytes(2, 'little')
                    library.write_bytes(binary)
                elif kind == 'wrong-version':
                    provider = pkgconfig/'usr/lib/pkgconfig/fixture.pc'
                    provider.write_text(provider.read_text().replace('1.2', '0.5'))
                elif kind == 'unknown':
                    extra['UNKNOWN_INTERNAL_REASON'] = '1'
                else:
                    extra['LINT_ERROR'] = 'archive'
                result = self.run_command('--chroot', self.chroot, env={
                    'INTERNAL_LIBRARY_DIR': str(libraries),
                    'INTERNAL_PKGCONFIG_DIR': str(pkgconfig),
                    'MIXED_PKGCONFIG_DEPENDENCY': '1', **extra,
                })
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())

    @unittest.skipUnless(shutil.which('pkg-config'), 'native pkg-config needed')
    def test_internal_pkgconfig_does_not_hide_missing_invalid_or_incompatible_dependencies(self):
        for kind in ('missing', 'symlink', 'invalid', 'wrong-version', 'unrelated', 'host-only', 'host-newer', 'uninstalled', 'other-error'):
            with self.subTest(kind=kind):
                files = self.internal_pkgconfig_files()
                provider = files/'usr/lib/pkgconfig/fixture.pc'
                consumer = files/'usr/lib/pkgconfig/consumer.pc'
                extra = {}
                if kind in {'host-only', 'host-newer'}:
                    host = Path(tempfile.mkdtemp(dir=self.root))
                    shutil.copy2(provider, host/'fixture.pc')
                    extra['PKG_CONFIG_PATH'] = str(host)
                if kind in {'missing', 'host-only'}:
                    provider.unlink()
                elif kind == 'symlink':
                    provider.unlink()
                    provider.symlink_to('missing.pc')
                elif kind == 'invalid': provider.write_text('not a pkg-config file\n')
                elif kind in {'wrong-version', 'host-newer', 'uninstalled'}:
                    if kind == 'uninstalled': shutil.copy2(provider, provider.with_name('fixture-uninstalled.pc'))
                    provider.write_text(provider.read_text().replace('1.2', '0.5'))
                elif kind == 'unrelated': consumer.write_text(consumer.read_text().replace('fixture >= 1.0', 'other'))
                else: extra['LINT_ERROR'] = 'archive'
                result = self.run_command('--chroot', self.chroot,
                                          env={'INTERNAL_PKGCONFIG_DIR': str(files), **extra})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())

    def internal_pkgconfig_files(self):
        files = Path(tempfile.mkdtemp(dir=self.root))
        pc = files/'usr/lib/pkgconfig'
        pc.mkdir(parents=True)
        # External requirements must not be needed to prove this one internal edge.
        (pc/'fixture.pc').write_text('Name: fixture\nDescription: Internal provider\nVersion: 1.2\nRequires: external\n')
        (pc/'consumer.pc').write_text('Name: consumer\nDescription: Internal consumer\nVersion: 1\nRequires.private: fixture >= 1.0\n')
        return files

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_missing_invalid_or_unrelated_internal_libraries_do_not_hide_errors(self):
        for kind in ('missing', 'broken', 'cycle', 'missing-intermediate', 'text', 'wrong-soname', 'wrong-abi', 'unrelated', 'other-error'):
            with self.subTest(kind=kind):
                files = self.internal_library_files()
                library = files/'usr/lib/libfixture.so.1.2'
                link = files/'usr/lib/libfixture.so.1'
                extra = {}
                if kind == 'missing': link.unlink()
                if kind in {'broken', 'cycle'}:
                    link.unlink(); link.symlink_to('missing' if kind == 'broken' else link.name)
                if kind == 'missing-intermediate':
                    link.unlink(); link.symlink_to('missing/../' + library.name)
                if kind == 'text': library.write_text('not an ELF library')
                if kind == 'wrong-soname':
                    subprocess.run(['cc', '-shared', '-fPIC', '-Wl,-soname,libother.so.1',
                                    str(self.root/'fixture.c'), '-o', str(library)], check=True)
                if kind == 'wrong-abi':
                    binary = bytearray(library.read_bytes())
                    binary[18:20] = (183).to_bytes(2, 'little')  # AArch64, incompatible with this consumer.
                    library.write_bytes(binary)
                if kind == 'unrelated': shutil.copyfile('/usr/bin/true', files/'usr/bin/demo')
                if kind == 'other-error': extra['LINT_ERROR'] = 'archive'
                result = self.run_command('--chroot', self.chroot,
                                          env={'INTERNAL_LIBRARY_DIR': str(files), **extra})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())

    def internal_library_files(self):
        files = self.root/'elf-fixture'
        if files.exists(): shutil.rmtree(files)
        (files/'usr/lib').mkdir(parents=True)
        (files/'usr/bin').mkdir()
        source = self.root/'fixture.c'
        source.write_text('int fixture(void) { return 0; }\n')
        library = files/'usr/lib/libfixture.so.1.2'
        subprocess.run(['cc', '-shared', '-fPIC', '-Wl,-soname,libfixture.so.1',
                        str(source), '-o', str(library)], check=True)
        (files/'usr/lib/libfixture.so.1').symlink_to(library.name)
        source = self.root/'consumer.c'
        source.write_text('extern int fixture(void); int main(void) { return fixture(); }\n')
        subprocess.run(['cc', str(source), str(library), '-o', str(files/'usr/bin/demo')], check=True)
        return files

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_exact_sibling_library_dependency_is_verified_from_current_outputs(self):
        self.check_sibling_library('valid', resolved=True)

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_invalid_sibling_library_evidence_preserves_error(self):
        for kind in ('no-dependency', 'wrong-version', 'wrong-base', 'wrong-provenance',
                     'unselected', 'wrong-abi', 'broken-link', 'ambiguous'):
            with self.subTest(kind=kind):
                self.check_sibling_library(kind, resolved=False)

    def check_sibling_library(self, kind, *, resolved):
        from optionkit import release_validation as rv
        from optionkit.runtime_policy import load_policy
        files = self.internal_library_files()
        if kind == 'wrong-abi':
            library = files/'usr/lib/libfixture.so.1.2'
            binary = bytearray(library.read_bytes())
            binary[18:20] = (183).to_bytes(2, 'little')
            library.write_bytes(binary)
        if kind == 'broken-link':
            link = files/'usr/lib/libfixture.so.1'
            link.unlink(); link.symlink_to('missing')
        policy = load_policy(TOOLS/'waypure-policy.toml')
        row = {'pkgbase': 'demo-waypure', 'pkgnames': ['demo-waypure', 'demo-python-waypure'],
               'version': '1.0-1', 'inputs': {'PKGBUILD': 'a'*64}}

        def archive(name, members, *, provider=False):
            path = self.root/(name+'.pkg.tar')
            version = '2.0-1' if provider and kind == 'wrong-version' else row['version']
            base = 'other' if provider and kind == 'wrong-base' else row['pkgbase']
            provenance = 'b'*64 if provider and kind == 'wrong-provenance' else row['inputs']['PKGBUILD']
            metadata = f'pkgname = {name}\npkgbase = {base}\npkgver = {version}\narch = x86_64\n'
            if not provider and kind != 'no-dependency':
                metadata += 'depend = demo-waypure=1.0-1\n'
            with tarfile.open(path, 'w') as output:
                for name, content in {'.PKGINFO': metadata, '.BUILDINFO':
                                      f'pkgbuild_sha256sum = {provenance}\n'}.items():
                    data = content.encode(); item = tarfile.TarInfo(name); item.size = len(data)
                    output.addfile(item, io.BytesIO(data))
                for member in members:
                    output.add(files/member, arcname=member, recursive=False)
            return path

        consumer = archive('demo-python-waypure', ['usr/bin/demo'])
        provider = archive('demo-waypure', ['usr/lib/libfixture.so.1', 'usr/lib/libfixture.so.1.2'], provider=True)
        if kind == 'unselected':
            row['pkgnames'].remove('demo-waypure')
        companions = [provider]
        if kind == 'ambiguous':
            duplicate = self.root/'duplicate.pkg.tar'; shutil.copyfile(provider, duplicate)
            companions.append(duplicate)
        message = (consumer.name + ": Dependency demo detected and not included (libraries "
                   "['usr/lib/libfixture.so.1'] needed in files ['usr/bin/demo'])")
        lint = {'status': 'FAIL', 'findings': [{'severity': 'error', 'code': 'namcap', 'message': message}]}
        rv.internal_library_dependencies(lint, consumer, rv.audit_artifact(consumer, policy),
                                         companions=companions, row=row, policy=policy)
        self.assertEqual(lint['findings'][0]['severity'], 'note' if resolved else 'error')
        if resolved:
            self.assertIn('demo-waypure', str(lint['findings'][0]['resolved_by']))

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_bundled_python_modules_and_mixed_library_findings_are_proven_without_importing(self):
        for mixed, reverse in ((False, False), (True, False), (True, True)):
            with self.subTest(mixed=mixed, reverse=reverse):
                files = self.internal_python_files()
                result = self.run_command('--chroot', self.chroot,
                                          env={'INTERNAL_PYTHON_DIR': str(files),
                                               **({'MIXED_INTERNAL_DEPENDENCY': '1'} if mixed else {}),
                                               **({'REVERSE_INTERNAL_REASONS': '1'} if reverse else {})})
                self.assertEqual(result.returncode, 0, result.stderr)
                record = json.loads(self.receipt.read_text())
                resolved = [f for lint in record['namcap'] for f in lint['findings']
                            if f['code'] == 'namcap.internal-python']
                self.assertEqual(len(resolved), 1)
                self.assertEqual(resolved[0]['severity'], 'note')
                self.assertTrue(any(p.endswith('/_fixture.so') for p in resolved[0]['resolved_by']))
                self.assertFalse((self.root/'imported-archive-code').exists())

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_internal_python_resolution_retains_missing_and_unverifiable_edges(self):
        for kind in ('missing-python', 'missing-extension', 'broken-link', 'invalid-python',
                     'unrelated-import', 'missing-consumer', 'wrong-abi', 'missing-init',
                     'private-directory', 'missing-library', 'binary-consumer', 'oversized-python',
                     'oversized-package-shadow', 'symlink-package-shadow', 'compiled-package-shadow',
                     'package-directory-link', 'bytecode-package-shadow',
                     'unknown-reason', 'other-error'):
            with self.subTest(kind=kind):
                files = self.internal_python_files()
                site = files/f'usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
                extra = {}
                if kind == 'missing-python': (site/'fixture.py').unlink()
                if kind == 'missing-extension': (site/'_fixture.so').unlink()
                if kind == 'broken-link':
                    (site/'_fixture.so').unlink()
                    (site/'_fixture.so').symlink_to('missing.so')
                if kind == 'invalid-python': (site/'fixture.py').write_text('invalid syntax!\n')
                if kind == 'unrelated-import': (files/'usr/bin/demo.py').write_text('import unrelated\n')
                if kind == 'missing-consumer': (files/'usr/bin/demo.py').unlink()
                if kind == 'binary-consumer': shutil.copyfile(site/'_fixture.so', files/'usr/bin/demo.py')
                if kind == 'oversized-python': (site/'fixture.py').write_bytes(b'#' * (2 * 1024 * 1024 + 1))
                if kind in {'oversized-package-shadow', 'symlink-package-shadow', 'compiled-package-shadow'}:
                    package = site/'fixture'
                    package.mkdir()
                    if kind == 'oversized-package-shadow':
                        (package/'__init__.py').write_bytes(b'#' * (2 * 1024 * 1024 + 1))
                    elif kind == 'symlink-package-shadow': (package/'__init__.py').symlink_to('../fixture.py')
                    else: shutil.copyfile(site/'_fixture.so', package/'__init__.so')
                if kind == 'package-directory-link':
                    (site/'unverified-package').mkdir()
                    (site/'unverified-package/__init__.py').write_text('invalid syntax!\n')
                    (site/'fixture').symlink_to('unverified-package')
                if kind == 'bytecode-package-shadow':
                    package = site/'fixture'
                    package.mkdir()
                    source = self.root/'bytecode-source.py'
                    source.write_text('import missing_runtime_dependency\n')
                    py_compile.compile(str(source), cfile=str(package/'__init__.pyc'), doraise=True)
                if kind == 'wrong-abi':
                    binary = bytearray((site/'_fixture.so').read_bytes())
                    binary[18:20] = (183).to_bytes(2, 'little')
                    (site/'_fixture.so').write_bytes(binary)
                if kind == 'missing-init':
                    subprocess.run(['cc', '-shared', '-fPIC', str(self.root/'fixture.c'),
                                    '-o', str(site/'_fixture.so')], check=True)
                if kind == 'private-directory':
                    shutil.move(str(site/'_fixture.so'), str(files/'usr/lib/_fixture.so'))
                if kind == 'missing-library': (files/'usr/lib/libfixture.so.1').unlink()
                if kind == 'unknown-reason': extra['UNKNOWN_INTERNAL_REASON'] = '1'
                if kind == 'other-error': extra['LINT_ERROR'] = 'archive'
                result = self.run_command('--chroot', self.chroot,
                                          env={'INTERNAL_PYTHON_DIR': str(files),
                                               'MIXED_INTERNAL_DEPENDENCY': '1', **extra})
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())

    def internal_python_files(self):
        files = self.internal_library_files()
        site = files/f'usr/lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages'
        site.mkdir(parents=True)
        (files/'usr/bin/demo.py').write_text('import fixture\n')
        (site/'fixture.py').write_text("open(" + repr(str(self.root/'imported-archive-code')) + ", 'w').close()\nimport _fixture\n")
        extension = self.root/'python-extension.c'
        extension.write_text('void *PyInit__fixture(void) { return 0; }\n')
        subprocess.run(['cc', '-shared', '-fPIC', str(extension), '-o', str(site/'_fixture.so')], check=True)
        return files

    def test_selected_contents_can_overlap_metadata_and_pkgconfig_in_a_stream(self):
        from optionkit.release_validation import archive_metadata
        path = self.root/'contents.pkg.tar'
        entries = {'.PKGINFO': b'pkgname = fixture\n',
                   'usr/lib/pkgconfig/fixture.pc': b'Name: fixture\nVersion: 1\n'}
        with tarfile.open(path, 'w') as archive:
            for name, data in entries.items():
                item = tarfile.TarInfo(name)
                item.size = len(data)
                archive.addfile(item, io.BytesIO(data))
        result = archive_metadata(path, include_pkgconfig=True, include_contents=set(entries))
        self.assertEqual(result['.PKGINFO']['pkgname'], ['fixture'])
        self.assertEqual(result['contents'], entries)
        self.assertEqual(result['pkgconfig']['usr/lib/pkgconfig/fixture.pc'], entries['usr/lib/pkgconfig/fixture.pc'])

    def test_companion_inventory_rejects_canonical_aliases_and_type_collisions(self):
        from optionkit.release_validation import archive_metadata
        for alias,kind in (('usr//bin/demo',tarfile.SYMTYPE),
                           ('usr/bin/./demo',tarfile.SYMTYPE),
                           ('././usr/bin/demo',tarfile.DIRTYPE)):
            with self.subTest(alias=alias):
                path=self.root/'aliases.pkg.tar'
                with tarfile.open(path,'w') as archive:
                    item=tarfile.TarInfo('usr/bin/demo');item.size=1
                    archive.addfile(item,io.BytesIO(b'x'))
                    item=tarfile.TarInfo(alias);item.type=kind;item.linkname='missing'
                    archive.addfile(item)
                with self.assertRaisesRegex(ValueError,'duplicate archive member'):
                    archive_metadata(path,include_files=True)

    def test_language_and_repository_configuration_changes_invalidate_receipts(self):
        for relative in ('etc/makepkg.conf.d/rust.conf','etc/pacman.d/cachyos-mirrorlist'):
            with self.subTest(relative=relative):
                path=self.chroot/'root'/relative
                path.parent.mkdir(parents=True,exist_ok=True)
                path.write_text('# original configuration\n')
                result=self.run_command('--chroot',self.chroot,env={'MUTATE_CONTEXT':str(path)})
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertIn('baseline changed',result.stderr)
                self.assertFalse(self.receipt.exists())

    def test_baseline_hash_tracks_added_removed_dropins_and_profile(self):
        from optionkit.release_validation import baseline
        first=baseline(self.chroot)
        path=self.chroot/'root/etc/makepkg.conf.d/rust.conf'
        path.parent.mkdir()
        path.write_text('RUSTFLAGS="-C target-cpu=native"\n')
        second=baseline(self.chroot)
        self.assertNotEqual(first,second)
        self.assertIn('etc/makepkg.conf.d/rust.conf',second['sha256'])
        path.unlink()
        self.assertEqual(first,baseline(self.chroot))
        profile=self.chroot/'root/etc/waypure-build-profile.json'
        profile.write_text('{"target":"native"}')
        self.assertNotEqual(first,baseline(self.chroot))

    @unittest.skipUnless(Path('/usr/share/devtools/lib/archroot.sh').is_file(), 'installed devtools version needed')
    def test_outdated_marker_is_rejected_before_archbuild_can_recreate_copies(self):
        self.archbuild_tools()
        (self.chroot/'root/.arch-chroot').write_text('v999999999\n')
        result=self.run_command('--plan','--builder','extra-x86_64-build','--chroot',self.chroot)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('outdated',result.stderr)
        self.assertEqual(self.calls(),[])

    def test_sudo_plan_elevates_only_build_and_selects_normal_user(self):
        result=self.run_command('--plan','--json','--auth','sudo','--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        command=json.loads(result.stdout)['packages'][0]['build_command']
        self.assertEqual(command[:3],[shutil.which('sudo',path=self.env['PATH']),'--',shutil.which('env',path=self.env['PATH'])])
        self.assertIn(str(self.bin/'makechrootpkg'),command)
        self.assertEqual(command[command.index('-U')+1],__import__('pwd').getpwuid(os.getuid()).pw_name)
        self.assertEqual(self.calls(),[])

    def second_package(self):
        second=self.repo/'second'
        second.mkdir()
        for name in ('PKGBUILD','.SRCINFO'):
            (second/name).write_text((self.package/name).read_text().replace('demo-waypure','second-waypure'))
        return second

    def test_sequential_wrapper_receipts_record_each_baseline_transition(self):
        self.archbuild_tools()
        self.second_package()
        result=self.run_command('--json','--builder','extra-x86_64-build','--auth','doas',
                                '--chroot',self.chroot,packages=('demo','second'))
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        first,second=report['packages']
        self.assertEqual(first['baseline'],second['baseline_before_build'])
        self.assertEqual(report['baseline_after_validation'],second['baseline'])
        self.assertEqual([r['artifacts'][0]['pkgname'] for r in report['packages']],['demo-waypure','second-waypure'])

    def test_later_recipe_cannot_leave_earlier_stale_recipe_or_archive_as_pass(self):
        self.second_package()
        for kind in ('recipe','archive'):
            with self.subTest(kind=kind):
                for directory in self.package.glob('.waypure-validation-*.log'): shutil.rmtree(directory)
                before=(self.package/'PKGBUILD').read_bytes()
                result=self.run_command('--chroot',self.chroot,packages=('demo','second'),
                                        env={'MUTATE_EARLIER':kind,'EARLIER_PACKAGE':str(self.package)})
                (self.package/'PKGBUILD').write_bytes(before)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertFalse(self.receipt.exists())
                self.assertIn('changed',result.stderr)

    def run_command(self, *arguments, env=None, packages=('demo',)):
        return subprocess.run([sys.executable, str(TOOLS/'waypure-validate'), '--repo', str(self.repo),
                               *map(str, arguments), *packages], capture_output=True, text=True,
                              env={**self.env, **(env or {})}, timeout=15)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    @property
    def receipt(self):
        return self.package/'.waypure-validation.json.log'

    def archive(self, name='helper', version='1.0-1', extra='', buildinfo=None):
        path=self.root/(name+'-'+version+'-any.pkg.tar')
        with tarfile.open(path,'w') as archive:
            data=('pkgname = '+name+'\npkgver = '+version+'\narch = any\n'+extra).encode()
            item=tarfile.TarInfo('.PKGINFO'); item.size=len(data); archive.addfile(item,io.BytesIO(data))
            if buildinfo is not None:
                data=buildinfo.encode()
                item=tarfile.TarInfo('.BUILDINFO'); item.size=len(data); archive.addfile(item,io.BytesIO(data))
        return path

    def test_plan_is_readonly_and_shows_clean_forced_checked_build(self):
        result = self.run_command('--plan','--json','--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['status'],'PLAN')
        build=report['packages'][0]['build_command']
        self.assertEqual(build[:5],['makechrootpkg','-c','-u','-r',str(self.chroot)])
        self.assertIn('--force',build)
        self.assertIn('--check',build)
        self.assertEqual(self.calls(),[])
        self.assertFalse(self.receipt.exists())
        self.assertEqual(sorted(p.name for p in self.package.iterdir()),['.SRCINFO','PKGBUILD'])

    def test_update_is_reported_as_requested_without_claiming_native_success(self):
        result = self.run_command('--plan', '--json', '--chroot', self.chroot)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn('not independently verified', report['update_assurance'])
        self.assertIn('not independently verified', report['baseline']['update'])
        self.assertIn('update failure', ' '.join(report['notes']))
        self.assertNotIn('updated working copy', report['scope'])

    def test_execution_requires_explicit_existing_chroot_and_tools(self):
        for arguments in [(), ('--chroot',self.root/'missing')]:
            with self.subTest(arguments=arguments):
                result=self.run_command(*arguments)
                self.assertNotEqual(result.returncode,0)
                self.assertFalse(self.receipt.exists())
        (self.bin/'makechrootpkg').unlink()
        result=self.run_command('--chroot',self.chroot,env={'PATH':str(self.bin)})
        self.assertNotEqual(result.returncode,0)
        self.assertIn('makechrootpkg',result.stderr)

    def test_execution_verifies_sources_lints_and_records_hashed_artifacts(self):
        result=self.run_command('--chroot',self.chroot,'--json')
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        self.assertEqual(receipt['status'],'PASS')
        self.assertEqual(receipt['version'],'1.0-1')
        self.assertIn('native build output',Path(receipt['build_log']).read_text())
        self.assertEqual(receipt['inputs']['PKGBUILD'],hashlib.sha256((self.package/'PKGBUILD').read_bytes()).hexdigest())
        self.assertEqual(receipt['artifacts'][0]['pkgname'],'demo-waypure')
        self.assertEqual(receipt['artifacts'][0]['buildinfo']['pkgbuild_sha256sum'],[receipt['inputs']['PKGBUILD']])
        self.assertTrue(receipt['artifacts'][0]['sha256'])
        self.assertEqual(receipt['baseline']['packages'],[{'name':'base-devel','version':'1-1'}])
        self.assertIn('sufficien',receipt['scope'].lower())
        calls=self.calls()
        verify=calls.index(['makepkg','--verifysource','--nodeps'])
        build=next(i for i,call in enumerate(calls) if call[0]=='makechrootpkg')
        self.assertLess(verify,build)
        self.assertEqual(sum(call[0]=='namcap' for call in calls),2)
        self.assertFalse(any(call[:2]==['pacman','-U'] for call in calls))

    def test_failed_attempt_invalidates_prior_receipt_and_never_accepts_old_output(self):
        old=self.archive('demo-waypure')
        (self.package/old.name).write_bytes(old.read_bytes())
        self.receipt.write_text('{"status":"PASS"}')
        result=self.run_command('--chroot',self.chroot,env={'NO_OUTPUT':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.receipt.exists())
        self.assertIn('output',result.stderr.lower())
        self.assertTrue((self.package/old.name).exists())

    def test_native_zero_exit_lint_errors_fail_before_or_after_build(self):
        for stage in ['recipe','archive']:
            with self.subTest(stage=stage):
                self.trace.unlink(missing_ok=True)
                result=self.run_command('--chroot',self.chroot,env={'LINT_ERROR':stage})
                self.assertNotEqual(result.returncode,0)
                self.assertIn('bad',result.stderr)
                self.assertFalse(self.receipt.exists())
                self.assertEqual(any(call[0]=='makechrootpkg' for call in self.calls()),stage=='archive')

    def test_verification_failure_never_starts_chroot(self):
        result=self.run_command('--chroot',self.chroot,env={'VERIFY_STATUS':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(call[0]=='makechrootpkg' for call in self.calls()))
        self.assertFalse(self.receipt.exists())

    def test_archive_policy_identity_provenance_and_input_drift_fail_closed(self):
        cases=[{'FORBIDDEN_PATH':'1'}, {'OUTPUT_METADATA':'depend = libx11\n'},
               {'OUTPUT_VERSION':'2.0-1'}, {'OUTPUT_METADATA':'arch = aarch64\n'}, {'OUTPUT_NAME':'surprise'}, {'UNEXPECTED_OUTPUT':'1'},
               {'PROVENANCE_HASH':'0'*64}, {'MUTATE_ARCHIVE':'1'}, {'CHANGE_INPUT':'1'}, {'METADATA_DRIFT':'1'}, {'CHROOT_STATUS':'3'}]
        original=(self.package/'PKGBUILD').read_text()
        for env in cases:
            with self.subTest(env=env):
                (self.package/'PKGBUILD').write_text(original)
                self.receipt.write_text('{"status":"PASS"}')
                result=self.run_command('--chroot',self.chroot,env=env)
                self.assertNotEqual(result.returncode,0,result.stdout)
                self.assertFalse(self.receipt.exists())

    def test_buildinfo_absence_is_explicit_when_provenance_is_unavailable(self):
        result=self.run_command('--chroot',self.chroot,env={'NO_BUILDINFO':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        self.assertIsNone(receipt['artifacts'][0]['pkgbuild_sha256sum'])
        self.assertTrue(any('BUILDINFO' in warning for warning in receipt['warnings']))

    def test_recorded_recipe_version_identity_and_policy_must_match(self):
        original=(self.package/'PKGBUILD').read_text()
        for text in [original.replace('pkgver=1.0','pkgver=2.0'),original.replace('demo-waypure','other'),
                     original.replace('pkgver=1.0','pkgver=$(unknown)'),original+'depends=(libx11)\n', original+'depends=(other-waypure)\n']:
            with self.subTest(text=text):
                (self.package/'PKGBUILD').write_text(text)
                result=self.run_command('--plan','--chroot',self.chroot)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.calls(),[])

    def test_split_output_set_must_be_complete(self):
        self.package.joinpath('PKGBUILD').write_text("pkgbase=demo-waypure\npkgname=(demo-waypure demo-tools-waypure)\npkgver=1.0\npkgrel=1\narch=('any')\n")
        self.package.joinpath('.SRCINFO').write_text(self.srcinfo+'pkgname = demo-tools-waypure\n')
        result=self.run_command('--chroot',self.chroot,env={'INCOMPLETE_SPLIT':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.receipt.exists())

    def test_explicit_archives_are_identified_hashed_and_only_injected_into_chroot(self):
        archive=self.archive(extra='provides = helper-virtual=1.0-1\n')
        result=self.run_command('--chroot',self.chroot,'--install',archive)
        self.assertEqual(result.returncode,0,result.stderr)
        calls=self.calls()
        build=next(call for call in calls if call[0]=='makechrootpkg')
        self.assertEqual(build[build.index('-I')+1],str(archive))
        self.assertIn(['pacman','-Qp',str(archive)],calls)
        receipt=json.loads(self.receipt.read_text())
        self.assertEqual(receipt['install_archives'][0]['pkgname'],'helper')
        self.assertEqual(receipt['install_archives'][0]['sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
        self.assertFalse(any(call[0]=='pacman' and call[1]!='-Qp' for call in calls))

    def test_conflicting_injected_names_capabilities_and_versions_are_rejected(self):
        first=self.archive('helper',extra='provides = shared\n')
        second=self.archive('other',extra='provides = shared\n')
        duplicate=self.root/'another.pkg.tar'
        duplicate.write_bytes(first.read_bytes())
        for selected in [[first,second],[first,duplicate],[self.archive('demo-waypure')]]:
            with self.subTest(selected=selected):
                arguments=[value for path in selected for value in ['--install',path]]
                result=self.run_command('--plan','--chroot',self.chroot,*arguments)
                self.assertNotEqual(result.returncode,0)
        self.package.joinpath('PKGBUILD').write_text(self.package.joinpath('PKGBUILD').read_text()+'depends=(helper\'>=2.0\')\n')
        self.package.joinpath('.SRCINFO').write_text(self.srcinfo+'\tdepends = helper>=2.0\n')
        result=self.run_command('--plan','--chroot',self.chroot,'--install',first)
        self.assertNotEqual(result.returncode,0)

    def test_self_bootstrap_requires_opt_in_and_records_original_provenance(self):
        archive=self.archive('demo-waypure',buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        rejected=self.run_command('--plan','--chroot',self.chroot,'--install',archive)
        self.assertNotEqual(rejected.returncode,0)
        self.assertIn('conflicts with selected output',rejected.stderr)
        result=self.run_command('--plan','--json','--chroot',self.chroot,'--bootstrap-install',archive)
        self.assertEqual(result.returncode,0,result.stderr)
        plan=json.loads(result.stdout)
        item=plan['install_archives'][0]
        self.assertEqual(item['role'],'bootstrap')
        self.assertEqual(item['pkgbuild_sha256sum'],'a'*64)
        self.assertEqual(item['sha256'],hashlib.sha256(archive.read_bytes()).hexdigest())
        build=plan['packages'][0]
        self.assertEqual(build['build_command'][build['build_command'].index('-I')+1],str(archive))
        self.assertNotEqual(Path(build['output_directory']),archive.parent)
        self.assertIn('--force',build['build_command'])
        self.assertIn('--check',build['build_command'])
        self.assertFalse(self.receipt.exists())

    def test_bootstrap_is_input_only_and_cannot_replace_a_missing_fresh_build(self):
        archive=self.archive('demo-waypure',buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        result=self.run_command('--chroot',self.chroot,'--bootstrap-install',archive)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        self.assertEqual(receipt['install_archives'][0]['role'],'bootstrap')
        self.assertEqual(receipt['artifacts'][0]['pkgbuild_sha256sum'],hashlib.sha256((self.package/'PKGBUILD').read_bytes()).hexdigest())
        self.assertNotEqual(receipt['artifacts'][0]['sha256'],receipt['install_archives'][0]['sha256'])
        failed=self.run_command('--chroot',self.chroot,'--bootstrap-install',archive,env={'NO_OUTPUT':'1'})
        self.assertNotEqual(failed.returncode,0)
        self.assertIn('missing current build output',failed.stderr)
        self.assertFalse(self.receipt.exists())

    def test_bootstrap_requires_selected_output_and_valid_source_provenance(self):
        unrelated=self.archive(buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        result=self.run_command('--plan','--chroot',self.chroot,'--bootstrap-install',unrelated)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('must name a selected output',result.stderr)
        for buildinfo in [None,'pkgbuild_sha256sum = invalid\n',
                          'pkgbuild_sha256sum = '+'a'*64+'\npkgbuild_sha256sum = '+'b'*64+'\n']:
            with self.subTest(buildinfo=buildinfo):
                archive=self.archive('demo-waypure',buildinfo=buildinfo)
                result=self.run_command('--plan','--chroot',self.chroot,'--bootstrap-install',archive)
                self.assertNotEqual(result.returncode,0)
                self.assertIn('bootstrap .BUILDINFO',result.stderr)

    def test_bootstrap_does_not_bypass_policy_or_duplicate_input_checks(self):
        bad=self.archive('demo-waypure',extra='depend = libx11\n',buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        result=self.run_command('--plan','--chroot',self.chroot,'--bootstrap-install',bad)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('depends on libx11',result.stderr)
        archive=self.archive('demo-waypure',buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        result=self.run_command('--plan','--chroot',self.chroot,'--bootstrap-install',archive,'--bootstrap-install',archive)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('duplicate',result.stderr)

    def test_bootstrap_mutation_during_output_lint_prevents_pass(self):
        archive=self.archive('demo-waypure',buildinfo='pkgbuild_sha256sum = '+'a'*64+'\n')
        result=self.run_command('--chroot',self.chroot,'--bootstrap-install',archive,
                                env={'MUTATE_CONTEXT':str(archive)})
        self.assertNotEqual(result.returncode,0)
        self.assertIn('injected archive changed',result.stderr)
        self.assertFalse(self.receipt.exists())

    def test_recipe_validation_failure_also_invalidates_previous_receipt(self):
        self.receipt.write_text('{"status":"PASS"}')
        (self.package/'.SRCINFO').write_text(self.srcinfo.replace('1.0','2.0'))
        result=self.run_command('--chroot',self.chroot)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.receipt.exists())

    def test_unverified_sources_cannot_be_recorded_as_trusted(self):
        for checksum in ['SKIP', 'bad']:
            with self.subTest(checksum=checksum):
                (self.package/'.SRCINFO').write_text(self.srcinfo.replace('pkgname = demo-waypure',
                    'source = https://example.invalid/release.tar.gz\nsha256sums = '+checksum+'\npkgname = demo-waypure'))
                result=self.run_command('--plan','--chroot',self.chroot)
                self.assertNotEqual(result.returncode,0)
                self.assertEqual(self.calls(),[])

    def test_native_debug_package_is_audited_without_changing_build_options(self):
        result=self.run_command('--chroot',self.chroot,env={'DEBUG_OUTPUT':'1'})
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        debug=next(artifact for artifact in receipt['artifacts'] if artifact['pkgname']=='demo-waypure-debug')
        self.assertTrue(debug['auxiliary_debug'])
        result=self.run_command('--chroot',self.chroot,env={'DEBUG_OUTPUT':'1','BAD_DEBUG':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(self.receipt.exists())

    def test_prepared_language_source_evidence_is_recorded_without_resolving_cargo(self):
        source=self.package/'src/demo-1.0'
        source.mkdir(parents=True)
        for marker,content,backend in [
                ('pyproject.toml','[project]\nname="demo"\nversion="1.0"\ndependencies=[]\n[build-system]\nrequires=["setuptools"]\nbuild-backend="setuptools.build_meta"\n','python'),
                ('Cargo.toml','[package]\nname="demo"\nversion="1.0"\n[dependencies]\n','cargo')]:
            with self.subTest(backend=backend):
                for path in source.iterdir(): path.unlink()
                (source/marker).write_text(content)
                result=self.run_command('--chroot',self.chroot)
                self.assertEqual(result.returncode,0,result.stderr)
                receipt=json.loads(self.receipt.read_text())
                self.assertEqual(receipt['source_evidence']['backend'],backend)
                self.assertIn(marker,receipt['source_evidence']['evidence']['sources'])
                self.assertTrue(any('coverage' in warning.lower() or 'declarations' in warning.lower() for warning in receipt['warnings']))
                self.assertFalse(any(call[0]=='cargo' for call in self.calls()))

    def test_unavailable_prepared_source_is_reported_without_claiming_source_coverage(self):
        result=self.run_command('--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        receipt=json.loads(self.receipt.read_text())
        self.assertIsNone(receipt['source_evidence'])
        self.assertTrue(any('prepared source' in warning.lower() for warning in receipt['warnings']))

    @unittest.skipUnless(__import__('shutil').which('zstd'), 'native zstd needed')
    def test_archive_metadata_drains_large_valid_decoder_padding(self):
        archive=self.archive()
        with archive.open('ab') as stream: stream.write(b'\0'*(2*1024*1024))
        compressed=archive.with_name(archive.name+'.zst')
        with compressed.open('wb') as stream:
            subprocess.run(['zstd','-q','-c',str(archive)],stdout=stream,check=True)
        result=subprocess.run([sys.executable,'-c',
            'import sys; sys.path.insert(0,sys.argv[1]); from pathlib import Path; '
            'from optionkit.release_validation import archive_metadata; '
            'print(archive_metadata(Path(sys.argv[2]))[".PKGINFO"]["pkgname"][0])',
            str(TOOLS),str(compressed)],capture_output=True,text=True,env=self.env,timeout=5)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(result.stdout.strip(),'helper')

    def test_generated_metadata_preserves_split_dependency_scope(self):
        (self.package/'PKGBUILD').write_text("pkgbase=demo-waypure\npkgname=(demo-waypure demo-tools-waypure)\npkgver=1.0\npkgrel=1\narch=('any')\n")
        (self.package/'.SRCINFO').write_text(self.srcinfo+'\tdepends = shared\npkgname = demo-tools-waypure\n')
        result=self.run_command('--chroot',self.chroot,env={'METADATA_SCOPE_DRIFT':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertIn('metadata',result.stderr)
        self.assertFalse(any(call[0]=='makechrootpkg' for call in self.calls()))

    def test_host_metadata_cannot_create_or_replace_claimed_chroot_outputs(self):
        for env in ({'POSTFLIGHT_OUTPUT': '1', 'NO_OUTPUT': '1'}, {'POSTFLIGHT_OUTPUT': '1'}):
            with self.subTest(env=env):
                result = self.run_command('--chroot', self.chroot, env=env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())
                self.assertIn('output', result.stderr.lower())

    def test_later_archive_lint_cannot_mutate_previous_output_or_add_archive(self):
        for env in ({'DEBUG_OUTPUT': '1', 'MUTATE_PRIOR_ARCHIVE': '1'}, {'ADD_LINT_ARCHIVE': '1'}):
            with self.subTest(env=env):
                result = self.run_command('--chroot', self.chroot, env=env)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())
                self.assertIn('output', result.stderr.lower())

    def test_archive_lint_cannot_change_policy_baseline_or_injected_input(self):
        policy = self.root/'policy.toml'
        policy.write_bytes((TOOLS/'waypure-policy.toml').read_bytes())
        archive = self.archive()
        for path in (policy, self.chroot/'root/etc/pacman.conf', archive):
            with self.subTest(path=path):
                before = path.read_bytes()
                result = self.run_command('--chroot', self.chroot, '--policy', policy, '--install', archive,
                                          env={'MUTATE_CONTEXT': str(path)})
                path.write_bytes(before)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertFalse(self.receipt.exists())
                self.assertIn('changed', result.stderr)

    def test_package_outputs_from_preflight_cannot_be_attributed_to_chroot(self):
        result=self.run_command('--chroot',self.chroot,env={'PREFLIGHT_OUTPUT':'1','NO_OUTPUT':'1'})
        self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(call[0]=='makechrootpkg' for call in self.calls()))
        self.assertFalse(self.receipt.exists())

    def test_plan_hashes_declared_sources_with_log_or_package_archive_suffixes(self):
        sources = ['payload.log', 'upstream.pkg.tar.zst']
        checksums = []
        for name in sources:
            path = self.package/name
            path.write_text('upstream source '+name)
            checksums.append(hashlib.sha256(path.read_bytes()).hexdigest())
        with (self.package/'PKGBUILD').open('a') as stream:
            stream.write('source=('+ ' '.join(sources)+')\nsha256sums=('+ ' '.join(checksums)+')\n')
        with (self.package/'.SRCINFO').open('a') as stream:
            for source in sources: stream.write('\tsource = '+source+'\n')
            for checksum in checksums: stream.write('\tsha256sums = '+checksum+'\n')
        result = self.run_command('--chroot', self.chroot, '--plan', '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        inputs = json.loads(result.stdout)['packages'][0]['inputs']
        for name, checksum in zip(sources, checksums):
            self.assertEqual(inputs.get(name), checksum)

    @unittest.skipUnless(__import__('shutil').which('makepkg',path=os.defpath) and os.geteuid()!=0,
                         'native makepkg as an unprivileged user needed')
    def test_native_ci_fixture_fails_if_check_is_skipped(self):
        fixture=self.root/'native-fixture'
        shutil.copytree(TOOLS/'tests/fixtures/release-smoke/fixture',fixture)
        native=shutil.which('makepkg',path=os.defpath)
        config=self.root/'fixture-makepkg.conf'
        config.write_text(Path('/etc/makepkg.conf').read_text()+'\nBUILDDIR='+str(self.root/'native-build')+'\n')
        environment={**os.environ,'PATH':os.defpath,'PKGDEST':str(fixture),'SRCDEST':str(fixture),
                     'LOGDEST':str(fixture),'SRCPKGDEST':str(fixture)}
        command=[native,'--config',str(config),'--nodeps','--nosign','--force']
        skipped=subprocess.run([*command,'--nocheck'],cwd=fixture,env=environment,
                               text=True,capture_output=True,timeout=20)
        self.assertNotEqual(skipped.returncode,0,skipped.stdout+skipped.stderr)
        checked=subprocess.run([*command,'--cleanbuild','--check'],cwd=fixture,env=environment,
                               text=True,capture_output=True,timeout=20)
        self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)
        self.assertTrue(list(fixture.glob('*.pkg.tar*')))

    @unittest.skipUnless(__import__('shutil').which('makepkg',path=os.defpath) and os.geteuid()!=0,
                         'native makepkg as an unprivileged user needed')
    def test_real_makepkg_preflight_checks_local_source_integrity_without_building(self):
        payload=self.package/'payload.txt'
        payload.write_text('trusted local source\n')
        checksum=hashlib.sha256(payload.read_bytes()).hexdigest()
        recipe=("pkgname=demo-waypure\npkgver=1.0\npkgrel=1\npkgdesc='Release validation fixture'\n"
                "arch=('any')\nurl='https://example.invalid'\nlicense=('MIT')\n"
                "source=('payload.txt')\nsha256sums=('"+checksum+"')\n"
                "package() { install -Dm644 payload.txt \"$pkgdir/usr/share/demo/payload.txt\"; }\n")
        (self.package/'PKGBUILD').write_text(recipe)
        config=self.root/'makepkg.conf'
        config.write_text(Path('/etc/makepkg.conf').read_text()+
                          '\nBUILDDIR='+__import__('shlex').quote(str(self.root/'build'))+'\n')
        native=__import__('shutil').which('makepkg',path=os.defpath)
        generated=subprocess.run([native,'--config',str(config),'--printsrcinfo'],cwd=self.package,
                                 env={**self.env,'PKGDEST':str(self.root/'metadata-output')},
                                 text=True,capture_output=True,timeout=10)
        self.assertEqual(generated.returncode,0,generated.stderr)
        (self.package/'.SRCINFO').write_text(generated.stdout)
        self.executable('makepkg','import os, sys\nos.execv('+repr(native)+', ['+repr(native)+', "--config", '+repr(str(config))+', *sys.argv[1:]])\n')
        result=self.run_command('--chroot',self.chroot)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(self.receipt.exists())
        self.trace.unlink(missing_ok=True)
        payload.write_text('changed without matching checksum\n')
        result=self.run_command('--chroot',self.chroot)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('validity',result.stderr)
        self.assertFalse(self.receipt.exists())
        self.assertFalse(any(call[0]=='makechrootpkg' for call in self.calls()))
