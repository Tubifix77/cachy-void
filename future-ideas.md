# Future ideas

**Only things not yet built.** This file used to double as a changelog — every
idea listed, then annotated "DONE" — which made it useless for its actual job.
What exists is documented where it belongs: the design in
[`architecture.md`](architecture.md), the user-facing surface in
[`README.md`](README.md) and [`INSTALL.md`](INSTALL.md), the look in
[`branding.md`](branding.md), and the history in `git log`. Nothing is recorded
here twice.

Two companions govern which ideas are worth having, and neither is an idea
itself: the **selection rule** every addition has to pass, which is stated right
here rather than numbered among the proposals it judges, and
[`rejected-ideas.md`](rejected-ideas.md), where everything already turned down is
recorded with its verdict — check it before proposing something, because a
rejected idea stays an idea until the verdict is written down.

**The selection rule (agreed 2026-08-15). An addition must be
upstream-maintained. If keeping it working falls on US — our own srcpkg, our own
fork, our own rules file to curate — it is DISQUALIFIED, no matter how well it
fits the philosophy, UNLESS it *is* core CachyOS substance.**

`linux-cachy`/BORE is the one sanctioned exception: maintaining that fork is the
project's reason to exist, not a side burden. Everything else must be one
`xbps-install` away, with Void (or the package's upstream) carrying the updates.

Rationale: this is a solo hobby overlay that deliberately rides upstream Void so
it "doesn't rot". Every self-maintained component is a standing bill against that
design; the kernel is the only bill worth paying. (Our own *config* appliers,
like §1's Tier-2 work, are not packages and are not disqualified — but they are
not free either.)

---

## 1. Desktop support beyond the four that exist

The *next* desktop, and only that. Four are branded now — LXQt, the bare Openbox
session, Plasma and Xfce — through three Tier-2 appliers, since Openbox shares
LXQt's. Read `branding.md` §5.12 (Plasma), §5.13 (Xfce) and §6 first for the
applier model and the plumbing a new one plugs into.

**Price it honestly.** The plumbing IS cheap now — a row in the detector's table, a
function, and the install-time choice, dry-run and offline isolation test come free.
The plumbing was never the expensive part. Of the fourteen bug fixes the Plasma
applier took, **not one** was findable offline: they were sensor daemons that never
start on Void, a launcher URL scheme that silently renders a placeholder, a panel
frame that is a slab instead of a line, applet order that no API can set, a colour
scheme that refuses to re-apply under its own name. Every one needed the desktop
installed and a human looking at it. Openbox was the same story from the other
direction — it provides nothing, so the applier had to supply audio, polkit,
notifications and a panel before the look mattered at all.

So the real cost of a desktop is **discovery**, and it scales with how much the
desktop assumes: Openbox assumed nothing and needed everything; Plasma provided
everything its own way and assumed systemd. Budget login cycles, not lines.

- **GTK/GNOME is the only one left, and the lowest priority.** It resists
  theming, ships its own network applet, and its users are the least likely to
  want a Qt-shaped look. Wrap it in `cachy-de-trial` as Xfce was.

**What Xfce actually cost, now that it is done** — worth reading before starting
GNOME, because it revises the estimate above. The offline half was cheap and
mostly correct: xfconf is a clean mechanism, and Arc-Dark shipping its own
`xfwm4/` decorations meant no bitmap titlebars to author. The expensive half was
everything only a screen shows, and it arrived as a *stream of small wrong
details* rather than one big problem: a grey menu icon that was the wallpaper
motif instead of the brand mark, a dock launcher missing the `items` array that
makes a launcher work, an icon theme whose cache hid every alias, three grip
lines that took three diagnoses (not the panel lock, not separator style — the
tasklist's own `show-handle`), a tray missing volume and media because Xfce has
no plugin for either, and a display layout Xfce restores at login that silently
put the whole desktop on a closed laptop panel. None of that was findable
offline. Budget login cycles and an owner looking at it, exactly as §1 says.

**Rules for any of them** (unchanged, and they are what keep this from sprawling):
never install a desktop, never override a user's own theme, and an unrecognised
session gets the shared assets plus a plain message — never a half-applied look.
Each Tier-2 applier is our own config code, so the selection rule's maintenance
test does not disqualify it, but it is not free either: **three appliers is already at the sane
ceiling** — the ceiling this section set before Xfce, and Xfce spent it. A fourth
needs a better reason than "we could", and GNOME above is not obviously that
reason.

---

## 2. Updater — remaining gaps

- **Component toggles.** Which optional pieces get installed is `deploy.sh` flags
  today; a checkbox list (or a small TUI selector at install time) would make
  them discoverable. Note the reframing: a full graphical *installer* looks
  unnecessary now that `get.sh` reduced installation to one pasted line.
- **A lighter "daily" mode.** Run the `-Su` + service-cycle and *prompt* before
  any long compile, instead of doing the whole build/deploy in one `--commit`.
- **One-click snapshot restore.** `--snapshots` prints the restore commands for
  the host's layout (architecture.md §9.5b); doing it *for* the user is the part
  that is not built. It needs exactly two new sudoers grants — `btrfs subvolume
  snapshot` (the writable variant) and `btrfs subvolume set-default`, as narrow
  as the §9.5 originals — plus a confirm dialog and a reboot prompt. Only on
  layouts the engine already classifies `LAYOUT_TOPLEVEL`; a `subvol=`-pinned
  root must keep refusing, because fstab overrides the default subvolume and the
  restore would be a silent no-op. **This is the one item here with a real
  hardware gate:** on a `btrfs-convert`ed root `/boot` lives inside the root
  tree, so a restore rewinds kernels too, and how a foreign bootloader's
  hand-written menuentry resolves against a changed default subvolume must be
  tested, not deduced. A further question to settle rather than assume: showing
  "you are running from restored snapshot X" needs `btrfs subvolume get-default`
  — a third grant, which may or may not be worth it over the printed warning.

---

## 3. Undecided levers

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
