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

The *next* desktop, and only that. Read `branding.md` §5.12/§6 first: it has the
applier model, the plumbing a new one plugs into, and how to test one without
hardware — which is what makes the scope below small.

- **XFCE is the cheap one.** `xfconf-query` sets the wm theme and wallpaper, and
  the dual-boot Debian on the test laptop already runs it, so it is testable
  without installing anything new on the Void side.
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
