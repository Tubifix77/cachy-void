# Rejected ideas

**Decisions, not a backlog.** Every entry here was considered and turned down —
so this is neither [`future-ideas.md`](future-ideas.md) (things still worth
building) nor a changelog of what shipped. It exists because a rejected idea is
still an *idea*: without a written verdict it gets re-proposed, re-argued, and
sometimes re-derived from scratch by whoever forgot.

**How to use it:** check here before proposing anything. Each row names the single
condition that would reopen the question — usually "never", occasionally a
concrete external event (Void packaging something upstream). "I still think it
would be nice" is not one of those conditions. If a reopening condition *is* met,
move the row into `future-ideas.md` rather than editing the verdict here.

The selection rule these verdicts are measured against lives in
[`future-ideas.md`](future-ideas.md) §6, because it governs new proposals.

---

## Packages and components

| Idea | Verdict | Reopens if |
|---|---|---|
| **ananicy-cpp** (automatic nice/ioprio rules for games) | Ruled out permanently. The largest genuinely missing piece of CachyOS substance, and rejected anyway: it is not in Void's repos, so adopting it means maintaining a srcpkg *and* a community rules file forever — disqualified by the selection rule, not by philosophy fit | Void packages it upstream (`xbps-query -Rs ananicy` returns a hit) |
| **sched-ext / scx userspace schedulers** | Same disposition — no Void packages, and BORE *is* the project's scheduler identity | Void packages it upstream |
| **linux-tkg / XanMod** | Rejected. `linux-cachy` forks Void's **own** kernel template plus the BORE patch; adopting a foreign kernel tree would replace the thing that makes the fork maintainable, and BORE already works | never |
| **A `pipewire` runit service** | Never add one. PipeWire starts as a user-session/DBus service on Void; a system service would fight it | never |
| **Alternative global allocator system-wide** (jemalloc/scudo via `LD_PRELOAD`) | Not closed — still an open question, tracked in `future-ideas.md` §5. Listed here only because it is often *assumed* rejected: the lean is reject for the base overlay, at most an opt-in per-game tweak | — (see future-ideas §5) |

## Distribution and installation

| Idea | Verdict | Reopens if |
|---|---|---|
| **Becoming a standalone distro** (own ISO, own binary repo) | Rejected. The overlay deliberately rides upstream Void so it does not rot; a distro means owning the whole update surface, and a binary repo means signing and hosting builds for hardware we do not have. `get.sh` already reduced installing to one pasted line | never |
| **A graphical installer wizard** | Rejected as unnecessary. `get.sh` + `bootstrap.sh` install non-interactively in one command on a machine that already runs Void; a wizard would add a GUI dependency to the one path that must work on a bare system. (Component *selection* is still an open idea — `future-ideas.md` §3) | never |

## Kernel and boot

| Idea | Verdict | Reopens if |
|---|---|---|
| **Auto-purging old kernels** | Rejected. A kernel that boots healthy today can still fail next week on a path not yet exercised (suspend, a codec, a device), and rebuilding one costs hours; deleting the fallback is unrecoverable. Leftovers are *reported* instead — size, role (rollback target / running / spare) and the exact `vkpurge` command | never |
| **A "purge kernels" button in the updater** | Rejected. It would mean granting `vkpurge` to the updater — widening the sudo boundary — to save typing one command a couple of times a year | never |
| **`grub-mkconfig` / `update-grub` from the updater** | Rejected, and provably unnecessary: Void's own `grub` package ships `/etc/kernel.d/post-install/50-grub` **and** `post-remove/50-grub`, so a GRUB-owning Void host regenerates its config on every kernel install and removal. Provisioning does it once (`deploy.sh --with-grub`); a foreign bootloader is never touched | never |
| **Remote-fetched `bore.lock` / minisign signatures** | Rejected. Fetching the expected hash alongside the artifact collapses the trust model — a network adversary would supply both. The lockfile stays local and human-owned | never |
| **Making Void take over the bootloader** (`efibootmgr -o` to reorder UEFI entries, `os-prober` + `grub-mkconfig` so Void's GRUB absorbs the other OSes) | Rejected as out of scope. This is base-install topology — what a distro's installer owns — and Cachy-Void is an overlay: it *adapts* to whatever boot layout it finds (§8.6's `oneshot`/`manual`/`external`/`skip` classes are exactly that) and never restructures it. Rearranging someone's multi-boot to unlock an optional feature is not a trade an overlay gets to make on their behalf. A user who wants one-shot kernel boot-testing is free to arrange GRUB ownership themselves first — that is their decision about their machine, not a feature of ours | never (as something the overlay does) |
| **`mitigations=off`** | Rejected — invariant I7. Performance-over-hardening is this project's stance in general, but silently disabling CPU vulnerability mitigations on someone else's machine is a different category of decision | never |

## Desktop and look

| Idea | Verdict | Reopens if |
|---|---|---|
| **A locked-in desktop** (KDE Plasma or any other) | Rejected. The original arguments were Wayland-era KWin features — explicit sync, fractional scaling, controller navigation — argued from hardware this project does not have, and two of the three are not the desktop's job at all. Desktops are detected and optional; support is per-DE appliers (`future-ideas.md` §1) | never |
| **An accurate compilation progress percentage** | Rejected. Not knowable for an `xbps-src` build, let alone an 18-hour kernel compile, and a progress bar that lies is worse than none. The window shows an indeterminate pulse, elapsed time, and the current stage in words | never |
| **Adding hues to the palette** | Rejected by default — `branding.md` §2 caps the palette at five roles plus derived tokens, with an explicit *resist adding hues* rule. Not a blanket ban: `--warn` was added in 2026-08 through the documented process (derive a muted tone, add it to the canonical table first, use it as border/icon only) | a genuinely new *role* is needed **and** the process in branding.md §2 is followed |
