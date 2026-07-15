"""Unit tests for §8.4 deterministic template synthesis (engine/template.py).

Builds a mock void-packages tree in a temp sandbox and verifies that
regeneration produces a syntactically clean linux-cachy template with inherited
version/checksum, the BORE patch dropped into patches/, and the fragment
appended — plus the ASSERT-A/B/C failure paths and the exit-code mapping.
"""
import tempfile
import unittest
from pathlib import Path

from engine.template import (
    XbpsTemplateEditor, synthesize, detect_march, exit_code_for,
    TemplateSynthesisError, EXIT_HALT, _assert_regen,
)

UPSTREAM_TEMPLATE = """\
# Template file for 'linux6.12'
pkgname=linux6.12
version=6.12.35
revision=1
_kernver="${version}_${revision}"
short_desc="Linux kernel and modules (${version%.*} series)"
maintainer="x <x@example.com>"
license="GPL-2.0-only"
homepage="https://www.kernel.org"
distfiles="https://cdn.kernel.org/linux-${version}.tar.xz"
checksum=1111111111111111111111111111111111111111111111111111111111111111
subpackages="linux6.12-headers linux6.12-dbg"

do_install() {
	: install
}

linux6.12-headers_package() {
	short_desc+=" - source headers"
	pkg_install() {
		:
	}
}

linux6.12-dbg_package() {
	short_desc+=" - debug"
	pkg_install() {
		:
	}
}
"""

DOTCONFIG = 'CONFIG_LOCALVERSION="_1"\nCONFIG_FOO=y\nCONFIG_BAR=m\n'
FRAGMENT = "CONFIG_SCHED_BORE=y\n# CONFIG_HZ_250 is not set\n"
PATCH = b"--- a/x\n+++ b/x\n@@ bore @@\n"


def _mk_upstream(root: Path, series="6.12", template=UPSTREAM_TEMPLATE,
                 with_dotconfig=True):
    d = root / "srcpkgs" / f"linux{series}"
    (d / "files").mkdir(parents=True)
    (d / "template").write_text(template, encoding="utf-8")
    (d / "patches").mkdir()
    if with_dotconfig:
        (d / "files" / "x86_64-dotconfig").write_text(DOTCONFIG, encoding="utf-8")
    return root


class EditorTests(unittest.TestCase):

    def test_parse_pkgver(self):
        self.assertEqual(XbpsTemplateEditor(UPSTREAM_TEMPLATE).parse_pkgver(),
                         ("6.12.35", "1"))

    def test_rename_transforms_all_identifiers(self):
        out = XbpsTemplateEditor(UPSTREAM_TEMPLATE).rename_package("linux6.12")
        self.assertIn("pkgname=linux-cachy\n", out)
        self.assertIn("linux-cachy-headers_package()", out)
        self.assertIn("linux-cachy-dbg_package()", out)
        self.assertIn('subpackages="linux-cachy-headers linux-cachy-dbg"', out)
        self.assertNotIn("linux6.12", out)

    def test_rename_leaves_version_and_checksum_untouched(self):
        ed = XbpsTemplateEditor(UPSTREAM_TEMPLATE)
        before = ed.checksum_lines()
        ed.rename_package("linux6.12")
        self.assertEqual(ed.parse_pkgver(), ("6.12.35", "1"))   # version inherited
        self.assertEqual(ed.checksum_lines(), before)           # checksum inherited

    def test_parse_pkgver_missing_raises(self):
        with self.assertRaises(TemplateSynthesisError):
            XbpsTemplateEditor("pkgname=x\n").parse_pkgver()


class SynthesizeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        _mk_upstream(self.tmp)

    def test_happy_path_produces_valid_fork(self):
        res = synthesize(void_packages=self.tmp, series="6.12",
                         patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertEqual(res.pkgver, "6.12.35_1")
        fork = self.tmp / "srcpkgs" / "linux-cachy"
        template = (fork / "template").read_text(encoding="utf-8")
        self.assertIn("pkgname=linux-cachy\n", template)
        self.assertNotIn("linux6.12", template)
        # patch dropped in patches/ (no template edit needed — §2.3)
        self.assertEqual((fork / "patches" / "0001-bore.patch").read_bytes(), PATCH)
        # fragment appended to the dotconfig
        dot = (fork / "files" / "x86_64-dotconfig").read_text(encoding="utf-8")
        # base config preserved (LOCALVERSION line is deliberately rewritten)
        self.assertIn("CONFIG_FOO=y", dot)
        self.assertIn("CONFIG_BAR=m", dot)
        self.assertIn("CONFIG_SCHED_BORE=y", dot)
        # checksum inherited byte-for-byte (ASSERT-C)
        self.assertEqual(res.checksum_lines,
                         [l for l in UPSTREAM_TEMPLATE.splitlines()
                          if l.startswith("checksum=")])
        # unique release string (finding #8): BOTH sides suffixed coherently
        self.assertIn('_kernver="${version}_${revision}-cachy"', template)
        self.assertIn('CONFIG_LOCALVERSION="_1-cachy"', dot)

    def test_subpackage_symlinks_created(self):
        # xbps-src resolves subpackages via sibling symlinks; regen must create
        # srcpkgs/<sub> -> linux-cachy for each *_package() (real-hardware find:
        # the kernel compiled all night then died for lack of these).
        res = synthesize(void_packages=self.tmp, series="6.12",
                         patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertEqual(res.subpackages,
                         ["linux-cachy-dbg", "linux-cachy-headers"])
        srcpkgs = self.tmp / "srcpkgs"
        for sub in ("linux-cachy-headers", "linux-cachy-dbg"):
            link = srcpkgs / sub
            self.assertTrue(link.is_symlink(), f"{sub} must be a symlink")
            import os as _os
            self.assertEqual(_os.readlink(link), "linux-cachy")

    def test_regen_is_idempotent_for_symlinks(self):
        synthesize(void_packages=self.tmp, series="6.12",
                   patch_bytes=PATCH, fragment_text=FRAGMENT)
        # second pass must not choke on the now-existing symlinks
        res2 = synthesize(void_packages=self.tmp, series="6.12",
                          patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertIn("linux-cachy-headers", res2.subpackages)

    def test_idempotent_regeneration(self):
        first = synthesize(void_packages=self.tmp, series="6.12",
                           patch_bytes=PATCH, fragment_text=FRAGMENT)
        second = synthesize(void_packages=self.tmp, series="6.12",
                            patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertEqual(first.pkgver, second.pkgver)
        # regenerating over an existing fork must not accumulate fragment copies
        dot = (self.tmp / "srcpkgs" / "linux-cachy" / "files"
               / "x86_64-dotconfig").read_text(encoding="utf-8")
        self.assertEqual(dot.count("CONFIG_SCHED_BORE=y"), 1)

    def test_missing_kernver_line_fails_assert_d(self):
        # A template without the _kernver line cannot get a unique release
        # string -> refuse rather than rebuild a colliding kernel (finding #8).
        root = Path(tempfile.mkdtemp())
        stripped = UPSTREAM_TEMPLATE.replace(
            '_kernver="${version}_${revision}"\n', "")
        _mk_upstream(root, template=stripped)
        with self.assertRaises(TemplateSynthesisError) as ctx:
            synthesize(void_packages=root, series="6.12",
                       patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertIn("ASSERT-D", str(ctx.exception))

    def test_missing_upstream_template_raises(self):
        empty = Path(tempfile.mkdtemp())
        with self.assertRaises(TemplateSynthesisError):
            synthesize(void_packages=empty, series="6.12",
                       patch_bytes=PATCH, fragment_text=FRAGMENT)

    def test_missing_patch_raises(self):
        with self.assertRaises(TemplateSynthesisError):
            synthesize(void_packages=self.tmp, series="6.12",
                       patch_bytes=b"", fragment_text=FRAGMENT)

    def test_missing_dotconfig_raises(self):
        root = Path(tempfile.mkdtemp())
        _mk_upstream(root, with_dotconfig=False)
        with self.assertRaises(TemplateSynthesisError):
            synthesize(void_packages=root, series="6.12",
                       patch_bytes=PATCH, fragment_text=FRAGMENT)

    def test_failed_synthesis_leaves_previous_fork_intact(self):
        good = synthesize(void_packages=self.tmp, series="6.12",
                          patch_bytes=PATCH, fragment_text=FRAGMENT)
        marker = good.srcpkg_dir / "template"
        original = marker.read_text(encoding="utf-8")
        # Now break the upstream so the next synthesis fails after the good fork
        # already exists.
        (self.tmp / "srcpkgs" / "linux6.12" / "files" / "x86_64-dotconfig").unlink()
        with self.assertRaises(TemplateSynthesisError):
            synthesize(void_packages=self.tmp, series="6.12",
                       patch_bytes=PATCH, fragment_text=FRAGMENT)
        self.assertEqual(marker.read_text(encoding="utf-8"), original)


class AssertionGateTests(unittest.TestCase):
    """White-box tests of ASSERT-A/B/C — the invariants that gate the swap.

    (These are defense-in-depth: the normal transform can't violate them, so we
    hand _assert_regen a deliberately-corrupt staging tree.)
    """

    def _stage(self, template_text, checksums=("checksum=1111",)):
        d = Path(tempfile.mkdtemp()) / "linux-cachy"
        d.mkdir(parents=True)
        (d / "template").write_text(template_text, encoding="utf-8")
        return d, list(checksums)

    def test_assert_a_residual_series_token(self):
        d, ck = self._stage(
            "pkgname=linux-cachy\nlinux-cachy-headers_package(){ :;}\n"
            "# stray linux6.12 reference\nchecksum=1111\n")
        with self.assertRaises(TemplateSynthesisError) as ctx:
            _assert_regen(d, "6.12", "linux-cachy", ck)
        self.assertIn("ASSERT-A", str(ctx.exception))

    def test_assert_b_pkgname_not_renamed(self):
        d, ck = self._stage(
            "pkgname=linux-cachy\nchecksum=1111\n")   # no headers subpackage
        with self.assertRaises(TemplateSynthesisError) as ctx:
            _assert_regen(d, "6.12", "linux-cachy", ck)
        self.assertIn("ASSERT-B", str(ctx.exception))

    def test_assert_c_checksum_divergence(self):
        d, _ = self._stage(
            "pkgname=linux-cachy\nlinux-cachy-headers_package(){ :;}\n"
            "checksum=9999\n")
        with self.assertRaises(TemplateSynthesisError) as ctx:
            _assert_regen(d, "6.12", "linux-cachy", ["checksum=1111"])
        self.assertIn("ASSERT-C", str(ctx.exception))


class ExitMappingTests(unittest.TestCase):

    def test_synthesis_error_maps_to_70(self):
        self.assertEqual(exit_code_for(TemplateSynthesisError("x")), EXIT_HALT)
        self.assertEqual(EXIT_HALT, 70)


class DetectMarchTests(unittest.TestCase):

    def _cpuinfo(self, flags):
        p = Path(tempfile.mkdtemp()) / "cpuinfo"
        p.write_text(f"processor\t: 0\nflags\t\t: {flags}\n", encoding="utf-8")
        return str(p)

    def test_v4_requires_full_avx512_subset(self):
        full = "fpu avx2 avx512f avx512bw avx512cd avx512dq avx512vl"
        self.assertEqual(detect_march(self._cpuinfo(full)), "x86-64-v4")

    def test_partial_avx512_with_v3_features_is_v3(self):
        partial = "fpu avx2 fma bmi2 avx512f avx512vl"    # missing bw/cd/dq
        self.assertEqual(detect_march(self._cpuinfo(partial)), "x86-64-v3")

    def test_no_avx2_is_v2_not_v3(self):
        # Real deployment target: Ivy Bridge (i3-3110M) — AVX but no AVX2.
        # Recommending v3 here would produce SIGILL binaries.
        ivy = "fpu sse4_1 sse4_2 popcnt avx"
        self.assertEqual(detect_march(self._cpuinfo(ivy)), "x86-64-v2")

    def test_ancient_cpu_is_baseline(self):
        self.assertEqual(detect_march(self._cpuinfo("fpu sse2 sse3")), "x86-64")

    def test_no_cpuinfo_degrades_to_v2_floor(self):
        # Absence of proof must degrade, never upgrade (no SIGILL by default).
        self.assertEqual(detect_march("/no/such/cpuinfo"), "x86-64-v2")


if __name__ == "__main__":
    unittest.main()
