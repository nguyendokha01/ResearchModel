"""G6 unit 4: kb.py export-docx writes a vault note as a .docx (zipfile + WordprocessingML), temp vault only."""
import re
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

import pymupdf

from . import test_core as fixtures
from kbsys import config, docx_export

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

NOTE = """---
type: concept
---
# Vitrification µ ±
<!-- kb:auto start -->
Hello **bold** and *italic* text, [[Other note|alias]] and [site](http://x.org) and [[Plain target]].
<!-- kb:auto end -->

## Danh sách
- first item
1. numbered item
> [!quote] Quote title
> The cells survived (test2026_example_abcd:S0001), p<0.05 *P<0.05, **P<0.01.

| Name | Link |
|---|---|
| a | [[Note\\|shown]] |

![[fig1.png|300]]
> Figure 1. Greek α β γ and tiếng Việt có dấu.
![[missing.png]]
"""


class DocxExportTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def make(self):
        root = config.vault_dir("one")
        (root / "Concepts").mkdir()
        (root / "Concepts" / "Vitrification.md").write_text(NOTE, encoding="utf-8")
        (root / "_Attachments" / "p1").mkdir(parents=True)
        pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 40, 20), False)
        pix.clear_with(200)
        pix.save(str(root / "_Attachments" / "p1" / "fig1.png"))

    def export(self, note="Concepts/Vitrification.md", **kw):
        out, images = docx_export.export("one", note, **kw)
        with zipfile.ZipFile(out) as z:
            names = z.namelist()
            xml = z.read("word/document.xml").decode("utf-8")
            rels = z.read("word/_rels/document.xml.rels").decode("utf-8")
        return out, images, names, xml, rels

    def test_document_parts_text_and_image(self):
        self.make()
        out, images, names, xml, rels = self.export()
        root = ET.fromstring(xml)   # well-formed XML
        self.assertEqual(images, 1)
        for part in ("[Content_Types].xml", "_rels/.rels", "word/document.xml", "word/_rels/document.xml.rels", "word/media/image1.png"):
            self.assertIn(part, names)
        self.assertIn("media/image1.png", rels)
        self.assertIn('r:embed="rId3"', xml)
        self.assertIn(f'cx="{300 * 9525}"', xml)   # the |300 hint, aspect kept (40x20 -> 150 px high)
        self.assertIn(f'cy="{150 * 9525}"', xml)
        paras = ["".join(t.text or "" for t in p.iter(W + "t")) for p in root.iter(W + "p")]
        self.assertIn("Vitrification µ ±", paras)
        self.assertIn("Hello bold and italic text, alias and site and Plain target.", paras)
        self.assertNotIn("kb:auto", xml)
        self.assertNotIn("type: concept", xml)
        self.assertIn('w:val="Heading1"', xml)
        self.assertIn('w:val="Heading2"', xml)
        self.assertIn("• first item", paras)
        self.assertIn("1. numbered item", paras)
        self.assertIn("Quote title", paras)
        self.assertIn("The cells survived (test2026_example_abcd:S0001), p<0.05 *P<0.05, **P<0.01.", paras)  # verbatim, stars kept
        self.assertIn("Figure 1. Greek α β γ and tiếng Việt có dấu.", paras)
        self.assertEqual(len(re.findall("<w:tbl>", xml)), 1)
        self.assertIn("shown", paras)             # the escaped \| alias is one cell, not two
        self.assertEqual(len(root.findall(f".//{W}tr")[1].findall(f"{W}tc")), 2)
        self.assertIn("[image not found: missing.png]", paras)

    def test_resolves_note_name_and_never_overwrites(self):
        self.make()
        first, *_ = self.export("vitrification")
        second, *_ = self.export("Vitrification")
        self.assertEqual(first.name, "Vitrification.docx")
        self.assertEqual(second.name, "Vitrification_1.docx")
        self.assertTrue(first.is_relative_to(config.ROOT / "runtime" / "exports"))
        out = Path(tempfile.gettempdir()) / "demo3-docx-test" / "x.docx"
        self.addCleanup(lambda: [f.unlink() for f in out.parent.glob("*")] and out.parent.rmdir())
        self.assertEqual(self.export(out=out)[0], out)

    def test_review_edges_keep_text_and_stay_in_vault(self):
        """Review 2026-11-03: empty frontmatter, a '| - | - |' data row, a bad image, a name with [ ], a junction out."""
        root = config.vault_dir("one")
        (root / "bad.png").write_bytes(b"not a png")
        (root / "A [x].md").write_text("---\n---\nkeep this\n| a | b |\n|---|---|\n| - | - |\n![[bad.png]]\n", encoding="utf-8")
        out, images = docx_export.export("one", "A [x]")
        with zipfile.ZipFile(out) as z:
            paras = ["".join(t.text or "" for t in p.iter(W + "t")) for p in ET.fromstring(z.read("word/document.xml")).iter(W + "p")]
        self.assertEqual(images, 0)
        for text in ("keep this", "-", "[image not found: bad.png]"):
            self.assertIn(text, paras)
        outside = config.ROOT / "outside"
        outside.mkdir()
        (outside / "J.md").write_text("# outside\n![[s.png]]\n", encoding="utf-8")
        pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 4, 2), False).save(str(outside / "s.png"))
        if subprocess.run(["cmd", "/c", "mklink", "/J", str(root / "junc"), str(outside)], capture_output=True).returncode:
            self.skipTest("no junction")
        with self.assertRaises(ValueError):
            docx_export.export("one", "J")
        (root / "U.md").write_text("![[s.png]]\n", encoding="utf-8")
        self.assertEqual(docx_export.export("one", "U")[1], 0)   # the image behind the junction is not read

    def test_refuses_outside_vault_and_unknown(self):
        self.make()
        for bad in ("../two/Home.md", "/etc/passwd", "C:/Windows/x.md", "Concepts/../../x.md"):
            with self.assertRaises(ValueError, msg=bad):
                docx_export.export("one", bad)
        with self.assertRaises(ValueError):
            docx_export.export("one", "No such note")
        with self.assertRaises(ValueError):
            docx_export.export("../two", "Home.md")
        with self.assertRaises(ValueError):
            docx_export.export("nope", "Home.md")
        for out in (config.vault_dir("one") / "x.docx", config.STORE / "sub" / "x.docx"):  # review: no lock, so never there
            with self.assertRaises(ValueError, msg=out):
                docx_export.export("one", "Concepts/Vitrification.md", out=out)
            self.assertFalse(out.exists() or out.parent.exists() and out.parent.name == "sub")


if __name__ == "__main__":
    unittest.main()
