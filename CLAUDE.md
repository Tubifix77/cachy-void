# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

**Done — installed, live, and in continuous daily use** (this was the vacation
machine). The distro overlay, branding, runit services, and the Python updater engine
(`updater/engine/`: xbps, grub, snapshot, trust, journal, health, health_daemon) are all
built and covered by a real test suite (`updater/tests/`, 15 files + a shell harness; 528 tests). `architecture.md`
remains the authoritative spec for anything new; if anything disagrees with it, the spec
wins.

Real-world usage surfaces real gaps, and they get fixed as they're found — that's
normal operation, not unfinished work. Case in point already in the commit history: bare
dhcpcd wasn't enough on the laptop, so `--with-networkmanager` was added as an opt-in
WiFi picker (NetworkManager + nm-tray), including icon-visibility fixes and a clean
`--uninstall` path back to dhcpcd.

**The updater has now been exercised for real, end to end (2026-08).** On the live
laptop it has: applied a full upstream system update through the GUI (~36 packages,
auto pre-deploy btrfs snapshot, services cycled without dropping the session), and
closed the complete kernel circuit unattended — sync → BORE patch trust → template
regeneration (6.12.103) → G2 gate → overnight compile → package → install → nvidia
DKMS rebuild → reboot onto the new kernel, verified healthy (BORE live, 1000 Hz, full
preempt, BBR built-in). Those runs surfaced and fixed real gaps (empty-queue system
pass §4.5a, GUI silence, `--sync` remote naming, two non-interactive `oldconfig`
prompt hangs — the fragment must answer *every* symbol, including choice options that
become NEW). The §8.7 confirm/promote path has now ALSO run for real: the run exposed that
foreign-bootloader hosts were lumped into "skip" (no candidate, no promotion ever), that
the CLI daemon entrypoint never called the confirm layer at all, and that the H1/H2
probes were structurally always-false unprivileged (`sv status`/`dmesg` denied) — all
fixed (new §8.6 `external` class, confirm-before-watchdog made normative, narrow sudo
fallbacks), and verified live: STAGED → CONFIRMING → battery green → **PROMOTE**,
`ported_version` advanced to the self-built kernel, state TRACKING.
**Still genuinely untested:** the Void-owned-GRUB one-shot boot-test + auto-rollback
(§8.6 oneshot choreography — this box's GRUB is Debian's, so it runs the external
class; the oneshot path remains code-reviewed + mock-tested only).

**Milestone 2026-08-26 — the desktop integration and the updater's surfaces are
hardware-verified too.** All four desktops are branded on the testbed (LXQt, the
bare Openbox session, Plasma, Xfce), each owner-approved on a real screen; Xfce
alone took a dozen live fix commits no offline test could have found, and running
two desktops side by side (Xephyr) exposed a latent shared-half bug that had been
silently misrouting every write since Plasma. The updater: a 90-package upstream
update via the GUI; the tray verified on Plasma and Xfce and refreshed by the GUI's
events over its instance-lock socket; the Update confirm stating whether a press
compiles; `--pending`'s attention vocabulary enumerated and test-enforced against
the tray (two tokens had been emitted and displayed by nobody); and `--status`
naming the §4.9 scheduled run — because the owner found the laptop compiling a
kernel at 3am and did not know the feature existed. That last one is the lesson of
the month: documented and opt-in was not enough; the running program has to say it.

## Desktop Branding Is Dispatched, Not Assumed

`cachy-de-detect` is the single detector, called by `deploy.sh` at install time and
by `cachy-branding` at apply time so the two cannot drift. One brandable desktop is
branded silently; several and the user is asked; the answer is recorded in
`/etc/cachy-void/branding-targets`. `cachy-branding` is split into `apply_shared`
(Tier 1), `apply_openbox_session`, `apply_lxqt` and `apply_shell`, with Plasma and
Xfce in their own `cachy-branding-plasma` and `cachy-branding-xfce`; the dispatcher
runs the shared half always and the appliers only for resolved targets.
`--desktop|--de lxqt,plasma,xfce|auto` overrides,
and `--dry-run` reports what would be applied and *why* without writing anything
(it is allowed as root precisely so it works in a container).

**Test it without hardware** — three layers, only one of which needs a machine:
the decision (`test_de_detect.py`, `test_branding_dispatch.py`, `--dry-run`), the
files written (`updater/tests/dispatch-isolation.sh`, real appliers into disjoint
`HOME`s — plus one shared `HOME`, because two appliers editing the same file is a
different failure from two appliers leaking into each other), and the look (a
real login; nothing offline substitutes). The isolation harness runs in the Void
WSL sandbox given a non-root user and `kf6-kconfig`.

## What This Project Is

"Cachy-Void": a performance overlay on stock Void Linux — CachyOS-style tuning (x86-64-v3/`-O3` compilation, BORE-patched 1000 Hz kernel, aggressive sysctl/zram/udev config) while preserving Void's fortes (runit as PID 1, no systemd anywhere, clean XBPS dependency resolution). The base system stays upstream Void binaries; only a curated allowlist of performance-critical packages is built locally, orchestrated by a Python updater (`architecture.md` §4).

## Target Platform Caveat

All code targets **Void Linux** (XBPS, `xbps-src`, runit). This development machine is
Windows, but that has not been a real barrier: a Void Linux WSL instance on this same
Windows 11 box was used for extensive real testing — actual `xbps`/`runit` execution, not
just mocked `subprocess` calls — before the overlay was ever installed onto the laptop's
live Void partition. Unit tests still mock `subprocess` for the fast/offline gate, but
"can't be executed locally" is not an accurate description of how this was actually
built and validated.

One trap of developing POSIX scripts from Windows, hit repeatedly: **Windows Python's
`write_text()` turns every `\n` into `\r\n`**, so a read-modify-write of a shell script
rewrites the whole file with CRLF and Void then fails it with `/usr/bin/env: 'bash\r'`.
Always write with `newline="\n"`, and check `read_bytes().count(b"\r\n")` before
deploying. The Python suite will not catch it — it runs under Linux Python in WSL.

## Invariants To Never Violate (spec §0, I1–I7)

**Scope — read this before judging "is X against the philosophy?" or proposing any addition.** These invariants govern how packages are **built/packaged** and the **init system** — NOT which stock apps you install or which system defaults you set. `xbps-install` of any stock prebuilt Void binary that runs under runit is always the blessed path (identical in kind to installing mesa or gamemode). Installing/removing an app, choosing a tray applet, or swapping a system default (e.g. `dhcpcd` → NetworkManager) is normal user config — **never** an invariant question. You only cross a line by: source-building outside the allowlist (with the v3/`-O3` toolchain), editing upstream-tracked `void-packages` files, touching the bootstrap blacklist, or bringing in systemd. **Decision rubric:** build/packaging/init action → check I1–I7 below; install/remove/config a stock thing → allowed, stop. Re-check against the *literal* invariants — do **not** extrapolate from a sense of "lean" (that is taste, not a rule; see the recurring mistake in memory `invariants-are-build-scoped`).

- **Additive overlay only**: never modify upstream-tracked `void-packages` files; customizations are new `srcpkgs/*` dirs or untracked `etc/conf`. This is what keeps `git pull --rebase` conflict-free. (This is a *build-tree* rule — it is about `void-packages`, not about system config or which services you run.)
- **Bootstrap blacklist**: `glibc`, `musl`, `gcc`, `binutils`, `xbps`, `runit`, `base-files` are never built or replaced locally. Blacklist beats allowlist.
- **runit only**: services via runit dirs, scheduling via `snooze`, zram via `zramen`. No systemd units/timers/zram-generator.
- **Fail-fast, system intact**: no system mutation unless all builds succeeded; single sudo boundary — exactly the commands enumerated in spec §4.1 (`xbps-install`, `sv`, `xcheckrestart`, `xbps-pkgdb`, the three GRUB staging binaries `grub-set-default`/`grub-reboot`/`grub-editenv`, and the §9.5 btrfs snapshot ops `btrfs subvolume snapshot -r`/`delete`/`list`), plus the user-facing maintenance/"update everything" grants — `xbps-remove -o|-O -n|-y` (`--clean`: orphans + cache only; these flags cannot name a package), `flatpak update --system -y` (folded into Update; only refreshes installed refs), and the read-only `dmesg --level=emerg,alert,crit` (§8.7 H2 — `dmesg_restrict=1` denies the unprivileged daemon) — nothing else. Note `btrfs subvolume create` is deliberately **not** in the updater's grant — the `/.cachy-snapshots` subvol is created once by `deploy.sh` (which already runs as root); the engine only takes/prunes snapshots inside it. `vkpurge` is deliberately **not** granted: kernel purges stay manual (§2.5/§4.7); `--clean` only *suggests* them.
- **Deps stay binary**: only allowlist targets compile from source; never recursively source-build dependencies.
- **32-bit multilib stays upstream**: never cross-build i686 with `x86-64-v*` flags.
- **No `mitigations=off`**, and `-f`/`--force` is sanctioned only for the same-pkgver overlay takeover (spec §4.6).

## What The Overlay May Change Without Asking

A line worth holding, because "it would be faster without X" is a recurring
temptation and the answer is not always yes:

- **Invisible tuning is ours to set** — sysctls, THP, zram, the §3.3 watchdog
  blacklist, udev rules, compiler flags. Nobody loses a feature they would notice.
- **User-facing features are the user's to choose.** We add, and we *say things*;
  we do not quietly remove or disable what someone may be using. Precedent: when
  `--clean` risked taking `libgamemode` it was made to **refuse**, not to be
  clever; branding is opt-in and reversible; a desktop is never installed for you.
- **When something is genuinely costly but genuinely a feature, name it and give
  the command.** Plasma's `baloo` file indexer is the worked example: real
  background I/O on a gaming box, and also what powers KRunner's file-content
  search, so `cachy-branding-plasma` reports it and prints `balooctl6 disable`
  rather than deciding. Same family as the nvidia-Wayland warning, the missing
  `kded6` notice, and the tier-1 "here is where the assets are" message.

This is not an invariant (see the build-scoped rubric above) — it is a taste rule
about intrusiveness, and it is the reason the overlay has stayed additive in
*behaviour* as well as in packaging.

## Architecture Notes That Are Easy To Get Wrong

- **Kernel**: `srcpkgs/linux-cachy` is a fork of Void's own kernel template + BORE patch in `patches/` — *not* linux-tkg or XanMod. Unique pkgname means upstream never touches it, but also that it doesn't auto-update: the updater only warns on version drift (spec §2.6).
- **Same-version shadowing**: local rebuilds share `pkgver` with upstream binaries, so `xbps-install -Su` alone won't switch an installed package to the local build. The forced-reinstall step and repo-priority config in spec §4.6 exist for this; don't "simplify" them away.
- **Queue formula lives in §7.3** (it supersedes §4.3): `M` term = allowlist members never built (without it, a fresh setup builds nothing, since `show-local-updates` is empty against an empty repo); `P` term = built-but-never-deployed packages (closes the orphan hole when a run dies mid-build); `O` term = same-version packages whose installed *origin* is still an upstream mirror (closes the takeover hole when a run dies between `-Su` and the §4.6 `-f` loop). Recovery is always recomputed from live queries — the journal is witness-only, there is deliberately no `--resume` flag.
- **Name domains (spec §7.1)**: templates are srcpkgs, installed packages are binpkgs; map binpkg→srcpkg via the `srcpkgs/<sub>` symlink convention. No-widen rule: the updater never installs a binpkg that isn't already installed. Version comparisons delegate to `xbps-uhelper cmpver`, never reimplemented.
- **Kernel state machine (spec §8)**: the template is *regenerated* from upstream each bump, never incrementally patched. The G2 config gate exists because `oldconfig` silently drops unknown symbols — a failed BORE patch otherwise ships a stock-scheduler kernel that "built fine"; a G2 failure withholds the kernel but never blocks userspace updates. `bore.lock` is only ever updated by a human. GRUB one-shot staging requires a grubenv-writable `/boot` (ext*/vfat; btrfs/zfs/LVM degrade to safe `manual` mode), and staging **refuses** (exit 70) when `GRUB_DEFAULT≠saved` (`manual-unsafe`) — the saved-default edit is `deploy.sh --with-grub`'s job, never the updater's. `ported_version` advances only on a healthy boot (PROMOTED), not on a successful build.
- **Rollback**: a corrupted repo index is fixed by deleting `x86_64-repodata` and re-running `xbps-rindex -a`. `xbps-pkgdb -m repounlock` is *not* an index-repair tool (an old draft claimed this; the spec explicitly retires it).
- **Deliberate tuning values** (spec §2.4, §3.1): `vm.swappiness = 100` (zram-paired), `vm.max_map_count = 2147483642`, `kernel.sched_rt_runtime_us = -1`, 1000 Hz + full preemption. These are intentional, aggressive choices — do not normalize them to conventional defaults.
- **The §7.5 preflight is real now, and a frozen kernel state is enforced.** Both were in the spec from day one and unimplemented until a six-hour unattended kernel build died for disk space (2026-09-06). `build_preflight` refuses below `[build] min_free_gib` (30) or without a masterdir marker, exit 31, journaled, kernel state untouched. A failed `linux-cachy` build records `AWAIT_HUMAN_BUILD`; any `FROZEN_STATES` member skips synthesis *and* withholds the kernel from the queue until `--kernel-ack`. A failed build tree is *kept* on purpose (§7.5 forensics) — do not "fix" that; it is named in `--status` with its reclaim command instead. Tests stub the preflight (`_commit` in `test_cli.py`) because a temp dir cannot promise 30 GiB; `PreflightTests` inject the disk verdict. The floor SCALES: `min_free_gib` (30) only when `linux-cachy` is queued, `min_free_userspace_gib` (5) otherwise — the first version applied 30 to everything and refused a routine update within the hour.
- **The build space is relocatable, and `Config.build_space` is the only place to ask where it is.** `[build] masterdir` moves the chroot to another disk (passed to xbps-src as `-m`, never written into `etc/conf`, which `deploy.sh` regenerates). Four things must agree and are easy to desynchronise: `build_preflight`, `disk_lines`, `grub.locate_dotconfig`'s G2 glob, and `build_xbps`. **Only the masterdir moves** — the hostdir holds `binpkgs`, referenced by absolute path in `/etc/xbps.d`, so on removable media an unplug would delete the overlay repo from xbps' view. Setting it never escalates: `/etc/cachy-void/updater.toml` is root-owned by design, and the CLI prints the command instead.
- **`--no-kernel` must REMOVE the kernel from the queue, not just set a flag.** `kernel_enable=False` gates synthesis, G2 and the freeze check — but an installed `linux-cachy` with a newer template still enters the queue by the ordinary outdated-and-installed rule (`_always_build` covers only the K-exemption). Before this was fixed, *Update* would compile a kernel **and skip G2**. Any new kernel guard must be applied to `build_list`/`q_deploy`, not merely conditioned on `kernel_enable`. And every withhold path ends in the §4.5a system pass — emptying the queue must never skip the upstream update (§8 preamble).
- **Two ways a compile starts, and only one of them is a button.** The GUI's "Update" is `--commit --no-kernel`; "Update kernel" is the compile. But the §4.9 scheduled service (`/etc/sv/cachy-void-update`, enabled by `--with-schedule`) runs `--sync` then `--commit --yes` — **kernel included** — unattended. A kernel building at 3am with nobody at the keyboard is that service working as designed, and the testbed has it enabled. `--status` names it whenever it is on; check there before assuming a stray process.
- **Front-end vocabularies are asserted, not documented.** `--pending`'s `attention` tokens are `ATTENTION_TOKENS` in the CLI and `REASON_TEXT` in the tray, and a test requires the two sets to be equal. The tray filters unknown tokens silently, so a token on one side only is not a visible bug — it is a tray that says nothing about something real, which happened twice. Same family: the GUI and tray share the socket name and keyword as duplicated literals, also test-asserted. When a message must be true for *this* run (does this press compile? does this box update itself?), derive it from the CLI's own output — never re-derive in a front-end, never hardcode.
