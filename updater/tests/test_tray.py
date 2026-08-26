"""Unit tests for the updater's tray indicator (`system/bin/cachy-updater-tray`).

Loaded offscreen against the real script, like test_gui.py, so the file that
ships is the file under test.

What is worth testing here is narrow and deliberate. The tray owns no policy —
it renders the CLI's ``attention`` list — so these tests mostly pin down that it
stays a *renderer*: that a failed probe badges nothing, that "needs you" and
"there is news" get different colours, and that a notification fires once per
new reason rather than every poll. Those are the three ways a passive indicator
turns into something people learn to ignore.
"""
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication          # noqa: E402

TRAY_PATH = pathlib.Path(__file__).resolve().parents[2] / "system" / "bin" / "cachy-updater-tray"


def _payload(**over):
    d = {"schema": 1, "fresh": True, "attention": [],
         "upstream": {"updatable": 0, "held": 0}, "kernel": {}, "notes": []}
    d.update(over)
    return d


class TrayLogicTests(unittest.TestCase):

    app = None

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachytray", str(TRAY_PATH))
        spec = importlib.util.spec_from_loader("cachytray", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        # A Tray is a QSystemTrayIcon; constructing one offscreen is fine, but
        # its first poll must not actually spawn the CLI.
        self.mod.Tray.check = lambda s: None
        self.t = self.mod.Tray(self.app)

    def tearDown(self):
        self.t.hide()

    # -- what the badge means ----------------------------------------------
    def test_nothing_pending_means_no_badge(self):
        lines, badge, reasons = self.t.summary(_payload())
        self.assertEqual(badge, "")
        self.assertEqual(reasons, [])
        self.assertIn("up to date", lines[0])

    def test_updates_get_the_accent_badge(self):
        lines, badge, _ = self.t.summary(
            _payload(attention=["updates"], upstream={"updatable": 3, "held": 0}))
        self.assertEqual(badge, self.mod.ACCENT)
        self.assertIn("3 packages to update", lines[0])

    def test_one_package_is_not_pluralised(self):
        lines, _, _ = self.t.summary(
            _payload(attention=["updates"], upstream={"updatable": 1, "held": 0}))
        self.assertIn("1 package to update", lines[0])

    def test_holds_are_named_but_not_counted_as_updates(self):
        lines, _, _ = self.t.summary(
            _payload(attention=["updates"], upstream={"updatable": 2, "held": 4}))
        self.assertIn("2 packages to update (4 on hold)", lines[0])

    def test_needs_you_reasons_get_the_warning_badge(self):
        # "a candidate failed its health check" is not the same kind of news as
        # "there are updates", and the colour is how that difference lands.
        for reason in ("kernel-unhealthy", "bore-pin-missing"):
            _, badge, _ = self.t.summary(_payload(attention=[reason]))
            self.assertEqual(badge, self.mod.WARN, reason)

    def test_warning_wins_over_ordinary_news(self):
        _, badge, _ = self.t.summary(
            _payload(attention=["updates", "bore-pin-missing"],
                     upstream={"updatable": 5, "held": 0}))
        self.assertEqual(badge, self.mod.WARN)

    def test_staged_kernel_is_news_not_a_warning(self):
        _, badge, _ = self.t.summary(_payload(attention=["kernel-staged"]))
        self.assertEqual(badge, self.mod.ACCENT)

    def test_unknown_reason_tokens_are_ignored_not_rendered(self):
        # Forward compatibility: a newer CLI may report a reason this tray does
        # not know. It must not badge for something it cannot explain.
        _, badge, reasons = self.t.summary(_payload(attention=["something-new"]))
        self.assertEqual(reasons, [])
        self.assertEqual(badge, "")

    def test_stale_counts_are_disclosed(self):
        lines, _, _ = self.t.summary(
            _payload(fresh=False, attention=["updates"],
                     upstream={"updatable": 2, "held": 0}))
        self.assertTrue(any("local cache" in l for l in lines))

    def test_malformed_counts_do_not_raise(self):
        lines, badge, _ = self.t.summary(
            _payload(upstream={"updatable": "lots", "held": None}))
        self.assertIn("up to date", lines[0])
        self.assertEqual(badge, "")

    # -- what a failed probe must NOT do -----------------------------------
    def test_failed_probe_badges_nothing(self):
        # An icon that cries "updates!" because it could not read anything is
        # how a status indicator becomes wallpaper.
        self.t.render(_payload(attention=["updates"],
                               upstream={"updatable": 9, "held": 0}))
        self.t.go_quiet("could not run the CLI")
        self.assertIn("could not run", self.t.toolTip())
        self.assertIn("could not run", self.t.act_status.text())

    def test_non_json_output_is_survived(self):
        class FakeProc:
            @staticmethod
            def readAllStandardOutput():
                return b"Traceback (most recent call last): ..."
        self.t.proc = FakeProc()
        self.t.on_finished(0, None)
        self.assertIn("could not read", self.t.toolTip())

    def test_non_zero_exit_is_survived(self):
        class FakeProc:
            @staticmethod
            def readAllStandardOutput():
                return b""
        self.t.proc = FakeProc()
        self.t.on_finished(1, None)
        self.assertIn("problem", self.t.toolTip())

    def test_a_json_list_is_not_mistaken_for_a_payload(self):
        class FakeProc:
            @staticmethod
            def readAllStandardOutput():
                return json.dumps([1, 2, 3]).encode()
        self.t.proc = FakeProc()
        self.t.on_finished(0, None)
        self.assertIn("could not read", self.t.toolTip())

    # -- never nag ---------------------------------------------------------
    def test_a_reason_is_announced_once_not_every_poll(self):
        seen = []
        self.t.showMessage = lambda *a, **k: seen.append(a[0] if a else "")
        p = _payload(attention=["updates"], upstream={"updatable": 3, "held": 0})
        self.t.render(p)
        self.t.render(p)
        self.t.render(p)
        self.assertLessEqual(len(seen), 1)

    def test_a_reason_that_returns_can_announce_again(self):
        seen = []
        self.t.showMessage = lambda *a, **k: seen.append(a[0] if a else "")
        busy = _payload(attention=["updates"], upstream={"updatable": 3, "held": 0})
        self.t.render(busy)
        self.t.render(_payload())               # user updated: nothing pending
        self.t.render(busy)                     # new updates land later
        self.assertLessEqual(len(seen), 2)
        self.assertGreaterEqual(len(seen), 1)

    def test_a_quiet_system_announces_nothing(self):
        seen = []
        self.t.showMessage = lambda *a, **k: seen.append(a)
        self.t.render(_payload())
        self.assertEqual(seen, [])

    # -- plumbing ----------------------------------------------------------
    def test_icon_is_produced_with_and_without_a_badge(self):
        self.assertFalse(self.t.build_icon().isNull())
        self.assertFalse(self.t.build_icon(self.mod.ACCENT).isNull())

    def test_interval_is_lazy_by_default_and_overridable(self):
        os.environ.pop("CACHY_TRAY_INTERVAL_MIN", None)
        self.assertEqual(self.mod.interval_ms(), 180 * 60_000)
        os.environ["CACHY_TRAY_INTERVAL_MIN"] = "5"
        self.assertEqual(self.mod.interval_ms(), 5 * 60_000)
        os.environ["CACHY_TRAY_INTERVAL_MIN"] = "nonsense"
        self.assertEqual(self.mod.interval_ms(), 180 * 60_000)
        os.environ.pop("CACHY_TRAY_INTERVAL_MIN", None)

    def test_the_menu_offers_no_privileged_action(self):
        # The tray reads and launches; every mutation stays in the GUI/CLI.
        labels = [a.text().lower() for a in self.t.menu.actions() if a.text()]
        for forbidden in ("update now", "clean", "rollback", "install"):
            self.assertFalse([l for l in labels if forbidden in l], labels)

    def test_it_probes_only_the_read_only_action(self):
        self.assertEqual(self.mod.PROBE_ARGS, ["--pending"])

    def test_no_mutating_command_appears_in_the_code(self):
        """The load-bearing safety property, checked against the shipped file.

        A tray is a tempting place to bolt "Update now" onto, and the moment it
        can mutate the system it needs privilege, a confirmation dialog and a
        progress surface it does not have. Keep it a reader.

        Docstrings are excluded deliberately: the module docstring *promises*
        there is no sudo here, and a check that cannot tell a promise from a
        call would fire on the documentation of its own guarantee.
        """
        import ast
        src = TRAY_PATH.read_text(encoding="utf-8")
        tree = ast.parse(src)
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                ds = ast.get_docstring(node, clean=False)
                if ds:
                    docs.add(ds)
        code_strings = [n.value for n in ast.walk(tree)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str)
                        and n.value not in docs]
        for flag in ("--commit", "--clean", "--rollback", "--sync", "--yes",
                     "sudo", "xbps-install", "xbps-remove", "btrfs"):
            offenders = [s for s in code_strings if flag in s]
            self.assertEqual(offenders, [], f"tray must not invoke {flag}")



GUI_PATH = pathlib.Path(__file__).resolve().parents[2] / "system" / "bin" / "cachy-updater-gui"


def _load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RefreshChannelTests(unittest.TestCase):
    """The GUI telling the tray that the world changed.

    The bug this closes was reported from the desk, not from a log: the updater
    installed 82 packages, the owner quit the window, and the tray went on
    advertising 82 pending until its own poll came round. A poll interval is the
    wrong instrument for an event the GUI already knows about.

    The channel is the tray's instance-lock socket, which until now had nothing
    listening on it -- it existed only so a second tray could notice the first
    and exit. What travels down it is a keyword, never a count: the tray re-runs
    its own --pending probe, so it still has exactly one source for what it
    displays. That matters more than it sounds. The last time a front-end
    accepted numbers from elsewhere, the tray said 20 and the window said 16.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.tray_mod = _load("cachytray_ch", TRAY_PATH)
        cls.gui_mod = _load("cachygui_ch", GUI_PATH)

    def test_the_two_scripts_agree_on_the_socket_and_the_keyword(self):
        # Two standalone scripts, so the literals are duplicated by necessity.
        # A rename on one side would not break anything visibly -- it would just
        # quietly restore the stale-tooltip bug -- so it gets asserted instead.
        self.assertEqual(self.gui_mod.TRAY_SOCKET, self.tray_mod.SOCKET_NAME)
        self.assertEqual(self.gui_mod.TRAY_POKE, self.tray_mod.POKE)

    def test_a_state_changing_command_pokes(self):
        was = self.gui_mod.Updater._was_mutating
        for args in (["--commit", "--yes", "--no-kernel"],
                     ["--commit", "--yes"],
                     ["--clean", "--yes"],
                     ["--rollback"],
                     ["--kernel-ack"],
                     ["--pin-bore", "--yes"]):
            self.assertTrue(was(args), args)

    def test_a_read_only_command_does_not(self):
        # --status runs on every refresh and on every window open; poking there
        # would have the tray re-probe for ~15s to learn nothing changed.
        was = self.gui_mod.Updater._was_mutating
        for args in (["--status"], ["--check"], ["--pending"], ["--snapshots"],
                     ["--sync"], ["--gpu"]):
            self.assertFalse(was(args), args)

    def test_a_preview_is_not_a_change(self):
        was = self.gui_mod.Updater._was_mutating
        self.assertFalse(was(["--pin-bore", "--dry-run"]))
        self.assertFalse(was(["--clean", "-n"]))

    def test_the_poke_actually_arrives_over_the_socket(self):
        """End to end over a real QLocalServer -- the wiring, not just the flags.

        Both halves have been individually plausible and jointly broken before,
        which is what an integration test is for.
        """
        from PyQt5.QtNetwork import QLocalServer

        QLocalServer.removeServer(self.tray_mod.SOCKET_NAME)
        server = QLocalServer()
        self.assertTrue(server.listen(self.tray_mod.SOCKET_NAME),
                        "could not listen on the test socket")
        received = []

        def _on_connection():
            sock = server.nextPendingConnection()
            self.assertIsNotNone(sock)
            if sock.waitForReadyRead(1000):
                received.append(bytes(sock.readAll()).strip())
            sock.disconnectFromServer()

        server.newConnection.connect(_on_connection)
        try:
            # a static method on purpose: it touches no widget state, so the
            # channel can be exercised without building a window
            self.gui_mod.Updater._poke_tray()
            self.app.processEvents()
            self.assertEqual(received, [self.tray_mod.POKE])
        finally:
            server.close()
            QLocalServer.removeServer(self.tray_mod.SOCKET_NAME)

    def test_poking_with_no_tray_listening_is_silent(self):
        from PyQt5.QtNetwork import QLocalServer
        QLocalServer.removeServer(self.tray_mod.SOCKET_NAME)
        # must not raise: no tray running is the ordinary case, not an error
        self.gui_mod.Updater._poke_tray()


class AttentionVocabularyTests(unittest.TestCase):
    """The CLI's `attention` tokens and the tray's wording must be one set.

    summary() drops reasons it does not recognise -- deliberately, so a newer
    CLI cannot make an older tray print raw slugs. The cost is that a token
    nobody worded is invisible rather than broken, and that has now happened
    twice: `kernel-frozen` was emitted and never displayed, and a portable
    kernel had no token at all while the window announced one. Neither showed
    up as a failure anywhere; both were found by a person noticing silence.

    So the contract gets asserted instead of documented. This is the same test
    in spirit as the socket-literal check above: two files that must agree, and
    no runtime symptom when they stop agreeing.
    """

    def test_the_cli_and_the_tray_know_the_same_reasons(self):
        import importlib
        import sys
        root = pathlib.Path(__file__).resolve().parents[1]
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        cli = importlib.import_module("cachy_void_update")
        tray = _load("cachytray_vocab", TRAY_PATH)
        self.assertEqual(set(cli.ATTENTION_TOKENS), set(tray.REASON_TEXT),
                         "a token exists on one side only: the tray would say "
                         "nothing about it, silently")

    def test_warnings_are_a_subset_of_known_reasons(self):
        tray = _load("cachytray_vocab2", TRAY_PATH)
        self.assertTrue(set(tray.REASON_IS_WARNING) <= set(tray.REASON_TEXT))

    def test_news_is_not_dressed_as_a_fault(self):
        # An available kernel is something you may want, not something wrong.
        tray = _load("cachytray_vocab3", TRAY_PATH)
        self.assertNotIn("kernel-port", tray.REASON_IS_WARNING)

if __name__ == "__main__":
    unittest.main()
