"""Source-derived Autoconf defaults and their native configure behavior."""

import os
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]
PLATFORM = '''v4l_default=no
dvb_default=no
bktr_default=no
proxy_default=no
case "$host_os" in
  linux*)
    v4l_default=yes
    dvb_default=yes
    proxy_default=yes
    ;;
  freebsd* | openbsd* | netbsd*)
    bktr_default=yes
    ;;
esac
AC_ARG_ENABLE([v4l], [AS_HELP_STRING([--enable-v4l], [Video4Linux capture])], [], [enable_v4l=$v4l_default])
if test "x$enable_v4l" = xyes; then
  AC_DEFINE([WITH_V4L], [1], [Capture support])
else
  proxy_default=no
fi
AC_ARG_ENABLE([dvb], [AS_HELP_STRING([--enable-dvb], [DVB capture])], [], [enable_dvb=$dvb_default])
AC_ARG_ENABLE([bktr], [AS_HELP_STRING([--enable-bktr], [BSD capture])], [], [enable_bktr=$bktr_default])
AC_ARG_ENABLE([proxy], [AS_HELP_STRING([--enable-proxy], [Shared capture daemon])], [], [enable_proxy=$proxy_default])
'''


class AutoconfDefaultsCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root/'src/demo-1.0'
        self.source.mkdir(parents=True)
        self.env = {**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'}
        self.deadline = 30

    def tool(self, *arguments):
        result = subprocess.run([str(TOOLS/'waypure-options'), *arguments], cwd=self.root,
                                env=self.env, capture_output=True, text=True, timeout=self.deadline)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout

    def review(self, script, flags=''):
        (self.source/'configure.ac').write_text('AC_INIT([Demo], [1.0])\n' + script + '\nAC_OUTPUT\n')
        (self.root/'PKGBUILD').write_text('pkgver=1.0\n_make_options=(\n' + flags + '\n)\n')
        self.tool('review')
        return self.tool('render', '1.0', '--prune-redundant')

    def row(self, output, feature):
        pattern = r'^\s*(?:#\s*)?(?:--(?:enable|disable)-' + re.escape(feature) + r'(?:[ =]|$)|feature:' + re.escape(feature) + '=)'
        rows = [line for line in output.splitlines() if re.search(pattern, line)]
        self.assertEqual(len(rows), 1, output)
        return rows[0]

    def test_linux_case_defaults_render_native_flags_with_platform_evidence(self):
        output = self.review(PLATFORM)
        for feature, flag in (('v4l', '--enable-v4l'), ('dvb', '--enable-dvb'),
                              ('bktr', '--disable-bktr'), ('proxy', '--enable-proxy')):
            with self.subTest(feature=feature):
                row = self.row(output, feature)
                self.assertIn(flag, row)
                self.assertNotIn('UNKNOWN', row)
                self.assertIn('linux', row)

    def test_selected_root_changes_dependent_default_and_justifies_pruning(self):
        output = self.review(PLATFORM, '--disable-v4l\n--disable-proxy')
        row = self.row(output, 'proxy')
        self.assertIn('# --disable-proxy', row)
        self.assertIn('REDUNDANT', row)
        self.assertIn('feature:v4l=OFF', row)
        self.assertTrue(self.row(output, 'v4l').lstrip().startswith('--disable-v4l'))

    def test_explicit_dependent_option_overrides_its_platform_default(self):
        output = self.review(PLATFORM, '--disable-v4l\n--enable-proxy')
        self.assertTrue(self.row(output, 'proxy').lstrip().startswith('--enable-proxy'))

    def test_chained_quoted_defaults_resolve_without_word_splitting(self):
        output = self.review('''base=no
next="$base"
last=${next}
AC_ARG_ENABLE([codec], [AS_HELP_STRING([--enable-codec], [Codec])], [], [enable_codec="$last"])
''')
        self.assertIn('--disable-codec', self.row(output, 'codec'))
        self.assertNotIn('UNKNOWN', self.row(output, 'codec'))

    def test_literal_here_document_is_data_and_cannot_mutate_shell_defaults(self):
        output = self.review('''cat <<EOF >site.h
/* Site definitions */
#define ENABLE_CODEC 1
enable_codec=not_a_shell_assignment
EOF
codec_default=no
AC_ARG_ENABLE([codec], [AS_HELP_STRING([--enable-codec], [Codec])], [], [enable_codec=$codec_default])
''')
        self.assertIn('--disable-codec', self.row(output, 'codec'))
        self.assertNotIn('UNKNOWN', self.row(output, 'codec'))

    def test_conditional_shell_assignment_cannot_supply_a_false_default(self):
        output = self.review('''driver_default=no
test "$probe" = yes && driver_default=yes
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_quoted_case_prefix_keeps_its_unquoted_wildcard(self):
        output = self.review('''driver_default=no
case "$host_os" in
  "linux"*) driver_default=yes ;;
esac
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('--enable-driver', self.row(output, 'driver'))

    def test_quoted_case_whitespace_remains_literal(self):
        output = self.review('''driver_default=no
case "$host_os" in
  " linux-gnu") driver_default=yes ;;
esac
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('--disable-driver', self.row(output, 'driver'))

    def test_elif_does_not_move_following_declarations_into_the_else_branch(self):
        output = self.review('''driver_default=no
if test "$host_os" = linux-gnu; then
  driver_default=yes
elif test "$host_os" = freebsd; then
  driver_default=yes
fi
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('--enable-driver', self.row(output, 'driver'))
        self.assertNotIn('UNKNOWN', self.row(output, 'driver'))

    def test_command_after_assignment_cannot_hide_mutations(self):
        output = self.review('''driver_default=no
temporary=x eval 'driver_default=yes'
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_unsupported_condition_command_cannot_preserve_a_false_constant(self):
        for condition in ('set_driver_default', 'test "$probe" = yes && set_driver_default'):
            with self.subTest(condition=condition):
                output = self.review('driver_default=no\nif ' + condition + '; then\n :\nfi\n'
                                     + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
                self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_large_unknown_branches_do_not_repeat_full_program_for_each_flag(self):
        self.deadline = 15
        script = '\n'.join('temporary_' + str(index) + '=no' for index in range(400)) + '\n'
        script += '\n'.join('AC_ARG_ENABLE([feature' + str(index)
                            + '], [AS_HELP_STRING([--enable-feature' + str(index)
                            + '], [Feature])], [], [enable_feature' + str(index) + '=no])'
                            for index in range(80)) + '\n'
        script += '\n'.join('if test "$probe' + str(index) + '" = yes; then\n command'
                            + str(index) + '\nelse\n other_command' + str(index) + '\nfi'
                            for index in range(100)) + '\n'
        flags = '\n'.join('--disable-feature' + str(index) for index in range(80))
        try:
            output = self.review(script, flags)
        except subprocess.TimeoutExpired:
            self.fail('Large conditional option review exceeded the 15-second subprocess deadline')
        self.assertTrue(self.row(output, 'feature0').lstrip().startswith('--disable-feature0'))
        self.assertIn('UNKNOWN', self.row(output, 'feature0'))

    def test_many_options_sharing_defaults_keep_a_compact_catalogue(self):
        script = 'AC_INIT([Demo], [1.0])\nbase=no\n'
        for index in range(400):
            previous = 'base' if index == 0 else 'alias' + str(index - 1)
            script += 'alias' + str(index) + '="$' + previous + '"\n'
        for index in range(80):
            script += ('AC_ARG_ENABLE([feature' + str(index) + '], [Feature], [], [enable_feature'
                       + str(index) + '="$alias399"])\n')
        (self.source/'configure.ac').write_text(script)
        catalogue = self.root/'catalogue.json.log'
        self.tool('snapshot', str(self.source), '--backend', 'autoconf', '-o', str(catalogue))
        self.assertLess(catalogue.stat().st_size, 1024 * 1024, 'Shared source rules should fit in a 1 MiB catalogue')
        self.assertEqual(json.loads(catalogue.read_text())['options']['feature:feature79']['default'], 'OFF')

    def test_source_only_diff_does_not_claim_generated_options_were_removed(self):
        (self.source/'configure.ac').write_text('AC_INIT([Demo], [1.0])\nAC_ARG_ENABLE([old], [AS_HELP_STRING([--enable-old], [Old feature])])\n')
        (self.source/'configure').write_text("#!/bin/sh\nprintf '%s\\n' '  --enable-shared  Shared libraries'\n")
        (self.source/'m4').mkdir()
        generated = self.source/'m4/generated.m4'
        generated.write_text('AC_ARG_ENABLE([generated], [AS_HELP_STRING([--enable-generated], [Generated feature])])\n')
        old = self.root/'old.json.log'
        new = self.root/'new.json.log'
        self.tool('snapshot', str(self.source), '--backend', 'autoconf', '-o', str(old))
        (self.source/'configure').unlink()
        generated.unlink()
        (self.source/'configure.ac').write_text('AC_INIT([Demo], [1.1])\n')
        self.tool('snapshot', str(self.source), '--backend', 'autoconf', '-o', str(new))
        output = self.tool('diff', str(old), str(new))
        self.assertNotIn('- feature:shared', output)
        self.assertNotIn('- feature:generated', output)
        self.assertIn('- feature:old', output)
        self.assertIn('configure help', output)

    def test_rule_changes_are_reported_without_dumping_the_interpreter_program(self):
        old = self.root/'old.json.log'
        new = self.root/'new.json.log'
        (self.source/'configure.ac').write_text('AC_INIT([Demo], [1.0])\n' + PLATFORM)
        self.tool('snapshot', str(self.source), '--backend', 'autoconf', '-o', str(old))
        (self.source/'configure.ac').write_text('AC_INIT([Demo], [1.1])\n' + PLATFORM.replace('linux*)', 'linux-gnu)'))
        self.tool('snapshot', str(self.source), '--backend', 'autoconf', '-o', str(new))
        output = self.tool('diff', str(old), str(new))
        self.assertIn('feature:dvb', output)
        self.assertIn('rules', output)
        self.assertNotIn('"kind":', output)

    def test_internal_configure_variables_do_not_become_proposed_public_flags(self):
        (self.source/'configure').write_text('''#!/bin/sh
if test "$1" = --help; then
  printf '%s\\n' '  --enable-driver  Driver'
else
  test "$enable_driver_auto" = yes
fi
''')
        output = self.review('AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=no])\n')
        self.assertNotIn('feature:driver-auto', output)

    def test_unknown_condition_keeps_differing_branches_unknown_and_explains_it(self):
        output = self.review('''if test "$have_driver" = yes; then
  driver_default=yes
else
  driver_default=no
fi
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        row = self.row(output, 'driver')
        self.assertIn('UNKNOWN', row)
        self.assertIn('have_driver', row)

    def test_equal_unknown_branches_prove_a_common_default(self):
        output = self.review('''if test "$probe" = yes; then
  driver_default=no
else
  driver_default=no
fi
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('--disable-driver', self.row(output, 'driver'))
        self.assertNotIn('UNKNOWN', self.row(output, 'driver'))

    def test_external_probe_actions_cannot_be_mistaken_for_literal_defaults(self):
        output = self.review('''driver_default=no
AC_CHECK_LIB([driver], [driver_init], [driver_default=yes], [driver_default=no])
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        row = self.row(output, 'driver')
        self.assertIn('UNKNOWN', row)
        self.assertIn('AC_CHECK_LIB', row)

    def test_command_substitution_stays_unknown_and_is_never_executed(self):
        sentinel = self.root/'must-not-execute'
        output = self.review('driver_default=$(touch "' + str(sentinel) + '"; printf yes)\n'
                             + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
        self.assertIn('UNKNOWN', self.row(output, 'driver'))
        self.assertFalse(sentinel.exists())

    def test_cross_host_override_cannot_inherit_linux_platform_defaults(self):
        output = self.review(PLATFORM, '--host=x86_64-w64-mingw32')
        self.assertIn('UNKNOWN', self.row(output, 'v4l'))
        self.assertIn('host', self.row(output, 'v4l'))

    def test_cross_build_override_also_invalidates_the_default_host(self):
        output = self.review(PLATFORM, '--build=x86_64-w64-mingw32')
        self.assertIn('UNKNOWN', self.row(output, 'v4l'))

    def test_m4_variable_writes_cannot_preserve_an_earlier_default(self):
        for mutation in ('AS_VAR_SET([driver_default], [yes])',
                         'AC_SUBST([driver_default], [yes])',
                         'AC_CHECK_LIB([driver], [driver_init], [AS_VAR_SET([driver_default], [yes])])'):
            with self.subTest(mutation=mutation):
                output = self.review('driver_default=no\n' + mutation + '\n'
                                     + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
                self.assertNotIn('--disable-driver', self.row(output, 'driver'))

    def test_unknown_custom_macros_cannot_preserve_a_false_default(self):
        for statement in ('CUSTOM_DRIVER_SETUP', 'CUSTOM_DRIVER_SETUP([driver_default])'):
            with self.subTest(statement=statement):
                output = self.review('driver_default=no\n' + statement + '\n'
                                     + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
                self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_defined_custom_helpers_keep_unrelated_option_defaults(self):
        output = self.review('''AC_DEFUN([CHECK_HELPER], [
  AC_CHECK_LIB([driver], [driver_init], [AS_VAR_SET([$1], [yes])], [AS_VAR_SET([$1], [no])])
])
driver_default=no
CHECK_HELPER([have_driver])
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertIn('--disable-driver', self.row(output, 'driver'))
        self.assertNotIn('UNKNOWN', self.row(output, 'driver'))

    def test_unsupported_indirect_mutations_keep_defaults_unknown(self):
        for statement in ('AC_CHECK_LIB([driver], [driver_init], [CUSTOM_DRIVER_SETUP])',
                          'for item in yes; do\n set_driver_default\ndone',
                          'printf -v driver_default yes',
                          'case "$unknown" in\n "$((driver_default=1))") : ;;\nesac'):
            with self.subTest(statement=statement):
                output = self.review('driver_default=no\n' + statement + '\n'
                                     + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
                self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_arithmetic_expansion_side_effects_are_not_discarded(self):
        for statement in (': "$((driver_default=1))"',
                          'case "$((driver_default=1))" in\n *) : ;;\nesac'):
            with self.subTest(statement=statement):
                output = self.review('driver_default=no\n' + statement + '\n'
                                     + 'AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])\n')
                self.assertIn('UNKNOWN', self.row(output, 'driver'))

    def test_compound_test_conditions_do_not_imply_unknown_command_mutations(self):
        output = self.review('''driver_default=no
if test "$unknown" = yes || test "$other" = yes; then
  unrelated=yes
fi
AC_ARG_ENABLE([driver], [AS_HELP_STRING([--enable-driver], [Driver])], [], [enable_driver=$driver_default])
''')
        self.assertNotIn('UNKNOWN', self.row(output, 'driver'))
        self.assertIn('--disable-driver', self.row(output, 'driver'))

    @unittest.skipUnless(shutil.which('autoconf'), 'Autoconf needed')
    def test_source_predictions_and_pruned_flags_match_native_configure(self):
        output = self.review(PLATFORM + 'AC_MSG_NOTICE([capture=$enable_v4l proxy=$enable_proxy bktr=$enable_bktr])\n',
                             '--disable-v4l\n--disable-proxy')
        subprocess.run(['autoconf'], cwd=self.source, check=True, capture_output=True, text=True)
        for label, array in (('original', '_make_options=(--disable-v4l --disable-proxy)\n'),
                             ('proposal', output)):
            directory = self.root/label
            directory.mkdir()
            result = subprocess.run(['bash', '-c', array + '\nsh "$1" host_os=linux-gnu "${_make_options[@]}"',
                                     'bash', str(self.source/'configure')], cwd=directory,
                                    check=True, capture_output=True, text=True)
            self.assertIn('capture=no proxy=no bktr=no', result.stdout)
        self.assertIn('REDUNDANT', self.row(output, 'proxy'))


if __name__ == '__main__':
    unittest.main()
