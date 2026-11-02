import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, db, mother_vault, notes


class MotherVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            VAULTS_DIR=root / "vaults", CONFIG_DIR=root / "config", DB_PATH=root / "store" / "library.db",
        )
        self.patch.start()
        config.CONFIG_DIR.mkdir(parents=True)
        (config.CONFIG_DIR / "vaults.yaml").write_text(
            "vaults:\n  alpha:\n    vault_kind: research\n    inputs: [pdf]\n  beta:\n    vault_kind: research\n    inputs: [pdf]\n",
            encoding="utf-8",
        )
        for name in ("alpha", "beta"):
            vault = config.VAULTS_DIR / name
            (vault / "_Maps").mkdir(parents=True)
            (vault / "Papers").mkdir()
            (vault / "Home.md").write_text("# Home\n", encoding="utf-8")
            (vault / "_Maps" / "Index.md").write_text("# Index\n", encoding="utf-8")
        self.con = db.connect()
        self.con.execute(
            "INSERT INTO papers (paper_id,title,year,doi,pmid,note_name) VALUES (?,?,?,?,?,?)",
            ("p1", "Shared paper", 2020, "10.1/shared", "123", "Shared paper"),
        )
        self.con.executemany("INSERT INTO paper_vaults VALUES (?,?)", [("p1", "alpha"), ("p1", "beta")])
        self.con.commit()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.temp.cleanup()

    def _refresh(self):
        return mother_vault.refresh(self.con)

    def test_stray_folder_without_home_or_obsidian_is_not_a_vault(self):
        (config.VAULTS_DIR / "vit" / "_Maps").mkdir(parents=True)  # what a leaking test once left behind
        self._refresh()
        self.assertEqual(config.research_vaults(), ["alpha", "beta"])
        self.assertFalse((config.VAULTS_DIR / "Registry" / "Vault - vit.md").exists())

    def test_shared_paper_registry_has_both_path_qualified_links(self):
        self._refresh()
        path = config.VAULTS_DIR / "Registry" / "Papers" / "p1.md"
        text = path.read_text(encoding="utf-8")
        self.assertIn("in_vaults:\n- alpha\n- beta", text)
        self.assertIn("[[alpha/Papers/Shared paper|alpha]]", text)
        self.assertIn("[[beta/Papers/Shared paper|beta]]", text)
        self.assertNotIn("[[Home]]", text)

    def test_human_text_outside_markers_survives(self):
        self._refresh()
        path = config.VAULTS_DIR / "_Maps" / "Efforts.md"
        path.write_text(path.read_text(encoding="utf-8") + "\nHuman note stays.\n", encoding="utf-8")
        self._refresh()
        self.assertIn("Human note stays.", path.read_text(encoding="utf-8"))

    def test_researcher_graph_keys_survive(self):
        graph = config.VAULTS_DIR / ".obsidian" / "graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_text(json.dumps({
            "search": "tag:#researcher-choice", "zoom": 3,
            "colorGroups": [{"query": "tag:#researcher-choice", "color": {"a": 1, "rgb": 7}}],
        }), encoding="utf-8")
        self._refresh()
        settings = json.loads(graph.read_text(encoding="utf-8"))
        self.assertEqual(settings["search"], "tag:#researcher-choice")
        self.assertEqual(settings["zoom"], 3)
        self.assertIn("tag:#researcher-choice", [group["query"] for group in settings["colorGroups"]])
        # U6g: researcher groups come first (first match wins), then every machine group in order
        self.assertEqual(settings["colorGroups"][0]["query"], "tag:#researcher-choice")
        self.assertEqual([g["query"] for g in settings["colorGroups"][1:]], [q for q, _, _ in notes.GRAPH_GROUPS])

    def test_refresh_is_idempotent(self):
        self._refresh()
        files = {p.relative_to(config.VAULTS_DIR): p.read_bytes() for p in config.VAULTS_DIR.rglob("*") if p.is_file()}
        self._refresh()
        again = {p.relative_to(config.VAULTS_DIR): p.read_bytes() for p in config.VAULTS_DIR.rglob("*") if p.is_file()}
        self.assertEqual(files, again)

    def test_atlas_never_emits_bare_home_or_index_links(self):
        self._refresh()
        text = (config.VAULTS_DIR / "_Maps" / "Atlas.md").read_text(encoding="utf-8")
        self.assertIn("[[alpha/Home|alpha]]", text)
        self.assertIn("[[alpha/_Maps/Index|Index]]", text)
        self.assertNotIn("[[Home]]", text)
        self.assertNotIn("[[Index]]", text)


if __name__ == "__main__":
    unittest.main()
