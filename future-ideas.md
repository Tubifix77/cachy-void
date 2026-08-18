# Roadmap: desktop integration, theming, and parked ideas

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

## 1. Desktop environments — agnostic by default, themed where it pays

**RETRACTED (2026-08-18): there is no "locked-in" desktop, and there never
should have been one.** This section used to declare KDE Plasma (Wayland) *the*
target, which misrepresented the whole project. The three reasons given were all
Wayland-era KWin features argued from a **GTX 750 Ti**: explicit sync (the real
box is Kepler on the **470 legacy** driver, which predates the GBM/explicit-sync
support that makes NVIDIA Wayland viable at all), fractional scaling (a 1080p TV
wants 100%), and controller navigation for Big Picture (that is *Steam's*
feature — it works from any session, even a bare WM). A desktop identity had been
imported as if it were a technical requirement, most likely because CachyOS
leads with Plasma as its flagship.

**What is actually true of the code (measured, not asserted):** the performance
layer, the gaming layer and the updater contain **zero** DE references — sysctl,
udev, zram, kernel, compiler profile, `cachy-game`, GameMode/MangoHud/vkBasalt,
earlyoom, snapshots, `cachy-void-update` and its window are all DE-agnostic
already (the window paints its own palette precisely so it does not depend on
the session's theme). `deploy.sh` mentions LXQt only in branding paths. The
entire desktop assumption lives in **one optional applier**,
`system/bin/cachy-branding`, plus the tray-applet choice in
`--with-networkmanager`.

**The intended shape — support several desktops, favour none:**

- **Tier 0 — every desktop, and none.** Everything above. Works today on any
  session, on a bare WM, or headless. This is the overlay.
- **Tier 1 — any Qt desktop.** Palette assets that need no DE integration:
  Kvantum theme, wallpaper, `qterminal` scheme, the branded SDDM greeter, the
  icon set. Applies unchanged under LXQt, Plasma, or a lone Openbox.
- **Tier 2 — per-DE integration**, one small applier each, written against that
  DE's own config mechanism and nothing else:
  - **LXQt** — built (`cachy-branding`; panel, openbox, session).
  - **KDE Plasma** — the obvious second target: it is CachyOS's flagship, it is
    Qt so every Tier-1 asset transfers, and it is what most Linux gamers run.
    Scope is modest: a `.colors` scheme, the Kvantum bridge, wallpaper, a Konsole
    colour scheme, and `plasma-nm` instead of `nm-tray`. **Skip** plank, rofi,
    picom and conky there — Plasma already owns those roles (panel, KRunner,
    KWin compositing, widgets) — which makes the Plasma applier *smaller* than
    the LXQt one, not bigger.

    **Feasibility, verified against Void's repo 2026-08-19** (Plasma 6.7.4 is
    packaged and current):

    | Package | Size | Note |
    |---|---|---|
    | `plasma-desktop` | 41 MB | the desktop |
    | `plasma-workspace` | 58 MB | ships **only** `/usr/share/wayland-sessions/plasma.desktop` |
    | `plasma-workspace-x11` | — | ships `startplasma-x11` + `xsessions/plasmax11.desktop`; pulls `kwin-x11` |
    | `plasma-nm` | 14 MB | Plasma's own network applet |
    | `konsole` | 10 MB | terminal (its scheme replaces qterminal's) |

    **The trap: Plasma 6.7 is Wayland-by-default and Void follows upstream's
    split** — plain `kwin` ships only `kwin_wayland`, and X11 lives in the
    separate `kwin-x11` + `plasma-workspace-x11` packages. So installing "Plasma"
    the obvious way can leave SDDM offering only a Wayland session; if X11 is
    wanted, `plasma-workspace-x11` must be installed **explicitly**.

    *Precision about Wayland on the testbed (correcting an over-broad claim made
    earlier in this file's own history):* the laptop is **Optimus** — an Intel
    HD 4000 iGPU alongside the GK107M. A Wayland compositor driven by the
    **Intel** side is perfectly viable (Mesa, GBM, all present); what the
    nvidia470 legacy branch cannot do is drive a Wayland session itself, and
    offloading to a 470-era GPU under Wayland is the doubtful part. So "Wayland
    is impossible here" is wrong — the accurate statement is that **X11 is the
    known-good path for a 470 offload setup**, which is why the X11 packages
    matter for testing. §1's retraction does not depend on this either way: it
    rests on explicit sync being absent from 470 regardless of session type,
    fractional scaling being unneeded on a 1080p TV, and controller navigation
    belonging to Steam.

    **Why theming Plasma is easier than LXQt was:** KDE ships official,
    scriptable apply tools — `plasma-apply-colorscheme`,
    `plasma-apply-wallpaperimage`, `plasma-apply-desktoptheme`,
    `plasma-apply-lookandfeel`, `plasma-apply-cursortheme` — so the applier can
    set and revert the look through supported interfaces instead of editing
    config files behind the DE's back.

    **Testability plan** (removes the "code-reviewed only" caveat below):
    install Plasma *alongside* LXQt on the test box — desktops coexist as
    separate SDDM session entries, nothing is replaced, and a pre-install btrfs
    snapshot makes the whole experiment reversible. Then iterate the applier over
    a few login cycles.

    **Performance is not the blocker** (owner's first-hand report, 2026-08-19):
    Plasma has already run *fine* on this exact laptop under Debian and
    Tumbleweed. **8 GB of RAM is what carries it** — KDE's cost is mostly memory
    and compositing rather than raw CPU, so a 2013 Ivy Bridge with enough RAM is
    comfortable. An earlier "expect sluggishness" note here was inference from
    the CPU's age and has been withdrawn; it was contradicted by having actually
    run the thing.
  - **XFCE** — cheap third (`xfconf` for wm theme + wallpaper), and worth noting
    the dual-boot Debian on the test laptop runs it.
  - **GTK/GNOME** — lowest priority; GNOME resists theming and ships its own
    network applet.
- **Rules, unchanged:** never install a desktop, never override a user's own
  theme (opt-in only, reversible), and an unrecognised session gets Tier 1 plus a
  plain message — never a broken half-applied look.

**The honest costs.** Each Tier-2 applier is a standing maintenance bill in the
sense of §7 — not disqualified (it is our own config code, not a forked
package), but not free either; two or three is a sane ceiling. And **we cannot
test Plasma theming on the current hardware**: no Plasma is installed anywhere
in this project's reach, so a Plasma applier would ship code-reviewed and
mock-tested only — the same honesty caveat as the Void-owned-GRUB one-shot path
(§8.6). Void packages Plasma, so installing it alongside LXQt on the test box is
the cheap way to make it testable before claiming support.

---

## 2. Smart Environment Detection (DE Check)

To respect Void's philosophy of minimal intervention (never forcing unnecessary
configurations on a user), we will build an intelligent detection scanner into
our Python program.

**How it works (generalised 2026-08-18 — it was written Plasma-first, matching
§1's retracted lock-in):** identify the session rather than one product. Read
`XDG_CURRENT_DESKTOP` first, corroborate with binaries on PATH
(`lxqt-session`, `plasmashell`/`kwin_x11`, `xfce4-session`, `gnome-shell`), and
map the result to a §1 Tier-2 applier — falling back to Tier 1 for anything
unrecognised, which is a supported outcome and not a failure.

- **If a KNOWN desktop is detected:** the user is prompted at the end of
  installation to apply the matching identity — SDDM greeter, terminal scheme,
  palette, wallpaper, plus that desktop's own integration (panel/colour scheme).
- **If Plasma specifically is detected:** prefer `plasma-nm` over `nm-tray` for
  the WiFi picker, since the tray applet must match the session's toolkit — the
  same reasoning that made nm-tray right for LXQt and nm-applet wrong.
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

---

## 9. Peer-overlay audit (2026-08-18) — seven expansion ideas

Method: diffed Cachy-Void 1:1 against six comparable projects, reading their
actual config trees rather than their feature pages — **CachyOS**
(`CachyOS-Settings`, file by file), **ALHP** (the Arch x86-64-v3/v4 rebuild),
**Garuda**, **Nobara**, **Manjaro**, **PikaOS**. Every candidate below was then
checked against Void's live package index and against the §7 maintenance test.

**Parity result first, so nobody re-runs this audit:** we are at or ahead of
CachyOS on the config layer. All 11 sysctls in their
`usr/lib/sysctl.d/70-cachyos-settings.conf` are present in ours, plus six they
don't set (`max_map_count`, `split_lock_mitigate=0`, `tcp_fastopen`, `bbr`,
`inotify.max_user_watches`, `sched_rt_runtime_us`). Their THP tmpfiles
(`defrag=defer+madvise`, `khugepaged/max_ptes_none=409`) are already applied by
`deploy.sh`. Their five perf-relevant udev rules are all ported. Their
`game-performance` is a thin `powerprofilesctl` wrapper — `cachy-game` (gamemode
+ PRIME + gamescope + vkBasalt) is strictly more. The only rules we lack are
`30-zram`, `69-hdparm`, `85-iw-regulatory` (marginal) and `71-nvidia` (mostly
covered by `99-cachy-nvidia.conf`). Everything below is what genuinely remains.

### 9.1 `grub-btrfs` — the missing half of the snapshot story

`grub-btrfs 4.14` **is in Void's repos**, shipping a `grub-btrfs-runit` package —
the daemon is already runit-wired upstream, so this is one `xbps-install` and
passes §7 outright. Today we take a read-only pre-deploy snapshot into
`/.cachy-snapshots` (§9.5) and then offer **no way to boot one**: recovery is a
live-system restore from a system that may not be up. Garuda, openSUSE and
CachyOS (limine-snapper-sync) all close this loop. We have the safety net
without the ladder out.

**Caveat that sets the sequencing:** grub-btrfs writes GRUB menu entries, so it
is only meaningful on a host where **Void owns GRUB** — it belongs bundled with
the §6c "give Void the bootloader" opt-in and the §8.6 one-shot path, not
before it. On a foreign-GRUB box (the Medion) it has nowhere to write.
`snapper 0.13.1` is also in Void if a rollback UI is ever wanted, but check its
timer assumptions against `snooze` before adopting — that could drag a
self-maintained unit in through the back door.

### 9.2 RT priority is half-wired — `limits.d/20-audio.conf`

We set `kernel.sched_rt_runtime_us = -1` (§3.1) and ported CachyOS's `hpet` /
`rtc0` / `cpu_dma_latency` group-`audio` rules into `40-rtaudio-perms.rules` —
but not their `etc/security/limits.d/20-audio.conf`:

```
@audio - rtprio 99
@audio - nice  -11
```

Without it nothing in userspace can actually **claim** the RT priority we went
to the trouble of unthrottling. PipeWire/JACK and the audio half of the gaming
profile are the beneficiaries. One file, installed by `deploy.sh` under the
existing ledger tags.

**Verify before shipping:** that Void's `/etc/pam.d/system-login` stack pulls
`pam_limits.so` (if it doesn't, the file is inert and the fix is elsewhere), and
that the `audio` group exists on a stock Void install.

### 9.3 `XBPS_LDFLAGS` is unset entirely — no LTO, no link-time flags

`system/etc/conf` is `-march=@MARCH@ -O3 -pipe` and nothing else. This is the
one thing **every** v3-rebuild peer does that we don't: ALHP has enabled LTO for
all packages built since 2021-11-04; CachyOS layers Clang ThinLTO plus
`-fno-semantic-interposition`, `-fno-plt`, `-Wl,--as-needed`, `-Wl,-O2`.

Cheapest remaining perf lever in the overlay, and it costs nothing to maintain —
it's a build flag, not a component. Needs a per-package opt-out list because LTO
breaks a handful of things, but a 10-entry allowlist is exactly the right size
to trial it on. Check first what `xbps-src` already sets by default so we extend
rather than clobber. Ordering: this is a full rebuild cycle, so it lands with a
deliberate `-Su` + §4.6 takeover pass, not sneaked into a routine update.

### 9.4 Secure Boot is undetected — and it makes our whole product not boot

`sbctl 0.18` is in Void. The core act of this project is installing a
**self-built, unsigned** kernel; with Secure Boot enabled that host does not
boot, and nothing in the preflight, the G1/G2 gates or `cachy-health` notices.
CachyOS ships `sbctl-batch-sign` for exactly this.

This does not need to become a signing feature. A read of
`/sys/firmware/efi/efivars/SecureBoot-*` in the preflight that **refuses the
kernel build with a clear message** (userspace updates unaffected, per the
existing G2 precedent) is the whole ask. Signing support is a separate, larger
decision.

### 9.5 No `.github/` — 245 tests that never run, and a `curl | sh` install

The repo has no `.github/` directory at all: no CI on push, no tags, no
releases, no CHANGELOG, no SECURITY.md, no issue templates. `CachyOS-Settings`
itself ships CONTRIBUTING.md and CODE_OF_CONDUCT.md. The test suite is the
project's strongest quality claim and it currently only ever runs when one
person remembers to run it.

Related and sharper: `get.sh` is fetched and piped to `sh` with **no checksum or
signature**. Every peer distributes through a signed repo or a signed ISO. For a
public repo that invites strangers to pipe a script into a shell, this is the
gap a stranger sees first. Minimum viable fix: publish a SHA-256 in the README
next to the command and have `get.sh` verify what it then downloads.

### 9.6 `fwupd` — the third update domain

`fwupd 2.1.7` is in Void. The updater owns xbps and (since the overhaul) flatpak;
device firmware is the remaining domain, and "update everything" is the window's
stated job. Nobara and Bazzite both surface it. Fits the §7 test trivially.

Design note: firmware updates are reboot-coupled and occasionally brick things,
so this is a **preview-then-confirm** citizen under design rule 2 — surface
`fwupdmgr get-updates` in the window, never fold it into the Update button.

### 9.7 NTSYNC — track it, pinned to the next base bump

Absent from `overlay/config/` and from `modules-load.d/cachy.conf` (which loads
only `tcp_bbr`). It is CachyOS's headline Wine/Proton win and they ship a
`modules-load.d/ntsync.conf` for it.

**Correctly premature, not an omission:** Wine's implementation wants kernel
≥6.14 and our `base_series` is 6.12. Recording it here so it is a tracked item
attached to the next base bump (§8.2 detects the bump; the fragment gains
`CONFIG_NTSYNC=y` and the module load lands with it) rather than something we
rediscover in another audit.

### 9.8 Confirmed correctly absent — closed, do not re-raise

The audit re-verified these against Void's index on 2026-08-18 and they all
returned **empty**: `ananicy-cpp`, `scx-scheds`, `uksmd`, `tuned`,
`auto-cpufreq`, `umu-launcher`, `steam-devices`. §7 therefore rules them out
exactly as already recorded — the disqualifier is still standing, so the closed
decisions stay closed. `mitigations=off` remains I7. The no-ISO / no-binary-repo
stance is the design, not an oversight: it is what makes riding upstream Void
free.

Two peer *concepts* we lack that the audit judged **taste, not gaps**: a
hardware/driver auto-detect layer (Manjaro `mhwd`, CachyOS `chwd`, Nobara Driver
Manager — the window's GPU pane already carries most of that value), and a
"welcome" app (CachyOS Hello, Garuda Assistant).

**If only two of the seven ever get built:** §9.3 (pure upside, zero maintenance
bill) and §9.5 (the gap a stranger sees first).
