"""Regressions from real package reviews, using native build-system fixtures."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]


class CollectorGapsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / 'src/demo-1.0'
        self.source.mkdir(parents=True)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        wrapper = self.bin / 'arch-meson'
        wrapper.write_text('#!/bin/sh\nexec meson setup --auto-features enabled "$@"\n')
        wrapper.chmod(0o755)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1',
                    'PATH': str(self.bin) + os.pathsep + os.environ['PATH']}

    def run_tool(self, *arguments, expected=0):
        result = subprocess.run([str(TOOLS / 'waypure-options'), *map(str, arguments)],
                                cwd=self.root, text=True, capture_output=True,
                                env=self.env, timeout=30)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result

    def meson(self, selected='', wrapper='arch-meson'):
        (self.source / 'meson.build').write_text("project('Demo', version: '1.0')\n")
        (self.source / 'meson.options').write_text(
            "option('x11', type: 'feature', value: 'auto')\n"
            "option('required_feature', type: 'feature', value: 'enabled')\n"
            "option('boolean_feature', type: 'boolean', value: true)\n")
        (self.root / 'PKGBUILD').write_text(
            'pkgver=1.0\nbuild() {\n local _meson_options=(\n' + selected
            + '\n )\n ' + wrapper + ' "demo-$pkgver" build "${_meson_options[@]}"\n}\n')

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_arch_meson_default_explains_auto_feature_as_enabled(self):
        self.meson()
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        line = next(line for line in output.splitlines() if '-Dx11=' in line)
        self.assertIn('-Dx11=enabled', line)
        self.assertIn('arch-meson', line)
        self.assertIn('auto_features=enabled', line)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_long_auto_features_overrides_wrapper_and_preserves_native_argv(self):
        for flag in ('--auto-features=disabled', '--auto-features disabled', '-Dauto_features=disabled'):
            with self.subTest(flag=flag):
                self.meson(' ' + flag, wrapper='meson setup' if flag.startswith('-D') else 'arch-meson')
                self.run_tool('review')
                output = self.run_tool('render', '1.0').stdout
                line = next(line for line in output.splitlines() if '-Dx11=' in line)
                self.assertIn('-Dx11=disabled', line)
                self.assertIn('IMPLIED OFF', line)
                self.assertIn('auto_features=disabled', line)
                self.assertIn('-Dboolean_feature=true', output)
                self.assertIn('-Drequired_feature=enabled', output)
                self.assertIn(flag, output)
                self.run_tool('check', '1.0')

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_native_meson_wrapper_rejects_mixed_auto_feature_spellings(self):
        self.meson(' -Dauto_features=disabled')
        result = subprocess.run([str(self.bin/'arch-meson'), str(self.root/'native'), str(self.source), '-Dauto_features=disabled'],
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        review = self.run_tool('review', expected=1)
        self.assertIn('both -Dauto_features and --auto-features', review.stdout)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_native_meson_agrees_with_auto_feature_explanation(self):
        self.meson(' --auto-features=disabled')
        build_file = self.source / 'meson.build'
        build_file.write_text(build_file.read_text() +
                              "configure_file(output: 'effective.h', configuration: {'X_OFF': get_option('x11').disabled(), 'BOOL_ON': get_option('boolean_feature')})\n")
        subprocess.run(['meson', 'setup', str(self.root / 'native'), str(self.source),
                        '--auto-features=disabled'], check=True, capture_output=True, text=True)
        native = (self.root / 'native/effective.h').read_text()
        self.assertIn('#define X_OFF', native)
        self.assertIn('#define BOOL_ON', native)
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        self.assertIn('# -Dx11=disabled # [IMPLIED OFF]', output)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_auto_features_aliases_cannot_hide_conflicting_duplicates(self):
        self.meson(' --auto-features=disabled\n -Dauto_features=enabled')
        result = self.run_tool('review', expected=1)
        self.assertIn('duplicate setting auto_features', result.stderr)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_unknown_wrapper_is_not_executed_or_used_as_a_dependency_proof(self):
        self.meson()
        sentinel = self.root / 'executed'
        (self.bin / 'arch-meson').write_text('#!/bin/sh\ntouch "' + str(sentinel) + '"\n')
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        line = next(line for line in output.splitlines() if 'x11=' in line)
        self.assertIn('UNKNOWN', line)
        self.assertFalse(sentinel.exists())

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_wrapper_argument_mutation_cannot_supply_a_verified_default(self):
        self.meson()
        wrapper = self.bin/'arch-meson'
        wrapper.write_text('#!/bin/sh\nset -- "$@" --auto-features=enabled\nexec meson setup --auto-features disabled "$@"\n')
        path = self.source/'meson.build'
        path.write_text(path.read_text() + "configure_file(output: 'effective.h', configuration: {'X_ON': get_option('x11').enabled()})\n")
        subprocess.run([str(wrapper), str(self.root/'native'), str(self.source)],
                       check=True, capture_output=True, text=True)
        self.assertIn('#define X_ON', (self.root/'native/effective.h').read_text())
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        line = next(line for line in output.splitlines() if 'x11=' in line)
        self.assertIn('UNKNOWN', line)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_arch_source_validation_preamble_preserves_literal_defaults(self):
        self.meson()
        (self.bin/'arch-meson').write_text('''#!/bin/bash -e
# Arch's source validation does not change positional arguments.
for arg in "$@" .; do
    if [[ $arg == [^-]* && -d $arg && -f $arg/meson.build ]]; then
        arch-meson-check-wraps "$arg"
        break;
    fi
done
set -x
exec meson setup --auto-features enabled "$@"
''')
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        self.assertIn('-Dx11=enabled # [IMPLIED ON] arch-meson: auto_features=enabled', output)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_unquoted_wrapper_argv_forwarding_is_unknown(self):
        self.meson()
        (self.bin/'arch-meson').write_text('#!/bin/sh\nexec meson setup --auto-features enabled $@\n')
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        line = next(line for line in output.splitlines() if 'x11=' in line)
        self.assertIn('UNKNOWN', line)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_subproject_deprecation_does_not_mark_the_root_option(self):
        self.meson(' -Dx11=disabled')
        child = self.source / 'subprojects/child'
        child.mkdir(parents=True)
        (child / 'meson.build').write_text("project('Child', version: '1.0')\n")
        (child / 'meson.options').write_text(
            "option('x11', type: 'feature', value: 'auto', deprecated: 'next_backend')\n"
            "option('next_backend', type: 'feature', value: 'auto')\n")
        path = self.source / 'meson.build'
        path.write_text(path.read_text() + "subproject('child')\n")
        self.run_tool('review', '--strict')
        data = json.loads((self.root / 'options/1.0.catalog.json.log').read_text())
        self.assertFalse(data['options']['x11']['deprecated'])
        self.assertTrue(data['options']['child:x11']['deprecated'])

    def cmake(self, content, flags):
        (self.source / 'CMakeLists.txt').write_text(
            'cmake_minimum_required(VERSION 3.22)\nproject(Demo VERSION 1.0 LANGUAGES NONE)\n' + content)
        (self.root / 'PKGBUILD').write_text('pkgver=1.0\nbuild() {\n local _cmake_options=(\n'
                                            + flags + '\n )\n cmake -S "demo-$pkgver" "${_cmake_options[@]}"\n}\n')

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_verified_sdl_style_wrappers_match_native_cache_and_pruning(self):
        self.cmake('''include(cmake/options.cmake)
set_option(WITH_ROOT "Root" ON)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
option_string(ASSERT_MODE "Assertions" auto)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF\n -DASSERT_MODE=disabled')
        directory = self.source / 'cmake'
        directory.mkdir()
        (directory / 'options.cmake').write_text('''include(CMakeDependentOption)
macro(add_to_alloptions _NEWNAME)
 list(APPEND ALLOPTIONS ${_NEWNAME})
endmacro()
macro(set_option _NAME _DESC)
 add_to_alloptions(${_NAME})
 if(${ARGC} EQUAL 3)
  set(_DEFLT ${ARGV2})
 else()
  set(_DEFLT OFF)
 endif()
 option(${_NAME} ${_DESC} ${_DEFLT})
endmacro()
macro(dep_option _NAME _DESC _DEFLT _DEPTEST _FAILDFLT)
 add_to_alloptions("${_NAME}")
 cmake_dependent_option("${_NAME}" "${_DESC}" "${_DEFLT}" "${_DEPTEST}" "${_FAILDFLT}")
endmacro()
macro(option_string _NAME _DESC _VALUE)
 add_to_alloptions(${_NAME})
 set(${_NAME} ${_VALUE} CACHE STRING "${_DESC}")
 set(HAVE_${_NAME} ${_VALUE})
endmacro()
''')
        self.run_tool('review')
        output = self.run_tool('render', '1.0', '--prune-redundant').stdout
        self.assertIn('# -DWITH_CHILD=OFF # [REDUNDANT]', output)
        self.assertIn('-DASSERT_MODE=disabled', output)
        for label, flags in (('original', ['-DWITH_ROOT=OFF','-DWITH_CHILD=OFF','-DASSERT_MODE=disabled']),
                             ('proposal', subprocess.check_output(['bash','-c',output + '\nprintf "%s\\0" "${_cmake_options[@]}"'],text=True).split('\0')[:-1])):
            build = self.root / label
            subprocess.run(['cmake','-S',str(self.source),'-B',str(build),*flags],check=True,capture_output=True,text=True)
            cache = (build/'CMakeCache.txt').read_text()
            self.assertIn('ASSERT_MODE:STRING=disabled', cache)
            self.assertNotIn('WITH_CHILD:BOOL=ON', cache)

    def test_macro_name_alone_does_not_authorize_pruning(self):
        self.cmake('''macro(dep_option NAME DESC DEFAULT CONDITION FALLBACK)
 option(${NAME} "${DESC}" ${DEFAULT})
 set(${NAME} ON CACHE BOOL "Forced" FORCE)
endmacro()
option(WITH_ROOT "Root" OFF)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF')
        output = self.run_tool('review', expected=1)
        self.assertIn('WITH_CHILD', output.stdout)
        rendered = (self.root/'options/1.0.options.sh.log').read_text()
        self.assertIn('\n  -DWITH_CHILD=OFF', rendered)

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_unquoted_wrapper_condition_cannot_prune_a_required_off_flag(self):
        self.cmake('''include(CMakeDependentOption)
macro(dep_option NAME DESC DEFAULT CONDITION FALLBACK)
 cmake_dependent_option(${NAME} "${DESC}" ${DEFAULT} ${CONDITION} ${FALLBACK})
endmacro()
option(WITH_ROOT "Root" ON)
option(WITH_OTHER "Other" OFF)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT;WITH_OTHER" OFF)
''', ' -DWITH_ROOT=ON\n -DWITH_OTHER=OFF\n -DWITH_CHILD=OFF')
        result = subprocess.run([str(TOOLS/'waypure-options'), 'review'], cwd=self.root,
                                text=True, capture_output=True, env=self.env, timeout=30)
        self.assertIn(result.returncode, (0, 1), result.stderr)
        output = self.run_tool('render', '1.0', '--prune-redundant').stdout
        original = ['-DWITH_ROOT=ON', '-DWITH_OTHER=OFF', '-DWITH_CHILD=OFF']
        proposed = subprocess.check_output(['bash', '-c', output + '\nprintf "%s\\0" "${_cmake_options[@]}"'], text=True).split('\0')[:-1]
        for label, flags in (('original', original), ('proposal', proposed)):
            build = self.root/label
            subprocess.run(['cmake', '-S', str(self.source), '-B', str(build), *flags],
                           check=True, capture_output=True, text=True)
            self.assertIn('WITH_CHILD:BOOL=OFF', (build/'CMakeCache.txt').read_text())

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_literal_dollar_placeholders_cannot_authorize_wrapper_pruning(self):
        for index, condition in enumerate(('[=[${CONDITION}]=]', r'"\${CONDITION}"')):
            with self.subTest(condition=condition):
                self.cmake('''include(CMakeDependentOption)
macro(dep_option NAME DESC DEFAULT CONDITION FALLBACK)
 cmake_dependent_option("${NAME}" "${DESC}" "${DEFAULT}" ''' + condition + ''' "${FALLBACK}")
endmacro()
option(WITH_ROOT "Root" ON)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF')
                subprocess.run([str(TOOLS/'waypure-options'), 'review'], cwd=self.root,
                               text=True, capture_output=True, env=self.env, timeout=30)
                output = self.run_tool('render', '1.0', '--prune-redundant').stdout
                self.assertIn('\n  -DWITH_CHILD=OFF', output)
                if index:
                    # Escaped dollars are also literal, but this dependent
                    # condition is rejected by native CMake rather than ON.
                    continue
                proposed = subprocess.check_output(['bash', '-c', output + '\nprintf "%s\\0" "${_cmake_options[@]}"'], text=True).split('\0')[:-1]
                for label, flags in (('original', ['-DWITH_ROOT=OFF', '-DWITH_CHILD=OFF']), ('proposal', proposed)):
                    build = self.root/(label + str(index))
                    subprocess.run(['cmake', '-S', str(self.source), '-B', str(build), *flags],
                                   check=True, capture_output=True, text=True)
                    self.assertIn('WITH_CHILD:BOOL=OFF', (build/'CMakeCache.txt').read_text())

    def test_dynamic_wrapper_default_keeps_option_available_and_unresolved(self):
        self.cmake('''macro(set_option NAME DESC DEFAULT)
 option(${NAME} "${DESC}" ${DEFAULT})
endmacro()
set_option(WITH_LIBC "Use system C library" ${UNIX})
''', ' -DWITH_LIBC=OFF')
        self.run_tool('review')
        output = self.run_tool('render', '1.0', '--prune-redundant').stdout
        self.assertIn('\n  -DWITH_LIBC=OFF', output)
        self.assertIn('effective value UNKNOWN', output)

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_registration_helper_writes_cannot_hide_a_dependency_change(self):
        self.cmake('''include(CMakeDependentOption)
macro(register_option NAME)
 list(APPEND WITH_ROOT ${NAME})
endmacro()
macro(dep_option NAME DESC DEFAULT CONDITION FALLBACK)
 register_option(${NAME})
 cmake_dependent_option("${NAME}" "${DESC}" "${DEFAULT}" "${CONDITION}" "${FALLBACK}")
endmacro()
option(WITH_ROOT "Root" OFF)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF')
        self.run_tool('review')
        output = self.run_tool('render', '1.0', '--prune-redundant').stdout
        proposed = subprocess.check_output(['bash', '-c', output + '\nprintf "%s\\0" "${_cmake_options[@]}"'], text=True).split('\0')[:-1]
        for label, flags in (('original', ['-DWITH_ROOT=OFF', '-DWITH_CHILD=OFF']), ('proposal', proposed)):
            build = self.root/label
            subprocess.run(['cmake', '-S', str(self.source), '-B', str(build), *flags],
                           check=True, capture_output=True, text=True)
            self.assertIn('WITH_CHILD:BOOL=OFF', (build/'CMakeCache.txt').read_text())

    def test_registration_helper_source_changes_invalidate_cached_proofs(self):
        self.cmake('''include(cmake/helper.cmake)
include(cmake/wrappers.cmake)
option(WITH_ROOT "Root" ON)
dep_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF')
        directory = self.source/'cmake'
        directory.mkdir()
        (directory/'helper.cmake').write_text('macro(register_option NAME)\n list(APPEND ALLOPTIONS ${NAME})\nendmacro()\n')
        (directory/'wrappers.cmake').write_text('''macro(dep_option NAME DESC DEFAULT CONDITION FALLBACK)
 register_option("${NAME}")
 cmake_dependent_option("${NAME}" "${DESC}" "${DEFAULT}" "${CONDITION}" "${FALLBACK}")
endmacro()
''')
        repo = self.audit_fixture()
        package = repo/'demo'
        source = package/'src/demo-1.0'
        shutil.copytree(self.source, source, dirs_exist_ok=True)
        (package/'PKGBUILD').write_text((self.root/'PKGBUILD').read_text())
        self.run_tool('-C', package, 'review')
        helper = source/'cmake/helper.cmake'
        helper.write_text(helper.read_text().replace('ALLOPTIONS', 'WITH_ROOT'))
        result = subprocess.run([str(TOOLS/'waypure-audit'), '--repo', str(repo), '--json'],
                                text=True, capture_output=True, env=self.env, check=True)
        row = json.loads(result.stdout)['packages'][0]
        self.assertEqual(row['checks']['options'], 'STALE', row['findings'])

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_extra_wrapper_arguments_cannot_discard_helper_writes(self):
        self.cmake('''include(CMakeDependentOption)
macro(register_option NAME)
 list(APPEND WITH_ROOT ${NAME})
endmacro()
macro(set_option NAME DESC DEFAULT)
 register_option(${NAME})
 option(${NAME} "${DESC}" ${DEFAULT})
endmacro()
option(WITH_ROOT "Root" ON)
set_option(WITH_DUMMY "Dummy" ON ignored)
cmake_dependent_option(WITH_CHILD "Child" ON "WITH_ROOT" OFF)
''', ' -DWITH_ROOT=OFF\n -DWITH_CHILD=OFF')
        self.run_tool('review')
        output = self.run_tool('render', '1.0', '--prune-redundant').stdout
        proposed = subprocess.check_output(['bash', '-c', output + '\nprintf "%s\\0" "${_cmake_options[@]}"'], text=True).split('\0')[:-1]
        for label, flags in (('original', ['-DWITH_ROOT=OFF', '-DWITH_CHILD=OFF']), ('proposal', proposed)):
            build = self.root/label
            subprocess.run(['cmake', '-S', str(self.source), '-B', str(build), *flags],
                           check=True, capture_output=True, text=True)
            self.assertIn('WITH_CHILD:BOOL=OFF', (build/'CMakeCache.txt').read_text())

    def test_verified_subsystem_wrapper_expands_literal_names(self):
        self.cmake('''macro(define_sdl_subsystem _name)
 cmake_parse_arguments("_ds" "" "" "DEPS" ${ARGN})
 string(TOUPPER ${_name} _uname)
 if(NOT DEFINED SDL_${_uname}_DEFAULT)
  set(SDL_${_uname}_DEFAULT ON)
 endif()
 if(_ds_DEPS)
  cmake_dependent_option(SDL_${_uname} "Enable the ${_name} subsystem" "${SDL_${_uname}_DEFAULT}" "${_ds_DEPS}" OFF)
 else()
  option(SDL_${_uname} "Enable the ${_name} subsystem" "${SDL_${_uname}_DEFAULT}")
 endif()
 list(APPEND SDL_SUBSYSTEMS "${_name}")
endmacro()
define_sdl_subsystem(Audio)
define_sdl_subsystem(Render DEPS SDL_VIDEO)
''', ' -DSDL_AUDIO=ON\n -DSDL_RENDER=OFF')
        self.run_tool('review')
        data = json.loads((self.root/'options/1.0.catalog.json.log').read_text())
        self.assertIn('SDL_AUDIO', data['options'])
        self.assertIsNone(data['options']['SDL_AUDIO']['default'])

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_ctest_include_supplies_build_testing_without_project_execution(self):
        sentinel = self.root / 'executed'
        self.cmake('include(CTest)\nfile(WRITE "' + str(sentinel) + '" "executed")\n', ' -DBUILD_TESTING=OFF')
        self.run_tool('review')
        self.assertFalse(sentinel.exists())
        data = json.loads((self.root/'options/1.0.catalog.json.log').read_text())
        self.assertEqual(data['options']['BUILD_TESTING']['default'], 'ON')

    def test_local_ctest_module_cannot_be_assumed_to_declare_build_testing(self):
        self.cmake('list(PREPEND CMAKE_MODULE_PATH "${CMAKE_CURRENT_SOURCE_DIR}/cmake")\ninclude(CTest)\n', ' -DBUILD_TESTING=OFF')
        directory = self.source / 'cmake'
        directory.mkdir()
        (directory/'CTest.cmake').write_text('# custom module has no BUILD_TESTING declaration\n')
        result = self.run_tool('review', expected=1)
        self.assertIn('BUILD_TESTING', result.stdout)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_parentheses_in_description_do_not_hide_a_deprecation(self):
        self.meson(' -Dx11=disabled')
        path = self.source / 'meson.options'
        path.write_text(path.read_text().replace("value: 'auto')", "value: 'auto', description: 'Old backend (legacy)', deprecated: 'required_feature')", 1))
        result = self.run_tool('review', '--strict', expected=1)
        self.assertIn('x11: deprecated', result.stdout)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_value_deprecation_checks_only_the_selected_value(self):
        self.meson(' -Dimage_codec=old')
        path = self.source / 'meson.options'
        path.write_text(path.read_text() + "option('image_codec', type: 'combo', choices: ['old', 'new'], value: 'new', deprecated: {'old': 'new'})\n")
        result = self.run_tool('review', '--strict', expected=1)
        self.assertIn('image_codec', result.stdout)
        self.assertIn('deprecated', result.stdout)
        recipe = self.root / 'PKGBUILD'
        recipe.write_text(recipe.read_text().replace('image_codec=old', 'image_codec=new'))
        self.run_tool('review', '--strict')

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_deprecation_words_inside_help_do_not_deprecate_the_option(self):
        self.meson(' -Dx11=disabled')
        path = self.source / 'meson.options'
        path.write_text(path.read_text().replace("value: 'auto')", "value: 'auto', description: 'Example deprecated: true text')", 1))
        self.run_tool('review', '--strict')

    def autoconf(self, flags):
        (self.source / 'configure.ac').write_text('AC_INIT([Demo], [1.0])\n')
        (self.source / 'configure').write_text("#!/bin/sh\nif test \"$1\" = --help; then\ncat <<'HELP'\n  --enable-present  Build the supported feature\nHELP\nelse\nprintf '%s\\n' \"$@\" > arguments\nfi\n")
        (self.root / 'PKGBUILD').write_text('pkgver=1.0\nbuild() {\n local _make_options=(\n' + flags + '\n )\n ./configure "${_make_options[@]}"\n}\n')

    def test_configure_variable_assignments_remain_literal_array_arguments(self):
        self.autoconf(' --prefix=/usr\n PSDelegate="/path with spaces/gs"\n --enable-present')
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        self.assertIn('PSDelegate="/path with spaces/gs"', output)
        subprocess.run(['bash', '-c', output + '\nsh "' + str(self.source/'configure') + '" "${_make_options[@]}"'],
                       cwd=self.root, check=True, capture_output=True, text=True)
        self.assertIn('PSDelegate=/path with spaces/gs', (self.root/'arguments').read_text().splitlines())

    @unittest.skipUnless(shutil.which('autoconf'), 'Autoconf needed')
    def test_configure_assignment_order_preserves_native_feature_behavior(self):
        self.autoconf('')
        (self.source/'configure.ac').write_text('''AC_INIT([Demo], [1.0])
AC_ARG_ENABLE([present], [AS_HELP_STRING([--enable-present], [Supported feature])], [], [enable_present=yes])
AC_MSG_NOTICE([present=$enable_present])
AC_OUTPUT
''')
        subprocess.run(['autoconf', '--force'], cwd=self.source, check=True, capture_output=True, text=True)
        for flags, effective in (('--enable-present\n enable_present=no', 'no'),
                                 ('enable_present=no\n --enable-present', 'yes')):
            with self.subTest(flags=flags):
                (self.root/'PKGBUILD').write_text('pkgver=1.0\n_make_options=(\n ' + flags + '\n)\n')
                self.run_tool('review')
                output = self.run_tool('render', '1.0', '--prune-redundant').stdout
                for label, array in (('original', '_make_options=(\n ' + flags + '\n)\n'), ('proposal', output)):
                    build = self.root/(label + effective)
                    build.mkdir()
                    result = subprocess.run(['bash', '-c', array + '\nsh "$1" "${_make_options[@]}"', 'bash', str(self.source/'configure')],
                                            cwd=build, check=True, capture_output=True, text=True)
                    self.assertIn('present=' + effective, result.stdout)
                self.assertIn('effective value UNKNOWN', output)

    def test_explicit_configure_array_name_does_not_fall_back_silently(self):
        self.autoconf(' --enable-present')
        result = self.run_tool('review', '--array', '_configure_options', expected=1)
        self.assertIn('_configure_options', result.stderr)

    @unittest.skipUnless(shutil.which('autoconf'), 'Autoconf needed')
    def test_configure_feature_variable_uncertainty_normalizes_punctuation(self):
        self.autoconf(' --enable-present.api\n enable_present_api=no')
        (self.source/'configure.ac').write_text('''AC_INIT([Demo], [1.0])
AC_ARG_ENABLE([present.api], [AS_HELP_STRING([--enable-present.api], [Supported API])])
AC_MSG_NOTICE([present=$enable_present_api])
AC_OUTPUT
''')
        subprocess.run(['autoconf', '--force'], cwd=self.source, check=True, capture_output=True, text=True)
        self.run_tool('review')
        output = self.run_tool('render', '1.0').stdout
        result = subprocess.run(['bash', '-c', output + '\nsh "$1" "${_make_options[@]}"', 'bash', str(self.source/'configure')],
                                cwd=self.root, check=True, capture_output=True, text=True)
        self.assertIn('present=no', result.stdout)
        self.assertIn('effective value UNKNOWN', output)

    def test_complete_configure_help_rejects_a_removed_feature(self):
        self.autoconf(' --enable-removed')
        result = self.run_tool('review', expected=1)
        self.assertIn('feature:removed', result.stdout)
        self.assertIn('absent', result.stdout)

    def test_wrapped_configure_help_still_declares_the_option(self):
        self.autoconf(' --enable-present\n --with-system-codec')
        path = self.source/'configure'
        path.write_text(path.read_text().replace('  --enable-present  Build the supported feature',
                                                '  --enable-present\n                          Build the supported feature\n  --with-system-codec\n                          Use the system codec'))
        self.run_tool('review')

    def test_hidden_configure_input_is_unverified_instead_of_declared_removed(self):
        self.autoconf(' --enable-hidden')
        path = self.source/'configure'
        path.write_text(path.read_text().replace('else\n', 'else\nif test "$enable_hidden" = yes; then :; fi\n'))
        result = self.run_tool('review')
        self.assertIn('unadvertised', result.stdout)

    def test_source_only_autoconf_keeps_unresolved_options_explicit(self):
        self.autoconf(' --enable-generated')
        (self.source/'configure').unlink()
        result = self.run_tool('review')
        self.assertIn('generated configure help may define it', result.stdout)

    def test_building_deprecated_apis_does_not_deprecate_the_configure_option(self):
        self.autoconf(' --enable-deprecated-apis')
        path = self.source/'configure.ac'
        path.write_text(path.read_text() + 'AC_ARG_ENABLE([deprecated-apis], [AS_HELP_STRING([--enable-deprecated-apis], [Build deprecated APIs])], [], [enable_deprecated_apis=yes])\n')
        self.run_tool('review', '--strict')
        path.write_text(path.read_text().replace('Build deprecated APIs', 'Deprecated: use stable-apis'))
        self.run_tool('review', '--strict', expected=1)

    def test_configured_array_and_backend_are_honored_by_review(self):
        self.cmake('option(WITH_WAYLAND "Wayland" OFF)\n', ' -DWITH_WAYLAND=ON')
        (self.source/'configure.ac').write_text('AC_INIT([Other], [1.0])\n')
        recipe = self.root/'PKGBUILD'
        recipe.write_text(recipe.read_text().replace('_cmake_options', '_custom_options'))
        (self.root/'build-options.toml').write_text('[package]\nbackend="cmake"\narray="_custom_options"\n')
        self.run_tool('review')
        self.assertIn('_custom_options=(', (self.root/'options/1.0.options.sh.log').read_text())

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_explicit_source_version_mapping_retains_native_identity_and_observations(self):
        self.cmake('option(WITH_WAYLAND "Wayland" ON)\n', ' -DWITH_WAYLAND=ON')
        path = self.source/'CMakeLists.txt'
        path.write_text(path.read_text().replace('VERSION 1.0 ', 'VERSION 1.0.7 '))
        (self.root/'build-options.toml').write_text('[package]\nsource_version="1.0.7"\n')
        self.run_tool('review')
        data = json.loads((self.root/'options/1.0.catalog.json.log').read_text())
        self.assertEqual(data['source_version'], '1.0.7')
        self.assertEqual(data['version'], '1.0')
        subprocess.run(['cmake', '-S', str(self.source), '-B', str(self.root/'native')],check=True,capture_output=True,text=True)
        self.run_tool('check','1.0','--build',self.root/'native')
        path.write_text(path.read_text().replace('VERSION 1.0.7 ', 'VERSION 1.0.8 '))
        result = self.run_tool('review', expected=1)
        self.assertIn('source_version', result.stderr)

    def test_explicit_version_override_records_the_source_version(self):
        self.cmake('', '')
        output = self.root/'override.json.log'
        self.run_tool('snapshot', self.source, '--version', '1.0.0', '-o', output)
        data = json.loads(output.read_text())
        self.assertEqual(data['source_version'], '1.0')
        self.assertIn('explicit', data['version_origin'].lower())

    @unittest.skipUnless(shutil.which('cmake'), 'CMake needed')
    def test_cmake_package_alias_cannot_admit_another_native_release(self):
        self.cmake('', '')
        path = self.source/'CMakeLists.txt'
        path.write_text(path.read_text().replace('VERSION 1.0 ', 'VERSION 1.0.7 '))
        (self.root/'build-options.toml').write_text('[package]\nsource_version="1.0.7"\n')
        self.run_tool('review')
        path.write_text(path.read_text().replace('VERSION 1.0.7 ', 'VERSION 1.0 '))
        subprocess.run(['cmake', '-S', str(self.source), '-B', str(self.root/'native')],
                       check=True, capture_output=True, text=True)
        self.run_tool('check', '1.0', '--build', self.root/'native', expected=1)

    @unittest.skipUnless(shutil.which('meson'), 'Meson needed')
    def test_meson_package_alias_cannot_admit_another_native_release(self):
        self.meson()
        path = self.source/'meson.build'
        path.write_text(path.read_text().replace("version: '1.0'", "version: '1.0.7'"))
        (self.root/'build-options.toml').write_text('[package]\nsource_version="1.0.7"\n')
        self.run_tool('review')
        path.write_text(path.read_text().replace("version: '1.0.7'", "version: '1.0'"))
        subprocess.run(['meson', 'setup', str(self.root/'native'), str(self.source)],
                       check=True, capture_output=True, text=True)
        self.run_tool('check', '1.0', '--build', self.root/'native', expected=1)

    def test_rust_source_review_records_typed_evidence_instead_of_configure_flags(self):
        (self.source/'Cargo.toml').write_text('[package]\nname="demo"\nversion="1.0"\n')
        (self.root/'PKGBUILD').write_text('pkgver=1.0\nbuild() { cargo build --no-default-features; }\n')
        result = self.run_tool('review')
        self.assertIn('Source evidence', result.stdout)
        self.assertTrue((self.root/'options/1.0.source.json.log').is_file())
        self.assertNotIn('no source tree', result.stderr)

    def test_observations_from_a_different_project_are_rejected(self):
        self.cmake('', '')
        self.run_tool('review')
        native = self.root/'native'
        native.mkdir()
        (native/'CMakeCache.txt').write_text('CMAKE_PROJECT_VERSION:STATIC=1.0\nCMAKE_PROJECT_NAME:STATIC=Other\n')
        result = self.run_tool('check','1.0','--build',native,expected=1)
        self.assertIn('project', result.stderr.lower())

    def audit_fixture(self, dependency='', rust=False):
        repo = self.root/'repo'
        package = repo/'demo'
        source = package/'src/demo-1.0'
        source.mkdir(parents=True, exist_ok=True)
        if rust:
            (source/'Cargo.toml').write_text('[package]\nname="demo"\nversion="1.0"\n')
        (package/'PKGBUILD').write_text('pkgver=1.0\npkgrel=1\n')
        (package/'.SRCINFO').write_text('pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tsource = https://example.invalid/demo-1.0.tar.gz\n\tsha256sums = '
                                       + 'a'*64 + '\npkgname = demo-waypure\n\tprovides = demo\n\tconflicts = demo\n'
                                       + ('\tdepends = ' + dependency + '\n' if dependency else ''))
        return repo

    def test_audit_collects_cargo_source_without_suggesting_a_download(self):
        repo = self.audit_fixture(rust=True)
        result = subprocess.run([str(TOOLS/'waypure-audit'),'--repo',str(repo),'--json'],
                                text=True,capture_output=True,env=self.env,check=True)
        row = json.loads(result.stdout)['packages'][0]
        self.assertEqual(row['checks']['options'], 'SKIP')
        self.assertEqual(row['source']['backend'], 'cargo')
        self.assertEqual(row['checks']['source'], 'WARN')
        self.assertNotIn('options.unsupported', [item['code'] for item in row['findings']])
        self.assertNotIn('options.missing', [item['code'] for item in row['findings']])

    def test_autodeps_sonames_cannot_bypass_the_x11_policy(self):
        for dependency in ('lib:libX11.so.6','libX11.so=6-64'):
            with self.subTest(dependency=dependency):
                repo = self.audit_fixture(dependency=dependency)
                result = subprocess.run([str(TOOLS/'waypure-audit'),'--repo',str(repo),'--metadata-only','--json'],
                                        text=True,capture_output=True,env=self.env)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                findings = json.loads(result.stdout)['packages'][0]['findings']
                self.assertIn('dependency.x11', [item['code'] for item in findings])

    def test_conventional_cmake_args_array_is_discovered(self):
        self.cmake('option(WITH_WAYLAND "Wayland" ON)\n',' -DWITH_WAYLAND=ON')
        recipe = self.root/'PKGBUILD'
        recipe.write_text(recipe.read_text().replace('_cmake_options','_cmake_args'))
        self.run_tool('review')
        self.assertIn('_cmake_args=(', (self.root/'options/1.0.options.sh.log').read_text())


if __name__ == '__main__':
    unittest.main()
