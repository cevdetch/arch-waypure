"""Maintenance commands against real recipes, Git, source tarballs and ELF files."""

import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
import textwrap


TOOLS = Path(__file__).resolve().parents[1]


class MaintenanceCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        self.package = self.make_package("demo")

    def make_package(self, name, dependencies=()):
        package = self.repo / name
        source = package / "src" / (name + "-1.0")
        source.mkdir(parents=True)
        (source / "CMakeLists.txt").write_text('cmake_minimum_required(VERSION 3.22)\nproject(Demo VERSION 1.0 LANGUAGES NONE)\noption(WITH_X11 "X11" ON)\n')
        (package / "PKGBUILD").write_text('pkgver=1.0\npkgrel=1\nbuild() {\n local _cmake_options=(\n -DWITH_X11=OFF # WAYLAND\n )\n}\n')
        (package / ".SRCINFO").write_text(
            f"pkgbase = {name}-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tarch = x86_64\n"
            + "\tsha256sums = " + "a" * 64 + "\n"
            + "\tsource = https://example.invalid/demo-1.0.tar.gz\n"
            + f"pkgname = {name}-waypure\n\tprovides = {name}\n\tconflicts = {name}\n"
            + "".join("\tdepends = " + dependency + "\n" for dependency in dependencies))
        return package

    def run_tool(self, command, *arguments, ok=True, cwd=None, env=None):
        executable = TOOLS / command
        self.assertTrue(executable.is_file(), f"missing maintenance command: {command}")
        result = subprocess.run([str(executable), *map(str, arguments)], cwd=cwd or self.package,
                                text=True, capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", **(env or {})})
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def audit(self, *arguments, ok=True, env=None):
        result = self.run_tool("waypure-audit", "--repo", self.repo, "--json", *arguments, ok=ok, env=env)
        self.assertTrue(result.stdout.lstrip().startswith('{'), result.stdout + result.stderr)
        return result, json.loads(result.stdout)

    def arch_repository(self):
        upstream = self.root / "arch"
        upstream.mkdir()
        (upstream / "PKGBUILD").write_text("pkgver=1.0\npkgrel=1\n")
        (upstream / ".SRCINFO").write_text("pkgbase = demo\n\tpkgver = 1.0\n\tpkgrel = 1\npkgname = demo\n")
        for args in (("init", "-q"), ("add", "."), ("commit", "-qm", "Initial packaging")):
            subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                            "-c", "user.name=Test", "-c", "user.email=test@example.invalid", *args], cwd=upstream, check=True)
        return upstream

    def test_arch_review_discovers_pkgbase_without_a_profile_file(self):
        upstream = self.arch_repository()
        result = self.run_tool("waypure-options", "review", "--arch-local", upstream)
        self.assertIn("Arch snapshot", result.stdout)
        self.assertFalse((self.package / "build-options.toml").exists())

    @unittest.skipUnless(shutil.which("makepkg"), "makepkg needed for release preparation")
    def test_prepare_extracts_verified_source_without_building_or_running_prepare(self):
        source = self.package / "src/demo-1.0"
        archive = self.package / "demo-1.0.tar.gz"
        with tarfile.open(archive, "w:gz") as stream:
            stream.add(source, arcname="demo-1.0")
        shutil.rmtree(source)
        recipe = self.package / "PKGBUILD"
        sentinel = self.root / "must-not-build"
        recipe.write_text(
            f"pkgname=demo\npkgver=1.0\npkgrel=1\narch=(any)\nsource=('{archive}')\n"
            + f"sha256sums=('{hashlib.sha256(archive.read_bytes()).hexdigest()}')\n"
            + f"prepare() {{ touch '{sentinel}'; }}\n"
            + "package() { :; }\n"
            + recipe.read_text().replace("build() {", f"build() {{\n touch '{sentinel}'"))
        before = recipe.read_bytes()
        self.run_tool("waypure-options", "review", "--prepare")
        self.assertTrue((source / "CMakeLists.txt").exists())
        self.assertFalse(sentinel.exists())
        self.assertEqual(recipe.read_bytes(), before)

    def test_recipe_labels_survive_render_copy_without_profiles(self):
        self.run_tool("waypure-options", "review")
        first = self.run_tool("waypure-options", "render", "1.0").stdout
        self.assertIn("WAYLAND [SELECTED]", first)
        (self.package / "PKGBUILD").write_text("pkgver=1.0\npkgrel=1\nbuild() {\n" + first + "\n}\n")
        second = self.run_tool("waypure-options", "render", "1.0").stdout
        self.assertEqual(first, second)

    def test_selected_deprecation_warns_normally_and_fails_strict_review(self):
        path = self.package / "src/demo-1.0/CMakeLists.txt"
        path.write_text(path.read_text().replace('"X11"', '"Deprecated: use WITH_NEW"'))
        ordinary = self.run_tool("waypure-options", "review")
        self.assertIn("deprecated", ordinary.stdout.lower())
        strict = self.run_tool("waypure-options", "review", "--strict", ok=False)
        self.assertNotEqual(strict.returncode, 0)
        self.assertIn("deprecated", strict.stdout.lower())

    def test_deprecated_option_with_computed_value_still_fails_strict_check(self):
        path = self.package / "src/demo-1.0/CMakeLists.txt"
        path.write_text(path.read_text().replace('"X11"', '"Deprecated: use WITH_NEW"'))
        recipe = self.package / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("WITH_X11=OFF", "WITH_X11=${computed_value}"))
        self.run_tool("waypure-options", "review")
        result = self.run_tool("waypure-options", "check", "1.0", "--strict", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deprecated", result.stdout.lower())

    def test_cmake_deprecation_messages_before_declarations_are_retained(self):
        source = self.package / "src/demo-1.0"
        directory = source / "cmake"
        directory.mkdir()
        (directory / "compat.cmake").write_text('message(DEPRECATION "WITH_X11 is deprecated")\n')
        self.run_tool("waypure-options", "review")
        data = json.loads((self.package / "options/1.0.catalog.json.log").read_text())
        self.assertTrue(data["options"]["WITH_X11"]["deprecated"])
        self.assertIn("cmake/compat.cmake", data["sources"])

    def test_cmake_deprecation_before_option_in_same_file_is_detected(self):
        path = self.package / "src/demo-1.0/CMakeLists.txt"
        path.write_text('message(DEPRECATION "WITH_X11 is deprecated")\n' + path.read_text())
        result = self.run_tool("waypure-options", "review", "--strict", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("deprecated", result.stdout.lower())

    def test_ffmpeg_explicit_deprecation_reaches_selected_option_check(self):
        source = self.package / "src/demo-1.0"
        (source / "RELEASE").write_text("1.0\n")
        (source / "configure").write_text("#!/bin/sh\ncat <<'HELP'\nExternal libraries:\n  --enable-oldlib       [deprecated] use newlib\nHELP\n")
        (source / "configure").chmod(0o755)
        (self.package / "PKGBUILD").write_text("pkgver=1.0\npkgrel=1\nbuild() {\n local _make_options=(\n --enable-oldlib\n )\n}\n")
        result = self.run_tool("waypure-options", "review", "--strict", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("oldlib", result.stdout)
        self.assertIn("deprecated", result.stdout.lower())

    def test_qt_explicit_deprecation_reaches_selected_option_check(self):
        source = self.package / "src/demo-1.0"
        (source / ".cmake.conf").write_text('set(QT_REPO_MODULE_VERSION "1.0")\n')
        (source / "CMakeLists.txt").write_text('project(QtDemo)\n')
        (source / "configure.cmake").write_text('qt_feature(old LABEL "Deprecated: use new")\nqt_feature(deprecated_apis LABEL "Build deprecated APIs")\n')
        (self.package / "PKGBUILD").write_text("pkgver=1.0\npkgrel=1\nbuild() {\n local _cmake_options=(\n -DFEATURE_old=ON\n -DFEATURE_deprecated_apis=ON\n )\n}\n")
        self.run_tool("waypure-options", "review")
        data = json.loads((self.package / "options/1.0.catalog.json.log").read_text())
        self.assertTrue(data["options"]["FEATURE_old"]["deprecated"])
        self.assertFalse(data["options"]["FEATURE_deprecated_apis"]["deprecated"])
        result = self.run_tool("waypure-options", "check", "1.0", "--strict", ok=False)
        self.assertNotEqual(result.returncode, 0)

    def test_collector_upgrade_preserves_baseline_and_selects_new_variant(self):
        self.run_tool("waypure-options", "review")
        baseline = self.package / "options/1.0.catalog.json.log"
        previous = json.loads(baseline.read_text())
        previous["notes"].append("Older collector's evidence")
        baseline.write_text(json.dumps(previous))
        before = baseline.read_bytes()
        self.run_tool("waypure-options", "review")
        self.assertEqual(baseline.read_bytes(), before)
        self.assertEqual(len(list((self.package / "options").glob("1.0.*.catalog.json.log"))), 1)
        result = self.run_tool("waypure-options", "check", "1.0")
        self.assertNotIn("Older collector's evidence", result.stdout)

    def test_previous_release_comparison_uses_its_selected_variant(self):
        path = self.package / "src/demo-1.0/CMakeLists.txt"
        original = path.read_text()
        for label in ("ORIGINAL", "FIRST", "SECOND", "FIRST"):
            path.write_text(original.replace('"X11"', '"' + label + '"'))
            self.run_tool("waypure-options", "review")
        source = self.package / "src/demo-2.0"
        shutil.copytree(path.parent, source)
        (source / "CMakeLists.txt").write_text(path.read_text().replace("VERSION 1.0", "VERSION 2.0"))
        recipe = self.package / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=2.0"))
        result = self.run_tool("waypure-options", "review")
        self.assertNotIn("~ WITH_X11", result.stdout)

    def test_audit_reads_metadata_and_options_without_executing_recipe(self):
        self.run_tool("waypure-options", "review")
        recipe = self.package / "PKGBUILD"
        sentinel = self.root / "must-not-execute"
        recipe.write_text(f"touch '{sentinel}'\n" + recipe.read_text())
        before = (self.package / ".SRCINFO").read_bytes()
        _, report = self.audit("demo")
        row = report["packages"][0]
        self.assertEqual(row["checks"]["recipe"], "PASS")
        self.assertEqual(row["checks"]["options"], "PASS")
        self.assertFalse(sentinel.exists())
        self.assertEqual((self.package / ".SRCINFO").read_bytes(), before)

    def test_audit_rejects_cross_package_pins_but_accepts_versioned_siblings(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = other-waypure\n\tdepends = demo-helper-waypure=1.0-1\npkgname = demo-helper-waypure\n\tprovides = demo-helper\n\tconflicts = demo-helper\n")
        result, report = self.audit("demo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        errors = [finding for finding in report["packages"][0]["findings"] if finding["severity"] == "error"]
        pins = [finding["message"] for finding in errors if finding["code"] == "dependency.pin"]
        self.assertEqual(len(pins), 1)
        self.assertIn("other-waypure", pins[0])

    def test_audit_shows_transitive_local_x11_dependency_path(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = middle\n")
        self.make_package("middle", ("libx11",))
        result, report = self.audit("demo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        messages = [finding["message"] for finding in report["packages"][0]["findings"]]
        self.assertTrue(any("demo -> middle -> libx11" in message for message in messages), messages)

    def test_audit_labels_jack_dependencies_as_pipewire_policy_violations(self):
        metadata = self.package / '.SRCINFO'
        metadata.write_text(metadata.read_text() + '\tdepends = lib:libjack.so.0\n')
        recipe = self.package / 'PKGBUILD'
        recipe.write_text('makedepends=(pipewire-jack)\n' + recipe.read_text())
        _, report = self.audit('demo', '--metadata-only', ok=False)
        findings = report['packages'][0]['findings']
        messages = [item['message'] for item in findings if item['code'] == 'dependency.pipewire']
        self.assertTrue(any('lib:libjack.so.0' in message for message in messages), findings)
        self.assertTrue(any('pipewire-jack' in message for message in messages), findings)
        self.assertNotIn('dependency.x11', [item['code'] for item in findings])

    def test_audit_reports_transitive_netjack_dependency(self):
        metadata = self.package / '.SRCINFO'
        metadata.write_text(metadata.read_text() + '\tdepends = middle\n')
        self.make_package('middle', ('pipewire-netjack2',))
        _, report = self.audit('demo', '--metadata-only', ok=False)
        findings = report['packages'][0]['findings']
        self.assertTrue(any(item['code'] == 'dependency.path' and
                            'PIPEWIRE: demo -> middle -> pipewire-netjack2' in item['message']
                            for item in findings), findings)

    def test_metadata_policy_checks_outputs_and_provides_without_banning_source_base(self):
        metadata = self.package / '.SRCINFO'
        metadata.write_text(metadata.read_text().replace('pkgbase = demo-waypure', 'pkgbase = pulseaudio')
                            + '\tprovides_x86_64 = jack=1.9\n'
                              '\tprovides = lib:libX11.so.6\n'
                              'pkgname = pipewire-jack\n')
        result, report = self.audit('demo', '--metadata-only', ok=False)
        self.assertEqual(result.returncode, 1)
        findings = report['packages'][0]['findings']
        for kind, value in (('pipewire', 'jack=1.9'), ('x11', 'lib:libX11.so.6'), ('pipewire', 'pipewire-jack')):
            self.assertTrue(any(item['code'] == 'dependency.' + kind and value in item['message']
                                for item in findings), findings)
        self.assertFalse(any(item['code'] == 'dependency.pipewire' and 'pulseaudio' in item['message']
                             for item in findings), findings)

    def test_literal_recipe_capabilities_are_checked_before_srcinfo_refresh(self):
        recipe = self.package / 'PKGBUILD'
        recipe.write_text("pkgname=pipewire-jack\nprovides=('jack=1.9' 'lib:libX11.so.6')\n" + recipe.read_text())
        _, report = self.audit('demo', '--metadata-only', ok=False)
        findings = report['packages'][0]['findings']
        for value in ('pipewire-jack', 'jack=1.9', 'lib:libX11.so.6'):
            self.assertTrue(any(item['code'].startswith('dependency.') and value in item['message']
                                for item in findings), findings)

    def test_prohibited_optdepends_warn_and_keep_descriptions_and_soname_namespaces(self):
        optional = ('jack>=1.9: optional JACK output', 'lib:libX11.so.6: optional display')
        recipe = self.package / 'PKGBUILD'
        recipe.write_text('optdepends_x86_64=(' + ' '.join(repr(item) for item in optional) + ')\n' + recipe.read_text())
        metadata = self.package / '.SRCINFO'
        metadata.write_text(metadata.read_text() + ''.join('\toptdepends_x86_64 = ' + item + '\n' for item in optional)
                            + '\toptdepends_aarch64 = libxcb: other architecture\n'
                              '\toptdepends = libpulse: can talk about JACK without providing it\n')
        result, report = self.audit('demo', '--metadata-only')
        self.assertEqual(result.returncode, 0)
        findings = report['packages'][0]['findings']
        policy = [item for item in findings if item['code'].startswith('dependency.')]
        self.assertTrue(policy, findings)
        self.assertTrue(all(item['severity'] == 'warning' for item in policy), policy)
        for value in optional:
            self.assertTrue(any(value in item['message'] for item in policy), policy)
        self.assertFalse(any('libxcb' in item['message'] or 'libpulse' in item['message'] for item in policy), policy)
        self.assertFalse(any(item['code'] == 'dependency.path' for item in policy), policy)

    def test_archive_metadata_checks_capabilities_and_warns_for_optional_suggestions(self):
        from_metadata = (b'pkgname = demo-waypure\npkgbase = pulseaudio\npkgver = 1.0-1\n'
                         b'provides = jack=1.9\nprovides = lib:libX11.so.6\n'
                         b'optdepend = libjack.so=0-64: optional JACK\n')
        self.make_archive({'.PKGINFO': from_metadata})
        result, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertEqual(result.returncode, 1)
        findings = report['packages'][0]['findings']
        for kind, value in (('pipewire', 'jack=1.9'), ('x11', 'lib:libX11.so.6')):
            self.assertTrue(any(item['code'] == 'artifact.' + kind and item['severity'] == 'error'
                                and value in item['message'] for item in findings), findings)
        self.assertTrue(any(item['severity'] == 'warning' and 'libjack.so=0-64: optional JACK' in item['message']
                            for item in findings), findings)
        self.assertFalse(any(item['code'] == 'artifact.pipewire' and 'pulseaudio' in item['message'] for item in findings))

    def test_archive_policy_checks_actual_output_name(self):
        self.make_archive({'.PKGINFO': b'pkgname = pipewire-jack\npkgver = 1.0-1\n'})
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertTrue(any(item['code'] == 'artifact.pipewire' and 'pipewire-jack' in item['message']
                            for item in report['packages'][0]['findings']))

    def test_archive_rejects_known_helpers_and_static_archives_without_banning_libtool(self):
        self.make_archive({'./usr/bin/pw-jack': b'#!/bin/sh\n', 'usr/bin/Xwayland': b'helper',
                           'usr/lib/libjack.a': b'!<arch>\n', 'usr/lib32/libX11.a': b'!<arch>\n',
                           'usr/lib/libxkbcommon.a': b'!<arch>\n', 'usr/lib/libEGL.a': b'!<arch>\n',
                           'usr/lib/ImageMagick/module.la': b'libtool metadata'})
        result, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertEqual(result.returncode, 1)
        findings = [item for item in report['packages'][0]['findings'] if item['code'].startswith('artifact.')]
        for kind, value in (('pipewire', 'usr/bin/pw-jack'), ('x11', 'usr/bin/Xwayland'),
                            ('pipewire', 'usr/lib/libjack.a'), ('x11', 'usr/lib32/libX11.a')):
            self.assertTrue(any(item['code'] == 'artifact.' + kind and value in item['message']
                                for item in findings), findings)
        self.assertFalse(any(value in item['message'] for item in findings
                             for value in ('libxkbcommon.a', 'libEGL.a', 'module.la')), findings)

    def test_archive_checks_symlink_and_hardlink_targets_without_following_them(self):
        path = self.make_archive({})
        with tarfile.open(path, 'a') as archive:
            for name, target, kind in (
                ('usr/bin/alias', '/usr/bin/pw-jack', tarfile.SYMTYPE),
                ('usr/lib/private.so', '../lib/libX11.so.6', tarfile.SYMTYPE),
                ('usr/lib/private.a', './usr/lib/libjack.a', tarfile.LNKTYPE),
                ('usr/bin/other', './Xwayland', tarfile.SYMTYPE),
                ('usr/bin/root-escape', '../../../usr/bin/pw-jack', tarfile.SYMTYPE),
                ('usr/lib/safe.so', '/libX11.so.6/libEGL.so.1', tarfile.SYMTYPE),
            ):
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, target
                archive.addfile(member)
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        findings = report['packages'][0]['findings']
        for target in ('/usr/bin/pw-jack', '../lib/libX11.so.6', './usr/lib/libjack.a', './Xwayland', '../../../usr/bin/pw-jack'):
            self.assertTrue(any('target' in item['message'] and target in item['message']
                                for item in findings), findings)
        self.assertFalse(any('safe.so' in item['message'] for item in findings), findings)

    @unittest.skipUnless(shutil.which('cmake') and shutil.which('cc'), 'CMake and C compiler needed')
    def test_configured_audit_uses_native_cmake_link_targets_and_keeps_inactive_options_separate(self):
        source = self.package / 'src/demo-1.0'
        (source / 'dummy.c').write_text('int dummy(void) { return 0; }\n')
        (source / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.22)\nproject(Demo VERSION 1.0 LANGUAGES C)\n'
            'option(WITH_X11 "X11" ON)\noption(INACTIVE_XRANDR "Inactive child" ON)\n'
            'add_library(dummy SHARED dummy.c)\nif(WITH_X11)\n'
            'target_link_libraries(dummy PRIVATE /usr/lib/libX11.so.6)\n'
            'target_link_options(dummy PRIVATE -ljack)\nendif()\n')
        self.run_tool('waypure-options', 'review')
        build = self.root / 'build'
        query = build / '.cmake/api/v1/query/codemodel-v2'
        query.parent.mkdir(parents=True)
        query.touch()
        for enabled in ('ON', 'OFF'):
            subprocess.run(['cmake', '-S', str(source), '-B', str(build), '-DWITH_X11=' + enabled],
                           check=True, capture_output=True, text=True)
            result, report = self.audit('demo', '--build', build, ok=False)
            findings = report['packages'][0]['findings']
            self.assertEqual(result.returncode, 1 if enabled == 'ON' else 0, findings)
            if enabled == 'ON':
                # CMake may rewrite a missing absolute library path as -lX11.
                self.assertTrue(any(item['code'] == 'build.x11' and
                                    ('libX11.so.6' in item['message'] or '-lX11' in item['message'])
                                    for item in findings), findings)
                self.assertTrue(any(item['code'] == 'build.pipewire' and '-ljack' in item['message']
                                    for item in findings), findings)
            else:
                self.assertEqual(report['packages'][0]['checks']['build'], 'PASS', findings)
                self.assertFalse(any(item['code'] == 'build.x11' for item in findings), findings)

    def test_configured_audit_reports_missing_catalogue_and_rejects_ambiguous_selection(self):
        result, report = self.audit('demo', '--build', self.root / 'missing', ok=False)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(report['packages'][0]['checks']['build'], 'FAIL')
        self.assertTrue(any(item['code'] == 'build.unavailable' for item in report['packages'][0]['findings']))
        self.make_package('other')
        result = self.run_tool('waypure-audit', '--repo', self.repo, '--build', self.root, ok=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('one package', result.stderr)

    def test_dependency_paths_keep_split_output_dependencies_separate(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = safe-part\n")
        split = self.make_package("split")
        (split / ".SRCINFO").write_text(textwrap.dedent('''\
            pkgbase = split-waypure
                pkgver = 1.0
                pkgrel = 1
                sha256sums = aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
            pkgname = safe-part-waypure
                provides = safe-part
                conflicts = safe-part
            pkgname = unsafe-part-waypure
                provides = unsafe-part
                conflicts = unsafe-part
                depends = libx11
        '''))
        _, report = self.audit("demo")
        self.assertNotIn("dependency.path", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_incompatible_versioned_provider_does_not_create_a_dependency_path(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = versioned>=2\n")
        middle = self.make_package("middle", ("libx11",))
        metadata = middle / ".SRCINFO"
        metadata.write_text(metadata.read_text().replace("provides = middle", "provides = versioned=1"))
        _, report = self.audit("demo")
        self.assertNotIn("dependency.path", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_audit_detects_srcinfo_version_drift(self):
        recipe = self.package / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=2.0"))
        result, report = self.audit("demo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("srcinfo.version", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_audit_sees_new_recipe_dependencies_before_srcinfo_is_regenerated(self):
        recipe = self.package / "PKGBUILD"
        recipe.write_text("depends=('libx11')\n" + recipe.read_text())
        result, report = self.audit("demo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        codes = [finding["code"] for finding in report["packages"][0]["findings"]]
        self.assertIn("srcinfo.fields", codes)
        self.assertIn("dependency.x11", codes)

    def test_dependency_comment_parentheses_do_not_truncate_literal_array(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('''makedepends=(
  git
  meson
  perl # Vendored generator needs Perl (and XML::Parser).
  perl-xml-parser
  systemd
)
''' + recipe.read_text())
        metadata = self.package/".SRCINFO"
        metadata.write_text(metadata.read_text().replace('pkgname = demo-waypure',
                            '\tmakedepends = git\n\tmakedepends = meson\n\tmakedepends = perl\n'
                            '\tmakedepends = perl-xml-parser\n\tmakedepends = systemd\npkgname = demo-waypure'))
        result, report = self.audit("demo", "--metadata-only", ok=False)
        self.assertEqual(result.returncode, 0, report["packages"][0]["findings"])
        self.assertNotIn("srcinfo.fields", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_comment_closing_parenthesis_cannot_hide_an_x11_dependency(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('depends=(\n glibc # ) is a comment\n libx11\n)\n' + recipe.read_text())
        metadata = self.package/".SRCINFO"
        metadata.write_text(metadata.read_text() + '\tdepends = glibc\n')
        _, report = self.audit("demo", "--metadata-only", ok=False)
        codes = [finding["code"] for finding in report["packages"][0]["findings"]]
        self.assertIn("dependency.x11", codes)
        self.assertIn("srcinfo.fields", codes)

    def test_computed_epoch_and_scalar_dependency_assignments_are_uncertain(self):
        recipe = self.package / "PKGBUILD"
        recipe.write_text("epoch=${computed_epoch}\ndepends=$computed_dependencies\n" + recipe.read_text())
        _, report = self.audit("demo")
        row = report["packages"][0]
        self.assertEqual(row["checks"]["recipe"], "WARN")
        messages = [finding["message"] for finding in row["findings"] if finding["code"] == "srcinfo.dynamic"]
        self.assertTrue(any("epoch" in message for message in messages), messages)
        self.assertTrue(any("depends" in message for message in messages), messages)

    def test_literal_package_aliases_are_verified_against_metadata(self):
        recipe = self.package/"PKGBUILD"
        original = recipe.read_text()
        prefix = '''_project=demo
_pkgname="${_project}"
pkgname="${_pkgname}-waypure"
provides=("${_pkgname}")
conflicts=("$_pkgname")
'''
        for variable in ("_pkgname", "_pkgbase"):
            with self.subTest(variable=variable):
                recipe.write_text(prefix.replace("_pkgname", variable) + original)
                _, report = self.audit("demo", "--metadata-only")
                row = report["packages"][0]
                self.assertEqual(row["checks"]["recipe"], "PASS", row["findings"])
                self.assertNotIn("srcinfo.dynamic", [item["code"] for item in row["findings"]])

    def test_expanded_aliases_still_detect_stale_metadata(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_pkgname=renamed-demo\nprovides=("${_pkgname}")\nconflicts=("$_pkgname")\n' + recipe.read_text())
        result, report = self.audit("demo", "--metadata-only", ok=False)
        self.assertEqual(result.returncode, 1)
        messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.fields"]
        self.assertTrue(any("provides" in message and "renamed-demo" in message for message in messages), messages)
        self.assertTrue(any("conflicts" in message and "renamed-demo" in message for message in messages), messages)

    def test_literal_package_name_alias_detects_stale_metadata_identity(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_pkgname=demo\npkgname=${_pkgname}-waypure\n' + recipe.read_text())
        metadata = self.package/".SRCINFO"
        metadata.write_text(metadata.read_text().replace('pkgbase = demo-waypure', 'pkgbase = old-demo-waypure')
                            .replace('pkgname = demo-waypure', 'pkgname = old-demo-waypure'))
        result, report = self.audit("demo", "--metadata-only", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('srcinfo.identity', [item['code'] for item in report['packages'][0]['findings']])

    def test_literal_split_package_names_detect_stale_metadata_identity(self):
        recipe = self.package/'PKGBUILD'
        recipe.write_text('pkgbase=demo-waypure\n_names=demo\npkgname=("${_names}-a" "${_names}-b")\n' + recipe.read_text())
        result, report = self.audit('demo', '--metadata-only', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('srcinfo.identity', [item['code'] for item in report['packages'][0]['findings']])

    def test_literal_license_mismatch_is_not_a_recipe_pass(self):
        recipe = self.package/'PKGBUILD'
        recipe.write_text("license=('MIT')\n" + recipe.read_text())
        metadata = self.package/'.SRCINFO'
        metadata.write_text(metadata.read_text().replace('\tarch = x86_64', '\tlicense = BSD-2-Clause\n\tarch = x86_64'))
        result, report = self.audit('demo', '--metadata-only', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(item['code'] == 'srcinfo.fields' and 'license' in item['message'] for item in report['packages'][0]['findings']))

    def test_literal_source_mismatch_is_not_a_recipe_pass(self):
        recipe = self.package/'PKGBUILD'
        recipe.write_text("source=('https://example.invalid/other.tar.gz')\n" + recipe.read_text())
        _, report = self.audit('demo', '--metadata-only', ok=False)
        self.assertTrue(any(item['code'] == 'srcinfo.fields' and 'source:' in item['message'] for item in report['packages'][0]['findings']))

    def test_source_count_must_match_checksum_count(self):
        metadata = self.package/'.SRCINFO'
        metadata.write_text(metadata.read_text().replace('\tsha256sums =', '\tsource = second.patch\n\tsha256sums ='))
        result, report = self.audit('demo', '--metadata-only', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('source.checksums', [item['code'] for item in report['packages'][0]['findings']])

    def test_source_and_checksum_order_must_match_recorded_metadata(self):
        recipe = self.package/'PKGBUILD'
        metadata = self.package/'.SRCINFO'
        original_recipe, original_metadata = recipe.read_text(), metadata.read_text()
        recorded = original_metadata.replace('\tsource = https://example.invalid/demo-1.0.tar.gz\n', '\tsource = https://example.invalid/demo-1.0.tar.gz\n\tsource = second.patch\n')
        recorded = recorded.replace('\npkgname =', '\n\tsha256sums = ' + 'b'*64 + '\npkgname =')
        metadata.write_text(recorded)
        for source, hashes, field in (
            ("'second.patch' 'https://example.invalid/demo-1.0.tar.gz'", "'" + 'a'*64 + "' '" + 'b'*64 + "'", 'source:'),
            ("'https://example.invalid/demo-1.0.tar.gz' 'second.patch'", "'" + 'b'*64 + "' '" + 'a'*64 + "'", 'sha256sums:'),
        ):
            with self.subTest(field=field):
                recipe.write_text('source=(' + source + ')\nsha256sums=(' + hashes + ')\n' + original_recipe)
                _, report = self.audit('demo', '--metadata-only', ok=False)
                self.assertTrue(any(item['code'] == 'srcinfo.fields' and field in item['message'] for item in report['packages'][0]['findings']))

    def test_architecture_specific_sources_need_their_own_checksums(self):
        metadata = self.package/'.SRCINFO'
        original = metadata.read_text()
        for checksum in ('', '\tsha256sums_x86_64 = ' + 'b'*64 + '\n'):
            with self.subTest(checksum=bool(checksum)):
                metadata.write_text(original.replace('\tpkgrel = 1', '\tsource_x86_64 = arch.patch\n' + checksum + '\tpkgrel = 1'))
                _, report = self.audit('demo', '--metadata-only', ok=False)
                codes = [item['code'] for item in report['packages'][0]['findings']]
                self.assertEqual('source.checksums' in codes, not bool(checksum), report['packages'][0]['findings'])

    def test_variable_dependencies_cannot_bypass_x11_checks(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_backend=libx11\ndepends=("${_backend}")\n' + recipe.read_text())
        _, report = self.audit("demo", "--metadata-only", ok=False)
        codes = [item["code"] for item in report["packages"][0]["findings"]]
        self.assertIn("dependency.x11", codes)

    def test_shared_scalar_reader_resolves_literal_version_aliases(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_release=1.0\n' + recipe.read_text().replace('pkgver=1.0', 'pkgver="${_release}"'))
        self.run_tool("waypure-options", "review")
        _, report = self.audit("demo")
        self.assertEqual(report["packages"][0]["checks"]["recipe"], "PASS")

    def test_ambiguous_package_aliases_remain_unverified(self):
        prefixes = (
            '_pkgname=$UNKNOWN\n',
            '_pkgname=demo\n_pkgname=other\n',
            '_pkgname=demo\n_pkgname+=-other\n',
            '_pkgname=demo\nif true; then\n _pkgname=other\nfi\n',
            '_pkgname=demo\nunset _pkgname\n',
            '_pkgname=demo\nsource /missing/metadata\n',
            '_pkgname=$later\nlater=demo\n',
            '_pkgname=demo\n_tmp=x eval \'_pkgname=evil\'\n',
            '_pkgname=demo\n_tmp=x _pkgname=evil\n',
            '_pkgname=demo\n_tmp= _pkgname=evil\n',
            '_pkgname= true\n',
            '_pkgname=demo\nIFS=:\n',
            'RANDOM=demo\n_pkgname="$RANDOM"\n',
            'LINENO=demo\n_pkgname="$LINENO"\n',
            'SECONDS=demo\n_pkgname="$SECONDS"\n',
            'BASH_SUBSHELL=demo\n_pkgname="$BASH_SUBSHELL"\n',
            'OPTIND=demo\n_pkgname="$OPTIND"\n',
        )
        original = (self.package/"PKGBUILD").read_text()
        for prefix in prefixes:
            with self.subTest(prefix=prefix):
                (self.package/"PKGBUILD").write_text(prefix + 'provides=("${_pkgname}")\nconflicts=("$_pkgname")\n' + original)
                _, report = self.audit("demo", "--metadata-only")
                messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
                self.assertTrue(any(message.startswith("provides ") for message in messages), messages)
                self.assertTrue(any(message.startswith("conflicts ") for message in messages), messages)

    def test_mutated_scalar_version_alias_stays_unverified(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_release=1.0\n' + recipe.read_text().replace('pkgver=1.0', 'pkgver="$_release"\npkgver=2.0'))
        metadata = self.package/".SRCINFO"
        metadata.write_text(metadata.read_text().replace('pkgver = 1.0', 'pkgver = 2.0'))
        _, report = self.audit("demo", "--metadata-only")
        messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
        self.assertTrue(any(message.startswith("pkgver ") for message in messages), messages)

    def test_indexed_metadata_array_writes_stay_unverified(self):
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_pkgname=demo\nprovides=("$_pkgname")\nprovides[0]=other\n' + recipe.read_text())
        _, report = self.audit("demo", "--metadata-only")
        messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
        self.assertTrue(any(message.startswith("provides ") for message in messages), messages)

    def test_array_command_suffixes_stay_unverified(self):
        recipe = self.package/"PKGBUILD"
        original = recipe.read_text()
        for suffix in ('; provides[0]=other', '; unset provides'):
            with self.subTest(suffix=suffix):
                recipe.write_text('_pkgname=demo\nprovides=("$_pkgname")' + suffix + '\n' + original)
                _, report = self.audit("demo", "--metadata-only")
                messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
                self.assertTrue(any(message.startswith("provides ") for message in messages), messages)

    def test_unset_metadata_arrays_and_aliases_stay_unverified(self):
        recipe = self.package/"PKGBUILD"
        original = recipe.read_text()
        for target in ('provides', '_pkgname', '-v -- provides', "'provides[0]'", '"$_target"'):
            with self.subTest(target=target):
                recipe.write_text('_target=provides\n_pkgname=demo\nprovides=("$_pkgname")\nunset ' + target + '\n' + original)
                _, report = self.audit("demo", "--metadata-only")
                messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
                self.assertTrue(any(message.startswith("provides ") for message in messages), messages)

    def test_unset_scalar_versions_and_aliases_stay_unverified(self):
        recipe = self.package/"PKGBUILD"
        original = '_release=1.0\n' + recipe.read_text().replace('pkgver=1.0', 'pkgver="$_release"')
        for target in ('pkgver', '_release'):
            with self.subTest(target=target):
                recipe.write_text(original + '\nunset ' + target + '\n')
                _, report = self.audit("demo", "--metadata-only")
                messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.dynamic"]
                self.assertTrue(any(message.startswith("pkgver ") for message in messages), messages)

    def test_package_alias_resolution_never_executes_shell_commands(self):
        sentinel = self.root/"must-not-execute"
        recipe = self.package/"PKGBUILD"
        recipe.write_text('_pkgname=$(touch "' + str(sentinel) + '"; printf demo)\nprovides=("${_pkgname}")\n' + recipe.read_text())
        _, report = self.audit("demo", "--metadata-only")
        self.assertFalse(sentinel.exists())
        self.assertIn("srcinfo.dynamic", [item["code"] for item in report["packages"][0]["findings"]])

    def test_single_quoted_and_escaped_references_stay_literal(self):
        original = (self.package/"PKGBUILD").read_text()
        for word in ("'${_pkgname}'", r'\${_pkgname}'):
            with self.subTest(word=word):
                (self.package/"PKGBUILD").write_text('_pkgname=demo\nprovides=(' + word + ')\n' + original)
                result, report = self.audit("demo", "--metadata-only", ok=False)
                self.assertEqual(result.returncode, 1)
                messages = [item["message"] for item in report["packages"][0]["findings"] if item["code"] == "srcinfo.fields"]
                self.assertTrue(any("recipe=['${_pkgname}']" in message for message in messages), messages)

    def test_removed_literal_array_entries_invalidate_srcinfo(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text() + "\tdepends = old-dependency\n")
        recipe = self.package / "PKGBUILD"
        recipe.write_text("depends=()\n" + recipe.read_text())
        result, report = self.audit("demo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("srcinfo.fields", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_newer_sync_version_remains_visible_with_an_old_git_snapshot(self):
        self.run_tool("waypure-options", "review", "--arch-local", self.arch_repository())
        binaries = self.root / "bin"
        binaries.mkdir()
        pacman = binaries / "pacman"
        pacman.write_text("#!/bin/sh\n[ \"$1\" = -Sl ] || exit 2\nprintf 'extra demo 2:2.0-1\\n'\n")
        pacman.chmod(0o755)
        result, report = self.audit("demo", env={"PATH": str(binaries) + os.pathsep + os.environ["PATH"], 'WAYPURE_ALPM_BACKEND':'cli'}, ok=False)
        self.assertNotEqual(result.returncode, 0)
        row = report["packages"][0]
        self.assertIn("arch.epoch", [finding["code"] for finding in row["findings"]])
        self.assertEqual({item["version"] for item in row["arch_observations"]}, {"1.0-1", "2:2.0-1"})

    def test_replacement_alias_uses_declared_official_name(self):
        path = self.package / ".SRCINFO"
        path.write_text(path.read_text().replace("provides = demo", "provides = official-demo").replace("conflicts = demo", "conflicts = official-demo"))
        _, report = self.audit("demo")
        self.assertNotIn("package.replacement", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_changed_source_tree_marks_catalogue_stale(self):
        self.run_tool("waypure-options", "review")
        source = self.package / "src/demo-1.0/CMakeLists.txt"
        source.write_text(source.read_text() + 'option(NEW_FEATURE "New" ON)\n')
        _, report = self.audit("demo")
        self.assertEqual(report["packages"][0]["checks"]["options"], "STALE")
        self.assertIn("options.stale", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_missing_option_evidence_is_visible_and_fails_strict_audit(self):
        _, report = self.audit("demo")
        self.assertEqual(report["packages"][0]["checks"]["options"], "MISSING")
        result, _ = self.audit("demo", "--strict", ok=False)
        self.assertNotEqual(result.returncode, 0)

    def test_unknown_package_selection_fails_instead_of_auditing_nothing(self):
        result = self.run_tool("waypure-audit", "--repo", self.repo, "typo", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("typo", result.stderr)

    def fake_namcap(self, output='', status=0):
        directory = self.root/'bin'
        directory.mkdir(exist_ok=True)
        command = directory/'namcap'
        command.write_text('#!/usr/bin/env python3\nimport sys\nprint(' + repr(output) + ')\nsys.exit(' + str(status) + ')\n')
        command.chmod(0o755)
        return {'PATH': str(directory) + os.pathsep + os.environ['PATH']}

    def test_explicit_namcap_honors_error_findings_with_zero_exit_status(self):
        env = self.fake_namcap('demo E: synthetic license file missing')
        result, report = self.audit('demo', '--metadata-only', '--namcap', ok=False, env=env)
        self.assertEqual(result.returncode, 1, result.stderr)
        row = report['packages'][0]
        self.assertEqual(row['checks']['namcap'], 'FAIL')
        self.assertTrue(any(item['code'] == 'namcap.error' and 'synthetic license' in item['message'] for item in row['findings']))

    def test_namcap_warning_is_visible_and_strict_audit_fails(self):
        env = self.fake_namcap('PKGBUILD (demo) W: synthetic non-unique source filename')
        _, report = self.audit('demo', '--metadata-only', '--namcap', env=env)
        self.assertEqual(report['packages'][0]['checks']['namcap'], 'WARN')
        result, _ = self.audit('demo', '--metadata-only', '--namcap', '--strict', ok=False, env=env)
        self.assertEqual(result.returncode, 1)

    def test_default_audit_does_not_invoke_namcap(self):
        env = self.fake_namcap('demo E: default audit must not invoke this command')
        recipe = self.package/'PKGBUILD'
        sentinel = self.root/'must-not-source'
        recipe.write_text('touch ' + str(sentinel) + '\n' + recipe.read_text())
        _, report = self.audit('demo', '--metadata-only', env=env)
        self.assertEqual(report['packages'][0]['checks'].get('namcap'), 'SKIP')
        self.assertNotIn('namcap.error', [item['code'] for item in report['packages'][0]['findings']])
        self.assertFalse(sentinel.exists())

    def make_archive(self, extra):
        path = self.package / "demo-waypure-1.0-1-x86_64.pkg.tar"
        with tarfile.open(path, "w") as archive:
            entries = {".PKGINFO": b"pkgname = demo-waypure\npkgbase = demo-waypure\npkgver = 1.0-1\n", **extra}
            for name, data in entries.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path

    @unittest.skipUnless(shutil.which("cc") and shutil.which("readelf"), "C compiler and readelf needed")
    def test_artifact_audit_reads_real_elf_dependencies_without_running_binary(self):
        source = self.root / "dummy.c"
        source.write_text("int dummy(void) { return 0; }\n")
        library = self.root / "libX11.so.6"
        subprocess.run(["cc", "-shared", "-fPIC", "-Wl,-soname,libX11.so.6", str(source), "-o", str(library)], check=True)
        consumer = self.root / "consumer.so"
        subprocess.run(["cc", "-shared", "-fPIC", "-Wl,--no-as-needed", str(source), str(library), "-o", str(consumer)], check=True)
        self.make_archive({"usr/lib/consumer.so": consumer.read_bytes()})
        result, report = self.audit("demo", "--artifacts", ok=False)
        self.assertNotEqual(result.returncode, 0)
        findings = report["packages"][0]["findings"]
        self.assertTrue(any(finding["code"] == "artifact.x11" and "libX11.so.6" in finding["message"] for finding in findings), findings)

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_artifact_preserves_paths_in_forbidden_needed_diagnostics(self):
        source = self.root / 'dummy.c'
        source.write_text('int dummy(void) { return 0; }\n')
        entries = {}
        for index, soname in enumerate(('/usr/lib/libX11.so.6', '$ORIGIN/../lib/libjack.so.0')):
            library = self.root / f'lib{index}.so'
            consumer = self.root / f'consumer{index}.so'
            subprocess.run(['cc', '-shared', '-fPIC', '-Wl,-soname,' + soname,
                            str(source), '-o', str(library)], check=True)
            subprocess.run(['cc', '-shared', '-fPIC', '-Wl,--no-as-needed', str(source),
                            str(library), '-o', str(consumer)], check=True)
            entries[f'usr/lib/consumer{index}.so'] = consumer.read_bytes()
        self.make_archive(entries)
        result, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertEqual(result.returncode, 1)
        findings = report['packages'][0]['findings']
        for kind, value in (('x11', '/usr/lib/libX11.so.6'), ('pipewire', '$ORIGIN/../lib/libjack.so.0')):
            self.assertTrue(any(item['code'] == 'artifact.' + kind and 'needs ' + value in item['message']
                                for item in findings), findings)

    def test_artifact_audit_accepts_epoch_in_archive_filename(self):
        metadata = self.package/".SRCINFO"
        metadata.write_text(metadata.read_text().replace("pkgrel = 1", "pkgrel = 1\n\tepoch = 1"))
        recipe = self.package/"PKGBUILD"
        recipe.write_text("epoch=1\n" + recipe.read_text())
        original = self.make_archive({".PKGINFO": b"pkgname = demo-waypure\npkgver = 1:1.0-1\n"})
        original.rename(self.package/"demo-waypure-1:1.0-1-x86_64.pkg.tar")
        _, report = self.audit("demo", "--artifacts", "--metadata-only")
        row = report["packages"][0]
        self.assertEqual(row["checks"]["artifacts"], "PASS", row["findings"])
        self.assertEqual(row["artifacts"][0]["pkgver"], "1:1.0-1")

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_artifact_audit_rejects_bundled_forbidden_library_without_needed_entries(self):
        source = self.root/'synthetic.c'
        source.write_text('int synthetic(void) { return 0; }\n')
        library = self.root/'dummy.so'
        subprocess.run(['cc', '-shared', '-nostdlib', '-Wl,-soname,libX11.so.6', str(source), '-o', str(library)], check=True)
        self.make_archive({'usr/lib/renamed.so': library.read_bytes()})
        result, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(any(item['code'] == 'artifact.x11' and 'libX11.so.6' in item['message'] for item in report['packages'][0]['findings']))

    def test_artifact_audit_rejects_forbidden_library_symlink_name(self):
        path = self.make_archive({})
        with tarfile.open(path, 'a') as archive:
            member = tarfile.TarInfo('usr/lib/libX11.so.6')
            member.type = tarfile.SYMTYPE
            member.linkname = 'renamed.so'
            archive.addfile(member)
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertIn('artifact.x11', [item['code'] for item in report['packages'][0]['findings']])

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_artifact_audit_labels_jack_sonames_and_netjack_modules(self):
        source = self.root / 'synthetic.c'
        source.write_text('int synthetic(void) { return 0; }\n')
        library = self.root / 'dummy.so'
        subprocess.run(['cc', '-shared', '-nostdlib', '-Wl,-soname,libjack.so.0',
                        str(source), '-o', str(library)], check=True)
        module = self.root / 'module.so'
        subprocess.run(['cc', '-shared', '-nostdlib', str(source), '-o', str(module)], check=True)
        self.make_archive({'usr/lib/renamed.so': library.read_bytes(),
                           'usr/lib/pipewire-0.3/libpipewire-module-netjack2-driver.so': module.read_bytes()})
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        findings = report['packages'][0]['findings']
        messages = [item['message'] for item in findings if item['code'] == 'artifact.pipewire']
        self.assertTrue(any('libjack.so.0' in message for message in messages), findings)
        self.assertTrue(any('libpipewire-module-netjack2-driver.so' in message for message in messages), findings)
        self.assertNotIn('artifact.x11', [item['code'] for item in findings])

    def test_artifact_audit_labels_audio_dependency_and_jack_library_links(self):
        path = self.make_archive({'.PKGINFO': b'pkgname = demo-waypure\npkgver = 1.0-1\ndepend = pipewire-jack\n'})
        with tarfile.open(path, 'a') as archive:
            member = tarfile.TarInfo('usr/lib/libjackserver.so.0')
            member.type = tarfile.SYMTYPE
            member.linkname = 'renamed.so'
            archive.addfile(member)
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        findings = report['packages'][0]['findings']
        messages = [item['message'] for item in findings if item['code'] == 'artifact.pipewire']
        self.assertTrue(any('depends on pipewire-jack' in message for message in messages), findings)
        self.assertTrue(any('libjackserver.so.0' in message for message in messages), findings)

    @unittest.skipUnless(shutil.which('cc') and shutil.which('readelf'), 'C compiler and readelf needed')
    def test_artifact_audit_rejects_forbidden_library_hardlink_name(self):
        source = self.root/'synthetic.c'
        source.write_text('int synthetic(void) { return 0; }\n')
        library = self.root/'dummy.so'
        subprocess.run(['cc', '-shared', '-nostdlib', str(source), '-o', str(library)], check=True)
        path = self.make_archive({'usr/lib/renamed.so': library.read_bytes()})
        with tarfile.open(path, 'a') as archive:
            member = tarfile.TarInfo('usr/lib/libX11.so.6')
            member.type = tarfile.LNKTYPE
            member.linkname = 'usr/lib/renamed.so'
            archive.addfile(member)
        _, report = self.audit('demo', '--metadata-only', '--artifacts', ok=False)
        self.assertIn('artifact.x11', [item['code'] for item in report['packages'][0]['findings']])

    def test_same_version_artifact_older_than_recipe_is_stale(self):
        path = self.make_archive({})
        os.utime(path, (1, 1))
        _, report = self.audit('demo', '--metadata-only', '--artifacts')
        row = report['packages'][0]
        self.assertEqual(row['checks']['artifacts'], 'STALE', row['findings'])
        self.assertIn('artifact.stale', [item['code'] for item in row['findings']])

    def test_text_file_named_like_a_library_is_not_proof_of_bundled_elf(self):
        self.make_archive({'usr/share/doc/demo/libX11.so.6.txt': b'Text about an optional library\n'})
        _, report = self.audit('demo', '--metadata-only', '--artifacts')
        self.assertNotIn('artifact.x11', [item['code'] for item in report['packages'][0]['findings']])

    def test_archive_path_escape_is_reported_without_extracting_files(self):
        self.make_archive({"../../outside": b"must not extract"})
        result, report = self.audit("demo", "--artifacts", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "outside").exists())
        self.assertIn("archive.path", [finding["code"] for finding in report["packages"][0]["findings"]])

    def test_installed_audit_does_not_need_the_builder_executable_to_read_metadata(self):
        prefix = self.root / 'installed'
        self.run_tool('install', '--prefix', prefix)
        (prefix / 'lib/arch-waypure/waypure-build').unlink()
        result = subprocess.run([str(prefix / 'bin/waypure-audit'), '--repo', str(self.repo),
                                 'demo', '--metadata-only', '--json'],
                                cwd=self.root, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(json.loads(result.stdout)['packages'][0]['checks']['recipe'], 'PASS')

    def test_installer_exposes_audit_and_builder_from_another_directory(self):
        prefix = self.root / "installed"
        self.run_tool("install", "--prefix", prefix)
        for name in ("waypure-audit", "waypure-build"):
            self.assertTrue((prefix / "bin" / name).is_file(), name + " must be installed")
            result = subprocess.run([str(prefix / "bin" / name), "--help"], cwd=self.root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((prefix / "bin/waypure-versions").is_file())


if __name__ == "__main__":
    unittest.main()
