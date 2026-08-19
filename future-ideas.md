# Future ideas

**Only things not yet built.** This file used to double as a changelog — every
idea listed, then annotated "DONE" — which made it useless for its actual job.
What exists is documented where it belongs: the design in
[`architecture.md`](architecture.md), the user-facing surface in
[`README.md`](README.md) and [`INSTALL.md`](INSTALL.md), the look in
[`branding.md`](branding.md), and the history in `git log`. Nothing is recorded
here twice.

Two companions govern which ideas are worth having, and both live outside this
list: the **selection rule** for any addition is §5 below, and everything already
turned down is in [`rejected-ideas.md`](rejected-ideas.md) — check it before
proposing something, because a rejected idea stays an idea until the verdict is
written down.

---

## 1. Desktop support beyond LXQt

The overlay itself is desktop-agnostic — only the optional look is not (see
README's Highlights table). Adding a desktop means adding an *applier*, nothing
else.

- **Tier 1 — any Qt desktop:** assets that need no integration (Kvantum theme,
  wallpaper, terminal scheme, SDDM greeter, icon set). Already applies unchanged
  under LXQt, Plasma or a lone Openbox.
- **Tier 2 — one small applier per desktop**, written against that desktop's own
  config mechanism and nothing else. **Two already exist**, both in
  `cachy-branding` and both tested on real hardware: **LXQt** (panel, session,
  Kvantum) and the **bare Openbox session** (branding.md §5.9) — so Plasma would
  be the *third* environment, not the first attempt at a second.

  The Openbox work is worth reading before writing another applier, because it
  produced the transferable lesson: an applier's real job is covering *what the
  environment does not provide*. Stock Openbox draws nothing, so a user who picks
  that session gets a black screen and reads it as broken; it needed a wallpaper,
  a compositor, a **tint2 panel** (without a taskbar a launched window can open
  behind another and reads as "I clicked Terminal and nothing happened") and a
  curated root menu, since Openbox's stock one lists ~48 apps from other desktops.
  Plasma is the opposite case — it provides all of that itself — which is exactly
  why its applier is smaller: colours, Kvantum bridge, wallpaper, Konsole scheme.

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

**Small open item on the Openbox session:** its tray applets (pasystray, udiskie,
cbatticon) draw stock icons rather than the mono brand set, because `Luv-Void`
only covers the names that were read out of nm-tray's source. Extending the
applier's `emit()` recolouring means reading each app's icon names from *its*
source too — cheap, but it must be verified rather than guessed.

**After that:** XFCE is cheap (`xfconf` for wm theme + wallpaper, and the
dual-boot Debian on the test laptop runs it). GTK/GNOME is lowest priority — it
resists theming and ships its own network applet.

**Rules for any of them:** never install a desktop, never override a user's own
theme (opt-in, reversible), and an unrecognised session gets Tier 1 plus a plain
message — never a half-applied look. Each Tier-2 applier is a standing
maintenance bill (our config code, so not disqualified by §5, but not free
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

## 3. Updater — remaining gaps

- **Staged-candidate readout.** The status pane shows the BORE pin, the
  known-good kernel and any drift, but not "candidate X is staged, awaiting its
  trial boot" — the one kernel state a user currently cannot see.
- **Component toggles.** Which optional pieces get installed is `deploy.sh` flags
  today; a checkbox list (or a small TUI selector at install time) would make
  them discoverable. Note the reframing: a full graphical *installer* looks
  unnecessary now that `get.sh` reduced installation to one pasted line.
- **A lighter "daily" mode.** Run the `-Su` + service-cycle and *prompt* before
  any long compile, instead of doing the whole build/deploy in one `--commit`.
- **Verify the new kernel is actually bootable.** Nothing currently checks the
  step between "kernel built and installed" and "kernel present in the boot
  menu". On a GRUB-owning host that step is Void's own job — the `grub` package's
  `/etc/kernel.d/post-install/50-grub` hook regenerates `grub.cfg` on every
  kernel install, the same mechanism whose sibling `10-dkms` is proven to fire
  for `linux-cachy` — so the updater must **not** regenerate anything (see
  `rejected-ideas.md`). What it *can* do is confirm the outcome: after installing
  a kernel, check that `grub.cfg` contains an entry for the new version and say
  so, or warn if it does not. The engine already parses `grub.cfg` for §8.6
  staging, so this is a read-only check needing no new privileges, and it catches
  the failure modes that today leave a kernel built but unbootable with nobody
  the wiser (a hook that errored, `/boot` not mounted at install time).
- **Say the multi-boot truth out loud.** On a foreign-bootloader host (the
  `external` class — a dual-boot gamer's normal case) new kernels *do* boot
  automatically, via the evergreen `/boot/vmlinuz-current` symlink that the
  `99-boot-symlinks` hook repoints; what is impossible is *choosing* an older one
  without regenerating the other OS's menu. The updater should state that plainly
  instead of leaving the user to infer it from a "bookkeeping-only" message.

---

## 4. Undecided levers

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

## 5. Selection rule for any addition (agreed 2026-08-15)

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

Ideas this rule has already disqualified — and every other rejection — are
recorded with their verdicts in [`rejected-ideas.md`](rejected-ideas.md).
