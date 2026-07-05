"""Unit tests for the transaction journal (architecture.md §7.5/§7.6)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.atomicio import read_jsonl, write_json_atomic, read_json, sweep_tmp
from engine.journal import Journal, crash_report


class JournalWriteTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.j = Journal(self.tmp).start("20260705T000000Z", "deadbeef")

    def _load(self):
        return read_json(Path(self.tmp) / "journal.json")

    def test_start_writes_valid_object(self):
        d = self._load()
        self.assertEqual(d["run_id"], "20260705T000000Z")
        self.assertEqual(d["git_head"], "deadbeef")
        self.assertEqual(d["phase"], "sync")

    def test_set_order_seeds_pending_pkgs(self):
        self.j.set_order(["b", "a"], "fallback")
        d = self._load()
        self.assertEqual(d["order"], ["b", "a"])
        self.assertEqual(d["order_provenance"], "fallback")
        self.assertEqual(d["pkgs"]["a"]["status"], "pending")

    def test_pkg_status_transitions_and_timestamps(self):
        self.j.set_order(["mesa"], "sorter")
        self.j.set_pkg_status("mesa", "building")
        self.assertIsNotNone(self._load()["pkgs"]["mesa"]["started"])
        self.j.set_pkg_status("mesa", "built", log="build-mesa.log")
        entry = self._load()["pkgs"]["mesa"]
        self.assertEqual(entry["status"], "built")
        self.assertEqual(entry["log"], "build-mesa.log")
        self.assertIsNotNone(entry["ended"])

    def test_every_write_leaves_parseable_json(self):
        self.j.set_phase("build")
        self.j.set_deploy_bins(["mesa-dri"])
        # If any write were non-atomic/torn this would raise.
        self.assertEqual(self._load()["deploy_bins"], ["mesa-dri"])

    def test_audit_log_is_appendonly_jsonl(self):
        self.j.set_phase("build")
        events = read_jsonl(Path(self.tmp) / "journal.log")
        self.assertGreaterEqual(len(events), 2)          # start + phase
        self.assertEqual(events[0]["event"], "start")
        self.assertTrue(all("ts" in e for e in events))

    def test_bad_status_and_phase_rejected(self):
        with self.assertRaises(ValueError):
            self.j.set_phase("bogus")
        with self.assertRaises(ValueError):
            self.j.set_pkg_status("x", "bogus")

    def test_wal_audit_line_lands_before_snapshot(self):
        # WAL discipline (§7.6): if the snapshot write dies, the audit trail
        # must already name the transition; the snapshot must be unchanged.
        with mock.patch("engine.journal.write_json_atomic",
                        side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.j.set_phase("build")
        events = read_jsonl(Path(self.tmp) / "journal.log")
        self.assertEqual(events[-1]["event"], "phase")       # log has it
        self.assertEqual(self._load()["phase"], "sync")      # snapshot doesn't


class CrashRecoveryTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = Path(self.tmp) / "journal.json"

    def test_interrupted_while_building_is_described(self):
        j = Journal(self.tmp).start("rid", "abc")
        j.set_order(["a", "b", "c"], "sorter")
        j.set_pkg_status("a", "built")
        j.set_pkg_status("b", "building")     # <- crash here (never finalized)
        rep = crash_report(self.path)
        self.assertTrue(rep.interrupted)
        self.assertEqual(rep.building, "b")
        self.assertEqual(rep.built, ["a"])
        self.assertEqual(rep.pending, ["c"])
        self.assertIn("recomputing the queue", rep.note)   # §7.6 doctrine

    def test_clean_finish_not_interrupted(self):
        j = Journal(self.tmp).start("rid", "abc")
        j.finish()
        rep = crash_report(self.path)
        self.assertFalse(rep.interrupted)

    def test_missing_journal(self):
        rep = crash_report(self.path)
        self.assertFalse(rep.interrupted)
        self.assertEqual(rep.note, "no journal present")

    def test_corrupt_journal_is_tolerated(self):
        self.path.write_text("{ this is not json", encoding="utf-8")
        rep = crash_report(self.path)
        self.assertTrue(rep.interrupted)
        self.assertIn("corrupt", rep.note)


class AtomicIoTests(unittest.TestCase):

    def test_atomic_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "x.json"
            write_json_atomic(p, {"k": 1})
            self.assertEqual(read_json(p), {"k": 1})

    def test_jsonl_skips_torn_final_line(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "log.jsonl"
            p.write_text('{"a":1}\n{"b":2}\n{trunca', encoding="utf-8")
            self.assertEqual(read_jsonl(p), [{"a": 1}, {"b": 2}])

    def test_sweep_tmp_removes_only_litter(self):
        # F10: hard power cuts leave .tmp-*.json; the sweep must remove those
        # and nothing else.
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / ".tmp-dead1.json").write_text("{", encoding="utf-8")
            (Path(d) / "journal.json").write_text("{}", encoding="utf-8")
            self.assertEqual(sweep_tmp(d), 1)
            self.assertFalse((Path(d) / ".tmp-dead1.json").exists())
            self.assertTrue((Path(d) / "journal.json").exists())

    def test_sweep_tmp_missing_dir_is_noop(self):
        self.assertEqual(sweep_tmp("/no/such/dir/anywhere"), 0)


if __name__ == "__main__":
    unittest.main()
