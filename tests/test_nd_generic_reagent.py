"""Regressions for the ND-02/04/07 triage (G4.3b): generic tags, reagent phrases, the dura mater homonym, unique concept ids."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concepts, db, notes, presentation


class GenericTagTests(unittest.TestCase):
    def registry(self, records=()):
        return concepts.Registry([{"concept_id": "local:" + r.lower().replace(" ", "-"), "name": r, "type": "concept", "synonyms": []}
                                  for r in records])

    def test_generic_analysis_tag_gets_no_link_and_no_stub_but_a_confirmed_one_stays(self):
        temp = Path(tempfile.mkdtemp())
        (temp / "generic_terms.yaml").write_text("generic_terms: [Clinical data, Informed consent]\n", encoding="utf-8")
        analysis = {"concepts": ["Clinical data", "clinical-data", "Muse cell", "Informed consent"]}
        with patch.object(concepts.config, "CONFIG_DIR", temp):
            terms = notes._canonical_analysis_terms(analysis, self.registry(["Muse cell", "Informed consent"]))
        self.assertEqual(terms["concepts"], ["Muse cell", "Informed consent"])  # Informed consent is a registry node: researcher-confirmed

    def test_tidy_moves_an_unlinked_generic_stub(self):
        temp = Path(tempfile.mkdtemp())
        folder = temp / "Concepts"
        folder.mkdir()
        (folder / "Clinical data.md").write_text(notes._stub_template("Concepts", "Clinical data"), encoding="utf-8")
        (temp / "Papers").mkdir()
        (temp / "Papers" / "P.md").write_text("Concepts: [[Muse cell]]\n", encoding="utf-8")
        with patch.object(notes.config, "vault_dir", return_value=temp), patch.object(notes.config, "rel", side_effect=str):
            report = notes.tidy_vault(None, "v", dry_run=True, registry=self.registry())
        self.assertEqual([Path(m["from"]).name for m in report["moved"]], ["Clinical data.md"])


class ReagentPhraseTests(unittest.TestCase):
    def test_british_and_misspelt_fetal_bovine_serum_are_reagents(self):
        bovine = {"name": "Bovine", "type": "species"}
        fetus = {"name": "Fetus", "type": "cell_type"}
        for text in ("with 20% (v/v) foetal bovine serum (FBS; A100-001A)", "and 10% fetus bovine serum in an atmosphere"):
            self.assertFalse(concepts._pattern_matches(text, concepts._regex("bovine"), bovine), text)
        self.assertFalse(concepts._pattern_matches("and 10% fetus bovine serum in an atmosphere", concepts._regex("Fetus"), fetus))
        self.assertTrue(concepts._pattern_matches("the human fetus liver was used", concepts._regex("Fetus"), fetus))
        self.assertTrue(concepts._pattern_matches("bovine embryos were vitrified.", concepts._regex("bovine"), bovine))

    def test_dura_mater_is_not_the_mater_gene(self):
        self.assertTrue(presentation.homonym_blocked("Mater", "The dura mater was kept intact."))
        self.assertFalse(presentation.homonym_blocked("Mater", "Genes involved in early development (i.e., Mater gene) were lower."))


class UniqueIdTests(unittest.TestCase):
    def build(self, kept_name):
        # 'Na/K' and 'Na K' have different merge keys but the same slug: the kept id of one must not be taken again by the other
        temp = Path(tempfile.mkdtemp())
        db.write_jsonl(temp / "concepts.jsonl", [{"concept_id": "local:na-k", "name": kept_name, "type": "molecule"}])
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
        con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        con.executemany("INSERT INTO paper_terms VALUES ('molecules', ?)", [("Na/K",), ("Na K",)])
        with patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}),                 patch.object(concepts.config, "STORE", temp), patch.object(concepts.mesh, "available", return_value=False):
            return {r["name"]: r["concept_id"] for r in concepts.build_registry(con).records}

    def test_new_name_with_the_slug_of_a_kept_id_gets_its_own_id(self):
        ids = self.build("Na/K")  # the kept concept sorts first
        self.assertEqual(len(set(ids.values())), 2, ids)
        self.assertEqual(ids["Na/K"], "local:na-k")  # the kept id stays

    def test_kept_id_stays_when_the_new_concept_sorts_first(self):
        ids = self.build("Na K")  # 'na/k' < 'nak': the new concept is met first and must not take the kept id
        self.assertEqual(len(set(ids.values())), 2, ids)
        self.assertEqual(ids["Na K"], "local:na-k")


class MeshFallbackTests(unittest.TestCase):
    """A name that is an acronym keeps the MeSH match of a longer spelling merged under it (CK7 + Cytokeratin 7)."""

    def enrich(self, name, synonyms):
        hits = {"cytokeratin 7": [{"descriptor_ui": "D1", "synonyms": []}], "pca": [{"descriptor_ui": "D2", "synonyms": []}]}
        record = {"concept_id": "local:x", "name": name, "type": "molecule", "synonyms": synonyms, "status": "ai_proposed",
                  "mesh_ui": None, "mesh_tree_numbers": [], "parents": []}
        with patch.object(concepts.mesh, "available", return_value=True),                 patch.object(concepts.mesh, "lookup", side_effect=lambda term, **kw: hits.get(term.casefold(), [])),                 patch.object(concepts.mesh, "descriptor", side_effect=lambda ui: {"ui": ui, "tree_numbers": []}),                 patch.object(concepts.mesh, "parent_descriptors", return_value=[]):
            concepts._enrich_mesh([record])
        return record

    def test_a_longer_spelling_gives_the_acronym_node_its_mesh_descriptor(self):
        self.assertEqual(self.enrich("CK7", ["Cytokeratin 7", "CK-7"])["mesh_ui"], "D1")

    def test_an_acronym_synonym_never_picks_a_descriptor(self):
        self.assertIsNone(self.enrich("Passive cutaneous anaphylaxis", ["PCA"])["mesh_ui"])  # ND-01, ND-08: PCA is not looked up

    def test_two_synonyms_naming_two_descriptors_give_no_match(self):
        hits = {"cytokeratin 7": [{"descriptor_ui": "D1", "synonyms": []}], "keratin seven": [{"descriptor_ui": "D3", "synonyms": []}]}
        record = {"concept_id": "local:x", "name": "CK7", "type": "molecule", "synonyms": ["Cytokeratin 7", "Keratin seven"],
                  "status": "ai_proposed", "mesh_ui": None, "mesh_tree_numbers": [], "parents": []}
        with patch.object(concepts.mesh, "available", return_value=True), patch.object(concepts.mesh, "lookup", side_effect=lambda term, **kw: hits.get(term.casefold(), [])):
            concepts._enrich_mesh([record])
        self.assertIsNone(record["mesh_ui"])


if __name__ == "__main__":
    unittest.main()
