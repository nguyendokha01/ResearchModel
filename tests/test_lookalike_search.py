"""CHR-04 / CHR-01: look-alike symbols and NFD queries match on every search path; stored text stays as extracted."""
import unicodedata
import unittest

from . import test_core as fixtures
from kbsys import config, db, find, mcp_server, neural, search, sentences, sources_misc

DEGREE = ["37 °C", "37 ˚C", "37 ◦C", "37 ^◦C", "37 ℃", "37 ºC", "37 ^°C"]
DELTA = ["ΔG", "∆G"]
TEXTS = {f"deg{i}": f"Embryos were held at {form} for ten minutes." for i, form in enumerate(DEGREE)}
TEXTS.update({f"delta{i}": f"The {form} of binding was reported in the results." for i, form in enumerate(DELTA)})
TEXTS["cafe"] = "The café sample was imaged."
# noise: in an index made before db.FTS_TOKENIZE, U+2206 and U+2103 are separators, so "∆G" / "∆Ct" / "37 ℃" index as "g" / "ct" / "37"
TEXTS.update({"grams": "A 5 g dose was given.", "patient": "Patient 37 was seen.", "ct": "The Ct value was 20."})


class LookalikeSearchTests(unittest.TestCase):
    tearDown = fixtures.Demo3Tests.tearDown

    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        for pid, text in TEXTS.items():
            self.add_paper(pid, text)
        self.cfg = config.load()

    def add_paper(self, pid, text):
        folder = db.paper_dir(pid)
        row = {"sentence_id": f"{pid}:S0001", "seq": 1, "passage_id": f"{pid}:0001", "page": 1, "section": "results",
               "subsection": None, "kind": "text", "source": "main", "text": text, "start": 0, "end": len(text),
               "citations": [], "mentions": []}
        db.write_json(folder / "metadata.json", {"paper_id": pid, "title": pid, "year": 2026, "vaults": ["one"], "authors": [],
                                                 "source_files": [], "evidence_type": "primary_research", "extraction_version": 2})
        db.write_jsonl(folder / "sentences.jsonl", [row])
        db.write_jsonl(folder / "passages.jsonl", [{"passage_id": row["passage_id"], "seq": 1, "page": 1,
                                                    "section": "results", "kind": "text", "text": text}])
        db.write_jsonl(folder / "evidence.jsonl", [])
        db.load_paper(self.con, pid)
        self.con.commit()

    def paths(self, query):
        """paper ids found by each search path for one query"""
        found = {"find": {h["paper_id"] for h in find.search(self.con, self.cfg, query, vault="one", semantic=False, limit=50)["hits"]},
                 "search": {r["paper_id"] for r in search.search(self.con, self.cfg, query, vault="one")[0]},
                 "mcp": {r["paper_id"] for r in mcp_server._sentence_search(self.con, self.cfg, query, vault="one")}}
        return found

    def test_every_degree_form_in_query_finds_every_degree_form_stored(self):
        wanted = {p for p in TEXTS if p.startswith("deg")}
        for query in DEGREE:
            for name, ids in self.paths(query).items():
                self.assertEqual(ids & wanted, wanted, (query, name))

    def test_every_delta_form_finds_both(self):
        for query in DELTA:
            for name, ids in self.paths(query).items():
                self.assertEqual({p for p in ids if p.startswith("delta")}, {"delta0", "delta1"}, (query, name))

    def test_glued_and_spaced_degree_sign_find_each_other(self):
        self.add_paper("glued", "Samples were cooled to -196℃ at once.")
        # ponytail: "196℃" is one token, so the separate terms of an unquoted "-196 °C" (find) cannot match it
        for query in ('"-196 °C"', "-196°C", '"-196 °C"', '"-196 °C"'):  # NBSP / thin space pasted from a PDF
            for name, ids in self.paths(query).items():
                self.assertIn("glued", ids, (query, name))

    def test_generated_lookalike_forms_do_not_widen_the_match(self):
        for query, noise in (("ΔG", "grams"), ('"ΔG"', "grams"), ("ΔCt", "ct"), ('"37 °C"', "patient")):
            for name, ids in self.paths(query).items():
                self.assertNotIn(noise, ids, (query, name))

    def test_british_and_american_spellings_find_each_other(self):  # U6d: gold M1/M2 "mobilisation" vs stored mobilization
        self.add_paper("us", "Muse cell mobilization rose after stroke.")
        self.add_paper("uk", "Ischaemic injury was followed by oedema.")
        for query, pid in (("mobilisation", "us"), ("mobilization", "us"), ("ischemic edema", "uk")):
            for name, ids in self.paths(query).items():
                self.assertIn(pid, ids, (query, name))

    def test_greek_letter_name_and_symbol_find_each_other(self):  # U6d: gold V3 "gamma-H2A.X" vs stored γH2A.X
        self.add_paper("glued_g", "Vitrification increased γH2A.X intensity.")
        self.add_paper("hyphen_g", "The γ-H2A.X foci rose.")
        self.add_paper("named_g", "Staining for gamma-H2A.X rose.")
        for query in ("gamma-H2A.X", "γH2A.X", "γ-H2A.X"):
            for name, ids in self.paths(query).items():
                self.assertTrue({"glued_g", "hyphen_g", "named_g"} <= ids, (query, name, ids))

    def test_a_lone_greek_name_or_a_unit_is_not_folded(self):
        self.assertEqual(sentences.glyph_variants("delta"), ["delta"])
        self.assertEqual(sentences.glyph_variants("beta catenin"), ["beta catenin"])
        self.assertEqual(sentences.glyph_variants("10 µM"), ["10 µM"])
        self.assertEqual(sentences.glyph_variants("ΔG"), ["ΔG", "∆G"])  # CHR-04 stays as it was
        self.assertIn("TNF-α", sentences.glyph_variants("TNF-alpha"))
        self.assertIn("IL-1beta", sentences.glyph_variants("IL-1β"))

    def test_a_cut_british_stem_does_not_match_another_word(self):
        cases = (("organized", "organisms", "organised"), ("characterized", "characteristic", "characterised"),
                 ("hypothesized", "the hypothesis", "hypothesised"), ("analysed", "analysis", "analyses"))
        for query, other, same in cases:
            pattern = find._group_patterns([[query]])[0]
            self.assertFalse(pattern.search(other), (query, other))
            self.assertTrue(pattern.search(same), (query, same))
        pattern = find._group_patterns([["mobilisation"]])[0]
        self.assertTrue(all(pattern.search(w) for w in ("mobilized", "mobilising", "mobilization")))
        self.assertTrue(find._group_patterns([["iris"]])[0].search("iris"))  # a word that ends in -is itself

    def test_every_word_matches_itself_and_highlight_keeps_the_guard(self):  # U6d review
        for word in ("Swiss", "dismiss", "abyss", "organised", "mobilisation", "analyses", "iris", "crisis", "cis"):
            self.assertTrue(find._group_patterns([[word]])[0].search(word), word)
        for query, other in (("organized", "organisms"), ("hypothesized", "hypothesis"), ("analyzed", "analysis")):
            self.assertFalse([t for t in find.highlight_terms([[query]]) if other.startswith(t)], (query, other))
        self.assertIn("organise", find.highlight_terms([["organized"]]))

    def test_a_bare_haem_or_a_trailing_mu_is_not_folded(self):  # U6d review: hem/4-μ prefix-match hemisphere/4-μm
        self.assertEqual(sentences.glyph_variants("haem"), ["haem"])
        self.assertIn("hemoglobin", sentences.glyph_variants("haemoglobin"))
        self.assertFalse(find._group_patterns([["haem"]])[0].search("left hemisphere"))
        self.assertEqual(sentences.glyph_variants("4-MU"), ["4-MU"])
        self.assertFalse(find._group_patterns([["4-MU"]])[0].search("4-μm-thick sections"))
        self.assertIn("μ-opioid", sentences.glyph_variants("mu-opioid"))

    def test_an_old_index_keeps_the_typed_form_only_and_rebuild_db_upgrades_it(self):
        old = "tokenize='porter unicode61 remove_diacritics 2'"
        for table in db.FTS_TABLES:  # an index made before the tokenizer change (the live store until G5)
            self.con.execute(f"DROP TABLE {table}")
        self.con.executescript(db.SCHEMA.replace(db.FTS_TOKENIZE, old))
        for table in db.FTS_TABLES:
            self.con.execute(f"INSERT INTO {table}({table}) VALUES ('rebuild')")
        self.assertFalse(db.fts_glyph_tokens(self.con))
        for query, noise in (("ΔG", "grams"), ("ΔCt", "ct"), ('"37 °C"', "patient")):
            for name, ids in self.paths(query).items():
                self.assertNotIn(noise, ids, (query, name))
        db.rebuild(self.con)
        self.assertTrue(db.fts_glyph_tokens(self.con))
        for name, ids in self.paths("ΔG").items():
            self.assertIn("delta1", ids, name)
            self.assertNotIn("grams", ids, name)

    def test_a_lone_degree_symbol_is_a_find_term(self):
        self.assertIn("deg0", {h["paper_id"] for h in find.search(self.con, self.cfg, "℃", vault="one", semantic=False)["hits"]})

    def test_nfd_vietnamese_gets_the_offline_vocabulary(self):
        self.assertEqual(neural._expanded_query(unicodedata.normalize("NFD", "tế bào")), neural._expanded_query("tế bào"))

    def test_nfd_query_is_judged_like_its_nfc_form(self):
        nfd = unicodedata.normalize("NFD", "café")
        self.assertNotEqual(nfd, "café")
        for name, ids in self.paths(nfd).items():
            self.assertIn("cafe", ids, name)
        self.assertEqual(find.parse_detail(nfd, self.cfg)[0], find.parse_detail("café", self.cfg)[0])

    def test_stored_text_is_not_rewritten(self):
        self.paths("37 °C"), self.paths("ΔG")
        for pid, text in TEXTS.items():
            self.assertEqual(self.con.execute("SELECT text FROM sentences WHERE paper_id=?", (pid,)).fetchone()[0], text)

    def test_fold_and_variants_agree_on_every_form(self):
        self.assertEqual({sentences.fold_search_glyphs(f) for f in DEGREE}, {"37 °C"})
        self.assertEqual({sentences.fold_search_glyphs(f) for f in DELTA}, {"ΔG"})
        for form in DEGREE + DELTA:
            self.assertIn(sentences.fold_search_glyphs(form), sentences.glyph_variants(form))
        self.assertEqual(sentences.fold_search_glyphs("A^˚ (Å)"), "A^˚ (Å)")  # a ring above not before C is left alone

    def test_source_search_expands_glyph_variants(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("The probe was kept at 37 ˚C; the ∆G was small. A 5 g dose was given.", encoding="utf-8", newline="\n")
            sources_misc.import_source(path, "one", con=self.con)
        for query in ("37 °C", "∆G"):  # "ΔG" -> stored "∆G": the same FTS limit as above
            self.assertTrue(sources_misc.search_sources(self.con, query, vault="one"), query)
        self.assertFalse([r for r in sources_misc.search_sources(self.con, "ΔG", vault="one") if "5 g" in r["text"]])

    def test_an_old_source_index_is_gated_on_its_own_table_and_rebuild_db_upgrades_it(self):
        import tempfile
        from pathlib import Path
        self.con.execute("DROP TABLE IF EXISTS source_sentences_fts")
        self.con.execute("CREATE VIRTUAL TABLE source_sentences_fts USING fts5(sentence_id UNINDEXED, source_id UNINDEXED, vault "
                         "UNINDEXED, text, locator UNINDEXED, source_kind UNINDEXED, tokenize='porter unicode61 remove_diacritics 2')")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "note.md"
            path.write_text("The ∆G was small. A 5 g dose was given.", encoding="utf-8", newline="\n")
            sources_misc.import_source(path, "one", con=self.con)
        self.assertTrue(db.fts_glyph_tokens(self.con))  # the paper tables are current; the source table is not
        self.assertFalse(db.fts_glyph_tokens(self.con, ("source_sentences_fts",)))
        self.assertFalse(sources_misc.search_sources(self.con, "ΔG", vault="one"))  # a generated "∆G" would be "g" here
        db.rebuild(self.con)  # `kb.py rebuild-db` (G5) re-indexes imported sources with the current tokenizer
        self.assertTrue(db.fts_glyph_tokens(self.con, ("source_sentences_fts",)))
        self.assertEqual([r["text"] for r in sources_misc.search_sources(self.con, "ΔG", vault="one")], ["The ∆G was small."])


if __name__ == "__main__":
    unittest.main(verbosity=2)
