"""End-to-end integration of the kernel synthesis circuit (§8.2 → §8.3 → §8.4).

Exercises the closed loop wired in cachy_void_update._kernel_synthesis:
classify_bump detects a mocked upstream bump -> ensure_trusted_patch confirms the
sha against a local bore.lock -> synthesize replaces the worktree. Every
kernel-path failure must break the chain, record the correct §8.8 stall state,
and leave userspace free to proceed. Filesystem is a temp sandbox; the network
is either avoided (reuse-first) or injected.
"""
import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cachy_void_update as cli
from engine import grub, trust

UPSTREAM_TEMPLATE = r"""# Template file for 'linux6.12'
pkgname=linux6.12
version=6.12.35
revision=1
_kernver="${version}_${revision}"
short_desc="Linux kernel (${version%.*} series)"
maintainer="x <x@example.com>"
license="GPL-2.0-only"
homepage="https://www.kernel.org"
distfiles="https://cdn.kernel.org/linux-${version}.tar.xz"
checksum=1111111111111111111111111111111111111111111111111111111111111111
subpackages="linux6.12-headers linux6.12-dbg"

do_configure() {
	sed -i -e "s|^\(CONFIG_LOCALVERSION=\).*|\1\"_${revision}\"|" .config
}

linux6.12-headers_package() { short_desc+=" - headers"; }
linux6.12-dbg_package() { short_desc+=" - dbg"; }
"""

DOTCONFIG = 'CONFIG_LOCALVERSION="_1"\nCONFIG_FOO=y\n'
FRAGMENT = "CONFIG_SCHED_BORE=y\n"
PATCH = b"--- a/x\n+++ b/x\n@@ bore @@\n"
PATCH_SHA = hashlib.sha256(PATCH).hexdigest()


def _vercmp(a, b):
    def key(v):
        ver, _, rev = v.partition("_")
        return (tuple(int(x) for x in ver.split(".")), int(rev or 0))
    return (key(a) > key(b)) - (key(a) < key(b))


class VerXbps:
    """Minimal xbps stand-in; the synthesis circuit only needs vercmp."""
    def vercmp(self, a, b):
        return _vercmp(a, b)


def _bore_lock(path: Path, sha=PATCH_SHA, series="6.12", valid=True):
    if not valid:
        path.write_text("this is not : valid = toml [", encoding="utf-8")
        return path
    path.write_text(
        '[repo]\nurl = "https://example/bore"\n'
        'pinned_commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n\n'
        f'[[patch]]\nseries = "{series}"\nfile = "p.patch"\n'
        f'sha256 = "{sha}"\nbore_version = "6.0.0"\napproved = "2026 t"\n',
        encoding="utf-8")
    return path


class Fixture:
    """A temp void-packages + state + bore.lock, wired into a Config."""

    def __init__(self, *, ported="6.12.34_1", with_upstream=True,
                 with_dotconfig=True, lock_valid=True, lock_sha=PATCH_SHA):
        self.root = Path(tempfile.mkdtemp())
        srcpkgs = self.root / "srcpkgs"
        if with_upstream:
            up = srcpkgs / "linux6.12"
            (up / "files").mkdir(parents=True)
            (up / "patches").mkdir()
            (up / "template").write_text(UPSTREAM_TEMPLATE, encoding="utf-8")
            if with_dotconfig:
                (up / "files" / "x86_64-dotconfig").write_text(
                    DOTCONFIG, encoding="utf-8")
        else:
            srcpkgs.mkdir(parents=True)

        self.state_dir = self.root / "state"
        self.fragment = self.root / "fragment.config"
        self.fragment.write_text(FRAGMENT, encoding="utf-8")
        self.lock = _bore_lock(self.root / "bore.lock", sha=lock_sha,
                               valid=lock_valid)
        self.config = cli.Config(
            void_packages=self.root, state_dir=self.state_dir,
            fragment_path=self.fragment, bore_lock=self.lock)
        # seed tracked series + ported version (human bootstrap, §8.2)
        store = grub.KernelStateStore(self.config.kernel_state_path)
        st = grub.default_state(base_series="6.12", ported_version=ported)
        store.save(st)

    def state_name(self):
        return grub.KernelStateStore(self.config.kernel_state_path).load()["state"]

    def seed_cached_patch(self, data=PATCH):
        p = self.config.kernel_patch_path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    @property
    def fork(self):
        return self.root / "srcpkgs" / "linux-cachy"


class HappyPathTests(unittest.TestCase):

    def test_bump_verify_synthesize_reaches_ready(self):
        fx = Fixture()
        fx.seed_cached_patch(PATCH)            # reuse-first: no network needed
        out = []
        cli._kernel_synthesis(fx.config, VerXbps(), out.append)

        self.assertEqual(fx.state_name(), "READY")
        template = (fx.fork / "template").read_text(encoding="utf-8")
        self.assertIn("pkgname=linux-cachy\n", template)
        self.assertNotIn("linux6.12", template)
        self.assertEqual((fx.fork / "patches" / "0001-bore.patch").read_bytes(), PATCH)
        dot = (fx.fork / "files" / "x86_64-dotconfig").read_text(encoding="utf-8")
        self.assertIn("CONFIG_SCHED_BORE=y", dot)
        self.assertTrue(any("upstream bump" in m for m in out))
        self.assertTrue(any("regenerated linux-cachy 6.12.35_1" in m for m in out))

    def test_fetch_path_when_no_cache(self):
        fx = Fixture()
        fetched = []

        def fetcher(url, commit, file):
            fetched.append((url, commit, file))
            return PATCH                       # sha matches the lock
        cli._kernel_synthesis(fx.config, VerXbps(), lambda *_: None, fetcher=fetcher)
        self.assertEqual(fx.state_name(), "READY")
        self.assertEqual(len(fetched), 1)      # network was consulted once

    def test_no_bump_is_a_noop(self):
        fx = Fixture(ported="6.12.35_1")       # already at upstream
        cli._kernel_synthesis(fx.config, VerXbps(), lambda *_: None)
        self.assertFalse(fx.fork.exists())
        self.assertEqual(fx.state_name(), "TRACKING")


class FailureChainTests(unittest.TestCase):

    def test_crypto_signature_failure_halts_before_synthesis(self):
        # Tampered fetched patch -> HashMismatch -> chain breaks BEFORE any
        # template work; state records the integrity halt (§8.3).
        fx = Fixture()

        def tampered(url, commit, file):
            return b"malicious payload"        # sha != lock
        out = []
        cli._kernel_synthesis(fx.config, VerXbps(), out.append, fetcher=tampered)

        self.assertEqual(fx.state_name(), "HALT_HASH_MISMATCH")
        self.assertFalse(fx.fork.exists())     # synthesis never ran
        self.assertTrue(any("integrity FAILED" in m for m in out))

    def test_malformed_bore_lock_awaits_human_patch(self):
        fx = Fixture(lock_valid=False)
        cli._kernel_synthesis(fx.config, VerXbps(), lambda *_: None)
        self.assertEqual(fx.state_name(), "AWAIT_HUMAN_PATCH")
        self.assertFalse(fx.fork.exists())

    def test_synthesis_assert_failure_awaits_human_template(self):
        # Trust passes (reuse-first), but the upstream dotconfig is missing so
        # synthesize raises -> AWAIT_HUMAN_TEMPLATE (§8.4), not a crypto halt.
        fx = Fixture(with_dotconfig=False)
        fx.seed_cached_patch(PATCH)
        cli._kernel_synthesis(fx.config, VerXbps(), lambda *_: None)
        self.assertEqual(fx.state_name(), "AWAIT_HUMAN_TEMPLATE")

    def test_series_gone_awaits_human_series(self):
        fx = Fixture(with_upstream=False)
        cli._kernel_synthesis(fx.config, VerXbps(), lambda *_: None)
        self.assertEqual(fx.state_name(), "AWAIT_HUMAN_SERIES")
        self.assertFalse(fx.fork.exists())


class CommitResilienceTests(unittest.TestCase):
    """A kernel-path failure must not stop userspace deploy (§8 preamble)."""

    def test_kernel_halt_still_deploys_userland(self):
        fx = Fixture(lock_valid=False)         # kernel synthesis will halt

        # A userland target that needs deploying (O-term: upstream origin until
        # the §4.6 takeover flips it to the overlay).
        overlay = str(fx.config.repos[0])

        class UserlandXbps(VerXbps):
            def __init__(self): self._taken: set[str] = set()
            def installed(self): return ["mesa"]
            def srcpkg_of(self, b): return "mesa" if b == "mesa" else None
            def inst_pkgver(self, b): return "mesa-1.0_1"
            def repo_ver(self, n): return "mesa-1.0_1" if n == "mesa" else None
            def origin(self, b):
                return overlay if b in self._taken else "https://upstream"
            def take_over(self, b): self._taken.add(b)
            def show_local_updates(self): return []
            def sort_dependencies(self, p): return sorted(p), True
        fx.config.targets = ["mesa", "linux-cachy"]
        xb = UserlandXbps()

        calls = []
        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return _cp(0, "abc\n")
            if args[:2] == ["sudo", "xbps-install"] and "-fy" in args and "mesa" in args:
                xb.take_over("mesa")                       # §4.6 takeover converges
            return _cp(0, "")

        rc = cli.cmd_commit(xb, fx.config, assume_yes=True,
                            dry_run=False, out=lambda *_: None, run=run)
        self.assertEqual(rc, cli.EXIT_OK)                 # userspace succeeded
        self.assertEqual(fx.state_name(), "AWAIT_HUMAN_PATCH")   # kernel withheld
        self.assertTrue(any(c[:2] == ["sudo", "xbps-install"] for c in calls))


def _cp(returncode=0, stdout="", stderr=""):
    import subprocess
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


if __name__ == "__main__":
    unittest.main()
