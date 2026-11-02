"""U10b: the local HTML page of a checked answer (answer.render_html / write_html), the [#n] of Part 1, and the rule that
only the MCP dispatch writes the page."""
import html
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from . import test_brainstorm_u10 as u10
from kbsys import agent_tasks, answer, config, neural

LONG = " ".join(f"Cells in group {c} were counted after warming." for c in "ABCDEFGH")   # > 300 chars, no hedge, no negation
CARD = {"first_author": "Smith", "year": 2024, "title": "Cells after warming", "doi": "10.1/x", "pmid": "123", "evidence_type": "review"}


def unit(n, text, paper="p1", page=3, **extra):
    return {"id": f"{paper}:E{n:04d}", "paper_id": paper, "kind": "unit", "unit_type": "result", "text": text, "pages": [page],
            "sections": ["results"], "sentence_ids": [f"{paper}:S{n:04d}"], "review_paper": False, **extra}


def cites(*numbers):
    return {"answer": [{"text": f"Cells were counted after warming [#{n}].", "evidence": [n]} for n in numbers]}


class ShortQuoteTests(unittest.TestCase):
    def test_short_quote_is_never_cut_and_a_long_one_is_a_prefix_cut_at_a_sentence_end(self):
        self.assertEqual(answer.short_quote("A short quote."), ("A short quote.", None))
        shown, whole = answer.short_quote(LONG)
        self.assertEqual(whole, LONG)
        self.assertTrue(shown.endswith("…") and LONG.startswith(shown[:-1]) and len(shown) <= answer.SHORT_QUOTE + 1)
        self.assertTrue(shown[:-1].endswith("warming."))

    def test_a_hedge_or_negation_in_the_hidden_tail_keeps_the_quote_whole(self):
        for tail in ("The effect may differ.", "The change was not significant.", "It didn't change.", "We cannot say.",
                     "Nor was it lower.", "It rose without a drug.", "The drug failed to reduce damage.", "Cells were unable to recover.",
                     "A lack of effect was seen.", "The effect was absent.", "None of the cells survived.", "Levels were unchanged.",
                     "The change was non-significant.", "The change was insignificant.", "It might differ.", "The data suggest a link.",
                     "There was no difference."):
            with self.subTest(tail):
                self.assertEqual(answer.short_quote(LONG + " " + tail), (LONG + " " + tail, None))

    def test_no_sentence_end_in_reach_keeps_the_quote_whole(self):
        one = "Cells " + "were counted after warming and washing " * 12 + "."
        self.assertEqual(answer.short_quote(one), (one, None))


class RenderTests(unittest.TestCase):
    def page(self, reply, items, cards=None, vault=None):
        checked = answer.check_parts(reply, items, cards if cards is not None else {"p1": CARD})
        return answer.render_html(checked, items, "Is it <b>true</b>?", vault, {"p1": CARD} if cards is None else cards), checked

    def test_every_shown_quote_is_a_substring_of_the_item_text(self):
        items = [unit(1, "Cells were counted after warming."), unit(2, LONG), unit(3, LONG + " The effect may differ.")]
        page, checked = self.page(cites(1, 2, 3), items)
        self.assertTrue(all(r["check"] == "ok" for r in checked["answer"]), checked["answer"])
        blocks = [html.unescape(m) for m in re.findall(r"<blockquote[^>]*>(.*?)</blockquote>", page, re.S)]
        self.assertTrue(blocks)
        for block in blocks:
            body = re.sub(r"<details>.*?</details>", "", block, flags=re.S).rstrip("…")
            self.assertTrue(any(body in item["text"] for item in items), body)
            for full in re.findall(r"<summary>[^<]*</summary>(.*?)</details>", block, re.S):
                self.assertTrue(any(full == item["text"] for item in items), full)
        self.assertIn("xem đủ câu", page)                       # the long, plain quote is folded ...
        self.assertEqual(page.count("xem đủ câu"), 1)           # ... the hedged one and the short one are not
        self.assertIn(html.escape(LONG + " The effect may differ."), page)

    def test_struck_row_is_in_del_with_its_reason_and_the_label_of_ai_knowledge_shows(self):
        items = [unit(1, "Cells were counted after warming.")]
        reply = {"answer": [{"text": "Cells were 99 percent after warming [#1].", "evidence": [1]}],
                 "reasoning": [{"text": "Một nhận định chung.", "basis": ["ai_knowledge"]},
                               {"text": "Có thể đếm tế bào [#1].", "basis": [1]}]}
        page, checked = self.page(reply, items)
        self.assertEqual(checked["answer"][0]["check"], "struck:A2")
        self.assertIn("<del>Cells were 99 percent after warming [#1].</del>", page)
        self.assertIn(html.escape(answer.CHECK_TEXT["A2"]), page)
        self.assertIn(html.escape(answer.AI_KNOWLEDGE_LABEL), page)
        self.assertIn("Suy luận của AI — không phải nội dung tài liệu.", page)
        self.assertIn("suy luận từ [#1]", page)
        self.assertIn("0 câu trả lời có bằng chứng từ 0 bài · 2 câu suy luận · 0 điều không tìm thấy · 1 câu bị gạch", page)

    def test_badges_review_and_metadata(self):
        page, _ = self.page(cites(1), [unit(1, "Cells were counted after warming.")])
        for expected in ("KẾT QUẢ", "TỔNG QUAN", "[#1] Smith 2024 · tr. 3", "DOI 10.1/x", "PMID 123", "Cells after warming"):
            self.assertIn(expected, page)
        self.assertNotIn("<script", page)
        self.assertNotIn("http", page)

    def test_script_is_escaped_and_only_obsidian_is_a_link(self):
        evil = "<script>alert(1)</script> Cells were counted after warming."
        for url, linked in (("javascript:alert(1)", False), ("http://evil.example/x", False), ("data:text/html,x", False),
                            ("obsidian://open?vault=v&file=a.md", True)):
            with self.subTest(url):
                page, _ = self.page(cites(1), [unit(1, evil, obsidian_url=url)])
                self.assertNotIn("<script>", page)
                self.assertIn("&lt;script&gt;", page)
                self.assertEqual("href=" in page, linked)
                if linked:
                    self.assertIn('href="obsidian://open?vault=v&amp;file=a.md"', page)
        self.assertNotIn("<b>true</b>", self.page(cites(1), [unit(1, "Cells were counted after warming.")])[0])   # the question too

    def test_vault_note_link_comes_from_paper_note_links(self):
        link = {"p1": {"note_path": "Papers/a.md", "obsidian_url": "obsidian://open?vault=one&file=Papers%2Fa.md"}}
        with patch.object(neural, "paper_note_links", return_value=link) as links:
            page, _ = self.page(cites(1), [unit(1, "Cells were counted after warming.")], vault="one")
        links.assert_called_once()
        self.assertIn('href="obsidian://open?vault=one&amp;file=Papers%2Fa.md"', page)

    def test_sources_list_each_paper_once_with_page_and_sentence_id(self):
        items = [unit(1, "Cells were counted after warming.", page=3), unit(2, "Cells were washed after warming.", page=7),
                 unit(3, "Other cells were counted after warming.", paper="p2", page=9)]
        page, _ = self.page(cites(1, 2, 3), items, {"p1": CARD, "p2": {**CARD, "title": "Second paper"}})
        sources = page.split("Bằng chứng từ tài liệu (nguồn)")[1]
        self.assertEqual(sources.count("Cells after warming"), 1)   # p1 cited twice, listed once
        self.assertEqual(sources.count("Second paper"), 1)
        for expected in ("tr. 3", "tr. 7", "tr. 9", "p1:S0001", "p1:S0002", "p2:S0003", "results"):
            self.assertIn(expected, sources)
        self.assertNotIn("p1:S0001", page.split("Bằng chứng từ tài liệu (nguồn)")[0].replace('title="p1:E0001"', ""))   # ids only in sources / tooltips

    def test_part_1_lines_of_the_markdown_start_with_the_item_number(self):
        items = [unit(1, "Cells were counted after warming."), unit(2, "Cells were washed after warming.")]
        checked = answer.check_parts(cites(1, 2), items, {"p1": CARD})
        lines = answer.render_markdown(checked, items).split("## Trả lời dựa trên")[0].splitlines()[1:]
        quoted = [line for line in lines if line.strip()]
        self.assertEqual([line[:7] for line in quoted], ["> [#1] ", "> [#2] "])   # still a Markdown blockquote
        self.assertIn('"Cells were counted after warming."', quoted[0])


class DispatchTests(u10.BrainstormBase):
    """Only the MCP dispatch writes the page."""

    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="demo3-html-"))
        patcher = patch.object(answer, "OUTPUT_DIR", self.out / "answers")
        patcher.start()
        self.addCleanup(patcher.stop)

    def written(self):
        return sorted(answer.OUTPUT_DIR.glob("*.html")) if answer.OUTPUT_DIR.is_dir() else []

    def test_the_shared_checks_write_no_file(self):
        sid = self.ask(range(6), session="new")["session"]
        answer.check_parts(self.reply((0, 1)), [], {})
        neural.check(self.con, sid, self.reply((0, 1)))
        kept = []
        out = agent_tasks.answer_check(self.con, config.load(), "q", ["q"], self.reply((0, 1)), vault="one", session=sid, items_out=kept)
        self.assertEqual(self.written(), [])
        self.assertNotIn("items", out)
        self.assertEqual(len(kept), 6)   # the items the dispatch writes its page from

    def test_neural_check_returns_the_page_for_a_session_and_for_a_plain_ticket(self):
        for key, ticket in (("session", self.ask(range(6), session="new")["session"]), ("ticket", self.ask([0, 1])["ticket"])):
            with self.subTest(key):
                out = self.call("neural_check", **{key: ticket, "reply": self.reply((0, 1))})
                path = Path(out["html_path"])
                self.assertTrue(path.is_file() and path.parent == answer.OUTPUT_DIR and out["html_url"] == path.as_uri())
                text = path.read_text(encoding="utf-8")
                self.assertIn(html.escape(self.rows[0]["text"]), text)
                self.assertIn("ROS", text)
        self.assertEqual(len(self.written()), 2)

    def test_ask_check_returns_the_page_with_and_without_a_session_and_not_the_items(self):
        sid = self.ask(range(6), session="new")["session"]
        out = self.call("ask_check", question="ROS", queries=["ROS"], reply=self.reply((0, 1)), view="evidence4", vault="one", session=sid)
        self.assertNotIn("items", out)
        self.assertTrue(Path(out["html_path"]).is_file())
        args = dict(question="ROS", queries=["ROS"], view="evidence4", vault="one", speed="fast")
        self.call("ask_prepare", **args)
        plain = self.call("ask_check", reply={"answer": [{"text": "Cells were counted [#1].", "evidence": [1]}]}, **args)
        first = plain["answer"]["evidence"][0]["quote"]
        self.assertNotIn("items", plain)
        page = Path(plain["html_path"]).read_text(encoding="utf-8")
        self.assertIn(html.escape(first), page)
        self.assertIn(self.pid, page)   # the id is in the source list
        self.assertEqual(len(self.written()), 2)

    def test_a_failed_write_does_not_fail_the_check(self):
        sid = self.ask(range(6), session="new")["session"]
        with patch.object(answer, "write_html", side_effect=OSError("disk full")):
            out = self.call("neural_check", session=sid, reply=self.reply((0, 1)))
        self.assertEqual(out["answer"][0]["check"], "ok")
        self.assertEqual(out["html_error"], "disk full")

    def test_a_failed_page_of_any_kind_does_not_fail_the_check(self):   # review U10b: only OSError was caught
        sid = self.ask(range(6), session="new")["session"]
        for target, name in ((answer, "render_html"), (neural, "ticket_snapshot")):
            with self.subTest(name), patch.object(target, name, side_effect=KeyError("x")):
                out = self.call("neural_check", session=sid, reply=self.reply((0, 1)))
                self.assertEqual(out["answer"][0]["check"], "ok")
                self.assertIn("html_error", out)
                self.assertNotIn("html_path", out)
        self.assertEqual(self.written(), [])


if __name__ == "__main__":
    unittest.main()
