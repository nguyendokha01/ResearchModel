"""Author-year citations are not lost: "et al" is recognised, hyphenated and accented surnames stay whole,
and no source file carries a control character (a backspace once stood where a regex word boundary was meant)."""
import re
import unittest
from pathlib import Path

from kbsys import references, sentences

ROOT = Path(__file__).resolve().parents[1]
CONTROL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")  # all but tab, newline, carriage return


def cites(text, refs):
    ctx = sentences.CitationContext(refs)
    return [(c["marker"], c["ref_nums"], c["confidence"]) for c in sentences.find_citations(text, ctx, "introduction")]


class AuthorYearTests(unittest.TestCase):
    OTHER = [{"num": None, "first_author": "Marlow", "year": 2010}]  # a non-empty list turns author-year matching on

    def test_et_al_is_kept_without_a_reference_entry(self):
        got = cites("Shown before (Marin-Olvera et al., 2008; Tun-Cetina et al., 2013). Marlow et al. (2010) agree.",
                    self.OTHER)
        self.assertEqual(got, [("Marin-Olvera et al., 2008", [], "low"), ("Tun-Cetina et al., 2013", [], "low"),
                               ("Marlow et al. (2010)", [1], "high")])

    def test_hyphenated_and_accented_surnames_resolve_to_their_reference(self):
        refs = [dict(references.parse_fields(raw), num=None) for raw in (
            "Marin-Olvera, V. M., De-la-Cañas, C., Varga, R. M. (2008). Garden notes on soil culture. Example Press.",
            "Tun-Cetina, G. I., Pérez-Ruiz, A., Dorsey, K., et al. (2013). New notes on seed germination timing.",
            "De-la-Cañas, C., Tun-Cetina, G., Quintana, G., et al. (2012). GENE1 is expressed. BMC Plant Biol. 12:203.",
            "Müllert, M. (2000). Copper salts in the cleaning of old pipes. Curr Top 249, 135-164.",
            "Orlovsky, B. F. (1984). Seasonal wind patterns over small lakes. Curr Top 108, 99-114.")]
        got = cites("See (Marin-Olvera et al., 2008), (Tun-Cetina et al., 2013), De-la-Cañas et al. (2012), "
                    "Müllert et al. (2000) and (Orlovsky, 1984).", refs)
        self.assertEqual([(m, n, c) for m, n, c in got],
                         [("Marin-Olvera et al., 2008", [1], "high"), ("Tun-Cetina et al., 2013", [2], "high"),
                          ("De-la-Cañas et al. (2012)", [3], "high"), ("Müllert et al. (2000)", [4], "high"),
                          ("Orlovsky, 1984", [5], "high")])

    def test_parse_fields_keeps_the_whole_surname(self):
        for raw, want in (("Smith-Jones A, Lee J (2012) Cells and freezing of oocytes. J Cell 5: 1-9.", "Smith-Jones"),
                          ("Garza-Duarte, C. F. J., Marin-Olvera, V. M. (2001). Modification. Biotechnol. Lett.", "Garza-Duarte"),
                          ("De-la-Cañas, C., Tun-Cetina, G. (2012). GENE1 is expressed. BMC Plant Biol. 12:203.", "De-la-Cañas"),
                          ("Stern K, Oakley C. Counting boats. Lancet Neurol 2007;6:101-109.", "Stern")):
            self.assertEqual(references.parse_fields(raw)["first_author"], want)

    def test_reference_and_citation_surnames_agree(self):
        """The reference parser and the in-text pattern read the same surname, so the citation resolves;
        a name the parser cannot read whole is null, never a cut stub ("Mc", "Lo", "Safti")."""
        for name in ("McGowan", "LoSchiavo", "D’Incalci", "O'Brien", "DeVries", "Smith-Jones", "Müllert"):
            refs = [dict(references.parse_fields(f"{name}, A., Lee, B. (2009). A title of the study. J Cell 5: 1-9."),
                         num=None)]
            self.assertEqual(cites(f"Shown ({name} et al., 2009).", refs), [(f"{name} et al., 2009", [1], "high")])
        for raw in ("Saftić, A. (2009). T.", "Žukauskaitė, A. (2009). T.", "PLoS Medicine (2009). T.",
                    "Smith–Jones, A. (2009). T.", "Pérez, A. (2009). T."):
            self.assertIsNone(references.parse_fields(raw)["first_author"], raw)


class NoControlCharacterTests(unittest.TestCase):
    def test_no_control_character_in_code_config_or_hooks(self):
        bad = []
        for folder in ("scripts", "tests", "config", ".claude/hooks"):
            for path in (ROOT / folder).rglob("*"):
                if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".json", ".sh", ".md"} \
                        and "__pycache__" not in path.parts:
                    for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").split("\n"), 1):
                        if CONTROL.search(line):
                            bad.append(f"{path.relative_to(ROOT)}:{n}")
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
