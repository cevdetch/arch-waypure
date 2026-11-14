"""Public commands and installation behavior with isolated native boundaries."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


class CommandsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root/'bin'
        self.bin.mkdir()
        self.trace = self.root/'native.json.log'
        self.env = {**os.environ, 'PATH': str(self.bin)+os.pathsep+os.environ['PATH'],
                    'TRACE': str(self.trace), 'WAYPURE_DIR': str(self.root/'packages'),
                    'WAYPURE_ALPM_BACKEND': 'cli',
                    'PYTHONDONTWRITEBYTECODE': '1'}
        self.executable('pacman', '''
import json, os, sys
with open(os.environ['TRACE'], 'a') as output:
    output.write(json.dumps(sys.argv[1:])+'\\n')
args=sys.argv[1:]
if '--config' in args:
    index=args.index('--config')
    del args[index:index+2]
if args == ['-Qq']:
    print(os.environ.get('INSTALLED', ''))
    sys.exit(int(os.environ.get('QUERY_STATUS', '0')))
if args == ['-Sl']:
    print('extra same 1.0-1\\nextra different 2.0-1')
elif args == ['-Q']:
    if 'INSTALLED' in os.environ:
        lines=[name+' 1.0-1' for name in os.environ['INSTALLED'].splitlines() if name]
        if lines: print('\\n'.join(lines))
    else:
        print('same-waypure 1.0-1\\ndifferent-waypure 1.0-1')
    sys.exit(int(os.environ.get('QUERY_STATUS','0')))
elif args and args[0] == '-Rns':
    sys.exit(int(os.environ.get('REMOVE_STATUS', '0')))
else:
    sys.exit(99)
''')
        self.executable('sudo', 'import os, sys\nos.execvp(sys.argv[1], sys.argv[1:])\n')
        self.executable('vercmp', "import sys\nprint(0 if sys.argv[1]==sys.argv[2] else -1)\n")

    def executable(self, name, body):
        path = self.bin/name
        path.write_text('#!'+sys.executable+'\n'+body)
        path.chmod(0o755)

    def calls(self):
        return [json.loads(line) for line in self.trace.read_text().splitlines()] if self.trace.exists() else []

    def run_command(self, name, *args, env=None, directory=TOOLS):
        path = directory/name
        self.assertTrue(path.is_file(), 'missing command: '+str(path))
        return subprocess.run([str(path), *map(str,args)], cwd=self.root, text=True,
                              capture_output=True, env={**self.env, **(env or {})}, timeout=10)

    def test_canonical_help_does_not_query_or_remove_packages(self):
        for name in ['waypure-options','waypure-audit','waypure-build','waypure-versions','waypure-remove-x11','waypure-validate','waypure-chroot','waypure-audit-audio','waypure-audit-graphics','waypure-audit-x11']:
            with self.subTest(command=name):
                result=self.run_command(name,'--help')
                self.assertEqual(result.returncode,0,result.stderr)
                self.assertIn('usage',result.stdout.lower())
        self.assertEqual(self.calls(),[])

    def test_versions_diff_keeps_mismatches_and_redirected_output_has_no_ansi(self):
        result=self.run_command('waypure-versions','--diff')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('different',result.stdout)
        self.assertNotIn('same ',result.stdout)
        self.assertNotIn('\x1b[',result.stdout)

    def test_versions_rejects_unknown_arguments_without_native_calls(self):
        result=self.run_command('waypure-versions','--typo')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertEqual(self.calls(),[])

    def test_versions_native_query_failure_cannot_look_like_an_empty_report(self):
        self.executable('pacman', "import sys\nprint('synthetic package database failure',file=sys.stderr)\nsys.exit(2)\n")
        result=self.run_command('waypure-versions')
        self.assertNotEqual(result.returncode,0)
        self.assertIn('synthetic package database failure',result.stderr)

    def test_cleanup_defaults_to_preview_and_uses_reviewed_candidates(self):
        result=self.run_command('waypure-remove-x11',env={'INSTALLED':'libx11\nlibxau\nxorg-xwayland\nxcb-util-wm\nlibxkbcommon\nlibxml2\nlibxcrypt\npipewire\nlibx11-waypure'})
        self.assertEqual(result.returncode,0,result.stderr)
        candidates={line.strip() for line in result.stdout.splitlines() if line.startswith('  ')}
        self.assertEqual(candidates,{'libx11','libxau','xorg-xwayland','xcb-util-wm'})
        self.assertEqual(self.calls(),[['-Q']])

    def test_cleanup_apply_removes_only_selected_installed_packages_with_confirmation(self):
        result=self.run_command('waypure-remove-x11','--apply',env={'INSTALLED':'libx11\nxcb-util-wm\nlibxkbcommon'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls(),[['-Q'],['-Rns','--','libx11','xcb-util-wm']])

    def test_cleanup_query_and_apply_share_configuration_with_and_without_sudo(self):
        config=self.root/'alternate pacman.conf'
        fixture=self.root/'cleanup fixture'
        fixture.mkdir()
        for name in ('optionkit','waypure-policy.toml'):
            (fixture/name).symlink_to(TOOLS/name)
        sudo_trace=self.root/'sudo.json.log'
        self.executable('sudo','import json, os, sys\nfrom pathlib import Path\nPath('+repr(str(sudo_trace))+').write_text(json.dumps(sys.argv[1:]))\nos.execvp(sys.argv[1], sys.argv[1:])\n')
        source=(TOOLS/'waypure-remove-x11').read_text()
        self.assertEqual(source.count('EUID'),1)
        for effective_uid in (0,1000):
            with self.subTest(effective_uid=effective_uid):
                # Control only Bash's readonly identity variable in this copied fixture.
                script=fixture/'waypure-remove-x11'
                script.write_text(source.replace('EUID',str(effective_uid)))
                script.chmod(0o755)
                self.trace.unlink(missing_ok=True)
                sudo_trace.unlink(missing_ok=True)
                result=self.run_command('waypure-remove-x11','--apply',directory=fixture,
                                        env={'INSTALLED':'libx11','WAYPURE_PACMAN_CONFIG':str(config)})
                self.assertEqual(result.returncode,0,result.stderr)
                arguments=['-Rns','--config',str(config),'--','libx11']
                self.assertEqual(self.calls(),[['-Q','--config',str(config)],arguments])
                self.assertEqual(sudo_trace.exists(),effective_uid!=0)
                if sudo_trace.exists():
                    self.assertEqual(json.loads(sudo_trace.read_text()),['pacman',*arguments])

    def test_cleanup_empty_selection_does_not_invoke_removal(self):
        result=self.run_command('waypure-remove-x11','--apply',env={'INSTALLED':'libxkbcommon\npipewire'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls(),[['-Q']])

    def test_cleanup_ignores_audio_policy_and_generic_wayland_helpers(self):
        result=self.run_command('waypure-remove-x11',env={
            'INSTALLED':'libxau\nlibxcvt\nlibxkbcommon\njack2\npipewire-jack\npipewire-netjack2\npulseaudio\nlibpulse\nalsa-lib'})
        self.assertEqual(result.returncode,0,result.stderr)
        candidates={line.strip() for line in result.stdout.splitlines() if line.startswith('  ')}
        self.assertEqual(candidates,{'libxau'})
        self.assertEqual(self.calls(),[['-Q']])

    def test_cleanup_matches_multilib_x11_names_and_preserves_replacement_exclusion(self):
        result=self.run_command('waypure-remove-x11',env={
            'INSTALLED':'lib32-libx11\nlib32-xcb-util\nlib32-libx11-waypure\nlib32-libxcvt\nlib32-pipewire-jack'})
        self.assertEqual(result.returncode,0,result.stderr)
        candidates={line.strip() for line in result.stdout.splitlines() if line.startswith('  ')}
        self.assertEqual(candidates,{'lib32-libx11','lib32-xcb-util'})
        self.assertEqual(self.calls(),[['-Q']])

    def test_cleanup_query_errors_are_not_reported_as_an_empty_selection(self):
        result=self.run_command('waypure-remove-x11','--apply',env={'QUERY_STATUS':'2'})
        self.assertNotEqual(result.returncode,0)
        self.assertTrue(result.stderr.strip())
        self.assertEqual(self.calls(),[['-Q']])

    def test_cleanup_propagates_removal_failure(self):
        result=self.run_command('waypure-remove-x11','--apply',env={'INSTALLED':'libx11','REMOVE_STATUS':'7'})
        self.assertEqual(result.returncode,7,result.stderr)

    def test_cleanup_reads_custom_policy_and_deduplicates_overlapping_patterns(self):
        policy=self.root/'custom policy.toml'
        policy.write_text('[dependencies]\nforbidden=["custom-*", "custom-one"]\n[cleanup]\nextra=["extra-package"]\n')
        result=self.run_command('waypure-remove-x11','--policy',policy,'--apply',env={'INSTALLED':'custom-one\nextra-package\nlibx11\ncustom-one-waypure'})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(self.calls(),[['-Q'],['-Rns','--','custom-one','extra-package']])

    def test_cleanup_invalid_arguments_and_policy_never_reach_removal(self):
        policy=self.root/'bad.toml'
        for contents in ['bad = [', '[dependencies]\nforbidden="libx11"', '[dependencies]\nforbidden=["libx11"]\n[cleanup]\nextra=[3]']:
            with self.subTest(contents=contents):
                policy.write_text(contents)
                result=self.run_command('waypure-remove-x11','--policy',policy,'--apply',env={'INSTALLED':'libx11'})
                self.assertEqual(result.returncode,2,result.stderr)
        result=self.run_command('waypure-remove-x11','--typo')
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertFalse(any(call[0]=='-Rns' for call in self.calls()))

    def test_installer_preserves_existing_regular_scripts(self):
        prefix=self.root/'prefix with spaces'
        (prefix/'bin').mkdir(parents=True)
        original=prefix/'bin/waypure-remove-x11'
        original.write_text('# Original personal cleanup script\n')
        result=subprocess.run([sys.executable,str(TOOLS/'install'),'--prefix',str(prefix)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        backups=list((prefix/'lib/arch-waypure/backups').glob('waypure-remove-x11.*'))
        self.assertEqual([path.read_text() for path in backups],['# Original personal cleanup script\n'])
        for name in ['waypure-options','waypure-versions','waypure-remove-x11','waypure-validate','waypure-audit-audio','waypure-audit-graphics','waypure-audit-x11']:
            checked=self.run_command(name,'--help',directory=prefix/'bin')
            self.assertEqual(checked.returncode,0,checked.stderr)
        repeated=subprocess.run([sys.executable,str(TOOLS/'install'),'--prefix',str(prefix)],capture_output=True,text=True)
        self.assertEqual(repeated.returncode,0,repeated.stderr)
        self.assertEqual(len(list((prefix/'lib/arch-waypure/backups').iterdir())),1)

    def test_installer_removes_retired_owned_commands_and_old_library_copies(self):
        prefix=self.root/'prefix'
        library=prefix/'lib/arch-waypure'
        library.mkdir(parents=True)
        (prefix/'bin').mkdir()
        retired={'build-options':'waypure-options','omg-better-clean.sh':'waypure-versions',
                 'remove-x11.sh':'waypure-remove-x11'}
        for index,(name,canonical) in enumerate(retired.items()):
            (library/name).write_text('# Obsolete managed command\n')
            target=canonical if index%2 else name
            (prefix/'bin'/name).symlink_to('../lib/arch-waypure/'+target)
        for _ in range(2):
            result=subprocess.run([sys.executable,str(TOOLS/'install'),'--prefix',str(prefix)],capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            for name,canonical in retired.items():
                self.assertFalse((prefix/'bin'/name).is_symlink())
                self.assertFalse((prefix/'bin'/name).exists())
                self.assertFalse((library/name).exists())
                self.assertTrue((prefix/'bin'/canonical).is_file())

    def test_installer_preserves_unmanaged_paths_with_retired_names(self):
        prefix=self.root/'prefix'
        (prefix/'bin').mkdir(parents=True)
        original=prefix/'bin/build-options'
        original.write_text('# Unrelated personal command\n')
        external=self.root/'personal-versions'
        external.write_text('# Unrelated version helper\n')
        (prefix/'bin/omg-better-clean.sh').symlink_to(external)
        (prefix/'bin/remove-x11.sh').mkdir()
        result=subprocess.run([sys.executable,str(TOOLS/'install'),'--prefix',str(prefix)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(original.read_text(),'# Unrelated personal command\n')
        self.assertEqual((prefix/'bin/omg-better-clean.sh').resolve(),external)
        self.assertEqual(external.read_text(),'# Unrelated version helper\n')
        self.assertTrue((prefix/'bin/remove-x11.sh').is_dir())

    def test_installer_directory_collision_leaves_other_commands_untouched(self):
        prefix=self.root/'prefix'
        (prefix/'bin/waypure-build').mkdir(parents=True)
        previous=prefix/'bin/waypure-options'
        previous.write_text('original command\n')
        result=subprocess.run([sys.executable,str(TOOLS/'install'),'--prefix',str(prefix)],capture_output=True,text=True)
        self.assertEqual(result.returncode,2,result.stderr)
        self.assertFalse(previous.is_symlink())
        self.assertEqual(previous.read_text(),'original command\n')
