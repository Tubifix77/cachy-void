"""Unit tests for the unified CLI wiring (architecture.md §4/§7/§8).

Covers the read-only planning path (--check), the F5 confirmation gate, the
O-term deploy-only recovery through cmd_commit, the §8.5 G2 kernel withhold,
and the full §8.6 staging wiring (F1) with an injected boot layout.
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import cachy_void_update as cli
from engine import grub as grub_mod


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


class FakeXbps:
    """Solver/executor stub for CLI paths; records build-side interactions."""

    def __init__(self, *, installed=(), src_map=None, inst_ver=None, repo_ver=None,
                 local_updates=(), sort_ok=True, origins=None,
                 configure_rc=0, build_rc=0, files_map=None):
        self._installed = list(installed)
        self._src_map = dict(src_map or {})
        self._inst_ver = dict(inst_ver or {})
        self._repo_ver = dict(repo_ver or {})
        self._local_updates = list(local_updates)
        self._sort_ok = sort_ok
        self._origins = dict(origins or {})
        self._configure_rc = configure_rc
        self._build_rc = build_rc
        self._files_map = dict(files_map or {})
        self.configure_calls: list[str] = []
        self.build_calls: list[str] = []
        self.clean_calls: list[str] = []

    def installed(self): return list(self._installed)
    def srcpkg_of(self, b): return self._src_map.get(b)
    def inst_pkgver(self, b): return self._inst_ver[b]
    def repo_ver(self, n): return self._repo_ver.get(n)
    def show_local_updates(self): return list(self._local_updates)
    def sort_dependencies(self, pkgs): return sorted(pkgs), self._sort_ok
    def vercmp(self, a, b): return _vercmp(a, b)

    def origin(self, b):
        return self._origins.get(b, "/vp/hostdir/binpkgs")

    def files(self, b):
        return list(self._files_map.get(b, []))

    def configure(self, pkg):
        self.configure_calls.append(pkg)
        return self._configure_rc

    def clean(self, pkg):
        self.clean_calls.append(pkg)

    def build(self, pkg, jobs, log_path=None):
        self.build_calls.append(pkg)
        if log_path:
            Path(log_path).write_text("build ok\n", encoding="utf-8")
        return self._build_rc

    def mark_converged(self, b, origin="/vp/hostdir/binpkgs"):
        """Model a completed §4.6 takeover of binpkg b: origin -> overlay and
        installed version -> repo version, so a re-query in §7.7 post-verify
        sees a converged system (a static mock otherwise can't)."""
        self._origins[b] = origin
        if self._repo_ver.get(b) is not None:
            self._inst_ver[b] = self._repo_ver[b]


def _config(targets, blacklist=(), **kw):
    kw.setdefault("state_dir", Path("/nonexistent-cachy-state"))
    kw.setdefault("log_root", Path("/nonexistent-cachy-log"))
    return cli.Config(void_packages=Path("/vp"), targets=list(targets),
                      blacklist=list(blacklist), **kw)


class Sink:
    def __init__(self): self.lines = []
    def __call__(self, *a): self.lines.append(" ".join(str(x) for x in a))
    def text(self): return "\n".join(self.lines)


class ConfigTests(unittest.TestCase):

    def test_load_config(self):
        toml = (
            '[paths]\nvoid_packages = "/home/b/void-packages"\n'
            '[build]\njobs = 4\n'
            '[packages]\ntargets = ["mesa", "wine"]\nblacklist = ["glibc"]\n'
            '[services]\nrestart_skip = ["dbus"]\n'
            '[kernel]\nenable = false\nfragment = "/etc/x/frag.config"\n'
        )
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "updater.toml"
            p.write_text(toml, encoding="utf-8")
            cfg = cli.load_config(p)
        self.assertEqual(cfg.void_packages, Path("/home/b/void-packages"))
        self.assertEqual(cfg.jobs, 4)
        self.assertEqual(cfg.targets, ["mesa", "wine"])
        self.assertEqual(cfg.blacklist, ["glibc"])
        self.assertFalse(cfg.kernel_enable)
        self.assertEqual(cfg.fragment_path, Path("/etc/x/frag.config"))

    def test_repos_and_state_paths_derived(self):
        cfg = _config(["mesa"])
        self.assertEqual([str(r) for r in cfg.repos],
                         ["/vp/hostdir/binpkgs", "/vp/hostdir/binpkgs/nonfree"])
        self.assertTrue(str(cfg.kernel_state_path).endswith(
            "kernel/kernel-state.json"))

    def test_missing_void_packages_raises(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "u.toml"
            p.write_text("[build]\njobs=1\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                cli.load_config(p)


class CheckCommandTests(unittest.TestCase):

    def test_check_reports_queue(self):
        xb = FakeXbps(
            installed=["mesa"], src_map={"mesa": "mesa"},
            inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
            local_updates=["mesa"])
        out = Sink()
        rc = cli.main(["--check"], xbps=xb, config=_config(["mesa"]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("build queue", out.text())
        self.assertIn("mesa", out.text())

    def test_check_empty_queue(self):
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
                      local_updates=[])
        out = Sink()
        rc = cli.main(["--check"], xbps=xb, config=_config(["mesa"]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("queue empty", out.text())

    def test_check_surfaces_O_term_recovery(self):
        # Same version, upstream origin -> deploy-only queue (takeover resume).
        xb = FakeXbps(installed=["gamemode"], src_map={"gamemode": "gamemode"},
                      inst_ver={"gamemode": "1.0_1"},
                      repo_ver={"gamemode": "1.0_1"},
                      origins={"gamemode": "https://upstream"})
        out = Sink()
        rc = cli.main(["--check"], xbps=xb, config=_config(["gamemode"]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("deploy queue (1): gamemode", out.text())

    def test_check_flags_kernel_reboot(self):
        xb = FakeXbps(
            installed=["linux-cachy"], src_map={"linux-cachy": "linux-cachy"},
            inst_ver={"linux-cachy": "6.12.34_1"},
            repo_ver={"linux-cachy": "6.12.34_1"},
            local_updates=["linux-cachy"])
        out = Sink()
        rc = cli.main(["--check"], xbps=xb,
                      config=_config(["linux-cachy"]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("reboot will be required", out.text())

    def test_check_xbps_failure_is_exit_30_not_traceback(self):
        class Exploding(FakeXbps):
            def show_local_updates(self):
                from engine.xbps import XbpsError
                raise XbpsError("masterdir not bootstrapped")
        xb = Exploding(installed=["mesa"], src_map={"mesa": "mesa"},
                       inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"})
        out = Sink()
        rc = cli.main(["--check"], xbps=xb, config=_config(["mesa"]), out=out)
        self.assertEqual(rc, cli.EXIT_QUERY)
        self.assertIn("queue construction failed", out.text())


class CommitCommandTests(unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self, targets, void_packages="/vp"):
        return cli.Config(void_packages=Path(void_packages),
                          targets=list(targets), blacklist=[],
                          state_dir=self.tmp / "state",
                          log_root=self.tmp / "log",
                          fragment_path=self.tmp / "fragment.config")

    def _runstub(self):
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return cp(0, stdout="abc123\n")
            if args[0] == "uname":
                return cp(0, stdout="6.12.34_1\n")
            return cp(0, stdout="")
        return run, calls

    def _orphaned_takeover_xbps(self):
        # Full name-prefixed pkgvers, as real `xbps-query -p pkgver` returns.
        return FakeXbps(installed=["gamemode"],
                        src_map={"gamemode": "gamemode"},
                        inst_ver={"gamemode": "gamemode-1.0_1"},
                        repo_ver={"gamemode": "gamemode-1.0_1"},
                        origins={"gamemode": "https://upstream"})

    def _takeover_run(self, xbps, *, xcheckrestart="", sv_status=""):
        """Run stub modeling §4.6 takeover convergence: a `-fy <pkg>` install
        flips that pkg to overlay origin/version so the §7.7 post-verify sees a
        converged system. Optionally feeds xcheckrestart / sv-status for §4.7."""
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return cp(0, stdout="abc123\n")
            if args[0] == "uname":
                return cp(0, stdout="6.12.34_1\n")
            if args[:2] == ["sudo", "xbps-install"] and "-fy" in args:
                for a in args:
                    if a in xbps._installed:
                        xbps.mark_converged(a)
                return cp(0)
            if args[:2] == ["sudo", "xcheckrestart"]:
                return cp(0, stdout=xcheckrestart)
            if args[:3] == ["sudo", "sv", "status"]:
                return cp(0, stdout=sv_status)
            return cp(0, stdout="")
        return run, calls

    def test_deploy_only_run_prompts_and_abort_is_clean(self):
        # F5 regression: a deploy-only recovery run must NOT mutate the system
        # without confirmation in interactive mode.
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(self._orphaned_takeover_xbps(),
                            self._cfg(["gamemode"]),
                            assume_yes=False, dry_run=False, out=out,
                            run=run, confirm=lambda p: "n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("aborted", out.text())
        self.assertFalse(any(c[0] == "sudo" for c in calls))

    def test_deploy_only_run_with_yes_deploys_takeover(self):
        # O-term recovery end-to-end: no build, but -Su + forced takeover run,
        # and §7.7 post-verify confirms the takeover converged.
        out = Sink()
        xb = self._orphaned_takeover_xbps()
        run, calls = self._takeover_run(xb)
        rc = cli.cmd_commit(xb, self._cfg(["gamemode"]),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(any(c[:2] == ["sudo", "xbps-install"] and "-Suy" in c
                            for c in calls))
        self.assertTrue(any("-fy" in c and "gamemode" in c
                            for c in calls if c[0] == "sudo"))
        self.assertIn("post-verify", out.text())

    def test_kernel_withheld_when_fragment_missing(self):
        # §8.5: a missing fragment is a G2 failure -> kernel withheld, no build,
        # userspace (here: nothing else) continues, exit 0.
        xb = FakeXbps(installed=["linux-cachy"],
                      src_map={"linux-cachy": "linux-cachy"},
                      inst_ver={"linux-cachy": "6.12.35_1"},
                      repo_ver={"linux-cachy": "6.12.35_1"},
                      local_updates=["linux-cachy"])
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(xb, self._cfg(["linux-cachy"]),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("withheld", out.text())
        self.assertEqual(xb.build_calls, [])
        self.assertFalse(any(c[0] == "sudo" for c in calls))
        state = json.loads(
            (self.tmp / "state" / "kernel" / "kernel-state.json").read_text())
        self.assertEqual(state["state"], "AWAIT_HUMAN_TEMPLATE")

    def test_full_kernel_staging_wiring_oneshot(self):
        # F1 regression: --commit with a kernel in queue must run the G2 gate,
        # build, deploy, then REALLY stage (grub-set-default + grub-reboot via
        # sudo) and persist STAGED state with a known-good anchor.
        vp = self.tmp / "vp"
        dot = vp / "masterdir-x86_64" / "builddir" / "linux-6.12.35"
        dot.mkdir(parents=True)
        (dot / ".config").write_text("CONFIG_SCHED_BORE=y\n", encoding="utf-8")
        (self.tmp / "fragment.config").write_text("CONFIG_SCHED_BORE=y\n",
                                                  encoding="utf-8")
        grub_cfg = self.tmp / "grub.cfg"
        grub_cfg.write_text(GRUB_CFG, encoding="utf-8")
        layout = grub_mod.BootLayout(grub_mod.MODE_ONESHOT, "test",
                                     grub_cfg=str(grub_cfg))

        xb = FakeXbps(installed=["linux-cachy"],
                      src_map={"linux-cachy": "linux-cachy"},
                      inst_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      repo_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      local_updates=["linux-cachy"],
                      files_map={"linux-cachy": ["/boot/vmlinuz-6.12.35_1"]})
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(xb, self._cfg(["linux-cachy"], void_packages=vp),
                            assume_yes=True, dry_run=False, out=out, run=run,
                            stage_layout=layout)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(xb.configure_calls, ["linux-cachy"])   # G2 ran
        self.assertEqual(xb.build_calls, ["linux-cachy"])
        sudo_cmds = [c for c in calls if c[0] == "sudo"]
        self.assertTrue(any("grub-set-default" in c for c in sudo_cmds))
        self.assertTrue(any("grub-reboot" in c for c in sudo_cmds))
        state = json.loads(
            (self.tmp / "state" / "kernel" / "kernel-state.json").read_text())
        self.assertEqual(state["state"], "STAGED")
        self.assertEqual(state["candidate"]["kver"], "6.12.35_1")
        self.assertEqual(state["known_good"]["kver"], "6.12.34_1")
        self.assertIn("6.12.34_1", state["known_good"]["grub_ref"])

    def test_kernel_first_install_widens_with_headers(self):
        # K-exemption end-to-end: kernel NOT installed, template exists ->
        # queued, G2 passes, built, then Stage 4 installs it + headers before
        # staging (the single sanctioned widen).
        vp = self.tmp / "vp"
        (vp / "srcpkgs" / "linux-cachy").mkdir(parents=True)
        (vp / "srcpkgs" / "linux-cachy" / "template").write_text(
            "pkgname=linux-cachy\nversion=6.12.35\nrevision=1\n",
            encoding="utf-8")
        dot = vp / "masterdir-x86_64" / "builddir" / "linux-6.12.35"
        dot.mkdir(parents=True)
        (dot / ".config").write_text("CONFIG_SCHED_BORE=y\n", encoding="utf-8")
        (self.tmp / "fragment.config").write_text("CONFIG_SCHED_BORE=y\n",
                                                  encoding="utf-8")
        grub_cfg = self.tmp / "grub.cfg"
        grub_cfg.write_text(GRUB_CFG, encoding="utf-8")   # has 6.12.35 + .34
        layout = grub_mod.BootLayout(grub_mod.MODE_ONESHOT, "test",
                                     grub_cfg=str(grub_cfg))

        xb = FakeXbps(installed=[],                      # kernel NOT installed
                      src_map={},
                      inst_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      repo_ver={},                        # no binpkg yet -> M
                      files_map={"linux-cachy": ["/boot/vmlinuz-6.12.35_1"]})
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(xb, self._cfg(["linux-cachy"], void_packages=vp),
                            assume_yes=True, dry_run=False, out=out, run=run,
                            stage_layout=layout)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(xb.build_calls, ["linux-cachy"])
        installs = [c for c in calls if c[:2] == ["sudo", "xbps-install"]
                    and "linux-cachy" in c]
        self.assertTrue(installs, "kernel first-install must run")
        self.assertIn("linux-cachy-headers", installs[0])
        sudo_cmds = [c for c in calls if c[0] == "sudo"]
        self.assertTrue(any("grub-reboot" in c for c in sudo_cmds))
        state = json.loads(
            (self.tmp / "state" / "kernel" / "kernel-state.json").read_text())
        self.assertEqual(state["state"], "STAGED")

    def test_manual_unsafe_layout_refuses_staging_exit_70(self):
        # F3: an unsafe layout must refuse (exit 70), deploy stays intact.
        layout = grub_mod.BootLayout(grub_mod.MODE_MANUAL_UNSAFE,
                                     "GRUB_DEFAULT is not 'saved'")
        (self.tmp / "fragment.config").write_text("", encoding="utf-8")
        xb = FakeXbps(installed=["linux-cachy"],
                      src_map={"linux-cachy": "linux-cachy"},
                      inst_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      repo_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      origins={"linux-cachy": "https://upstream"})
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(xb, self._cfg(["linux-cachy"]),
                            assume_yes=True, dry_run=False, out=out, run=run,
                            stage_layout=layout)
        self.assertEqual(rc, cli.EXIT_KERNEL)
        self.assertIn("REFUSED", out.text())
        # deploy happened before the refusal:
        self.assertTrue(any(c[:2] == ["sudo", "xbps-install"] for c in calls))

    def test_commit_cycles_sshd_after_deploy(self):
        # finding #3 end-to-end: an openssh update flagged by xcheckrestart must
        # trigger a clean `sudo sv restart sshd` in the deploy path — instead of
        # the bare -Su re-exec that broke new ssh connections.
        svcroot = self.tmp / "service"
        (svcroot / "sshd").mkdir(parents=True)
        xb = self._orphaned_takeover_xbps()
        run, calls = self._takeover_run(
            xb, xcheckrestart="631 /usr/bin/sshd (openssh)\n",
            sv_status="run: sshd: (pid 631) 42s\n")
        out = Sink()
        rc = cli.cmd_commit(xb, self._cfg(["gamemode"]),
                            assume_yes=True, dry_run=False, out=out, run=run,
                            service_root=svcroot)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["sudo", "sv", "restart", "sshd"], calls)
        self.assertIn("sshd", out.text())

    def test_commit_snapshots_before_deploy_on_btrfs(self):
        # §9.5: on a btrfs deploy subvol, a read-only snapshot is taken BEFORE -Suy.
        xb = self._orphaned_takeover_xbps()
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return cp(0, stdout="abc123\n")
            if args[0] == "uname":
                return cp(0, stdout="6.12.34_1\n")
            if args[0] == "findmnt":
                return cp(0, stdout="btrfs\n")
            if args[:2] == ["sudo", "xbps-install"] and "-fy" in args:
                for a in args:
                    if a in xb._installed:
                        xb.mark_converged(a)
                return cp(0)
            return cp(0, stdout="")

        out = Sink()
        rc = cli.cmd_commit(xb, self._cfg(["gamemode"]),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        snap_i = next(i for i, c in enumerate(calls)
                      if c[:4] == ["sudo", "btrfs", "subvolume", "snapshot"])
        su_i = next(i for i, c in enumerate(calls)
                    if c[:2] == ["sudo", "xbps-install"] and "-Suy" in c)
        self.assertLess(snap_i, su_i, "snapshot must precede the -Suy")

    def test_commit_aborts_when_forced_snapshot_unavailable(self):
        # §9.5: [snapshot] enable=true but subvol not btrfs -> exit 53, NO deploy.
        xb = self._orphaned_takeover_xbps()
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["git", "rev-parse", "HEAD"]:
                return cp(0, stdout="abc123\n")
            if args[0] == "uname":
                return cp(0, stdout="6.12.34_1\n")
            if args[0] == "findmnt":
                return cp(0, stdout="ext4\n")
            return cp(0, stdout="")

        cfg = self._cfg(["gamemode"])
        cfg.snapshot_enable = True                      # force snapshots
        out = Sink()
        rc = cli.cmd_commit(xb, cfg, assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_SNAPSHOT_UNAVAIL)
        self.assertFalse(any(c[:2] == ["sudo", "xbps-install"] for c in calls),
                         "must abort before any deploy")


class SystemPassTests(unittest.TestCase):
    """§4.5a — empty overlay queue must still apply pending UPSTREAM updates.

    Regression for the first real-hardware run: 35 upstream packages pending,
    overlay in sync -> Update printed "queue empty" and changed nothing, while
    --status tier [1] kept reporting them. Same failure class as the Flatpak
    gap: an updater that silently skips a tier gives false "fully updated"
    security.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _cfg(self):
        return cli.Config(void_packages=Path("/vp"),
                          targets=["mesa"], blacklist=[],
                          state_dir=self.tmp / "state",
                          log_root=self.tmp / "log",
                          fragment_path=self.tmp / "fragment.config")

    def _insync_xbps(self):
        # overlay fully in sync -> build/deploy queues are both empty
        return FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                        inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
                        local_updates=[])

    def _run_with_pending(self, pending_lines):
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["sudo", "xbps-install", "-Sun"]:
                return cp(0, stdout="\n".join(pending_lines))
            return cp(0, stdout="")
        return run, calls

    def test_empty_queue_applies_pending_system_updates(self):
        out = Sink()
        run, calls = self._run_with_pending([
            "foo-1.1_1 update x86_64 https://repo 10 10",
            "bar-2.0_1 update x86_64 https://repo 10 10",
            "linux6.12-6.12.98_1 hold x86_64 https://repo 10 10",  # pinned: not counted
        ])
        rc = cli.cmd_commit(self._insync_xbps(), self._cfg(),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("2 upstream update(s) pending", out.text())
        self.assertTrue(any(c[:2] == ["sudo", "xbps-install"] and "-Suy" in c
                            for c in calls))
        self.assertIn("system update complete", out.text())

    def test_empty_queue_no_pending_is_noop(self):
        out = Sink()
        run, calls = self._run_with_pending(
            ["linux6.12-6.12.98_1 hold x86_64 https://repo 10 10"])
        rc = cli.cmd_commit(self._insync_xbps(), self._cfg(),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("base already up to date", out.text())
        self.assertFalse(any("-Suy" in c for c in calls))

    def test_empty_queue_system_pass_asks_before_mutating(self):
        out = Sink()
        run, calls = self._run_with_pending(
            ["foo-1.1_1 update x86_64 https://repo 10 10"])
        rc = cli.cmd_commit(self._insync_xbps(), self._cfg(),
                            assume_yes=False, dry_run=False, out=out, run=run,
                            confirm=lambda p: "n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("aborted", out.text())
        self.assertFalse(any("-Suy" in c for c in calls))

    def test_empty_queue_dry_run_never_mutates(self):
        out = Sink()
        run, calls = self._run_with_pending(
            ["foo-1.1_1 update x86_64 https://repo 10 10"])
        rc = cli.cmd_commit(self._insync_xbps(), self._cfg(),
                            assume_yes=True, dry_run=True, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertFalse(any(c and c[0] == "sudo" for c in calls))

    def test_empty_queue_sun_failure_is_exit_query(self):
        out = Sink()
        calls: list[list[str]] = []

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:3] == ["sudo", "xbps-install", "-Sun"]:
                return cp(1, stderr="repo unreachable")
            return cp(0, stdout="")
        rc = cli.cmd_commit(self._insync_xbps(), self._cfg(),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_QUERY)
        self.assertFalse(any("-Suy" in c for c in calls))


class ServiceCycleTests(unittest.TestCase):
    """§4.7 Stage 4c — service lifecycle."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.svcroot = self.tmp / "service"
        self.svcroot.mkdir()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _mk(self, *names):
        for n in names:
            (self.svcroot / n).mkdir()

    def _run(self, *, xcr="", xcr_rc=0, status=None, restart_rc=0,
             status_after=None):
        """Dispatching run stub. `status` maps svc -> `sv status` line (used
        for PID mapping and, unless overridden by `status_after`, for the
        post-restart verification call)."""
        status = status or {}
        status_after = status_after or {}
        calls: list[list[str]] = []
        seen: dict[str, int] = {}

        def run(args, cwd=None):
            calls.append(list(args))
            if args[:2] == ["sudo", "xcheckrestart"]:
                return cp(xcr_rc, stdout=xcr)
            if args[:3] == ["sudo", "sv", "status"]:
                svc = args[3]
                n = seen.get(svc, 0)
                seen[svc] = n + 1
                if n >= 1 and svc in status_after:
                    return cp(0, stdout=status_after[svc])
                return cp(0, stdout=status.get(svc, f"down: {svc}: 1s\n"))
            if args[:3] == ["sudo", "sv", "restart"]:
                return cp(restart_rc)
            return cp(0)
        return run, calls

    def test_restarts_matched_service(self):
        self._mk("sshd", "dbus")
        run, calls = self._run(
            xcr="631 /usr/bin/sshd (openssh)\n",
            status={"sshd": "run: sshd: (pid 631) 10s\n",
                    "dbus": "run: dbus: (pid 700) 10s\n"})
        out = Sink()
        rc = cli._cycle_services(_config([], restart_skip=["udevd", "dbus"]),
                                 out, run, service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["sudo", "sv", "restart", "sshd"], calls)
        self.assertNotIn(["sudo", "sv", "restart", "dbus"], calls)  # not flagged
        self.assertIn("restarted", out.text())

    def test_skips_restart_skip_service_exit_60(self):
        self._mk("dbus")
        run, calls = self._run(
            xcr="700 /usr/bin/dbus-daemon (dbus)\n",
            status={"dbus": "run: dbus: (pid 700) 10s\n"})
        out = Sink()
        rc = cli._cycle_services(_config([], restart_skip=["dbus"]),
                                 out, run, service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_SERVICES)
        self.assertNotIn(["sudo", "sv", "restart", "dbus"], calls)
        self.assertIn("restart_skip", out.text())

    def test_unmatched_pid_reported_not_fatal(self):
        self._mk("sshd")
        run, calls = self._run(
            xcr="9999 /usr/bin/rome (feral-rome)\n",
            status={"sshd": "run: sshd: (pid 631) 10s\n"})
        out = Sink()
        rc = cli._cycle_services(_config([]), out, run,
                                 service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertFalse(any(c[:3] == ["sudo", "sv", "restart"] for c in calls))
        self.assertIn("relogin", out.text())

    def test_nothing_flagged(self):
        run, calls = self._run(xcr="")
        out = Sink()
        rc = cli._cycle_services(_config([]), out, run,
                                 service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertFalse(any(c[:2] == ["sudo", "sv"] for c in calls))
        self.assertIn("none running replaced", out.text())

    def test_restart_failure_is_incomplete_exit_60(self):
        self._mk("sshd")
        run, calls = self._run(
            xcr="631 /usr/bin/sshd (openssh)\n",
            status={"sshd": "run: sshd: (pid 631) 10s\n"},
            restart_rc=1)
        out = Sink()
        rc = cli._cycle_services(_config([]), out, run,
                                 service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_SERVICES)
        self.assertIn("did not confirm", out.text())

    def test_restart_unconfirmed_is_incomplete_exit_60(self):
        # sv restart returns 0 but the service does not come back to `run:`.
        self._mk("sshd")
        run, calls = self._run(
            xcr="631 /usr/bin/sshd (openssh)\n",
            status={"sshd": "run: sshd: (pid 631) 10s\n"},
            status_after={"sshd": "down: sshd: 0s, normally up\n"})
        out = Sink()
        rc = cli._cycle_services(_config([]), out, run,
                                 service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_SERVICES)
        self.assertIn("did not confirm", out.text())

    def test_xcheckrestart_failure_warns_exit_60(self):
        run, calls = self._run(xcr_rc=1)
        out = Sink()
        rc = cli._cycle_services(_config([]), out, run,
                                 service_root=self.svcroot)
        self.assertEqual(rc, cli.EXIT_SERVICES)
        self.assertIn("xcheckrestart", out.text())
        self.assertFalse(any(c[:3] == ["sudo", "sv", "restart"] for c in calls))

    def test_parse_ignores_noise_and_deleted_suffix(self):
        flagged = cli._parse_xcheckrestart(
            "\n631 /usr/bin/sshd (deleted) (openssh)\n"
            "  /usr/lib/libfoo.so (deleted)\n"       # -v LIBS detail line
            "700 /usr/bin/dbus-daemon (dbus)\n")
        self.assertEqual([p for p, _ in flagged], [631, 700])


class PostVerifyTests(unittest.TestCase):
    """§7.7 post-deploy convergence gate (exit 52)."""

    REPO = {"/vp/hostdir/binpkgs", "/vp/hostdir/binpkgs/nonfree"}

    def test_converged_ok(self):
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "mesa-1.0_1"},
                      repo_ver={"mesa": "mesa-1.0_1"})   # origin defaults overlay
        out = Sink()
        self.assertEqual(cli._post_verify(["mesa"], xb, self.REPO, out),
                         cli.EXIT_OK)
        self.assertIn("converged", out.text())

    def test_nonoverlay_origin_is_52(self):
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "mesa-1.0_1"},
                      repo_ver={"mesa": "mesa-1.0_1"},
                      origins={"mesa": "https://upstream"})
        out = Sink()
        self.assertEqual(cli._post_verify(["mesa"], xb, self.REPO, out),
                         cli.EXIT_VERIFY)
        self.assertIn("still originates", out.text())

    def test_version_mismatch_is_52(self):
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "mesa-1.0_1"},
                      repo_ver={"mesa": "mesa-1.0_2"})
        out = Sink()
        self.assertEqual(cli._post_verify(["mesa"], xb, self.REPO, out),
                         cli.EXIT_VERIFY)
        self.assertIn("pkgver", out.text())

    def test_split_version_is_52(self):
        # two installed binpkgs of one srcpkg stuck at different versions
        xb = FakeXbps(installed=["qt", "qt-devel"],
                      src_map={"qt": "qt", "qt-devel": "qt"},
                      inst_ver={"qt": "qt-5.0_1", "qt-devel": "qt-devel-5.0_2"},
                      repo_ver={"qt": "qt-5.0_1", "qt-devel": "qt-devel-5.0_2"})
        out = Sink()
        self.assertEqual(cli._post_verify(["qt", "qt-devel"], xb, self.REPO, out),
                         cli.EXIT_VERIFY)
        self.assertIn("installed version", out.text())

    def test_kernel_target_excluded(self):
        # linux-cachy with an upstream origin must NOT trip post-verify — the
        # kernel is introduced/verified by §8.6, not here.
        xb = FakeXbps(installed=["linux-cachy"],
                      src_map={"linux-cachy": "linux-cachy"},
                      inst_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      repo_ver={"linux-cachy": "linux-cachy-6.12.35_1"},
                      origins={"linux-cachy": "https://upstream"})
        out = Sink()
        self.assertEqual(cli._post_verify(["linux-cachy"], xb, self.REPO, out),
                         cli.EXIT_OK)

    def test_empty_deploy_is_ok(self):
        xb = FakeXbps(installed=[], src_map={})
        out = Sink()
        self.assertEqual(cli._post_verify([], xb, self.REPO, out), cli.EXIT_OK)


class ArgparseTests(unittest.TestCase):

    def test_action_required(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args([])

    def test_actions_mutually_exclusive(self):
        with self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--check", "--commit"])

    def test_bad_config_path_returns_usage(self):
        out = Sink()
        rc = cli.main(["--check", "--config", "/no/such/file.toml"], out=out)
        self.assertEqual(rc, cli.EXIT_USAGE)


class RunnerStdinTests(unittest.TestCase):
    """_run must never let a child WAIT on a prompt (18h `make oldconfig` hang,
    §8.4 real-hardware finding): stdin is /dev/null, so stdin-readers see EOF."""

    def test_stdin_is_devnull(self):
        cp_ = cli._run(["cat"])          # would block forever on an inherited tty
        self.assertEqual(cp_.returncode, 0)
        self.assertEqual(cp_.stdout, "")


class StageKernelExternalTests(unittest.TestCase):
    """§8.6 external mode: _stage_kernel must record FULL bookkeeping (STAGED +
    candidate) while issuing zero bootloader commands — lumping foreign-GRUB
    hosts into skip meant healthy boots were never promoted (Medion finding)."""

    def test_external_records_staged_candidate_no_grub_commands(self):
        import tempfile as _tf
        from engine import grub as _grub
        tmp = _tf.mkdtemp()
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(tmp))
        xbps = FakeXbps(
            installed=["linux-cachy"],
            inst_ver={"linux-cachy": "6.12.103_1"},
            files_map={"linux-cachy": ["/boot/vmlinuz-6.12.103_1-cachy"]})
        calls = []

        def run(args, cwd=None):
            calls.append(list(args))
            if list(args)[:2] == ["uname", "-r"]:
                return cp(0, "6.12.95_1-cachy\n")
            return cp(0, "")
        layout = _grub.BootLayout(_grub.MODE_EXTERNAL, "foreign boot manager")
        out = Sink()
        rc = cli._stage_kernel(cfg, xbps, out, run, layout=layout)
        self.assertEqual(rc, cli.EXIT_OK)
        state = _grub.KernelStateStore(cfg.kernel_state_path).load()
        self.assertEqual(state["state"], "STAGED")
        self.assertEqual(state["candidate"]["kver"], "6.12.103_1-cachy")
        self.assertEqual(state["grub"]["mode"], "external")
        self.assertIsNone(state["grub"]["candidate_ref"])
        grubby = [c for c in calls if any("grub" in tok for tok in c)]
        self.assertEqual(grubby, [])            # foreign menu never touched
        self.assertIn("external bookkeeping", out.text())


class SyncRemoteTests(unittest.TestCase):
    """--sync must work with EITHER remote name: 'upstream' (manual setups) or
    'origin' (bootstrap.sh's plain clone — the hardcoded 'upstream' made every
    bootstrap-created checkout fail with exit 20 on the Medion)."""

    def _sync(self, remote_listing):
        calls = []

        def run(args, cwd=None):
            a = list(args)
            calls.append(a)
            if a == ["git", "remote"]:
                return cp(0, remote_listing)
            if a[:2] == ["git", "rev-parse"]:
                return cp(0, "abc123def456\n")
            return cp(0, "")
        rc = cli.cmd_sync(_config([]), out=Sink(), run=run)
        return rc, calls

    def test_plain_clone_uses_origin(self):
        rc, calls = self._sync("origin\n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["git", "fetch", "origin"], calls)
        self.assertIn(["git", "pull", "--rebase", "origin", "master"], calls)

    def test_upstream_preferred_when_present(self):
        rc, calls = self._sync("origin\nupstream\n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["git", "fetch", "upstream"], calls)
        self.assertIn(["git", "pull", "--rebase", "upstream", "master"], calls)


class StatusTests(unittest.TestCase):
    """--status: read-only, aggregates all tiers, degrades gracefully."""

    @staticmethod
    def _run(args):
        a = list(args)
        if a[:2] in (["xbps-install", "-Mun"], ["xbps-install", "-un"]):
            return cp(0, "foo-1.2_3 update x86_64\nbar-2.0_1 update x86_64\n")
        if a[0] == "xbps-remove":
            return cp(0, "orphan-1_1 x86_64\n")
        if a[:2] == ["vkpurge", "list"]:
            return cp(0, "6.12.30_1\n")
        if a[0] == "du":
            return cp(0, "512M\t/var/cache/xbps\n")
        if a[0] == "sh":
            return cp(0, "01:00.0 VGA compatible controller: NVIDIA GT 730M\n")
        if a[0] == "dkms":
            return cp(0, "nvidia/470.256.02, 6.12.95_1-cachy, x86_64: installed\n")
        if a[:2] == ["flatpak", "--version"]:
            return cp(0, "Flatpak 1.15.0\n")
        if a[:3] == ["flatpak", "remote-ls", "--updates"]:
            return cp(0, "org.mozilla.firefox\n")
        return cp(0, "")

    def test_reports_all_sections(self):
        xbps = FakeXbps()
        out = Sink()
        rc = cli.cmd_status(xbps, _config([]), out=out, run=self._run)
        self.assertEqual(rc, cli.EXIT_OK)
        t = out.text()
        for marker in ("[1] System", "2 upstream", "[2] Performance overlay",
                       "[3] Kernel", "[4] Maintenance", "orphaned packages: 1",
                       "6.12.30_1", "[5] GPU", "GT 730M", "nvidia/470",
                       "[6] Flatpak", "1 app(s) updatable"):
            self.assertIn(marker, t)

    def test_hold_lines_not_counted_as_updatable(self):
        # pinned kernels (`hold`) must not read as "updatable" — Update rightly
        # skips them, and counting them is false-alarm security (found on the
        # Medion: "4 updatable" that were all the pinned kernels)
        def run(args):
            if list(args)[:2] in (["xbps-install", "-Mun"],
                                  ["xbps-install", "-un"]):
                return cp(0, "foo-1.2_3 update x86_64\n"
                             "linux6.12-6.12.98_1 hold x86_64\n"
                             "linux6.18-6.18.40_1 hold x86_64\n")
            return cp(0, "")
        out = Sink()
        rc = cli.cmd_status(FakeXbps(), _config([]), out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("1 upstream package(s) updatable", out.text())
        self.assertIn("(+2 on hold)", out.text())

    def test_degrades_when_tools_missing(self):
        def boom(args):
            raise OSError("not found")
        out = Sink()
        rc = cli.cmd_status(FakeXbps(), _config([]), out=out, run=boom)
        self.assertEqual(rc, cli.EXIT_OK)          # never fails on a probe
        self.assertIn("[1] System", out.text())

    def test_status_is_wired_into_main(self):
        # --status must be a valid, read-only action (dispatches without mutation)
        out = Sink()
        rc = cli.main(["--status"], xbps=FakeXbps(), config=_config([]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)

    def test_overlay_query_failure_still_reports_later_tiers(self):
        """A broken void-packages must not hide the kernel/BORE-pin, maintenance
        and GPU tiers — the GUI's pin banner keys off tier [3] (found live: an
        unbootstrapped checkout truncated --status right before it)."""
        from engine.xbps import XbpsError

        class Boom(FakeXbps):
            def show_local_updates(self):
                raise XbpsError("./xbps-src: cannot run as root")

        out = Sink()
        rc = cli.cmd_status(Boom(), _config(["mesa"]), out=out, run=self._run)
        t = out.text()
        self.assertEqual(rc, cli.EXIT_QUERY)      # the failure is still reported
        self.assertIn("query failed", t)
        for marker in ("[3] Kernel", "[4] Maintenance", "[5] GPU", "[6] Flatpak"):
            self.assertIn(marker, t)


class CleanCommandTests(unittest.TestCase):
    """--clean: preview -> confirm -> remove orphans + cache; never purges kernels."""

    def _run(self, *, orphans="orphan1-1_1\norphan2-2_1\n", cache="cached-1_1\n",
             kernels="6.12.30_1\n", apply_rc=0):
        calls = []

        def run(args):
            a = list(args)
            if a[:2] == ["sudo", "-n"]:
                a = a[2:]
            calls.append(a)
            if a[:3] == ["xbps-remove", "-o", "-n"]:
                return cp(0, orphans)
            if a[:3] == ["xbps-remove", "-O", "-n"]:
                return cp(0, cache)
            if a[:2] == ["vkpurge", "list"]:
                return cp(0, kernels)
            if a[:3] == ["xbps-remove", "-o", "-y"]:
                return cp(apply_rc)
            if a[:3] == ["xbps-remove", "-O", "-y"]:
                return cp(apply_rc)
            return cp(0, "")
        return run, calls

    def test_previews_and_removes_with_yes(self):
        run, calls = self._run()
        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        t = out.text()
        self.assertIn("orphaned packages to remove: 2", t)
        self.assertIn("removed 2 orphaned package(s)", t)
        self.assertIn("cleaned obsolete package cache", t)
        self.assertIn(["xbps-remove", "-o", "-y"], calls)
        self.assertIn(["xbps-remove", "-O", "-y"], calls)

    def test_suggests_old_kernels_but_never_purges(self):
        run, calls = self._run()
        out = Sink()
        cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        self.assertIn("old kernel files present", out.text())
        self.assertIn("6.12.30_1", out.text())
        # it now also hands over the exact command instead of a vague hint
        self.assertIn("sudo vkpurge rm 6.12.30_1", out.text())
        # the invariant: no vkpurge rm is ever issued (§2.5/§4.7)
        self.assertFalse(any(c[:2] == ["vkpurge", "rm"] for c in calls))

    def test_dry_run_previews_and_removes_nothing(self):
        run, calls = self._run()
        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=True, dry_run=True,
                           out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("preview only", out.text())
        self.assertFalse(any(c[:3] == ["xbps-remove", "-o", "-y"] for c in calls))
        self.assertFalse(any(c[:3] == ["xbps-remove", "-O", "-y"] for c in calls))

    def test_refuses_orphan_sweep_containing_a_locally_built_package(self):
        """libgamemode (built here, ships libgamemodeauto.so.0 for gamemoderun)
        is orphan-eligible because nothing LINKS it — sweeping it would break
        the gaming layer silently. Real find on the live box."""
        calls = []

        def run(args):
            calls.append(list(args))
            a = list(args)
            if a[:1] == ["sudo"]:
                a = a[2:]
            if a[:4] == ["xbps-remove", "-o", "-n"]:
                return cp(0,
                          "libnma-1.10.6_1 remove x86_64 "
                          "https://repo-default.voidlinux.org/current 1 1\n"
                          "libgamemode-1.8.2_1 remove x86_64 "
                          "/vp/hostdir/binpkgs 1 1\n")
            if a[:4] == ["xbps-remove", "-O", "-n"]:
                return cp(0, "old-pkg-1_1\n")
            return cp(0, "")

        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        t = out.text()
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("REFUSING to remove orphans", t)
        self.assertIn("libgamemode-1.8.2_1", t)
        self.assertIn("sudo xbps-pkgdb -m manual libgamemode-1.8.2_1", t)
        # the sweep is skipped entirely (the flag cannot exclude one package)...
        flat = [c[2:] if c[:1] == ["sudo"] else c for c in calls]
        self.assertFalse(any(c[:3] == ["xbps-remove", "-o", "-y"] for c in flat))
        # ...while cache cleaning is unaffected
        self.assertTrue(any(c[:3] == ["xbps-remove", "-O", "-y"] for c in flat))

    def test_upstream_only_orphans_are_swept_normally(self):
        calls = []

        def run(args):
            calls.append(list(args))
            a = list(args)[2:] if list(args)[:1] == ["sudo"] else list(args)
            if a[:4] == ["xbps-remove", "-o", "-n"]:
                return cp(0, "libnma-1.10.6_1 remove x86_64 "
                             "https://repo-default.voidlinux.org/current 1 1\n")
            return cp(0, "")

        out = Sink()
        cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        flat = [c[2:] if c[:1] == ["sudo"] else c for c in calls]
        self.assertNotIn("REFUSING", out.text())
        self.assertTrue(any(c[:3] == ["xbps-remove", "-o", "-y"] for c in flat))

    def test_local_origin_detection_matches_repo_and_subrepo(self):
        lines = [
            "a-1_1 remove x86_64 /vp/hostdir/binpkgs 1 1",
            "b-1_1 remove x86_64 /vp/hostdir/binpkgs/nonfree 1 1",
            "c-1_1 remove x86_64 https://repo-default.voidlinux.org/current 1 1",
            "malformed line",
        ]
        hits = cli._local_origin_orphans(lines, ["/vp/hostdir/binpkgs"])
        self.assertEqual(hits, ["a-1_1", "b-1_1"])

    def test_nothing_to_clean(self):
        run, calls = self._run(orphans="", cache="")
        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("nothing to clean", out.text())
        self.assertFalse(any(c[:3] == ["xbps-remove", "-o", "-y"] for c in calls))

    def test_abort_on_no_confirmation(self):
        run, calls = self._run()
        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=False, out=out, run=run,
                           confirm=lambda *_: "n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("aborted by user", out.text())
        self.assertFalse(any(c[:3] == ["xbps-remove", "-o", "-y"] for c in calls))

    def test_removal_failure_is_exit_clean(self):
        run, _ = self._run(apply_rc=1)
        out = Sink()
        rc = cli.cmd_clean(_config([]), assume_yes=True, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_CLEAN)


class GpuCommandTests(unittest.TestCase):
    """--gpu: read-only advisory; detects card, driver, DKMS; degrades gracefully."""

    @staticmethod
    def _run_nvidia(args):
        a = list(args)
        if a[0] == "sh":
            return cp(0, "01:00.0 VGA compatible controller: NVIDIA GT 730M\n")
        if a[0] == "dkms":
            return cp(0, "nvidia/470.256.02, 6.12.95_1-cachy, x86_64: installed\n")
        if a[:2] == ["xbps-install", "-un"]:
            return cp(0, "")
        return cp(0, "")

    def test_nvidia_advisory(self):
        # inst_pkgver returns the full name-version (as the real Xbps does)
        xb = FakeXbps(installed=["nvidia470"],
                      inst_ver={"nvidia470": "nvidia470-470.256.02_1"})
        out = Sink()
        rc = cli.cmd_gpu(xb, _config([]), out=out, run=self._run_nvidia)
        self.assertEqual(rc, cli.EXIT_OK)
        t = out.text()
        self.assertIn("NVIDIA card present", t)
        self.assertIn("nvidia470 470.256.02_1", t)   # name not doubled
        self.assertIn("Kepler", t)          # the legacy-series hint
        self.assertIn("470.256.02", t)      # dkms line

    def test_warns_about_a_kernel_with_no_module_built(self):
        """The check the panel claimed to do but didn't: listing what DKMS HAS
        never reveals what is MISSING. Live case — nvidia470 builds for the 6.12
        kernels but not for the installed 6.18 series, so booting 6.18 silently
        drops to nouveau."""
        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GK107M [GeForce GT 730M]\n")
            if a[0] == "dkms":
                return cp(0, "nvidia/470.256.02, 6.12.103_1-cachy, x86_64: installed\n")
            if a[:2] == ["ls", "-1"]:
                return cp(0, "6.12.103_1-cachy\n6.18.38_1\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(installed=["nvidia470"],
                             inst_ver={"nvidia470": "nvidia470-470.256.02_2"}),
                    _config([]), out=out, run=run)
        t = out.text()
        self.assertIn("kernel 6.18.38_1 has NO out-of-tree module", t)
        self.assertIn("nouveau", t)
        self.assertNotIn("kernel 6.12.103_1-cachy has NO", t)

    def test_no_warning_when_every_kernel_is_covered(self):
        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GK107M [GeForce GT 730M]\n")
            if a[0] == "dkms":
                return cp(0, "nvidia/470.256.02, 6.12.103_1-cachy, x86_64: installed\n")
            if a[:2] == ["ls", "-1"]:
                return cp(0, "6.12.103_1-cachy\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=run)
        self.assertNotIn("NO out-of-tree module", out.text())

    def test_series_hint_is_suppressed_when_the_series_matches(self):
        """It is a 300-character wall; it earns its place only when something
        looks wrong."""
        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GK107M [GeForce GT 730M]\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(installed=["nvidia470"],
                             inst_ver={"nvidia470": "nvidia470-470.256.02_2"}),
                    _config([]), out=out, run=run)
        t = out.text()
        self.assertIn("driver series matches this card (Kepler -> nvidia470)", t)
        self.assertNotIn("driver series by GPU family", t)

    def test_wrong_series_warns_and_shows_the_table(self):
        def run(args):
            a = list(args)
            if a[0] == "sh":   # Turing card...
                return cp(0, "01:00.0 VGA: NVIDIA TU117M [GeForce GTX 1650]\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(installed=["nvidia470"],      # ...legacy driver
                             inst_ver={"nvidia470": "nvidia470-470.256.02_2"}),
                    _config([]), out=out, run=run)
        t = out.text()
        self.assertIn("WARNING: this looks like a Turing card", t)
        self.assertIn("driver series by GPU family", t)     # table shown on doubt

    def test_superseded_build_names_the_command_not_clean_up(self):
        """Clean up NEVER removes kernels (§2.5/§4.7), so a note saying 'see
        Clean up' promised something it deliberately will not do — a user who
        had just run Clean up was rightly confused the kernel was still there."""
        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GK107M\n")
            if a[0] == "dkms":
                return cp(0, "nvidia/470.256.02, 6.12.95_1-cachy, x86_64: installed\n")
            if a[:2] == ["ls", "-1"]:
                return cp(0, "6.12.95_1-cachy\n")
            if a[:2] == ["vkpurge", "list"]:
                return cp(0, "6.12.95_1-cachy\ncurrent\n")
            if a[0] == "du":
                return cp(0, "232448\t/lib/modules/6.12.95_1-cachy\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=run)
        t = out.text()
        self.assertIn("remove: sudo vkpurge rm 6.12.95_1-cachy", t)
        self.assertNotIn("see Clean up", t)

    def test_running_kernel_is_marked_in_the_dkms_list(self):
        import os as _os
        running = _os.uname().release

        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GK107M\n")
            if a[0] == "dkms":
                return cp(0, f"nvidia/470.256.02, {running}, x86_64: installed\n"
                             "nvidia/470.256.02, 9.99.9_1, x86_64: installed\n")
            if a[:2] == ["ls", "-1"]:
                return cp(0, f"{running}\n9.99.9_1\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=run)
        self.assertIn("<- running kernel", out.text())

    def test_dkms_kernel_column_parsed_in_both_layouts(self):
        installed = ["6.12.103_1-cachy"]
        modern = ["nvidia/470.256.02, 6.12.103_1-cachy, x86_64: installed"]
        legacy = ["nvidia, 470.256.02, 6.12.103_1-cachy, x86_64: installed"]
        self.assertEqual(cli.dkms_kernels(modern, installed),
                         {"6.12.103_1-cachy": "installed"})
        self.assertEqual(cli.dkms_kernels(legacy, installed),
                         {"6.12.103_1-cachy": "installed"})
        # and without a /lib/modules listing to match against
        self.assertEqual(cli.dkms_kernels(modern), {"6.12.103_1-cachy": "installed"})
        self.assertEqual(cli.dkms_kernels(legacy), {"6.12.103_1-cachy": "installed"})

    def test_chip_code_beats_marketing_name_for_series(self):
        self.assertEqual(cli.expected_nvidia_series("nvidia gk107m [geforce gt 730m]"),
                         ("nvidia470", "Kepler"))
        self.assertEqual(cli.expected_nvidia_series("nvidia gf119 [geforce gt 520]"),
                         ("nvidia390", "Fermi"))
        self.assertEqual(cli.expected_nvidia_series("nvidia tu117m"),
                         ("nvidia", "Turing"))
        self.assertEqual(cli.expected_nvidia_series("some unlabelled card"), ("", ""))

    def test_dkms_not_installed_warns(self):
        def run(args):
            a = list(args)
            if a[0] == "sh":
                return cp(0, "01:00.0 VGA: NVIDIA GT 730M\n")
            if a[0] == "dkms":
                return cp(0, "nvidia/470.256.02, 6.18.38_1, x86_64: added\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=run)
        self.assertIn("NOT 'installed'", out.text())

    def test_amd_path(self):
        def run(args):
            if list(args)[0] == "sh":
                return cp(0, "01:00.0 VGA: Advanced Micro Devices AMD Radeon\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=run)
        self.assertIn("AMD card", out.text())

    def test_degrades_when_tools_missing(self):
        def boom(args):
            raise OSError("no lspci")
        out = Sink()
        rc = cli.cmd_gpu(FakeXbps(), _config([]), out=out, run=boom)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("GPU & drivers", out.text())

    def test_gpu_wired_into_main(self):
        out = Sink()
        rc = cli.main(["--gpu"], xbps=FakeXbps(), config=_config([]), out=out)
        self.assertEqual(rc, cli.EXIT_OK)


class FlatpakTests(unittest.TestCase):
    """Flatpak is folded into the Update — present→update, absent→silent no-op,
    failure→loud (never false 'up to date')."""

    @staticmethod
    def _fp(*, present=True, has_system=False, user_rc=0, sys_rc=0):
        calls = []

        def run(args):
            a = list(args)
            if a[:2] == ["sudo", "-n"]:
                a = a[2:]
            calls.append(a)
            if a[:2] == ["flatpak", "--version"]:
                return cp(0 if present else 127, "1.15\n" if present else "")
            if a[:3] == ["flatpak", "update", "--user"]:
                return cp(user_rc)
            if a[:3] == ["flatpak", "list", "--system"]:
                return cp(0, "org.foo.App\n" if has_system else "")
            if a[:3] == ["flatpak", "update", "--system"]:
                return cp(sys_rc)
            return cp(0, "")
        return run, calls

    def test_noop_when_flatpak_absent(self):
        run, calls = self._fp(present=False)
        out = Sink()
        rc = cli._update_flatpak(_config([]), out, run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertFalse(any(c[:3] == ["flatpak", "update", "--user"] for c in calls))

    def test_updates_user_only_when_no_system_apps(self):
        run, calls = self._fp(present=True, has_system=False)
        out = Sink()
        rc = cli._update_flatpak(_config([]), out, run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["flatpak", "update", "--user", "-y"], calls)
        # no spurious system update (would trigger a needless sudo/polkit hit)
        self.assertFalse(any(c[:3] == ["flatpak", "update", "--system"] for c in calls))

    def test_updates_system_when_system_apps_present(self):
        run, calls = self._fp(present=True, has_system=True)
        out = Sink()
        rc = cli._update_flatpak(_config([]), out, run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn(["flatpak", "update", "--system", "-y"], calls)

    def test_failure_is_loud_not_silent(self):
        run, _ = self._fp(present=True, user_rc=1)
        out = Sink()
        rc = cli._update_flatpak(_config([]), out, run)
        self.assertEqual(rc, cli.EXIT_FLATPAK)          # never a false 'up to date'
        self.assertIn("did NOT apply", out.text())

    def test_commit_empty_queue_still_updates_flatpak(self):
        cfg = _config(["mesa"], kernel_enable=False)
        origin = cfg.repo_strs[0]                        # match overlay → empty queue
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
                      local_updates=[], origins={"mesa": origin})
        run, calls = self._fp(present=True)
        out = Sink()
        rc = cli.cmd_commit(xb, cfg, assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("queue empty", out.text())
        self.assertIn(["flatpak", "update", "--user", "-y"], calls)

    def test_dry_run_does_not_touch_flatpak(self):
        cfg = _config(["mesa"], kernel_enable=False)
        origin = cfg.repo_strs[0]
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
                      local_updates=[], origins={"mesa": origin})
        run, calls = self._fp(present=True)
        cli.cmd_commit(xb, cfg, assume_yes=True, dry_run=True, out=Sink(), run=run)
        self.assertFalse(any(c[:2] == ["flatpak", "update"] for c in calls))


class NoKernelScopeTests(unittest.TestCase):
    """--no-kernel scopes a run to userspace by disabling kernel_enable."""

    def test_flag_disables_kernel_enable(self):
        cfg = _config(["mesa"])              # kernel_enable defaults True
        self.assertTrue(cfg.kernel_enable)
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
                      local_updates=[])
        cli.main(["--check", "--no-kernel"], xbps=xb, config=cfg, out=Sink())
        self.assertFalse(cfg.kernel_enable)

    def test_kernel_gate_off_yields_no_kernel_build(self):
        # with kernel_enable False, the K-exemption never queues linux-cachy
        cfg = _config(["mesa"])
        cfg.kernel_enable = False
        self.assertEqual(cli._always_build(cfg), [])


class OldKernelInventoryTests(unittest.TestCase):
    """§2.5/§4.7: leftover kernels are annotated and warned about, never purged."""

    def test_current_symlink_is_never_offered_for_removal(self):
        """`vkpurge list` prints "current" — the /boot/vmlinuz-current SYMLINK.
        Listing it as removable would invite deleting the boot symlink."""
        items = cli.classify_old_kernels(["6.12.95_1-cachy", "current"])
        self.assertEqual([i.kver for i in items], ["6.12.95_1-cachy"])

    def test_roles_protect_the_fallback_and_the_running_kernel(self):
        items = cli.classify_old_kernels(
            ["6.12.90_1-cachy", "6.12.95_1", "6.12.103_1-cachy"],
            known_good="6.12.95_1", running="6.12.103_1-cachy")
        roles = {i.kver: i.role for i in items}
        self.assertEqual(roles["6.12.95_1"], "fallback")
        self.assertEqual(roles["6.12.103_1-cachy"], "running")
        self.assertEqual(roles["6.12.90_1-cachy"], "removable")

    def test_keepers_are_labelled_and_only_spares_get_a_remove_command(self):
        items = cli.classify_old_kernels(
            ["6.12.90_1-cachy", "6.12.95_1"], known_good="6.12.95_1",
            size_of=lambda _k: 232448)
        text = "\n".join(cli.old_kernel_lines(items))
        self.assertIn("6.12.95_1 — 227 MB   [rollback target: KEEP]", text)
        self.assertIn("sudo vkpurge rm 6.12.90_1-cachy", text)
        self.assertNotIn("sudo vkpurge rm 6.12.95_1\n", text + "\n")

    def test_single_spare_is_not_warned_about(self):
        items = cli.classify_old_kernels(["6.12.90_1-cachy"])
        self.assertNotIn("piling up", "\n".join(cli.old_kernel_lines(items)))

    def test_more_than_one_spare_warns_with_total_size(self):
        """The owner's call: don't auto-purge, but don't let them pile up
        silently either."""
        items = cli.classify_old_kernels(
            ["6.12.80_1-cachy", "6.12.90_1-cachy", "6.12.95_1"],
            known_good="6.12.95_1", size_of=lambda _k: 232448)
        text = "\n".join(cli.old_kernel_lines(items))
        self.assertIn("warning: 2 superseded kernels are piling up", text)
        self.assertIn("454 MB", text)
        self.assertIn("one spare is enough", text)

    def test_size_unknown_degrades_gracefully(self):
        items = cli.classify_old_kernels(["6.12.90_1-cachy"],
                                         size_of=lambda _k: None)
        self.assertIn("size unknown", cli.old_kernel_lines(items)[0])


class RollbackVisibilityTests(unittest.TestCase):
    """--status must announce that recovery is possible; the GUI keys its
    rollback button off that line."""

    def _cfg_and_state(self, tmp, known_good):
        (Path(tmp) / "kernel").mkdir(exist_ok=True)
        (Path(tmp) / "kernel" / "kernel-state.json").write_text(json.dumps({
            "schema": 1, "state": "TRACKING", "base_series": "6.12",
            "known_good": {"kver": known_good, "grub_ref": None}}),
            encoding="utf-8")
        lock = Path(tmp) / "bore.lock"
        lock.write_text('[repo]\nurl = "u"\npinned_commit = "' + "a" * 40 +
                        '"\n\n[[patch]]\nseries = "6.12"\nfile = "f"\n'
                        f'sha256 = "{"0" * 64}"\n', encoding="utf-8")
        return cli.Config(void_packages=Path(tmp) / "vp", state_dir=Path(tmp),
                          bore_lock=lock)

    class _Xbps:
        vercmp = staticmethod(_vercmp)

    def test_marker_present_when_running_differs_from_known_good(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg_and_state(tmp, "6.12.95_1")   # not what we run
            out = Sink()
            cli._kernel_report(cfg, self._Xbps(), out)
            self.assertIn("rollback available", out.text())

    def test_no_marker_when_running_is_the_known_good(self):
        import os as _os
        with tempfile.TemporaryDirectory() as tmp:
            cfg = self._cfg_and_state(tmp, _os.uname().release)
            out = Sink()
            cli._kernel_report(cfg, self._Xbps(), out)
            self.assertNotIn("rollback available", out.text())


class PinBoreCommandTests(unittest.TestCase):
    """§8.3a assisted pin: preview writes nothing, approval appends the pin
    and seeds the patch cache, refusals/duplicates change nothing."""

    PATCH = b"@@ BORE @@\n"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        (tmp / "kernel").mkdir()
        (tmp / "kernel" / "kernel-state.json").write_text(
            json.dumps({"schema": 1, "state": "TRACKING",
                        "base_series": "6.15"}), encoding="utf-8")
        self.lock_path = tmp / "bore.lock"
        self.lock_path.write_text(
            '[repo]\nurl = "https://example/bore"\n'
            f'pinned_commit = "{"a" * 40}"\n\n'
            '[[patch]]\nseries = "6.12"\nfile = "patches/x.patch"\n'
            f'sha256 = "{"0" * 64}"\n', encoding="utf-8")
        self.cfg = cli.Config(void_packages=tmp / "vp", state_dir=tmp,
                              bore_lock=self.lock_path)

        from engine.trust import PinProposal, sha256_bytes
        self.proposal = PinProposal(
            series="6.15", repo_url="https://example/bore", commit="c" * 40,
            file="patches/stable/linux-6.15-bore/0001.patch",
            sha256=sha256_bytes(self.PATCH), bore_version="6.7.0",
            size=len(self.PATCH))
        self.discover = lambda **kw: (self.proposal, self.PATCH)

    def _lock_text(self):
        return self.lock_path.read_text(encoding="utf-8")

    def test_dry_run_previews_and_writes_nothing(self):
        out = Sink()
        rc = cli.cmd_pin_bore(self.cfg, out, dry_run=True,
                              discover=self.discover)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("Proposed BORE pin", out.text())
        self.assertIn(self.proposal.sha256, out.text())
        self.assertNotIn("6.15", self._lock_text())
        self.assertFalse(self.cfg.kernel_patch_path.exists())

    def test_approval_pins_and_seeds_cache(self):
        out = Sink()
        rc = cli.cmd_pin_bore(self.cfg, out, discover=self.discover,
                              ask=lambda _q: "y")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("6.15", self._lock_text())
        self.assertIn(self.proposal.sha256, self._lock_text())
        self.assertEqual(self.cfg.kernel_patch_path.read_bytes(), self.PATCH)

    def test_assume_yes_skips_prompt(self):
        rc = cli.cmd_pin_bore(self.cfg, Sink(), assume_yes=True,
                              discover=self.discover,
                              ask=lambda _q: self.fail("must not prompt"))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("6.15", self._lock_text())

    def test_decline_writes_nothing(self):
        before = self._lock_text()
        rc = cli.cmd_pin_bore(self.cfg, Sink(), discover=self.discover,
                              ask=lambda _q: "n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(self._lock_text(), before)

    def test_already_pinned_is_a_noop(self):
        out = Sink()
        (Path(self._tmp.name) / "kernel" / "kernel-state.json").write_text(
            json.dumps({"base_series": "6.12"}), encoding="utf-8")
        rc = cli.cmd_pin_bore(self.cfg, out,
                              discover=lambda **kw: self.fail("no fetch"))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("already pinned", out.text())

    def test_discovery_failure_maps_to_query_exit(self):
        from engine import trust

        def failing(**kw):
            raise trust.PatchUnavailable("no patch upstream")
        rc = cli.cmd_pin_bore(self.cfg, Sink(), discover=failing)
        self.assertEqual(rc, cli.EXIT_QUERY)

    class _XbpsStub:
        vercmp = staticmethod(_vercmp)

    def test_kernel_report_flags_missing_pin(self):
        """--status tier [3] must carry the GUI's banner marker."""
        out = Sink()
        cli._kernel_report(self.cfg, self._XbpsStub(), out)
        self.assertIn("BORE pin: MISSING", out.text())

    def test_kernel_report_shows_existing_pin(self):
        (Path(self._tmp.name) / "kernel" / "kernel-state.json").write_text(
            json.dumps({"base_series": "6.12"}), encoding="utf-8")
        out = Sink()
        cli._kernel_report(self.cfg, self._XbpsStub(), out)
        self.assertIn("BORE pin: series 6.12 pinned", out.text())
        self.assertNotIn("BORE pin: MISSING", out.text())


class HealthDaemonConfigTests(unittest.TestCase):
    """A missing/unloadable config must PARK the health daemon, never crash-loop
    it under runit (finding: cachy-health spun before updater.toml existed)."""

    def _patch_park(self):
        seen = {}
        orig = cli._park
        cli._park = lambda out, reason: (seen.update(reason=reason), 0)[1]
        self.addCleanup(lambda: setattr(cli, "_park", orig))
        return seen

    def test_health_daemon_parks_on_missing_config(self):
        seen = self._patch_park()
        rc = cli.main(["--health-daemon", "--config", "/no/such/updater.toml"],
                      out=Sink())
        self.assertEqual(rc, 0)                     # parked, not EXIT_USAGE
        self.assertIn("parking", seen["reason"])

    def test_other_actions_still_error_on_missing_config(self):
        # only the daemon parks; one-shot actions must still surface the error
        out = Sink()
        rc = cli.main(["--check", "--config", "/no/such/updater.toml"], out=out)
        self.assertEqual(rc, cli.EXIT_USAGE)
        self.assertIn("cannot load config", out.text())


if __name__ == "__main__":
    unittest.main()


class StagedCandidateReadoutTests(unittest.TestCase):
    """§8.6 staged-candidate visibility: a kernel that is built, installed and
    waiting for its trial boot used to appear NOWHERE in --status. The advice
    printed with it must match the host's boot class — promising an automatic
    return to the known-good kernel on a host that cannot do one is worse than
    saying nothing."""

    def _state(self, tmp, **over):
        st = grub_mod.default_state(base_series="6.12", ported_version="6.12.95_1")
        st.update(over)
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(tmp))
        grub_mod.KernelStateStore(cfg.kernel_state_path).save(st)
        return cfg

    def _report(self, cfg):
        out = Sink()
        cli._kernel_report(cfg, FakeXbps(), out)
        return out.text()

    def test_staged_oneshot_promises_the_automatic_return(self):
        tmp = tempfile.mkdtemp()
        cfg = self._state(
            tmp, state="STAGED",
            candidate={"kver": "6.12.103_1-cachy"},
            known_good={"kver": "6.12.95_1"},
            grub={"mode": grub_mod.MODE_ONESHOT})
        t = self._report(cfg)
        self.assertIn("kernel candidate: 6.12.103_1-cachy is staged", t)
        self.assertIn("awaiting its trial boot", t)
        self.assertIn("next power cycle returns to 6.12.95_1", t)

    def test_staged_external_says_the_fallback_is_manual(self):
        # The multi-boot truth: no one-shot exists here, so the honest line is
        # "pick it yourself in the foreign menu".
        tmp = tempfile.mkdtemp()
        cfg = self._state(
            tmp, state="STAGED",
            candidate={"kver": "6.12.103_1-cachy"},
            known_good={"kver": "6.12.95_1"},
            grub={"mode": grub_mod.MODE_EXTERNAL})
        t = self._report(cfg)
        self.assertIn("is staged", t)
        self.assertIn("foreign bootloader owns the menu", t)
        self.assertIn("pick 6.12.95_1 there yourself", t)
        self.assertNotIn("on its own", t)          # never promise the one-shot

    def test_confirming_says_it_is_on_trial(self):
        tmp = tempfile.mkdtemp()
        cfg = self._state(tmp, state="CONFIRMING",
                          candidate={"kver": "6.12.103_1-cachy"},
                          known_good={"kver": "6.12.95_1"},
                          grub={"mode": grub_mod.MODE_EXTERNAL})
        self.assertIn("ON TRIAL", self._report(cfg))

    def test_unhealthy_candidate_is_reported_as_frozen(self):
        tmp = tempfile.mkdtemp()
        cfg = self._state(tmp, state="CANDIDATE_UNHEALTHY",
                          candidate={"kver": "6.12.103_1-cachy"},
                          known_good={"kver": "6.12.95_1"},
                          grub={"mode": grub_mod.MODE_EXTERNAL})
        t = self._report(cfg)
        self.assertIn("did NOT pass", t)
        self.assertIn("userspace updates continue", t)

    def test_tracking_state_says_nothing_about_candidates(self):
        # No candidate in flight => no noise. The readout must not invent state.
        tmp = tempfile.mkdtemp()
        cfg = self._state(tmp, state="TRACKING", known_good={"kver": "6.12.95_1"})
        self.assertNotIn("kernel candidate:", self._report(cfg))


class BootPathReportTests(unittest.TestCase):
    """§8.6b: after staging, say whether the kernel can actually be booted."""

    def _stage(self, layout, chk):
        import tempfile as _tf
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(_tf.mkdtemp()))
        xbps = FakeXbps(installed=["linux-cachy"],
                        inst_ver={"linux-cachy": "6.12.103_1"},
                        files_map={"linux-cachy": ["/boot/vmlinuz-6.12.103_1-cachy"]})

        def run(args, cwd=None):
            if list(args)[:2] == ["uname", "-r"]:
                return cp(0, "6.12.95_1\n")
            return cp(0, "")
        orig = grub_mod.verify_bootable
        seen = {}

        def fake(*, layout, kver, **kw):
            seen["kver"] = kver
            seen["mode"] = layout.mode
            return chk
        grub_mod.verify_bootable = fake
        try:
            out = Sink()
            rc = cli._stage_kernel(cfg, xbps, out, run, layout=layout)
        finally:
            grub_mod.verify_bootable = orig
        return rc, out.text(), seen

    def test_external_staging_reports_the_boot_path(self):
        chk = grub_mod.BootCheck(grub_mod.BOOT_OK, "boot-symlink",
                                 "a /boot symlink now points at 6.12.103_1-cachy",
                                 hint="booting an OLDER kernel means repointing it")
        rc, text, seen = self._stage(
            grub_mod.BootLayout(grub_mod.MODE_EXTERNAL, "foreign"), chk)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(seen["kver"], "6.12.103_1-cachy")   # the CANDIDATE, not pkgver
        self.assertIn("boot check: a /boot symlink now points at", text)
        self.assertIn("repointing it", text)                 # the hint is surfaced

    def test_absent_entry_is_flagged_as_a_warning(self):
        chk = grub_mod.BootCheck(grub_mod.BOOT_ABSENT, "grub-menu",
                                 "grub.cfg has NO boot entry for 6.12.103_1-cachy",
                                 hint="re-run the hooks")
        rc, text, _ = self._stage(
            grub_mod.BootLayout(grub_mod.MODE_MANUAL_UNSAFE, "GRUB_DEFAULT not saved",
                                grub_cfg="/boot/grub/grub.cfg"), chk)
        self.assertIn("WARNING — boot check:", text)
        self.assertIn("NO boot entry", text)

    def test_unknown_is_not_a_warning(self):
        # "we could not look" must never be styled like "your kernel is broken".
        chk = grub_mod.BootCheck(grub_mod.BOOT_UNKNOWN, "grub-menu",
                                 "could not read /boot/grub/grub.cfg")
        rc, text, _ = self._stage(
            grub_mod.BootLayout(grub_mod.MODE_MANUAL_UNSAFE, "GRUB_DEFAULT not saved",
                                grub_cfg="/boot/grub/grub.cfg"), chk)
        self.assertIn("boot check: could not read", text)
        self.assertNotIn("WARNING", text)


class PendingProbeTests(unittest.TestCase):
    """--pending: the cheap machine-readable probe a poller can live on.

    Two properties matter more than the field list. It must NEVER exit non-zero
    (a tray reading a non-zero code would report the updater as broken when the
    real story is "the mirror was down"), and it must carry the *policy* in
    `attention` so front-ends never re-derive "is this worth showing?" and drift
    from what the window says.
    """

    def _cfg(self, tmp, **over):
        st = grub_mod.default_state(base_series="6.12", ported_version="6.12.95_1")
        st.update(over)
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(tmp))
        grub_mod.KernelStateStore(cfg.kernel_state_path).save(st)
        return cfg

    def _probe(self, cfg, run):
        out = Sink()
        rc = cli.cmd_pending(cfg, out=out, run=run)
        return rc, json.loads(out.text())

    @staticmethod
    def _run_ok(args):
        a = list(args)
        if a == ["xbps-install", "-Mun"]:
            return cp(0, "foo-1.2_3 update x86_64\nbar-2.0_1 install x86_64\n"
                         "linux6.12-6.12.98_1 hold x86_64\n")
        return cp(0, "")

    def test_counts_actionable_updates_and_holds_separately(self):
        cfg = self._cfg(tempfile.mkdtemp())
        rc, d = self._probe(cfg, self._run_ok)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(d["upstream"],
                         {"updatable": 2, "held": 1, "drivers": 0})
        self.assertTrue(d["fresh"])
        self.assertIn("updates", d["attention"])

    def test_memory_sync_is_what_gets_run(self):
        # -M is the whole reason this can be honest without root: it fetches the
        # remote index into memory, so the count is fresh and nothing is written.
        seen = []

        def run(args):
            seen.append(list(args))
            return cp(0, "")
        self._probe(self._cfg(tempfile.mkdtemp()), run)
        self.assertEqual(seen[0], ["xbps-install", "-Mun"])

    def test_falls_back_to_the_cache_and_says_so(self):
        def run(args):
            if list(args) == ["xbps-install", "-Mun"]:
                return cp(16, "", "failed to fetch")
            return cp(0, "foo-1.2_3 update x86_64\n")
        rc, d = self._probe(self._cfg(tempfile.mkdtemp()), run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertFalse(d["fresh"])
        self.assertEqual(d["upstream"]["updatable"], 1)
        self.assertTrue(any("stale" in n for n in d["notes"]))

    def test_missing_xbps_is_reported_not_raised(self):
        def run(args):
            raise OSError("no xbps here")
        rc, d = self._probe(self._cfg(tempfile.mkdtemp()), run)
        self.assertEqual(rc, cli.EXIT_OK)          # a probe never fails the caller
        self.assertEqual(d["upstream"]["updatable"], 0)
        self.assertNotIn("updates", d["attention"])
        self.assertTrue(any("unavailable" in n for n in d["notes"]))

    def test_staged_kernel_raises_its_own_attention_flag(self):
        cfg = self._cfg(tempfile.mkdtemp(), state="STAGED",
                        candidate={"kver": "6.12.103_1-cachy"},
                        known_good={"kver": "6.12.95_1"},
                        grub={"mode": grub_mod.MODE_EXTERNAL})
        _, d = self._probe(cfg, self._run_ok)
        self.assertIn("kernel-staged", d["attention"])
        self.assertEqual(d["kernel"]["candidate"], "6.12.103_1-cachy")
        self.assertEqual(d["kernel"]["mode"], "external")

    def test_unhealthy_candidate_is_a_distinct_flag(self):
        # A front-end paints this differently from "there are updates", so the
        # two must never collapse into one token.
        cfg = self._cfg(tempfile.mkdtemp(), state="CANDIDATE_UNHEALTHY",
                        candidate={"kver": "6.12.103_1-cachy"},
                        known_good={"kver": "6.12.95_1"})
        _, d = self._probe(cfg, self._run_ok)
        self.assertIn("kernel-unhealthy", d["attention"])
        self.assertNotIn("kernel-staged", d["attention"])

    def test_quiet_system_raises_no_update_flag(self):
        # Nothing pending must mean no badge. (The BORE-pin flag is not asserted
        # either way here: bore.lock lives outside the temp state dir, so its
        # presence depends on the checkout, not on this probe.)
        def run(args):
            return cp(0, "")
        _, d = self._probe(self._cfg(tempfile.mkdtemp()), run)
        self.assertEqual(d["upstream"]["updatable"], 0)
        self.assertNotIn("updates", d["attention"])

    def test_unreadable_kernel_state_degrades_in_band(self):
        cfg = cli.Config(void_packages=Path("/vp"),
                         state_dir=Path("/nonexistent-cachy-state"))
        rc, d = self._probe(cfg, self._run_ok)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("updates", d["attention"])   # the upstream half still works

    def test_output_is_a_single_json_document(self):
        # The contract is machine-readable: no banner, no trailing prose.
        out = Sink()
        cli.cmd_pending(self._cfg(tempfile.mkdtemp()), out=out, run=self._run_ok)
        json.loads(out.text())                     # would raise on any extra text
        self.assertEqual(out.text().count('"schema"'), 1)

    def test_pending_is_wired_into_main_without_a_solver(self):
        # If --pending ever needed the solver it would stop being cheap; passing
        # xbps=None proves the dispatch happens before build_xbps().
        out = Sink()
        rc = cli.main(["--pending"], config=self._cfg(tempfile.mkdtemp()), out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        json.loads(out.text())


class SnapshotCommandTests(unittest.TestCase):
    """--snapshots: make the safety net visible, and never touch it.

    The point of this command is that a net nobody can find is only reached for
    after it was needed. So the tests care about two things: that each snapshot
    is ANNOTATED with what that run actually did (a list of subvolume names tells
    a user nothing about which one to go back to), and that nothing here mutates
    anything — the restore commands are printed for a human to run.
    """

    LISTING = (
        "ID 309 gen 3904 top level 281 path .cachy-snapshots/deploy-20260811T011412Z\n"
        "ID 377 gen 4818 top level 281 path .cachy-snapshots/deploy-20260813T180630Z\n"
        "ID 380 gen 5795 top level 281 path .cachy-snapshots/de-trial-plasma-20260820T002346Z\n")

    TESTBED_FSTAB = "UUID=785e634b / btrfs defaults 0 0\n"

    def _cfg(self, journals=None):
        tmp = Path(tempfile.mkdtemp())
        logs = tmp / "log"
        for run_id, data in (journals or {}).items():
            d = logs / f"run-{run_id}"
            d.mkdir(parents=True)
            (d / "journal.json").write_text(json.dumps(data), encoding="utf-8")
        logs.mkdir(parents=True, exist_ok=True)
        return cli.Config(void_packages=Path("/vp"), state_dir=tmp, log_root=logs,
                          snapshot_dir="/.cachy-snapshots", snapshot_subvol="/")

    def _run_ok(self, args):
        a = list(args)
        if a[:4] == ["sudo", "btrfs", "subvolume", "list"]:
            return cp(0, self.LISTING)
        return cp(0, "")

    def _out(self, cfg, run=None, fstab=None):
        """Run the command with /etc/fstab faked, since the recipe depends on it."""
        real = Path.read_text
        text = self.TESTBED_FSTAB if fstab is None else fstab

        def fake(self, *a, **kw):
            if str(self) == "/etc/fstab":
                return text
            return real(self, *a, **kw)
        out = Sink()
        Path.read_text = fake
        try:
            rc = cli.cmd_snapshots(cfg, out=out, run=run or self._run_ok)
        finally:
            Path.read_text = real
        return rc, out.text()

    def test_lists_every_snapshot_with_its_age_and_purpose(self):
        rc, t = self._out(self._cfg())
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("deploy-20260811T011412Z", t)
        self.assertIn("de-trial-plasma-20260820T002346Z", t)
        self.assertIn("taken before trying a desktop (plasma)", t)
        self.assertIn("automatic, taken just before an update", t)

    def test_non_automatic_snapshots_are_marked_as_kept(self):
        # Only deploy-* is ever pruned; saying so stops a user assuming their
        # hand-made bookmark will survive on its own.
        _, t = self._out(self._cfg())
        line = [l for l in t.splitlines() if "only automatic ones are pruned" in l]
        self.assertTrue(line)

    def test_a_deploy_snapshot_is_annotated_from_its_journal(self):
        cfg = self._cfg({"20260811T011412Z": {
            "schema": 1, "phase": "done",
            "deploy_bins": ["openbox", "mesa", "gamemode"], "failure": None}})
        _, t = self._out(cfg)
        self.assertIn("3 overlay packages deployed (openbox, mesa, gamemode)", t)

    def test_an_upstream_only_run_says_so_rather_than_nothing(self):
        cfg = self._cfg({"20260811T011412Z": {
            "schema": 1, "phase": "done", "deploy_bins": [], "failure": None}})
        _, t = self._out(cfg)
        self.assertIn("upstream-only update (no overlay rebuild)", t)

    def test_a_failed_run_is_flagged_because_that_changes_the_choice(self):
        cfg = self._cfg({"20260813T180630Z": {
            "schema": 1, "phase": "failed", "deploy_bins": ["qt5"],
            "failure": {"pkg": "qt5", "exit": 40}}})
        _, t = self._out(cfg)
        self.assertIn("run FAILED on qt5", t)

    def test_a_missing_journal_is_simply_unannotated(self):
        _, t = self._out(self._cfg())          # no journals at all
        self.assertIn("deploy-20260811T011412Z", t)
        self.assertNotIn("overlay package", t)

    def test_the_recipe_is_for_this_host_not_a_doc_pointer(self):
        _, t = self._out(self._cfg())
        self.assertIn("sudo btrfs subvolume set-default", t)
        self.assertIn("pre-restore-", t)       # the current root is saved first
        self.assertIn("sudo reboot", t)
        self.assertIn("set-default 5", t)      # and the way back

    def test_a_pinned_layout_refuses_with_a_reason(self):
        _, t = self._out(self._cfg(),
                         fstab="UUID=abc / btrfs rw,subvol=@ 0 1\n")
        self.assertNotIn("sudo btrfs subvolume set-default /", t)
        self.assertIn("silently ignored", t)

    def test_it_says_out_loud_that_it_ran_nothing(self):
        _, t = self._out(self._cfg())
        self.assertIn("only ever reads", t)

    def test_it_issues_no_command_beyond_the_granted_list(self):
        seen = []

        def run(args):
            seen.append(list(args))
            return cp(0, self.LISTING)
        self._out(self._cfg(), run=run)
        self.assertEqual(seen, [["sudo", "btrfs", "subvolume", "list", "-o",
                                 "/.cachy-snapshots"]])

    def test_no_snapshots_explains_rather_than_printing_an_empty_list(self):
        _, t = self._out(self._cfg(), run=lambda a: cp(0, ""))
        self.assertIn("none found", t)
        self.assertIn("btrfs root", t)

    def test_a_denied_listing_does_not_fail_the_command(self):
        rc, t = self._out(self._cfg(), run=lambda a: cp(1, "", "denied"))
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("none found", t)

    def test_snapshots_is_wired_into_main_without_a_solver(self):
        real = Path.read_text

        def fake(self, *a, **kw):
            if str(self) == "/etc/fstab":
                return SnapshotCommandTests.TESTBED_FSTAB
            return real(self, *a, **kw)
        Path.read_text = fake
        try:
            out = Sink()
            rc = cli.main(["--snapshots"], config=self._cfg(), out=out)
        finally:
            Path.read_text = real
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("Cachy-Void — snapshots", out.text())


class KernelAckTests(unittest.TestCase):
    """§8.8 — the escape from a frozen kernel path.

    This exists because a real laptop reached CANDIDATE_UNHEALTHY (a dropped
    WiFi connection, no kernel staged) and there was no way out: the spec names
    `cachy-void-update kernel ack` and the CLI never grew it. So the tests care
    that it clears the freeze, that it does NOTHING else, and that it refuses to
    pretend when there is nothing frozen.
    """

    def _cfg(self, **over):
        tmp = tempfile.mkdtemp()
        st = grub_mod.default_state(base_series="6.12", ported_version="6.12.95_1")
        st.update(over)
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(tmp))
        grub_mod.KernelStateStore(cfg.kernel_state_path).save(st)
        return cfg

    def _state(self, cfg):
        return grub_mod.KernelStateStore(cfg.kernel_state_path).load()

    def test_the_real_world_case_clears_with_no_candidate(self):
        # Exactly the state found on the box: frozen, healthy, nothing staged.
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=None,
                        known_good={"kver": "6.12.103_1-cachy"},
                        health={"ok": True, "checks": {"H4_network": True},
                                "ts": "2026-08-24T23:39:46Z",
                                "consecutive_failures": 0})
        out = Sink()
        rc = cli.cmd_kernel_ack(cfg, out=out, assume_yes=True)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(self._state(cfg)["state"], "TRACKING")
        self.assertIn("no candidate is recorded", out.text())

    def test_it_names_the_freeze_and_says_userspace_is_unaffected(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=None)
        out = Sink()
        cli.cmd_kernel_ack(cfg, out=out, assume_yes=True)
        t = out.text()
        self.assertIn("CANDIDATE_UNHEALTHY", t)
        self.assertIn("userspace updates are unaffected", t)

    def test_a_real_candidate_is_archived_not_erased(self):
        # The episode is history worth keeping; silently dropping it would make
        # a repeat failure look like a first one.
        cand = {"pkgver": "6.12.103_1", "kver": "6.12.103_1-cachy"}
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=cand)
        cli.cmd_kernel_ack(cfg, out=Sink(), assume_yes=True)
        st = self._state(cfg)
        self.assertEqual(st["state"], "TRACKING")
        self.assertIsNone(st["candidate"])
        self.assertEqual(len(st["history"]), 1)
        self.assertEqual(st["history"][0]["candidate"], cand)
        self.assertEqual(st["history"][0]["state"], "CANDIDATE_UNHEALTHY")

    def test_it_refuses_to_pretend_when_nothing_is_frozen(self):
        cfg = self._cfg(state="TRACKING")
        out = Sink()
        rc = cli.cmd_kernel_ack(cfg, out=out, assume_yes=True)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("nothing to acknowledge", out.text())
        self.assertEqual(self._state(cfg)["state"], "TRACKING")

    def test_a_staged_candidate_is_not_something_to_acknowledge(self):
        # STAGED is a normal, in-flight state — clearing it would discard a
        # kernel that is simply waiting for its trial boot.
        cfg = self._cfg(state="STAGED",
                        candidate={"kver": "6.12.103_1-cachy"})
        cli.cmd_kernel_ack(cfg, out=Sink(), assume_yes=True)
        self.assertEqual(self._state(cfg)["state"], "STAGED")

    def test_every_frozen_state_is_clearable(self):
        for name in cli.FROZEN_STATES:
            cfg = self._cfg(state=name)
            cli.cmd_kernel_ack(cfg, out=Sink(), assume_yes=True)
            self.assertEqual(self._state(cfg)["state"], "TRACKING", name)

    def test_declining_the_prompt_changes_nothing(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=None)
        out = Sink()
        rc = cli.cmd_kernel_ack(cfg, out=out, assume_yes=False, ask=lambda _p: "n")
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertIn("left frozen", out.text())
        self.assertEqual(self._state(cfg)["state"], "CANDIDATE_UNHEALTHY")

    def test_a_closed_stdin_is_treated_as_no(self):
        def eof(_p):
            raise EOFError
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY")
        cli.cmd_kernel_ack(cfg, out=Sink(), assume_yes=False, ask=eof)
        self.assertEqual(self._state(cfg)["state"], "CANDIDATE_UNHEALTHY")

    def test_it_installs_removes_and_stages_nothing(self):
        # An ack is bookkeeping. If it ever needs a subprocess, that is a
        # different command with a different confirmation.
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY",
                        known_good={"kver": "6.12.103_1-cachy"})
        before = self._state(cfg)
        cli.cmd_kernel_ack(cfg, out=Sink(), assume_yes=True)
        after = self._state(cfg)
        self.assertEqual(after["known_good"], before["known_good"])
        self.assertEqual(after["ported_version"], before["ported_version"])
        self.assertEqual(after["base_series"], before["base_series"])

    def test_it_is_wired_into_main(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY")
        out = Sink()
        rc = cli.main(["--kernel-ack", "--yes"], config=cfg, out=out)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertEqual(self._state(cfg)["state"], "TRACKING")


class FrozenStateVisibilityTests(unittest.TestCase):
    """A frozen kernel path must be VISIBLE, with or without a candidate.

    The original readout required a candidate to say anything, so the box that
    prompted all this reported a clean bill of health while its kernel path was
    frozen — the failure mode being fixed, not a hypothetical.
    """

    def _cfg(self, **over):
        tmp = tempfile.mkdtemp()
        st = grub_mod.default_state(base_series="6.12", ported_version="6.12.95_1")
        st.update(over)
        cfg = cli.Config(void_packages=Path("/vp"), state_dir=Path(tmp))
        grub_mod.KernelStateStore(cfg.kernel_state_path).save(st)
        return cfg

    def test_status_reports_a_freeze_that_has_no_candidate(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=None,
                        known_good={"kver": "6.12.103_1-cachy"})
        out = Sink()
        cli._kernel_report(cfg, FakeXbps(), out)
        t = out.text()
        self.assertIn("FROZEN", t)
        self.assertIn("--kernel-ack", t)
        self.assertIn("Userspace updates are unaffected", t)

    def test_status_still_names_the_candidate_when_there_is_one(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY",
                        candidate={"kver": "6.12.103_1-cachy"},
                        known_good={"kver": "6.12.95_1"})
        out = Sink()
        cli._kernel_report(cfg, FakeXbps(), out)
        t = out.text()
        self.assertIn("6.12.103_1-cachy did NOT pass", t)
        self.assertIn("--kernel-ack", t)

    def test_pending_flags_a_candidateless_freeze_distinctly(self):
        # A front-end paints "a kernel failed" differently from "the path is
        # frozen for no visible reason", so the tokens stay distinct.
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY", candidate=None)
        out = Sink()
        cli.cmd_pending(cfg, out=out, run=lambda a: cp(0, ""))
        d = json.loads(out.text())
        self.assertIn("kernel-frozen", d["attention"])
        self.assertNotIn("kernel-unhealthy", d["attention"])

    def test_pending_flags_a_real_unhealthy_candidate_as_before(self):
        cfg = self._cfg(state="CANDIDATE_UNHEALTHY",
                        candidate={"kver": "6.12.103_1-cachy"})
        out = Sink()
        cli.cmd_pending(cfg, out=out, run=lambda a: cp(0, ""))
        d = json.loads(out.text())
        self.assertIn("kernel-unhealthy", d["attention"])

    def test_a_healthy_tracking_box_flags_neither(self):
        cfg = self._cfg(state="TRACKING")
        out = Sink()
        cli.cmd_pending(cfg, out=out, run=lambda a: cp(0, ""))
        d = json.loads(out.text())
        self.assertNotIn("kernel-frozen", d["attention"])
        self.assertNotIn("kernel-unhealthy", d["attention"])


class MarchLabelTests(unittest.TestCase):
    """The overlay tier used to hard-code x86-64-v3 and printed it on a v2 box —
    a small lie, in the one pane whose whole job is telling the truth."""

    def test_it_reads_the_real_march_from_the_build_profile(self):
        tmp = Path(tempfile.mkdtemp())
        (tmp / "etc").mkdir()
        (tmp / "etc" / "conf").write_text(
            'XBPS_CFLAGS="-march=x86-64-v2 -mtune=generic -O3"\n', encoding="utf-8")
        cfg = cli.Config(void_packages=tmp, state_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cli._march_label(cfg), " / x86-64-v2")

    def test_an_unreadable_profile_says_nothing_rather_than_guessing(self):
        cfg = cli.Config(void_packages=Path("/nonexistent-vp"),
                         state_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(cli._march_label(cfg), "")


class CountAgreementTests(unittest.TestCase):
    """--status and --pending must never report different numbers.

    They did: the tray memory-synced (fresh) while the window read the on-disk
    cache (stale), so the same machine showed 20 in the tray and 16 in the
    window, both defensible and together useless. One helper now serves both,
    so a divergence has to be introduced deliberately.
    """

    LISTING = ("foo-1.2_3 update x86_64\n"
               "bar-2.0_1 update x86_64\n"
               "baz-3.0_1 install x86_64\n"
               "linux6.12-6.12.98_1 hold x86_64\n")

    def _run(self, args):
        a = list(args)
        if a[:2] == ["xbps-install", "-Mun"]:
            return cp(0, self.LISTING)
        return cp(0, "")

    def test_both_front_ends_see_the_same_counts(self):
        cfg = _config([])
        st = Sink()
        cli.cmd_status(FakeXbps(), cfg, out=st, run=self._run)
        pd = Sink()
        cli.cmd_pending(cfg, out=pd, run=self._run)
        data = json.loads(pd.text())
        self.assertIn(f"{data['upstream']['updatable']} upstream package(s) "
                      "updatable", st.text())
        self.assertIn(f"(+{data['upstream']['held']} on hold)", st.text())
        self.assertEqual(data["upstream"],
                         {"updatable": 3, "held": 1, "drivers": 0})

    def test_status_memory_syncs_rather_than_trusting_the_cache(self):
        seen = []

        def run(args):
            seen.append(list(args))
            return cp(0, self.LISTING)
        cli.cmd_status(FakeXbps(), _config([]), out=Sink(), run=run)
        self.assertIn(["xbps-install", "-Mun"], seen)

    def test_an_unreachable_mirror_is_disclosed_in_both(self):
        def run(args):
            a = list(args)
            if a[:2] == ["xbps-install", "-Mun"]:
                return cp(16, "", "failed to fetch")
            if a[:2] == ["xbps-install", "-un"]:
                return cp(0, self.LISTING)
            return cp(0, "")
        st = Sink()
        cli.cmd_status(FakeXbps(), _config([]), out=st, run=run)
        self.assertIn("mirror unreachable", st.text())
        pd = Sink()
        cli.cmd_pending(_config([]), out=pd, run=run)
        self.assertFalse(json.loads(pd.text())["fresh"])


class DriverAdviceTests(unittest.TestCase):
    """What to do about a new graphics driver is said AFTER it is installed.

    It used to ride in the pending summary, where "restart apps to use it" sat
    beside "20 packages to update" and invited the fair question: use what, the
    driver I have not installed yet? Advice belongs at the moment it becomes
    actionable, and only for what actually changed.
    """

    def test_nothing_is_said_when_no_driver_changed(self):
        out = Sink()
        cli.report_driver_change({"mesa": "26.1.7_1"}, {"mesa": "26.1.7_1"}, out)
        self.assertEqual(out.text(), "")

    def test_a_userspace_gl_update_explains_reopening_not_rebooting(self):
        out = Sink()
        cli.report_driver_change({"mesa": "26.1.7_1"}, {"mesa": "26.1.8_1"}, out)
        t = out.text()
        self.assertIn("mesa 26.1.7_1 -> 26.1.8_1", t)
        self.assertIn("close and reopen", t)
        self.assertIn("no reboot", t)

    def test_a_kernel_module_driver_asks_for_a_reboot(self):
        out = Sink()
        cli.report_driver_change({"nvidia470": "470.256.02_1"},
                                 {"nvidia470": "470.260.00_1"}, out)
        t = out.text()
        self.assertIn("REBOOT", t)
        self.assertNotIn("close and reopen", t)

    def test_a_newly_installed_driver_is_not_reported_as_changed(self):
        # No "before" version means it was not an update; saying "updated" would
        # be wrong, and there is nothing the user must do about a fresh install.
        out = Sink()
        cli.report_driver_change({}, {"mesa": "26.1.8_1"}, out)
        self.assertEqual(out.text(), "")

    def test_both_kinds_at_once_each_get_their_own_advice(self):
        out = Sink()
        cli.report_driver_change(
            {"mesa": "26.1.7_1", "nvidia470": "470.256.02_1"},
            {"mesa": "26.1.8_1", "nvidia470": "470.260.00_1"}, out)
        t = out.text()
        self.assertIn("REBOOT", t)
        self.assertIn("close and reopen", t)

    def test_the_pending_status_line_carries_no_instruction(self):
        # The status tier may explain what a driver IS, but must not tell the
        # user to act on something not yet installed.
        xb = FakeXbps(installed=["mesa"], src_map={"mesa": "mesa"},
                      inst_ver={"mesa": "26.1.7_1"})

        def run(args):
            a = list(args)
            if a[:2] == ["xbps-install", "-un"] and "mesa" in a:
                return cp(0, "mesa-26.1.8_1 update x86_64\n")
            if a[:2] in (["xbps-install", "-Mun"], ["xbps-install", "-un"]):
                return cp(0, "mesa-26.1.8_1 update x86_64\n")
            return cp(0, "")
        out = Sink()
        cli.cmd_status(xb, _config([]), out=out, run=run)
        t = out.text()
        self.assertIn("one of the system packages above", t)
        self.assertNotIn("restart apps to use it", t)


class DriverSplitTests(unittest.TestCase):
    """Which pending packages count as hardware drivers or firmware.

    Wider than "GPU" on purpose: the testbed alone carries intel-ucode, sof- and
    alsa-firmware, ipw2100/2200-firmware and five linux-firmware-* blobs, so a
    label saying "gpu" would be wrong the day any of those moves (the owner
    asked precisely this).
    """

    def test_the_graphics_stack_counts(self):
        for name in ("mesa", "mesa-dri", "mesa-libgallium", "mesa-32bit",
                     "mesa-dri-32bit", "libgbm", "libgbm-32bit", "libdrm",
                     "nvidia470", "nvidia470-dkms", "xf86-video-intel"):
            self.assertTrue(cli.is_driver_pkg(name), name)

    def test_cpu_microcode_and_firmware_count_too(self):
        for name in ("intel-ucode", "amd-ucode", "linux-firmware-intel",
                     "linux-firmware-network", "sof-firmware", "alsa-firmware",
                     "ipw2200-firmware"):
            self.assertTrue(cli.is_driver_pkg(name), name)

    def test_ordinary_packages_do_not(self):
        for name in ("firefox", "perl", "python3-yaml", "steam", "autoconf",
                     "wpa_supplicant", "libz3", "liblilv", "xdg-user-dirs"):
            self.assertFalse(cli.is_driver_pkg(name), name)

    def test_the_split_matches_the_real_pending_list(self):
        # The testbed's actual 20 pending updates, verbatim: eight of them are
        # one mesa release (four packages, each with a 32-bit twin).
        listing = (
            "linux6.12-6.12.104_1 hold\n"
            "linux6.12-headers-6.12.104_1 hold\n"
            "autoconf-2.73_1 update\nfirefox-154.0_1 update\n"
            "firefox-i18n-da-154.0_1 update\nlibgbm-26.1.8_1 update\n"
            "libgbm-32bit-26.1.8_1 update\nliblilv-0.28.0_1 update\n"
            "libsord-0.16.22_1 update\nlibz3-5.1.0_1 update\n"
            "libz3-32bit-5.1.0_1 update\nmesa-26.1.8_1 update\n"
            "mesa-32bit-26.1.8_1 update\nmesa-dri-26.1.8_1 update\n"
            "mesa-dri-32bit-26.1.8_1 update\nmesa-libgallium-26.1.8_1 update\n"
            "mesa-libgallium-32bit-26.1.8_1 update\nperl-5.42.3_1 update\n"
            "python3-yaml-6.0.3_1 update\nsteam-1.0.0.87_1 update\n"
            "wpa_supplicant-2.12_2 update\nxdg-user-dirs-0.20_2 update\n")
        n, held, fresh, drivers = cli.upstream_counts(
            lambda a: cp(0, listing) if a[:2] == ["xbps-install", "-Mun"] else cp(1))
        self.assertEqual((n, held, drivers), (20, 2, 8))
        self.assertTrue(fresh)

    def test_status_states_the_split_rather_than_a_lump(self):
        listing = ("mesa-26.1.8_1 update\nfirefox-154.0_1 update\n"
                   "perl-5.42.3_1 update\n")

        def run(args):
            a = list(args)
            if a[:2] in (["xbps-install", "-Mun"], ["xbps-install", "-un"]):
                return cp(0, listing)
            return cp(0, "")
        out = Sink()
        cli.cmd_status(FakeXbps(), _config([]), out=out, run=run)
        self.assertIn("2 package update(s), 1 driver/firmware update(s)", out.text())
