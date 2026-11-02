"""N3b: readable citations in every generated note; machine IDs only in the one Source IDs callout."""
import unittest

from kbsys import notes, presentation, result_notes

from . import test_vault_render_v2 as fixtures

META = {"paper_id": "smith2026_example_paper_title_9d24", "note_name": "Smith 2026 - Example",
        "authors": [{"family": "Smith", "name": "Smith M"}], "year": 2026}


class CitationHelperTests(unittest.TestCase):
    def test_cite_links_to_the_paper_note_and_registers_ids(self):
        with presentation.collect_source_ids() as ids:
            link = presentation.cite(META, [4], ["S0123", f"{META['paper_id']}:E0077"], META["paper_id"])
        self.assertEqual(link, "[[Smith 2026 - Example|Smith 2026, p. 4]]")
        self.assertEqual(ids, {f"{META['paper_id']}:S0123": "Smith 2026, p. 4",
                               f"{META['paper_id']}:E0077": "Smith 2026, p. 4"})
        self.assertEqual(presentation.cite(META, [4, 5], link=False), "Smith 2026, pp. 4, 5")
        self.assertEqual(presentation.cite({"paper_id": "x"}, None, link=False), "x n.d.")
        presentation.cite(META, 1, ["S1"])  # no active collector: nothing to register, no error

    def test_source_ids_callout_is_collapsed_and_ignored_by_the_leak_check(self):
        ids = {"a2020_x_1234:S0001": "A 2020, p. 1", "a2020_x_1234:S0002": "A 2020, p. 1"}
        callout = "\n".join(presentation.source_ids_callout(ids))
        self.assertIn("> [!info]- Source IDs", callout)
        self.assertEqual(callout.count("A 2020, p. 1:"), 1)  # grouped per citation
        self.assertEqual(presentation.machine_ids("# Heading\n\ntext (A 2020, p. 1)\n" + callout + "\nmore text"), [])
        self.assertEqual(presentation.source_ids_callout({}), [])

    def test_leak_check_finds_ids_in_headings_and_body(self):
        text = "### a2020_x_1234:F3 — Figure\nCâu nhắc tới hình: S0064 sentence\n- source_mention (a2020_x_1234:E0077)\n"
        self.assertEqual(presentation.machine_ids(text), ["a2020_x_1234:F3", "S0064", "a2020_x_1234:E0077"])
        self.assertEqual(presentation.machine_ids("sucrose (Sigma, Cat. No. S1888) and PDB:3LZ1"), [])  # catalogue numbers
        self.assertEqual(presentation.machine_ids("---\nid: a2020_x_1234:E0001\n---\n# Title\n"), [])  # front matter

    def test_object_titles_have_no_internal_ids(self):
        caption = "Figure S3. Heat map of differentially expressed genes. (A) Control. (B) Treated."
        self.assertEqual(presentation.object_title("figure", f"{META['paper_id']}:FS3", "Figure S3", caption, META),
                         "Figure S3 — Heat map of differentially expressed genes (Smith 2026)")
        self.assertEqual(presentation.object_title("table", "p:T2", None, "", META), "Table 2 (Smith 2026)")
        title = presentation.object_title("formula", "p:Mc0ffee0123456789", None, "", None, 7)
        self.assertEqual(title, "Formula (p. 7)")


class GeneratedNoteCitationTests(unittest.TestCase):
    setUp = fixtures.VaultRenderV2Tests.setUp
    tearDown = fixtures.VaultRenderV2Tests.tearDown
    paper = fixtures.VaultRenderV2Tests.paper

    def unit(self):
        return {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result", "sentence_ids": ["p:S1"],
                "statement": "Methylation fell.", "outcome": {"measure": "Methylation"}}

    def test_evd_and_claim_bodies_cite_readably(self):
        self.paper("p", [], "Methylation fell markedly.")
        meta = {"paper_id": "p", "note_name": "Paper p", "first_author": "P", "year": 2025,
                "authors": [{"family": "P", "name": "P"}]}
        for body in (result_notes._result_body(self.unit(), meta, "v", {}),
                     result_notes._claim_body(self.unit(), meta, [], [], [], [])):
            self.assertEqual(presentation.machine_ids(body), [])
            self.assertIn("[[Paper p|P 2025, p. 1]]", body)
            self.assertEqual(body.count("[!info]- Source IDs"), 1)
            self.assertIn("p:S1", body.split("[!info]- Source IDs", 1)[1])

    def test_conflict_body_has_one_callout_for_both_sides(self):
        self.paper("p", [], "Methylation fell markedly.")
        self.paper("q", [], "Methylation did not change.")
        sides = []
        for pid in ("p", "q"):
            meta = {"paper_id": pid, "note_name": f"Paper {pid}", "first_author": pid.upper(), "year": 2025}
            unit = {**self.unit(), "unit_id": f"{pid}:E1", "paper_id": pid, "sentence_ids": [f"{pid}:S1"]}
            sides.append(result_notes._side(unit, meta, result_notes._read_sentences(pid)))
        body = result_notes._conflict_body({"relation_id": "r", "status": "ai_proposed", "type": "contradicts"},
                                           sides, [])
        self.assertEqual(presentation.machine_ids(body), [])
        self.assertEqual(body.count("[!info]- Source IDs"), 1)
        callout = body.split("[!info]- Source IDs", 1)[1]
        self.assertIn("p:S1", callout)
        self.assertIn("q:S1", callout)
        self.assertIn("[[Paper q|Q 2025, p. 1]]", body)

    def test_paper_note_pipe_in_link_alias_does_not_break_result_table(self):
        unit = {**self.unit(), "pages": [2], "paper": {"paper_id": "p", "note_name": "Paper p", "title": "T"},
                "status": "ok"}
        table = notes._paper_results_table(presentation.evidence_table([unit]))
        self.assertEqual(table[0].count("|"), 7)
        self.assertEqual(table[2].count("|"), 7)
        self.assertTrue(table[2].rstrip().endswith("p. 2 |"))


class SourceIdsSurviveTheCapTests(unittest.TestCase):
    def note(self, body_lines):
        ids = {f"p:S{n:04d}": "A 2020, p. 1" for n in range(1, 6)}
        callout = "\n".join(presentation.source_ids_callout(ids))
        return (f"---\ntype: concept\n---\n# T\n\n{notes.AUTO_START}\n" + "\n".join(body_lines) + "\n" + callout
                + f"\n{notes.AUTO_END}\n\n## My notes\n\nmine\n")

    def test_cap_trims_body_before_the_source_ids_callout(self):
        paragraphs = [f"Paragraph {n} " + "word " * 80 + "\n" for n in range(40)]
        quotes = [f"> [!quote]- [[P|A 2020, p. {n}]] · results\n> " + "text " * 80 + "\n" for n in range(40)]
        for lines in (paragraphs, quotes):
            for cap in range(2500, 3500, 40):  # whatever the leftover room, the callout stays
                capped = notes.cap_managed_note(self.note(lines), cap, "concept")
                self.assertLessEqual(len(capped.encode("utf-8")), cap)
                self.assertIn("p:S0005", capped)
                self.assertIn("[!info]- Source IDs", capped)
                self.assertTrue(capped.endswith("## My notes\n\nmine\n"))

    def test_callout_is_kept_even_when_it_alone_exceeds_the_cap(self):
        capped = notes.cap_managed_note(self.note(["Paragraph " + "word " * 500]), 200,
                                        "concept")
        self.assertIn("p:S0005", capped)
        self.assertTrue(capped.endswith("## My notes\n\nmine\n"))

    def test_link_alias_and_table_cell_are_clean(self):
        meta = {**META, "authors": [{"family": "Bad|Name]"}]}
        self.assertEqual(presentation.cite(meta, [1]), "[[Smith 2026 - Example|BadName 2026, p. 1]]")
        unit = {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result", "statement": "s", "pages": [2],
                "paper": {"paper_id": "p", "note_name": "Paper p", "title": "T"}, "status": "ok"}
        self.assertNotIn("\\\\|", "\n".join(presentation.evidence_table([unit])))  # no doubled backslash before a pipe


if __name__ == "__main__":
    unittest.main()
