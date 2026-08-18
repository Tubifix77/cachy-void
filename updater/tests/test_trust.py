"""Unit tests for the BORE patch trust pipeline (architecture.md §8.3).

The network is always mocked; the local bore.lock is the trust anchor. These
tests assert the integrity gate, the malformed-lock exit mapping, and the
offline/degraded fallback — never touching git or the real repo.
"""
import tempfile
import unittest
from pathlib import Path

from engine.trust import (
    load_bore_lock, ensure_trusted_patch, sha256_bytes, exit_code_for,
    append_pin, PinProposal, select_bore_patch,
    TrustError, TrustConfigError, HashMismatch, PatchUnavailable, NetworkError,
    EXIT_CONFIG, EXIT_HALT,
)

PATCH = b"--- a/kernel/sched.c\n+++ b/kernel/sched.c\n@@ BORE @@\n"
PATCH_SHA = sha256_bytes(PATCH)


def _lock_toml(sha=PATCH_SHA, series="6.12", commit="a" * 40,
               extra_patch="", omit_repo=False, omit_patch=False):
    parts = []
    if not omit_repo:
        parts.append(f'[repo]\nurl = "https://example/bore"\npinned_commit = "{commit}"\n')
    if not omit_patch:
        parts.append(f'[[patch]]\nseries = "{series}"\n'
                     f'file = "patches/x.patch"\nsha256 = "{sha}"\n'
                     f'bore_version = "6.0.0"\napproved = "2026-07-05 t"\n')
    parts.append(extra_patch)
    return "\n".join(parts)


def _write(dirpath, text, name="bore.lock"):
    p = Path(dirpath) / name
    p.write_text(text, encoding="utf-8")
    return p


class LoadLockTests(unittest.TestCase):

    def test_valid_lock(self):
        with tempfile.TemporaryDirectory() as d:
            lock = load_bore_lock(_write(d, _lock_toml()))
        self.assertEqual(lock.repo_url, "https://example/bore")
        entry = lock.patch_for("6.12")
        self.assertEqual(entry.sha256, PATCH_SHA)
        self.assertEqual(entry.bore_version, "6.0.0")

    def test_missing_file_is_config_error(self):
        with self.assertRaises(TrustConfigError):
            load_bore_lock("/no/such/bore.lock")

    def test_bad_toml_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TrustConfigError):
                load_bore_lock(_write(d, "this is : not = toml ["))

    def test_short_sha_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TrustConfigError):
                load_bore_lock(_write(d, _lock_toml(sha="deadbeef")))

    def test_missing_repo_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TrustConfigError):
                load_bore_lock(_write(d, _lock_toml(omit_repo=True)))

    def test_no_patch_entries_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TrustConfigError):
                load_bore_lock(_write(d, _lock_toml(omit_patch=True)))

    def test_unknown_series_is_config_error(self):
        with tempfile.TemporaryDirectory() as d:
            lock = load_bore_lock(_write(d, _lock_toml()))
            with self.assertRaises(TrustConfigError):
                lock.patch_for("6.6")


class ExitMappingTests(unittest.TestCase):
    """Explicit exit-code mapping (§8.3)."""

    def test_malformed_lock_maps_to_exit_1(self):
        self.assertEqual(exit_code_for(TrustConfigError("x")), EXIT_CONFIG)
        self.assertEqual(EXIT_CONFIG, 1)

    def test_integrity_failures_map_to_exit_70(self):
        self.assertEqual(exit_code_for(HashMismatch("x")), EXIT_HALT)
        self.assertEqual(exit_code_for(PatchUnavailable("x")), EXIT_HALT)
        self.assertEqual(exit_code_for(TrustError("x")), EXIT_HALT)
        self.assertEqual(EXIT_HALT, 70)


class TrustGateTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.lock = load_bore_lock(_write(self.tmp, _lock_toml()))
        self.patch_path = self.tmp / "patches" / "0001-bore.patch"

    def test_reuse_first_skips_network(self):
        self.patch_path.parent.mkdir(parents=True)
        self.patch_path.write_bytes(PATCH)
        calls = []

        def fetcher(*a):
            calls.append(a)
            raise AssertionError("network must not be touched on valid cache")

        res = ensure_trusted_patch(lock=self.lock, series="6.12",
                                   patch_path=self.patch_path, fetcher=fetcher)
        self.assertEqual(res.source, "cache")
        self.assertEqual(calls, [])

    def test_fetch_and_verify_writes_patch(self):
        def fetcher(url, commit, file):
            self.assertEqual(commit, "a" * 40)
            return PATCH
        res = ensure_trusted_patch(lock=self.lock, series="6.12",
                                   patch_path=self.patch_path, fetcher=fetcher)
        self.assertEqual(res.source, "network")
        self.assertEqual(self.patch_path.read_bytes(), PATCH)

    def test_hash_mismatch_halts_and_writes_nothing(self):
        def fetcher(*a):
            return b"tampered content"
        with self.assertRaises(HashMismatch):
            ensure_trusted_patch(lock=self.lock, series="6.12",
                                 patch_path=self.patch_path, fetcher=fetcher)
        self.assertFalse(self.patch_path.exists())   # unverified artifact never lands

    def test_offline_with_no_cache_is_unavailable(self):
        with self.assertRaises(PatchUnavailable):
            ensure_trusted_patch(lock=self.lock, series="6.12",
                                 patch_path=self.patch_path,
                                 fetcher=lambda *a: PATCH, allow_network=False)

    def test_network_error_with_no_cache_is_unavailable(self):
        def fetcher(*a):
            raise NetworkError("timeout")
        with self.assertRaises(PatchUnavailable):
            ensure_trusted_patch(lock=self.lock, series="6.12",
                                 patch_path=self.patch_path, fetcher=fetcher)

    def test_stale_cache_is_replaced_by_valid_network(self):
        self.patch_path.parent.mkdir(parents=True)
        self.patch_path.write_bytes(b"old stale patch")
        res = ensure_trusted_patch(lock=self.lock, series="6.12",
                                   patch_path=self.patch_path,
                                   fetcher=lambda *a: PATCH)
        self.assertEqual(res.source, "network")
        self.assertEqual(self.patch_path.read_bytes(), PATCH)

    def test_stale_cache_offline_is_unavailable_not_silent_pass(self):
        self.patch_path.parent.mkdir(parents=True)
        self.patch_path.write_bytes(b"old stale patch")
        with self.assertRaises(PatchUnavailable):
            ensure_trusted_patch(lock=self.lock, series="6.12",
                                 patch_path=self.patch_path,
                                 fetcher=lambda *a: PATCH, allow_network=False)


class CommittedLockTests(unittest.TestCase):
    """The repo's committed bore.lock must at least be structurally valid."""

    def test_repo_bore_lock_parses(self):
        repo_lock = Path(__file__).resolve().parents[1] / "bore.lock"
        lock = load_bore_lock(repo_lock)
        self.assertIn("6.12", lock.patches)


class SelectBorePatchTests(unittest.TestCase):
    """§8.3a: pick THE BORE patch from the real upstream dir layouts
    (verified live: 0001-…bore… + optional 0002 companions)."""

    def test_real_612_layout(self):
        names = ["patches/stable/linux-6.12-bore/0001-linux6.12.37-bore-6.6.3.patch",
                 "patches/stable/linux-6.12-bore/0002-sched-fair-Prefer-full-idle-SMT-cores.patch"]
        self.assertEqual(select_bore_patch(names), names[0])

    def test_real_66_layout_no_dash_before_version(self):
        names = ["patches/stable/linux-6.6-bore/0001-linux6.6.107-bore5.9.6.patch",
                 "patches/stable/linux-6.6-bore/0002-sched-fair-Prefer-full-idle-SMT-cores-by-Andrea-Righ.patch"]
        self.assertEqual(select_bore_patch(names), names[0])

    def test_single_file_wins_regardless_of_name(self):
        self.assertEqual(select_bore_patch(["dir/0001-something.patch"]),
                         "dir/0001-something.patch")

    def test_two_bore_files_is_ambiguous(self):
        with self.assertRaises(PatchUnavailable):
            select_bore_patch(["d/0001-bore-a.patch", "d/0002-bore-b.patch"])

    def test_many_nonbore_files_is_ambiguous(self):
        with self.assertRaises(PatchUnavailable):
            select_bore_patch(["d/0001-x.patch", "d/0002-y.patch"])


class PerEntryCommitTests(unittest.TestCase):
    """§8.3a: a [[patch]] entry may pin its own commit; older entries keep
    riding the repo-wide pinned_commit untouched."""

    def test_entry_commit_overrides_repo_commit_in_fetch(self):
        entry_commit = "b" * 40
        toml = _lock_toml(extra_patch=(
            f'[[patch]]\nseries = "6.15"\nfile = "patches/y.patch"\n'
            f'sha256 = "{PATCH_SHA}"\ncommit = "{entry_commit}"\n'))
        seen = {}

        def fetcher(url, commit, file):
            seen[file] = commit
            return PATCH

        with tempfile.TemporaryDirectory() as d:
            lock = load_bore_lock(_write(d, toml))
            for series, file in (("6.12", "patches/x.patch"),
                                 ("6.15", "patches/y.patch")):
                ensure_trusted_patch(lock=lock, series=series,
                                     patch_path=Path(d) / series / "p.patch",
                                     fetcher=fetcher)
        self.assertEqual(seen["patches/x.patch"], "a" * 40)   # repo-wide
        self.assertEqual(seen["patches/y.patch"], entry_commit)

    def test_malformed_entry_commit_is_config_error(self):
        toml = _lock_toml(extra_patch=(
            f'[[patch]]\nseries = "6.15"\nfile = "patches/y.patch"\n'
            f'sha256 = "{PATCH_SHA}"\ncommit = "not-a-sha"\n'))
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(TrustConfigError):
                load_bore_lock(_write(d, toml))


class AppendPinTests(unittest.TestCase):
    """§8.3a append_pin: text-append that preserves commentary, validates the
    result, refuses duplicates, and never half-writes."""

    def _proposal(self, series="6.15"):
        return PinProposal(series=series, repo_url="https://example/bore",
                           commit="c" * 40, file=f"patches/stable/z.patch",
                           sha256=PATCH_SHA, bore_version="9.9.9", size=len(PATCH))

    def test_append_then_reload(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, "# human comment survives\n" + _lock_toml())
            append_pin(p, self._proposal(), approved="2026-08-18 test")
            lock = load_bore_lock(p)
            self.assertIn("6.15", lock.patches)
            self.assertEqual(lock.patches["6.15"].commit, "c" * 40)
            self.assertEqual(lock.patches["6.15"].sha256, PATCH_SHA)
            self.assertIn("# human comment survives",
                          p.read_text(encoding="utf-8"))
            # the pre-existing pin is untouched
            self.assertIn("6.12", lock.patches)

    def test_duplicate_series_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, _lock_toml())
            with self.assertRaises(TrustConfigError):
                append_pin(p, self._proposal(series="6.12"), approved="x")

    def test_pinned_entry_fetches_at_its_own_commit(self):
        """The appended pin round-trips: ensure_trusted_patch fetches the new
        series at the entry's commit and verifies the recorded hash."""
        with tempfile.TemporaryDirectory() as d:
            p = _write(d, _lock_toml())
            append_pin(p, self._proposal(), approved="x")
            lock = load_bore_lock(p)
            res = ensure_trusted_patch(
                lock=lock, series="6.15", patch_path=Path(d) / "p.patch",
                fetcher=lambda url, commit, file: (
                    PATCH if commit == "c" * 40 else b"wrong commit"))
            self.assertEqual(res.sha256, PATCH_SHA)


if __name__ == "__main__":
    unittest.main()
