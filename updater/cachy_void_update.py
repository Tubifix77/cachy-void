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
    cfg = Config(
        void_packages=Path(vp),
        jobs=int(build.get("jobs", 0)),
        targets=list(pkgs.get("targets", [])),
        blacklist=list(pkgs.get("blacklist", [])),
        restart_skip=list(svc.get("restart_skip", [])),
        kernel_enable=bool(kern.get("enable", True)),
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

        cand_kver = split_pkgver(xbps.inst_pkgver(KERNEL_TARGET))[1]
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
               out=print, run=_run, confirm=input, stage_layout=None) -> int:
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
    out("commit complete." if rc_kernel == EXIT_OK
        else "commit complete — userspace deployed, kernel staging FAILED (see above).")
    return rc_kernel


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
    return EXIT_OK


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
    action.add_argument("--commit", action="store_true", help="Stages 3-4: build, deploy, stage kernel")
    action.add_argument("--rollback", action="store_true", help="re-pin the known-good kernel")
    action.add_argument("--health-daemon", dest="health_daemon", action="store_true",
                        help="§8.7: run the post-boot health watchdog loop")
    p.add_argument("--config", default=DEFAULT_CONFIG, help=f"config path (default {DEFAULT_CONFIG})")
    p.add_argument("--dry-run", action="store_true", help="plan only; make no changes")
    p.add_argument("--yes", action="store_true", help="assume yes; run unattended")
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

    try:
        if args.rollback:
            return cmd_rollback(config, out=out)
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
