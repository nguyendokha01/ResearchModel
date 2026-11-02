"""ND-05 / ND-06: paper notes link, and stubs exist for, only nodes that pass the vault threshold (concepts.meets)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concept_notes, concepts, config, db, notes, standard

from .standard import cases_graph as cases
from .standard.test_graph import found

CONCEPT_FOLDER = notes.TERM_FOLDERS["concepts"]


def record(name):
    return {"concept_id": "local:" + name.lower(), "name": name, "type": "concept", "synonyms": [], "parents": []}


class VaultThresholdNoteTests(unittest.TestCase):
    """Real temp database and paper files; `_collect` is replaced by the concept_links rows of the vault plus the units in self.units."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", LOGS_DIR=root / "store" / "logs",
            CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.con = db.connect()
        self.units = {}
        self.registry = concepts.Registry([record(n) for n in ("Aaa", "Bbb", "Ccc", "Ddd")])
        patcher = patch.object(concept_notes, "_collect", self.collect)
        patcher.start()
        self.addCleanup(patcher.stop)
        for pid, studied in (("p1", ["Ddd"]), ("p2", [])):
            self.paper(pid, studied)

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.temp.cleanup()

    def collect(self, con, rec, registry, vault):
        by_paper = {}
        for row in concept_notes._linked_papers(con, {rec["concept_id"]}, vault):
            by_paper.setdefault(row["paper_id"], []).append(row)
        return {rec["concept_id"]}, by_paper, [{"unit_id": u} for u in self.units.get(rec["concept_id"], [])]

    def paper(self, pid, studied):
        folder = db.paper_dir(pid)
        db.write_json(folder / "metadata.json", {
            "paper_id": pid, "title": f"Title {pid}", "vaults": ["v"], "authors": [], "source_files": [],
            "note_name": pid, "evidence_type": "primary_research"})
        db.write_json(folder / "analysis.json", {"classification": "CORE", "classification_reason": "x", "concepts": studied,
                                                 "evidence_type": "primary_research", "claims": []})
        db.write_jsonl(folder / "sentences.jsonl", [{
            "sentence_id": f"{pid}:S0001", "paper_id": pid, "seq": 1, "passage_id": f"{pid}:0001", "page": 1,
            "section": "results", "kind": "text", "text": "Aaa Bbb Ccc Ddd were observed.", "source": "main"}])
        self.con.execute("INSERT INTO paper_vaults VALUES (?, 'v')", (pid,))

    def link(self, name, pid, n, level="mentioned"):
        self.con.execute("INSERT INTO concept_links VALUES (?,?,?,?,?,?)",
                         (f"local:{name.lower()}", pid, level, n, "[]", f"{pid}:S0001"))
        self.con.commit()

    def render(self, pid):
        meta = db.read_json(db.paper_dir(pid) / "metadata.json")
        notes.write_paper_note(meta, "v", con=self.con, registry=self.registry)
        return (config.vault_dir("v") / "Papers" / f"{pid}.md").read_text(encoding="utf-8")

    def stub(self, name):
        return (config.vault_dir("v") / CONCEPT_FOLDER / f"{name}.md").exists()

    def stubs(self):
        notes.ensure_linked_stubs(self.con, "v", self.registry)

    def test_two_mentions_in_one_paper_and_no_unit_get_no_link_and_no_stub(self):
        self.link("Aaa", "p1", 2)
        text = self.render("p1")
        self.stubs()
        self.assertNotIn("Aaa", text)
        self.assertNotIn("Mentioned in text", text)
        self.assertFalse(self.stub("Aaa"))

    def test_three_mentions_over_two_papers_of_the_vault_are_linked_and_stubbed(self):
        self.link("Bbb", "p1", 2)
        self.link("Bbb", "p2", 1)
        self.assertIn("Mentioned in text: [[Bbb]]", self.render("p1"))
        self.assertIn("Mentioned in text: [[Bbb]]", self.render("p2"))
        self.stubs()
        self.assertTrue(self.stub("Bbb"))

    def test_mentions_of_another_vault_do_not_count(self):
        self.con.execute("INSERT INTO paper_vaults VALUES ('p3', 'other')")
        self.link("Bbb", "p1", 2)
        self.link("Bbb", "p3", 5)
        self.assertNotIn("Bbb", self.render("p1"))

    def test_one_evidence_unit_and_one_mention_are_linked_and_stubbed(self):
        self.link("Ccc", "p1", 1)
        self.units["local:ccc"] = ["p1:E0001"]
        self.assertIn("Mentioned in text: [[Ccc]]", self.render("p1"))
        self.stubs()
        self.assertTrue(self.stub("Ccc"))

    def test_studied_term_below_the_threshold_stays_plain_text_and_gets_no_stub(self):
        self.link("Ddd", "p1", 1, level="studied")
        text = self.render("p1")
        self.assertIn("Ddd", text)
        self.assertNotIn("[[Ddd", text)
        notes.ensure_analysis_stubs(db.read_json(db.paper_dir("p1") / "metadata.json"), "v", self.registry)
        self.stubs()
        self.assertFalse(self.stub("Ddd"))

    def test_a_node_that_gains_evidence_is_linked_at_the_next_run(self):
        self.link("Ddd", "p1", 1, level="studied")
        self.assertNotIn("[[Ddd", self.render("p1"))
        self.units["local:ddd"] = ["p1:E0001"]
        self.assertIn("[[Ddd", self.render("p1"))


    def test_gate_does_not_judge_a_term_without_registry_node_and_is_per_vault(self):
        self.link("Bbb", "p1", 3)
        gate = notes.vault_gate(self.con, self.registry, "v")
        self.assertTrue(gate("No such node"))
        self.assertTrue(gate("Bbb"))
        self.assertFalse(gate("Aaa"))
        self.assertFalse(notes.vault_gate(self.con, self.registry, "other")("Bbb"))
        self.assertIn("Bbb", notes.render_auto_block(
            db.read_json(db.paper_dir("p1") / "metadata.json"), {}, {"classification": "CORE", "concepts": ["Aaa"]},
            self.con, self.registry, vault=None))


class ThresholdIsOneRuleTests(unittest.TestCase):
    def check(self, mentions, units):
        with tempfile.TemporaryDirectory() as tmp:
            vault, registry, collect = cases.node_case(cases.generated(), mentions, units)(Path(tmp))
            links, unit_rows = collect(None, None, None, None)[1:]
            return not found("ND-05", vault, registry, collect), concept_notes.shown_in_vault(links, unit_rows)

    def test_shown_in_vault_and_nd05_agree_on_the_boundary(self):
        for mentions, units, expect in ((2, 0, False), (3, 0, True), (0, 1, True), (1, 0, False), (2, 1, True), (0, 0, False)):
            with self.subTest(mentions=mentions, units=units):
                self.assertEqual(self.check(mentions, units), (expect, expect))

    def test_both_follow_the_standard_params(self):
        def param(clause, key):
            return {"min_evidence": 2, "min_mentions": 5}[key]
        with patch.object(standard, "param", param):
            self.assertEqual(self.check(4, 1), (False, False))
            self.assertEqual(self.check(5, 0), (True, True))
            self.assertEqual(self.check(0, 2), (True, True))
            self.assertFalse(concepts.meets(1, 4))
            self.assertTrue(concepts.meets(2, 0))


if __name__ == "__main__":
    unittest.main()
