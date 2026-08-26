"""Unit tests for cachy-de-detect — the desktop detector (branding.md §6).

The detector decides which branding applier runs, from deploy.sh at install time
and from cachy-branding at apply time. Getting it wrong is not cosmetic: it means
a desktop gets someone else's config written at it, or the machine's only desktop
gets nothing.

It is a shell script, so these tests drive the real script — no reimplementation.
CACHY_DE_ROOT and CACHY_DE_PATH exist for exactly this reason: every case below
is a fake filesystem, so the whole matrix (one desktop, several, none, a session
file with no binary, window managers we deliberately skip) is exercised on a
machine with no desktop installed at all.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "system" / "bin" / "cachy-de-detect"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "needs bash to run the detector")
@unittest.skipUnless(SCRIPT.is_file(), "cachy-de-detect not found")
class DeDetectTests(unittest.TestCase):

    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "usr/share/xsessions").mkdir(parents=True)
        (self.root / "usr/share/wayland-sessions").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # --- helpers ---------------------------------------------------------
    def bin(self, *names):
        for n in names:
            p = self.root / "usr/bin" / n
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

    def session(self, name, wayland=False):
        d = "wayland-sessions" if wayland else "xsessions"
        (self.root / "usr/share" / d / name).write_text("[Desktop Entry]\n")

    def sh(self, *args, current=None):
        env = dict(os.environ)
        env["CACHY_DE_ROOT"] = str(self.root)
        env["CACHY_DE_PATH"] = "/usr/bin"
        env.pop("XDG_CURRENT_DESKTOP", None)
        if current is not None:
            env["XDG_CURRENT_DESKTOP"] = current
        cp = subprocess.run([BASH, str(SCRIPT), *args], env=env,
                            capture_output=True, text=True)
        return cp

    def ids(self, out):
        return [ln.split("\t")[0] for ln in out.strip().splitlines() if ln.strip()]

    # --- detection -------------------------------------------------------
    def test_nothing_installed_detects_nothing(self):
        cp = self.sh()
        self.assertEqual(cp.stdout.strip(), "")
        self.assertEqual(self.sh("--appliers").stdout.strip(), "")

    def test_single_desktop_is_found_with_its_applier(self):
        self.bin("lxqt-session")
        rows = self.sh().stdout.strip().splitlines()
        self.assertEqual(len(rows), 1)
        id_, label, applier, tier, state, ev = rows[0].split("\t")
        self.assertEqual((id_, applier, tier), ("lxqt", "lxqt", "2"))
        self.assertTrue(ev.startswith("bin:"))

    def test_binary_absent_but_session_file_present_still_counts(self):
        # a desktop can be installed while its session binary is not on our PATH
        self.session("xfce.desktop")
        rows = self.sh().stdout.strip().splitlines()
        self.assertEqual(self.ids("\n".join(rows)), ["xfce"])
        self.assertIn("session:", rows[0])

    def test_wayland_only_session_is_detected(self):
        self.session("plasma.desktop", wayland=True)
        self.assertEqual(self.ids(self.sh().stdout), ["plasma"])

    # --- the choice deploy.sh keys off ------------------------------------
    def test_one_applier_when_only_lxqt(self):
        self.bin("lxqt-session")
        self.assertEqual(self.sh("--appliers").stdout.split(), ["lxqt"])

    def test_openbox_folds_into_the_lxqt_applier(self):
        """Openbox is branded BY the LXQt applier, so two desktops must not
        become two questions — deploy.sh would ask about one piece of work."""
        self.bin("lxqt-session", "openbox")
        self.assertEqual(self.ids(self.sh().stdout), ["lxqt", "openbox"])
        self.assertEqual(self.sh("--appliers").stdout.split(), ["lxqt"])

    def test_two_appliers_when_lxqt_and_plasma(self):
        self.bin("lxqt-session", "openbox", "plasmashell")
        self.assertEqual(sorted(self.sh("--appliers").stdout.split()),
                         ["lxqt", "plasma"])

    def test_xfce_is_a_tier2_target(self):
        """Xfce has its own applier now, so it must be offered as a choice —
        the inverse of the rule it used to illustrate: a desktop we CAN brand
        has to appear, or the user is never asked about work we can do."""
        self.bin("xfce4-session")
        self.assertEqual(self.sh("--appliers").stdout.split(), ["xfce"])
        self.assertIn("xfce", self.ids(self.sh().stdout))

    def test_tier1_desktop_offers_no_applier(self):
        """Tier 1 is still a supported outcome — shared assets, no applier. It
        must not show up as something to choose, or the user is promised
        integration we do not have. MATE stands in for the class now that Xfce
        has graduated to tier 2."""
        self.bin("mate-session")
        self.assertEqual(self.sh("--appliers").stdout.strip(), "")
        self.assertIn("mate", self.ids(self.sh().stdout))

    def test_three_appliers_when_all_three_desktops_are_present(self):
        """The stated ceiling, exercised: lxqt, plasma and xfce each resolve to
        their own applier and none of them absorbs another."""
        self.bin("lxqt-session", "plasmashell", "xfce4-session")
        self.assertEqual(sorted(self.sh("--appliers").stdout.split()),
                         ["lxqt", "plasma", "xfce"])

    def test_window_managers_are_recognised_but_never_targets(self):
        """i3/sway/dwm are a decided 'no' (rejected-ideas), not an oversight:
        they are reported so the user sees they were seen, at tier 0."""
        self.bin("i3", "sway", "dwm")
        rows = {r.split("\t")[0]: r.split("\t")[3] for r in self.sh().stdout.strip().splitlines()}
        self.assertEqual(rows, {"i3": "0", "sway": "0", "dwm": "0"})
        self.assertEqual(self.sh("--appliers").stdout.strip(), "")

    # --- current session --------------------------------------------------
    def test_current_from_xdg_current_desktop(self):
        self.bin("lxqt-session", "plasmashell")
        self.assertEqual(self.sh("--current", current="KDE").stdout.strip(), "plasma")
        self.assertEqual(self.sh("--current", current="LXQt").stdout.strip(), "lxqt")

    def test_current_handles_colon_list_and_case(self):
        """XDG_CURRENT_DESKTOP is a colon-separated list and its case varies."""
        self.bin("plasmashell")
        self.assertEqual(self.sh("--current", current="KDE:plasma").stdout.strip(), "plasma")
        self.assertEqual(self.sh("--current", current="lxqt").stdout.strip(), "lxqt")

    def test_current_marks_the_running_desktop_in_the_listing(self):
        self.bin("lxqt-session", "plasmashell")
        rows = {r.split("\t")[0]: r.split("\t")[4]
                for r in self.sh(current="KDE").stdout.strip().splitlines()}
        self.assertEqual(rows["plasma"], "current")
        self.assertEqual(rows["lxqt"], "installed")

    def test_current_session_applier_is_offered_first(self):
        """deploy.sh defaults the prompt to the running session, so order matters."""
        self.bin("lxqt-session", "plasmashell")
        self.assertEqual(self.sh("--appliers", current="KDE").stdout.split()[0], "plasma")
        self.assertEqual(self.sh("--appliers", current="LXQt").stdout.split()[0], "lxqt")

    def test_unknown_current_desktop_is_not_an_error_for_the_listing(self):
        self.bin("lxqt-session")
        cp = self.sh("--current", current="Enlightenment")
        self.assertNotEqual(cp.returncode, 0)
        self.assertEqual(self.ids(self.sh(current="Enlightenment").stdout), ["lxqt"])

    # --- mapping ----------------------------------------------------------
    def test_applier_of(self):
        self.assertEqual(self.sh("--applier-of", "openbox").stdout.strip(), "lxqt")
        self.assertEqual(self.sh("--applier-of", "plasma").stdout.strip(), "plasma")
        self.assertEqual(self.sh("--applier-of", "xfce").stdout.strip(), "xfce")
        # "-" is still the answer for a tier-1 desktop, which MATE now is
        self.assertEqual(self.sh("--applier-of", "mate").stdout.strip(), "-")
        self.assertNotEqual(self.sh("--applier-of", "nosuchde").returncode, 0)

    def test_summary_is_human_readable(self):
        self.bin("lxqt-session", "plasmashell", "i3")
        out = self.sh("--summary", current="LXQt").stdout
        self.assertIn("RUNNING NOW", out)
        self.assertIn("not a branding target", out)
        self.assertIn("lxqt", out)

    def test_bad_option_exits_nonzero(self):
        self.assertEqual(self.sh("--nonsense").returncode, 2)


if __name__ == "__main__":
    unittest.main()
