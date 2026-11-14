"""Exercise color choices through the real commands, pipes and terminals."""

import json
import os
from pathlib import Path
import pty
import re
import subprocess
import tempfile
import unittest


TOOLS = Path(__file__).resolve().parents[1]
ANSI = re.compile(r"\x1b\[[0-9;]*m")


class TerminalOutputCase(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.repo = Path(temporary.name)
        self.package = self.repo / "demo"
        source = self.package / "src/demo-1.0"
        source.mkdir(parents=True)
        (source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.22)\nproject(Demo VERSION 1.0 LANGUAGES NONE)\n'
            'option(WITH_X11 "X11" ON)\noption(WITH_COMMON "Common" ON)\n')
        (self.package / "PKGBUILD").write_text(
            'pkgver=1.0\npkgrel=1\nbuild() {\n local _cmake_options=(\n -DWITH_X11=OFF # WAYLAND\n )\n}\n')
        (self.package / ".SRCINFO").write_text(
            'pkgbase = demo-waypure\n\tpkgver = 1.0\n\tpkgrel = 1\n\tarch = x86_64\n'
            '\tsha256sums = ' + 'a' * 64 + '\n'
            '\tsource = https://example.invalid/demo-1.0.tar.gz\n'
            'pkgname = demo-waypure\n\tprovides = demo\n\tconflicts = demo\n')

    def run_tool(self, command, *arguments, terminal=False, env=None, expected=0):
        environment = {key: value for key, value in os.environ.items() if key != "NO_COLOR"}
        environment.update(TERM="xterm-256color", PYTHONDONTWRITEBYTECODE="1")
        environment.update(env or {})
        argv = [str(TOOLS / command), *map(str, arguments)]
        if terminal:
            master, slave = pty.openpty()
            try:
                # The fixtures produce less than one terminal buffer of output.
                result = subprocess.run(argv, cwd=self.package, env=environment,
                                        stdout=slave, stderr=subprocess.PIPE, timeout=15)
                os.close(slave)
                slave = None
                output = bytearray()
                while True:
                    try:
                        block = os.read(master, 65536)
                    except OSError as error:
                        if error.errno != 5:  # Linux PTYs signal EOF with EIO.
                            raise
                        break
                    if not block:
                        break
                    output.extend(block)
                stdout, stderr = output.decode().replace("\r\n", "\n"), result.stderr.decode()
            finally:
                os.close(master)
                if slave is not None:
                    os.close(slave)
        else:
            result = subprocess.run(argv, cwd=self.package, env=environment,
                                    text=True, capture_output=True, timeout=15)
            stdout, stderr = result.stdout, result.stderr
        self.assertEqual(result.returncode, expected, stdout + stderr)
        return stdout

    def audit(self, *arguments, **kwargs):
        return self.run_tool("waypure-audit", "--repo", self.repo, "--metadata-only", *arguments, **kwargs)

    def test_audit_auto_colors_a_terminal_but_keeps_pipe_output_plain(self):
        plain = self.audit()
        colored = self.audit(terminal=True)
        self.assertNotIn("\x1b", plain)
        self.assertRegex(colored, r"\x1b\[[0-9;]*mPASS\x1b\[0m")
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_forced_audit_color_preserves_columns_and_the_plain_report(self):
        plain = self.audit("--color=never")
        colored = self.audit("--color=always")
        self.assertIn("\x1b", colored)
        self.assertEqual(ANSI.sub("", colored), plain)

    def test_auto_respects_no_color_and_dumb_terminals(self):
        for environment in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
            with self.subTest(environment=environment):
                self.assertNotIn("\x1b", self.audit(terminal=True, env=environment))

    def test_explicit_color_choice_overrides_the_environment(self):
        colored = self.audit("--color=always", env={"NO_COLOR": "1", "TERM": "dumb"})
        self.assertIn("\x1b", colored)
        self.assertNotIn("\x1b", self.audit("--color=never", terminal=True))

    def test_empty_no_color_allows_automatic_color(self):
        self.assertIn("\x1b", self.audit(terminal=True, env={"NO_COLOR": ""}))

    def test_json_stays_plain_even_with_forced_color(self):
        output = self.audit("--json", "--color=always", terminal=True)
        self.assertNotIn("\x1b", output)
        self.assertEqual(json.loads(output)["format"], "waypure-audit-1")

    def test_never_also_keeps_argument_help_plain(self):
        for command, arguments in (("waypure-audit", ()), ("waypure-options", ("review",))):
            with self.subTest(command=command):
                output = self.run_tool(command, *arguments, "--color=never", "--help", terminal=True)
                self.assertNotIn("\x1b", output)

    def test_audit_colors_failure_and_warning_labels(self):
        metadata = self.package / ".SRCINFO"
        metadata.write_text(metadata.read_text() + "\tdepends = libx11\n")
        output = self.audit("--color=always", expected=1)
        self.assertIn("\x1b[31mFAIL\x1b[0m", output)
        self.assertIn("\x1b[31mERROR\x1b[0m", output)
        # No retained official packaging evidence exists for this fixture.
        self.assertIn("\x1b[33mWARNING\x1b[0m", output)

    def test_review_colors_diagnostics_but_preserves_saved_reports_and_arrays(self):
        source = self.package / "src/demo-1.0/CMakeLists.txt"
        source.write_text(source.read_text().replace('"X11"', '"Deprecated: use WITH_NEW"'))
        output = self.run_tool("waypure-options", "review", "--color=always")
        self.assertIn("\x1b[36mNOTE\x1b[0m", output)
        report = (self.package / "options/1.0.review.log").read_text()
        array = (self.package / "options/1.0.options.sh.log").read_text()
        self.assertNotIn("\x1b", report + array)
        self.assertTrue(ANSI.sub("", output).startswith(report))
        rendered = self.run_tool("waypure-options", "--color=always", "render", "1.0")
        self.assertNotIn("\x1b", rendered)
        subprocess.run(["bash", "-n"], input=rendered, text=True, check=True)

    def test_global_color_survives_subcommand_defaults_and_check_errors_are_red(self):
        self.run_tool("waypure-options", "review")
        recipe = self.package / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("WITH_X11=OFF", "REMOVED_OPTION=OFF"))
        output = self.run_tool("waypure-options", "--color=always", "check", "1.0", expected=1)
        self.assertIn("\x1b[31mERROR\x1b[0m", output)
        output = self.run_tool("waypure-options", "--color=always", "check", "1.0", "--color=never", expected=1)
        self.assertNotIn("\x1b", output)

    def test_release_diff_colors_added_removed_and_changed_options(self):
        self.run_tool("waypure-options", "review")
        source = self.package / "src/demo-2.0"
        source.mkdir()
        (source / "CMakeLists.txt").write_text(
            'cmake_minimum_required(VERSION 3.22)\nproject(Demo VERSION 2.0 LANGUAGES NONE)\n'
            'option(WITH_WAYLAND "Wayland" ON)\noption(WITH_COMMON "Common" OFF)\n')
        recipe = self.package / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=2.0").replace("WITH_X11=OFF", "WITH_WAYLAND=ON"))
        self.run_tool("waypure-options", "review")
        output = self.run_tool("waypure-options", "diff", "1.0", "2.0", "--color=always")
        self.assertIn("\x1b[32m+ WITH_WAYLAND", output)
        self.assertIn("\x1b[31m- WITH_X11", output)
        self.assertIn("\x1b[33m~ WITH_COMMON", output)


if __name__ == "__main__":
    unittest.main()
