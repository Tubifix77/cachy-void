# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Status

Greenfield. `architecture.md` is the **authoritative spec** — read it before implementing anything; if anything disagrees with it, the spec wins. No code exists yet; there are no build, lint, or test commands for this repo itself. The deliverables and target file layout are defined in `architecture.md` §6.

## What This Project Is

"Cachy-Void": a performance overlay on stock Void Linux — CachyOS-style tuning (x86-64-v3/`-O3` compilation, BORE-patched 1000 Hz kernel, aggressive sysctl/zram/udev config) while preserving Void's fortes (runit as PID 1, no systemd anywhere, clean XBPS dependency resolution). The base system stays upstream Void binaries; only a curated allowlist of performance-critical packages is built locally, orchestrated by a Python updater (`architecture.md` §4).

## Target Platform Caveat

All code targets **Void Linux** (XBPS, `xbps-src`, runit). This development machine is Windows — nothing here can be executed or integration-tested locally. Validate updater logic with unit tests that mock `subprocess` calls rather than attempting live runs.

## Invariants To Never Violate (spec §0, I1–I7)

- **Additive overlay only**: never modify upstream-tracked `void-packages` files; customizations are new `srcpkgs/*` dirs or untracked `etc/conf`. This is what keeps `git pull --rebase` conflict-free.
- **Bootstrap blacklist**: `glibc`, `musl`, `gcc`, `binutils`, `xbps`, `runit`, `base-files` are never built or replaced locally. Blacklist beats allowlist.
- **runit only**: services via runit dirs, scheduling via `snooze`, zram via `zramen`. No systemd units/timers/zram-generator.
- **Fail-fast, system intact**: no system mutation unless all builds succeeded; single sudo boundary — exactly the commands enumerated in spec §4.1 (`xbps-install`, `sv`, `xcheckrestart`, `xbps-pkgdb`, the three GRUB staging binaries `grub-set-default`/`grub-reboot`/`grub-editenv`, and the §9.5 btrfs snapshot ops `btrfs subvolume snapshot -r`/`delete`/`list`), plus the user-facing maintenance/"update everything" grants — `xbps-remove -o|-O -n|-y` (`--clean`: orphans + cache only; these flags cannot name a package) and `flatpak update --system -y` (folded into Update; only refreshes installed refs) — nothing else. Note `btrfs subvolume create` is deliberately **not** in the updater's grant — the `/.cachy-snapshots` subvol is created once by `deploy.sh` (which already runs as root); the engine only takes/prunes snapshots inside it. `vkpurge` is deliberately **not** granted: kernel purges stay manual (§2.5/§4.7); `--clean` only *suggests* them.
- **Deps stay binary**: only allowlist targets compile from source; never recursively source-build dependencies.
- **32-bit multilib stays upstream**: never cross-build i686 with `x86-64-v*` flags.
- **No `mitigations=off`**, and `-f`/`--force` is sanctioned only for the same-pkgver overlay takeover (spec §4.6).

## Architecture Notes That Are Easy To Get Wrong

- **Kernel**: `srcpkgs/linux-cachy` is a fork of Void's own kernel template + BORE patch in `patches/` — *not* linux-tkg or XanMod. Unique pkgname means upstream never touches it, but also that it doesn't auto-update: the updater only warns on version drift (spec §2.6).
- **Same-version shadowing**: local rebuilds share `pkgver` with upstream binaries, so `xbps-install -Su` alone won't switch an installed package to the local build. The forced-reinstall step and repo-priority config in spec §4.6 exist for this; don't "simplify" them away.
- **Queue formula lives in §7.3** (it supersedes §4.3): `M` term = allowlist members never built (without it, a fresh setup builds nothing, since `show-local-updates` is empty against an empty repo); `P` term = built-but-never-deployed packages (closes the orphan hole when a run dies mid-build); `O` term = same-version packages whose installed *origin* is still an upstream mirror (closes the takeover hole when a run dies between `-Su` and the §4.6 `-f` loop). Recovery is always recomputed from live queries — the journal is witness-only, there is deliberately no `--resume` flag.
- **Name domains (spec §7.1)**: templates are srcpkgs, installed packages are binpkgs; map binpkg→srcpkg via the `srcpkgs/<sub>` symlink convention. No-widen rule: the updater never installs a binpkg that isn't already installed. Version comparisons delegate to `xbps-uhelper cmpver`, never reimplemented.
- **Kernel state machine (spec §8)**: the template is *regenerated* from upstream each bump, never incrementally patched. The G2 config gate exists because `oldconfig` silently drops unknown symbols — a failed BORE patch otherwise ships a stock-scheduler kernel that "built fine"; a G2 failure withholds the kernel but never blocks userspace updates. `bore.lock` is only ever updated by a human. GRUB one-shot staging requires a grubenv-writable `/boot` (ext*/vfat; btrfs/zfs/LVM degrade to safe `manual` mode), and staging **refuses** (exit 70) when `GRUB_DEFAULT≠saved` (`manual-unsafe`) — the saved-default edit is `deploy.sh --with-grub`'s job, never the updater's. `ported_version` advances only on a healthy boot (PROMOTED), not on a successful build.
- **Rollback**: a corrupted repo index is fixed by deleting `x86_64-repodata` and re-running `xbps-rindex -a`. `xbps-pkgdb -m repounlock` is *not* an index-repair tool (an old draft claimed this; the spec explicitly retires it).
- **Deliberate tuning values** (spec §2.4, §3.1): `vm.swappiness = 100` (zram-paired), `vm.max_map_count = 2147483642`, `kernel.sched_rt_runtime_us = -1`, 1000 Hz + full preemption. These are intentional, aggressive choices — do not normalize them to conventional defaults.
