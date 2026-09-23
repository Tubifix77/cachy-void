"""§4.9 visibility: is a run happening, is a kernel build imminent, can I skip it.

The gap these close was reported as three questions in a row: "say I am awake
when the nightly updates run, how would I be informed it is running?", "how
would I be informed it is done?", and "maybe even pop up a warning before the
kernel update so I could cancel it for this night". All three had the same
answer before this: you would not be. A scheduled run is a separate process
under runit -- no output in the window, no button greyed, no progress bar --
so the only evidence was the fans.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import cachy_void_update as cvu  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TRAY_PATH = (pathlib.Path(__file__).resolve().parents[2]
             / "system" / "bin" / "cachy-updater-tray")


class ScheduleFactsTests(unittest.TestCase):
    """The §4.9 service in parts, so nobody parses English back out of a line."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.link = self.tmp / "svc"
        self.conf = self.tmp / "conf"

    def _facts(self, conf_text=None, enabled=True, sv="run"):
        if enabled:
            self.link.mkdir(exist_ok=True)
        if conf_text is not None:
            self.conf.write_text(conf_text, encoding="utf-8")
        return cvu.schedule_facts(link=self.link, conf=self.conf,
                                  run=lambda _a: _CP(sv + ": x"))

    def test_absent_service_is_not_enabled(self):
        f = cvu.schedule_facts(link=self.tmp / "nope", conf=self.conf)
        self.assertFalse(f["enabled"])

    def test_time_and_kernel_scope_are_read(self):
        f = self._facts("SNOOZE_HOUR=1" + chr(10) + "SNOOZE_MINUTE=0" + chr(10)
                        + "SCHEDULE_KERNEL=yes" + chr(10))
        self.assertTrue(f["enabled"])
        self.assertEqual((f["hour"], f["minute"]), (1, 0))
        self.assertTrue(f["kernel"])

    def test_schedule_kernel_no_is_respected(self):
        f = self._facts("SNOOZE_HOUR=3" + chr(10) + "SCHEDULE_KERNEL=no" + chr(10))
        self.assertFalse(f["kernel"])

    def test_kernel_defaults_to_true_like_the_service_does(self):
        # The run script does `: "${SCHEDULE_KERNEL:=yes}"`. A front-end that
        # assumed "no" would under-warn about the most disruptive thing here.
        f = self._facts("SNOOZE_HOUR=1" + chr(10))
        self.assertTrue(f["kernel"])

    def test_a_snooze_pattern_is_not_a_clock_time(self):
        # snooze takes */6; rendering that as a countdown would be a lie.
        f = self._facts("SNOOZE_HOUR=*/6" + chr(10) + "SNOOZE_MINUTE=0" + chr(10))
        self.assertIsNone(f["hour"])
        self.assertIsNone(cvu.minutes_until(f["hour"], f["minute"]))

    def test_a_stopped_service_is_enabled_but_not_running(self):
        f = self._facts("SNOOZE_HOUR=1" + chr(10), sv="down")
        self.assertTrue(f["enabled"])
        self.assertFalse(f["running"])


class MinutesUntilTests(unittest.TestCase):

    def _at(self, h, m):
        return time.struct_time((2026, 9, 24, h, m, 0, 2, 267, -1))

    def test_later_today(self):
        self.assertEqual(cvu.minutes_until(1, 0, self._at(0, 50)), 10)

    def test_wraps_past_midnight(self):
        # 01:00 seen from 23:30 is 90 minutes away, not -1350.
        self.assertEqual(cvu.minutes_until(1, 0, self._at(23, 30)), 90)

    def test_exactly_now_is_zero(self):
        self.assertEqual(cvu.minutes_until(1, 0, self._at(1, 0)), 0)

    def test_no_time_no_answer(self):
        self.assertIsNone(cvu.minutes_until(None, 0, self._at(1, 0)))


class WarnWindowTests(unittest.TestCase):

    def setUp(self):
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))

    def test_default_is_ten_not_one(self):
        os.environ.pop(cvu.KERNEL_WARN_ENV, None)
        self.assertEqual(cvu.kernel_warn_minutes(), 10)

    def test_overridable(self):
        os.environ[cvu.KERNEL_WARN_ENV] = "30"
        self.assertEqual(cvu.kernel_warn_minutes(), 30)

    def test_nonsense_falls_back(self):
        for bad in ("", "abc", "0", "-5", "99999"):
            os.environ[cvu.KERNEL_WARN_ENV] = bad
            self.assertEqual(cvu.kernel_warn_minutes(), 10, bad)


class SkipKernelTonightTests(unittest.TestCase):
    """One-shot, scheduled-run-only, self-expiring."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = cvu.Config(void_packages=self.tmp / "vp",
                              log_root=self.tmp / "state" / "log")
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        os.environ.pop(cvu.ORIGIN_ENV, None)

    def _arm(self, minutes_ahead=60):
        cvu.skip_kernel_path(self.cfg).parent.mkdir(parents=True, exist_ok=True)
        cvu.skip_kernel_path(self.cfg).write_text(
            time.strftime("%Y-%m-%dT%H:%M:%S",
                          time.localtime(time.time() + minutes_ahead * 60)),
            encoding="utf-8")

    def test_no_veto_means_not_active(self):
        self.assertFalse(cvu.skip_kernel_active(self.cfg))

    def test_an_armed_veto_is_active(self):
        self._arm()
        self.assertTrue(cvu.skip_kernel_active(self.cfg))

    def test_an_expired_veto_is_ignored(self):
        # A veto that outlives its night would disable kernel builds for ever,
        # which is how an opt-out becomes a mystery six months later.
        self._arm(minutes_ahead=-60)
        self.assertFalse(cvu.skip_kernel_active(self.cfg))

    def test_a_corrupt_veto_is_ignored_not_fatal(self):
        cvu.skip_kernel_path(self.cfg).parent.mkdir(parents=True, exist_ok=True)
        cvu.skip_kernel_path(self.cfg).write_text("banana", encoding="utf-8")
        self.assertFalse(cvu.skip_kernel_active(self.cfg))

    def test_the_verb_refuses_when_nothing_is_scheduled(self):
        lines = []
        rc = cvu.cmd_skip_kernel_tonight(self.cfg, out=lines.append,
                                         run=lambda _a: _CP(""))
        self.assertEqual(rc, cvu.EXIT_OK)
        self.assertTrue(any("nothing to skip" in l for l in lines), lines)
        self.assertFalse(cvu.skip_kernel_path(self.cfg).exists())


class SkipAppliesToTheServiceOnlyTests(unittest.TestCase):
    """The veto binds the scheduled run and nothing else."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = cvu.Config(void_packages=self.tmp / "vp",
                              log_root=self.tmp / "state" / "log")
        self.cfg.kernel_enable = True
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(), os.environ.update(self._env)))
        p = cvu.skip_kernel_path(self.cfg)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(time.strftime("%Y-%m-%dT%H:%M:%S",
                                   time.localtime(time.time() + 3600)),
                     encoding="utf-8")
        self.seen = {}

        def _fake_commit(_x, cfg, **kw):
            self.seen["kernel_enable"] = cfg.kernel_enable
            return cvu.EXIT_OK
        self._patch = unittest.mock.patch.object(cvu, "cmd_commit", _fake_commit)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        unittest.mock.patch.object(cvu, "poke_tray", lambda *a, **k: False).start()

    def test_the_scheduled_run_skips_the_kernel(self):
        os.environ[cvu.ORIGIN_ENV] = "schedule"
        cvu.main(["--commit", "--yes"], config=self.cfg, xbps=object(),
                 out=lambda _l: None)
        self.assertFalse(self.seen["kernel_enable"])

    def test_the_veto_is_consumed_after_one_run(self):
        os.environ[cvu.ORIGIN_ENV] = "schedule"
        cvu.main(["--commit", "--yes"], config=self.cfg, xbps=object(),
                 out=lambda _l: None)
        self.assertFalse(cvu.skip_kernel_path(self.cfg).exists())

    def test_a_manual_kernel_press_is_NOT_vetoed(self):
        # Pressing "Update kernel" by hand after skipping the nightly is an
        # unambiguous request for the kernel. Overriding it would be the
        # updater second-guessing a deliberate act.
        os.environ[cvu.ORIGIN_ENV] = "gui"
        cvu.main(["--commit", "--yes"], config=self.cfg, xbps=object(),
                 out=lambda _l: None)
        self.assertTrue(self.seen["kernel_enable"])
        self.assertTrue(cvu.skip_kernel_path(self.cfg).exists(),
                        "a manual run consumed the nightly's veto")


class TrayNightlyRenderingTests(unittest.TestCase):
    """The tray renders the CLI's facts; it must not invent them."""

    @classmethod
    def setUpClass(cls):
        from PyQt5.QtWidgets import QApplication
        loader = importlib.machinery.SourceFileLoader("cachytray2", str(TRAY_PATH))
        spec = importlib.util.spec_from_loader("cachytray2", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mod.Tray.check = lambda s: None
        self.t = self.mod.Tray(self.app)
        self.addCleanup(self.t.hide)
        self.sent = []
        self.t.supportsMessages = lambda: True
        self.t.showMessage = lambda *a, **k: self.sent.append(a)

    @staticmethod
    def _payload(**over):
        d = {"schema": 1, "fresh": True, "attention": [],
             "upstream": {"updatable": 0, "held": 0}, "kernel": {},
             "run": {"active": False}, "notes": []}
        d.update(over)
        return d

    def test_a_running_nightly_is_named_in_the_tooltip(self):
        lines, badge, _ = self.t.summary(self._payload(
            attention=["run-active"],
            run={"active": True, "nightly": True,
                 "holder": "the scheduled nightly run (§4.9) (pid 705)"}))
        self.assertTrue(any("scheduled nightly run" in l for l in lines), lines)

    def test_a_run_in_progress_is_not_an_amber_warning(self):
        # It is the machine working, not a problem.
        _l, badge, _r = self.t.summary(self._payload(
            attention=["run-active"], run={"active": True, "holder": "x"}))
        self.assertNotEqual(badge, self.mod.WARN)

    def test_the_countdown_shows_the_real_number(self):
        lines, badge, _ = self.t.summary(self._payload(
            attention=["nightly-kernel-soon"], nightly={"minutes": 7}))
        self.assertTrue(any("7 min" in l for l in lines), lines)
        self.assertEqual(badge, self.mod.WARN)

    def test_a_finished_nightly_announces_itself_once(self):
        self.t.render(self._payload(attention=["run-active"],
                                    run={"active": True, "nightly": True,
                                         "holder": "the scheduled nightly run"}))
        self.sent.clear()
        self.t.render(self._payload())
        self.assertEqual(len(self.sent), 1, self.sent)
        self.assertIn("finished", self.sent[0][0])
        self.sent.clear()
        self.t.render(self._payload())
        self.assertEqual(self.sent, [], "it announced the same finish twice")

    def test_a_finished_MANUAL_run_is_not_announced(self):
        # The window streamed the whole thing; narrating what you just watched
        # is how an indicator trains people to dismiss it.
        self.t.render(self._payload(attention=["run-active"],
                                    run={"active": True, "nightly": False,
                                         "holder": "the updater window"}))
        self.sent.clear()
        self.t.render(self._payload())
        self.assertEqual(self.sent, [], self.sent)

    def test_a_tray_started_midway_does_not_claim_a_finish(self):
        # nightly_seen is False at startup on purpose.
        self.t.render(self._payload())
        self.assertEqual(self.sent, [], self.sent)


class FrontEndLiteralTests(unittest.TestCase):
    """Duplicated literals between a front-end and the CLI, asserted.

    Same family as the tray's attention vocabulary: the GUI keys its "Skip
    tonight's kernel" button off a phrase the CLI prints. If the CLI rewords
    it, the button silently stops appearing -- nothing errors, a feature just
    quietly leaves.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def test_the_gui_keys_off_a_phrase_the_cli_actually_prints(self):
        gui = (self.ROOT / "system" / "bin"
               / "cachy-updater-gui").read_text(encoding="utf-8")
        self.assertIn('"kernel INCLUDED" in text', gui)
        line = cvu.scheduled_run_line.__doc__ or ""
        src = (self.ROOT / "updater"
               / "cachy_void_update.py").read_text(encoding="utf-8")
        self.assertIn("kernel INCLUDED", src)

    def test_the_gui_runs_the_verb_the_cli_registers(self):
        gui = (self.ROOT / "system" / "bin"
               / "cachy-updater-gui").read_text(encoding="utf-8")
        self.assertIn('"--skip-kernel-tonight"', gui)
        parser = cvu.build_parser()
        ns = parser.parse_args(["--skip-kernel-tonight"])
        self.assertTrue(ns.skip_kernel_tonight)


class _CP:
    def __init__(self, stdout="", rc=0):
        self.stdout, self.returncode, self.stderr = stdout, rc, ""


import unittest.mock  # noqa: E402  (used above; imported late to keep the top tidy)


class PendingGateTests(unittest.TestCase):
    """When does --pending say "a kernel build starts soon"?

    Every clause matters and each one is a separate way to be wrong. A
    countdown to a build that will not happen is worse than no countdown: it
    is the false alarm that teaches someone to ignore the real one.
    """

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = cvu.Config(void_packages=self.tmp / "vp",
                              log_root=self.tmp / "state" / "log")
        self.patches = []

    def _probe(self, *, minutes=5, kernel_port=True, holder="",
               skipping=False, enabled=True, running=True, kernel=True):
        """Run cmd_pending with every external fact pinned."""
        facts = {"enabled": enabled, "running": running,
                 "hour": 1, "minute": 0, "kernel": kernel}
        pat = [
            unittest.mock.patch.object(cvu, "schedule_facts",
                                       lambda **k: dict(facts)),
            unittest.mock.patch.object(cvu, "minutes_until",
                                       lambda *a, **k: minutes),
            unittest.mock.patch.object(cvu, "read_lock_holder",
                                       lambda _c: holder),
            unittest.mock.patch.object(cvu, "skip_kernel_active",
                                       lambda _c, **k: skipping),
            unittest.mock.patch.object(cvu, "upstream_counts",
                                       lambda _r: (0, 0, True, 0)),
            unittest.mock.patch.object(cvu, "last_run_failure",
                                       lambda _c: None),
            unittest.mock.patch.object(
                cvu, "kernel_port_available",
                lambda *a, **k: cvu.PortVerdict(
                    kernel_port, "6.12.111", "6.12.108_1", "upstream", None)),
        ]
        for p in pat:
            p.start()
            self.addCleanup(p.stop)
        lines = []
        cvu.cmd_pending(self.cfg, out=lines.append, run=lambda _a: _CP(""),
                        disk_usage=lambda _p: _DU())
        return json.loads(chr(10).join(lines))

    def test_it_warns_inside_the_window(self):
        d = self._probe(minutes=5)
        self.assertIn("nightly-kernel-soon", d["attention"])
        self.assertEqual(d["nightly"]["minutes"], 5)

    def test_it_is_silent_outside_the_window(self):
        d = self._probe(minutes=240)
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    def test_it_is_silent_when_there_is_no_kernel_to_build(self):
        # The most important clause: warning about a compile that is not
        # going to happen is the false alarm that kills the real one.
        d = self._probe(minutes=5, kernel_port=False)
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    def test_it_is_silent_once_tonight_is_already_skipped(self):
        d = self._probe(minutes=5, skipping=True)
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    def test_it_is_silent_when_the_schedule_is_userspace_only(self):
        d = self._probe(minutes=5, kernel=False)
        self.assertNotIn("nightly-kernel-soon", d["attention"])
        self.assertNotIn("nightly", d)

    def test_it_is_silent_when_the_service_is_paused(self):
        d = self._probe(minutes=5, running=False)
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    def test_it_is_silent_when_nothing_is_scheduled(self):
        d = self._probe(minutes=5, enabled=False)
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    def test_it_is_silent_while_a_run_is_already_going(self):
        # Counting down to something already under way is nonsense.
        d = self._probe(minutes=5, holder="the scheduled nightly run")
        self.assertNotIn("nightly-kernel-soon", d["attention"])

    # -- the run-in-progress half -----------------------------------------
    def test_a_running_nightly_is_reported(self):
        d = self._probe(holder="the scheduled nightly run (§4.9) (pid 705)")
        self.assertIn("run-active", d["attention"])
        self.assertTrue(d["run"]["active"])
        self.assertTrue(d["run"]["nightly"])

    def test_a_running_window_is_reported_but_not_as_nightly(self):
        d = self._probe(holder="the updater window (pid 99)")
        self.assertIn("run-active", d["attention"])
        self.assertTrue(d["run"]["active"])
        self.assertFalse(d["run"]["nightly"])

    def test_no_run_means_no_token(self):
        d = self._probe(holder="")
        self.assertNotIn("run-active", d["attention"])
        self.assertFalse(d["run"]["active"])


class _DU:
    total = free = used = 500 * 1024 ** 3

if __name__ == "__main__":
    unittest.main()
