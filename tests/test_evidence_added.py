"""G4.3 EV-19 / V15: a statement adds no entity beyond its cited sentences. `evidence.added_entities` is the one rule
(validator flag V15 and conform check EV-19). Each false alarm has a PASS case; a real added word still fails."""
import unittest

from kbsys import concepts, evidence


def added(statement, *texts, refs=None, cited=(), groups=(), abbreviations=()):
    rows = [{"text": t} for t in texts]
    return evidence.added_entities(statement, rows, refs or {}, set(cited), set(), groups, abbreviations)


class AddedEntities(unittest.TestCase):
    def test_a_real_added_word_is_reported_once(self):
        self.assertEqual(added("Zeta cells and Zeta lines grew.", "They grew."), ["Zeta"])
        self.assertEqual(added("Survival rose, mediated by HIF1A.", "Survival rose."), ["HIF1A"])

    def test_held_words_pass(self):
        self.assertEqual(added("Smith et al. reported p150 PAF.", "Reported (Smith, 2010) the sip150 of PAFs.", refs={"p:R1": {"first_author": "Smith"}}, cited={"p:R1"}), [])
        self.assertEqual(added("DMSO was added.", "Dimethyl sulfoxide was added.", groups=[("dimethyl sulfoxide", "DMSO")]), [])

    def test_a1_pdf_split_and_list_shorthand(self):
        self.assertEqual(added("Vitis vinifera and Theobroma cacao.", "Vitis vinifera and Theo broma cacao."), [])
        self.assertEqual(added("HDAC4 and HDAC11 are class II.", "Class II (HDAC 4, 5) and HDAC 11."), [])
        self.assertEqual(added("HDAC2, HDAC3 and HDAC8 are nuclear.", "Class I HDACs (HDAC1,2,3,8) are nuclear."), [])
        self.assertEqual(added("HDAC5, HDAC6, HDAC10 are class II.", "Class II HDACs (HDAC 4, 5, 6, 7, 9, and 10)."), [])
        self.assertEqual(added("The HDACis act.", "The HDA- Cis act."), [])
        # FAIL: a number that is not on the list, and a word that is not two consecutive tokens
        self.assertEqual(added("HDAC12 acts.", "Class II (HDAC 4, 5, 6)."), ["HDAC12"])
        self.assertEqual(added("The Theobroma grew.", "Theo grew. Broma was seen."), ["Theobroma"])

    def test_a2_latin_binomial_abbreviated_in_the_source(self):
        self.assertEqual(added("Cloning of Arabidopsis thaliana MET1.", "MET1 was cloned in A. thaliana."), [])
        self.assertEqual(added("The Pinus pinaster lines.", "In P.pinaster the lines."), [])
        self.assertEqual(added("The Pinus sylvestris lines.", "In P. pinaster the lines."), ["Pinus"])  # another epithet
        self.assertEqual(added("The Zea mays lines.", "In P. pinaster the lines."), ["Zea"])

    def test_a3_a_sentence_id_is_no_entity(self):
        self.assertEqual(added("Shown (sentence continues in S0034).", "Shown here."), [])

    def test_b1_abbreviation_defined_by_the_paper(self):
        abbreviations = [{"short": "TE", "definitions": [{"text": "trophectoderm"}]}]
        self.assertEqual(added("Cells of the Trophectoderm grew.", "TE cells grew.", abbreviations=abbreviations), [])
        self.assertEqual(added("Cells of the TE grew.", "Trophectoderm cells grew.", abbreviations=abbreviations), [])
        # FAIL: the paper defines it but the cited sentences use neither form, or the paper does not define it
        self.assertEqual(added("Cells of the TE grew.", "Cells grew.", abbreviations=abbreviations), ["TE"])
        self.assertEqual(added("Cells of the TE grew.", "Trophectoderm cells grew.", abbreviations=[]), ["TE"])

    def test_b2_initialism_in_the_cited_text(self):
        self.assertEqual(added("Cells in alpha-MEM grew.", "Cells in alpha-minimum essential medium grew."), [])
        self.assertEqual(added("At the Max Planck Institute.", "At the MPI for Biochemistry."), [])
        self.assertEqual(added("The National Institutes of Health.", "The NIH funded it."), [])
        self.assertEqual(added("Control had 24.", "CTRL had 24."), [])
        # FAIL: letters of the acronym are not the initials of the words, an acronym window made of acronyms
        self.assertEqual(added("Cells in MEM grew.", "Cells in culture medium grew."), ["MEM"])
        self.assertEqual(added("The BM MSCs grew.", "The BM-MCSs grew."), ["MSCs"])
        self.assertEqual(added("Controls had 24.", "CTRL had 24."), [])

    def test_no_accidental_match_lets_a_real_added_word_pass(self):
        """Review of EV-19: each shortcut above must not accept a word the cited text does not hold."""
        self.assertEqual(added("The Dynamin rose.", "DNA rose."), ["Dynamin"])  # letters of DNA in order, but the last letter differs
        self.assertEqual(added("The Sertoli cells rose.", "STO cells rose."), ["Sertoli"])
        self.assertEqual(added("AC5 was measured.", "HDAC 4, 5 were measured."), ["AC5"])  # a prefix inside a longer name
        self.assertEqual(added("The Alpha cells grew.", "In group A. Cells grew."), ["Alpha"])  # a sentence start is not an epithet
        self.assertEqual(added("PD rose.", "Patients developed rose."), ["PD"])  # two letters: initials of any 2 words
        self.assertEqual(added("ABC grew.", "Groups A, B, C grew."), ["ABC"])  # single letters of a list
        self.assertEqual(added("Rose in PAFs without heat.", "PW/O heat rose."), ["PAFs"])  # 'PAFs without' spells PW
        self.assertEqual(added("In Zeta, Muse cells rose.", "ZM cells rose."), ["Zeta", "Muse"])  # not across a comma


def sentence(n, text, **extra):
    return {"sentence_id": f"P:S{n:04d}", "seq": n, "passage_id": "a", "page": 1, "section": "results", "kind": "text",
            "text": text, "citations": [], "mentions": [], **extra}


class V15(unittest.TestCase):
    def flags(self, statement, text, abbreviations=None):
        ctx = evidence.PaperContext.__new__(evidence.PaperContext)
        ctx.paper_id, ctx.meta, ctx.table_caption_ids, ctx.refs = "P", {}, set(), {}
        ctx.sentences = {s["sentence_id"]: s for s in [sentence(1, text)]}
        ctx._index()
        ctx.registry, ctx.canonical = concepts.Registry([]), {"_groups": {}}
        if abbreviations is not None:
            ctx.abbreviations = abbreviations
        flags = []
        evidence._check_unit(ctx, 1, {"unit_type": "background", "statement": statement, "sentence_ids": ["S0001"]}, False,
                             lambda i, c, m: self.fail((c, m)), lambda i, c, m: flags.append((c, m)), [])
        return [m for c, m in flags if c == "V15"]

    def test_an_added_entity_is_one_flag_listing_the_words(self):
        got = self.flags("The Zeta cells and HIF1A rose.", "Cells rose.")
        self.assertEqual(len(got), 1, got)
        self.assertIn("'Zeta', 'HIF1A'", got[0])
        self.assertIn("cite the sentence that names it", got[0])

    def test_no_flag_when_the_cited_sentences_hold_it(self):
        self.assertEqual(self.flags("The Zeta cells rose.", "Zeta cells rose."), [])
        self.assertEqual(self.flags("The TE cells rose.", "Trophectoderm cells rose.", [{"short": "TE", "definitions": [{"text": "trophectoderm"}]}]), [])
        self.assertEqual(len(self.flags("The TE cells rose.", "Trophectoderm cells rose.")), 1)  # not defined by the paper: flagged


if __name__ == "__main__":
    unittest.main()
