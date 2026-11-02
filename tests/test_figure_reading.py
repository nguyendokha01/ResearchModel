"""Offline tests for the source-bound figure/table reading layer."""
import json
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zlib

from kbsys import config, db, media, workflow
from kbsys import figure_reading


def tiny_png(path, byte=0):
    """Write a valid 2x2 RGB PNG without Pillow or another image dependency."""
    raw = b"".join(b"\x00" + bytes((byte, 20, 40)) * 2 for _ in range(2))

    def chunk(kind, value):
        return (struct.pack(">I", len(value)) + kind + value +
                struct.pack(">I", zlib.crc32(kind + value) & 0xFFFFFFFF))

    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)) +
            chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class FigureReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", LOGS_DIR=root / "logs", VAULTS_DIR=root / "vaults",
        )
        self.paths.start()
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.paths.stop()
        self.temp.cleanup()

    def add_paper(self, paper_id, kind="figure", section="results", object_local="F1",
                  mention_section=None, sections=None, label=None):
        folder = db.paper_dir(paper_id)
        folder.mkdir(parents=True, exist_ok=True)
        crop_relative = f"crops/media-test/{object_local}.png"
        crop = folder / crop_relative
        tiny_png(crop)
        object_id = f"{paper_id}:{object_local}"
        mention_section = section if mention_section is None else mention_section
        caption_id = f"{paper_id}:S0001"
        mention_id = f"{paper_id}:S0002"
        rows = [
            {"sentence_id": caption_id, "seq": 1, "text": f"{object_local}. The value was 3.2.",
             "section": section, "kind": "caption", "source": "main"},
            {"sentence_id": mention_id, "seq": 2, "text": f"The groups are described in {object_local}.",
             "section": mention_section, "kind": "text", "source": "main",
             "mentions": [object_id] if kind in {"figure", "table"} else []},
        ]
        if kind == "table":
            rows.append({"sentence_id": f"{paper_id}:S0003", "seq": 3,
                         "text": "Control | 8 | 3.2", "section": section, "kind": "table_row",
                         "source": "main", "object_id": object_id})
        db.write_json(folder / "metadata.json", {"paper_id": paper_id, "source_files": []})
        db.write_jsonl(folder / "sentences.jsonl", rows)
        db.write_jsonl(folder / "figures.jsonl", [] if kind != "figure" else [{
            "figure_id": object_id, "label": object_local, "source": "main", "page": 1,
            "bbox": [0, 0, 2, 2], "caption_sentence_ids": [caption_id],
            "mentioned_in": [mention_id],
        }])
        db.write_jsonl(folder / "tables.jsonl", [] if kind != "table" else [{
            "table_id": object_id, "label": object_local, "source": "main", "page": 1,
            "bbox": [0, 0, 2, 2], "caption_sentence_ids": [caption_id], "mentioned_in": [],
            "header": ["Group", "n", "value"], "rows": [["Control", "8", "3.2"]],
            "footnotes": "", "quality": "exact", "extraction": "fixture",
        }])
        db.write_jsonl(folder / "evidence.jsonl", [])
        signature, hashes, sources = media._signature(folder, {}, [], False)
        is_data_object = kind in {"figure", "table"}
        bundle = {
            "object_id": object_id, "kind": kind,
            "label": label if label is not None else object_local if is_data_object else None,
            "caption": [rows[0]] if is_data_object else [],
            "data": [rows[-1]] if kind == "table" else [],
            "mentions": [rows[1]] if is_data_object else [], "anchors": [], "unresolved_sentence_ids": [],
            "associated_units": [], "source_locator": {"status": "unavailable", "page": 1, "source": "main"},
            "crop": {"path": crop_relative, "sha256": media._hash(crop), "status": "available"},
            "table": {"header": ["Group", "n", "value"], "rows": [["Control", "8", "3.2"]],
                      "footnotes": "", "quality": "exact", "extraction": "fixture"} if kind == "table" else None,
        }
        manifest = {"version": media.VERSION, "paper_id": paper_id, "revision": "fixture",
                    "built_at": "fixture", "input_sha256": signature, "input_hashes": hashes,
                    "sources": sources, "manual_regions": [], "detect": False, "status": "partial",
                    "bundles": [bundle], "scope_note": "fixture"}
        manifest["output_sha256"] = media._digest(manifest)
        db.write_json(folder / "media_manifest.json", manifest)
        if sections is not None:
            db.write_json(folder / "sections.json", sections)
        self.con.execute("INSERT INTO paper_vaults VALUES (?, ?)", (paper_id, "vault"))
        self.con.commit()
        return folder, object_id, crop

    @staticmethod
    def reply(sentence_id="paper:S0001", note="The caption reports 3.2."):
        return {
            "panels": [{"panel": "A", "chart_kind": "bar", "groups": ["Control"],
                        "measure": "value", "unit": "unit", "values": [
                            {"group": "Control", "value": 3.2, "error": 0.1, "n": 8,
                             "read_quality": "approximate"}], "significance_marks": []}],
            "caption_consistency": [{"claim_sentence_id": sentence_id, "status": "consistent", "note": note}],
            "summary": {"text": "One legible bar.", "panel_refs": ["A"]},
        }

    def test_crop_hash_mismatch_is_refused(self):
        _, object_id, crop = self.add_paper("paper")
        crop.write_bytes(crop.read_bytes() + b"changed")
        with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
            figure_reading.task("paper", object_id)

    def test_valid_submit_is_accepted_and_stored(self):
        folder, object_id, _ = self.add_paper("paper")
        result = figure_reading.submit("paper", object_id, self.reply(), "sonnet")
        self.assertTrue(result["ok"])
        self.assertEqual(result["analyst"], "sonnet")
        self.assertTrue((folder / "figure_readings" / "F1.json").is_file())
        loaded = figure_reading.load("paper", object_id)
        self.assertEqual(loaded["status"], "current")
        self.assertEqual(loaded["panels"][0]["values"][0]["read_quality"], "approximate")
        self.assertIn("figure_read by sonnet", figure_reading.show("paper", object_id))

    def test_invented_number_is_rejected(self):
        self.add_paper("paper")
        with self.assertRaisesRegex(ValueError, "Invented number"):
            figure_reading.submit("paper", "paper:F1", self.reply(note="The caption reports 99."), "sonnet")

    def test_summary_number_must_come_from_a_source_or_a_panel(self):  # FG-05
        self.add_paper("paper")
        bad = self.reply()
        bad["summary"]["text"] = "Control rose 91 % over Tuj-1 in Figure 1."
        with self.assertRaisesRegex(ValueError, "Invented number in summary: 91"):
            figure_reading.submit("paper", "paper:F1", bad, "sonnet")
        good = self.reply()
        good["summary"]["text"] = "Control is 3.2 (n = 8), as Tuj-1 in Figure 1."  # 3.2 from the caption, 8 from a panel, the rest are labels
        self.assertTrue(figure_reading.submit("paper", "paper:F1", good, "sonnet")["ok"])

    def test_unknown_sentence_id_is_rejected(self):
        self.add_paper("paper")
        with self.assertRaisesRegex(ValueError, "Unknown sentence ID"):
            figure_reading.submit("paper", "paper:F1", self.reply("paper:S9999"), "sonnet")

    def test_reading_goes_stale_after_crop_changes(self):
        _, object_id, crop = self.add_paper("paper")
        figure_reading.submit("paper", object_id, self.reply(), "sonnet")
        crop.write_bytes(crop.read_bytes() + b"new crop revision")
        loaded = figure_reading.load("paper", object_id)
        self.assertEqual(loaded["status"], "stale")
        self.assertTrue(any("crop" in reason for reason in loaded["stale_reasons"]))

    def test_reading_goes_stale_after_sentence_changes(self):
        folder, object_id, _ = self.add_paper("paper")
        figure_reading.submit("paper", object_id, self.reply(), "sonnet")
        rows = list(db.read_jsonl(folder / "sentences.jsonl"))
        rows[0]["text"] += " Revised source wording."
        db.write_jsonl(folder / "sentences.jsonl", rows)
        loaded = figure_reading.load("paper", object_id)
        self.assertEqual(loaded["status"], "stale")
        self.assertTrue(any("source" in reason for reason in loaded["stale_reasons"]))

    def test_pending_order_results_figures_then_tables_then_other(self):
        self.add_paper("p1", "figure", "results", "F1")
        self.add_paper("p2", "table", "results", "T1")
        self.add_paper("p3", "figure", "discussion", "F2")
        rows = figure_reading.pending(self.con, "vault")
        self.assertEqual([(row["paper_id"], row["kind"]) for row in rows],
                         [("p1", "figure"), ("p2", "table"), ("p3", "figure")])

    def test_formula_anchor_is_excluded_unless_explicitly_requested(self):
        folder, _, _ = self.add_paper("formula", "formula", "references", "M03d56d5", label=None)
        bundle = db.read_json(folder / "media_manifest.json")["bundles"]
        with patch.object(figure_reading.media, "bundles", return_value=bundle):
            self.assertEqual(figure_reading.pending(self.con, "vault"), [])
            rows = figure_reading.pending(self.con, "vault", include_formulas=True)
        self.assertEqual([(row["object_id"], row["kind"]) for row in rows],
                         [("formula:M03d56d5", "formula")])

    def test_source_section_comes_from_object_mention(self):
        self.add_paper("mention", section="references", mention_section="results", object_local="F4",
                       sections=[{"name": "results", "page_start": 1},
                                 {"name": "references", "page_start": 5}])
        rows = figure_reading.pending(self.con, "vault")
        self.assertEqual(rows[0]["source_section"], "results")

    def test_supplementary_objects_follow_main_objects(self):
        self.add_paper("p1", object_local="F1", section="results")
        self.add_paper("p2", object_local="FS1", section="results")
        self.add_paper("p3", object_local="F2", section="discussion")
        rows = figure_reading.pending(self.con, "vault")
        self.assertEqual([row["object_id"] for row in rows], ["p1:F1", "p3:F2", "p2:FS1"])


class NumberTokenTests(unittest.TestCase):
    def test_integers_keep_trailing_zeros_and_thousands_join(self):
        tokens = figure_reading._number_tokens
        self.assertEqual(tokens("GO:1903900"), {("1903900", "")})
        self.assertEqual(tokens("1,903,900"), {("1903900", "")})
        self.assertNotEqual(tokens("100"), tokens("1"))
        self.assertEqual(tokens("2.50"), {("2.5", "")})


class NoteLabelTests(unittest.TestCase):
    def test_labels_are_not_claim_numbers(self):
        claims = lambda note: figure_reading._number_tokens(figure_reading._LABEL.sub(" ", note))
        self.assertEqual(claims("SSEA-3 and FGFR1 in Figure 1 and panel 2 at 2W"), set())
        self.assertEqual(claims("Figure 4H-J shows 41.67% vs 12"), {("41.67", "%"), ("12", "")})
        self.assertEqual(claims("Tuj-1 and Sox-2 cells, Panel A shows 12"), {("12", "")})  # FG-02: a mixed-case marker name is a label
        self.assertEqual(claims("Day-3, Group-91, Well-12 and Dose-50 stay claims"), {("3", ""), ("91", ""), ("12", ""), ("50", "")})
        self.assertEqual(figure_reading._NAME_NUMBER.sub(" ", "Group-91 and Tuj-1"), "Group-91 and  ")  # same words in the summary rule
        self.assertEqual(claims("Day 3 and 91 cells"), {("3", ""), ("91", "")})  # a name needs its hyphen: plain numbers stay claims
        self.assertEqual({n for n, _ in claims("3.5-fold, 10mg, 10-day, p<0.05, Figure 1, 35% up")},
                         {"3.5", "10", "0.05", "35"})


class WarningLabelTests(unittest.TestCase):
    def test_mismatch_is_labelled_for_notes_and_ai_search(self):
        reading = {"label": "Figure 3", "red_warning_candidate": True,
                   "caption_consistency": [{"claim_sentence_id": "p:S0178", "status": "inconsistent",
                                            "note": "Panel A prints N.S."},
                                           {"claim_sentence_id": "p:S0001", "status": "consistent", "note": ""}]}
        self.assertIn("Panel A prints N.S.", figure_reading.warning(reading))
        self.assertEqual(figure_reading.warning({"crop_wrong": True, "red_warning_candidate": True}), "")
        self.assertEqual(figure_reading.warning({"caption_consistency": []}), "")
        with tempfile.TemporaryDirectory() as temp, patch.object(config, "PAPERS_DIR", Path(temp)):
            db.write_json(Path(temp) / "p" / "figure_readings" / "F3.json", reading)
            flags = figure_reading.sentence_warnings("p")
            self.assertEqual(set(flags), {"p:S0178"})
            from kbsys import answer
            block = answer._item_block(1, {"paper_id": "p", "sections": ["results"], "pages": [7], "unit_type": "result",
                                           "review_paper": False, "text": "S1P decreased.", "sentence_ids": ["p:S0178"]},
                                       {"cite": "Smith 2020"})
            self.assertIn("WARNING: Figure 3:", block)


if __name__ == "__main__":
    unittest.main()
