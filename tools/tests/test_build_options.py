"""Integration tests for the package option maintenance command."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]
COMMAND = TOOLS / "waypure-options"


class ToolkitCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "src/demo-1.0"
        self.source.mkdir(parents=True)
        (self.source / "CMakeLists.txt").write_text('''
cmake_minimum_required(VERSION 3.22)
project(Demo VERSION 1.0 LANGUAGES NONE)
include(CMakeDependentOption)
option(WITH_X11 "Build X11 support" ON)
option(WITH_WAYLAND "Build Wayland support" OFF)
cmake_dependent_option(WITH_XCB "Build XCB integration" ON "WITH_X11" OFF)
set(DRIVER "auto" CACHE STRING "Graphics driver")
set_property(CACHE DRIVER PROPERTY STRINGS auto vulkan software)
''')
        (self.root / "PKGBUILD").write_text('''
pkgver=1.0
build() {
  local _cmake_options=(
    -B build -G Ninja
    -DWITH_X11=OFF # WAYLAND
    -DWITH_WAYLAND=ON # WAYLAND
    -DWITH_XCB=OFF # WAYLAND
    '-DCMAKE_CXX_FLAGS=-O2 -DVALUE=1' # compiler flags
  )
}
''')

    def run_tool(self, *arguments, ok=True):
        result = subprocess.run([str(COMMAND), *map(str, arguments)], cwd=self.root,
                                text=True, capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}) if COMMAND.exists() else None
        self.assertIsNotNone(result, "tools/waypure-options must provide the shared CLI")
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def snapshot(self, source=None, filename="first.json.log", backend="cmake"):
        output = self.root / filename
        self.run_tool("snapshot", source or self.source, "--backend", backend, "-o", output)
        return output

    def test_snapshot_keeps_declared_defaults_and_choices(self):
        data = json.loads(self.snapshot().read_text())
        self.assertEqual(data["backend"], "cmake")
        self.assertEqual(data["version"], "1.0")
        self.assertEqual(data["options"]["WITH_X11"]["default"], "ON")
        self.assertEqual(data["options"]["DRIVER"]["choices"], ["auto", "vulkan", "software"])
        self.assertFalse((self.source / "CMakeCache.txt").exists())

    def test_changed_catalogue_cannot_replace_a_release_baseline(self):
        path = self.snapshot()
        original = path.read_bytes()
        with (self.source / "CMakeLists.txt").open("a") as stream:
            stream.write('option(NEW_FEATURE "New" ON)\n')
        result = self.run_tool("snapshot", self.source, "--backend", "cmake", "-o", path, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(path.read_bytes(), original)

    def test_diff_reports_removed_new_and_changed_options(self):
        old = self.snapshot()
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text().replace('option(WITH_WAYLAND "Build Wayland support" OFF)',
                                                'option(WITH_PIPEWIRE "Build PipeWire support" ON)')
                        .replace('option(WITH_X11 "Build X11 support" ON)',
                                 'option(WITH_X11 "Build X11 support" OFF)'))
        new = self.snapshot(filename="second.json.log")
        output = self.run_tool("diff", old, new).stdout
        self.assertIn("+ WITH_PIPEWIRE", output)
        self.assertIn("- WITH_WAYLAND", output)
        self.assertIn("~ WITH_X11", output)
        self.assertIn("ON -> OFF", output)

    def test_render_explains_redundancy_without_executing_pkgbuild(self):
        recipe = self.root / "PKGBUILD"
        sentinel = self.root / "must-not-exist"
        recipe.write_text(f"touch '{sentinel}'\n" + recipe.read_text())
        output = self.run_tool("render", self.snapshot(), "--prune-redundant").stdout
        self.assertIn("# -DWITH_XCB=OFF", output)
        self.assertIn("REDUNDANT", output)
        self.assertIn("WITH_X11=OFF", output)
        self.assertIn("'-DCMAKE_CXX_FLAGS=-O2 -DVALUE=1'", output)
        self.assertIn("WAYLAND", output)
        self.assertFalse(sentinel.exists())
        syntax = subprocess.run(["bash", "-n"], input=output, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    def test_explicit_off_matching_default_is_kept_as_packaging_intent(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'option(WITH_ACCESSIBILITY "Optional integration" OFF)\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    -B build -G Ninja', '    -DWITH_ACCESSIBILITY=OFF # PERSONAL\n    -B build -G Ninja'))
        output = self.run_tool("render", self.snapshot(), "--prune-redundant").stdout
        line = next(line for line in output.splitlines() if "-DWITH_ACCESSIBILITY=OFF" in line)
        self.assertTrue(line.lstrip().startswith("-D"), line)

    def test_render_distinguishes_cmake_string_defaults_from_boolean_switches(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + '''
set(EMPTY "" CACHE STRING "Optional text")
set(LITERAL_OFF "OFF" CACHE STRING "Literal text")
set(LITERAL_ON "ON" CACHE STRING "Literal text")
set(ZERO "0" CACHE STRING "Count")
option(DISABLED "Optional feature" OFF)
''')
        output = self.run_tool("render", self.snapshot()).stdout
        for name in ["EMPTY", "LITERAL_OFF", "LITERAL_ON", "ZERO"]:
            with self.subTest(option=name):
                line = next(line for line in output.splitlines() if "-D" + name + "=" in line)
                self.assertIn("[DEFAULT]", line)
        self.assertIn("empty string", next(line for line in output.splitlines() if "-DEMPTY=" in line))
        self.assertIn("[DEFAULT OFF]", next(line for line in output.splitlines() if "-DDISABLED=" in line))
        syntax = subprocess.run(["bash", "-n"], input=output, text=True, capture_output=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)

    @unittest.skipUnless(shutil.which("meson"), "Meson needed")
    def test_render_distinguishes_meson_string_and_integer_defaults_from_switches(self):
        (self.source / "meson.build").write_text("project('meson-demo', version: '1.0')\n")
        (self.source / "meson.options").write_text('''
option('text', type: 'string', value: '')
option('literal', type: 'string', value: 'false')
option('count', type: 'integer', value: 0)
option('disabled', type: 'boolean', value: false)
option('probe', type: 'feature', value: 'auto')
''')
        (self.root / "PKGBUILD").write_text('pkgver=1.0\nbuild() {\n local _meson_options=(\n )\n}\n')
        output = self.run_tool("render", self.snapshot(backend="meson")).stdout
        for name in ["text", "literal", "count"]:
            with self.subTest(option=name):
                line = next(line for line in output.splitlines() if "-D" + name + "=" in line)
                self.assertIn("[DEFAULT]", line)
        self.assertIn("[DEFAULT OFF]", next(line for line in output.splitlines() if "-Ddisabled=" in line))
        self.assertIn("[AUTO]", next(line for line in output.splitlines() if "-Dprobe=" in line))

    def test_commented_option_explains_its_effective_default_without_selecting_it(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'option(WITH_EXAMPLE "Example integration" ON)\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    -B build -G Ninja',
                                                    '    # -DWITH_EXAMPLE=OFF # MINIMAL\n    -B build -G Ninja'))
        output = self.run_tool("render", self.snapshot()).stdout
        line = next(line for line in output.splitlines() if "-DWITH_EXAMPLE=" in line)
        self.assertTrue(line.lstrip().startswith("# -D"), line)
        self.assertIn("[UNSET]", line)
        self.assertIn("effective value ON", line)
        self.assertIn("MINIMAL", line)

    def test_cmake_normal_assignment_prevents_a_false_dependency_proof(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'set(WITH_X11 ON)\n')
        output = self.run_tool("render", self.snapshot(), "--prune-redundant").stdout
        line = next(line for line in output.splitlines() if "-DWITH_XCB=OFF" in line)
        self.assertTrue(line.lstrip().startswith("-D"), line)

    @unittest.skipUnless(shutil.which("cmake"), "CMake needed for native behavior")
    def test_guarded_cmake_declaration_cannot_justify_pruning(self):
        path = self.source / "CMakeLists.txt"
        path.write_text('''cmake_minimum_required(VERSION 3.22)
project(Demo VERSION 1.0 LANGUAGES NONE)
include(CMakeDependentOption)
option(WITH_ROOT "Root" ON)
if(WITH_ROOT)
  cmake_dependent_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
endif()
if(NOT DEFINED WITH_CHILD OR WITH_CHILD)
  set(CHILD_RESULT ON)
else()
  set(CHILD_RESULT OFF)
endif()
file(WRITE "${CMAKE_BINARY_DIR}/result" "${CHILD_RESULT}")
''')
        recipe = self.root / "PKGBUILD"
        recipe.write_text('pkgver=1.0\nbuild() {\n local _cmake_options=(\n -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF\n )\n}\n')
        output = self.run_tool("render", self.snapshot(), "--prune-redundant").stdout
        proposed = subprocess.check_output(["bash", "-c", output + '\nprintf "%s\\0" "${_cmake_options[@]}"'], text=True).split("\0")[:-1]
        for name, flags in (("original", ["-DWITH_ROOT=OFF", "-DWITH_CHILD=OFF"]), ("proposal", proposed)):
            build = self.root / name
            result = subprocess.run(["cmake", "-S", str(self.source), "-B", str(build), *flags], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((build / "result").read_text(), "OFF")

    def test_function_scoped_cmake_declaration_cannot_justify_pruning(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text().replace('cmake_dependent_option(WITH_XCB "Build XCB integration" ON "WITH_X11" OFF)',
                                                'function(unused)\ncmake_dependent_option(WITH_XCB "Build XCB integration" ON "WITH_X11" OFF)\nendfunction()'))
        output = self.run_tool("render", self.snapshot(), "--prune-redundant").stdout
        line = next(line for line in output.splitlines() if "-DWITH_XCB=OFF" in line)
        self.assertTrue(line.lstrip().startswith("-D"), line)

    def test_cmake_comparisons_handle_quoted_literals_and_rhs_variables(self):
        path = self.source / "CMakeLists.txt"
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    -B build -G Ninja', '    -DWITH_MODE_GATE=ON\n    -B build -G Ninja'))
        original = path.read_text()
        for index, condition in enumerate(('CMAKE_SYSTEM_NAME STREQUAL "Linux"', 'MODE STREQUAL OTHER')):
            with self.subTest(condition=condition):
                path.write_text(original + 'set(MODE same CACHE STRING "Mode")\nset(OTHER same CACHE STRING "Other mode")\n'
                                + 'cmake_dependent_option(WITH_MODE_GATE "Mode gate" ON [[' + condition + ']] OFF)\n')
                self.run_tool("check", self.snapshot(filename=f"compare-{index}.json.log"))

    def test_profile_proposes_explicit_changes_and_check_reports_conflicts(self):
        (self.root / "build-options.toml").write_text('''
[package]
backend = "cmake"
profiles = ["WAYLAND"]
[profiles.WAYLAND]
description = "Use the Wayland backend"
flags = ["-DWITH_X11=OFF", "-DWITH_WAYLAND=ON"]
''')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("-DWITH_X11=OFF", "-DWITH_X11=ON"))
        before = recipe.read_bytes()
        inventory = self.snapshot()
        result = self.run_tool("check", inventory, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WAYLAND", result.stdout + result.stderr)
        rendered = self.run_tool("render", inventory).stdout
        self.assertIn("-DWITH_X11=OFF", rendered)
        self.assertIn("POLICY CHANGE", rendered)
        self.assertEqual(recipe.read_bytes(), before)

    def test_pruned_profile_proposal_satisfies_the_same_policy(self):
        (self.root / "build-options.toml").write_text('''
[package]
profiles = ["WAYLAND"]
[profiles.WAYLAND]
flags = ["-DWITH_X11=OFF", "-DWITH_WAYLAND=ON", "-DWITH_XCB=OFF"]
''')
        inventory = self.snapshot()
        output = self.run_tool("render", inventory, "--prune-redundant").stdout
        (self.root / "PKGBUILD").write_text("pkgver=1.0\nbuild() {\n" + output + "\n}\n")
        self.run_tool("check", inventory)

    def test_render_copy_cycles_preserve_notes_without_accumulating_annotations(self):
        (self.root / "build-options.toml").write_text('[package]\nprofiles=["WAYLAND"]\n[profiles.WAYLAND]\nflags=["-DWITH_X11=OFF", "-DWITH_XCB=OFF"]\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('# WAYLAND', '# WAYLAND; keep this maintainer note'))
        inventory = self.snapshot()
        first = self.run_tool("render", inventory, "--prune-redundant").stdout
        previous = first
        for index in range(3):
            recipe.write_text("pkgver=1.0\nbuild() {\n" + previous + "\n}\n")
            output = self.run_tool("render", inventory, "--prune-redundant").stdout
            self.assertEqual(output.count("keep this maintainer note"), first.count("keep this maintainer note"))
            self.assertEqual(output.count("compiler flags"), 1)
            # The first copy changes REDUNDANT to IMPLIED OFF, because the
            # maintainer has now omitted that explicit flag. Later copies settle.
            if index:
                self.assertEqual(output, previous)
            previous = output
        self.assertIn("keep this maintainer note", first)
        self.assertIn("compiler flags", first)

    def test_unknown_dependent_condition_does_not_satisfy_missing_policy_flag(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'cmake_dependent_option(WITH_BACKEND "Backend" OFF "TEST_PROBE" ON)\n')
        (self.root / "build-options.toml").write_text('[package]\nprofiles=["WAYLAND"]\n[profiles.WAYLAND]\nflags=["-DWITH_BACKEND=OFF"]\n')
        result = self.run_tool("check", self.snapshot(), ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WITH_BACKEND", result.stdout)

    def test_profile_with_invalid_native_value_cannot_render(self):
        (self.root / "build-options.toml").write_text('''
[package]
profiles = ["WAYLAND"]
[profiles.WAYLAND]
flags = ["-DWITH_X11=banana"]
''')
        result = self.run_tool("render", self.snapshot(), ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("WAYLAND", result.stderr)

    def test_installation_runs_from_an_unrelated_directory(self):
        prefix = self.root / "installed"
        installer = TOOLS / "install"
        self.assertTrue(installer.exists(), "toolkit needs a relocatable installation entry point")
        result = subprocess.run([str(installer), "--prefix", str(prefix)], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in ("waypure-options", "qt6-options", "ffmpeg-options"):
            result = subprocess.run([str(prefix / "bin" / name), "--help"], cwd=self.root,
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_removed_policy_option_blocks_review_but_writes_report(self):
        (self.root / "build-options.toml").write_text('''
[package]
backend = "cmake"
profiles = ["WAYLAND"]
[profiles.WAYLAND]
flags = ["-DWITH_REMOVED_X11=OFF"]
''')
        result = self.run_tool("review", ok=False)
        self.assertNotEqual(result.returncode, 0)
        report = self.root / "options/1.0.review.log"
        self.assertTrue(report.exists(), result.stderr)
        self.assertIn("WITH_REMOVED_X11", report.read_text())

    def test_review_writes_persistent_array_and_compares_previous_release(self):
        self.run_tool("review")
        baseline = self.root / "options/1.0.catalog.json.log"
        before = baseline.read_bytes()
        second = self.root / "src/demo-1.1"
        shutil.copytree(self.source, second)
        cmake = second / "CMakeLists.txt"
        cmake.write_text(cmake.read_text().replace("VERSION 1.0", "VERSION 1.1")
                         + 'option(NEW_FEATURE "New release feature" ON)\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=1.1"))
        self.run_tool("review")
        self.assertEqual(baseline.read_bytes(), before)
        output = self.root / "options/1.1.options.sh.log"
        self.assertTrue(output.exists())
        self.assertIn("_cmake_options=(", output.read_text())
        self.assertIn("+ NEW_FEATURE", (self.root / "options/1.1.review.log").read_text())

    def test_older_source_is_not_used_after_pkgver_bump(self):
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=2.0"))
        result = self.run_tool("review", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("2.0", result.stdout + result.stderr)
        self.assertFalse((self.root / "options/2.0.catalog.json.log").exists())

    @unittest.skipUnless(shutil.which("meson"), "Meson is needed for source introspection")
    def test_meson_collects_project_options_without_configuring(self):
        source = self.root / "meson-demo"
        source.mkdir()
        (source / "meson.build").write_text("project('meson-demo', version: '2.0')\n")
        (source / "meson.options").write_text('''
option('x11', type: 'feature', value: 'auto', description: 'X11 backend')
option('wayland', type: 'boolean', value: true)
option('driver', type: 'combo', choices: ['vulkan', 'software'], value: 'vulkan')
''')
        data = json.loads(self.snapshot(source, backend="meson").read_text())
        self.assertEqual(data["version"], "2.0")
        self.assertEqual(data["options"]["x11"]["default"], "auto")
        self.assertEqual(data["options"]["driver"]["choices"], ["vulkan", "software"])
        self.assertFalse((source / "meson-private").exists())

    @unittest.skipUnless(shutil.which("meson"), "Meson needed")
    def test_meson_subproject_options_keep_their_namespace(self):
        (self.source / "meson.build").write_text("project('meson-demo', version: '1.0')\nsubproject('sub')\n")
        subproject = self.source / "subprojects/sub"
        subproject.mkdir(parents=True)
        (subproject / "meson.build").write_text("project('sub')\n")
        (subproject / "meson.options").write_text("option('foo', type: 'feature', value: 'auto')\n")
        (self.root / "PKGBUILD").write_text('pkgver=1.0\nbuild() {\n local _meson_options=(\n -Dsub:foo=disabled\n )\n}\n')
        (self.root / "build-options.toml").write_text('[package]\nprofiles=["WAYLAND"]\n[profiles.WAYLAND]\nflags=["-Dsub:foo=disabled"]\n')
        inventory = self.snapshot(backend="meson")
        self.assertIn("sub:foo", json.loads(inventory.read_text())["options"])
        self.run_tool("check", inventory)
        self.assertIn("-Dsub:foo=disabled", self.run_tool("render", inventory).stdout)

    @unittest.skipUnless(shutil.which("meson"), "Meson needed")
    def test_meson_array_choices_accept_native_forms_and_reject_invalid_members(self):
        (self.source / "meson.build").write_text("project('meson-demo', version: '1.0')\n")
        (self.source / "meson.options").write_text("option('drivers', type: 'array', choices: ['a', 'b', 'c'], value: ['a'])\n")
        recipe = self.root / "PKGBUILD"
        config = self.root / "build-options.toml"
        inventory = self.snapshot(backend="meson")
        for index, value in enumerate(("a,b", '["a", "b"]', "[]")):
            with self.subTest(value=value):
                native = subprocess.run(["meson", "setup", str(self.root / f"native-{index}"), str(self.source), "-Ddrivers=" + value], text=True, capture_output=True)
                self.assertEqual(native.returncode, 0, native.stdout + native.stderr)
                flag = "-Ddrivers=" + value
                recipe.write_text("pkgver=1.0\nbuild() {\n local _meson_options=(\n '" + flag + "'\n )\n}\n")
                config.write_text('[package]\nprofiles=["DRIVERS"]\n[profiles.DRIVERS]\nflags=[' + json.dumps(flag) + ']\n')
                self.run_tool("check", inventory)
                self.run_tool("render", inventory)
        recipe.write_text('pkgver=1.0\nbuild() {\n local _meson_options=(\n -Ddrivers=a,missing\n )\n}\n')
        config.write_text('[package]\n')
        result = self.run_tool("check", inventory, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stdout)

    def test_autoconf_source_catalogue_tracks_deprecated_option(self):
        source = self.root / "autoconf-demo"
        source.mkdir()
        (source / "configure.ac").write_text('''
AC_INIT([autoconf-demo], [3.0])
AC_ARG_ENABLE([x11], [AS_HELP_STRING([--disable-x11], [Disable X11])], [], [enable_x11=yes])
AC_ARG_WITH([old-driver], [AS_HELP_STRING([--with-old-driver], [Deprecated: use --with-driver])])
''')
        data = json.loads(self.snapshot(source, backend="autoconf").read_text())
        self.assertEqual(data["version"], "3.0")
        self.assertEqual(data["options"]["feature:x11"]["default"], "ON")
        self.assertTrue(data["options"]["with:old-driver"]["deprecated"])

    @unittest.skipUnless(shutil.which("autoconf"), "Autoconf needed for generated configure help")
    def test_autotools_template_directory_arguments_pass_option_check(self):
        source = self.root / "autoconf-demo"
        source.mkdir()
        (source / "configure.ac").write_text('AC_INIT([autoconf-demo], [3.0])\n')
        generated = subprocess.run(["autoconf"], cwd=source, capture_output=True, text=True)
        self.assertEqual(generated.returncode, 0, generated.stderr)
        template = (TOOLS / "templates/autotools.PKGBUILD").read_text()
        recipe = template.replace('_pkgname=\n', '_pkgname=autoconf-demo\n').replace('pkgver=\n', 'pkgver=3.0\n')
        (self.root / "PKGBUILD").write_text(recipe)
        self.run_tool("check", self.snapshot(source, backend="autoconf"))

    def test_legacy_qt_catalogue_can_be_read_without_migrating_it(self):
        source = self.root / "qt-demo"
        source.mkdir()
        (source / ".cmake.conf").write_text('set(QT_REPO_MODULE_VERSION "6.12.0")\n')
        (source / "CMakeLists.txt").write_text('project(QtDemo)\n')
        (source / "configure.cmake").write_text('qt_feature(xlib LABEL Xlib CONDITION X11_FOUND)\n')
        old = self.root / "legacy.json.log"
        result = subprocess.run([str(TOOLS / "qt6-options"), "snapshot", str(source), "-o", str(old)],
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        before = old.read_bytes()
        result = self.run_tool("render", old)
        self.assertIn("FEATURE_xlib", result.stdout)
        self.assertIn("UNKNOWN", result.stdout)
        self.assertEqual(old.read_bytes(), before)

    def test_build_cache_failure_does_not_remove_source_guard(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'option(WITH_PROBE "Detected backend" ${HAVE_BACKEND})\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    -B build -G Ninja', '    -DWITH_PROBE=OFF\n    -B build -G Ninja'))
        build = self.root / "build"
        build.mkdir()
        (build / "CMakeCache.txt").write_text('CMAKE_PROJECT_VERSION:STATIC=1.0\nWITH_PROBE:BOOL=OFF\n')
        output = self.run_tool("render", self.snapshot(), "--prune-redundant", "--build", build).stdout
        line = next(line for line in output.splitlines() if "-DWITH_PROBE=OFF" in line)
        self.assertTrue(line.lstrip().startswith("-D"), line)
        self.assertIn("OBSERVED=OFF", line)

    def test_cmake_cache_choices_are_hints_and_allow_arch_build_type(self):
        path = self.source / "CMakeLists.txt"
        path.write_text(path.read_text() + 'set(CMAKE_BUILD_TYPE "Release" CACHE STRING "Build type")\nset_property(CACHE CMAKE_BUILD_TYPE PROPERTY STRINGS Debug Release)\n')
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    -B build -G Ninja', '    -DCMAKE_BUILD_TYPE=None\n    -DDRIVER=custom\n    -B build -G Ninja'))
        self.run_tool("check", self.snapshot())

    def test_unsafe_version_override_cannot_escape_report_directory(self):
        result = self.run_tool("review", "--source", self.source, "--version", "../escape", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / "escape.catalog.json.log").exists())

    @unittest.skipUnless(shutil.which("meson"), "Meson needed")
    def test_computed_meson_version_uses_pkgver_without_running_version_script(self):
        sentinel = self.root / "must-not-execute-version-script"
        (self.source / "meson.build").write_text("project('computed-demo', version: run_command('sh', '-c', 'touch " + str(sentinel) + "; echo 9.0', check: true).stdout().strip())\n")
        (self.source / "meson.options").write_text("option('wayland', type: 'boolean', value: true)\n")
        (self.root / "build-options.toml").write_text('[package]\nbackend="meson"\nsource="src/demo-1.0"\n')
        (self.root / "PKGBUILD").write_text('pkgver=1.0\nbuild() {\n local _meson_options=(\n -Dwayland=true\n )\n}\n')
        self.run_tool("review")
        data = json.loads((self.root / "options/1.0.catalog.json.log").read_text())
        self.assertEqual(data["version"], "1.0")
        self.assertIn("pkgver", data["version_origin"])
        self.assertFalse(sentinel.exists())

    def test_qt_configure_input_referenced_in_feature_rule_is_recognized(self):
        source = self.root / "qt-demo"
        source.mkdir()
        (source / ".cmake.conf").write_text('set(QT_REPO_MODULE_VERSION "6.12.0")\n')
        (source / "CMakeLists.txt").write_text('project(QtDemo)\n')
        (source / "configure.cmake").write_text("qt_feature(opengl LABEL OpenGL CONDITION INPUT_opengl STREQUAL 'desktop')\n")
        (self.root / "PKGBUILD").write_text('pkgver=6.12.0\nbuild() {\n local _cmake_options=(\n -DINPUT_opengl=desktop\n )\n}\n')
        self.run_tool("check", self.snapshot(source, backend="qt6"))

    def ffmpeg_source(self):
        source = self.root / "ffmpeg-demo"
        source.mkdir()
        (source / "RELEASE").write_text("1.0\n")
        configure = source / "configure"
        configure.write_text('''#!/bin/sh
libxcb_shm_deps="libxcb"
cat <<'HELP'
External libraries:
  --enable-libxcb          enable XCB support
  --enable-libxcb-shm      enable XCB shared memory
Toolchain options:
  --extra-cflags=CFLAGS    add compiler flags
HELP
''')
        configure.chmod(0o755)
        recipe = self.root / "PKGBUILD"
        recipe.write_text('''pkgver=1.0
build() {
  local _make_options=(
    --disable-libxcb # WAYLAND
    --disable-libxcb-shm # WAYLAND
    --extra-cflags="-O2 -pipe"
  )
}
''')
        return source

    def test_ffmpeg_repeated_snapshot_is_unchanged(self):
        source = self.ffmpeg_source()
        inventory = self.snapshot(source, backend="ffmpeg")
        self.run_tool("snapshot", source, "--backend", "ffmpeg", "-o", inventory)

    def test_ffmpeg_preserves_quotes_in_generated_array(self):
        inventory = self.snapshot(self.ffmpeg_source(), backend="ffmpeg")
        output = self.run_tool("render", inventory, "--prune-redundant").stdout
        values = subprocess.check_output(["bash", "-c", output + '\nprintf "%s\\0" "${_make_options[@]}"'], text=True).split("\0")
        self.assertIn("--extra-cflags=-O2 -pipe", values)
        self.assertIn("# --disable-libxcb-shm", output)

    def test_ffmpeg_component_changes_remain_part_of_release_diff(self):
        source = self.ffmpeg_source()
        configure = source / "configure"
        text = configure.read_text().replace('libxcb_shm_deps="libxcb"', '''libxcb_shm_deps="libxcb"
if [ "$1" = --list-decoders ]; then printf 'alpha\\n'; exit; fi''')
        text = text.replace('External libraries:', 'Component lists:\n  --list-decoders          list decoders\nExternal libraries:')
        configure.write_text(text)
        old = self.snapshot(source, backend="ffmpeg")
        configure.write_text(text.replace("printf 'alpha", "printf 'beta"))
        new = self.snapshot(source, filename="components.json.log", backend="ffmpeg")
        output = self.run_tool("diff", old, new).stdout
        self.assertIn("alpha", output)
        self.assertIn("beta", output)

    def test_ffmpeg_missing_component_pattern_fails_check(self):
        source = self.ffmpeg_source()
        configure = source / "configure"
        text = configure.read_text().replace('libxcb_shm_deps="libxcb"', '''libxcb_shm_deps="libxcb"
if [ "$1" = --list-decoders ]; then printf 'alpha\\n'; exit; fi''')
        text = text.replace('External libraries:', 'Component lists:\n  --list-decoders          list decoders\n  --enable-decoder=NAME    enable decoder\nExternal libraries:')
        configure.write_text(text)
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    --disable-libxcb # WAYLAND', '    --enable-decoder=missing\n    --disable-libxcb # WAYLAND'))
        result = self.run_tool("check", self.snapshot(source, backend="ffmpeg"), ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("missing", result.stdout)

    def test_ffmpeg_component_profiles_retain_enable_disable_polarity(self):
        source = self.ffmpeg_source()
        configure = source / "configure"
        configure.write_text(configure.read_text().replace('libxcb_shm_deps="libxcb"', '''libxcb_shm_deps="libxcb"
if [ "$1" = --list-decoders ]; then printf 'h264\\n'; exit; fi''').replace('External libraries:',
            'Component lists:\n  --list-decoders          list decoders\n  --enable-decoder=NAME    enable decoder\n  --disable-decoder=NAME   disable decoder\nExternal libraries:'))
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace('    --disable-libxcb # WAYLAND', '    --disable-decoder=h264\n    --disable-libxcb # WAYLAND'))
        (self.root / "build-options.toml").write_text('[package]\nprofiles=["DECODERS"]\n[profiles.DECODERS]\nflags=["--enable-decoder=h264"]\n')
        inventory = self.snapshot(source, backend="ffmpeg")
        result = self.run_tool("check", inventory, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("DECODERS", result.stdout)
        output = self.run_tool("render", inventory).stdout
        values = subprocess.check_output(["bash", "-c", output + '\nprintf "%s\\0" "${_make_options[@]}"'], text=True).split("\0")
        self.assertIn("--enable-decoder=h264", values)
        self.assertNotIn("--disable-decoder=h264", values)
        recipe.write_text("pkgver=1.0\nbuild() {\n" + output + "\n}\n")
        self.run_tool("check", inventory)

    def test_qt_forced_on_feature_with_false_condition_fails_check(self):
        source = self.root / "qt-demo"
        source.mkdir()
        (source / ".cmake.conf").write_text('set(QT_REPO_MODULE_VERSION "6.12.0")\n')
        (source / "CMakeLists.txt").write_text('project(QtDemo)\n')
        (source / "configure.cmake").write_text('qt_feature(root LABEL Root)\nqt_feature(child LABEL Child CONDITION QT_FEATURE_root)\n')
        (self.root / "PKGBUILD").write_text('pkgver=6.12.0\nbuild() {\n local _cmake_options=(\n -DFEATURE_root=OFF\n -DFEATURE_child=ON\n )\n}\n')
        result = self.run_tool("check", self.snapshot(source, backend="qt6"), ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FEATURE_child", result.stdout)


if __name__ == "__main__":
    unittest.main()
