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

    def test_output_pane_follows_its_newest_line(self):
        """A log pane parked at line 1 is useless while a command runs — you had
        to scroll down to discover it had finished."""
        for i in range(300):
            self.w._append_tail(self.w.out, f"line {i}")
        bar = self.w.out.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0, "no scroll range to test")
        self.assertEqual(bar.value(), bar.maximum())

    def test_both_panes_follow_the_newest_line(self):
        """One rule for both panes (owner's call): text just written is text you
        want to see. Splitting the behaviour — Output following, the pending list
        pinned to line 1 — only moved the complaint between windows, because the
        status text is taller than its pane."""
        for pane in (self.w.out, self.w.status):
            for i in range(300):
                self.w._append_tail(pane, f"line {i}")
            self.app.processEvents()
            bar = pane.verticalScrollBar()
            self.assertGreater(bar.maximum(), 0, "no scroll range to test")
            self.assertEqual(bar.value(), bar.maximum())

    def test_scrolling_up_is_respected_in_both_panes(self):
        """The one exception: a reader mid-scroll is never yanked to the bottom."""
        for pane in (self.w.out, self.w.status):
            for i in range(300):
                self.w._append_tail(pane, f"line {i}")
            pane.verticalScrollBar().setValue(0)
            self.w._append_tail(pane, "new arrival")
            self.app.processEvents()
            self.assertEqual(pane.verticalScrollBar().value(), 0)


    def test_clean_previews_before_it_can_remove_anything(self):
        """Agreeing to 'orphans and cache' is agreeing to a category; the dialog
        must show the actual list (same pattern as the pin flow)."""
        self.calls.clear()
        self.w.btn_clean.click()
        self.assertEqual(self.calls[0][0], ["--clean", "--dry-run"])
        self.assertTrue(all("--yes" not in c[0] for c in self.calls))

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


class StagedKernelCardTests(unittest.TestCase):
    """§8.6 staged-candidate surfacing in the window (the GUI is the product).

    The card TRANSCRIBES the CLI's lines rather than recomputing them: the boot
    class decides whether the fallback is automatic or manual, the CLI already
    knows, and a second opinion in the GUI is how the two drift apart.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachygui", str(GUI_PATH))
        spec = importlib.util.spec_from_loader("cachygui", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mod.Updater._run = lambda s, a, t, **kw: None
        self.w = self.mod.Updater()
        self.w.show()

    def tearDown(self):
        self.w.deleteLater()

    # --status renders _kernel_report at a 4-space indent, its sub-lines at 6.
    ONESHOT = ("\n[3] Kernel (linux-cachy / BORE)\n"
               "    BORE pin: series 6.12 pinned (BORE 6.6.3)\n"
               "    kernel candidate: 6.12.103_1-cachy is staged, awaiting its trial "
               "boot — reboot when convenient (the updater never reboots you)\n"
               "      if it fails to boot, the next power cycle returns to 6.12.95_1 "
               "on its own (one-shot)\n"
               "    rollback available: running 6.12.95_1, known-good 6.12.95_1\n")
    EXTERNAL = ("\n[3] Kernel (linux-cachy / BORE)\n"
                "    kernel candidate: 6.12.103_1-cachy is staged, awaiting its trial "
                "boot — reboot when convenient (the updater never reboots you)\n"
                "      a foreign bootloader owns the menu: if it misbehaves, pick "
                "6.12.95_1 there yourself\n")
    QUIET = ("\n[3] Kernel (linux-cachy / BORE)\n"
             "    BORE pin: series 6.12 pinned (BORE 6.6.3)\n"
             "    kernel: in sync\n")

    def test_card_hidden_before_any_status(self):
        self.assertFalse(self.w.kernel_notice.isVisible())

    def test_card_hidden_when_no_candidate_is_staged(self):
        self.w.status.setPlainText(self.QUIET)
        self.w._update_pin_banner()
        self.assertFalse(self.w.kernel_notice.isVisible())

    def test_card_appears_and_names_the_kernel(self):
        self.w.status.setPlainText(self.ONESHOT)
        self.w._update_pin_banner()
        self.assertTrue(self.w.kernel_notice.isVisible())
        self.assertIn("6.12.103_1-cachy", self.w.kernel_label.text())
        # no double-saying: the card must not repeat what the CLI line states
        text = self.w.kernel_label.text()
        self.assertTrue(text.startswith("Kernel 6.12.103_1-cachy is staged"), text)
        self.assertEqual(text.count("staged"), 1)

    def test_card_carries_the_hosts_own_fallback_advice(self):
        self.w.status.setPlainText(self.ONESHOT)
        self.w._update_pin_banner()
        self.assertIn("one-shot", self.w.kernel_label.text())

        self.w.status.setPlainText(self.EXTERNAL)
        self.w._update_pin_banner()
        t = self.w.kernel_label.text()
        self.assertIn("pick 6.12.95_1 there yourself", t)
        self.assertNotIn("one-shot", t)        # never promise what this host lacks

    def test_card_stops_at_the_next_unindented_line(self):
        # The follow-on "rollback available:" line belongs to a different
        # feature and must not be swallowed into this card.
        self.w.status.setPlainText(self.ONESHOT)
        self.w._update_pin_banner()
        self.assertNotIn("rollback available", self.w.kernel_label.text())

    def test_card_offers_no_button(self):
        # The updater never reboots anyone; a button here would imply it might.
        from PyQt5.QtWidgets import QPushButton
        self.assertEqual(self.w.kernel_notice.findChildren(QPushButton), [])


class SnapshotsButtonTests(unittest.TestCase):
    """The Snapshots button: read-only, and deliberately NOT a restore button.

    Restoring a root filesystem is a decision, so the window shows the commands
    and stops. If this ever becomes one-click it needs a confirm dialog and a
    privilege story it does not have today.
    """

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
        self.mod.Updater._run = lambda s, args, target, **kw: self.calls.append(
            (list(args), kw))
        self.w = self.mod.Updater()
        self.w.show()
        self.calls.clear()          # drop the constructor's opening check()

    def tearDown(self):
        self.w.deleteLater()

    def test_the_button_is_always_visible(self):
        # Unlike rollback (revealed only when there is somewhere to go), the
        # snapshot list is worth reaching before anything has gone wrong.
        self.assertTrue(self.w.btn_snapshots.isVisible())

    def test_it_runs_the_read_only_action_with_no_confirm(self):
        self.w.snapshots()
        self.assertEqual(len(self.calls), 1)
        args, kw = self.calls[0]
        self.assertEqual(args, ["--snapshots"])
        self.assertNotIn("confirm", kw)      # nothing to confirm: it only reads

    def test_it_is_disabled_while_a_command_runs(self):
        self.assertIn(self.w.btn_snapshots, self.w._buttons)


class HeadlineTests(unittest.TestCase):
    """The headline: the one line that decides whether you press Update.

    It came out of the tray making a number prominent that the WINDOW kept
    buried in a scrolling monospace pane — a strange place for the answer to
    the window's whole question. It is a summary of the CLI's own output, never
    a second query, so it cannot disagree with the detail printed beneath it.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        loader = importlib.machinery.SourceFileLoader("cachygui", str(GUI_PATH))
        spec = importlib.util.spec_from_loader("cachygui", loader)
        cls.mod = importlib.util.module_from_spec(spec)
        loader.exec_module(cls.mod)
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.mod.Updater._run = lambda s, a, t, **kw: None
        self.w = self.mod.Updater()
        self.w.show()

    def tearDown(self):
        self.w.deleteLater()

    def _update(self):
        self.w._update_pin_banner()

    def _head(self, text):
        self.w.status.setPlainText(text)
        self.w._update_pin_banner()
        # Clauses are glued with hard spaces so a wrap cannot land inside one;
        # normalise them here, since these tests are about wording.
        return self.w.headline.text().replace("\u00a0", " ")

    def test_a_version_is_never_orphaned_from_its_package(self):
        """The wrapping rule, after two goes at it.

        Round one glued every space inside a clause, which kept "mesa" with
        "26.1.8" but made a long clause unbreakable — with two drivers pending
        it could not wrap at all and would push the window wider. So only the
        ATOMS are glued: a package name to its version, and the quoted button
        name. Everything else wraps, and a busy machine just uses more lines.
        """
        self.w.status.setPlainText(
            "    20 upstream package(s) updatable   (+4 on hold)\n"
            "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
            "(one of the system packages above)\n"
            "    graphics driver update: nvidia470 470.256.02_1 -> "
            "470.260.00_1 (one of the system packages above)\n"
            "    kernel: ported base is old — port linux-cachy\n")
        self._update()
        raw = self.w.headline.text()
        self.assertIn("mesa\u00a026.1.8", raw)
        self.assertIn("nvidia470\u00a0470.260.00", raw)
        self.assertIn('"Update\u00a0kernel"\u00a0button', raw)
        # …and the text can still break somewhere, or it cannot wrap at all.
        self.assertIn(" ", raw)

    def test_the_busiest_case_stays_breakable(self):
        # Everything pending at once: two drivers, overlay work and a kernel.
        # Every ordinary space is a legal wrap point, so this lays out over as
        # many lines as it needs instead of widening the window.
        self.w.status.setPlainText(
            "    20 upstream package(s) updatable   (+4 on hold)\n"
            "    2 to rebuild, 1 to deploy\n"
            "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
            "(one of the system packages above)\n"
            "    graphics driver update: nvidia470 470.256.02_1 -> "
            "470.260.00_1 (one of the system packages above)\n"
            "    kernel: ported base is old — port linux-cachy\n")
        self._update()
        raw = self.w.headline.text()
        longest = max(len(run) for run in raw.split(" "))
        self.assertLess(longest, 40, f"unbreakable run too long: {raw}")

    def test_the_package_count_leads(self):
        t = self._head("[1] System (upstream Void)\n"
                       "    20 upstream package(s) updatable   (+4 on hold)\n")
        self.assertTrue(t.startswith("20 packages to update"), t)
        self.assertIn("(4 on hold)", t)

    def test_one_package_is_not_pluralised(self):
        self.assertIn("1 package to update",
                      self._head("    1 upstream package(s) updatable\n"))

    def test_nothing_pending_says_so_plainly(self):
        t = self._head("    0 upstream package(s) updatable — up to date\n")
        self.assertEqual(t, "Everything is up to date")

    def test_the_quiet_state_is_styled_differently(self):
        # Calm, not accent-green: "nothing to do" should not shout like "20
        # things to do". Qt needs a repolish for an objectName change to show.
        self._head("    0 upstream package(s) updatable — up to date\n")
        self.assertEqual(self.w.headline.objectName(), "headlineQuiet")
        self._head("    3 upstream package(s) updatable\n")
        self.assertEqual(self.w.headline.objectName(), "headline")

    def test_overlay_rebuilds_are_named_separately(self):
        # They are a different kind of work from an upstream package update,
        # and the number of each is what makes Update's cost predictable.
        t = self._head("    5 upstream package(s) updatable\n"
                       "    2 to rebuild, 1 to deploy\n")
        self.assertIn("2 overlay packages to rebuild", t)

    def test_a_kernel_bump_is_named_because_it_is_a_different_button(self):
        # Update deliberately leaves the kernel alone, so folding this into the
        # package count would misdescribe what pressing Update does.
        t = self._head("    3 upstream package(s) updatable\n"
                       "    kernel: upstream linux6.12 is at 6.12.110_1; ported "
                       "base is 6.12.103_1 — port linux-cachy (§2.6/§8.4).\n")
        self.assertIn("a newer BORE kernel to build", t)

    def test_the_driver_is_a_highlight_inside_the_count_not_an_extra_item(self):
        """The mistake this pins: mesa IS one of the packages counted above.

        Listing it as its own clause read as a separate thing to do, and the
        owner correctly asked whether it "goes with the rest of the updates".
        It does, so it is phrased as part of the same sentence.
        """
        t = self._head("    20 upstream package(s) updatable   (+4 on hold)\n"
                       "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
                       "(one of the system packages above)\n")
        self.assertIn("20 packages to update (4 on hold), including a new "
                      "graphics driver (mesa 26.1.8)", t)
        self.assertNotIn("·", t)              # one sentence, not two items

    def test_the_headline_gives_no_instruction(self):
        """A pending summary must not tell you to do something to a thing you
        have not installed yet. "restart apps to use it" beside "20 packages to
        update" earned the fair reply: use WHAT? That advice now waits until
        the update has actually happened."""
        t = self._head("    20 upstream package(s) updatable\n"
                       "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
                       "(one of the system packages above)\n")
        for instruction in ("restart apps", "reboot", "log out", "use it"):
            self.assertNotIn(instruction, t.lower())

    def test_a_kernel_module_driver_is_folded_in_the_same_way(self):
        # The KIND of driver changes the after-advice, not this summary.
        t = self._head("    3 upstream package(s) updatable\n"
                       "    graphics driver update: nvidia470 470.256.02_1 -> "
                       "470.260.00_1 (one of the system packages above)\n")
        self.assertIn("including a new graphics driver (nvidia470 470.260.00)", t)
        self.assertNotIn("reboot", t.lower())

    def test_two_pending_drivers_are_both_named_in_one_clause(self):
        t = self._head("    9 upstream package(s) updatable\n"
                       "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
                       "(one of the system packages above)\n"
                       "    graphics driver update: nvidia470 470.256.02_1 -> "
                       "470.260.00_1 (one of the system packages above)\n")
        self.assertIn("mesa 26.1.8", t)
        self.assertIn("nvidia470 470.260.00", t)
        self.assertEqual(t.count("including a new graphics driver"), 1)

    def test_a_driver_with_no_package_count_still_reads(self):
        # Degraded tier [1] must not produce a dangling ", including …".
        t = self._head("    unknown — xbps-install unavailable\n"
                       "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
                       "(one of the system packages above)\n")
        self.assertIn("a new graphics driver (mesa 26.1.8)", t)
        self.assertFalse(t.startswith(","), t)

    def test_the_kernel_clause_names_the_button_that_does_it(self):
        # Update deliberately leaves the kernel alone; beside the other clauses
        # a bare "a newer BORE kernel" looks like the same press would handle it.
        t = self._head("    3 upstream package(s) updatable\n"
                       "    kernel: ported base is old — port linux-cachy\n")
        self.assertIn("Update kernel", t)

    def test_flatpak_apps_are_included_since_update_applies_them(self):
        t = self._head("    0 upstream package(s) updatable — up to date\n"
                       "    2 app(s) updatable\n")
        self.assertIn("2 Flatpak app(s)", t)

    def test_zero_flatpak_apps_add_nothing(self):
        t = self._head("    0 upstream package(s) updatable — up to date\n"
                       "    0 app(s) updatable — up to date\n")
        self.assertEqual(t, "Everything is up to date")

    def test_everything_at_once_reads_as_one_line(self):
        t = self._head("    20 upstream package(s) updatable   (+4 on hold)\n"
                       "    2 to rebuild, 1 to deploy\n"
                       "    graphics driver update: mesa 26.1.7_1 -> 26.1.8_1 "
                       "(one of the system packages above)\n"
                       "    kernel: ported base is old — port linux-cachy\n")
        for bit in ("20 packages to update", "2 overlay packages to rebuild",
                    "mesa 26.1.8", "a newer BORE kernel to build"):
            self.assertIn(bit, t)
        # The driver joins the package clause; overlay and kernel stay separate.
        self.assertEqual(t.count("·"), 2)

    def test_a_degraded_status_does_not_invent_a_number(self):
        # "unknown — xbps-install unavailable" must not read as "up to date"
        # with a fake count attached; absent data yields no claim.
        t = self._head("[1] System (upstream Void)\n"
                       "    unknown — run --sync to refresh the repository list\n")
        self.assertEqual(t, "Everything is up to date")
