"""Figure locator: a journal banner in the page margin is never a figure; a side caption finds the figure beside it."""
import unittest

import pymupdf

from kbsys import objects


class FigureLocatorTests(unittest.TestCase):
    def page(self):
        doc = pymupdf.open()
        page = doc.new_page(width=594, height=792)
        page.draw_rect(pymupdf.Rect(58, 3, 536, 28), color=(0, 0, 0), fill=(0.5, 0.5, 0.5))  # "ARTICLE IN PRESS" banner
        page.draw_rect(pymupdf.Rect(220, 60, 536, 280), color=(0, 0, 0), fill=(0.1, 0.1, 0.1))  # the figure
        page.insert_textbox(pymupdf.Rect(60, 150, 200, 210), "Figure 1. Representative scan images show the lesion.",
                            fontsize=8)
        return doc, page

    def test_side_caption_and_banner(self):
        doc, page = self.page()
        block = next(b for b in page.get_text("dict")["blocks"] if b.get("lines"))
        rect = objects._figure_rect(page, {"bbox": block["bbox"]}, [{"bbox": block["bbox"]}])
        self.assertIsNotNone(rect)
        self.assertGreater(rect.y0, 50)  # the banner is not part of the figure
        self.assertGreaterEqual(rect.x0, 215)
        doc.close()

    def test_column_caption_does_not_take_other_column_figure(self):
        doc = pymupdf.open()
        page = doc.new_page(width=594, height=792)
        page.draw_rect(pymupdf.Rect(310, 60, 540, 280), color=(0, 0, 0), fill=(0.1, 0.1, 0.1))  # right column
        page.insert_textbox(pymupdf.Rect(50, 150, 285, 210), "Figure 2. A long caption in the left column that fills the whole width of that column and wraps over two lines.", fontsize=8)
        block = next(b for b in page.get_text("dict")["blocks"] if b.get("lines"))
        self.assertIsNone(objects._figure_rect(page, {"bbox": block["bbox"]}, [{"bbox": block["bbox"]}]))
        doc.close()


if __name__ == "__main__":
    unittest.main()
