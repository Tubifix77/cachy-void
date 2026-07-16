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
