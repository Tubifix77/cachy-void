# Roadmap: GUI, Plasma Theme, and Smart Detection

This document summarizes the concepts and features planned for our Cachy-fied
Void Linux installer/updater in the next phase of development.

> Status: **mixed — read the per-item notes, not the title.** This started as a
> pure idea list, but a good part of it has since shipped (§6 already tracks
> that, and §3 is now largely delivered *as the updater window* rather than as an
> installer). Some of it has also been deliberately **superseded** or **closed**:
> §3's neon/cyan aesthetic lost to the canonical palette in
> [`branding.md`](branding.md) §2, and §7/§8 record decisions that must not be
> relitigated. The authoritative design for what *is* implemented remains
> [`architecture.md`](architecture.md).

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

**Status (2026-08-18) — mostly delivered, but as the UPDATER window, not an
installer.** `cachy-updater-gui` is now the project's main visible program
(README "The updater window"; `system/bin/cachy-updater-gui`), so this section is
no longer a plan:

- **Delivered:** the monospace live-log pane (Output, tail-following); the
  kernel/boot dashboard — pin state, known-good vs running, and a rollback
  control that appears only when there is something to roll back to; a
  themed identity applied by the app itself rather than inherited.
- **Superseded:** the aesthetic. "Glowing neon-green + electric cyan/teal" lost
  to [`branding.md`](branding.md) §2 — obsidian/graphite with the single Void
  green, and an explicit *resist adding hues* rule. Cyan never arrived; the only
  hue added since is `--warn` (a muted brass amber, added through the process
  §2 prescribes) for the "kernel updates paused" notice.
- **Deliberately done differently:** the "real-time, accurate compilation
  progress bar". A percentage for an `xbps-src` build (let alone an 18-hour
  kernel compile) is not knowable, and a bar that lies is worse than none — so
  the window ships an indeterminate pulse + an elapsed-time heartbeat + the
  current stage in words. Do not re-propose a percentage.
- **Still open:** a *staged candidate* readout (the status pane shows the pin,
  the known-good and the drift, but not "candidate X awaiting its trial boot");
  and the interactive component checkboxes, which today are `deploy.sh` flags.
- **Reframed:** an installer GUI itself looks unnecessary now — `get.sh` reduced
  installation to one pasted line, which is a better fit for a tool that lands on
  an existing Void than a graphical wizard would be.

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

---

## 6. Requested ideas (2026-07-16) — mapped to current status

A list raised for consideration, cross-referenced against what already exists —
because most of it is already built:

**Already done / already is `cachy-void-update`:**

- **zram** — DONE (architecture.md §3.2, `zramen` runit service; live on the
  Medion: `zram0`, `vm.swappiness=100`).
- **Everyday Void updater that preserves BORE and only recompiles when a new
  version actually exists** — this *is* `cachy-void-update`. BORE can't be
  clobbered: `linux-cachy` has a unique pkgname + `-cachy` release suffix and the
  overlay is additive (I1), so upstream `linux6.12` updates never touch it. The
  "compile only when a real new version exists" is the §8.2 bump detector →
  §8.4 regen → build; an empty queue makes `--commit` a fast plain `-Su`.
  (Kernel-synthesis path HW-proven 2026-07-16, rc=0.)
- **Restart services after a Void update** — DONE 2026-07-16 (§4.7 Stage 4c:
  `xcheckrestart` → `sv restart` for non-`restart_skip` services). Void
  deliberately does NOT auto-restart services after xbps updates; this closes
  that gap and the sshd-cutoff (finding #3). Built + unit-tested; pending a
  hardware dogfood (a userspace takeover) to exercise it live.

**Genuinely new (worth adding):**

- **Simple branding theme** — a light, OPTIONAL visual identity: an accent
  colour set, a wallpaper, and a menu icon/logo. Must stay DE-aware (ties into
  §2 smart-DE detection) and opt-in (never override a user's own theme). Caveat:
  "main menu" is ambiguous — a *GRUB* theme is awkward on the test box because it
  boots via the other distro's GRUB (a hand-added `40_custom` entry), so the safe
  target is desktop wallpaper / SDDM / DE-menu icon, not GRUB. **→ now expanded
  into [`branding.md`](branding.md)** (palette, a low-key/matte/restrained
  direction — "grunge" was only an energy analogy, NOT a literal look — and a
  curated per-component theming plan: Kvantum/Rofi/Picom/Plank/Conky/wallpaper).
- **Gaming userspace layer (non-BORE runtime optimisations)** — `gamemode`
  (Feral: performance CPU governor + GPU perf + nice/ionice while a game runs) is
  already in the allowlist but not installed/enabled; make it a real component:
  install + enable + a launch wrapper composed with the existing `prime-run`
  offloader (e.g. `gamemoderun prime-run %command%`). Add companions: `mangohud`
  (perf overlay) and optionally `gamescope` (heavier — Valve microcompositor for
  scaling / frame-limiting). This is the runtime/userspace slice of the gaming
  overlay, complementing BORE (scheduler) and zram (memory). Candidate for a new
  architecture.md §3.4.

**Possible refinement:** a lighter "daily" updater mode/alias that runs the `-Su`
+ §4.7 service-cycle and *prompts* before any long compile, instead of doing the
whole build/deploy in one `--commit`.

### 6b. From the 2026-07-16 aesthetic / "steal from CachyOS" discussion

- **btrfs conversion** — the requested 6th item; tracked/executing separately
  (staged `/home/boas/void-convert.sh`), not a roadmap idea.
- **Proton-CachyOS-GE / GE-Proton** — NEW and the easiest real win: drop the
  prebuilt Proton into `~/.steam/root/compatibilitytools.d/` and select it per
  title in Steam. Pure userspace, no compile, no system change, trivially
  reversible — the most Void-friendly way to grab CachyOS's gaming sauce. Pairs
  with the gaming-userspace layer (§6 above).
- **Concrete branding palette** ("Industrial Cockpit", Void-logo adjacent): bg
  `#1b1d1e`, fg `#abb2bf`, accent `#478061` (desaturated forest green), alert
  `#8a2f32`; flat / no glass, JetBrains Mono or IBM Plex Mono, one thin panel,
  `btop` as the "telemetry" widget, structural/dark wallpaper, Void-logo menu
  icon (no "Start" button).
- **Audited the chat's "CachyOS tweaks" against our config — almost all already
  present**, often done more correctly: per-medium I/O schedulers (§3.3),
  `kernel.split_lock_mitigate=0` (§3.1), BBR (§3.1 + built into linux-cachy),
  `-march` userland ladder, BORE. THP: we set **ALWAYS** (§2.4) whereas the chat
  suggested `madvise` — a real, debatable divergence worth revisiting for a
  latency-sensitive gaming profile.
- **Rejected (unchanged):** `mitigations=off` (invariant I7) and `linux-tkg`
  (we fork Void's own kernel template, not tkg/XanMod; BORE already works).
- **Hardware caveat:** the shared mockup shows an AMD RX 6800XT + implies
  Wayland/Sway. The real box is Intel Ivy Bridge (v2) + NVIDIA GT 730M on
  nvidia470 (Kepler) → X11/**Openbox** (already in the allowlist), not
  Sway/Wayland (no viable Wayland on 470 legacy; Explicit Sync needs 555+).
  gamescope is also dubious on the 470 legacy driver.

### 6c. Extractions from geminichat.md (genesis brainstorm), audited 2026-07-16

Read the full 4,591-line genesis transcript; ~everything in it is already
realized in the spec/updater. Net-new worth keeping:

- **Make Void own GRUB (efibootmgr + os-prober)** — geminichat.md L4326-4391 has
  a concrete no-reinstall procedure: `efibootmgr -o <void>,<rest>` to reorder the
  UEFI boot order, then `xbps-install os-prober` + `grub-mkconfig` so Void's GRUB
  absorbs the other OSes. This is the **missing precondition for §8.6 one-shot
  staging** (Void needs its own grubenv-writable GRUB with `GRUB_DEFAULT=saved`)
  — i.e. exactly why staging SKIPPED on the Medion's foreign Debian GRUB. TODO:
  write it up as an INSTALL.md "multi-boot: give Void the bootloader" *opt-in*
  section. Deliberately NOT done on the Medion — Debian-owns-GRUB is the escape
  hatch there.
- **Alternative global allocator (jemalloc/scudo via LD_PRELOAD)** — L23-26, a
  real CachyOS lever raised but never decided. Leaning REJECT for the base
  overlay (LD_PRELOAD-ing a global allocator is invasive / un-Void); at most an
  opt-in per-game launch tweak. Decide explicitly rather than leave it dangling.
- **TUI installer + three-tier menu** — L4477 / L4534-4542: a lightweight TUI
  (not only the heavy GUI in §3) offering Core-only / Plasma+theme / **Hyprland**
  (minimalist Wayland). Folds into the GUI roadmap item. (Hyprland/Wayland is for
  newer GPUs, not the 470-legacy testbed.)

Audit also CONFIRMED stale: §1's KDE-Plasma/Wayland/Explicit-Sync lock-in was
argued from GTX-750-Ti/Maxwell assumptions that don't hold for the nvidia470/
Kepler reality — keep the DE detected/optional, never locked (see §6b caveat).
The transcript still contains, in plain text, ideas we deliberately RETIRED
(linux-tkg/XanMod, `xbps-pkgdb repolock/repounlock`, remote-fetched bore.lock +
minisign, the musl-breaks-games scare) — do not mine those back out of it.

**geminichat2.md (audited 2026-07-16):** essentially nothing new — L1-43 retreads
lightweight-DE gaming ground already covered (§2/§6b/§6c); L45-128 is an
off-topic Windows-11 Discord troubleshooting tangent, irrelevant. ONE keeper:
- **INSTALL.md prerequisite gap** (geminichat2.md L23-27): enabling Void's
  **multilib** repo + installing **32-bit GL/Vulkan driver libs** (e.g.
  `mesa-dri-32bit`, or the matching 32-bit NVIDIA libs) is a hard **Steam/Proton
  launch prerequisite** and is absent from INSTALL.md's prereqs (§1 currently
  lists only git/xtools/base-devel). This is the *install* side; complements
  invariant I6, which governs the *build* side (never cross-build i686 with
  `x86-64-v*`). TODO: add to INSTALL.md after verifying the exact Void package
  names for the target GPU (nvidia470 legacy → its own 32-bit libs).

**chatgptchat.md (audited 2026-07-16):** a Danish multi-model (ChatGPT/Gemini/Grok)
critique of a Gemini "Void LXQt gaming install guide," framed for a modern RTX 3080.
It is entirely BASE-SYSTEM gaming-desktop setup (repos, X11/LXQt/SDDM, NVIDIA +
Vulkan + 32-bit, PipeWire, Steam) — it never touches the Cachy-Void overlay, so its
home is an INSTALL.md "gaming-desktop prerequisites" appendix, not architecture.md.
Heavy overlap with geminichat2. No invariant conflicts; it actively CORROBORATES
LXQt/X11 for NVIDIA gaming (further undercutting the §1 Plasma lock-in). Keepers:
- **Modern-NVIDIA KMS tweak:** `options nvidia-drm modeset=1 fbdev=1` in
  `/etc/modprobe.d`. We ALREADY run `modeset=1` on the Medion (the fix walked home at
  the start of this session, verified `modeset=Y`); `fbdev=1` is the new bit and is
  MODERN-driver-only — scope it away from the nvidia470 legacy testbed.
- **PipeWire has NO runit service on Void** (starts as a user-session/DBus service).
  Guard: never add a `pipewire` runit service to this project.
- **Package/repo names — VERIFY on real Void, do NOT trust the chats.** The thread is
  three LLMs disagreeing over names — the exact failure mode behind the
  [[spec-bug-game-devices-udev]] bug. To confirm with `xbps-query -Rs` on the box
  before anything enters INSTALL.md: repos `void-repo-multilib` / `-nonfree` /
  `-multilib-nonfree`; 32-bit libs — the chat "corrected" `mesa-dri-32bit` →
  `mesa-32bit-dri`, but Void's `<name>-32bit` convention actually favours
  `mesa-dri-32bit`, so trust NEITHER until checked; plus `nvidia-libs-32bit`,
  `vulkan-loader-32bit`.

## 7. The maintenance test — selection rule for any future addition (agreed 2026-08-15)

Ratified while choosing the gaming-completion set (earlyoom, gamescope, vkBasalt,
fastfetch — all stock Void packages):

**An addition must be upstream-maintained. If keeping it working falls on US — our
own srcpkg, our own fork, our own rules file to curate — it is DISQUALIFIED, no
matter how well it fits the philosophy, UNLESS it *is* core CachyOS substance.**

`linux-cachy`/BORE is the one sanctioned exception: maintaining that fork is the
project's reason to exist, not a side burden. Everything else must be one
`xbps-install` away, with Void (or the package's upstream) carrying the updates.

Worked example — **ananicy-cpp**: the largest genuinely missing piece of CachyOS
substance (automatic nice/ioprio rules so games get priority; CachyOS ships it by
default with community rules). It is NOT in Void's repos, so adopting it means
maintaining a srcpkg *and* a rules file forever → **disqualified by this rule**,
not by philosophy fit. **Verdict: ruled out permanently — this is a closed
decision, not a parked idea.** The ONLY event that reopens it is Void packaging
it upstream (the disqualifier disappearing, not our appetite changing); until
`xbps-query -Rs ananicy` returns a hit, do not re-propose it, re-argue it, or
"just quickly" fork it. Same permanent disposition: sched-ext/scx userspace
(no Void packages + BORE is the identity).

Rationale: this is a solo hobby overlay that deliberately rides upstream Void so
it "doesn't rot" (the same reasoning that rejected the standalone-distro/binary-
repo path). Every self-maintained component is a standing bill against that
design; the kernel is the only bill worth paying.

---

## 8. Delivered / closed on 2026-08-18 (the updater overhaul)

Recorded here so none of it gets re-proposed as an "idea" later.

**Delivered:**

- **One-line install** — `get.sh` (xbps-fetch or `curl | sh`) clones and hands off
  to `bootstrap.sh`; flags ride through to `deploy.sh`. This closes the
  "installation is fiddly" complaint *without* a binary repo or an ISO — both of
  which stay rejected (see §7's rationale: the overlay rides upstream so it
  doesn't rot).
- **Assisted BORE pin** (architecture.md §8.3a) — the trust anchor is still
  human-owned, but the clerical half (locate the patch, hash it, edit TOML) is
  automated behind a reviewed confirmation, surfaced in the window as a notice
  card. A user can no longer fail to discover why their kernel never updates.
- **Recovery in the window** — `--rollback` was CLI-only for months; the status
  pane now emits `rollback available` and the GUI reveals a *Boot known-good
  kernel* button on it.
- **The updater window is core, not branding** — it used to install only with
  `--with-branding`, i.e. a box could have no GUI at all, which quietly
  invalidated every "the updater will tell you" claim.

**Closed decisions (do not relitigate):**

- **Auto-purging old kernels: REJECTED.** Tempting (they pile up at ~227 MB
  each), but a kernel that boots healthy today can still fail next week on a path
  not yet exercised, and rebuilding one costs hours. The agreed middle ground is
  *visibility*: every leftover is reported with size + role (rollback target /
  running / spare) + its own `vkpurge` command, and more than one spare raises a
  warning. Deletion stays a human act (§2.5/§4.7).
- **A "purge old kernels" button: REJECTED** — it would mean granting `vkpurge`
  to the updater (widening the §4.1 boundary) to save typing one command a couple
  of times a year. The suggestion text carries the command instead.
- **The updater running `grub-mkconfig`: REJECTED, and now provably
  unnecessary** — Void's own `grub` package ships
  `/etc/kernel.d/post-install/50-grub` *and* `post-remove/50-grub`, so a
  GRUB-owning Void host regenerates its config on every kernel install/removal.
  Provisioning does it once (`deploy.sh --with-grub`); a foreign bootloader is
  never touched (§8.6 `external`).

**Design rules the overhaul established** (see also memory / README):

1. Nothing that matters is CLI-only — if the engine knows it, the window says it.
2. Preview, then confirm — destructive or trust-establishing actions show the
   real list/checksum first; approving a *category* is not consent.
3. Annotate, never dump — sizes, roles and exact commands, because "which of
   these can I delete?" is where a wrong guess costs a bootable system.
