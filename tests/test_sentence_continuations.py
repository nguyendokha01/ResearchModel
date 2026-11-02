"""Offline fixtures for D3.40 sentence continuation and fragment display."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import answer, node_profile, sentences, find, search


def row(sid, text, *, seq, passage=None, kind="text", page=1, **extra):
    return {"sentence_id": sid, "seq": seq, "passage_id": passage or f"p:{seq:04d}",
            "source": "main", "page": page, "section": "results", "kind": kind,
            "text": text, "citations": [], "mentions": [], **extra}


class ContinuationLinkTests(unittest.TestCase):
    def test_skips_caption_figure_and_table_rows_and_records_basis(self):
        rows = [
            row("p:S0001", "The result showed no significant change of", seq=1, page=8),
            row("p:S0002", "Figure 3. An unrelated caption.", seq=2, kind="caption", page=8),
            row("p:S0003", "[figure block]", seq=3, kind="figure", page=8),
            row("p:S0004", "Control | 8 | 12", seq=4, kind="table_row", page=8),
            row("p:S0005", "H3K9me3 between the groups.", seq=5, page=9),
        ]
        original_ids = [r["sentence_id"] for r in rows]
        sentences.link_page_breaks(rows)
        self.assertEqual(rows[0]["continues"], "p:S0005")
        self.assertEqual(rows[0]["context_basis"], "source_continuation")
        self.assertEqual([r["sentence_id"] for r in rows], original_ids)

    def test_existing_page_split_and_lowercase_continuation_still_link(self):
        rows = [
            row("p:S0001", "The measured value was", seq=1, page=1),
            row("p:S0002", "lower in treated embryos.", seq=2, page=2),
        ]
        sentences.link_page_breaks(rows)
        self.assertEqual(rows[0]["continues"], "p:S0002")
        self.assertEqual(rows[0]["context_basis"], "source_continuation")

    def test_links_across_a_front_matter_block_only_for_a_lowercase_start(self):
        def case(tail):
            rows = [
                row("p:S0001", "Other steps governed by gene silencing include the copying of mobile units, such as", seq=1),
                row("p:S0002", "Citation: Doe J (2015) The role of chromatin.", seq=2, section="front_matter"),
                row("p:S0003", "doi: 10.1000/example.2015.00001", seq=3, section="front_matter"),
                row("p:S0004", tail, seq=4, page=2),
            ]
            sentences.link_page_breaks(rows)
            return rows
        rows = case("viruses, transgenes and transposons.")
        self.assertEqual(rows[0]["continues"], "p:S0004")
        self.assertEqual(rows[0]["context_basis"], "source_continuation")
        self.assertNotIn("continues", case("Somatic embryogenesis is a powerful tool.")[0])

    def test_front_matter_block_never_links_complete_or_other_section(self):
        fm = lambda n: row(f"p:S000{n}", "doi: 10.1/x", seq=n, section="front_matter")
        done = [row("p:S0001", "The result was complete.", seq=1), fm(2), row("p:S0003", "among groups.", seq=3)]
        sentences.link_page_breaks(done)
        self.assertNotIn("continues", done[0])
        other = [row("p:S0001", "The result of", seq=1), fm(2), row("p:S0003", "among groups.", seq=3, section="methods")]
        sentences.link_page_breaks(other)
        self.assertNotIn("continues", other[0])
        figure = [row("p:S0001", "The result of", seq=1), fm(2), row("p:S0003", "Fig 1", seq=3, kind="caption"),
                  row("p:S0004", "among groups.", seq=4)]
        sentences.link_page_breaks(figure)
        self.assertEqual(figure[0]["continues"], "p:S0004")  # caption was already skipped before this change

    def test_terminal_or_unrelated_body_text_is_not_skipped(self):
        terminal = [
            row("p:S0001", "The result was complete.", seq=1),
            row("p:S0002", "among the three groups.", seq=2),
        ]
        sentences.link_page_breaks(terminal)
        self.assertNotIn("continues", terminal[0])

        unrelated = [
            row("p:S0001", "The result was higher", seq=1),
            row("p:S0002", "A new sentence starts here.", seq=2),
            row("p:S0003", "the later fragment.", seq=3),
        ]
        sentences.link_page_breaks(unrelated)
        self.assertNotIn("continues", unrelated[0])

    def test_read_time_link_keeps_legacy_file_unchanged_and_invalidates_cache(self):
        rows = [
            row("p:S0001", "The result showed no significant change of", seq=1, page=8),
            row("p:S0002", "Figure 3. An unrelated caption.", seq=2, kind="caption", page=8),
            row("p:S0003", "H3K9me3 between the groups.", seq=3, page=9),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sentences.jsonl"
            original = b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in rows)
            path.write_bytes(original)
            linked = sentences.cached_linked_rows(rows, path)
            self.assertEqual(linked[0]["continues"], "p:S0003")
            self.assertEqual(path.read_bytes(), original)

            changed = [row("p:S0001", "The result was complete.", seq=1, page=8),
                       row("p:S0003", "among the groups.", seq=3, page=9)]
            path.write_bytes(b"changed-source-signature\n")
            refreshed = sentences.cached_linked_rows(changed, path)
            self.assertNotIn("continues", refreshed[0])


class FragmentQuoteTests(unittest.TestCase):
    def test_answer_links_legacy_caption_interruption_at_read_time(self):
        rows = [
            row("p:S0001", "The result showed no significant change of", seq=1, page=8),
            row("p:S0002", "Figure 3. An unrelated caption.", seq=2, kind="caption", page=8),
            row("p:S0003", "H3K9me3 between the groups.", seq=3, page=9),
        ]
        with patch.object(answer, "read_jsonl", return_value=rows):
            self.assertEqual(
                answer._whole_quote({"id": "p:S0001", "paper_id": "p", "text": rows[0]["text"]}, {}),
                "The result showed no significant change of H3K9me3 between the groups.",
            )

    def test_answer_prefixes_unlinked_lowercase_start_and_keeps_page_join(self):
        rows = [
            row("p:S0001", "among the three groups", seq=1, passage="p:cut"),
            row("p:S0002", "the treated group differed.", seq=2, passage="p:cut"),
            row("p:S0003", "In group B, no significant change of", seq=3),
            row("p:S0004", "H3K27me3 was found.", seq=4),
        ]
        rows[2]["continues"] = rows[3]["sentence_id"]
        with patch.object(answer, "read_jsonl", return_value=rows):
            cut = answer._whole_quote({"id": "p:S0001", "paper_id": "p", "text": rows[0]["text"]}, {})
            self.assertTrue(cut.startswith("[…] among the three groups"))
            self.assertIn("câu bị cắt", cut)
            self.assertEqual(
                answer._whole_quote({"id": "p:S0004", "paper_id": "p", "text": rows[3]["text"]}, {}),
                "In group B, no significant change of H3K27me3 was found.",
            )

    def test_answer_does_not_prefix_fragment_with_linked_predecessor(self):
        rows = [
            row("p:S0001", "The result was", seq=1),
            row("p:S0002", "among the three groups.", seq=2),
        ]
        rows[0]["continues"] = rows[1]["sentence_id"]
        with patch.object(answer, "read_jsonl", return_value=rows):
            self.assertEqual(
                answer._whole_quote({"id": "p:S0002", "paper_id": "p", "text": rows[1]["text"]}, {}),
                "The result was among the three groups.",  # linked predecessor joined, no "[…]" prefix
            )
        self.assertEqual(answer._whole_quote({"id": "p:E0001", "text": "a stored unit quote."}, {}),
                         "a stored unit quote.")

    def test_source_backed_note_ref_has_same_fragment_qualification(self):
        rows = {"p:S0001": row("p:S0001", "among the three groups.", seq=1)}
        refs = node_profile._refs(["p:S0001"], rows, "p", {"_references": {}})
        self.assertEqual(refs[0]["text"], "[…] among the three groups.")


    def test_node_profile_links_legacy_caption_interruption(self):
        rows = {
            "p:S0001": row("p:S0001", "The result showed no significant change of", seq=1, page=8),
            "p:S0002": row("p:S0002", "Figure 3. An unrelated caption.", seq=2, kind="caption", page=8),
            "p:S0003": row("p:S0003", "H3K9me3 between the groups.", seq=3, page=9),
        }
        refs = node_profile._refs(["p:S0001"], rows, "p", {"_references": {}})
        self.assertEqual([ref["text"] for ref in refs],
                         ["The result showed no significant change of", "H3K9me3 between the groups."])


class ReadTimeContinuationTests(unittest.TestCase):
    def test_legacy_footer_license_table_gap_and_uppercase_continuation(self):
        rows = [
            row("zhang:S0020", "This was associated with ovarian insufficiency", seq=20, passage="p:1"),
            row("zhang:S0021", "© 2026 Dove Press. All rights reserved.", seq=21),
            row("zhang:S0022", "Further terms of this licence can be read at the journal site.", seq=22),
            row("zhang:S0023", "A footer affiliation@example.org", seq=23),
            row("zhang:S0027", "(POI).", seq=27),
            row("wang:S0022", "The embryos were transferred into a stable", seq=22, passage="p:2"),
            row("wang:S0030", "Control | 4 | 8 | 12", seq=30, kind="table_row", passage="p:2"),
            row("wang:S0035", "frozen state.", seq=35, passage="p:3"),
            row("abd:S0180", "The histone mark was retained in the treated embryos", seq=180),
            row("abd:S0181", "H3K9me3 remains detectable.", seq=181),
        ]
        linked = sentences.link_page_breaks_copy(rows)
        by_id = {r["sentence_id"]: r for r in linked}
        self.assertEqual(by_id["zhang:S0020"]["continues"], "zhang:S0027")
        self.assertEqual(by_id["wang:S0022"]["continues"], "wang:S0035")
        self.assertEqual(by_id["abd:S0180"]["continues"], "abd:S0181")

    def test_existing_correct_links_remain_and_wrong_persisted_sources_are_removed(self):
        rows = [
            row("p:S0001", "The result showed no significant change of", seq=1),
            row("p:S0002", "H3K9me3 between groups.", seq=2),
            row("p:S0003", "Table 1 161 42 83", seq=3, kind="table_row", continues="p:S0004"),
            row("p:S0004", "genes.", seq=4),
            row("p:S0005", "The licence text ended here.", seq=5, continues="p:S0006"),
            row("p:S0006", "(POI).", seq=6),
        ]
        rows[0]["continues"] = "p:S0002"
        linked = sentences.link_page_breaks_copy(rows)
        by_id = {r["sentence_id"]: r for r in linked}
        self.assertEqual(by_id["p:S0001"]["continues"], "p:S0002")
        self.assertNotIn("continues", by_id["p:S0003"])
        self.assertNotIn("continues", by_id["p:S0005"])

    def test_same_passage_word_cut_joins_without_a_space(self):
        rows = [
            row("zhang:S0275", "polycyst", seq=275, passage="long", start=0, end=8),
            row("zhang:S0276", "ic ovary syndrome.", seq=276, passage="long", start=8, end=26),
        ]
        linked = sentences.link_page_breaks_copy(rows)
        self.assertEqual(linked[0]["continues"], "zhang:S0276")
        with patch.object(answer, "read_jsonl", return_value=rows):
            self.assertEqual(answer._whole_quote({"id": rows[0]["sentence_id"], "paper_id": "zhang",
                                                  "text": rows[0]["text"]}, {}),
                             "polycystic ovary syndrome.")

    def test_terminal_citation_does_not_get_cut_marker(self):
        self.assertEqual(answer._whole_quote({"id": "yang:S0043", "paper_id": "yang",
                                              "text": "The dose was 10?5 mol/L.^132,134"}, {}),
                         "The dose was 10?5 mol/L.^132,134")

    def test_flags_preserve_source_glyph_and_table_text(self):
        glyph = row("yang:S0043", "The dose was 10?5 mol/L.", seq=43)
        table = row("yang:S0380", "BAX gene expression 10 mg Fe/ml 20 30", seq=380)
        linked = sentences.link_page_breaks_copy([glyph, table])
        self.assertTrue(linked[0]["source_glyph_suspect"])
        self.assertTrue(linked[1]["table_text_suspect"])
        self.assertIn("10?5", linked[0]["text"])
        self.assertTrue(sentences.table_text_suspect(row("p:S1", "Control | 10 | 20 | 30", seq=1)))
        self.assertFalse(sentences.table_text_suspect(row("p:S2", "The embryos were vitrified at 0°C.", seq=2)))

    def test_heading_display_and_search_glyph_folding_keep_raw_text(self):
        heading = row("p:S1", "Methods The embryos were vitrified.", seq=1)
        self.assertEqual(sentences.display_text(heading), "Methods\nThe embryos were vitrified.")
        self.assertIn("◦C", find.fts_expression([["°C"]]))
        self.assertIn("^◦C", search.parse_query("°C", {"synonyms": []})[0])
        self.assertEqual(sentences.fold_search_glyphs("-60 ^◦C"), "-60 °C")

    def test_answer_and_readers_never_write_sentence_bytes(self):
        rows = [row("p:S1", "A result showed no significant change of", seq=1),
                row("p:S2", "H3K9me3.", seq=2)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sentences.jsonl"
            original = b"".join((json.dumps(r, ensure_ascii=False) + "\n").encode("utf-8") for r in rows)
            path.write_bytes(original)
            linked = sentences.cached_linked_rows(rows, path)
            self.assertEqual(path.read_bytes(), original)
            with patch.object(answer, "read_jsonl", return_value=rows):
                answer._whole_quote({"id": "p:S1", "paper_id": "p", "text": rows[0]["text"]}, {})
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(linked[0]["continues"], "p:S2")


class PageBreakRuleTests(unittest.TestCase):
    """One should-link and one must-not-link case per rule of the page-break linker (G4.2 CH-02/CH-04 Fix 1)."""

    def links(self, *texts):
        rows = [row(f"p:S{n:04d}", text, seq=n, page=n) for n, text in enumerate(texts, 1)]
        sentences.link_page_breaks(rows)
        return [r.get("continues") for r in rows]

    def test_short_head_links_only_when_it_ends_in_a_function_word(self):
        self.assertEqual(self.links("Although", "Cells did not reduce the lesion size.")[0], "p:S0002")
        self.assertEqual(self.links("Otherwise, a", "Kruskal-Wallis test was used.")[0], "p:S0002")
        self.assertIsNone(self.links("Cells grew well", "Samples were pooled.")[0])
        self.assertIsNone(self.links("Because", "Samples were pooled.")[0])  # "because" is not in the set

    def test_numeric_sentence_is_body_but_figure_panel_text_is_not(self):
        head = ("Next, QRS yield values shifted with several voltage settings (5 V, 8 V, 12 V) and "
                "several mixes of Fe3O4 and GO NPs (0.2% and 0.05%, 0.3% and")
        self.assertEqual(self.links(head, "0.05%, 0.4% and 0.05%, w/v).")[0], "p:S0002")
        panel = ("15 Linked widget stack 30 min Linked widget stack 0 0.1 0.2 0.3 0.5 0.7 1.0 1.5 M KCl "
                 "Beam frame e Q α1 Q α2 R α2 Q α2 Q α1 S α1 Axis X GAP 1.5 X GAP 0.5 X GAP 1.5 X GAP 0.5")
        rows = [row("p:S0001", panel, seq=1, page=6), row("p:S0002", "Fig. 5 Rearrangement of the widget stack.",
                                                          seq=2, kind="caption", page=6),
                row("p:S0003", "whereas the straight gap between the GAP 1.5 and GAP 5.5 is 70 Å (Fig. 4b).", seq=3, page=7)]
        sentences.link_page_breaks(rows)
        self.assertNotIn("continues", rows[0])  # a figure panel linked to a later row was a wrong link

    def test_affiliation_word_in_a_sentence_with_a_verb_stays_body(self):
        head = "Animal experiments were reviewed and approved by the Animal Ethics Board of the Example University"
        self.assertEqual(self.links(head, "Graduate School of Medicine.")[0], "p:S0002")
        self.assertEqual(self.links("Pets were reviewed and approved by Local Pet",
                                    "Welfare Board of Harbour Hospital, Example University (no. 2031) and was conducted accordingly.")[0], "p:S0002")
        self.assertIsNone(self.links("Department of Biology, University of Tokyo, Japan and the", "Graduate School of Medicine.")[0])
        # a target that mentions a university but does not read as the rest of the head is skipped, never taken
        self.assertIsNone(self.links("Cells were washed", "Samples were stored at the University of Tokyo for later analysis.")[0])
        # an affiliation line whose "Applied"/"Engineering" reads as a verb is still layout, never a head
        self.assertIsNone(self.links("Department of Applied Biology, Kyoto University, Kyoto, Japan", "Vitrification is widely used.")[0])
        self.assertIsNone(self.links("Department of Bioengineering, University of Tokyo, Japan and the", "Graduate School of Medicine.")[0])

    def test_slash_start_and_hyphen_end_continue(self):
        self.assertEqual(self.links("As shown in Fig. 7, peak tide heights", "/ maximum ice fractions occurred late.")[0], "p:S0002")
        self.assertEqual(self.links("the sheep reference genome (Ovis_aries/OAR-", "Oar_v3.1 reference genome).")[0], "p:S0002")
        self.assertIsNone(self.links("The cells grew well.", "/ maximum ice fractions occurred late.")[0])  # complete head
        self.assertIsNone(self.links("The cells grew well", "Samples were pooled.")[0])

    def test_one_word_tail_in_another_passage_links(self):
        head = "One bead moved but stayed attached to the same rod"
        self.assertEqual(self.links(head, "RIM.")[0], "p:S0002")
        rows = [row("p:S0001", head, seq=1, passage="p:1", start=0, end=len(head)), row("p:S0002", "RIM.", seq=2, passage="p:1", start=500, end=504)]
        sentences.link_page_breaks(rows)
        self.assertNotIn("continues", rows[0])  # same passage and not adjacent: not a page break
        # a capitalised one-word row is a run-in heading or a label, not the tail ("Antibodies.")
        self.assertIsNone(self.links("Slides were counterstained with DAPI", "Antibodies.")[0])
        self.assertIsNone(self.links("Slides were counterstained with DAPI", "S1 Dataset.")[0])

    def test_lowercase_methods_sentence_with_numbers_is_not_a_table_row(self):
        head = "Briefly, each slide was treated with the first antibody against ABC-9 (mouse monoclonal antibody, dilution 1:200, CAT1234; Acme)"
        tail = "overnight at 4°C, and then with Rhodamine (RHO) Pure (sheep antimouse IgG, dilution 1:50; Acme Labs, Inc.) for 2 hours at room temperature."
        self.assertEqual(self.links(head, tail)[0], "p:S0002")
        self.assertTrue(sentences.table_text_suspect(row("p:S0002", tail, seq=2)))  # the table flag itself is unchanged
        self.assertIsNone(self.links(head, "total counts 12 15 17 19 21 23")[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
