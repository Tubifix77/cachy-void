"""Kernel Injection State Manager (KISM) — architecture.md §8.

Consolidates the kernel-side machinery the task grouped under grub.py:
  * persistent kernel state (§8.1) with atomic, crash-safe writes;
  * upstream bump detection & classification (§8.2);
  * the G2 config gate (§8.5) that catches silent ``oldconfig`` symbol drops
    before a mis-patched kernel can ever become a boot entry;
  * boot-layout detection (§8.6 preflight) that degrades to manual mode on
    grubenv-hostile filesystems and *skips physical operations entirely* under
    WSL2 / any layout without a writable ``/boot/grub/grub.cfg``;
  * deterministic GRUB menu-ref resolution (§8.6); and
  * one-shot staging with a known-good fallback default (§8.6).

Every external interaction (subprocess, filesystem) is injectable so the whole
module is unit-testable without a real bootloader.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

from .atomicio import read_json, write_json_atomic

SCHEMA = 1

# Filesystems on which GRUB can rewrite grubenv for a one-shot entry (§8.6).
GRUBENV_SAFE_FS = {"ext2", "ext3", "ext4", "vfat"}

# Boot-layout modes (§8.6 mode split — the two manual classes MUST stay distinct).
MODE_ONESHOT = "oneshot"                # grub-reboot one-shot promotion available
MODE_MANUAL = "manual"                  # safe: saved-default works, no one-shot
                                        #   (grubenv-hostile fs, user picks entry)
MODE_MANUAL_UNSAFE = "manual-unsafe"    # GRUB_DEFAULT != saved: pinning is a
                                        #   silent no-op -> staging must REFUSE
MODE_SKIP = "skip"                      # no usable bootloader (WSL2/virtualized)


class GrubError(RuntimeError):
    """A kernel/boot-staging operation could not be completed (exit 70)."""


def _default_run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


# ==========================================================================
# §8.1 Persistent kernel state
# ==========================================================================
def default_state(base_series: str = "", ported_version: str = "") -> dict:
    return {
        "schema": SCHEMA,
        "state": "TRACKING",
        "base_series": base_series,
        "ported_version": ported_version,   # full "X.Y.Z_R" pkgver (see §8.2 note)
        "candidate": None,
        "known_good": None,
        "grub": None,
        "bore": None,
        "services_up_at_staging": [],
        "staged_boot_id": None,
        "history": [],
    }


class KernelStateStore:
    """Load/save the §8.1 kernel state atomically."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict:
        if not self.path.exists():
            return default_state()
        return read_json(self.path)

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(self.path, data)


# ==========================================================================
# §8.2 Bump detection & classification
# ==========================================================================
_VERSION_RE = re.compile(r"^version=([0-9][0-9.]*)\s*$", re.M)
_REVISION_RE = re.compile(r"^revision=([0-9]+)\s*$", re.M)

# Classification events.
EV_NONE = "NONE"
EV_BUMP_PATCHLEVEL = "BUMP_PATCHLEVEL"
EV_AWAIT_HUMAN_SERIES = "AWAIT_HUMAN_SERIES"


def parse_template_pkgver(template_text: str) -> str:
    """Extract ``version_revision`` from a void-packages kernel template."""
    mv = _VERSION_RE.search(template_text)
    mr = _REVISION_RE.search(template_text)
    if not mv or not mr:
        raise GrubError("could not parse version=/revision= from template")
    return f"{mv.group(1)}_{mr.group(1)}"


def classify_bump(*, series_template_text: Optional[str], ported_version: str,
                  vercmp: Callable[[str, str], int]) -> tuple[str, Optional[str]]:
    """Classify the current upstream state against the tracked kernel (§8.2).

    Returns ``(event, template_pkgver)``. ``series_template_text`` is None when
    the tracked series template has disappeared upstream (series EOL).
    """
    if series_template_text is None:
        return EV_AWAIT_HUMAN_SERIES, None
    tmpl = parse_template_pkgver(series_template_text)
    if not ported_version or vercmp(tmpl, ported_version) > 0:
        return EV_BUMP_PATCHLEVEL, tmpl
    return EV_NONE, tmpl


# ==========================================================================
# §8.5 G2 config gate
# ==========================================================================
_SET_RE = re.compile(r"^(CONFIG_\w+)=")
_NOTSET_RE = re.compile(r"^#\s*(CONFIG_\w+) is not set\s*$")


def g2_config_gate(dotconfig_text: str, fragment_text: str) -> tuple[bool, list[str]]:
    """Verify every symbol of the §2.4 fragment survived ``oldconfig`` (§8.5).

    The failure this guards against is silent: if the BORE patch did not
    introduce e.g. ``CONFIG_SCHED_BORE``'s Kconfig entry, ``oldconfig`` deletes
    the unknown symbol *without error* and a stock-scheduler kernel builds
    "fine". Returns ``(ok, missing)`` where ``missing`` lists the fragment lines
    that are not satisfied by the generated ``.config``.

    Rules (§8.5):
      * ``CONFIG_X=v``            must appear literally in the .config.
      * ``# CONFIG_X is not set`` must appear literally OR the symbol must be
        entirely absent (no ``CONFIG_X=...`` line).
      * comment/section lines in the fragment are ignored.
    """
    config_lines = [ln.rstrip("\n") for ln in dotconfig_text.splitlines()]
    config_set = {ln.strip() for ln in config_lines}
    set_symbols = {m.group(1) for ln in config_lines
                   if (m := _SET_RE.match(ln.strip()))}

    missing: list[str] = []
    for raw in fragment_text.splitlines():
        line = raw.strip()
        if not line:
            continue
        notset = _NOTSET_RE.match(line)
        if notset:
            sym = notset.group(1)
            if line in config_set:
                continue                    # literally "is not set"
            if sym in set_symbols:
                missing.append(line)        # fragment wants it off, .config sets it
            # otherwise the symbol is absent -> satisfied
            continue
        if line.startswith("#"):
            continue                        # section/explanatory comment
        if _SET_RE.match(line):
            if line not in config_set:
                missing.append(line)
    return (not missing, missing)


# ==========================================================================
# §8.6 Boot layout detection (with WSL2 / virtualized graceful degradation)
# ==========================================================================
@dataclass
class BootLayout:
    mode: str                    # MODE_ONESHOT | MODE_MANUAL | MODE_SKIP
    reason: str
    grub_cfg: Optional[str] = None
    default_grub: Optional[str] = None


def is_wsl(proc_version_path: str = "/proc/version") -> bool:
    try:
        with open(proc_version_path, encoding="utf-8") as fh:
            text = fh.read().lower()
    except OSError:
        return False
    return "microsoft" in text or "wsl" in text


def detect_boot_layout(*,
                       grub_cfg: str = "/boot/grub/grub.cfg",
                       default_grub: str = "/etc/default/grub",
                       run: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_run,
                       exists: Callable[[str], bool] = os.path.exists,
                       wsl: Optional[bool] = None) -> BootLayout:
    """Decide how (or whether) we can drive the bootloader here (§8.6 preflight).

    Under WSL2, or any layout lacking a writable ``/boot/grub/grub.cfg``, returns
    MODE_SKIP so callers perform *no physical boot operations* and only warn.
    """
    if wsl is None:
        wsl = is_wsl()
    if wsl:
        return BootLayout(MODE_SKIP,
                          "WSL2 detected: no real bootloader; skipping all boot "
                          "operations (kernel staging is a no-op here)")
    if not exists(grub_cfg):
        return BootLayout(MODE_SKIP,
                          f"{grub_cfg} not found: virtualized, non-GRUB, or a "
                          "foreign boot manager (e.g. another distro's GRUB owns "
                          "boot in a multi-boot setup); skipping boot operations")

    # The saved-default check comes FIRST: without it, grub-set-default writes
    # are silently ignored and no manual fallback pinning is possible either.
    if not _grub_default_is_saved(default_grub, exists):
        return BootLayout(MODE_MANUAL_UNSAFE,
                          "GRUB_DEFAULT is not 'saved': grub-set-default writes "
                          "would be silently ignored and the newest installed "
                          "kernel becomes the default. Remedy: run "
                          "`deploy.sh --with-grub` (sanctioned edit), then retry",
                          grub_cfg=grub_cfg, default_grub=default_grub)

    fstype = _fstype_of(os.path.dirname(grub_cfg), run)
    if fstype not in GRUBENV_SAFE_FS:
        return BootLayout(MODE_MANUAL,
                          f"/boot filesystem {fstype or 'undeterminable'!r} cannot "
                          "host a GRUB one-shot; falling back to manual selection "
                          "(saved default still pins the known-good fallback)",
                          grub_cfg=grub_cfg, default_grub=default_grub)

    return BootLayout(MODE_ONESHOT, "grubenv-writable GRUB layout",
                      grub_cfg=grub_cfg, default_grub=default_grub)


def _fstype_of(path: str, run) -> str:
    """Filesystem type via findmnt; '' when undeterminable (missing binary
    included — degraded environments must degrade the MODE, never traceback)."""
    try:
        cp = run(["findmnt", "-no", "FSTYPE", "--target", path])
    except OSError:
        return ""
    return cp.stdout.strip() if cp.returncode == 0 else ""


def _grub_default_is_saved(default_grub: str, exists) -> bool:
    if not exists(default_grub):
        return False
    try:
        with open(default_grub, encoding="utf-8") as fh:
            for line in fh:
                if line.strip() == "GRUB_DEFAULT=saved":
                    return True
    except OSError:
        return False
    return False


# ==========================================================================
# §8.6 GRUB menu-ref resolution (deterministic, id-only)
# ==========================================================================
_MENU_ID_RE = re.compile(r"\$menuentry_id_option\s+'([^']+)'")


def locate_dotconfig(void_packages: str | os.PathLike) -> Path:
    """Deterministically locate the generated kernel .config (§8.5, G2).

    The glob MUST match exactly one file: zero means configure never ran,
    several means stale builddirs could feed the wrong config — both are gate
    failures, never a guess.
    """
    import glob as _glob
    pattern = os.path.join(os.fspath(void_packages),
                           "masterdir*", "builddir", "linux*", ".config")
    matches = sorted(_glob.glob(pattern))
    if len(matches) != 1:
        raise GrubError(
            f"expected exactly one kernel .config under {pattern!r}, "
            f"found {len(matches)}: {matches or 'none'}")
    return Path(matches[0])


def parse_menu_entries(grub_cfg_text: str) -> list[tuple[str, str]]:
    """Return ``(ref, entry_id)`` for every menuentry, honoring submenu nesting.

    ``ref`` is ``<submenu_id>>...>``-prefixed per GRUB's ``submenu>entry`` syntax.
    Only ``$menuentry_id_option`` ids are used — menu *titles* are never matched.
    """
    entries: list[tuple[str, str]] = []
    submenu_stack: list[tuple[int, str]] = []   # (depth_at_open, id)
    depth = 0
    for line in grub_cfg_text.splitlines():
        stripped = line.strip()
        m = _MENU_ID_RE.search(line)
        if stripped.startswith("submenu") and m:
            submenu_stack.append((depth, m.group(1)))
        elif stripped.startswith("menuentry") and m:
            prefix = ">".join(sid for _, sid in submenu_stack)
            ref = f"{prefix}>{m.group(1)}" if prefix else m.group(1)
            entries.append((ref, m.group(1)))
        depth += line.count("{") - line.count("}")
        while submenu_stack and depth <= submenu_stack[-1][0]:
            submenu_stack.pop()
    return entries


def resolve_menu_ref(grub_cfg_text: str, kver: str) -> str:
    """Resolve the unique GRUB ref whose entry id contains ``kver`` (§8.6).

    Requires exactly one match; zero or several raise :class:`GrubError`.
    """
    matches = [ref for ref, eid in parse_menu_entries(grub_cfg_text) if kver in eid]
    if len(matches) != 1:
        raise GrubError(
            f"expected exactly one GRUB entry for kernel {kver!r}, found {len(matches)}")
    return matches[0]


# ==========================================================================
# §8.6 One-shot staging with known-good fallback
# ==========================================================================
@dataclass
class StageResult:
    mode: str
    candidate_ref: Optional[str] = None
    default_ref: Optional[str] = None
    actions: list[str] = field(default_factory=list)   # commands actually issued


def stage_candidate(*, layout: BootLayout, candidate_kver: str,
                    known_good_kver: str,
                    run: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_run,
                    read_text: Callable[[str], str] = None) -> StageResult:
    """Stage a candidate kernel for a single trial boot (§8.6).

    * MODE_SKIP          -> no physical action (WSL2/virtualized).
    * MODE_MANUAL_UNSAFE -> REFUSED: pinning would be a silent no-op; the caller
      must surface the refusal (exit 70) and the remedy. No commands are issued.
    * MODE_MANUAL        -> pin the default to the known-good kernel (works:
      saved default is readable at boot); the user selects the candidate.
    * MODE_ONESHOT       -> pin default to known-good AND arm a one-shot for the
      candidate, so a panic/hang returns to known-good with zero interaction.
    """
    if layout.mode in (MODE_SKIP, MODE_MANUAL_UNSAFE):
        return StageResult(layout.mode, actions=[])

    if read_text is None:
        def read_text(p):  # noqa: E306
            with open(p, encoding="utf-8") as fh:
                return fh.read()

    cfg = read_text(layout.grub_cfg)
    good_ref = resolve_menu_ref(cfg, known_good_kver)
    cand_ref = resolve_menu_ref(cfg, candidate_kver)

    actions: list[str] = []

    def _do(args):
        actions.append(" ".join(args))
        cp = run(args)
        if cp.returncode != 0:
            raise GrubError(f"command failed: {' '.join(args)}\n{cp.stderr}")

    # The fallback default is always the proven kernel.
    _do(["grub-set-default", good_ref])
    if layout.mode == MODE_ONESHOT:
        _do(["grub-reboot", cand_ref])     # consumed on next boot only

    return StageResult(layout.mode, candidate_ref=cand_ref,
                       default_ref=good_ref, actions=actions)


def promote(*, layout: BootLayout, candidate_kver: str,
            run: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_run,
            read_text: Callable[[str], str] = None) -> Optional[str]:
    """Make the candidate the persistent default after a healthy boot (§8.7).

    Returns the promoted ref, or None under MODE_SKIP / MODE_MANUAL_UNSAFE
    (where a grub-set-default write would be a silent no-op).
    """
    if layout.mode in (MODE_SKIP, MODE_MANUAL_UNSAFE):
        return None
    if read_text is None:
        def read_text(p):  # noqa: E306
            with open(p, encoding="utf-8") as fh:
                return fh.read()
    cfg = read_text(layout.grub_cfg)
    ref = resolve_menu_ref(cfg, candidate_kver)
    cp = run(["grub-set-default", ref])
    if cp.returncode != 0:
        raise GrubError(f"grub-set-default {ref} failed: {cp.stderr}")
    return ref
