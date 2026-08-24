"""Unit tests for the Kernel Injection State Manager (architecture.md §8)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

from engine import grub
from engine.grub import (
    GrubError, KernelStateStore, classify_bump, parse_template_pkgver,
    g2_config_gate, parse_menu_entries, resolve_menu_ref, detect_boot_layout,
    stage_candidate, locate_dotconfig, BootLayout,
    MODE_ONESHOT, MODE_MANUAL, MODE_MANUAL_UNSAFE, MODE_SKIP,
)


def cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def _vercmp(a, b):
    def key(v):
        ver, _, rev = v.partition("_")
        return (tuple(int(x) for x in ver.split(".")), int(rev or 0))
    return (key(a) > key(b)) - (key(a) < key(b))


GRUB_CFG = """
menuentry 'Void Linux' $menuentry_id_option 'gnulinux-simple-UUID' {
}
submenu 'Advanced options' $menuentry_id_option 'gnulinux-advanced-UUID' {
	menuentry 'Void, with Linux 6.12.35_1' $menuentry_id_option 'gnulinux-6.12.35_1-advanced-UUID' {
	}
	menuentry 'Void, with Linux 6.12.34_1' $menuentry_id_option 'gnulinux-6.12.34_1-advanced-UUID' {
	}
}
"""

FRAGMENT = """
# --- Cachy-Void overrides ---
CONFIG_SCHED_BORE=y
CONFIG_HZ_1000=y
# CONFIG_HZ_250 is not set
CONFIG_PREEMPT=y
"""


class TemplateParseTests(unittest.TestCase):

    def test_parse_pkgver(self):
        self.assertEqual(parse_template_pkgver("version=6.12.34\nrevision=2\n"),
                         "6.12.34_2")

    def test_parse_pkgver_bad(self):
        with self.assertRaises(GrubError):
            parse_template_pkgver("no version here")


class ClassifyBumpTests(unittest.TestCase):

    def test_none_when_equal(self):
        ev, tmpl = classify_bump(series_template_text="version=6.12.34\nrevision=1\n",
                                 ported_version="6.12.34_1", vercmp=_vercmp)
        self.assertEqual(ev, grub.EV_NONE)
        self.assertEqual(tmpl, "6.12.34_1")

    def test_patchlevel_when_newer(self):
        ev, _ = classify_bump(series_template_text="version=6.12.35\nrevision=1\n",
                              ported_version="6.12.34_1", vercmp=_vercmp)
        self.assertEqual(ev, grub.EV_BUMP_PATCHLEVEL)

    def test_revision_only_bump_counts(self):
        ev, _ = classify_bump(series_template_text="version=6.12.34\nrevision=2\n",
                              ported_version="6.12.34_1", vercmp=_vercmp)
        self.assertEqual(ev, grub.EV_BUMP_PATCHLEVEL)

    def test_series_gone(self):
        ev, tmpl = classify_bump(series_template_text=None,
                                 ported_version="6.12.34_1", vercmp=_vercmp)
        self.assertEqual(ev, grub.EV_AWAIT_HUMAN_SERIES)
        self.assertIsNone(tmpl)


class G2GateTests(unittest.TestCase):

    def test_all_present_passes(self):
        dotconfig = ("CONFIG_SCHED_BORE=y\nCONFIG_HZ_1000=y\n"
                     "# CONFIG_HZ_250 is not set\nCONFIG_PREEMPT=y\n")
        ok, missing = g2_config_gate(dotconfig, FRAGMENT)
        self.assertTrue(ok, missing)
        self.assertEqual(missing, [])

    def test_silently_dropped_symbol_is_caught(self):
        # oldconfig dropped CONFIG_SCHED_BORE entirely -> must be flagged.
        dotconfig = "CONFIG_HZ_1000=y\n# CONFIG_HZ_250 is not set\nCONFIG_PREEMPT=y\n"
        ok, missing = g2_config_gate(dotconfig, FRAGMENT)
        self.assertFalse(ok)
        self.assertIn("CONFIG_SCHED_BORE=y", missing)

    def test_not_set_satisfied_by_absence(self):
        ok, _ = g2_config_gate("CONFIG_X=y\n", "# CONFIG_Y is not set\n")
        self.assertTrue(ok)

    def test_not_set_violated_when_symbol_is_set(self):
        ok, missing = g2_config_gate("CONFIG_Y=y\n", "# CONFIG_Y is not set\n")
        self.assertFalse(ok)
        self.assertIn("# CONFIG_Y is not set", missing)

    def test_wrong_value_is_missing(self):
        ok, missing = g2_config_gate("CONFIG_HZ_1000=m\n", "CONFIG_HZ_1000=y\n")
        self.assertFalse(ok)
        self.assertIn("CONFIG_HZ_1000=y", missing)


class MenuRefTests(unittest.TestCase):

    def test_parse_counts_all_entries(self):
        entries = parse_menu_entries(GRUB_CFG)
        ids = [eid for _, eid in entries]
        self.assertIn("gnulinux-simple-UUID", ids)
        self.assertEqual(len(entries), 3)

    def test_resolve_nested_ref(self):
        ref = resolve_menu_ref(GRUB_CFG, "6.12.35_1")
        self.assertEqual(
            ref, "gnulinux-advanced-UUID>gnulinux-6.12.35_1-advanced-UUID")

    def test_zero_match_raises(self):
        with self.assertRaises(GrubError):
            resolve_menu_ref(GRUB_CFG, "9.9.9_9")

    def test_multiple_match_raises(self):
        dupe = GRUB_CFG + (
            "menuentry 'dup' $menuentry_id_option 'gnulinux-6.12.35_1-extra' {\n}\n")
        with self.assertRaises(GrubError):
            resolve_menu_ref(dupe, "6.12.35_1")


class BootLayoutTests(unittest.TestCase):

    def test_wsl_skips(self):
        lo = detect_boot_layout(wsl=True)
        self.assertEqual(lo.mode, MODE_SKIP)
        self.assertIn("WSL2", lo.reason)

    def test_missing_grub_cfg_is_external_not_skip(self):
        # A real machine whose bootloader we don't own (another distro's GRUB
        # chain-boots Void) must get BOOKKEEPING staging, not a silent skip —
        # lumping it into skip meant healthy boots were never promoted (first
        # real-hardware kernel run, Debian-owned GRUB).
        lo = detect_boot_layout(wsl=False, exists=lambda p: False)
        self.assertEqual(lo.mode, grub.MODE_EXTERNAL)
        self.assertIn("foreign boot manager", lo.reason)

    def test_wsl_beats_external(self):
        # WSL has no bootable kernel path at all: skip, never external.
        lo = detect_boot_layout(wsl=True, exists=lambda p: False)
        self.assertEqual(lo.mode, MODE_SKIP)

    def test_unsafe_fs_with_saved_default_is_safe_manual(self):
        # btrfs cannot host a one-shot, but the saved default still pins the
        # known-good fallback -> safe manual, NOT unsafe.
        with tempfile.TemporaryDirectory() as d:
            dg = Path(d) / "grub"
            dg.write_text("GRUB_DEFAULT=saved\n", encoding="utf-8")
            lo = detect_boot_layout(wsl=False, exists=lambda p: True,
                                    default_grub=str(dg),
                                    run=lambda a: cp(stdout="btrfs\n"))
            self.assertEqual(lo.mode, MODE_MANUAL)

    def test_grub_default_not_saved_is_manual_unsafe(self):
        # GRUB_DEFAULT!=saved: pinning would be a silent no-op -> unsafe class,
        # regardless of how friendly the filesystem is.
        with tempfile.TemporaryDirectory() as d:
            dg = Path(d) / "grub"
            dg.write_text("GRUB_DEFAULT=0\n", encoding="utf-8")
            lo = detect_boot_layout(wsl=False, exists=lambda p: True,
                                    default_grub=str(dg),
                                    run=lambda a: cp(stdout="ext4\n"))
            self.assertEqual(lo.mode, MODE_MANUAL_UNSAFE)
            self.assertIn("deploy.sh --with-grub", lo.reason)

    def test_findmnt_missing_degrades_to_safe_manual(self):
        # Minimal environments without findmnt must degrade the mode,
        # never leak the exception (audit F6).
        def boom(args):
            raise FileNotFoundError("findmnt")
        with tempfile.TemporaryDirectory() as d:
            dg = Path(d) / "grub"
            dg.write_text("GRUB_DEFAULT=saved\n", encoding="utf-8")
            lo = detect_boot_layout(wsl=False, exists=lambda p: True,
                                    default_grub=str(dg), run=boom)
            self.assertEqual(lo.mode, MODE_MANUAL)

    def test_oneshot_when_ext4_and_saved(self):
        with tempfile.TemporaryDirectory() as d:
            dg = Path(d) / "grub"
            dg.write_text("GRUB_DEFAULT=saved\n", encoding="utf-8")
            lo = detect_boot_layout(
                wsl=False, exists=lambda p: True, default_grub=str(dg),
                run=lambda a: cp(stdout="ext4\n"))
            self.assertEqual(lo.mode, MODE_ONESHOT)


class StagingTests(unittest.TestCase):

    def _layout(self, mode):
        return BootLayout(mode, "test", grub_cfg="/boot/grub/grub.cfg")

    def test_skip_mode_no_actions(self):
        res = stage_candidate(layout=self._layout(MODE_SKIP),
                              candidate_kver="6.12.35_1", known_good_kver="6.12.34_1")
        self.assertEqual(res.mode, MODE_SKIP)
        self.assertEqual(res.actions, [])

    def test_manual_unsafe_is_refused_with_no_actions(self):
        # Pinning under GRUB_DEFAULT!=saved is a silent no-op; staging must
        # refuse rather than give false assurance (audit F3).
        res = stage_candidate(layout=self._layout(MODE_MANUAL_UNSAFE),
                              candidate_kver="6.12.35_1", known_good_kver="6.12.34_1")
        self.assertEqual(res.mode, MODE_MANUAL_UNSAFE)
        self.assertEqual(res.actions, [])

    def test_external_mode_bookkeeping_only(self):
        # Foreign bootloader: zero commands, but the mode is preserved so the
        # caller records the candidate for §8.7 confirm/promotion.
        res = stage_candidate(layout=self._layout(grub.MODE_EXTERNAL),
                              candidate_kver="6.12.103_1-cachy",
                              known_good_kver="6.12.95_1-cachy")
        self.assertEqual(res.mode, grub.MODE_EXTERNAL)
        self.assertEqual(res.actions, [])
        self.assertIsNone(res.candidate_ref)

    def test_external_promote_returns_none_no_commands(self):
        calls = []
        def run(a, c=None):
            calls.append(list(a))
            return cp()
        ref = grub.promote(layout=self._layout(grub.MODE_EXTERNAL),
                           candidate_kver="6.12.103_1-cachy", run=run)
        self.assertIsNone(ref)
        self.assertEqual(calls, [])   # never touches the foreign bootloader

    def test_manual_mode_sets_default_only(self):
        calls = []
        def run(a, c=None):
            calls.append(a)
            return cp()
        res = stage_candidate(layout=self._layout(MODE_MANUAL),
                              candidate_kver="6.12.35_1", known_good_kver="6.12.34_1",
                              run=run, read_text=lambda p: GRUB_CFG)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "grub-set-default")

    def test_oneshot_sets_default_and_reboot(self):
        calls = []
        def run(a, c=None):
            calls.append(a)
            return cp()
        res = stage_candidate(layout=self._layout(MODE_ONESHOT),
                              candidate_kver="6.12.35_1", known_good_kver="6.12.34_1",
                              run=run, read_text=lambda p: GRUB_CFG)
        cmds = [c[0] for c in calls]
        self.assertEqual(cmds, ["grub-set-default", "grub-reboot"])
        self.assertIn("6.12.34", calls[0][1])   # default = known good
        self.assertIn("6.12.35", calls[1][1])   # one-shot = candidate

    def test_oneshot_command_failure_raises(self):
        with self.assertRaises(GrubError):
            stage_candidate(layout=self._layout(MODE_ONESHOT),
                            candidate_kver="6.12.35_1", known_good_kver="6.12.34_1",
                            run=lambda a, c=None: cp(returncode=1, stderr="boom"),
                            read_text=lambda p: GRUB_CFG)


class LocateDotconfigTests(unittest.TestCase):
    """F8: the generated .config must be located deterministically."""

    def _mk(self, root, masterdir, series):
        d = Path(root) / masterdir / "builddir" / series
        d.mkdir(parents=True)
        (d / ".config").write_text("CONFIG_X=y\n", encoding="utf-8")
        return d / ".config"

    def test_exactly_one_match(self):
        with tempfile.TemporaryDirectory() as d:
            expected = self._mk(d, "masterdir-x86_64", "linux-6.12.35")
            self.assertEqual(locate_dotconfig(d), expected)

    def test_zero_matches_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(GrubError):
                locate_dotconfig(d)

    def test_multiple_matches_raise(self):
        # Stale builddirs could feed the wrong config -> hard failure.
        with tempfile.TemporaryDirectory() as d:
            self._mk(d, "masterdir-x86_64", "linux-6.12.35")
            self._mk(d, "masterdir-stale", "linux-6.12.30")
            with self.assertRaises(GrubError):
                locate_dotconfig(d)


class KernelStateStoreTests(unittest.TestCase):

    def test_default_when_absent_then_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            store = KernelStateStore(Path(d) / "kernel-state.json")
            st = store.load()
            self.assertEqual(st["state"], "TRACKING")
            st["ported_version"] = "6.12.34_1"
            store.save(st)
            self.assertEqual(store.load()["ported_version"], "6.12.34_1")


if __name__ == "__main__":
    unittest.main()


class VerifyBootableTests(unittest.TestCase):
    """§8.6b — the read-only "can this kernel actually boot?" probe.

    The distinction these tests pin down: BOOT_ABSENT is a warning about the
    system, BOOT_UNKNOWN is an admission about us. Conflating them would either
    cry wolf on an unreadable file or stay silent on a genuinely missing entry.
    """

    def _grub_layout(self, mode=MODE_ONESHOT):
        return BootLayout(mode, "test", grub_cfg="/boot/grub/grub.cfg",
                          default_grub="/etc/default/grub")

    def test_grub_entry_present_is_ok(self):
        chk = grub.verify_bootable(layout=self._grub_layout(), kver="6.12.35_1",
                                   read_text=lambda p: GRUB_CFG)
        self.assertEqual(chk.status, grub.BOOT_OK)
        self.assertEqual(chk.mechanism, "grub-menu")
        self.assertEqual(chk.refs,
                         ["gnulinux-advanced-UUID>gnulinux-6.12.35_1-advanced-UUID"])
        self.assertEqual(chk.hint, "")          # nothing for the operator to do

    def test_grub_entry_missing_warns_and_names_the_hook(self):
        # The failure this exists to catch: kernel installed, 50-grub never ran
        # (or /boot was not mounted), so the menu cannot offer the new kernel.
        chk = grub.verify_bootable(layout=self._grub_layout(),
                                   kver="6.12.103_1-cachy",
                                   read_text=lambda p: GRUB_CFG)
        self.assertEqual(chk.status, grub.BOOT_ABSENT)
        self.assertIn("NO boot entry", chk.detail)
        self.assertIn("50-grub", chk.hint)
        self.assertIn("xbps-reconfigure", chk.hint)

    def test_unreadable_grub_cfg_is_unknown_never_absent(self):
        def boom(_p):
            raise PermissionError(13, "Permission denied")
        chk = grub.verify_bootable(layout=self._grub_layout(), kver="6.12.35_1",
                                   read_text=boom)
        self.assertEqual(chk.status, grub.BOOT_UNKNOWN)
        self.assertIn("could not read", chk.detail)
        self.assertNotIn("NO boot entry", chk.detail)

    def test_external_with_boot_symlink_is_ok(self):
        # The testbed's shape: a hand-made evergreen symlink the foreign menu
        # loads. Matching is by VERSION in the target, so any operator naming
        # convention is found — Void itself defines no such mechanism.
        entries = ["vmlinuz-6.12.103_1-cachy", "vmlinuz-current", "config-x", "grub"]
        links = {"/boot/vmlinuz-current": "vmlinuz-6.12.103_1-cachy"}

        def readlink(p):
            if p in links:
                return links[p]
            raise OSError(22, "Invalid argument")

        chk = grub.verify_bootable(layout=BootLayout(grub.MODE_EXTERNAL, "test"),
                                   kver="6.12.103_1-cachy",
                                   listdir=lambda d: entries, readlink=readlink)
        self.assertEqual(chk.status, grub.BOOT_OK)
        self.assertEqual(chk.mechanism, "boot-symlink")
        self.assertIn("vmlinuz-current -> vmlinuz-6.12.103_1-cachy", chk.detail)
        self.assertIn("OLDER", chk.hint)        # the multi-boot truth, said out loud

    def test_external_without_symlink_is_unknown_not_ok(self):
        # No symlink: the kernel exists only under its versioned name, and a
        # foreign menu entry naming a versioned path still boots the OLD one.
        # We cannot see that menu, so we must not claim either outcome.
        def readlink(_p):
            raise OSError(22, "Invalid argument")
        chk = grub.verify_bootable(layout=BootLayout(grub.MODE_EXTERNAL, "test"),
                                   kver="6.12.103_1-cachy",
                                   listdir=lambda d: ["vmlinuz-6.12.103_1-cachy"],
                                   readlink=readlink)
        self.assertEqual(chk.status, grub.BOOT_UNKNOWN)
        self.assertEqual(chk.mechanism, "versioned-only")
        self.assertIn("BEFORE rebooting", chk.hint)

    def test_external_symlink_for_a_different_kernel_does_not_count(self):
        # The stale-symlink trap: current still points at the OLD kernel, which
        # is precisely when the user must be told to look before rebooting.
        def readlink(p):
            if p == "/boot/vmlinuz-current":
                return "vmlinuz-6.12.95_1"
            raise OSError(22, "Invalid argument")
        chk = grub.verify_bootable(layout=BootLayout(grub.MODE_EXTERNAL, "test"),
                                   kver="6.12.103_1-cachy",
                                   listdir=lambda d: ["vmlinuz-current"],
                                   readlink=readlink)
        self.assertEqual(chk.status, grub.BOOT_UNKNOWN)
        self.assertEqual(chk.mechanism, "versioned-only")

    def test_unreadable_boot_dir_degrades_quietly(self):
        def boom(_d):
            raise PermissionError(13, "Permission denied")
        chk = grub.verify_bootable(layout=BootLayout(grub.MODE_EXTERNAL, "test"),
                                   kver="6.12.103_1-cachy",
                                   listdir=boom, readlink=lambda p: "x")
        self.assertEqual(chk.status, grub.BOOT_UNKNOWN)

    def test_wsl_skip_verifies_nothing(self):
        chk = grub.verify_bootable(layout=BootLayout(MODE_SKIP, "wsl"), kver="6.12.35_1")
        self.assertEqual(chk.status, grub.BOOT_UNKNOWN)
        self.assertEqual(chk.mechanism, "none")
        self.assertIn("nothing to verify", chk.detail)

    def test_manual_unsafe_still_checks_the_menu(self):
        # Staging REFUSES here, but the question "is the kernel in the menu?"
        # is still answerable and still worth answering.
        chk = grub.verify_bootable(layout=self._grub_layout(MODE_MANUAL_UNSAFE),
                                   kver="6.12.34_1", read_text=lambda p: GRUB_CFG)
        self.assertEqual(chk.status, grub.BOOT_OK)
