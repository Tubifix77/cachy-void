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

Error-boundary contract (§4.8): every path returns an exit code from the table
below; a traceback reaching the user is itself a bug (last-resort boundary in
``main``). Kernel-path stalls (G2 failure, staging refusal) never block
userspace updates (§8 preamble).
"""
from __future__ import annotations

import argparse
import os
import re
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
from engine.atomicio import sweep_tmp  # noqa: E402
from engine.health import HealthChecker  # noqa: E402
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
    kwargs = {"void_packages": config.void_packages, "repos": config.repos}
    if run is not None:
        kwargs["run"] = run
    return Xbps(**kwargs)


def build_health_daemon(config: Config, out=print, run=None) -> HealthDaemon:
    """Wire the §8.7 health daemon with a cmd_rollback-backed active rollback.

    The rollback callable closes over ``config`` so the watchdog's trip fires
    the real §8.6/§8.7 rollback path; the engine module stays CLI-agnostic.
    """
    run = run or _run
    checker = HealthChecker(run=lambda args: run(args))
    store = grub.KernelStateStore(config.kernel_state_path)
    return HealthDaemon(
        checker=checker,
        state_store=store,
        rollback=lambda: cmd_rollback(config, out=out, run=run),
        config=DaemonConfig(),
        out=out,
    )


# ==========================================================================
# Process helpers
# ==========================================================================
def _run(args: Sequence[str], cwd: Optional[str] = None) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


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


def _kernel_report(config: Config, xbps, out) -> None:
    """§8.2 bump classification — informational (template regen §8.4 is a
    human step for now). Never fails the run."""
    if not config.kernel_enable:
        return
    try:
        state = grub.KernelStateStore(config.kernel_state_path).load()
        series = state.get("base_series") or ""
        if not series:
            return
        tpath = config.void_packages / "srcpkgs" / f"linux{series}" / "template"
        text = tpath.read_text(encoding="utf-8") if tpath.exists() else None
        ev, tmpl = grub.classify_bump(
            series_template_text=text,
            ported_version=state.get("ported_version", ""),
            vercmp=xbps.vercmp)
        if ev == grub.EV_BUMP_PATCHLEVEL:
            out(f"kernel: upstream linux{series} is at {tmpl}; ported base is "
                f"{state.get('ported_version') or '<none>'} — port linux-cachy "
                "(§2.6/§8.4).")
        elif ev == grub.EV_AWAIT_HUMAN_SERIES:
            out(f"kernel: tracked series linux{series} is gone upstream — "
                "human decision required (§8.2).")
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
                                      ported_version=state.get("ported_version", ""),
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
        dotconfig = grub.locate_dotconfig(config.void_packages).read_text(
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
        out(f"kernel {cand_kver} staged ({res.mode}): GRUB default pinned to "
            f"known-good {known}; reboot when convenient. NEVER auto-rebooting.")
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


def cmd_status(xbps, config: Config, out=print, run=_run) -> int:
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

    out("\n[1] System (upstream Void)")
    try:
        cp = run(["xbps-install", "-un"])          # dry-run, cached repodata
        if cp.returncode == 0:
            n = len(_lines(cp))
            out(f"    {n} upstream package(s) updatable"
                + ("" if n else " — up to date")
                + ("   (list may be stale; --sync refreshes it)" if n else ""))
        else:
            out("    unknown — run --sync to refresh the repository list")
    except OSError:
        out("    unknown — xbps-install unavailable")

    out("\n[2] Performance overlay (rebuilt at -O3 / x86-64-v3)")
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
    except (XbpsError, MappingError, CycleError) as exc:
        out(f"    query failed: {exc}")
        return EXIT_QUERY
    except OSError as exc:
        out(f"    query failed: {exc}")
        return EXIT_QUERY

    out("\n[3] Kernel (linux-cachy / BORE)")
    _kernel_report(config, xbps, out=lambda m: out("    " + m))

    out("\n[4] Maintenance / cleanup")
    try:
        cp = run(["xbps-remove", "-o", "-n"])
        out(f"    orphaned packages: {len(_lines(cp)) if cp.returncode == 0 else 'unknown (needs root)'}")
    except OSError:
        pass
    try:
        cp = run(["vkpurge", "list"])
        old = _lines(cp) if cp.returncode == 0 else []
        out("    removable old kernels: " + (", ".join(old) if old else "none"))
    except OSError:
        pass
    try:
        cp = run(["du", "-sh", "/var/cache/xbps"])
        if cp.returncode == 0 and cp.stdout.strip():
            out(f"    package cache on disk: {cp.stdout.split()[0]}")
    except OSError:
        pass

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

    out("")
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

        if run(["git", "fetch", "upstream"], vp).returncode != 0:
            out("error: git fetch upstream failed")
            return EXIT_SYNC

        rebase = run(["git", "pull", "--rebase", "upstream", "master"], vp)
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


def cmd_commit(xbps, config: Config, *, assume_yes: bool, dry_run: bool,
               out=print, run=_run, confirm=input, stage_layout=None,
               service_root: Path = Path("/var/service")) -> int:
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
            out("queue empty — nothing to do.")
            return EXIT_OK
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

    out(f"build order  [{order.provenance}]: {' -> '.join(order.order) or '-'}")
    out(f"deploy queue ({len(plan.q_deploy)}): {', '.join(plan.q_deploy) or '-'}")
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
    build_list = [*order.order, *order.second_pass]
    q_deploy = list(plan.q_deploy)
    if config.kernel_enable and KERNEL_TARGET in build_list:
        if not _g2_gate(config, xbps, out):
            build_list = [p for p in build_list if p != KERNEL_TARGET]
            q_deploy = [t for t in q_deploy if t != KERNEL_TARGET]
            _record_kernel_state(config, {"state": "AWAIT_HUMAN_TEMPLATE"}, out)
            out(f"warning: {KERNEL_TARGET} withheld from this run "
                "(AWAIT_HUMAN_TEMPLATE, §8.5); userspace updates continue.")
            if not build_list and not q_deploy:
                out("queue empty after kernel withhold.")
                return EXIT_OK

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
    journal.set_order(build_list, order.provenance)

    # Stage 3 — build (§7.5)
    for pkg in build_list:
        journal.set_pkg_status(pkg, "building")
        log_path = str(rundir / f"build-{pkg}.log")
        try:
            xbps.clean(pkg)
            rc = xbps.build(pkg, config.effective_jobs, log_path)
        except (XbpsError, OSError) as exc:
            journal.set_pkg_status(pkg, "failed", log=log_path)
            journal.fail(pkg, EXIT_BUILD)
            out(f"error: build environment failure for {pkg}: {exc}")
            return EXIT_BUILD
        if rc != 0:
            journal.set_pkg_status(pkg, "failed", log=log_path)
            journal.fail(pkg, EXIT_BUILD)
            out(f"error: build failed for {pkg} (rc={rc}); see {log_path}")
            _emit_tail(log_path, out)
            return EXIT_BUILD
        journal.set_pkg_status(pkg, "built", log=log_path)

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
    # (60) > clean. Userspace is already deployed in every branch.
    if rc_kernel != EXIT_OK:
        out("commit complete — userspace deployed, kernel staging FAILED (see above).")
        return rc_kernel
    if rc_services != EXIT_OK:
        out("commit complete — deployed & staged; some services need a manual "
            "restart or relogin (§4.7).")
        return rc_services
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
# Maintenance / cleanup (§4.7 note; extends the §4.1 sudo boundary)
# ==========================================================================
def cmd_clean(config: Config, *, assume_yes: bool, out=print, run=_run,
              confirm=input) -> int:
    """Reclaim disk: remove orphaned packages and clean the obsolete package
    cache. Preview-then-confirm; every removal goes through the §4.1 sudo
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

    # old kernels — SUGGEST ONLY (never purge; §2.5/§4.7)
    try:
        old = _lines(run(["vkpurge", "list"]))
    except OSError:
        old = []
    if old:
        out("\nold kernels present (NOT removed — kernel purges are manual, "
            "§2.5/§4.7):")
        for k in old:
            out(f"    {k}")
        out("    keep the last known-good kernel; when ready: sudo vkpurge rm <ver>")

    if not orphans and not cache:
        out("\nnothing to clean — no orphans, cache already tidy.")
        return EXIT_OK

    if not assume_yes:
        if confirm("\nremove orphans and clean the cache? [y/N] ").strip().lower() \
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
                    out(f"    driver package: {d} {xbps.inst_pkgver(d)}")
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
        if ds:
            out(f"\nDKMS modules ({len(ds)}):")
            for l in ds:
                out("    " + l)
            if any("installed" not in l.lower() for l in ds):
                out("    warning: a DKMS module is NOT 'installed' — it may be "
                    "missing for the running kernel (rebuild: sudo "
                    "xbps-reconfigure -f <driver>-dkms).")
        else:
            out("\nDKMS: no out-of-tree modules (driver is in-tree or absent).")
    except OSError:
        pass

    out("")
    return EXIT_OK


# ==========================================================================
# Deploy helper
# ==========================================================================
def _deploy(config: Config, deploy_bins, xbps, out, run) -> int:
    repo_args = [f"--repository={r}" for r in config.repos]
    globs = [str(r / "*.xbps") for r in config.repos]
    if run(["xbps-rindex", "-a", *globs]).returncode != 0:
        out("error: xbps-rindex failed")
        return EXIT_INDEX
    if run(["sudo", "xbps-install", "-Suy", *repo_args]).returncode != 0:
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
    action.add_argument("--commit", action="store_true", help="Stages 3-4: build, deploy, stage kernel")
    action.add_argument("--rollback", action="store_true", help="re-pin the known-good kernel")
    action.add_argument("--clean", action="store_true",
                        help="reclaim disk: remove orphans + clean the package cache")
    action.add_argument("--gpu", action="store_true",
                        help="read-only GPU/driver advisory (card, driver, DKMS)")
    action.add_argument("--health-daemon", dest="health_daemon", action="store_true",
                        help="§8.7: run the post-boot health watchdog loop")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"config path (default {DEFAULT_CONFIG})")
    p.add_argument("--dry-run", action="store_true", help="plan only; make no changes")
    p.add_argument("--yes", action="store_true", help="assume yes; run unattended")
    p.add_argument("--no-kernel", dest="no_kernel", action="store_true",
                   help="userspace only: skip all kernel synthesis/build/staging this run")
    return p


def main(argv: Optional[Sequence[str]] = None, *,
         xbps=None, config: Optional[Config] = None, out=print) -> int:
    args = build_parser().parse_args(argv)

    if config is None:
        try:
            config = load_config(args.config)
        except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
            out(f"error: cannot load config {args.config}: {exc}")
            return EXIT_USAGE

    # --no-kernel: scope this run to userspace only. Disabling kernel_enable up
    # front gates synthesis, the G2 gate, build and staging uniformly (the GUI's
    # "Update" button uses this; "Update kernel" runs a full --commit).
    if getattr(args, "no_kernel", False):
        config.kernel_enable = False

    try:
        if args.rollback:
            return cmd_rollback(config, out=out)
        if args.clean:
            return cmd_clean(config, assume_yes=args.yes, out=out)
        if args.health_daemon:
            outcome = build_health_daemon(config, out=out).run_loop()
            if outcome == DEGRADED:
                # Under runit an immediate clean exit means respawn-spin (§8.7
                # inert-safe): park quietly instead; sv down still terminates us.
                out("health-daemon: degraded environment — parking with no "
                    "supervisor changes (sv down to stop).")
                while True:
                    time.sleep(3600)
            return EXIT_OK if outcome == HEALTHY else EXIT_KERNEL
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
            return cmd_commit(xbps, config, assume_yes=args.yes,
                              dry_run=args.dry_run, out=out)
        return EXIT_USAGE  # unreachable (group is required)
    except Exception as exc:  # last-resort boundary (§4.8: no tracebacks)
        out(f"fatal: unhandled {type(exc).__name__}: {exc}")
        return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
