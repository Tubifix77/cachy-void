"""Unit tests for the XBPS parsing/query layer (architecture.md §7.1, §7.2).

Pure parsers are tested directly. Subprocess-backed methods are tested with an
injected fake runner that records the command and returns canned output. A few
tests touch the real filesystem (srcpkg symlinks) and, when available, the real
``xbps-uhelper`` binary to pin the cmpver exit-code convention.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.xbps import (
    Xbps, norm, split_pkgver, pkgname_of, ParseError,
)


def cp(args, returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(list(args), returncode, stdout, stderr)


class FakeRunner:
    """Injectable (args, cwd) -> CompletedProcess that dispatches on argv."""

    def __init__(self, handler):
        self._handler = handler
        self.calls = []

    def __call__(self, args, cwd):
        args = list(args)
        self.calls.append((args, cwd))
        return self._handler(args, cwd)


# --------------------------------------------------------------------------
# Pure parsing (§7.2)
# --------------------------------------------------------------------------
class NormTests(unittest.TestCase):

    def test_plain_name(self):
        self.assertEqual(norm("gcc"), "gcc")

    def test_strips_version_constraint(self):
        self.assertEqual(norm("libfoo>=1.0"), "libfoo")
        self.assertEqual(norm("bar<2"), "bar")
        self.assertEqual(norm("baz=3_1"), "baz")

    def test_strips_virtual_prefix_and_pkgver(self):
        self.assertEqual(norm("virtual?libGL-1.0_1"), "libGL")

    def test_strips_pkgver_suffix(self):
        self.assertEqual(norm("mesa-24.0_1"), "mesa")

    def test_takes_first_whitespace_field(self):
        self.assertEqual(norm("  foo   bar "), "foo")

    def test_empty_raises(self):
        with self.assertRaises(ParseError):
            norm("")
        with self.assertRaises(ParseError):
            norm("   ")


class PkgverSplitTests(unittest.TestCase):

    def test_simple(self):
        self.assertEqual(split_pkgver("mesa-24.0_1"), ("mesa", "24.0_1"))

    def test_hyphenated_name_splits_on_last_hyphen(self):
        self.assertEqual(pkgname_of("python3-foo-1.2_3"), "python3-foo")

    def test_missing_hyphen_raises(self):
        with self.assertRaises(ParseError):
            split_pkgver("noversion")

    def test_missing_revision_raises(self):
        with self.assertRaises(ParseError):
            split_pkgver("foo-bar")     # ver part has no '_'


# --------------------------------------------------------------------------
# Subprocess-backed queries (§7.2) via injected runner
# --------------------------------------------------------------------------
class QueryTests(unittest.TestCase):

    def _xb(self, handler, repos=()):
        return Xbps(void_packages="/vp", repos=repos, run=FakeRunner(handler))

    def test_installed_parses_field_two(self):
        def h(args, cwd):
            self.assertEqual(args, ["xbps-query", "-l"])
            return cp(args, stdout="ii foo-1.0_1 A desc\nii bar-baz-2.0_1 Another\n\n")
        self.assertEqual(self._xb(h).installed(), ["foo", "bar-baz"])

    def test_installed_bad_line_raises(self):
        xb = self._xb(lambda a, c: cp(a, stdout="garbage\n"))
        with self.assertRaises(ParseError):
            xb.installed()

    def test_repo_ver_first_matching_repo(self):
        def h(args, cwd):
            # name present only in the second repo
            if "--repository=/r1" in args:
                return cp(args, returncode=2, stdout="")
            if "--repository=/r2" in args:
                return cp(args, returncode=0, stdout="mesa-1.0_1\n")
            return cp(args, returncode=2)
        xb = self._xb(h, repos=["/r1", "/r2"])
        self.assertEqual(xb.repo_ver("mesa"), "mesa-1.0_1")

    def test_repo_ver_absent_is_none(self):
        xb = self._xb(lambda a, c: cp(a, returncode=2, stdout=""), repos=["/r1"])
        self.assertIsNone(xb.repo_ver("ghost"))

    def test_inst_pkgver_and_origin(self):
        def h(args, cwd):
            if args[:3] == ["xbps-query", "-p", "pkgver"]:
                return cp(args, stdout="wine-9.0_1\n")
            if args[:3] == ["xbps-query", "-p", "repository"]:
                return cp(args, stdout="https://repo/current\n")
            return cp(args, returncode=1)
        xb = self._xb(h)
        self.assertEqual(xb.inst_pkgver("wine"), "wine-9.0_1")
        self.assertEqual(xb.origin("wine"), "https://repo/current")

    def test_show_local_updates_normalizes(self):
        def h(args, cwd):
            self.assertEqual(cwd, "/vp")
            return cp(args, stdout="mesa-24.0_1 update\nwine-9.0_1 update\n")
        self.assertEqual(self._xb(h).show_local_updates(), ["mesa", "wine"])

    def test_show_build_deps_normalizes(self):
        def h(args, cwd):
            return cp(args, stdout="gcc>=10\nvirtual?libGL-1_1\nmeson\n")
        self.assertEqual(self._xb(h).show_build_deps("mesa"),
                         ["gcc", "libGL", "meson"])

    def test_sort_dependencies_reports_ok(self):
        xb = self._xb(lambda a, c: cp(a, returncode=0, stdout="b\na\n"))
        lines, ok = xb.sort_dependencies(["a", "b"])
        self.assertEqual((lines, ok), (["b", "a"], True))

    def test_sort_dependencies_nonzero_not_ok(self):
        xb = self._xb(lambda a, c: cp(a, returncode=1, stdout=""))
        lines, ok = xb.sort_dependencies(["a", "b"])
        self.assertFalse(ok)

    def test_sort_dependencies_empty_input_shortcircuits(self):
        runner = FakeRunner(lambda a, c: cp(a))
        xb = Xbps(void_packages="/vp", run=runner)
        self.assertEqual(xb.sort_dependencies([]), ([], True))
        self.assertEqual(runner.calls, [])     # no subprocess for empty input

    def test_vercmp_exit_code_mapping(self):
        table = {("1", "1"): 0, ("2", "1"): 1, ("1", "2"): 255}
        def h(args, cwd):
            a, b = args[2], args[3]
            return cp(args, returncode=table[(a, b)])
        xb = self._xb(h)
        self.assertEqual(xb.vercmp("1", "1"), 0)
        self.assertEqual(xb.vercmp("2", "1"), 1)
        self.assertEqual(xb.vercmp("1", "2"), -1)


# --------------------------------------------------------------------------
# srcpkg_of against a real symlink tree (§7.1)
# --------------------------------------------------------------------------
class SrcpkgMappingTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.src = Path(self.tmp) / "srcpkgs"
        (self.src / "mesa").mkdir(parents=True)
        os.symlink("mesa", self.src / "mesa-dri")     # subpackage symlink
        self.xb = Xbps(void_packages=self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_template_maps_to_itself(self):
        self.assertEqual(self.xb.srcpkg_of("mesa"), "mesa")

    def test_subpackage_symlink_maps_to_parent(self):
        self.assertEqual(self.xb.srcpkg_of("mesa-dri"), "mesa")

    def test_foreign_binpkg_is_none(self):
        self.assertIsNone(self.xb.srcpkg_of("glibc"))


@unittest.skipUnless(shutil.which("xbps-uhelper"),
                     "xbps-uhelper not available (non-Void host)")
class VercmpIntegrationTests(unittest.TestCase):
    """Pin the real cmpver exit-code convention against the actual binary."""

    def setUp(self):
        self.xb = Xbps(void_packages="/vp")   # default (real) runner

    def test_real_cmpver_convention(self):
        self.assertEqual(self.xb.vercmp("1.0_1", "1.0_1"), 0)
        self.assertEqual(self.xb.vercmp("1.1_1", "1.0_1"), 1)
        self.assertEqual(self.xb.vercmp("1.0_1", "1.1_1"), -1)
        self.assertEqual(self.xb.vercmp("1.0_2", "1.0_1"), 1)   # revision bump


if __name__ == "__main__":
    unittest.main()
