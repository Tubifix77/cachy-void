# Cachy-Void

**An ultra-optimized gaming layer for [Void Linux](https://voidlinux.org) — CachyOS-style performance without giving up what makes Void great.**

Cachy-Void steals CachyOS's best performance ideas — hardware-targeted compilation (`-march=x86-64-v3 -O3`), a low-latency BORE-scheduled kernel, and aggressive runtime tuning — and grafts them onto stock Void, while **preserving Void's fortes**: runit stays PID 1 (no systemd, ever), and XBPS dependency resolution is left clean. A small Python engine keeps a curated allowlist of packages locally recompiled and, when you opt in, regenerates and **boot-tests** a custom `linux-cachy` kernel with automatic rollback.

The base system stays 100% upstream Void binaries. Only a short, curated overlay is built locally — so you keep Void's stability and fast security updates.

---

## Highlights

| Area | What you get |
|---|---|
| **Compiler profile** | `x86-64-v3`/`v4` + `-O3 -pipe` for a curated userland allowlist (mesa, wine, pipewire, …) via `xbps-src` + ccache. |
| **Low-latency kernel** | `linux-cachy`: a fork of Void's own kernel template + BORE scheduler, 1000 Hz timer, full preemption, MGLRU, BBR. |
| **Runtime tuning** | Gaming `sysctl` (swappiness/zram, `max_map_count`, RT throttling off), per-medium I/O schedulers, 1000 Hz input polling. |
| **Safe kernel updates** | SHA-256-pinned BORE patch trust, deterministic template regeneration, a config gate that catches silent `oldconfig` drops, and GRUB **one-shot** boot-testing — a bad kernel rolls back on the next power cycle with zero interaction. |
| **Automated updater** | A fail-fast, resumable update engine: syncs `void-packages`, computes a topologically-ordered build queue, compiles, deploys with overlay priority, and cycles runit services. |
| **Void-native** | runit services (`zramen`, `cachy-health`), a narrow sudoers boundary, no systemd units or timers anywhere. |

---

## Quick Start

Run as your **regular user** (not root) on a Void host:

```bash
git clone https://github.com/Tubifix77/cachy-void.git
cd cachy-void
./bootstrap.sh
```

`bootstrap.sh` verifies the environment, derives the kernel tracking state from your running kernel, installs the prerequisites, ensures a `void-packages` checkout, provisions the system, and seeds the initial state — end to end.

Then preview and run your first update:

```bash
cachy() { /usr/libexec/cachy-void-updater/cachy_void_update.py --config /etc/cachy-void/updater.toml "$@"; }
cachy --check            # read-only: show what would build/deploy
cachy --sync             # rebase void-packages onto upstream
cachy --commit --yes     # build, deploy, and stage the kernel
```

Full instructions, configuration, and the uninstall path are in **[INSTALL.md](INSTALL.md)**.

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
bootstrap.sh             Zero-touch provisioning entry point
deploy.sh                Idempotent, reversible system installer
system/                  Static config: sysctl, udev, xbps.d, sudoers, runit services
overlay/config/          Kernel .config fragment (BORE, 1000 Hz, …)
updater/
  cachy_void_update.py   Unified CLI (--sync/--check/--commit/--rollback/--health-daemon)
  engine/                Solver, XBPS layer, journal, kernel state machine, trust, health
  tests/                 Mock-driven unit + integration suites
```

---

## Status

The update engine, dependency solver, trust pipeline, template synthesis, and installer are implemented and covered by a mock-driven test suite (170 tests). Logic, parsing, the compiler pipeline, and installer paths have been validated in a Void WSL2 sandbox.

Hardware-level steps that require real hardware and a real runit PID 1 — the actual kernel compile/boot-test and GRUB one-shot staging — have not yet been exercised on bare metal. Treat kernel features as ready-for-testing, not battle-tested. Contributions and real-hardware reports are welcome.

---

## Design principles

- **Additive overlay only** — never modify upstream-tracked `void-packages` files, so `git pull --rebase` stays conflict-free.
- **Fail-fast, system-intact** — a failure at any stage leaves the running system bootable and unchanged.
- **Preserve Void** — runit, no systemd, clean XBPS resolution; the bootstrap layer always comes from upstream mirrors.
- **The spec is law** — `architecture.md` is the single source of truth; code and docs are kept in lockstep with it.
