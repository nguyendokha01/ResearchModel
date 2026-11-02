"""G4.3 small validator fixes: EV-01, EV-03, EV-05, EV-08, EV-16. Each fix has a PASS case (the false alarm is gone)
and a FAIL case (the real problem is still reported)."""
import unittest

from kbsys import concepts, evidence


def sentence(n, text, section="results", **extra):
    return {"sentence_id": f"P:S{n:04d}", "seq": n, "passage_id": "a", "page": 1, "section": section, "kind": "text",
            "text": text, "citations": [], "mentions": [], **extra}


def make_ctx(rows):
    sents = {s["sentence_id"]: s for s in rows}
    ctx = evidence.PaperContext.__new__(evidence.PaperContext)
    ctx.paper_id, ctx.meta, ctx.table_caption_ids, ctx.refs, ctx.sentences = "P", {}, set(), {}, sents
    ctx._index()
    ctx.registry, ctx.canonical = concepts.Registry([]), {"_groups": {}}
    return ctx


def check_unit(ctx, **unit):
    errors, flags, normalized = [], [], []
    got = evidence._check_unit(ctx, 1, {"unit_type": "result", "terms": {"concepts": ["Survival"]}, **unit}, False,
                               lambda i, c, m: errors.append(c), lambda i, c, m: flags.append((c, m)), normalized)
    return got, errors, flags


class ResearchQuestionTests(unittest.TestCase):
    def flags(self, rows, ids):
        flags = []
        evidence._check_research_question(make_ctx(rows), {"text": "Why?", "sentence_ids": ids},
                                          lambda *a: self.fail(a), lambda i, c, m: flags.append(c))
        return flags

    def test_review_without_body_headings_may_cite_main_text(self):
        rows = [sentence(1, "Abstract text.", "abstract"), sentence(2, "This review summarises the evidence.", "main_text")]
        self.assertEqual(self.flags(rows, ["S0002"]), [])

    def test_paper_with_imrad_structure_still_flagged_for_main_text_and_results(self):
        rows = [sentence(1, "Aim of the study.", "introduction"), sentence(2, "Loose text.", "main_text"),
                sentence(3, "Survival rose.", "results")]
        self.assertEqual(self.flags(rows, ["S0002"]), ["V1"])
        self.assertEqual(self.flags(rows, ["S0003"]), ["V1"])
        self.assertEqual(self.flags(rows, ["S0001"]), [])
        # a main_text-only paper citing its conclusion is still outside abstract/introduction
        rows = [sentence(1, "Text.", "main_text"), sentence(2, "We conclude.", "conclusion")]
        self.assertEqual(self.flags(rows, ["S0002"]), ["V1"])


class GroundedTests(unittest.TestCase):
    def ctx(self, *texts):
        return make_ctx([sentence(n, t) for n, t in enumerate(texts, start=1)])

    def test_acronym_and_expansion(self):
        ctx = self.ctx("Magnetic resonance imaging showed the graft.", "Oocytes were studied by FISH.")
        self.assertTrue(ctx.grounded("MRI"))
        self.assertFalse(ctx.grounded("MITF"))                       # real absence stays reported
        self.assertFalse(self.ctx("Magnetic resonance was used.").grounded("MRI"))
        self.assertFalse(self.ctx("The cat sat on a mat.").grounded("TCS"))  # a stop word is no part of an expansion
        self.assertFalse(self.ctx("Mice in a box.").grounded("MIB"))  # nor a word of under 3 letters
        self.assertFalse(self.ctx("The cat sat on the mat.").grounded("CSM"))  # initials of stop words are no expansion
        self.assertTrue(self.ctx("MRI showed the graft.").grounded("Magnetic resonance imaging"))

    def test_chi_square(self):
        self.assertTrue(self.ctx("Groups were compared using a χ^2 test.").grounded("Chi-square test"))
        self.assertTrue(self.ctx("A chi2 test was used.").grounded("Chi-square test"))
        self.assertFalse(self.ctx("A t test was used.").grounded("Chi-square test"))

    def test_derivational_stem(self):
        self.assertTrue(self.ctx("The cells were filtered through a mesh.").grounded("Filtration"))
        self.assertFalse(self.ctx("Solutions passed through 0.22 um filters.").grounded("Filtration"))  # a noun is not the process
        self.assertFalse(self.ctx("Cells were imaged.").grounded("3D deconvolution"))


class DirectionTests(unittest.TestCase):
    def flags(self, text, direction="no_change", measure="Flag-H3.3 deposition"):
        got, errors, flags = check_unit(make_ctx([sentence(1, text)]), statement="Survival did not change here.",
                                        sentence_ids=["S0001"], outcome={"direction": direction, "measure": measure})
        self.assertEqual(errors, [])
        return [c for c, _ in flags if c == "V7"]

    def test_loss_in_a_retention_sentence_is_not_a_decrease(self):
        text = "Flag-H3.3 deposition was maintained in G1 nuclei; loss of H3.3 occurs only after meiosis."
        self.assertEqual(self.flags(text), [])
        self.assertEqual(self.flags("Flag-H3.3 deposition showed a loss in G1 nuclei."), ["V7"])  # FAIL case stays

    def test_equally_low_in_both_groups_is_no_change(self):
        self.assertEqual(self.flags("The TSH level was equally low in both groups (2.4 mU/L).", measure="TSH level"), [])
        self.assertEqual(self.flags("The TSH level was low in both groups (2.4 mU/L).", measure="TSH level"), ["V7"])
        # "equally reduced" keeps its direction: it must not hide a wrong 'increase'
        self.assertEqual(self.flags("The TSH level was equally reduced in both groups.", direction="increase", measure="TSH level"), ["V7"])

    def test_a_quantified_loss_next_to_a_retention_word_is_still_a_decrease(self):
        text = "Deposition was maintained in controls, but viability showed a loss of 40% after warming."
        self.assertEqual(self.flags(text), ["V7"])


class StatementWordsTests(unittest.TestCase):
    ASSOC = "Exposure is associated with the upregulation of gene family Q."

    def test_participles_after_to_with_of_and_are_not_causal_verbs(self):
        for statement in ("The review links the change to reduced sperm motility.",
                          "The review associates exposure with increased HDAC expression.",
                          "Linked to increased deacetylation, suppressed gene expression, and reduced success.",
                          "Disruption of ovarian regulation and fertility.",
                          "Associated with Polycomb-mediated silencing.",
                          "Acetylation did not primarily determine nucleosome packing."):
            self.assertEqual(evidence._stronger_words(statement, self.ASSOC), [], statement)

    def test_real_causal_verbs_are_still_found(self):
        for statement, word in (("Exposure reduced sperm motility.", "reduced"), ("Exposure regulates HDAC expression.", "regulates"),
                                ("Exposure caused the change.", "caused"), ("Exposure promotes and reduces motility.", "promotes")):
            self.assertIn(word, evidence._stronger_words(statement, self.ASSOC), statement)
        for statement, word in (("Exposure up-regulates HDAC expression.", "regulates"), ("Exposure down-regulated HDAC activity.", "regulated"),
                                ("Exposure not only caused the change but more.", "caused")):
            self.assertIn(word, evidence._stronger_words(statement, self.ASSOC), statement)
        # a second verb after "and" is a verb when no "to/with" opens a list in the sentence
        self.assertEqual(evidence._stronger_words("Exposure increased motility and reduced success.",
                                                  "Exposure is associated with increased motility."), ["reduced"])

    def test_seeming_and_potential_keep_the_source_hedge(self):
        ctx = make_ctx([sentence(1, "Muse cells do not seem to bind a niche, a possible target.", "introduction")])
        base = dict(unit_type="background", sentence_ids=["S0001"])
        for statement, flagged in (("Muse cells are not seeming to bind a niche.", False),
                                   ("Muse cells are a potential target of the work.", False),
                                   ("Muse cells bind a niche here.", True),
                                   ("Muse cells have a high membrane potential.", True)):
            _, errors, flags = check_unit(ctx, statement=statement, **base)
            self.assertEqual(("V5" in [c for c, _ in flags]), flagged, statement)


class QuoteGapTests(unittest.TestCase):
    ROWS = [sentence(1, "Cells were counted", continues="P:S0003"), sentence(2, "Figure 1. A caption.", kind="caption"),
            sentence(3, "after warming."), sentence(5, "Next sentence.")]

    def test_no_marker_between_the_halves_of_one_sentence(self):
        self.assertEqual(evidence._quote([self.ROWS[0], self.ROWS[2]]), "Cells were counted after warming.")
        self.assertEqual(evidence._quote(self.ROWS[:1] + self.ROWS[2:]), "Cells were counted after warming. […] Next sentence.")
        self.assertEqual(evidence._quote([self.ROWS[0], self.ROWS[3]]), "Cells were counted […] Next sentence.")  # FAIL case: real gap

    def test_stored_quotes_with_the_marker_do_not_drift(self):
        by_id = {s["sentence_id"]: s for s in self.ROWS}
        old = {"sentence_ids": ["P:S0001", "P:S0003"], "quote": "Cells were counted […] after warming.",
               "sections": ["results"]}
        self.assertEqual(evidence.source_drift([old], by_id, "P"), [])
        changed = {**old, "quote": "Cells were counted […] after thawing."}
        self.assertEqual(evidence.source_drift([changed], by_id, "P"), [1])

    def test_conform_reports_a_stored_marker_once_as_ev16_and_not_as_a_differing_quote(self):
        from types import SimpleNamespace
        from kbsys import conform_evidence
        unit = {"unit_id": "P:E0001", "sentence_ids": ["P:S0001", "P:S0003"]}
        old = conform_evidence._quote_problems(SimpleNamespace(sentences=self.ROWS, units=[
            {**unit, "quote": "Cells were counted […] after warming."}]))
        self.assertEqual([m.split(" (")[0] for _, m in old], ["gap marker inside one sentence"])
        # a real gap (S0003 -> S0005) beside the marker inside the sentence, its own marker dropped
        dropped = conform_evidence._quote_problems(SimpleNamespace(sentences=self.ROWS, units=[
            {**unit, "sentence_ids": ["P:S0001", "P:S0003", "P:S0005"], "quote": "Cells were counted […] after warming. Next sentence."}]))
        self.assertEqual([m for _, m in dropped], ["quote differs from the text rebuilt from the cited sentences"])
        fresh = conform_evidence._quote_problems(SimpleNamespace(sentences=self.ROWS, units=[
            {**unit, "quote": "Cells were counted after warming."}]))
        self.assertEqual(list(fresh), [])
        other = conform_evidence._quote_problems(SimpleNamespace(sentences=self.ROWS, units=[{**unit, "quote": "Other."}]))
        self.assertEqual([m for _, m in other], ["quote differs from the text rebuilt from the cited sentences"])


if __name__ == "__main__":
    unittest.main()
