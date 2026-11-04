"""CHR-03: `kb.py store-lf` rewrites CRLF store files as LF only when their content stays identical."""
import json
import tempfile
import unittest
from pathlib import Path

from . import test_core as fixtures
from kbsys import config, db


class StoreLfTests(unittest.TestCase):
    setUp, tearDown = fixtures.Demo3Tests.setUp, fixtures.Demo3Tests.tearDown

    def test_crlf_files_become_lf_and_others_are_left(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        hist = config.PAPERS_DIR / "p1" / "tier1_history" / "x"
        hist.mkdir(parents=True)
        files = {config.STORE / "gap_candidates.jsonl": b'{"a": 1}\r\n{"b": "x"}\r\n',
                 hist / "sections.json": b'{\r\n  "a": [1, 2]\r\n}',
                 config.PAPERS_DIR / "p1" / "note.md": b"line\r\nline2\r\n",
                 config.PAPERS_DIR / "p1" / "lone.json": b'{"a": 1}\r',  # a lone CR stays: refused
                 config.PAPERS_DIR / "p1" / "bad.jsonl": b'{"a": \r\n1}\r\n',  # CRLF inside a JSONL row: not one row per line
                 config.PAPERS_DIR / "p1" / "crop.png": b"\r\n",  # not a text file
                 config.PAPERS_DIR / "p1" / "inner.json": b'{"a": "x\r\ny"}\r\n'}  # raw CR in a string: invalid JSON, refused
        for path, raw in files.items():
            path.write_bytes(raw)
        dry = db.store_lf()
        self.assertEqual(sorted(dry["files"]), ["gap_candidates.jsonl", "papers/p1/note.md", "papers/p1/tier1_history/x/sections.json"])
        self.assertEqual(sorted(r.split(" ")[0] for r in dry["refused"]), ["papers/p1/bad.jsonl", "papers/p1/inner.json", "papers/p1/lone.json"])
        mtime = (config.STORE / "gap_candidates.jsonl").stat().st_mtime_ns
        self.assertEqual((config.STORE / "gap_candidates.jsonl").read_bytes(), files[config.STORE / "gap_candidates.jsonl"])  # dry run
        db.store_lf(apply=True)
        self.assertEqual((config.STORE / "gap_candidates.jsonl").read_bytes(), b'{"a": 1}\n{"b": "x"}\n')
        self.assertEqual((config.STORE / "gap_candidates.jsonl").stat().st_mtime_ns, mtime)  # analysis_runner orders files by mtime
        self.assertEqual((config.PAPERS_DIR / "p1" / "inner.json").read_bytes(), files[config.PAPERS_DIR / "p1" / "inner.json"])
        self.assertEqual(json.loads((hist / "sections.json").read_bytes()), {"a": [1, 2]})
        self.assertNotIn(b"\r", (hist / "sections.json").read_bytes())
        self.assertEqual((config.PAPERS_DIR / "p1" / "lone.json").read_bytes(), b'{"a": 1}\r')
        self.assertEqual((config.PAPERS_DIR / "p1" / "crop.png").read_bytes(), b"\r\n")
        self.assertEqual(db.store_lf()["files"], [])  # idempotent


if __name__ == "__main__":
    unittest.main(verbosity=2)
