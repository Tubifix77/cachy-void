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
    subpackages: list[str] = None  # sibling symlinks created (headers, dbg, …)


_SUBPKG_FN_RE = re.compile(r"^([A-Za-z0-9][\w+.-]*)_package\(\)", re.M)


def _link_subpackages(srcpkgs: Path, final: Path, new_pkgname: str) -> list[str]:
    """Create srcpkgs/<sub> -> <new_pkgname> for every <sub>_package() function.

    Void's xbps-src identifies subpackages by these functions and resolves each
    through a sibling symlink; the symlinks live beside the template dir, so they
    are created after the atomic swap and refreshed idempotently.
    """
    text = (final / "template").read_text(encoding="utf-8")
    subs = sorted({m.group(1) for m in _SUBPKG_FN_RE.finditer(text)
                   if m.group(1) != new_pkgname})
    for sub in subs:
        link = srcpkgs / sub
        if link.is_symlink():
            if os.readlink(link) == new_pkgname:
                continue
            link.unlink()
        elif link.is_dir():
            raise TemplateSynthesisError(
                f"{link} exists as a real directory, not our subpackage symlink")
        elif link.exists():
            link.unlink()
        os.symlink(new_pkgname, link)   # relative, matches Void convention
    return subs


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

        # UNIQUE RELEASE STRING (real-hardware finding #8): the stock kernel at
        # the same version owns /boot/vmlinuz-<ver>_<rev> and /usr/lib/modules/
        # <ver>_<rev> — and xbps does NOT refuse the file collision, it silently
        # takes ownership and overwrites the stock kernel (destroying the boot
        # fallback). Suffix both the template's _kernver and the dotconfig's
        # CONFIG_LOCALVERSION so our kernel installs strictly side-by-side.
        _apply_release_suffix(staging, new_pkgname)

        _assert_regen(staging, series, new_pkgname, upstream_checksums)

        # Atomic swap: replace any existing fork in a single rename.
        final = srcpkgs / new_pkgname
        if final.exists():
            doomed = work / ".old"
            os.replace(final, doomed)
        os.replace(staging, final)

        # §7.1/§8.4: xbps-src derives subpackages from the <pkgname>-<sub>_package()
        # functions and REQUIRES a sibling symlink srcpkgs/<sub> -> <pkgname> for
        # each (upstream ships these; a bare dir copy does not). Without them the
        # build compiles fully and then dies at packaging: "nonexistent file:
        # srcpkgs/<pkgname>-<sub>/template" (real-hardware finding, first kernel).
        subs = _link_subpackages(srcpkgs, final, new_pkgname)

        version, revision = XbpsTemplateEditor(new_text).parse_pkgver()
        return SynthesisResult(
            pkgver=f"{version}_{revision}",
            srcpkg_dir=final,
            checksum_lines=upstream_checksums,
            subpackages=subs)
    finally:
        shutil.rmtree(work, ignore_errors=True)


RELEASE_SUFFIX = "-cachy"

# The packaging side: _kernver drives /boot names, the module dir the template
# cd's into, and the kernel hooks version.
_KERNVER_LINE_RE = re.compile(r'^(_kernver="\$\{version\}_\$\{revision\})(")', re.M)
# The kernel side: Void kernel templates FORCE CONFIG_LOCALVERSION="_${revision}"
# onto .config at configure time (a sed that overrides the dotconfig), and that
# is what sets the kernel's own release string (uname -r, modules_install dir).
# The suffix must go HERE, coupled with _kernver, or the two disagree and
# do_install cd's into a modules dir make never created (real-hardware #11).
_LV_SETTER_RE = re.compile(r'(CONFIG_LOCALVERSION=[^\n]*?_\$\{revision\})(\\?")')


def _apply_release_suffix(staging: Path, new_pkgname: str) -> None:
    """Make the kernel release unique & self-consistent: ...95_1 -> ...95_1-cachy.

    Both edits live in the TEMPLATE (found by looking at build ground truth, not
    deduction): ``_kernver`` (packaging paths) and the configure-time
    ``CONFIG_LOCALVERSION`` setter (the kernel's real release). Editing the
    dotconfig is futile — the setter clobbers it. Both must carry the suffix or
    packaging and the built kernel disagree.
    """
    tpl_path = staging / "template"
    text = tpl_path.read_text(encoding="utf-8")

    text, n_kv = _KERNVER_LINE_RE.subn(rf"\1{RELEASE_SUFFIX}\2", text)
    if n_kv != 1:
        raise TemplateSynthesisError(
            'ASSERT-D failed: expected exactly one _kernver="${version}_'
            f'${{revision}}" line, found {n_kv}')

    text, n_lv = _LV_SETTER_RE.subn(rf"\1{RELEASE_SUFFIX}\2", text)
    if n_lv != 1:
        raise TemplateSynthesisError(
            'ASSERT-D failed: expected exactly one configure-time '
            'CONFIG_LOCALVERSION="_${revision}" setter to suffix, found '
            f'{n_lv} — the built kernel release would not match _kernver')

    tpl_path.write_text(text, encoding="utf-8")


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
