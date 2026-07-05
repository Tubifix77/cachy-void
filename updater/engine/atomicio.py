"""Atomic and append-only file primitives shared by the journal and state stores.

Both the transaction journal (§7.6) and the kernel state file (§8.1) require
crash-safe writes: a violently interrupted process must never leave a
half-written state file behind. ``write_json_atomic`` guarantees the target is
either the old contents or the fully new contents, never a truncated mixture.
"""
from __future__ import annotations

import glob
import json
import os
import tempfile
from typing import Any


def write_json_atomic(path: str | os.PathLike, obj: Any) -> None:
    """Serialize ``obj`` to ``path`` atomically (temp file -> fsync -> rename)."""
    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(directory)
    except BaseException:
        _silent_unlink(tmp)
        raise


def append_jsonl(path: str | os.PathLike, obj: Any) -> None:
    """Append one JSON object as a single line (JSON-lines audit trail)."""
    line = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def read_json(path: str | os.PathLike) -> Any:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: str | os.PathLike) -> list[Any]:
    """Read a JSON-lines file, skipping any trailing partial (torn) final line."""
    out: list[Any] = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # A crash mid-append can leave a truncated final line; ignore it.
                break
    return out


def sweep_tmp(directory: str | os.PathLike) -> int:
    """Remove stale ``.tmp-*.json`` litter left by hard-killed atomic writes.

    The in-process exception guard in :func:`write_json_atomic` cannot run on a
    power cut; this sweep is the complementary cleanup at tool start. Returns
    the number of files removed; a missing directory removes nothing.
    """
    removed = 0
    for path in glob.glob(os.path.join(os.fspath(directory), ".tmp-*.json")):
        try:
            os.unlink(path)
            removed += 1
        except OSError:
            pass
    return removed


def _fsync_dir(directory: str) -> None:
    try:
        dfd = os.open(directory, os.O_DIRECTORY)
    except (OSError, AttributeError):
        return  # not supported on this platform (e.g. Windows); best effort
    try:
        os.fsync(dfd)
    except OSError:
        pass
    finally:
        os.close(dfd)


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass
