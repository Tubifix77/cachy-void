"""XBPS query & parsing layer — architecture.md §7.1 and §7.2.

This module is the *only* place that speaks to xbps / xbps-src. It exposes:

  * pure parsing helpers (``norm``, ``split_pkgver``, ``pkgname_of``) that carry
    the §7.2 parsing contracts and can be tested in complete isolation, and
  * an :class:`Xbps` facade whose subprocess calls are injectable, so the
    command construction and output parsing can be unit-tested with mocks.

Governing rules encoded here:
  * srcpkg vs. binpkg are distinct name domains (§7.1); ``srcpkg_of`` maps a
    binpkg back to its template via the ``srcpkgs/<sub> -> <parent>`` symlink
    convention, returning ``None`` for anything that is "not ours".
  * version ordering is delegated to ``xbps-uhelper cmpver`` (§7.2); we never
    reimplement XBPS version semantics.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Sequence


class XbpsError(RuntimeError):
    """A queried command failed unexpectedly."""


class ParseError(XbpsError):
    """A command produced output that violates its §7.2 contract (exit 30)."""


# --------------------------------------------------------------------------
# Pure parsing helpers (§7.2 NORM and pkgver contracts)
# --------------------------------------------------------------------------
_CONSTRAINT_RE = re.compile(r"[<>=].*$")           # strip ">=1.0", "<2", "=3_1"
_PKGVER_SUFFIX_RE = re.compile(r"-[^-]+_[0-9]+$")  # strip trailing "-1.2.3_4"
_VIRTUAL_PREFIX = "virtual?"


def norm(token: str) -> str:
    """Normalize a dependency token to a bare package name (§7.2 NORM).

    first whitespace field -> strip a leading ``virtual?`` -> strip a trailing
    version constraint -> strip a trailing ``-<version>_<revision>`` suffix.
    """
    stripped = token.strip()
    if not stripped:
        raise ParseError("empty dependency token")
    head = stripped.split()[0]
    if head.startswith(_VIRTUAL_PREFIX):
        head = head[len(_VIRTUAL_PREFIX):]
    head = _CONSTRAINT_RE.sub("", head)
    head = _PKGVER_SUFFIX_RE.sub("", head)
    if not head:
        raise ParseError(f"token {token!r} normalized to empty")
    return head


def split_pkgver(name_ver: str) -> tuple[str, str]:
    """Split ``name-<version>_<revision>`` into ``(pkgname, version_revision)``.

    pkgnames may contain hyphens; the version never does, so we split on the
    final hyphen and require the right side to look like ``<version>_<revision>``.
    """
    token = name_ver.strip()
    idx = token.rfind("-")
    if idx <= 0:
        raise ParseError(f"cannot split pkgver from {name_ver!r}")
    name, ver = token[:idx], token[idx + 1:]
    if not name or "_" not in ver:
        raise ParseError(f"malformed pkgver {name_ver!r}")
    return name, ver


def pkgname_of(name_ver: str) -> str:
    """Bare pkgname from a ``name-ver_rev`` string."""
    return split_pkgver(name_ver)[0]


# --------------------------------------------------------------------------
# Subprocess result contract (kept tiny so tests can fake it trivially)
# --------------------------------------------------------------------------
Runner = Callable[[Sequence[str], Optional[str]], "subprocess.CompletedProcess"]


def _default_runner(args: Sequence[str], cwd: Optional[str]) -> "subprocess.CompletedProcess":
    return subprocess.run(list(args), cwd=cwd, capture_output=True, text=True)


@dataclass
class Xbps:
    """Facade over xbps / xbps-src (§7.2 live-query primitives).

    :param void_packages: path to the void-packages checkout.
    :param repos: ordered local repo roots ``R`` (binpkgs, binpkgs/nonfree).
    :param run: injectable ``(args, cwd) -> CompletedProcess`` for testing.
    """

    void_packages: Path
    repos: Sequence[Path] = field(default_factory=list)
    run: Runner = _default_runner

    def __post_init__(self) -> None:
        self.void_packages = Path(self.void_packages)
        self.srcpkgs_dir = self.void_packages / "srcpkgs"
        self.repos = [Path(r) for r in self.repos]

    # -- low level -------------------------------------------------------
    def _capture(self, args: Sequence[str], cwd: Optional[str] = None,
                 check: bool = True) -> "subprocess.CompletedProcess":
        cp = self.run(args, cwd)
        if check and cp.returncode != 0:
            raise XbpsError(f"command failed ({cp.returncode}): {' '.join(args)}\n{cp.stderr}")
        return cp

    def _xbps_src(self, *args: str, check: bool = True) -> "subprocess.CompletedProcess":
        return self._capture(["./xbps-src", *args], cwd=str(self.void_packages), check=check)

    # -- name-domain mapping (§7.1) -------------------------------------
    def srcpkg_of(self, binpkg: str) -> Optional[str]:
        """Map an installed binpkg back to its template srcpkg, or ``None``."""
        p = self.srcpkgs_dir / binpkg
        if p.is_symlink():
            return Path(os.readlink(p)).name
        if p.is_dir():
            return binpkg
        return None

    # -- installed system (binpkg domain) ------------------------------
    def installed(self) -> list[str]:
        """Installed binpkg names from ``xbps-query -l`` (field 2 = name-ver)."""
        cp = self._capture(["xbps-query", "-l"])
        names: list[str] = []
        for line in cp.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split()
            if len(parts) < 2:
                raise ParseError(f"unparseable xbps-query -l line: {line!r}")
            names.append(pkgname_of(parts[1]))
        return names

    def inst_pkgver(self, binpkg: str) -> str:
        """Installed full pkgver of a binpkg (``xbps-query -p pkgver``)."""
        return self._capture(["xbps-query", "-p", "pkgver", binpkg]).stdout.strip()

    def origin(self, binpkg: str) -> str:
        """Repository an installed binpkg came from (``-p repository``)."""
        return self._capture(["xbps-query", "-p", "repository", binpkg]).stdout.strip()

    # -- repository queries (srcpkg/binpkg by repo) --------------------
    def repo_ver(self, name: str) -> Optional[str]:
        """First non-empty pkgver of ``name`` across local repos ``R``, else None.

        Real-hardware finding (Medion kickoff): without ``-R`` xbps-query answers
        from the INSTALLED pkgdb and silently ignores ``--repository``, making
        every installed package look locally built (false O-term, dead M-term).
        ``-R`` selects repository mode; ``-i`` ignores configured repos so ONLY
        the given local root is consulted.
        """
        for repo in self.repos:
            cp = self._capture(
                ["xbps-query", "-R", "-i", f"--repository={repo}",
                 "-p", "pkgver", name],
                check=False,
            )
            ver = cp.stdout.strip()
            if cp.returncode == 0 and ver:
                return ver
        return None

    def seed_exists(self, name: str) -> bool:
        """True if a binary for ``name`` exists locally or on a mirror (§7.4 seed)."""
        if self.repo_ver(name) is not None:
            return True
        cp = self._capture(["xbps-query", "-R", "-p", "pkgver", name], check=False)
        return cp.returncode == 0 and bool(cp.stdout.strip())

    # -- xbps-src build-side queries (srcpkg domain) -------------------
    def show_local_updates(self) -> list[str]:
        """Outdated local templates: NORM of field 1 per line (``L``)."""
        cp = self._xbps_src("show-local-updates")
        out: list[str] = []
        for line in cp.stdout.splitlines():
            if line.strip():
                out.append(norm(line))
        return out

    def show_build_deps(self, srcpkg: str) -> list[str]:
        """Build dependencies of a template, one NORM'd name per line."""
        cp = self._xbps_src("show-build-deps", srcpkg)
        return [norm(line) for line in cp.stdout.splitlines() if line.strip()]

    def sort_dependencies(self, srcpkgs: Sequence[str]) -> tuple[list[str], bool]:
        """Topologically sort ``srcpkgs``; returns (ordered_lines, ok).

        ``ok`` is True only when the command exited 0. The caller still verifies
        the result is a permutation of the input before trusting it (§7.4).
        """
        if not srcpkgs:
            return [], True
        cp = self._xbps_src("sort-dependencies", *srcpkgs, check=False)
        lines = [ln.strip() for ln in cp.stdout.splitlines() if ln.strip()]
        return lines, cp.returncode == 0

    def build(self, srcpkg: str, jobs: int = 1, log_path: Optional[str] = None) -> int:
        """Build one template (``./xbps-src -jN pkg``); returns its exit code.

        Combined output is written to ``log_path`` when given, so the caller can
        emit the tail on failure (§7.5).
        """
        cp = self.run(["./xbps-src", f"-j{jobs}", "pkg", srcpkg], str(self.void_packages))
        if log_path is not None:
            with open(log_path, "w", encoding="utf-8") as fh:
                fh.write(cp.stdout or "")
                fh.write(cp.stderr or "")
        return cp.returncode

    def clean(self, srcpkg: str) -> None:
        """Purge a template's stale work directory (idempotent, §7.5)."""
        self._xbps_src("clean", srcpkg, check=False)

    def configure(self, srcpkg: str) -> int:
        """Run template phases through configure (fetch/extract/patch/configure).

        Used by the G2 gate (§8.5) to generate the kernel .config without
        paying for a full compile. Returns the exit code.
        """
        return self._xbps_src("configure", srcpkg, check=False).returncode

    def vercmp(self, a: str, b: str) -> int:
        """Return -1/0/1 for a<b / a==b / a>b via ``xbps-uhelper cmpver``.

        xbps-uhelper cmpver exit convention: 0 == equal, 1 == a>b, 255 == a<b.
        """
        cp = self._capture(["xbps-uhelper", "cmpver", a, b], check=False)
        rc = cp.returncode
        if rc == 0:
            return 0
        if rc == 1:
            return 1
        if rc in (255, -1):
            return -1
        raise XbpsError(f"unexpected cmpver exit {rc} for {a!r} vs {b!r}")
