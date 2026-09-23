"""Make every test module importable from any working directory.

Fifteen of the twenty test modules do `from engine.xbps import ...` and have
no path bootstrap of their own, so they can only be imported when `updater/`
is already on `sys.path` -- which, before this file, meant "when pytest or
unittest happened to be started from inside `updater/`".

A full-suite run from the repository root nevertheless PASSED, which is worse
than failing: five modules do bootstrap the path themselves, alphabetical
collection imports `test_build_log_stream.py` early, and from that moment
`updater/` is on `sys.path` for everyone. The suite was green by accident of
file naming. Running one of the fifteen on its own from the root still failed
outright:

    $ python3 -m pytest updater/tests/test_xbps.py -q
    E   ModuleNotFoundError: No module named 'engine'

pytest imports this file before collecting anything beside it, so the path is
correct for every module, in any order, run alone or together, from any
directory. `python -m unittest` does not read conftest.py. From inside `updater/` it
works unaided; from the repo root, put `updater` on the path explicitly
(`-t updater` does NOT work -- it asks unittest to treat `tests` as a package
and it has no `__init__.py`):

    cd updater && python3 -m unittest discover -s tests
    PYTHONPATH=updater python3 -m unittest discover -s updater/tests
"""
import pathlib
import sys

UPDATER = pathlib.Path(__file__).resolve().parents[1]
if str(UPDATER) not in sys.path:
    sys.path.insert(0, str(UPDATER))
