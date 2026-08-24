# Future ideas

**Only things not yet built.** This file used to double as a changelog — every
idea listed, then annotated "DONE" — which made it useless for its actual job.
What exists is documented where it belongs: the design in
[`architecture.md`](architecture.md), the user-facing surface in
[`README.md`](README.md) and [`INSTALL.md`](INSTALL.md), the look in
[`branding.md`](branding.md), and the history in `git log`. Nothing is recorded
here twice.

Two companions govern which ideas are worth having, and both live outside this
list: the **selection rule** for any addition is §4 below, and everything already
turned down is in [`rejected-ideas.md`](rejected-ideas.md) — check it before
proposing something, because a rejected idea stays an idea until the verdict is
written down.

---

## 1. Desktop support beyond the three that exist

The *next* desktop, and only that. Read `branding.md` §5.12/§6 first for the applier
model and the plumbing a new one plugs into.

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

- **XFCE** is the most plausible next one — `xfconf-query` is a clean config
  mechanism and the dual-boot Debian on the test laptop runs XFCE, so its *look* can
  be studied there. But it would still need installing on the Void side to develop
  against, exactly as Plasma did. That part is at least safe and reversible now:
  wrap it in `cachy-de-trial`.
- **GTK/GNOME is the lowest priority.** It resists theming, ships its own network
  applet, and its users are the least likely to want a Qt-shaped look.

**Rules for any of them** (unchanged, and they are what keep this from sprawling):
never install a desktop, never override a user's own theme, and an unrecognised
session gets the shared assets plus a plain message — never a half-applied look.
Each Tier-2 applier is our own config code, so §4's maintenance test does not
disqualify it, but it is not free either: three is already at the sane ceiling,
and a fourth needs a better reason than "we could".

---

## 2. Updater — remaining gaps

- **Component toggles.** Which optional pieces get installed is `deploy.sh` flags
  today; a checkbox list (or a small TUI selector at install time) would make
  them discoverable. Note the reframing: a full graphical *installer* looks
  unnecessary now that `get.sh` reduced installation to one pasted line.
- **A lighter "daily" mode.** Run the `-Su` + service-cycle and *prompt* before
  any long compile, instead of doing the whole build/deploy in one `--commit`.

### 2b. From the Omarchy 4 review (2026-08-23): two updater upgrades

Omarchy 4 was surveyed for anything adoptable (most of it is self-maintained
desktop-replacement — disqualified by §4 by construction). Two ideas survived the
filter, and both land in the updater — the one place where the maintenance is
already ours and "make it more user-friendly" cannot cross the philosophy. They
are **two features, not one**, but they share one piece of groundwork.

**Shared groundwork — a fast, machine-readable status surface.** `--status` today
is a human-readable overview that "can take a while" (the GUI shows a busy line
for it). Both features below want a cheap
read-only probe: pending upstream count, kernel drift, staged candidate,
snapshot inventory — as JSON (say `--pending`), answering in a second. Single
source of truth stays the CLI; the GUI and the tray both consume it. (The
kernel/boot-state *readouts* this section used to list are built; what is left
is the fast machine-readable surface, which is now this section's groundwork.)

**Idea 1 — a passive "updates pending" tray presence.** Today the updater only
speaks when opened. A small tray icon (our own PyQt code — the toolkit is already
a dependency, so §4 does not apply) that idles, periodically probes, shows a
quiet badge when updates are pending, and opens `cachy-updater-gui` on click.
Facts that make it cheap, all verified:
  - **The probe needs no root and no staleness workaround**: `xbps-install -Mun`
    (`-M, --memory-sync`) fetches the remote index *in memory*, unprivileged,
    ignoring the on-disk cache — a fresh dry-run update list with zero system
    writes. `-u` respects holds, so pinned kernels never cause a stuck badge.
    Poll every few hours; never hammer mirrors.
  - Overlay-side pending (the §7.3 M/P/O terms) deliberately stays out of the
    probe — those only materialize after a sync anyway; the badge says "upstream
    has N for you", the GUI's Check remains the full truth.
  - Sessions: one ungated XDG autostart entry (this is a product surface wanted
    in *every* desktop — the opposite of the branding rule) covers LXQt and
    Plasma; the bare-Openbox session runs no XDG autostart, so its line goes in
    the branding-managed `~/.config/openbox/autostart` (tint2 already provides
    the tray there). Heed the nm-tray lesson: exactly one autostart mechanism
    per session, never two.
  - Notifications (libnotify) at most once per new-updates transition; never
    nag, never auto-open. The icon must read as void-tactical, not as alarm.

**Idea 2 — a snapshot restore surface ("go back to before an update").** §9.5
takes snapshots religiously; §5 documents restoring one — but as a manual
runbook. The affordance belongs in the GUI, next to (and clearly distinct from)
"Boot known-good kernel": that button re-pins a *kernel*; this feature restores
*userland*. Never reuse the word "rollback" across the two. Phased, because the
privilege story differs per filesystem layout:
  - **Phase R1 — see and understand (no new privileges).** A Snapshots pane:
    list via the already-granted `btrfs subvolume list`, showing every snapshot
    (`deploy-*`, `manual-*`, `de-trial-*` — prune only ever touches `deploy-*`),
    its age, and — the annotate-never-dump touch — what the matching journal
    run_id says that update *did* ("36 pkgs incl. mesa, firefox"). Then detect
    the host's layout and render the exact §5 restore recipe for THIS machine,
    not a generic doc pointer.
  - **Phase R2 — one-click restore, where the layout permits (opt-in, spec
    §4.1/§9.5 amendment).** On a root with no `subvol=` pin in fstab (the
    btrfs-convert testbed is exactly this: root = top-level ID 5, fstab mounts
    the bare UUID), restore is two commands: writable snapshot of the chosen
    read-only one, then `btrfs subvolume set-default`, then a user-confirmed
    reboot. Needs exactly two new sudoers grants: `btrfs subvolume snapshot`
    (writable variant) and `btrfs subvolume set-default` — as narrow as the §9.5
    originals. Take a `-r` safety snapshot of the current root first (already
    granted), so restore itself is undoable. On the §9.1 `@` layout, fstab's
    `subvol=@` ignores set-default and the honest move is Phase R1's recipe
    (the snapper-style rename-swap needs mount+mv grants — too wide; refuse
    rather than widen).
  - **Verify-before-ship items**, named now so nobody assumes: (1) on the
    converted testbed `/boot` lives *inside* the root tree (unlike §9.1 where
    it is outside the `@` subvol), so a restore also rewinds `/boot` content —
    how the foreign GRUB's hand-written menuentry resolves paths against a
    changed default subvolume must be tested, not deduced; (2) post-restore
    state must be visible ("running from restored snapshot X", get-default ≠ 5)
    with cleanup guidance, or the box quietly accumulates mystery subvolumes.

Also noted in passing during the same survey, for whoever tends the testbed: the
`ext2_saved` subvolume (btrfs-convert's undo image) still pins pre-conversion
blocks on the box; deleting it is the standard post-conversion cleanup once the
btrfs root is trusted — an operator decision, not overlay business.

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

---

## 4. Selection rule for any addition (agreed 2026-08-15)

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
