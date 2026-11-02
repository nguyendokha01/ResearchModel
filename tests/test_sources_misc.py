"""Offline tests for non-PDF source ingestion (D3.52)."""

import hashlib
import json
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from kbsys import config, sources_misc


class SourcesMiscTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=self.root, STORE=self.root / "knowledge_store",
            PAPERS_DIR=self.root / "knowledge_store" / "papers",
            LOGS_DIR=self.root / "logs", VAULTS_DIR=self.root / "vaults",
        )
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _docx(path):
        document = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Experiment heading</w:t></w:r></w:p>
    <w:p><w:r><w:t>The embryo was vitrified. Recovery was recorded.</w:t></w:r></w:p>
    <w:tbl><w:tr><w:tc><w:p><w:r><w:t>Group</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>Value</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>Control</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>7</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    <w:p><w:r><w:t>Image below.</w:t></w:r></w:p>
    <w:sectPr/>
  </w:body>
</w:document>"""
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/media/figure.png", b"embedded image bytes")

    def test_docx_parsing_media_stable_duplicate_and_original_preserved(self):
        path = self.root / "notebook.docx"
        self._docx(path)
        before = hashlib.sha256(path.read_bytes()).hexdigest()
        imported = sources_misc.import_source(path, "experiments", "Notebook")
        self.assertFalse(imported["duplicate"])
        self.assertEqual(imported["source_kind"], "docx")
        self.assertEqual(imported["evidence_class"], "human_note")
        shown = sources_misc.show_source(imported["source_id"])
        self.assertEqual(shown["title"], "Notebook")
        self.assertTrue(any(s.get("heading_level") == 1 for s in shown["sentences"]))
        self.assertTrue(any(s["locator"]["kind"] == "table" for s in shown["sentences"]))
        self.assertEqual(shown["attachments"], ["figure.png"])
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before)

        duplicate = sources_misc.import_source(path, "second_vault")
        self.assertTrue(duplicate["duplicate"])
        self.assertEqual(duplicate["source_id"], imported["source_id"])
        self.assertEqual(sources_misc.list_sources("experiments")[0]["source_id"], imported["source_id"])
        self.assertEqual(sources_misc.list_sources("second_vault")[0]["source_id"], imported["source_id"])
        self.assertEqual(len(list((self.root / "knowledge_store" / "sources").iterdir())), 1)

    def test_note_image_statuses_and_sidecar_text(self):
        note = self.root / "lab.md"
        note.write_text("First note.\n\nSecond paragraph.", encoding="utf-8")
        imported_note = sources_misc.import_source(note, "mine")
        self.assertEqual(imported_note["source_kind"], "note")
        self.assertEqual(imported_note["ingest_status"], "ready")
        self.assertEqual(len(sources_misc.show_source(imported_note["source_id"])["sentences"]), 2)

        image = self.root / "microscope.png"
        image.write_bytes(b"PNG placeholder")
        image_import = sources_misc.import_source(image, "mine")
        self.assertEqual(image_import["ingest_status"], "needs_ocr")

        described = self.root / "described.jpg"
        described.write_bytes(b"JPEG placeholder")
        described.with_suffix(".txt").write_text("Caption text.", encoding="utf-8")
        described_import = sources_misc.import_source(described, "mine")
        self.assertEqual(described_import["ingest_status"], "ready")
        self.assertEqual(sources_misc.show_source(described_import["source_id"])["sentences"][0]["text"], "Caption text.")

    def test_media_transcript_status_timestamps_fts_and_rendered_note(self):
        media = self.root / "session.mp4"
        media.write_bytes(b"random media placeholder")
        needs_transcript = sources_misc.import_source(media, "experiments")
        self.assertEqual(needs_transcript["ingest_status"], "needs_transcript")

        transcript_media = self.root / "recording.mp4"
        transcript_media.write_bytes(b"another media placeholder")
        transcript_media.with_suffix(".vtt").write_text(
            "WEBVTT\n\n00:00:01.000 --> 00:00:03.500\nThe instrument started.\n\n"
            "00:00:04.000 --> 00:00:05.000\nSample loaded.\n", encoding="utf-8")
        imported = sources_misc.import_source(transcript_media, "experiments", "Recorded session")
        self.assertEqual(imported["ingest_status"], "ready")
        sentences = sources_misc.show_source(imported["source_id"])["sentences"]
        self.assertEqual(sentences[0]["time_start"], "00:00:01.000")
        self.assertEqual(sentences[0]["time_end"], "00:00:03.500")
        self.assertEqual(sentences[0]["locator"]["kind"], "time_range")

        con = sqlite3.connect(":memory:")
        count = sources_misc.index_sources(con)
        self.assertGreaterEqual(count, 2)
        hits = sources_misc.search_sources(con, "instrument started", "experiments")
        self.assertTrue(any("instrument started" in hit["text"] for hit in hits))
        con.close()

        rendered = sources_misc.render_note(imported["source_id"])
        self.assertIn("type: source", rendered)
        self.assertIn("source_kind: video", rendered)
        self.assertIn("kb:auto:start", rendered)
        self.assertIn("## My notes", rendered)


if __name__ == "__main__":
    unittest.main()
