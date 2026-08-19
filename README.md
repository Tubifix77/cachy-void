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
| **Graphical front-end** | `cachy-updater-gui` — the window you actually live with after installing (see [The updater window](#the-updater-window)). Installed by default, painted in the void-tactical palette, and a thin shell over the same tested CLI: it never has privileges of its own. |
| **32-bit ready** | Void ships 64-bit libraries only, but the Steam client and most Proton titles are 32-bit — so the install **enables Void's `multilib` repository** and adds the 32-bit driver libraries matching your GPU. It is a stock Void repo package, ledger-recorded and removed again by `--uninstall`; opt out with `--no-multilib`. |
| **Gaming layer** | `cachy-game` launch wrapper (GameMode → PRIME → optional gamescope → game) with opt-in MangoHud, **gamescope** (frame limiting/FSR) and **vkBasalt** toggles, `earlyoom` guarding the aggressive zram posture, and `cachy-proton` to install Proton-CachyOS. |
| **Maintenance & GPU** | `--clean` (orphans + package cache; **never** kernels, and it refuses a sweep containing a package the overlay built), `--gpu` (detected card, driver + pending update, module actually loaded, and a warning for any installed kernel with **no** out-of-tree module built). |
| **btrfs rollback net** | Optional pre-deploy read-only snapshots taken right before each deploy (`[snapshot]`), on top of the always-converges recovery path. |
| **Optional desktop look** | `void-tactical` — a low-key obsidian/green identity (Kvantum + panel + Conky telemetry + wallpaper + a branded SDDM login screen), fully reversible. The desktop *integration* covers **LXQt** and a **bare Openbox session** (which stock Openbox leaves as a black screen — the applier adds wallpaper, panel, compositor and a curated root menu); everything else in this table is desktop-agnostic and runs under any session, a bare WM, or headless. |
| **Void-native** | runit services (`zramen`, `cachy-health`), a narrow sudoers boundary, no systemd units or timers anywhere. |

---

## Quick Start

One pasted command on a **completely fresh** Void install — `xbps-fetch` ships with xbps itself, so nothing needs to be installed first. Run as your **regular user** (not root):

```bash
xbps-fetch https://raw.githubusercontent.com/Tubifix77/cachy-void/main/get.sh && sh get.sh
```

Optional flags ride along (e.g. `sh get.sh --with-networkmanager --with-branding`). If you already have `curl`, the pipe form works too: `curl -fsSL https://raw.githubusercontent.com/Tubifix77/cachy-void/main/get.sh | sh`. And of course the classic route still works:

```bash
git clone https://github.com/Tubifix77/cachy-void.git
cd cachy-void
./bootstrap.sh
```

Either way you end up in the same place: `get.sh` clones the repository to `~/cachy-void` (its permanent home — the clone is the overlay's source tree and your uninstaller, so keep it) and hands off to `bootstrap.sh`, which verifies the environment, derives the kernel tracking state from your running kernel, installs the prerequisites, ensures a `void-packages` checkout, provisions the system (including a default `/etc/cachy-void/updater.toml`), and seeds the initial state — end to end.

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

## The updater window

Once `deploy.sh` has finished, **`cachy-updater-gui` is the part of Cachy-Void you actually live with** — so it is treated as a product, not a wrapper. It is installed by default (not part of the optional theme), and it is a thin shell over the tested CLI: every button runs the same `cachy-void-update` command you could type, so the window has no privileges of its own.

| Control | What it does |
|---|---|
| **Update** | Sync `void-packages`, then update the system + rebuild the performance overlay (kernel untouched). |
| **Update kernel** | The same, including the BORE kernel: compiles, then a reboot switches to it. |
| **Clean up** | Orphans + package cache. **Previews first** and lists exactly what will go; never removes kernels. |
| **GPU / drivers** | Card, driver + pending update, whether the module is really loaded, DKMS builds per kernel — and a warning for any installed kernel with **no** module built. |
| **Boot known-good kernel** | Appears *only* when the running kernel isn't the recorded known-good one; re-points the bootloader default, uninstalls nothing. |
| **Review & pin…** | Appears *only* when your kernel series has no approved BORE patch: it fetches the patch, shows commit + checksum, and records it when you approve. Until then kernel updates pause while everything else still updates. |
| **i** | What each status tier means, what maintains itself, and what needs you. Stays open and readable while a command runs. |
| *checked N minutes ago* + **Re-check** | The pending list is a point-in-time read; the age says how stale, and re-reading is one quiet click (it also runs on open and after every command). |

Three rules shape it, learned by using it on real hardware:

- **Nothing that matters is CLI-only.** A paused kernel, an available rollback, a driver that never rebuilt — if the updater knows it, the window says it. Recovery lived behind a flag for months, which is no use to the person whose kernel just misbehaved.
- **Preview, then confirm.** Destructive or trust-establishing actions show you the actual list or checksum first; you approve a *decision*, not a category.
- **Annotate, never dump.** Leftover kernels carry their size and role (rollback target / running / spare) and their own removal command, because "which of these can I delete?" is the one question where a wrong guess costs a bootable system.

---

## How it works

The updater runs a four-stage pipeline (sync → queue → build → atomic deploy), driven by a queue algebra that only ever builds packages that are **both** outdated **and** installed, never touching the bootstrap layer (`glibc`, `musl`, `xbps`, `runit`, …). Crash recovery is by recomputation from live state, not a replayed log.

For kernels, the engine closes a verification circuit: detect an upstream bump → verify the BORE patch against a local, human-owned `bore.lock` → regenerate `linux-cachy` from the fresh upstream template → gate the config → build → stage for a one-shot trial boot. A post-boot health daemon promotes the kernel only if it boots healthy.

The complete, authoritative design is in **[architecture.md](architecture.md)**.

---

## What lands on your system

Everything the installer touches is recorded in a per-change **ledger** — inspect your machine's actual inventory any time with `sudo ./deploy.sh --log`, and reverse all of it with `--uninstall`. Packages are installed **only if absent** (anything you already had is left alone and never uninstalled later), and no upstream-owned config file is edited — Cachy-Void drops its own new files.

**Core install** (what `bootstrap.sh` / a plain `deploy.sh` adds):

| What | Exactly |
|---|---|
| Stock Void packages | `zramen` (zram), `earlyoom` (OOM guard), `xtools`, `snooze` (job scheduler), `pciutils`, `gamemode`, `MangoHud`, `gamescope`, `vkBasalt` (+ 32-bit siblings if multilib is on), `protontricks`, `winetricks`, `Vulkan-Tools`, `liberation-fonts-ttf`, `wqy-microhei` (CJK), `xz` |
| runit services enabled | `zramen`, `earlyoom`, `cachy-health` (post-boot kernel health check). `cachy-void-update` (daily timer) is provisioned but only **enabled** with `--with-schedule` |
| Tuning config (new files) | `/etc/sysctl.d/99-cachy-gaming.conf`, udev rules (I/O schedulers, audio anti-crackle on AC, RT-audio perms, SATA ALPM off), `/etc/modprobe.d/` (input polling, watchdog blacklist, amdgpu-for-GCN1/2, NVIDIA dynamic power management), `/etc/modules-load.d/cachy.conf`, a marked runtime-tuning block in `/etc/rc.local` (THP knobs + PCI latency timers) |
| Updater plumbing | engine at `/usr/libexec/cachy-void-updater/`, `/etc/cachy-void/updater.toml`, `/etc/xbps.d/00-cachy-overlay.conf` (local-repo priority), a narrow `visudo`-validated `/etc/sudoers.d/cachy-void`, compiler profile in *your* `void-packages/etc/conf` (untracked) |
| Tools | `/usr/local/bin/`: `cachy-void-update`, `cachy-game`, `cachy-proton` (+ `/etc/xdg/MangoHud/MangoHud.conf`) |

**Opt-in flags add:**

| Flag | Adds |
|---|---|
| `--with-branding` | Packages `kvantum papirus-icon-theme papirus-folders plank rofi conky picom python3-PyQt5` (+ optional `arc-theme font-hack ImageMagick feh tint2 setxkbmap fastfetch`), theme assets under `/usr/share/cachy-void/branding`, the `cachy-branding` applier, and the **void-tactical** SDDM login theme. The desktop look itself is applied per-user by `cachy-branding` (backed up, `--remove` restores). *(The updater window is **not** here — it installs by default; a box with no GUI would have no way to see what the updater is telling it.)* |
| `--with-networkmanager` | `NetworkManager` + `nm-tray` (Qt WiFi picker), enables the NM service, **disables `dhcpcd`** (they conflict) |
| `--with-grub` | Edits `/etc/default/grub` (ledger-backed): `GRUB_DEFAULT=saved` (required for one-shot kernel boot-tests) + `usbcore.autosuspend=-1` |
| `--with-schedule` | Enables the daily unattended-update runit service |
| `--no-multilib` | *Opts **out*** of the default 32-bit gaming support (multilib repo + 32-bit driver/loader libs). Only useful if you never run 32-bit titles or manage repositories yourself |

The kernel (`linux-cachy`) and the compiled overlay live in **your** `void-packages` checkout and local repo — they're ordinary XBPS packages, visible via `xbps-query` like everything else.

---

## Repository layout

```
architecture.md          Authoritative design spec (single source of truth)
INSTALL.md               Installation & provisioning manual
branding.md              The void-tactical desktop look (optional)
future-ideas.md          Ideas not yet built (+ the selection rule for new ones)
rejected-ideas.md        Decisions against, with what would reopen each
get.sh                   One-line installer bootstrap (fetch -> clone -> bootstrap.sh)
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
  tests/                 Mock-driven unit + integration suites (245 tests)
```

---

## Status

The whole spec is implemented and covered by a **245-test** mock-driven suite (run in a Void WSL2 sandbox): the update engine, dependency solver, trust pipeline, template synthesis, kernel state machine, health daemon, and installer.

**Validated on real hardware** (a Void + LXQt laptop): the updater's own `--commit` built `linux-cachy` end-to-end (BORE patch trust → template regen → G2 config gate → compile → deploy), the kernel **booted** (BORE live, 1000 Hz, full preempt), the **NVIDIA DKMS driver built against the BORE kernel**, and games ran on it. The **post-boot health daemon** has also run its full §8.7 confirm cycle on metal: candidate confirmed, the H1–H5 battery passed, and the kernel was **promoted** to tracked/known-good. The performance overlay, zram/sysctl tuning, service cycling, btrfs snapshots, and gaming layer are all exercised on bare metal.

**The gaming-completion set (earlyoom, gamescope, vkBasalt, the Proton toolbox, MangoHud) has been run live** and confirmed working: raw Vulkan/GL rendering on the actual discrete GPU, `cachy-game`'s GameMode composition registering correctly (verified over D-Bus), MangoHud's legacy-Optimus **minimal** profile rendering exactly as designed, and `earlyoom` running continuously under runit. That pass also caught and fixed a real bug (below).

That first live kernel cycle earned its keep by exposing a family of **state-bookkeeping bugs** in the updater — the built kernel booted perfectly, but the record-keeping around it didn't: hosts whose bootloader belongs to *another* distro (multi-boot) were treated as having no bootloader at all, so a healthy boot was never promoted; the daemon's confirm layer wasn't reachable from its production entrypoint; and two health probes (`sv status`, `dmesg`) were silently denied to the unprivileged daemon, making them always-false. All four are fixed (with a new `external` bootloader class for multi-boot hosts and narrow read-only sudo fallbacks), regression-tested, and verified live — the promotion above ran through exactly this repaired path.

**Honest caveats — please report back if you try these:**
- Real-hardware testing so far is on **one** profile: `x86-64-v2` CPU, legacy `nvidia470`, and a *foreign*-owned GRUB (the `external` class above). The `x86-64-v3`/`v4` build path, **modern NVIDIA** GPUs, and a **Void-owned GRUB** (which activates the GRUB **one-shot** boot-test + automatic rollback for the first time) are **code-reviewed and audited but not yet run on metal**.
- **Secure Boot:** Void's NVIDIA driver is an unsigned DKMS module — with Secure Boot on it won't load. Disable it for Linux or MOK-sign (see [INSTALL §13](INSTALL.md)).

<sub>Aside on hardware age, not a caveat: the gaming-set testing above ran on the oldest, least capable hardware likely to try this — a 15-year-old Optimus laptop (`i3-3110M`, legacy `nvidia470`, `GT 730M`). One thing that pass caught: vkBasalt did nothing without a shipped config file (now fixed — a real, restrained default ships). One thing it confirmed rather than fixed: gamescope's already-documented "unreliable on legacy nvidia470" now has a precise cause (`vkCreateDevice: VkResult -7` — the driver lacks DRM format-modifier support), and it fails cleanly with no side effects. None of this is expected to matter on the modern hardware this ships for; it's the ceiling of the hardware it was tested on, not a limit of the software.</sub>

Everything is reversible — `sudo ./deploy.sh --uninstall` restores from a per-change backup ledger. Contributions and real-hardware reports are very welcome.

---

## Design principles

- **Additive overlay only** — never modify upstream-tracked `void-packages` files, so `git pull --rebase` stays conflict-free.
- **Fail-fast, system-intact** — a failure at any stage leaves the running system bootable and unchanged.
- **Preserve Void** — runit, no systemd, clean XBPS resolution; the bootstrap layer always comes from upstream mirrors.
- **The spec is law** — `architecture.md` is the single source of truth; code and docs are kept in lockstep with it.

---

## License

The fusion extends to the licensing. **Cachy-Void's own code** (installer, engine, tooling) is **BSD-2-Clause** — the same permissive license Void uses for the `void-packages` build system this project overlays (Void's *form*). The **performance substance it builds** keeps its upstream copyleft terms, unchanged: `linux-cachy` and the BORE patch are **GPL-2.0** (CachyOS's *substance*). Permissive form, copyleft substance — see [LICENSE](LICENSE).
