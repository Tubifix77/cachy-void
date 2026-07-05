"""Transaction journal — architecture.md §7.5 and §7.6.

DOCTRINE (normative, §7.6): the journal is a *witness*, never an authority. No
control-flow decision reads it. Crash and failure recovery is achieved by
recomputing the §7.3 queue from live queries (the ``P`` term is the resume
mechanism), so there is deliberately no ``--resume`` flag. A missing, stale, or
corrupt journal is a warning and nothing more.

This module therefore provides:
  * :class:`Journal` — the authoritative *current-run* state as a single JSON
    object written atomically after every transition (§7.6 schema), plus an
    append-only JSON-lines audit trail for humans/forensics; and
  * :func:`crash_report` — parses an interrupted journal to *describe* (never to
    drive) what a violently interrupted run was doing, so the CLI can explain
    the situation before recomputing the real queue.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .atomicio import append_jsonl, read_json, write_json_atomic

SCHEMA = 1

# Run phases (§7.6).
PHASES = ("sync", "query", "build", "deploy", "done", "failed")
# Per-package build states (§7.6).
PKG_STATES = ("pending", "building", "built", "failed")

_TERMINAL = ("done", "failed")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class Journal:
    """Atomic, witness-only transaction journal for one updater run."""

    def __init__(self, run_dir: str | Path):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.run_dir / "journal.json"
        self.log_path = self.run_dir / "journal.log"
        self._data: dict = {}

    # -- lifecycle -------------------------------------------------------
    def start(self, run_id: str, git_head: str) -> "Journal":
        self._data = {
            "schema": SCHEMA,
            "run_id": run_id,
            "git_head": git_head,
            "phase": "sync",
            "order": [],
            "order_provenance": None,
            "pkgs": {},
            "deploy_bins": [],
            "failure": None,
        }
        self._commit("start", run_id=run_id, git_head=git_head)
        return self

    # -- mutations (each one is an atomic whole-file replace) -----------
    def set_phase(self, phase: str) -> None:
        if phase not in PHASES:
            raise ValueError(f"unknown phase {phase!r}")
        self._data["phase"] = phase
        self._commit("phase", phase=phase)

    def set_order(self, order: list[str], provenance: str) -> None:
        self._data["order"] = list(order)
        self._data["order_provenance"] = provenance
        self._data["pkgs"] = {p: {"status": "pending", "log": None,
                                  "started": None, "ended": None} for p in order}
        self._commit("order", provenance=provenance, count=len(order))

    def set_pkg_status(self, pkg: str, status: str,
                       log: Optional[str] = None) -> None:
        if status not in PKG_STATES:
            raise ValueError(f"unknown pkg status {status!r}")
        entry = self._data["pkgs"].setdefault(
            pkg, {"status": "pending", "log": None, "started": None, "ended": None})
        entry["status"] = status
        if log is not None:
            entry["log"] = log
        if status == "building":
            entry["started"] = _now()
        elif status in ("built", "failed"):
            entry["ended"] = _now()
        self._commit("pkg", pkg=pkg, status=status)

    def set_deploy_bins(self, bins: list[str]) -> None:
        self._data["deploy_bins"] = list(bins)
        self._commit("deploy_bins", count=len(bins))

    def fail(self, pkg: Optional[str], exit_code: int) -> None:
        self._data["failure"] = {"pkg": pkg, "exit": exit_code}
        self._data["phase"] = "failed"
        self._commit("failed", pkg=pkg, exit=exit_code)

    def finish(self) -> None:
        self._data["phase"] = "done"
        self._commit("done")

    # -- reads (forensic only; never drives control flow) --------------
    @property
    def data(self) -> dict:
        return dict(self._data)

    # -- internals -------------------------------------------------------
    def _commit(self, event: str, **fields) -> None:
        # WAL discipline (§7.6): the audit line lands BEFORE the snapshot, so
        # after a crash the log's last line names the transition that may not
        # have reached journal.json. Both files tolerate torn tails; neither
        # is read by control flow, so either divergence direction is benign.
        append_jsonl(self.log_path, {"ts": _now(), "event": event, **fields})
        write_json_atomic(self.path, self._data)


@dataclass
class CrashReport:
    """Description of an interrupted run, reconstructed for humans (§7.6)."""
    interrupted: bool
    phase: Optional[str]
    run_id: Optional[str]
    building: Optional[str]          # package caught mid-build, if any
    built: list[str]                 # packages that finished building
    pending: list[str]               # packages that never started
    failure: Optional[dict]
    note: str


def crash_report(journal_path: str | Path) -> CrashReport:
    """Parse a journal to *describe* an interrupted transaction.

    This never reconstructs a queue to act on: per §7.6 the caller must recompute
    the real queue from live queries. The report exists so the CLI can tell the
    operator what happened before it does so.
    """
    path = Path(journal_path)
    if not path.exists():
        return CrashReport(False, None, None, None, [], [], None,
                           "no journal present")
    try:
        data = read_json(path)
    except (ValueError, OSError):
        return CrashReport(True, None, None, None, [], [], None,
                           "journal is corrupt; ignoring it and recomputing "
                           "the queue from live state (§7.6)")

    phase = data.get("phase")
    pkgs = data.get("pkgs", {})
    building = next((p for p, e in pkgs.items() if e.get("status") == "building"),
                   None)
    built = sorted(p for p, e in pkgs.items() if e.get("status") == "built")
    pending = sorted(p for p, e in pkgs.items() if e.get("status") == "pending")
    interrupted = phase not in _TERMINAL

    if not interrupted:
        note = f"previous run ended cleanly in phase {phase!r}"
    elif building:
        note = (f"previous run was interrupted while building {building!r}; "
                "recomputing the queue from live state — the P term will "
                "re-pick up any built-but-undeployed packages (§7.3/§7.6)")
    else:
        note = ("previous run was interrupted; recomputing the queue from live "
                "state (§7.6). No journal-driven resume is performed.")

    return CrashReport(interrupted, phase, data.get("run_id"),
                       building, built, pending, data.get("failure"), note)
