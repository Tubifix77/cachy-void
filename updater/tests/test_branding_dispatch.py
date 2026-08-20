"""Unit tests for cachy-branding's dispatch: which appliers run, and why.

This is what `--dry-run` was built for. The appliers themselves need a desktop, a
session and installed assets, so they can only be exercised on real hardware — but
the *decision* about which of them to run is pure logic, and getting it wrong is
the failure that matters: a desktop gets another desktop's config written at it, or
the machine's only desktop gets nothing.

So these drive the real script with CACHY_DE_ROOT pointed at a fake filesystem and
HOME pointed at a temp dir, and assert both the decision and that nothing is
written. No desktop, no assets, no session required.
"""
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "system" / "bin" / "cachy-branding"
BINDIR = ROOT / "system" / "bin"
BASH = shutil.which("bash")


@unittest.skipUnless(BASH, "needs bash to run the applier")
@unittest.skipUnless(SCRIPT.is_file(), "cachy-branding not found")
class BrandingDispatchTests(unittest.TestCase):

    def setUp(self):
        self.fake = Path(tempfile.mkdtemp())          # fake / for the detector
        self.home = Path(tempfile.mkdtemp())          # HOME that must stay empty
        (self.fake / "usr/bin").mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.fake, ignore_errors=True)
        shutil.rmtree(self.home, ignore_errors=True)

    # --- helpers ---------------------------------------------------------
    def desktop(self, *names):
        for n in names:
            p = self.fake / "usr/bin" / n
            p.write_text("#!/bin/sh\n")
            p.chmod(0o755)

    def sh(self, *args):
        env = dict(os.environ)
        env["CACHY_DE_ROOT"] = str(self.fake)
        env["CACHY_DE_PATH"] = "/usr/bin"
        env["HOME"] = str(self.home)
        env["PATH"] = str(BINDIR) + os.pathsep + env.get("PATH", "")
        env.pop("XDG_CURRENT_DESKTOP", None)
        return subprocess.run([BASH, str(SCRIPT), "--dry-run", *args],
                              env=env, capture_output=True, text=True)

    def assertHomeUntouched(self):
        left = [p for p in self.home.rglob("*")]
        self.assertEqual(left, [], "a dry run wrote into HOME: %s" % left)

    # --- the decision ----------------------------------------------------
    def test_single_desktop_runs_only_its_appliers(self):
        self.desktop("lxqt-session")
        out = self.sh().stdout
        self.assertIn("would brand: lxqt", out)
        self.assertIn("apply_lxqt", out)
        self.assertIn("apply_openbox_session", out)
        self.assertNotIn("cachy-branding-plasma", out)
        self.assertHomeUntouched()

    def test_two_desktops_run_both_appliers(self):
        self.desktop("lxqt-session", "plasmashell")
        out = self.sh().stdout
        self.assertIn("apply_lxqt", out)
        self.assertIn("cachy-branding-plasma", out)

    def test_plasma_only_does_not_write_lxqt_config(self):
        """The whole point of the split: a Plasma-only target must not drag the
        LXQt session appliers along with it."""
        self.desktop("lxqt-session", "plasmashell")
        out = self.sh("--desktop", "plasma").stdout
        self.assertIn("cachy-branding-plasma", out)
        self.assertNotIn("apply_lxqt", out)
        self.assertNotIn("apply_openbox_session", out)

    def test_shared_appliers_always_run(self):
        """Tier 1 is unconditional - a desktop with no applier still gets assets."""
        self.desktop("xfce4-session")           # tier 1, no applier of its own
        out = self.sh().stdout
        self.assertIn("apply_shared", out)

    def test_desktop_flag_beats_the_detector(self):
        self.desktop("lxqt-session")            # detector would say lxqt
        out = self.sh("--desktop", "plasma").stdout
        self.assertIn("the --de/--desktop flag", out)
        self.assertIn("would brand: plasma", out)

    def test_de_is_an_alias_for_desktop(self):
        self.desktop("lxqt-session")
        self.assertIn("would brand: plasma", self.sh("--de", "plasma").stdout)

    def test_auto_means_resolve_normally(self):
        self.desktop("lxqt-session", "plasmashell")
        auto = self.sh("--desktop", "auto").stdout
        plain = self.sh().stdout
        self.assertIn("cachy-de-detect", auto)          # not "the flag"
        self.assertEqual(
            [l for l in auto.splitlines() if l.startswith("would brand")],
            [l for l in plain.splitlines() if l.startswith("would brand")])

    def test_comma_and_space_lists_both_work(self):
        self.desktop("lxqt-session", "plasmashell")
        for spec in ("lxqt,plasma", "lxqt plasma"):
            out = self.sh("--desktop", spec).stdout
            self.assertIn("apply_lxqt", out, spec)
            self.assertIn("cachy-branding-plasma", out, spec)

    def test_shell_is_opt_in(self):
        self.desktop("lxqt-session")
        self.assertIn("skipped (no --shell given)", self.sh().stdout)
        self.assertIn("zsh (opt-in)", self.sh("--shell", "zsh").stdout)

    def test_unknown_target_is_reported_not_silently_dropped(self):
        self.desktop("lxqt-session")
        cp = self.sh("--desktop", "nosuchdesktop")
        self.assertIn("unknown branding target", cp.stdout + cp.stderr)

    # --- the safety properties ------------------------------------------
    def test_dry_run_reports_missing_assets_instead_of_dying(self):
        """A real run stops without assets; the dry run has to keep going, or it
        could never be tested anywhere the assets are not installed."""
        self.desktop("lxqt-session")
        cp = self.sh()
        self.assertEqual(cp.returncode, 0)
        self.assertIn("MISSING", cp.stdout)

    def test_dry_run_writes_nothing_even_with_a_full_target_list(self):
        self.desktop("lxqt-session", "plasmashell", "openbox")
        self.sh("--desktop", "lxqt,plasma", "--shell", "zsh")
        self.assertHomeUntouched()

    def test_dry_run_says_where_the_decision_came_from(self):
        self.desktop("lxqt-session")
        self.assertIn("resolved from", self.sh().stdout)


if __name__ == "__main__":
    unittest.main()
