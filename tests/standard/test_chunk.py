"""Edges of the CH checks that the generic PASS / FAIL cases do not cover."""
import copy
import unittest

from kbsys import conform
from kbsys.conform import Paper, Store

from . import cases_chunk as cases
from .cases_chunk import sent

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


class Fragments(unittest.TestCase):
    def test_linked_fragments_are_clean_but_only_with_a_correct_link(self):
        self.assertEqual(found("CH-02", cases.linked(None)), [])
        bad = found("CH-02", cases.link_to_table_cell(None))
        self.assertTrue(any("tail fragment" in v for v in bad), bad)  # the link is wrong, so the tail is still orphaned

    def test_unlinked_head_and_tail_are_reported_apart(self):
        got = found("CH-02", cases.unlinked(None))
        self.assertEqual(sorted(v.split()[0] for v in got), ["head", "tail"], got)

    def test_not_fragments(self):
        rows = [sent(1, "mRNA levels were measured."), sent(2, "Keywords: cells, vitrification, oocytes"),
                sent(3, "(TIF)"), sent(4, "Group A 5", in_table=True), sent(5, "Fig. 1 shows cells", kind="caption"),
                sent(6, "of the cells", section="references"), sent(7, "Mail: a@b.org"), sent(8, "p<0.05 was used.")]
        self.assertEqual(found("CH-02", Paper("p", sentences=rows)), [])

    def test_a_cut_without_terminal_punctuation_is_a_tail_even_for_one_word(self):
        self.assertEqual(len(found("CH-02", Paper("p", sentences=[sent(1, "The")]))), 1)

    def test_link_problems(self):
        def link(first, second, **kw):
            return Paper("p", sentences=[sent(1, first, continues="p:S0002"), sent(2, second, **kw)])
        self.assertEqual(found("CH-03", link(cases.CUT, cases.REST)), [])
        self.assertIn("terminal", found("CH-03", link("Cells were frozen.", cases.REST))[0])
        self.assertIn("another section", found("CH-03", link(cases.CUT, cases.REST, section="methods"))[0])
        self.assertIn("rest of the head", found("CH-03", link("Cells grew", "Survival was high."))[0])
        self.assertIn("not in this paper", found("CH-03", Paper("p", sentences=[sent(1, cases.CUT, continues="p:S0099")]))[0])
        back = Paper("p", sentences=[sent(1, cases.REST), sent(2, cases.CUT, continues="p:S0001")])
        self.assertIn("later", found("CH-03", back)[0])
        heading = Paper("p", sentences=[sent(1, cases.CUT, continues="p:S0002"), sent(2, "thawed", subsection="thawed")])
        self.assertIn("heading", found("CH-03", heading)[0])

    def test_link_that_skips_the_true_next_part(self):
        rows = [sent(1, cases.CUT, continues="p:S0003"), sent(2, "thawed in warm medium for an hour."), sent(3, "and washed.")]
        self.assertIn("skips", found("CH-03", Paper("p", sentences=rows))[0])
        rows[1] = sent(2, "Group: A | n: 5", kind="table_row", passage_id=None)  # a table row in between is fine
        self.assertEqual(found("CH-03", Paper("p", sentences=rows)), [])

    def test_passage_cut_is_ok_with_a_link_or_table_text(self):
        paper = cases.passages_ok(None)
        self.assertEqual(found("CH-04", paper), [])
        paper.sentences[1].pop("continues")
        self.assertEqual(len(found("CH-04", paper)), 1)
        paper.sentences[1]["in_table"] = True
        self.assertEqual(found("CH-04", paper), [])

    def test_keyword_list_passage_without_a_final_stop_is_not_cut(self):
        text = "Key words: Example stem cells (ESCs); Model tissue; Test cells; Sample injury"
        paper = Paper("p", passages=[cases.passage(1, text)], sentences=[sent(1, text)])
        self.assertEqual(found("CH-04", paper), [])
        paper = Paper("p", passages=[cases.passage(1, cases.CUT)], sentences=[sent(1, cases.CUT)])
        self.assertEqual(len(found("CH-04", paper)), 1)  # prose is still reported

    def test_non_prose_tails_pass_ch04_but_prose_and_a_missing_row_do_not(self):
        def tail(text):
            return Paper("p", passages=[cases.passage(1, text)], sentences=[sent(1, text)])
        for text in ("(TIF)", "STEM CELLS 2016;34:160–173", "For permissions, please e-mail: info@example.org",
                     "Keywords: Tidal pools, mapping, long-range sensing, sensor drift"):
            self.assertEqual(found("CH-04", tail(text)), [], text)
        self.assertEqual(len(found("CH-04", tail("Briefly, a 120-µL aliquot of filtered sea water was"))), 1)
        for text in ("Cells were then treated with recombinant TNF-α", "as shown in the infarct area (Fig. 3A–"):
            self.assertEqual(len(found("CH-04", tail(text))), 1, text)  # a Greek letter or en dash end is not a number end
        self.assertEqual(len(found("CH-04", Paper("p", passages=[cases.passage(1, cases.CUT)], sentences=[]))), 1)  # no row: possible loss
        from kbsys import conform_chunk
        self.assertFalse(conform_chunk._is_list("Abbreviations were defined in the text, as usual, for all groups"))
        self.assertFalse(conform_chunk._is_list("Abbreviations used in this study are cells, groups"))

    def test_split_probe_reports_each_wrong_case(self):
        got = found("CH-01", lambda text: [(0, text.index(".") + 1), (text.index(".") + 2, len(text))])
        self.assertTrue(got)
        self.assertEqual(found("CH-01"), [])


class Tables(unittest.TestCase):
    def test_caption_glued_to_a_body_sentence(self):
        paper = cases.tables_clean(None)
        paper.sentences[3]["text"] = "Survival was highest. Table 1. Survival by group."
        got = found("CH-05", paper)
        self.assertEqual(len(got), 1, got)
        self.assertIn("caption", got[0])

    def test_a_label_only_caption_is_not_a_glue(self):
        paper = cases.tables_clean(None)
        paper.sentences[0]["text"] = "Table 1."
        paper.sentences[3]["text"] = "Survival is shown in Table 1."
        self.assertEqual(found("CH-05", paper), [])

    def test_dump_is_found_and_does_not_depend_on_the_flag_of_other_rows(self):
        got = found("CH-05", cases.table_dumped(None))
        self.assertEqual(len(got), 1, got)

    def test_split_row_zero_rows_and_bad_quality(self):
        split = cases.table(rows=[["A", "1", "2"], ["", "3", "4"]])
        self.assertIn("split", found("CH-06", Paper("p", tables=[split]))[0])
        zero = cases.table(rows=[], quality="ok")
        self.assertIn("0 rows", found("CH-06", Paper("p", tables=[zero]))[0])
        self.assertEqual(len(found("CH-06", Paper("p", tables=[cases.table(quality="good")]))), 1)
        wrapped = cases.table(header=["A", "B"], rows=[["x", "long cell"], ["", "wrapped on"]])
        self.assertIn("split", found("CH-06", Paper("p", tables=[wrapped]))[0])  # 2 columns, one wrapped cell
        span = cases.table(header=list("ABCDEFG"), rows=[["Sperm", "Goat", "AFP", "1", "ml", "up", "(62)"],
                                                       ["", "Pig", "AFGP", "2", "ml", "up", "(63)"]])
        self.assertEqual(found("CH-06", Paper("p", tables=[span])), [])  # rowspan: a new record, not a split
        empty_uncertain = cases.table(header=[], rows=[], quality="uncertain")
        self.assertEqual(found("CH-06", Paper("p", tables=[empty_uncertain])), [])  # flagged, so allowed


class Sections(unittest.TestCase):
    def test_front_matter_after_the_body_started_or_with_a_mention(self):
        base = [sent(1, "Cells were frozen.", section="methods")]
        late = Paper("p", sentences=base + [sent(2, "Some notice here.", section="front_matter")],
                     passages=[cases.passage(2, "x", section="front_matter")])
        self.assertIn("front_matter", found("CH-07", late)[0])
        sidebar = Paper("p", sentences=base + [sent(2, "Some notice here.", section="front_matter")],
                        passages=[cases.passage(2, "x", section="front_matter", sidebar=True)])
        self.assertEqual(found("CH-07", sidebar), [])
        mention = Paper("p", sentences=[sent(1, "See Fig 1.", section="front_matter", mentions=["p:F1"])])
        self.assertEqual(len(found("CH-07", mention)), 1)

    def test_whole_body_in_one_section(self):
        rows = [sent(1, "Cells were frozen.", section="main_text", subsection="A"), sent(2, "Survival was high.", section="main_text"),
                sent(3, "We conclude.", section="conclusion")]
        self.assertEqual(found("CH-07", Paper("p", sentences=rows)), [])  # main_text carrying its sub-headings passes (D-PENDING-19)
        no_headings = [dict(r, subsection=None) for r in rows]
        self.assertEqual(len(found("CH-07", Paper("p", sentences=no_headings))), 1)  # no sub-heading carried; a conclusion apart does not count
        two = copy.deepcopy(rows)
        two[1]["section"] = "results"
        self.assertEqual(found("CH-07", Paper("p", sentences=two)), [])
        intro = [dict(r, section="introduction") for r in rows[:2]]
        self.assertEqual(len(found("CH-07", Paper("p", sentences=intro))), 1)

    def test_reference_entry_pieces_are_not_body(self):
        fragment = cases.passage(1, "Warming stress changes tubule signatures in the test strain. J Cell Test 2013; 8: 56-67.",
                                 section="references")
        self.assertEqual(found("CH-07", Paper("p", passages=[fragment])), [])


class Probes(unittest.TestCase):
    @staticmethod
    def structural_judge(row):
        return row["kind"] != "text" or bool(row.get("in_table")) or row.get("lay_size", 1.0) / row.get("lay_ref", 1.0) < 0.92

    def test_layout_probe_passes_a_structural_judge_and_fails_one_that_never_says_layout(self):
        self.assertEqual(found("CH-08", self.structural_judge), [])
        self.assertTrue(found("CH-08", lambda row: False))

    def test_product_layout_judge_is_wrong_only_for_rows_without_lay_size(self):
        got = found("CH-08")
        # University, over 600 characters: type B, until the G5 re-extraction (many numbers: a verb-bearing lower-case
        # sentence is body since G4.2 CH-02/04 Fix 1)
        self.assertEqual(len(got), 2, got)
        self.assertTrue(all("no lay_size" in v for v in got), got)  # rows that carry lay_size are judged by font size alone

    def test_rows_with_lay_size_are_judged_by_font_size_alone(self):
        from kbsys import sentences
        judge = sentences._layout_interruption
        for text, size, layout in (("University of Tests, Department of Cells", 9.0, False), ("x. " * 300, 9.0, False),
                                   ("1 2 3 4 5 6 7 8 9 10 " * 3, 9.0, False), ("Cells were counted.", 8.27, True),
                                   ("Cells were counted.", 8.3, False)):
            self.assertEqual(judge({"kind": "text", "text": text, "lay_size": size, "lay_ref": 9.0}), layout, (text[:20], size))
        self.assertTrue(judge({"kind": "text", "text": "Cells.", "lay_size": 9.0, "lay_ref": 9.0, "in_table": True}))
        self.assertTrue(judge({"kind": "caption", "text": "Figure 1.", "lay_size": 9.0, "lay_ref": 9.0}))
        self.assertTrue(judge({"kind": "text", "text": "Embryos came from the University hospital."}))  # type B: text rule

    def test_hyphen_probe_passes_a_judge_that_keeps_the_name_whole(self):
        def judge(raw):
            first = raw.split()[0].rstrip(",;.")
            return {"first_author": None if first.startswith("(") else first}
        self.assertEqual(found("CH-10", judge), [])
        self.assertEqual(found("CH-10"), [])  # the product keeps the hyphenated names whole
        self.assertEqual(len(found("CH-10", lambda raw: {"first_author": raw.split("-")[0]})), 3)  # a cutting judge fails

    def test_text_preservation_probe_with_the_product(self):
        self.assertEqual(found("CH-11"), [])

    def test_citation_invariants_flag_unit_errors_of_citations_only(self):
        def unit(report):
            report.error("citations/author_year", "got x")
            report.error("split_sentences", "unrelated")
        store = Store(paper_ids=[])
        self.assertEqual(found("CH-09", store, cases.fake_check(), unit), ["citations/author_year: got x"])
        self.assertEqual(found("CH-09", Store(paper_ids=["p1"]), cases.fake_check(("p1:S0001", "continues must point at a sentence")),
                               lambda report: None), ["p1:S0001: continues must point at a sentence"])


class Pages(unittest.TestCase):
    def test_pass_case_needs_its_exclusions(self):
        paper = cases.pages_clean(None)
        self.assertEqual(found("CH-12", paper), [])
        no_heading = Paper("p", pages=paper.pages, passages=paper.passages, sentences=paper.sentences)
        self.assertEqual(len(found("CH-12", no_heading)), 1)  # without sections.json the heading "Results" is lost text
        unique = copy.deepcopy(paper.pages)
        unique[1]["text"] = unique[1]["text"].replace("Journal of Tests 2", "A different header here")
        self.assertEqual(len(found("CH-12", Paper("p", sections=paper.sections, pages=unique, passages=paper.passages,
                                                  sentences=paper.sentences))), 2)  # not repeated: not furniture

    def test_a_word_broken_at_a_line_end_is_not_compared(self):
        paper = Paper("p", pages=[{"page": 1, "text": "Cells were frozen in the incuba-\ntor overnight."}],
                      passages=[cases.passage(1, "Cells were frozen in the incubator overnight.")],
                      sentences=[sent(1, "Cells were frozen in the incubator overnight.")])
        self.assertEqual(found("CH-12", paper), [])

    def test_a_line_without_a_word_is_no_text_to_keep(self):
        pages = [{"page": 1, "text": "1234567890():,;\n0963-6897/16 $90.00\nCells were frozen."}]
        paper = Paper("p", pages=pages, sentences=[sent(1, "Cells were frozen.")], passages=[cases.passage(1, "Cells were frozen.")])
        self.assertEqual(found("CH-12", paper), [])
        pages[0]["text"] += "\nMitochondria swelled dramatically."
        self.assertEqual(len(found("CH-12", paper)), 1)  # a line with words is still compared

    def test_text_in_a_table_cell_counts_as_kept(self):
        paper = Paper("p", pages=[{"page": 1, "text": "Mitochondria swelled."}], tables=[cases.table(header=["Mitochondria swelled"])],
                      sentences=[sent(1, "Survival was high.")])
        self.assertEqual(found("CH-12", paper), [])


if __name__ == "__main__":
    unittest.main()
