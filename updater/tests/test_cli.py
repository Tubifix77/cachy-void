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


class StatusTests(unittest.TestCase):
    """--status: read-only, aggregates all tiers, degrades gracefully."""

    @staticmethod
    def _run(args):
        a = list(args)
        if a[:2] == ["xbps-install", "-un"]:
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
        return cp(0, "")

    def test_reports_all_sections(self):
        xbps = FakeXbps()
        out = Sink()
        rc = cli.cmd_status(xbps, _config([]), out=out, run=self._run)
        self.assertEqual(rc, cli.EXIT_OK)
        t = out.text()
        for marker in ("[1] System", "2 upstream", "[2] Performance overlay",
                       "[3] Kernel", "[4] Maintenance", "orphaned packages: 1",
                       "6.12.30_1", "[5] GPU", "GT 730M", "nvidia/470"):
            self.assertIn(marker, t)

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
        self.assertIn("old kernels present", out.text())
        self.assertIn("6.12.30_1", out.text())
        # the invariant: no vkpurge rm is ever issued (§2.5/§4.7)
        self.assertFalse(any(c[:2] == ["vkpurge", "rm"] for c in calls))

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
        xb = FakeXbps(installed=["nvidia470"], inst_ver={"nvidia470": "470.256.02_1"})
        out = Sink()
        rc = cli.cmd_gpu(xb, _config([]), out=out, run=self._run_nvidia)
        self.assertEqual(rc, cli.EXIT_OK)
        t = out.text()
        self.assertIn("NVIDIA card present", t)
        self.assertIn("nvidia470 470.256.02_1", t)
        self.assertIn("Kepler", t)          # the legacy-series hint
        self.assertIn("470.256.02", t)      # dkms line

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


if __name__ == "__main__":
    unittest.main()
