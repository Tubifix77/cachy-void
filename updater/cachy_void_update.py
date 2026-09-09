#!/usr/bin/env python3
"""cachy-void-update — unified updater CLI (architecture.md §4, §7, §8).

Wires the dependency solver (§7), the transaction journal (§7.6), the build/
deploy execution (§4.4-§4.7) and the kernel state manager (§8) behind four
semantic actions:

    --sync       Stage 1: rebase void-packages onto upstream master (§4.2)
    --check      Stage 2: compute and print the build/deploy queue (read-only)
    --commit     Stages 3-4: build the queue, deploy it, run the G2 gate for a
                 queued kernel (§8.5), and stage the candidate for a one-shot
                 trial boot (§8.6)
    --rollback   re-pin the GRUB default to the known-good kernel (§8.6/§8.7)
    --pin-bore   assisted §8.3 trust pin: fetch+hash the BORE patch for the
                 tracked series and write bore.lock on explicit human approval
                 (the GUI's 'Pin BORE patch' button wraps this)

Error-boundary contract (§4.8): every path returns an exit code from the table
below; a traceback reaching the user is itself a bug (last-resort boundary in
``main``). Kernel-path stalls (G2 failure, staging refusal) never block
userspace updates (§8 preamble).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from engine.ddre import build_queue, topo_order, CycleError, MappingError  # noqa: E402
from engine.journal import Journal, crash_report  # noqa: E402
from engine.xbps import Xbps, XbpsError, ParseError, split_pkgver  # noqa: E402
from engine.atomicio import read_json, sweep_tmp  # noqa: E402
from engine.health import HealthChecker  # noqa: E402
from engine import health as _health_mod  # noqa: E402
from engine.health_daemon import HealthDaemon, DaemonConfig, DEGRADED, HEALTHY  # noqa: E402
from engine import grub  # noqa: E402
from engine import trust  # noqa: E402
from engine import template  # noqa: E402
from engine import snapshot  # noqa: E402

# -- exit codes (§4.8 / §7.8 / §8) -----------------------------------------
EXIT_OK = 0
EXIT_USAGE = 1
EXIT_LOCKED = 10
EXIT_SYNC = 20
EXIT_BOOTSTRAP = 21
EXIT_QUERY = 30
EXIT_PREFLIGHT = 31
EXIT_CYCLE = 32
EXIT_MAPPING = 33
EXIT_BUILD = 40
EXIT_INDEX = 50
EXIT_INSTALL = 51
EXIT_VERIFY = 52
EXIT_SNAPSHOT_UNAVAIL = 53
EXIT_SNAPSHOT_FAILED = 54
EXIT_CLEAN = 55
EXIT_FLATPAK = 56
EXIT_SERVICES = 60
EXIT_KERNEL = 70

KERNEL_TARGET = "linux-cachy"
DEFAULT_CONFIG = "/etc/cachy-void/updater.toml"


# ==========================================================================
# Configuration (§4.1 / §8.9)
# ==========================================================================
@dataclass
class Config:
    void_packages: Path
    jobs: int = 0                                   # 0 -> nproc
    min_free_gib: int = 30                          # §7.5 floor, kernel builds
    min_free_userspace_gib: int = 5                 # §7.5 floor, everything else
    masterdir: Optional[Path] = None                # §7.5 build space (None -> default)
    targets: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    restart_skip: list[str] = field(default_factory=list)
    state_dir: Path = Path("/var/lib/cachy-void")
    log_root: Path = Path.home() / ".local/state/cachy-void/log"
    kernel_enable: bool = True
    fragment_path: Path = Path("/etc/cachy-void/cachy-fragment.config")
    bore_lock: Optional[Path] = None      # None -> script-adjacent bore.lock
    snapshot_enable: str | bool = "auto"  # §9.5: "auto" | True | False
    snapshot_subvol: str = "/"
    snapshot_dir: str = "/.cachy-snapshots"
    snapshot_keep: int = 5

    @property
    def bore_lock_path(self) -> Path:
        return self.bore_lock or (Path(__file__).resolve().parent / "bore.lock")

    @property
    def kernel_patch_path(self) -> Path:
        # §8.3 reuse-first cache path (also what synthesize rewrites).
        return (self.void_packages / "srcpkgs" / KERNEL_TARGET /
                "patches" / "0001-bore.patch")

    @property
    def build_space(self) -> Path:
        """The directory that will hold the build chroot.

        The configured masterdir when set, else the void-packages checkout
        (whose `masterdir-<arch>` xbps-src creates itself). One question,
        one answer -- the preflight, the disk report, the G2 gate and the
        xbps-src invocation must never disagree about where the build is.
        """
        return self.masterdir or self.void_packages

    @property
    def repos(self) -> list[Path]:
        base = self.void_packages / "hostdir" / "binpkgs"
        return [base, base / "nonfree"]

    @property
    def repo_strs(self) -> list[str]:
        return [str(r) for r in self.repos]

    @property
    def kernel_state_path(self) -> Path:
        # §8.1: kernel/ subdir is owned by the build user (deploy.sh) so the
        # unprivileged updater can record staging transitions.
        return self.state_dir / "kernel" / "kernel-state.json"

    @property
    def effective_jobs(self) -> int:
        return self.jobs if self.jobs > 0 else (os.cpu_count() or 1)


def load_config(path: str | Path) -> Config:
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    try:
        vp = raw["paths"]["void_packages"]
    except KeyError as exc:
        raise ValueError("config missing [paths] void_packages") from exc
    build = raw.get("build", {})
    pkgs = raw.get("packages", {})
    svc = raw.get("services", {})
    kern = raw.get("kernel", {})
    snap = raw.get("snapshot", {})
    cfg = Config(
        void_packages=Path(vp),
        jobs=int(build.get("jobs", 0)),
        min_free_gib=int(build.get("min_free_gib", 30)),
        min_free_userspace_gib=int(build.get("min_free_userspace_gib", 5)),
        masterdir=(Path(build["masterdir"]) if build.get("masterdir") else None),
        targets=list(pkgs.get("targets", [])),
        blacklist=list(pkgs.get("blacklist", [])),
        restart_skip=list(svc.get("restart_skip", [])),
        kernel_enable=bool(kern.get("enable", True)),
        snapshot_enable=snap.get("enable", "auto"),
        snapshot_subvol=str(snap.get("subvol", "/")),
        snapshot_dir=str(snap.get("dir", "/.cachy-snapshots")),
        snapshot_keep=int(snap.get("keep", 5)),
    )
    if kern.get("fragment"):
        cfg.fragment_path = Path(kern["fragment"])
    if kern.get("bore_lock"):
        cfg.bore_lock = Path(kern["bore_lock"])
    return cfg


def build_xbps(config: Config, run=None) -> Xbps:
    kwargs = {"void_packages": config.void_packages, "repos": config.repos,
              "masterdir": config.masterdir}
    if run is not None:
        kwargs["run"] = run
    return Xbps(**kwargs)


def build_health_daemon(config: Config, out=print, run=None,
                        layout=None) -> HealthDaemon:
    """Wire the §8.7 health daemon with a cmd_rollback-backed active rollback.

    The rollback callable closes over ``config`` so the watchdog's trip fires
    the real §8.6/§8.7 rollback path; the engine module stays CLI-agnostic.
    Under MODE_EXTERNAL the watchdog cannot drive the (foreign) bootloader, so
    active rollback is disabled — a trip records CANDIDATE_UNHEALTHY and tells
    the operator, instead of issuing grub commands that would be no-ops.
    """
    run = run or _run
    if layout is None:
        try:
            layout = grub.detect_boot_layout(run=run)
        except Exception:                       # noqa: BLE001 - inert-safe
            layout = grub.BootLayout(grub.MODE_SKIP, "layout undeterminable")
    checker = HealthChecker(run=lambda args: run(args))
    store = grub.KernelStateStore(config.kernel_state_path)
    return HealthDaemon(
        checker=checker,
        state_store=store,
        rollback=lambda: cmd_rollback(config, out=out, run=run),
        config=DaemonConfig(),
        out=out,
        active_rollback=(layout.mode not in
                         (grub.MODE_EXTERNAL, grub.MODE_SKIP,
                          grub.MODE_MANUAL_UNSAFE)),
        promote_grub=lambda kver: grub.promote(
            layout=layout, candidate_kver=kver, run=_sudo(run)),
    )


# ==========================================================================
# Process helpers
# ==========================================================================
def _run(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    # stdin=DEVNULL: no child may ever WAIT on a prompt. An unanswered kconfig
    # symbol once parked `make oldconfig` on a question for 18 hours (§8.4,
    # real-hardware finding); with EOF on stdin the same mistake fails fast and
    # loud instead — the kernel is withheld, userspace proceeds (fail-safe).
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL)


def _sudo(run) -> Callable[[Sequence[str]], subprocess.CompletedProcess]:
    """Prefix privileged commands with the §4 sudoers boundary (-n: NOPASSWD)."""
    return lambda args: run(["sudo", "-n", *args])


# ==========================================================================
# Kernel-side helpers (KISM wiring, §8)
# ==========================================================================
def _always_build(config: Config) -> list[str]:
    """§7.3 K-exemption: queue the kernel even though it is not installed —
    but only once its template actually exists (post-synthesis)."""
    if not config.kernel_enable:
        return []
    try:
        tpl = config.void_packages / "srcpkgs" / KERNEL_TARGET / "template"
        return [KERNEL_TARGET] if tpl.is_file() else []
    except OSError:
        return []


def upstream_series_version(series: str, run) -> str:
    """The linux<series> version Void is offering RIGHT NOW, or "".

    Read from the REPOSITORY index with -M (memory sync), not from the local
    void-packages checkout. The checkout only advances when something runs a
    sync, so a box that has not synced recently cannot see a kernel Void
    shipped days ago — which is exactly what the testbed hit: a nine-day-old
    checkout still reading 6.12.103 while 6.12.104 sat in the very same
    --status output as a held package. The repository is always current for the
    cost of one unprivileged index fetch, so detection asks it too.

    The checkout still governs the BUILD (§8.4 regenerates the template from
    it), and Update's first step is a sync — so a bump found here is buildable
    by the time anything acts on it.
    """
    try:
        cp = run(["xbps-query", "-R", "-M", "-p", "pkgver", f"linux{series}"])
    except OSError:
        return ""
    if cp.returncode != 0:
        return ""
    token = (cp.stdout or "").strip().splitlines()
    if not token:
        return ""
    try:
        return split_pkgver(token[0].strip())[1]
    except (ValueError, KeyError, ParseError):
        return ""   # a repo that answers something unparseable is a no-answer


def _checkout_age_days(void_packages) -> float:
    """Days since the void-packages checkout last fetched, or -1 if unknowable.

    ``.git/FETCH_HEAD`` is rewritten by every fetch, so its mtime is the honest
    answer to "when was this check last able to see anything new".
    """
    for rel in (".git/FETCH_HEAD", ".git/HEAD"):
        try:
            mtime = (pathlib.Path(void_packages) / rel).stat().st_mtime
        except OSError:
            continue
        return max(0.0, (time.time() - mtime) / 86400.0)
    return -1.0


def _kernel_report(config: Config, xbps, out, run=_run) -> None:
    """§8.2 bump classification — informational (template regen §8.4 is a
    human step for now). Never fails the run."""
    if not config.kernel_enable:
        return
    try:
        state = grub.KernelStateStore(config.kernel_state_path).load()
        series = state.get("base_series") or ""
        if not series:
            return
        # §8.3a pin visibility: say OUT LOUD whether this box's series is
        # vouched for. "The kernel silently never updates" must never be a
        # mystery state — the GUI keys its 'Pin BORE patch' banner off the
        # exact substring "BORE pin: MISSING".
        try:
            lock = trust.load_bore_lock(config.bore_lock_path)
            entry = lock.patches.get(series)
            if entry:
                out(f"BORE pin: series {series} pinned"
                    + (f" (BORE {entry.bore_version})" if entry.bore_version else "")
                    + (f" — {entry.approved}" if entry.approved else ""))
            else:
                out(f"BORE pin: MISSING for series {series} — kernel updates "
                    "stay paused until you approve the patch (the updater's "
                    "'Pin BORE patch' button, or: cachy-void-update --pin-bore)")
        except trust.TrustConfigError as exc:
            out(f"BORE pin: bore.lock unusable ({exc})")

        good = (state.get("known_good") or {}).get("kver") or ""
        # Staged-candidate visibility (§8.6): between "kernel built" and
        # "kernel proven" the candidate sits in the state store and appeared
        # NOWHERE — the one kernel state a user could not see. What happens if
        # it misbehaves depends entirely on the boot class, so say which one
        # applies here rather than describing a rollback the host cannot do.
        name = state.get("state") or ""
        cand = (state.get("candidate") or {}).get("kver") or ""
        mode = (state.get("grub") or {}).get("mode") or ""
        if cand and name == "STAGED":
            out(f"kernel candidate: {cand} is staged, awaiting its trial boot "
                "— reboot when convenient (the updater never reboots you)")
            if mode == grub.MODE_ONESHOT:
                out(f"  if it fails to boot, the next power cycle returns to "
                    f"{good or 'the known-good kernel'} on its own (one-shot)")
            elif mode == grub.MODE_EXTERNAL:
                out(f"  a foreign bootloader owns the menu: if it misbehaves, pick "
                    f"{good or 'the known-good kernel'} there yourself")
            elif mode:
                out(f"  the boot default stays on {good or 'the known-good kernel'}; "
                    "select the candidate in the menu to try it")
            # §8.6b, said HERE and not only in the deploy log. The moment a
            # person most needs "will a reboot actually land on the candidate?"
            # is while reading this and deciding whether to reboot -- and that
            # was the one moment it stayed silent. It fired correctly during
            # the deploy hours earlier, into a runit log nobody reads.
            #
            # It matters most on exactly this host class: an `external` boot
            # class means nothing was staged with GRUB, so the whole question
            # is whether the operator's own convention -- an evergreen symlink,
            # a versioned menuentry -- currently resolves to the new kernel. A
            # stale symlink boots the OLD kernel and looks like success.
            _boot_layout = _boot_layout_for_report(config, run)
            if _boot_layout is not None:
                _report_boot_path(_boot_layout, cand, out, indent="  ")
        elif cand and name == "CONFIRMING":
            out(f"kernel candidate: {cand} booted and is ON TRIAL — the confirm "
                "service promotes it once its health battery passes")
        elif name in FROZEN_STATES:
            # Note the missing `cand and` guard: requiring a candidate here is
            # how a frozen kernel path stayed INVISIBLE on real hardware. The
            # freeze is the thing worth reporting, and it can outlive (or never
            # have had) a candidate at all.
            out(f"kernel path FROZEN ({name})")
            for line in frozen_explanation(name, cand or ""):
                out(f"      {line}")
            out("      Userspace updates are unaffected.")
            out("      resume kernel updates with:  cachy-void-update --kernel-ack")

        # Recovery visibility: if the running kernel is not the recorded
        # known-good one, say that going back is possible. The front-end keys
        # its rollback button off the exact substring "rollback available" —
        # otherwise recovery stays a CLI-only secret, which is no use to the
        # person whose kernel just misbehaved.
        try:
            running = os.uname().release
        except (AttributeError, OSError):
            running = ""
        if good and running and good != running:
            out(f"rollback available: running {running}, known-good {good}")

        # The drift verdict itself lives in kernel_port_available(), shared
        # with --pending. It used to live only here, which is why the tray
        # never mentioned a kernel port while this report did.
        v = kernel_port_available(config, state, run, xbps.vercmp)
        if v.event == grub.EV_AWAIT_HUMAN_SERIES:
            out(f"kernel: tracked series linux{series} is gone upstream — "
                "human decision required (§8.2).")
        elif v.available and v.source == "checkout":
            out(f"kernel: upstream linux{series} is at {v.upstream_version}; "
                f"ported base is {v.ported_version or '<none>'} — port "
                "linux-cachy (§2.6/§8.4).")
        elif v.available:
            # The repository answered where the checkout could not: say so, and
            # say how far behind the checkout is, since that is the reason.
            out(f"kernel: Void is shipping linux{series} {v.upstream_version}; "
                f"ported base is {v.ported_version} — port linux-cachy "
                "(§2.6/§8.4).")
            age = _checkout_age_days(config.void_packages)
            if age >= 1:
                out(f"  (found in the repository; the void-packages checkout "
                    f"here is {int(age)} days old and has not caught up — "
                    "Update syncs it as its first step.)")
        elif not v.upstream_version:
            # Could not ask either source: say so rather than implying "no".
            age = _checkout_age_days(config.void_packages)
            if age >= 2:
                out(f"kernel: could not query the repository, and the "
                    f"void-packages checkout here is {int(age)} days old — "
                    "run Update (or --sync) before trusting this as 'no "
                    "newer kernel'.")
    except (grub.GrubError, XbpsError, OSError) as exc:
        out(f"warning: kernel bump check skipped: {exc}")


def _kernel_synthesis(config: Config, xbps, out, *, fetcher=None) -> None:
    """Close the §8.2→§8.3→§8.4 circuit: detect bump → verify BORE patch →
    regenerate the linux-cachy template so it enters the §7 queue organically.

    Best-effort by contract (§8 preamble): every kernel-path failure is captured,
    recorded as the appropriate stall state, and returns cleanly so userspace
    updates proceed. Nothing here raises into the commit pipeline. ``fetcher`` is
    the injectable §8.3 patch fetcher (None → the real git fetcher).
    """
    if not config.kernel_enable:
        return
    store = grub.KernelStateStore(config.kernel_state_path)
    try:
        state = store.load()
    except OSError as exc:
        out(f"warning: cannot read kernel state ({exc}); skipping kernel synthesis")
        return

    # §8.8: a frozen state is a human gate, and until now it gated nothing --
    # the next run re-synthesised (overwriting the state with READY) and
    # rebuilt, which is how a box could fail the same six-hour build every
    # night while its "frozen" kernel path sat in a file nobody enforced.
    if state.get("state") in FROZEN_STATES:
        out(f"kernel: state is {state['state']} — the kernel path is frozen until "
            "a human acknowledges it (§8.8): no template regeneration and no "
            "build this run; userspace updates continue. Resume with: "
            "cachy-void-update --kernel-ack")
        return
    series = state.get("base_series") or ""
    if not series:
        out("kernel: no base_series tracked — synthesis needs a human to bootstrap "
            "the tracked series in kernel-state.json (§8.2); skipping.")
        return

    # §8.2 classify.
    try:
        tpath = config.void_packages / "srcpkgs" / f"linux{series}" / "template"
        text = tpath.read_text(encoding="utf-8") if tpath.exists() else None
        ev, tmpl = grub.classify_bump(series_template_text=text,
                                      ported_version=acted_version(state),
                                      vercmp=xbps.vercmp)
    except (grub.GrubError, XbpsError, OSError) as exc:
        out(f"warning: kernel bump classification failed ({exc}); skipping")
        return

    if ev == grub.EV_NONE:
        return
    if ev == grub.EV_AWAIT_HUMAN_SERIES:
        _record_kernel_state(config, {"state": "AWAIT_HUMAN_SERIES"}, out)
        out(f"kernel: tracked series linux{series} gone upstream — "
            "AWAIT_HUMAN_SERIES (§8.2); kernel withheld, userspace continues.")
        return

    # ev == BUMP_PATCHLEVEL — verify then regenerate.
    out(f"kernel: upstream bump to {tmpl} detected (§8.2); verifying BORE patch (§8.3)")
    _record_kernel_state(config, {"state": "PATCH_VERIFY"}, out)
    try:
        lock = trust.load_bore_lock(config.bore_lock_path)
        result = trust.ensure_trusted_patch(
            lock=lock, series=series, patch_path=config.kernel_patch_path,
            fetcher=fetcher, out=out)
        patch_bytes = config.kernel_patch_path.read_bytes()
    except trust.TrustConfigError as exc:
        _record_kernel_state(config, {"state": "AWAIT_HUMAN_PATCH"}, out)
        out(f"warning: bore.lock invalid ({exc}); kernel withheld "
            "(AWAIT_HUMAN_PATCH, §8.3). Userspace updates continue.")
        return
    except trust.HashMismatch as exc:
        _record_kernel_state(config, {"state": "HALT_HASH_MISMATCH"}, out)
        out(f"warning: BORE patch integrity FAILED ({exc}); kernel withheld "
            "(HALT_HASH_MISMATCH, §8.3 — possible tamper). Userspace continues.")
        return
    except trust.TrustError as exc:            # PatchUnavailable / NetworkError
        _record_kernel_state(config, {"state": "HALT_HASH_MISMATCH"}, out)
        out(f"warning: BORE patch unavailable ({exc}); kernel withheld (§8.3). "
            "Userspace updates continue.")
        return
    except OSError as exc:
        out(f"warning: patch trust step failed ({exc}); kernel withheld")
        return

    out(f"kernel: patch trusted ({result.source}); regenerating template (§8.4)")
    _record_kernel_state(config, {"state": "REGENERATE"}, out)
    try:
        fragment = config.fragment_path.read_text(encoding="utf-8")
        res = template.synthesize(
            void_packages=config.void_packages, series=series,
            patch_bytes=patch_bytes, fragment_text=fragment,
            new_pkgname=KERNEL_TARGET)
    except template.TemplateSynthesisError as exc:
        _record_kernel_state(config, {"state": "AWAIT_HUMAN_TEMPLATE"}, out)
        out(f"warning: template synthesis FAILED ({exc}); kernel withheld "
            "(AWAIT_HUMAN_TEMPLATE, §8.4). Userspace updates continue.")
        return
    except OSError as exc:
        _record_kernel_state(config, {"state": "AWAIT_HUMAN_TEMPLATE"}, out)
        out(f"warning: template synthesis I/O error ({exc}); kernel withheld "
            "(AWAIT_HUMAN_TEMPLATE, §8.4). Userspace updates continue.")
        return

    _record_kernel_state(config, {"state": "READY"}, out)
    out(f"kernel: regenerated {KERNEL_TARGET} {res.pkgver} — entering the build "
        "queue; the G2 gate (§8.5) runs before it compiles.")


def _g2_gate(config: Config, xbps, out) -> bool:
    """G2 (§8.5): configure the kernel template and verify every fragment
    symbol survived oldconfig. False = withhold the kernel this run."""
    try:
        fragment = config.fragment_path.read_text(encoding="utf-8")
    except OSError:
        out(f"warning: kernel fragment missing at {config.fragment_path}; "
            "the G2 gate cannot run and is never skipped (§8.5)")
        return False
    rc = xbps.configure(KERNEL_TARGET)
    if rc != 0:
        out(f"warning: ./xbps-src configure {KERNEL_TARGET} failed (rc={rc})")
        return False
    try:
        dotconfig = grub.locate_dotconfig(config.void_packages,
                                          config.masterdir).read_text(
            encoding="utf-8")
    except (grub.GrubError, OSError) as exc:
        out(f"warning: G2 .config extraction failed: {exc}")
        return False
    ok, missing = grub.g2_config_gate(dotconfig, fragment)
    if not ok:
        out("warning: G2 config gate FAILED — symbols silently dropped by "
            "oldconfig: " + "; ".join(missing))
    return ok


def _record_kernel_state(config: Config, updates: dict, out) -> None:
    """Best-effort persist of kernel-state transitions (§8.1)."""
    try:
        store = grub.KernelStateStore(config.kernel_state_path)
        state = store.load()
        state.update(updates)
        store.save(state)
    except OSError as exc:
        out(f"warning: could not persist kernel state: {exc}")


def _snapshot_services(run, service_root: str = "/var/service") -> list[str]:
    """Names of runit services currently up (H1 baseline, §8.6). Best-effort."""
    up: list[str] = []
    try:
        for name in sorted(os.listdir(service_root)):
            cp = run(["sv", "status", os.path.join(service_root, name)])
            if cp.returncode == 0 and cp.stdout.strip().startswith("run:"):
                up.append(name)
    except OSError:
        return []
    return up


def _boot_layout_for_report(config: Config, run) -> "object | None":
    """The boot layout for a READ-ONLY report, or None if it cannot be read.

    Separate from the staging path's resolution on purpose: a status readout
    must never fail, never mutate, and never make the caller handle an
    exception. Anything unreadable simply means the boot-path line is omitted
    rather than the whole tier collapsing.
    """
    try:
        return grub.detect_boot_layout(run=run)
    except (grub.GrubError, OSError, ValueError):
        return None


def _report_boot_path(layout, kver: str, out, indent: str = "") -> None:
    """§8.6b: say out loud whether the freshly installed kernel can be booted.

    Read-only and never fatal — a probe that cannot look degrades to a stated
    reason, because "we could not check" must never read as "your kernel is
    broken". This is the step between "installed" and "bootable" that nothing
    used to verify, and on a foreign-bootloader host it is also where the
    multi-boot truth gets said instead of inferred.
    """
    try:
        chk = grub.verify_bootable(layout=layout, kver=kver)
    except OSError as exc:
        out(f"{indent}boot check skipped: {exc}")
        return
    prefix = "WARNING — boot check" if chk.status == grub.BOOT_ABSENT else "boot check"
    out(f"{indent}{prefix}: {chk.detail}")
    if chk.hint:
        out(f"{indent}  {chk.hint}")


def _stage_kernel(config: Config, xbps, out, run, layout=None) -> int:
    """§8.6: stage the freshly deployed kernel for a one-shot trial boot.

    Returns EXIT_OK or EXIT_KERNEL; never raises (F7 boundary). ``layout`` is
    injectable for tests; production detects it live.
    """
    try:
        if layout is None:
            layout = grub.detect_boot_layout(run=run)
        if layout.mode == grub.MODE_SKIP:
            out(f"kernel staging skipped: {layout.reason}")
            return EXIT_OK
        if layout.mode == grub.MODE_MANUAL_UNSAFE:
            out(f"kernel staging REFUSED: {layout.reason}")
            out("the new kernel is installed and may already be the GRUB "
                "default with no pinned fallback — fix GRUB_DEFAULT before "
                "relying on automatic rollback.")
            _report_boot_path(layout, _kernel_release_of(xbps, KERNEL_TARGET), out)
            return EXIT_KERNEL

        cand_kver = _kernel_release_of(xbps, KERNEL_TARGET)
        store = grub.KernelStateStore(config.kernel_state_path)
        state = store.load()
        known = (state.get("known_good") or {}).get("kver") or _uname_r(run)
        if not known:
            out("kernel staging REFUSED: no known-good kernel identifiable")
            return EXIT_KERNEL
        if known == cand_kver:
            out(f"kernel staging skipped: candidate {cand_kver} equals the "
                "known-good kernel")
            return EXIT_OK

        res = grub.stage_candidate(layout=layout, candidate_kver=cand_kver,
                                   known_good_kver=known, run=_sudo(run))
        _record_kernel_state(config, {
            "state": "STAGED",
            "candidate": {"pkgver": xbps.inst_pkgver(KERNEL_TARGET),
                          "kver": cand_kver, "built": True, "installed": True},
            "known_good": {"kver": known, "grub_ref": res.default_ref},
            "grub": {"mode": res.mode, "candidate_ref": res.candidate_ref,
                     "default_ref": res.default_ref},
            "staged_boot_id": _boot_id(),
            "services_up_at_staging": _snapshot_services(run),
        }, out)
        if res.mode == grub.MODE_EXTERNAL:
            out(f"kernel {cand_kver} staged (external bookkeeping): a foreign "
                "bootloader owns the menu — no GRUB commands were issued. "
                f"Reboot when convenient; if {cand_kver} misbehaves, select the "
                f"known-good {known} entry in the boot menu yourself. "
                "NEVER auto-rebooting.")
        else:
            out(f"kernel {cand_kver} staged ({res.mode}): GRUB default pinned to "
                f"known-good {known}; reboot when convenient. NEVER auto-rebooting.")
        _report_boot_path(layout, cand_kver, out)
        return EXIT_OK
    except grub.GrubError as exc:
        out(f"error: kernel staging failed: {exc} — the deploy itself is "
            "intact; fall back to manual GRUB selection (§2.5)")
        return EXIT_KERNEL
    except (XbpsError, ParseError, OSError) as exc:
        out(f"error: kernel staging aborted: {exc}")
        return EXIT_KERNEL


def _kernel_release_of(xbps, pkg: str) -> str:
    """Kernel release string from the package's installed vmlinuz filename.

    Finding #8: the release carries a uniqueness suffix (§8.4), so it is NOT
    derivable from pkgver — the file list is the ground truth.
    """
    for path in xbps.files(pkg):
        token = path.split()[0] if path else ""
        if "/boot/vmlinuz-" in token:
            return token.rsplit("/boot/vmlinuz-", 1)[1]
    raise XbpsError(f"{pkg}: no /boot/vmlinuz-* found in its file list")


def _uname_r(run) -> Optional[str]:
    try:
        cp = run(["uname", "-r"])
        return cp.stdout.strip() or None
    except OSError:
        return None


def _boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


# ==========================================================================
# Witness reporting & litter sweep (§7.6, F9/F10)
# ==========================================================================
def _report_previous_run(config: Config, out) -> None:
    """Describe an interrupted previous run (witness-only — informs, never
    drives) and sweep .tmp-*.json litter from hard-killed atomic writes."""
    try:
        sweep_tmp(config.kernel_state_path.parent)
        if not config.log_root.is_dir():
            return
        runs = sorted(p for p in config.log_root.iterdir()
                      if p.is_dir() and p.name.startswith("run-"))
        for rd in runs:
            sweep_tmp(rd)
        if runs:
            rep = crash_report(runs[-1] / "journal.json")
            if rep.interrupted:
                out(f"note: {rep.note}")
                if rep.built:
                    out("note: previously built, pending deploy (P/O terms "
                        "will pick these up): " + ", ".join(rep.built))
    except OSError:
        pass


# ==========================================================================
# Actions
# ==========================================================================
def cmd_check(xbps, config: Config, out=print) -> int:
    """Stage 2 — compute and print the queue. Read-only (§7.3/§7.4)."""
    try:
        plan = build_queue(xbps, config.targets, config.blacklist,
                           config.repo_strs, always_build=_always_build(config))
        if not plan.q_build and not plan.q_deploy:
            out("queue empty — nothing to build or deploy.")
            _kernel_report(config, xbps, out)
            return EXIT_OK
        order = topo_order(xbps, plan.q_build)
    except MappingError as exc:
        out(f"error: srcpkg mapping anomaly: {exc}")
        return EXIT_MAPPING
    except CycleError as exc:
        out(f"error: {exc}")
        return EXIT_CYCLE
    except XbpsError as exc:              # ParseError included
        out(f"error: queue construction failed: {exc}")
        return EXIT_QUERY
    except OSError as exc:
        out(f"error: environment failure during queries: {exc}")
        return EXIT_QUERY

    out(f"build queue  ({len(plan.q_build)}): {', '.join(plan.q_build) or '-'}")
    out(f"deploy queue ({len(plan.q_deploy)}): {', '.join(plan.q_deploy) or '-'}")
    out(f"build order  [{order.provenance}]: {' -> '.join(order.order) or '-'}")
    if order.second_pass:
        out(f"convergence pass: {' -> '.join(order.second_pass)}")
    if KERNEL_TARGET in plan.q_deploy:
        out(f"note: {KERNEL_TARGET} is queued — a reboot will be required (§8.6).")
    _kernel_report(config, xbps, out)
    return EXIT_OK


# What counts as a hardware driver or firmware package. Deliberately WIDER than
# "GPU": this box alone carries intel-ucode (CPU microcode), sof/alsa-firmware
# (audio DSP), ipw2100/2200-firmware (WiFi) and five linux-firmware-* blobs, any
# of which can turn up in an update — so a label saying "gpu" would be wrong on
# the day one of them moves (owner asked exactly this).
_DRIVER_EXACT = {"intel-ucode", "amd-ucode"}
_DRIVER_PREFIXES = ("mesa", "libgbm", "libdrm", "nvidia",
                    "xf86-video-", "xf86-input-")


def is_driver_pkg(name: str) -> bool:
    """True for a hardware driver / firmware binpkg.

    32-bit twins count as the same kind of thing (Void ships mesa-32bit et al
    for Steam), and sub-packages count with their parent: a single mesa release
    arrives as mesa, mesa-dri, mesa-libgallium and libgbm, each doubled. That is
    why a "one driver update" can legitimately show as eight packages — the unit
    everywhere else in this interface is the PACKAGE, and changing units here to
    flatter a number would be its own kind of lie.
    """
    base = name[:-6] if name.endswith("-32bit") else name
    if base in _DRIVER_EXACT:
        return True
    if base.startswith("linux-firmware") or base.endswith("-firmware"):
        return True
    if "-firmware-" in base:
        return True
    return any(base == pre.rstrip("-") or base.startswith(pre)
               or (pre == "nvidia" and base.startswith("nvidia"))
               for pre in _DRIVER_PREFIXES)


def _count_upstream(cp) -> tuple[int, int]:
    """Split an ``xbps-install -un`` dry-run listing into (actionable, held).

    Held lines (pinned kernels etc.) are things Update rightly skips, so
    counting them as updatable is false-alarm noise — found on the Medion,
    where "4 updatable" were all pinned kernels. Shared by --status tier [1]
    and --pending so the two can never disagree.
    """
    lines = [l for l in (cp.stdout or "").splitlines() if l.strip()]
    n = len([l for l in lines
             if l.split()[1:2] and l.split()[1] in ("update", "install")])
    return n, len(lines) - n


def _split_drivers(cp) -> int:
    """How many of the ACTIONABLE pending packages are drivers/firmware."""
    n = 0
    for line in (cp.stdout or "").splitlines():
        f = line.split()
        if len(f) > 1 and f[1] in ("update", "install"):
            try:
                if is_driver_pkg(split_pkgver(f[0])[0]):
                    n += 1
            except (ValueError, KeyError):
                continue
    return n


def upstream_counts(run) -> tuple:
    """``(updatable, held, fresh, drivers)`` from a dry-run system update.

    ``-M`` (``--memory-sync``) fetches the remote index into memory for this one
    command: unprivileged, no cache write, and the answer is CURRENT. When the
    mirror is unreachable it falls back to the on-disk cache and reports
    ``fresh=False`` rather than passing stale numbers off as fact. ``updatable``
    is ``None`` when nothing could be determined at all.

    Shared by ``--status`` and ``--pending`` deliberately. Two front-ends of the
    same product showing different counts is worse than either number being
    wrong, and that is exactly what happened when the tray memory-synced while
    the window read the cache: 20 in one, 16 in the other, both "correct".
    """
    try:
        cp = run(["xbps-install", "-Mun"])
        if cp.returncode == 0:
            n, held = _count_upstream(cp)
            return n, held, True, _split_drivers(cp)
        cp = run(["xbps-install", "-un"])          # offline: the cache will do
        if cp.returncode == 0:
            n, held = _count_upstream(cp)
            return n, held, False, _split_drivers(cp)
    except OSError:
        pass
    return None, 0, False, 0


SCHED_SERVICE = "cachy-void-update"
SCHED_LINK = pathlib.Path("/var/service") / SCHED_SERVICE
SCHED_CONF = pathlib.Path("/etc/sv") / SCHED_SERVICE / "conf"


def _sv_state(link, run) -> str:
    """'run' | 'down' | '' for a runit service, via `sv status`.

    The supervise directory is 0700 root, so an unprivileged reader cannot see
    that a service was stopped with `sv down`. `sv` is inside the updater's own
    §4.1 grant (service cycling), so the sudo fallback is the same one the
    deploy stage already uses -- read-only here.
    """
    if run is None:
        return ""
    for argv in (["sv", "status", str(link)],
                 ["sudo", "-n", "sv", "status", str(link)]):
        try:
            cp = run(argv)
        except OSError:
            continue
        text = (cp.stdout or "").strip()
        if text.startswith("run:"):
            return "run"
        if text.startswith("down:"):
            return "down"
    return ""


def scheduled_run_line(*, link: pathlib.Path = SCHED_LINK,
                       conf: pathlib.Path = SCHED_CONF, run=None) -> str:
    """One line for the §4.9 unattended run when it is ON, else "".

    This exists because of a real surprise: the owner found the laptop's fans
    roaring at 3am, tracked it to a `linux-cachy` compile, and asked how the
    updater could start one when nobody pressed Update. The answer was the
    scheduled service, enabled at install time with `--with-schedule` months
    earlier. Nothing had been hidden — README, INSTALL and architecture.md all
    document it — but documentation read once on install day cannot compete
    with a program that never mentions it again.

    Note WHAT it runs: `--commit --yes`, with no `--no-kernel`. That is the
    "Update kernel" button, not "Update" — an unattended run can start a
    multi-hour kernel build and a reboot-gated deploy. Which is exactly the
    kind of user-facing behaviour the project's own rule says is the user's to
    choose, not ours to leave implicit.

    Printed only when ON. Off-and-unknown harms nobody; on-and-unknown is the
    case that cost a night's confusion, so only that one earns a line in every
    report. Unprivileged: a symlink test and one small file read.
    """
    try:
        if not link.exists():
            return ""
    except OSError:
        return ""
    when = ""
    try:
        text = conf.read_text(encoding="utf-8", errors="replace")
        got = {}
        for field in ("SNOOZE_HOUR", "SNOOZE_MINUTE"):
            m = re.search(rf"^{field}=(\S+)", text, re.M)
            if m:
                got[field] = m.group(1).strip().strip('"').strip("'")
        h, mi = got.get("SNOOZE_HOUR"), got.get("SNOOZE_MINUTE")
        if h and mi:
            # Only format a clock time when both really are plain numbers:
            # snooze also accepts patterns like */6, and rendering "*/6:30"
            # as a time would be a confident lie about when this fires.
            if h.isdigit() and mi.isdigit():
                when = f" at {int(h):02d}:{int(mi):02d} daily"
            else:
                when = f" on the schedule -H {h} -M {mi}"
    except OSError:
        when = ""      # say nothing rather than assume the shipped default

    # Enabled is not the same as running: `sv down` stops a service without
    # removing its link, and a report that said ON while the owner had just
    # stopped it would be the plausible-but-false message this project keeps
    # hunting. Only claimed when sv can actually be asked.
    if _sv_state(link, run) == "down":
        return ("scheduled updates: enabled but currently STOPPED (sv down) — nothing "
                f"runs unattended until: sudo sv up {SCHED_SERVICE}\n"
                f"  (when up, it runs --sync then --commit --yes by itself{when}, "
                "kernel INCLUDED)")
    # Which SCOPE the unattended run has. Read from the same conf as the time,
    # because a report that says "kernel INCLUDED" on a box configured
    # otherwise is the plausible-but-false message this project keeps hunting.
    kernel = True
    try:
        m = re.search(r"^SCHEDULE_KERNEL=(\S+)", text, re.M)
        if m and m.group(1).strip().strip('"').strip("'").lower() in (
                "no", "false", "0"):
            kernel = False
    except (OSError, NameError):
        pass

    if not kernel:
        return ("scheduled updates: ON — this box runs --sync then --commit --yes "
                f"--no-kernel by itself{when},\n"
                "  USERSPACE ONLY: the base and the performance overlay are kept "
                "current,\n"
                "  and BORE kernel builds wait for the \"Update kernel\" button.\n"
                f"  Turn it off entirely: sudo rm {SCHED_LINK}")
    return ("scheduled updates: ON — this box runs --sync then --commit --yes by "
            f"itself{when},\n"
            "  kernel INCLUDED (the same work as the \"Update kernel\" button, not "
            "\"Update\"),\n"
            f"  so a build can start unattended. Turn it off: sudo rm {SCHED_LINK}\n"
            "  Userspace-only instead: set SCHEDULE_KERNEL=no in "
            f"{SCHED_CONF}")


def cmd_build_space(config: Config, path=None, out=print, run=_run,
                    config_path=None, disk_usage=shutil.disk_usage) -> int:
    """Show where kernel builds happen, or validate and set a new location.

    Why this is a first-class action rather than a line in a config file: a
    kernel build needs ~20 GB of transient tree, and §7.5 asks for 30 GB free
    before it will start. On a laptop whose root partition is 62 GB that is a
    permanent third of the disk reserved for something used once a month — and
    it is the reason a user would keep the userspace overlay and quietly give
    up on the BORE half. Pointing the build at another disk removes the
    constraint, and the product should say so rather than leaving it to a
    config file nobody reads.

    NEVER escalates. The config lives in /etc and is root-owned, which is
    correct: the build space is a property of the machine, not a preference of
    whoever is logged in. So when the file cannot be written this prints the
    exact command that applies the change and stops. A tool that quietly
    acquired root to edit its own config would be a worse tool.
    """
    if path is None:
        out(build_space_line(config, run, disk_usage))
        ok, lines = validate_build_space(config.build_space, config, run, disk_usage)
        for line in lines:
            out("  " + line)
        if not ok:
            out("  this location cannot host a kernel build (see above)")
        out("")
        out("to move it:  cachy-void-update --build-space /path/on/another/disk")
        return EXIT_OK

    target = Path(path).expanduser()
    ok, lines = validate_build_space(target, config, run, disk_usage)
    out(f"build space candidate: {target}")
    for line in lines:
        out("  " + line)
    if not ok:
        out("refusing: nothing was changed.")
        return EXIT_USAGE

    cfg_file = Path(config_path) if config_path else None
    if cfg_file is None or not cfg_file.is_file():
        out("")
        out("validated. Add this to the [build] section of your updater.toml:")
        out(f'    masterdir = "{target}"')
        return EXIT_OK

    try:
        text = cfg_file.read_text(encoding="utf-8")
        new = _set_toml_masterdir(text, str(target))
        cfg_file.write_text(new, encoding="utf-8", newline="\n")
    except OSError:
        # The ordinary case: /etc is root-owned. Hand over the command instead
        # of reaching for privilege we deliberately do not hold.
        out("")
        out(f"validated, but {cfg_file} is not writable by you (it is root-owned,")
        out("as machine configuration should be). Apply it with:")
        out(f"    sudo sed -i '/^\\[build\\]/a masterdir = \"{target}\"' {cfg_file}")
        out("  (or edit that file and put the line under [build])")
        return EXIT_OK

    out("")
    out(f"build space set to {target} in {cfg_file}.")
    out("The chroot is created there on the next build; the old one can be")
    out("removed by hand once you are happy with it.")
    return EXIT_OK


def _set_toml_masterdir(text: str, value: str) -> str:
    """Insert or replace ``masterdir = "..."`` inside [build]. Text-level on
    purpose: tomllib reads but does not write, and rewriting the whole file
    from a parsed dict would discard the user's comments."""
    line = f'masterdir = "{value}"'
    if re.search(r"^\s*masterdir\s*=", text, re.M):
        return re.sub(r"^\s*masterdir\s*=.*$", line, text, count=1, flags=re.M)
    if re.search(r"^\[build\]", text, re.M):
        return re.sub(r"^(\[build\][^\n]*\n)", r"\1" + line + "\n", text,
                      count=1, flags=re.M)
    return text.rstrip() + f"\n\n[build]\n{line}\n"


def cmd_pending(config: Config, out=print, run=_run,
                disk_usage=shutil.disk_usage) -> int:
    """Fast, machine-readable "is anything waiting?" probe — JSON on stdout.

    Why this exists next to ``--status``: that command is a *human report* and
    an expensive one (it builds the §7.3 overlay queue, shells out to du, dkms
    and flatpak) — the GUI shows a busy line for it. Anything that polls, such
    as a tray indicator, needs one cheap answer instead, from the two sources
    that cost nothing: an unprivileged xbps dry-run and the kernel state file.

    Two deliberate omissions. The **overlay** queue (§7.3 M/P/O terms) is not
    probed: it only materializes after a `--sync`, and computing it is the slow
    part we are avoiding — the badge says "upstream has N for you" and the GUI's
    Check remains the whole truth. **Flatpak** is skipped for the same reason
    (a remote query per remote).

    One thing was omitted by accident rather than by design, and is now here:
    whether a newer kernel is available to PORT. --status reported it and this
    probe did not, so the window's headline read "New BORE kernel" beside a
    silent tray. It costs at most one extra unprivileged repository query (only
    when the local checkout has nothing to say), which a three-hourly poll can
    afford far more easily than it can afford being wrong.

    Never fails: a probe that exits non-zero would read as "the updater is
    broken". Degradations are reported *in band* (``fresh``, ``notes``) and the
    exit code stays EXIT_OK. ``attention`` carries the policy so a front-end
    never has to re-derive it from counts.
    """
    payload: dict = {"schema": 1, "fresh": False,
                     "upstream": {"updatable": 0, "held": 0},
                     "kernel": {}, "attention": [], "notes": []}

    n, held, fresh, drivers = upstream_counts(run)
    payload["fresh"] = fresh
    if n is None:
        payload["notes"].append("xbps-install unavailable: could not determine "
                                "the upstream update count")
    else:
        payload["upstream"] = {"updatable": n, "held": held,
                               "drivers": drivers}
        if not fresh:
            payload["notes"].append("remote index unreachable; counts are from "
                                    "the on-disk cache and may be stale")
        if n:
            payload["attention"].append(ATTN_UPDATES)

    # Kernel: read the state store directly — no solver, no subprocess.
    try:
        state = grub.KernelStateStore(config.kernel_state_path).load()
        name = state.get("state") or ""
        cand = (state.get("candidate") or {}).get("kver") or ""
        good = (state.get("known_good") or {}).get("kver") or ""
        payload["kernel"] = {"state": name, "candidate": cand or None,
                             "known_good": good or None,
                             "mode": (state.get("grub") or {}).get("mode") or None}
        if cand and name == "STAGED":
            payload["attention"].append(ATTN_KERNEL_STAGED)
        elif name in FROZEN_STATES:
            # Same correction as the human readout: the freeze is reportable on
            # its own, with or without a candidate to blame it on.
            payload["attention"].append(ATTN_KERNEL_FROZEN if not cand
                                        else ATTN_KERNEL_UNHEALTHY)
        series = state.get("base_series") or ""
        if config.kernel_enable and series:
            try:
                lock = trust.load_bore_lock(config.bore_lock_path)
                pinned = bool(lock.patches.get(series))
            except (trust.TrustConfigError, OSError):
                pinned = False
            payload["kernel"]["bore_pin"] = "pinned" if pinned else "missing"
            if not pinned:
                payload["attention"].append(ATTN_BORE_PIN_MISSING)
        # A newer kernel to port is news the tray never carried: --status
        # reported it and the probe did not, so the window said "New BORE
        # kernel" while the icon sat quiet. Same helper as the human
        # readout, so the two cannot disagree again.
        if config.kernel_enable:
            def _vercmp(a, b):
                # Same delegation as Xbps.vercmp (0 equal / 1 a>b / 255 a<b),
                # without dragging a whole Xbps into a probe that needs nothing
                # else from it.
                cp = run(["xbps-uhelper", "cmpver", a, b])
                rc = cp.returncode
                if rc == 0:
                    return 0
                if rc == 1:
                    return 1
                if rc in (255, -1):
                    return -1
                raise XbpsError(f"unexpected cmpver exit {rc}")

            v = kernel_port_available(config, state, run, _vercmp)
            payload["kernel"]["port_available"] = bool(v.available)
            if v.available:
                payload["kernel"]["upstream_version"] = v.upstream_version
                payload["attention"].append(ATTN_KERNEL_PORT)
    except (grub.GrubError, OSError, ValueError) as exc:
        payload["notes"].append(f"kernel state unreadable: {exc}")

    # A failed newest run is "something needs you" -- the tray badges it
    # amber and the window shows the reason. A later good run clears it.
    _last = last_run_failure(config)
    if _last:
        payload["last_run"] = _last
        payload["attention"].append(ATTN_RUN_FAILED)
    _free = _free_gib("/", disk_usage)
    if _free is not None and _free < 1.0:
        payload["notes"].append(f"disk nearly full: {_free * 1024:.0f} MiB free")

    out(json.dumps(payload, indent=2, sort_keys=True))
    return EXIT_OK


def _deploy_annotation(config: Config, run_id: str) -> str:
    """What the run behind a `deploy-*` snapshot actually did, from its journal.

    Annotate, never dump (§4.10's habit): a list of subvolume names tells a user
    nothing about which one to go back to, whereas "3 packages deployed" and
    "this run FAILED" is the thing they are actually trying to remember. The
    journal is witness-only (§7.6) and read strictly for display here.
    """
    try:
        data = read_json(config.log_root / f"run-{run_id}" / "journal.json")
    except (OSError, ValueError):
        return ""
    if not isinstance(data, dict):
        return ""
    bins = [b for b in (data.get("deploy_bins") or []) if b]
    phase = data.get("phase") or ""
    bits = []
    if bins:
        shown = ", ".join(bins[:4]) + (", …" if len(bins) > 4 else "")
        bits.append(f"{len(bins)} overlay package{'' if len(bins) == 1 else 's'} "
                    f"deployed ({shown})")
    elif phase == "done":
        bits.append("upstream-only update (no overlay rebuild)")
    if phase == "failed":
        fail = data.get("failure") or {}
        bits.append(f"run FAILED{' on ' + fail['pkg'] if fail.get('pkg') else ''}")
    elif phase and phase != "done":
        bits.append(f"run left at phase '{phase}'")
    return "; ".join(bits)


_SNAP_KIND_TEXT = {
    snapshot.KIND_DEPLOY: "automatic, taken just before an update",
    snapshot.KIND_MANUAL: "taken by hand",
    snapshot.KIND_TRIAL: "taken before trying a desktop",
    snapshot.KIND_OTHER: "unrecognised name",
}


def cmd_snapshots(config: Config, out=print, run=_run) -> int:
    """§9.5b — show the safety net, and how to use it on THIS machine.

    Read-only by construction: it lists snapshots through the grant §9.5 already
    holds and prints the restore commands for the host's actual layout without
    running any of them. Restoring a root filesystem is a decision, and the
    person whose machine it is gets to make it with the commands in front of
    them.
    """
    out("Cachy-Void — snapshots")
    out("=" * 46)
    snap_dir = config.snapshot_dir
    try:
        snaps = snapshot.list_snapshots(snap_dir=snap_dir, run=run)
    except OSError as exc:
        out(f"    cannot list snapshots: {exc}")
        return EXIT_OK
    if not snaps:
        out(f"    none found under {snap_dir}")
        out("    (pre-deploy snapshots need a btrfs root and the §9.5 snapshot "
            "subvolume, which deploy.sh creates)")
        return EXIT_OK

    out(f"\n{len(snaps)} snapshot(s) under {snap_dir}, newest first:")
    for s in snaps:
        age = snapshot.age_text(s.stamp)
        head = f"  {s.name}"
        meta = _SNAP_KIND_TEXT.get(s.kind, "")
        if s.label:
            meta += f" ({s.label})"
        out(f"{head}")
        out(f"      {age + ' — ' if age else ''}{meta}"
            + ("" if s.prunable else "   [kept: only automatic ones are pruned]"))
        if s.run_id:
            note = _deploy_annotation(config, s.run_id)
            if note:
                out(f"      {note}")

    # The recipe, for this host and no other.
    try:
        fstab = Path("/etc/fstab").read_text(encoding="utf-8")
    except OSError as exc:
        out(f"\ncannot read /etc/fstab ({exc}) — no restore recipe offered")
        return EXIT_OK
    layout = snapshot.detect_restore_layout(fstab)
    newest = next((s for s in snaps if s.kind == snapshot.KIND_DEPLOY), snaps[0])
    plan = snapshot.restore_recipe(layout=layout, snapshot=newest,
                                   snap_dir=snap_dir, mount=config.snapshot_subvol)
    out(f"\nGoing back to one of these ({layout.detail}):")
    if plan.supported:
        out(f"\n  Shown for {newest.name} — substitute any name from the list above.")
        for step in plan.steps:
            out(f"    $ {step}")
        if plan.undo:
            out(f"\n  Changed your mind after rebooting:")
            out(f"    $ {plan.undo}")
    out("")
    for n in plan.notes:
        out(f"  * {n}")
    out("")
    out("  Nothing above was run: this command only ever reads.")
    return EXIT_OK


# States that freeze the kernel path until a human says "I have dealt with it"
# (§8.8's last row). Userspace updates continue throughout — only kernel work
# waits — but there was no way to leave them: the spec names
# `cachy-void-update kernel ack` and the CLI never grew it, so a box that landed
# in one was stuck short of hand-editing JSON. Found the hard way on real
# hardware, where a dropped WiFi connection parked a perfectly healthy laptop in
# CANDIDATE_UNHEALTHY.
FROZEN_STATES = ("CANDIDATE_UNHEALTHY", "ROLLED_BACK", "AWAIT_HUMAN_SERIES",
                 "AWAIT_HUMAN_TEMPLATE", "AWAIT_HUMAN_PATCH", "AWAIT_HUMAN_BUILD",
                 "HALT_HASH_MISMATCH")

# ---------------------------------------------------------------------------
# The `attention` vocabulary of --pending. Named constants rather than string
# literals scattered through cmd_pending, because the whole contract with a
# front-end is this list: the CLI decides WHAT deserves attention, the front-end
# only decides how to word it. A token no front-end knows is worse than no
# token, since the tray filters unknown reasons out silently — which is exactly
# how "kernel-frozen" came to be emitted by the CLI and displayed by nobody.
# test_tray.py asserts this set and the tray's REASON_TEXT are the same set.
ATTN_UPDATES = "updates"
ATTN_KERNEL_STAGED = "kernel-staged"
ATTN_KERNEL_UNHEALTHY = "kernel-unhealthy"
ATTN_KERNEL_FROZEN = "kernel-frozen"
ATTN_KERNEL_PORT = "kernel-port"
ATTN_BORE_PIN_MISSING = "bore-pin-missing"
ATTN_RUN_FAILED = "run-failed"
ATTENTION_TOKENS = frozenset({
    ATTN_UPDATES, ATTN_KERNEL_STAGED, ATTN_KERNEL_UNHEALTHY,
    ATTN_KERNEL_FROZEN, ATTN_KERNEL_PORT, ATTN_BORE_PIN_MISSING,
    ATTN_RUN_FAILED,
})


PortVerdict = collections.namedtuple(
    "PortVerdict", "available upstream_version ported_version source event")


def acted_version(state: dict) -> str:
    """The newest kernel version the kernel path has ALREADY acted on.

    ``ported_version`` is the version proven by a healthy boot, and in steady
    state it is the right baseline for "is there something new upstream?".
    Between staging and promotion it is not: there is a version already
    regenerated, built, installed and staged that ``ported_version`` does not
    know about yet, because §8.8 advances it only on PROMOTE.

    Comparing upstream against ``ported_version`` in that window re-detects the
    SAME version as a fresh bump on every run — and §8.4 synthesis then writes
    state READY over STAGED, discarding the staging record. The §8.7 confirm
    service exits when the state is not STAGED/CONFIRMING, so the candidate can
    never be promoted, ``ported_version`` never advances, and the box reports
    "a newer BORE kernel is available" for the kernel it is already running.

    Observed exactly that way on the testbed (2026-09-09): 6.12.108 built and
    staged on the 8th, the 01:00 run on the 9th re-detected it and reset the
    state, and the reboot came up running 6.12.108 while the updater still
    offered to build it. §8.8's STAGED→discard transition is for a *new*
    upstream bump; the same version is not a bump at all.
    """
    cand = (state.get("candidate") or {}).get("kver") or ""
    if state.get("state") in ("STAGED", "CONFIRMING") and cand:
        # candidate kvers carry the fork suffix ("6.12.108_1-cachy"); template
        # and ported versions do not.
        return re.sub(r"-cachy$", "", cand)
    return state.get("ported_version", "") or ""


def kernel_port_available(config: "Config", state: dict, run, vercmp) -> PortVerdict:
    """(available, upstream_ver, ported_ver) — the §2.6/§8.4 drift verdict.

    ONE implementation, shared by the human report and the machine probe. It
    was not shared before, and the consequence was a tray that never mentioned
    a kernel port while the window's own headline said "New BORE kernel": the
    same class of split-brain as the tray saying 20 packages beside a window
    saying 16, and found the same way — the owner noticed the two disagreeing.

    Two sources in order of cost. The void-packages checkout is a local file
    read and free, so it is asked first. If it says nothing, the REPOSITORY is
    asked, because the checkout is only as current as the last sync — the case
    that hid linux6.12 6.12.104 behind a nine-day-old checkout. That second
    question costs one unprivileged index fetch, which is why --pending's
    docstring now names it: a three-hourly poll can afford it, and the
    alternative is a tray permanently silent about kernels.

    A series that has vanished upstream is deliberately NOT "a port available":
    it is a human decision (§8.2), and it already has its own frozen state.

    ``vercmp`` is passed in rather than reached for: the report path already
    holds an ``Xbps`` instance, while --pending deliberately holds none, and
    both must reach the same verdict. Neither may compare versions itself
    (invariant: comparisons delegate to ``xbps-uhelper cmpver``).

    ``source`` is part of the verdict because the human report says different
    things depending on who answered — "upstream linux6.12 is at X" for the
    checkout, "Void is shipping X" plus a checkout-age note for the repository.
    Sharing the predicate must not flatten that; a reader who is told the
    repository knew first also learns their checkout is behind.
    """
    series = state.get("base_series") or ""
    # NOT ported_version: see acted_version(). A staged candidate has
    # already been acted on, and calling it "available" tells the user to
    # build what they are about to boot.
    ported = acted_version(state)
    if not series:
        return PortVerdict(False, "", ported, "", "")
    try:
        tpath = config.void_packages / "srcpkgs" / f"linux{series}" / "template"
        text = tpath.read_text(encoding="utf-8") if tpath.exists() else None
        ev, tmpl = grub.classify_bump(series_template_text=text,
                                      ported_version=ported,
                                      vercmp=vercmp)
    except (grub.GrubError, XbpsError, OSError, ValueError):
        return PortVerdict(False, "", ported, "", "")
    if ev == grub.EV_BUMP_PATCHLEVEL:
        return PortVerdict(True, tmpl, ported, "checkout", ev)
    if ev == grub.EV_AWAIT_HUMAN_SERIES:
        return PortVerdict(False, "", ported, "", ev)
    repo_ver = upstream_series_version(series, run)
    if repo_ver and ported:
        try:
            if vercmp(repo_ver, ported) > 0:
                return PortVerdict(True, repo_ver, ported, "repository", ev)
        except (XbpsError, OSError):
            pass
    return PortVerdict(False, repo_ver, ported, "", ev)


def cmd_kernel_ack(config: Config, out=print, *, assume_yes: bool = False,
                   ask=input) -> int:
    """§8.8 — acknowledge a frozen kernel state and return to TRACKING.

    Deliberately does nothing else: it does not install, remove, re-stage or
    touch a bootloader. It says what is frozen and why that matters, archives
    any candidate into ``history`` so the episode is not simply erased, and
    clears the freeze. Confirmed, because "the thing that went wrong is dealt
    with" is a claim only the operator can make.
    """
    store = grub.KernelStateStore(config.kernel_state_path)
    try:
        state = store.load()
    except (OSError, ValueError) as exc:
        out(f"error: cannot read kernel state: {exc}")
        return EXIT_KERNEL
    name = state.get("state") or ""
    if name not in FROZEN_STATES:
        out(f"kernel state is {name or 'unset'} — nothing to acknowledge "
            "(this command only clears a frozen state).")
        return EXIT_OK

    cand = (state.get("candidate") or {}).get("kver")
    out(f"kernel state: {name}")
    for line in frozen_explanation(name, cand or ""):
        out(f"  {line}")
    health = state.get("health") or {}
    if health:
        out(f"  last health check: ok={health.get('ok')} "
            f"at {health.get('ts') or 'unknown'}")
        failed = [k for k, v in (health.get("checks") or {}).items() if not v]
        if failed:
            out(f"  failing checks: {', '.join(sorted(failed))}")
    out("  while frozen, KERNEL work is paused; userspace updates are unaffected.")
    out("Acknowledging returns the kernel path to TRACKING. Nothing is installed, "
        "removed, or re-staged.")

    if not assume_yes:
        try:
            reply = ask("Acknowledge and resume kernel updates? [y/N] ")
        except EOFError:
            reply = ""
        if (reply or "").strip().lower() not in ("y", "yes"):
            out("left frozen — nothing changed.")
            return EXIT_OK

    if cand:
        hist = list(state.get("history") or [])
        hist.append({"state": name, "candidate": state.get("candidate"),
                     "acked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
        state["history"] = hist
    state["candidate"] = None
    state["staged_boot_id"] = None
    state["state"] = "TRACKING"
    try:
        store.save(state)
    except OSError as exc:
        out(f"error: could not write kernel state: {exc}")
        return EXIT_KERNEL
    out("kernel state -> TRACKING; kernel updates resume with the next run.")
    return EXIT_OK


def _march_label(config: Config) -> str:
    """" / x86-64-v2" read from the build profile, or "" when unknown.

    The header used to hard-code v3 and printed that on a v2 machine — a small
    lie, but in the one pane whose whole job is telling the truth about this
    box. The march lives in void-packages' etc/conf (§1.1), which is where the
    build actually gets it from.
    """
    try:
        text = (config.void_packages / "etc" / "conf").read_text(encoding="utf-8")
    except OSError:
        return ""
    m = re.search(r"-march=([A-Za-z0-9_-]+)", text)
    return f" / {m.group(1)}" if m else ""


def cmd_status(xbps, config: Config, out=print, run=_run,
               disk_usage=shutil.disk_usage) -> int:
    """Read-only overview of every update tier — the 'what's pending' view.

    Groups into the four sections the front-end presents: [1] upstream Void,
    [2] the performance overlay (allowlist), [3] the BORE kernel, [4] maintenance
    and [5] GPU/drivers. Every probe is best-effort: a tool that is missing or
    needs root simply reports "unknown" — status never mutates and never fails
    the run (EXIT_OK unless the overlay query itself throws)."""
    def _lines(cp):
        return [l for l in (cp.stdout or "").splitlines() if l.strip()]

    out("Cachy-Void — status")
    out("=" * 46)
    # Before the tiers, not after: whether this box updates itself changes how
    # every count below should be read (those pending updates may install
    # themselves tonight, kernel and all).
    _sched = scheduled_run_line(run=run)
    if _sched:
        out(_sched)
        out("")
    # And whether the newest run FAILED. The 3am failure was visible only in
    # a runit log directory; the window that people actually look at said
    # nothing, two hours later, about six wasted hours and a full disk.
    _last = last_run_summary(config)
    if _last:
        out(_last)
        out("")
    # A failing tier must not swallow the tiers below it. Tier [2] used to
    # `return EXIT_QUERY` on a broken/unbootstrapped void-packages, which hid
    # the kernel, maintenance and GPU sections — including the §8.3a BORE-pin
    # warning the GUI banner keys off (found live in a degraded sandbox where
    # xbps-src refused to run). Remember the failure, keep reporting, return it
    # at the end so scripts still see a non-zero code.
    rc = EXIT_OK

    out("\n[1] System (upstream Void)")
    # The SAME counting path as --pending, deliberately: see upstream_counts().
    n, held, fresh, drivers = upstream_counts(run)
    if n is None:
        # Say WHY when the reason is knowable. On a full disk xbps cannot
        # even memory-sync an index, and "unavailable" reads as a network
        # problem -- which sends the reader to the wrong fix.
        _why = ""
        _free = _free_gib("/", disk_usage)
        if _free is not None and _free < 0.5:
            _why = (f" — the disk is full ({_free * 1024:.0f} MiB free); nothing "
                    "can be checked until space is freed")
        out("    unknown — xbps-install could not run" + _why)
    else:
        # Split rather than one lump: "is a driver in there?" is the question
        # this tier gets asked, and a total cannot answer it.
        if drivers:
            out(f"    {n - drivers} package update(s), "
                f"{drivers} driver/firmware update(s)"
                + (f"   (+{held} on hold)" if held else "")
                + ("" if fresh else "   (mirror unreachable; local cache)"))
        else:
            out(f"    {n} upstream package(s) updatable"
                + ("" if n else " — up to date")
                + (f"   (+{held} on hold)" if held else "")
                + ("" if fresh else "   (mirror unreachable; from the local cache)"))

    out(f"\n[2] Performance overlay (rebuilt at -O3{_march_label(config)})")
    try:
        plan = build_queue(xbps, config.targets, config.blacklist,
                           config.repo_strs, always_build=_always_build(config))
        nb = [p for p in plan.q_build if p != KERNEL_TARGET]
        nd = [p for p in plan.q_deploy if p != KERNEL_TARGET]
        if nb or nd:
            out(f"    {len(nb)} to rebuild, {len(nd)} to deploy")
            if nb:
                out("      rebuild: " + ", ".join(nb))
            if nd:
                out("      deploy:  " + ", ".join(nd))
        else:
            out("    in sync with upstream")
    except (XbpsError, MappingError, CycleError, OSError) as exc:
        out(f"    query failed: {exc}")
        out("    (the sections below are unaffected)")
        rc = EXIT_QUERY

    out("\n[3] Kernel (linux-cachy / BORE)")
    _kernel_report(config, xbps, out=lambda m: out("    " + m), run=run)

    out("\n[4] Maintenance / cleanup")
    try:
        cp = run(["xbps-remove", "-o", "-n"])
        out(f"    orphaned packages: {len(_lines(cp)) if cp.returncode == 0 else 'unknown (needs root)'}")
    except OSError:
        pass
    inv = _kernel_inventory(config, run)
    if inv:
        out("    old kernel files present:")
        for line in old_kernel_lines(inv):
            out("      " + line)
    else:
        out("    removable old kernels: none")
    try:
        cp = run(["du", "-sh", "/var/cache/xbps"])
        if cp.returncode == 0 and cp.stdout.strip():
            out(f"    package cache on disk: {cp.stdout.split()[0]}")
    except OSError:
        pass
    for line in disk_lines(config, disk_usage):
        out("    " + line)
    out("    " + build_space_line(config, run, disk_usage))
    _dbg = _debug_pkgs(config)
    if _dbg:
        out(f"    debug-symbol packages in the local repo: {len(_dbg)} "
            f"({sum(sz for _f, sz in _dbg) / GIB:.1f} GiB; never installed from — "
            "Clean up removes them)")

    out("\n[5] GPU & drivers")
    try:
        cp = run(["sh", "-c", "lspci | grep -Ei 'vga|3d|display'"])
        for g in _lines(cp):
            out("    " + (g.split(': ', 1)[-1] if ': ' in g else g))
    except OSError:
        pass
    try:
        cp = run(["dkms", "status"])
        ds = _lines(cp) if cp.returncode == 0 else []
        if ds:
            out(f"    DKMS modules ({len(ds)}):")
            for l in ds:
                out("      " + l)
        else:
            out("    DKMS: none (or driver is not DKMS)")
    except OSError:
        pass
    # A pending GRAPHICS DRIVER update is worth naming even though it rides the
    # ordinary system update, because "an update is pending" answers nothing:
    # WHICH driver, to WHAT version, and what must happen before it actually
    # takes effect. That last part differs by kind and conflating them is a lie
    # — mesa is userspace GL (the running apps keep the old one until they are
    # restarted), while an nvidia driver rebuilds a KERNEL MODULE at install
    # time and the running kernel keeps the old module until a reboot.
    # Dependency packages dragged along (libgbm, mesa-dri, …) are deliberately
    # not named: the headline package is the answer to "what is changing".
    try:
        drv = sorted(b for b in xbps.installed()
                     if re.fullmatch(r"nvidia\d*(-dkms)?|mesa", b))
    except (XbpsError, OSError):
        drv = []
    if drv:
        try:
            pend = {}
            for line in _lines(run(["xbps-install", "-un", *drv])):
                tok = line.split()[0] if line.split() else ""
                try:
                    name, ver = split_pkgver(tok)
                except (ValueError, KeyError):
                    continue
                if name in drv:
                    pend[name] = ver
            for name in drv:
                if name not in pend:
                    continue
                try:
                    cur = split_pkgver(xbps.inst_pkgver(name))[1]
                except (XbpsError, KeyError, ValueError, OSError):
                    cur = "?"
                is_module = bool(re.fullmatch(r"nvidia\d*(-dkms)?", name))
                # One of the packages counted in tier [1], not an extra thing to
                # do — saying otherwise invited "does that go with the rest?".
                # And NO instruction here: what to do afterwards is printed when
                # the update has actually happened, not while it is pending.
                out(f"    graphics driver update: {name} {cur} -> {pend[name]} "
                    "(one of the system packages above)")
                out("      " + (
                    "a kernel module: it is rebuilt at install time and needs a "
                    "reboot afterwards before the new one is in use"
                    if is_module else
                    "the OpenGL/Vulkan stack games render through; afterwards, "
                    "programs already open keep the old copy until reopened"))
        except OSError:
            pass

    out("\n[6] Flatpak apps")
    try:
        if run(["flatpak", "--version"]).returncode == 0:
            cp = run(["flatpak", "remote-ls", "--updates"])
            if cp.returncode == 0:
                n = len(_lines(cp))
                out(f"    {n} app(s) updatable" + ("" if n else " — up to date"))
            else:
                out("    unknown — could not query Flatpak remotes")
        else:
            out("    flatpak not installed")
    except OSError:
        out("    flatpak not installed")

    out("")
    return rc


def cmd_pin_bore(config: Config, out=print, *, assume_yes: bool = False,
                 dry_run: bool = False, discover=None, ask=input) -> int:
    """§8.3a — assisted BORE pin: automate the CLERICAL work of vouching.

    Locates the BORE patch for the tracked series at upstream HEAD, shows the
    human exactly what was found (commit, file, sha256, size), and only writes
    the bore.lock entry after an explicit confirmation. --dry-run previews and
    writes nothing; --yes skips the terminal prompt (the GUI uses it AFTER its
    own confirm dialog — the human approval always happens somewhere). The
    update pipeline never calls this: an unpinned series stays withheld until
    a person acts, exactly as before — this just makes acting take one click
    instead of a hand-computed sha256 and a TOML edit.
    """
    try:
        state = grub.KernelStateStore(config.kernel_state_path).load()
    except OSError as exc:
        out(f"error: cannot read kernel state ({exc})")
        return EXIT_USAGE
    series = state.get("base_series") or ""
    if not series:
        out("error: no tracked kernel series in kernel-state.json (§8.2) — "
            "run bootstrap.sh, or set base_series manually, before pinning.")
        return EXIT_USAGE

    try:
        lock = trust.load_bore_lock(config.bore_lock_path)
    except trust.TrustConfigError as exc:
        out(f"error: cannot use bore.lock at {config.bore_lock_path}: {exc}")
        return EXIT_USAGE
    if series in lock.patches:
        e = lock.patches[series]
        out(f"series {series} is already pinned (sha256 {e.sha256[:16]}…"
            + (f", approved {e.approved}" if e.approved else "") + ") — nothing to do.")
        return EXIT_OK

    out(f"pin-bore: locating the BORE patch for series {series} at upstream HEAD…")
    discover = discover or (lambda **kw: trust.discover_bore_patch(**kw))
    try:
        proposal, data = discover(
            repo_url=lock.repo_url, series=series,
            cache_dir=config.kernel_patch_path.parent / ".bore-cache")
    except trust.TrustError as exc:
        out(f"error: {exc}")
        return EXIT_QUERY

    out("")
    out("Proposed BORE pin — review before approving:")
    out(f"    series        {proposal.series}")
    out(f"    repo          {proposal.repo_url}")
    out(f"    commit        {proposal.commit}")
    out(f"    file          {proposal.file}")
    out(f"    BORE version  {proposal.bore_version or '<unknown>'}")
    out(f"    sha256        {proposal.sha256}")
    out(f"    size          {proposal.size} bytes")
    out("")
    if dry_run:
        out("(preview only — nothing was written; approve with --pin-bore, "
            "without --dry-run)")
        return EXIT_OK

    if not assume_yes:
        try:
            reply = ask("Pin this patch — trust it for all future kernel "
                        f"builds on series {series}? [y/N] ")
        except EOFError:
            reply = ""
        if reply.strip().lower() not in ("y", "yes"):
            out("not pinned — nothing was written.")
            return EXIT_OK

    approved = (f"{time.strftime('%Y-%m-%d')} {os.environ.get('USER') or os.environ.get('LOGNAME') or 'operator'} "
                f"(assisted pin at upstream HEAD {proposal.commit[:12]})")
    try:
        trust.append_pin(config.bore_lock_path, proposal, approved)
        # Seed the verified artifact so the next kernel run's reuse-first path
        # (§8.3) hits the cache — the pin works offline from this moment on.
        config.kernel_patch_path.parent.mkdir(parents=True, exist_ok=True)
        config.kernel_patch_path.write_bytes(data)
    except (trust.TrustConfigError, OSError) as exc:
        out(f"error: pin failed: {exc}")
        return EXIT_USAGE
    out(f"pinned: series {series} -> {proposal.sha256[:16]}… (bore.lock updated, "
        "patch cached). The next 'Update kernel' run will build the BORE kernel.")
    return EXIT_OK


def cmd_sync(config: Config, out=print, run=_run) -> int:
    """Stage 1 — rebase onto upstream master, rolling back on conflict (§4.2)."""
    vp = str(config.void_packages)
    try:
        pre = run(["git", "rev-parse", "HEAD"], vp)
        if pre.returncode != 0:
            out(f"error: not a git checkout: {vp}")
            return EXIT_SYNC
        head = pre.stdout.strip()

        # The Void remote may be named 'upstream' (manual setups) or 'origin'
        # (bootstrap.sh does a plain `git clone` — git's default). Hardcoding
        # 'upstream' made every bootstrap-created checkout fail sync (exit 20).
        remotes = run(["git", "remote"], vp)
        names = remotes.stdout.split() if remotes.returncode == 0 else []
        remote = "upstream" if "upstream" in names else "origin"

        if run(["git", "fetch", remote], vp).returncode != 0:
            out(f"error: git fetch {remote} failed")
            return EXIT_SYNC

        rebase = run(["git", "pull", "--rebase", remote, "master"], vp)
        if rebase.returncode != 0:
            run(["git", "rebase", "--abort"], vp)
            now = run(["git", "rev-parse", "HEAD"], vp).stdout.strip()
            out("error: rebase failed and was rolled back "
                f"(HEAD {'unchanged' if now == head else 'CHANGED — investigate'})")
            return EXIT_SYNC

        if run(["./xbps-src", "bootstrap-update"], vp).returncode != 0:
            out("error: bootstrap-update failed")
            return EXIT_BOOTSTRAP

        new = run(["git", "rev-parse", "HEAD"], vp).stdout.strip()
        out(f"sync ok ({head[:12]} -> {new[:12]})")
        return EXIT_OK
    except OSError as exc:
        out(f"error: sync environment failure: {exc}")
        return EXIT_SYNC


# ==========================================================================
# Disk, preflight, and last-run visibility (§7.5, §4.10)
# ==========================================================================
# Written after the first real unattended failure: a linux-cachy build ran for
# six hours and died at the module-link stage with ENOSPC, leaving a 20 GB tree
# on a 63 GB disk that then sat at 100% -- and --status said nothing about the
# disk, nothing about why its upstream probe had gone "unavailable", and nothing
# about a run having failed two hours earlier. §7.5's preflight had specified
# the guard all along; it was never implemented.
GIB = 1024 ** 3


def _free_gib(path, disk_usage=shutil.disk_usage):
    """Free GiB on the filesystem holding ``path`` (a missing leaf still has
    one -- walk up to the nearest existing ancestor), or None if unreadable."""
    q = Path(path)
    while not q.exists() and q != q.parent:
        q = q.parent
    try:
        return disk_usage(str(q)).free / GIB
    except OSError:
        return None


def _masterdirs(config: Config) -> list:
    """Every chroot directory in play — one when relocated, else the glob."""
    if config.masterdir:
        return [Path(config.masterdir)] if Path(config.masterdir).is_dir() else []
    try:
        return sorted(d for d in Path(config.void_packages).glob("masterdir*")
                      if d.is_dir())
    except OSError:
        return []


def _du(path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _e: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


def _leftover_trees(config: Config) -> list:
    """``(path, bytes)`` for every tree under ``masterdir*/builddir``.

    §7.5 keeps a build tree on disk after a run -- for forensics after a
    failure, and simply because nothing removes it after a success -- and the
    NEXT attempt's ``clean`` takes it away. Correct, and on the testbed it was
    also 20 GB that nobody knew about. So it is named, with its size and the
    command, wherever disk is reported.
    """
    found = []
    for md in _masterdirs(config):
        bd = md / "builddir"
        if not bd.is_dir():
            continue
        try:
            # xbps-src keeps its own dot-directories beside the trees
            # (`.xbps-linux-cachy`); those are bookkeeping, not a build.
            trees = sorted(t for t in bd.iterdir()
                           if t.is_dir() and not t.name.startswith("."))
        except OSError:
            continue
        for t in trees:
            size = _du(t)
            if size >= 1024 * 1024:          # a real tree is never kilobytes
                found.append((t, size))
    return found


def _srcpkg_of_tree(name: str) -> str:
    """``linux-cachy-6.12.108`` -> ``linux-cachy`` (the wrksrc naming rule)."""
    return re.sub(r"-[0-9][^-]*$", "", name)


def disk_lines(config: Config, disk_usage=shutil.disk_usage) -> list:
    """Free space on the build filesystem, the §7.5 floor, and leftover trees."""
    lines = []
    root = Path(config.void_packages)
    try:
        probe = root if root.exists() else Path("/")
        du = disk_usage(str(probe))
    except OSError:
        return lines
    free, total = du.free / GIB, du.total / GIB
    pct = (100 * du.used / du.total) if du.total else 0
    relocated = config.masterdir is not None
    label = "the void-packages filesystem" if relocated else "the build filesystem"
    lines.append(f"disk: {free:.1f} GiB free of {total:.0f} GiB on {label} "
                 f"({pct:.0f}% used)")
    # Only warn about the floor that actually applies here. Once the build
    # space has been moved, this filesystem holds the hostdir -- packages,
    # sources and ccache -- and is judged by the smaller floor. Warning about
    # the kernel floor here said "the next build will refuse" on a box where
    # the build would have been fine, which is the exact failure mode this
    # project keeps meeting: true-sounding, wrong for the run in front of you.
    floor = config.min_free_userspace_gib if relocated else config.min_free_gib
    key = "min_free_userspace_gib" if relocated else "min_free_gib"
    if free < floor:
        lines.append(f"  below the {floor} GiB needed here "
                     f"([build] {key}) — the next build will refuse (§7.5)")
    for tree, size in _leftover_trees(config):
        lines.append(f"build tree left in masterdir: {tree.name} — {size / GIB:.1f} GiB "
                     "(the next build cleans it; reclaim now: "
                     f"./xbps-src clean {_srcpkg_of_tree(tree.name)})")
    return lines


# Filesystems that cannot host a build chroot, and why. A masterdir is a real
# root filesystem: it needs unix ownership, permissions, symlinks and device
# nodes. The FAT/NTFS family has none of that, so a build there fails deep
# inside a package rather than at the point the directory was chosen.
_CHROOT_HOSTILE = {
    "vfat": "no unix permissions, ownership or symlinks",
    "exfat": "no unix permissions, ownership or symlinks",
    "msdos": "no unix permissions, ownership or symlinks",
    "ntfs": "unix permissions are emulated at best",
    "ntfs3": "unix permissions are emulated at best",
    "fuseblk": "a FUSE mount (often NTFS/exFAT) — permissions are emulated",
    "iso9660": "read-only",
    "squashfs": "read-only",
}


def validate_build_space(path, config: Config, run=_run,
                         disk_usage=shutil.disk_usage) -> tuple:
    """``(ok, lines)`` — may this directory host the kernel build?

    Checked in the order a user would hit them, and every failure names the
    reason rather than a generic refusal. Deliberately a *preview*: it changes
    nothing, so a front-end can show the verdict before anything is committed.
    """
    lines = []
    q = Path(path)
    if not q.is_dir():
        return False, [f"{q} is not a directory"]
    if not os.access(q, os.W_OK):
        return False, [f"{q} is not writable by this user"]

    fstype, opts, source = "", "", ""
    try:
        cp = run(["findmnt", "-no", "FSTYPE,OPTIONS,SOURCE", "-T", str(q)])
        if cp.returncode == 0 and cp.stdout.strip():
            parts = cp.stdout.split()
            fstype = parts[0] if parts else ""
            opts = parts[1] if len(parts) > 1 else ""
            source = parts[2] if len(parts) > 2 else ""
    except OSError:
        pass

    if fstype in _CHROOT_HOSTILE:
        return False, [f"{q} is on {fstype}: {_CHROOT_HOSTILE[fstype]}. A build "
                       "chroot cannot live there."]
    if "noexec" in opts.split(","):
        return False, [f"{q} is mounted noexec — a chroot must execute what it "
                       "builds. Remount without noexec, or choose elsewhere."]

    free = _free_gib(q, disk_usage)
    need = config.min_free_gib
    if free is None:
        lines.append(f"could not read free space on {q}")
    elif free < need:
        return False, [f"{q} has {free:.1f} GiB free; a kernel build needs at "
                       f"least {need} GiB ([build] min_free_gib)."]
    else:
        lines.append(f"{free:.1f} GiB free — enough for a kernel build "
                     f"(floor {need} GiB)")
    if fstype:
        lines.append(f"filesystem: {fstype}" + (f" on {source}" if source else ""))

    # Removable media is allowed but never silently: unplugging it mid-build
    # ends the build. Fail-fast means the system stays intact, but six hours
    # do not come back.
    if _is_usb_backed(source):
        lines.append("NOTE: this disk is attached over USB. Unplugging it during "
                     "a build ends the build (nothing is deployed, but the hours "
                     "are lost), and it must be mounted before one starts.")
    return True, lines


def _is_usb_backed(source: str, sysfs="/sys/class/block") -> bool:
    """True when a block device sits behind USB.

    ``/sys/block/<dev>/removable`` is NOT the test, though it is the obvious
    one: a USB enclosure containing an ordinary SSD reports **0**, because the
    drive inside really is fixed — it is the bridge that unplugs. That is the
    common case for exactly the disks people would point a build at, so the
    flag would have made this warning fire for card readers and nothing else.
    Verified on the testbed: /sys/block/sdb/removable = 0 while the device path
    reads .../usb4/4-2/... The path is authoritative.

    Uses the PARTITION's own sysfs node rather than deriving the parent disk:
    stripping trailing digits turns nvme0n1p1 into nvme0n1p, and the partition
    node's realpath already contains the whole bus chain.
    """
    name = os.path.basename(source or "")
    if not name:
        return False
    try:
        return "/usb" in os.path.realpath(os.path.join(sysfs, name))
    except OSError:
        return False


def build_space_line(config: Config, run=_run,
                     disk_usage=shutil.disk_usage) -> str:
    """One line naming where kernel builds happen and whether it fits."""
    where = config.build_space
    default = config.masterdir is None
    free = _free_gib(where, disk_usage)
    room = f"{free:.1f} GiB free" if free is not None else "free space unknown"
    tag = "default (the void-packages checkout)" if default else "configured"
    verdict = ""
    if free is not None and free < config.min_free_gib:
        verdict = (f" — below the {config.min_free_gib} GiB a kernel build needs; "
                   "set [build] masterdir to a roomier disk")
    return f"kernel build space: {where}  [{tag}]  {room}{verdict}"


def build_preflight(config: Config, build_list, out,
                    disk_usage=shutil.disk_usage) -> str:
    """§7.5 preflight. "" when the build may start, else the refusal text.

    Two checks, both normative in the spec since day one and implemented only
    now: the masterdir is initialised (``masterdir*/.xbps_chroot_init``), and
    free disk on the hostdir's and the masterdir's filesystems clears a floor.

    **The floor scales to what is queued**, which the spec did not say and the
    first implementation got wrong: ``[build] min_free_gib`` (default 30) is a
    KERNEL number -- a kernel build tree alone reached 20 GB on the testbed
    before the disk ran out, and packaging wants room on top. Demanding the
    same 30 GiB to rebuild gamemode is nonsense, and it cost the owner a
    routine 46-package update within an hour of this landing: the run was
    refused, so the upstream pass never happened either. Userspace builds get
    ``min_free_userspace_gib`` (default 5) instead.

    Refusing at minute one costs nothing; discovering it at hour six cost a
    night.
    """
    if not build_list:
        return ""
    problems = []
    mds = _masterdirs(config)
    if not mds or not any((md / ".xbps_chroot_init").exists() for md in mds):
        _m = f" -m {config.masterdir}" if config.masterdir else ""
        problems.append(
            "the build chroot is not initialized (no .xbps_chroot_init under "
            f"{config.build_space}) — run: cd {config.void_packages} && "
            f"./xbps-src{_m} binary-bootstrap")
    kernel_queued = KERNEL_TARGET in build_list
    need = config.min_free_gib if kernel_queued else config.min_free_userspace_gib
    hostdir = Path(config.void_packages) / "hostdir"
    master = mds[0] if mds else config.build_space

    # The two directories have genuinely different appetites, and giving them
    # the same floor is wrong in the expensive direction. The masterdir holds
    # the BUILD TREE -- that is where 20 GB went and what min_free_gib is sized
    # for. The hostdir holds the finished packages, the source tarballs and
    # ccache: a kernel run adds a few hundred MB plus the transient 2.1 GB of
    # debug symbols, nowhere near 20 GB.
    #
    # Found the moment the build space was first moved for real: the masterdir
    # had 257 GiB free on another disk and the run was still refused, because
    # the hostdir's 20.6 GiB failed a floor meant for a build tree that was no
    # longer even on that filesystem.
    for label, path, floor, why in (
            ("masterdir (the build tree)", master, need,
             " — a kernel build tree alone reached 20 GB before the testbed ran "
             "out of disk" if kernel_queued else ""),
            ("hostdir (packages, sources, ccache)", hostdir,
             config.min_free_userspace_gib, "")):
        free = _free_gib(path, disk_usage)
        if free is None or free >= floor:
            continue
        key = ("min_free_gib" if (floor == config.min_free_gib and kernel_queued)
               else "min_free_userspace_gib")
        what = ("a kernel build" if kernel_queued else
                "building " + ", ".join(build_list[:3]))
        problems.append(
            f"only {free:.1f} GiB free on the {label} filesystem ({path}); "
            f"{what} needs at least {floor} GiB ([build] {key})" + why)
        for tree, size in _leftover_trees(config):
            problems.append(
                f"  a build tree from a previous run holds {size / GIB:.1f} GiB "
                f"({tree.name}) — reclaim it with: cd {config.void_packages} && "
                f"./xbps-src clean {_srcpkg_of_tree(tree.name)}")
    if not problems:
        return ""
    return ("refusing to build (§7.5 preflight — nothing has been changed):\n  "
            + "\n  ".join(problems))


def frozen_explanation(state_name: str, candidate: str = "") -> list:
    """Why the kernel path is frozen, in the reader's terms. One implementation.

    There were two, and they disagreed. `--status` and `--kernel-ack` both
    describe the same state file, and the candidateless branch of each said
    "most likely a health blip" — which is right for a watchdog mislog and
    flatly wrong for AWAIT_HUMAN_BUILD, where a build genuinely failed and
    produced no candidate. On 2026-09-07 that sent the reader looking for a
    phantom health problem instead of the build log named two lines above.
    Fixing it in one place and not the other is how a fact ends up with two
    voices, so now there is one.
    """
    if candidate:
        return [f"{candidate} did NOT pass — the kernel path stays frozen until "
                "you acknowledge it."]
    if state_name == "AWAIT_HUMAN_BUILD":
        return ["the last linux-cachy BUILD failed, so no kernel was produced "
                "(§8.5 G3). Nothing is wrong with the running kernel.",
                "the last-run notice in --status says why the build failed."]
    if state_name in ("AWAIT_HUMAN_TEMPLATE", "AWAIT_HUMAN_PATCH",
                      "AWAIT_HUMAN_SERIES", "HALT_HASH_MISMATCH"):
        return [f"the kernel path stopped at a human gate ({state_name}) before "
                "any kernel was built — nothing was installed or staged."]
    return ["no candidate is recorded, so this freeze is not about a kernel "
            "that failed — most likely a health blip was recorded as one by a "
            "watchdog older than this version."]


def _frozen_kernel_state(config: Config) -> str:
    """The persisted kernel state if it is a frozen one (§8.8), else ""."""
    try:
        name = grub.KernelStateStore(config.kernel_state_path).load().get("state") or ""
    except (grub.GrubError, OSError, ValueError):
        return ""
    return name if name in FROZEN_STATES else ""


def _note_kernel_build_failure(config: Config, pkg: str, out) -> None:
    """G3 (§8.5): a failed linux-cachy build freezes the kernel path.

    Normative in the gate table ("exit 40 → AWAIT_HUMAN_BUILD") and never
    implemented: the state stayed READY, so the next scheduled run would have
    tried the identical build again. A human looks first now, and says so
    with --kernel-ack; userspace is never held up by it.
    """
    if pkg != KERNEL_TARGET or not config.kernel_enable:
        return
    _record_kernel_state(config, {"state": "AWAIT_HUMAN_BUILD"}, out)
    out("kernel: G3 build failed → AWAIT_HUMAN_BUILD (§8.5). Kernel updates pause "
        "until a human has looked; userspace updates continue on later runs. "
        "Resume with: cachy-void-update --kernel-ack")


TRAY_SOCKET_NAME = "cachy-updater-tray"
TRAY_POKE = b"refresh"


def poke_tray(sockdir: str = "/tmp") -> bool:
    """Tell the tray to re-probe. True if the poke was delivered.

    The GUI has done this since f0c0296 over the tray's instance-lock socket,
    but only the GUI: the §4.9 scheduled service, a terminal `--commit`, and
    `--kernel-ack` all change exactly the state the tray displays and told it
    nothing, leaving a stale badge for up to the poll interval (3 hours by
    default). Observed the long way round on the testbed (2026-09-09): a kernel
    promoted at 03:03 and the tray still offered to build it, because every
    step of the repair had happened outside the GUI.

    A RAW AF_UNIX socket on purpose — QLocalServer on Unix is a plain stream
    socket at ``$TMPDIR/<name>``, so this needs no PyQt5. That matters: PyQt5
    is an optional dependency (a headless box has no GUI and no tray), and the
    core CLI must not acquire a GUI toolkit to send seven bytes. Verified
    against the running tray by A/B with a control window, not assumed.

    Never raises and never blocks for long: no tray is the ordinary case on a
    server or a bare WM, and a poke failing must never affect a run's outcome.
    """
    import socket
    path = os.path.join(sockdir, TRAY_SOCKET_NAME)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(1.0)
        try:
            sock.connect(path)
            sock.sendall(TRAY_POKE)
        finally:
            sock.close()
        return True
    except (OSError, AttributeError):
        return False


def prune_run_logs(config: Config, keep: int = 20) -> None:
    """Keep the newest ``keep`` run directories (§7.6: "keep 20").

    Specified and never implemented; it matters more now that an upstream-only
    run journals too, so runs accumulate on every --commit rather than only on
    ones that build something. Best-effort: losing a log is never worth failing
    a run over.
    """
    try:
        root = Path(config.log_root)
        runs = sorted(d for d in root.iterdir()
                      if d.is_dir() and d.name.startswith("run-"))
    except OSError:
        return
    import shutil as _shutil
    for old in runs[:-keep] if keep > 0 else runs:
        try:
            _shutil.rmtree(old)
        except OSError:
            pass


def last_run_failure(config: Config):
    """The newest run's failure record as a dict, or None.

    Witness-only (§7.6): nothing here drives control flow. It exists so the
    report and the tray can say "the last run failed, here is why" -- the one
    fact about the 3am run that no surface carried at 11am.
    """
    root = Path(config.log_root)
    try:
        if not root.is_dir():
            return None
        runs = sorted(d for d in root.iterdir()
                      if d.is_dir() and d.name.startswith("run-"))
    except OSError:
        return None
    if not runs:
        return None
    rd = runs[-1]
    try:
        data = read_json(rd / "journal.json")
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("phase") != "failed":
        return None
    failure = data.get("failure") or {}
    when = ""
    try:
        for line in (rd / "journal.log").read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get("event") == "failed":
                when = str(ev.get("ts") or "")
    except OSError:
        pass
    pkg = failure.get("pkg")
    log = None
    if pkg:
        log = ((data.get("pkgs") or {}).get(pkg) or {}).get("log")
    return {"run_id": data.get("run_id") or rd.name[4:], "pkg": pkg,
            "exit": failure.get("exit"), "reason": failure.get("reason") or "",
            "when": when, "log": log, "dir": str(rd)}


def _ago(ts: str, now=None) -> str:
    try:
        t = time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")) - time.timezone
    except (ValueError, OverflowError):
        return ""
    secs = max(0, int((now if now is not None else time.time()) - t))
    if secs < 90:
        return "just now"
    if secs < 5400:
        return f"{secs // 60} min ago"
    if secs < 172800:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


def last_run_summary(config: Config, now=None) -> str:
    """Human lines for --status about the newest run, if it failed; else ""."""
    f = last_run_failure(config)
    if not f:
        return ""
    if f["when"]:
        stamp = f["when"].replace("T", " ").rstrip("Z") + " UTC"
        ago = _ago(f["when"], now)
        when = f"{ago} ({stamp})" if ago else f"at {stamp}"
    else:
        when = f"in run {f['run_id']}"
    if f["exit"] == EXIT_PREFLIGHT:
        head = f"last update run was REFUSED before building, {when}:"
        detail = f["reason"] or "the §7.5 preflight check failed"
    else:
        head = f"last update run FAILED {when}:"
        what = f"{f['pkg']}: " if f["pkg"] else ""
        detail = what + (f["reason"] or f"exit {f['exit']}")
    lines = [head, "  " + detail]
    if f["log"]:
        lines.append(f"  log: {f['log']}")
    lines.append("  (a later successful run replaces this notice)")
    return "\n".join(lines)


def _debug_pkgs(config: Config) -> list:
    """``(path, bytes)`` of debug-symbol binpkgs in the local repo's debug dir."""
    d = Path(config.repos[0]) / "debug"
    if not d.is_dir():
        return []
    found = []
    try:
        for f in sorted(d.glob("*.xbps")):
            found.append((f, f.stat().st_size))
    except OSError:
        pass
    return found


def _drop_debug_files(files, config: Config, run) -> int:
    """Delete debug binpkgs (user-owned; no privilege) and tidy the index."""
    freed = 0
    for f, size in files:
        try:
            Path(f).unlink()
            freed += size
        except OSError:
            pass
    d = Path(config.repos[0]) / "debug"
    try:
        if any(d.glob("*.xbps")):
            run(["xbps-rindex", "-c", str(d)])
        else:
            for idx in list(d.glob("*-repodata")) + list(d.glob("*-stagedata")):
                idx.unlink()
            d.rmdir()
    except OSError:
        pass
    return freed


def cmd_commit(xbps, config: Config, *, assume_yes: bool, dry_run: bool,
               out=print, run=_run, confirm=input, stage_layout=None,
               service_root: Path = Path("/var/service"),
               preflight=build_preflight) -> int:
    """Stages 3-4 — build, deploy, gate & stage kernel (§4.4-§4.7, §8.5-§8.6).

    The kernel synthesis circuit (§8.2→§8.3→§8.4) runs first: a detected upstream
    bump is trust-verified and the linux-cachy template regenerated *before* the
    queue is built, so a freshly bumped kernel enters Q organically. Any
    kernel-path failure withholds the kernel and userspace still proceeds.
    """
    _report_previous_run(config, out)
    _kernel_synthesis(config, xbps, out)

    try:
        plan = build_queue(xbps, config.targets, config.blacklist,
                           config.repo_strs, always_build=_always_build(config))
        if not plan.q_build and not plan.q_deploy:
            out("queue empty — nothing to build or deploy.")
            # The overlay needs nothing, but on a rolling base upstream updates may
            # still be pending — and tier [1] of --status reports them. An Update
            # that then does nothing breaks the "update everything" promise (same
            # class as the Flatpak gap), so the system pass still runs (§4.5a).
            if dry_run:
                return EXIT_OK
            return _system_update(config, xbps, out, run, confirm, assume_yes,
                                  service_root=service_root)
        order = topo_order(xbps, plan.q_build)
    except MappingError as exc:
        out(f"error: srcpkg mapping anomaly: {exc}")
        return EXIT_MAPPING
    except CycleError as exc:
        out(f"error: {exc}")
        return EXIT_CYCLE
    except XbpsError as exc:
        out(f"error: queue construction failed: {exc}")
        return EXIT_QUERY
    except OSError as exc:
        out(f"error: environment failure during queries: {exc}")
        return EXIT_QUERY

    build_list = [*order.order, *order.second_pass]
    q_deploy = list(plan.q_deploy)

    def _withhold(reason: str):
        """Drop the kernel from both queues, say why, and -- if that empties
        them -- still run the §4.5a system pass.

        That last part was missing from every withhold path: they returned
        EXIT_OK, so a run whose only queue member was the kernel skipped the
        upstream update entirely. The §8 preamble is explicit that a kernel
        stall never blocks userspace, and §4.5a is explicit that an empty
        queue still does the system pass; the withhold paths obeyed neither.
        """
        bl = [q for q in build_list if q != KERNEL_TARGET]
        qd = [t for t in q_deploy if t != KERNEL_TARGET]
        out(reason)
        return bl, qd

    # The CHEAP withholds happen before the queue is printed, so the preview
    # states what the run will actually do. They used to sit after the dry-run
    # exit, which meant `--commit --no-kernel --dry-run` advertised a kernel
    # build that the same flag then withheld -- the preview contradicting the
    # run it previews. The G2 gate stays below the prompt: it shells out to
    # `xbps-src configure`, which is minutes of work, and a preview must stay
    # cheap.
    #
    # --no-kernel scopes the run to userspace, and until now it did not
    # actually remove the kernel from the queue: `_always_build` governs only
    # the K-exemption (queueing a kernel that is NOT installed), while an
    # installed linux-cachy whose regenerated template outranks the local repo
    # enters by the ordinary outdated-and-installed rule regardless. Both
    # kernel guards are gated on kernel_enable, so under --no-kernel the kernel
    # would have gone straight into the build loop WITHOUT the G2 config gate
    # -- i.e. the "Update" button, which promises to leave the kernel alone,
    # would have started a multi-hour compile and skipped the only defence
    # against a silently descheduled kernel. Found on the testbed (2026-09-06):
    # a failed unattended run had left a regenerated 6.12.108 template on disk,
    # so Update's queue was exactly [linux-cachy].
    if not config.kernel_enable and (KERNEL_TARGET in build_list
                                     or KERNEL_TARGET in q_deploy):
        build_list, q_deploy = _withhold(
            f"note: {KERNEL_TARGET} is queued but this run is --no-kernel — "
            "withheld. Use 'Update kernel' (or drop --no-kernel) to build it.")
    # §8.8: a frozen kernel path is withheld from the queue as well, not
    # only from synthesis -- the template already on disk would otherwise
    # walk straight into the build.
    if config.kernel_enable and KERNEL_TARGET in build_list:
        frozen = _frozen_kernel_state(config)
        if frozen:
            build_list, q_deploy = _withhold(
                f"warning: {KERNEL_TARGET} withheld from this run — kernel "
                f"state is {frozen} (frozen, §8.8); resume with "
                "`cachy-void-update --kernel-ack`. Userspace updates continue.")
    if not build_list and not q_deploy:
        out("queue empty after kernel withhold — the system pass still runs (§4.5a).")
        if dry_run:
            return EXIT_OK
        return _system_update(config, xbps, out, run, confirm, assume_yes,
                              service_root=service_root)

    out(f"build order  [{order.provenance}]: {' -> '.join(build_list) or '-'}")
    out(f"deploy queue ({len(q_deploy)}): {', '.join(q_deploy) or '-'}")
    if dry_run:
        out("dry-run: stopping before build.")
        return EXIT_OK
    # F5: ANY commit that reaches this point mutates the system (deploy at
    # minimum) — confirmation is unconditional in interactive mode.
    if not assume_yes:
        if confirm("proceed with build/deploy? [y/N] ").strip().lower() not in ("y", "yes"):
            out("aborted by user.")
            return EXIT_OK

    # §8.5 G2 gate — after the prompt (configure is minutes of work), before
    # any compile. A withheld kernel never blocks userspace (§8 preamble).
    if config.kernel_enable and KERNEL_TARGET in build_list:
        if not _g2_gate(config, xbps, out):
            _record_kernel_state(config, {"state": "AWAIT_HUMAN_TEMPLATE"}, out)
            build_list, q_deploy = _withhold(
                f"warning: {KERNEL_TARGET} withheld from this run "
                "(AWAIT_HUMAN_TEMPLATE, §8.5); userspace updates continue.")
            if not build_list and not q_deploy:
                out("queue empty after kernel withhold — the system pass still "
                    "runs (§4.5a).")
                return _system_update(config, xbps, out, run, confirm, assume_yes,
                                      service_root=service_root)

    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rundir = config.log_root / f"run-{run_id}"
    git_head = ""
    try:
        git_head = run(["git", "rev-parse", "HEAD"],
                       str(config.void_packages)).stdout.strip()
    except OSError:
        pass
    journal = Journal(rundir).start(run_id, git_head)
    journal.set_phase("build")
    prune_run_logs(config)
    journal.set_order(build_list, order.provenance)

    # §7.5 preflight — refuse BEFORE the first compile, and say why. Specified
    # from the start, implemented only after six hours of kernel build died
    # at the module-link stage with ENOSPC. Journaled so --status can report
    # the refusal; the kernel state is deliberately untouched -- a full disk
    # is the environment's fault, not the kernel's, and a retry once space
    # exists needs no human acknowledgement.
    refusal = preflight(config, build_list, out)
    if refusal:
        out(refusal)
        _lines = refusal.splitlines()
        journal.fail(None, EXIT_PREFLIGHT,
                     reason=(_lines[1].strip() if len(_lines) > 1 else refusal))
        return EXIT_PREFLIGHT

    # Stage 3 — build (§7.5)
    for pkg in build_list:
        journal.set_pkg_status(pkg, "building")
        log_path = str(rundir / f"build-{pkg}.log")
        try:
            xbps.clean(pkg)
            rc = xbps.build(pkg, config.effective_jobs, log_path)
        except (XbpsError, OSError) as exc:
            journal.set_pkg_status(pkg, "failed", log=log_path)
            journal.fail(pkg, EXIT_BUILD, reason=f"build environment failure: {exc}")
            out(f"error: build environment failure for {pkg}: {exc}")
            if getattr(exc, "errno", None) == 28 or "No space left" in str(exc):
                for line in disk_lines(config):
                    out("  " + line)
            _note_kernel_build_failure(config, pkg, out)
            return EXIT_BUILD
        if rc != 0:
            journal.set_pkg_status(pkg, "failed", log=log_path)
            journal.fail(pkg, EXIT_BUILD, reason=f"xbps-src exited {rc}")
            out(f"error: build failed for {pkg} (rc={rc}); see {log_path}")
            _emit_tail(log_path, out)
            _note_kernel_build_failure(config, pkg, out)
            return EXIT_BUILD
        journal.set_pkg_status(pkg, "built", log=log_path)
        # The kernel template hand-builds a -dbg package (see
        # Xbps.drop_debug_pkgs): 2 GB per kernel into a repo nothing installs
        # from. Dropped here, right after the build that made it.
        freed = xbps.drop_debug_pkgs(pkg)
        if freed:
            out(f"dropped the debug-symbol package(s) of {pkg} "
                f"({freed / GIB:.1f} GiB; the debug repo is never installed from)")

    # §9.5 pre-deploy snapshot — a btrfs rollback net taken IMMEDIATELY before the
    # Stage 4 `-Suy`, and only when something will actually deploy. Witness-only
    # (§7.6). A forced-but-unavailable (53) or a failed (54) snapshot aborts here,
    # before any system mutation.
    if q_deploy:
        try:
            snapshot.pre_deploy_snapshot(
                enable=config.snapshot_enable, subvol=config.snapshot_subvol,
                snap_dir=config.snapshot_dir, keep=config.snapshot_keep,
                run_id=run_id, run=run, out=out)
        except snapshot.SnapshotUnavailable as exc:
            out(f"error: {exc}")
            journal.fail(None, EXIT_SNAPSHOT_UNAVAIL)
            return EXIT_SNAPSHOT_UNAVAIL
        except snapshot.SnapshotFailed as exc:
            out(f"error: {exc}")
            journal.fail(None, EXIT_SNAPSHOT_FAILED)
            return EXIT_SNAPSHOT_FAILED

    # Stage 4 — deploy (§4.5-§4.7)
    journal.set_phase("deploy")
    try:
        deploy_bins = sorted(b for b in xbps.installed()
                             if xbps.srcpkg_of(b) in set(q_deploy))
        journal.set_deploy_bins(deploy_bins)
        rc = _deploy(config, deploy_bins, xbps, out, run)
    except (XbpsError, OSError) as exc:
        out(f"error: deploy failure: {exc}")
        rc = EXIT_INSTALL
    if rc != EXIT_OK:
        journal.fail(None, rc)
        return rc

    # §4.7 Stage 4c — cycle services running replaced binaries. A bare `-Su`
    # re-execs daemons like sshd into a half-updated state (finding #3, which
    # dropped the ssh lifeline mid-run); a controlled `sv restart` is the fix.
    rc_services = _cycle_services(config, out, run, service_root=service_root)

    # Flatpak apps (independent of XBPS) — "update everything" or don't call it an
    # updater. No-op if Flatpak isn't installed; loud on failure.
    rc_flatpak = _update_flatpak(config, out, run)

    # §8.6 kernel staging (F1/F7: real staging inside a GrubError boundary)
    rc_kernel = EXIT_OK
    if config.kernel_enable and KERNEL_TARGET in q_deploy:
        if KERNEL_TARGET not in set(xbps.installed()):
            # §7.3 K-exemption completes here — the single sanctioned widen:
            # the kernel is INTRODUCED, with headers (§2.5) so dkms modules
            # (nvidia) build against it during install.
            out(f"kernel: first install of {KERNEL_TARGET} + headers (§8.6)")
            repo_args = [f"--repository={r}" for r in config.repos]
            cp = run(["sudo", "xbps-install", "-y", *repo_args,
                      KERNEL_TARGET, f"{KERNEL_TARGET}-headers"])
            if cp.returncode != 0:
                out("error: kernel first-install failed — staging skipped "
                    "(userspace deploy is intact)")
                rc_kernel = EXIT_KERNEL
        if rc_kernel == EXIT_OK:
            rc_kernel = _stage_kernel(config, xbps, out, run, layout=stage_layout)

    journal.finish()
    # Severity order: kernel staging failure (70) > service cycling partial
    # (60) > flatpak partial (56) > clean. Userspace is already deployed in every
    # branch; a flatpak failure never undoes the XBPS deploy, only reports.
    if rc_kernel != EXIT_OK:
        out("commit complete — userspace deployed, kernel staging FAILED (see above).")
        return rc_kernel
    if rc_services != EXIT_OK:
        out("commit complete — deployed & staged; some services need a manual "
            "restart or relogin (§4.7).")
        return rc_services
    if rc_flatpak != EXIT_OK:
        out("commit complete — system deployed; some Flatpak updates did NOT apply "
            "(see above).")
        return rc_flatpak
    out("commit complete.")
    return EXIT_OK


def cmd_rollback(config: Config, out=print, run=_run) -> int:
    """Re-pin the GRUB default to the known-good kernel (§8.6/§8.7)."""
    try:
        store = grub.KernelStateStore(config.kernel_state_path)
        state = store.load()
        good = state.get("known_good")
        if not good:
            out("no known-good kernel recorded; nothing to roll back.")
            return EXIT_OK

        layout = grub.detect_boot_layout(run=run)
        if layout.mode == grub.MODE_SKIP:
            out(f"boot rollback skipped: {layout.reason}")
            return EXIT_OK
        if layout.mode == grub.MODE_MANUAL_UNSAFE:
            out(f"boot rollback impossible: {layout.reason}")
            return EXIT_KERNEL
        ref = grub.promote(layout=layout, candidate_kver=good["kver"],
                           run=_sudo(run))
        out(f"rolled back: GRUB default pinned to known-good {good['kver']} "
            f"({ref}).")
        return EXIT_OK
    except grub.GrubError as exc:
        out(f"error: {exc}")
        return EXIT_KERNEL
    except OSError as exc:
        out(f"error: rollback environment failure: {exc}")
        return EXIT_KERNEL


# ==========================================================================
# Old-kernel inventory (§2.5/§4.7 — suggest, never purge)
# ==========================================================================
# `vkpurge list` also prints "current", which is the /boot/vmlinuz-current
# SYMLINK, not a kernel. Offering it as removable would invite a user to delete
# their boot symlink, so it is filtered here in one place for every caller.
VKPURGE_NON_KERNELS = frozenset({"current"})


@dataclass(frozen=True)
class OldKernel:
    kver: str
    size_kb: Optional[int]
    role: str            # "removable" | "fallback" | "running"


def classify_old_kernels(vkpurge_lines: Sequence[str], *,
                         known_good: str = "", running: str = "",
                         size_of: Optional[Callable[[str], Optional[int]]] = None
                         ) -> list[OldKernel]:
    """Annotate `vkpurge list` output so a human can act on it safely.

    Leftover kernel files are not interchangeable: one of them may be the
    recorded rollback target, and one may be the kernel currently running.
    Naming which is which is the difference between a useful suggestion and a
    dangerous one — so roles are computed, never left to the reader.
    """
    items: list[OldKernel] = []
    for raw in vkpurge_lines:
        kver = raw.strip()
        if not kver or kver in VKPURGE_NON_KERNELS:
            continue
        if running and kver == running:
            role = "running"
        elif known_good and kver == known_good:
            role = "fallback"
        else:
            role = "removable"
        items.append(OldKernel(kver, size_of(kver) if size_of else None, role))
    return items


def old_kernel_lines(items: Sequence[OldKernel]) -> list[str]:
    """Render the inventory, warning when superseded kernels are piling up.

    Deliberately no auto-purge (§2.5/§4.7): a kernel that boots healthy today
    can still fail next week on suspend or a codec path, and rebuilding one
    costs hours. Piling up silently is the only part worth fixing, so it is
    reported instead of resolved.
    """
    lines: list[str] = []
    for k in items:
        size = f"{k.size_kb // 1024} MB" if k.size_kb else "size unknown"
        if k.role == "fallback":
            lines.append(f"{k.kver} — {size}   [rollback target: KEEP]")
        elif k.role == "running":
            lines.append(f"{k.kver} — {size}   [currently running: KEEP]")
        else:
            lines.append(f"{k.kver} — {size}   (remove: sudo vkpurge rm {k.kver})")
    spare = [k for k in items if k.role == "removable"]
    if len(spare) > 1:
        total = sum(k.size_kb or 0 for k in spare) // 1024
        lines.append(
            f"warning: {len(spare)} superseded kernels are piling up"
            + (f" (~{total} MB)" if total else "")
            + " — one spare is enough. Purges stay manual (§2.5/§4.7); "
              "remove the oldest first.")
    return lines


def _kernel_inventory(config: Config, run) -> list[OldKernel]:
    """Collect the annotated old-kernel inventory (best-effort, read-only)."""
    def _size(kver: str) -> Optional[int]:
        try:
            cp = run(["du", "-sk", f"/lib/modules/{kver}"])
            return int(cp.stdout.split()[0]) if cp.returncode == 0 else None
        except (OSError, ValueError, IndexError):
            return None

    try:
        cp = run(["vkpurge", "list"])
        raw = [l for l in (cp.stdout or "").splitlines() if l.strip()] \
            if cp.returncode == 0 else []
    except OSError:
        return []
    good = ""
    try:
        state = grub.KernelStateStore(config.kernel_state_path).load()
        good = (state.get("known_good") or {}).get("kver") or ""
    except (OSError, grub.GrubError):
        pass
    try:
        running = os.uname().release
    except (AttributeError, OSError):
        running = ""
    return classify_old_kernels(raw, known_good=good, running=running,
                                size_of=_size)


# ==========================================================================
# Maintenance / cleanup (§4.7 note; extends the §4.1 sudo boundary)
# ==========================================================================
def _local_origin_orphans(orphan_lines: Sequence[str],
                          local_repos: Sequence[str]) -> list[str]:
    """Orphans whose *origin* is one of our overlay repos (§7.1 name domains).

    A package the overlay deliberately BUILT is not garbage, even when nothing
    links it: that is the signature of a runtime-only library (LD_PRELOAD /
    dlopen), which xbps cannot see a dependency edge for. `xbps-remove -o`
    cannot exclude individual packages — and the §4.1 grant forbids naming any —
    so an orphan sweep containing one of these is all-or-nothing and must not
    proceed. Live case: libgamemode (ships libgamemodeauto.so.0 for
    gamemoderun) was orphan-eligible, and removing it would have broken the
    gaming layer silently.
    """
    hits: list[str] = []
    for line in orphan_lines:
        fields = line.split()
        if len(fields) < 4:
            continue
        if any(fields[3] == repo or fields[3].startswith(repo + "/")
               for repo in local_repos):
            hits.append(fields[0])
    return hits


def cmd_clean(config: Config, *, assume_yes: bool, dry_run: bool = False,
              out=print, run=_run, confirm=input) -> int:
    """Reclaim disk: remove orphaned packages, clean the obsolete package
    cache, and drop debug-symbol packages from the local repo (user-owned
    files in ``hostdir/binpkgs/debug`` -- 2 GB per kernel build, never an
    install source, see Xbps.drop_debug_pkgs). Preview-then-confirm; every removal goes through the §4.1 sudo
    boundary (this adds exactly the two ``xbps-remove`` maintenance forms — the
    minimal widening, nothing that can name a package).

    Old kernels are **suggested, never purged**: §2.5/§4.7 make kernel removal a
    manual step (always keep the last known-good kernel until the new one has
    survived a real session), so this only prints ``vkpurge list`` output. The
    ``vkpurge`` binary is deliberately absent from the sudoers grant.
    """
    sudo = _sudo(run)

    def _lines(cp):
        return [l for l in (cp.stdout or "").splitlines() if l.strip()]

    out("Cachy-Void — cleanup")
    out("=" * 46)

    # -- preview (dry-run, through sudo so it works whether or not root is needed)
    try:
        orphans = _lines(sudo(["xbps-remove", "-o", "-n"]))
    except OSError as exc:
        out(f"error: cannot preview orphans: {exc}")
        return EXIT_CLEAN
    try:
        cache = _lines(sudo(["xbps-remove", "-O", "-n"]))
    except OSError:
        cache = []

    out(f"\norphaned packages to remove: {len(orphans)}")
    for l in orphans:
        out("    " + l)
    out(f"obsolete cached packages to clean: {len(cache)}")
    dbg = _debug_pkgs(config)
    if dbg:
        out(f"debug-symbol packages to drop from the local repo: {len(dbg)} "
            f"({sum(sz for _f, sz in dbg) / GIB:.1f} GiB)")
        for f, sz in dbg:
            out(f"    {Path(f).name}  {sz / GIB:.1f} GiB")

    # Refuse a mixed sweep: an orphan we BUILT is a protection gap, not garbage.
    ours = _local_origin_orphans(orphans, config.repo_strs)
    if ours:
        out("\nREFUSING to remove orphans: " + ", ".join(ours)
            + f" {'was' if len(ours) == 1 else 'were'} built by this overlay.")
        out("    Nothing links them, but that is exactly how a runtime-only")
        out("    library looks (LD_PRELOAD/dlopen) — removing one can break the")
        out("    overlay silently. `xbps-remove -o` cannot skip a single package,")
        out("    so the whole orphan sweep is skipped.")
        for p in ours:
            out(f"    keep it:  sudo xbps-pkgdb -m manual {p}")
        out("    (or drop it from the allowlist if it is genuinely unused, then"
            " re-run)")
        orphans = []          # cache cleaning below is unaffected

    # old kernels — SUGGEST ONLY (never purge; §2.5/§4.7)
    inv = _kernel_inventory(config, run)
    if inv:
        out("\nold kernel files present (NOT removed — kernel purges are manual, "
            "§2.5/§4.7):")
        for line in old_kernel_lines(inv):
            out(f"    {line}")

    if not orphans and not cache and not dbg:
        out("\nnothing to clean — no orphans, cache already tidy, no debug packages.")
        return EXIT_OK

    if dry_run:
        # The front-end previews with this before asking, so a user sees WHAT
        # disappears rather than agreeing to a category ("orphans and cache").
        out("\n(preview only — nothing was removed)")
        return EXIT_OK

    if not assume_yes:
        if confirm("\nremove orphans, clean the cache and drop the debug packages "
                   "listed? [y/N] ").strip().lower() \
                not in ("y", "yes"):
            out("aborted by user.")
            return EXIT_OK

    rc = EXIT_OK
    if orphans:
        if sudo(["xbps-remove", "-o", "-y"]).returncode == 0:
            out(f"removed {len(orphans)} orphaned package(s).")
        else:
            out("error: removing orphans failed.")
            rc = EXIT_CLEAN
    if cache:
        if sudo(["xbps-remove", "-O", "-y"]).returncode == 0:
            out("cleaned obsolete package cache.")
        else:
            out("error: cleaning the cache failed.")
            rc = EXIT_CLEAN
    if dbg:
        freed = _drop_debug_files(dbg, config, run)
        out(f"dropped {len(dbg)} debug-symbol package(s) ({freed / GIB:.1f} GiB).")
    out("cleanup complete." if rc == EXIT_OK else "cleanup finished with errors.")
    return rc


# ==========================================================================
# GPU / driver advisory (read-only)
# ==========================================================================
# Coarse NVIDIA family → recommended driver series. Precise per-device mapping
# needs a PCI-ID table; this keeps a human-checkable rule of thumb keyed on the
# marketing name that lspci already prints.
_NVIDIA_LEGACY_HINT = (
    "NVIDIA driver series by GPU family: Kepler (GeForce 6xx/7xx) -> nvidia470; "
    "Fermi (4xx/5xx) -> nvidia390; Maxwell and newer (9xx/10xx/16xx/20xx+) -> "
    "nvidia (current). Match your card above; the wrong series will not load.")

# Chip codes are far more reliable than marketing names for this, and lspci
# usually prints them ("GK107M [GeForce GT 730M]"): GF=Fermi, GK=Kepler,
# GM/GP/GV/TU/GA/AD = Maxwell and newer.
_NVIDIA_CHIP_RE = re.compile(r"\b(gf|gk|gm|gp|gv|tu|ga|ad)\d{3}", re.I)
_NVIDIA_CHIP_SERIES = {"gf": "nvidia390", "gk": "nvidia470"}
_NVIDIA_FAMILY_NAME = {"gf": "Fermi", "gk": "Kepler", "gm": "Maxwell",
                       "gp": "Pascal", "gv": "Volta", "tu": "Turing",
                       "ga": "Ampere", "ad": "Ada"}


def expected_nvidia_series(gpu_blob: str) -> tuple[str, str]:
    """(package, family-name) the detected chip wants, or ("", "") if unknown.

    Keyed on the chip code rather than the marketing name — "GK107M" is
    unambiguous where "GeForce GT 730M" is not.
    """
    m = _NVIDIA_CHIP_RE.search(gpu_blob)
    if not m:
        return "", ""
    prefix = m.group(1).lower()
    return (_NVIDIA_CHIP_SERIES.get(prefix, "nvidia"),
            _NVIDIA_FAMILY_NAME.get(prefix, ""))


def dkms_kernels(status_lines: Sequence[str],
                 installed_kernels: Sequence[str] = ()) -> dict[str, str]:
    """Map kernel release -> module state from ``dkms status`` output.

    dkms has shipped two column layouts ("nvidia/470, KVER, arch: state" and
    "nvidia, 470, KVER, arch: state"), so the kernel column is identified by
    matching the set of kernels actually present under /lib/modules when it is
    known, falling back to the field before the architecture.
    """
    known = set(installed_kernels)
    found: dict[str, str] = {}
    for line in status_lines:
        head, _, state = line.partition(":")
        fields = [f.strip() for f in head.split(",")]
        if len(fields) < 2:
            continue
        kver = ""
        for f in fields[1:]:
            if f in known:
                kver = f
                break
        if not kver:
            # no /lib/modules listing available: arch is last, kernel precedes it
            kver = fields[-2] if len(fields) >= 3 else fields[1]
        if kver:
            found[kver] = state.strip() or "unknown"
    return found


def cmd_gpu(xbps, config: Config, out=print, run=_run) -> int:
    """Read-only GPU/driver advisory: detect the card, report the installed
    driver + whether an update is pending (applied by a normal Update, since
    drivers are upstream binaries), and surface DKMS health. Best-effort — every
    probe degrades to 'unknown' and the command never mutates or fails."""
    def _lines(cp):
        return [l for l in (cp.stdout or "").splitlines() if l.strip()]

    out("Cachy-Void — GPU & drivers")
    out("=" * 46)

    gpus: list[str] = []
    try:
        gpus = [g.split(": ", 1)[-1] if ": " in g else g
                for g in _lines(run(["sh", "-c",
                                     "lspci | grep -Ei 'vga|3d|display'"]))]
    except OSError:
        pass
    if gpus:
        out("\ndetected:")
        for g in gpus:
            out("    " + g)
    else:
        out("\ndetected: unknown (lspci unavailable)")
    blob = " ".join(gpus).lower()

    if "nvidia" in blob:
        out("\nNVIDIA card present.")
        # which proprietary driver package is installed?
        try:
            drv = sorted(b for b in xbps.installed()
                         if re.fullmatch(r"nvidia\d*(-dkms)?", b))
        except (XbpsError, OSError):
            drv = []
        if drv:
            for d in drv:
                try:
                    out(f"    driver package: {d} {split_pkgver(xbps.inst_pkgver(d))[1]}")
                except (XbpsError, KeyError, OSError):
                    out(f"    driver package: {d}")
            # driver updates ride the normal upstream update (dry-run check)
            try:
                pend = _lines(run(["xbps-install", "-un", *drv]))
                out("    update pending — apply via Update (system)" if pend
                    else "    driver up to date")
            except OSError:
                pass
        else:
            out("    no proprietary NVIDIA driver package installed "
                "(running nouveau, or driver not set up).")
        try:
            ver = open("/sys/module/nvidia/version", encoding="utf-8").read().strip()
            out(f"    kernel module loaded: nvidia {ver}")
        except OSError:
            out("    kernel module: nvidia not loaded")

        # The series hint is a 300-character wall; it earns its place only when
        # something looks wrong. Confirm in one line when the installed series
        # matches the detected chip, and print the full table otherwise.
        want, family = expected_nvidia_series(blob)
        if want and drv:
            if any(d == want or d.startswith(want + "-") for d in drv):
                out(f"    driver series matches this card"
                    + (f" ({family} -> {want})" if family else f" ({want})"))
            else:
                out(f"    WARNING: this looks like a {family or 'newer'} card, which "
                    f"wants {want} — installed: {', '.join(drv)}.")
                out("    " + _NVIDIA_LEGACY_HINT)
        else:
            out("    " + _NVIDIA_LEGACY_HINT)
    elif "amd" in blob or "ati" in blob or "radeon" in blob:
        out("\nAMD card — driver is Mesa (amdgpu/RADV), no proprietary package "
            "needed; it updates with the normal system Update.")
    elif "intel" in blob:
        out("\nIntel graphics — driver is Mesa (built-in), no proprietary "
            "package needed; it updates with the normal system Update.")

    # DKMS health (applies to any out-of-tree driver, nvidia*-dkms included)
    try:
        ds = _lines(run(["dkms", "status"]))
    except OSError:
        ds = []
    try:
        kernels = [k for k in _lines(run(["ls", "-1", "/lib/modules"])) if k]
    except OSError:
        kernels = []
    try:
        running = os.uname().release
    except (AttributeError, OSError):
        running = ""
    if ds:
        # Which leftover kernels are merely spares? Lets the list say why a
        # build exists for a kernel you may be about to purge.
        try:
            spares = {k.kver for k in _kernel_inventory(config, run)
                      if k.role == "removable"}
        except OSError:
            spares = set()
        out(f"\nDKMS modules ({len(ds)}):")
        for l in ds:
            note = ""
            if running and running in l:
                note = "   <- running kernel"
            else:
                # Name the command, never "see Clean up": Clean up NEVER removes
                # kernels (§2.5/§4.7), so pointing at it promised something it
                # deliberately will not do — and a user who had just run it was
                # rightly confused that the kernel was still there.
                hit = next((s for s in spares if s in l), "")
                if hit:
                    note = f"   (superseded kernel — remove: sudo vkpurge rm {hit})"
            out("    " + l + note)
        if any("installed" not in l.lower() for l in ds):
            out("    warning: a DKMS module is NOT 'installed' — it may be "
                "missing for the running kernel (rebuild: sudo "
                "xbps-reconfigure -f <driver>-dkms).")

        # The check this panel claimed to perform but did not: an installed
        # kernel with NO build at all. Listing what DKMS *has* never reveals
        # what is *missing*, and the consequence is losing the proprietary
        # driver the moment you boot that kernel. (Live find: nvidia470 has no
        # module for the installed 6.18 series.)
        built = dkms_kernels(ds, kernels)
        gaps = [k for k in kernels if k not in built]
        for k in gaps:
            out(f"    WARNING: kernel {k} has NO out-of-tree module built — "
                "booting it would leave you on the in-tree driver "
                "(nouveau/modesetting for NVIDIA).")
        if gaps:
            out("    A legacy driver series usually cannot build against a much "
                "newer kernel; rebuild attempt: sudo xbps-reconfigure -f "
                "<driver>-dkms")
    else:
        out("\nDKMS: no out-of-tree modules (driver is in-tree or absent).")

    out("")
    return EXIT_OK


# ==========================================================================
# Empty-queue system pass (§4.5a)
# ==========================================================================
def _system_update(config: Config, xbps, out, run, confirm, assume_yes,
                   service_root: Path) -> int:
    """Apply pending *upstream* updates when the overlay queue is empty.

    "If you give people an updater, it has to update everything": the overlay
    being in sync must not leave the rolling base stale while --status tier [1]
    reports updates as pending. Runs the same Stage-4 choreography as a queue
    deploy — §9.5 pre-deploy snapshot, one ``-Suy`` (via ``_deploy`` with an
    empty deploy list, keeping a single call site), §4.7 service cycling, then
    Flatpak. Held packages (e.g. pinned kernels) are honored by xbps itself.
    """
    # This run gets a journal like any other, and it is created BEFORE the
    # "nothing pending" exit on purpose. Two things were broken by its absence.
    # (1) --status's failed-run notice promises "a later successful run replaces
    # this notice", and an upstream-only run left no journal -- so a stale
    # failure sat on screen indefinitely while successful updates ran past it.
    # The owner spotted it: "is it the bore update error that just stays around
    # as a ghost until a new bore with 30 gigs of free space is made?" -- exactly
    # right, since only a run that BUILDS something journalled. (2) the snapshot
    # inventory's "upstream-only update (no overlay rebuild)" annotation could
    # never fire, because it reads the journal this path never wrote.
    run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    rundir = config.log_root / f"run-{run_id}"
    git_head = ""
    try:
        git_head = run(["git", "rev-parse", "HEAD"],
                       str(config.void_packages)).stdout.strip()
    except (OSError, AttributeError):
        pass
    journal = Journal(rundir).start(run_id, git_head)
    journal.set_phase("deploy")
    prune_run_logs(config)

    cp = run(["sudo", "xbps-install", "-Sun"])
    if cp.returncode != 0:
        out("error: could not query upstream updates (xbps-install -Sun)")
        journal.fail(None, EXIT_QUERY, reason="could not query upstream updates")
        return EXIT_QUERY
    pending = [l for l in (cp.stdout or "").splitlines()
               if len(l.split()) > 1 and l.split()[1] in ("update", "install")]
    if not pending:
        out("system: base already up to date.")
        rc_flatpak = _update_flatpak(config, out, run)
        if rc_flatpak != EXIT_OK:
            journal.fail(None, rc_flatpak, reason="flatpak update failed")
        else:
            journal.finish()
        return rc_flatpak

    out(f"system: {len(pending)} upstream update(s) pending — applying (§4.5a).")
    if not assume_yes:
        ans = confirm("apply upstream system updates now? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            out("aborted — nothing changed.")
            journal.finish()          # a declined run is not a failed one
            return EXIT_OK

    # §9.5 rollback net: this mutates the system, so it gets the same snapshot
    # protection as a queue deploy.
    try:
        snapshot.pre_deploy_snapshot(
            enable=config.snapshot_enable, subvol=config.snapshot_subvol,
            snap_dir=config.snapshot_dir, keep=config.snapshot_keep,
            run_id=run_id, run=run, out=out)
    except snapshot.SnapshotUnavailable as exc:
        out(f"error: {exc}")
        journal.fail(None, EXIT_SNAPSHOT_UNAVAIL, reason=str(exc))
        return EXIT_SNAPSHOT_UNAVAIL
    except snapshot.SnapshotFailed as exc:
        out(f"error: {exc}")
        journal.fail(None, EXIT_SNAPSHOT_FAILED, reason=str(exc))
        return EXIT_SNAPSHOT_FAILED

    rc = _deploy(config, [], xbps, out, run)
    if rc != EXIT_OK:
        journal.fail(None, rc, reason="upstream system update failed")
        return rc
    rc_services = _cycle_services(config, out, run, service_root=service_root)
    rc_flatpak = _update_flatpak(config, out, run)
    if rc_services != EXIT_OK:
        out("system update complete — some services need a manual restart "
            "or relogin (§4.7).")
        journal.finish()      # the update itself succeeded; §4.7 is advisory
        return rc_services
    if rc_flatpak != EXIT_OK:
        journal.fail(None, rc_flatpak, reason="flatpak update failed")
        return rc_flatpak
    out("system update complete.")
    journal.finish()
    return EXIT_OK


# ==========================================================================
# Deploy helper
# ==========================================================================
def _stream_run(args, out, run) -> subprocess.CompletedProcess:
    """Run a LONG command, echoing its output line-by-line through out().

    The -Suy is minutes of downloads + unpacking; with captured output the GUI
    (and a terminal) shows nothing at all for the whole stretch, which reads as
    "crashed". Only used where the caller checks returncode alone (the echoed
    stdout is not re-parsed). Falls back to the injected runner when a test (or
    any non-default runner) is in play, so mocks keep working unchanged.
    """
    if run is not _run:
        return run(args)
    proc = subprocess.Popen(list(args), stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            stdin=subprocess.DEVNULL)
    assert proc.stdout is not None
    for line in proc.stdout:
        out("  " + line.rstrip())
    proc.wait()
    return subprocess.CompletedProcess(list(args), proc.returncode,
                                       stdout="", stderr="")


def graphics_drivers(xbps) -> list:
    """Installed graphics-driver packages, newest-facing names only.

    ``mesa`` is userspace OpenGL/Vulkan; ``nvidia*`` builds a kernel module.
    What the user must do after an update differs between the two, which is the
    only reason this distinction exists.
    """
    try:
        return sorted(b for b in xbps.installed()
                      if re.fullmatch(r"nvidia\d*(-dkms)?|mesa", b))
    except (XbpsError, OSError):
        return []


def driver_versions(xbps, names) -> dict:
    """{pkg: installed version} for the given packages; missing ones omitted."""
    out = {}
    for n in names:
        try:
            out[n] = split_pkgver(xbps.inst_pkgver(n))[1]
        except (XbpsError, KeyError, ValueError, OSError):
            pass
    return out


def report_driver_change(before: dict, after: dict, out) -> None:
    """Say what a graphics-driver update means NOW that it has happened.

    Placed here on purpose. This advice used to ride along in the pending
    summary, where it read as an instruction for the present — "restart apps to
    use it" next to "20 packages to update" invites the fair question *use
    what, the driver I have not installed yet?* (owner, and he was right). It is
    only actionable once the new driver is on disk, so it is only said then.
    """
    changed = [(n, before[n], after[n]) for n in sorted(after)
               if n in before and before[n] != after[n]]
    if not changed:
        return
    for name, old, new in changed:
        if re.fullmatch(r"nvidia\d*(-dkms)?", name):
            out(f"graphics driver updated: {name} {old} -> {new}")
            out("  its kernel module was rebuilt during the install, but the "
                "running kernel still has the old one loaded. REBOOT before "
                "expecting the new driver.")
        else:
            out(f"graphics driver updated: {name} {old} -> {new}")
            out("  this is the OpenGL/Vulkan stack your games render through. "
                "Anything already open — a game, Steam, the browser — is still "
                "using the old copy it loaded at launch; close and reopen those "
                "to pick it up. Nothing else is needed, and no reboot.")


def _deploy(config: Config, deploy_bins, xbps, out, run) -> int:
    repo_args = [f"--repository={r}" for r in config.repos]
    globs = [str(r / "*.xbps") for r in config.repos]
    if run(["xbps-rindex", "-a", *globs]).returncode != 0:
        out("error: xbps-rindex failed")
        return EXIT_INDEX
    # Snapshot the graphics drivers so the post-update advice can be about what
    # ACTUALLY changed rather than about what was merely pending.
    drivers = graphics_drivers(xbps)
    drv_before = driver_versions(xbps, drivers)
    out("downloading & installing (xbps output follows) …")
    if _stream_run(["sudo", "xbps-install", "-Suy", *repo_args],
                   out, run).returncode != 0:
        out("error: xbps-install -Su failed (see §5; possible shlib rejection)")
        return EXIT_INSTALL
    # §4.6 same-version takeover for binpkgs still on a non-overlay origin
    repo_paths = set(config.repo_strs)
    for b in deploy_bins:
        if xbps.origin(b) not in repo_paths:
            if run(["sudo", "xbps-install", "-fy", *repo_args, b]).returncode != 0:
                out(f"error: forced reinstall of {b} failed")
                return EXIT_INSTALL
    out(f"deployed {len(deploy_bins)} package(s).")
    report_driver_change(drv_before, driver_versions(xbps, drivers), out)
    return _post_verify(deploy_bins, xbps, repo_paths, out)


def _post_verify(deploy_bins, xbps, repo_paths, out) -> int:
    """§7.7 post-deploy convergence gate; EXIT_VERIFY (52) on any mismatch.

    Confirms the userspace deploy actually converged: every deployed binpkg is
    now overlay-sourced and installed at the overlay pkgver, and each deploy
    target resolves to exactly one installed version (no split/partial
    transaction). ``linux-cachy`` is excluded — it is introduced and verified by
    the §8.6 staging path and may legitimately not be single-version here.
    Version strings are normalized subpackage-safe before ``vercmp`` (§7.2).
    """
    vbins = [b for b in deploy_bins if xbps.srcpkg_of(b) != KERNEL_TARGET]
    for b in vbins:
        origin = xbps.origin(b)
        if origin not in repo_paths:
            out(f"error: post-verify: {b} still originates from {origin} — "
                "takeover did not converge (exit 52, §7.7).")
            return EXIT_VERIFY
        rv = xbps.repo_ver(b)
        if rv is None or xbps.vercmp(split_pkgver(xbps.inst_pkgver(b))[1],
                                     split_pkgver(rv)[1]) != 0:
            out(f"error: post-verify: {b} installed pkgver != overlay pkgver "
                "(exit 52, §7.7).")
            return EXIT_VERIFY
    targets: set[str] = set()
    for b in vbins:
        s = xbps.srcpkg_of(b)
        if s is not None:
            targets.add(s)
    versions: dict[str, set[str]] = {t: set() for t in targets}
    for b in xbps.installed():
        s = xbps.srcpkg_of(b)
        if s in versions:
            versions[s].add(split_pkgver(xbps.inst_pkgver(b))[1])
    for t in sorted(targets):
        n = len(versions[t])
        if n != 1:
            out(f"error: post-verify: {t} resolves to {n} installed version(s) "
                "— partial/non-convergent deploy (exit 52, §7.7).")
            return EXIT_VERIFY
    if vbins:
        out("post-verify: userspace deploy converged (§7.7).")
    return EXIT_OK


# ==========================================================================
# §4.7 Stage 4c — service lifecycle
# ==========================================================================
_PID_IN_STATUS = re.compile(r"\(pid (\d+)\)")


def _parse_xcheckrestart(text: str) -> list[tuple[int, str]]:
    """Parse `xcheckrestart` output into (pid, description) pairs.

    xtools' xcheckrestart prints one line per process still mapping a
    replaced/deleted binary or library: ``<pid> <exe> (<pkg>)``. The leading
    integer is the PID; blank lines and ``-v`` LIBS detail lines are ignored.
    """
    flagged: list[tuple[int, str]] = []
    for line in (text or "").splitlines():
        head = line.strip().split(" ", 1)[0]
        if head.isdigit():
            flagged.append((int(head), line.strip()))
    return flagged


def _service_pids(service_root: Path, run) -> dict[int, str]:
    """Map each runit service's supervised PID -> service name (§4.7 step 2).

    The spec maps via ``/var/service/*/supervise/pid``, but that directory is
    0700 root; ``sudo sv status`` reads the same ``supervise/status`` and stays
    inside the §4.1 sudo boundary (no ``cat`` grant needed). Service *names*
    come from the world-readable service dir itself.
    """
    pid_to_svc: dict[int, str] = {}
    try:
        names = sorted(p.name for p in service_root.iterdir())
    except OSError:
        return pid_to_svc
    for svc in names:
        st = run(["sudo", "sv", "status", svc])
        if st.returncode != 0:
            continue
        m = _PID_IN_STATUS.search(st.stdout or "")
        if m:
            pid_to_svc[int(m.group(1))] = svc
    return pid_to_svc


def _cycle_services(config: Config, out, run,
                    service_root: Path = Path("/var/service")) -> int:
    """§4.7 Stage 4c — restart runit services running replaced binaries/libs.

    Returns EXIT_OK when everything flagged was cleanly restarted (or nothing
    was flagged), EXIT_SERVICES (60) when a matched service was deliberately
    skipped (``restart_skip``) or a restart could not be confirmed running.
    Matched-but-skipped services and unmatched PIDs (user session, games,
    compositor) are *reported* — never killed (§4.7 step 4). The kernel reboot
    notice (step 5) is owned by the §8.6 staging path, not here.
    """
    probe = run(["sudo", "xcheckrestart"])
    if probe.returncode != 0:
        out("warning: xcheckrestart unavailable/failed — cannot cycle services; "
            "restart anything using replaced libraries manually (§4.7).")
        return EXIT_SERVICES
    flagged = _parse_xcheckrestart(probe.stdout)
    if not flagged:
        out("services: none running replaced binaries (§4.7).")
        return EXIT_OK

    pid_to_svc = _service_pids(service_root, run)
    skip = set(config.restart_skip)
    matched: dict[str, int] = {}
    unmatched: list[str] = []
    for pid, desc in flagged:
        svc = pid_to_svc.get(pid)
        if svc:
            matched.setdefault(svc, pid)
        else:
            unmatched.append(desc)

    restarted: list[str] = []
    skipped: list[str] = []
    incomplete: list[str] = []
    for svc in sorted(matched):
        if svc in skip:
            skipped.append(svc)
            continue
        r = run(["sudo", "sv", "restart", svc])
        ok = r.returncode == 0
        if ok:
            st = run(["sudo", "sv", "status", svc])
            ok = st.returncode == 0 and (st.stdout or "").lstrip().startswith("run:")
        (restarted if ok else incomplete).append(svc)

    if restarted:
        out(f"services restarted (§4.7): {', '.join(restarted)}")
    if skipped:
        out("services NOT auto-restarted (in restart_skip — session-fatal; "
            f"relogin/reboot to apply): {', '.join(skipped)}")
    if incomplete:
        out("warning: services did not confirm 'run' after restart: "
            f"{', '.join(incomplete)}")
    if unmatched:
        out(f"note: {len(unmatched)} non-service process(es) still map replaced "
            "files (games/compositor/session) — relogin to clear:")
        for desc in unmatched:
            out(f"  {desc}")

    return EXIT_OK if not (skipped or incomplete) else EXIT_SERVICES


# ==========================================================================
# Flatpak — the "update everything" promise (independent of XBPS)
# ==========================================================================
def _flatpak_present(run) -> bool:
    try:
        return run(["flatpak", "--version"]).returncode == 0
    except OSError:
        return False


def _update_flatpak(config, out, run) -> int:
    """Update Flatpak apps as part of a userspace Update.

    An updater that silently ignored Flatpaks would give a false sense of "fully
    updated" — worse than any scope concern — so this runs on every ``--commit``.
    Best-effort by PRESENCE only: no Flatpak installed → silent no-op; a Flatpak
    that IS present but fails to update is surfaced LOUDLY (EXIT_FLATPAK), never
    swallowed. Per-user installs need no privilege; system installs go through the
    §4.1 sudo boundary (``flatpak update --system``) and are only touched when
    system apps actually exist (no spurious sudo/polkit hit otherwise).
    """
    if not _flatpak_present(run):
        return EXIT_OK
    out("\nflatpak: updating apps")
    ok = True

    # per-user installs — no privilege needed
    try:
        if run(["flatpak", "update", "--user", "-y"]).returncode != 0:
            out("warning: flatpak --user update failed"); ok = False
    except OSError as exc:
        out(f"warning: flatpak --user update error: {exc}"); ok = False

    # system installs — only if any exist, and through the sudo boundary
    has_system = False
    try:
        cp = run(["flatpak", "list", "--system", "--columns=application"])
        has_system = cp.returncode == 0 and bool(
            [l for l in (cp.stdout or "").splitlines() if l.strip()])
    except OSError:
        pass
    if has_system:
        try:
            if _sudo(run)(["flatpak", "update", "--system", "-y"]).returncode != 0:
                out("warning: flatpak --system update failed — check the sudo grant "
                    "or run `sudo flatpak update` manually"); ok = False
        except OSError as exc:
            out(f"warning: flatpak --system update error: {exc}"); ok = False

    out("flatpak: up to date." if ok
        else "flatpak: some updates did NOT apply (see above).")
    return EXIT_OK if ok else EXIT_FLATPAK


def _emit_tail(path: str, out, lines: int = 60) -> None:
    try:
        with open(path, encoding="utf-8") as fh:
            tail = fh.read().splitlines()[-lines:]
    except OSError:
        return
    out("--- build log tail ---")
    for line in tail:
        out(line)


# ==========================================================================
# Entry point
# ==========================================================================
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cachy-void-update",
                                description="Cachy-Void system updater (§4/§7/§8).")
    action = p.add_mutually_exclusive_group(required=True)
    action.add_argument("--sync", action="store_true", help="Stage 1: rebase onto upstream")
    action.add_argument("--check", action="store_true", help="Stage 2: print the queue (read-only)")
    action.add_argument("--status", action="store_true", help="read-only overview of all update tiers")
    action.add_argument("--kernel-ack", dest="kernel_ack", action="store_true",
                       help="§8.8: acknowledge a frozen kernel state "
                            "(CANDIDATE_UNHEALTHY etc.) and resume kernel updates")
    action.add_argument("--snapshots", action="store_true",
                       help="list pre-deploy snapshots and how to restore one "
                            "on this host (read-only)")
    action.add_argument("--build-space", dest="build_space", nargs="?",
                       const="", metavar="PATH",
                       help="show where kernel builds happen (§7.5), or "
                            "validate and set a new location on a roomier disk")
    action.add_argument("--pending", action="store_true",
                       help="fast machine-readable probe (JSON): what is waiting, "
                            "for pollers and front-ends")
    action.add_argument("--commit", action="store_true", help="Stages 3-4: build, deploy, stage kernel")
    action.add_argument("--rollback", action="store_true", help="re-pin the known-good kernel")
    action.add_argument("--clean", action="store_true",
                        help="reclaim disk: remove orphans + clean the package cache")
    action.add_argument("--gpu", action="store_true",
                        help="read-only GPU/driver advisory (card, driver, DKMS)")
    action.add_argument("--pin-bore", dest="pin_bore", action="store_true",
                        help="assisted §8.3 pin: fetch+hash the BORE patch for the "
                             "tracked series, show it, and write bore.lock on your "
                             "explicit approval (--dry-run previews)")
    action.add_argument("--health-daemon", dest="health_daemon", action="store_true",
                        help="§8.7: run the post-boot health watchdog loop")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"config path (default {DEFAULT_CONFIG})")
    p.add_argument("--dry-run", action="store_true", help="plan only; make no changes")
    p.add_argument("--yes", action="store_true", help="assume yes; run unattended")
    p.add_argument("--no-kernel", dest="no_kernel", action="store_true",
                   help="userspace only: skip all kernel synthesis/build/staging this run")
    return p


def _park(out, reason: str) -> int:
    """Idle forever without exiting (runit-safe). A clean exit under runit means
    respawn-spin; parking keeps a service inert until `sv down` stops it. Factored
    out so both the §8.7 degraded path and the missing-config path are testable."""
    out(reason)
    while True:
        time.sleep(3600)


def main(argv: Optional[Sequence[str]] = None, *,
         xbps=None, config: Optional[Config] = None, out=print) -> int:
    # When stdout is a pipe (the GUI's QProcess), Python block-buffers it — a
    # long -Suy would leave even the already-printed milestone lines invisible
    # until exit, which reads as "crashed". Line-buffer so every out() shows
    # the moment it happens.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, OSError):
        pass
    args = build_parser().parse_args(argv)

    if config is None:
        try:
            config = load_config(args.config)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            out(f"error: cannot load config {args.config}: {exc}")
            # The health daemon runs under runit — a bare EXIT_USAGE here would
            # crash-loop it (e.g. before updater.toml exists). Park inert instead;
            # every other action is one-shot, so they still surface the error.
            if getattr(args, "health_daemon", False):
                return _park(out, "health-daemon: no usable config at "
                             f"{args.config} — parking (create it / sv down to stop).")
            return EXIT_USAGE

    # --no-kernel: scope this run to userspace only. Disabling kernel_enable up
    # front gates synthesis, the G2 gate, build and staging uniformly (the GUI's
    # "Update" button uses this; "Update kernel" runs a full --commit).
    if getattr(args, "no_kernel", False):
        config.kernel_enable = False

    try:
        if args.pin_bore:
            return cmd_pin_bore(config, out=out, assume_yes=args.yes,
                                dry_run=args.dry_run)
        if args.rollback:
            return cmd_rollback(config, out=out)
        if args.clean:
            rc = cmd_clean(config, assume_yes=args.yes,
                           dry_run=args.dry_run, out=out)
            if not args.dry_run:
                poke_tray()      # orphans/cache/debug packages just changed
            return rc
        if args.health_daemon:
            daemon = build_health_daemon(config, out=out)
            # §8.7 confirm layer FIRST (once per boot): decide the fate of any
            # staged candidate — promote / unhealthy / rolled-back — before the
            # continuous watchdog starts. This was previously only reachable via
            # a --once flag nothing invoked (real-hardware finding: a healthy
            # candidate boot was never promoted).
            decision = daemon.confirm_boot()
            if decision != _health_mod.NOOP:
                out(f"health-daemon: confirm decision = {decision}")
            outcome = daemon.run_loop()
            if outcome == DEGRADED:
                # Under runit an immediate clean exit means respawn-spin (§8.7
                # inert-safe): park quietly instead; sv down still terminates us.
                return _park(out, "health-daemon: degraded environment — parking "
                             "with no supervisor changes (sv down to stop).")
            return EXIT_OK if outcome == HEALTHY else EXIT_KERNEL
        if args.kernel_ack:
            rc = cmd_kernel_ack(config, out=out, assume_yes=args.yes)
            poke_tray()          # the freeze it just cleared is on the badge
            return rc
        if args.snapshots:
            # No solver either: the inventory is a btrfs list plus a journal read.
            return cmd_snapshots(config, out=out)
        if args.build_space is not None:
            # No solver: this is a directory question, not a package one.
            return cmd_build_space(config, args.build_space or None, out=out,
                                   config_path=args.config)
        if args.pending:
            # Deliberately ahead of build_xbps(): the probe must not pay for the
            # solver, which is what makes it cheap enough to poll.
            return cmd_pending(config, out=out)
        if xbps is None:
            xbps = build_xbps(config)
        if args.check:
            return cmd_check(xbps, config, out=out)
        if args.status:
            return cmd_status(xbps, config, out=out)
        if args.gpu:
            return cmd_gpu(xbps, config, out=out)
        if args.sync:
            return cmd_sync(config, out=out)
        if args.commit:
            rc = cmd_commit(xbps, config, assume_yes=args.yes,
                            dry_run=args.dry_run, out=out)
            if not args.dry_run:
                # Whatever the outcome, the tray's picture of the world moved:
                # packages deployed, a kernel staged, or a freeze recorded.
                poke_tray()
            return rc
        return EXIT_USAGE  # unreachable (group is required)
    except Exception as exc:  # last-resort boundary (§4.8: no tracebacks)
        out(f"fatal: unhandled {type(exc).__name__}: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
