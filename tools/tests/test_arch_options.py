"""Use a real Git repository to verify pinned Arch packaging comparisons."""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


COMMAND = Path(__file__).resolve().parents[1] / "waypure-options"


class ArchCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.upstream = self.root / "arch"
        self.upstream.mkdir()
        self.git("init", "-q")
        self.git("config", "user.name", "Test Maintainer")
        self.git("config", "user.email", "test@example.invalid")
        self.write_release("1.0", "old-dependency")
        self.old = self.git("rev-parse", "HEAD").strip()
        self.write_release("1.1", "new-dependency")
        (self.upstream / "new.patch").write_text("Packaging patch content\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Add packaging patch")
        self.new = self.git("rev-parse", "HEAD").strip()

    def git(self, *arguments):
        return subprocess.check_output(["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false", *arguments],
                                       cwd=self.upstream, text=True, stderr=subprocess.STDOUT)

    def write_release(self, version, dependency):
        sentinel = self.root / "must-not-execute"
        (self.upstream / "PKGBUILD").write_text(f"pkgver={version}\ndepends=({dependency})\ntouch '{sentinel}'\n")
        (self.upstream / ".SRCINFO").write_text(f"pkgbase = demo\n\tpkgver = {version}\n\tpkgrel = 1\n\tepoch = 2\n\tdepends = {dependency}\npkgname = demo\n")
        self.git("add", ".")
        self.git("commit", "-qm", "Package " + version)

    def run_tool(self, *arguments, ok=True):
        result = subprocess.run([str(COMMAND), *map(str, arguments)], cwd=self.root,
                                text=True, capture_output=True,
                                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_snapshots_use_commit_contents_without_executing_recipes(self):
        before = self.git("status", "--porcelain")
        path = self.root / "old.json.log"
        self.run_tool("arch", "snapshot", "--local", self.upstream, "--ref", self.old,
                      "--pkgbase", "demo", "-o", path)
        data = json.loads(path.read_text())
        self.assertEqual(data["commit"], self.old)
        self.assertIn("old-dependency", data["files"]["PKGBUILD"]["text"])
        self.assertEqual(data["version"], "2:1.0-1")
        self.assertFalse((self.root / "must-not-execute").exists())
        self.assertEqual(self.git("status", "--porcelain"), before)
        self.assertEqual(self.git("rev-parse", "HEAD").strip(), self.new)

    def test_arch_diff_includes_metadata_and_packaging_patches(self):
        old, new = self.root / "old.json.log", self.root / "new.json.log"
        for commit, output in ((self.old, old), (self.new, new)):
            self.run_tool("arch", "snapshot", "--local", self.upstream, "--ref", commit,
                          "--pkgbase", "demo", "-o", output)
        output = self.run_tool("arch", "diff", old, new).stdout
        self.assertIn("2:1.0-1 -> 2:1.1-1", output)
        self.assertIn("new.patch", output)
        self.assertIn("old-dependency", output)
        self.assertIn("new-dependency", output)

    def test_invalid_ref_does_not_create_an_arch_baseline(self):
        output = self.root / "invalid.json.log"
        result = self.run_tool("arch", "snapshot", "--local", self.upstream, "--ref=--help",
                               "--pkgbase", "demo", "-o", output, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(output.exists())

    def test_official_repository_without_srcinfo_uses_literal_metadata(self):
        (self.upstream / ".SRCINFO").unlink()
        recipe = self.upstream / "PKGBUILD"
        recipe.write_text("epoch=3\npkgrel=1\n" + recipe.read_text())
        self.git("add", ".")
        self.git("commit", "-qm", "Metadata only in PKGBUILD")
        output = self.root / "literal.json.log"
        self.run_tool("arch", "snapshot", "--local", self.upstream, "--pkgbase", "demo", "-o", output)
        self.assertEqual(json.loads(output.read_text())["version"], "3:1.1-1")
        self.assertFalse((self.root / "must-not-execute").exists())

    def make_package(self):
        source = self.root / "src/demo-1.0"
        source.mkdir(parents=True)
        (source / "CMakeLists.txt").write_text('project(Demo VERSION 1.0 LANGUAGES NONE)\noption(WITH_FOO "Foo" ON)\n')
        (self.root / "PKGBUILD").write_text('pkgver=1.0\nepoch=2\nbuild() {\n local _cmake_options=(\n -DWITH_FOO=OFF\n )\n}\n')
        (self.root / "build-options.toml").write_text('[package]\nbackend="cmake"\narch="demo"\n')

    def test_corrupt_arch_receipt_leaves_an_error_in_review_report(self):
        self.make_package()
        self.run_tool("review", "--arch-local", self.upstream)
        self.run_tool("review", "--offline")
        report = self.root / "options/1.0.review.log"
        self.assertIn("OFFLINE", report.read_text())
        (self.root / "options/arch/current.json.log").write_text('{"snapshot":"../invalid.json.log"}\n')
        result = self.run_tool("review", "--offline", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ERROR", report.read_text())

    def test_offline_arch_snapshot_must_match_configured_pkgbase(self):
        self.make_package()
        self.run_tool("review", "--arch-local", self.upstream)
        config = self.root / "build-options.toml"
        config.write_text(config.read_text().replace('arch="demo"', 'arch="other"'))
        result = self.run_tool("review", "--offline", ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("other", result.stdout + result.stderr)

    def arch_changes(self):
        report = self.root / "options/1.0.review.log"
        line = next(line for line in report.read_text().splitlines() if line.startswith("Arch changes: "))
        return Path(line.removeprefix("Arch changes: "))

    def test_arch_release_comparison_survives_repeated_and_offline_reviews(self):
        self.make_package()
        self.git("update-ref", "HEAD", self.old)
        self.run_tool("review", "--arch-local", self.upstream)
        self.git("update-ref", "HEAD", self.new)
        self.run_tool("review", "--arch-local", self.upstream)
        delta = self.arch_changes()
        expected = delta.read_text()
        self.assertIn("old-dependency", expected)
        self.assertIn("new-dependency", expected)
        for flags in (("--arch-local", self.upstream), ("--offline",)):
            self.run_tool("review", *flags)
            self.assertEqual(self.arch_changes(), delta)
            self.assertEqual(delta.read_text(), expected)
        self.assertIn(self.old[:12], delta.name)
        self.assertIn(self.new[:12], delta.name)

    def test_next_release_uses_last_recorded_arch_commit_as_its_anchor(self):
        self.make_package()
        self.git("update-ref", "HEAD", self.old)
        self.run_tool("review", "--arch-local", self.upstream)
        self.git("update-ref", "HEAD", self.new)
        self.run_tool("review", "--arch-local", self.upstream)
        original = self.arch_changes()
        before = original.read_bytes()
        recipe = self.root / "PKGBUILD"
        recipe.write_text(recipe.read_text().replace("pkgver=1.0", "pkgver=2.0"))
        source = self.root / "src/demo-2.0"
        source.mkdir()
        (source / "CMakeLists.txt").write_text((self.root / "src/demo-1.0/CMakeLists.txt").read_text().replace("VERSION 1.0", "VERSION 2.0"))
        self.run_tool("review", "--arch-local", self.upstream)
        report = (self.root / "options/2.0.review.log").read_text()
        line = next(line for line in report.splitlines() if line.startswith("Arch changes: "))
        new_delta = Path(line.removeprefix("Arch changes: "))
        self.assertIn("No Arch packaging file changes", new_delta.read_text())
        self.assertIn(self.new[:12] + "-to-" + self.new[:12], new_delta.name)
        self.assertEqual(original.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
