"""Pre-deploy btrfs snapshot — architecture.md §9.5.

Optional, btrfs-only, opt-in. Takes a read-only snapshot of the deploy subvolume
immediately before Stage 4's ``xbps-install -Suy``, giving the userland deploy the
same rollback the kernel already gets from §8.6. Per the §7.6 doctrine the snapshot
is a witness/rollback artifact only — nothing here is ever read to drive control
flow. The snapshot dir is expected to be a pre-created dedicated subvolume
(deploy.sh's job); creating it is deliberately NOT in the sudoers grant.

Second half (§9.5b): the read-only inventory and restore RECIPE that make the
net visible. Taking snapshots nobody can find is only half a safety feature, and
the half that matters is reached under stress — so the product lists them, says
what each was taken for, and prints the exact commands for this host's layout
without ever running them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


class SnapshotError(RuntimeError):
    """Base for §9.5 snapshot failures."""


class SnapshotUnavailable(SnapshotError):
    """`[snapshot] enable=true` but the deploy subvol is not btrfs (exit 53)."""


class SnapshotFailed(SnapshotError):
    """The pre-deploy snapshot command failed with snapshots enabled (exit 54)."""


def _fstype(target: str, run) -> str:
    cp = run(["findmnt", "-no", "FSTYPE", "--target", target])
    return (cp.stdout or "").strip() if cp.returncode == 0 else ""


def pre_deploy_snapshot(*, enable, subvol: str, snap_dir: str, keep: int,
                        run_id: str, run, out=print) -> Optional[str]:
    """Take the §9.5 pre-deploy snapshot; return its path, or None if skipped.

    ``enable`` is ``True`` (force — raise if not btrfs), ``False`` (disabled), or
    the string ``"auto"`` (active iff the deploy subvol is btrfs). Raises
    :class:`SnapshotUnavailable` (→ exit 53) or :class:`SnapshotFailed` (→ exit 54);
    the caller aborts the commit before any mutation in both cases.
    """
    if enable is False:
        return None
    fstype = _fstype(subvol, run)
    if fstype != "btrfs":
        if enable is True:
            raise SnapshotUnavailable(
                f"[snapshot] enable=true but {subvol} is "
                f"{fstype or 'not a mountpoint'}, not btrfs")
        out(f"snapshot: {subvol} is not btrfs — skipping pre-deploy snapshot (§9.5 auto).")
        return None
    dest = f"{snap_dir.rstrip('/')}/deploy-{run_id}"
    cp = run(["sudo", "btrfs", "subvolume", "snapshot", "-r", subvol, dest])
    if cp.returncode != 0:
        raise SnapshotFailed(
            f"btrfs snapshot of {subvol} -> {dest} failed: {(cp.stderr or '').strip()}")
    out(f"snapshot: created read-only {dest} (§9.5)")
    _prune(snap_dir, keep, run, out)
    return dest


def _prune(snap_dir: str, keep: int, run, out) -> None:
    """Delete the oldest ``deploy-*`` snapshots beyond ``keep``. run_ids are
    sortable UTC timestamps, so lexical order is chronological. Best-effort — a
    prune failure is logged, never fatal (the fresh snapshot already exists).
    """
    if keep is None or keep < 0:
        return
    cp = run(["sudo", "btrfs", "subvolume", "list", "-o", snap_dir])
    if cp.returncode != 0:
        out(f"snapshot: prune skipped — cannot list subvolumes under {snap_dir}")
        return
    names = []
    for line in (cp.stdout or "").splitlines():
        _, _, path = line.partition(" path ")
        base = path.strip().rsplit("/", 1)[-1]
        if base.startswith("deploy-"):
            names.append(base)
    names.sort()
    stale = names[:-keep] if keep > 0 else names
    for base in stale:
        target = f"{snap_dir.rstrip('/')}/{base}"
        d = run(["sudo", "btrfs", "subvolume", "delete", target])
        out(f"snapshot: pruned {target}" if d.returncode == 0
            else f"snapshot: prune of {target} failed (non-fatal)")


# ==========================================================================
# §9.5b Snapshot inventory & restore recipe (read-only)
# ==========================================================================
# The gap: §9.5 takes snapshots religiously and §5's runbook explains restoring
# one, but nothing in the product ever *shows* them. A safety net nobody can see
# is a safety net nobody reaches for under stress, which is the only time it
# matters. This half is deliberately read-only: it lists what exists, says what
# each snapshot was taken for, and prints the exact restore commands for THIS
# machine's layout. It executes none of them.
#
# Everything here uses privileges the updater already has (§9.5 granted
# `btrfs subvolume list`) plus an unprivileged read of /etc/fstab. Creation times
# come from the snapshot NAME rather than `btrfs subvolume show`, which is not in
# the grant — the names are UTC stamps by construction (`deploy-<run_id>`), so
# the information is already there.

KIND_DEPLOY = "deploy"      # taken automatically before a deploy (§9.5)
KIND_MANUAL = "manual"      # taken by hand before something risky
KIND_TRIAL = "de-trial"     # taken by cachy-de-trial before installing a desktop
KIND_OTHER = "other"

# Only `deploy-*` is ever pruned (see _prune), so the others are the user's own
# bookmarks and must never be presented as disposable.
PRUNED_KINDS = (KIND_DEPLOY,)

_STAMP_RE = re.compile(r"^(?P<prefix>.*?)-?(?P<stamp>\d{8}T\d{6}Z)$")


@dataclass
class SnapshotInfo:
    """One snapshot, as shown to a human."""
    name: str
    path: str
    kind: str
    stamp: Optional[str] = None          # 20260811T011412Z, or None if unparseable
    run_id: Optional[str] = None         # joins a deploy-* snapshot to its journal
    label: str = ""                      # the prefix minus the kind, e.g. "plasma"

    @property
    def prunable(self) -> bool:
        return self.kind in PRUNED_KINDS


def parse_snapshot_name(name: str) -> SnapshotInfo:
    """Classify a snapshot directory name. Never raises on an odd name.

    Prefixes can themselves contain dashes (``de-trial-plasma-<stamp>``,
    ``manual-pre-first-update-<stamp>``), so the STAMP is matched at the end and
    everything before it is the prefix — not the other way round.
    """
    m = _STAMP_RE.match(name)
    stamp = m.group("stamp") if m else None
    prefix = (m.group("prefix") if m else name) or ""
    if prefix.startswith("deploy"):
        kind, label = KIND_DEPLOY, prefix[len("deploy"):].strip("-")
    elif prefix.startswith("de-trial"):
        kind, label = KIND_TRIAL, prefix[len("de-trial"):].strip("-")
    elif prefix.startswith("manual"):
        kind, label = KIND_MANUAL, prefix[len("manual"):].strip("-")
    else:
        kind, label = KIND_OTHER, prefix
    return SnapshotInfo(name=name, path="", kind=kind, stamp=stamp,
                        run_id=stamp if kind == KIND_DEPLOY else None,
                        label=label)


def age_text(stamp: Optional[str], now=None) -> str:
    """"3 days ago" / "today" from a UTC stamp; "" when it cannot be parsed."""
    if not stamp:
        return ""
    try:
        when = datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    now = now or datetime.now(timezone.utc)
    days = (now - when).days
    if days < 0:
        return "in the future (clock skew?)"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    return f"{days // 30} months ago"


def list_snapshots(*, snap_dir: str, run) -> list[SnapshotInfo]:
    """Every snapshot under ``snap_dir``, newest first. [] when unavailable.

    Uses the §9.5 `btrfs subvolume list` grant. A failure here is never fatal:
    "we cannot see the snapshots" is a thing to report, not to crash on.
    """
    cp = run(["sudo", "btrfs", "subvolume", "list", "-o", snap_dir])
    if cp.returncode != 0:
        return []
    out: list[SnapshotInfo] = []
    for line in (cp.stdout or "").splitlines():
        _, _, path = line.partition(" path ")
        base = path.strip().rsplit("/", 1)[-1]
        if not base:
            continue
        info = parse_snapshot_name(base)
        info.path = f"{snap_dir.rstrip('/')}/{base}"
        out.append(info)
    # Names end in sortable UTC stamps, so lexical order is chronological.
    out.sort(key=lambda s: (s.stamp or "", s.name), reverse=True)
    return out


# -- restore layout ---------------------------------------------------------
LAYOUT_TOPLEVEL = "toplevel"   # root mounts the top-level tree: set-default works
LAYOUT_PINNED = "pinned"       # fstab names a subvol=: set-default is ignored
LAYOUT_UNKNOWN = "unknown"     # could not tell; never guess about someone's root


@dataclass
class RestoreLayout:
    kind: str
    subvol: Optional[str] = None      # the pinned subvol, when there is one
    boot_inside_root: bool = True     # is /boot part of what a restore rewinds?
    detail: str = ""


def detect_restore_layout(fstab_text: str) -> RestoreLayout:
    """Decide, from fstab alone, whether a set-default restore can work here.

    Why fstab is the right source: `set-default` changes what mounts *by
    default* and is documented to be overridden by an explicit mount option, so
    a root entry carrying `subvol=`/`subvolid=` makes the whole approach a
    silent no-op — the filesystem would obey fstab and boot the same root as
    before. That is worth detecting rather than discovering after a reboot.
    """
    root_opts = None
    root_fs = None
    boot_separate = False
    for raw in fstab_text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) < 3:
            continue
        target, fstype = parts[1], parts[2]
        opts = parts[3] if len(parts) > 3 else ""
        if target == "/":
            root_opts, root_fs = opts, fstype
        elif target in ("/boot", "/boot/efi") and target == "/boot":
            boot_separate = True
    if root_fs is None:
        return RestoreLayout(LAYOUT_UNKNOWN, boot_inside_root=not boot_separate,
                             detail="no root (/) entry found in fstab")
    if root_fs != "btrfs":
        return RestoreLayout(LAYOUT_UNKNOWN, boot_inside_root=not boot_separate,
                             detail=f"root filesystem is {root_fs}, not btrfs — "
                                    "snapshots do not apply")
    for opt in (root_opts or "").split(","):
        name = opt.strip()
        if name.startswith("subvol=") or name.startswith("subvolid="):
            return RestoreLayout(LAYOUT_PINNED, subvol=name.split("=", 1)[1],
                                 boot_inside_root=not boot_separate,
                                 detail=f"fstab mounts / with {name}")
    return RestoreLayout(LAYOUT_TOPLEVEL, boot_inside_root=not boot_separate,
                         detail="fstab mounts / without a subvol= pin")


@dataclass
class RestorePlan:
    """A recipe, not an action: commands for the human, and what to know first."""
    supported: bool
    steps: list = field(default_factory=list)     # shell commands, in order
    notes: list = field(default_factory=list)     # what to understand before running
    undo: Optional[str] = None                    # the one command that reverses it


def restore_recipe(*, layout: RestoreLayout, snapshot: SnapshotInfo,
                   snap_dir: str = "/.cachy-snapshots",
                   mount: str = "/") -> RestorePlan:
    """The exact commands to boot from ``snapshot`` on THIS machine.

    Three facts shape every line, all verified against btrfs-progs docs rather
    than remembered: `set-default` accepts a subvolume PATH (no ID hunting),
    it takes effect on the next mount (hence the reboot), and it is overridden
    by an explicit `subvol=` mount option (hence LAYOUT_PINNED refusing).
    """
    notes: list = []
    if layout.kind == LAYOUT_PINNED:
        return RestorePlan(
            supported=False,
            notes=[f"This root is mounted with an explicit {layout.subvol!r} "
                   "subvolume, so setting a different default subvolume would be "
                   "silently ignored — the system would boot the same root as "
                   "before. Restoring here means either editing that fstab option "
                   "(a boot-critical edit, and yours to make deliberately) or "
                   "booting the snapshot from a bootloader that can, such as "
                   "grub-btrfs. This tool will not touch fstab.",
                   f"The snapshot itself is intact at {snapshot.path} and can be "
                   "mounted read-only to copy individual files out, which is often "
                   "all that is actually wanted."])
    if layout.kind == LAYOUT_UNKNOWN:
        return RestorePlan(supported=False,
                           notes=[f"Cannot determine this host's btrfs layout: "
                                  f"{layout.detail}. Nothing is offered rather than "
                                  "guessing about your root filesystem."])

    rw = f"{snap_dir.rstrip('/')}/restore-{snapshot.name}"
    steps = [
        f"sudo btrfs subvolume snapshot {mount} {snap_dir.rstrip('/')}/pre-restore-$(date -u +%Y%m%dT%H%M%SZ)",
        f"sudo btrfs subvolume snapshot {snapshot.path} {rw}",
        f"sudo btrfs subvolume set-default {rw}",
        "sudo reboot",
    ]
    notes = [
        "The snapshots are read-only, so the second command makes a WRITABLE copy "
        "to boot from — setting a read-only subvolume as the default gives you a "
        "root filesystem that cannot be written to.",
        "The first command snapshots the CURRENT root first, so this is reversible "
        "in both directions, not just one.",
        "set-default takes effect on the next mount, which is why the reboot is "
        "part of the recipe and not an afterthought.",
        f"After rebooting, {snap_dir} will look EMPTY from the restored root: "
        "nested subvolumes are not part of a snapshot. The snapshots are all still "
        "there — mount the top level to see them again "
        "(`sudo mount -o subvolid=5 <your root device> /mnt`).",
    ]
    if layout.boot_inside_root:
        notes.append(
            "/boot is INSIDE the root filesystem on this host, so a restore also "
            "rewinds your kernels and initramfs. If your bootloader's entry names a "
            "kernel version that only exists in the current root, that entry will "
            "stop working after the restore — check it before rebooting, and keep an "
            "installation medium reachable.")
    return RestorePlan(supported=True, steps=steps, notes=notes,
                       undo=f"sudo btrfs subvolume set-default 5 {mount}   "
                            "# back to the original root, then reboot")
