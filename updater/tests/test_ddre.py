"""Unit tests for the Dependency Resolution Engine (architecture.md §7.3, §7.4).

Everything runs against a FakeXbps stub — no subprocess, no repo, no filesystem —
so the queue algebra, topological ordering, cycle handling and orphan-hole
recovery are validated purely as logic.
"""
import unittest

from engine.ddre import build_queue, topo_order, CycleError


def _vercmp(a: str, b: str) -> int:
    """Simple numeric version compare for test fixtures ('1.0_1' style)."""
    def key(v):
        ver, _, rev = v.partition("_")
        return (tuple(int(x) for x in ver.split(".")), int(rev or 0))
    ka, kb = key(a), key(b)
    return (ka > kb) - (ka < kb)


class FakeXbps:
    """Canned stand-in for engine.xbps.Xbps used by the DDRE."""

    def __init__(self, *, installed=(), src_map=None, inst_ver=None,
                 repo_ver=None, local_updates=(), build_deps=None,
                 sort_result=None, seeds=(), origins=None):
        self._installed = list(installed)
        self._src_map = dict(src_map or {})
        self._inst_ver = dict(inst_ver or {})
        self._repo_ver = dict(repo_ver or {})
        self._local_updates = list(local_updates)
        self._build_deps = dict(build_deps or {})
        # sort_result: callable(list)->(list,bool), or None for "trustworthy"
        self._sort_result = sort_result
        self._seeds = set(seeds)
        self._origins = dict(origins or {})

    def installed(self):
        return list(self._installed)

    def srcpkg_of(self, b):
        return self._src_map.get(b)

    def inst_pkgver(self, b):
        return self._inst_ver[b]

    def origin(self, b):
        return self._origins.get(b, "/local/overlay/binpkgs")

    def repo_ver(self, name):
        return self._repo_ver.get(name)

    def seed_exists(self, name):
        return name in self._seeds

    def show_local_updates(self):
        return list(self._local_updates)

    def show_build_deps(self, s):
        return list(self._build_deps.get(s, []))

    def sort_dependencies(self, pkgs):
        pkgs = list(pkgs)
        if self._sort_result is None:
            return sorted(pkgs), True          # a valid permutation
        return self._sort_result(pkgs)

    def vercmp(self, a, b):
        return _vercmp(a, b)


# --------------------------------------------------------------------------
# §7.3 Queue algebra
# --------------------------------------------------------------------------
class QueueAlgebraTests(unittest.TestCase):

    def test_L_outdated_installed_builds(self):
        xb = FakeXbps(
            installed=["mesa", "wine"],
            src_map={"mesa": "mesa", "wine": "wine"},
            inst_ver={"mesa": "1.0_1", "wine": "1.0_1"},
            repo_ver={"mesa": "1.0_1", "wine": "1.0_1"},
            local_updates=["mesa"],           # template newer than local repo
        )
        plan = build_queue(xb, targets=["mesa", "wine"], blacklist=[])
        self.assertEqual(plan.q_build, ["mesa"])
        self.assertEqual(plan.q_deploy, ["mesa"])

    def test_M_first_build_when_no_local_binary(self):
        # Fresh repo: show-local-updates empty, no repo binary yet -> M term.
        xb = FakeXbps(
            installed=["mesa"],
            src_map={"mesa": "mesa"},
            inst_ver={"mesa": "1.0_1"},
            repo_ver={},                       # repo_ver(mesa) -> None
            local_updates=[],
        )
        plan = build_queue(xb, targets=["mesa"], blacklist=[])
        self.assertEqual(plan.q_build, ["mesa"])

    def test_blacklist_beats_allowlist(self):
        xb = FakeXbps(
            installed=["glibc", "mesa"],
            src_map={"glibc": "glibc", "mesa": "mesa"},
            inst_ver={"glibc": "2.0_1", "mesa": "1.0_1"},
            repo_ver={"glibc": "2.0_1", "mesa": "1.0_1"},
            local_updates=["glibc", "mesa"],
        )
        plan = build_queue(xb, targets=["glibc", "mesa"], blacklist=["glibc"])
        self.assertNotIn("glibc", plan.q_build)
        self.assertEqual(plan.q_build, ["mesa"])

    def test_uninstalled_target_excluded(self):
        # foo is a target and outdated, but not installed -> not in S(I).
        xb = FakeXbps(
            installed=["mesa"],
            src_map={"mesa": "mesa"},
            inst_ver={"mesa": "1.0_1"},
            repo_ver={"mesa": "1.0_1", "foo": "1.0_1"},
            local_updates=["foo", "mesa"],
        )
        plan = build_queue(xb, targets=["foo", "mesa"], blacklist=[])
        self.assertEqual(plan.q_build, ["mesa"])

    def test_subpackage_maps_to_template(self):
        # Only mesa-dri / mesa-dev are installed; both map to srcpkg 'mesa'.
        xb = FakeXbps(
            installed=["mesa-dri", "mesa-dev"],
            src_map={"mesa-dri": "mesa", "mesa-dev": "mesa"},
            inst_ver={"mesa-dri": "1.0_1", "mesa-dev": "1.0_1"},
            repo_ver={"mesa": "1.0_1"},
            local_updates=["mesa"],
        )
        plan = build_queue(xb, targets=["mesa"], blacklist=[])
        self.assertEqual(plan.q_build, ["mesa"])   # proves binpkg->srcpkg mapping

    def test_P_orphan_hole_recovered_in_deploy_not_build(self):
        # Built in a prior run but never deployed: repo binary newer than the
        # installed version, yet absent from L (repo matches template) and M
        # (a binary exists). Must appear in Q_deploy but NOT Q_build.
        xb = FakeXbps(
            installed=["gamemode"],
            src_map={"gamemode": "gamemode"},
            inst_ver={"gamemode": "1.0_1"},
            repo_ver={"gamemode": "1.1_1"},    # newer local binary awaiting install
            local_updates=[],
        )
        plan = build_queue(xb, targets=["gamemode"], blacklist=[])
        self.assertEqual(plan.q_build, [])
        self.assertEqual(plan.q_deploy, ["gamemode"])

    def test_P_diverged_subpackages_recovered(self):
        # Subpackages of one template installed at different versions -> P.
        xb = FakeXbps(
            installed=["pipewire", "pipewire-devel"],
            src_map={"pipewire": "pipewire", "pipewire-devel": "pipewire"},
            inst_ver={"pipewire": "1.0_1", "pipewire-devel": "1.0_2"},
            repo_ver={"pipewire": "1.0_2"},
            local_updates=[],
        )
        plan = build_queue(xb, targets=["pipewire"], blacklist=[])
        self.assertIn("pipewire", plan.q_deploy)

    # ---- O term: interrupted §4.6 takeover (regression for the audit F4) ----

    def test_O_orphaned_takeover_recovered_deploy_only(self):
        # Crash between -Su and the -f takeover: version equal to the local
        # repo, origin still upstream. Invisible to L/M/P; O must catch it.
        xb = FakeXbps(
            installed=["gamemode"],
            src_map={"gamemode": "gamemode"},
            inst_ver={"gamemode": "1.0_1"},
            repo_ver={"gamemode": "1.0_1"},
            local_updates=[],
            origins={"gamemode": "https://repo-default.voidlinux.org/current"},
        )
        plan = build_queue(xb, targets=["gamemode"], blacklist=[],
                           local_repos=["/vp/hostdir/binpkgs"])
        self.assertEqual(plan.q_build, [])                 # nothing to compile
        self.assertEqual(plan.q_deploy, ["gamemode"])      # takeover resumes

    def test_O_quiet_when_origin_is_overlay(self):
        xb = FakeXbps(
            installed=["gamemode"],
            src_map={"gamemode": "gamemode"},
            inst_ver={"gamemode": "1.0_1"},
            repo_ver={"gamemode": "1.0_1"},
            local_updates=[],
            origins={"gamemode": "/vp/hostdir/binpkgs"},   # takeover done
        )
        plan = build_queue(xb, targets=["gamemode"], blacklist=[],
                           local_repos=["/vp/hostdir/binpkgs"])
        self.assertEqual(plan.q_deploy, [])

    def test_O_inert_without_local_repos(self):
        # Callers that cannot classify origins get pure build planning.
        xb = FakeXbps(
            installed=["gamemode"],
            src_map={"gamemode": "gamemode"},
            inst_ver={"gamemode": "1.0_1"},
            repo_ver={"gamemode": "1.0_1"},
            local_updates=[],
            origins={"gamemode": "https://upstream"},
        )
        plan = build_queue(xb, targets=["gamemode"], blacklist=[])
        self.assertEqual(plan.q_deploy, [])

    def test_K_exemption_queues_uninstalled_kernel(self):
        # linux-cachy is INTRODUCED, not taken over: with always_build it must
        # enter the queue despite not being installed (regression: first real
        # kernel run queued nothing because S(I) gated it out).
        xb = FakeXbps(
            installed=["mesa"], src_map={"mesa": "mesa"},
            inst_ver={"mesa": "1.0_1"}, repo_ver={"mesa": "1.0_1"},
            local_updates=[],                      # kernel comes via M-term
        )
        plan = build_queue(xb, targets=["linux-cachy", "mesa"], blacklist=[],
                           always_build=["linux-cachy"])
        self.assertEqual(plan.q_build, ["linux-cachy"])
        self.assertEqual(plan.q_deploy, ["linux-cachy"])
        # without the exemption the gate holds:
        plan2 = build_queue(xb, targets=["linux-cachy", "mesa"], blacklist=[])
        self.assertEqual(plan2.q_build, [])

    def test_full_pkgver_forms_are_normalized_before_vercmp(self):
        # The real facade returns FULL pkgvers ("mesa-dri-1.0_1"); subpackage
        # name components must never enter version comparison.
        xb = FakeXbps(
            installed=["mesa-dri"],
            src_map={"mesa-dri": "mesa"},
            inst_ver={"mesa-dri": "mesa-dri-1.0_1"},
            repo_ver={"mesa": "mesa-1.1_1"},
            local_updates=[],
        )
        plan = build_queue(xb, targets=["mesa"], blacklist=[])
        self.assertEqual(plan.q_deploy, ["mesa"])          # P: 1.0_1 < 1.1_1


# --------------------------------------------------------------------------
# §7.4 Topological ordering
# --------------------------------------------------------------------------
class OrderingTests(unittest.TestCase):

    def test_trustworthy_sorter_used(self):
        xb = FakeXbps(sort_result=lambda p: (["b", "a", "c"], True))
        bo = topo_order(xb, ["a", "b", "c"])
        self.assertEqual(bo.provenance, "sorter")
        self.assertEqual(bo.order, ["b", "a", "c"])
        self.assertEqual(bo.second_pass, [])

    def test_sorter_dropping_a_node_triggers_fallback(self):
        # Sorter returns fewer nodes than given -> not a permutation -> fallback.
        xb = FakeXbps(
            sort_result=lambda p: (["a", "b"], True),   # dropped "c"
            build_deps={"a": [], "b": [], "c": []},
        )
        bo = topo_order(xb, ["a", "b", "c"])
        self.assertEqual(bo.provenance, "fallback")
        self.assertEqual(set(bo.order), {"a", "b", "c"})

    def test_fallback_orders_dependency_first(self):
        xb = FakeXbps(
            sort_result=lambda p: ([], False),          # force fallback
            build_deps={"a": ["b"], "b": []},           # a build-deps on b
        )
        bo = topo_order(xb, ["a", "b"])
        self.assertEqual(bo.provenance, "fallback")
        self.assertEqual(bo.order, ["b", "a"])          # b before a
        self.assertEqual(bo.second_pass, [])

    def test_cycle_with_seed_builds_with_second_pass(self):
        xb = FakeXbps(
            sort_result=lambda p: ([], False),
            build_deps={"a": ["b"], "b": ["a"]},        # a <-> b cycle
            seeds=["a"],                                # one binary seed exists
        )
        bo = topo_order(xb, ["a", "b"])
        self.assertEqual(set(bo.order), {"a", "b"})
        self.assertEqual(sorted(bo.second_pass), ["a", "b"])   # convergence pass

    def test_cycle_without_seed_raises(self):
        xb = FakeXbps(
            sort_result=lambda p: ([], False),
            build_deps={"a": ["b"], "b": ["a"]},
            seeds=[],                                   # no seed anywhere
        )
        with self.assertRaises(CycleError):
            topo_order(xb, ["a", "b"])

    def test_self_loop_is_a_cycle_group(self):
        xb = FakeXbps(
            sort_result=lambda p: ([], False),
            build_deps={"a": ["a"], "b": []},           # a self-depends
            seeds=["a"],
        )
        bo = topo_order(xb, ["a", "b"])
        self.assertEqual(bo.second_pass, ["a"])         # only the cyclic node
        self.assertEqual(set(bo.order), {"a", "b"})


if __name__ == "__main__":
    unittest.main()
