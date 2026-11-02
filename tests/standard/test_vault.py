"""Data parts of the VN and RQ checks apart from their probes, probes with broken judges, and edges the generic PASS / FAIL cases do not cover."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kbsys import config, conform, notes, review_policy
from kbsys import conform_vault as cv
from kbsys.conform import Store, Vault

from . import cases_vault as cases
from .cases_graph import mem_db, put

conform.load_checks()
found = conform.violations
node, paper, vault, item, queue = cases.node, cases.paper, cases.vault, cases.item, cases.queue


def tmpdir(test):
    folder = tempfile.TemporaryDirectory()
    test.addCleanup(folder.cleanup)
    return Path(folder.name)


class Cases(unittest.TestCase):
    def test_each_fail_case_fails_for_its_own_reason(self):
        expect = {"VN-01": "machine marker", "VN-02": "unapproved section", "VN-03": "machine IDs visible", "VN-04": "broken characters",
                  "VN-05": "does not resolve", "VN-06": "no note embeds", "VN-07": "not the text of the cited sentences",
                  "VN-08": "local dashboard", "VN-09": "lacks -path:\"Review Queue\"", "VN-10": "retired machine note",
                  "RQ-01": "group", "RQ-02": "AI node card", "RQ-03": "box", "RQ-05": "no row in review_decisions.jsonl",
                  "RQ-06": "not at the vault root", "RQ-07": "failed_pdf"}
        for cid, text in expect.items():
            with self.subTest(cid):
                got = found(cid, *cases.CASES[cid][1](tmpdir(self)))
                self.assertTrue(any(text in v for v in got), (cid, got))

    def test_red_cases_pass_with_a_fixed_product(self):
        # RQ-01 / RQ-02: review_digest.write
        for cid in ("RQ-01", "RQ-02"):
            self.assertEqual(found(cid, *cases.CASES[cid][0](tmpdir(self)), cases.good_write), [], cid)


class Notes(unittest.TestCase):
    def check(self, cid, files, *rest, attachments=()):
        return found(cid, vault(tmpdir(self), files, attachments), *rest)

    def test_vn02_misordered_empty_and_placeholder(self):
        misordered = node(body="## Sources\n\n- [[A]]\n\n## Definition\n\n- Cells were counted.\n")
        self.assertTrue(any("out of order" in v for v in self.check("VN-02", [("Concepts/A.md", misordered)])))
        self.assertTrue(any("empty section" in v for v in self.check("VN-02", [("Concepts/A.md", node(body="## Definition\n\n## Sources\n\n- [[A]]\n"))])))
        self.assertTrue(any("placeholder bullet" in v for v in self.check("VN-02", [("Concepts/A.md", node(body="## Definition\n\n- —\n"))])))
        result = "---\ntype: result\n---\n# EVD\n\n" + notes.AUTO_START + "\n## Finding\n\nText.\n\n## Whatever\n\nText.\n" + notes.AUTO_END + "\n"
        self.assertTrue(any("unapproved" in v for v in self.check("VN-02", [("Results/EVD - A.md", result)])))
        self.assertEqual(self.check("VN-02", [("Mine/Ideas/I.md", "## anything\n")]), [])

    def test_vn03_titles_and_catalogue_numbers(self):
        self.assertTrue(any("file name" in v for v in self.check("VN-03", [("Concepts/Cells E0077.md", node())])))
        self.assertTrue(any("in the title" in v for v in self.check("VN-03", [("Concepts/A.md", node(front="type: concept\ntitle: Cells p1_ab12:S0123\n"))])))
        self.assertTrue(any("machine IDs" in v for v in self.check("VN-03", [("Concepts/A.md", node(body="## Definition\n\n### E0077 noted\n"))])))
        callout = "## Definition\n\n- Cells (Cat. No. S1888).\n\n> [!info]- Source IDs\n> - Chen 2020, p. 3: p1_ab12:S0001, p1_ab12:E0002\n"
        self.assertEqual(self.check("VN-03", [("Concepts/A.md", node(body=callout))]), [])

    def test_vn04_raw_data_and_case_links(self):
        for body, text in (("- {'measure': 'survival', 'value': 3}\n", "raw dict"), ("- Result: None\n", "bare None"),
                           ("- Cells (n=3, p=None)\n", "bare None"), ("- See [[caspase]].\n", "differs from the note title"),
                           ("- Cells were counted � in group A.\n", "broken characters")):
            files = [("Concepts/A.md", node(body="## Definition\n\n" + body)), ("Concepts/Caspase.md", node("Caspase"))]
            self.assertTrue(any(text in v for v in self.check("VN-04", files)), (body, text))
        files = [("Concepts/A.md", node(body="## Definition\n\n- None of the cells survived. See [[Caspase]].\n")), ("Concepts/Caspase.md", node("Caspase"))]
        self.assertEqual(self.check("VN-04", files), [])
        commented = node(body="## Definition\n\n- Cells were counted.\n<!-- kb:synthesis-state:{\"model\": \"x\"} -->\n")
        self.assertEqual(self.check("VN-04", [("Concepts/A.md", commented)]), [])

    def test_vn05_rules(self):
        files = [("Concepts/A.md", node(body="## Definition\n\n- [Sibling](B.md) and ![](../_Attachments/p/F1.png) and [site](https://example.org/x.png).\n")),
                 ("Concepts/B.md", node("B"))]
        self.assertEqual(self.check("VN-05", files, None, lambda v: [], attachments=["_Attachments/p/F1.png"]), [])
        broken = [("Concepts/A.md", node(body="## Definition\n\n- [Gone](Gone.md) and ![](../_Attachments/p/gone.png).\n"))]
        self.assertEqual(len(self.check("VN-05", broken, None, lambda v: [])), 2)
        counted = [("Mine/Ideas/I.md", "[[Nothing]]\n"), ("_Templates/T.md", "[[{{title}}]]\n")]
        self.assertEqual(self.check("VN-05", counted, None, lambda v: []), [])  # human-managed notes are not counted
        both = [("Concepts/A.md", node(body="## Definition\n\n| x |\n|---|\n| [[Caspase\\|alias]] |\n")), ("Concepts/Caspase.md", node("Caspase"))]
        self.assertEqual(self.check("VN-05", both), [])  # the product index resolves the escaped pipe
        defect = self.check("VN-05", both, None, lambda v: [("Concepts/A.md", "Caspase\\|alias")])  # an index that does not
        self.assertTrue(any("vault index" in v for v in defect), defect)
        gone = self.check("VN-05", [("Concepts/A.md", node(body="## Definition\n\n- See [[Nowhere]].\n"))])  # the real index calls it missing too
        self.assertEqual(len(gone), 1, gone)  # reported once, as broken, not again as "Obsidian resolves it"
        self.assertIn("does not resolve", gone[0])

    def test_vn06_references_and_duplicates(self):
        tmp = tmpdir(self)
        put(tmp / "v" / "_Attachments" / "p1" / "F3.png", "same")
        put(tmp / "v" / "_Attachments" / "p1" / "F4.png", "same")
        put(tmp / "v" / "_Attachments" / "p1" / "F5.png", "other")
        put(tmp / "v" / "_Attachments" / "p1" / "manifest.json", "{}")
        att = [f"_Attachments/p1/{n}" for n in ("F3.png", "F4.png", "F5.png", "manifest.json")]
        got = found("VN-06", Vault("v", tmp / "v", notes=[("Concepts/A.md", node(body="## Definition\n\n![[F5.png]]\n"))], attachments=att))
        self.assertEqual(sorted(v.split(":")[0] for v in got), ["_Attachments/p1/F3.png", "_Attachments/p1/F4.png"])
        self.assertTrue(all("identical file" in v for v in got))

    def test_vn07_citation_quote_and_size_rules(self):
        t = tmpdir(self)
        for quote, alias, text in (("Cells were counted in group A. Dishes", "Chen 2020, p. 3", "not the text"),
                                   ("Cells were counted in group A.", "Chen", "not 'Author Year, p. N'"),
                                   ("Cells were counted in group A.", "Chen 2020 p 3", "not 'Author Year, p. N'")):
            got = found("VN-07", *cases.vn07(tmpdir(self), quote, alias))
            self.assertTrue(any(text in v for v in got), (quote, alias, got))
        self.assertEqual(found("VN-07", *cases.vn07(t, "[…] Dishes were washed twice.")), [])  # a lower-case start marker is allowed
        self.assertEqual(found("VN-07", *cases.vn07(tmpdir(self), "The The")), [])  # a fragment is a sentence too
        big = found("VN-07", *cases.vn07(tmpdir(self), "Cells were counted in group A.", filler=notes.PAPER_NOTE_MAX_BYTES + 10))
        self.assertTrue(any("over the cap" in v for v in big), big)
        no_ids = node(body="## Definition\n\n> [!quote]- [[Chen 2020|Chen 2020, p. 3]] · results\n> Cells were counted in group A.\n")
        got = found("VN-07", vault(tmpdir(self), [("Concepts/A.md", no_ids)]), cases.sentences)
        self.assertTrue(any("no ID in the Source IDs" in v for v in got), got)
        edited = paper(body="## Tóm tắt (Abstract)\n\n- p. 3\n> Cells were counted in group B.\n\nFigure 1\n> [!figure] Caption (verbatim)\n> Anything.\n")
        got = found("VN-07", vault(tmpdir(self), [("Papers/Chen 2020.md", edited)]), cases.sentences)
        self.assertEqual(len([v for v in got if "paper's sentences" in v]), 1, got)

    def test_vn07_caps_agree_with_the_writer(self):
        self.assertEqual((cv.standard.param("VN-07", "paper_note_kib") * 1024, cv.standard.param("VN-07", "node_note_kib") * 1024),
                         (notes.PAPER_NOTE_MAX_BYTES, notes.NODE_NOTE_MAX_BYTES))

    def test_vn08_pdf_links_vietnamese_and_exemptions(self):
        vi = node(body="## Definition\n\n- Cells were counted.\n- Đọc từ ảnh bởi AI.\n> Đọc quote is exempt.\n")
        got = self.check("VN-08", [("Concepts/A.md", vi)])
        self.assertEqual(len(got), 1)
        self.assertIn("1 machine-written lines mix Vietnamese", got[0])
        quoted_only = node(body="## Definition\n\n> [!quote]- x\n> Đọc quote.\n")
        self.assertEqual(self.check("VN-08", [("Concepts/A.md", quoted_only)]), [])
        index = node(body="## Bản đồ\n").replace(cases.FP, notes.INDEX_MARK)
        self.assertEqual(self.check("VN-08", [("_Maps/Index.md", index), ("Papers/Chen 2020.md", paper())]), [])
        self.assertEqual(self.check("VN-08", [("Mine/Ideas/I.md", "http://127.0.0.1:8765/neural?tab=note\n")]), [])
        self.assertTrue(self.check("VN-08", [("Home.md", "[x](http://localhost:8765/graph)\n")]))

    def test_vn08_only_the_paper_heading_anchor_is_exempt(self):
        head = notes.PAPER_FIGURES_HEADING
        ok = node(body=f"## Definition\n\n- see [[Lee 2025#{head}|p. 5]] and ![[Lee 2025#{head}|x]]\n")
        self.assertEqual(self.check("VN-08", [("Concepts/A.md", ok)]), [])
        for line in ("[[Lee 2025#Đọc từ ảnh bởi AI|a]]", f"[[Lee 2025#{head}]]", "Giá trị #Đọc ảnh", f"[[Lee 2025#{head} được|a]]"):
            got = self.check("VN-08", [("Concepts/A.md", node(body=f"## Definition\n\n- {line}\n"))])
            self.assertEqual(len(got), 1, (line, got))

    def test_vn09_each_rule(self):
        for kwargs, text in (({"hide": False}, "hideUnresolved"), ({"drop_filter": "-tag:#kb-index"}, "-tag:#kb-index"),
                             ({"drop_filter": "-file:Home"}, "-file:Home")):
            got = found("VN-09", *cases.graph_files(tmpdir(self), **kwargs))
            self.assertTrue(any(text in v for v in got), (kwargs, got))
        t = tmpdir(self)
        cases.graph_files(t)
        path = t / "v" / ".obsidian" / "graph.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data["colorGroups"].reverse()  # the machine groups are no longer first, in order
        data["showAttachments"] = True
        path.write_text(json.dumps(data), encoding="utf-8")
        got = found("VN-09", Vault("v", t / "v", notes=[]))
        self.assertTrue(any("colour groups" in v for v in got) and any("showAttachments" in v for v in got), got)
        t = tmpdir(self)
        cases.graph_files(t)
        plugin = t / "v" / ".obsidian" / "plugins" / "extended-graph" / "data.json"
        plugin.write_text(plugin.read_text(encoding="utf-8").replace('"#dc2626"', '"#15803d"'), encoding="utf-8")
        self.assertTrue(any("conflicts_with" in v for v in found("VN-09", Vault("v", t / "v", notes=[]))))
        self.assertEqual(found("VN-09", Vault("v", tmpdir(self) / "v", notes=[])), [".obsidian/graph.json is missing"])

    def test_vn09_accepts_what_the_writer_produces(self):
        with cv._sandbox(vaults=("one",)) as (root, con):
            notes.write_graph_settings("one")
            self.assertEqual(found("VN-09", Vault("one", config.vault_dir("one"), notes=[])), [])


class Probes(unittest.TestCase):
    def test_vn01_a_rebuild_that_writes_mine_or_human_text_is_caught(self):
        def rewrite(con, vault_name):
            notes.refresh_all(con, vault_name)
            for p in (config.vault_dir(vault_name) / "Mine").rglob("*.md"):
                p.write_text("changed\n", encoding="utf-8", newline="\n")
            for p in (config.vault_dir(vault_name) / "Papers").glob("Note p1.md"):
                p.write_text(p.read_text(encoding="utf-8").replace("My line after the block.", ""), encoding="utf-8", newline="\n")
        got = list(cv.probe_rebuild(rewrite))
        self.assertTrue(any("Mine/" in v for v in got) and any("human text" in v for v in got), got)
        self.assertTrue(any("vacuous" in v for v in cv.probe_rebuild(lambda con, name: None)))
        self.assertEqual(list(cv.probe_rebuild()), [])

    def test_vn01_markers_must_be_one_pair(self):
        text = node() + notes.AUTO_START + "\n"
        got = found("VN-01", Store(vaults=[vault(tmpdir(self), [("Concepts/A.md", text)])]))
        self.assertTrue(any("not one start followed by one end" in v for v in got), got)

    def test_vn10_human_text_must_survive(self):
        def lose_text(vault_name, live):
            for p in (config.vault_dir(vault_name) / "Organisms").glob("Old human.md"):
                p.write_text(notes.AUTO_START + "\n" + notes.AUTO_END + "\n", encoding="utf-8", newline="\n")
            return []
        got = found("VN-10", lose_text)
        self.assertTrue(any("lost the text" in v for v in got) and any("not in knowledge_store/removed" in v for v in got), got)
        self.assertEqual(found("VN-10"), [])

    def test_rq07_a_broken_refresh_is_caught(self):
        real = review_policy.refresh

        def purge_and_reflag(con, vault_name, apply=False):
            real(con, vault_name, apply=apply)
            con.execute("INSERT INTO review_items (paper_id, kind, detail) VALUES ('pc', 'topic_relevance', '[one] again')")
            con.execute("DELETE FROM papers WHERE paper_id = 'pa'")
        got = list(cv.probe_refresh(purge_and_reflag))
        self.assertTrue(any("flagged papers" in v for v in got) and any("purged" in v for v in got), got)
        self.assertEqual(list(cv.probe_refresh()), [])

    def test_rq07_connected_paper_must_not_be_flagged(self):
        got = found("RQ-07", *cases.rq07(tmpdir(self), connected=True))
        self.assertTrue(any("says is connected" in v for v in got), got)


class Queue(unittest.TestCase):
    def test_rq01_groups_and_tags(self):
        three = queue().split("## 4.")[0]
        self.assertTrue(any("3 groups" in v for v in cv.queue_group_problems(three)))
        self.assertTrue(any("group 2 but sits under group 1" in v for v in cv.queue_group_problems(queue({1: item(2)}))))
        self.assertEqual(list(cv.queue_group_problems(queue({1: item(1), 2: item(2, "ai:x"), 3: item(3, "orphan:y"), 4: item(4, "claim:z")}))), [])
        self.assertTrue(any("no group tag" in v for v in cv.queue_group_problems(queue({1: item().replace("[Nhóm 1]", "")}))))
        self.assertTrue(any("no group tag" in v for v in cv.queue_group_problems(queue({1: item().replace("[Nhóm 1]", "[Nhóm 5]")}))))

    def test_rq02_each_forbidden_kind(self):
        for key, text in (("card:local_cells", "AI node card"), ("conflict:A", "conflict"), ("crop:p1:F1", "wrong crop")):
            self.assertTrue(any(text in v for v in cv.queue_forbidden(queue({1: item(1, key)}))), key)
        for body, text in (("[[CONFLICT - A vs B]]", "conflict"), ("[[Open hypotheses]]", "open hypothesis")):
            self.assertTrue(any(text in v for v in cv.queue_forbidden(queue({1: item(extra=body + "\n")}))), body)
        self.assertTrue(any("open hypothesis" in v for v in cv.queue_forbidden(queue().replace("Sai do AI, sửa chưa chắc", "Giả thuyết mở"))))
        self.assertEqual(list(cv.queue_forbidden(queue({1: item(), 2: item(2, "ai:x")}))), [])
        wordy = item().replace("the caption differs", "a hypothesis is stated as a result in a conflict of views")  # words of the paper, not a kind
        self.assertEqual(list(cv.queue_forbidden(queue({1: wordy}))), [])

    def test_rq03_each_missing_part(self):
        sentence = cases.SENTENCE_TEXT
        for part, expect in (("> *nguy", "quote"), ("**Máy phát hiện:**", "finding"), ("**Đề xuất:**", "proposal"), ("Giữ nguyên", "box 'Giữ nguyên'")):
            text = item().replace(part, "X " + part[1:] if part == "> *nguy" else "X")
            got = found("RQ-03", cases.queue_vault(tmpdir(self), queue({1: text})), sentence)
            self.assertTrue(any(expect in v for v in got), (part, got))
        edited = item().replace("Cells were counted in group A.", "Cells were counted in group B.")
        got = found("RQ-03", cases.queue_vault(tmpdir(self), queue({1: edited})), sentence)
        self.assertTrue(any("the quote is not the text of p1:S0001" in v for v in got), got)
        self.assertTrue(any("lacks" in v for v in found("RQ-03", cases.queue_vault(tmpdir(self), cases.LEGACY), sentence)))

    def test_rq05_log_rows_need_key_and_decision(self):
        t = tmpdir(self)
        put(t / "s" / "review_decisions.jsonl", json.dumps({"key": "fig:p1:F3"}) + "\n")
        got = found("RQ-05", Store(root=t / "s", vaults=[]))
        self.assertTrue(any("no key or no decision" in v for v in got), got)
        legacy = cases.LEGACY.replace("- [ ]", "- [x]")
        self.assertEqual(found("RQ-05", Store(root=t / "none", vaults=[cases.queue_vault(t, legacy)])), [])  # an old-format tick is "seen", not a decision

    def test_rq06_each_rule(self):
        for kwargs, text in (({"first": "Index"}, "first link of Home.md"), ({"pinned": False}, "not pinned")):
            self.assertTrue(any(text in v for v in found("RQ-06", *cases.rq06(tmpdir(self), **kwargs))), kwargs)
        t = tmpdir(self)
        home = ("Home.md", "# Home\n\n- [[Cần duyệt]]\n")
        got = found("RQ-06", Vault("v", t / "v", notes=[home, (cases.QUEUE, queue(instructions=2))]))
        self.assertTrue(any("2 instruction lines" in v for v in got), got)
        self.assertEqual(found("RQ-06", Vault("v", t / "v", notes=[home])), [f"no {cases.QUEUE} in the vault"])
        put(t / "v" / ".obsidian" / "bookmarks.json", json.dumps({"items": [{"type": "file", "path": cases.QUEUE}]}))
        self.assertFalse(any("not pinned" in v for v in found("RQ-06", Vault("v", t / "v", notes=[home, (cases.QUEUE, queue())]))))

    def test_real_writer_output_is_a_clean_queue(self):
        """The queue review_digest.write produces for one of each old kind: four groups, no card, conflict, hypothesis or wrong crop."""
        found_texts = cv._synthetic_queue(cv.review_digest.write)
        self.assertEqual(len(found_texts), 1)
        self.assertEqual(list(cv.queue_forbidden(found_texts[0])) + list(cv.queue_group_problems(found_texts[0])), [])


if __name__ == "__main__":
    unittest.main()
