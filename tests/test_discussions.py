"""W5b: one answer protocol for every AI; saved discussions are discussion nodes, never overwritten."""
import unittest

from . import test_core as fixtures
from kbsys import config, discussions, mcp_server, neural


class DiscussionTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def test_protocol_and_save_via_mcp(self):
        rules = mcp_server.call_tool(self.con, config.load(), "get_answer_rules", {})["protocol"]
        self.assertIn("## 1. Tra cứu", rules)
        first = mcp_server.call_tool(self.con, config.load(), "save_discussion",
                                     {"vault": "one", "title": "H3K9me3 / vitrification?", "markdown": "See [[Lee 2025]]."})
        second = discussions.save("one", "H3K9me3 / vitrification?", "Second talk.")
        self.assertNotEqual(first["saved"], second["saved"])  # never overwritten
        text = (config.vault_dir("one") / first["saved"]).read_text(encoding="utf-8")
        self.assertTrue(first["saved"].startswith("Discussions/"))
        self.assertIn("type: discussion", text)
        self.assertIn("[[Lee 2025]]", text)
        with self.assertRaises(ValueError):
            discussions.save("missing_vault", "t", "x")
        for bad in ("..", "../one", "one/.."):  # a vault name from an MCP client must stay inside vaults/
            with self.assertRaises(ValueError):
                discussions.save(bad, "t", "x")

    def test_concurrent_saves_of_one_title_never_overwrite(self):
        import threading
        saved, errors = [], []

        def worker(i):
            try:
                saved.append(discussions.save("one", "Same title", f"BODY-{i}")["saved"])
            except Exception as err:  # reported below
                errors.append(err)
        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(saved)), 8)
        bodies = {(config.vault_dir("one") / name).read_text(encoding="utf-8").rsplit("\n\n", 1)[1].strip() for name in saved}
        self.assertEqual(bodies, {f"BODY-{i}" for i in range(8)})  # nothing lost
        again = discussions.save("one", "Same title", "BODY-9")["saved"]
        self.assertNotIn(again, saved)

    def test_only_research_vaults_and_single_line_title(self):
        for name in ("_Maps", "Registry"):  # mother-vault folders are directories under vaults/ but not research vaults
            (config.VAULTS_DIR / name).mkdir()
            with self.assertRaises(ValueError):
                discussions.save(name, "t", "x")
            self.assertFalse((config.VAULTS_DIR / name / "Discussions").exists())
        with self.assertRaises(ValueError):
            discussions.save("one", "t", "x", kind="other")
        self.assertFalse((config.vault_dir("one") / "Discussions").exists())  # a refused call leaves nothing behind
        saved = discussions.save("one", "Line one\ntype: evidence\n---\nmore", "Body.")
        text = (config.vault_dir("one") / saved["saved"]).read_text(encoding="utf-8")
        self.assertEqual(text.count("\n---\n"), 1)  # only the real front matter closing rule
        self.assertIn("# Line one type: evidence --- more\n", text)
        self.assertNotIn("\r", text)
        with self.assertRaises(ValueError):
            discussions.save("one", "  \n ", "x")
        crlf = discussions.save("one", "Windows client", "line 1\r\nline 2")["saved"]  # a client may send CRLF
        self.assertNotIn(b"\r", (config.vault_dir("one") / crlf).read_bytes())

    def test_neural_reads_discussions_as_discussion_kind(self):
        self.assertIn("'Discussions':'discussion'", open(neural.__file__, encoding="utf-8").read())

    def test_knowledge_gap_kind(self):
        saved = discussions.save("one", "Gap: placental pyroptosis", "Evidence ... AI knowledge ... my view", kind="knowledge_gap")
        text = (config.vault_dir("one") / saved["saved"]).read_text(encoding="utf-8")
        self.assertIn("type: knowledge_gap", text)
        self.assertIn("knowledge-gap", text)
        with self.assertRaises(ValueError):
            discussions.save("one", "t", "x", kind="other")


if __name__ == "__main__":
    unittest.main()
