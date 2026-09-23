"""§8.5 G3 freeze: it must carry the evidence a human needs."""
import pathlib
import shutil
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
import cachy_void_update as cvu  # noqa: E402


class FrozenBuildEvidenceTests(unittest.TestCase):
    """AWAIT_HUMAN_BUILD must name something to look AT.

    The state asks a human to look and, until now, named nothing: the pointer
    it offered was the last-run notice, which said "xbps-src exited 1". On
    2026-09-23 the true cause was a second updater run tearing down the build
    tree mid-compile, and the only place that was visible was the build log.
    The owner's words for the message they got: it "seems wrong".
    """

    def _explain(self, detail):
        return cvu.frozen_explanation("AWAIT_HUMAN_BUILD", "", detail)

    def test_the_log_path_is_named(self):
        lines = self._explain({"log": "/var/log/run-7/build-linux-cachy.log"})
        self.assertTrue(any("/var/log/run-7/build-linux-cachy.log" in l
                            for l in lines), lines)

    def test_the_reason_and_version_are_named(self):
        lines = self._explain({"reason": "xbps-src exited 1",
                               "pkgver": "linux-cachy-6.12.111_1",
                               "ts": "2026-09-23T23:02:00Z"})
        joined = " ".join(lines)
        self.assertIn("xbps-src exited 1", joined)
        self.assertIn("6.12.111", joined)
        self.assertIn("2026-09-23T23:02:00Z", joined)

    def test_an_interrupted_build_is_distinguished_from_a_broken_one(self):
        # The distinction that would have saved the whole investigation: a
        # tree that vanishes mid-compile is not a bad kernel.
        lines = self._explain({"log": "/tmp/b.log"})
        joined = " ".join(lines).lower()
        self.assertIn("interrupted", joined)
        self.assertIn("re-run", joined)

    def test_it_still_reassures_about_the_running_kernel(self):
        joined = " ".join(self._explain({"log": "/tmp/b.log"}))
        self.assertIn("Nothing is wrong with the running kernel", joined)

    def test_no_detail_falls_back_to_the_old_pointer(self):
        # A state written by an older version has no build_failure block; the
        # reader must degrade, not crash or go silent.
        for detail in (None, {}):
            lines = cvu.frozen_explanation("AWAIT_HUMAN_BUILD", "", detail)
            self.assertTrue(lines)
            self.assertTrue(any("--status" in l for l in lines), lines)

    def test_a_candidate_freeze_is_unaffected(self):
        # CANDIDATE_UNHEALTHY is a different story and must not borrow this one.
        lines = cvu.frozen_explanation("CANDIDATE_UNHEALTHY", "6.12.9-cachy",
                                       {"log": "/tmp/b.log"})
        self.assertFalse(any("/tmp/b.log" in l for l in lines), lines)


class FreezeRecordsEvidenceTests(unittest.TestCase):
    """The failing run must WRITE the evidence; the state outlives the run."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.recorded = []

    def _cfg(self):
        c = cvu.Config(void_packages=pathlib.Path(self.tmp) / "vp")
        c.kernel_enable = True
        return c

    def test_the_reason_and_log_reach_the_state_file(self):
        cfg = self._cfg()
        with unittest.mock.patch.object(
                cvu, "_record_kernel_state",
                lambda _c, patch, _o: self.recorded.append(patch)):
            cvu._note_kernel_build_failure(
                cfg, cvu.KERNEL_TARGET, lambda _l: None,
                reason="xbps-src exited 1", log="/tmp/build.log")
        self.assertEqual(len(self.recorded), 1)
        got = self.recorded[0]
        self.assertEqual(got["state"], "AWAIT_HUMAN_BUILD")
        self.assertEqual(got["build_failure"]["reason"], "xbps-src exited 1")
        self.assertEqual(got["build_failure"]["log"], "/tmp/build.log")
        self.assertTrue(got["build_failure"]["ts"], "no timestamp recorded")

    def test_a_non_kernel_failure_never_freezes(self):
        cfg = self._cfg()
        with unittest.mock.patch.object(
                cvu, "_record_kernel_state",
                lambda _c, patch, _o: self.recorded.append(patch)):
            cvu._note_kernel_build_failure(cfg, "gamemode", lambda _l: None,
                                           reason="boom", log="/tmp/g.log")
        self.assertEqual(self.recorded, [])


if __name__ == "__main__":
    unittest.main()
