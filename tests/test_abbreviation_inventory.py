"""VN-03: the paper-note abbreviation list never shows a catalogue number or bare sentence id as an abbreviation."""
import unittest

from kbsys import structure


def row(n, text, section="methods"):
    return {"sentence_id": f"p:S{n:04d}", "text": text, "section": section, "page": 1}


class AbbreviationInventoryTests(unittest.TestCase):
    def test_catalogue_number_is_not_an_abbreviation(self):
        got = structure._abbreviation_inventory([
            row(1, "Melatonin (Cat. No. S1888, Sigma) was added."),
            row(2, "Reactive oxygen species (ROS) were measured; ROS rose in MII oocytes."),
            row(3, "The inhibitor Sigma (S1888) was used."),
        ])
        self.assertNotIn("S1888", got)
        self.assertEqual([d["text"] for d in got["ROS"]["definitions"]], ["Reactive oxygen species"])
        self.assertIn("MII", got)


if __name__ == "__main__":
    unittest.main()
