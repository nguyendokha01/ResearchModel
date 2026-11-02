"""Edges of the IN / CHR checks that the generic PASS / FAIL cases do not cover."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, conform, conform_intake, db, tier1
from kbsys.conform import Paper

from .. import test_core as fixtures
from . import cases_intake as cases

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


class DataHalves(unittest.TestCase):
    def tmp(self):
        import tempfile
        from pathlib import Path
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def test_nfc_data_half_is_clean_with_a_judge_that_normalises(self):
        self.assertEqual(found("CHR-01", *(cases.chr01_pass(self.tmp()) + (cases.good_judge,))), [])
        bad = found("CHR-01", *cases.chr01_fail(self.tmp()))
        self.assertEqual(len(bad), 2, bad)  # the sentence and the note; the passage file is empty

    def test_lookalike_probe_passes_when_all_forms_fold_together(self):
        fold = {ord(c): "a" for c in "\u25e6\u02da\u00b0\u0394\u2206"}
        self.assertEqual(found("CHR-04", lambda v: v.replace("^", "").translate(fold)), [])

    def test_code_scan_flags_a_control_character_from_a_short_escape_only(self):
        tmp = self.tmp()
        cases.put(tmp / "scripts" / "bad.py", 'import re\nA = re.compile("\\bword")\nB = "\\x01"\n')
        got = found("CHR-02", cases.store(tmp), tmp)
        self.assertEqual(len(got), 1, got)
        self.assertIn("bad.py:2", got[0])
        self.assertIn("U+0008", got[0])

    def test_notes_and_sentences_with_broken_characters(self):
        tmp = self.tmp()
        store = cases.store(tmp, sentences=[cases.sentence("Cells \ufffd grew."), cases.sentence("Î² subunit")],
                            notes=[("a.md", "private \ue000 use"), ("b.md", "fine")])
        got = found("CHR-02", store, tmp)
        self.assertEqual(len(got), 4, got)

    def test_carriage_return_in_vault_note_and_repo_config_file(self):
        tmp = self.tmp()
        cases.put(tmp / "config" / "x.yaml", b"a: 1\r\n")
        got = found("CHR-03", cases.store(tmp, notes=[("a.md", "x\r\ny")]), tmp)
        self.assertEqual(len(got), 2, got)

    def test_carriage_return_in_a_mine_note_is_not_reported_but_in_a_generated_note_is(self):
        tmp = self.tmp()  # Mine/ is human content (rule 5): CHR-03 is about generated notes and store files
        got = found("CHR-03", cases.store(tmp, notes=[("Mine/Ideas/i.md", "x\r\ny"), ("Papers/p.md", "x\r\ny")]), tmp)
        self.assertEqual(len(got), 1, got)
        self.assertIn("v/Papers/p.md", got[0])

    def test_active_source_missing_is_a_violation_archived_is_not(self):
        tmp = self.tmp()
        gone = {"path": str(tmp / "gone.pdf"), "sha256": "0" * 64}
        self.assertEqual(len(found("IN-03", Paper("p", meta={"source_files": [{**gone, "status": "active"}]}))), 1)
        self.assertEqual(found("IN-03", Paper("p", meta={"source_files": [{**gone, "status": "unavailable"}]})), [])

    def test_source_entry_without_sha_or_path_or_known_status(self):
        got = found("IN-07", Paper("p", meta={"source_files": [{"status": "lost"}]}))
        self.assertEqual(len(got), 3, got)

    def test_duplicate_keys_in_the_library(self):
        con = cases.papers_db(("a", None, "7", "t" * 30, 2020), ("b", None, "7", "t" * 30, 2020))
        got = found("IN-01", conform.Store(con=con))
        self.assertEqual(len(got), 2, got)  # same PMID, same title + year

    def test_same_file_hash_in_two_papers_is_a_duplicate(self):
        tmp = self.tmp()
        rows = (("a", None, None, "x" * 30, 2020), ("b", None, None, "y" * 30, 2021))
        for pid, sha in (("a", "ab" * 32), ("b", "ab" * 32)):
            cases.put(tmp / "papers" / pid / "metadata.json", json.dumps({"source_files": [{"sha256": sha}]}))
        got = found("IN-01", conform.Store(con=cases.papers_db(*rows), root=tmp))
        self.assertEqual(len(got), 1, got)
        self.assertIn("file hash", got[0])
        cases.put(tmp / "papers" / "b" / "metadata.json", json.dumps({"source_files": [{"sha256": "cd" * 32}]}))
        self.assertEqual(found("IN-01", conform.Store(con=cases.papers_db(*rows), root=tmp)), [])

    def test_find_existing_probe_flags_a_judge_that_finds_nothing(self):
        self.assertTrue(found("IN-01", *cases.in01_fail_probe(self.tmp())))

    def test_supplement_probe_does_not_pass_on_an_unrelated_oserror(self):
        def flaky(con, cfg, paper_id, path):
            if Path(path).name == "other.txt":
                raise FileNotFoundError(path)  # an OSError, but not the refusal of a damaged copy
            return tier1.attach_supplement(con, cfg, paper_id, path)
        got = found("IN-09", flaky)
        self.assertTrue(any("never reached" in v for v in got), got)

    def test_probe_root_is_resolved_so_config_rel_works_when_temp_is_an_alias(self):
        real = tempfile.TemporaryDirectory

        class Aliased:  # <tmp>/x/.. : the same folder, spelled so that resolve() changes it (8.3 names, junctions)
            def __init__(self, **kwargs):
                self.inner = real(**kwargs)

            def __enter__(self):
                tmp = Path(self.inner.__enter__())
                (tmp / "x").mkdir()
                return str(tmp / "x" / "..")

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)
        with patch.object(conform_intake.tempfile, "TemporaryDirectory", Aliased):
            self.assertEqual(found("IN-10"), [])


class SelftestWrappers(unittest.TestCase):
    """IN-05 and CHR-08 against the real selftest.check_paper on a stored paper."""
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def store(self):
        return conform.Store(con=self.con, root=config.STORE, paper_ids=[self.pid])

    def test_clean_paper(self):
        self.assertEqual(found("IN-05", self.store()), [])
        self.assertEqual(found("CHR-08", self.store()), [])

    def test_seq_gap_is_an_id_violation(self):
        self.rows[2]["seq"] = 9
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        got = found("IN-05", self.store())
        self.assertTrue(any("I2" in v for v in got), got)

    def test_ocr_passage_without_a_recorded_page(self):
        passages = list(db.read_jsonl(self.folder / "passages.jsonl"))
        passages[0]["ocr"] = {"engine": "x"}
        db.write_jsonl(self.folder / "passages.jsonl", passages)
        got = found("CHR-08", self.store())
        self.assertTrue(any("OCR1" in v for v in got), got)


if __name__ == "__main__":
    unittest.main()
