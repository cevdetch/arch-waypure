"""Run with python -B -m unittest discover -s tools/tests -v."""

import contextlib
import io
import itertools
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]
REPO = TOOLS.parent
sys.path.insert(0, str(TOOLS))
from optionkit import qt6 as qt


def selection(**values):
    result = qt.Selection()
    result.update({name: {"value": value, "comment": "", "raw": f"-D{name}={value}"}
                   for name, value in values.items()})
    return result


class SourceCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.source = self.directory / "qtdemo-6.12.0"
        self.source.mkdir()
        (self.source / ".cmake.conf").write_text('set(QT_REPO_MODULE_VERSION "6.12.0")\n')
        (self.source / "CMakeLists.txt").write_text('project(QtDemo VERSION 6.12.0)\n')

    def capture(self, text):
        (self.source / "configure.cmake").write_text(text)
        return qt.capture(self.source)

    def test_lexer_and_conditional_declarations(self):
        data = self.capture('''
#[=[ qt_feature(fake CONDITION OFF) ]=]
set(unrelated [==[qt_feature(also_fake CONDITION OFF)]==])
qt_feature(root LABEL "Root (display) # text")
if(WIN32)
    qt_feature(platform LABEL Windows)
else()
    qt_feature(platform LABEL Unix)
endif()
function(setup)
    qt_feature(not_called LABEL Hidden)
    option(QT_BUILD_TESTS "Build tests" OFF)
endfunction()
option(QT_BUILD_PROJECT_${module} "Generated" ON)
''')
        self.assertEqual(set(data["features"]), {"root", "platform"})
        self.assertTrue(qt.visible(data, "platform"))
        self.assertIsNone(qt.Model(data).feature("platform").value)
        self.assertIn("QT_BUILD_TESTS", data["options"])
        self.assertNotIn("QT_BUILD_PROJECT_${module}", data["options"])
        self.assertEqual(data["unresolved"][0]["kind"], "option")

    def test_tarball_and_tree_have_same_inventory(self):
        expected = self.capture('qt_feature(root LABEL Root CONDITION ON)\n')
        archive = self.directory / "source.tar.xz"
        with tarfile.open(archive, "w:xz") as stream:
            stream.add(self.source, arcname=self.source.name)
        self.assertEqual(qt.capture(archive), expected)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()),
                         ["qtdemo-6.12.0", "source.tar.xz"])

    def test_unsafe_archive_path_rejected(self):
        archive = self.directory / "unsafe.tar"
        with tarfile.open(archive, "w") as stream:
            member = tarfile.TarInfo("../escaped.cmake")
            member.size = 3
            stream.addfile(member, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(ValueError, "unsafe archive"):
            qt.capture(archive)

    def test_transitive_off_and_emission(self):
        data = self.capture('''
qt_feature(root LABEL Root)
qt_feature(child LABEL Child CONDITION QT_FEATURE_root AND TEST_child)
qt_feature(grandchild LABEL Grandchild CONDITION QT_FEATURE_child AND TARGET Unknown::Target)
qt_feature(emitted LABEL Emitted EMIT_IF QT_FEATURE_root)
''')
        model = qt.Model(data, selection(FEATURE_root="OFF", FEATURE_emitted="ON"))
        for name in ("root", "child", "grandchild", "emitted"):
            self.assertIs(model.feature(name).value, False, name)
        self.assertIn("FEATURE_root=OFF", qt.proof_text(model.feature("grandchild")))

    def test_unknown_enable_cannot_prove_off(self):
        data = self.capture('''
qt_feature(root LABEL Root)
qt_feature(child LABEL Child CONDITION QT_FEATURE_root ENABLE TEST_enable)
qt_feature(disabled LABEL Disabled CONDITION ON ENABLE ON DISABLE ON)
''')
        model = qt.Model(data, selection(FEATURE_root="OFF"))
        self.assertIsNone(model.feature("child").value)
        self.assertIs(model.feature("disabled").value, False)

    def test_cmake_feature_assignment_prevents_default_only_removal(self):
        data = self.capture('''
function(setup_features)
    set(FEATURE_custom ${computed} CACHE BOOL "Custom")
endfunction()
qt_feature(custom LABEL Custom AUTODETECT OFF)
''')
        choices = selection(FEATURE_custom="OFF")
        self.assertIsNone(qt.Model(data).feature("custom").value)
        self.assertEqual(qt.reduced_choices(data, choices)[1], {})
        self.assertIs(qt.Model(data, choices).feature("custom").value, False)

    def test_system_library_force_rules(self):
        data = self.capture('''
qt_feature(force_system_libs LABEL System AUTODETECT OFF)
qt_feature(force_bundled_libs LABEL Bundled AUTODETECT OFF)
qt_feature(system_library SYSTEM_LIBRARY LABEL Library CONDITION OFF)
''')
        model = qt.Model(data, selection(FEATURE_force_system_libs="ON"))
        self.assertIs(model.feature("system_library").value, True)
        model = qt.Model(data, selection(FEATURE_force_system_libs="ON", FEATURE_force_bundled_libs="ON"))
        self.assertIs(model.feature("system_library").value, False)

    def test_input_defaults_and_selected_override(self):
        data = self.capture('''
set(INPUT_codec "undefined" CACHE STRING "Codec")
set_property(CACHE INPUT_codec PROPERTY STRINGS undefined no system)
qt_feature(codec LABEL Codec CONDITION NOT INPUT_codec STREQUAL 'no')
''')
        self.assertIs(qt.Model(data).feature("codec").value, True)
        self.assertIs(qt.Model(data, selection(INPUT_codec="no")).feature("codec").value, False)
        self.assertEqual(data["options"]["INPUT_codec"]["values"], ["undefined", "no", "system"])

    def test_mutually_dependent_offs_keep_a_root(self):
        data = self.capture('''
qt_feature(a LABEL A CONDITION QT_FEATURE_b)
qt_feature(b LABEL B CONDITION QT_FEATURE_a)
''')
        remaining, removed = qt.reduced_choices(data, selection(FEATURE_a="OFF", FEATURE_b="OFF"))
        self.assertEqual(len(removed), 1)
        self.assertEqual(len(remaining), 1)
        for name in removed:
            self.assertIs(qt.Model(data, remaining).feature(name[8:]).value, False)

    def test_detection_failure_cannot_justify_removing_guard(self):
        data = self.capture('qt_feature(xlib LABEL Xlib CONDITION X11_FOUND)\n')
        choices = selection(FEATURE_xlib="OFF")
        observed = qt.Model(data, {}, {"X11_FOUND": "OFF"})
        self.assertIs(observed.feature("xlib").value, False)
        self.assertIsNone(qt.Model(data).feature("xlib").value)
        self.assertEqual(qt.reduced_choices(data, choices)[1], {})

    def test_alias_inputs_change_the_primary_feature(self):
        data = self.capture('''
qt_feature(primary LABEL Primary AUTODETECT OFF)
qt_feature_alias(opposite ALIAS_OF_FEATURE primary NEGATE)
qt_feature(child LABEL Child CONDITION QT_FEATURE_primary)
''')
        choices = selection(FEATURE_opposite="OFF", FEATURE_child="OFF")
        self.assertIs(qt.Model(data, choices).feature("primary").value, True)
        self.assertNotIn("FEATURE_child", qt.reduced_choices(data, choices)[1])

    def test_nested_alias_inputs_and_linux_filter(self):
        data = self.capture('''
qt_feature(primary LABEL Primary AUTODETECT OFF)
qt_feature_alias(opposite ALIAS_OF_FEATURE primary NEGATE)
qt_feature_alias(second ALIAS_OF_FEATURE opposite NEGATE)
qt_feature(child LABEL Child CONDITION QT_FEATURE_primary)
qt_feature(windows_parent LABEL Windows CONDITION WIN32)
qt_feature(windows_child LABEL Child CONDITION QT_FEATURE_windows_parent AND TEST_probe)
''')
        choices = selection(FEATURE_second="ON", FEATURE_child="OFF")
        self.assertIs(qt.Model(data, choices).feature("primary").value, True)
        self.assertNotIn("FEATURE_child", qt.reduced_choices(data, choices)[1])
        self.assertFalse(qt.visible(data, "windows_child"))
        self.assertTrue(qt.visible(data, "windows_child", include_all=True))
        self.assertTrue(qt.visible(data, "primary"))

    def test_dummy_project_does_not_set_module_name(self):
        (self.source / "CMakeLists.txt").write_text('project(QtDemoDummy)\nproject(QtDemo VERSION 6.12.0)\n')
        self.assertEqual(self.capture('qt_feature(root LABEL Root)\n')["module"], "qtdemo")

    def test_pkgbuild_is_text_and_render_preserves_bash(self):
        data = self.capture('qt_feature(root LABEL Root)\n')
        sentinel = self.directory / "must-not-exist"
        recipe = self.directory / "PKGBUILD"
        recipe.write_text(f'''touch '{sentinel}'
build() {{
  local _cmake_options=(
    -B build
    # -D QT_COPYRIGHT:STRING=Copyright (C) Qt and contributors.
    # -DQT_FEATURE_ incomplete comment
    '-DCMAKE_CXX_FLAGS=-O2 -DVALUE=1'
    -DFEATURE_root="${{dynamic}}" # USER
  )
}}
''')
        choices = qt.selected_options(recipe)
        self.assertFalse(sentinel.exists())
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qt.render(data, choices)
        rendered = output.getvalue()
        self.assertIn('-DFEATURE_root="${dynamic}" # USER', rendered)
        self.assertIn("'-DCMAKE_CXX_FLAGS=-O2 -DVALUE=1'", rendered)
        self.assertIn("-B build", rendered)
        result = subprocess.run(["bash", "-n"], input=rendered, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_multiple_assignments_rejected(self):
        recipe = self.directory / "PKGBUILD"
        recipe.write_text('_cmake_options=(\n -DFEATURE_a=OFF -DFEATURE_b=OFF\n)\n')
        with self.assertRaisesRegex(ValueError, "one -D assignment"):
            qt.selected_options(recipe)

    def test_immutable_snapshot(self):
        data = self.capture('qt_feature(root LABEL Root)\n')
        output = self.directory / "snapshot.json.log"
        with contextlib.redirect_stdout(io.StringIO()):
            qt.write_snapshot(data, output)
            before = output.stat().st_mtime_ns
            qt.write_snapshot(data, output)
        self.assertEqual(output.stat().st_mtime_ns, before)
        data["version"] = "6.12.1"
        with self.assertRaisesRegex(ValueError, "already exists"):
            qt.write_snapshot(data, output)

    def test_diff_ignores_line_moves_and_reports_rule_changes(self):
        old = self.capture('qt_feature(root LABEL Root CONDITION TEST_old)\n')
        new = self.capture('\n\nqt_feature(root LABEL Root CONDITION TEST_old)\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qt.compare(old, new)
        self.assertIn("No relevant option/default/dependency-rule changes", output.getvalue())
        new = self.capture('qt_feature(root LABEL Root CONDITION TEST_new)\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            qt.compare(old, new)
        self.assertIn("CONDITION: TEST_old -> TEST_new", output.getvalue())

    def test_cache_version_mismatch_rejected(self):
        data = self.capture('qt_feature(root LABEL Root)\n')
        cache = self.directory / "CMakeCache.txt"
        cache.write_text('CMAKE_PROJECT_NAME:STATIC=QtDemo\nCMAKE_PROJECT_VERSION:STATIC=6.11.1\n')
        with self.assertRaisesRegex(ValueError, "build is 6.11.1"):
            qt.read_cache(cache, data)

    def test_internal_feature_settings_reported(self):
        data = self.capture('qt_feature(root LABEL Root)\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            failed = qt.check(data, selection(QT_FEATURE_root="OFF"), {})
        self.assertTrue(failed)
        self.assertIn("internal Qt state; use FEATURE_root", output.getvalue())
        self.assertNotIn("OFF settings that can be omitted", output.getvalue())
        with self.assertRaisesRegex(ValueError, "replace QT_FEATURE"):
            qt.reduced_choices(data, selection(QT_FEATURE_root="OFF"))

    def test_inherited_cache_feature_is_not_an_unknown_module_feature(self):
        data = self.capture('qt_feature(root LABEL Root)\n')
        cache = self.directory / "CMakeCache.txt"
        cache.write_text('//Qt feature: pkg_config (from target Qt6::Core)\nQT_FEATURE_pkg_config:INTERNAL=ON\n')
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            failed = qt.check(data, selection(FEATURE_pkg_config="ON"), qt.read_cache(cache))
        self.assertFalse(failed)
        self.assertIn("inherited from Qt6::Core", output.getvalue())


class ExpressionTests(unittest.TestCase):
    def test_unknowns_stay_unknown_without_decisive_operand(self):
        model = qt.Model({"features": {}, "options": {}})
        for text, expected in (("TEST_missing", None), ("TEST_missing AND OFF", False),
                               ("TEST_missing OR ON", True), ("TEST_missing AND ON", None),
                               ("TARGET Missing::Lib", None), ("UNSUPPORTED MATCHES .", None)):
            self.assertIs(model.expression(text.split()).value, expected, text)

    def test_qt_and_cmake_boolean_precedence(self):
        model = qt.Model({"features": {}, "options": {}})
        self.assertIs(model.expression("OFF AND ON OR ON".split()).value, False)
        self.assertIs(model.expression("OFF AND ON OR ON".split(), cmake=True).value, True)
        self.assertIs(model.expression("ON OR OFF AND OFF".split(), cmake=True).value, True)

    def test_against_upstream_qt_evaluator(self):
        base = Path(os.environ.get("QTBASE_SOURCE", REPO / "qt6-base/src/qtbase-6.12.0"))
        engine = base / "cmake/QtFeature.cmake"
        if not engine.is_file() or not shutil.which("cmake"):
            self.skipTest("set QTBASE_SOURCE to run Qt's own evaluator as an oracle")
        expressions = ["A AND B OR C", "A OR B AND C", "NOT A AND B OR C",
                       "( A AND B ) OR C", "A AND ( B OR NOT C )", "NOT ( A OR B ) AND C"]
        script = ["cmake_minimum_required(VERSION 3.22)", f'include("{engine}")']
        expected = []
        for a, b, c in itertools.product(("OFF", "ON"), repeat=3):
            choices = selection(A=a, B=b, C=c)
            script.extend(f"set({name} {value['value']})" for name, value in choices.items())
            model = qt.Model({"features": {}, "options": {}}, choices)
            for expression in expressions:
                # Qt expects parentheses as list tokens, not function-call nesting.
                arguments = ";".join(expression.split())
                script.extend((f'qt_evaluate_config_expression(result "{arguments}")',
                               'message(STATUS "RESULT=${result}")'))
                expected.append(model.expression(expression.split()).value)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "oracle.cmake"
            path.write_text("\n".join(script) + "\n")
            result = subprocess.run(["cmake", "-P", str(path)], cwd=directory,
                                    text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        actual = [line.partition("RESULT=")[2] == "ON" for line in result.stdout.splitlines()
                  if "RESULT=" in line]
        self.assertEqual(actual, expected)


if __name__ == "__main__":
    unittest.main()
