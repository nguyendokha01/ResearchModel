"""Regressions for the selftest checker itself (false errors found on the real library, 2026-10-12)."""
import json
import unittest
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import concept_notes, concepts, config, db, presentation, selftest, standard


class SelftestCheckerTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def run_concepts(self, records, links, first=0):
        base = {"status": "ai_proposed", "type": "concept", "synonyms": [], "parents": [], "mesh_tree_numbers": []}
        db.write_jsonl(config.STORE / "concepts.jsonl", [{**base, **r} for r in records])
        for concept_id, n in links:
            self.con.execute("INSERT INTO concept_links VALUES (?,?,?,?,?,?)", (
                concept_id, self.pid, "mentioned", n, json.dumps(["introduction"]), self.rows[first]["sentence_id"]))
        self.con.commit()
        with patch("kbsys.concepts.mesh.available", return_value=False):
            report = selftest.Report()
            selftest.check_concepts(report, self.con)
        return report.errors

    def test_paper_note_attachment_links_resolve_by_file_not_note(self):
        vault = config.vault_dir("one")
        (vault / "_Attachments" / self.pid).mkdir(parents=True)
        (vault / "_Attachments" / self.pid / "F1.png").write_bytes(b"png")
        (vault / "Papers").mkdir()
        (vault / "Papers" / "note.md").write_text(
            f"![[_Attachments/{self.pid}/F1.png]]\n![[_Attachments/{self.pid}/F9.png]]\n", encoding="utf-8")
        errors = [e for e in self.run_concepts([], []) if "paper link" in e]
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("F9.png", errors[0])

    def test_mesh_match_check_follows_the_build_rule_for_a_descriptor_reached_through_a_synonym(self):
        # CK7 has no MeSH hit by name; "Cytokeratin 7" merged under it does (concepts.mesh_hits, ND-02/03)
        record = {"concept_id": "local:ck7", "name": "CK7", "synonyms": ["Cytokeratin 7"], "status": "mesh", "mesh_ui": "D1"}
        db.write_jsonl(config.STORE / "concepts.jsonl", [{"type": "molecule", "parents": [], "mesh_tree_numbers": [], **record}])
        with patch("kbsys.concepts.mesh.available", return_value=True),                 patch("kbsys.concepts.mesh.descriptor", return_value={"ui": "D1", "tree_numbers": []}),                 patch("kbsys.concepts.mesh.parent_descriptors", return_value=[]),                 patch("kbsys.concepts.mesh.lookup", side_effect=lambda term, **kw: [{"descriptor_ui": "D1"}] if term == "Cytokeratin 7" else []):
            report = selftest.Report()
            selftest.check_concepts(report, self.con)
        self.assertEqual([e for e in report.errors if "MeSH" in e], [])

    def test_concept_link_check_uses_the_linker_matcher(self):
        # Species words inside reagent names ("fetal bovine serum") and table rows follow concepts._link_stream.
        self.rows[0]["text"] = "Cells grew in fetal bovine serum."
        self.rows[1]["text"] = "Bovine embryos were used."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        errors = self.run_concepts([{"concept_id": "local:bovine", "name": "Bovine", "type": "species"}],
                                   [("local:bovine", 1)], first=1)
        self.assertEqual([e for e in errors if "concept_links" in e], [])

    def test_concept_link_check_accepts_hyphen_variants_of_gene_symbols(self):
        # BCL2 is linked from "Bcl-2" by the linker's gene pattern; a first-word pre-filter must not hide it.
        self.rows[1]["text"] = "Bcl-2 expression increased."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        errors = self.run_concepts([{"concept_id": "local:bcl2", "name": "BCL2"}], [("local:bcl2", 1)], first=1)
        self.assertEqual([e for e in errors if "concept_links" in e], [])

    def test_concept_note_is_expected_only_above_the_vault_threshold(self):
        record = {"concept_id": "local:zzz", "name": "Zzz"}
        self.rows[0]["text"] = "Zzz was observed."
        db.write_jsonl(self.folder / "sentences.jsonl", self.rows)
        for n, expect_error in ((1, False), (standard.param("ND-05", "min_mentions"), True)):
            self.con.execute("DELETE FROM concept_links")
            concept_notes._COLLECT_CACHE.clear()
            errors = [e for e in self.run_concepts([record], [("local:zzz", n)]) if "missing canonical note" in e]
            self.assertEqual(bool(errors), expect_error, (n, errors))

    def test_generated_note_headings_follow_the_renderers(self):
        from kbsys import notes
        vault = config.vault_dir("one")

        def note(fingerprint, *headings):
            return "\n".join([notes.AUTO_START, f"<!-- kb:concept-fingerprint:{fingerprint * 40} -->",
                              *headings, notes.AUTO_END, ""])
        node = tuple("## " + h for h in concept_notes.node_headings("concept")[:-1])  # hidden sections are fine
        for folder, name, headings in (("Concepts", "Node", node), ("_Maps", "Hub", selftest.NOTE_MOC_HEADINGS)):
            (vault / folder).mkdir()
            (vault / folder / f"{name}.md").write_text(note("a", *headings), encoding="utf-8")
        (vault / "Concepts" / "Bad.md").write_text(note("b", "## Synthesis"), encoding="utf-8")
        (vault / "Concepts" / "Late.md").write_text(note("c", "## Sources", "## Key findings"), encoding="utf-8")
        errors = [e for e in self.run_concepts([], []) if "for type" in e]
        self.assertEqual(sorted(e.split(" in ")[-1] for e in errors), ["Bad.md", "Late.md"], errors)

    def test_machine_ids_outside_the_source_ids_callout_are_errors(self):
        from kbsys import notes
        vault = config.vault_dir("one")
        (vault / "Concepts").mkdir()
        (vault / "Results").mkdir()

        def note(body):
            return f"---\nid: x2020_y_1234:E0001\n---\n# T\n\n{notes.AUTO_START}\n{body}\n{notes.AUTO_END}\n"
        callout = "> [!info]- Source IDs\n> - A 2020, p. 1: x2020_y_1234:S0001, x2020_y_1234:E0001\n"
        (vault / "Concepts" / "Clean.md").write_text(note("## Key findings\n\ntext (A 2020, p. 1)\n\n" + callout), encoding="utf-8")
        (vault / "Concepts" / "Dirty.md").write_text(note("### x2020_y_1234:F3 — Figure\nCâu nhắc tới hình: S0064 x\n"), encoding="utf-8")
        (vault / "Results" / "EVD - dirty.md").write_text(note("> **x2020_y_1234:S0001** · p. 4\n"), encoding="utf-8")
        errors = [e for e in self.run_concepts([], []) if "machine IDs visible" in e]
        self.assertEqual(sorted(e.split(" in ")[-1].split(":")[0] for e in errors),
                         ["Concepts/Dirty.md", "Results/EVD - dirty.md"], errors)

    def test_withdrawn_by_rule_relation_status_is_valid(self):
        db.write_jsonl(config.STORE / "relations.jsonl", [
            {"relation_id": "a" * 16, "status": "withdrawn_by_rule", "from_unit": "x:E0001", "to_unit": "x:E0002"},
            {"relation_id": "b" * 16, "status": "bogus", "from_unit": "x:E0001", "to_unit": "x:E0002"}])
        report = selftest.Report()
        selftest.check_relations(report)
        bad = [e for e in report.errors if "invalid relation status" in e]
        self.assertEqual(len(bad), 1, bad)
        self.assertIn("b" * 16, bad[0])


class GreekCapitalisationTests(unittest.TestCase):
    def test_greek_leading_letter_is_not_uppercased(self):
        self.assertEqual(concepts.capitalize_first("β-tubulin"), "β-tubulin")
        self.assertEqual(concepts._display_name("β-tubulin"), "β-tubulin")
        self.assertEqual(concepts._display_name("histone"), "Histone")
        # a legacy capital-Beta spelling merges with the real one instead of becoming a second concept
        self.assertEqual(concepts._key("Β-tubulin"), concepts._key("β-tubulin"))

    def test_matches_term_cached_regex_keeps_case_rules(self):
        self.assertTrue(presentation.matches_term("The ROS level rose.", "ROS"))
        self.assertFalse(presentation.matches_term("Ros level", "ROS"))
        self.assertTrue(presentation.matches_term("Tubulins", "Tubulin", ["tubulins"]))
        self.assertFalse(presentation.matches_term("Tubulinsx", "Tubulin"))


if __name__ == "__main__":
    unittest.main()


class NoteTextProblemTests(unittest.TestCase):
    def test_note_text_problems(self):
        from kbsys import notes
        block = lambda body: f"# T\n\n{notes.AUTO_START}\n{body}\n{notes.AUTO_END}\n"
        stems = {"histone": {"Histone"}}
        clean = block("## Definition\n\nHistone binds DNA ([[Histone]]).\n\n## Evidence\n\n### A\n\ntext")
        self.assertEqual(selftest.note_text_problems(clean, stems), [])
        bad = block("## Definition\n## Evidence\n\nΒ±Î² chưa tìm thấy [[HISTONE]]\n\n## Sources\n")
        found = " | ".join(selftest.note_text_problems(bad, stems))
        for needle in ("empty section '## Definition'", "empty section '## Sources'", "broken characters 'Î²'",
                       "placeholder text 'chưa tìm thấy'", "only by case"):
            self.assertIn(needle, found)
        self.assertEqual([p for p in selftest.note_text_problems(block("## Definition\n\nSynthesis pending"), stems, False)], [])
        self.assertIn("placeholder text", " ".join(selftest.note_text_problems(block("## Definition\n\nnot found in the current knowledge base"), stems)))
