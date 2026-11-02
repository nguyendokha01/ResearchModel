"""CH-08: layout interruptions recognised by font size (`lay` on passages, `lay_size` / `lay_ref` / `lay_y` on sentences)."""
import tempfile
import unittest
from pathlib import Path

import pymupdf

from kbsys import extract, sentences


def passage(n, section, text, lay, **extra):
    return {"passage_id": f"p:{n:04d}", "page": 1, "section": section, "kind": "text", "text": text, "lay": lay, **extra}


def rows(passages):
    return sentences.build("p", passages, sentences.CitationContext([]))


def judged(one, *others):
    return next(r for r in rows([one, *others]) if r["passage_id"] == one["passage_id"])


class BlockToSentence(unittest.TestCase):
    def test_a_sentence_spanning_two_blocks_is_measured_by_its_largest_block_of_20_percent(self):
        text = "Cells were frozen and then thawed in warm medium."  # 49 characters: 29 at 10 pt (59 %), 20 at 6 pt (41 %)
        row, = rows([passage(1, "methods", text, [[29, 10.0, 0.80, 0.90], [20, 6.0, 0.05, 0.10]])])
        self.assertEqual((row["lay_size"], row["lay_ref"]), (10.0, 10.0))
        self.assertEqual(row["lay_y"], [0.05, 0.9])
        self.assertNotIn("lay_rel", row)

    def test_offsets_are_scaled_to_the_text_left_after_hyphen_resolution(self):
        # the blocks were counted as 17 + 16 characters; hyphen resolution left 31, so the boundary moves to 15.97
        text = "Aaaa bbbb cccc. Dddd eeee ffff."
        first, second = rows([passage(1, "methods", text, [[17, 10.0, 0.1, 0.2], [16, 6.0, 0.3, 0.4]])])
        self.assertEqual((first["lay_size"], first["lay_y"]), (10.0, [0.1, 0.2]))
        self.assertEqual((second["lay_size"], second["lay_y"]), (6.0, [0.3, 0.4]))

    def test_passages_read_back_from_passages_jsonl_give_the_same_rows(self):  # tier1.build without a PDF
        import json
        stored = [passage(1, "methods", "Aaaa bbbb cccc. Dddd eeee ffff.", [[17, 10.0, 0.1, 0.2], [16, 6.0, 0.3, 0.4]])]
        self.assertEqual(rows(json.loads(json.dumps(stored))), rows(stored))

    def test_rows_without_lay_get_no_field(self):
        row, = rows([{"passage_id": "p:0001", "page": 1, "section": "methods", "kind": "text", "text": "Cells grew."}])
        for key in ("lay_size", "lay_ref", "lay_y"):
            self.assertNotIn(key, row)

    def test_split_passage_parts_keep_their_own_blocks(self):
        text = "Aaaa bbbb cccc. Dddd eeee ffff."
        lay = [[15, 10.0, 0.1, 0.2], [16, 6.0, 0.3, 0.4]]
        self.assertEqual(sentences.layout_slice(lay, 31, 16, 31), [(15.0, 6.0, 0.3, 0.4)])
        self.assertEqual(sentences.layout_slice(lay, 31, 0, 15), [(15.0, 10.0, 0.1, 0.2)])
        self.assertEqual(text[16:], "Dddd eeee ffff.")


class Judge(unittest.TestCase):
    def body(self, size):
        return passage(1, "introduction", "Cells were counted. " * 50, [[1000, size, 0.1, 0.9]])

    def test_a_row_set_in_small_print_is_layout(self):  # paper C S0022: affiliations, one 7.46 pt block, body about 8.2 pt
        row = judged(passage(2, "introduction", "^1Department of Harbour Studies and Ocean Science.",
                             [[60, 7.46, 0.05, 0.08]]), self.body(8.2))
        self.assertEqual((row["lay_size"], row["lay_ref"]), (7.46, 8.2))
        self.assertTrue(sentences._layout_interruption(row))  # 7.46 / 8.2 = 0.91

    def test_a_body_tail_glued_to_small_print_is_body(self):  # paper D S0217: 0.73 at 7.0 pt, 0.27 at 9.8 pt, ref 9.8
        text = "D day of tank rinse, K core at the ring stage, and plates grew well after that day."
        cut = round(len(text) * 0.73)
        row = judged(passage(2, "introduction", text, [[cut, 7.0, 0.9, 0.95], [len(text) - cut, 9.8, 0.2, 0.3]]), self.body(9.8))
        self.assertEqual((row["lay_size"], row["lay_ref"]), (9.8, 9.8))
        self.assertFalse(sentences._layout_interruption(row))

    def test_table_cells_without_a_block_of_20_percent_fall_back_to_the_mean(self):  # paper A S0189
        lay = [[4, 8.97, 0.5, 0.52]] * 2 + [[4, 6.97, 0.5, 0.52]] * 38
        row = judged(passage(2, "introduction", "cell " * 40, lay), self.body(9.0))
        self.assertAlmostEqual(row["lay_size"], 7.07, places=2)  # the mean, not the 8.97 pt of the biggest cell
        self.assertTrue(sentences._layout_interruption(row))

    def test_a_smaller_font_methods_section_is_never_layout_against_its_own_reference(self):  # paper E / paper F
        methods = passage(2, "methods", "Cells were frozen. " * 20, [[380, 7.0, 0.1, 0.9]])
        got = rows([self.body(9.5), methods])
        self.assertEqual({(r["lay_size"], r["lay_ref"]) for r in got if r["section"] == "methods"}, {(7.0, 7.0)})
        self.assertFalse(any(sentences._layout_interruption(r) for r in got))

    def test_text_rules_do_not_apply_to_a_row_with_lay_size(self):
        text = "Department of Cells, University of Tests. " * 30  # over 600 characters, "University"
        row = {"kind": "text", "lay_size": 9.0, "lay_ref": 9.0, "text": text}
        self.assertFalse(sentences._layout_interruption(row))
        self.assertTrue(sentences._layout_interruption({"kind": "text", "text": text}))  # no keys: the text rule

    def test_a_page_one_footer_line_at_or_above_body_size_is_layout(self):  # paper G S0032: 9.53 / 8.96, top 0.96
        line = {"kind": "text", "page": 1, "lay_size": 9.53, "lay_ref": 8.96, "lay_y": [0.96, 0.97],
                "text": "EXAMPLE JOURNAL OF SCIENCE 2031;4:1-10 www.example-journal.org"}
        self.assertTrue(sentences._layout_interruption(line))
        self.assertFalse(sentences._layout_interruption({**line, "page": 2}))  # page 1 only
        self.assertFalse(sentences._layout_interruption({**line, "lay_y": [0.84, 0.86]}))  # above the footer band

    def test_the_last_body_line_of_page_one_stays_body(self):  # paper I S0023 / paper J S0020: top 0.86-0.87, ratio 1.00
        line = {"kind": "text", "page": 1, "lay_size": 9.0, "lay_ref": 9.0, "lay_y": [0.87, 0.9],
                "text": "Resveratrol is a phytoalexin contained in"}
        self.assertFalse(sentences._layout_interruption(line))
        self.assertFalse(sentences._layout_interruption({**line, "lay_size": 9.25}))  # 1.028 is not above 1.03


class ReferenceSize(unittest.TestCase):
    BODY = passage(1, "results", "Cells were counted. " * 50, [[1000, 10.0, 0.1, 0.9]])

    def test_reference_is_the_mean_of_the_modal_half_point_bin(self):
        self.assertEqual(sentences._size_ref([(8.1, 300), (8.2, 100), (9.9, 150)]), round((8.1 * 300 + 8.2 * 100) / 400, 2))
        self.assertIsNone(sentences._size_ref([]))

    def test_front_matter_is_measured_against_the_paper_body_not_its_own_small_print(self):
        small = passage(2, "front_matter", "Received: 1 May 2020", [[20, 8.0, 0.3, 0.31]])
        row = rows([small, self.BODY])[0]
        self.assertEqual((row["lay_size"], row["lay_ref"]), (8.0, 10.0))
        self.assertTrue(sentences._layout_interruption(row))
        big = passage(2, "front_matter", "A title of the paper", [[20, 18.0, 0.1, 0.2]])
        self.assertFalse(sentences._layout_interruption(rows([big, self.BODY])[0]))

    def test_a_results_and_discussion_body_is_the_front_matter_reference(self):  # extract label `results_discussion`
        small = passage(2, "front_matter", "Received: 1 May 2020", [[20, 8.0, 0.3, 0.31]])
        body = passage(1, "results_discussion", "Cells were counted. " * 50, [[1000, 10.0, 0.1, 0.9]])
        row = rows([small, body])[0]
        self.assertEqual(row["lay_ref"], 10.0)
        self.assertTrue(sentences._layout_interruption(row))

    def test_small_print_inside_a_section_is_layout_and_back_matter_uses_its_own_section(self):
        results = passage(2, "results", "Group A is shown in the table.", [[31, 8.0, 0.9, 0.95]])
        thanks = passage(3, "acknowledgements", "We thank the staff.", [[19, 8.0, 0.5, 0.6]])
        got = rows([self.BODY, results, thanks])
        self.assertTrue(sentences._layout_interruption(next(r for r in got if r["passage_id"] == "p:0002")))
        self.assertEqual(next(r for r in got if r["section"] == "acknowledgements")["lay_ref"], 8.0)

    def test_section_without_text_passages_falls_back_to_the_body_and_no_body_gives_its_own_mode(self):
        legend = passage(2, "figure_legends", "Figure 1. Cells.", [[16, 8.0, 0.5, 0.6]], kind="caption")
        self.assertEqual(rows([self.BODY, legend])[-1]["lay_ref"], 10.0)  # captions are not in the text modes
        self.assertEqual(sentences.layout_reference_sizes([self.BODY])[None], 10.0)
        lone = passage(2, "front_matter", "Received: 1 May 2020", [[20, 8.0, 0.3, 0.31]])
        self.assertEqual(rows([lone])[0]["lay_ref"], 8.0)  # no body at all: its own mode


class Extraction(unittest.TestCase):
    def test_passage_carries_one_entry_per_block_in_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = str(Path(tmp) / "a.pdf")
            with pymupdf.open() as doc:
                page = doc.new_page(width=600, height=840)
                page.insert_text((70, 60), "Methods", fontsize=16)
                page.insert_textbox((70, 70, 530, 140), "Cells were frozen in medium. " * 3, fontsize=10)
                page.insert_textbox((70, 300, 530, 360), "Small print follows the main text here. " * 2, fontsize=7)
                doc.save(pdf)
            ex = extract.extract(pdf, ocr={"enabled": False})
        passage_ = next(p for p in ex.passages if p["text"].startswith("Cells were frozen"))
        self.assertEqual([b[1] for b in passage_["lay"]], [10.0, 7.0])
        self.assertAlmostEqual(sum(b[0] for b in passage_["lay"]), len(passage_["text"]), delta=2)
        self.assertTrue(all(0 <= b[2] < b[3] <= 1 for b in passage_["lay"]), passage_["lay"])
        self.assertLess(passage_["lay"][0][3], passage_["lay"][1][2])  # the first block is above the second

    def test_sentence_keys_are_persisted(self):
        from kbsys import tier1
        self.assertLessEqual({"lay_size", "lay_ref", "lay_y"}, set(tier1.SENTENCE_KEYS))
        self.assertNotIn("lay_rel", tier1.SENTENCE_KEYS)


if __name__ == "__main__":
    unittest.main()
