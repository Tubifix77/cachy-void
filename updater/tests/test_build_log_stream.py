"""The build log must exist WHILE the build runs, not only after it.

A kernel build on the testbed is 40 minutes to six hours. The previous
implementation captured the whole thing in memory and wrote the file when the
compiler exited, so for the entire build there was no file at all -- nothing
for a person, the tray or the window to watch -- and an interrupted build, the
one most worth reading afterwards, left nothing behind at all.
"""
import os
import pathlib
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from engine.xbps import Xbps, _stream_to_log  # noqa: E402


class StreamToLogTests(unittest.TestCase):

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.log = self.tmp / "build.log"

    def test_output_is_captured(self):
        rc = _stream_to_log(["sh", "-c", "echo one; echo two"], None,
                            str(self.log))
        self.assertEqual(rc, 0)
        self.assertEqual(self.log.read_text(encoding="utf-8").split(),
                         ["one", "two"])

    def test_stderr_is_folded_in(self):
        # A build's errors are the part you most want in the log.
        _stream_to_log(["sh", "-c", "echo out; echo err >&2"], None,
                       str(self.log))
        text = self.log.read_text(encoding="utf-8")
        self.assertIn("out", text)
        self.assertIn("err", text)

    def test_the_exit_code_is_returned(self):
        self.assertEqual(
            _stream_to_log(["sh", "-c", "exit 7"], None, str(self.log)), 7)

    def test_the_log_is_readable_BEFORE_the_command_finishes(self):
        # The whole point. Run something that speaks, then waits: the first
        # line must be on disk while the process is still alive.
        script = "echo first; sleep 3; echo last"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from engine.xbps import _stream_to_log; "
             "_stream_to_log(['sh','-c',%r], None, %r)"
             % (str(pathlib.Path(__file__).resolve().parents[1]),
                script, str(self.log))])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        deadline = time.time() + 2.5
        seen = ""
        while time.time() < deadline:
            if self.log.exists():
                seen = self.log.read_text(encoding="utf-8")
                if "first" in seen:
                    break
            time.sleep(0.05)
        self.assertIn("first", seen,
                      "nothing was on disk while the command was still running")
        self.assertNotIn("last", seen, "the test raced past the sleep")

    def test_a_killed_build_keeps_what_it_had_written(self):
        # The forensic case: the build that gets interrupted is the one whose
        # log matters most, and it used to be the only one that produced none.
        script = "echo progress-line; sleep 30"
        proc = subprocess.Popen(
            [sys.executable, "-c",
             "import sys; sys.path.insert(0, %r); "
             "from engine.xbps import _stream_to_log; "
             "_stream_to_log(['sh','-c',%r], None, %r)"
             % (str(pathlib.Path(__file__).resolve().parents[1]),
                script, str(self.log))])
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.log.exists() and "progress-line" in \
                    self.log.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        proc.kill()
        proc.wait()
        self.assertIn("progress-line",
                      self.log.read_text(encoding="utf-8"),
                      "a killed run lost its log")


class BuildUsesTheStreamerTests(unittest.TestCase):
    """build() routes to the streamer only when a log was asked for."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.calls = []

    def _xbps(self, **kw):
        def _run(args, cwd=None):
            self.calls.append(("run", list(args), cwd))
            return subprocess.CompletedProcess(args, 0, "", "")
        return Xbps(void_packages=self.tmp, run=_run, **kw)

    def test_no_log_path_uses_the_plain_runner(self):
        # Every existing caller and test does this; it must not change.
        x = self._xbps()
        self.assertEqual(x.build("linux-cachy", 4), 0)
        self.assertEqual(self.calls[0][0], "run")

    def test_a_log_path_uses_the_streamer(self):
        seen = {}

        def _stream(args, cwd, log_path):
            seen.update(args=list(args), cwd=cwd, log=log_path)
            return 0
        x = self._xbps(stream=_stream)
        self.assertEqual(x.build("linux-cachy", 4, "/tmp/x.log"), 0)
        self.assertEqual(seen["log"], "/tmp/x.log")
        self.assertEqual(self.calls, [], "it also ran the captured runner")

    def test_the_streamer_still_gets_the_masterdir_flag(self):
        # The bug that filled a root partition overnight: build() assembling
        # its own argv and missing -m. A new code path is a new chance to
        # repeat it.
        seen = {}

        def _stream(args, cwd, log_path):
            seen["args"] = list(args)
            return 0
        x = self._xbps(stream=_stream, masterdir=pathlib.Path("/mnt/ext/b"))
        x.build("linux-cachy", 4, "/tmp/x.log")
        self.assertIn("-m", seen["args"])
        self.assertIn("/mnt/ext/b", seen["args"])
        self.assertIn("-j4", seen["args"])

    def test_the_streamers_exit_code_is_returned(self):
        x = self._xbps(stream=lambda *a: 40)
        self.assertEqual(x.build("linux-cachy", 4, "/tmp/x.log"), 40)

if __name__ == "__main__":
    unittest.main()
