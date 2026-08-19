# Future ideas

**Only things not yet built.** This file used to double as a changelog — every
idea listed, then annotated "DONE" — which made it useless for its actual job.
What exists is documented where it belongs: the design in
[`architecture.md`](architecture.md), the user-facing surface in
[`README.md`](README.md) and [`INSTALL.md`](INSTALL.md), the look in
[`branding.md`](branding.md), and the history in `git log`. Nothing is recorded
here twice.

Two things that are *not* ideas stay at the bottom, because they govern which
ideas are worth having: the **selection rule** (§6) and the **closed decisions**
(§7). Check both before proposing anything.

---

## 1. Desktop support beyond LXQt

The overlay itself is desktop-agnostic — only the optional look is not (see
README's Highlights table). Adding a desktop means adding an *applier*, nothing
else.

- **Tier 1 — any Qt desktop:** assets that need no integration (Kvantum theme,
  wallpaper, terminal scheme, SDDM greeter, icon set). Already applies unchanged
  under LXQt, Plasma or a lone Openbox.
- **Tier 2 — one small applier per desktop**, written against that desktop's own
  config mechanism and nothing else. LXQt exists (`cachy-branding`).

**Next target: KDE Plasma.** CachyOS's flagship, Qt (so every Tier-1 asset
transfers), and what most Linux gamers run. Scope: a `.colors` scheme, the
Kvantum bridge, wallpaper, a Konsole colour scheme, and `plasma-nm` instead of
`nm-tray`. *Skip* plank, rofi, picom and conky — Plasma owns those roles already
(panel, KRunner, KWin compositing, widgets), so the applier is **smaller** than
LXQt's.

Verified against Void's repo (2026-08-19) — Plasma 6.7.4, current:

| Package | Size | Note |
|---|---|---|
| `plasma-desktop` | 41 MB | the desktop |
| `plasma-workspace` | 58 MB | ships **only** `wayland-sessions/plasma.desktop` |
| `plasma-workspace-x11` | — | `startplasma-x11` + `xsessions/plasmax11.desktop`; pulls `kwin-x11` |
| `plasma-nm` | 14 MB | Plasma's own network applet |
| `konsole` | 10 MB | terminal (its scheme replaces qterminal's) |

- **The trap:** Plasma 6.7 is Wayland-by-default and Void follows upstream's
  split — plain `kwin` ships only `kwin_wayland`. Installing "Plasma" the obvious
  way can leave SDDM offering only a Wayland session, so **`plasma-workspace-x11`
  must be installed explicitly** where X11 is wanted. On the Optimus testbed X11
  is the known-good path for a 470-offload setup (a Wayland compositor driven by
  the *Intel* side is fine; 470 driving Wayland itself is not).
- **Theming Plasma is easier than LXQt was:** KDE ships scriptable apply tools —
  `plasma-apply-colorscheme`, `-wallpaperimage`, `-desktoptheme`,
  `-lookandfeel`, `-cursortheme` — so an applier can set *and revert* through
  supported interfaces instead of editing config behind the desktop's back.
- **Making it testable:** install Plasma *alongside* LXQt on the test box —
  desktops coexist as separate SDDM sessions, nothing is replaced, and a
  pre-install btrfs snapshot makes it reversible. Performance is not a concern:
  Plasma has already run fine on that laptop under Debian and Tumbleweed (8 GB
  of RAM carries it). Without this, a Plasma applier could only ship
  code-reviewed — the same caveat as the Void-owned-GRUB one-shot path.
- **Open question before claiming support:** do `plasma-nm` and `nm-tray`
  coexist cleanly when both desktops are installed? Two applets fighting over one
  tray is a known-shape bug here.

**After that:** XFCE is cheap (`xfconf` for wm theme + wallpaper, and the
dual-boot Debian on the test laptop runs it). GTK/GNOME is lowest priority — it
resists theming and ships its own network applet.

**Rules for any of them:** never install a desktop, never override a user's own
theme (opt-in, reversible), and an unrecognised session gets Tier 1 plus a plain
message — never a half-applied look. Each Tier-2 applier is a standing
maintenance bill (our config code, so not disqualified by §6, but not free
either): two or three is a sane ceiling.

---

## 2. Desktop detection

Identify the session rather than one product: read `XDG_CURRENT_DESKTOP` first,
corroborate with binaries on PATH (`lxqt-session`, `plasmashell`/`kwin_x11`,
`xfce4-session`, `gnome-shell`), map to a §1 Tier-2 applier, and fall back to
Tier 1 for anything unrecognised — a supported outcome, not a failure. Where a
desktop ships its own network applet (Plasma → `plasma-nm`), prefer it over
`nm-tray`: the applet has to match the session's toolkit.

---

## 3. Updater window — remaining gaps

- **Staged-candidate readout.** The status pane shows the BORE pin, the
  known-good kernel and any drift, but not "candidate X is staged, awaiting its
  trial boot" — the one kernel state a user currently cannot see.
- **Component toggles.** Which optional pieces get installed is `deploy.sh` flags
  today; a checkbox list (or a small TUI selector at install time) would make
  them discoverable. Note the reframing: a full graphical *installer* looks
  unnecessary now that `get.sh` reduced installation to one pasted line.
- **A lighter "daily" mode.** Run the `-Su` + service-cycle and *prompt* before
  any long compile, instead of doing the whole build/deploy in one `--commit`.

---

## 4. Install-side gaps

- **Multilib + 32-bit driver prerequisites are missing from INSTALL.md.**
  Enabling Void's multilib repo and installing 32-bit GL/Vulkan libraries is a
  hard prerequisite for Steam/Proton, and INSTALL.md §1 lists only
  git/xtools/base-devel. **Verify every package name on real Void before writing
  it down** (`xbps-query -Rs`): candidates are `void-repo-multilib`, `-nonfree`,
  `-multilib-nonfree`, plus the 32-bit libraries — and note that the
  `<name>-32bit` convention has already been mis-cited in chat transcripts, which
  is exactly how the `game-devices-udev` spec bug happened. This is the *install*
  side; invariant I6 governs the build side (never cross-build i686 with
  `x86-64-v*`).
- **Give Void its own GRUB (opt-in).** `efibootmgr -o <void>,<rest>` to reorder
  the UEFI boot order, then `os-prober` + `grub-mkconfig` so Void's GRUB absorbs
  the other OSes. This is the **missing precondition for §8.6 one-shot kernel
  staging** — the reason staging degrades to the `external` class on the test
  box. Deliberately *not* done there: Debian-owns-GRUB is that machine's escape
  hatch. Worth an INSTALL.md "multi-boot: give Void the bootloader" section for
  anyone who wants the one-shot boot test.

---

## 5. Undecided levers

Each of these needs a decision rather than another mention.

- **THP: `always` vs `madvise`.** The overlay sets `always`; conventional gaming
  advice is `madvise`. A real, debatable divergence for a latency-sensitive
  profile — worth measuring rather than arguing.
- **Alternative global allocator** (jemalloc/scudo via `LD_PRELOAD`). A genuine
  CachyOS lever, but LD_PRELOAD-ing a global allocator system-wide is invasive
  and un-Void. Leaning reject for the base overlay; at most an opt-in per-game
  launch tweak.
- **`options nvidia-drm fbdev=1`** — a modern-driver-only KMS tweak (we already
  run `modeset=1`). Irrelevant to the nvidia470 legacy testbed; only meaningful
  once a modern-GPU target exists.
- **A second hardware target.** The living-room Asus GR8 mini-PC was the original
  second box. Anything claimed for modern GPUs (Wayland, explicit sync, `fbdev`,
  gamescope) needs hardware like that to be more than theory.

---

## 6. Selection rule for any addition (agreed 2026-08-15)

**An addition must be upstream-maintained. If keeping it working falls on US —
our own srcpkg, our own fork, our own rules file to curate — it is DISQUALIFIED,
no matter how well it fits the philosophy, UNLESS it *is* core CachyOS
substance.**

`linux-cachy`/BORE is the one sanctioned exception: maintaining that fork is the
project's reason to exist, not a side burden. Everything else must be one
`xbps-install` away, with Void (or the package's upstream) carrying the updates.

Rationale: this is a solo hobby overlay that deliberately rides upstream Void so
it "doesn't rot". Every self-maintained component is a standing bill against that
design; the kernel is the only bill worth paying. (Our own *config* appliers,
like §1's Tier-2 work, are not packages and are not disqualified — but they are
not free either.)

---

## 7. Closed — do not re-propose

Each of these was decided, not deferred. The listed condition is the *only* thing
that reopens it.

| Idea | Verdict | Reopens if |
|---|---|---|
| **ananicy-cpp** (automatic nice/ioprio rules) | Ruled out permanently — not in Void's repos, so adopting it means maintaining a srcpkg *and* a rules file forever (§6) | Void packages it upstream (`xbps-query -Rs ananicy` returns a hit) |
| **sched-ext / scx userspace** | Same disposition — no Void packages, and BORE is the identity | Void packages it |
| **`mitigations=off`** | Rejected — invariant I7 | never |
| **linux-tkg / XanMod** | Rejected — we fork Void's *own* kernel template; BORE already works | never |
| **Standalone distro / binary repo / ISO installer** | Rejected — the overlay rides upstream Void so it doesn't rot, and `get.sh` already reduced installing to one pasted line | never |
| **A locked-in desktop (Plasma or otherwise)** | Rejected — the original arguments were Wayland-era KWin features argued from hardware this project doesn't have; desktops are detected and optional (§1) | never |
| **Auto-purging old kernels** | Rejected — a kernel that boots healthy today can still fail later, and a rebuild costs hours. Leftovers are *reported* with size, role and the exact command instead | never |
| **A "purge kernels" button** | Rejected — it would widen the sudo grant to save typing one command a couple of times a year | never |
| **`grub-mkconfig` in the updater** | Rejected and unnecessary — Void's own `grub` package regenerates the config via `/etc/kernel.d/{post-install,post-remove}/50-grub` | never |
| **An accurate compilation progress percentage** | Rejected — unknowable for an `xbps-src` or 18-hour kernel build, and a bar that lies is worse than none. The window shows a pulse, elapsed time and the stage in words | never |
| **A `pipewire` runit service** | Never add one — PipeWire starts as a user-session/DBus service on Void | never |
| **Remote-fetched `bore.lock` / minisign** | Rejected — fetching the expected hash alongside the artifact collapses the trust model | never |
