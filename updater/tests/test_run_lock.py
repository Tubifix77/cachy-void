"""§4 run lock -- one mutating run at a time (architecture.md:437)."""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import cachy_void_update as cvu  # noqa: E402


class _LockStubXbps:
    """Minimal xbps stand-in: status/pending probe it but must not need a box."""

    def __getattr__(self, _name):
        def _nope(*_a, **_k):
            raise cvu.XbpsError("no xbps in this test")
        return _nope


@unittest.skipIf(cvu.fcntl is None, "flock is POSIX-only")
class RunLockTests(unittest.TestCase):
    """§4 'Locking': one mutating run at a time, second exits code 10.

    The requirement is as old as the spec and was implemented by nothing for
    the project's whole life -- EXIT_LOCKED was defined and never referenced.
    It cost a real kernel build on 2026-09-23: a manual "Update kernel" at
    00:56 and the §4.9 nightly at 01:00 built the same package in the same
    chroot, and the first died three seconds after the second started,
    reporting source headers missing that were present on disk. The lesson
    these tests encode is that the failure did not LOOK like a collision --
    so the lock has to prevent it, and the refusal has to name the holder.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.cfg = cvu.Config(void_packages=pathlib.Path(self.tmp) / "vp",
                              log_root=pathlib.Path(self.tmp) / "state" / "log")
        self._env = dict(os.environ)
        self.addCleanup(lambda: (os.environ.clear(),
                                 os.environ.update(self._env)))
        os.environ.pop(cvu.ORIGIN_ENV, None)

    # -- the lock itself ---------------------------------------------------
    def test_an_uncontended_run_gets_the_lock(self):
        with cvu.run_lock(self.cfg, "--commit") as holder:
            self.assertEqual(holder, "")

    def test_a_second_run_is_refused_while_the_first_holds_it(self):
        with cvu.run_lock(self.cfg, "--commit --yes") as first:
            self.assertEqual(first, "")
            with cvu.run_lock(self.cfg, "--commit") as second:
                self.assertTrue(second, "second run was NOT blocked")

    def test_the_lock_is_released_when_the_run_ends(self):
        # The reason the spec says flock and not a pidfile: no stale state.
        with cvu.run_lock(self.cfg, "--commit"):
            pass
        with cvu.run_lock(self.cfg, "--commit") as holder:
            self.assertEqual(holder, "")

    def test_the_refusal_names_the_scheduled_run(self):
        # "I had no idea anything else was running" is the actual complaint
        # this fixes; a refusal that says only "locked" would not fix it.
        os.environ[cvu.ORIGIN_ENV] = "schedule"
        with cvu.run_lock(self.cfg, "--commit --yes"):
            os.environ[cvu.ORIGIN_ENV] = "gui"
            with cvu.run_lock(self.cfg, "--commit") as holder:
                self.assertIn("nightly", holder)
                self.assertIn("--commit --yes", holder)

    def test_the_refusal_names_the_window(self):
        os.environ[cvu.ORIGIN_ENV] = "gui"
        with cvu.run_lock(self.cfg, "--commit"):
            with cvu.run_lock(self.cfg, "--sync") as holder:
                self.assertIn("window", holder)

    def test_an_unlabelled_holder_still_blocks(self):
        # Degrading to a vague description is fine; failing to block is not.
        with cvu.run_lock(self.cfg, "--commit"):
            cvu.lock_path(self.cfg).write_text("", encoding="utf-8")
            with cvu.run_lock(self.cfg, "--commit") as holder:
                self.assertTrue(holder)

    # -- the read-only view front-ends use ---------------------------------
    def test_read_lock_holder_is_empty_when_free(self):
        self.assertEqual(cvu.read_lock_holder(self.cfg), "")

    def test_read_lock_holder_sees_a_running_run(self):
        os.environ[cvu.ORIGIN_ENV] = "schedule"
        with cvu.run_lock(self.cfg, "--commit --yes"):
            self.assertIn("nightly", cvu.read_lock_holder(self.cfg))

    def test_read_lock_holder_does_not_consume_the_lock(self):
        # It must not lock out the run it is reporting on -- the tray polls.
        cvu.read_lock_holder(self.cfg)
        with cvu.run_lock(self.cfg, "--commit") as holder:
            self.assertEqual(holder, "")
            cvu.read_lock_holder(self.cfg)
            cvu.read_lock_holder(self.cfg)

    # -- what main() does with it ------------------------------------------
    def test_commit_is_refused_while_another_run_holds_the_lock(self):
        lines = []
        with cvu.run_lock(self.cfg, "--commit --yes"):
            rc = cvu.main(["--commit", "--yes"], config=self.cfg,
                          xbps=object(), out=lines.append)
        self.assertEqual(rc, cvu.EXIT_LOCKED)
        self.assertTrue(any("refusing to start" in l for l in lines), lines)

    def test_sync_is_refused_while_another_run_holds_the_lock(self):
        lines = []
        with cvu.run_lock(self.cfg, "--commit --yes"):
            rc = cvu.main(["--sync"], config=self.cfg, xbps=object(),
                          out=lines.append)
        self.assertEqual(rc, cvu.EXIT_LOCKED)

    def test_a_blocked_run_changes_nothing(self):
        # The refusal must be inert: cmd_commit is never entered, so a stub
        # xbps that explodes on any use proves nothing touched the system.
        class _Boom:
            def __getattr__(self, name):
                raise AssertionError("the blocked run touched xbps." + name)
        with cvu.run_lock(self.cfg, "--commit --yes"):
            rc = cvu.main(["--commit", "--yes"], config=self.cfg,
                          xbps=_Boom(), out=lambda _l: None)
        self.assertEqual(rc, cvu.EXIT_LOCKED)

    def test_read_only_actions_are_never_blocked(self):
        # The tray polls --pending on a timer and the window reads --status
        # while a build runs. Locking those would break both to prevent
        # nothing, since neither mutates anything.
        with cvu.run_lock(self.cfg, "--commit --yes"):
            for argv in (["--pending"], ["--status"]):
                lines = []
                rc = cvu.main(argv, config=self.cfg, xbps=_LockStubXbps(),
                              out=lines.append)
                self.assertNotEqual(rc, cvu.EXIT_LOCKED, argv)
                self.assertFalse(any("refusing to start" in l for l in lines),
                                 (argv, lines))

    def test_status_reports_a_run_in_progress(self):
        os.environ[cvu.ORIGIN_ENV] = "schedule"
        with cvu.run_lock(self.cfg, "--commit --yes"):
            lines = []
            cvu.main(["--status"], config=self.cfg, xbps=_LockStubXbps(),
                     out=lines.append)
        self.assertTrue(any("RUN IN PROGRESS" in l for l in lines), lines)
        self.assertTrue(any("nightly" in l for l in lines), lines)

    def test_status_says_nothing_when_no_run_is_going(self):
        lines = []
        cvu.main(["--status"], config=self.cfg, xbps=_LockStubXbps(),
                 out=lines.append)
        self.assertFalse(any("RUN IN PROGRESS" in l for l in lines), lines)


class RunLockWiringTests(unittest.TestCase):
    """The two front-ends must declare who they are.

    Without this the lock still works but its message cannot say WHICH run is
    holding it -- and "something is running" is barely better than the
    missing-header error it replaces.
    """

    ROOT = pathlib.Path(__file__).resolve().parents[2]

    def test_the_scheduled_service_declares_itself(self):
        run = (self.ROOT / "system" / "sv" / "cachy-void-update"
               / "run").read_text(encoding="utf-8")
        self.assertIn("CACHY_RUN_ORIGIN=schedule", run)
        self.assertLess(run.index("CACHY_RUN_ORIGIN"), run.index("exec chpst"),
                        "the export must precede the exec to reach the run")

    def test_the_window_declares_itself(self):
        gui = (self.ROOT / "system" / "bin"
               / "cachy-updater-gui").read_text(encoding="utf-8")
        self.assertIn('"CACHY_RUN_ORIGIN", "gui"', gui)

    def test_origin_values_agree_with_the_cli(self):
        # A front-end shipping an origin the CLI does not recognise degrades
        # silently to "another run" -- the same class of bug as the tray's
        # unknown attention tokens, which went unnoticed twice.
        self.assertEqual(set(cvu._ORIGIN_TEXT), {"schedule", "gui", "manual"})


if __name__ == "__main__":
    unittest.main()
