# Cachy-Void — Architecture & Build Specification

**Status: AUTHORITATIVE.** This document is the sole source of truth for the Cachy-Void project. If code, configs, or other docs disagree with this file, this file wins. Last revised: 2026-07-05.

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
# Network: BBR built in and default (no module-load ordering issues).
# DEFAULT_TCP_CONG is a DERIVED string — the DEFAULT_BBR choice symbol must be
# set, or oldconfig silently reverts to cubic (G2-caught, first real kernel run)
CONFIG_TCP_CONG_BBR=y
CONFIG_DEFAULT_BBR=y
CONFIG_DEFAULT_TCP_CONG="bbr"
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
vm.max_map_count = 2147483642    # SteamOS value; required by many Proton titles
# --- Files ---
fs.file-max = 2097152
fs.inotify.max_user_watches = 524288
# --- Scheduling / kernel ---
kernel.split_lock_mitigate = 0   # split-lock stalls cost 10%+ fps in affected games
kernel.nmi_watchdog = 0
kernel.sched_rt_runtime_us = -1  # no RT throttling. Accepted risk: a runaway
                                 # SCHED_FIFO task can monopolize a core.
# --- Network ---
net.core.netdev_max_backlog = 4096
net.ipv4.tcp_fastopen = 3
net.ipv4.tcp_congestion_control = bbr
```

Apply/verify: `sudo sysctl --system`. On the stock Void kernel (fallback boots), BBR is a module — add `tcp_bbr` to `/etc/modules-load.d/cachy.conf` so the sysctl line never silently fails. On `linux-cachy` it is built in (§2.4).

### 3.2 zram (runit-native, no zram-generator)

```sh
sudo xbps-install zramen
sudo ln -s /etc/sv/zramen /var/service/
```

Configure in the service's `conf` file using the variable names the `zramen` package actually ships (verified against `zramen-1.0.1_1`): `ZRAM_COMP_ALGORITHM=zstd` (best ratio; package default `lz4`), `ZRAM_SIZE=100` — a **percent** of RAM, not a fraction (package default 25) — and, critically, `ZRAM_MAX_SIZE` raised above its 4096 MiB default so 100% is not silently capped at 4 GiB. `ZRAM_PRIORITY` sits above any disk swap (package default 32767). Disk swap partitions may coexist at lower priority but are not required. (An earlier draft used `ZRAM_ALG`/`ZRAM_PRIO`/a `1.0` fraction — all unrecognized by zramen; that is retired.)

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

---

## 4. The Automated Updater (`cachy-void-update`)

A standalone Python 3 script (stdlib only: `subprocess`, `tomllib`, `logging`). It runs as the **regular build user**; root is reached exclusively through `sudo` for the exact commands in Stage 4 (single privilege boundary, I4). A sudoers fragment (`/etc/sudoers.d/cachy-void`) grants NOPASSWD for `xbps-install`, `sv`, `xbps-pkgdb`, and the three narrow GRUB staging binaries `grub-set-default`, `grub-reboot`, `grub-editenv` (§8.6) — nothing else.

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

---

## 5. Recovery Runbook

- **Corrupted local repo index:** delete `hostdir/binpkgs/x86_64-repodata`, then re-run `xbps-rindex -a hostdir/binpkgs/*.xbps`. (Note: `xbps-pkgdb -m repolock|repounlock <pkg>` pins/unpins a *package* to its repository — it is **not** an index-repair tool. A prior draft of this document claimed otherwise; that claim is retired.)
- **Failed build (exit 40):** system untouched. Read the emitted log tail; either fix the template locally, add a flag exception, or remove the package from `targets`. Re-run.
- **Failed install (exit 51):** XBPS transactions are per-run atomic at the package level. Run `xbps-pkgdb -a` to verify pkgdb integrity, then `sudo xbps-install -Su` from upstream mirrors to converge to a consistent state. The upstream binary always exists as the fallback for every overlay package.
- **Bad kernel:** boot the previous kernel from the GRUB menu (it is always still installed, §2.5), then `vkpurge rm <bad-ver>` and rebuild. If the overlay repo itself is suspect, `sudo xbps-install -f linux<series>` from upstream restores a stock kernel.
- **Nuke the overlay entirely:** remove `/etc/xbps.d/00-cachy-overlay.conf`, then `sudo xbps-install -Suf` of the affected targets from upstream mirrors. The base system was never anything but stock Void — this always converges.

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
│   ├── sv/zramen/{run,finish,conf}      # §3.2 zram service (run: override template)
│   └── sv/cachy-health/{run,conf}       # §8.7 post-boot health daemon service
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

- `findmnt -no FSTYPE --target /boot/grub` ∈ {ext2, ext3, ext4, vfat}. GRUB cannot rewrite `grubenv` on btrfs/zfs/LVM/RAID — there, a one-shot entry is never consumed and would boot-loop into the candidate; oneshot mode is **forbidden** on those filesystems.
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

The confirm service works identically in oneshot and manual modes. Staging's privileged commands (`grub-set-default`, `grub-reboot`, `grub-editenv`) are issued through the §4 sudoers grants.

### 8.7 The confirm service (runit-native)

The service is named **`cachy-health`** (`system/sv/cachy-health/run`); it is the post-boot validation daemon (`engine/health_daemon.py`), driven under runit. It subsumes the earlier `cachy-kernel-confirm` name. It has two layers:

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
