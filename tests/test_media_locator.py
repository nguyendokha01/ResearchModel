"""D3.70 graphic/caption locator on synthetic PDFs in isolated temporary stores."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from kbsys import config, db, media

BODY = "Figure 1 shows that the treated group differed from the control group in this body text."


def row(n, text, kind="text", page=1, **extra):
    return {"sentence_id": f"paper:S{n:04d}", "seq": n, "text": text, "source": "main", "page": page,
            "passage_id": f"paper:{n:04d}", "section": "results", "kind": kind, **extra}


def figure(n, page, caption_ids, bbox=None):
    return {"figure_id": f"paper:F{n}", "label": f"Figure {n}", "source": "main", "page": page, "bbox": bbox,
            "crop": None, "caption_sentence_ids": [f"paper:S{i:04d}" for i in caption_ids], "mentioned_in": []}


class LocatorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(config, ROOT=self.root, STORE=self.root / "store",
                                    PAPERS_DIR=self.root / "store/papers", LOGS_DIR=self.root / "logs")
        self.paths.start()
        self.folder = db.paper_dir("paper")
        self.folder.mkdir(parents=True)
        self.pdf = self.root / "original.pdf"

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def store(self, pages, rows, figures=(), tables=()):
        with pymupdf.open() as doc:
            for draw in pages:
                draw(doc.new_page(width=400, height=500))
            doc.save(self.pdf)
        db.write_json(self.folder / "metadata.json", {"paper_id": "paper", "title": "Fixture", "source_files": [
            {"role": "main", "path": "original.pdf", "status": "active", "sha256": media._hash(self.pdf)}]})
        db.write_jsonl(self.folder / "sentences.jsonl", rows)
        db.write_jsonl(self.folder / "figures.jsonl", list(figures))
        db.write_jsonl(self.folder / "tables.jsonl", list(tables))
        db.write_jsonl(self.folder / "evidence.jsonl", [])

    def bundle(self, manifest, oid):
        return next(b for b in manifest["bundles"] if b["object_id"] == oid)

    @staticmethod
    def graphic_page(label=None):
        def draw(page):
            page.draw_rect([40, 60, 360, 400], color=(0, 0, 0), fill=(0.2, 0.5, 0.9))
            if label:
                page.insert_text((40, 440), label, fontsize=11)
        return draw

    def assert_pixels(self, bundle):
        crop = pymupdf.Pixmap(str(media.crop_path(self.folder, bundle["crop"]["path"])))
        with pymupdf.open(self.pdf) as doc:
            page = doc[bundle["source_locator"]["page"] - 1]
            expected = page.get_pixmap(clip=pymupdf.Rect(bundle["source_locator"]["bbox"]), dpi=media.DPI)
        self.assertEqual(crop.samples, expected.samples)

    def test_same_page_figure_above_caption(self):
        def draw(page):
            page.draw_rect([50, 40, 350, 200], color=(0, 0, 0), fill=(1, 0.8, 0.2))
            page.insert_text((50, 225), "Figure 1. Same page caption.", fontsize=9)
            page.insert_text((50, 300), BODY[:60], fontsize=12)
        self.store([draw], [row(1, "Figure 1. Same page caption.", "caption")], [figure(1, 1, [1])])
        b = self.bundle(media.build("paper", detect=False), "paper:F1")
        loc = b["source_locator"]
        self.assertEqual((loc["region_kind"], loc["locator_basis"], loc["region_basis"]),
                         ("object_region", "same_page_caption", "automatic_locator"))
        self.assertEqual(loc["page"], 1)
        self.assertTrue(pymupdf.Rect(loc["bbox"]).contains(pymupdf.Rect(50, 40, 350, 200)))
        self.assertLess(loc["bbox"][3], 212)  # graphic only, caption below stays out
        self.assertEqual(loc["region_completeness"], "not_assessed")
        self.assertIsNone(loc["object_bbox"])  # no stored annotation was invented
        self.assertIsNone(list(db.read_jsonl(self.folder / "figures.jsonl"))[0]["bbox"])
        self.assert_pixels(b)

    def test_manuscript_legend_page_then_figure_pages(self):
        def legends(page):
            for i, y in enumerate((60, 140, 220), start=1):
                page.insert_text((40, y), f"Figure {i}. Legend title {i}.", fontsize=11)
                page.insert_text((40, y + 26), f"panel text {i}.", fontsize=11)
        rows = []
        for i in range(3):
            n = 3 * i
            rows += [row(n + 1, f"Figure {i + 1}.", "caption"), row(n + 2, f"Legend title {i + 1}.", "caption"),
                     row(n + 3, f"panel text {i + 1}.")]
        figures = [figure(i + 1, 1, [3 * i + 1, 3 * i + 2]) for i in range(3)]
        self.store([legends, self.graphic_page(), self.graphic_page("Figure 2"), self.graphic_page()], rows, figures)
        with patch.object(media, "_locate_source", return_value={}):
            before = media.build("paper", detect=False)
        self.assertEqual(self.bundle(before, "paper:F1")["source_locator"]["region_kind"], "source_page_preview")
        db.write_json(self.folder / "media_regions.json", [])  # new input: the old manifest is not reused
        after = media.build("paper", detect=False)
        got = {oid: (self.bundle(after, oid)["source_locator"]["page"], self.bundle(after, oid)["source_locator"]["locator_basis"])
               for oid in ("paper:F1", "paper:F2", "paper:F3")}
        self.assertEqual(got, {"paper:F1": (2, "manuscript_figure_page_order"),
                               "paper:F2": (3, "manuscript_figure_page_label"),
                               "paper:F3": (4, "manuscript_figure_page_order")})
        f1 = self.bundle(after, "paper:F1")
        self.assertEqual(f1["source_locator"]["caption_page"], 1)
        self.assertEqual([r["sentence_id"] for r in f1["caption"]], ["paper:S0001", "paper:S0002", "paper:S0003"])
        self.assertEqual(f1["stored_caption_sentence_ids"], ["paper:S0001", "paper:S0002"])
        self.assert_pixels(f1)
        old = self.bundle(before, "paper:F1")
        audit = f1["source_locator"]["previous"]
        self.assertEqual((audit["region_kind"], audit["page"], audit["crop_path"]),
                         ("source_page_preview", 1, old["crop"]["path"]))
        self.assertNotEqual(f1["crop"]["path"], old["crop"]["path"])
        self.assertEqual(media._hash(media.crop_path(self.folder, old["crop"]["path"])), old["crop"]["sha256"])
        report = media.relocate("paper")  # dry run: nothing changes now
        self.assertFalse(report["persist"])
        self.assertTrue(all(o["before"] == o["after"] for o in report["objects"]))

    def test_manuscript_label_contradicting_order_stays_unlocated(self):
        def legends(page):
            for i, y in enumerate((60, 140), start=1):
                page.insert_text((40, y), f"Figure {i}. Legend title {i}.", fontsize=11)
        rows = [row(1, "Figure 1. Legend title 1.", "caption"), row(2, "Figure 2. Legend title 2.", "caption")]
        # "Figure 1" printed on the second graphic page: order and label disagree for F2.
        self.store([legends, self.graphic_page(), self.graphic_page("Figure 1")], rows,
                   [figure(1, 1, [1]), figure(2, 1, [2])])
        manifest = media.build("paper", detect=False)
        f1, f2 = self.bundle(manifest, "paper:F1"), self.bundle(manifest, "paper:F2")
        self.assertEqual((f1["source_locator"]["page"], f1["source_locator"]["locator_basis"]),
                         (3, "manuscript_figure_page_label"))
        loc = f2["source_locator"]
        self.assertEqual((loc["region_kind"], loc["locator_basis"], loc["locator_note"], loc["bbox"]),
                         ("unlocated_object", "unlocated", "caption_page_text_only", None))
        self.assertEqual(f2["crop"]["status"], "unavailable")
        self.assertEqual(manifest["status"], "partial")

    def test_multiline_caption_does_not_take_following_body_paragraph(self):
        def draw(page):
            page.draw_rect([50, 40, 350, 200], color=(0, 0, 0), fill=(1, 0.8, 0.2))
            page.insert_text((50, 225), "Figure 1. Multi-line caption starts here and", fontsize=9)
            page.insert_text((50, 236), "continues on a second PDF line and", fontsize=9)
            page.insert_text((50, 247), "ends on the third line.", fontsize=9)
            page.insert_text((50, 290), BODY[:45], fontsize=12)
            page.insert_text((50, 305), BODY[45:], fontsize=12)
        rows = [row(1, "Figure 1.", "caption"), row(2, "Multi-line caption starts here and", "caption"),
                row(3, "continues on a second PDF line and ends on the third line."),
                row(4, BODY, mentions=["paper:F1"])]
        self.store([draw], rows, [figure(1, 1, [1, 2])])
        b = self.bundle(media.build("paper", detect=False, persist=False), "paper:F1")
        self.assertEqual([r["sentence_id"] for r in b["caption"]], ["paper:S0001", "paper:S0002", "paper:S0003"])
        self.assertEqual(b["caption_basis"], "pdf_caption_region")
        self.assertEqual([r["sentence_id"] for r in b["mentions"]], ["paper:S0004"])
        self.assertEqual(b["caption"][2], rows[2])  # exact copied source row

    def test_text_table_region_between_label_and_body(self):
        def draw(page):
            page.insert_text((40, 40), "Table 1. Group outcomes by treatment.", fontsize=9)
            for y, cells in ((60, ("Group", "n", "Rate")), (75, ("Control", "12", "45%")), (90, ("Treated", "11", "60%"))):
                for x, text in zip((40, 150, 250), cells):
                    page.insert_text((x, y), text, fontsize=9)
            page.insert_text((40, 140), BODY[:50], fontsize=12)
            page.insert_text((40, 156), BODY[50:], fontsize=12)
        table = {"table_id": "paper:T1", "label": "Table 1", "source": "main", "page": 1, "bbox": None, "crop": None,
                 "caption_sentence_ids": ["paper:S0001"], "mentioned_in": [], "header": [], "rows": [],
                 "footnotes": None, "quality": "uncertain", "extraction": "text_fallback"}
        self.store([draw], [row(1, "Table 1. Group outcomes by treatment.", "caption"), row(2, BODY)], tables=[table])
        b = self.bundle(media.build("paper", detect=False), "paper:T1")
        loc = b["source_locator"]
        self.assertEqual((loc["region_kind"], loc["locator_basis"]), ("object_region", "same_page_caption"))
        x0, y0, x1, y1 = loc["bbox"]
        self.assertTrue(x0 <= 40 and x1 >= 265 and 40 <= y0 <= 52 and 90 <= y1 < 120, loc["bbox"])
        self.assertEqual([r["sentence_id"] for r in b["caption"]], ["paper:S0001"])
        self.assert_pixels(b)

    def test_stored_object_region_is_kept(self):
        def draw(page):
            page.draw_rect([50, 40, 350, 200], color=(0, 0, 0), fill=(1, 0.8, 0.2))
            page.draw_rect([60, 260, 340, 420], color=(0, 0, 0), fill=(0.3, 0.3, 0.3))
            page.insert_text((50, 225), "Figure 1. Stored caption.", fontsize=9)
        stored = [50, 40, 350, 200]
        self.store([draw], [row(1, "Figure 1. Stored caption.", "caption")], [figure(1, 1, [1], bbox=stored)])
        loc = self.bundle(media.build("paper", detect=False), "paper:F1")["source_locator"]
        self.assertEqual((loc["bbox"], loc["object_bbox"], loc["region_kind"], loc["locator_basis"], loc["region_basis"]),
                         (stored, stored, "object_region", "stored_object_region", "stored_locator"))
        self.assertIsNone(loc["previous"])


if __name__ == "__main__":
    unittest.main()
