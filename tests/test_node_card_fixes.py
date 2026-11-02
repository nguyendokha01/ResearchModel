"""Regression tests for the 2026-10-12 node-card review fixes (validators, homonyms, abbreviations, task scope)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import answer, concept_notes, concepts, config, db, evidence, node_cards, presentation, structure
from tests import test_node_cards as base


class ValidatorFalsePositiveTests(unittest.TestCase):
    ITEMS = [{"paper_id": "p", "text": "Laser-induced warming reached 5 C/min.", "kind": "sentence",
              "unit_type": "background", "hedging": None, "review_paper": False, "cited_refs": []}]

    def test_definitive_ignores_hyphenated_compounds(self):
        self.assertIsNone(evidence.DEFINITIVE.search("laser-induced and drug-induced warming"))
        self.assertIsNotNone(evidence.DEFINITIVE.search("warming induced a change"))

    def test_bracketed_reference_numbers_are_not_markers_for_node_cards(self):
        sentence = {"text": "Warming was fast [95,96].", "evidence": [1]}
        self.assertNotIn("A1", answer.check_sentence(sentence, self.ITEMS, {})["problems"])  # answers cite as [#n]; [95,96] is a reference
        self.assertIn("A1", answer.check_sentence({"text": "Warming was fast [#95]."}, self.ITEMS, {})["problems"])
        self.assertNotIn("A1", answer.check_sentence(sentence, self.ITEMS, {}, text_markers=False)["problems"])

    def test_figure_and_table_labels_are_not_claim_numbers(self):
        for label in ("Figure 6", "Fig. 2B", "Table S1"):
            sentence = {"text": f"Warming reached 5 C/min ({label}).", "evidence": [1]}
            self.assertNotIn("A2", answer.check_sentence(sentence, self.ITEMS, {}, text_markers=False)["problems"], label)
        sentence = {"text": "Warming reached 7 C/min.", "evidence": [1]}
        self.assertIn("A2", answer.check_sentence(sentence, self.ITEMS, {}, text_markers=False)["problems"])


class MachineIdInCardTextTests(unittest.TestCase):
    """VN-03: a sentence id in AI card text is refused at submit and shown as a citation label in the note."""

    def codes(self, text):
        source_map = {"p:S0127": {"paper_id": "p", "text": "Compaction was measured.", "hedges": []}}
        statement = {"text": text, "sentence_ids": ["p:S0127"], "attribution": "primary", "hedge": "none"}
        return {p["code"] for p in node_cards._check_statement("fields.x", statement, source_map, {"node": {"name": "Compaction"}})}

    def test_submit_refuses_a_bare_id_and_accepts_a_catalogue_number(self):
        self.assertIn("machine_id", self.codes("Compaction (S0127) was measured."))
        self.assertIn("machine_id", self.codes("Compaction (p:S0127) was measured."))
        self.assertNotIn("machine_id", self.codes("Compaction was measured with Cat. No. S1888."))

    def test_note_shows_the_citation_label_of_the_statements_own_sentence(self):
        ref = {"paper_id": "doe2026_x", "sentence_id": "doe2026_x:S0127"}
        other = {"paper_id": "lee2020_y", "sentence_id": "lee2020_y:S0127"}
        with patch.object(concept_notes, "_ref_cite", return_value="[[Doe 2026|Doe 2026, p. 4]]"):
            self.assertEqual(concept_notes._cite_bare_ids("Doe (S0127) and S0200.", [ref]), "Doe ([[Doe 2026|Doe 2026, p. 4]]) and S0200.")
            self.assertEqual(concept_notes._cite_bare_ids("Doe (doe2026_x:S0127).", [ref]), "Doe ([[Doe 2026|Doe 2026, p. 4]]).")
            self.assertEqual(concept_notes._cite_bare_ids("Doe (S0127).", [ref, other]), "Doe (S0127).")   # ambiguous: left alone

    def test_catalogue_number_equal_to_a_sentence_id_stays_and_summary_skips_an_id(self):
        ref = {"paper_id": "doe2026_x", "sentence_id": "doe2026_x:S1888"}
        with patch.object(concept_notes, "_ref_cite", return_value="[[Doe 2026|Doe 2026, p. 4]]"):
            self.assertEqual(concept_notes._cite_bare_ids("Drug (Cat. No. S1888) and S1888.", [ref]),
                             "Drug (Cat. No. S1888) and [[Doe 2026|Doe 2026, p. 4]].")
        profile = {"card": {"statements": [{"field": "definition", "text": "Compaction (S0127) is a process of embryos."},
                                           {"field": "definition", "text": "Compaction is a process of embryos."}]}}
        self.assertEqual(concept_notes._source_summary({"name": "Compaction"}, [], profile), "Compaction is a process of embryos.")


class HomonymTests(unittest.TestCase):
    def test_blocked_only_in_the_other_sense(self):
        self.assertFalse(presentation.matches_term("Gene Ontology (GO) analysis was used.", "GO"))
        self.assertFalse(presentation.matches_term("GO terms were enriched.", "GO"))
        self.assertFalse(presentation.matches_term("growing oocytes (GO) were stained.", "GO"))
        self.assertTrue(presentation.matches_term("graphene oxide (GO) NPs were mixed in.", "GO"))
        self.assertFalse(presentation.matches_term("The 3-D reconstruction used SIRT.", "SIRT"))
        self.assertTrue(presentation.matches_term("SIRT1 and SIRT overexpression.", "SIRT"))
        self.assertFalse(presentation.matches_term("Only the gel filtration step was run.", "Filtration"))
        self.assertTrue(presentation.matches_term("Filtration removed debris.", "Filtration"))
        self.assertFalse(presentation.matches_term("The implantation of MSCs benefits.", "Implantation"))
        self.assertTrue(presentation.matches_term("Embryo implantation rate rose.", "Implantation"))

    def test_concept_linker_and_notes_follow(self):
        record = {"concept_id": "local:go", "name": "GO", "type": "molecule", "synonyms": []}
        pattern = concepts._compiled_patterns(concepts.Registry([record]))["local:go"][0]
        self.assertFalse(concepts._pattern_matches("Gene Ontology (GO) analysis", pattern, record))
        self.assertTrue(concepts._pattern_matches("graphene oxide (GO) NPs", pattern, record))
        registry = concepts.Registry([record])
        self.assertEqual(concept_notes._text_concept_ids({"quote": "GO terms were enriched."}, {"local:go"}, registry), set())
        self.assertEqual(concept_notes._text_concept_ids({"quote": "graphene oxide (GO) NPs."}, {"local:go"}, registry), {"local:go"})


class AbbreviationTests(unittest.TestCase):
    def test_initialism(self):
        self.assertTrue(concepts._initialism("IL-1R2", "IL-1 receptor 2"))
        self.assertTrue(concepts._initialism("DNMT", "DNA methyltransferase"))
        self.assertFalse(concepts._initialism("ROS", "Oxygen species"))
        self.assertFalse(concepts._initialism("A", "Apple"))

    def test_pairs_come_from_paper_abbreviation_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "p1"
            folder.mkdir(parents=True)
            (folder / "structure.json").write_text(json.dumps({"abbreviations": [
                {"short": "IL-1R2", "definitions": [{"text": "IL-1 receptor 2"}]},
                {"short": "CPA", "definitions": [{"text": "Zebra"}]},       # not an initialism
                {"short": "ROS", "definitions": [{"text": "reactive oxygen species"}]}]}), encoding="utf-8")
            known = {concepts._key(t) for t in ("IL-1R2", "IL-1 receptor 2", "CPA", "Zebra")}  # ROS pair: not registry terms
            with patch.object(config, "PAPERS_DIR", Path(tmp)):
                self.assertEqual(concepts.abbreviation_pairs(known),
                                 [(concepts._key("IL-1R2"), concepts._key("IL-1 receptor 2"))])


class NodeCardTaskScopeTests(unittest.TestCase):
    setUp = base.NodeCardTests.setUp
    tearDown = base.NodeCardTests.tearDown
    _sentence = staticmethod(base.NodeCardTests._sentence)
    _write_paper = base.NodeCardTests._write_paper

    def _sources(self):
        return {row["id"] for row in node_cards.task(self.con, "v", "EG")["sentences"]}

    def test_unit_naming_the_node_brings_its_abbreviation_sentence(self):
        rows = db.read_jsonl(db.paper_dir("p") / "sentences.jsonl")
        db.write_jsonl(db.paper_dir("p") / "sentences.jsonl",
                       [*rows, self._sentence("p:S0005", 5, "Secretion of E2 was lower.", "results", "res2")])
        units = list(db.read_jsonl(db.paper_dir("p") / "evidence.jsonl"))
        units.append({"unit_id": "p:E2", "seq": 2, "unit_type": "result", "status": "ok",
                      "statement": "EG secretion was lower.", "quote": "x", "sentence_ids": ["p:S0005"]})
        db.write_jsonl(db.paper_dir("p") / "evidence.jsonl", units)
        self.assertIn("p:S0005", self._sources())

    def test_paper_reached_only_through_a_child_node_still_has_sentences(self):
        self.records.append({"concept_id": "local:child", "name": "Childterm", "type": "molecule",
                             "synonyms": [], "parents": ["local:eg"]})
        self.registry.by_id["local:child"] = self.records[-1]
        self._write_paper("q", "primary_research",
                          [self._sentence("q:S0001", 1, "Childterm was measured.", "results", "para")], [])
        self.con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES('q','v')")
        self.con.execute("INSERT INTO concept_links VALUES('local:child','q','mentioned',1,'results','q:S0001')")
        self.con.commit()
        self.assertIn("q:S0001", self._sources())


class PendingSpeedTests(unittest.TestCase):
    def test_paper_rows_parse_each_file_once_inside_a_scope(self):
        calls = []
        structure._ROWS_CACHE.clear()
        real = db.read_jsonl
        with patch.object(db, "read_jsonl", side_effect=lambda path: calls.append(path) or real(path)), \
                patch.object(structure, "paper_read_stamp", return_value="stamp"):
            structure.paper_rows("nopaper", "sentences.jsonl")
            structure.paper_rows("nopaper", "sentences.jsonl")
            self.assertEqual(len(calls), 2)  # no scope: every call reads
            calls.clear()
            with structure.stamp_scope():
                structure.paper_rows("nopaper", "sentences.jsonl")
                structure.paper_rows("nopaper", "sentences.jsonl")
            self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()


class Review14Tests(unittest.TestCase):
    def test_acronym_defined_differently_in_another_paper_is_not_merged(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, text in (("p1", "cryoprotective agent"), ("p2", "cyclophosphamide"),
                              ("p3", "In vitro fertilization"), ("p4", "in vitro fertilisation")):
                short = "CPA" if pid in ("p1", "p2") else "IVF"
                (Path(tmp) / pid).mkdir()
                (Path(tmp) / pid / "structure.json").write_text(json.dumps({"abbreviations": [
                    {"short": short, "definitions": [{"text": text}]}]}), encoding="utf-8")
            known = {concepts._key(t) for t in ("CPA", "cryoprotective agent", "cyclophosphamide",
                                                "IVF", "In vitro fertilization")}
            with patch.object(config, "PAPERS_DIR", Path(tmp)):
                pairs = concepts.abbreviation_pairs(known)
            self.assertEqual(pairs, [(concepts._key("IVF"), concepts._key("In vitro fertilization"))])

    def test_shipped_homonyms_pca_and_vitrification(self):
        blocked = presentation.homonym_blocked
        self.assertTrue(blocked("PCA", "Groups were compared by principal component analysis (PCA)."))
        self.assertTrue(blocked("PCA", "PCA analysis of the sample ratio data demonstrates three groups."))
        self.assertFalse(blocked("PCA", "Passive cutaneous anaphylaxis (PCA) was induced in mice."))
        self.assertTrue(blocked("Vitrification", "The glass transition temperature of the polymer was measured."))
        self.assertTrue(blocked("Vitrification", "The glass transition of polymers (eg, polystyrene, gly-rich films) was measured."))  # ND-08: eg / gly are not EG / GLY
        self.assertFalse(blocked("PCA", "PC12 cells were sensitised before passive cutaneous anaphylaxis (PCA)."))
        for text in ("The glass transition temperature (Tg) of the vitrification solution was -120 C.",
                     "A 150 g/L glucose solution exhibits a lower glass transition temperature than EG and GLY.",
                     "Oocytes were vitrified in a cryoprotectant solution.",
                     "The glass transition of the EG solution was measured.", "The glass transition of 30% GLY was measured."):
            self.assertFalse(blocked("Vitrification", text), text)

    def test_homonym_file_missing_or_changed_inside_one_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(config, "CONFIG_DIR", Path(tmp)):
                self.assertFalse(presentation.homonym_blocked("GO", "Gene Ontology analysis"))
                (Path(tmp) / "homonyms.yaml").write_text("GO:\n  - 'gene ontology'\n", encoding="utf-8")
                self.assertTrue(presentation.homonym_blocked("GO", "Gene Ontology analysis"))
                (Path(tmp) / "homonyms.yaml").write_text("GO:\n  - 'kegg'\n", encoding="utf-8")
                import os
                st = (Path(tmp) / "homonyms.yaml").stat()
                os.utime(Path(tmp) / "homonyms.yaml", ns=(st.st_atime_ns, st.st_mtime_ns + 10**9))
                self.assertFalse(presentation.homonym_blocked("GO", "Gene Ontology analysis"))


class CappedTaskOrderTests(unittest.TestCase):
    def test_results_before_methods_when_a_task_is_capped(self):
        rows = [{"id": "S1", "seq": 1, "section": "methods"}, {"id": "S9", "seq": 9, "section": "results"},
                {"id": "S5", "seq": 5, "section": "discussion"}]
        self.assertEqual([r["id"] for r in sorted(rows, key=node_cards._capped_order)], ["S9", "S5", "S1"])


class VaultThresholdTests(unittest.TestCase):
    def test_node_needs_evidence_or_three_mentions_in_the_vault(self):
        shown = concept_notes.shown_in_vault
        self.assertFalse(shown([{"n_sentences": 1}, {"n_sentences": 1}], []))  # passing mentions in other-topic papers
        self.assertTrue(shown({"p1": [{"n_sentences": 2}], "p2": [{"n_sentences": 1}]}, []))
        self.assertTrue(shown([{"n_sentences": 1}], [{"unit_id": "p:E1"}]))  # an evidence unit always counts


class ConflictTagTests(unittest.TestCase):
    def test_conflict_tag_follows_has_conflict_and_keeps_user_tags(self):
        fm = {"tags": ["substance", "my-tag"], "has_conflict": True}
        concept_notes._conflict_tag(fm)
        self.assertEqual(fm["tags"], ["substance", "my-tag", "conflict"])
        fm["has_conflict"] = False
        concept_notes._conflict_tag(fm)
        self.assertEqual(fm["tags"], ["substance", "my-tag"])
