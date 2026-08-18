# Cachy-Void — Architecture & Build Specification

**Status: AUTHORITATIVE.** This document is the sole source of truth for the Cachy-Void project. If code, configs, or other docs disagree with this file, this file wins. Last revised: 2026-07-15.

---

## 0. Vision & Design Invariants

Cachy-Void is a performance overlay on top of stock Void Linux, not a fork. The base system remains upstream Void binaries (stability, fast security updates, clean XBPS dependency graph). On top of it sits a small, curated, locally-compiled overlay: a BORE-patched low-latency kernel and a short list of performance-critical userspace packages rebuilt with hardware-targeted flags. An automated updater keeps the overlay current without ever endangering the base system.

**Adopted from CachyOS:**

| Concept | Realization here |
|---|---|
| Hardware-targeted compilation | `x86-64-v3`/`v4` + `-O3` via `void-packages/etc/conf` (§1) |
| Low-latency kernel scheduling | BORE scheduler patch, 1000 Hz, full preemption (§2) |
| Aggressive runtime tuning | sysctl + zram + udev I/O-scheduler rules (§3) |

**Preserved from Void (non-negotiable):**

| Void forte | Consequence |
|---|---|
| runit as PID 1 | All services are runit service dirs; scheduling via `snooze`; zero systemd units, ever. `dbus` and `elogind` are permitted (Void ships them systemd-free). |
| XBPS dependency purity | No `--force` except the narrowly-scoped overlay reinstall in §4.6. No manual file drops into `/usr`. Dependencies resolve normally. |
| Rolling upstream binaries | Everything not on the overlay allowlist comes from Void mirrors untouched. |

**Global invariants** (referenced throughout as I1–I7):

- **I1 — Additive overlay only.** Never modify files tracked by upstream `void-packages`. All customization lives in *new* `srcpkgs/*` directories or in `etc/conf` (untracked). This keeps `git pull --rebase` structurally conflict-free.
- **I2 — Bootstrap blacklist is absolute.** `glibc`, `musl`, `gcc`, `binutils`, `xbps`, `runit`, `base-files` are never built locally and never replaced from the overlay. Upstream mirrors own them.
- **I3 — runit untouched, systemd never.** No component may depend on systemd units, timers, or `zram-generator`.
- **I4 — Fail-fast, system intact.** No system mutation (Stage 4) happens unless Stages 1–3 completed fully. A failure at any point leaves the running system bootable and unchanged.
- **I5 — Dependencies stay binary.** Only allowlisted targets are compiled from source. `xbps-src` fetches build/runtime dependencies as upstream binaries; we never recursively source-build the world.
- **I6 — 32-bit multilib stays upstream.** Never cross-build i686/multilib packages with `x86-64-v*` flags (they would not even compile). `wine-32bit`, `mesa-32bit` etc. remain stock binaries; version-matched mixing with optimized 64-bit halves is expected and fine.
- **I7 — Security posture stays stock.** `mitigations=off` is *not* part of this spec. Kernel hardening defaults from Void's config are kept unless a line in §2.4 explicitly overrides them.

---

## 1. Compiler Profile Configuration

### 1.1 File: `void-packages/etc/conf`

`etc/conf` is sourced by `xbps-src` and is git-untracked (I1). Exact content:

```sh
# Cachy-Void global compiler profile
XBPS_CFLAGS="-march=x86-64-v3 -O3 -pipe"
XBPS_CXXFLAGS="${XBPS_CFLAGS}"
XBPS_FFLAGS="-march=x86-64-v3 -O3 -pipe"
XBPS_MAKEJOBS=16          # set to nproc of the build host
XBPS_CCACHE=yes           # mandatory: -O3 rebuild cycles are expensive
```

### 1.2 v3 vs. v4 selection rule

- Default is **`x86-64-v3`** (AVX2, FMA3, BMI2 — every gaming-relevant CPU since Haswell/Zen 1).
- Use **`x86-64-v4`** only if the CPU implements the full AVX-512 v4 subset (`avx512f/bw/cd/dq/vl`). Check: `grep -o 'avx512vl' /proc/cpuinfo | head -1`. In practice: Zen 4/Zen 5 qualify; Intel 12th–14th gen desktop does **not** (AVX-512 fused off).
- The choice is host-wide and set once in `etc/conf`. Do not mix: the local binpkg repo must contain one ABI level.
- **Pre-Haswell hosts are `x86-64-v2` only.** No AVX2 (Ivy Bridge and older — real deployment targets exist) ⇒ v3 binaries fault with SIGILL. Both `detect_march()` (§8.4 module) and `deploy.sh` (which now auto-detects when `--march` is not given, same ladder in shell) implement: v4 → v3 (`avx2+fma+bmi2`) → v2 (`sse4_2+popcnt`) → baseline — never recommending a level the host cannot prove; undeterminable hosts get the v2 safe floor. `--march` remains the explicit override (required when provisioning a disk for a *different* machine via `--root`).

### 1.3 Rules and caveats

- **No global LTO.** `-flto` breaks too many templates to be a blanket flag. Per-template LTO may be added later, case by case. This is a decided trade-off, not an oversight.
- **Respect template filtering.** Some templates strip or override user CFLAGS (hardening, `build_style` quirks). Accept it. Never patch `common/` build machinery to force flags through (violates I1).
- **Native builds only.** This profile assumes the build host is the target host. If a cross target is ever added, `-march` must be stripped for it (see I6).
- **Same-version shadowing.** A locally built package usually has the *same* `pkgver` as the upstream binary. XBPS resolves version ties by repository order — the overlay repo must be registered first (§4.6). Note the consequence: a same-version local rebuild does **not** count as an "update"; switching an installed upstream binary to the local build requires the forced reinstall step in §4.6.

---

## 2. Low-Latency Kernel Spec (`srcpkgs/linux-cachy`)

### 2.1 Approach — decided

The kernel is a **fork of Void's own current kernel template** (e.g., `srcpkgs/linux6.12`) renamed to `linux-cachy`, carrying the BORE patch and a config fragment. Not linux-tkg, not XanMod. Rationale: Void's template already handles headers/dbg subpackages, dracut, dkms hooks, and GRUB integration; forking it keeps our diff minimal, and the unique `pkgname` means upstream can never shadow or conflict with it (I1, I2).

### 2.2 Template creation procedure

```sh
cd void-packages
cp -r srcpkgs/linux6.12 srcpkgs/linux-cachy          # fork current stable series
grep -rn 'linux6\.12' srcpkgs/linux-cachy/            # find every self-reference
# → rename pkgname, subpackage names (linux-cachy-headers, linux-cachy-dbg),
#   and their *_package() functions accordingly. Keep version pinned to the
#   forked series.
```

Commit the new directory as a local commit on top of upstream master. It is the *only* kernel package this project builds.

### 2.3 BORE patch placement

- Source: the matching `linux-<series>-bore` patch from the upstream BORE repository (github.com/firelzrd/bore-scheduler). The patch is series-specific — a 6.12 patch applies only to 6.12.y.
- Placement: `srcpkgs/linux-cachy/patches/0001-bore.patch`. `xbps-src` auto-applies everything in `patches/` with `-Np1`; no template changes are needed for the patch itself.
- After any version bump: refresh the patch to the matching series, then regenerate checksums with `xgensum -f srcpkgs/linux-cachy/template` (from `xtools`). *(Manual flow only — the automated regeneration in §8.4 inherits upstream checksums byte-for-byte and needs no `xgensum`.)*

### 2.4 Kernel config fragment

Start from the template's stock `files/x86_64-dotconfig` and append this fragment (the build's `oldconfig` pass normalizes duplicates — later entries win):

```
# --- Cachy-Void overrides ---
# Scheduler: BORE on top of EEVDF
CONFIG_SCHED_BORE=y
CONFIG_SCHED_AUTOGROUP=y
# Timer: 1000 Hz for input latency
CONFIG_HZ_1000=y
CONFIG_HZ=1000
# CONFIG_HZ_100 is not set
# CONFIG_HZ_250 is not set
# CONFIG_HZ_300 is not set
# Preemption: full, pinned (no boot-time dynamic switching)
CONFIG_PREEMPT=y
# CONFIG_PREEMPT_NONE is not set
# CONFIG_PREEMPT_VOLUNTARY is not set
# CONFIG_PREEMPT_DYNAMIC is not set
# Memory: MGLRU on by default, THP always
CONFIG_LRU_GEN=y
CONFIG_LRU_GEN_ENABLED=y
CONFIG_TRANSPARENT_HUGEPAGE=y
CONFIG_TRANSPARENT_HUGEPAGE_ALWAYS=y
# Network: BBR built IN (=y, vs stock =m) so no module-load ordering issue.
# The *default* congestion control is a Kconfig CHOICE; a dotconfig append
# cannot flip a choice already satisfied by cubic (oldconfig drops it — G2-caught
# on the first real kernel run), so the runtime default is set by the §3.1
# sysctl net.ipv4.tcp_congestion_control=bbr instead, not by a kernel choice.
CONFIG_TCP_CONG_BBR=y
CONFIG_NET_SCH_FQ=y
```

BORE's runtime tunables (`kernel.sched_bore` and friends) default to sane values from the patch; they are deliberately *not* set in §3 unless benchmarking shows a need.

**Kernel image compiler flags stay stock** (no `-march`/`-O3` for the kernel itself). The kernel gains little from userland ABI levels and `-O3` kernels are a known breakage source; the win here is the scheduler and config, not kernel codegen. Optional later extension: graysky's `kernel_compiler_patch`.

### 2.5 Build, install, boot hygiene

```sh
./xbps-src pkg linux-cachy
sudo xbps-install --repository=hostdir/binpkgs linux-cachy linux-cachy-headers
```

- `linux-cachy-headers` must always be co-installed: Void's dkms kernel hooks then rebuild out-of-tree modules (nvidia et al.) automatically on kernel install.
- Void's `kernel.d` post-install hooks handle dracut initramfs and GRUB entries; verify once that the `grub` package's hook is present in `/etc/kernel.d/post-install/`.
- Old kernel cleanup is **manual only**: `vkpurge list` / `vkpurge rm <ver>`. Always keep the previous known-good kernel installed until the new one has survived a real gaming session. The updater never purges kernels (§4.7).

### 2.6 Maintenance rule

`linux-cachy` does **not** track upstream automatically (unique pkgname = invisible to upstream bumps). When Void bumps its stable series template: diff upstream's template against the fork, port the delta, refresh the BORE patch, `xgensum`, commit. The updater warns when `srcpkgs/linux<series>/template` is newer than `linux-cachy`'s pinned version (§4.3, drift check) — that warning is the maintenance trigger.

**Superseded in part by §8:** patch-level bumps within the tracked series are automated by the kernel injection state machine; series changes and all anomalies remain human-gated exactly as described above.

---

## 3. Gaming Sysctl & Udev Parameters

### 3.1 File: `/etc/sysctl.d/99-cachy-gaming.conf`

Void applies `/etc/sysctl.d/*.conf` at boot via runit core-services. Exact content:

```ini
# --- Memory / zram (pairs with §3.2; do NOT apply without zram active) ---
vm.swappiness = 100              # aggressive swap to zstd-zram keeps page cache hot
vm.page-cluster = 0              # no readahead on zram; it's not a disk
vm.vfs_cache_pressure = 50
vm.dirty_bytes = 268435456       # 256 MiB absolute writeback ceiling
vm.dirty_background_bytes = 67108864
vm.dirty_writeback_centisecs = 1500   # writeback wakeups 5s->15s: less background
                                 # jitter (CachyOS default; kernel default 500)
vm.max_map_count = 2147483642    # SteamOS value; required by many Proton titles
# --- Files ---
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
# --- Scheduling / kernel ---
kernel.split_lock_mitigate = 0   # split-lock stalls cost 10%+ fps in affected games
kernel.nmi_watchdog = 0
kernel.printk = 3 3 3 3          # quiet console: no kernel chatter on ttys
                                 # (CachyOS default; kernel default 4 4 1 7)
kernel.sched_rt_runtime_us = -1  # no RT throttling. Accepted risk: a runaway
                                 # SCHED_FIFO task can monopolize a core.
# --- Network ---
net.core.netdev_max_backlog = 4096
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_congestion_control = bbr
```

Apply/verify: `sudo sysctl --system`. On the stock Void kernel (fallback boots), BBR is a module — add `tcp_bbr` to `/etc/modules-load.d/cachy.conf` so the sysctl line never silently fails. On `linux-cachy` it is built in (§2.4).

**Codified stance — performance over hardening (owner decision, 2026-08-17):**
when a tweak trades performance against hardening, this overlay picks
performance *for* the user — Void is not a security-first distro and this
project's substance is performance. Applied consistently in both directions:
`kernel.kptr_restrict=2` stays **out** (pure hardening, zero perf; Void's
default `1` already covers the unprivileged case), and
`NVreg_InitializeSystemMemoryAllocations=0` goes **in** (§3.3 — perf at an
explicit security cost). Also omitted: `kernel.unprivileged_userns_clone=1` —
the knob **does not exist** on Void's kernel (Arch/Debian patch; live-verified).

### 3.1b Runtime tuning beyond sysctl.d/udev (`/etc/rc.local`)

Some tuning targets are reachable by neither `sysctl.d` (procfs only) nor udev
(not device events) — on runit the sanctioned mechanism for those is
**`/etc/rc.local`** (run by Void's core-services at boot end). deploy.sh manages
ONE marked block in it (backed up + ledger-tracked like any other file; the block
is rebuilt idempotently on re-runs — older narrower markers from previous versions
are migrated away — and `--uninstall` restores the pre-Cachy file). Every command
is guarded so a fallback boot missing a knob or a tool never errors. Two members:

**THP runtime knobs.** §2.4 builds `THP=always` into the kernel; CachyOS pairs that
policy with two runtime knobs its `cachyos-settings` ships as tmpfiles (verified
2026-08-15): `transparent_hugepage/defrag = defer+madvise` (tcmalloc-style tuning)
and `khugepaged/max_ptes_none = 409` — the 6.12+ THP *shrinker*: "THP=always vastly
overprovisions THPs in sparsely accessed memory areas"; 409/512 means any THP that is
>80 % zero-filled is split, bringing `always`'s memory usage down to ~`madvise`
levels while keeping its performance.

**PCI latency timers.** CachyOS's `pci-latency` script (verified 2026-08-16):
`setpci` sets every PCI device's latency timer to 20 cycles, the host bridge to 0,
and sound cards (class `04xx`) to 80 — their anti-audio-gap tweak ("prevent devices
with high default latency timers from causing gaps in sound"). `setpci` ships in
`pciutils` (guaranteed by deploy.sh). Honesty note (live-verified): `latency_timer`
is a *conventional-PCI* register that PCI **Express** hardwires to zero — on a
PCIe-only machine the write is a harmless no-op (reads back `0x00` even as root);
the tweak only bites where conventional-PCI segments exist. Shipped anyway,
exactly as CachyOS does: no-op where irrelevant, helpful where not.

```sh
# >>> cachy-void runtime tuning (§3.1b) >>>
[ -w /sys/kernel/mm/transparent_hugepage/defrag ] && \
    echo defer+madvise > /sys/kernel/mm/transparent_hugepage/defrag
[ -w /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none ] && \
    echo 409 > /sys/kernel/mm/transparent_hugepage/khugepaged/max_ptes_none
if command -v setpci >/dev/null 2>&1; then
    setpci -s '*:*' latency_timer=20 2>/dev/null || true
    setpci -s '0:0' latency_timer=0  2>/dev/null || true
    setpci -d '*:*:04xx' latency_timer=80 2>/dev/null || true
fi
# <<< cachy-void runtime tuning <<<
```

### 3.2 zram (runit-native, no zram-generator)

```sh
sudo xbps-install zramen
sudo ln -s /etc/sv/zramen /var/service/
```

Configure in the service's `conf` file using the variable names the `zramen` package actually ships (verified against `zramen-1.0.1_1`): `ZRAM_COMP_ALGORITHM=zstd` (best ratio; package default `lz4`), `ZRAM_SIZE=100` — a **percent** of RAM, not a fraction (package default 25) — and, critically, `ZRAM_MAX_SIZE` raised above its 4096 MiB default so 100% is not silently capped at 4 GiB. `ZRAM_PRIORITY` sits above any disk swap (package default 32767). Disk swap partitions may coexist at lower priority but are not required. (An earlier draft used `ZRAM_ALG`/`ZRAM_PRIO`/a `1.0` fraction — all unrecognized by zramen; that is retired.)

**Memory-pressure guard: `earlyoom`.** `vm.swappiness=100` + full-RAM zram is a deliberately aggressive posture (§3.1), and its failure mode is the classic one: a leaking game fills RAM *and* compressed swap, and the box livelocks for minutes before the kernel OOM killer acts. `earlyoom` (stock Void package, runit service) is that posture's safety valve: a tiny unprivileged daemon that kills the largest offender *before* the freeze. Ship it enabled with package defaults — no tuning, no config file; the value is that it exists. This is a companion of the zram choice, not an independent feature: whoever gets §3.1's swappiness also gets its guard.

```sh
sudo xbps-install earlyoom
sudo ln -s /etc/sv/earlyoom /var/service/
```

### 3.3 Udev rules

**`/etc/udev/rules.d/60-ioschedulers.rules`** — right scheduler per medium:

```
ACTION=="add|change", KERNEL=="nvme[0-9]*", ATTR{queue/scheduler}="none"
ACTION=="add|change", KERNEL=="sd[a-z]|mmcblk[0-9]*", ATTR{queue/rotational}=="0", ATTR{queue/scheduler}="mq-deadline"
ACTION=="add|change", KERNEL=="sd[a-z]", ATTR{queue/rotational}=="1", ATTR{queue/scheduler}="bfq"
```

**Game controllers:** there is **no** `game-devices-udev` package on Void (an earlier draft wrongly named one — retired). Standard controllers (Xbox/PS/generic HID) are handled by the kernel plus `elogind` seat management, which grants `uaccess` to the active seat's input devices; the Steam client installs its own device rules as well. Ship a `60-controllers.rules` in the overlay *only* for exotic devices (arcade sticks, Steam Controller) that need explicit rules — never depend on a package that does not exist.

**Input latency policy:**

- USB autosuspend is disabled globally via kernel cmdline: add `usbcore.autosuspend=-1` to `GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub`, then `sudo update-grub`. This is a desktop gaming box; the power cost is accepted. (Per-device udev `power/control` rules are the laptop-friendly alternative, keyed on `idVendor`.)
- Legacy HID polling: `/etc/modprobe.d/99-gaming-input.conf` with `options usbhid mousepoll=1` (1000 Hz). Modern gaming mice negotiate their native rate anyway; this only lifts legacy devices.

Apply: `sudo udevadm control --reload && sudo udevadm trigger`.

**CachyOS-settings parity rules (adopted 2026-08-15, each verified against the
`CachyOS/CachyOS-Settings` repo before adoption — provenance in the rule headers):**

- **`20-audio-pm.rules`** — pins `snd_hda_intel power_save=0` while on AC (their
  comment: "prevents audio cracks on some hardware"), restoring the saved value when
  the box goes to battery. Ported near-verbatim (bash stays; Void ships it).
- **`40-rtaudio-perms.rules`** — `rtc0` + `hpet` to group `audio`, and
  `/dev/cpu_dma_latency` 0660 root:audio, so a realtime-audio userspace (JACK,
  pro-audio under PipeWire) can use precise timers and hold C-states without root.
- **`50-sata-alpm.rules`** — SATA `link_power_management_policy=max_performance`
  (ALPM off): latency over power on link transitions, the desktop-gaming default.
- **`/etc/modprobe.d/99-cachy-watchdog.conf`** — blacklists `iTCO_wdt` (Intel) and
  `sp5100_tco` (AMD Ryzen): a hardware watchdog nobody arms is pure periodic-timer
  overhead on a gaming box (found *loaded* on the reference laptop). `kernel.
  nmi_watchdog=0` in §3.1 is the software half of the same decision.
- **`/etc/modprobe.d/99-cachy-amdgpu.conf`** — forces the modern `amdgpu` driver
  over legacy `radeon` for GCN 1.0 (Southern Islands) and 2.x (Sea Islands)
  cards (`si_support`/`cik_support`), unlocking Vulkan/RADV on that hardware.
  Verbatim CachyOS. Inert on non-AMD machines; **honesty note: shipped untested
  on real AMD hardware** (the reference box is NVIDIA) — verbatim adoption of
  CachyOS's file is the mitigations here.
- **`/etc/modprobe.d/99-cachy-nvidia.conf`** — CachyOS's NVIDIA options, adopted
  in full: `NVreg_DynamicPowerManagement=0x02` (a Turing-or-newer *mobile* dGPU
  powers fully down when idle; accepted-but-inert on older generations/desktops,
  drivers ≥ 435 know it) and `NVreg_InitializeSystemMemoryAllocations=0` (skip
  zeroing memory handed to the GPU — performance at an explicit security cost;
  shipped under the §3.1 performance-over-hardening stance; override with `=1`
  in a later modprobe.d file to revert per-box). Takes effect when the module
  loads, i.e. next boot on a running system.

---

### 3.4 Gaming userspace layer (runtime)

The third leg of the performance stool: **BORE** tunes the *scheduler* (§2), **zram**
tunes *memory* (§3.2), and this layer tunes the *per-game runtime* — non-kernel,
non-persistent optimisations that apply only while a game runs. It is pure
userspace: no runit service, no kernel involvement, trivially reversible.

Two upstream tools plus a composition wrapper:

- **`gamemode`** (Feral) — a D-Bus–activated user daemon (`gamemoded`; **no runit
  service**, like PipeWire) that, for the duration of a game, switches the CPU
  governor to `performance`, requests the GPU's high-perf mode, and applies
  nice/ionice. Activated per-process by `gamemoderun`. It is already in the
  allowlist, so the updater can rebuild it `-O3`; deploy.sh only guarantees it is
  present. **Privilege model (source-verified + live-verified):** the daemon
  never writes sysfs itself — it runs `pkexec cpugovctl set …`, the shipped
  polkit policy denies *everyone* by default, and the only grant is the shipped
  rules-file exception for the **`gamemode` group**. Void creates that group but
  adds no one, so the governor switch is silently inert on a stock install
  (pkexec: "Not authorized") — deploy.sh therefore adds the `--user` to the
  group (upstream's own sanctioned mechanism; polkit picks it up without a
  re-login). Requires polkit to be present at runtime (any polkit-using desktop
  ships it); without polkit the governor feature is inert and everything else
  about GameMode still works.
- **`MangoHud`** — an opt-in performance overlay (FPS/frametime/CPU+GPU temp),
  loaded as a Vulkan/GL layer via the `MANGOHUD=1` environment variable. The
  32-bit sibling `MangoHud-32bit` is needed for 32-bit titles and is therefore
  **multilib-gated** (§ INSTALL multilib); its absence is non-fatal. Two config
  profiles ship: the default **full** HUD, and a **minimal** HUD auto-selected on
  a legacy NVIDIA Optimus laptop (driver ≤ 470) — there the dGPU's load/power/temp
  are not reliably exposed via NVML during PRIME offload (even `nvidia-smi`
  struggles), so the GPU panel would read a misleading 0% while a game renders;
  the minimal profile keeps the accurate swapchain-based fps/frametime + CPU and
  drops the GPU sensors. `deploy.sh --hud-profile auto|full|minimal` overrides.
- **`gamescope`** — Valve's micro-compositor (stock Void package): the *display*
  leg of the runtime triad (gamemode = CPU, MangoHud = telemetry, gamescope =
  presentation). It isolates the game from desktop-compositor jank and provides
  frame limiting and FSR upscaling. Opt-in via `CACHY_GS=1` (add extra gamescope
  flags through `CACHY_GS_OPTS`, e.g. `-W 1920 -H 1080 -r 60`); it requires a
  working Vulkan driver, and — like every layer here — is skipped when absent.
- **`vkBasalt`** — a Vulkan post-processing layer (stock Void package, 32-bit
  sibling multilib-gated like MangoHud's); its contrast-adaptive sharpening
  pairs naturally with FSR upscaling. Opt-in via `CACHY_VKB=1` (which exports
  the layer's own `ENABLE_VKBASALT=1`); absent = inert, the env var is ignored.
  **Ships a restrained default config** (`effects = cas`, `casSharpness = 0.4`)
  at `/etc/vkBasalt.conf` — real-hardware finding: with *no* config file present
  at all, vkBasalt logs `no good config file` and silently applies nothing, so
  the toggle alone did not work until this shipped. `cachy-game` seeds a
  per-user copy for Proton/Steam-Runtime containers, same never-clobber
  pattern as MangoHud's (§3.4 below) — their `/etc` is their own.
- **Proton toolbox (optional, no wrappers needed):** `protontricks` +
  `winetricks` (prefix repair/injection — the workhorses behind "the game needs
  vcrun/dotnet/a font"), `Vulkan-Tools` (`vulkaninfo`/`vkcube` — the diagnostic
  companion to `--gpu`; note Void's **capital-V** package name), and
  `liberation-fonts-ttf` (metric-compatible Arial/Times substitutes — the classic
  missing-text fix in Windows games), and `wqy-microhei` (CJK coverage — the
  tofu-box fix for East-Asian text in games; CachyOS's gaming set ships the
  sibling `wqy-zenhei`, which Void does not package, so the packaged sibling is
  adopted instead — verified 2026-08-16). All are members of CachyOS's own
  gaming dependency sets (verified 2026-08-15/16) and stock Void packages — the
  §future-ideas maintenance test passes by construction.
- **`cachy-game`** — a launch wrapper that composes the offloader and gamemode:
  `gamemoderun` → `prime-run` (the NVIDIA PRIME offload, §6b) → optionally
  `gamescope --` → the game. It **skips any piece that is absent**, so it is
  correct on a desktop GPU (no `prime-run`) or a box without gamemode. MangoHud
  is opt-in via `CACHY_HUD=1`, gamescope via `CACHY_GS=1`, vkBasalt via
  `CACHY_VKB=1` — the wrapper stays invisible unless asked. The Steam per-title
  launch option becomes simply `cachy-game %command%`.

```sh
# /usr/local/bin/cachy-game  (composition core; the shipped file adds the
# Proton-container MangoHud-config seeding documented above)
#!/bin/sh
[ "${CACHY_HUD:-0}" = 1 ] && export MANGOHUD=1
[ "${CACHY_VKB:-0}" = 1 ] && export ENABLE_VKBASALT=1
if [ "${CACHY_GS:-0}" = 1 ] && command -v gamescope >/dev/null 2>&1; then
    set -- gamescope ${CACHY_GS_OPTS:-} -- "$@"
fi
command -v prime-run   >/dev/null 2>&1 && set -- prime-run "$@"
command -v gamemoderun >/dev/null 2>&1 && set -- gamemoderun "$@"
exec "$@"
```

- **`cachy-proton`** — a per-user helper (no root) that installs **Proton-CachyOS**
  (CachyOS's own gaming Proton fork — the fusion, grabbed as a prebuilt drop-in
  with no compile) into `~/.steam/root/compatibilitytools.d/`. It is **ABI-aware**:
  Proton-CachyOS ships a baseline `x86_64` and an `x86_64_v3` build, so on a
  pre-Haswell CPU it selects the baseline (the v3 build SIGILLs) — the same ABI
  ladder as the userland overlay (§1.2). It resolves the latest GitHub release,
  **verifies the `.sha512sum`** before extracting (trust-first), and is idempotent.
  Because Steam compat tools are strictly per-user state, this is a helper the user
  runs — never a root/deploy action.

**Out of scope:** `gamescope` (Valve's microcompositor) — valuable on modern GPUs
but unreliable on the `nvidia470` legacy driver, so it is not part of the layer
(install it by hand where it helps). `GE-Proton` (GloriousEggroll) is a fine
alternative to Proton-CachyOS but not shipped by default; install it by hand.

`deploy.sh` provisions this layer by default (it is what a *gaming* overlay is
for): ensure `gamemode` + `MangoHud` (+ `MangoHud-32bit` when multilib is
present), install `cachy-game`, and drop a restrained default
`/etc/xdg/MangoHud/MangoHud.conf`. Every item is ledger-tracked and reverts on
`--uninstall`.

---

## 4. The Automated Updater (`cachy-void-update`)

A standalone Python 3 script (stdlib only: `subprocess`, `tomllib`, `logging`). It runs as the **regular build user**; root is reached exclusively through `sudo` for the exact commands in Stage 4 (single privilege boundary, I4). A sudoers fragment (`/etc/sudoers.d/cachy-void`) grants NOPASSWD for `xbps-install`, `sv`, `xcheckrestart` (§4.7 — it must run as root to read system daemons' `/proc/*/maps`), `xbps-pkgdb`, and the three narrow GRUB staging binaries `grub-set-default`, `grub-reboot`, `grub-editenv` (§8.6) — nothing else.

Operational frame:

- **Locking:** `flock` on a lockfile; a second concurrent run exits immediately (code 10).
- **Logging:** per-run directory `~/.local/state/cachy-void/log/run-<timestamp>/` with one log per stage and one per package build. Keep the last 20 runs.
- **Modes:** `--dry-run` (print the queue and exit after Stage 2), `--yes` (unattended), default is interactive confirmation before Stage 3.

### 4.1 Configuration: `/etc/cachy-void/updater.toml`

```toml
[paths]
void_packages = "/home/gamer/void-packages"

[build]
jobs = 0    # 0 = nproc

[packages]
# The overlay allowlist: the ONLY packages ever built locally. Seed set:
targets = [
  "linux-cachy",
  "mesa", "vulkan-loader",
  "SDL2", "pipewire", "wireplumber",
  "wine", "gamemode", "mangohud",
  "ffmpeg",
]
# Absolute blacklist (I2). May be extended, never shrunk:
blacklist = ["glibc", "musl", "gcc", "binutils", "xbps", "runit", "base-files"]

[services]
# Managed by runit but never auto-restarted (session-fatal); reported instead:
restart_skip = ["udevd", "dbus", "elogind"]
```

Blacklist beats allowlist: a package appearing in both is never built, and the conflict is logged as a config error.

### 4.2 Stage 1 — Repository synchronization

```sh
cd $void_packages
git fetch upstream                       # upstream = void-linux/void-packages
pre=$(git rev-parse HEAD)
git pull --rebase upstream master
./xbps-src bootstrap-update              # keep the build chroot itself current
```

Failure handling: on any rebase error → `git rebase --abort`, assert `HEAD == $pre`, log `git status` and the conflicting paths, exit 20. Under I1 (additive-only overlay) a conflict is near-impossible; if one occurs it means someone edited upstream-tracked files — surface it loudly, never auto-resolve. `bootstrap-update` failure → exit 21, no system changes made.

### 4.3 Stage 2 — Queue construction (never guess)

Build the queue by set algebra; every input is queried live:

```
L = pkgnames from `./xbps-src show-local-updates`     (local repo outdated vs. templates)
M = allowlist members with NO binpkg in hostdir/binpkgs yet   (first-build bootstrap)
I = installed pkgnames from `xbps-query -l`
Q = ((L ∪ M) ∩ I ∩ targets) − blacklist
```

- The `M` term matters: on a fresh setup `show-local-updates` reports nothing (empty local repo), so without it the updater would never build anything. Detect via `xbps-query --repository=hostdir/binpkgs -p pkgver <pkg>`.
- Parsing rules: `show-local-updates` → take the first whitespace-separated field per non-empty line; `xbps-query -l` → field 2 is `pkgname-version`, split on the *last* `-`. Any line that doesn't parse aborts the stage (exit 30) — a format change must be noticed, not skipped past.
- **Drift check (warning only):** if `srcpkgs/linux<current-series>/template` carries a newer version than `linux-cachy`, print the §2.6 maintenance warning.
- `Q` empty → exit 0, nothing to do.
- **Superseded by §7.3:** the production queue formula extends the above with the pending-deploy `P` term and the srcpkg↔subpackage mapping layer. Implementations MUST follow §7.

### 4.4 Stage 3 — Topological build

```sh
./xbps-src sort-dependencies $Q          # strict topological order over the queue
for pkg in $ordered; do
    ./xbps-src -j$jobs pkg $pkg          # deps arrive as upstream binaries (I5)
done
```

On any non-zero exit: print the last 60 lines of that package's build log, halt everything, exit 40. The running system is untouched (I4). `-O3`/`-march` build failures are *expected occasionally*; the fix is a human decision (pin the package, add a per-template flag exception, or drop it from targets) — never an automatic retry with weaker flags.

### 4.5 Stage 4a — Index & install

```sh
xbps-rindex -a $void_packages/hostdir/binpkgs/*.xbps    # idempotent safety net
sudo xbps-install -Suy --repository=$void_packages/hostdir/binpkgs
```

Reached only when every build in Stage 3 succeeded — partial overlays are never installed.

#### 4.5a Empty-queue system pass

An empty overlay queue (`Q = ∅`) must not leave the rolling base stale: `--status`
tier [1] reports pending upstream updates, and an Update that then does nothing
breaks the "update everything" promise (same failure class as skipping Flatpak).
When `--commit` finds nothing to build or deploy it still queries
`xbps-install -Sun`; if upstream updates are pending it runs the same Stage-4
choreography with an empty deploy set — §9.5 pre-deploy snapshot, one `-Suy`
(same single call site as §4.5; the §4.6 takeover loop is vacuous), §4.7 service
cycling, then Flatpak. "Reached only when every build succeeded" holds vacuously:
zero builds were needed. Packages on `hold` (e.g. pinned kernels) are honored by
xbps itself. `--dry-run` still reports and exits before any mutation, and without
`--yes` the pass asks for confirmation first.

### 4.6 Stage 4b — Same-version takeover

Because local rebuilds share `pkgver` with upstream binaries (§1.3), `-Su` alone will not replace an installed upstream build with the freshly compiled one. For each `pkg ∈ Q` still originating from a non-overlay repo (check `xbps-query -p repository <pkg>`):

```sh
sudo xbps-install -fy --repository=$void_packages/hostdir/binpkgs <pkg>
```

This is the **only** sanctioned use of `-f` in the entire system (I7-compatible: it reinstalls the identical pkgver from the prioritized repo, nothing more). For persistence, the overlay is also registered system-wide via `/etc/xbps.d/00-cachy-overlay.conf`:

```
repository=/home/gamer/void-packages/hostdir/binpkgs
```

(xbps.d files apply in lexical order; `00-cachy-overlay.conf` sorts before `00-repository-main.conf`, so version ties resolve to the overlay.)

### 4.7 Stage 4c — Service lifecycle

1. Run `xcheckrestart` (from `xtools`): lists PIDs running deleted/replaced binaries or libraries.
2. Map PIDs to runit services by reading `/var/service/*/supervise/pid`.
3. For each matched service not in `restart_skip`: `sudo sv restart <service>`, then verify with `sv status`.
4. Matched-but-skipped services and unmatched PIDs (user session processes, games, compositors) are *reported* with a "restart/relogin required" notice — never killed.
5. If `linux-cachy` was in `Q`: print an unmissable **reboot required** banner and write a flag file in the state dir. No auto-reboot, no kernel purging (§2.5). When the kernel state machine is enabled, this step is replaced by the staged one-shot boot protocol (§8.6).

### 4.8 Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including "queue empty") |
| 1 | Config/usage error, or an unexpected fatal error caught by the CLI's last-resort boundary (a traceback reaching the user is itself a bug) |
| 10 | Lock held (another run active) |
| 20 / 21 | Git sync failed & rolled back / bootstrap-update failed |
| 30 | Query or parse failure in Stage 2 |
| 31 / 32 / 33 | Preflight failed / unresolvable dependency cycle / srcpkg-mapping anomaly (§7.8) |
| 40 | Package build failed (log tail emitted) |
| 50 / 51 | Index failure / `xbps-install` failure |
| 52 | Post-deploy verification failed (§7.7) |
| 60 | Success, but some service restarts skipped or incomplete |
| 70 | Kernel boot-staging failure (§8.6) |

### 4.9 Scheduling (runit-native)

Optional unattended runs via `snooze` under runit — no cron daemon, no timers:

```sh
# /etc/sv/cachy-void-update/run
#!/bin/sh
exec snooze -H 5 -M 30 /usr/local/bin/cachy-void-update --yes
```

**Implementation notes** (this sketch is elaborated as shipped in
`system/sv/cachy-void-update/`):
- The engine lives at `/usr/libexec/cachy-void-updater/cachy_void_update.py`
  (§6/§8.9), not `/usr/local/bin`, and takes `--config`. The service runs it as
  the unprivileged updater user via `chpst -u` (same model as cachy-health §8.7);
  the process is never root and reaches privilege only through the §4.1 sudoers
  grants.
- A "run" is `--sync` then, only on success, `--commit --yes` — the full
  unattended update, not a bare commit. Schedule (`SNOOZE_HOUR`/`SNOOZE_MINUTE`,
  which accept snooze patterns) lives in the companion `conf`.
- `snooze` flags verified against Void's `snooze-0.5.1`: `-H` is the hour field,
  `-M` the minute field (uppercase for time fields; `-M` is **not** month here).
- **Opt-in**: `deploy.sh` always provisions the service dir but only *enables* it
  with `--with-schedule`. An unattended build+deploy is a deliberate choice, so
  the default install leaves it disabled and one `ln -s` (or a re-run with the
  flag) away.

### 4.10 User-facing actions (amendment)

The engine exposes a small front-end surface (driven by the GUI, §6) beyond the
build/deploy core. Two are read-only; two extend the §4.1 sudo boundary by a
minimal, non-package-naming set:

- **`--no-kernel`** — scopes a `--commit` to userspace (disables `kernel_enable`
  for the run, gating synthesis/G2/build/staging). The GUI maps *Update* →
  `--commit --no-kernel` and *Update kernel* → full `--commit`.
- **`--clean`** — preview→confirm removal of orphans + obsolete cache. Grants
  **exactly** `xbps-remove -o|-O -n|-y` (flags that cannot name a package).
  **Kernel purges stay manual** (§2.5/§4.7): it only *prints* `vkpurge list`;
  `vkpurge` is never granted.
- **`--gpu`** — read-only advisory (card, installed driver + pending update,
  legacy-series hint, DKMS health). No mutation, no grant.
- **Flatpak** — an updater that silently skipped Flatpaks would give a false
  "fully updated" — so `flatpak update` is folded into every `--commit` (and a
  `[6]` line in `--status`). Per-user installs need no privilege; system installs
  add **exactly** `flatpak update --system -y` (updates installed refs only; no
  install/remove). No-op when Flatpak is absent; failures are surfaced, never
  swallowed. Exit 56 on a Flatpak-only failure (never undoes the XBPS deploy).
- **Health battery H2** — with `kernel.dmesg_restrict=1` (hardened kernels,
  observed on real hardware) the unprivileged daemon cannot read dmesg at all,
  making H2 structurally always-False. The boundary adds **exactly**
  `dmesg --level=emerg,alert,crit` (fixed argument, read-only); the checker
  tries unprivileged first and falls back to the grant.

---

## 5. Recovery Runbook

- **Corrupted local repo index:** delete `hostdir/binpkgs/x86_64-repodata`, then re-run `xbps-rindex -a hostdir/binpkgs/*.xbps`. (Note: `xbps-pkgdb -m repolock|repounlock <pkg>` pins/unpins a *package* to its repository — it is **not** an index-repair tool. A prior draft of this document claimed otherwise; that claim is retired.)
- **Failed build (exit 40):** system untouched. Read the emitted log tail; either fix the template locally, add a flag exception, or remove the package from `targets`. Re-run.
- **Failed install (exit 51):** XBPS transactions are per-run atomic at the package level. Run `xbps-pkgdb -a` to verify pkgdb integrity, then `sudo xbps-install -Su` from upstream mirrors to converge to a consistent state. The upstream binary always exists as the fallback for every overlay package.
- **Bad kernel:** boot the previous kernel from the GRUB menu (it is always still installed, §2.5), then `vkpurge rm <bad-ver>` and rebuild. If the overlay repo itself is suspect, `sudo xbps-install -f linux<series>` from upstream restores a stock kernel.
- **Nuke the overlay entirely:** remove `/etc/xbps.d/00-cachy-overlay.conf`, then `sudo xbps-install -Suf` of the affected targets from upstream mirrors. The base system was never anything but stock Void — this always converges.
- **Roll back a bad userland deploy (btrfs hosts):** if pre-deploy snapshots are enabled (§9.5), restore the pre-deploy snapshot — `sudo btrfs subvolume set-default <id> <mount>` then reboot, or select it via grub-btrfs. A convenience over the always-converges path above, never a replacement for it.

---

## 6. Repository Deliverables Map

What this repository must eventually contain; each file traces back to a section:

```
cachy-void/
├── architecture.md                      # this document (authoritative)
├── overlay/
│   ├── srcpkgs/linux-cachy/             # §2, §8.4: template, patches/0001-bore.patch,
│   │                                    #   files/x86_64-dotconfig (regenerated artifact)
│   └── config/cachy-fragment.config     # §2.4 fragment — input to §8.4 regeneration
├── system/
│   ├── etc/conf                         # §1: compiler profile (→ void-packages/etc/conf)
│   ├── sysctl.d/99-cachy-gaming.conf    # §3.1
│   ├── udev/60-ioschedulers.rules       # §3.3
│   ├── modprobe.d/99-gaming-input.conf  # §3.3
│   ├── xbps.d/00-cachy-overlay.conf     # §4.6, §7.2 (lists both repo roots)
│   ├── sudoers.d/cachy-void             # §4 privilege boundary
│   ├── bin/cachy-game                   # §3.4 game launch wrapper (→ /usr/local/bin)
│   ├── bin/cachy-proton                 # §3.4 Proton-CachyOS installer (→ /usr/local/bin)
│   ├── bin/cachy-branding               # branding.md applier, per-user (→ /usr/local/bin)
│   ├── xdg/MangoHud.conf                # §3.4 full HUD (→ /etc/xdg/MangoHud/)
│   ├── xdg/MangoHud-minimal.conf        # §3.4 legacy-Optimus HUD (no GPU sensors)
│   ├── config/picom.conf               # branding.md §5.3 compositor (matte shadows)
│   ├── branding/{openbox,plank,rofi,conky}/  # branding.md theme assets (→ /usr/share/cachy-void/branding)
│   ├── sv/zramen/{run,finish,conf}      # §3.2 zram service (run: override template)
│   ├── sv/cachy-health/{run,conf}       # §8.7 post-boot health daemon service
│   └── sv/cachy-void-update/{run,conf,log/run}  # §4.9 scheduled-update service (opt-in)
├── updater/
│   ├── cachy_void_update.py             # §4: CLI entry point (+ --health-daemon)
│   ├── engine/                          # ddre.py (§7), xbps.py (§7.2), journal.py (§7.6),
│   │                                    #   grub.py (§8), health.py + health_daemon.py (§8.7),
│   │                                    #   trust.py (§8.3), template.py (§8.4), atomicio.py
│   ├── bore.lock                        # §8.3 patch-trust lockfile (local, human-owned)
│   └── updater.toml                     # §4.1 + §8.9 (→ /etc/cachy-void/)
└── deploy.sh                            # installs system/ files + mirrors the Python
                                         #   engine to /usr/libexec/cachy-void-updater,
                                         #   enables the cachy-health runit service, and
                                         #   auto-simulates on WSL2 (--simulate) to avoid
                                         #   destructive ops with no runit PID 1
```

---

## 7. The Dynamic Dependency Resolution Engine (DDRE)

Normative specification of the updater's Stage 2–4 internals. **Supersedes the §4.3 queue formula; extends §4.8.** All build-side commands run from `$void_packages` as the build user. Governing doctrine: *the pkgdb and the package repositories are the only authorities; every control-flow decision is recomputed from live queries* (§7.6).

### 7.1 Name domains & the mapping layer

Two name domains exist and MUST NOT be conflated:

- **srcpkg** — a template directory `srcpkgs/<S>/`. Build-side commands (`pkg`, `sort-dependencies`, `show-build-deps`, `show-local-updates`) speak srcpkg.
- **binpkg** — an installable package. System-side commands (`xbps-query`, `xbps-install`, `xcheckrestart`) speak binpkg. Every template produces a binpkg named exactly `S`; subpackages are *additional* binpkgs with other names.

binpkg → srcpkg mapping uses the void-packages invariant that every subpackage exists as a symlink `srcpkgs/<sub> → <parent>`:

```python
def srcpkg_of(b: str) -> str | None:
    p = SRCPKGS / b
    if p.is_symlink(): return Path(os.readlink(p)).name
    if p.is_dir():     return b
    return None        # no template: removed upstream or foreign .xbps — "not ours"
```

- `targets` in `updater.toml` MUST be srcpkg names. A target that resolves through a symlink is normalized to its parent with a logged warning; a target with `srcpkg_of() is None` is a config error (exit 1).
- Forward enumeration of a template's subpackages is never needed; the inverse suffices everywhere: `subpkgs_installed(S) = { b ∈ I : srcpkg_of(b) = S }`.
- **No-widen rule (normative):** Stage 4 may only reinstall or upgrade binpkgs *already installed*. The updater NEVER installs a binpkg name ∉ I — deploying srcpkg `mesa` when only `mesa-dri` is installed must not introduce binpkg `mesa` onto the system.

### 7.2 Live-query primitives & parsing contracts

| Value | Command | Contract |
|---|---|---|
| `I` (installed binpkgs) | `xbps-query -l` | field 2 = `name-ver_rev`; `NORM` |
| `inst_ver(b)` | `xbps-query -p pkgver <b>` | full pkgver |
| `origin(b)` | `xbps-query -p repository <b>` | absolute repo path |
| `repo_ver(S)` | `xbps-query --repository=<r> -p pkgver <S>` per `r ∈ R` | empty ⇒ absent |
| `L` (outdated srcpkgs) | `./xbps-src show-local-updates` | field 1 per non-empty line; `NORM` |
| build deps of `S` | `./xbps-src show-build-deps <S>` | one token per line; `NORM` |
| topological order | `./xbps-src sort-dependencies <S…>` | one srcpkg per line |

`NORM(tok)`: take the first whitespace-separated field → strip a leading `virtual?` → strip a trailing version constraint (`[<>=].*`) → where a bare name is required, strip a trailing pkgver suffix matching `-[^-]+_[0-9]+$`. Any token violating its contract aborts the stage with exit 30 — format drift MUST surface, never be skipped past.

Version ordering MUST be delegated to `xbps-uhelper cmpver` through a single `vercmp()` wrapper whose interpretation of the exit-code convention is pinned by unit tests over fixed triples (`1.0_1 < 1.0_2 < 1.1_1`). Reimplementing XBPS version semantics is forbidden.

Local repo roots: `R = [hostdir/binpkgs, hostdir/binpkgs/nonfree]` — restricted targets (e.g. nvidia) land in `nonfree` and additionally require `XBPS_ALLOW_RESTRICTED=yes` in `etc/conf`. Every repo query and every `--repository=` flag iterates all of `R`; `00-cachy-overlay.conf` lists both roots.

### 7.3 Queue algebra (supersedes §4.3)

```
S(I)         = { srcpkg_of(b) : b ∈ I } − {None}
inst_vers(t) = { inst_ver(b) : b ∈ I, srcpkg_of(b) = t }

L = NORM'd show-local-updates              # template newer than local repo binpkg
M = { t ∈ targets : repo_ver(t) absent in all R }        # never built (first run)
P = { t ∈ targets : repo_ver(t) exists ∧
      ( max_ver(inst_vers(t)) < repo_ver(t)              # built but never deployed
        ∨ |inst_vers(t)| > 1 ) }                         # subpackages diverged
O = { t ∈ targets : repo_ver(t) exists ∧
      max_ver(inst_vers(t)) = repo_ver(t) ∧              # versions already equal…
      ∃ b ∈ I : srcpkg_of(b) = t ∧ origin(b) ∉ R }       # …but takeover never completed

Q_build  = ((L ∪ M) ∩ S(I) ∩ targets) − blacklist
Q_deploy = Q_build ∪ (((P ∪ O) ∩ S(I)) − blacklist)
```

Why `P` exists — **the orphan hole**: if a run builds A and B but dies at C, Stage 4 never fires. A and B now sit in the local repo newer than their installed versions, yet appear in neither `L` (repo already matches template) nor `M` (binpkgs exist). Without `P` they would stay orphaned until the next upstream bump. The `|inst_vers(t)| > 1` arm heals the other partial state — subpackages of one template installed at different versions (e.g. an install transaction interrupted by power loss) — by forcing the template back through deploy until convergent. Both recoveries are computed from live queries alone: **losing every updater state file costs nothing but log history.**

Why `O` exists — **the takeover hole**: the §4.6 same-version takeover runs *after* `-Su`. A crash between the two leaves binpkgs at the *same* version as the local repo but still originating from an upstream mirror — invisible to `L` (repo == template), `M` (a binary exists), and `P` (`vercmp == 0`, no divergence), so the takeover would be orphaned until the next version bump. `O` keys on installation *origin* — the only live signal that distinguishes "takeover done" from "takeover pending" — making the takeover self-healing across interruptions, again from live queries alone.

**K-exemption (kernel introduction).** `linux-cachy` is the one package the overlay *introduces* rather than takes over, so it bypasses the `∩ S(I)` installed-gate: once its template exists (post-§8.4 synthesis) and `L ∪ M` evidence holds, it enters `Q_build`/`Q_deploy` despite not being installed. Stage 4 then performs the **single sanctioned widen** — an explicit first install of `linux-cachy` **and `linux-cachy-headers`** (§2.5, so dkms modules such as nvidia build during install) — before §8.6 staging. The no-widen rule stays absolute for every other package. *(Found on the first real kernel run: the integration fixture had pre-installed the kernel, masking this bootstrap hole.)*

`Q_build = ∅ ∧ Q_deploy = ∅` ⇒ exit 0.

### 7.4 Ordering: verified sorter, graph fallback, cycle groups

**Primary path.** Run `./xbps-src sort-dependencies $(sort(Q_build))` — input lexicographically sorted for determinism. Accept the result iff exit status is 0 AND the output is exactly a permutation of the input (same set, same cardinality). Anything else ⇒ the sorter is distrusted for this run and the fallback engages. Silently proceeding with a dropped or duplicated node is forbidden.

**Fallback — restricted graph.** Vertices = `Q_build`; edge `dep → S` for every `dep ∈ NORM(show-build-deps S)` whose srcpkg-normalized name is itself in `Q_build`. Virtual deps that do not normalize to a queue member contribute no edge — they are satisfied as binaries (I5).

1. Tarjan SCC decomposition → condensation DAG → Kahn's algorithm over it, lexicographic tie-break (deterministic output for identical inputs).
2. Every SCC with |SCC| > 1, and every self-loop, is a **cycle group**:
   - **Seed rule:** the group is buildable iff at least one member already has a binary in `R` or on an upstream mirror (`xbps-query -R -p pkgver`). No seed anywhere ⇒ exit 32 — a cycle with no binary seed cannot be bootstrapped mechanically; that is a human decision.
   - Members build in lexicographic order; each pulls its cyclic partners as *existing older binaries*, which is sound by I5.
   - **Two-pass convergence:** after the full queue completes, every cycle-group member is rebuilt exactly once more in the same order, so each links against its partners' new versions. Exactly one extra pass — a convergence fixpoint loop is forbidden (unbounded).
3. The chosen order and its provenance (`sorter` | `fallback` + SCC list) are written to the journal before the first build.

### 7.5 Stage 3 execution loop

Preflight (any failure ⇒ exit 31, nothing mutated):
- masterdir initialized: marker `masterdir*/.xbps_chroot_init` exists — otherwise instruct `./xbps-src binary-bootstrap` and stop;
- free disk ≥ `build.min_free_gib` (default 30) on both `hostdir`'s and the masterdir's filesystems.

```python
order, second_pass = topo_order(Q_build)              # §7.4
for S in [*order, *second_pass]:
    journal.set(S, "building")
    run(["./xbps-src", "clean", S])                   # idempotent: purge stale wrksrc
    rc = run_logged(["./xbps-src", f"-j{cfg.jobs}", "pkg", S],
                    log=rundir / f"build-{S}.log",
                    timeout=cfg.timeout_min or None)
    if rc != 0:
        journal.set(S, "failed"); emit_tail(log, 60); sys.exit(40)
    journal.set(S, "built")
```

- **Isolation guarantee:** failure at member *k* leaves members 1..k−1 as binpkgs in `R` only; the running system is untouched (Stage 4 unreached); those binpkgs are recovered by the next run's `P` term without rebuilding.
- **Timeout** ⇒ SIGKILL the entire process group (builds spawn chroot children; killing the leader alone leaks them), then `./xbps-src clean S`, then exit 40 with build-failure semantics. If subsequent builds fail with chroot/mount errors after a timeout kill, the remedy is `./xbps-src zap && ./xbps-src binary-bootstrap` (§5).
- No retries, no flag-weakening retries (§4.4 stands). A failed wrksrc is deliberately left on disk for forensics; the `clean` at the next attempt removes it.

### 7.6 State journal — witness, never authority

`~/.local/state/cachy-void/journal.json`:

```json
{ "schema": 1, "run_id": "20260705T193002Z", "git_head": "<sha>",
  "phase": "sync|query|build|deploy|done|failed",
  "order": ["…"], "order_provenance": "sorter|fallback",
  "pkgs": { "mesa": { "status": "pending|building|built|failed",
                      "log": "build-mesa.log", "started": "…", "ended": "…" } },
  "deploy_bins": ["mesa-dri", "…"],
  "failure": { "pkg": "wine", "exit": 40 } }
```

Writes are atomic: temp file in the same directory → `fsync` → `os.replace`. An append-only `journal.log` (JSON-lines) accompanies each snapshot as the human audit trail, written **ahead** of the snapshot commit (WAL discipline: after a crash, the log's final line names the transition that may not have reached `journal.json`). Both files tolerate torn final writes; neither is ever read by control flow. Journals archive with the run's log directory (keep 20, §4).

**Doctrine (normative):** the journal is a witness for humans and forensics only. No control-flow decision may read it. Crash and failure recovery is achieved by *recomputing §7.3 from live queries* — the `P` term is the resume mechanism. A missing, stale, or corrupt journal produces a warning and nothing else. Consequently there is deliberately **no `--resume` flag**, and the journal/reality-divergence bug class cannot exist.

### 7.7 Stage 4 deploy gate (refines §4.5–§4.7)

Reached only when every `Q_build` member is `built`. Note that `-Su` deliberately performs the *general* system update (upstream binaries for everything outside the overlay) in the same transaction — this tool is the system updater, with overlay priority on version ties.

```python
deploy_bins = sorted(b for b in I if srcpkg_of(b) in Q_deploy)       # no-widen rule
run(["xbps-rindex", "-a", *repo_globs(R)])                           # idempotent safety net
sudo(["xbps-install", "-Suy", *[f"--repository={r}" for r in R]])
for b in deploy_bins:                                                # §4.6 same-pkgver takeover
    if origin(b) not in R:
        sudo(["xbps-install", "-fy", *[f"--repository={r}" for r in R], b])

# Post-verify — any failure ⇒ exit 52:
for b in deploy_bins:
    assert origin(b) in R
    assert vercmp(inst_ver(b), repo_ver_bin(b)) == 0    # binpkg b's pkgver in R
for t in Q_deploy:
    assert len(inst_vers(t)) == 1                       # convergent: no partial deploy
```

The convergence assertions cover the *userspace* deploy only: `linux-cachy` is excluded — it is introduced and verified through the §8.6 staging path (on a first bump it is not yet installed at this point, and keeping the previous known-good kernel installed alongside the candidate is deliberate, §2.5/§4.7), so the single-version check does not apply to it here.

**Shared-library rejection:** XBPS validates shlib requires/provides at transaction time. If `xbps-install` refuses on shlib grounds ⇒ exit 51 and HARD STOP. Forcing past a shlib error is forbidden in all circumstances — it is XBPS proving the overlay would break ABI coherence. Recovery per §5. Service cycling then proceeds per §4.7; if `linux-cachy ∈ Q_deploy`, hand off to §8.6 instead of the generic reboot banner.

### 7.8 Failure taxonomy (extends §4.8)

| Exit | Condition | System state afterwards |
|---|---|---|
| 31 | preflight: masterdir uninitialized / low disk | untouched |
| 32 | cycle group with no binary seed | untouched |
| 33 | hard srcpkg-mapping anomaly on a managed path | untouched |
| 40 | build failure or timeout at member *k* | untouched; k−1 binpkgs await next run's `P` |
| 51 | install failure / shlib rejection | transaction-atomic; run `xbps-pkgdb -a`, then §5 |
| 52 | post-deploy verification mismatch | deployed but unproven — investigate before any further run |

---

## 8. The Version-Sensitive Kernel Injection State Machine (KISM)

Automates §2.2–§2.6 for **patch-level** kernel bumps; series changes and every anomaly stop at an explicit human gate. Division of labor: KISM owns template regeneration, patch trust, and the boot lifecycle; the DDRE (§7) compiles and deploys `linux-cachy` as an ordinary queue member. KISM runs inside `cachy-void-update` between Stage 1 and Stage 2, plus a root-owned confirm service at boot (§8.7).

A kernel-path stall (any `AWAIT_*`/`HALT_*` state) never blocks userspace updates: the updater proceeds without the kernel bump and exits 0 with a prominent warning; `cachy-void-update kernel status` reports the machine state.

### 8.1 Persistent state

`/var/lib/cachy-void/kernel/kernel-state.json` (the `kernel/` directory is created by `deploy.sh` **owned by the build user**, so the unprivileged updater records staging transitions without widening sudo; the root confirm service records boot verdicts — root writes anywhere. 0644; atomic writes as §7.6; every transition appended to `kernel-state.log` as an audit trail):

```json
{ "schema": 1,
  "state": "TRACKING",
  "base_series": "6.12",
  "ported_version": "6.12.34",
  "candidate": { "pkgver": "6.12.35_1", "kver": "6.12.35_1",
                 "built": false, "installed": false },
  "known_good": { "kver": "6.12.34_1", "grub_ref": "…" },
  "grub": { "mode": "oneshot", "candidate_ref": "…", "default_ref": "…" },
  "bore": { "pinned_commit": "<sha>", "patch_sha256": "<hex>", "bore_version": "…" },
  "services_up_at_staging": ["…"],
  "staged_boot_id": "<uuid>",
  "history": [] }
```

Void kernel packages encode the full pkgver in the kernel release string: a booted candidate is identified by **exact string equality** of `uname -r` against `candidate.kver` (e.g. `6.12.35_1`). This exactness is load-bearing; never substring-match.

### 8.2 Bump detection & classification (runs after Stage 1)

1. Parse `version=` and `revision=` from `srcpkgs/linux${base_series}/template` (`^version=([0-9.]+)$`, `^revision=([0-9]+)$`; parse failure ⇒ HALT).
2. Compare against `ported_version` via `vercmp`:
   - equal → no event;
   - newer, same series → **BUMP_PATCHLEVEL** (automated path); revision-only bumps count — template fixes must flow;
   - `srcpkgs/linux${base_series}` missing (series EOL'd/removed) → **AWAIT_HUMAN_SERIES**.
3. Informational only: if the `linux` meta-package now points at a newer series, log a notice. Series switching is always a human act (new BORE patch family + dotconfig review).

### 8.3 BORE patch trust pipeline

Chain of trust, stated once: **(a)** kernel tarballs are sha256-pinned by upstream Void's own template `checksum=`, inherited verbatim by regeneration (§8.4, ASSERT-C); **(b)** the BORE patch — the only foreign artifact in the overlay — is pinned by `updater/bore.lock`; **(c)** at rest, the overlay git repo content-addresses everything.

`bore.lock` (committed; edited by humans only, at approval time):

```toml
[repo]
url           = "https://github.com/firelzrd/bore-scheduler"
pinned_commit = "<full commit sha>"

[[patch]]
series       = "6.12"
file         = "<path within repo at pinned_commit>"
sha256       = "<hex>"
bore_version = "…"
approved     = "2026-07-05 twb"
```

Procedure on BUMP_PATCHLEVEL:

1. **Reuse-first:** if `srcpkgs/linux-cachy/patches/0001-bore.patch` exists and its sha256 equals the lockfile entry for `base_series` → reuse, no network. BORE patches routinely apply unchanged across patch-level kernel releases; gate G1 (§8.5) is the arbiter of whether reuse actually holds.
2. Otherwise fetch: `git fetch` the pinned commit from the locked URL into a cache clone, extract `file`, sha256 it. Match against lockfile → stage as `patches/0001-bore.patch`. Mismatch → **HALT_HASH_MISMATCH**: freeze the kernel path; possible upstream tamper or a moved file — a human verifies and re-pins.
3. When a reused patch **fails gate G1** (the kernel drifted enough to break it): fetch the current tip of the BORE repo, locate the series patch, and present at **AWAIT_HUMAN_PATCH**: a unified diff of old patch → new patch, plus the new sha256 and commit. Approval = the operator updates `bore.lock` (new `pinned_commit`, `sha256`, `approved`) and re-runs. The machine NEVER self-updates the lockfile — trust-on-first-use is a one-time human act per artifact.

**Implementation (`engine/trust.py`).** The lockfile is **local and human-owned; it is never fetched over the network** — fetching the expected hashes alongside the artifact would collapse the trust model (a network adversary would supply both). Only the *patch artifact* crosses the network, and it is verified against the local `bore.lock`. Typed failures and their exit mapping (kernel-path per §8 preamble — in the integrated flow these *withhold the kernel and let userspace continue*; the codes below apply when trust is the terminal operation, e.g. `kernel approve-patch`):

- `TrustConfigError` — `bore.lock` missing, unparseable, or a `sha256` that is not 64 hex chars → **exit 1** (a broken committed lockfile is an operator/config defect).
- `HashMismatch` — a fetched or cached patch whose sha256 ≠ the pinned value → **HALT_HASH_MISMATCH**, **exit 70**. Possible tamper or moved file; a human verifies and re-pins.
- `PatchUnavailable` — offline **and** no valid cached patch, so trust cannot be bootstrapped → **exit 70**.

**Offline/degraded fallback (permitted):** reuse-first (step 1) is the offline path — a cached `patches/0001-bore.patch` whose sha256 matches `bore.lock` is trusted with **no network**. A network timeout during step 2 falls back to that cached patch if (and only if) it validates; otherwise `PatchUnavailable`. Cache validation never weakens the hash check — an invalid cache is `HashMismatch`, never a silent pass.

#### 8.3a Assisted pinning (`--pin-bore` / the GUI's "Pin BORE patch" button)

"The machine never self-updates the lockfile" survives intact — what changed is *what counts as the clerical part*. The pin was always two things fused together: a **human trust decision** ("I vouch for this patch") and **clerical work** (locate the series patch upstream, download it, compute sha256, hand-edit TOML). The clerical half is exactly where users fail — nobody discovers a lockfile path from a GUI with one Update button — so `trust.discover_bore_patch()` + `trust.append_pin()` automate it while the decision stays human and explicit:

- **Surfacing:** `--status` tier [3] always states the pin state out loud ("BORE pin: series X pinned …" / "BORE pin: MISSING …"). The GUI keys a warning banner + **Pin BORE patch…** button off the MISSING marker — the paused kernel is a visible, one-click-fixable state, never a silent one.
- **Flow:** discovery fetches upstream **HEAD**, selects the series' `0001-…bore….patch` (companion patches like the SMT-idle tweak are ignored — only ONE file is ever pinned; ambiguity refuses and defers to a manual pin), and presents series/commit/file/sha256/size. Nothing is written until the human approves — terminal `[y/N]` for the CLI, the confirm dialog for the GUI (`--pin-bore --dry-run` previews, then `--pin-bore --yes` writes; the dialog *is* the approval). The update pipeline itself never calls any of this: an unpinned series still just withholds the kernel.
- **Per-entry `commit`:** an appended `[[patch]]` records the upstream commit it was found at, and `ensure_trusted_patch` fetches each entry at `entry.commit or repo.pinned_commit` — so pinning a new series at today's HEAD can never invalidate an older entry whose file moved since `pinned_commit`. Approved pins also seed the §8.3 artifact cache immediately (the pin works offline from the moment it is made).
- **Ownership/merge (deploy.sh):** the mirrored lockfile is owned by the updater user (a root-owned anchor would dead-end the flow behind a sudo grant §4.1 doesn't have), and redeploys **merge**: ship the repo lockfile, then re-append local `[[patch]]` series it doesn't carry — repo-shipped pins refresh, locally-approved pins survive.
- **Deliberate limits:** replacing an existing pin (post-`HALT_HASH_MISMATCH`) stays a manual, eyes-on edit — `append_pin` refuses duplicates. Fully unattended pinning stays unimplemented: it would collapse trust-on-first-use into "trust whatever GitHub serves today".

### 8.4 Deterministic template regeneration

Never textually patch the previous fork — **regenerate from the current upstream template every time** (idempotent; zero drift accumulation). All work happens in a temp worktree; `srcpkgs/linux-cachy` is swapped only after every assertion passes, so a failed regeneration leaves the previous fork untouched for free.

```
REGEN(series):
  W ← copy of srcpkgs/linux<series>/                    # fresh upstream truth
  transform W/template (exact-match substitutions only):
      s/^pkgname=linux<series>$/pkgname=linux-cachy/
      s/^linux<series>-(headers|dbg)_package\(\)/linux-cachy-\1_package()/
      subpackages="…" list entries likewise
  W/patches/0001-bore.patch            ← §8.3 verified artifact
  W/files/x86_64-dotconfig             ← upstream dotconfig ⧺ "\n" ⧺ overlay/config/cachy-fragment.config
  ASSERT-A: zero remaining literal "linux<series>" tokens in W/template
  ASSERT-B: W/template defines pkgname=linux-cachy AND linux-cachy-headers_package()
  ASSERT-C: checksum= lines byte-identical to upstream's (we add no distfiles)
  atomically replace srcpkgs/linux-cachy with W; commit to overlay branch
  LINK: for each <sub>_package() function, create the sibling symlink
        srcpkgs/<sub> -> linux-cachy (xbps-src resolves subpackages through
        these; without them the kernel COMPILES then dies at packaging with
        "nonexistent file: srcpkgs/linux-cachy-dbg/template" — first-kernel find)
  SUFFIX (ASSERT-D): suffix the release string coherently on BOTH sides —
        template `_kernver="${version}_${revision}-cachy"` AND dotconfig
        `CONFIG_LOCALVERSION="_<rev>-cachy"` — so the fork installs strictly
        side-by-side with the same-version stock kernel. MANDATORY, not
        cosmetic: xbps's file-conflict check is transaction-scoped and does
        NOT refuse taking over an already-installed package's paths (it
        silently overwrote the stock kernel on the first real install —
        finding #8; cf. github.com/void-linux/xbps issue #287). Prior art:
        CachyOS/linux-tkg kernels all carry uname suffixes (-cachyos, -bore)
        for exactly this reason. Consequence: the kernel RELEASE (uname -r,
        the kver in §8.1 state) is pkgver+suffix and MUST be derived from the
        package's installed vmlinuz filename, never from pkgver.
```

Any assertion failure → **AWAIT_HUMAN_TEMPLATE** with the offending diff attached. No `xgensum` exists in this flow — ASSERT-C proves checksums are inherited. (The dotconfig append relies on kconfig's documented behavior that later entries win during `oldconfig`; the resulting warnings are expected noise.)

**Implementation (`engine/template.py`).** `XbpsTemplateEditor` performs *only* the exact-match line transforms above on template **text** (rename `pkgname` and the `*_package()`/`subpackages=` identifiers); the `synthesize()` orchestrator runs the full REGEN into a temp worktree and atomically swaps. Three things it deliberately does **not** do, because they contradict this section and are common ways to reintroduce drift:

- **It never edits `version`, `revision`, or `checksum`.** Those are *inherited byte-for-byte* from the freshly copied upstream template — that is the whole anti-drift point, enforced by ASSERT-C. A "version/checksum injector" is an anti-pattern here; the version bump *is* the upstream copy. `parse_pkgver()` reads them for reporting only.
- **It never injects `-march`/`-O3` into the kernel template.** Kernel image flags stay stock (§2.4 — `-O3` kernels are a known breakage source). Host-CPU tailoring belongs in `etc/conf` (§1.2, userland ABI level), applied by `deploy.sh`. `detect_march()` is provided as a §1.2 *`etc/conf` recommender* (v3 default; v4 only on a full AVX-512 subset) and never touches `srcpkgs/linux-cachy`.
- **It never edits the template to "reference" the patch.** `xbps-src` auto-applies everything in `patches/` (§2.3); synthesis just drops the §8.3-verified artifact there.

Failures raise `TemplateSynthesisError` (a missing upstream template, a missing verified patch, or any failed ASSERT). It is a kernel-path halt → **AWAIT_HUMAN_TEMPLATE**; exit **70** when synthesis is the terminal operation, or a withhold-and-continue in the integrated flow (§8 preamble).

**Integration point.** The circuit is closed in `cachy_void_update._kernel_synthesis`, which runs at the top of `--commit` *before* the queue is built (so a regenerated `linux-cachy` enters Q organically via its bumped template version): classify (§8.2) → `trust.ensure_trusted_patch` (§8.3) → `template.synthesize` (§8.4), each transition recorded to `kernel-state.json`. The distinct stall states are preserved end-to-end — a **trust** failure records `HALT_HASH_MISMATCH` (integrity) or `AWAIT_HUMAN_PATCH` (bad lockfile), a **synthesis** ASSERT failure records `AWAIT_HUMAN_TEMPLATE`; all are captured so userspace deploy proceeds regardless.

### 8.5 Validation gates (cheap → expensive; fail = revert the regenerated template)

| Gate | Command | Catches |
|---|---|---|
| **G1 apply** | `./xbps-src patch linux-cachy` | BORE patch no longer applies to the bumped tree. Runs fetch/extract/patch phases only — minutes, no compilation. Fail → §8.3 step 3 (AWAIT_HUMAN_PATCH). |
| **G2 config** | `./xbps-src configure linux-cachy`, then assert every symbol of the §2.4 fragment in `masterdir*/builddir/linux*/.config` (the glob MUST match **exactly one** file — zero or several is itself a gate failure, since stale builddirs could feed the wrong config; `CONFIG_X=v` lines must appear literally; `# CONFIG_X is not set` lines must appear literally or the symbol must be absent) | **Silent oldconfig drops.** If the BORE patch failed to introduce `SCHED_BORE`'s Kconfig entry, `oldconfig` deletes the unknown symbol *without any error* and you ship a stock-scheduler kernel that "built fine". This gate is the only defense against that outcome; it is not optional. Fail → AWAIT_HUMAN_TEMPLATE. |
| **G3 build** | ordinary DDRE Stage 3 (`pkg linux-cachy`) | `-O3`/codegen/toolchain breakage; §7 semantics apply (exit 40 → AWAIT_HUMAN_BUILD). |

On G1+G2 pass the template commit stands and `linux-cachy` enters the §7 queue organically (its template version now exceeds the local repo's). `ported_version` is **not** yet advanced — only PROMOTED advances it (§8.8): the tracked base moves when a kernel *boots healthy*, not when it compiles.

The fragment's runtime copy is installed by `deploy.sh` at `/etc/cachy-void/cachy-fragment.config` (source of truth: `overlay/config/cachy-fragment.config`, §6). A missing runtime fragment is a G2 **failure**, never a skip — the gate is not optional. A G2 failure withholds `linux-cachy` from the current run (state `AWAIT_HUMAN_TEMPLATE`) while userspace updates proceed (§8 preamble).

### 8.6 Boot staging: the one-shot promotion protocol

Preflight (soft failures degrade to `grub.mode = "manual"`; hard errors → exit 70):

- `findmnt -no FSTYPE --target /boot/grub` ∈ {ext2, ext3, ext4, vfat}. GRUB cannot rewrite `grubenv` on btrfs/zfs/LVM/RAID — there, a one-shot entry is never consumed and would boot-loop into the candidate; oneshot mode is **forbidden** on those filesystems. See §9.3 for the recommended host layout that keeps `oneshot` available.
- `GRUB_DEFAULT=saved` present in `/etc/default/grub`. The sanctioned edit that establishes it is performed **once, by `deploy.sh --with-grub`** (root context, backed up, manifest-tracked, reversible) — never at staging time; staging only *verifies*. If absent, the layout is **`manual-unsafe`**: `grub-set-default` writes would be silently ignored and the newest installed kernel typically becomes the default, so staging **refuses** (exit 70) and names the remedy. This supersedes the earlier "set it during preflight" wording — the updater process never edits bootloader config.
- Resolve GRUB refs (below) for candidate and known-good; any ambiguity → exit 70.

**GRUB ref resolution.** Parse `/boot/grub/grub.cfg` (just regenerated by Void's kernel hook): collect `menuentry`/`submenu` lines and their `$menuentry_id_option '<id>'` values. The ref for kernel `KVER` is `<submenu_id>><entry_id>` where `<entry_id>` contains the exact `KVER` string (top-level ref if `GRUB_DISABLE_SUBMENU` is in effect). Require **exactly one** match per kernel; zero or multiple → exit 70. Menu *titles* are never matched — ids only.

**First install:** if the candidate kernel is not yet installed (initial adoption), Stage 4 installs `linux-cachy linux-cachy-headers` from the overlay repo explicitly before staging — the K-exemption's completing act (§7.3); headers per §2.5 so kernel hooks build dkms modules (nvidia) immediately.

Staging (oneshot mode):

```
snapshot: services_up_at_staging ← names of runit services currently up
grub-set-default '<known_good_ref>'     # anchor: default remains the proven kernel
grub-reboot     '<candidate_ref>'       # consumed on next boot — exactly one trial
state ← STAGED; staged_boot_id ← /proc/sys/kernel/random/boot_id
banner: "reboot when convenient"        # NEVER auto-reboot
```

The failure geometry: if the candidate panics or hangs, the user power-cycles; the one-shot is already consumed, so GRUB returns to `known_good` with zero interaction. Promotion — making the candidate the default — happens only in userspace of a *healthy candidate boot* (§8.7).

**Mode split (normative).** "Manual" covers two different safety classes and MUST NOT be conflated:

- `manual` (safe): grubenv-hostile filesystem (btrfs/zfs/LVM) **with** `GRUB_DEFAULT=saved`. GRUB *reads* grubenv fine at boot — it only cannot consume a one-shot — so pinning the known-good default works. Staging proceeds minus `grub-reboot`; the user selects the candidate in the GRUB menu, and fallback is selecting the old entry — exactly §2.5's behavior. An undeterminable filesystem (e.g. `findmnt` unavailable) degrades here, never to oneshot.
- `manual-unsafe`: `GRUB_DEFAULT≠saved`. Pinning is a silent no-op; staging refuses (exit 70) per the preflight bullet above.
- `external`: **a foreign bootloader owns boot** — `/boot/grub/grub.cfg` is absent on a real (non-WSL) machine, e.g. another distro's GRUB chain-boots Void through the evergreen `/boot` symlinks in a multi-boot setup. Kernels boot fine here; only menu control is impossible. Staging is **bookkeeping-only**: record the candidate, `staged_boot_id`, and the services snapshot (state → STAGED) and issue **zero** bootloader commands. The §8.7 confirm battery and promotion run identically — promotion advances `ported_version`/`known_good` without any `grub-set-default` — and both fallback (unhealthy candidate) and watchdog response are **manual**: the operator selects the known-good entry in the foreign menu. The watchdog therefore never fires an *active* rollback in this mode; a trip records CANDIDATE_UNHEALTHY and instructs the operator. *(Added after the first real-hardware kernel run: a Debian-owned-GRUB host was lumped into `skip`, so a healthy candidate boot was never promoted.)*
- `skip`: no bootable kernel path at all (WSL2/containers). No staging, no confirm — telemetry only.

The confirm service works identically in oneshot and manual modes. Staging's privileged commands (`grub-set-default`, `grub-reboot`, `grub-editenv`) are issued through the §4 sudoers grants.

### 8.7 The confirm service (runit-native)

The service is named **`cachy-health`** (`system/sv/cachy-health/run`); it is the post-boot validation daemon (`engine/health_daemon.py`), driven under runit. It subsumes the earlier `cachy-kernel-confirm` name. **Entrypoint order (normative):** the runit-driven process runs the confirm layer **once, first** (deciding any staged candidate's fate for this boot), *then* enters the continuous watchdog loop — the confirm layer must never be reachable only through an optional flag. It has two layers:

- **Confirm layer (one-shot, normative §8.7):** exactly the `kernel-confirm` logic below — run once per boot (guarded by a `boot_id` sentinel), decide PROMOTE / CANDIDATE_UNHEALTHY / ROLLED_BACK. Rollback here is **passive**: during the trial boot the GRUB default is *already* the known-good kernel (§8.6), so leaving it untouched is the rollback.
- **Watchdog layer (continuous, operational extension):** after a candidate has been PROMOTED — when the default has *become* the candidate — the daemon keeps sampling the H1–H5 battery on short telemetry intervals, writing each result to the state store's `health` field. If the battery fails **`kernel.trip_after` (default 3) consecutive** intervals it fires an **active** rollback (`cmd_rollback` → re-pin default to known-good), since here there is no armed one-shot to fall back on. This is the only place active rollback is warranted.

**Dual-mode degradation (normative):** if the daemon detects a virtualized/WSL or bootloader-less workspace (`grub.detect_boot_layout` ⇒ `MODE_SKIP`, or `is_wsl()`), it logs battery metrics to stdout and **exits 0 without any supervisor or GRUB mutation** — no rollback, no state pinning. The health infrastructure is inert-safe in the sandbox.

The runit `run` script drops privileges to the updater user via `chpst -u` and sources its `conf`; the daemon reaches root only through the §4 sudoers grants when it must stage/rollback.

`kernel-confirm` logic (the confirm layer, run once per boot):

```
s ← read kernel-state
if s.state ∉ {STAGED, CONFIRMING}: exit 0                     # nothing in flight
if uname -r == s.candidate.kver:
    s.state ← CONFIRMING
    wait until uptime ≥ kernel.promote_after_s (default 180)
    battery — each check retried until kernel.settle_s (default 120) elapses:
      H1  every service in services_up_at_staging is up now (sv status /var/service/*)
      H2  dmesg --level=emerg,alert,crit is empty
      H3  a /dev/dri/renderD* node exists                     # it is a gaming box
      H4  a default route exists (ip route show default)      # [kernel] require_network
      H5  every /etc/cachy-void/health.d/*.sh exits 0         # operator extensions
    all pass → grub-set-default '<candidate_ref>'; known_good ← candidate;
               ported_version ← candidate's upstream version; state ← TRACKING (promoted);
               banner may SUGGEST `vkpurge rm` of kernels older than N−1 — never executes it
    any fail → state ← CANDIDATE_UNHEALTHY; GRUB default untouched, so the next
               reboot returns to known_good automatically; write banner file
elif boot_id ≠ s.staged_boot_id:      # a reboot happened, but not into the candidate
    state ← ROLLED_BACK               # panic, hang, or operator chose another entry —
                                      # alert; keep candidate installed for forensics
```

### 8.8 Transition table (normative — any transition not listed is a bug)

| State | Event / guard | Action | Next |
|---|---|---|---|
| TRACKING | upstream ver > ported, same series | — | BUMP_PATCHLEVEL |
| TRACKING | tracked series template gone | alert | AWAIT_HUMAN_SERIES |
| BUMP_PATCHLEVEL | — | §8.3 sourcing | PATCH_VERIFY |
| PATCH_VERIFY | lockfile hash match (reuse or fetch) | stage patch | REGENERATE |
| PATCH_VERIFY | fetched hash ≠ lockfile | freeze kernel path | HALT_HASH_MISMATCH |
| REGENERATE | ASSERT A–C pass | commit template | GATES |
| REGENERATE | any assert fails | keep previous template | AWAIT_HUMAN_TEMPLATE |
| GATES | G1 fails | revert template; §8.3(3) diff | AWAIT_HUMAN_PATCH |
| GATES | G2 fails | revert template | AWAIT_HUMAN_TEMPLATE |
| GATES | G1+G2 pass | `linux-cachy` enters §7 queue | READY |
| READY | DDRE built + installed candidate | §8.6 staging | STAGED |
| READY | DDRE exit 40 on `linux-cachy` | forensics per §7 | AWAIT_HUMAN_BUILD |
| STAGED | new upstream bump before reboot | clear one-shot (`grub-editenv - unset next_entry`); discard candidate | BUMP_PATCHLEVEL |
| STAGED / CONFIRMING | boot, uname == candidate, battery passes | promote; advance `ported_version` | TRACKING |
| STAGED / CONFIRMING | battery fails | no GRUB change; banner | CANDIDATE_UNHEALTHY |
| STAGED | boot, uname ≠ candidate, boot_id changed | alert | ROLLED_BACK |
| CANDIDATE_UNHEALTHY / ROLLED_BACK / AWAIT_* / HALT_* | `cachy-void-update kernel ack` after human fix | archive candidate to history | TRACKING |

Guards: at most one candidate in flight; no restaging while CONFIRMING; every `AWAIT_*`/`HALT_*` freezes only the kernel path (userspace updates continue, §8 preamble).

### 8.9 Configuration & interface additions

`updater.toml` gains:

```toml
[build]
timeout_min  = 0       # 0 = unlimited (§7.5); kernel builds are legitimately long
min_free_gib = 30      # §7.5 preflight

[kernel]
enable          = true
grub_mode       = "auto"   # auto → oneshot when grubenv is writable, else manual
promote_after_s = 180
settle_s        = 120
require_network = true     # battery H4
```

CLI verbs: `cachy-void-update kernel status | ack | approve-patch` — `approve-patch` prints the §8.3(3) diff and the exact `bore.lock` lines to change; it never edits the lockfile itself. Exit code 70 per §4.8. The daemon runs under runit as `cachy-health` and is also directly invokable as `cachy-void-update --health-daemon` (used by the service `run` script). New deliverables: `system/sv/cachy-health/`, `system/sv/zramen/run`, `updater/bore.lock`, `overlay/config/cachy-fragment.config` (§6).

---

## 9. Host Filesystem Recommendation & Pre-Deploy Snapshots

The root filesystem is chosen at Void install time, upstream of this overlay, so cachy-void does not mandate it. But the choice interacts with two flagship behaviors — the kernel one-shot rollback (§8.6) and the pre-deploy snapshot (§9.5) — so the project makes a recommendation and states one hard constraint. This section is normative for the recommendation and the snapshot step; the host FS itself remains the operator's decision.

### 9.1 Recommended layout — decided

```
ESP            /boot/efi   vfat            # firmware requirement
/boot          ext4                        # grubenv-writable — see §9.3
btrfs pool     zstd
  ├── @        /                           # snapshot target (§9.5)
  ├── @home    /home
  └── @snap    /.cachy-snapshots           # dedicated subvol, NOT nested under @
```

**Recommendation: btrfs for `/` (and `/home`); keep `/boot` on ext4 (or vfat).**

Rationale — *symmetry of safety*, not speed. cachy-void's kernel half already ships automatic rollback (§8.6: a bad kernel is un-booted by the next power cycle with zero interaction). Its userland half — the overlay rebuild and Stage 4 deploy (§7) — has no undo beyond converge-from-upstream (§5). btrfs closes that asymmetry: a read-only snapshot taken immediately before Stage 4 (§9.5) makes a broken overlay deploy a rollback, giving userland the *same safety class* the kernel already has. The whole product then tells one coherent story — *every change we make, kernel and userland, is reversible.*

Precedent: the ancestor project defaults to btrfs+zstd with snapshot-boot integration (`limine-snapper-sync`) for exactly this "reboot into a pre-update state in seconds" workflow. *(Verified against the CachyOS wiki, 2026-07-15: btrfs is the CachyOS installer default with ZSTD compression and Snapper snapshots; CachyOS switched its default to btrfs from XFS.)* cachy-void reaches the same recovery outcome the runit-native way (no systemd, no snapper timers): manual/`snooze`-driven snapshots plus grub-btrfs for snapshot boot entries.

**Honesty guardrails (normative — the recommendation MUST NOT be oversold):**

- **Not a performance recommendation.** On an SSD, ext4/btrfs/f2fs game-load times are a wash; the gaming wins live entirely in §1–§3 (compiler profile, scheduler, sysctl). btrfs is recommended for *recovery* and must be framed as such in all user-facing docs. zstd transparent compression can trim I/O on slower SATA SSDs, but it costs CPU, and that cost *compounds with the zstd zram of §3.2* on the pre-Haswell hosts this project explicitly supports (§1.2 v2 floor). Recommend `compress=zstd:1` on old CPUs, not CachyOS's default level 3.
- **Adoption asymmetry favors btrfs for an *upgrade* package.** An existing ext4 install can migrate in place with `btrfs-convert`, which keeps a rollback image of the original ext4 until the operator deletes it. f2fs has **no** in-place converter — adopting it means full backup + reformat + restore. For a tool that bolts onto systems people already run, that friction is decisive. (User-facing docs MUST still say "back up before converting"; `btrfs-convert` is best-effort and a live conversion of an unbacked-up rig is an unsupported path.)
- **f2fs remains a legitimate operator choice** for a deliberately disposable, reinstall-on-break lab host — leanest flash-native Void build, at the cost of the entire snapshot safety layer. cachy-void supports it (in-tree, §9.4) but does not recommend it, because "reinstall on break" contradicts the overlay's reversible-by-design philosophy.

### 9.2 Excluded: ZFS (decided)

ZFS is **not** a supported root FS for a cachy-void host, for reasons stronger than the RAM/complexity budget usually cited:

- **Out-of-tree module vs. a custom kernel.** cachy-void's entire purpose is compiling bespoke `linux-cachy` kernels (§2, §8). ZFS is an out-of-tree module that must be rebuilt against *every* such kernel or the pool will not import — a boot-critical failure surface placed directly across the project's main activity. The host already carries one out-of-tree module (nvidia, non-boot-critical under PRIME offload); a second one holding root is how a kernel bump yields an unbootable machine.
- **grubenv-hostile (§8.6).** ZFS is on the one-shot's forbidden-filesystem list, so it also forfeits the kernel-rollback feature this section is built around.

### 9.3 The hard constraint — grubenv writability (cross-ref §8.6)

Whatever the root FS, **`/boot/grub` MUST be grubenv-writable (ext2/3/4 or vfat)** or the kernel one-shot degrades to `manual` mode (§8.6): still safe — the known-good default is pinned and the operator selects the candidate in the GRUB menu — but no longer a zero-interaction power-cycle. Consequences:

- **Recommended layout (separate ext4 `/boot`)** → `oneshot` preserved. Best experience.
- **Whole-disk btrfs including `/boot/grub`** → *supported but degraded* to `manual`. Never below `manual` (never `manual-unsafe`) as long as `GRUB_DEFAULT=saved` holds (established once by `deploy.sh --with-grub`, §8.6). This is the price of the simpler single-filesystem layout; document it, do not forbid it.

### 9.4 In-tree + kernel-config constraints (normative)

- The root FS driver MUST be a mainline **in-tree** filesystem (btrfs, f2fs, ext4, xfs). This re-confirms §9.2's ZFS exclusion at the config level: the overlay cannot ship a rollback story that depends on an out-of-tree module surviving every custom-kernel build.
- `linux-cachy`'s config (§2.4) MUST keep `CONFIG_BTRFS_FS` enabled whenever btrfs is the root or snapshot FS. Void's dracut pulls the module into the initramfs for a btrfs root automatically, so `=m` suffices; a future minimal-config pass that compiles it out entirely yields a kernel that **builds green then cannot mount root**. The root FS driver is sacred — if a root-FS-symbol assertion is added to the §2.4 fragment, the **G2 gate (§8.5)** is its natural guardian (same mechanism that already catches silent `oldconfig` drops).

### 9.5 Pre-deploy snapshot (new updater step)

Optional, btrfs-only, opt-in. Grants the Stage 4 userland deploy the rollback the kernel already has. Consistent with the §7.6 doctrine, the snapshot is a **witness/rollback artifact, never control flow** — the updater never *reads* it to decide anything.

Config additions to `updater.toml` (extends §4.1 / §8.9):

```toml
[snapshot]
enable = "auto"              # "auto" → active iff the deploy subvol is btrfs;
                             #   true forces (errors if not btrfs); false disables
subvol = "/"                 # subvolume snapshotted before deploy
dir    = "/.cachy-snapshots" # a DEDICATED subvol, not nested under `subvol`
keep   = 5                   # retain the last N pre-deploy snapshots
```

Behavior — runs inside Stage 4, **immediately before** the `xbps-install -Suy` of §7.7, and only when `Q_deploy ≠ ∅`:

1. Resolve `findmnt -no FSTYPE --target <subvol>`. Not btrfs and `enable=="auto"` → **skip with a logged notice, never fail**. Not btrfs and `enable==true` → **exit 53** (the operator asked for a net that cannot exist).
2. `sudo btrfs subvolume snapshot -r <subvol> <dir>/deploy-<run_id>` — read-only, keyed by the §7.6 `run_id` so the snapshot ties back to the journal.
3. Prune oldest to `keep`.
4. Snapshot command failure with snapshots enabled ⇒ **abort before any mutation (I4)**, **exit 54** — never deploy without the requested net.

The snapshot is deliberately **not** taken on the kernel path: the kernel has its own one-shot (§8.6), and the kernel image lives under `/boot` (outside the `/` subvol) anyway, so double-covering it is both redundant and ineffective.

Privilege (extends the §4 sudoers boundary): adds exactly `btrfs subvolume snapshot -r *`, `btrfs subvolume delete *` (prune), and `btrfs subvolume list *` (prune enumeration) to `/etc/sudoers.d/cachy-void` — the minimal set, nothing broader.

New exit codes (extends §4.8 / §7.8):

| Exit | Condition | System state afterwards |
|---|---|---|
| 53 | `[snapshot] enable=true` but deploy subvol is not btrfs | untouched |
| 54 | pre-deploy snapshot command failed | untouched (aborted before deploy) |

Deliverables (extends §6): the `[snapshot]` block in `updater.toml`; the three btrfs grants in `system/sudoers.d/cachy-void`; the snapshot logic in `engine/` (naturally `journal.py`-adjacent, but it writes no control state). grub-btrfs is an optional operator install, not a cachy-void dependency.
