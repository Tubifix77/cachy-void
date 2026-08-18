"""BORE patch trust pipeline — architecture.md §8.3.

Security model (do not "improve" this): ``bore.lock`` is a **local, human-owned
trust anchor**, committed to the repo and edited only by an operator at approval
time. It is *never* fetched over the network. Only the BORE *patch artifact*
crosses the network, and it is verified against the sha256 pinned in the local
lockfile (trust-on-first-use). Fetching the expected hash alongside the artifact
would collapse the model — a network adversary would then supply both.

Failure taxonomy and exit mapping (§8.3; kernel-path per §8 preamble):
    TrustConfigError   malformed/missing bore.lock            -> exit 1
    HashMismatch       artifact sha256 != pinned value        -> exit 70 (HALT)
    PatchUnavailable   offline and no valid cached patch      -> exit 70

Offline/degraded fallback is permitted and is exactly the reuse-first path: a
cached patch whose sha256 matches the lockfile is trusted with no network. An
invalid cache is never silently accepted.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# Exit codes (mirror cachy_void_update; kept local to avoid an engine->CLI dep).
EXIT_CONFIG = 1
EXIT_HALT = 70

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


# ==========================================================================
# Exceptions
# ==========================================================================
class TrustError(RuntimeError):
    """Base for all trust-gate failures."""


class TrustConfigError(TrustError):
    """bore.lock is missing, unparseable, or structurally invalid (exit 1)."""


class HashMismatch(TrustError):
    """A patch artifact's sha256 does not match the pinned value (exit 70)."""


class PatchUnavailable(TrustError):
    """Offline with no valid cached patch; trust cannot be bootstrapped (exit 70)."""


class NetworkError(TrustError):
    """The fetcher could not retrieve the artifact (timeout/offline)."""


def exit_code_for(err: BaseException) -> int:
    """Map a trust failure to its §8.3 exit code."""
    if isinstance(err, TrustConfigError):
        return EXIT_CONFIG
    if isinstance(err, TrustError):
        return EXIT_HALT
    return EXIT_CONFIG


# ==========================================================================
# Lockfile model
# ==========================================================================
@dataclass(frozen=True)
class PatchEntry:
    series: str
    file: str
    sha256: str
    bore_version: str
    approved: str
    # Optional per-entry commit. A later pin must reference a NEWER upstream
    # commit than [repo].pinned_commit without invalidating older entries
    # (whose files may have moved at that commit) — so each entry may carry
    # its own; empty = fall back to the repo-wide pinned_commit.
    commit: str = ""


@dataclass(frozen=True)
class BoreLock:
    repo_url: str
    pinned_commit: str
    patches: dict[str, PatchEntry]

    def patch_for(self, series: str) -> PatchEntry:
        try:
            return self.patches[series]
        except KeyError:
            raise TrustConfigError(
                f"bore.lock has no [[patch]] entry for series {series!r}")


def load_bore_lock(path: str | Path) -> BoreLock:
    """Parse and structurally validate the local bore.lock (§8.3)."""
    p = Path(path)
    if not p.exists():
        raise TrustConfigError(f"bore.lock not found at {p}")
    try:
        with open(p, "rb") as fh:
            raw = tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise TrustConfigError(f"bore.lock is not valid TOML: {exc}") from exc

    repo = raw.get("repo")
    if not isinstance(repo, dict) or not repo.get("url") or not repo.get("pinned_commit"):
        raise TrustConfigError("bore.lock [repo] must set url and pinned_commit")
    if not re.fullmatch(r"[0-9a-fA-F]{7,40}", str(repo["pinned_commit"])):
        raise TrustConfigError("bore.lock pinned_commit is not a git sha")

    entries = raw.get("patch")
    if not isinstance(entries, list) or not entries:
        raise TrustConfigError("bore.lock must define at least one [[patch]]")

    patches: dict[str, PatchEntry] = {}
    for e in entries:
        if not isinstance(e, dict):
            raise TrustConfigError("malformed [[patch]] entry")
        missing = [k for k in ("series", "file", "sha256") if not e.get(k)]
        if missing:
            raise TrustConfigError(f"[[patch]] missing keys: {', '.join(missing)}")
        sha = str(e["sha256"]).lower()
        if not _SHA256_RE.match(sha):
            raise TrustConfigError(
                f"[[patch]] sha256 for series {e['series']!r} is not 64 hex chars")
        series = str(e["series"])
        if series in patches:
            raise TrustConfigError(f"duplicate [[patch]] for series {series!r}")
        commit = str(e.get("commit", "") or "")
        if commit and not re.fullmatch(r"[0-9a-fA-F]{7,40}", commit):
            raise TrustConfigError(
                f"[[patch]] commit for series {series!r} is not a git sha")
        patches[series] = PatchEntry(
            series=series, file=str(e["file"]), sha256=sha,
            bore_version=str(e.get("bore_version", "")),
            approved=str(e.get("approved", "")), commit=commit)
    return BoreLock(str(repo["url"]), str(repo["pinned_commit"]), patches)


# ==========================================================================
# Hashing
# ==========================================================================
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# ==========================================================================
# Network fetcher (injectable; default uses git)
# ==========================================================================
# A fetcher retrieves the artifact bytes for (repo_url, commit, file) or raises
# NetworkError. Kept abstract so tests never touch the network.
Fetcher = Callable[[str, str, str], bytes]


class GitPatchFetcher:
    """Default fetcher: shallow-fetch the pinned commit and read the blob.

    Never used by unit tests (they inject a fake), so its correctness is by
    construction; every failure mode is funneled into NetworkError so the
    pipeline can apply the offline fallback.
    """

    def __init__(self, cache_dir: str | Path, timeout: int = 60):
        self.cache_dir = Path(cache_dir)
        self.timeout = timeout

    def __call__(self, url: str, commit: str, file: str) -> bytes:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        git = ["git", "-C", str(self.cache_dir)]
        try:
            if not (self.cache_dir / ".git").exists():
                self._git(["git", "init", "-q", str(self.cache_dir)])
                self._git(git + ["remote", "add", "origin", url], check=False)
            self._git(git + ["fetch", "--depth", "1", "origin", commit])
            cp = subprocess.run(git + ["show", f"{commit}:{file}"],
                                capture_output=True, timeout=self.timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise NetworkError(f"fetch failed: {exc}") from exc
        if cp.returncode != 0:
            raise NetworkError(
                f"git show {commit}:{file} failed: "
                f"{cp.stderr.decode('utf-8', 'replace').strip()}")
        return cp.stdout

    def _git(self, args, check: bool = True) -> None:
        cp = subprocess.run(args, capture_output=True, timeout=self.timeout)
        if check and cp.returncode != 0:
            raise NetworkError(
                f"{' '.join(args)} failed: "
                f"{cp.stderr.decode('utf-8', 'replace').strip()}")


# ==========================================================================
# The trust gate
# ==========================================================================
@dataclass
class PatchResult:
    source: str          # "cache" | "network"
    sha256: str
    path: Path


def ensure_trusted_patch(*, lock: BoreLock, series: str, patch_path: str | Path,
                         fetcher: Optional[Fetcher] = None,
                         allow_network: bool = True,
                         out: Callable[[str], None] = lambda _m: None) -> PatchResult:
    """Guarantee ``patch_path`` holds the sha256-pinned BORE patch (§8.3).

    Reuse-first (offline path): a cached patch matching the lockfile is trusted
    with no network. Otherwise fetch and verify. Never writes an unverified
    artifact; never accepts an invalid cache silently.
    """
    entry = lock.patch_for(series)          # raises TrustConfigError if absent
    patch_path = Path(patch_path)

    # Step 1 — reuse-first.
    if patch_path.exists():
        got = sha256_file(patch_path)
        if got == entry.sha256:
            out(f"trust: reusing verified cached patch for {series} (no network)")
            return PatchResult("cache", got, patch_path)
        out(f"trust: cached patch for {series} does not match bore.lock; refetching")

    # Step 2 — fetch and verify.
    if not allow_network:
        raise PatchUnavailable(
            f"offline and no valid cached patch for series {series}")
    fetcher = fetcher or GitPatchFetcher(patch_path.parent / ".bore-cache")
    try:
        data = fetcher(lock.repo_url, entry.commit or lock.pinned_commit, entry.file)
    except NetworkError as exc:
        raise PatchUnavailable(
            f"network unavailable and no valid cached patch for {series}: {exc}"
        ) from exc

    got = sha256_bytes(data)
    if got != entry.sha256:
        raise HashMismatch(
            f"BORE patch for {series} sha256 {got} != pinned {entry.sha256} "
            "(HALT_HASH_MISMATCH: possible tamper or moved file — a human must "
            "verify and re-pin bore.lock)")

    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_bytes(data)
    out(f"trust: fetched and verified patch for {series}")
    return PatchResult("network", got, patch_path)


# ==========================================================================
# Assisted pinning (§8.3a)
# ==========================================================================
# The pin stays a HUMAN act — these helpers only do the clerical work (locate
# the patch upstream, hash it, write the TOML) so "vouching" is a reviewed
# confirmation instead of a hand-edit. Nothing here is ever called by the
# update pipeline itself: only the explicit --pin-bore command (and the GUI
# button that wraps it) reaches this code, always behind a shown-proposal
# confirmation. Auto-pinning would collapse the trust model into "trust
# whatever GitHub serves today" and is deliberately not implemented.

_BORE_DIR_TPL = "patches/stable/linux-{series}-bore"
_BORE_VER_RE = re.compile(r"bore-?([0-9][\w.]*)\.patch$")


def select_bore_patch(names: list[str], *, series: str = "?",
                      subdir: str = "?") -> str:
    """Pick THE BORE patch from a series directory's .patch files.

    Real upstream layout (verified live 2026-08-18): each series dir holds
    ``0001-…bore….patch`` (the scheduler patch — the only file our pipeline
    applies and pins) plus optional numbered companions (e.g. an SMT-idle
    tweak). Rule: exactly one filename containing "bore" wins; a single file
    of any name also wins; anything else is ambiguous and defers to a manual
    pin rather than guessing about someone's trust anchor.
    """
    bore = [n for n in names if "bore" in n.rsplit("/", 1)[-1].lower()]
    if len(bore) == 1:
        return bore[0]
    if len(names) == 1:
        return names[0]
    raise PatchUnavailable(
        f"ambiguous BORE patch layout for series {series} "
        f"({len(names)} .patch files, {len(bore)} matching 'bore', in {subdir}) "
        "— pin manually (INSTALL §6.2)")


@dataclass(frozen=True)
class PinProposal:
    """Everything a human needs to see before approving a pin."""
    series: str
    repo_url: str
    commit: str          # upstream HEAD the patch was found at
    file: str
    sha256: str
    bore_version: str
    size: int


def discover_bore_patch(*, repo_url: str, series: str, cache_dir: str | Path,
                        timeout: int = 60) -> tuple[PinProposal, bytes]:
    """Locate the BORE patch for ``series`` at upstream HEAD and hash it.

    Read-only with respect to the trust anchor: returns a proposal (and the
    artifact bytes, so an approved pin can seed the cache without a refetch);
    writing anything is the caller's post-confirmation job. Raises
    PatchUnavailable when upstream publishes no patch for the series (or the
    layout is ambiguous — manual pinning per INSTALL §6.2 then applies), and
    NetworkError on fetch failure.
    """
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(cache)]

    def _git(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            cp = subprocess.run(args, capture_output=True, timeout=timeout)
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise NetworkError(f"{' '.join(args)} failed: {exc}") from exc
        if check and cp.returncode != 0:
            raise NetworkError(
                f"{' '.join(args)} failed: "
                f"{cp.stderr.decode('utf-8', 'replace').strip()}")
        return cp

    if not (cache / ".git").exists():
        _git(["git", "init", "-q", str(cache)])
        _git(git + ["remote", "add", "origin", repo_url], check=False)
    _git(git + ["fetch", "-q", "--depth", "1", "origin", "HEAD"])
    commit = _git(git + ["rev-parse", "FETCH_HEAD"]).stdout.decode().strip()

    subdir = _BORE_DIR_TPL.format(series=series)
    cp = _git(git + ["ls-tree", "-r", "--name-only", "FETCH_HEAD", subdir],
              check=False)
    names = [n for n in cp.stdout.decode("utf-8", "replace").splitlines()
             if n.endswith(".patch")]
    if not names:
        raise PatchUnavailable(
            f"upstream publishes no BORE patch for series {series} "
            f"(looked in {subdir} at {commit[:12]})")
    file = select_bore_patch(names, series=series, subdir=subdir)

    data = _git(git + ["show", f"FETCH_HEAD:{file}"]).stdout
    m = _BORE_VER_RE.search(file)
    return (PinProposal(series=series, repo_url=repo_url, commit=commit,
                        file=file, sha256=sha256_bytes(data),
                        bore_version=m.group(1) if m else "",
                        size=len(data)),
            data)


def append_pin(lock_path: str | Path, proposal: PinProposal, approved: str) -> None:
    """Append an approved [[patch]] entry to bore.lock (text-append: the
    file's human commentary survives). Refuses a duplicate series — replacing
    an existing pin (e.g. after HALT_HASH_MISMATCH) is deliberately a manual,
    eyes-on edit. Validates by re-loading; a failed append is rolled back.
    """
    p = Path(lock_path)
    lock = load_bore_lock(p)                     # validates the current file
    if proposal.series in lock.patches:
        raise TrustConfigError(
            f"series {proposal.series} is already pinned in {p} — replacing an "
            "existing pin is a manual edit (INSTALL §6.2)")

    original = p.read_text(encoding="utf-8")
    block = (
        f'\n[[patch]]\n'
        f'series       = "{proposal.series}"\n'
        f'file         = "{proposal.file}"\n'
        f'sha256       = "{proposal.sha256}"\n'
        f'bore_version = "{proposal.bore_version}"\n'
        f'commit       = "{proposal.commit}"\n'
        f'approved     = "{approved}"\n')
    p.write_text(original.rstrip("\n") + "\n" + block, encoding="utf-8")
    try:
        load_bore_lock(p)
    except TrustConfigError:
        p.write_text(original, encoding="utf-8")   # roll back a bad append
        raise
