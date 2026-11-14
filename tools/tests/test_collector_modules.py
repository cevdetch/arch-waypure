"""Native option collection works without executable entry point files."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


class CollectorModulesCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.library = self.root / 'library'
        shutil.copytree(TOOLS / 'optionkit', self.library / 'optionkit',
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        self.source = self.root / 'source'
        self.source.mkdir()

    def collect(self, backend):
        code = ('import json,sys; from pathlib import Path; '
                'from optionkit.adapters import capture; '
                'data=capture(Path(sys.argv[2]),sys.argv[1]); '
                'print(json.dumps({"backend":data["backend"],"version":data["version"],'
                '"options":list(data["options"])}))')
        result = subprocess.run([sys.executable, '-B', '-c', code, backend, str(self.source)],
                                cwd=self.root, capture_output=True, text=True, timeout=10,
                                env={**os.environ, 'PYTHONPATH': str(self.library)})
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def command(self, backend, *arguments):
        return subprocess.run([str(TOOLS / (backend + '-options')), *map(str, arguments)],
                              cwd=self.root, capture_output=True, text=True, timeout=10,
                              env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'})

    def qt_source(self):
        (self.source / '.cmake.conf').write_text('set(QT_REPO_MODULE_VERSION "6.12.0")\n')
        (self.source / 'CMakeLists.txt').write_text('project(QtDemo VERSION 6.12.0)\n')
        (self.source / 'configure.cmake').write_text('qt_feature(root LABEL "Root" CONDITION ON)\n')

    def ffmpeg_source(self):
        (self.source / 'RELEASE').write_text('9.0.2\n')
        configure = self.source / 'configure'
        configure.write_text('#!' + sys.executable + '\nimport sys\n'
                             'if sys.argv[1]=="--help":\n'
                             ' print("External libraries:\\n  --enable-demo enable demo [yes]\\n  --list-decoders show decoders")\n'
                             'elif sys.argv[1]=="--list-decoders": print("h264 hevc")\n'
                             'else: sys.exit(2)\n')
        configure.chmod(0o755)

    def test_qt_library_collects_without_command_scripts(self):
        self.qt_source()
        self.assertEqual(self.collect('qt6'),
                         {'backend': 'qt6', 'version': '6.12.0', 'options': ['FEATURE_root']})

    def test_ffmpeg_library_collects_without_command_scripts(self):
        self.ffmpeg_source()
        self.assertEqual(self.collect('ffmpeg'),
                         {'backend': 'ffmpeg', 'version': '9.0.2',
                          'options': ['feature:demo', 'option:list-decoders']})
        self.assertFalse((self.source / 'ffbuild').exists())

    def test_cmake_library_collects_without_qt_command_script(self):
        (self.source / 'CMakeLists.txt').write_text(
            'project(Demo VERSION 1.0)\noption(WITH_DEMO "Demo" ON)\n')
        self.assertEqual(self.collect('cmake'),
                         {'backend': 'cmake', 'version': '1.0', 'options': ['WITH_DEMO']})

    def test_qt_cli_repeats_snapshot_and_preserves_selected_render_check(self):
        self.qt_source()
        snapshot = self.root / 'qt.json.log'
        recipe = self.root / 'PKGBUILD'
        recipe.write_text('_cmake_options=(\n -DFEATURE_root=OFF\n)\n')
        for _ in range(2):
            result = self.command('qt6', 'snapshot', self.source, '-o', snapshot)
            self.assertEqual(result.returncode, 0, result.stderr)
        rendered = self.command('qt6', 'render', snapshot, '--pkgbuild', recipe, '--entries-only', '--all')
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn('-DFEATURE_root=OFF', rendered.stdout)
        self.assertNotIn('_cmake_options=(', rendered.stdout)
        checked = self.command('qt6', 'check', snapshot, '--pkgbuild', recipe)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_ffmpeg_cli_preserves_baseline_and_positional_check(self):
        self.ffmpeg_source()
        snapshot = self.root / 'ffmpeg.json.log'
        recipe = self.root / 'PKGBUILD'
        recipe.write_text('_make_options=(\n --enable-demo\n)\n')
        result = self.command('ffmpeg', 'snapshot', self.source, '-o', snapshot)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = snapshot.read_bytes()
        repeated = self.command('ffmpeg', 'snapshot', self.source, '-o', snapshot)
        self.assertEqual(repeated.returncode, 1)
        self.assertEqual(snapshot.read_bytes(), before)
        rendered = self.command('ffmpeg', 'render', snapshot, '--pkgbuild', recipe, '--entries-only', '--features-only')
        self.assertEqual(rendered.returncode, 0, rendered.stderr)
        self.assertIn('--enable-demo', rendered.stdout)
        self.assertNotIn('--list-decoders', rendered.stdout)
        checked = self.command('ffmpeg', 'check', snapshot, recipe)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)

    def test_specialized_help_describes_available_actions(self):
        for backend, actions in (('qt6', ('render', 'check', 'explain')), ('ffmpeg', ('render', 'check'))):
            with self.subTest(backend=backend):
                result = self.command(backend, '--help')
                self.assertEqual(result.returncode, 0, result.stderr)
                for action in actions:
                    self.assertRegex(result.stdout, r'(?m)^\s+' + action + r'\s+\S')
