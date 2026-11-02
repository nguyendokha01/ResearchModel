"""RQ-01..RQ-03, RQ-06: `Cần duyệt.md` at the vault root holds four groups of self-contained items and keeps ticked boxes."""
import json
import unittest

from . import test_core as fixtures
from kbsys import config, conform, conform_vault as cv, db, notes, review_digest, review_policy, vault_layout
from kbsys.conform import Vault

conform.load_checks()
ROOT_NAME = "Cần duyệt.md"


class ReviewDigestTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def readings(self):
        sid = lambda n: f"{self.pid}:S{n:04d}"
        db.write_json(db.paper_dir(self.pid) / "metadata.json", {"paper_id": self.pid, "note_name": "Paper P", "year": 2026, "authors": [{"name": "Chen A"}]})
        folder = db.paper_dir(self.pid) / "figure_readings"
        db.write_json(folder / "F1.json", {"label": "Figure 1", "red_warning_candidate": True,
                                           "caption_consistency": [{"claim_sentence_id": sid(2), "status": "inconsistent", "note": "the figure shows less"}]})
        db.write_json(folder / "F2.json", {"label": "Figure 2", "caption_consistency": [{"claim_sentence_id": sid(3), "status": "inconsistent", "note": "panel B differs"}]})
        db.write_json(folder / "F3.json", {"label": "Figure 3", "crop_wrong": True, "note": "Only the page header."})

    def write(self):
        review_digest.write(self.con, "one")
        return (config.vault_dir("one") / ROOT_NAME).read_text(encoding="utf-8")

    def test_four_groups_quote_by_code_and_no_other_kind(self):
        self.readings()
        db.write_json(config.STORE / "node_cards" / "one" / "local_x.json",
                      {"card": {"name": "Histone"}, "status": "accepted", "unit_ids": ["a"], "paper_ids": ["p"]})
        (config.vault_dir("one") / "Claims").mkdir()
        (config.vault_dir("one") / "Claims" / "CONFLICT - A vs B.md").write_text("---\nstatus: open\n---\n", encoding="utf-8")
        text = self.write()
        self.assertFalse((config.vault_dir("one") / "Review Queue" / ROOT_NAME).exists())
        self.assertEqual(list(cv.queue_group_problems(text)) + list(cv.queue_forbidden(text)), [])
        group1, group2, group3, group4 = text.split("\n## ")[1:]
        self.assertIn("> ROS seems to increase survival in the example model. — (Chen A 2026, p. 1)", group1)
        self.assertIn("Bài: [[Paper P]] · PDF trang 1", group1)
        self.assertIn("**Máy phát hiện:** Figure 1: the figure shows less", group1)
        self.assertIn("> The example protocol used 5 mg for 2 hours. — (Chen A 2026, p. 1)", group2)
        self.assertEqual(text.count("### [Nhóm"), 2)  # the wrong crop, the card and the conflict are not items
        self.assertTrue("\n*Không có.*\n" in group3 and "\n*Không có.*\n" in group4)  # an empty group says so
        sentence = lambda sid: next(r[0] for r in self.con.execute("SELECT text FROM sentences WHERE sentence_id = ?", (sid,)))
        self.assertEqual(conform.violations("RQ-03", Vault("one", config.vault_dir("one"), notes=[(ROOT_NAME, text)]), sentence), [])

    def test_orphan_paper_is_a_group_3_item(self):
        db.add_review_item(self.con, review_policy.KIND, "[one] No supported connection", self.pid)
        text = self.write()
        self.assertIn("### [Nhóm 3]", text.split("\n## 3.")[1].split("\n## 4.")[0])
        self.assertIn("> Reactive oxygen species (ROS) may affect the test model.", text)
        self.assertEqual(list(cv.queue_group_problems(text)), [])

    def test_ticks_and_text_outside_the_block_survive(self):
        self.readings()
        path = config.vault_dir("one") / ROOT_NAME
        path.write_text(self.write().replace("- [ ] Giữ nguyên, không phải lỗi", "- [x] Giữ nguyên, không phải lỗi", 1) + "\nMy decision: keep.\n", encoding="utf-8")
        text = self.write()
        self.assertEqual(text.count("- [x] Giữ nguyên, không phải lỗi"), 1)
        self.assertEqual(text.count("- [x]"), 1)
        self.assertIn("- [x] Giữ nguyên", text.split("\n## 2.")[0])  # still on the item of group 1
        self.assertIn("My decision: keep.", text)

    def test_note_newline_cannot_open_an_item_or_tick_a_box(self):
        db.write_json(db.paper_dir(self.pid) / "metadata.json", {"paper_id": self.pid, "note_name": "Paper P"})
        db.write_json(db.paper_dir(self.pid) / "figure_readings" / "F2.json", {"label": "Figure 2", "caption_consistency": [
            {"claim_sentence_id": f"{self.pid}:S0002", "status": "inconsistent", "note": "a\n### [Nhóm 2] fake\n- [x] Đồng ý đề xuất"}]})
        text = self.write()
        self.assertEqual(([l[:9] for l in text.splitlines() if l.startswith("### ")], [l for l in text.splitlines() if l.startswith("- [x]")]), (["### [Nhóm"], []))
        self.assertEqual(review_digest.write(self.con, "one"), {"items": [0, 1, 0, 0]})

    def test_deleted_box_line_comes_back_and_an_item_without_a_paper_links_an_existing_note(self):
        self.readings()
        path = config.vault_dir("one") / ROOT_NAME
        path.write_text(self.write().replace("- [ ] Khác (ghi chú của bạn, ngoài khối tự động)\n", ""), encoding="utf-8")
        self.assertEqual(self.write().count("- [ ] Khác"), 2)  # one per item of groups 1 and 2
        db.add_review_item(self.con, review_policy.KIND, "[one] No supported connection", None)
        self.assertNotIn("[[None]]", self.write())

    def test_old_file_in_review_queue_moves_to_the_root(self):
        old = config.vault_dir("one") / "Review Queue" / ROOT_NAME
        old.parent.mkdir()
        old.write_text(notes.INDEX_FRONTMATTER + f"{notes.AUTO_START}\n{notes.INDEX_MARK}\n# Cần duyệt\n\n- [ ] [[Cells]] <!--k:card:x-->\n{notes.AUTO_END}\n\nMy old note.\n", encoding="utf-8")
        text = self.write()
        self.assertFalse(old.exists())
        self.assertIn("My old note.", text)
        self.assertNotIn("Cells", text)

    def test_queue_is_easy_to_find(self):
        """RQ-06: at the root, three instruction lines, the first link of Home (only the pin, a researcher's choice, is open)."""
        vdir = config.vault_dir("one")
        vault_layout._write_home(vdir, "one", [])
        queue = self.write()
        found = conform.violations("RQ-06", Vault("one", vdir, notes=[("Home.md", (vdir / "Home.md").read_text(encoding="utf-8")), (ROOT_NAME, queue)]))
        self.assertEqual(len(review_digest.INSTRUCTIONS), cv.INSTRUCTION_LINES)
        self.assertEqual(found, [])  # the queue is bookmarked by the write (RQ-06 pin, researcher 2026-10-18)

    def test_pin_adds_a_bookmark_once_and_keeps_the_researchers_entries(self):
        path = config.vault_dir("one") / ".obsidian" / "bookmarks.json"
        mine = {"type": "file", "path": "Home.md", "title": "Home"}
        db.write_json(path, {"items": [mine, {"type": "group", "title": "G", "items": [{"type": "search", "query": "x"}]}], "extra": 1})
        self.write()
        self.write()
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["extra"], 1)
        self.assertEqual(data["items"][0], mine)
        self.assertEqual(data["items"][1]["items"], [{"type": "search", "query": "x"}])
        self.assertEqual([i["path"] for i in data["items"] if i.get("path") == ROOT_NAME], [ROOT_NAME])
        db.write_json(path, {"items": [{"type": "group", "title": "G", "items": [{"type": "file", "path": ROOT_NAME}]}]})  # already pinned inside a group
        self.write()
        self.assertEqual(len(json.loads(path.read_text(encoding="utf-8"))["items"]), 1)

    def test_pin_leaves_an_unreadable_bookmarks_file_alone(self):
        path = config.vault_dir("one") / ".obsidian" / "bookmarks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{not json", encoding="utf-8")
        self.write()
        self.assertEqual(path.read_text(encoding="utf-8"), "{not json")


if __name__ == "__main__":
    unittest.main()
