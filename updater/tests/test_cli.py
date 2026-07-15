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
                 configure_rc=0, build_rc=0):
        self._installed = list(installed)
        self._src_map = dict(src_map or {})
        self._inst_ver = dict(inst_ver or {})
        self._repo_ver = dict(repo_ver or {})
        self._local_updates = list(local_updates)
        self._sort_ok = sort_ok
        self._origins = dict(origins or {})
        self._configure_rc = configure_rc
        self._build_rc = build_rc
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
        return FakeXbps(installed=["gamemode"],
                        src_map={"gamemode": "gamemode"},
                        inst_ver={"gamemode": "1.0_1"},
                        repo_ver={"gamemode": "1.0_1"},
                        origins={"gamemode": "https://upstream"})

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
        # O-term recovery end-to-end: no build, but -Su + forced takeover run.
        out = Sink()
        run, calls = self._runstub()
        rc = cli.cmd_commit(self._orphaned_takeover_xbps(),
                            self._cfg(["gamemode"]),
                            assume_yes=True, dry_run=False, out=out, run=run)
        self.assertEqual(rc, cli.EXIT_OK)
        self.assertTrue(any(c[:2] == ["sudo", "xbps-install"] and "-Suy" in c
                            for c in calls))
        self.assertTrue(any("-fy" in c and "gamemode" in c
                            for c in calls if c[0] == "sudo"))

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
                      local_updates=["linux-cachy"])
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
                      repo_ver={})                        # no binpkg yet -> M
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


if __name__ == "__main__":
    unittest.main()
