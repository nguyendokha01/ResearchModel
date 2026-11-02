"""Offline tests for non-PDF retrieval and vault Source notes (D3.57)."""

import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kbsys import config, db, find, source_notes, sources_misc


class SourceSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=self.root, STORE=self.root / "knowledge_store",
            DB_PATH=self.root / "knowledge_store" / "library.db",
            PAPERS_DIR=self.root / "knowledge_store" / "papers",
            LOGS_DIR=self.root / "logs", VAULTS_DIR=self.root / "vaults",
        )
        self.patch.start()
        for vault in ("one", "two"):
            config.vault_dir(vault).mkdir(parents=True)
            (config.vault_dir(vault) / "Home.md").write_text(f"# {vault}\n", encoding="utf-8")  # a research vault, as create_vault writes
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.temp.cleanup()

    def _note(self, name, text, vault="one"):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return sources_misc.import_source(path, vault, con=self.con)

    @staticmethod
    def _docx(path):
        document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>EG was used in the experiment.</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>CPA</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/media/photo.png", b"attachment")

    def test_sources_are_scoped_and_separate_from_paper_hits(self):
        self._note("one.md", "Unique source alpha marker.", "one")
        self._note("two.md", "Unique source beta marker.", "two")

        alpha = find.search(self.con, config.load(), "alpha marker", vault="one", semantic=False)
        self.assertTrue(alpha["source_hits"])
        self.assertEqual(alpha["hits"], [])
        self.assertEqual(alpha["source_hits"][0]["vault"], "one")
        self.assertFalse(find.search(self.con, config.load(), "beta marker", vault="one", semantic=False)["source_hits"])
        self.assertEqual(find.search(self.con, config.load(), "beta marker", vault="two", semantic=False)["source_hits"][0]["kind"], "human_note")

    def test_source_hit_label_and_locator(self):
        self._note("paragraph.md", "The sample was prepared for retrieval.", "one")
        hit = find.search(self.con, config.load(), "sample prepared", vault="one", semantic=False)["source_hits"][0]
        self.assertEqual(hit["kind"], "human_note")
        self.assertIn("không phải bằng chứng đã công bố", hit["not_published_evidence"])
        self.assertEqual(hit["locator"]["kind"], "paragraph")
        self.assertEqual(hit["locator_label"], "paragraph 1")

        media = self.root / "session.mp4"
        media.write_bytes(b"media")
        media.with_suffix(".vtt").write_text(
            "WEBVTT\n\n00:00:02.000 --> 00:00:04.000\nThe instrument started.\n",
            encoding="utf-8")
        sources_misc.import_source(media, "one", con=self.con)
        transcript = find.search(self.con, config.load(), "instrument started", vault="one", semantic=False)["source_hits"][0]
        self.assertEqual(transcript["kind"], "transcript")
        self.assertIn("00:00:02.000", transcript["locator_label"])

    def test_import_and_incremental_indexing(self):
        # Import must reuse the caller's active transaction, including on the
        # duplicate-membership path. A second writer caused the reported lock.
        with patch.object(sources_misc, "_index_connection",
                          side_effect=AssertionError("opened a second writer")):
            first = self._note("first.md", "Incremental first marker.")
            sources_misc.index_sources(self.con, [first["source_id"]])
            first_rowid = self.con.execute(
                "SELECT rowid FROM source_sentences_fts WHERE source_id = ?",
                (first["source_id"],)).fetchone()[0]
            before = self.con.execute("SELECT COUNT(*) FROM source_sentences_fts").fetchone()[0]
            self.assertTrue(self.con.in_transaction)
            second = self._note("second.md", "Incremental second marker.")
            after = self.con.execute("SELECT COUNT(*) FROM source_sentences_fts").fetchone()[0]
            self.assertTrue(self.con.in_transaction)
            self.assertEqual(before, 1)
            self.assertEqual(after, 2)
            self.assertEqual(self.con.execute(
                "SELECT rowid FROM source_sentences_fts WHERE source_id = ?",
                (first["source_id"],)).fetchone()[0], first_rowid)
            sources_misc.index_sources(self.con, [second["source_id"]])
            self.assertEqual(self.con.execute(
                "SELECT COUNT(*) FROM source_sentences_fts").fetchone()[0], 2)
            duplicate = sources_misc.import_source(self.root / "second.md", "two", con=self.con)
            self.assertTrue(duplicate["duplicate"])
            self.assertTrue(sources_misc.search_sources(self.con, "second marker", "two"))
            self.assertTrue(self.con.in_transaction)
        self.assertTrue(sources_misc.search_sources(self.con, "first marker", "one"))
        self.assertTrue(sources_misc.search_sources(self.con, "second marker", "one"))

    def test_standalone_import_commits_and_closes_owned_connection(self):
        path = self.root / "standalone.md"
        path.write_text("Standalone import marker.", encoding="utf-8")
        owned_con = sqlite3.connect(config.STORE / "library.db")
        with patch.object(sources_misc, "_index_connection", return_value=owned_con) as opened:
            sources_misc.import_source(path, "one")
        opened.assert_called_once_with()
        with self.assertRaises(sqlite3.ProgrammingError):
            owned_con.execute("SELECT 1")
        self.assertTrue(sources_misc.search_sources(self.con, "standalone marker", "one"))

    def test_source_note_links_existing_nodes_preserves_human_text_and_copies_attachments(self):
        store = config.STORE
        (store).mkdir(parents=True, exist_ok=True)
        (store / "concepts.jsonl").write_text(
            json.dumps({"concept_id": "local:eg", "name": "EG", "type": "molecule", "synonyms": []}) + "\n"
            + json.dumps({"concept_id": "local:missing", "name": "Missing", "type": "concept", "synonyms": []}) + "\n",
            encoding="utf-8")
        vault = config.vault_dir("one")
        (vault / "Substances").mkdir()
        (vault / "Substances" / "EG.md").write_text("# EG\n", encoding="utf-8")
        docx = self.root / "lab.docx"
        self._docx(docx)
        imported = sources_misc.import_source(docx, "one", "Lab record", con=self.con)

        self.assertEqual(source_notes.write("one"), 1)
        note = next((vault / "Sources").glob("*.md"))
        generated = note.read_text(encoding="utf-8")
        self.assertIn("[[Substances/EG|EG]]", generated)
        self.assertNotIn("Missing", generated)
        self.assertTrue((vault / "_Attachments" / imported["source_id"] / "photo.png").is_file())

        with note.open("a", encoding="utf-8") as handle:
            handle.write("\nResearcher observation: preserve this sentence.\n")
        source_notes.write("one")
        refreshed = note.read_text(encoding="utf-8")
        self.assertIn("Researcher observation: preserve this sentence.", refreshed)
        self.assertEqual(refreshed.count(sources_misc.AUTO_START), 1)


if __name__ == "__main__":
    unittest.main()
