"""G1 / N14: MCP arguments are checked at the stdio boundary (paper_id, vault, limit)."""
import json
import unittest

from . import test_core as fixtures
from kbsys import config, mcp_server


class McpArgumentTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def call(self, tool, **args):
        reply = mcp_server.handle(self.con, config.load(), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": tool, "arguments": args}})
        if "error" in reply:  # JSON-RPC error: missing required argument
            return True, reply["error"]["message"]
        return reply["result"]["isError"], reply["result"]["content"][0]["text"]

    def test_paper_id_cannot_leave_the_papers_folder(self):
        outside = config.PAPERS_DIR.parent / "escape"  # <store>/escape is reachable as paper_id '../escape'
        outside.mkdir()
        for name in ("evidence.jsonl", "references.jsonl"):
            (outside / name).write_text(json.dumps({"unit_id": "SECRET"}) + "\n", encoding="utf-8")
        (outside / "analysis_state.json").write_text('{"status": "SECRET"}', encoding="utf-8")
        for tool, extra in (("get_evidence", {}), ("lookup_reference", {"number": 1}), ("analysis_status", {}),
                            ("read_paper", {})):
            for bad in ("../escape", "..", str(outside), "no_such_paper_2026", "", 7):
                error, text = self.call(tool, paper_id=bad, **extra)
                self.assertTrue(error, (tool, bad))
                self.assertNotIn("SECRET", text)
        error, _ = self.call("analysis_status", paper_id=self.pid)  # a stored paper still works
        self.assertFalse(error)

    def test_limit_is_clamped_and_must_be_an_integer(self):
        error, text = self.call("search_sentences", query="ROS", vault="one", limit=-1)  # SQLite LIMIT -1 = unlimited
        self.assertFalse(error)
        self.assertEqual(len(json.loads(text)), 1)
        error, text = self.call("search_sentences", query="ROS", vault="one", limit=10_000)
        self.assertFalse(error)
        self.assertLessEqual(len(json.loads(text)), 100)
        for bad in ("abc", True, 2.5, [3]):
            self.assertTrue(self.call("search_sentences", query="ROS", vault="one", limit=bad)[0], bad)
        args = {"paper_id": self.pid, "limit": 10_000}
        mcp_server._check_args(self.con, "read_paper", args)
        self.assertEqual(args["limit"], 500)  # a reading page may be longer than a search result
        mcp_server._check_args(self.con, "find_overlaps", args)
        self.assertEqual(args["limit"], 100)

    def test_vault_must_be_a_research_vault(self):
        self.assertFalse(self.call("list_pending_papers", vault="one")[0])
        self.assertFalse(self.call("list_pending_papers")[0])
        for bad in ("nonexistent_vault", "../x", ["one"], 5):
            error, text = self.call("list_pending_papers", vault=bad)
            self.assertTrue(error, bad)
            self.assertIn("Unknown vault", text)
        self.assertTrue(self.call("search_sentences", query="ROS", vault="nonexistent_vault")[0])
        (config.VAULTS_DIR / "_Maps").mkdir()  # a mother-vault folder is not a research vault
        self.assertTrue(self.call("search_sentences", query="ROS", vault="_Maps")[0])


if __name__ == "__main__":
    unittest.main()
