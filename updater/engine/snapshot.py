"""Pre-deploy btrfs snapshot — architecture.md §9.5.

Optional, btrfs-only, opt-in. Takes a read-only snapshot of the deploy subvolume
immediately before Stage 4's ``xbps-install -Suy``, giving the userland deploy the
same rollback the kernel already gets from §8.6. Per the §7.6 doctrine the snapshot
is a witness/rollback artifact only — nothing here is ever read to drive control
flow. The snapshot dir is expected to be a pre-created dedicated subvolume
(deploy.sh's job); creating it is deliberately NOT in the sudoers grant.
"""
from __future__ import annotations

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
