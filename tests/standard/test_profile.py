"""Edges of the SP, FG and NC checks that the generic PASS / FAIL cases do not cover, and the probes run against broken judges."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kbsys import conform, config, db, figure_reading, node_cards, structure
from kbsys import conform_profile as cp

from . import cases_profile as cases

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


def tmp_store(test, **kwargs):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    return cases.stored_store(Path(folder.name), **kwargs)


class Wording(unittest.TestCase):
    def test_a_denial_is_not_a_claim(self):
        self.assertFalse(cp._absence("This is absence of a Methods section, not evidence that no protocols exist in the cited studies."))
        self.assertFalse(cp._absence("The review does not describe a literature search of its own."))
        self.assertTrue(cp._absence("No protocols exist."))
        self.assertTrue(cp._absence("Not described anywhere in the literature."))
        self.assertTrue(cp._absence("This does not exist."))


class Stale(unittest.TestCase):
    def test_a_profile_made_from_older_sentences_is_not_judged(self):
        def change_source(folder):
            path = folder / "sentences.jsonl"
            path.write_text(path.read_text(encoding="utf-8").replace("example model", "sample model"), encoding="utf-8", newline="")
            cases.edit("section_profile.json", lambda d: d["answers"].pop("when"))(folder)
        store = tmp_store(self, damage=change_source)
        self.assertEqual(found("SP-01", store.paper(cp.PID)), [])

    def test_a_reading_made_from_older_sentences_is_not_judged_for_numbers(self):
        def change(folder):
            cases.reading_edit(lambda d: d["caption_consistency"][0].update(note="Panel A shows 91 cells."))(folder)
            path = folder / "sentences.jsonl"
            path.write_text(path.read_text(encoding="utf-8").replace("Cell number was 12", "Cell number was 13"), encoding="utf-8", newline="")
        paper = cases.figure_store(Path(self.enterContext(tempfile.TemporaryDirectory())), change).paper(cp.PID)
        self.assertEqual(found("FG-02", paper), [])
        self.assertEqual(found("FG-05", cases.Store(root=paper.folder.parent.parent, paper_ids=[cp.PID])), [])

    def test_a_stale_reading_that_load_labels_stale_is_fine(self):
        bad = cases.CASES["FG-04"][1](Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.assertEqual([v for v in found("FG-04", bad[0], lambda paper_id, object_id: {"status": "stale"}) if not v.startswith("probe")], [])


class Review(unittest.TestCase):
    """Edges found by the review of the SP / FG checks."""

    def test_sp02_a_malformed_overview_item_is_not_a_crash(self):
        def malform(folder):
            cases.edit("paper_overview.json", lambda d: d["research_question"].extend(["not an item", {"text": "x", "sentence_ids": [["S1"]]}]))(folder)
        paper = tmp_store(self, damage=malform).paper(cp.PID)
        self.assertEqual(found("SP-02", paper), [])  # SP-06 reports the items; SP-02 must not raise

    def test_sp03_a7_setting_warning_is_reported(self):
        paper = tmp_store(self, damage=cases.edit("section_profile.json", lambda d: d["answers"]["what"].update(warnings=["A7_setting"]))).paper(cp.PID)
        self.assertTrue(any("A7_setting" in v for v in found("SP-03", paper)))

    def test_fg02_a_plain_reading_and_a_mixed_case_marker_name_pass(self):
        plain = cases.figure_store(Path(self.enterContext(tempfile.TemporaryDirectory()))).paper(cp.PID)
        self.assertEqual(found("FG-02", plain), [])
        tuj = cases.CASES["FG-02"][0](Path(self.enterContext(tempfile.TemporaryDirectory())))
        self.assertEqual(found("FG-02", *tuj), [])  # Tuj-3 is a marker name, not an invented number 3


class Protocol(unittest.TestCase):
    def absent_methods(self, reason):
        folder = Path(self.enterContext(tempfile.TemporaryDirectory()))
        root = folder / "s"
        rows = tuple(r for r in cp.ROWS if r[0] != "methods")
        with mock.patch.multiple(config, STORE=root, PAPERS_DIR=root / "papers"):
            cp.make_paper(root / "papers", rows=rows)
            task = structure.prepare_protocol(cp.PID)
            reply = {**cp._echo(task), "protocols": [], "skipped": [], "source_absence_reason": reason}
            structure.submit_protocol(None, cp.PID, reply, "test")
        return cases.Store(root=root, paper_ids=[cp.PID]).paper(cp.PID)

    def test_absent_methods_with_an_honest_reason_passes(self):
        paper = self.absent_methods("Narrative review without a Methods section; not evidence that no protocols exist in the cited studies.")
        self.assertEqual(found("SP-05", paper), [])

    def test_absent_methods_claiming_no_protocols_fails(self):
        self.assertEqual(len(found("SP-05", self.absent_methods("No protocols exist."))), 1)


class Probes(unittest.TestCase):
    """Each probe must see a product that is wrong."""

    def test_sp07_accepting_a_stale_reply(self):
        store = tmp_store(self)
        got = found("SP-07", store, lambda *a, **k: {"ok": True}, lambda *a, **k: {"ok": True})
        self.assertEqual(len(got), 2, got)

    def test_sp08_always_complete(self):
        report = {"complete": True, "pending": [], "wh": {"ready": True}, "protocol": {"ready": True}, "scientific_acceptance": "not_assessed"}
        got = found("SP-08", tmp_store(self), lambda paper_id: dict(report))
        self.assertTrue(any("without profiles" in v for v in got), got)
        self.assertTrue(any("without a protocol" in v for v in got), got)

    def test_sp09_one_big_chunk_and_a_cut_chain(self):
        store = tmp_store(self)
        got = found("SP-09", store, (lambda rows: [[r["sentence_id"] for r in rows]],))
        self.assertTrue(any("sentences and" in v for v in got), got)
        cutting = lambda rows: [[r["sentence_id"] for r in rows[i:i + 32]] for i in range(0, len(rows), 32)]
        got = found("SP-09", store, (cutting,))
        self.assertTrue(any("cut from" in v for v in got), got)

    def test_fg01_accepting_crop_wrong_without_a_note(self):
        got = found("FG-01", cases.figure_store(Path(self.enterContext(tempfile.TemporaryDirectory()))),
                    lambda paper_id, object_id, reply, analyst: {"panels": [], "caption_consistency": []})
        self.assertTrue(any("without a note was accepted" in v for v in got), got)

    def test_fg05_a_reading_that_rewrites_the_caption(self):
        def submit(paper_id, object_id, reply, analyst):
            path = config.PAPERS_DIR / paper_id / "sentences.jsonl"
            path.write_text(path.read_text(encoding="utf-8").replace("counted", "weighed"), encoding="utf-8", newline="")
            return figure_reading.submit(paper_id, object_id, reply, analyst)
        got = found("FG-05", cases.figure_store(Path(self.enterContext(tempfile.TemporaryDirectory()))), submit)
        self.assertTrue(any("changed sentences.jsonl" in v for v in got), got)

    def test_nc03_currency_that_never_changes(self):
        got = found("NC-03", cases.card_store(Path(self.enterContext(tempfile.TemporaryDirectory()))),
                    node_cards.submit, lambda vault, concept_id: {"card_currency": "current"})
        self.assertEqual(len(got), 3, got)  # new unit, new paper, changed quote

    def test_nc04_a_card_with_card_check_passes(self):
        self.assertEqual(found("NC-04", lambda vault, concept_id: {"card_check": "passed"}), [])


if __name__ == "__main__":
    unittest.main()
