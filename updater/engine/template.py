"""Local package synthesis — architecture.md §8.4 (deterministic regeneration).

The kernel template is **regenerated from the current upstream template every
time**, never textually patched forward — that is the anti-drift invariant. This
module therefore provides:

  * :class:`XbpsTemplateEditor` — applies *only* the exact-match identifier
    transforms (rename ``pkgname`` and the ``*_package()`` / ``subpackages=``
    entries) to template text, and reads (never writes) ``version``/``revision``/
    ``checksum``; and
  * :func:`synthesize` — the full REGEN: copy upstream into a temp worktree,
    transform, drop the §8.3-verified BORE patch into ``patches/``, append the
    §2.4 fragment to the dotconfig, run ASSERT-A/B/C, then atomically swap.

Deliberately absent (see §8.4): no version/checksum *injection* (inherited
byte-for-byte from upstream, ASSERT-C), no ``-march`` in the kernel template
(kernel flags stay stock, §2.4 — :func:`detect_march` recommends the *etc/conf*
ABI level instead), no template edit to "reference" the patch (xbps-src
auto-applies ``patches/``, §2.3).
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# Kernel-path halt exit code (§8.4 / §8.6); mirrors trust.EXIT_HALT.
EXIT_HALT = 70


class TemplateSynthesisError(RuntimeError):
    """Regeneration could not produce a valid linux-cachy template (exit 70).

    Raised for a missing upstream template, a missing verified patch, or any
    failed ASSERT. Maps to AWAIT_HUMAN_TEMPLATE in the integrated flow (§8.4).
    """


def exit_code_for(_err: BaseException) -> int:
    return EXIT_HALT


# ==========================================================================
# XbpsTemplateEditor — exact-match identifier transforms on template TEXT
# ==========================================================================
_VERSION_RE = re.compile(r"^version=([0-9][0-9.]*)\s*$", re.M)
_REVISION_RE = re.compile(r"^revision=([0-9]+)\s*$", re.M)
_CHECKSUM_RE = re.compile(r"^checksum=", re.M)


class XbpsTemplateEditor:
    """Operates on the text of a void-packages kernel template.

    Every mutation is an exact-match line transform (§8.4); it never rewrites
    version/revision/checksum or appends compiler flags.
    """

    def __init__(self, text: str):
        self.text = text

    # -- read-only accessors --------------------------------------------
    def parse_pkgver(self) -> tuple[str, str]:
        """(version, revision) as inherited from the template."""
        mv = _VERSION_RE.search(self.text)
        mr = _REVISION_RE.search(self.text)
        if not mv or not mr:
            raise TemplateSynthesisError(
                "template does not define both version= and revision=")
        return mv.group(1), mr.group(1)

    def checksum_lines(self) -> list[str]:
        """The ``checksum=...`` line(s), for the ASSERT-C byte-identity check."""
        return [ln for ln in self.text.splitlines() if _CHECKSUM_RE.match(ln)]

    def pkgname(self) -> Optional[str]:
        m = re.search(r"^pkgname=(\S+)\s*$", self.text, re.M)
        return m.group(1) if m else None

    # -- the one legitimate mutation: rename identifiers -----------------
    def rename_package(self, old_pkgname: str, new_pkgname: str = "linux-cachy") -> str:
        """Rename ``pkgname`` and every derived identifier, exact-match only.

        Transforms (§8.4):
          pkgname=<old>                      -> pkgname=<new>
          <old>-<sub>_package()              -> <new>-<sub>_package()
          subpackages="<old>-a <old>-b ..."  -> subpackages entries renamed
        Adjacent, unrelated definitions (arrays, functions) are never touched.
        """
        old = re.escape(old_pkgname)
        new = new_pkgname
        text = self.text

        text = re.sub(rf"^pkgname={old}$", f"pkgname={new}", text, flags=re.M)

        # Subpackage function names: "<old>-<sub>_package() {"
        text = re.sub(rf"^{old}(-[A-Za-z0-9_+.]+)_package\(\)",
                      rf"{new}\1_package()", text, flags=re.M)

        # subpackages="..." list: rename only whole <old>-* tokens inside it.
        def _fix_subpackages(m: re.Match) -> str:
            body = m.group(2)
            body = re.sub(rf"(?<![\w.+-]){old}(-[A-Za-z0-9_+.]+)",
                          rf"{new}\1", body)
            return f"{m.group(1)}{body}{m.group(3)}"

        text = re.sub(r'^(subpackages=")([^"]*)(")',
                      _fix_subpackages, text, flags=re.M)

        # Finally, replace any *standalone* <old> series token (comments,
        # descriptions, stray references) so ASSERT-A finds zero residuals. The
        # boundaries deliberately exclude '-' and '.', so "<old>-headers" and a
        # version like "<old>.35" are left to the specific transforms / untouched.
        text = re.sub(rf"(?<![\w.+-]){old}(?![\w.+-])", new, text)

        self.text = text
        return text

    def has_literal(self, token: str) -> bool:
        return token in self.text


# ==========================================================================
# synthesize — the full §8.4 REGEN
# ==========================================================================
@dataclass
class SynthesisResult:
    pkgver: str                # "version_revision" inherited from upstream
    srcpkg_dir: Path           # final srcpkgs/linux-cachy path
    checksum_lines: list[str]  # inherited checksum lines (ASSERT-C witness)


def synthesize(*, void_packages: str | os.PathLike, series: str,
               patch_bytes: bytes, fragment_text: str,
               new_pkgname: str = "linux-cachy") -> SynthesisResult:
    """Regenerate ``srcpkgs/<new_pkgname>`` from ``srcpkgs/linux<series>`` (§8.4).

    :param patch_bytes: the §8.3-verified BORE patch content (already trusted).
    :param fragment_text: the §2.4 config fragment to append to the dotconfig.

    The upstream tree is copied, transformed, augmented, asserted, and only then
    atomically swapped in. A failure leaves any existing fork untouched.
    """
    vp = Path(void_packages)
    srcpkgs = vp / "srcpkgs"
    upstream = srcpkgs / f"linux{series}"
    if not (upstream / "template").is_file():
        raise TemplateSynthesisError(
            f"upstream template not found: {upstream / 'template'} "
            "(series removed upstream? -> AWAIT_HUMAN_SERIES)")
    if not patch_bytes:
        raise TemplateSynthesisError("no verified BORE patch supplied (§8.3)")

    upstream_template = (upstream / "template").read_text(encoding="utf-8")
    upstream_editor = XbpsTemplateEditor(upstream_template)
    upstream_checksums = upstream_editor.checksum_lines()

    # Build into a sibling temp worktree so the swap is an atomic rename.
    srcpkgs.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(dir=srcpkgs, prefix=".synth-"))
    try:
        staging = work / new_pkgname
        shutil.copytree(upstream, staging, symlinks=True)

        editor = XbpsTemplateEditor(upstream_template)
        new_text = editor.rename_package(f"linux{series}", new_pkgname)
        (staging / "template").write_text(new_text, encoding="utf-8")

        patches_dir = staging / "patches"
        patches_dir.mkdir(exist_ok=True)
        (patches_dir / "0001-bore.patch").write_bytes(patch_bytes)

        dotconfig = staging / "files" / "x86_64-dotconfig"
        if not dotconfig.is_file():
            raise TemplateSynthesisError(
                f"upstream dotconfig not found: {dotconfig}")
        base = dotconfig.read_text(encoding="utf-8")
        if not base.endswith("\n"):
            base += "\n"
        dotconfig.write_text(base + fragment_text.rstrip("\n") + "\n",
                             encoding="utf-8")

        _assert_regen(staging, series, new_pkgname, upstream_checksums)

        # Atomic swap: replace any existing fork in a single rename.
        final = srcpkgs / new_pkgname
        if final.exists():
            doomed = work / ".old"
            os.replace(final, doomed)
        os.replace(staging, final)

        version, revision = XbpsTemplateEditor(new_text).parse_pkgver()
        return SynthesisResult(
            pkgver=f"{version}_{revision}",
            srcpkg_dir=final,
            checksum_lines=upstream_checksums)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _assert_regen(staging: Path, series: str, new_pkgname: str,
                  upstream_checksums: list[str]) -> None:
    text = (staging / "template").read_text(encoding="utf-8")
    editor = XbpsTemplateEditor(text)

    # ASSERT-A: no residual "linux<series>" identifier tokens.
    if re.search(rf"(?<![\w.+-])linux{re.escape(series)}(?![\w.+-])", text):
        raise TemplateSynthesisError(
            f"ASSERT-A failed: residual 'linux{series}' token in template")

    # ASSERT-B: pkgname and the headers subpackage were renamed.
    if editor.pkgname() != new_pkgname:
        raise TemplateSynthesisError(
            f"ASSERT-B failed: pkgname is {editor.pkgname()!r}, not {new_pkgname!r}")
    if f"{new_pkgname}-headers_package()" not in text:
        raise TemplateSynthesisError(
            f"ASSERT-B failed: {new_pkgname}-headers_package() not defined")

    # ASSERT-C: checksums byte-identical to upstream (we add no distfiles).
    if editor.checksum_lines() != upstream_checksums:
        raise TemplateSynthesisError(
            "ASSERT-C failed: checksum lines diverge from upstream "
            "(regeneration must inherit them byte-for-byte)")


# ==========================================================================
# detect_march — §1.2 etc/conf ABI recommender (NOT a kernel-template flag)
# ==========================================================================
def detect_march(cpuinfo_path: str = "/proc/cpuinfo") -> str:
    """Recommend the ``etc/conf`` ``-march`` level from host CPU flags (§1.2).

    Ladder — the highest level the host can *prove* wins:

        x86-64-v4   full AVX-512 subset (avx512f/bw/cd/dq/vl)
        x86-64-v3   avx2 + fma + bmi2   (Haswell / Zen 1 and newer)
        x86-64-v2   sse4_2 + popcnt     (Nehalem+; e.g. Ivy Bridge has NO AVX2)
        x86-64      anything older

    v3 binaries fault with SIGILL on v2-only hosts (real deployment targets
    exist — pre-Haswell laptops), so absence of proof always degrades, never
    upgrades: an unreadable cpuinfo recommends the v2 safe floor. This feeds the
    *userland* profile in ``etc/conf`` (§1.1/§1.2); the kernel stays stock (§2.4).
    """
    try:
        with open(cpuinfo_path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError:
        return "x86-64-v2"   # cannot prove v3; v2 is the safe floor for any
                             # gaming-relevant x86_64 host (Nehalem, 2008+)
    flags: set[str] = set()
    for line in text.splitlines():
        if line.startswith("flags") and ":" in line:
            flags = set(line.split(":", 1)[1].split())
            break
    if {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"} <= flags:
        return "x86-64-v4"
    if {"avx2", "fma", "bmi2"} <= flags:
        return "x86-64-v3"
    if {"sse4_2", "popcnt"} <= flags:
        return "x86-64-v2"
    return "x86-64"
