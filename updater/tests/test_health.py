"""Unit tests for the post-boot health battery + watchdog (architecture.md §8.7)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine.health import (
    HealthChecker, HealthReport, evaluate,
    PROMOTE, UNHEALTHY, ROLLED_BACK, NOOP,
)


def cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


class Dispatch:
    """Fake run() that dispatches on argv[0] to canned CompletedProcesses."""

    def __init__(self, table):
        self.table = table

    def __call__(self, args):
        return self.table.get(args[0], cp())


class IndividualCheckTests(unittest.TestCase):

    def test_h1_services_up(self):
        good = HealthChecker(run=Dispatch({"sv": cp(stdout="run: /var/service/dbus: (pid 1) 5s")}))
        self.assertTrue(good.h1_services_up(["dbus"]))
        down = HealthChecker(run=Dispatch({"sv": cp(stdout="down: dbus: 1s")}))
        self.assertFalse(down.h1_services_up(["dbus"]))

    def test_h2_dmesg(self):
        clean = HealthChecker(run=Dispatch({"dmesg": cp(stdout="")}))
        self.assertTrue(clean.h2_dmesg_clean())
        dirty = HealthChecker(run=Dispatch({"dmesg": cp(stdout="CPU stuck")}))
        self.assertFalse(dirty.h2_dmesg_clean())

    def test_h3_gpu_node(self):
        yes = HealthChecker(globber=lambda pat: ["/dev/dri/renderD128"])
        self.assertTrue(yes.h3_gpu_node())
        no = HealthChecker(globber=lambda pat: [])
        self.assertFalse(no.h3_gpu_node())

    def test_h4_network(self):
        up = HealthChecker(run=Dispatch({"ip": cp(stdout="default via 10.0.0.1")}))
        self.assertTrue(up.h4_network())
        down = HealthChecker(run=Dispatch({"ip": cp(stdout="")}))
        self.assertFalse(down.h4_network())

    def test_h5_absent_dir_passes(self):
        hc = HealthChecker(health_d="/nonexistent/health.d")
        self.assertTrue(hc.h5_operator_scripts())

    def test_h5_script_failure_fails(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "a.sh").write_text("#!/bin/sh\nexit 1\n")
            hc = HealthChecker(
                run=Dispatch({"/bin/sh": cp(returncode=1)}),
                globber=lambda pat: [str(Path(d) / "a.sh")],
                health_d=d)
            self.assertFalse(hc.h5_operator_scripts())


class BatteryTests(unittest.TestCase):

    def _all_good(self):
        return HealthChecker(
            run=Dispatch({
                "sv": cp(stdout="run: x: (pid 2) 9s"),
                "dmesg": cp(stdout=""),
                "ip": cp(stdout="default via 10.0.0.1"),
            }),
            globber=lambda pat: ["/dev/dri/renderD128"],
            health_d="/nonexistent")

    def test_all_pass(self):
        rep = self._all_good().battery(["dbus"], require_network=True)
        self.assertTrue(rep.ok())
        self.assertIn("H4_network", rep.checks)

    def test_network_optional(self):
        rep = self._all_good().battery(["dbus"], require_network=False)
        self.assertTrue(rep.ok())
        self.assertNotIn("H4_network", rep.checks)

    def test_report_lists_failures(self):
        rep = HealthReport({"H1": True, "H2": False, "H3": True})
        self.assertFalse(rep.ok())
        self.assertEqual(rep.failures(), ["H2"])


class WatchdogTests(unittest.TestCase):

    def test_promote_on_healthy_candidate_boot(self):
        d = evaluate(state="STAGED", candidate_kver="6.12.35_1", uname_r="6.12.35_1",
                     staged_boot_id="B", current_boot_id="B", battery_ok=True)
        self.assertEqual(d, PROMOTE)

    def test_unhealthy_when_candidate_but_battery_fails(self):
        d = evaluate(state="CONFIRMING", candidate_kver="6.12.35_1", uname_r="6.12.35_1",
                     staged_boot_id="B", current_boot_id="B", battery_ok=False)
        self.assertEqual(d, UNHEALTHY)

    def test_rolled_back_on_different_boot(self):
        d = evaluate(state="STAGED", candidate_kver="6.12.35_1", uname_r="6.12.34_1",
                     staged_boot_id="B", current_boot_id="C", battery_ok=True)
        self.assertEqual(d, ROLLED_BACK)

    def test_noop_when_nothing_staged(self):
        d = evaluate(state="TRACKING", candidate_kver=None, uname_r="6.12.34_1",
                     staged_boot_id=None, current_boot_id="B", battery_ok=True)
        self.assertEqual(d, NOOP)

    def test_noop_when_same_boot_but_not_candidate(self):
        # Staged, still on the same boot, not yet running candidate: wait.
        d = evaluate(state="STAGED", candidate_kver="6.12.35_1", uname_r="6.12.34_1",
                     staged_boot_id="B", current_boot_id="B", battery_ok=True)
        self.assertEqual(d, NOOP)


if __name__ == "__main__":
    unittest.main()
