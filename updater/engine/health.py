"""Post-boot health battery and rollback watchdog — architecture.md §8.7.

(The task brief cited §8.3, but §8.3 is the BORE patch-trust pipeline; the
health battery H1-H5 and the promote/rollback decision live in §8.7, which is
what this module implements.)

After a candidate kernel is staged (§8.6) and the machine reboots, the confirm
service runs this battery. If every check passes on a boot that is actually
running the candidate, the candidate is promoted to the GRUB default; otherwise
the default is left pointing at the known-good kernel so the next reboot rolls
back automatically. :func:`evaluate` is the watchdog trigger — pure logic over
observed facts so it can be exhaustively unit-tested.
"""
from __future__ import annotations

import glob as _glob
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence

# Decision outcomes of the watchdog (§8.7 / §8.8).
PROMOTE = "PROMOTE"                    # healthy candidate boot -> make default
UNHEALTHY = "CANDIDATE_UNHEALTHY"     # candidate booted but failed the battery
ROLLED_BACK = "ROLLED_BACK"           # rebooted into something else (panic/manual)
NOOP = "NOOP"                         # nothing staged / not our concern

_ACTIVE_STATES = ("STAGED", "CONFIRMING")


def _default_run(args: Sequence[str]) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True)


@dataclass
class HealthReport:
    """Result of the H1-H5 battery."""
    checks: dict[str, bool] = field(default_factory=dict)

    def ok(self) -> bool:
        return bool(self.checks) and all(self.checks.values())

    def failures(self) -> list[str]:
        return sorted(k for k, v in self.checks.items() if not v)


class HealthChecker:
    """Runs the §8.7 battery. All I/O is injectable for testing."""

    def __init__(self, *,
                 run: Callable[[Sequence[str]], subprocess.CompletedProcess] = _default_run,
                 globber: Callable[[str], list[str]] = _glob.glob,
                 health_d: str | Path = "/etc/cachy-void/health.d",
                 service_root: str | Path = "/var/service"):
        self.run = run
        self.globber = globber
        self.health_d = Path(health_d)
        self.service_root = Path(service_root)

    # -- individual checks ----------------------------------------------
    def h1_services_up(self, services: Sequence[str]) -> bool:
        """Every service that was up at staging is up now."""
        for svc in services:
            cp = self.run(["sv", "status", str(self.service_root / svc)])
            if cp.returncode != 0 or not cp.stdout.strip().startswith("run:"):
                return False
        return True

    def h2_dmesg_clean(self) -> bool:
        """No emerg/alert/crit kernel messages this boot."""
        cp = self.run(["dmesg", "--level=emerg,alert,crit"])
        return cp.returncode == 0 and cp.stdout.strip() == ""

    def h3_gpu_node(self) -> bool:
        """A DRM render node exists (it is a gaming box)."""
        return bool(self.globber("/dev/dri/renderD*"))

    def h4_network(self) -> bool:
        """A default route exists."""
        cp = self.run(["ip", "route", "show", "default"])
        return cp.returncode == 0 and cp.stdout.strip() != ""

    def h5_operator_scripts(self) -> bool:
        """Every /etc/cachy-void/health.d/*.sh exits 0 (absent dir == pass)."""
        if not self.health_d.is_dir():
            return True
        for script in sorted(self.globber(str(self.health_d / "*.sh"))):
            cp = self.run(["/bin/sh", script])
            if cp.returncode != 0:
                return False
        return True

    # -- battery ---------------------------------------------------------
    def battery(self, services: Sequence[str], require_network: bool = True) -> HealthReport:
        checks = {
            "H1_services": self.h1_services_up(services),
            "H2_dmesg": self.h2_dmesg_clean(),
            "H3_gpu": self.h3_gpu_node(),
            "H5_scripts": self.h5_operator_scripts(),
        }
        if require_network:
            checks["H4_network"] = self.h4_network()
        return HealthReport(checks)


def evaluate(*, state: str, candidate_kver: Optional[str], uname_r: str,
             staged_boot_id: Optional[str], current_boot_id: Optional[str],
             battery_ok: bool) -> str:
    """The rollback watchdog trigger (§8.7).

    * running the candidate + battery passed          -> PROMOTE
    * running the candidate + battery failed          -> CANDIDATE_UNHEALTHY
    * a different boot than the one we staged into     -> ROLLED_BACK
    * nothing staged / not our concern                -> NOOP
    """
    if state not in _ACTIVE_STATES:
        return NOOP
    if candidate_kver is not None and uname_r == candidate_kver:
        return PROMOTE if battery_ok else UNHEALTHY
    if staged_boot_id is not None and current_boot_id != staged_boot_id:
        return ROLLED_BACK
    return NOOP
