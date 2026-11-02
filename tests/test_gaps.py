"""Candidate knowledge gaps (A11): deterministic generation, human decisions kept, MCP proposals."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concept_notes, concepts, config, db, gaps, mcp_server, relations

V = "vit"


class GapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="gaps-")
        root = Path(self.temp.name)
        self.saved = {n: getattr(config, n) for n in ("ROOT", "STORE", "DB_PATH", "PAPERS_DIR", "LOGS_DIR", "CACHE_DIR", "VAULTS_DIR")}
        for name, value in {"ROOT": root, "STORE": root / "store", "DB_PATH": root / "store/library.db",
                            "PAPERS_DIR": root / "store/papers", "LOGS_DIR": root / "store/logs",
                            "CACHE_DIR": root / "store/cache", "VAULTS_DIR": root / "vaults"}.items():
            setattr(config, name, value)
        config.PAPERS_DIR.mkdir(parents=True)
        (config.VAULTS_DIR / V).mkdir(parents=True)  # a research vault: gaps.list_candidates refuses unknown vaults
        (config.VAULTS_DIR / V / "Home.md").write_text("# Home\n", encoding="utf-8")
        self.groups = patch.object(relations, "MEASURE_GROUPS", root / "store/measure_groups.json")
        self.groups.start()
        self.con = db.connect()

    def tearDown(self):
        self.groups.stop()
        self.con.close()
        for name, value in self.saved.items():
            setattr(config, name, value)
        self.temp.cleanup()

    def paper(self, pid, units, vault=V):
        folder = config.PAPERS_DIR / pid
        folder.mkdir()
        db.write_json(folder / "metadata.json", {"paper_id": pid, "vaults": [vault]})
        db.write_jsonl(folder / "evidence.jsonl", [{"unit_id": f"{pid}:E{i:04d}", "status": "ok", **u}
                                                   for i, u in enumerate(units, 1)])

    @staticmethod
    def result(measure, direction="increase", agent="", species="", statement=""):
        return {"unit_type": "result", "statement": statement or f"{measure} {direction}",
                "outcome": {"measure": measure, "direction": direction}, "intervention": {"agent": agent},
                "model": {"species": species}, "terms": {}}

    def rows(self):
        return list(db.read_jsonl(gaps.path()) or [])

    def test_all_four_types_and_stable_ids(self):
        self.paper("a", [self.result("ROS level", agent="Resveratrol", species="Mouse"),
                         self.result("H3K9me3 level", "decrease", agent="Aphidicolin", species="Mouse"),
                         {"unit_type": "hypothesis", "statement": "Placental pyroptosis may follow vitrification.",
                          "terms": {"concepts": ["Placental pyroptosis"]}, "hedging": "hedged"}])
        self.paper("b", [self.result("ROS level", agent="Melatonin", species="Cattle"),
                         self.result("Blastocyst rate", "decrease", agent="H3K9me3", species="Cattle")])
        self.paper("other", [self.result("ROS level", agent="Resveratrol")], vault="elsewhere")
        db.write_jsonl(config.STORE / "relations.jsonl", [
            {"relation_id": "r1", "from_unit": "a:E0001", "to_unit": "b:E0001", "type": "contradicts",
             "status": "ai_proposed", "directions": ["increase", "decrease"], "outcome_measure": "ROS level", "vault": V},
            {"relation_id": "r2", "from_unit": "a:E0001", "to_unit": "b:E0001", "type": "contradicts", "status": "explained", "vault": V},
            {"relation_id": "r3", "from_unit": "a:E0001", "to_unit": "b:E0001", "type": "contradicts",
             "status": "ai_proposed", "explanation": "different dose", "vault": V}])
        out = gaps.generate(V)
        self.assertEqual(out["papers_scanned"], 2)
        self.assertEqual(out["candidates_by_type"], {"unexplained_conflict": 1, "unproven_hypothesis": 1,
                                                     "untested_combination": 2, "broken_chain": 1})
        rows = self.rows()
        self.assertEqual({r["status"] for r in rows}, {"needs_review"})
        self.assertEqual({r["scope_note"] for r in rows}, {gaps.SCOPE_NOTE})
        self.assertTrue(all(r["supporting_units"] and r["queries_run"] and r["papers_scanned"] == 2 for r in rows))
        chain = next(r for r in rows if r["gap_type"] == "broken_chain")
        self.assertEqual(chain["supporting_units"], ["a:E0002", "b:E0002"])  # Aphidicolin -> H3K9me3 -> Blastocyst rate
        again = gaps.generate(V)  # same ids, nothing changes
        self.assertEqual((again["new"], again["updated"], again["withdrawn"], again["unchanged"]), (0, 0, 0, 5))
        self.assertEqual(self.rows(), rows)

    def test_untested_combination_needs_different_papers_and_is_bounded(self):
        self.paper("a", [self.result("ROS level", agent="Resveratrol", species="Mouse")])
        self.paper("b", [self.result("ROS level", agent="Melatonin", species="Cattle")])
        self.paper("c", [self.result("ROS level", agent="Resveratrol", species="Cattle")])  # together in one paper
        gaps.generate(V)
        pairs = {tuple(r["entities"]) for r in self.rows() if r["gap_type"] == "untested_combination"}
        self.assertEqual(pairs, {("Melatonin", "Mouse")})  # Resveratrol+Cattle and Resveratrol+Mouse co-occur in a paper
        with patch.dict(gaps.DEFAULTS, {"max_untested_combinations": 0}):
            self.assertEqual(gaps.generate(V, True)["candidates_by_type"]["untested_combination"], 0)

    def test_hypothesis_with_result_evidence_is_not_a_gap(self):
        self.paper("a", [{"unit_type": "hypothesis", "statement": "H3K9me3 may matter.",
                          "terms": {"concepts": ["H3K9me3"]}},
                         {"unit_type": "hypothesis", "statement": "No entity here.", "terms": {}}])
        self.paper("b", [self.result("H3K9me3 fluorescence")])
        self.assertEqual(gaps.generate(V, True)["candidates_by_type"]["unproven_hypothesis"], 0)

    def test_entity_is_mentioned_under_a_synonym_or_child_concept_never_a_parent(self):
        def rec(name, **extra):  # RL-08
            return {"concept_id": "local:" + name.lower().replace(" ", "-"), "name": name, "synonyms": [], "parents": [], **extra}
        reg = concepts.Registry([rec("Reactive oxygen species", synonyms=["ROS"]), rec("Cell therapy"),
                                 rec("Cell transplantation", parents=["local:cell-therapy"]),
                                 rec("Superoxide", parents=["local:reactive-oxygen-species"])])
        hyp = lambda name: {"unit_type": "hypothesis", "statement": f"{name} may matter.", "terms": {"concepts": [name]}}  # noqa: E731
        self.paper("a", [hyp("Reactive oxygen species"), hyp("Cell therapy"), hyp("Superoxide"), hyp("Zorblax")])
        self.paper("b", [self.result("ROS level"), self.result("Cell transplantation rate")])
        with patch.object(concepts, "load_registry", lambda con=None: reg):
            out = gaps.generate(V)
        missing = {e for r in self.rows() if r["gap_type"] == "unproven_hypothesis" for e in r["entities"]}
        self.assertEqual(missing, {"superoxide", "zorblax"})  # synonym and child found; a parent result does not prove a child
        self.assertEqual(out["candidates_by_type"]["unproven_hypothesis"], 2)

    def test_registry_lookup_uses_the_raw_entity_not_the_gaps_key(self):
        # RL-08: gaps._key drops "." "+" and keeps β, so "IL-1β" -> "il 1β", which concepts._key never finds
        reg = concepts.Registry([{"concept_id": "local:il-1b", "name": "IL-1β", "synonyms": ["interleukin 1 beta"], "parents": []}])
        self.paper("a", [{"unit_type": "hypothesis", "statement": "IL-1β may matter.", "terms": {"molecules": ["IL-1β"]}}])
        self.paper("b", [self.result("Interleukin-1 beta level")])
        with patch.object(concepts, "load_registry", lambda con=None: reg):
            self.assertEqual(gaps.generate(V, True)["candidates_by_type"]["unproven_hypothesis"], 0)

    def test_dry_run_writes_nothing_and_human_decisions_survive(self):
        self.paper("a", [{"unit_type": "hypothesis", "statement": "X may cause Y.", "terms": {"concepts": ["Placental pyroptosis"]}}])
        self.assertEqual(gaps.generate(V, True)["new"], 1)
        self.assertFalse(gaps.path().exists())
        gaps.generate(V)
        row = self.rows()[0]
        row.update(status="accepted", description="researcher's wording")
        db.write_jsonl(gaps.path(), [row])
        out = gaps.generate(V)
        self.assertEqual((out["kept_human"], out["new"], out["updated"]), (1, 0, 0))
        self.assertEqual(self.rows(), [row])
        (config.PAPERS_DIR / "a" / "evidence.jsonl").write_text("", encoding="utf-8")  # no longer derivable
        gaps.generate(V)
        self.assertEqual(self.rows(), [row])  # a human-decided gap is never withdrawn

    def test_gap_no_longer_derivable_is_withdrawn_then_revived(self):
        self.paper("a", [{"unit_type": "hypothesis", "statement": "X may cause Y.", "terms": {"concepts": ["Placental pyroptosis"]}}])
        gaps.generate(V)
        evidence_path = config.PAPERS_DIR / "a" / "evidence.jsonl"
        saved = evidence_path.read_text(encoding="utf-8")
        evidence_path.write_text("", encoding="utf-8")
        self.assertEqual(gaps.generate(V)["withdrawn"], 1)
        self.assertEqual(self.rows()[0]["status"], "withdrawn_by_rule")
        self.assertEqual(gaps.list_candidates(V), [])
        evidence_path.write_text(saved, encoding="utf-8")
        self.assertEqual(gaps.generate(V)["revived"], 1)
        self.assertEqual(self.rows()[0]["status"], "needs_review")

    def test_mcp_propose_and_list(self):
        self.paper("a", [self.result("ROS level")])
        self.paper("o", [self.result("ROS level")], vault="elsewhere")
        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertLessEqual({"list_gap_candidates", "propose_gap"}, names)
        args = {"vault": V, "description": "No study tests ROS in cattle oocytes.", "supporting_units": ["a:E0001"],
                "gap_type": "untested_combination", "queries_run": ["search_sentences ROS cattle"]}
        first = mcp_server.call_tool(self.con, {}, "propose_gap", args)
        self.assertTrue(first["created"])
        gap = first["gap"]
        self.assertEqual((gap["status"], gap["provenance"], gap["scope_note"]), ("needs_review", "analyst", gaps.SCOPE_NOTE))
        self.assertFalse(mcp_server.call_tool(self.con, {}, "propose_gap", args)["created"])  # same text, same gap
        self.assertEqual(len(mcp_server.call_tool(self.con, {}, "list_gap_candidates", {"vault": V})), 1)
        for bad in ({"supporting_units": ["o:E0001"]}, {"supporting_units": []}, {"gap_type": "invented"},
                    {"supporting_units": ["a:E0099"]}, {"description": " "}):
            with self.assertRaises((ValueError, KeyError, TypeError), msg=bad):
                mcp_server.call_tool(self.con, {}, "propose_gap", {**args, "description": "other " + str(bad), **bad})
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(gaps.generate(V)["withdrawn"], 0)  # analyst rows are never touched by regeneration

    def test_catalogue_note_not_node_notes(self):
        self.paper("a", [{"unit_type": "hypothesis", "statement": "X may cause Y.", "terms": {"concepts": ["Placental pyroptosis"]}}])
        meta = config.PAPERS_DIR / "a" / "metadata.json"
        db.write_json(meta, {"paper_id": "a", "vaults": [V], "first_author": "Smith", "year": 2020, "note_name": "Smith 2020"})
        gaps.generate(V)
        (config.ROOT / "vaults" / V / "_Maps").mkdir(parents=True)
        gaps.write_catalogue(V)
        text = (config.vault_dir(V) / "_Maps" / "Open hypotheses.md").read_text(encoding="utf-8")
        self.assertIn("không có nghĩa là vắng trong y văn", text)  # absence from the vault is not absence from the literature
        self.assertIn("[[Smith 2020|Smith 2020", text)
        body = text.split("Source IDs")[0]
        self.assertNotIn("a:E0001", body)
        self.assertIn("a:E0001", text)  # only in the Source IDs callout
        self.assertFalse(hasattr(concept_notes, "GAP_HEADING"))

    def test_other_vault_conflict_stays_in_its_own_vault_and_analyst_rows_survive(self):
        self.paper("a", [self.result("ROS level")])
        self.paper("o", [self.result("ROS level")], vault="elsewhere")
        db.write_jsonl(config.STORE / "relations.jsonl", [
            {"relation_id": "r1", "from_unit": "o:E0001", "to_unit": "o:E0001", "type": "contradicts", "status": "ai_proposed", "vault": "elsewhere"}])
        self.assertEqual(gaps.generate(V)["candidates_by_type"]["unexplained_conflict"], 0)
        mcp_server.call_tool(self.con, {}, "propose_gap", {"vault": V, "description": "Z", "supporting_units": ["a:E0001"],
                                                          "gap_type": "broken_chain"})
        row = self.rows()[0]
        gaps.generate(V)
        self.assertEqual(self.rows(), [row])
        revived = {**row, "provenance": "generator", "status": "withdrawn_by_rule", "withdrawn_at": "x"}
        out = gaps._merge([revived], {row["gap_id"]: row}, V, dict.fromkeys(("revived", "updated", "unchanged", "kept_human", "withdrawn", "new"), 0))
        self.assertNotIn("withdrawn_at", out[0])


if __name__ == "__main__":
    unittest.main()
