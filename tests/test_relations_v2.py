"""Offline relation proposals: measured concepts, no-change outcomes, and review safety."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, db, relations, concepts


class _Registry:
    def __init__(self, *records):
        self.records = list(records)

    def lookup(self, value):
        value = str(value).casefold()
        return next((r for r in self.records if r["name"].casefold() == value), None)


class RelationsV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="relations-v2-")
        self.root = Path(self.temp.name)
        self.saved = {name: getattr(config, name) for name in
                      ("ROOT", "CONFIG_DIR", "STORE", "DB_PATH", "PAPERS_DIR", "LOGS_DIR", "CACHE_DIR", "VAULTS_DIR", "REVIEW_PENDING")}
        paths = {
            "ROOT": self.root, "CONFIG_DIR": self.root / "config", "STORE": self.root / "knowledge_store",
            "DB_PATH": self.root / "knowledge_store/library.db", "PAPERS_DIR": self.root / "knowledge_store/papers",
            "LOGS_DIR": self.root / "knowledge_store/logs", "CACHE_DIR": self.root / "knowledge_store/cache",
            "VAULTS_DIR": self.root / "vaults", "REVIEW_PENDING": self.root / "review/pending",
        }
        for name, value in paths.items():
            setattr(config, name, value)
        config.CONFIG_DIR.mkdir()
        config.STORE.mkdir()
        config.PAPERS_DIR.mkdir()
        (config.VAULTS_DIR / "topic_a").mkdir(parents=True)
        self.con = db.connect()
        self.relation_path = config.STORE / "relations.jsonl"
        self.groups_path = config.STORE / "measure_groups.json"
        self.registry = _Registry({"concept_id": "local:h3k9me3", "name": "H3K9me3",
                                  "type": "biological_entity", "synonyms": ["H3K9me3"]})
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.path_patch = patch.object(relations, "RELATIONS", self.relation_path)
        self.path_patch.start()
        self.groups_patch = patch.object(relations, "MEASURE_GROUPS", self.groups_path)
        self.groups_patch.start()

    def tearDown(self):
        self.groups_patch.stop()
        self.path_patch.stop()
        self.registry_patch.stop()
        self.con.close()
        for name, value in self.saved.items():
            setattr(config, name, value)
        self.temp.cleanup()

    def add_paper(self, paper_id, units, vault="topic_a"):
        folder = config.PAPERS_DIR / paper_id
        folder.mkdir(parents=True)
        db.write_json(folder / "metadata.json", {"paper_id": paper_id, "title": paper_id,
                                                   "year": 2025, "vaults": [vault]})
        db.write_jsonl(folder / "evidence.jsonl", units)

    @staticmethod
    def unit(paper_id, suffix, direction, measure="H3K9me3 fluorescence intensity", **extra):
        unit = {"unit_id": f"{paper_id}:{suffix}", "paper_id": paper_id, "unit_type": "result", "status": "ok",
                "statement": extra.pop("statement", f"The {measure} showed a {direction} after vitrification."),
                "quote": extra.pop("quote", "The result was reported in the Results section."),
                "sentence_ids": [f"{paper_id}:S0001"],
                "outcome": {"measure": measure, "direction": direction}}
        unit.update(extra)
        return unit

    def relations(self):
        return list(db.read_jsonl(self.relation_path) or [])

    def test_includes_no_change_and_mixed_units(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="H3K9me3 level")])
        self.add_paper("paper_c", [self.unit("paper_c", "E1", "mixed", measure="H3K9me3 abundance")])
        rows = relations._unit_rows("topic_a")
        self.assertEqual({row[1]["outcome"]["direction"] for row in rows}, {"increase", "no_change", "mixed"})

    def test_shared_concept_effect_vs_no_effect_warns_about_non_significance(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="H3K9me3 level")])
        result = relations.propose(self.con, "topic_a")
        records = self.relations()
        self.assertEqual(result["added"], 1)
        self.assertEqual(records[0]["type"], "effect_vs_no_effect")
        self.assertEqual(records[0]["match_basis"], "shared_concept:local:h3k9me3")
        self.assertEqual(records[0]["shared_concept_name"], "H3K9me3")
        self.assertEqual(records[0]["compared_measures"], ["H3K9me3 fluorescence intensity", "H3K9me3 level"])
        self.assertIn("Non-significance is not evidence of no effect", records[0]["rationale"])
        self.assertIn("Non-significance is not evidence of no effect",
                      json.dumps(records[0]["differences"], ensure_ascii=False))

    def test_generic_registry_concepts_do_not_match(self):
        self.registry = _Registry(
            {"concept_id": "local:embryo", "name": "Embryo", "type": "biological_entity", "synonyms": []},
            {"concept_id": "local:dna", "name": "DNA", "type": "molecule", "synonyms": []},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="Embryo")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="Embryo")])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relations(), [])

    def test_method_concept_is_never_a_match_basis(self):
        self.registry = _Registry(
            {"concept_id": "local:rna_seq", "name": "RNA-seq", "type": "method", "synonyms": []},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="RNA-seq")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="RNA-seq")])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relations(), [])

    def test_statement_only_concept_does_not_match(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="fluorescence intensity",
                                               statement="H3K9me3 was increased in this group.")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="fluorescence level",
                                               statement="H3K9me3 did not change in this group.")])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relations(), [])

    def test_aggregate_statement_clause_matches_specific_measure(self):
        statement = ("H3K9me3 did not significantly change, whereas H4K16ac, H4K12ac, "
                     "H3K4me3, and H3K4me2 signals increased in vitrified blastocysts.")
        self.add_paper("alpha_2026", [self.unit(
            "alpha_2026", "E0046", "increase",
            measure="H3K9me3 immunofluorescence intensity")])
        self.add_paper("beta_2025", [self.unit(
            "beta_2025", "E0036", "mixed", measure="Histone methylation and acetylation",
            statement=statement)])

        result = relations.propose(self.con, "topic_a")
        record = self.relations()[0]
        self.assertEqual(result["added"], 1)
        self.assertEqual(record["type"], "effect_vs_no_effect")
        self.assertEqual(record["match_basis"], "shared_concept_clause:local:h3k9me3")
        self.assertEqual(record["clause"], "H3K9me3 did not significantly change")
        self.assertEqual(record["comparability"],
                         "entity named in a statement clause; measure granularity differs — review")
        self.assertEqual(record["directions"], ["increase", "no_change"])

    def test_different_entity_clause_does_not_match_h3k9me3(self):
        self.registry = _Registry(
            {"concept_id": "local:h3k9me3", "name": "H3K9me3", "type": "biological_entity", "synonyms": []},
            {"concept_id": "local:h3k4me3", "name": "H3K4me3", "type": "biological_entity", "synonyms": []},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("alpha_2026", [self.unit(
            "alpha_2026", "E0046", "increase",
            measure="H3K9me3 immunofluorescence intensity")])
        self.add_paper("other_2025", [self.unit(
            "other_2025", "E1", "mixed", measure="Histone methylation and acetylation",
            statement="H3K4me3 increased in vitrified blastocysts.")])

        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relations(), [])

    def test_withdrawn_relation_is_revived_with_history(self):
        self.add_paper("revive_a", [self.unit("revive_a", "E1", "increase", measure="H3K9me3 intensity")])
        self.add_paper("revive_b", [self.unit("revive_b", "E1", "no_change", measure="H3K9me3 level")])
        relations.propose(self.con, "topic_a")
        record = self.relations()[0]
        relation_id = record["relation_id"]

        changed = self.unit("revive_b", "E1", "no_change", measure="signal",
                            statement="The signal did not change after vitrification.")
        db.write_jsonl(config.PAPERS_DIR / "revive_b" / "evidence.jsonl", [changed])
        withdrawn = relations.propose(self.con, "topic_a")
        self.assertEqual(withdrawn["withdrawn"], 1)
        self.assertEqual(self.relations()[0]["status"], "withdrawn_by_rule")

        revived_unit = self.unit("revive_b", "E1", "no_change", measure="signal",
                                 statement="H3K9me3 did not significantly change after vitrification.")
        db.write_jsonl(config.PAPERS_DIR / "revive_b" / "evidence.jsonl", [revived_unit])
        revived = relations.propose(self.con, "topic_a")
        record = {row["relation_id"]: row for row in self.relations()}[relation_id]
        self.assertEqual(revived["revived"], 1)
        self.assertEqual(record["status"], "ai_proposed")
        self.assertEqual(record["match_basis"], "shared_concept_clause:local:h3k9me3")
        self.assertTrue(any(item["event"] == "revived" for item in record["history"]))

    def test_mtdna_without_measure_or_clause_is_not_matched(self):
        self.registry = _Registry(
            {"concept_id": "local:h3k9me3", "name": "H3K9me3", "type": "biological_entity", "synonyms": []},
            {"concept_id": "local:mitochondrial-dna", "name": "mitochondrial DNA", "type": "molecule", "synonyms": ["mtDNA"]},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("beta_2025", [self.unit(
            "beta_2025", "E0031", "increase", measure="mitochondrial DNA copy number")])
        self.add_paper("alpha_2026", [self.unit(
            "alpha_2026", "E0046", "increase",
            measure="H3K9me3 immunofluorescence intensity",
            statement="H3K9me3 increased in vitrified blastocysts.")])

        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relations(), [])

    def test_most_specific_registry_concept_wins_over_parent(self):
        self.registry = _Registry(
            {"concept_id": "local:h3", "name": "H3", "type": "biological_entity", "synonyms": [],
             "parents": []},
            {"concept_id": "local:h3k9me3", "name": "H3K9me3", "type": "biological_entity", "synonyms": [],
             "parents": ["local:h3"]},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        # not "H3K9me3 and H3 ...": a bundled measure gets no unit-level concept (RL-03)
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="H3K9me3 H3 content")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="H3K9me3 H3 level")])
        result = relations.propose(self.con, "topic_a")
        self.assertEqual(result["added"], 1)
        self.assertEqual(self.relations()[0]["match_basis"], "shared_concept:local:h3k9me3")
        self.assertEqual(self.relations()[0]["shared_concept_name"], "H3K9me3")

    def test_same_paper_never_pairs_with_itself(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase"),
                                    self.unit("paper_a", "E2", "no_change", measure="H3K9me3 level")])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)

    def test_human_decisions_are_preserved_and_rerun_is_idempotent(self):
        self.registry = _Registry(
            {"concept_id": "local:h3k9me3", "name": "H3K9me3", "type": "biological_entity", "synonyms": []},
            {"concept_id": "local:ros", "name": "ROS", "type": "molecule", "synonyms": []},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change")])
        self.add_paper("paper_c", [self.unit("paper_c", "E1", "increase", measure="ROS level")])
        self.add_paper("paper_d", [self.unit("paper_d", "E1", "no_change", measure="ROS content")])
        first = relations.propose(self.con, "topic_a")
        self.assertEqual(first["added"], 2)
        records = self.relations()
        records[0]["status"] = "human_confirmed"
        records[1]["status"] = "rejected"
        db.write_jsonl(self.relation_path, records)
        rerun = relations.propose(self.con, "topic_a")
        self.assertEqual(rerun["added"], 0)
        self.assertEqual(rerun["upgraded"], 0)
        self.assertEqual({record["status"] for record in self.relations()}, {"human_confirmed", "rejected"})
        snapshot = self.relation_path.read_bytes()
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)
        self.assertEqual(self.relation_path.read_bytes(), snapshot)

    def test_stale_shared_concept_is_withdrawn_but_human_relation_is_kept(self):
        self.add_paper("stale_a", [self.unit("stale_a", "E1", "increase", measure="signal",
                                                statement="H3K9me3 increased.")])
        self.add_paper("stale_b", [self.unit("stale_b", "E1", "no_change", measure="level",
                                                statement="H3K9me3 did not change.")])
        self.add_paper("human_a", [self.unit("human_a", "E1", "increase", measure="signal",
                                                statement="H3K9me3 increased.")])
        self.add_paper("human_b", [self.unit("human_b", "E1", "no_change", measure="level",
                                                statement="H3K9me3 did not change.")])
        stale = {"relation_id": relations._relation_id("stale_a:E1", "stale_b:E1", "effect_vs_no_effect"),
                 "from_unit": "stale_a:E1", "to_unit": "stale_b:E1", "type": "effect_vs_no_effect",
                 "status": "ai_proposed", "match_basis": "shared_concept:local:h3k9me3"}
        human = {"relation_id": relations._relation_id("human_a:E1", "human_b:E1", "effect_vs_no_effect"),
                 "from_unit": "human_a:E1", "to_unit": "human_b:E1", "type": "effect_vs_no_effect",
                 "status": "human_confirmed", "match_basis": "shared_concept:local:h3k9me3",
                 "reviewed_note": "keep this human decision"}
        db.write_jsonl(self.relation_path, [stale, human])

        result = relations.propose(self.con, "topic_a")
        records = {record["relation_id"]: record for record in self.relations()}
        self.assertEqual(result["withdrawn"], 1)
        self.assertEqual(records[stale["relation_id"]]["status"], "withdrawn_by_rule")
        self.assertIn("withdrawn_reason", records[stale["relation_id"]])
        self.assertIn("withdrawn_at", records[stale["relation_id"]])
        self.assertEqual(records[human["relation_id"]]["status"], "human_confirmed")
        self.assertEqual(records[human["relation_id"]]["reviewed_note"], "keep this human decision")

    def test_cap_is_deterministic_and_reported(self):
        for index in range(31):
            paper_id = f"inc_{index:02d}"
            self.add_paper(paper_id, [self.unit(paper_id, "E1", "increase")])
        self.add_paper("no_change", [self.unit("no_change", "E1", "no_change")])
        result = relations.propose(self.con, "topic_a")
        self.assertEqual(len(self.relations()), relations.MAX_PAIRS_PER_CONCEPT)
        self.assertEqual(result["capped"][0]["skipped"], 1)
        self.assertEqual(result["capped"][0]["limit"], relations.MAX_PAIRS_PER_CONCEPT)

    def test_contradicts_needs_the_same_named_agent_and_other_agents_never_pair(self):
        self.add_paper("same", [self.unit("same", "E1", "increase", agent="CPA",
                                             intervention={"agent": "CPA"}, model={"cell_type": "bovine embryo"})])
        self.add_paper("same_2", [self.unit("same_2", "E1", "decrease", agent="CPA",
                                               intervention={"agent": "CPA"}, model={"cell_type": "bovine embryo"})])
        self.add_paper("different", [self.unit("different", "E1", "decrease", agent="DMSO",
                                                 intervention={"agent": "DMSO"}, model={"cell_type": "bovine embryo"})])
        result = relations.propose(self.con, "topic_a")
        types = {tuple(sorted((record["from_unit"].split(":")[0], record["to_unit"].split(":")[0]))): record["type"]
                 for record in self.relations()}
        self.assertEqual(types, {("same", "same_2"): "contradicts"})  # a matching cell type is not the agent
        self.assertEqual(result["by_type"]["contradicts"], 1)

    def test_named_agent_against_an_empty_agent_is_no_pair(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", intervention={"agent": "Resveratrol"})])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "decrease")])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)

    def test_two_empty_agents_need_a_shared_text_intervention_and_give_direction_differs(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "decrease")])
        self.add_paper("paper_c", [self.unit("paper_c", "E1", "decrease", statement="H3K9me3 intensity fell in mice.")])
        relations.propose(self.con, "topic_a")
        self.assertEqual([(r["type"], r["to_unit"]) for r in self.relations()], [("direction_differs", "paper_b:E1")])

    def test_different_measured_outcome_or_species_measure_is_no_pair(self):
        self.registry = _Registry(*self.registry.records,
                                  {"concept_id": "local:human", "name": "Human", "type": "species", "synonyms": []})
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="H3K9me3 intensity")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "decrease", measure="H3K9me3 foci number")])
        self.add_paper("paper_c", [self.unit("paper_c", "E1", "increase", measure="Human")])
        self.add_paper("paper_d", [self.unit("paper_d", "E1", "decrease", measure="Human")])
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)

    def test_bundled_measure_pairs_only_through_its_clause_not_as_a_whole(self):
        measure = "H3K9me3 and H3K4me3 abundance"
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "decrease", measure=measure,
                                               statement="H3K9me3 decreased after vitrification.")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "increase", measure="H3K9me3 level")])
        self.add_paper("paper_c", [self.unit("paper_c", "E1", "decrease", measure=measure)])  # no clause of its own
        relations.propose(self.con, "topic_a")
        rows = self.relations()
        self.assertEqual([(r["match_basis"], r["from_unit"], r["to_unit"]) for r in rows],
                         [("shared_concept_clause:local:h3k9me3", "paper_a:E1", "paper_b:E1")])
        self.assertEqual(rows[0]["compared_measures"], ["H3K9me3", "H3K9me3 level"])  # the clause side names the entity only

    def test_clause_with_two_directions_gives_no_direction_and_up_down_regulated_are_read(self):
        def clause(statement, **extra):
            row = ("p", self.unit("p", "E1", "mixed", measure="Histone marks", statement=statement, **extra), {})
            return relations._clause_matches(row, self.registry, {})
        self.assertEqual(clause("NAC reduced H3K9me3 and increased cell number in vitrified blastocysts."), {})
        found = clause("H3K9me3 was up-regulated in vitrified blastocysts.")
        self.assertEqual(found["local:h3k9me3"]["entity_direction"], "increase")
        found = clause("H3K9me3 was downregulated in vitrified blastocysts.")
        self.assertEqual(found["local:h3k9me3"]["entity_direction"], "decrease")

    def clause_directions(self, statement, measure="Histone marks", direction="mixed", **extra):
        row = ("p", self.unit("p", "E1", direction, measure=measure, statement=statement, **extra), {})
        found = relations._clause_matches(row, self.registry, {})
        return {v["shared_concept_name"]: v["entity_direction"] for v in found.values()}

    def concept(self, name, kind="concept"):
        self.registry = _Registry(*self.registry.records, {"concept_id": "local:" + name.casefold().replace(" ", "_"),
                                                           "name": name, "type": kind, "synonyms": []})

    def test_clause_gives_no_direction_to_enrichment_and_baseline_text(self):  # RL-03 rule A
        self.concept("GO", "molecule")
        self.concept("Cell migration")
        self.concept("Abc12")
        self.concept("Lining thickness")
        self.assertEqual(self.clause_directions(
            "GO function and KEGG pathway tests (enrichment analyses) showed that the up-regulated DEGs sat in x."), {})
        self.assertEqual(self.clause_directions("Cell migration were upregulated.", measure="Gene Ontology enrichment"), {})
        self.assertEqual(self.clause_directions(
            "Baseline characteristics were similar across arms, except for lining thickness, which was a little lower."), {})
        self.assertEqual(self.clause_directions("Abc12 was down-regulated in treated samples.",
                                                measure="Pathway enrichment and gene expression"), {"Abc12": "decrease"})

    def test_bare_enrichment_is_not_a_non_outcome_word(self):  # RL-03 rule A, researcher D-PENDING-22
        self.assertEqual(self.clause_directions("ChIP showed enrichment of H3K9me3 at promoters, which increased."),
                         {"H3K9me3": "increase"})
        for text in ("GO enrichment analysis showed H3K9me3 increased.", "KEGG enrichment showed H3K9me3 increased.",
                     "GO enrichment showed H3K9me3 increased.", "Pathway enrichment-analysis showed H3K9me3 increased."):
            self.assertEqual(self.clause_directions(text), {}, text)  # last two: researcher "Thêm" 2026-10-20

    def test_clause_skips_modifier_spans(self):  # RL-03 rule B
        self.concept("m^6A", "molecule")
        self.concept("Wxy3")
        self.concept("Wxy14")
        self.concept("Pyroptosis")
        self.concept("Ins2")
        self.concept("Ctnna3")
        self.assertEqual(self.clause_directions("Transcript levels of Wxy3 and Wxy14 (RNA m^6A reader) were down-regulated."),
                         {"Wxy3": "decrease", "Wxy14": "decrease"})
        self.assertEqual(self.clause_directions(
            "RT-qPCR confirmed five pyroptosis-related genes, and Ins2 and Ctnna3 were down-regulated."),
                         {"Ins2": "decrease", "Ctnna3": "decrease"})

    def test_an_isoform_suffix_is_not_a_modifier(self):  # RL-03 rule B, review of c418a4c
        for name in ("NGF", "PDGF", "Muse cell", "Ins2"):
            self.concept(name)
        self.assertEqual(self.clause_directions("The level of PDGF-BB and NGF-β rose in low-oxygen cultures."),
                         {"PDGF": "increase", "NGF": "increase"})
        self.assertEqual(self.clause_directions("Muse cell-transplanted animals showed higher Ins2-related scores."), {})

    def test_clause_skips_a_direction_that_belongs_to_another_predicate(self):  # RL-03 rule C
        self.concept("Implantation")
        self.concept("Body weight")
        self.concept("Banding")
        self.concept("HP1alpha")
        self.assertEqual(self.clause_directions(
            "After adjusting for body weight, both outcomes remained robust: the implantation rate increased."),
                         {"Implantation": "increase"})
        self.assertEqual(self.clause_directions(
            "Banding was impaired, and nuclear deposition of HP1alpha was reduced."),
                         {"HP1alpha": "decrease"})

    def test_clause_readings_that_must_stay(self):
        for name in ("H3K4me2", "H3K4me3", "H4K12ac", "H4K16ac", "mPTP opening", "mtDNA copy number",
                     "Mitochondrial membrane potential", "Inner cell mass", "Trophectoderm", "Clinical pregnancy"):
            self.concept(name)
        read = self.clause_directions
        self.assertEqual(read("H4K16ac, H4K12ac, H3K4me3, and H3K4me2 signals increased."),
                         dict.fromkeys(("H3K4me2", "H3K4me3", "H4K12ac", "H4K16ac"), "increase"))
        self.assertEqual(read("H3K9me3 did not significantly change."), {"H3K9me3": "no_change"})
        self.assertEqual(read("mPTP opening and mtDNA copy number did not differ."),
                         {"mPTP opening": "no_change", "mtDNA copy number": "no_change"})
        self.assertEqual(read("Mitochondrial membrane potential also decreased."), {"Mitochondrial membrane potential": "decrease"})
        self.assertEqual(read("Both inner cell mass and trophectoderm counts were decreased."),
                         {"Inner cell mass": "decrease", "Trophectoderm": "decrease"})
        self.assertEqual(read("The chance of clinical pregnancy was over 2-fold higher."), {"Clinical pregnancy": "increase"})

    def test_the_units_own_agent_is_not_a_clause_entity(self):
        self.registry = _Registry(*self.registry.records, {"concept_id": "local:resveratrol", "name": "Resveratrol",
                                                           "type": "molecule", "synonyms": ["RSV"]})
        statement = "Resveratrol reduced H3K9me3 loss in vitrified blastocysts."
        row = lambda **extra: ("p", self.unit("p", "E1", "mixed", measure="Histone marks", statement=statement, **extra), {})
        self.assertIn("local:resveratrol", relations._clause_matches(row(), self.registry, {}))
        found = relations._clause_matches(row(intervention={"agent": "Resveratrol (RSV)"}), self.registry, {})
        self.assertNotIn("local:resveratrol", found)

    def test_h3k9me3_clause_pair_with_empty_agents_and_shared_vitrification_text_survives(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", measure="H3K9me3 immunofluorescence intensity")])
        self.add_paper("paper_b", [self.unit(
            "paper_b", "E1", "mixed", measure="Histone methylation and acetylation",
            statement="H3K9me3 did not significantly change in vitrified blastocysts.")])
        relations.propose(self.con, "topic_a")
        record, = self.relations()
        self.assertEqual((record["type"], record["directions"]), ("effect_vs_no_effect", ["increase", "no_change"]))
        self.assertEqual(record["compared_measures"], ["H3K9me3 immunofluorescence intensity", "H3K9me3"])

    def test_withdrawal_reason_names_the_failed_rule(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase", intervention={"agent": "Resveratrol"})])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "decrease")])
        # any match_basis, not only shared concepts
        old = {"relation_id": "old", "from_unit": "paper_a:E1", "to_unit": "paper_b:E1", "type": "contradicts",
               "status": "ai_proposed", "match_basis": "exact_measure", "vault": "topic_a"}
        db.write_jsonl(self.relation_path, [old])
        self.assertEqual(relations.propose(self.con, "topic_a")["withdrawn"], 1)
        row = {r["relation_id"]: r for r in self.relations()}["old"]
        self.assertEqual((row["status"], row["withdrawn_reason"]),
                         ("withdrawn_by_rule", "RL-02: different intervention agents ('Resveratrol' vs None)"))

    def test_a_stored_mixed_direction_row_is_withdrawn(self):
        # a mixed unit used to stay a unit-level candidate, so its stored row was never withdrawn
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "mixed", measure="ROS content")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "decrease", measure="ROS content")])
        old = {"relation_id": "old", "from_unit": "paper_a:E1", "to_unit": "paper_b:E1", "type": "direction_differs",
               "status": "ai_proposed", "match_basis": "exact_measure", "vault": "topic_a",
               "directions": ["mixed", "decrease"]}
        db.write_jsonl(self.relation_path, [old])
        result = relations.propose(self.con, "topic_a")
        row = {r["relation_id"]: r for r in self.relations()}["old"]
        self.assertEqual((result["added"], row["status"], row["withdrawn_reason"]),
                         (0, "withdrawn_by_rule", "RL-02: a mixed direction is not opposite"))

    def test_reduced_measure_blanks_an_overlapping_name_and_synonym(self):
        record = {"concept_id": "local:hmgn1", "name": "Nonhistone Chromosomal Protein HMG 14", "synonyms": ["HMG 14"]}
        quantity = {"level"}
        self.assertEqual(relations._reduced_measure("Nonhistone Chromosomal Protein HMG 14 level", record, quantity), "")
        self.assertTrue(relations._same_outcome({"match_basis": "shared_concept:local:hmgn1", "shared_concept_id": "local:hmgn1"},
                                                ["Nonhistone Chromosomal Protein HMG 14 level", "HMG 14 level"],
                                                {"local:hmgn1": record}))

    def _pair_rows(self):
        self.add_paper("paper_a", [self.unit("paper_a", "E1", "increase")])
        self.add_paper("paper_b", [self.unit("paper_b", "E1", "no_change", measure="H3K9me3 level")])
        relations.propose(self.con, "topic_a")
        live, = self.relations()
        later = {"relation_id": "f" * 16, "from_unit": "paper_a:E1", "to_unit": "paper_b:E1", "type": "direction_differs",
                 "status": "withdrawn_by_rule", "vault": "topic_a"}  # sorts after the live row
        return live, later

    def test_a_later_withdrawn_row_of_the_pair_neither_resets_nor_overrides_the_kept_row(self):
        live, later = self._pair_rows()
        live.update(created_at="2026-01-01T00:00:00", supersedes="old", history=[{"event": "revived"}])
        db.write_jsonl(self.relation_path, [live, later])
        snapshot = self.relation_path.read_bytes()
        self.assertEqual(relations.propose(self.con, "topic_a")["upgraded"], 0)
        self.assertEqual(self.relation_path.read_bytes(), snapshot)  # created_at, supersedes and history kept
        live.update(status="human_confirmed", reviewed_note="keep")
        db.write_jsonl(self.relation_path, [live, later])
        snapshot = self.relation_path.read_bytes()
        relations.propose(self.con, "topic_a")
        self.assertEqual(self.relation_path.read_bytes(), snapshot)  # RL-07: the human row is untouched

    def test_a_withdrawn_row_of_the_same_type_is_revived_despite_another_withdrawn_row(self):
        live, later = self._pair_rows()
        live.update(status="withdrawn_by_rule", withdrawn_reason="old rule")
        db.write_jsonl(self.relation_path, [live, later])
        result = relations.propose(self.con, "topic_a")
        row = {r["relation_id"]: r for r in self.relations()}[live["relation_id"]]
        self.assertEqual((result["revived"], result["upgraded"], row["status"], row["history"][-1]["event"]),
                         (1, 0, "ai_proposed", "revived"))

    def test_clause_entities_need_shared_context_and_vitrification_pair_survives(self):
        self.registry = _Registry(
            {"concept_id": "local:h3k9me3", "name": "H3K9me3", "type": "biological_entity", "synonyms": []},
            {"concept_id": "local:blastocyst", "name": "blastocyst", "type": "biological_entity", "synonyms": []},
        )
        self.registry_patch.stop()
        self.registry_patch = patch.object(relations.concepts, "load_registry", return_value=self.registry)
        self.registry_patch.start()
        self.add_paper("beta_2025", [self.unit(
            "beta_2025", "E0036", "mixed", measure="Histone methylation",
            statement="H3K9me3 did not significantly change in vitrified blastocysts." )])
        self.add_paper("gamma_2010", [self.unit(
            "gamma_2010", "E1", "increase", measure="H3K9me3 radial density",
            statement="H3K9me3 radial density was higher in mouse retina cell nuclei." )])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 0)

        self.add_paper("alpha_2026", [self.unit(
            "alpha_2026", "E0046", "increase",
            measure="H3K9me3 immunofluorescence intensity",
            statement="H3K9me3 increased after vitrification." )])
        self.assertEqual(relations.propose(self.con, "topic_a")["added"], 1)
        record = self.relations()[0]
        self.assertEqual(record["match_basis"], "shared_concept_clause:local:h3k9me3")


    # ---- G1 / S16-11 + N19: vault scope ------------------------------------------------------------------
    def make_vault(self, name):
        (config.VAULTS_DIR / name).mkdir(parents=True, exist_ok=True)
        (config.VAULTS_DIR / name / "Home.md").write_text("# Home\n", encoding="utf-8")

    def test_propose_of_one_vault_never_withdraws_another_vaults_relation(self):
        self.add_paper("m_a", [self.unit("m_a", "E1", "increase", measure="signal", statement="H3K9me3 increased.")],
                       vault="topic_b")
        self.add_paper("m_b", [self.unit("m_b", "E1", "decrease", measure="level", statement="H3K9me3 decreased.")],
                       vault="topic_b")
        other = {"relation_id": "keepme", "from_unit": "m_a:E1", "to_unit": "m_b:E1", "type": "contradicts",
                 "status": "ai_proposed", "match_basis": "shared_concept:local:h3k9me3", "vault": "topic_b"}
        db.write_jsonl(self.relation_path, [other])
        # topic_a has papers of its own, but not the pair above
        self.add_paper("v_a", [self.unit("v_a", "E1", "increase")])
        result = relations.propose(self.con, "topic_a")
        self.assertEqual(result["withdrawn"], 0)
        self.assertEqual({r["relation_id"]: r["status"] for r in self.relations()}["keepme"], "ai_proposed")
        # inside its own vault the same stale pair is still withdrawn (the rule itself is unchanged)
        self.assertEqual(relations.propose(self.con, "topic_b")["withdrawn"], 1)

    def test_conflict_relations_match_vault_strictly_and_backfill_fills_legacy_rows(self):
        self.add_paper("a", [self.unit("a", "E1", "increase")])
        self.add_paper("b", [self.unit("b", "E1", "decrease")])
        self.add_paper("x", [self.unit("x", "E1", "increase")], vault="topic_b")
        legacy = {"relation_id": "legacy", "from_unit": "a:E1", "to_unit": "b:E1", "type": "contradicts",
                  "status": "ai_proposed"}
        cross = {"relation_id": "cross", "from_unit": "a:E1", "to_unit": "x:E1", "type": "contradicts",
                 "status": "ai_proposed"}  # no shared vault: stays unassigned
        db.write_jsonl(self.relation_path, [legacy, cross])
        for vault in ("topic_a", "topic_b", "nonexistent"):  # rows without a vault belong to nobody
            self.assertEqual(relations.conflict_relations(vault), [])
        self.assertEqual(len(relations.conflict_relations()), 2)
        self.assertEqual(relations.backfill_vault(dry_run=True)["filled"], 1)
        self.assertNotIn("vault", self.relations()[0])
        report = relations.backfill_vault()
        self.assertEqual((report["filled"], report["ambiguous"]), (1, ["cross"]))
        rows = {r["relation_id"]: r for r in self.relations()}
        self.assertEqual(rows["legacy"]["vault"], "topic_a")
        self.assertNotIn("vault", rows["cross"])
        self.assertEqual([r["relation_id"] for r in relations.conflict_relations("topic_a")], ["legacy"])
        self.assertEqual(relations.conflict_relations("topic_b"), [])
        self.assertEqual(relations.backfill_vault()["filled"], 0)  # idempotent

    def test_library_wide_propose_writes_the_shared_vault_not_an_invisible_none(self):
        self.add_paper("m_a", [self.unit("m_a", "E1", "increase")], vault="topic_b")
        self.add_paper("m_b", [self.unit("m_b", "E1", "decrease")], vault="topic_b")
        relations.propose(self.con)  # `kb.py propose-relations` without --vault
        rows = self.relations()
        self.assertTrue(rows)
        self.assertEqual({r["vault"] for r in rows}, {"topic_b"})
        self.assertEqual(len(relations.conflict_relations("topic_b")), len(rows))

    def test_list_conflicts_and_gap_candidates_reject_unknown_vault(self):
        from kbsys import gaps, mcp_server
        self.make_vault("topic_a")
        self.make_vault("topic_b")
        db.write_jsonl(self.relation_path, [{"relation_id": "r", "from_unit": "a:E1", "to_unit": "b:E1",
                                             "type": "contradicts", "status": "ai_proposed", "vault": "topic_b"}])
        db.write_jsonl(gaps.path(), [{"gap_id": "g1", "vault": "topic_b", "status": "needs_review"},
                                     {"gap_id": "g2", "status": "needs_review"}])  # legacy row without vault: nobody's
        self.assertEqual(mcp_server._conflicts("topic_a", self.con), [])
        self.assertEqual([g["gap_id"] for g in gaps.list_candidates("topic_b")], ["g1"])
        self.assertEqual(gaps.list_candidates("topic_a"), [])
        for bad in ("nonexistent_vault", "../x", "", None, ["topic_b"]):
            with self.assertRaises(ValueError):
                mcp_server._conflicts(bad, self.con)
            with self.assertRaises(ValueError):
                gaps.list_candidates(bad)


class SpeciesRelationTests(unittest.TestCase):
    def test_species_only_existing_proposal_stays_live(self):  # RL-02, D-PENDING-22 (OD-10)
        registry = _Registry({"concept_id": "h3", "name": "H3K9me3", "type": "molecule", "synonyms": []})
        rows = [
            ("a", {"unit_id": "a:E1", "statement": "H3K9me3 changed.", "sentence_ids": [],
                   "model": {"species": "mouse", "stage": "MII"}, "intervention": {"agent": "vitrification"},
                   "outcome": {"measure": "H3K9me3 level", "direction": "increase"}}, {}),
            ("b", {"unit_id": "b:E1", "statement": "H3K9me3 changed.", "sentence_ids": [],
                   "model": {"species": "bovine", "stage": "MII"}, "intervention": {"agent": "vitrification"},
                   "outcome": {"measure": "H3K9me3 level", "direction": "decrease"}}, {}),
        ]
        old = {"relation_id": relations._relation_id("a:E1", "b:E1", "contradicts"), "from_unit": "a:E1", "to_unit": "b:E1",
               "type": "contradicts", "match_basis": "shared_concept:h3",
               "status": "ai_proposed"}
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            relation_path = root / "relations.jsonl"
            db.write_jsonl(relation_path, [old])
            with patch.object(relations, "RELATIONS", relation_path), \
                 patch.object(relations, "_unit_rows", return_value=rows), \
                 patch.object(relations, "load_measure_groups", return_value={}), \
                 patch.object(concepts, "load_registry", return_value=registry), \
                 patch.object(config, "STORE", root):
                result = relations.propose(object())
            saved_rows = list(db.read_jsonl(relation_path))
        self.assertEqual(result["withdrawn"], 0)
        self.assertEqual(len(saved_rows), 1)
        saved = saved_rows[0]
        self.assertEqual(saved["status"], "ai_proposed")
        self.assertNotIn("withdrawn_reason", saved)

    def test_species_only_pairs_are_candidates_and_h3k9me3_pair_is_kept(self):  # RL-02, D-PENDING-22
        registry = _Registry({"concept_id": "h3", "name": "H3K9me3", "type": "molecule", "synonyms": []})

        def row(pid, eid, direction, species, agent="vitrification"):
            unit = {"unit_id": f"{pid}:{eid}", "statement": "H3K9me3 changed.",
                    "sentence_ids": [], "model": {"species": species, "stage": "MII"},
                    "intervention": {"agent": agent},
                    "outcome": {"measure": "H3K9me3 level", "direction": direction}}
            return pid, unit, {}

        different_species = relations._candidate_records(
            [row("a", "E1", "increase", "mouse"), row("b", "E1", "decrease", "bovine")], {}, registry)
        self.assertEqual([{r["left"][1]["unit_id"], r["right"][1]["unit_id"]} for r in different_species],
                         [{"a:E1", "b:E1"}])
        kept = relations._candidate_records(
            [row("Alpha", "E0046", "increase", "bovine"),
             row("Beta", "E0036", "decrease", "bovine")], {}, registry)
        self.assertEqual(len(kept), 1)
        self.assertEqual({kept[0]["left"][1]["unit_id"], kept[0]["right"][1]["unit_id"]},
                         {"Alpha:E0046", "Beta:E0036"})
        incompatible = [
            row("a", "E1", "increase", "bovine"),
            row("b", "E1", "decrease", "bovine"),
        ]
        incompatible[0][1]["outcome"]["measure"] = "H3K9me3 level"
        incompatible[0][1]["outcome"]["readout"] = "fluorescence"
        incompatible[1][1]["outcome"]["measure"] = "H3K9me3 expression"
        incompatible[1][1]["outcome"]["readout"] = "RNA abundance"
        self.assertEqual(relations._candidate_records(incompatible, {}, registry), [])


class MeasureEntityRelationTests(unittest.TestCase):
    def test_species_word_in_two_unrelated_measures_is_no_conflict(self):
        registry = _Registry({"concept_id": "local:human", "name": "Human", "type": "species", "synonyms": []})

        def row(pid, measure, direction):
            return pid, {"unit_id": f"{pid}:E1", "statement": "x", "sentence_ids": [],
                         "model": {"species": "Human", "cell_type": "Muse cell"}, "intervention": {},
                         "outcome": {"measure": measure, "direction": direction}}, {}

        pairs = relations._candidate_records([row("a", "Density of human Golgi-positive cells", "increase"),
                                              row("b", "Human Mash1, MAP-2 and DCX expression", "no_change")],
                                             {}, registry)
        self.assertEqual(pairs, [])

    def test_context_word_is_not_a_measure_entity_and_scores_are_exposed(self):
        registry = _Registry({"concept_id": "local:blastocyst", "name": "blastocyst",
                              "type": "biological_entity", "synonyms": []})
        row = ("p", {"outcome": {"measure": "high-quality blastocyst frequency"}})
        self.assertEqual(relations._shared_concepts(row, registry, {}), [])
        left = {"outcome": {"measure": "H3K9me3 level"}}
        right = {"outcome": {"measure": "H3K9me3 intensity"}}
        self.assertEqual(relations._measure_match_score({"match_basis": "shared_concept:local:h3"}, left, right), 0.8)


if __name__ == "__main__":
    unittest.main(verbosity=2)
