"""Dynamic Dependency Resolution Engine — architecture.md §7.3 and §7.4.

Pure logic over an injected :class:`~engine.xbps.Xbps` (or any object with the
same query surface), so the queue algebra and ordering can be exercised entirely
with mocked xbps output.
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Protocol, Optional, Sequence

from .xbps import norm

# "mesa-dri-24.0_1" -> "24.0_1"; a bare "24.0_1" passes through unchanged.
# pkgnames may contain hyphens; the version part never does.
_PKGVER_TAIL = re.compile(r"-([^-]+_[0-9]+)$")


def _version_of(pkgver: str) -> str:
    """Reduce a full ``name-version_revision`` to the bare version.

    Comparing *full* pkgvers of different binpkgs (e.g. a subpackage vs its
    template's repo entry) through ``cmpver`` would compare name components as
    version components; every comparison in the queue algebra therefore goes
    through this normalizer first.
    """
    m = _PKGVER_TAIL.search(pkgver.strip())
    return m.group(1) if m else pkgver.strip()


class CycleError(RuntimeError):
    """A cycle group has no binary seed to bootstrap from (exit 32)."""


class MappingError(RuntimeError):
    """A hard srcpkg-mapping anomaly on a managed path (exit 33)."""


class XbpsLike(Protocol):
    def installed(self) -> list[str]: ...
    def srcpkg_of(self, binpkg: str) -> Optional[str]: ...
    def inst_pkgver(self, binpkg: str) -> str: ...
    def origin(self, binpkg: str) -> str: ...
    def repo_ver(self, name: str) -> Optional[str]: ...
    def seed_exists(self, name: str) -> bool: ...
    def show_local_updates(self) -> list[str]: ...
    def show_build_deps(self, srcpkg: str) -> list[str]: ...
    def sort_dependencies(self, srcpkgs: Sequence[str]) -> tuple[list[str], bool]: ...
    def vercmp(self, a: str, b: str) -> int: ...


@dataclass
class QueuePlan:
    """Result of §7.3 queue construction."""
    q_build: list[str]    # what to compile, ordered later by topo_order
    q_deploy: list[str]   # superset that must be (re)installed in Stage 4


@dataclass
class BuildOrder:
    """Result of §7.4 ordering."""
    order: list[str]         # full build order, dependencies first
    second_pass: list[str]   # cycle-group members to rebuild exactly once more
    provenance: str          # "sorter" | "fallback"


# --------------------------------------------------------------------------
# §7.3 Queue algebra
# --------------------------------------------------------------------------
def build_queue(xbps: XbpsLike, targets: Sequence[str],
                blacklist: Sequence[str],
                local_repos: Sequence[str] = (),
                always_build: Sequence[str] = ()) -> QueuePlan:
    """Compute (Q_build, Q_deploy) from live queries (§7.3).

        S(I)         = { srcpkg_of(b) : b installed } - {None}
        inst_vers(t) = versions of installed binpkgs mapping to srcpkg t
        L = NORM(show-local-updates)
        M = targets with no repo binary anywhere (first build)
        P = installed targets whose repo binary is newer than installed,
            or whose subpackages are at divergent versions (orphan recovery)
        O = installed targets already at the repo version but still
            originating from a non-overlay repo (interrupted §4.6 takeover)
        K = always_build members with L|M evidence, EXEMPT from the S(I) gate
        Q_build  = (((L | M) & S(I) & targets) - blacklist) | K
        Q_deploy = Q_build | (((P | O) & S(I)) - blacklist)

    ``local_repos`` is the overlay repo root set ``R``; without it the O term
    is inert (origin cannot be classified), which callers other than the CLI
    may use for pure build-planning.

    ``always_build`` is the §7.3 K-exemption: packages the overlay *introduces*
    rather than takes over (linux-cachy) bypass the installed-gate — otherwise
    the first kernel could never enter the queue (real-hardware finding: the
    integration fixture had pre-installed the kernel, masking this hole). The
    no-widen rule stays absolute for everything else; the deliberate first
    install happens in Stage 4 (§8.6, with -headers per §2.5).
    """
    tset = set(targets) - set(blacklist)     # blacklist beats allowlist
    black = set(blacklist)
    repo_set = {str(r) for r in local_repos}

    inst_vers: dict[str, set[str]] = defaultdict(set)
    inst_bins: dict[str, list[str]] = defaultdict(list)
    for b in xbps.installed():
        s = xbps.srcpkg_of(b)
        if s is None:
            continue
        inst_vers[s].add(_version_of(xbps.inst_pkgver(b)))
        inst_bins[s].append(b)
    s_i = set(inst_vers)

    L = {norm(x) for x in xbps.show_local_updates()}
    M = {t for t in tset if xbps.repo_ver(t) is None}

    P: set[str] = set()
    O: set[str] = set()
    for t in tset & s_i:
        rv_full = xbps.repo_ver(t)
        if rv_full is None:
            continue
        rv = _version_of(rv_full)
        vers = inst_vers[t]
        if len(vers) > 1:                    # subpackages diverged
            P.add(t)
            continue
        current = max(vers, key=cmp_to_key(xbps.vercmp))
        cmp = xbps.vercmp(current, rv)
        if cmp < 0:                          # built but never deployed
            P.add(t)
        elif cmp == 0 and repo_set and any(
                xbps.origin(b) not in repo_set for b in inst_bins[t]):
            O.add(t)                         # takeover never completed

    K = {t for t in (set(always_build) & tset) if t in L or t in M}
    q_build = (((L | M) & s_i & tset) - black) | K
    q_deploy = q_build | (((P | O) & s_i) - black)
    return QueuePlan(sorted(q_build), sorted(q_deploy))


# --------------------------------------------------------------------------
# §7.4 Ordering: verified sorter, then restricted-graph fallback
# --------------------------------------------------------------------------
def topo_order(xbps: XbpsLike, q_build: Sequence[str]) -> BuildOrder:
    """Order Q_build for building; fall back to a hand-built graph if the
    external sorter cannot be trusted."""
    q = sorted(set(q_build))
    if not q:
        return BuildOrder([], [], "sorter")

    lines, ok = xbps.sort_dependencies(q)
    # Trust the sorter only if it returned an exact permutation of the input.
    if ok and len(lines) == len(q) and set(lines) == set(q):
        return BuildOrder(lines, [], "sorter")

    return _fallback_order(xbps, q)


def _fallback_order(xbps: XbpsLike, q: list[str]) -> BuildOrder:
    qset = set(q)
    # adjacency: edge u -> v means "u must be built before v"
    adj: dict[str, set[str]] = {s: set() for s in q}
    for s in q:
        for dep in xbps.show_build_deps(s):
            d = norm(dep)
            dsrc = xbps.srcpkg_of(d) or d      # normalize dep to its srcpkg
            if dsrc in qset:
                adj[dsrc].add(s)               # dep before dependent

    sccs = _tarjan_scc(q, adj)                 # each SCC as a list of nodes

    # Seed rule: every cyclic group needs at least one binary seed (§7.4).
    for scc in sccs:
        if _is_cycle(scc, adj):
            if not any(xbps.seed_exists(m) for m in scc):
                raise CycleError(f"cycle group {sorted(scc)} has no binary seed")

    order, second = _condensation_order(q, adj, sccs)
    return BuildOrder(order, second, "fallback")


def _is_cycle(scc: list[str], adj: dict[str, set[str]]) -> bool:
    if len(scc) > 1:
        return True
    node = scc[0]
    return node in adj[node]        # self-loop


def _tarjan_scc(nodes: list[str], adj: dict[str, set[str]]) -> list[list[str]]:
    """Tarjan's SCC. Returns SCCs; node order within each is unspecified
    (callers sort). Iterative to avoid recursion limits on deep graphs."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    sccs: list[list[str]] = []
    counter = 0

    # Deterministic outer iteration.
    for root in sorted(nodes):
        if root in index:
            continue
        work: list[tuple[str, list[str]]] = [(root, sorted(adj[root]))]
        while work:
            node, succ = work[-1]
            if node not in index:
                index[node] = low[node] = counter
                counter += 1
                stack.append(node)
                on_stack.add(node)
            advanced = False
            while succ:
                w = succ[0]
                if w not in index:
                    work.append((w, sorted(adj[w])))
                    succ.pop(0)
                    advanced = True
                    break
                if w in on_stack:
                    low[node] = min(low[node], index[w])
                succ.pop(0)
            if advanced:
                continue
            if low[node] == index[node]:
                comp: list[str] = []
                while True:
                    w = stack.pop()
                    on_stack.discard(w)
                    comp.append(w)
                    if w == node:
                        break
                sccs.append(comp)
            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
    return sccs


def _condensation_order(nodes: list[str], adj: dict[str, set[str]],
                        sccs: list[list[str]]) -> tuple[list[str], list[str]]:
    """Kahn topological sort over the SCC condensation with lexicographic
    tie-break, expanding each SCC to its members (sorted). Returns
    (full_order, second_pass) where second_pass is the cycle-group members."""
    comp_id: dict[str, int] = {}
    for i, scc in enumerate(sccs):
        for n in scc:
            comp_id[n] = i

    cedges: dict[int, set[int]] = defaultdict(set)
    cindeg: dict[int, int] = {i: 0 for i in range(len(sccs))}
    for u in nodes:
        for v in adj[u]:
            cu, cv = comp_id[u], comp_id[v]
            if cu != cv and cv not in cedges[cu]:
                cedges[cu].add(cv)
                cindeg[cv] += 1

    # Represent each component by its lexicographically smallest member for
    # deterministic tie-breaking in the priority frontier.
    rep = {i: min(sccs[i]) for i in range(len(sccs))}
    frontier = sorted((i for i in range(len(sccs)) if cindeg[i] == 0),
                      key=lambda i: rep[i])

    order: list[str] = []
    second: list[str] = []
    while frontier:
        c = frontier.pop(0)
        members = sorted(sccs[c])
        order.extend(members)
        if _is_cycle(sccs[c], adj):
            second.extend(members)          # one extra convergence pass (§7.4)
        for d in sorted(cedges[c], key=lambda i: rep[i]):
            cindeg[d] -= 1
            if cindeg[d] == 0:
                # insert keeping the frontier lexicographically ordered
                frontier.append(d)
                frontier.sort(key=lambda i: rep[i])
    return order, second
