"""CHR-03: text files are written with LF only, even on Windows; a CRLF from a client is converted before saving."""
import ast
import tempfile
import unittest
from pathlib import Path

from . import test_core as fixtures
from kbsys import config, db, discussions

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_FILES = {"refmanager.py"}  # self-test fixtures written into temporary folders only


def _text_writes_without_newline(path):
    """Lines of write_text / open(.., "w"|"a"|"x") / os.fdopen(.., "w") calls that do not set newline=."""
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or any(k.arg == "newline" for k in node.keywords):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "write_text":
            yield node.lineno
        elif name in ("open", "fdopen"):
            args = [a.value for a in node.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]
            args += [k.value.value for k in node.keywords if k.arg == "mode" and isinstance(k.value, ast.Constant)]
            if any(a and set(a) <= set("wax+t") for a in args):
                yield node.lineno


class LineEndingTests(unittest.TestCase):
    def test_store_writers_write_no_carriage_return(self):
        with tempfile.TemporaryDirectory() as folder:
            one, many = Path(folder) / "a.json", Path(folder) / "b.jsonl"
            db.write_json(one, {"a": 1, "b": ["x"]})
            db.write_jsonl(many, [{"a": 1}, {"b": 2}])
            for path in (one, many):
                self.assertGreater(len(path.read_bytes()), 2)
                self.assertNotIn(b"\r", path.read_bytes(), path.name)

    def test_client_crlf_and_cr_become_lf(self):
        self.assertEqual(config.lf("a\r\nb\rc\nd"), "a\nb\nc\nd")

    def test_no_text_writer_in_the_project_leaves_line_endings_to_the_platform(self):
        bad = []
        for folder in ("scripts", "tools", ".claude"):
            for path in sorted((ROOT / folder).rglob("*.py")):
                if path.name not in FIXTURE_FILES:
                    bad += [f"{path.relative_to(ROOT).as_posix()}:{n}" for n in _text_writes_without_newline(path)]
        self.assertEqual(bad, [], "text writers must set newline to LF (CHR-03)")


class SavedNoteTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def test_saved_discussion_from_a_windows_client_has_no_carriage_return(self):
        saved = discussions.save("one", "CRLF talk", "line one\r\nline two\rline three")["saved"]
        data = (config.vault_dir("one") / saved).read_bytes()
        self.assertNotIn(b"\r", data)
        self.assertIn(b"line one\nline two\nline three", data)


if __name__ == "__main__":
    unittest.main()
