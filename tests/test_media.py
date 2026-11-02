"""Exact PDF crops and recoverable media builds in isolated temporary stores only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from kbsys import config, db, media, tier1, quality


class MediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(config, ROOT=self.root, STORE=self.root / "store",
                                    PAPERS_DIR=self.root / "store/papers", LOGS_DIR=self.root / "logs")
        self.paths.start()
        self.folder = db.paper_dir("paper")
        self.folder.mkdir(parents=True)
        self.pdf = self.root / "original.pdf"
        self.make_pdf(self.pdf)
        self.meta = {"paper_id": "paper", "title": "Fixture", "source_files": [
            {"role": "main", "path": "original.pdf", "status": "active", "sha256": media._hash(self.pdf)}]}
        self.rows = [
            {"sentence_id": "paper:S0001", "seq": 1, "text": "Groups may differ in Figure 1 [2].", "source": "main",
             "page": 1, "passage_id": "paper:0001", "section": "results", "kind": "text", "mentions": ["paper:F1"], "citations": [{"marker": "[2]", "ref_nums": [2]}]},
            {"sentence_id": "paper:S0002", "seq": 2, "text": "Figure 1. Exact image caption.", "source": "main",
             "page": 1, "passage_id": "paper:0002", "section": "results", "kind": "caption"},
            {"sentence_id": "paper:S0003", "seq": 3, "text": "Table 1. Exact table caption.", "source": "main",
             "page": 1, "passage_id": "paper:0003", "section": "results", "kind": "caption"},
            {"sentence_id": "paper:S0004", "seq": 4, "text": "Group: Control | n: 8 | value: 3.2 ± 0.1", "source": "main",
             "page": 1, "passage_id": None, "section": "results", "kind": "table_row", "object_id": "paper:T1"}]
        self.figure = {"figure_id": "paper:F1", "label": "Figure 1", "source": "main", "page": 1,
                       "bbox": [20, 20, 120, 75], "crop": None, "caption_sentence_ids": ["paper:S0002"],
                       "mentioned_in": ["paper:S0001"]}
        self.table = {"table_id": "paper:T1", "label": "Table 1", "source": "main", "page": 1,
                      "bbox": [20, 80, 180, 120], "crop": None, "caption_sentence_ids": ["paper:S0003"],
                      "mentioned_in": [], "header": ["Group", "n", "value"], "rows": [["Control", "8", "3.2 ± 0.1"]],
                      "footnotes": "*P < 0.05", "quality": "uncertain", "extraction": "word_positions"}
        db.write_json(self.folder / "metadata.json", self.meta)
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        db.write_jsonl(self.folder / "figures.jsonl", [self.figure])
        db.write_jsonl(self.folder / "tables.jsonl", [self.table])
        db.write_jsonl(self.folder / "evidence.jsonl", [
            {"unit_id": "paper:E0001", "unit_type": "result", "status": "ok", "sentence_ids": ["paper:S0001"],
             "outcome": {"source_objects": ["T1"], "value_source": "figure_only"}}])

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    @staticmethod
    def make_pdf(path, rotation=0):
        with pymupdf.open() as doc:
            page = doc.new_page(width=240, height=220)
            page.draw_rect([20, 20, 120, 75], color=(0, 0, 1), fill=(1, 0.8, 0.2))
            page.insert_text((25, 50), "Original pixels")
            page.insert_text((20, 95), "Table 1. Exact table caption.")
            page.insert_text((20, 135), "Figure 1. Exact image caption.")
            page.insert_text((20, 180), "x = 2 + 3")
            page.set_rotation(rotation)
            doc.save(path)

    def build(self, **kwargs):
        return media.build("paper", detect=False, **kwargs)

    def test_coupled_exact_caption_data_mentions_and_unit_selection(self):
        before = {name: (self.folder / name).read_bytes() for name in ("sentences.jsonl", "evidence.jsonl", "figures.jsonl", "tables.jsonl")}
        manifest = self.build()
        figure, table = manifest["bundles"]
        self.assertEqual(figure["caption"][0], self.rows[1])
        self.assertEqual(figure["mentions"][0], self.rows[0])
        self.assertEqual(table["data"][0], self.rows[3])
        self.assertEqual(table["table"]["footnotes"], self.table["footnotes"])
        self.assertEqual(table["table"]["quality"], "uncertain")
        self.assertEqual(figure["associated_units"][0]["unit_id"], "paper:E0001")
        self.assertEqual(len(media.bundles("paper", unit_ids=["paper:E0001"])), 2)
        self.assertEqual(media.bundles("paper", sentence_ids=["paper:S0003"])[0]["object_id"], "paper:T1")
        self.assertEqual(media.bundles("paper", object_ids=["F1"])[0]["caption"][0]["text"], self.rows[1]["text"])
        self.assertEqual(before, {name: (self.folder / name).read_bytes() for name in before})
        self.assertEqual(media._hash(self.pdf), self.meta["source_files"][0]["sha256"])

    def test_exact_region_pixels_and_cache_replay(self):
        first = self.build()
        figure = first["bundles"][0]
        actual = pymupdf.Pixmap(str(media.crop_path(self.folder, figure["crop"]["path"])))
        with pymupdf.open(self.pdf) as doc:
            expected = doc[0].get_pixmap(clip=pymupdf.Rect(self.figure["bbox"]), dpi=media.DPI)
        self.assertEqual((actual.width, actual.height, actual.samples), (expected.width, expected.height, expected.samples))
        with patch.object(pymupdf, "open", side_effect=AssertionError("cache should not render")):
            replay = self.build()
        self.assertTrue(replay["cache_hit"])
        self.assertEqual(replay["revision"], first["revision"])

    def test_missing_crop_and_corruption_rebuild_keep_previous_revision(self):
        first = self.build()
        old = media.crop_path(self.folder, first["bundles"][0]["crop"]["path"])
        old.unlink()
        missing = self.build()
        self.assertFalse(missing["cache_hit"])
        new = media.crop_path(self.folder, missing["bundles"][0]["crop"]["path"])
        new.write_bytes(b"corrupt derivative")
        third = self.build()
        self.assertFalse(third["cache_hit"])
        self.assertTrue(new.exists())
        self.assertEqual(new.read_bytes(), b"corrupt derivative")
        self.assertEqual(len(list((self.folder / "media_history").glob("*/media_manifest.json"))), 2)

    def test_missing_and_changed_source_retain_original_crop(self):
        first = self.build()
        old = first["bundles"][0]["crop"]
        self.pdf.rename(self.root / "archived.pdf")
        missing = self.build()
        self.assertEqual(missing["status"], "partial")
        self.assertFalse(missing["cache_hit"])
        self.assertEqual(missing["bundles"][0]["source_locator"]["status"], "unavailable")
        self.assertEqual(missing["bundles"][0]["crop"]["path"], old["path"])
        self.assertEqual(missing["bundles"][0]["crop"]["sha256"], old["sha256"])
        self.make_pdf(self.pdf, 90)
        changed = self.build()
        self.assertEqual(changed["bundles"][0]["source_locator"]["status"], "source_changed")
        self.assertEqual(changed["bundles"][0]["crop"]["path"], old["path"])
        self.assertEqual(changed["bundles"][0]["crop"]["source_sha256"], first["sources"]["main"]["sha256"])

    def test_missing_and_corrupt_manifest_invalidate(self):
        first = self.build()
        path = self.folder / "media_manifest.json"
        path.write_text("{broken", encoding="utf8")
        second = self.build()
        self.assertFalse(second["cache_hit"])
        self.assertTrue(media.crop_path(self.folder, first["bundles"][0]["crop"]["path"]).exists())
        path.unlink()
        self.assertFalse(self.build()["cache_hit"])

    def test_changed_inputs_and_version_invalidate_cache(self):
        first = self.build()
        self.rows[0]["text"] += " Exact new source context."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        second = self.build()
        self.assertFalse(second["cache_hit"])
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        with patch.object(media, "VERSION", media.VERSION + 1):
            self.assertFalse(self.build()["cache_hit"])

    def test_interrupted_publish_retains_active_manifest_and_crops(self):
        first = self.build()
        before = (self.folder / "media_manifest.json").read_bytes()
        self.rows[0]["text"] += " A new source revision."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        with patch.object(media.workflow, "atomic_json", side_effect=OSError("interrupted")):
            with self.assertRaisesRegex(OSError, "interrupted"):
                self.build()
        self.assertEqual((self.folder / "media_manifest.json").read_bytes(), before)
        self.assertTrue(media.crop_path(self.folder, first["bundles"][0]["crop"]["path"]).exists())
        self.assertFalse(self.build()["cache_hit"])

    def test_render_failure_and_midbuild_source_change_do_not_publish(self):
        first = self.build()
        before = (self.folder / "media_manifest.json").read_bytes()
        with patch.object(media, "DPI", 111), patch.object(pymupdf.Page, "get_pixmap", side_effect=RuntimeError("render failed")):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                self.build()
        self.assertEqual((self.folder / "media_manifest.json").read_bytes(), before)
        real_sources = media._sources
        count = [0]

        def source_change(meta, folder):
            count[0] += 1
            values = real_sources(meta, folder)
            if count[0] > 1:
                values["main"]["sha256"] = "changed during build"
            return values

        with patch.object(media, "DPI", 111), patch.object(media, "_sources", side_effect=source_change):
            with self.assertRaisesRegex(RuntimeError, "changed during build"):
                self.build()
        self.assertEqual((self.folder / "media_manifest.json").read_bytes(), before)
        self.assertTrue(media.crop_path(self.folder, first["bundles"][0]["crop"]["path"]).exists())

    def test_manual_formula_and_rotated_exact_source_region(self):
        self.pdf.unlink()
        self.make_pdf(self.pdf, 90)
        self.meta["source_files"][0]["sha256"] = media._hash(self.pdf)
        db.write_json(self.folder / "metadata.json", self.meta)
        region = {"kind": "formula", "source": "main", "page": 1, "bbox": [15, 165, 95, 190],
                  "sentence_ids": ["S0001"], "optional_text": "DO NOT COPY USER TRANSCRIPTION"}
        manifest = self.build(regions=[region])
        formula = manifest["bundles"][-1]
        self.assertRegex(formula["object_id"], r"^paper:M[0-9a-f]{16}$")
        self.assertEqual(formula["kind"], "formula")
        self.assertEqual(formula["source_locator"]["region_basis"], "manual_locator")
        self.assertEqual(formula["source_locator"]["rotation"], 90)
        self.assertIn("x = 2 + 3", formula["optional_text"])
        self.assertNotIn("TRANSCRIPTION", formula["optional_text"])
        actual = pymupdf.Pixmap(str(media.crop_path(self.folder, formula["crop"]["path"])))
        with pymupdf.open(self.pdf) as doc:
            expected = doc[0].get_pixmap(clip=pymupdf.Rect(region["bbox"]) * doc[0].rotation_matrix, dpi=media.DPI)
        self.assertEqual(actual.samples, expected.samples)
        self.assertTrue(self.build()["cache_hit"])

    def test_automatic_formula_candidates_are_not_transcribed_or_complete_claims(self):
        manifest = media.build("paper")
        formula = [b for b in manifest["bundles"] if b["kind"] == "formula"]
        self.assertTrue(formula)
        self.assertEqual(formula[-1]["optional_text"], "x = 2 + 3")
        self.assertEqual(formula[-1]["detection"], "automatic_candidate")
        self.assertEqual(formula[-1]["text_status"], "unverified_extracted")
        self.assertIn("not complete", manifest["scope_note"])

    def test_missing_bbox_exact_full_page_preview_and_manual_refinement(self):
        # The D3.70 locator would find the fixture rectangle; this test covers the fallback.
        unlocated = patch.object(media, "_locate_source", return_value={})
        unlocated.start()
        self.addCleanup(unlocated.stop)
        self.figure["bbox"] = None
        db.write_jsonl(self.folder / "figures.jsonl", [self.figure])
        first = self.build()
        figure = first["bundles"][0]
        locator = figure["source_locator"]
        self.assertEqual(locator["region_kind"], "source_page_preview")
        self.assertEqual(locator["object_region_status"], "not_located")
        self.assertEqual(first["status"], "partial")
        self.assertTrue(self.build()["cache_hit"])
        self.assertIsNone(locator["object_bbox"])
        self.assertEqual(locator["bbox"], [0, 0, 240.0, 220.0])
        actual = pymupdf.Pixmap(str(media.crop_path(self.folder, figure["crop"]["path"])))
        with pymupdf.open(self.pdf) as doc:
            expected = doc[0].get_pixmap(dpi=media.DPI)
        self.assertEqual(actual.samples, expected.samples)
        refined = self.build(regions=[{"kind": "figure", "object_id": "paper:F1", "source": "main",
                                       "page": 1, "bbox": [20, 20, 120, 75]}])["bundles"][0]
        self.assertEqual(refined["source_locator"]["region_kind"], "manual_object_region")
        self.assertIsNone(refined["source_locator"]["object_bbox"])
        self.assertEqual(refined["source_locator"]["region_basis"], "manual_locator")
        self.assertEqual(refined["source_locator"]["region_completeness"], "not_assessed")
        self.assertEqual(refined["caption"], figure["caption"])
        self.assertIsNone(list(db.read_jsonl(self.folder / "figures.jsonl"))[0]["bbox"])
        self.assertTrue(media.crop_path(self.folder, figure["crop"]["path"]).exists())

    def test_special_symbol_manual_crop_without_reconstructed_text(self):
        manifest = self.build(regions=[{"kind": "special_symbol_region", "source": "main", "page": 1,
                                        "bbox": [15, 165, 95, 190], "optional_text": "invented repaired formula"}])
        symbol = manifest["bundles"][-1]
        self.assertEqual(symbol["kind"], "special_symbol_region")
        self.assertNotIn("invented", symbol["optional_text"])
        self.assertEqual(symbol["crop"]["status"], "available")

    def test_manual_figure_bbox_provenance_retains_stored_object_region(self):
        original_bbox = list(self.figure["bbox"])
        locator = {"kind": "figure", "object_id": "paper:F1", "source": "main", "page": 1,
                   "bbox": [10, 10, 180, 160]}
        manual = self.build(regions=[locator])["bundles"][0]
        self.assertEqual(manual["source_locator"]["object_bbox"], original_bbox)
        self.assertEqual(manual["source_locator"]["bbox"], locator["bbox"])
        self.assertEqual(manual["source_locator"]["region_basis"], "manual_locator")
        self.assertEqual(manual["source_locator"]["region_completeness"], "not_assessed")
        self.assertEqual(list(db.read_jsonl(self.folder / "figures.jsonl"))[0]["bbox"], original_bbox)

    def test_verified_ordinary_fonts_are_not_all_formula_candidates(self):
        def text_for(font):
            return {"blocks": [{"lines": [{"bbox": [10, 10, 180, 30],
                                             "spans": [{"text": "Ordinary source line", "font": font}]}]}]}
        with pymupdf.open(self.pdf) as doc:
            with patch.object(pymupdf.Page, "get_text", return_value=text_for("AdvPTimesI")):
                self.assertEqual(list(media._candidate_regions(doc, "main")), [])
            with patch.object(pymupdf.Page, "get_text", return_value=text_for("AdvPUnknown")):
                regions = list(media._candidate_regions(doc, "main"))
                self.assertEqual(regions[0]["kind"], "special_symbol_region")

    def test_unsafe_paths_locators_and_unknown_ids_rejected_before_publish(self):
        first = self.build()
        before = (self.folder / "media_manifest.json").read_bytes()
        for invalid in ("../escape.png", "crops/../escape.png", "C:/secret.png", "crops/a\\b.png", "crops/x:stream"):
            with self.subTest(path=invalid), self.assertRaises(ValueError):
                media.crop_path(self.folder, invalid)
        for field, value in (("page", 0), ("page", True), ("page", 9), ("bbox", [-1, 0, 20, 20]),
                             ("bbox", [10, 10, 5, 20]), ("bbox", [0, 0, 900, 900]),
                             ("bbox", [0, 0, float("nan"), 20]), ("source", "other:X1"),
                             ("sentence_ids", ["other:S1"]), ("source_sha256", "wrong")):
            region = {"kind": "special_symbol_region", "source": "main", "page": 1, "bbox": [1, 1, 20, 20], field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.build(regions=[region])
        with self.assertRaises(ValueError):
            media.build("../other")
        self.assertEqual((self.folder / "media_manifest.json").read_bytes(), before)
        self.assertTrue(media.crop_path(self.folder, first["bundles"][0]["crop"]["path"]).exists())

    def test_read_only_missing_crop_reports_without_writes(self):
        first = self.build()
        media.crop_path(self.folder, first["bundles"][0]["crop"]["path"]).unlink()
        before = {p.relative_to(self.folder): p.read_bytes() for p in self.folder.rglob("*") if p.is_file()}
        read = media.bundles("paper", object_ids=["F1"])[0]
        self.assertEqual(read["crop"]["status"], "missing")
        self.assertEqual(before, {p.relative_to(self.folder): p.read_bytes() for p in self.folder.rglob("*") if p.is_file()})

    def test_unresolved_mentioned_and_unit_object_placeholder_preserves_context(self):
        self.rows[0]["mentions"].append("paper:T3")
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        manifest = self.build()
        placeholder = next(b for b in manifest["bundles"] if b["object_id"] == "paper:T3")
        self.assertEqual(placeholder["source_locator"]["status"], "missing_object")
        self.assertIsNone(placeholder["source_locator"]["page"])
        self.assertEqual(placeholder["mentions"], [self.rows[0]])
        self.assertEqual(placeholder["crop"]["status"], "unavailable")
        self.assertEqual(manifest["status"], "partial")
        with patch.object(pymupdf, "open", side_effect=AssertionError("unchanged partial inventory must not re-render")):
            replay = self.build()
            selected = media.bundles("paper", object_ids=["paper:T3"])
        self.assertTrue(replay["cache_hit"])
        self.assertEqual(replay["status"], "partial")
        self.assertEqual(selected[0]["source_locator"]["status"], "missing_object")

    def test_supplement_source_coupling_and_missing_supplement_preservation(self):
        supplement = self.root / "supplement.pdf"
        self.make_pdf(supplement)
        self.meta["source_files"].append({"role": "supplement", "supp_id": "paper:X1", "path": "supplement.pdf",
                                          "sha256": media._hash(supplement)})
        db.write_json(self.folder / "metadata.json", self.meta)
        extra = {**self.rows[1], "sentence_id": "paper:S0005", "source": "paper:X1", "text": "Exact supplement caption."}
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows + [extra])
        suppfig = {**self.figure, "figure_id": "paper:FS1", "source": "paper:X1", "caption_sentence_ids": ["paper:S0005"]}
        db.write_jsonl(self.folder / "figures.jsonl", [self.figure, suppfig])
        first = self.build()
        old = next(b for b in first["bundles"] if b["object_id"] == "paper:FS1")
        self.assertEqual(old["caption"], [extra])
        supplement.unlink()
        second = self.build()
        kept = next(b for b in second["bundles"] if b["object_id"] == "paper:FS1")
        self.assertEqual(kept["source_locator"]["status"], "unavailable")
        self.assertEqual(kept["crop"]["sha256"], old["crop"]["sha256"])
        self.assertEqual(kept["caption"], [extra])

    def tier1_fixture(self, missing_supp=False):
        # Real sentence/caption linking; mock catalogue updates (outside this media task).
        self.rows = self.rows[:2]
        self.rows[1]["text"] = "Figure 1 shows exact image caption."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        passages = [{"passage_id": row["passage_id"], "seq": row["seq"], "text": row["text"], "source": "main",
                     "section": "results", "page": 1, "kind": row["kind"]} for row in self.rows]
        db.write_jsonl(self.folder / "passages.jsonl", passages)
        db.write_jsonl(self.folder / "pages.jsonl", [{"page": 1, "text": "Stored source text."}])
        db.write_jsonl(self.folder / "tables.jsonl", [])
        crop = self.folder / "crops/F1.jpg"
        crop.parent.mkdir()
        crop.write_bytes(b"recoverable old crop")
        self.figure["crop"] = "crops/F1.jpg"
        figures = [self.figure]
        if missing_supp:
            self.meta["supplements"] = [{"supp_id": "paper:X1", "kind": "pdf", "status": "extracted"}]
            self.meta["source_files"].append({"role": "supplement", "supp_id": "paper:X1", "path": "missing-supp.pdf", "sha256": "missing"})
            figures.append({**self.figure, "figure_id": "paper:FS1", "source": "paper:X1", "caption_sentence_ids": [], "mentioned_in": []})
        db.write_json(self.folder / "metadata.json", self.meta)
        db.write_jsonl(self.folder / "figures.jsonl", figures)
        return crop

    def run_tier1(self):
        with patch.object(tier1.references, "resolve"), patch.object(tier1.references, "link_back", return_value=0), \
                patch.object(tier1.db, "load_paper"), patch.object(tier1.concepts, "relink_paper"), \
                patch.object(quality, "inspect", return_value={}):
            return tier1.build(None, "paper")

    def test_tier1_missing_original_preserves_records_crops_and_archives(self):
        crop = self.tier1_fixture()
        self.pdf.unlink()
        before = (self.folder / "figures.jsonl").read_bytes()
        result = self.run_tier1()
        self.assertEqual(result["figures"], 1)
        self.assertEqual(result["retained_object_sources"], ["main"])
        self.assertEqual(crop.read_bytes(), b"recoverable old crop")
        self.assertEqual(list(db.read_jsonl(self.folder / "figures.jsonl"))[0]["caption_sentence_ids"], ["paper:S0002"])
        histories = list((self.folder / "tier1_history").glob("*/figures.jsonl"))
        self.assertEqual(histories[0].read_bytes(), before)

    def test_tier1_missing_supplement_keeps_source_local_object_and_crop(self):
        crop = self.tier1_fixture(missing_supp=True)
        result = self.run_tier1()
        self.assertEqual(result["retained_object_sources"], ["paper:X1"])
        figures = list(db.read_jsonl(self.folder / "figures.jsonl"))
        self.assertIn("paper:FS1", [f["figure_id"] for f in figures])
        retained = next(f for f in figures if f["figure_id"] == "paper:FS1")
        self.assertEqual(retained["crop"], "crops/F1.jpg")
        self.assertEqual(crop.read_bytes(), b"recoverable old crop")

    def test_tier1_render_interruption_does_not_erase_current_files(self):
        crop = self.tier1_fixture()
        before = (self.folder / "figures.jsonl").read_bytes()
        with patch.object(tier1.objects, "build", side_effect=RuntimeError("broken PDF rendering")):
            with self.assertRaisesRegex(RuntimeError, "broken PDF rendering"):
                self.run_tier1()
        self.assertEqual((self.folder / "figures.jsonl").read_bytes(), before)
        self.assertEqual(crop.read_bytes(), b"recoverable old crop")


def sentence(n, text, kind="text", **extra):
    return {"sentence_id": f"paper:S{n:04d}", "seq": n, "text": text, "source": "main",
            "page": 1, "passage_id": f"paper:{n:04d}", "section": "results", "kind": kind, **extra}


class FigureCropExpansionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(config, ROOT=self.root, STORE=self.root / "store",
                                    PAPERS_DIR=self.root / "store/papers", LOGS_DIR=self.root / "logs")
        self.paths.start()
        self.folder = db.paper_dir("paper")
        self.folder.mkdir(parents=True)
        self.pdf = self.root / "original.pdf"
        self._write_pdf(with_labels=True)
        self._write_records()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def _write_pdf(self, with_labels):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 40), False)
        pix.clear_with(0xD9E8FF)
        with pymupdf.open() as doc:
            page = doc.new_page(width=500, height=500)
            page.insert_image([80, 120, 180, 220], stream=pix.tobytes("png"))
            page.insert_image([220, 120, 320, 220], stream=pix.tobytes("png"))
            if with_labels:
                page.insert_text((72, 116), "a", fontsize=11)
                page.draw_line((110, 228), (290, 228), color=(0, 0, 0), width=1)
                page.insert_text((150, 245), "Class 2", fontsize=10)
                page.insert_text((322, 170), "SHL 1.5 B-DNA helix", fontsize=9)
            page.insert_text((70, 280), "Figure 1. Synthetic caption.", fontsize=10)
            page.insert_textbox(
                [40, 325, 460, 390],
                "This is body text below the figure caption and it must not be included in the crop. "
                "It is deliberately a wide multi-line paragraph.", fontsize=11)
            doc.save(self.pdf)

    def _write_records(self):
        db.write_json(self.folder / "metadata.json", {
            "paper_id": "paper", "title": "Fixture", "source_files": [
                {"role": "main", "path": "original.pdf", "status": "active", "sha256": media._hash(self.pdf)}]})
        db.write_jsonl(self.folder / "sentences.jsonl", [
            sentence(1, "Figure 1. Synthetic caption.", "caption"),
            sentence(2, "The result is shown in Figure 1.", mentions=["paper:F1"])])
        db.write_jsonl(self.folder / "figures.jsonl", [{
            "figure_id": "paper:F1", "label": "Figure 1", "source": "main", "page": 1,
            "bbox": [80, 120, 320, 220], "crop": None, "caption_sentence_ids": ["paper:S0001"],
            "mentioned_in": ["paper:S0002"]}])
        db.write_jsonl(self.folder / "tables.jsonl", [])
        db.write_jsonl(self.folder / "evidence.jsonl", [])

    def _bundle(self, manifest):
        return next(b for b in manifest["bundles"] if b["object_id"] == "paper:F1")

    def test_labels_and_vector_are_included_but_caption_and_body_are_not(self):
        bundle = self._bundle(media.build("paper", detect=False))
        loc = bundle["source_locator"]
        self.assertEqual(loc["locator_basis"], "stored_object_region")
        self.assertIn("drawings", loc["expanded_with"])
        self.assertIn("labels", loc["expanded_with"])
        self.assertLess(loc["bbox"][3], 270)  # caption starts around y=270
        with pymupdf.open(self.pdf) as doc:
            visible = doc[0].get_text("text", clip=pymupdf.Rect(loc["bbox"]))
        self.assertIn("a", visible)
        self.assertIn("Class 2", visible)
        self.assertIn("B-DNA helix", visible)  # label sticking out past the image edge stays whole
        self.assertNotIn("Synthetic caption", visible)
        self.assertNotIn("body text below", visible)

    def test_unrelated_rebuild_keeps_old_crop_hash(self):
        self._write_pdf(with_labels=False)
        self._write_records()
        first = self._bundle(media.build("paper", detect=False))
        old_crop = dict(first["crop"])
        rebuilt = self._bundle(media.build("paper", detect=True))
        self.assertEqual(rebuilt["crop"]["path"], old_crop["path"])
        self.assertEqual(rebuilt["crop"]["sha256"], old_crop["sha256"])
        self.assertEqual(rebuilt["source_locator"]["bbox"], first["source_locator"]["bbox"])
        self.assertNotIn("paper:F1", rebuilt.get("changed_objects", []))

    def test_relocate_reports_label_recovery_as_changed_object(self):
        self._write_pdf(with_labels=False)
        self._write_records()
        first = self._bundle(media.build("paper", detect=False))
        self._write_pdf(with_labels=True)
        self._write_records()
        report = media.relocate("paper", persist=False)
        self.assertIn("paper:F1", report["changed_objects"])
        row = next(item for item in report["objects"] if item["object_id"] == "paper:F1")
        self.assertTrue(row["changed"])
        self.assertTrue(row["crop_changed"])
        self.assertEqual(row["crop_status"], "not_built")
        self.assertNotEqual(row["before"]["bbox"], row["after"]["bbox"])
        self.assertTrue(first["crop"]["sha256"])
        applied = media.relocate("paper", persist=True)
        self.assertIn("paper:F1", applied["changed_objects"])
        updated = self._bundle(media.build("paper", detect=False))
        self.assertNotEqual(updated["crop"]["sha256"], first["crop"]["sha256"])


class RunningTextRuleTests(unittest.TestCase):
    def test_wide_block_of_short_labels_is_not_running_text(self):
        column = pymupdf.Rect(110, 0, 466, 700)
        labels = [{"bbox": (116, 588, 150, 596)}, {"bbox": (160, 598, 166, 606)},
                  {"bbox": (300, 598, 306, 606)}, {"bbox": (340, 600, 396, 609)}]
        self.assertFalse(media._is_running_text(labels, column))
        prose = [{"bbox": (110, 100, 460, 110)}, {"bbox": (110, 112, 455, 122)}]
        self.assertTrue(media._is_running_text(prose, column))


if __name__ == "__main__":
    unittest.main()
