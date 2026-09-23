"""No spec section numbers in anything a user can read.

"§4.9" is precise and useful in architecture.md, in a code comment, and in a
docstring -- all three are read by someone who has the spec open. In a runtime
message it is noise with an authoritative tone, which is worse than noise: it
implies there is a document the reader should have, without saying which one
or where. The owner's verdict, and it is the right one: "how on earth would
anyone know what those are? it makes no sense to include".

So the rule is mechanical and enforced here rather than remembered: a string
literal that can be printed, shown in a dialog, put in a tooltip or used as
argparse help text contains no section mark. Comments and docstrings are
deliberately exempt -- they exist for whoever is reading the code, and the
spec reference is genuinely the most useful thing to write there.

This is a lint, not a unit test, and that is on purpose. Sixty-six of these
had accumulated because every individual one looked harmless while being
written.
"""
import ast
import pathlib
import unittest

MARK = chr(0xA7)
ROOT = pathlib.Path(__file__).resolve().parents[2]

PY_FILES = [
    ROOT / "updater" / "cachy_void_update.py",
    ROOT / "system" / "bin" / "cachy-updater-gui",
    ROOT / "system" / "bin" / "cachy-updater-tray",
] + sorted((ROOT / "updater" / "engine").glob("*.py"))

SH_FILES = [
    ROOT / "deploy.sh",
    ROOT / "system" / "bin" / "cachy-branding",
    ROOT / "system" / "bin" / "cachy-branding-plasma",
    ROOT / "system" / "bin" / "cachy-branding-xfce",
    ROOT / "system" / "bin" / "cachy-de-detect",
    ROOT / "system" / "bin" / "cachy-de-trial",
    ROOT / "system" / "bin" / "cachy-game",
    ROOT / "system" / "sv" / "cachy-void-update" / "run",
    ROOT / "system" / "sv" / "cachy-health" / "run",
]


def _docstrings(tree):
    """ids of every string node that is a docstring (module/class/def)."""
    found = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if body and isinstance(body[0], ast.Expr) and \
                isinstance(body[0].value, ast.Constant) and \
                isinstance(body[0].value.value, str):
            found.add(id(body[0].value))
    return found


class NoSpecRefsInUserTextTests(unittest.TestCase):

    def test_no_python_runtime_string_carries_a_section_mark(self):
        offenders = []
        for path in PY_FILES:
            if not path.exists():
                continue
            src = path.read_text(encoding="utf-8")
            if MARK not in src:
                continue
            tree = ast.parse(src)
            docs = _docstrings(tree)
            for node in ast.walk(tree):
                if (isinstance(node, ast.Constant)
                        and isinstance(node.value, str)
                        and MARK in node.value
                        and id(node) not in docs):
                    offenders.append("%s:%d  %s" % (
                        path.name, node.lineno,
                        node.value.strip().replace(chr(10), " ")[:90]))
        self.assertEqual(
            offenders, [],
            "spec section numbers in runtime strings (%d) -- put them in a "
            "comment or the docs, not on the user's screen:" % len(offenders)
            + chr(10) + chr(10).join(offenders))

    def test_no_shell_output_carries_a_section_mark(self):
        # Comments are fine; a trailing `# ...` is stripped before the check.
        # Anything left is a log/warn/ok argument or heredoc body text, i.e.
        # something the installer prints.
        offenders = []
        for path in SH_FILES:
            if not path.exists():
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8")
                                     .splitlines(), 1):
                if MARK not in line:
                    continue
                code = line.split("#", 1)[0] if line.lstrip().startswith("#") \
                    else line
                if line.lstrip().startswith("#"):
                    continue
                # strip a trailing comment that begins after whitespace
                idx = code.find(" #")
                if idx != -1 and MARK not in code[:idx]:
                    continue
                offenders.append("%s:%d  %s" % (path.name, n,
                                                line.strip()[:90]))
        self.assertEqual(
            offenders, [],
            "spec section numbers in installer output (%d):" % len(offenders)
            + chr(10) + chr(10).join(offenders))

if __name__ == "__main__":
    unittest.main()
