"""The suite must run in a bare container, and say so honestly when it cannot.

Two regressions this prevents, both found by a fresh session in a cloud
container that could not run the tests and could not tell why:

* **An unguarded PyQt5 import aborts EVERYTHING.** Not one module -- pytest
  stops at collection ("Interrupted: 1 error during collection"), so a
  container merely missing an optional GUI package looks like a broken test
  suite. `test_gui.py` had always guarded its import; `test_tray.py` never
  did, and neither did the tray half of `test_nightly_visibility.py`.
* **Importing `engine` only worked from inside `updater/`.** Fifteen of the
  twenty modules rely on it being on `sys.path`, which `conftest.py` now
  guarantees. Before that the full suite passed from the repository root by
  ACCIDENT -- five modules bootstrap the path themselves and alphabetical
  collection ran one of them early -- while any of the fifteen run alone
  failed outright. Green-by-coincidence is worse than red.
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
TESTS = ROOT / "updater" / "tests"


def _module_level_names(tree):
    """Names imported at module scope (not inside try/def/class)."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(node)
    return out


class SuiteRunsInABareContainerTests(unittest.TestCase):

    def test_no_test_module_imports_pyqt5_unguarded(self):
        # Guarded means inside a try/except ImportError, so the module can
        # decide to skip. At module scope it is fatal to the whole run.
        offenders = []
        for path in sorted(TESTS.glob("test_*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in _module_level_names(tree):
                mod = (getattr(node, "module", None) or "")
                names = [a.name for a in node.names]
                if "PyQt5" in mod or any(n.startswith("PyQt5") for n in names):
                    offenders.append("%s:%d" % (path.name, node.lineno))
        self.assertEqual(
            offenders, [],
            "PyQt5 imported at module scope without a try/except ImportError; "
            "pytest aborts the ENTIRE run at collection when it is missing: "
            + ", ".join(offenders))

    def test_conftest_puts_updater_on_the_path(self):
        # The one file that makes `from engine.xbps import ...` work from any
        # directory. Losing it returns the suite to green-by-coincidence.
        conftest = TESTS / "conftest.py"
        self.assertTrue(conftest.is_file(), "updater/tests/conftest.py is gone")
        src = conftest.read_text(encoding="utf-8")
        self.assertIn("sys.path.insert", src)
        self.assertIn("parents[1]", src)

    def test_every_module_can_find_engine_without_help(self):
        # Belt and braces for the above: with only the repo root on sys.path,
        # conftest's insert is what makes these resolve.
        import engine  # noqa: F401  - imported via conftest's path insert
        import cachy_void_update  # noqa: F401

if __name__ == "__main__":
    unittest.main()
