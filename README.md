# Cachy-Void

**An ultra-optimized gaming layer for [Void Linux](https://voidlinux.org) — CachyOS-style performance without giving up what makes Void great.**

Cachy-Void steals CachyOS's best performance ideas — hardware-targeted compilation (`-march=x86-64-v3 -O3`), a low-latency BORE-scheduled kernel, and aggressive runtime tuning — and grafts them onto stock Void, while **preserving Void's fortes**: runit stays PID 1 (no systemd, ever), and XBPS dependency resolution is left clean. A small Python engine keeps a curated allowlist of packages locally recompiled and, when you opt in, regenerates and **boot-tests** a custom `linux-cachy` kernel with automatic rollback.

The base system stays 100% upstream Void binaries. Only a short, curated overlay is built locally — so you keep Void's stability and fast security updates.

---

## Highlights

| Area | What you get |
|---|---|
| **Compiler profile** | `x86-64-v3`/`v4` + `-O3 -pipe` for a curated userland allowlist (mesa, wine, pipewire, …) via `xbps-src` + ccache. The ABI level is **auto-detected** from your CPU. |
| **Low-latency kernel** | `linux-cachy`: a fork of Void's own kernel template + BORE scheduler, 1000 Hz timer, full preemption, MGLRU, BBR. |
| **Runtime tuning** | Gaming `sysctl` (swappiness/zram, `max_map_count`, RT throttling off), per-medium I/O schedulers, 1000 Hz input polling. |
| **Safe kernel updates** | SHA-256-pinned BORE patch trust, deterministic template regeneration, a config gate that catches silent `oldconfig` drops, and GRUB **one-shot** boot-testing — a bad kernel rolls back on the next power cycle with zero interaction. |
| **Automated updater** | A fail-fast update engine that syncs `void-packages`, computes a topologically-ordered build queue, compiles, deploys with overlay priority, cycles runit services, and — because an updater should update *everything* — refreshes **Flatpak** apps too. Recovery is by recomputation from live state. |
| **Graphical front-end** | `cachy-updater-gui` — a themed PyQt5 updater so updates actually happen (Update / Update kernel / Clean up / GPU advisory), over the same tested CLI. |
| **Gaming layer** | `cachy-game` launch wrapper (GameMode → PRIME → game), a restrained MangoHud profile (auto-tuned for legacy Optimus), and `cachy-proton` to install Proton-CachyOS. |
| **Maintenance & GPU** | `--clean` (orphans + package cache; never touches kernels), `--gpu` (detected card, driver, DKMS health, legacy-series advice). |
| **btrfs rollback net** | Optional pre-deploy read-only snapshots taken right before each deploy (`[snapshot]`), on top of the always-converges recovery path. |
| **Optional desktop look** | `void-tactical` — a low-key obsidian/green LXQt identity (Kvantum + panel + Conky telemetry + wallpaper + a branded SDDM login screen), fully reversible. |
| **Void-native** | runit services (`zramen`, `cachy-health`), a narrow sudoers boundary, no systemd units or timers anywhere. |

---

## Quick Start

Run as your **regular user** (not root) on a Void host:

```bash
git clone https://github.com/Tubifix77/cachy-void.git
cd cachy-void
./bootstrap.sh
```

`bootstrap.sh` verifies the environment, derives the kernel tracking state from your running kernel, installs the prerequisites, ensures a `void-packages` checkout, provisions the system (including a default `/etc/cachy-void/updater.toml`), and seeds the initial state — end to end.

Two quick things before your first update: **review `/etc/cachy-void/updater.toml`** (the `[packages]` allowlist), and — *only if you want the BORE kernel* — **pin `bore.lock` for your kernel series** ([INSTALL §6.2](INSTALL.md)). Then:

```bash
cachy() { /usr/libexec/cachy-void-updater/cachy_void_update.py --config /etc/cachy-void/updater.toml "$@"; }
cachy --check            # read-only: show what would build/deploy
cachy --sync             # rebase void-packages onto upstream
cachy --commit --yes     # build, deploy, refresh flatpaks, and stage the kernel
```

The performance overlay, runtime tuning, and gaming layer all work **without** the kernel step. Two extras are opt-in: the desktop look (`sudo ./deploy.sh --with-branding`, then run `cachy-branding` as your user) and unattended daily updates (`--with-schedule`).

Full instructions, configuration, multi-boot/Secure-Boot notes, and the uninstall path are in **[INSTALL.md](INSTALL.md)**.

---

## How it works

The updater runs a four-stage pipeline (sync → queue → build → atomic deploy), driven by a queue algebra that only ever builds packages that are **both** outdated **and** installed, never touching the bootstrap layer (`glibc`, `musl`, `xbps`, `runit`, …). Crash recovery is by recomputation from live state, not a replayed log.

For kernels, the engine closes a verification circuit: detect an upstream bump → verify the BORE patch against a local, human-owned `bore.lock` → regenerate `linux-cachy` from the fresh upstream template → gate the config → build → stage for a one-shot trial boot. A post-boot health daemon promotes the kernel only if it boots healthy.

The complete, authoritative design is in **[architecture.md](architecture.md)**.

---

## Repository layout

```
architecture.md          Authoritative design spec (single source of truth)
INSTALL.md               Installation & provisioning manual
branding.md              The void-tactical desktop look (optional)
bootstrap.sh             Zero-touch provisioning entry point
deploy.sh                Idempotent, reversible system installer (--with-grub/-branding/-schedule)
system/                  Static config + runit services + gaming/branding assets:
  sysctl.d, udev, xbps.d, modprobe.d, sudoers.d, sv/   Tuning, boundaries, services
  bin/                   cachy-game, cachy-proton, cachy-branding, cachy-updater-gui
  cachy-void/            Default updater.toml template
  branding/, sddm/       void-tactical theme assets + branded login screen
overlay/config/          Kernel .config fragment (BORE, 1000 Hz, …)
assets/                  Wallpapers + icons (the mark)
updater/
  cachy_void_update.py   Unified CLI (--sync/--check/--status/--commit/--rollback/--clean/--gpu/…)
  engine/                Solver, XBPS layer, journal, kernel state machine, trust, health, snapshot
  tests/                 Mock-driven unit + integration suites (226 tests)
```

---

## Status

The whole spec is implemented and covered by a **226-test** mock-driven suite (run in a Void WSL2 sandbox): the update engine, dependency solver, trust pipeline, template synthesis, kernel state machine, health daemon, and installer.

**Validated on real hardware** (a Void + LXQt laptop): the updater's own `--commit` built `linux-cachy` end-to-end (BORE patch trust → template regen → G2 config gate → compile → deploy), the kernel **booted** (BORE live, 1000 Hz, full preempt), the **NVIDIA DKMS driver built against the BORE kernel**, and games ran on it. The performance overlay, zram/sysctl tuning, service cycling, btrfs snapshots, and gaming layer are all exercised on bare metal.

**Honest caveats — please report back if you try these:**
- Real-hardware testing so far is on **one** profile: `x86-64-v2` CPU, legacy `nvidia470`, and a *foreign*-owned GRUB. The `x86-64-v3`/`v4` build path, **modern NVIDIA** GPUs, and a **Void-owned GRUB** (which activates the one-shot boot-test + health-daemon promote/rollback for the first time) are **code-reviewed and audited but not yet run on metal**.
- **Secure Boot:** Void's NVIDIA driver is an unsigned DKMS module — with Secure Boot on it won't load. Disable it for Linux or MOK-sign (see [INSTALL §13](INSTALL.md)).

Everything is reversible — `sudo ./deploy.sh --uninstall` restores from a per-change backup ledger. Contributions and real-hardware reports are very welcome.

---

## Design principles

- **Additive overlay only** — never modify upstream-tracked `void-packages` files, so `git pull --rebase` stays conflict-free.
- **Fail-fast, system-intact** — a failure at any stage leaves the running system bootable and unchanged.
- **Preserve Void** — runit, no systemd, clean XBPS resolution; the bootstrap layer always comes from upstream mirrors.
- **The spec is law** — `architecture.md` is the single source of truth; code and docs are kept in lockstep with it.
