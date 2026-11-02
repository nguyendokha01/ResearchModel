"""`kb.py submit-analysis --dry-run`: every check runs, nothing is stored, exit 2 while anything blocks."""
import contextlib
import io
import json
import unittest
from types import SimpleNamespace

from . import test_core as fixtures
from kbsys import config, db
import kb


class SubmitDryRunTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def test_dry_run_reports_and_stores_nothing(self):
        path = config.ROOT / "draft.json"
        path.write_text(json.dumps({"paper_id": self.pid, "analyst": "test", "classification": "CORE",
                                    "classification_reason": "x", "evidence_type": "primary_research",
                                    "units": [], "skipped": [], "summary": []}), encoding="utf-8")
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as stop:
            kb.cmd_submit_analysis(SimpleNamespace(paper_id=self.pid, file=str(path), dry_run=True), {}, self.con)
        self.assertEqual(stop.exception.code, 2)  # must_cover sentences are not covered
        report = json.loads(out.getvalue())
        self.assertTrue(report["dry_run"] and report["blocking"])
        self.assertFalse((db.paper_dir(self.pid) / "evidence.jsonl").exists())
        self.assertFalse((db.paper_dir(self.pid) / "analysis_state.json").exists())


if __name__ == "__main__":
    unittest.main()
