"""Unit tests for engine.snapshot — §9.5 pre-deploy btrfs snapshot."""
import subprocess
import unittest

from engine import snapshot


def cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


class Sink:
    def __init__(self): self.lines = []
    def __call__(self, *a): self.lines.append(" ".join(str(x) for x in a))
    def text(self): return "\n".join(self.lines)


class Recorder:
    """Dispatching run stub for btrfs/findmnt commands."""
    def __init__(self, *, fstype="btrfs", snap_rc=0, list_out="", del_rc=0):
        self.fstype = fstype
        self.snap_rc = snap_rc
        self.list_out = list_out
        self.del_rc = del_rc
        self.calls = []

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        if args[:1] == ["findmnt"]:
            return cp(0 if self.fstype else 1, self.fstype)
        if args[:4] == ["sudo", "btrfs", "subvolume", "snapshot"]:
            return cp(self.snap_rc, "", "" if self.snap_rc == 0 else "boom")
        if args[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            return cp(0, self.list_out)
        if args[:4] == ["sudo", "btrfs", "subvolume", "delete"]:
            return cp(self.del_rc)
        return cp(0)

    def kinds(self):
        return [c for c in self.calls]


RID = "20260716T220000Z"
DIR = "/.cachy-snapshots"


def snap(r, **kw):
    base = dict(subvol="/", snap_dir=DIR, keep=5, run_id=RID, run=r, out=Sink())
    base.update(kw)
    return snapshot.pre_deploy_snapshot(**base)


class SnapshotTests(unittest.TestCase):

    def test_disabled_is_noop(self):
        r = Recorder()
        self.assertIsNone(snap(r, enable=False))
        self.assertEqual(r.calls, [])                       # not even findmnt

    def test_auto_skips_when_not_btrfs(self):
        r = Recorder(fstype="ext4")
        out = Sink()
        self.assertIsNone(snapshot.pre_deploy_snapshot(
            enable="auto", subvol="/", snap_dir=DIR, keep=5, run_id=RID, run=r, out=out))
        self.assertIn("not btrfs", out.text())
        self.assertTrue(all(c[:1] == ["findmnt"] for c in r.calls))  # no btrfs op

    def test_forced_raises_when_not_btrfs(self):
        r = Recorder(fstype="ext4")
        with self.assertRaises(snapshot.SnapshotUnavailable):
            snap(r, enable=True)

    def test_snapshot_created_on_btrfs(self):
        r = Recorder(fstype="btrfs")
        dest = snap(r, enable="auto")
        self.assertEqual(dest, f"{DIR}/deploy-{RID}")
        self.assertIn(["sudo", "btrfs", "subvolume", "snapshot", "-r", "/",
                       f"{DIR}/deploy-{RID}"], r.calls)

    def test_forced_on_btrfs_works(self):
        r = Recorder(fstype="btrfs")
        self.assertEqual(snap(r, enable=True), f"{DIR}/deploy-{RID}")

    def test_snapshot_command_failure_raises(self):
        r = Recorder(fstype="btrfs", snap_rc=1)
        with self.assertRaises(snapshot.SnapshotFailed):
            snap(r, enable="auto")

    def test_prune_deletes_oldest_beyond_keep(self):
        listing = "\n".join(
            f"ID {i} gen {i} top level 5 path .cachy-snapshots/deploy-2026070{i}T000000Z"
            for i in range(1, 5))                          # deploy-04..07 (4 old)
        r = Recorder(fstype="btrfs", list_out=listing)
        snap(r, enable="auto", keep=2)
        dels = [c for c in r.calls if c[:4] == ["sudo", "btrfs", "subvolume", "delete"]]
        self.assertEqual(len(dels), 2)                     # 4 listed, keep 2 -> del 2
        self.assertEqual(dels[0][4], f"{DIR}/deploy-20260701T000000Z")  # oldest first

    def test_prune_keep_zero_deletes_all_listed(self):
        listing = "ID 1 gen 1 top level 5 path .cachy-snapshots/deploy-20260701T000000Z"
        r = Recorder(fstype="btrfs", list_out=listing)
        snap(r, enable="auto", keep=0)
        self.assertTrue(any(c[:4] == ["sudo", "btrfs", "subvolume", "delete"] for c in r.calls))


if __name__ == "__main__":
    unittest.main()


# ==========================================================================
# §9.5b inventory + restore recipe (read-only)
# ==========================================================================
# The box's REAL listing, copied from a live probe: the parser has to survive
# every naming shape actually in use, including prefixes containing dashes.
REAL_LISTING = """\
ID 283 gen 638 top level 281 path .cachy-snapshots/deploy-20260717T121555Z
ID 308 gen 3888 top level 281 path .cachy-snapshots/manual-pre-first-update-20260811T005327Z
ID 309 gen 3904 top level 281 path .cachy-snapshots/deploy-20260811T011412Z
ID 380 gen 5795 top level 281 path .cachy-snapshots/de-trial-plasma-20260820T002346Z
"""


class SnapshotNameTests(unittest.TestCase):

    def test_deploy_name_yields_a_joinable_run_id(self):
        i = snapshot.parse_snapshot_name("deploy-20260816T201557Z")
        self.assertEqual(i.kind, snapshot.KIND_DEPLOY)
        self.assertEqual(i.run_id, "20260816T201557Z")   # joins run-<id>/journal.json
        self.assertTrue(i.prunable)

    def test_dashed_prefixes_are_not_mistaken_for_the_stamp(self):
        # "de-trial-plasma-<stamp>" and "manual-pre-first-update-<stamp>" both
        # contain dashes, so the STAMP is matched at the end rather than the
        # prefix at the start. Splitting on the first dash would misclassify both.
        i = snapshot.parse_snapshot_name("de-trial-plasma-20260820T002346Z")
        self.assertEqual(i.kind, snapshot.KIND_TRIAL)
        self.assertEqual(i.label, "plasma")
        j = snapshot.parse_snapshot_name("manual-pre-first-update-20260811T005327Z")
        self.assertEqual(j.kind, snapshot.KIND_MANUAL)
        self.assertEqual(j.label, "pre-first-update")

    def test_only_automatic_snapshots_are_marked_prunable(self):
        # _prune touches deploy-* only, so a user's own bookmarks must never be
        # presented as disposable.
        for name in ("manual-x-20260811T005327Z", "de-trial-plasma-20260820T002346Z",
                     "something-else"):
            self.assertFalse(snapshot.parse_snapshot_name(name).prunable, name)

    def test_an_unparseable_name_degrades_instead_of_raising(self):
        i = snapshot.parse_snapshot_name("hand-made-thing")
        self.assertEqual(i.kind, snapshot.KIND_OTHER)
        self.assertIsNone(i.stamp)
        self.assertEqual(snapshot.age_text(i.stamp), "")

    def test_age_text_reads_as_a_human_would_say_it(self):
        from datetime import datetime, timezone
        now = datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc)
        self.assertEqual(snapshot.age_text("20260823T010000Z", now), "today")
        self.assertEqual(snapshot.age_text("20260822T010000Z", now), "yesterday")
        self.assertEqual(snapshot.age_text("20260816T010000Z", now), "7 days ago")
        self.assertIn("months ago", snapshot.age_text("20260101T010000Z", now))
        self.assertIn("future", snapshot.age_text("20270101T010000Z", now))


class SnapshotListTests(unittest.TestCase):

    def test_real_listing_parses_newest_first(self):
        snaps = snapshot.list_snapshots(snap_dir="/.cachy-snapshots",
                                        run=lambda a: cp(0, REAL_LISTING))
        self.assertEqual([s.name for s in snaps], [
            "de-trial-plasma-20260820T002346Z",
            "deploy-20260811T011412Z",
            "manual-pre-first-update-20260811T005327Z",
            "deploy-20260717T121555Z"])
        self.assertEqual(snaps[0].path,
                         "/.cachy-snapshots/de-trial-plasma-20260820T002346Z")

    def test_a_failed_list_is_empty_not_an_exception(self):
        self.assertEqual(
            snapshot.list_snapshots(snap_dir="/x", run=lambda a: cp(1, "", "denied")),
            [])

    def test_it_uses_the_grant_it_already_has(self):
        seen = []

        def run(a):
            seen.append(list(a))
            return cp(0, "")
        snapshot.list_snapshots(snap_dir="/.cachy-snapshots", run=run)
        self.assertEqual(seen, [["sudo", "btrfs", "subvolume", "list", "-o",
                                 "/.cachy-snapshots"]])


class RestoreLayoutTests(unittest.TestCase):
    """Whether a set-default restore can work here is decided from fstab, because
    an explicit `subvol=` mount option overrides the default subvolume — so on a
    pinned layout the approach is a silent no-op, worth detecting BEFORE a reboot
    rather than after one."""

    TESTBED = ("UUID=785e634b / btrfs defaults 0 0\n"
               "tmpfs /tmp tmpfs defaults,nosuid,nodev 0 0\n")
    RECOMMENDED = ("UUID=abc / btrfs rw,noatime,subvol=@ 0 1\n"
                   "UUID=def /boot ext4 defaults 0 2\n")

    def test_converted_toplevel_root_supports_set_default(self):
        lay = snapshot.detect_restore_layout(self.TESTBED)
        self.assertEqual(lay.kind, snapshot.LAYOUT_TOPLEVEL)
        self.assertTrue(lay.boot_inside_root)       # no separate /boot entry

    def test_pinned_subvol_layout_is_refused_not_attempted(self):
        lay = snapshot.detect_restore_layout(self.RECOMMENDED)
        self.assertEqual(lay.kind, snapshot.LAYOUT_PINNED)
        self.assertEqual(lay.subvol, "@")
        self.assertFalse(lay.boot_inside_root)      # /boot is its own mount
        plan = snapshot.restore_recipe(
            layout=lay,
            snapshot=snapshot.parse_snapshot_name("deploy-20260816T201557Z"))
        self.assertFalse(plan.supported)
        self.assertEqual(plan.steps, [])
        self.assertTrue(any("fstab" in n for n in plan.notes))

    def test_subvolid_counts_as_pinned_too(self):
        lay = snapshot.detect_restore_layout("UUID=abc / btrfs subvolid=256 0 1\n")
        self.assertEqual(lay.kind, snapshot.LAYOUT_PINNED)

    def test_a_non_btrfs_root_offers_nothing(self):
        lay = snapshot.detect_restore_layout("UUID=x / ext4 defaults 0 1\n")
        self.assertEqual(lay.kind, snapshot.LAYOUT_UNKNOWN)
        plan = snapshot.restore_recipe(
            layout=lay,
            snapshot=snapshot.parse_snapshot_name("deploy-20260816T201557Z"))
        self.assertFalse(plan.supported)

    def test_comments_and_short_lines_do_not_confuse_it(self):
        lay = snapshot.detect_restore_layout(
            "# a comment\n\nUUID=x / btrfs defaults 0 0\nbroken\n")
        self.assertEqual(lay.kind, snapshot.LAYOUT_TOPLEVEL)


class RestoreRecipeTests(unittest.TestCase):

    def _plan(self, fstab=None):
        snap = snapshot.parse_snapshot_name("deploy-20260816T201557Z")
        snap.path = "/.cachy-snapshots/deploy-20260816T201557Z"
        return snapshot.restore_recipe(
            layout=snapshot.detect_restore_layout(
                fstab if fstab is not None else RestoreLayoutTests.TESTBED),
            snapshot=snap)

    def test_it_snapshots_the_current_root_before_anything_else(self):
        # A restore must itself be undoable, or it is a one-way door.
        plan = self._plan()
        self.assertIn("pre-restore-", plan.steps[0])
        self.assertTrue(plan.steps[0].startswith("sudo btrfs subvolume snapshot /"))

    def test_it_makes_a_writable_copy_before_setting_a_default(self):
        # §9.5 snapshots are -r; booting one directly gives a read-only root.
        plan = self._plan()
        self.assertIn("restore-deploy-20260816T201557Z", plan.steps[1])
        self.assertNotIn("-r ", plan.steps[1])
        self.assertIn("set-default", plan.steps[2])

    def test_set_default_is_given_a_path_not_an_invented_id(self):
        # btrfs-progs accepts either form; a path spares the user reading an ID
        # out of a listing and mistyping it onto their root filesystem.
        self.assertRegex(self._plan().steps[2],
                         r"set-default /\.cachy-snapshots/restore-")

    def test_it_ends_in_a_reboot_because_set_default_needs_one(self):
        self.assertEqual(self._plan().steps[-1], "sudo reboot")

    def test_the_undo_command_is_always_offered(self):
        plan = self._plan()
        self.assertIsNotNone(plan.undo)
        self.assertIn("set-default 5", plan.undo)

    def test_it_warns_that_boot_is_rewound_when_boot_lives_in_root(self):
        # The trap on the converted testbed: a restore rewinds kernels too, so a
        # bootloader entry naming a current-only kernel stops working.
        self.assertTrue(any("/boot is INSIDE" in n for n in self._plan().notes))

    def test_no_boot_warning_when_boot_is_its_own_filesystem(self):
        plan = snapshot.restore_recipe(
            layout=snapshot.RestoreLayout(snapshot.LAYOUT_TOPLEVEL,
                                          boot_inside_root=False, detail="test"),
            snapshot=snapshot.parse_snapshot_name("deploy-20260816T201557Z"))
        self.assertFalse(any("/boot is INSIDE" in n for n in plan.notes))

    def test_it_explains_the_empty_snapshot_dir_surprise(self):
        # Nested subvolumes are not part of a snapshot, so after restoring the
        # snapshot dir looks empty — alarming, and entirely expected.
        self.assertTrue(any("look EMPTY" in n for n in self._plan().notes))
