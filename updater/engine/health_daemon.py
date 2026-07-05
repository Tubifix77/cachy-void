"""Post-boot health daemon — architecture.md §8.7.

Runtime driver around the H1-H5 battery (:mod:`engine.health`). Two layers:

  * **confirm** (one-shot, normative §8.7): on a candidate boot, decide
    PROMOTE / CANDIDATE_UNHEALTHY / ROLLED_BACK. Rollback in the trial boot is
    *passive* — the GRUB default is already the known-good kernel (§8.6).
  * **watchdog** (continuous, operational): after promotion the default has
    *become* the candidate, so a degrading kernel has no armed one-shot to fall
    back on. The loop samples the battery on short intervals and, after
    ``trip_after`` consecutive failures, fires an *active* rollback.

Dual-mode degradation: in a virtualized/WSL or bootloader-less workspace the
daemon logs metrics and returns without any supervisor/GRUB mutation.

All time, subprocess, and environment access is injected so the loop and the
watchdog trip are fully unit-testable with no real sleeping or hardware.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional, Sequence

# Absolute imports (with a sys.path bootstrap) so this module works when imported
# as a package member (via the CLI), via ``python3 -m engine.health_daemon``, and
# when run directly as ``python3 engine/health_daemon.py``.
try:
    from engine import grub
    from engine import health as _health
    from engine.grub import KernelStateStore
except ImportError:  # direct-file execution: put updater/ on the path
    import os as _os
    import sys as _sys
    _sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    from engine import grub
    from engine import health as _health
    from engine.grub import KernelStateStore

# run_loop outcomes.
DEGRADED = "DEGRADED"
HEALTHY = "HEALTHY"      # loop ended (max_ticks) with no trip
TRIPPED = "TRIPPED"      # watchdog fired the rollback


@dataclass
class DaemonConfig:
    trip_after: int = 3
    interval_s: float = 30.0
    promote_after_s: float = 180.0
    settle_s: float = 120.0
    retry_s: float = 5.0
    require_network: bool = True


class HealthDaemon:
    """Drives the §8.7 confirm + watchdog logic over an injectable environment."""

    def __init__(self, *, checker: _health.HealthChecker,
                 state_store: KernelStateStore,
                 rollback: Callable[[], int],
                 config: Optional[DaemonConfig] = None,
                 services: Optional[Sequence[str]] = None,
                 out: Callable[[str], None] = print,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic,
                 uname: Optional[Callable[[], Optional[str]]] = None,
                 boot_id: Optional[Callable[[], Optional[str]]] = None,
                 degraded: Optional[Callable[[], bool]] = None):
        self.checker = checker
        self.state_store = state_store
        self.rollback = rollback
        self.cfg = config or DaemonConfig()
        self._services_override = list(services) if services is not None else None
        self.out = out
        self.sleep = sleep
        self.clock = clock
        self._uname = uname or _default_uname
        self._boot_id = boot_id or _default_boot_id
        self._degraded = degraded or _default_degraded

    # -- helpers ---------------------------------------------------------
    def _services(self) -> list[str]:
        if self._services_override is not None:
            return self._services_override
        try:
            state = self.state_store.load()
        except OSError:
            return []
        return list(state.get("services_up_at_staging") or [])

    def _battery(self) -> _health.HealthReport:
        return self.checker.battery(self._services(), self.cfg.require_network)

    def _record(self, report: _health.HealthReport, consecutive: int) -> None:
        """Continuously update the state store's telemetry (best-effort)."""
        try:
            state = self.state_store.load()
            state["health"] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "ok": report.ok(),
                "checks": report.checks,
                "consecutive_failures": consecutive,
            }
            self.state_store.save(state)
        except OSError as exc:
            self.out(f"warning: could not persist health telemetry: {exc}")

    def _set_state(self, name: str) -> None:
        try:
            state = self.state_store.load()
            state["state"] = name
            self.state_store.save(state)
        except OSError:
            pass

    # -- watchdog layer --------------------------------------------------
    def run_loop(self, max_ticks: Optional[int] = None) -> str:
        """Continuous telemetry loop with the N-consecutive-failure watchdog.

        ``max_ticks`` bounds the loop for testing; None runs until a trip.
        """
        if self._degraded():
            report = self._battery()
            self.out("health-daemon: virtualized/bootloader-less workspace — "
                     "logging only, no supervisor or GRUB changes (§8.7).")
            self.out(f"health metrics: ok={report.ok()} {report.checks}")
            return DEGRADED

        consecutive = 0
        ticks = 0
        while max_ticks is None or ticks < max_ticks:
            report = self._battery()
            if report.ok():
                consecutive = 0
            else:
                consecutive += 1
                self.out(f"health FAIL {consecutive}/{self.cfg.trip_after}: "
                         f"{report.failures()}")
            self._record(report, consecutive)
            if consecutive >= self.cfg.trip_after:
                self._trip(consecutive)
                return TRIPPED
            ticks += 1
            self.sleep(self.cfg.interval_s)
        return HEALTHY

    def _trip(self, consecutive: int) -> int:
        self.out(f"watchdog: {consecutive} consecutive health failures — "
                 "firing active rollback to the known-good kernel (§8.7).")
        self._set_state(_health.UNHEALTHY)
        rc = self.rollback()
        self.out(f"rollback returned {rc}.")
        return rc

    # -- confirm layer (one-shot, normative §8.7) -----------------------
    def confirm_boot(self, *, on_promote: Callable[[], None] = lambda: None) -> str:
        """Decide the fate of a staged candidate on this boot.

        Returns a decision from :mod:`engine.health`. Physical promotion (GRUB)
        is delegated to ``on_promote`` so this method stays testable and free of
        bootloader I/O.
        """
        try:
            state = self.state_store.load()
        except OSError:
            return _health.NOOP
        name = state.get("state")
        if name not in ("STAGED", "CONFIRMING"):
            return _health.NOOP

        cand = (state.get("candidate") or {}).get("kver")
        uname = self._uname()
        battery_ok = False
        if cand and uname == cand:
            self._set_state("CONFIRMING")
            self._wait_promote_delay()
            battery_ok = self._battery_until_settle()

        decision = _health.evaluate(
            state="CONFIRMING" if (cand and uname == cand) else name,
            candidate_kver=cand, uname_r=uname or "",
            staged_boot_id=state.get("staged_boot_id"),
            current_boot_id=self._boot_id(), battery_ok=battery_ok)

        if decision == _health.PROMOTE:
            on_promote()
            self._promote_state(state, cand)
        elif decision == _health.UNHEALTHY:
            self._set_state(_health.UNHEALTHY)   # passive: GRUB default untouched
        elif decision == _health.ROLLED_BACK:
            self._set_state(_health.ROLLED_BACK)
        return decision

    def _wait_promote_delay(self) -> None:
        deadline = self.clock() + self.cfg.promote_after_s
        while self.clock() < deadline:
            self.sleep(min(self.cfg.retry_s, self.cfg.promote_after_s))

    def _battery_until_settle(self) -> bool:
        """Run the battery, retrying failures until settle_s elapses (§8.7)."""
        deadline = self.clock() + self.cfg.settle_s
        while True:
            if self._battery().ok():
                return True
            if self.clock() >= deadline:
                return False
            self.sleep(self.cfg.retry_s)

    def _promote_state(self, state: dict, cand: Optional[str]) -> None:
        try:
            cand_obj = state.get("candidate") or {}
            state["state"] = "TRACKING"
            state["known_good"] = {"kver": cand,
                                   "grub_ref": (state.get("grub") or {}).get("candidate_ref")}
            if cand_obj.get("pkgver"):
                state["ported_version"] = cand_obj["pkgver"]
            state["candidate"] = None
            self.state_store.save(state)
        except OSError:
            pass


# ==========================================================================
# Default environment probes
# ==========================================================================
def _default_uname() -> Optional[str]:
    import subprocess
    try:
        cp = subprocess.run(["uname", "-r"], capture_output=True, text=True)
        return cp.stdout.strip() or None
    except OSError:
        return None


def _default_boot_id() -> Optional[str]:
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _default_degraded() -> bool:
    if grub.is_wsl():
        return True
    try:
        return grub.detect_boot_layout().mode == grub.MODE_SKIP
    except Exception:
        return True   # if we cannot even determine the layout, stay inert-safe


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Thin entry point used by the runit `run` script.

    Lazily imports the CLI to build config + rollback wiring, keeping this
    engine module free of a static dependency on the CLI layer.
    """
    import argparse
    import os
    import sys
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import cachy_void_update as cli

    p = argparse.ArgumentParser(prog="cachy-health",
                                description="Cachy-Void post-boot health daemon (§8.7).")
    p.add_argument("--config", default=cli.DEFAULT_CONFIG)
    p.add_argument("--once", action="store_true",
                   help="run the confirm layer once and exit (boot-time use)")
    args = p.parse_args(argv)
    try:
        config = cli.load_config(args.config)
    except Exception as exc:  # noqa: BLE001 - never traceback out of a service
        print(f"health-daemon: cannot load config: {exc}")
        return cli.EXIT_USAGE

    daemon = cli.build_health_daemon(config)
    if args.once:
        decision = daemon.confirm_boot()
        print(f"health-daemon: confirm decision = {decision}")
        return 0
    outcome = daemon.run_loop()
    return 0 if outcome in (DEGRADED, HEALTHY) else EXIT_TRIPPED


EXIT_TRIPPED = 70


if __name__ == "__main__":
    import sys
    sys.exit(main())
