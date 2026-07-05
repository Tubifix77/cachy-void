"""Unit tests for the post-boot health daemon (architecture.md §8.7).

The watchdog loop, the three-strike active rollback, the confirm-layer decision
tree, and the WSL/degraded flight path are all exercised with mocked time,
battery results, state store, and rollback — no sleeping, no hardware, no init.
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine import health as _health
from engine.grub import KernelStateStore, default_state
from engine.health_daemon import HealthDaemon, DaemonConfig, DEGRADED, HEALTHY, TRIPPED


def report(ok: bool) -> _health.HealthReport:
    return _health.HealthReport({"H1": ok, "H2": True})


class FakeChecker:
    """Returns a scripted sequence of battery reports (last one repeats)."""

    def __init__(self, reports):
        self._reports = list(reports)
        self.calls = 0

    def battery(self, services, require_network):
        i = min(self.calls, len(self._reports) - 1)
        self.calls += 1
        return self._reports[i]


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def _store(tmp, **overrides):
    p = Path(tmp) / "kernel-state.json"
    st = default_state(base_series="6.12", ported_version="6.12.34_1")
    st.update(overrides)
    store = KernelStateStore(p)
    store.save(st)
    return store


def _daemon(tmp, checker, *, rollback=None, degraded=False,
            cfg=None, uname="6.12.35_1", boot_id="B", **store_overrides):
    clock = FakeClock()
    return HealthDaemon(
        checker=checker,
        state_store=_store(tmp, **store_overrides),
        rollback=rollback or mock.Mock(return_value=0),
        config=cfg or DaemonConfig(trip_after=3, interval_s=0, promote_after_s=0,
                                   settle_s=3, retry_s=1),
        services=["dbus"],
        out=lambda *a: None,
        sleep=clock.advance,
        clock=clock.now,
        uname=lambda: uname,
        boot_id=lambda: boot_id,
        degraded=lambda: degraded,
    )


class WatchdogTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_three_consecutive_failures_trip_rollback(self):
        rb = mock.Mock(return_value=0)
        d = _daemon(self.tmp, FakeChecker([report(False)]), rollback=rb)
        outcome = d.run_loop(max_ticks=10)
        self.assertEqual(outcome, TRIPPED)
        rb.assert_called_once_with()                     # fired exactly once
        state = d.state_store.load()
        self.assertEqual(state["state"], _health.UNHEALTHY)
        self.assertEqual(state["health"]["consecutive_failures"], 3)

    def test_trips_only_after_consecutive_not_cumulative(self):
        rb = mock.Mock(return_value=0)
        # fail, fail, PASS (reset), fail, fail, fail -> trips on the 6th tick
        seq = [report(False), report(False), report(True),
               report(False), report(False), report(False)]
        d = _daemon(self.tmp, FakeChecker(seq), rollback=rb)
        self.assertEqual(d.run_loop(max_ticks=10), TRIPPED)
        rb.assert_called_once()
        self.assertEqual(d.checker.calls, 6)

    def test_all_healthy_never_trips(self):
        rb = mock.Mock(return_value=0)
        d = _daemon(self.tmp, FakeChecker([report(True)]), rollback=rb)
        self.assertEqual(d.run_loop(max_ticks=5), HEALTHY)
        rb.assert_not_called()
        self.assertEqual(d.state_store.load()["health"]["consecutive_failures"], 0)

    def test_telemetry_written_each_tick(self):
        d = _daemon(self.tmp, FakeChecker([report(True)]))
        d.run_loop(max_ticks=2)
        health = d.state_store.load()["health"]
        self.assertTrue(health["ok"])
        self.assertIn("ts", health)
        self.assertIn("H1", health["checks"])


class DegradedModeTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_wsl_degraded_logs_and_exits_clean(self):
        rb = mock.Mock()
        logs = []
        d = _daemon(self.tmp, FakeChecker([report(False)]), rollback=rb,
                    degraded=True)
        d.out = lambda *a: logs.append(" ".join(str(x) for x in a))
        outcome = d.run_loop()          # no max_ticks: must still return promptly
        self.assertEqual(outcome, DEGRADED)
        rb.assert_not_called()          # zero destructive actions
        self.assertTrue(any("no supervisor" in m for m in logs))

    def test_degraded_path_raises_nothing(self):
        # Even with a checker that would fail, degraded mode must be exception-free.
        d = _daemon(self.tmp, FakeChecker([report(False)]), degraded=True)
        try:
            d.run_loop()
        except Exception as exc:  # noqa: BLE001
            self.fail(f"degraded path raised {exc!r}")


class ConfirmLayerTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def test_noop_when_nothing_staged(self):
        d = _daemon(self.tmp, FakeChecker([report(True)]), state="TRACKING")
        self.assertEqual(d.confirm_boot(), _health.NOOP)

    def test_promote_on_healthy_candidate_boot(self):
        promoted = []
        d = _daemon(self.tmp, FakeChecker([report(True)]),
                    uname="6.12.35_1",
                    state="STAGED",
                    candidate={"pkgver": "linux-cachy-6.12.35_1",
                               "kver": "6.12.35_1"},
                    staged_boot_id="B",
                    grub={"candidate_ref": "ref-35"})
        decision = d.confirm_boot(on_promote=lambda: promoted.append(True))
        self.assertEqual(decision, _health.PROMOTE)
        self.assertEqual(promoted, [True])
        state = d.state_store.load()
        self.assertEqual(state["state"], "TRACKING")
        self.assertEqual(state["known_good"]["kver"], "6.12.35_1")
        self.assertEqual(state["ported_version"], "linux-cachy-6.12.35_1")

    def test_unhealthy_candidate_is_passive_rollback(self):
        promoted = []
        d = _daemon(self.tmp, FakeChecker([report(False)]),
                    uname="6.12.35_1",
                    state="STAGED",
                    candidate={"pkgver": "linux-cachy-6.12.35_1",
                               "kver": "6.12.35_1"},
                    staged_boot_id="B")
        decision = d.confirm_boot(on_promote=lambda: promoted.append(True))
        self.assertEqual(decision, _health.UNHEALTHY)
        self.assertEqual(promoted, [])                      # never promoted
        self.assertEqual(d.state_store.load()["state"], _health.UNHEALTHY)

    def test_rolled_back_when_booted_elsewhere(self):
        d = _daemon(self.tmp, FakeChecker([report(True)]),
                    uname="6.12.34_1",                      # not the candidate
                    state="STAGED",
                    candidate={"pkgver": "linux-cachy-6.12.35_1",
                               "kver": "6.12.35_1"},
                    staged_boot_id="B", boot_id="C")        # different boot
        self.assertEqual(d.confirm_boot(), _health.ROLLED_BACK)
        self.assertEqual(d.state_store.load()["state"], _health.ROLLED_BACK)


class WiringTests(unittest.TestCase):
    """The CLI factory must fire the real cmd_rollback on a trip (§8.7)."""

    def test_build_health_daemon_wires_cmd_rollback(self):
        import cachy_void_update as cli
        cfg = cli.Config(void_packages=Path("/vp"),
                         state_dir=Path(tempfile.mkdtemp()))
        with mock.patch.object(cli, "cmd_rollback", return_value=0) as m:
            daemon = cli.build_health_daemon(cfg, out=lambda *a: None)
            rc = daemon.rollback()                          # simulate a trip
        self.assertEqual(rc, 0)
        m.assert_called_once()
        self.assertIs(m.call_args.args[0], cfg)             # correct config passed


if __name__ == "__main__":
    unittest.main()
