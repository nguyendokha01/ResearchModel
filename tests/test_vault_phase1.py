import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, concept_notes, notes


class VaultPhase1Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            VAULTS_DIR=root / "vaults")
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_palette_order_preserves_researcher_groups_and_other_graph_keys(self):
        path = config.vault_dir("one") / ".obsidian" / "graph.json"
        path.parent.mkdir(parents=True)
        custom = {"query": "tag:#researcher-choice", "color": {"a": 1, "rgb": 123}}
        original = {
            "colorGroups": [custom, {"query": "path:\"Papers\"", "color": {"a": 1, "rgb": 7}}],
            "search": "tag:#keep-this", "zoom": 3, "showTags": True, "customSetting": {"x": 1},
        }
        path.write_text(json.dumps(original), encoding="utf-8")

        notes.write_graph_settings("one")
        settings = json.loads(path.read_text(encoding="utf-8"))
        groups = settings["colorGroups"]
        # U6g: the researcher's own groups come first (their choice wins), then the machine groups in order
        self.assertEqual(groups[0], custom)
        self.assertEqual([g["query"] for g in groups[1:]], [g[0] for g in notes.GRAPH_GROUPS])
        self.assertEqual(groups[1]["query"], 'path:"Mine/"')  # personal notes win over every machine group
        self.assertEqual(groups[3]["color"]["rgb"], int("DC2626", 16))
        self.assertNotIn("[type:conflict]", [g["query"] for g in groups])  # old machine query replaced
        self.assertIn(custom, groups)
        self.assertEqual(settings["search"], original["search"])
        self.assertEqual(settings["zoom"], original["zoom"])
        self.assertEqual(settings["showTags"], original["showTags"])
        self.assertEqual(settings["customSetting"], original["customSetting"])

    def test_cap_keeps_complete_quote_rows_and_adds_dashboard_overflow(self):
        rows = []
        for i in range(1, 7):
            rows.append(
                f"> [!quote]- Paper p · `p:E{i}`\n"
                f"> Evidence sentence {i} with a complete source passage.\n"
                f"> `p:S{i}`\n")
        block = "\n".join([notes.AUTO_START, "## Summary", "", "Profile stays", *rows, notes.AUTO_END])
        capped = notes.cap_auto_block(block, 700, "concept")

        self.assertLessEqual(len(capped.encode("utf-8")), 700)
        self.assertIn("## Summary", capped)
        self.assertIn("Evidence sentence 1 with a complete source passage.", capped)
        omitted = sum(
            f"Evidence sentence {i} with a complete source passage." not in capped
            for i in range(1, 7))
        self.assertGreater(omitted, 0)
        self.assertIn(f"{omitted} more item", capped)
        self.assertNotIn("127.0.0.1", capped)
        for i in range(1, 7):
            row = f"Evidence sentence {i} with a complete source passage."
            if row in capped:
                self.assertIn(f"`p:S{i}`", capped)
                self.assertNotIn(f"Evidence sentence {i} with a complete source passage.\n> `p:S{i}`\n> [!quote]", capped)

    def test_human_section_is_unchanged_when_machine_block_is_capped(self):
        path = config.vault_dir("one") / "Substances" / "Term.md"
        path.parent.mkdir(parents=True)
        path.write_text(
            "---\ntags: [mine]\n---\n# Term\n\n" + notes.AUTO_START + "\nold\n" +
            notes.AUTO_END + "\n\n## My notes\nHuman hypothesis must remain.\n", encoding="utf-8")
        record = {"concept_id": "term", "name": "Term", "type": "molecule", "synonyms": [], "parents": []}
        rows = [f"> [!quote]- p:E{i}\n> {'x' * 500}\n> `p:S{i}`\n" for i in range(8)]
        block = "\n".join([notes.AUTO_START, "## Synthesis", "", *rows, notes.AUTO_END])
        capped = notes.cap_auto_block(block, 4000, "concept")
        self.assertEqual(concept_notes._write_note("one", record, capped, "fingerprint"), "updated")
        updated = path.read_text(encoding="utf-8")
        self.assertIn("Human hypothesis must remain.", updated)
        self.assertLessEqual(len(capped.encode("utf-8")), 4000)


if __name__ == "__main__":
    unittest.main()
