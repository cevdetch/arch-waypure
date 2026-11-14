"""Personal chroot configurations evaluated by their native consumers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

TOOLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TOOLS))


class ChrootProfileCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.devtools = self.root/'devtools'
        configs = self.devtools/'makepkg.conf.d'
        (configs/'conf.d').mkdir(parents=True)
        (configs/'x86_64.conf').write_text('''CARCH="x86_64"
CFLAGS="-march=x86-64 -mtune=generic -O2 -pipe -fstack-clash-protection"
CXXFLAGS="$CFLAGS -Wp,-D_GLIBCXX_ASSERTIONS"
LDFLAGS="-Wl,-z,relro -Wl,-z,now"
LTOFLAGS="-flto=auto"
OPTIONS=(strip docs !debug !lto)
COMPRESSZST=(zstd -c -T0 --ultra -20 -)
''')
        (configs/'conf.d/rust.conf').write_text('RUSTFLAGS="-C force-frame-pointers=yes"\nDEBUG_RUSTFLAGS="-C debuginfo=2 -C strip=none"\n')
        (configs/'conf.d/fortran.conf').write_text('FFLAGS="-march=x86-64 -mtune=generic -O2 -pipe"\nFCFLAGS="$FFLAGS"\n')
        self.repositories = {name: ['https://mirror.invalid/'+name] for name in
                             ('cachyos-znver4','cachyos-core-znver4','cachyos-extra-znver4','cachyos','core','extra')}
        self.output = self.root/'profile with spaces'
        self.chroot = self.root/'baseline with spaces'

    def prepare(self):
        from optionkit.chroot_profile import prepare
        return prepare(self.output, self.chroot, devtools=self.devtools, repositories=self.repositories)

    def test_effective_native_flags_keep_hardening_and_enable_debug_and_lto(self):
        self.prepare()
        command = '''source "$1/makepkg.conf"
source "$1/makepkg.conf.d/rust.conf"
source "$1/makepkg.conf.d/fortran.conf"
printf '%s\\n' "$CARCH" "$CFLAGS" "$CXXFLAGS" "$LDFLAGS" "$RUSTFLAGS" "$FFLAGS" "$FCFLAGS" "${OPTIONS[*]}" "${COMPRESSZST[*]}"
'''
        result = subprocess.run(['bash','-eu','-c',command,'profile',str(self.output)],capture_output=True,text=True,check=True)
        arch,cflags,cxxflags,ldflags,rust,fflags,fcflags,options,compression = result.stdout.splitlines()
        self.assertEqual(arch,'x86_64')
        for value in (cflags,cxxflags,fflags,fcflags):
            self.assertIn('-march=native',value)
            self.assertIn('-O3',value)
            self.assertNotIn('-O2',value)
        self.assertIn('-fstack-clash-protection',cflags)
        self.assertIn('-D_GLIBCXX_ASSERTIONS',cxxflags)
        self.assertIn('-z,relro',ldflags)
        self.assertIn('target-cpu=native',rust)
        self.assertIn('opt-level=3',rust)
        self.assertIn('force-frame-pointers=yes',rust)
        self.assertEqual(options.split(),['strip','docs','debug','lto'])
        self.assertIn('--ultra -20',compression)

    @unittest.skipUnless(shutil.which('pacman-conf'), 'native pacman configuration parser needed')
    def test_pacman_consumer_accepts_order_architectures_and_required_signatures(self):
        self.prepare()
        config = self.output/'pacman.conf'
        def read(*args):
            return subprocess.check_output(['pacman-conf','--config',str(config),*args],text=True).splitlines()
        self.assertEqual(read('--repo-list'),list(self.repositories))
        self.assertIn('x86_64_v4',read('Architecture'))
        self.assertIn('PackageRequired',read('SigLevel'))
        self.assertIn('PackageTrustedOnly',read('SigLevel'))
        self.assertEqual(read('--repo','cachyos-znver4','Server'),['https://mirror.invalid/cachyos-znver4'])
        self.assertFalse(self.chroot.exists())

    def test_setup_commands_are_quoted_and_refuse_an_existing_baseline(self):
        self.prepare()
        setup = self.output/'setup.sh.log'
        subprocess.run(['bash','-n',str(setup)],check=True)
        (self.chroot/'root').mkdir(parents=True)
        result = subprocess.run(['bash',str(setup)],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIn('already exists',result.stderr)

    def test_setup_creates_missing_parents_before_native_root_path_resolution(self):
        self.prepare()
        binaries=self.root/'bin'
        binaries.mkdir()
        doas=binaries/'doas'
        doas.write_text('#!'+sys.executable+'''\nimport os, subprocess, sys
from pathlib import Path
if sys.argv[1] == '/usr/bin/mkdir':
    os.execv(sys.argv[1],sys.argv[1:])
elif sys.argv[1] == '/usr/bin/mkarchroot':
    root=Path(sys.argv[-4])
    # Installed mkarchroot resolves with readlink -f before mkdir -p.
    subprocess.run(['readlink','-f',str(root)],check=True)
    root.mkdir()
else:
    sys.exit(93)
''')
        doas.chmod(0o755)
        result=subprocess.run(['bash',str(self.output/'setup.sh.log')],capture_output=True,text=True,
                              env={**os.environ,'PATH':str(binaries)+os.pathsep+os.environ['PATH']})
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue((self.chroot/'root').is_dir())

    def test_configuration_and_existing_outputs_are_never_replaced(self):
        self.prepare()
        before = (self.output/'makepkg.conf').read_bytes()
        with self.assertRaises(FileExistsError):
            self.prepare()
        self.assertEqual((self.output/'makepkg.conf').read_bytes(),before)
        profile=json.loads((self.output/'build-profile.json').read_text())
        self.assertEqual(profile['target'],'native')
        self.assertEqual(profile['repositories'],list(self.repositories))


if __name__ == '__main__':
    unittest.main()
