"""Focused D3.19 tests: store-owned quotes, four parts, provenance and neural note scope."""

import json
import sqlite3
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import answer, find, hedges, neural


class AnswerFormatTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"id": "paper:S0001", "paper_id": "paper", "kind": "sentence", "unit_type": None,
             "text": "The treatment may reduce 5 units.", "pages": [3], "sections": ["results"],
             "review_paper": False, "uncertainty": ["may"]},
            {"id": "paper:S0002", "paper_id": "paper", "kind": "sentence", "unit_type": None,
             "text": "A second source passage.", "pages": [], "sections": ["discussion"],
             "review_paper": False},
        ]
        self.cards = {"paper": {"first_author": "Smith", "year": 2024}}

    def test_code_quotes_are_exact_and_missing_metadata_is_explicit(self):
        rows = answer.code_evidence(self.items, self.cards, [1, 2])
        self.assertEqual(rows[0]["quote"], self.items[0]["text"])
        self.assertEqual(rows[0]["citation"], "[Smith, 2024, trang 3] (paper:S0001)")
        missing = answer.code_evidence(self.items, {"paper": {}}, [2])[0]
        self.assertEqual(missing["citation"], "[không rõ, không rõ, trang không rõ] (paper:S0002)")

    def test_reasoning_is_separate_and_invented_number_is_struck(self):
        checked = answer.check_parts({
            "answer": [{"text": "Điều trị có thể giảm 5 đơn vị.", "evidence": [1]}],
            "reasoning": [{"text": "Suy luận này nêu 99 đơn vị.", "basis": [1]}],
        }, self.items, self.cards)
        self.assertEqual(len(checked["answer"]), 1)
        self.assertEqual(checked["reasoning"][0]["check"], "struck:A2")
        self.assertTrue(any("paper:S0001" in m for m in checked["missing"]))

    def test_hedged_cited_item_without_marker_field_is_listed_in_part_4(self):
        items = [{**self.items[1], "text": "Results suggest a link.", "hedging": "hedged"}]
        checked = answer.check_parts({"answer": [{"text": "Có thể có liên quan.", "evidence": [1]}]}, items, self.cards)
        self.assertTrue(any("paper:S0002" in m for m in checked["missing"]))
        self.assertNotIn(answer.SCOPE_NOTE, checked["missing"])
        self.assertEqual(checked["scope_note"], answer.SCOPE_NOTE)

    def test_bare_verdict_in_part_2_is_warning_not_strike(self):
        plain = [{**self.items[0], "text": "Embryo survival was 80 percent.", "hedging": "none", "uncertainty": []}]
        checked = answer.check_parts(
            {"answer": [{"text": "Có. Tỷ lệ sống của phôi là 80 percent.", "evidence": [1]}]},
            plain, self.cards)
        row = checked["answer"][0]
        self.assertIn("A9", row["warnings"])
        self.assertEqual(row["check"], "ok")

    def test_heuristic_marker_and_markdown_headings(self):
        hit = {**self.items[0], "analysis_origin": "heuristic_draft"}
        self.assertIn("heuristic_draft", answer._item_block(1, hit, self.cards["paper"]))
        checked = answer.check_parts({"answer": [{"text": "Có thể giảm 5 đơn vị.", "evidence": [1]}]},
                                     self.items, self.cards)
        markdown = answer.render_markdown(checked, self.items)
        for heading in ("## Bằng chứng từ tài liệu", "## Trả lời dựa trên bằng chứng",
                        "## [REASONING] Phần suy luận của AI", "## Không tìm thấy / không chắc chắn"):
            self.assertIn(heading, markdown)
        self.assertIn('"The treatment may reduce 5 units."', markdown)
        self.assertIn("Suy luận của AI — không phải nội dung tài liệu.", markdown)

    def test_neural_check_rejects_unknown_note_reference(self):
        ticket = "a" * 32
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "neural_sessions"
            cache.mkdir()
            (cache / (ticket + ".json")).write_text(json.dumps({
                "vault": "one", "vault_context": {"nodes": []}, "items": [], "papers": {},
                "note_refs": ["known.md"], "dependency_fingerprints": {}, "source_fingerprints": {},
            }), encoding="utf-8")
            con = sqlite3.connect(":memory:")
            con.execute("CREATE TABLE paper_vaults (paper_id TEXT, vault TEXT)")
            con.commit()
            with patch.object(neural.config, "CACHE_DIR", Path(tmp)):
                with self.assertRaisesRegex(ValueError, "Unknown neural note reference"):
                    neural.check(con, ticket, {"answer": [], "reasoning": [{"text": "x", "basis": ["other.md"]}]})
            con.close()

    def test_source_items_cap_draft_unit_sentences_without_crowding_out_next_unit(self):
        class Cursor:
            def __init__(self, value):
                self.value = value

            def fetchone(self):
                return self.value

        class Connection:
            def __init__(self, rows):
                self.rows = rows

            def execute(self, _sql, params=()):
                sid = params[0] if params else None
                return Cursor(self.rows.get(sid))

        rows = {
            f"paper:S{i:04d}": {
                "sentence_id": f"paper:S{i:04d}", "paper_id": "paper", "kind": "text",
                "text": f"Source sentence {i}.", "page": 1, "section": "results",
                "publication_types": [], "paper_evidence_type": "primary",
            }
            for i in range(1, 10)
        }
        hits = [
            {"kind": "unit", "id": "paper:E0001", "paper_id": "paper", "status": "flagged",
             "sentence_ids": [f"paper:S{i:04d}" for i in range(1, 9)], "score": 2},
            {"kind": "unit", "id": "paper:E0002", "paper_id": "paper", "status": "flagged",
             "sentence_ids": ["paper:S0009"], "score": 1},
        ]
        with patch.object(answer.find, "_attach_citations"):
            items = answer.source_items(Connection(rows), hits)
        self.assertEqual([item["id"] for item in items],
                         ["paper:S0001", "paper:S0002", "paper:S0003", "paper:S0009"])


class Part3BasisTests(unittest.TestCase):
    """AN-15: a Part 3 sentence has exactly one kind of basis; `ai_knowledge` is Part 3 only."""
    ITEMS = [{"id": "paper:S0001", "paper_id": "paper", "kind": "sentence", "unit_type": None,
              "text": "The treatment may reduce 5 units.", "pages": [3], "sections": ["results"], "review_paper": False}]

    def reasoning(self, text, basis, **kw):
        return answer.check_reasoning({"text": text, "basis": basis}, self.ITEMS, {}, **kw)

    def test_part3_no_basis_is_struck_a11(self):
        self.assertEqual(self.reasoning("Một nhận định chung.", [])["check"], "struck:A11")
        self.assertEqual(self.reasoning("Một nhận định chung.", None)["check"], "struck:A11")
        self.assertEqual(self.reasoning("Một nhận định chung.", "")["check"], "struck:A11")
        self.assertEqual(self.reasoning("Nêu 99 đơn vị.", [])["check"], "struck:A11")   # A11 first, though A2 holds too

    def test_part3_no_basis_and_no_number_is_struck_a11(self):
        row = answer.check_parts({"reasoning": [{"text": "Không có số nào ở đây."}]}, self.ITEMS, {})["reasoning"][0]
        self.assertEqual((row["check"], row["basis_kind"]), ("struck:A11", "none"))

    def test_ai_knowledge_mixed_with_item_or_note_is_struck_a11(self):
        for basis in ([1, "ai_knowledge"], ["ai_knowledge", "known.md"], [1, "known.md", "ai_knowledge"]):
            with self.subTest(basis):
                row = self.reasoning("Một nhận định chung.", basis, note_refs=["known.md"])
                self.assertEqual((row["check"], row["basis_kind"]), ("struck:A11", "none"))
        self.assertNotIn("A8", self.reasoning("Một nhận định chung.", [1, "ai_knowledge"])["problems"])

    def test_ai_knowledge_alone_is_ok_and_markdown_shows_the_label(self):
        checked = answer.check_parts({"reasoning": [{"text": "Một nhận định chung.", "basis": ["ai_knowledge"]}]}, self.ITEMS, {})
        row = checked["reasoning"][0]
        self.assertEqual((row["check"], row["basis_kind"], row["basis_label"]), ("ok", "ai_knowledge", answer.AI_KNOWLEDGE_LABEL))
        self.assertIn("Một nhận định chung. — " + answer.AI_KNOWLEDGE_LABEL, answer.render_markdown(checked, self.ITEMS))

    def test_ai_knowledge_alone_with_a_number_not_in_items_is_a2(self):
        row = self.reasoning("Tỷ lệ là 99 phần trăm.", ["ai_knowledge"])
        self.assertEqual(row["check"], "struck:A2")
        self.assertNotIn("A8", row["problems"])
        self.assertEqual(self.reasoning("Có thể giảm 5 đơn vị.", ["ai_knowledge"])["check"], "ok")   # a number the items print

    def test_marker_spelling_duplicate_and_string_basis(self):
        for basis in (["AI_Knowledge"], [" ai_knowledge "], ["ai_knowledge", "ai_knowledge"], "ai_knowledge", ["\tAI_KNOWLEDGE\n"]):
            with self.subTest(basis):
                row = self.reasoning("Một nhận định chung.", basis)
                self.assertEqual((row["check"], row["basis_kind"], row["basis"]), ("ok", "ai_knowledge", ["ai_knowledge"]))
        self.assertEqual(answer.check_reasoning({"text": "Một nhận định chung.", "evidence": ["ai_knowledge"]},
                                                self.ITEMS, {})["basis_kind"], "ai_knowledge")   # `evidence` key used for reasoning
        self.assertEqual(self.reasoning("Một nhận định chung.", ["ai_knowledge_x"])["check"], "struck:A8")   # not the marker
        self.assertEqual(answer._numeric_refs(["ai_knowledge", 2, "known.md"]), ([2], ["known.md"]))

    def test_basis_kinds_and_labels_of_item_and_note_rows(self):
        kinds = {"items": [1], "notes": ["known.md"], "items_notes": [1, "known.md"]}
        for kind, basis in kinds.items():
            with self.subTest(kind):
                row = self.reasoning("Có thể giảm 5 đơn vị.", basis, note_refs=["known.md"], note_texts=["5"])
                self.assertEqual((row["check"], row["basis_kind"]), ("ok", kind))
                self.assertTrue(row["basis_label"].startswith("suy luận từ "))
        checked = answer.check_parts({"reasoning": [{"text": "Có thể giảm 5 đơn vị.", "basis": [1, "known.md"]}]}, self.ITEMS, {},
                                     note_refs=["known.md"])
        self.assertIn("(suy luận từ paper:S0001, known.md)", answer.render_markdown(checked, self.ITEMS))

    def test_ai_knowledge_outside_part3_is_struck_a11(self):
        for evidence in (["ai_knowledge"], [1, "ai_knowledge"], ["AI_Knowledge", 1], "ai_knowledge"):
            for scope in ("answer", "card", "note"):
                with self.subTest((evidence, scope)):
                    got = answer.check_sentence({"text": "Có thể giảm 5 đơn vị.", "evidence": evidence}, self.ITEMS, {}, scope=scope)
                    self.assertEqual((got["check"], got["problems"]), ("struck:A11", ["A11"]))
        part2 = answer.check_parts({"answer": [{"text": "Có thể giảm 5 đơn vị.", "evidence": [1, "ai_knowledge"]}]}, self.ITEMS, {})
        self.assertEqual(part2["answer"][0]["check"], "struck:A11")
        self.assertIn("~~Có thể giảm 5 đơn vị.~~ [bị gạch: ", answer.render_markdown(part2, self.ITEMS))   # AN-13: shown struck

    def test_one_label_in_instructions_protocol_and_render(self):
        from kbsys import discussions, mcp_server
        label = unicodedata.normalize("NFC", answer.AI_KNOWLEDGE_LABEL)
        self.assertEqual(label, answer.AI_KNOWLEDGE_LABEL)
        checked = answer.check_parts({"reasoning": [{"text": "Một nhận định chung.", "basis": ["ai_knowledge"]}]}, self.ITEMS, {})
        for name, text in (("instructions", mcp_server.INSTRUCTIONS), ("protocol", discussions.protocol()),
                           ("render", answer.render_markdown(checked, self.ITEMS))):
            with self.subTest(name):
                self.assertIn(label, unicodedata.normalize("NFC", text))
        self.assertIn(f'["{answer.AI_KNOWLEDGE}"] alone', answer.ANSWER_RULES)   # U9b: the prompt keeps the basis token, not the label

    def test_neural_check_note_only_and_ai_knowledge_bases(self):
        ticket = "b" * 32
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp) / "neural_sessions"
            cache.mkdir()
            (cache / (ticket + ".json")).write_text(json.dumps({
                "vault": "one", "vault_context": {"nodes": []}, "items": [], "papers": {},
                "note_refs": ["known.md"], "dependency_fingerprints": {}, "source_fingerprints": {},
            }), encoding="utf-8")
            con = sqlite3.connect(":memory:")
            con.execute("CREATE TABLE paper_vaults (paper_id TEXT, vault TEXT)")
            con.commit()
            reply = {"answer": [], "reasoning": [{"text": "Từ ghi chú.", "basis": ["known.md"]},
                                                 {"text": "Kiến thức chung.", "basis": ["ai_knowledge"]},
                                                 {"text": "Trộn.", "basis": ["known.md", "ai_knowledge"]},
                                                 {"text": "Không cơ sở.", "basis": []}]}
            with patch.object(neural.config, "CACHE_DIR", Path(tmp)):
                checked = neural.check(con, ticket, reply)   # "ai_knowledge" is no unknown note: no raise
            con.close()
        self.assertEqual([r["check"] for r in checked["reasoning"]], ["ok", "ok", "struck:A11", "struck:A11"])
        self.assertIn("Kiến thức chung. — " + answer.AI_KNOWLEDGE_LABEL, checked["markdown"])
        self.assertIn("Từ ghi chú. (suy luận từ known.md)", checked["markdown"])

    def test_text_marker_is_an_item_basis(self):
        # review U9a: "[#1]" in the text next to ai_knowledge was rendered as AI knowledge; with no basis it was "no basis"
        mixed = self.reasoning("Có thể giảm 5 đơn vị [#1].", ["ai_knowledge"])
        self.assertEqual((mixed["check"], mixed["basis_kind"], mixed["basis_label"]), ("struck:A11", "none", ""))
        cited = self.reasoning("Có thể giảm 5 đơn vị [#1].", [])
        self.assertEqual((cited["check"], cited["basis_kind"], cited["basis"]), ("ok", "items", [1]))
        self.assertEqual(self.reasoning("Một nhận định [#9].", [])["check"], "struck:A1")   # an unknown item stays A1

    def test_web_shows_suy_luan_tu_for_item_and_note_bases(self):
        web = Path(answer.__file__).parent / "web"
        for name in ("app.js", "neural.js"):
            with self.subTest(name):
                self.assertIn("suy luận từ ", (web / name).read_text(encoding="utf-8"))


class NumberIdentifierTests(unittest.TestCase):
    def test_known_identifier_digits_are_not_measurements(self):
        item = {"text": "SIRT1 expression was measured.", "paper_id": "p",
                "kind": "sentence", "unit_type": None,
                "identifier_terms": ["SIRT1", "sirtuin 1", "H3K9me3", "8-cell", "2D/3D"]}
        checked = answer.check_sentence(
            {"text": "Sirtuin 1 expression was measured [1].", "evidence": [1]}, [item], {})
        self.assertNotIn("A2", checked["problems"])

    def test_invented_measurement_stays_strict(self):
        item = {"text": "SIRT1 expression was measured.", "paper_id": "p",
                "kind": "sentence", "unit_type": None,
                "identifier_terms": ["SIRT1", "sirtuin 1"]}
        checked = answer.check_sentence(
            {"text": "Sirtuin 1 was used at 15 mM [1].", "evidence": [1]}, [item], {})
        self.assertIn("A2", checked["problems"])


class NfdTests(unittest.TestCase):
    """CHR-01 / AN-03 / RT-04: NFD text is judged exactly like its NFC form."""
    HEDGED = "Điều trị có thể cải thiện sự sống sót của tế bào."

    def judge(self, sentence, item_text, form):
        norm = lambda s: unicodedata.normalize(form, s)
        item = {"text": norm(item_text), "paper_id": "p", "kind": "sentence", "unit_type": None}
        return answer.check_sentence({"text": norm(sentence), "evidence": [1]}, [item], {})

    def test_nfd_sentence_over_a_hedged_item_is_struck_like_nfc(self):
        for sentence in ("Điều trị chắc chắn cải thiện sự sống sót.", "Treatment definitely improves survival."):
            nfc, nfd = (self.judge(sentence, self.HEDGED, f) for f in ("NFC", "NFD"))
            self.assertEqual(nfc, nfd)
        self.assertNotEqual(nfc["check"], "ok")

    def test_nfd_item_text_is_read_like_nfc(self):
        for sentence in ("Điều trị có thể cải thiện sự sống sót.", "Điều trị chắc chắn cải thiện sự sống sót."):
            got = [self.judge(sentence, self.HEDGED, f) for f in ("NFC", "NFD")]
            self.assertEqual(got[0], got[1])
        item = {"text": unicodedata.normalize("NFD", self.HEDGED), "paper_id": "p", "kind": "sentence", "unit_type": None}
        got = answer.check_sentence({"text": "Điều trị chắc chắn cải thiện sự sống sót.", "evidence": [1]}, [item], {})
        self.assertIn("A3", got["problems"])

    def test_hedge_reading_and_uncertainty_items_ignore_nfd(self):
        nfd = unicodedata.normalize("NFD", self.HEDGED)
        self.assertEqual(hedges.uncertainty(nfd), hedges.uncertainty(self.HEDGED))
        self.assertTrue(hedges.uncertainty(nfd))
        self.assertEqual(answer.uncertainty_items([{"text": nfd}], [1]), [1])

    def test_item_without_text_is_not_a_crash(self):
        got = answer.check_sentence({"text": "x [#1]", "evidence": [1]}, [{"paper_id": "p", "kind": "sentence", "unit_type": None}], {})
        self.assertIn("A3", answer.check_sentence({"text": "Treatment causes survival.", "evidence": [1]},
                                                  [{"text": None, "paper_id": "p", "kind": "sentence", "unit_type": None}], {})["problems"])
        self.assertEqual(got["evidence"], [1])

    def test_nfd_vietnamese_query_is_recognised_and_english_symbols_are_not(self):
        for query in ("ảnh hưởng của vitrification", "các nhóm là gì", "cà phê", "tế bào"):
            for form in ("NFC", "NFD"):
                self.assertTrue(find.looks_vietnamese(unicodedata.normalize(form, query)), (query, form))
        for query in ("survival at 37 °C", "5 µM ± 2 α β", "vitrification of oocytes"):
            for form in ("NFC", "NFD"):
                self.assertFalse(find.looks_vietnamese(unicodedata.normalize(form, query)), (query, form))


class LexiconTests(unittest.TestCase):
    """AN-03 / AN-04 / AN-11: Vietnamese causal verbs, real attribution, affirming verbs in a limits sentence."""
    def judge(self, text, kind="sentence", unit_type=None, limits=False, item_text=None, **item):
        base = {"text": "Treatment may improve survival of cells.", "paper_id": "p", "kind": kind, "unit_type": unit_type}
        base["text"] = item_text or base["text"]
        return answer.check_sentence({"text": text, "evidence": [1]}, [{**base, **item}], {}, limits=limits)

    def test_an03_causal_verbs_over_a_hedged_item_are_struck_in_nfc_and_nfd(self):
        for verb in ("khiến", "gây nên", "làm cho", "chứng tỏ"):
            for form in ("NFC", "NFD"):
                got = self.judge(unicodedata.normalize(form, f"Điều trị {verb} tế bào sống sót tốt hơn."))
                self.assertIn("A3", got["problems"], (verb, form))
        self.assertNotIn("A3", self.judge("Điều trị có thể làm cho tế bào sống sót tốt hơn.")["problems"])   # hedged stays hedged

    def test_an04_author_year_hypothesis_is_attribution_and_bare_theo_is_not(self):
        second = dict(kind="unit", unit_type="cited_statement", item_text="Vitrification damages the spindle.")
        for text in ("Smith 2016 đề xuất rằng vitrification làm hỏng thoi.", "Smith et al. 2016 gợi ý vitrification làm hỏng thoi.",
                     "Smith 2016 hypothesised that vitrification damages the spindle.", "Theo Smith 2016, vitrification làm hỏng thoi.",
                     "Theo nghiên cứu trước, vitrification làm hỏng thoi."):
            self.assertNotIn("A4", self.judge(text, **second)["problems"], text)
        for text in ("Số phôi sống giảm theo thời gian.", "Hiệu quả tăng theo liều.", "Vitrification làm hỏng thoi.",
                     "Vitrification dẫn đến hỏng thoi.", "Theo Hình 2, vitrification làm hỏng thoi."):
            self.assertIn("A4", self.judge(text, **second)["problems"], text)
        owned = dict(kind="sentence", unit_type="cited_statement", unit_id="p:E0001", item_text="Vitrification damages the spindle.")
        self.assertIn("A4", self.judge("Vitrification làm hỏng thoi.", **owned)["problems"])  # neural.prepare: a sentence of a cited_statement unit
        self.assertNotIn("A4", self.judge("Vitrification làm hỏng thoi.", kind="sentence", unit_type="background",
                                          item_text="Vitrification damages the spindle.")["problems"])  # node-card items (no unit_id) unchanged

    def test_an11_affirming_verb_is_struck_in_a_limits_sentence_but_a_negated_one_is_not(self):
        for text in ("Only one mouse study proves the effect.", "Only one mouse study demonstrates the effect.",
                     "One mouse study shows that the effect exists.", "Nghiên cứu duy nhất trên chuột chứng minh hiệu quả.",
                     "Một nghiên cứu duy nhất khẳng định hiệu quả."):
            self.assertIn("A3", self.judge(text, limits=True)["problems"], text)
        for text in ("One mouse study does not prove the effect.", "One mouse study cannot demonstrate the effect.",
                     "The effect has not been shown.", "Nghiên cứu duy nhất không chứng minh được hiệu quả.",
                     "Chưa có nghiên cứu nào chứng minh hiệu quả.", "Only one mouse study, not generalisable.",
                     "Not generalisable because of one mouse study."):
            got = self.judge(text, limits=True)
            self.assertEqual(got["check"], "ok", (text, got))

    def test_an11_review_fixes_long_vietnamese_limits_modal_can_and_other_clause_negators(self):
        for text in ("Không thể kết luận rằng H3 hoặc một biến thể H3 trực tiếp gây ra một kết quả sinh học cụ thể.",   # real answer text
                     "Không thể khái quát các kết quả trên thành kết luận chắc chắn ở người.",
                     "Whether vitrification causes spindle damage remains unproven.",
                     "There is insufficient evidence to conclude that vitrification causes damage.",
                     "Nothing here confirms that vitrification is safe.", "It would be wrong to say that vitrification proves damage."):
            self.assertEqual(self.judge(text, limits=True)["check"], "ok", text)
        for text in ("We can prove that vitrification damages the spindle.", "We would say vitrification proves damage.",
                     "Not conclusive so this proves the effect.", "Chưa rõ trong khi nghiên cứu này chứng minh hiệu quả.",
                     "Not proven, although this establishes the effect."):
            self.assertIn("A3", self.judge(text, limits=True)["problems"], text)

    def test_an04_review_fixes_vietnamese_capital_initials_and_a_number_after_theo(self):
        second = dict(kind="unit", unit_type="cited_statement", item_text="Vitrification damages the spindle.")
        for text in ("Theo Đặng, vitrification làm hỏng thoi.", "Đặng 2016 đề xuất vitrification làm hỏng thoi.",
                     "Smith (2016) suggested that vitrification damages the spindle."):
            for form in ("NFC", "NFD"):
                self.assertNotIn("A4", self.judge(unicodedata.normalize(form, text), **second)["problems"], (text, form))
        self.assertIn("A4", self.judge("Hiệu quả thay đổi theo 1000 IU.", **second)["problems"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

class WholeQuoteTests(unittest.TestCase):
    def test_cut_sentence_is_marked_and_continuation_joined(self):
        from unittest import mock
        rows = [{"sentence_id": "p:S0001", "text": "In group B, no significant change of", "continues": "p:S0003"},
                {"sentence_id": "p:S0003", "text": "H3K27me3 was found."}]
        with mock.patch.object(answer, "read_jsonl", return_value=rows):
            joined = answer._whole_quote({"id": "p:S0001", "paper_id": "p", "text": rows[0]["text"]}, {})
        self.assertEqual(joined, "In group B, no significant change of H3K27me3 was found.")
        with mock.patch.object(answer, "read_jsonl", return_value=[]):
            cut = answer._whole_quote({"id": "p:S0001", "paper_id": "p", "text": "no significant change of"}, {})
        self.assertIn("câu bị cắt", cut)
        self.assertEqual(answer._whole_quote({"id": "p:E0001", "text": "A full unit quote."}, {}), "A full unit quote.")


class PerItemChecksTests(unittest.TestCase):
    """AN-02 / AN-06 / AN-07: a number, a direction and a hedge are judged per cited item / per source sentence."""

    @staticmethod
    def item(text, n=1, **extra):
        return {"id": f"p{n}:S0001", "paper_id": f"p{n}", "kind": "sentence", "unit_type": None, "text": text,
                "review_paper": False, **extra}

    def verdict(self, text, items):
        return answer.check_sentence({"text": text}, items, {})["problems"]

    def test_an02_number_borrowed_from_another_entitys_item_is_struck(self):
        mark = self.item("H4K12ac rose in group A.")
        other = self.item("H3K9me3 reached 70 percent in group B.", 2)
        self.assertIn("A2", self.verdict("H4K12ac reached 70 percent [#1][#2].", [mark, other]))
        # near-misses: the number is in an item that names the entity; a cited year; no named entity at all
        both = self.item("H4K12ac reached 70 percent in group A.", 2)
        self.assertNotIn("A2", self.verdict("H4K12ac reached 70 percent [#1][#2].", [mark, both]))
        self.assertNotIn("A2", self.verdict("H4K12ac reached 70 percent [#1][#2].", [both, other]))
        self.assertNotIn("A2", self.verdict("Survival reached 70 percent [#1][#2].", [mark, other]))
        self.assertNotIn("A2", answer.check_sentence({"text": "H4K12ac rose in 2019 [#1][#2]."}, [mark, other],
                                                     {"p2": {"year": 2019}})["problems"])

    def test_an06_every_cited_direction_must_agree(self):
        inc = self.item("Level increased.", direction="increase")
        dec = self.item("Level decreased.", 2, direction="decrease")
        none = self.item("Level was measured.", 3)
        same = self.item("Level did not change.", 4, direction="no_change")
        self.assertIn("A6", self.verdict("Mức tăng [#1][#2].", [inc, dec]))
        self.assertIn("A6", self.verdict("Mức giảm [#1][#2].", [inc, dec]))
        self.assertIn("A6", self.verdict("Mức không thay đổi [#1][#2].", [inc, same]))
        self.assertNotIn("A6", self.verdict("Mức tăng [#1][#2].", [inc, none]))
        self.assertNotIn("A6", self.verdict("Mức tăng [#1][#2].", [inc, self.item("Level was higher.", 2, direction="increase")]))
        self.assertNotIn("A6", self.verdict("Mức không thay đổi [#1][#2].", [same, none]))
        # EV-22: "no change" read from the hedge lexicon (non_significance), not from a second Vietnamese list
        self.assertIn("A6", self.verdict("Sự khác biệt chưa có ý nghĩa thống kê [#1][#2].", [inc, same]))
        self.assertIn("A6", self.verdict("Mức thay đổi không đáng kể [#1][#2].", [inc, same]))
        self.assertNotIn("A6", self.verdict("Sự khác biệt chưa có ý nghĩa thống kê [#1][#2].", [same, none]))

    def test_an07_hedge_is_read_on_the_matching_source_sentence(self):
        unit = self.item("H3K9me3 was not significantly altered. H4K12ac increased (p < 0.05).", kind="unit",
                         unit_type="result", hedging="hedged")
        self.assertNotIn("A7", self.verdict("H4K12ac tăng (p < 0.05) [#1].", [unit]))
        self.assertIn("A7", self.verdict("H3K9me3 thay đổi [#1].", [unit]))          # the hedged finding keeps its hedge
        self.assertNotIn("A7", self.verdict("H3K9me3 không thay đổi đáng kể [#1].", [unit]))
        nonsig = self.item("Marker X was not significantly different between groups.", hedging="hedged")
        for kept in ("không khác biệt có ý nghĩa thống kê", "không có ý nghĩa thống kê", "chưa đạt ý nghĩa",
                     "không có thay đổi đáng kể"):
            with self.subTest(kept):
                self.assertNotIn("A7", self.verdict(f"Marker X {kept} giữa các nhóm [#1].", [nonsig]))
        self.assertIn("A7", self.verdict("Marker X khác biệt giữa các nhóm [#1].", [nonsig]))

    def test_an07_review_fixes_a_null_result_clause_cannot_drop_a_modal_hedge(self):
        may = self.item("H4K12ac may increase after vitrification (p < 0.05).")
        self.assertIn("A7", self.verdict("H4K12ac tăng sau vitrification, còn H3K9me3 không thay đổi đáng kể [#1].", [may]))
        self.assertIn("A7", self.verdict("H4K12ac tăng sau vitrification; khác biệt không có ý nghĩa thống kê [#1].", [may]))
        self.assertNotIn("A7", self.verdict("H4K12ac có thể tăng sau vitrification [#1].", [may]))

    def test_an07_review_fixes_hedge_that_leans_on_the_claim_sentence_or_cannot_be_localised(self):
        follow = self.item("Y increased 2-fold. However, the difference was not significant (P = 0.08).")
        self.assertIn("A7", self.verdict("Y tăng 2 lần [#1].", [follow]))
        later = self.item("Y increased 2-fold. Z was measured. W stayed.")
        self.assertNotIn("A7", self.verdict("Y tăng 2 lần [#1].", [later]))
        # flagged hedged, but the lexicon finds nothing in any sentence: the flag cannot be placed, so it stays
        self.assertIn("A7", self.verdict("Y tăng 2 lần [#1].", [self.item("Z fell. Y increased 2-fold.", hedging="hedged")]))
        # a hedged sentence that only shares the claim's words as much as another one stays in the reading
        tie = self.item("X may increase cell number. X increased cell number.")
        self.assertIn("A7", self.verdict("X tăng cell number [#1].", [tie]))

    def test_an10_identifier_or_condition_no_cited_item_holds_is_struck(self):
        sip = self.item("Treatment with sip150 reduced H4K12ac level in embryos.")
        self.assertIn("A10", self.verdict("Vitrification làm giảm H4K12ac ở phôi [#1].", [sip]))   # S16-01
        self.assertIn("A10", self.verdict("Sip150 làm giảm H3K9me3 ở phôi [#1].", [sip]))
        self.assertIn("A10", self.verdict("Slow freezing làm giảm H4K12ac [#1].", [self.item("Vitrification reduced H4K12ac.")]))
        self.assertIn("A10", self.verdict("H3K9me3 tăng [#1].", [self.item("Histone H3 increased.")]))   # a short held name does not cover a long one
        # a limits sentence names what the sources do not show
        self.assertNotIn("A10", answer.check_sentence({"text": "Chưa rõ vitrification có làm giảm H4K12ac [#1]."}, [sip], {}, limits=True)["problems"])

    def test_scope_card_and_note_pool_numbers_and_skip_a10(self):
        """NC-01 / D-PENDING-8: a card/note statement merging papers is not narrowed (A2) and not subject-checked (A10)."""
        mark = self.item("H4K12ac rose in group A.")
        other = self.item("H3K9me3 reached 70 percent in group B.", 2)
        merged = {"text": "H4K12ac reached 70 percent [#1][#2]."}
        sip = self.item("Treatment with sip150 reduced H4K12ac level in embryos.")
        foreign = {"text": "Vitrification làm giảm H4K12ac ở phôi [#1]."}
        for scope in ("card", "note"):
            with self.subTest(scope):
                self.assertNotIn("A2", answer.check_sentence(merged, [mark, other], {}, scope=scope)["problems"])
                self.assertNotIn("A10", answer.check_sentence(foreign, [sip], {}, scope=scope)["problems"])
                self.assertIn("A2", answer.check_sentence({"text": "H4K12ac reached 90 percent [#1][#2]."}, [mark, other], {}, scope=scope)["problems"])   # pooled, not waived
        self.assertIn("A2", answer.check_sentence(merged, [mark, other], {})["problems"])   # the ask-answer default
        self.assertIn("A10", answer.check_sentence(foreign, [sip], {})["problems"])

    def test_an10_near_misses_stay_silent(self):
        sip = self.item("Treatment with sip150 reduced H4K12ac level in embryos.")
        slash = self.item("H3K4me2/3 levels were higher in group A.")
        vit = self.item("Vitrified-warmed samples had reduced TGF-β1 and p53 levels.", 2)
        quiet = {
            "case difference": ("SIP150 làm giảm h4k12AC [#1].", [sip], {}),
            "NFD spelling": (unicodedata.normalize("NFD", "Thủy tinh hóa làm giảm TGF-beta1 [#1]."), [vit], {}),
            "Greek vs Latin name": ("TGF-beta1 thấp hơn ở phôi vitrified [#1].", [vit], {}),
            "Vietnamese for the condition": ("Phôi thủy tinh hóa có p53 thấp hơn [#1].", [vit], {}),
            "present in only one of two cited items": ("Sip150 làm giảm H4K12ac và p53 [#1][#2].", [sip, vit], {}),
            "no identifier at all": ("Tỷ lệ sống của phôi cao hơn ở nhóm A [#1].", [sip], {}),
            "PDF superscript m^6A": ("Mức m6A thấp hơn [#1].", [self.item("The m^6A level was lower.")], {}),
            "mark with a suffix the item shortens": ("H4K16ac tăng [#1].", [self.item("H4K12 and H4K16 levels increased.")], {}),
            "condition only in the paper title": ("Phôi vitrification có H4K12ac thấp hơn [#1].", [sip], {"p1": {"title": "Vitrification of embryos"}}),
            "slash form, both marks": ("H3K4me2 và H3K4me3 cao hơn ở nhóm A [#1].", [slash], {}),
            "slash form copied": ("H3K4me2/3 cao hơn ở nhóm A [#1].", [slash], {}),
            "one mark of a slash form": ("H3K4me2 cao hơn ở nhóm A [#1].", [slash], {}),
        }
        for name, (text, items, cards) in quiet.items():
            with self.subTest(name):
                self.assertEqual(answer.check_sentence({"text": text}, items, cards)["problems"], [])

    def test_an10_slash_form_is_two_marks_not_the_number_of_the_second(self):
        # S16-09: H3K4me2/3 and H4K12/16ac gave the numbers 3 and 16, which a source writing the marks apart does not hold
        apart = self.item("H3K4me2 and H3K4me3 rose; H4K12ac and H4K16ac fell.")
        self.assertEqual(self.verdict("H3K4me2/3 tăng, H4K12/16ac giảm [#1].", [apart]), [])
        self.assertEqual(answer._answer_numbers("H3K4me2/3 and H4K12/16ac"), (set(), set()))
        self.assertEqual(answer._answer_numbers("tỷ lệ 2/3")[0], {"2", "3"})   # a plain ratio is still numbers

    def test_an10_review_fixes(self):
        # a digit never extends a name: CD3 is not CD34, Sox1 is not Sox17 (it did before: "up to 3 characters, both ways")
        self.assertIn("A10", self.verdict("CD34 tăng [#1].", [self.item("CD3 increased.")]))
        self.assertIn("A10", self.verdict("Sox17 tăng [#1].", [self.item("Sox1 increased.")]))
        # ... but a superscript reference glued to a name is not part of it: H4K16ac^44 holds H4K16ac
        ref = self.item("Marks K12ac, and H4K16ac^44 are listed near gene starts.")
        self.assertNotIn("A10", self.verdict("H4K16ac tăng [#1].", [ref]))
        self.assertNotIn("A10", self.verdict("m6A giảm [#1].", [self.item("The m^6A level fell.")]))
        # both Vietnamese spellings of the condition are read, also mixed (thủy + hoá)
        for spelling in ("Thủy tinh hoá", "Thuỷ tinh hóa", "Thủy tinh hóa", "Thuỷ tinh hoá"):
            with self.subTest(spelling):
                self.assertIn("A10", self.verdict(f"{spelling} làm giảm H4K12ac [#1].", [self.item("H4K12ac fell.")]))
        # slash forms with a modification on both sides are two marks, not a bare "me3"
        quiet = {"me2/me3": ("H3K4me2/me3 cao hơn [#1].", "H3K4me2 and H3K4me3 were higher."),
                 "me1/2/3": ("H3K4me1/2/3 cao hơn [#1].", "H3K4me1, H3K4me2 and H3K4me3 were higher."),
                 "ac/me3": ("H3K27ac/me3 cao hơn [#1].", "H3K27ac and H3K27me3 were higher."),
                 "item with me2/me3": ("H3K4me2 và H3K4me3 cao hơn [#1].", "H3K4me2/me3 were higher.")}
        for name, (text, source) in quiet.items():
            with self.subTest(name):
                self.assertEqual(self.verdict(text, [self.item(source)]), [])
        self.assertIn("A10", self.verdict("H3K4me2/me3 cao hơn [#1].", [self.item("H3K4me2 was higher.")]))
        # a figure / table label with a slash is a label: "S1/2" is not the identifier S2
        self.assertNotIn("A10", self.verdict("Table S1/2 cho thấy mức tăng [#1].", [self.item("The level increased.")]))
