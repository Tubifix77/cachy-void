"""Unit tests for the graphical front-end's decision logic (§8.3a surfacing).

The GUI is thin by design — but the parts that are NOT thin are exactly the
parts a user's whole experience depends on: whether the BORE-pin banner appears,
and whether the pin button previews before it writes. Those are asserted here
against the real ``system/bin/cachy-updater-gui`` (loaded offscreen, with
``_run`` stubbed so no subprocess is ever spawned).

Skipped when PyQt5 is unavailable — the engine and CLI tests must stay runnable
on a headless/minimal box.
"""
import importlib.machinery
import importlib.util
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

GUI_PATH = Path(__file__).resolve().parents[2] / "system" / "bin" / "cachy-updater-gui"

try:
    from PyQt5.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:              # pragma: no cover - environment dependent
    HAVE_QT = False

PINNED = ("[3] Kernel (linux-cachy / BORE)\n"
          "    BORE pin: series 6.12 pinned (BORE 6.6.3) - 2026-07-15 boas\n")
MISSING = ("[3] Kernel (linux-cachy / BORE)\n"
           "    BORE pin: MISSING for series 6.12 - kernel updates stay paused\n")


@unittest.skipUnless(HAVE_QT, "PyQt5 not installed")
class PinBannerTests(unittest.TestCase):

    app = None

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachygui", str(GUI_PATH))
        spec = importlib.util.spec_from_loader("cachygui", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        # Stub the QProcess layer: record invocations, spawn nothing. Also keeps
        # the constructor's opening check() inert.
        self.mod.Updater._run = lambda s, args, target, **kw: self.calls.append(
            (list(args), kw))
        self.w = self.mod.Updater()
        # The window must actually be shown (offscreen): Qt reports isVisible()
        # False for every child of an unshown parent, which would make the
        # banner assertions pass vacuously.
        self.w.show()

    def tearDown(self):
        self.w.deleteLater()

    def test_banner_hidden_before_any_status(self):
        self.assertFalse(self.w.pin_label.isVisible())
        self.assertFalse(self.w.btn_pin.isVisible())

    def test_pinned_status_keeps_banner_hidden(self):
        self.w.status.setPlainText(PINNED)
        self.w._update_pin_banner()
        self.assertFalse(self.w.pin_label.isVisible())
        self.assertFalse(self.w.btn_pin.isVisible())

    def test_missing_status_raises_banner_and_button(self):
        self.w.status.setPlainText(MISSING)
        self.w._update_pin_banner()
        self.assertTrue(self.w.pin_label.isVisible())
        self.assertTrue(self.w.btn_pin.isVisible())

    def test_banner_states_the_consequence_not_jargon(self):
        self.assertIn("kernel updates are paused", self.w.pin_label.text().lower())

    def test_check_refreshes_the_banner(self):
        self.calls.clear()
        self.w.check()
        self.assertEqual(self.calls[0][0], ["--status"])
        self.assertIsNotNone(self.calls[0][1].get("done"))

    def test_pin_button_previews_before_writing(self):
        """First action must be the read-only preview; the write only happens
        after the confirm dialog (never reached here)."""
        self.calls.clear()
        self.w.btn_pin.click()
        self.assertEqual(self.calls[0][0], ["--pin-bore", "--dry-run"])
        self.assertTrue(all("--yes" not in c[0] for c in self.calls))

    def test_recheck_button_is_demoted_and_says_re_check(self):
        """It ran on open and re-runs after every command, so it must not look
        like the thing to press first — and its label must make clear a check
        already happened (user feedback: 'unsure if it ran automatically')."""
        self.assertEqual(self.w.btn_check.objectName(), "quiet")
        self.assertTrue(self.w.btn_check.text().startswith("Re-check"))
        # ...while the real primary action keeps its emphasis
        self.assertEqual(self.w.btn_update.objectName(), "primary")

    def test_age_text_wording(self):
        age = self.w._age_text
        self.assertEqual(age(None), "")
        self.assertEqual(age(0), "checked just now")
        self.assertEqual(age(89), "checked just now")
        self.assertEqual(age(95), "checked 1 minute ago")
        self.assertEqual(age(600), "checked 10 minutes ago")
        self.assertEqual(age(3600), "checked 1 hour ago")
        self.assertEqual(age(7200), "checked 2 hours ago")

    def test_check_stamps_the_snapshot_age(self):
        """A finished check must record when the pending list was read, so a
        stale pane can never look authoritative."""
        self.assertEqual(self.w.checked_at.text(), "")
        self.calls.clear()
        self.w.check()
        self.calls[0][1]["done"](0)          # simulate the CLI finishing
        self.assertEqual(self.w.checked_at.text(), "checked just now")

    def test_rollback_button_hidden_until_there_is_somewhere_to_go(self):
        self.assertFalse(self.w.btn_rollback.isVisible())
        self.w.status.setPlainText(PINNED)
        self.w._update_pin_banner()
        self.assertFalse(self.w.btn_rollback.isVisible())

    def test_rollback_button_appears_on_the_status_marker(self):
        self.w.status.setPlainText(
            PINNED + "    rollback available: running 6.12.103_1-cachy, "
                     "known-good 6.12.95_1\n")
        self.w._update_pin_banner()
        self.assertTrue(self.w.btn_rollback.isVisible())

    def test_rollback_asks_before_acting(self):
        """It changes what the machine boots — never on a single click."""
        self.calls.clear()
        self.w.rollback()
        self.assertEqual(self.calls[0][0], ["--rollback"])
        self.assertIn("known-good", self.calls[0][1]["confirm"])

    def test_pin_button_label_escapes_its_ampersand(self):
        """A lone '&' is a Qt mnemonic: "Review & pin…" rendered as
        "Review _pin…" until it was escaped (found by looking at a render)."""
        raw = self.w.btn_pin.text()
        self.assertNotIn("&", raw.replace("&&", ""))

    def test_window_paints_the_brand_palette(self):
        """The window must not depend on the ambient Qt theme — it inherited a
        light Fusion look whenever the platform theme wasn't loaded (over SSH,
        or on a box installed without --with-branding)."""
        sheet = self.w.styleSheet()
        for token in ("#1b1d1e", "#282c34", "#abb2bf", "#478061", "#967940"):
            self.assertIn(token, sheet)          # branding.md §2 palette

    def test_status_pane_outweighs_the_output_pane(self):
        """The pending-status pane is the content of this window; it was being
        squeezed into a scrolled sliver beneath a mostly-empty Output box."""
        layout = self.w.layout()
        stretches = [layout.stretch(i) for i in range(layout.count())]
        self.assertGreater(max(stretches), 0)
        # the status group carries the largest stretch of any child
        idx = next(i for i in range(layout.count())
                   if layout.itemAt(i).widget() is not None
                   and layout.itemAt(i).widget().findChild(type(self.w.status))
                   is self.w.status)
        self.assertEqual(layout.stretch(idx), max(stretches))

    def test_pin_button_joins_the_busy_lock(self):
        self.w._busy(True)
        self.assertFalse(self.w.btn_pin.isEnabled())
        self.w._busy(False)
        self.assertTrue(self.w.btn_pin.isEnabled())


@unittest.skipUnless(HAVE_QT, "PyQt5 not installed")
class HelpTextTests(unittest.TestCase):
    """The "i" button explains the update model. Its text is deliberately
    GENERAL so that adding packages to the overlay can never make it wrong —
    that property is asserted here, not left to good intentions."""

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachygui", str(GUI_PATH))
        spec = importlib.util.spec_from_loader("cachygui", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.mod.Updater._run = lambda s, args, target, **kw: self.calls.append(
            (list(args), kw))
        self.w = self.mod.Updater()
        self.w.show()

    def tearDown(self):
        dlg = getattr(self.w, "_help_dlg", None)
        if dlg is not None:
            dlg.close()
        self.w.deleteLater()

    def test_covers_every_tier_the_status_pane_prints(self):
        html = self.mod.HELP_HTML
        for tier in ("[1]", "[2]", "[3]", "[4]", "[5]", "[6]"):
            self.assertIn(tier, html)

    def test_frames_the_project_as_an_overlay_not_a_distro(self):
        """It is an overlay FOR Void — it does not ship Void. An early draft
        said 'stock Void Linux plus a performance overlay', which reads as a
        distro handing you Void a second time."""
        html = self.mod.HELP_HTML
        self.assertIn("performance overlay for Void Linux", html)
        self.assertNotIn("plus a performance overlay", html)

    def test_names_the_identity_specifics(self):
        html = self.mod.HELP_HTML
        for term in ("BORE", "1000", "-O3", "x86-64-v"):
            self.assertIn(term, html)

    def test_names_no_individual_packages(self):
        """Package-level detail would need maintaining on every overlay change;
        the text describes KINDS of things instead (owner's requirement)."""
        html = self.mod.HELP_HTML.lower()
        for pkg in ("mesa", "wine", "gamemode", "mangohud", "vkbasalt",
                    "gamescope", "zramen", "earlyoom", "networkmanager",
                    "nm-tray", "snooze", "xtools"):
            self.assertNotIn(pkg, html, f"help text names the package {pkg!r}")

    def test_says_the_overlay_itself_is_updated_elsewhere(self):
        self.assertIn("re-running its installer", self.mod.HELP_HTML)

    def test_help_stays_available_while_a_command_runs(self):
        """The likeliest moment to ask "what is it doing?" is mid-run, and
        reading cannot mutate anything — so help is outside the busy-lock."""
        self.assertNotIn(self.w.btn_help, self.w._buttons)
        self.w._busy(True)
        self.assertTrue(self.w.btn_help.isEnabled())
        self.w._busy(False)

    def test_repeated_clicks_reuse_one_dialog(self):
        self.w.show_help()
        first = self.w._help_dlg
        self.w.show_help()
        self.assertIs(self.w._help_dlg, first)

    def test_opening_help_spawns_no_process(self):
        self.calls.clear()
        self.w.btn_help.click()
        self.assertEqual(self.calls, [])


@unittest.skipUnless(HAVE_QT, "PyQt5 not installed")
class ActionWiringTests(unittest.TestCase):
    """The update buttons must keep syncing first and scoping the kernel."""

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachygui", str(GUI_PATH))
        spec = importlib.util.spec_from_loader("cachygui", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.calls = []
        self.mod.Updater._run = lambda s, args, target, **kw: self.calls.append(
            (list(args), kw))
        self.w = self.mod.Updater()
        # The window must actually be shown (offscreen): Qt reports isVisible()
        # False for every child of an unshown parent, which would make the
        # banner assertions pass vacuously.
        self.w.show()

    def tearDown(self):
        self.w.deleteLater()

    def test_update_button_syncs_first_and_skips_kernel(self):
        self.calls.clear()
        self.w.update_userspace()
        self.assertEqual(self.calls[0][0], ["--sync"])
        # the commit leg is only reached from the sync's done callback
        done = self.calls[0][1]["done"]
        self.calls.clear()
        done(0)
        self.assertIn("--no-kernel", self.calls[0][0])

    def test_failed_sync_aborts_the_chain(self):
        self.w.update_userspace()
        done = self.calls[0][1]["done"]
        self.calls.clear()
        done(1)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
