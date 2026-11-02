import json
import sqlite3
import unittest

from kbsys import concepts, db
import tempfile
from pathlib import Path
from unittest.mock import patch


class AcronymSpellingTests(unittest.TestCase):
    def test_acronym_counted_case_sensitively(self):
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        con.executemany("INSERT INTO sentences VALUES (?, 'results')",
                        [("Its effect was clear.",), ("its role",), ("Its size grew.",), ("Medium with ITS was used.",)])
        counts = concepts._text_spelling_counts(con, {"ITS"})
        self.assertEqual(dict(counts), {"ITS": 1})
        counts = concepts._text_spelling_counts(con, {"medium"})
        self.assertEqual(dict(counts), {"Medium": 1})


class GeneSymbolRegexTests(unittest.TestCase):
    def test_gene_symbol_accepts_title_case_and_hyphen_but_not_words(self):
        rx = concepts._regex("SIRT1", True)
        for text in ("SIRT1 is", "Sirt1 and", "SIRT-1 levels", "Sirt-1,"):
            self.assertTrue(rx.search(text), text)
        for text in ("sirt1", "xSirt1", "Sirt12", "SIRT2"):
            self.assertFalse(rx.search(text), text)


class GeneSymbolCandidateTests(unittest.TestCase):
    def test_fts_candidates_find_hyphenated_and_title_case_forms(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.executescript("""CREATE TABLE sentences (sentence_id TEXT PRIMARY KEY, paper_id TEXT, section TEXT, text TEXT);
            CREATE VIRTUAL TABLE sentences_fts USING fts5(text, content='sentences', content_rowid='rowid',
                tokenize='porter unicode61 remove_diacritics 2');""")
        for i, (pid, text) in enumerate([("a", "SIRT-1 was lowered."), ("b", "Sirt1 was lowered."), ("c", "Nothing here.")]):
            con.execute("INSERT INTO sentences VALUES (?,?,?,?)", (str(i), pid, "results", text))
            con.execute("INSERT INTO sentences_fts(rowid, text) VALUES (?,?)", (i + 1, text))
        self.assertEqual(concepts._fts_candidates(con, "SIRT1"), {"a", "b"})
        self.assertFalse(concepts._regex("AR2", True).search("ar2"))
        self.assertFalse(concepts._regex("P53", True).search("P-53 p53"))


class RegistrySynonymTests(unittest.TestCase):
    def test_word_variants_are_query_side_only(self):
        # the vitrification/vitrified/vitrify group must not become synonyms of the Vitrification node (link-concepts)
        from kbsys import config, find
        cfg = config.load()
        self.assertTrue(any("vitrified" in g for g in cfg["word_variants"]))
        self.assertFalse(any(t.lower().startswith("vitrif") and t.lower() != "vitrification" for g in concepts.synonym_groups() for t in g))
        self.assertIn("vitrified", find.parse_detail("vitrify", cfg)[0][0])

    def test_reviewed_synonym_groups_and_legacy_its(self):
        with patch.object(concepts.config, "load", return_value={"synonyms": [
            ["histone H4", "H4"], ["confocal", "confocal microscopy"],
            ["animal experiment", "animal experiments"]]}):
            groups = concepts.synonym_groups()
        flattened = [set(value.casefold() for value in group) for group in groups]
        self.assertTrue(any({"h4", "histone h4"}.issubset(group) for group in flattened))
        with tempfile.TemporaryDirectory() as temp:
            store = Path(temp)
            db.write_jsonl(store / "concepts.jsonl", [{"concept_id": "local:its", "name": "Its", "synonyms": ["its"]}])
            with patch.object(concepts.config, "STORE", store):
                self.assertEqual(concepts.load_registry().records[0]["name"], "ITS")


if __name__ == "__main__":
    unittest.main()


class CasingClashTests(unittest.TestCase):
    def test_registry_prefers_verbatim_acronym_over_pronoun(self):
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        con.executemany("INSERT INTO sentences VALUES (?, 'methods')",
                        [("Its size grew.",), ("Its role was clear.",), ("Medium with 1 x ITS was used.",)])
        from collections import Counter
        observed = Counter({"Its": 2, "ITS": 1})
        for acronym in [s for s in observed if s.isupper()]:
            for other in [s for s in observed if s != acronym and s.casefold() == acronym.casefold()]:
                del observed[other]
        self.assertEqual(list(observed), ["ITS"])
        self.assertEqual(dict(concepts._text_spelling_counts(con, {"ITS"})), {"ITS": 1})


class KeyNormalisationTests(unittest.TestCase):
    def test_typographic_variants_share_a_key_but_subunits_do_not(self):
        same = [["RT-qPCR", "RTqPCR", "RT qPCR"], ["m6A", "m 6A"], ["β-tubulin", "Β-tubulin", "β tubulin", "beta-tubulin"],
                ["real-time PCR", "real time PCR"], ["ITS", "Its"],
                ["Lyve-1", "Lyve1", "LYVE-1"], ["Fisher's exact test", "Fisher’s exact test", "Fisherʼs exact test"], ["Student's t-test", "Students t test"]]
        for group in same:
            self.assertEqual(len({concepts._key(v) for v in group}), 1, group)
        self.assertNotEqual(concepts._key("Tubulin"), concepts._key("β-tubulin"))
        self.assertNotEqual(concepts._key("type 1"), concepts._key("type 2"))
        self.assertNotEqual(concepts._key("DNA (cytosine-5-)-methyltransferase"), concepts._key("DNA methyltransferase"))

    def test_generic_study_terms_never_become_nodes(self):
        temp = Path(tempfile.mkdtemp())
        (temp / "generic_terms.yaml").write_text("generic_terms: [Healthy controls, Informed consent]\n", encoding="utf-8")
        with patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}), \
                patch.object(concepts.config, "CONFIG_DIR", temp), patch.object(concepts.config, "STORE", temp), \
                patch.object(concepts.mesh, "available", return_value=False):
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
            con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
            con.executemany("INSERT INTO paper_terms VALUES ('concepts', ?)",
                            [("healthy control",), ("Informed-consent",), ("Muse cells",)])
            names = [r["name"] for r in concepts.build_registry(con).records]
        self.assertEqual(names, ["Muse cells"])

    def test_config_parent_keeps_beta_tubulin_separate_and_acronym_loses_pronoun_synonym(self):
        records = [{"concept_id": "local:tubulin", "name": "Tubulin", "synonyms": [], "parents": []},
                   {"concept_id": "local:beta-tubulin", "name": "β-tubulin", "synonyms": [], "parents": []}]
        cfg = {"concept_parents": {"beta-tubulin": "Tubulin", "missing": "Tubulin"}}
        self.assertEqual(concepts.config_parent_ids(records, cfg), {"local:beta-tubulin": "local:tubulin"})
        self.assertEqual(concepts._slug("β-tubulin"), "beta-tubulin")
        with patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}):
            con = sqlite3.connect(":memory:")
            con.row_factory = sqlite3.Row
            con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
            con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
            con.executemany("INSERT INTO paper_terms VALUES ('molecules', ?)", [("ITS",), ("Its",)])
            con.execute("INSERT INTO sentences VALUES ('Medium with ITS was used.', 'methods')")
            with patch.object(concepts.config, "STORE", Path(tempfile.mkdtemp())), patch.object(concepts.mesh, "available", return_value=False):
                record = concepts.build_registry(con).records[0]
        self.assertEqual((record["name"], "Its" in record["synonyms"]), ("ITS", False))


class MeshTreeTypeTests(unittest.TestCase):
    """ND-09: a build-type `concept` node takes its type from its MeSH tree numbers."""

    def test_mesh_tree_type(self):
        for trees, expected in [(["A11.148", "A11.436"], "cell_type"), (["A11.284"], None), (["A11.284.430", "D12.776"], "molecule"),
                                (["D05.500", "D12.776"], "molecule"), (["B01.050"], "species"), (["E05.1"], "method"), (["B05.200"], None),
                                (["E01.370"], None), (["D09.947", "E05.1"], None), ([], None)]:
            self.assertEqual(concepts.mesh_tree_type(trees), expected, trees)

    def build(self, store):
        trees = {"D1": ["A11.148"], "D2": ["D12.776"], "D3": ["B01.050"]}
        hits = {"foo cell": "D1", "bar assay": "D2", "baz mouse": "D3"}
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
        con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        con.executemany("INSERT INTO paper_terms VALUES (?, ?)", [("concepts", "Foo cell"), ("methods", "Bar assay"), ("concepts", "Baz mouse")])
        with patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}), \
                patch.object(concepts.config, "CONFIG_DIR", store), patch.object(concepts.config, "STORE", store), \
                patch.object(concepts.mesh, "available", return_value=True), \
                patch.object(concepts.mesh, "lookup", side_effect=lambda t, **kw: [{"descriptor_ui": hits[t.casefold()], "synonyms": []}] if t.casefold() in hits else []), \
                patch.object(concepts.mesh, "descriptor", side_effect=lambda ui: {"ui": ui, "tree_numbers": trees[ui]}), \
                patch.object(concepts.mesh, "parent_descriptors", return_value=[]):
            registry = concepts.build_registry(con)
            concepts.write_registry(registry)
        return {r["name"]: r for r in registry.records}

    def test_build_types_marks_and_is_idempotent(self):
        store = Path(tempfile.mkdtemp())
        (store / "concepts.jsonl").write_text(json.dumps({"concept_id": "local:baz-mouse", "name": "Baz mouse", "type": "concept",
                                                          "status": "human_confirmed", "synonyms": []}) + "\n", encoding="utf-8")
        first = self.build(store)
        self.assertEqual((first["Foo cell"]["type"], first["Foo cell"]["classified_by"]), ("cell_type", "ND-09"))
        self.assertEqual((first["Bar assay"]["type"], first["Bar assay"]["classified_by"]), ("method", "terms"))
        self.assertEqual((first["Baz mouse"]["type"], first["Baz mouse"]["classified_by"]), ("concept", "human"))
        self.assertEqual(self.build(store), first)  # a second build changes nothing

    def test_a_resubmit_keeps_the_rule_type_reversible(self):
        # stored term lists keep the AI's list for a rule-typed node, so narrowing the rule gives the term type back
        first = self.build(Path(tempfile.mkdtemp()))
        registry = concepts.Registry(list(first.values()))
        terms = {key: [] for key in concepts.TERM_KEYS}
        terms["concepts"] = ["Foo cell"]
        self.assertEqual(concepts.normalize_terms(terms, registry, keep_rule_types=True)["concepts"], ["Foo cell"])
        self.assertEqual(concepts.normalize_terms(terms, registry)["cell_types"], ["Foo cell"])  # display follows the registry
        with patch.object(concepts, "mesh_tree_type", return_value=None):
            again = self.build(Path(tempfile.mkdtemp()))
        self.assertEqual((again["Foo cell"]["type"], again["Foo cell"]["classified_by"]), ("concept", "terms"))

    def test_a_rule_type_beats_an_older_card_type(self):
        from kbsys import node_profile
        profile = {"card": {"node_type": "concept"}}
        self.assertEqual(node_profile.graph_node_type(profile, "molecule", {"classified_by": "ND-09"}), "substance")
        self.assertEqual(node_profile.graph_node_type(profile, "molecule", {"classified_by": "terms"}), "concept")  # a term type keeps the card's

    def test_a_validated_unit_keeps_the_ai_list_for_a_rule_typed_node(self):
        # evidence._check_unit stores unit terms: a rule type is not written into them, a terms type still collapses
        from kbsys import evidence
        ctx = evidence.PaperContext.__new__(evidence.PaperContext)
        ctx.paper_id, ctx.meta, ctx.table_caption_ids, ctx.refs = "P", {}, set(), {}
        ctx.sentences = {"P:S0001": {"sentence_id": "P:S0001", "seq": 1, "passage_id": "a", "page": 1, "section": "results",
                                     "kind": "text", "text": "Foo cell counts rose.", "citations": [], "mentions": []}}
        ctx._index()
        ctx.canonical = {"_groups": {}}
        unit = {"unit_type": "background", "statement": "Foo cell counts rose.", "sentence_ids": ["S0001"],
                "terms": {"concepts": ["Foo cell"]}}
        for classified_by, key in (("ND-09", "concepts"), ("terms", "cell_types")):
            ctx.registry = concepts.Registry([{"concept_id": "local:foo-cell", "name": "Foo cell", "type": "cell_type",
                                               "synonyms": [], "status": "mesh", "classified_by": classified_by}])
            stored = evidence._check_unit(ctx, 1, dict(unit), False, lambda *a: None, lambda *a: None, [])
            self.assertEqual(stored["terms"][key], ["Foo cell"], classified_by)

    def test_a_stored_record_without_classified_by_is_not_changed(self):
        # a record stored before the field existed is not "changed" (relink_paper would rescan every concept x every paper)
        record = {"concept_id": "local:a", "name": "A", "type": "concept", "classified_by": "terms"}
        old = {k: v for k, v in record.items() if k != "classified_by"}
        self.assertEqual(concepts._changed_records({"local:a": old}, concepts.Registry([record])), [])
        self.assertEqual(len(concepts._changed_records({"local:a": {**old, "type": "molecule"}}, concepts.Registry([record]))), 1)
