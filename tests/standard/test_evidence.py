"""Edges of the EV checks that the generic PASS / FAIL cases do not cover."""
import ast
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kbsys import config, conform, db, evidence, figure_reading, review_policy, standard
from kbsys.conform import Paper

from . import cases_evidence as cases
from .cases_evidence import row, unit

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


class Quotes(unittest.TestCase):
    def test_edited_quote_and_gap_inside_a_sentence_are_each_one_finding(self):
        edited = cases.chain(False)
        edited.units[0]["quote"] = "Cells were thawed."
        got = found("EV-16", edited)
        self.assertEqual((len(got), "differs" in got[0]), (1, True), got)
        stored = cases.chain(True)  # the marker alone is one finding, not also a differing quote
        got = found("EV-16", stored)
        self.assertEqual((len(got), "gap marker" in got[0]), (1, True), got)
        stored.units[0]["quote"] = "Cells were thawed."  # an edit hidden behind the marker is reported as an edit
        self.assertTrue(any("differs" in v for v in found("EV-16", stored)))

    def test_a_gap_between_unrelated_sentences_is_allowed(self):
        rows = [row(1, "results", "Cells grew.", "a"), row(2, "results", "Cells died.", "a"), row(3, "results", "Cells divided.", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "result", [1, 3], "Cells grew and divided.", rows=rows)])
        self.assertEqual(found("EV-16", paper), [])

    def test_a_missing_source_sentence_is_left_to_ev15(self):
        paper = Paper("p", sentences=[], units=[unit(1, "result", [1], "Survival increased.")])
        self.assertEqual(found("EV-16", paper), [])
        self.assertEqual(len(found("EV-15", paper)), 1)


class Hedging(unittest.TestCase):
    def test_only_non_significance_is_no_hedge(self):
        rows = [row(1, "results", "Survival did not differ between groups (P > 0.05).", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "result", [1], "Survival did not differ.", rows=rows, hedging="hedged")])
        self.assertEqual(len(found("EV-06", paper)), 1)

    def test_a_unit_hedged_with_no_lexicon_word_anywhere_is_not_reported(self):
        rows = [row(1, "results", "Cells were counted.", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "result", [1], "Cells were counted.", rows=rows, hedging="hedged")])
        self.assertEqual(found("EV-06", paper), [])

    def test_named_main_claim_sentence_wins_over_the_guess(self):
        paper = cases.hedge_paper([unit(1, "result", [1, 2], "Survival increased to 82% in treated mouse embryos.",
                                        hedging="hedged", hedged_sentence_ids=["p:S0002"])])
        self.assertEqual(found("EV-06", paper), [])


class Added(unittest.TestCase):
    def test_authors_in_a_citation_marker_and_greek_names_are_in_the_source(self):
        rows = [row(1, "results", "Deletion of ∆EF was reported (Smith et al., 2010).", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "background", [1], "Smith et al. reported deltaEF deletion.", rows=rows)])
        self.assertEqual(found("EV-19", paper, []), [])

    def test_a_name_inside_a_longer_one_and_a_plural_acronym_are_found(self):
        rows = [row(1, "results", "The sip150 embryos had more PAFs.", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "result", [1], "The p150 embryos had more PAF.", rows=rows)])
        self.assertEqual(found("EV-19", paper, []), [])

    def test_a_pronoun_replaced_by_a_name_is_added_content(self):
        rows = [row(1, "results", "They comprise a small share.", "a")]
        paper = Paper("p", sentences=rows, units=[unit(1, "result", [1], "The Zeta cells comprise a small share.", rows=rows)])
        self.assertEqual(len(found("EV-19", paper, [])), 1)


class Robustness(unittest.TestCase):
    def test_a_crashing_validator_is_an_error_for_every_validator_clause_never_a_pass(self):
        paper = cases.paper()
        with mock.patch.object(evidence, "validate", side_effect=RuntimeError("boom")):
            for cid in ("EV-01", "EV-02", "EV-09"):
                with self.assertRaises(RuntimeError, msg=cid):
                    found(cid, paper)

    def test_a_reading_without_an_object_id_does_not_mark_every_sentence_without_one(self):
        reading = {"label": "Figure 1", "caption_consistency": [{"claim_sentence_id": "p:S0001", "status": "inconsistent"}]}
        paper = Paper("p", sentences=cases.sentences(), readings=[reading],
                      units=[unit(1, "result", [1], "Survival increased."), unit(2, "result", [4], "Counts were equal.")])
        self.assertEqual([v.split(":")[1] for v in found("EV-20", paper)], ["E0001"])  # only the claim sentence itself


class FigureFlag(unittest.TestCase):
    """EV-20: evidence.validate itself (real PaperContext, readings read from the paper folder) sets the FG flag."""

    def fg_flags(self, reading, sents, break_unit=None):
        paper = cases.paper(sents=sents)
        submission = evidence.submission_from("p", paper.analysis, paper.units)
        if break_unit:
            submission["units"][break_unit - 1]["unit_type"] = "bogus"
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(config, "PAPERS_DIR", Path(tmp)):
            folder = Path(tmp) / "p"
            (folder / "figure_readings").mkdir(parents=True)
            (folder / "metadata.json").write_text(json.dumps({**paper.meta, "extraction_version": 2}), encoding="utf-8")
            db.write_jsonl(folder / "sentences.jsonl", sents)
            db.write_jsonl(folder / "references.jsonl", paper.references)
            db.write_jsonl(folder / "tables.jsonl", [])
            (folder / "figure_readings" / "F1.json").write_text(json.dumps(reading), encoding="utf-8")
            result = evidence.validate(None, "p", submission)[0]
        self.assertEqual(bool(result["errors"]), bool(break_unit))
        return [f for f in result["flags"] if f["code"] == "FG"]

    READING = {"object_id": "p:F1", "label": "Figure 1", "caption_consistency": [
        {"claim_sentence_id": "p:S0001", "status": "inconsistent", "note": "The text names another group."}]}

    def test_a_unit_citing_the_claim_sentence_is_flagged_and_one_citing_neither_is_not(self):
        got = self.fg_flags(self.READING, cases.sentences())
        self.assertEqual([f["unit_index"] for f in got], [1], got)
        self.assertEqual(got[0]["severity"], "flag")
        self.assertTrue(got[0]["message"].startswith("Figure 1: " + figure_reading.WARNING_LABEL), got)

    def test_a_unit_citing_a_sentence_of_the_warned_object_is_flagged(self):
        sents = cases.sentences()
        sents[3]["object_id"] = "p:F1"  # S0004 belongs to the figure
        self.assertEqual([f["unit_index"] for f in self.fg_flags(self.READING, sents)], [1, 4])

    def test_a_unit_that_recheck_retains_although_rejected_keeps_the_flag(self):
        got = self.fg_flags(self.READING, cases.sentences(), break_unit=1)
        self.assertEqual([f["unit_index"] for f in got], [1], got)

    def test_a_wrong_crop_gives_no_flag(self):
        self.assertEqual(self.fg_flags({**self.READING, "crop_wrong": True}, cases.sentences()), [])


class Duplicates(unittest.TestCase):
    def test_a_merge_that_swallows_everything_and_a_resolver_that_gives_up_are_found(self):
        merge_all = lambda units: (units[:1], {u["unit_id"]: units[0]["unit_id"] for u in units[1:]})
        with tempfile.TemporaryDirectory() as tmp:
            store = cases.aliases_store(Path(tmp), {})
            self.assertTrue(any("merge kept" in v for v in found("EV-21", store, merge_all)))
            self.assertTrue(any("did not follow" in v for v in found("EV-21", store, review_policy.deduplicate, lambda con, uid: None)))


class Vocabulary(unittest.TestCase):
    def test_a_long_verb_list_with_modal_verbs_in_it_is_no_hedge_list(self):
        words = ", ".join(f'"verb{n}"' for n in range(30))
        with tempfile.TemporaryDirectory() as tmp:
            code = f'VERBS = {{"may", "might", "could", {words}}}' + chr(10)
            self.assertEqual(found("EV-22", *cases.tree(Path(tmp), code)), [])

    def test_an_exception_names_one_collection_of_one_file(self):
        labels = 'DIRECTION = {"associated", "association", "associated_with", "correlation", "correlated"}\n'  # the listed outcome-direction set
        other = 'MODAL = ["may", "might", "could", "suggest"]\n'
        with tempfile.TemporaryDirectory() as tmp:
            (root,) = cases.tree(Path(tmp), "")
            cases.put(root / "scripts" / "kbsys" / "result_notes.py", labels)
            self.assertEqual(found("EV-22", root), [])  # exempted: file and item match
            cases.put(root / "scripts" / "kbsys" / "result_notes.py", labels + other)
            self.assertEqual(len(found("EV-22", root)), 1)  # a second, different hedge list in the same file is reported
            cases.put(root / "scripts" / "kbsys" / "result_notes.py", other)
            cases.put(root / "scripts" / "kbsys" / "elsewhere.py", labels)
            self.assertEqual(len(found("EV-22", root)), 2)  # the same set in another file is no exception

    def test_every_listed_exception_still_names_a_collection_of_its_file(self):
        for e in standard.param("EV-22", "exceptions"):  # a moved or mistyped item exempts nothing: this test says so
            if not (config.ROOT / e["file"]).is_file():
                continue  # a development file that a release does not ship
            tree = ast.parse((config.ROOT / e["file"]).read_text(encoding="utf-8"))
            held = [[c.value for c in n.elts if isinstance(c, ast.Constant)] for n in ast.walk(tree) if isinstance(n, (ast.List, ast.Tuple, ast.Set))]
            held += [n.value.split("|") for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
            self.assertTrue(any(e["item"] in items for items in held), f"{e['file']}: no collection holds {e['item']!r}")


if __name__ == "__main__":
    unittest.main()
