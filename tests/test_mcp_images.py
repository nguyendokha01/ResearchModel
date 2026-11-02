"""U7 / NW-12: get_figure and get_pdf_page return an MCP image block with caption, page and links (temp store only)."""
import base64
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf

from . import test_core as fixtures, test_figure_reading   # the module, so its own tests are not collected twice
from kbsys import config, db, figure_reading, image_tools, mcp_server, neural


class McpImageTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown
    add_paper = test_figure_reading.FigureReadingTests.add_paper
    reply = test_figure_reading.FigureReadingTests.__dict__["reply"]

    def call(self, tool, **args):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        reply = mcp_server.handle(self.con, config.load(), {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": tool, "arguments": args}})
        if "error" in reply:
            return True, [{"type": "text", "text": reply["error"]["message"]}]
        return reply["result"]["isError"], reply["result"]["content"]

    def figure(self, **extra):
        self.add_paper(self.pid)
        return self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1", **extra)

    def source(self, pages=2, name="main.pdf", write=True, role="main"):
        path = config.ROOT / "sources" / name
        if write:
            path.parent.mkdir(exist_ok=True)
            doc = pymupdf.open()
            for n in range(pages):
                page = doc.new_page()
                for line in range(60):
                    page.insert_text((40, 40 + line * 11), f"Page {n + 1} line {line} value {line * 7.31:.2f} ±0.{line}", fontsize=8)
            doc.save(path)
            doc.close()
        self.con.execute("INSERT INTO source_files VALUES (?,?,?,?,?,?,?,?,?)",
                         (hashlib.sha256(name.encode()).hexdigest(), self.pid, f"sources/{name}", name, 1, "active", role, "b", "t"))
        self.con.commit()
        return path

    def pdf(self, **args):
        return self.call("get_pdf_page", vault="one", paper_id=self.pid, **args)

    # ---- get_figure
    def test_get_figure_returns_text_and_image_with_caption_page_links_and_reading(self):
        note = config.vault_dir("one") / "Papers" / "P.md"
        note.parent.mkdir()
        note.write_text(f"---\npaper_id: {self.pid}\n---\nnote\n", encoding="utf-8")
        neural.refresh("one", force=True)
        self.add_paper(self.pid)
        figure_reading.submit(self.pid, "F1", self.reply(sentence_id=f"{self.pid}:S0001"), "sonnet")
        for object_id in ("F1", f"{self.pid}:F1"):
            error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id=object_id)
            self.assertFalse(error)
            self.assertEqual([c["type"] for c in content], ["text", "image"])
            self.assertTrue(base64.b64decode(content[1]["data"]).startswith(b"\x89PNG"))
            self.assertNotIn(content[1]["data"], content[0]["text"])
            info = json.loads(content[0]["text"])
            self.assertNotIn("image", info)
            self.assertEqual((info["label"], info["kind"], info["page"]), ("F1", "figure", 1))
            self.assertEqual(info["caption"], [{"sentence_id": f"{self.pid}:S0001", "text": "F1. The value was 3.2."}])
            self.assertEqual((info["note_path"], info["obsidian_url"]), ("Papers/P.md", "obsidian://open?vault=one&file=Papers%2FP.md"))
            self.assertEqual((info["figure_reading"]["status"], info["figure_reading"]["stale"]), ("current", False))
            self.assertEqual(info["figure_reading"]["summary"]["text"], "One legible bar.")
            self.assertFalse(info["reading_inconsistent"])
            self.assertIn("not evidence unless they occur in the caption, a table row or a stored figure reading", info["pixel_numbers"])

    def test_get_figure_without_a_stored_reading_or_note_says_so(self):
        error, content = self.figure()
        info = json.loads(content[0]["text"])
        self.assertFalse(error)
        self.assertIsNone(info["figure_reading"])
        self.assertEqual(info["note_path"], None)
        self.assertIn("note_missing", info)

    def test_missing_tampered_or_vanished_crop_gives_text_only_with_a_reason(self):
        self.add_paper(self.pid)
        folder = db.paper_dir(self.pid)
        crop = next((folder / "crops").rglob("F1.png"))
        crop.write_bytes(crop.read_bytes() + b"changed")                      # hash no longer matches
        error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("SHA256 mismatch", json.loads(content[0]["text"])["image_note"])
        crop.unlink()                                                          # file gone
        error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertTrue(json.loads(content[0]["text"])["image_note"])

    def test_a_stored_reading_of_a_changed_crop_is_labelled_stale_and_no_image_is_sent(self):   # review: test gap (NW-12)
        self.add_paper(self.pid)
        figure_reading.submit(self.pid, "F1", self.reply(sentence_id=f"{self.pid}:S0001"), "sonnet")
        crop = next((db.paper_dir(self.pid) / "crops").rglob("F1.png"))
        crop.write_bytes(crop.read_bytes() + b"changed")
        error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        reading = json.loads(content[0]["text"])["figure_reading"]
        self.assertEqual((error, [c["type"] for c in content], reading["stale"], reading["status"]), (False, ["text"], True, "stale"))
        self.assertTrue(reading["stale_reasons"])

    def test_get_figure_writes_nothing_even_through_the_stale_manifest_fallback(self):   # review: test gap
        self.add_paper(self.pid)
        manifest = db.paper_dir(self.pid) / "media_manifest.json"
        snap = lambda: sorted((str(p), p.stat().st_mtime_ns, p.stat().st_size) for p in config.ROOT.rglob("*") if p.is_file())
        for stale in (False, True):
            if stale:
                text = manifest.read_text(encoding="utf-8")
                self.assertIn('"status": "partial"', text)
                manifest.write_text(text.replace('"status": "partial"', '"status": "pending"', 1), encoding="utf-8")
            before = snap()
            self.assertFalse(self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")[0])
            self.assertEqual(before, snap(), stale)

    def test_a_jpeg_crop_is_sent_as_image_jpeg_not_png(self):   # review 2026-10-25: bytes were always labelled image/png
        self.add_paper(self.pid)
        jpg = db.paper_dir(self.pid) / "crops" / "old.jpg"
        jpg.write_bytes(pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 4)).tobytes("jpg"))
        sha = hashlib.sha256(jpg.read_bytes()).hexdigest()
        with patch.object(figure_reading, "_verified_crop", lambda paper_id, bundle: (jpg, sha)):
            error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, content[1]["mimeType"]), (False, "image/jpeg"))

    def test_a_crop_deleted_after_verification_gives_text_only(self):
        self.add_paper(self.pid)
        real = figure_reading._verified_crop

        def verify_then_delete(paper_id, bundle):
            path, sha = real(paper_id, bundle)
            path.unlink()
            return path, sha
        with patch.object(figure_reading, "_verified_crop", verify_then_delete):
            error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("no image", json.loads(content[0]["text"])["image_note"])

    def test_a_crop_over_the_cap_is_not_returned_and_the_text_says_so(self):
        self.add_paper(self.pid)
        with patch.object(image_tools, "max_image_bytes", return_value=10):
            error, content = self.call("get_figure", vault="one", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("larger than", json.loads(content[0]["text"])["image_note"])

    def test_get_figure_refuses_what_is_not_this_papers_object(self):
        self.add_paper(self.pid)
        for bad in ("../x", "a/b", "a\\b", "..", "", "  ", 5, True, ["F1"], "otherpaper:F1", "F9", f"{self.pid}:F9"):
            error, _ = self.call("get_figure", vault="one", paper_id=self.pid, object_id=bad)
            self.assertTrue(error, bad)
        self.assertTrue(self.call("get_figure", vault="two", paper_id=self.pid, object_id="F1")[0])   # paper is not in that vault
        self.assertTrue(self.call("get_figure", vault="nope", paper_id=self.pid, object_id="F1")[0])
        self.assertTrue(self.call("get_figure", paper_id=self.pid, object_id="F1")[0])                  # vault is required

    def test_get_figure_task_still_hands_over_its_crop_and_keeps_the_path_in_the_text(self):
        self.add_paper(self.pid)
        error, content = self.call("get_figure_task", paper_id=self.pid, object_id="F1")
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text", "image"]))
        self.assertIn("crop_path", json.loads(content[0]["text"]))

    # ---- get_pdf_page
    def test_get_pdf_page_renders_one_page_without_writing_anything(self):
        path = self.source()
        before = (hashlib.sha256(path.read_bytes()).hexdigest(), sorted(p for p in config.ROOT.rglob("*") if p.is_file()))
        error, content = self.pdf(page=2)
        self.assertFalse(error)
        self.assertEqual([c["type"] for c in content], ["text", "image"])
        self.assertTrue(base64.b64decode(content[1]["data"]).startswith(b"\x89PNG"))
        info = json.loads(content[0]["text"])
        self.assertEqual((info["page"], info["page_count"], info["dpi"]), (2, 2, image_tools.DPI_DEFAULT))
        self.assertIn("note_missing", info)
        self.assertIn("NW-12", info["pixel_numbers"])
        self.assertNotIn("image", info)
        self.assertEqual(before, (hashlib.sha256(path.read_bytes()).hexdigest(), sorted(p for p in config.ROOT.rglob("*") if p.is_file())))
        self.assertTrue(self.pdf(page=1, dpi=50)[1][1]["data"] and self.pdf(page=1, dpi=200)[1][1]["data"])

    def test_pdf_page_over_the_cap_falls_back_to_a_lower_dpi_then_says_too_large(self):
        self.source(pages=1)
        with pymupdf.open(config.ROOT / "sources" / "main.pdf") as doc:
            big, small = (len(doc[0].get_pixmap(dpi=d).tobytes("png")) for d in (110, 50))
        self.assertGreater(big, small)
        with patch.object(image_tools, "max_image_bytes", return_value=(big + small) // 2):
            error, content = self.pdf(page=1)
        info = json.loads(content[0]["text"])
        self.assertEqual([c["type"] for c in content], ["text", "image"])
        self.assertTrue(image_tools.DPI_MIN <= info["dpi"] < image_tools.DPI_DEFAULT)
        self.assertLessEqual(len(base64.b64decode(content[1]["data"])), (big + small) // 2)
        with patch.object(image_tools, "max_image_bytes", return_value=1):
            error, content = self.pdf(page=1)
        info = json.loads(content[0]["text"])
        self.assertEqual((error, [c["type"] for c in content], info["dpi"]), (False, ["text"], image_tools.DPI_MIN))
        self.assertIn("too large", info["image_note"])
        self.assertEqual(info["page"], 1)

    def test_a_poster_size_page_is_refused_before_any_pixmap_is_built(self):   # RAM guard (8 GB machine)
        self.assertTrue(str(config.STORE).startswith(tempfile.gettempdir()))
        path = config.ROOT / "sources" / "main.pdf"
        path.parent.mkdir(exist_ok=True)
        with pymupdf.open() as doc:
            doc.new_page(width=200 * 72, height=200 * 72)   # a 200-inch poster: ~1.4 GB raw at 110 dpi
            doc.save(path)
        self.source(write=False)
        with patch.object(pymupdf.Page, "get_pixmap", side_effect=AssertionError("pixmap built")):
            error, content = self.pdf(page=1)
        info = json.loads(content[0]["text"])
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("too large: page 1 measures 200 x 200 inches", info["image_note"])
        self.source(pages=1, name="a4.pdf")   # an ordinary page still renders at the asked dpi
        self.con.execute("DELETE FROM source_files WHERE path = 'sources/main.pdf'")
        self.con.commit()
        error, content = self.pdf(page=1, dpi=200)
        self.assertEqual((error, json.loads(content[0]["text"])["dpi"]), (False, 200))

    def test_pdf_page_cap_comes_from_nw12(self):
        self.assertEqual(image_tools.max_image_bytes(), 4_000_000)
        with patch("kbsys.standard.param", return_value=0.000001):
            self.assertEqual(mcp_server._image_content(b"abc"), [])
        self.assertEqual(mcp_server._image_content(b"abc")[0]["mimeType"], "image/png")

    def test_pdf_page_out_of_range_is_a_tool_error_naming_the_page_count(self):
        self.source()
        error, content = self.pdf(page=3)
        self.assertTrue(error)
        self.assertIn("has 2 pages", content[0]["text"])

    def test_pdf_page_with_no_source_file_says_unavailable_not_error(self):
        error, content = self.pdf(page=1)                                     # no source_files row at all
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("source file unavailable", json.loads(content[0]["text"])["image_note"])
        self.source(write=False)                                              # row whose file is not on this machine
        self.assertIn("source file unavailable", json.loads(self.pdf(page=1)[1][0]["text"])["image_note"])

    def test_pdf_page_unreadable_sources_are_reported_not_crashed(self):
        path = self.source(write=False)
        path.parent.mkdir()
        path.write_bytes(b"not a pdf at all")
        error, content = self.pdf(page=1)
        self.assertEqual((error, [c["type"] for c in content]), (False, ["text"]))
        self.assertIn("cannot be opened", json.loads(content[0]["text"])["image_note"])
        doc = pymupdf.open()
        doc.new_page()
        doc.save(path, encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="u", owner_pw="o")
        doc.close()
        self.assertIn("password-protected", json.loads(self.pdf(page=1)[1][0]["text"])["image_note"])

    def test_pdf_page_arguments_are_checked(self):
        self.source()
        for bad in (0, -1, True, "1", 1.5, None, [1]):
            self.assertTrue(self.pdf(page=bad)[0], bad)
        for bad in (True, "110", 49, 201, 110.0, [110]):
            self.assertTrue(self.pdf(page=1, dpi=bad)[0], bad)
        self.assertTrue(self.call("get_pdf_page", paper_id=self.pid, page=1)[0])                  # vault required
        self.assertTrue(self.call("get_pdf_page", vault="one", page=1)[0])                        # paper_id required
        self.assertTrue(self.call("get_pdf_page", vault="two", paper_id=self.pid, page=1)[0])     # paper not in that vault
        self.assertTrue(self.call("get_pdf_page", vault="one", paper_id="../x", page=1)[0])
        self.assertFalse(self.pdf(page=1)[0])

    def test_both_tools_are_listed_read_only_with_required_vault(self):
        tools = {t["name"]: t for t in mcp_server.TOOLS}
        for name, required in (("get_figure", ["vault", "paper_id", "object_id"]), ("get_pdf_page", ["vault", "paper_id", "page"])):
            self.assertEqual(tools[name]["inputSchema"]["required"], required)
            self.assertIn("Read-only", tools[name]["description"])
            self.assertIn("NW-12", tools[name]["description"])
        self.assertIn("get_figure", (Path(config.__file__).parents[2] / "config" / "answer_protocol.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
