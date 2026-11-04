"""EV-17 (direction and species filled by code) and EV-18 (bundled finding flag): evidence_fill, evidence.directions,
the validate-time fill and `kb.py fill-evidence-fields`."""
import contextlib
import hashlib
import io
import unittest
from types import SimpleNamespace

import kb
from kbsys import conform_evidence, db, evidence, evidence_fill
from kbsys.conform import Paper
from kbsys.conform_intake import _sandbox, _seed

from tests.standard.cases_evidence import REGISTRY, ANALYSIS, row, unit


def sent(n, text, kind="text"):
    return row(n, "results", text, "a", kind=kind)


def fill(statement_sentences, species_text="", **unit_extra):
    rows = [sent(n, t, k) for n, (t, k) in enumerate(statement_sentences, 1)]
    u = {"unit_type": "result", "model": {}, "outcome": {}, **unit_extra}
    return evidence_fill.fill_fields(u, rows, {"title": species_text, "abstract": ""})


T = lambda text: (text, "text")


class DirectionTests(unittest.TestCase):
    def test_single_directions_are_filled_with_their_sentence(self):
        got = fill([T("Cells divided."), T("Survival was higher in group A.")])
        self.assertEqual(got["direction"], {"value": "increase", "source": "S0002"})
        self.assertEqual(fill([T("Survival was lower in group A.")])["direction"]["value"], "decrease")
        self.assertEqual(fill([T("Survival did not differ between groups.")])["direction"]["value"], "no_change")

    def test_mixed_contrast_none_and_no_change_with_another_are_not_filled(self):
        for text in ("Survival rose higher in A but was lower in B.", "Survival was higher in A while lower in B.",
                     "Survival was higher in A but did not differ in B.", "Cells were frozen."):
            self.assertNotIn("direction", fill([T(text)]), text)
        self.assertNotIn("direction", fill([T("Survival was higher."), T("Counts were lower.")]))

    def test_unsure_readings_are_not_filled(self):
        for text in ("More than 50 cells were counted per embryo.", "There was no more than 5% difference between groups.",
                     "At a high concentration of 10 mM the cells were damaged.", "Cells were cultured at low density.",
                     "Nothing was lost during vitrification.", "Better survival after thawing was not observed.",
                     "Subjects with more than two prior cycles were excluded.",
                     "32 genes were down regulated and 40 up regulated."):
            self.assertNotIn("direction", fill([T(text)]), text)
        self.assertEqual(fill([T("Genes were down regulated in B.")])["direction"]["value"], "decrease")

    def test_a_statement_with_another_direction_than_the_source_is_not_filled(self):
        self.assertNotIn("direction", fill([T("Survival was higher in A.")], statement="Survival decreased in A."))
        self.assertEqual(fill([T("Survival was higher in A.")], statement="Survival increased in A.")["direction"]["value"],
                         "increase")

    def test_caption_is_skipped_and_ai_value_kept(self):
        self.assertNotIn("direction", fill([("Survival was higher.", "caption")]))
        got = fill([T("Survival was higher."), ("Figure 1 shows lower rates.", "caption")])
        self.assertEqual(got["direction"]["value"], "increase")
        self.assertNotIn("direction", fill([T("Survival was higher.")], outcome={"direction": "decrease"}))
        self.assertEqual(evidence.directions("no change but higher"), {"no_change", "increase"})


class SpeciesTests(unittest.TestCase):
    def test_one_species_in_the_sentences(self):
        got = fill([T("Mice were treated."), T("Survival was higher.")])
        self.assertEqual(got["species"], {"value": "mouse", "source": "S0001"})

    def test_two_species_in_the_sentences_or_paper_are_not_filled(self):
        self.assertNotIn("species", fill([T("Mice and rats were treated.")]))
        self.assertNotIn("species", fill([T("Mice were treated."), T("Rats were treated.")], "Rat study"))  # sentences speak
        self.assertNotIn("species", fill([T("Cells were treated.")], "Mouse and human cells"))

    def test_paper_level_species_when_the_sentences_name_none(self):
        self.assertEqual(fill([T("Cells were treated.")], "Bovine embryos")["species"],
                         {"value": "bovine", "source": "paper:title"})
        rows = [sent(1, "Cells were treated.")]
        got = evidence_fill.fill_fields({"unit_type": "result"}, rows, {"title": "Embryos", "abstract": "In sheep embryos."})
        self.assertEqual(got["species"], {"value": "ovine", "source": "paper:abstract"})

    def test_a_graft_or_a_reagent_is_not_the_species_of_the_study(self):
        graft = [T("Integration of GFP+ human Muse cells was recognized in the cortex.")]
        self.assertNotIn("species", fill(graft, "Muse cells in a rat stroke model"))  # title names another species too
        self.assertEqual(fill(graft, "Human Muse cells")["species"]["value"], "human")
        for text in ("Goat anti-mouse IgG was used.", "A rabbit polyclonal antibody was used.", "Mouse monoclonal anti-H3 was used."):
            self.assertNotIn("species", fill([T(text)]), text)

    def test_ai_value_and_non_result_units_are_kept(self):
        self.assertNotIn("species", fill([T("Mice were treated.")], model={"species": "rat"}))
        self.assertEqual(evidence_fill.fill_fields({"unit_type": "background"}, [sent(1, "Mice were higher.")], {}), {})


def check(u_over, sents):
    """evidence._check_unit on one raw unit; returns (stored, flags, normalized)."""
    paper = Paper("p", sentences=sents, units=[], analysis=dict(ANALYSIS), references=[], tables=[],
                  meta={"title": "Trehalose in embryo freezing", "abstract": ""}, registry=REGISTRY)
    ctx = conform_evidence._context(paper)
    flags, normalized, errors = [], [], []
    raw = {"unit_type": "result", "statement": "Survival increased in treated mouse embryos.", "sentence_ids": ["S0001"],
           "terms": {"concepts": ["Survival"]}, **u_over}
    stored = evidence._check_unit(ctx, 1, raw, False, lambda i, c, m: errors.append((c, m)),
                                  lambda i, c, m: flags.append((c, m)), normalized)
    return stored, flags, normalized, errors, raw


SENTS = [sent(1, "Survival increased in treated mouse embryos."), sent(2, "Cells were counted.")]


class ValidateTests(unittest.TestCase):
    def test_validate_time_fill_records_provenance_and_does_not_flag(self):
        stored, flags, normalized, errors, raw = check({}, SENTS)
        self.assertEqual(stored["outcome"]["direction"], "increase")
        self.assertEqual(stored["model"]["species"], "mouse")
        self.assertEqual(stored["filled_by"], {"direction": {"by": "rule", "source": "S0001"},
                                               "species": {"by": "rule", "source": "S0001"}})
        self.assertIn({"unit_index": 1, "field": "outcome.direction", "from": None, "to": "increase",
                       "because": "rule: S0001"}, normalized)
        self.assertEqual([f for f in flags if f[0] == "V7"], [])
        self.assertEqual(errors, [])
        self.assertNotIn("direction", raw.get("outcome", {}))  # the submission itself is not changed

    def test_ai_value_is_kept_and_key_omitted_when_nothing_is_filled(self):
        stored, _, normalized, _, _ = check({"outcome": {"direction": "increase"}, "model": {"species": "Mouse"}}, SENTS)
        self.assertEqual((stored["outcome"]["direction"], stored["model"]["species"]), ("increase", "Mouse"))
        self.assertNotIn("filled_by", stored)
        self.assertFalse([n for n in normalized if n.get("field")])
        stored, *_ = check({"sentence_ids": ["S0002"], "statement": "Cells were counted in the group."}, SENTS)
        self.assertNotIn("filled_by", stored)

    def test_recheck_keeps_filled_by_and_submission_from_keeps_it(self):
        first, *_ = check({}, SENTS)
        again, _, normalized, _, _ = check({"outcome": first["outcome"], "model": first["model"],
                                            "filled_by": first["filled_by"]}, SENTS)
        self.assertEqual(again["filled_by"], first["filled_by"])
        self.assertFalse([n for n in normalized if n.get("field")])
        sub = evidence.submission_from("p", {}, [{**first, "unit_id": "p:E0001", "sentence_ids": ["p:S0001"]}])
        self.assertEqual(sub["units"][0]["filled_by"], first["filled_by"])
        # a claimed fill over a value that is empty again is not kept
        gone, *_ = check({"filled_by": {"direction": {"by": "rule", "source": "S0009"}},
                          "sentence_ids": ["S0002"], "statement": "Cells were counted in the group."}, SENTS)
        self.assertNotIn("filled_by", gone)

    def test_ai_fill_provenance_is_kept_other_claims_are_not(self):  # G5: AI fills of stored units keep filled_by
        ai = {"by": "ai", "source": "S0002"}
        kept, *_ = check({"sentence_ids": ["S0002"], "statement": "Cells were counted in the group.",
                          "outcome": {"direction": "not_applicable"}, "filled_by": {"direction": ai, "species": ai}}, SENTS)
        self.assertEqual(kept["filled_by"], {"direction": ai})  # species is still empty: no provenance for an empty field
        other, *_ = check({"sentence_ids": ["S0002"], "statement": "Cells were counted in the group.",
                           "outcome": {"direction": "not_applicable"}, "filled_by": {"direction": {"by": "human"}}}, SENTS)
        self.assertNotIn("filled_by", other)
        uncited, *_ = check({"sentence_ids": ["S0002"], "statement": "Cells were counted in the group.", "outcome":
                             {"direction": "not_applicable"}, "filled_by": {"direction": {**ai, "source": "S0001"}}}, SENTS)
        self.assertNotIn("filled_by", uncited)  # a source the unit does not cite is no provenance (review-fix 06998e0)
        ai = {**ai, "analyst": "claude/sonnet"}  # the G5 shape survives export -> submit (submission_from)
        first, *_ = check({"sentence_ids": ["S0002"], "statement": "Cells were counted in the group.",
                           "outcome": {"direction": "not_applicable"}, "filled_by": {"direction": ai}}, SENTS)
        sub = evidence.submission_from("p", {}, [{**first, "unit_id": "p:E0001", "sentence_ids": ["p:S0002"]}])
        again, *_ = check(sub["units"][0], SENTS)
        self.assertEqual(again["filled_by"], {"direction": ai})


class BundledFindingTests(unittest.TestCase):
    ROWS = [sent(1, "Survival increased in group A but decreased in group B.")]

    def bundled(self, statement, direction):
        _, flags, *_ = check({"statement": statement, "outcome": {"direction": direction}}, self.ROWS)
        return [m for c, m in flags if c == "V7" and m.startswith("bundled finding")]

    def test_flag_raised_for_a_single_direction_over_a_contrast(self):
        got = self.bundled("Survival increased in group A but decreased in group B.", "increase")
        self.assertEqual(len(got), 1)
        self.assertIn("turns from increase to decrease", got[0])

    def test_no_flag_for_one_direction_or_a_contrast_without_opposite_words_or_mixed(self):
        self.assertFalse(self.bundled("Survival increased in group A.", "increase"))
        self.assertFalse(self.bundled("Survival increased in group A but group B was studied.", "increase"))
        self.assertFalse(self.bundled("Survival increased in group A but decreased in group B.", "mixed"))


class CommandTests(unittest.TestCase):
    PID = "p1"

    def seed(self, root, con):
        _seed(con, self.PID, ("one",), title="Mouse embryo freezing", analysis_status="done")
        folder = db.paper_dir(self.PID)
        rows = [{**row(n, "results", text, "a"), "sentence_id": f"{self.PID}:S{n:04d}"}
                for n, text in enumerate(["Survival was higher in treated mice.", "Cells were counted."], 1)]
        db.write_jsonl(folder / "sentences.jsonl", rows)
        units = [unit(n, "result", [n], f"Unit {n} result.", rows=rows) for n in (1, 2)]
        for u in units:
            u["unit_id"] = f"{self.PID}:E{u['seq']:04d}"
        units[1]["model"] = {"species": "Human"}  # set by a human / AI: must stay
        db.write_jsonl(folder / "evidence.jsonl", units)
        return folder / "evidence.jsonl"

    @staticmethod
    def run_cmd(con, apply):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            kb.cmd_fill_evidence_fields(SimpleNamespace(paper="p1", apply=apply), {}, con)
        return out.getvalue()

    def test_dry_run_writes_nothing_then_apply_fills_once_and_keeps_human_values(self):
        with _sandbox() as (root, con):
            path = self.seed(root, con)
            digest = lambda: hashlib.sha256(path.read_bytes()).hexdigest()
            before = digest()
            text = self.run_cmd(con, apply=False)
            self.assertEqual(digest(), before)
            self.assertIn("dry run", text)
            self.assertIn("predicted empty", text)
            self.assertIn("sample", text)
            self.run_cmd(con, apply=True)
            row_db = con.execute("SELECT direction, species FROM evidence WHERE unit_id = ?", (f"{self.PID}:E0001",)).fetchone()
            self.assertEqual(tuple(row_db), ("increase", "mouse"))  # the database row follows the file
            first = digest()
            self.assertNotEqual(first, before)
            u1, u2 = list(db.read_jsonl(path))
            self.assertEqual(u1["outcome"]["direction"], "increase")
            self.assertEqual(u1["model"]["species"], "mouse")
            self.assertEqual(u1["filled_by"]["species"], {"by": "rule", "source": "S0001"})
            self.assertEqual(u1["statement"], "Unit 1 result.")
            self.assertEqual(u2["model"], {"species": "Human"})
            self.assertNotIn("species", u2.get("filled_by", {}))
            self.run_cmd(con, apply=True)  # idempotent
            self.assertEqual(digest(), first)


if __name__ == "__main__":
    unittest.main()
