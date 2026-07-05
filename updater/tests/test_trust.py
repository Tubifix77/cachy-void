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


if __name__ == "__main__":
    unittest.main()
