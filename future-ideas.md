# Roadmap: GUI, Plasma Theme, and Smart Detection

This document summarizes the concepts and features planned for our Cachy-fied
Void Linux installer/updater in the next phase of development.

> Status: **ideas / not yet implemented.** Nothing here is built. It captures
> intent for a future run; the authoritative design for what *is* implemented
> remains [`architecture.md`](architecture.md).

---

## 1. KDE Plasma as the Locked-In Gaming Target

We have decided to target all graphical optimizations and system integrations
specifically toward **KDE Plasma (Wayland)**. This ensures:

- Full utilization of **NVIDIA Explicit Sync** on the GTX 750 Ti graphics card.
- Perfect **fractional scaling** on the living room TV.
- Smooth **controller navigation** and a console-like experience when launching
  Steam Big Picture.

---

## 2. Smart Environment Detection (DE Check)

To respect Void's philosophy of minimal intervention (never forcing unnecessary
configurations on a user), we will build an intelligent detection scanner into
our Python program.

**How it works:** the program scans the system for the presence of KDE Plasma
files (e.g. checking whether `/usr/bin/plasmashell` or `kwin` exists).

- **If Plasma IS installed:** the user is prompted at the end of installation to
  inject our custom hybrid Void/Cachy theme — SDDM login screen, terminal
  configuration, hybrid color palette, and desktop shortcuts.
- **If Plasma is NOT installed:**
  - The program cleanly hides all Plasma-related theming options.
  - Instead, it offers a lightweight fallback: place a sharp, high-quality static
    hybrid-themed wallpaper in `/usr/share/backgrounds/` and leave the user's
    existing minimalist environment (e.g. LXQt or i3) completely untouched.

---

## 3. Graphical Installer GUI

We will design a dedicated, visually polished frontend for the installer that
bridges the design identities of both distributions.

**Aesthetics:** a sleek, deep carbon-gray background (representing Void Linux)
highlighted by glowing neon-green (Void) and vibrant electric cyan/teal accents
(CachyOS).

**Typography:** a sharp, modern sans-serif font for buttons and settings,
combined with a clean, highly legible monospace font for live compilation logs
and terminal outputs.

**Key features:**

- Interactive checkboxes to toggle optional system components (Mesa, Pipewire,
  pro-audio setups, etc.).
- A real-time, accurate compilation progress bar.
- A clear visual dashboard showing the active boot status (`kernel-state.json`),
  displaying the currently active "known good" kernel and any staged one-shot
  test kernels.

---

## 4. Development and Testing Workflow

We will continue using the **WSL Void environment** on the laptop as our primary
safe sandbox:

- All GUI components, detection scripts, and JSON parser updates will be built
  and thoroughly debugged in WSL first, to eliminate any risk to physical
  partitions.
- Once the code passes all tests and is 100% stable, we will push the changes to
  GitHub, pull them down to the **Asus GR8 mini-PC** in the living room, and
  deploy the physical kernel compilation and system adjustments directly to bare
  metal.

---

## 5. Two Deployment Routes for the First Real Target (LXQt laptop)

Defined 2026-07-06 after surveying the first bare-metal Void+DE install: a
dual-boot laptop — Ivy Bridge i3 (**x86-64-v2 ceiling**, no AVX2), GT 730M on
the `nvidia470` legacy driver, LXQt on X11, and Void booted by *the other
distro's* GRUB through a manual `vmlinuz-current` symlink (Void has no
bootloader of its own).

### Route A — the testing route (Cachy-Void is the subject)

Use the box as the real-hardware testbed for everything WSL cannot exercise.
Success = the engine survives contact with reality; findings feed fixes.

- `deploy.sh` on a real runit PID 1: `zramen` and `cachy-health` actually
  supervised, sudoers boundary in real use.
- The updater end-to-end against a live pkgdb: queue algebra with real
  subpackages, the O-term takeover, `xcheckrestart` service cycling.
- The first real kernel circuit: synthesize `linux-cachy` from `base_series
  6.12`, G1/G2 gates on a real configure, an overnight `--march x86-64-v2`
  build on the slow 2c/4t CPU — and the high-value question: does the
  **nvidia470 DKMS module build against a BORE-patched kernel**? (That answer
  transfers directly to the GTX 750 Ti box — same 470 driver family.)
- Boot topology: validates the foreign-GRUB `MODE_SKIP` degradation for real.
  §8.6 one-shot staging and the §8.7 confirm verdicts remain **untestable on
  this box** unless a later, deliberate opt-in phase gives Void its own GRUB.
- Expectation management: this route validates *mechanics*, not headline
  performance — v2 + `-O3` on Ivy Bridge yields modest compile-level gains.

### Route B — the optimized route (the box is the subject)

Adopt Cachy-Void's ideas so the LXQt install is game-optimized in its own
right. Only proven pieces graduate here.

- Immediate wins, low risk: `zramen` zstd swap (the single biggest QoL item on
  7.6 GiB RAM), the gaming sysctl profile, per-medium I/O scheduler rules.
- A **v2-rebuilt** userland allowlist trimmed to what this CPU can compile in
  reasonable time: `gamemode`, `SDL2`, `mangohud` first; `mesa` overnight;
  `wine` last (or never — upstream binaries remain the fallback by design).
- `linux-cachy` 6.12 (BORE + 1000 Hz + full preemption) — old 2c/4t hardware
  feels scheduler-latency wins the most. Boot stays manual via the symlink
  flip, which doubles as a trivially safe fallback.
- Optimus polish: PRIME render offload on 470, consolidate audio to
  pipewire(-pulse), and leave LXQt itself untouched — this box exercises §2's
  **non-Plasma fallback path** (wallpaper only) exactly as designed.

### Sequencing

Shared trunk first (`deploy.sh --march x86-64-v2` + zram + sysctl), then the
routes diverge. Route A findings gate what Route B adopts: a kernel that
proves itself in testing graduates to the daily driver.

### Ledger discipline (agreed 2026-07-13)

Every change to the box is recorded in deploy.sh's tagged ledger so it can be
rolled back **individually**, per **route**, or wholesale — live, or offline
from the dual-boot Debian over SSH via `deploy.sh --root /mnt/void …` even if
Void no longer boots (INSTALL.md §9/§11). Tags: `core` = shared trunk,
`test` = Route A experiment-only, `opt` = Route B keepers. **Route A exits with
`--uninstall-tag test` plus the §9 teardown checklist** (revert overlay
packages, remove build litter) — the box ends with only benefits: no scars,
no deadweight, no litter.
