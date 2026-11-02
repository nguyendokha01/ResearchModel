"""Data parts of the ND and RL checks, apart from their probes, and edges the generic PASS / FAIL cases do not cover."""
import tempfile
import unittest
from pathlib import Path

from kbsys import conform
from kbsys import conform_graph as cg

from . import cases_graph as cases

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


def data_only(cid, case):
    """The violations of a case that are not about a probe."""
    with tempfile.TemporaryDirectory() as tmp:
        return [v for v in found(cid, *case(Path(tmp))) if not v.startswith("probe")]


class DataParts(unittest.TestCase):
    def test_data_checks_pass_on_good_data_and_fail_on_damaged_data(self):
        for cid in ("ND-01", "ND-02", "ND-03", "ND-04", "ND-05", "ND-06", "ND-07", "RL-01", "RL-02", "RL-03", "RL-04", "RL-05", "RL-06",
                    "RL-08", "RL-10", "RL-11"):
            good, bad = cases.CASES[cid]
            with self.subTest(cid):
                self.assertEqual(data_only(cid, good), [], f"{cid} PASS case: data part reports violations")
                self.assertTrue(data_only(cid, bad), f"{cid} FAIL case: data part reports nothing")

    def test_each_fail_case_fails_for_its_own_reason(self):
        expect = {"ND-01": "homonyms.yaml blocks", "ND-02": "two registry concepts", "ND-03": "one concept written two ways",
                  "ND-04": "generic term", "ND-05": "below the threshold", "ND-06": "empty note", "ND-07": "concept_id must be unique",
                  "RL-01": "no etype", "RL-02": "different agents", "RL-03": "bundled measure", "RL-04": "does not name it",
                  "RL-05": "no vault", "RL-06": "non-significance", "RL-08": "under a synonym", "RL-10": "empty subject",
                  "RL-11": "no comparison note"}
        for cid, text in expect.items():
            with self.subTest(cid):
                got = found(cid, *self.args(cid))
                self.assertTrue(any(text in v for v in got), (cid, got))

    def args(self, cid):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        return cases.CASES[cid][1](Path(folder.name))


class Edges(unittest.TestCase):
    def test_rl02_compares_what_the_relation_compares(self):
        ok = cases.rl02_clause("H3K9me3", ["H3K9me3 immunofluorescence intensity", "H3K9me3"])
        self.assertEqual(data_only("RL-02", lambda tmp: ok), [])
        for compared in (["Mitochondrial DNA damage", "mtDNA copy number"], ["Mitochondrial DNA damage", "mitochondrial DNA"]):
            bad = data_only("RL-02", lambda tmp: cases.rl02_clause("mitochondrial DNA", compared, ["mtDNA"]))
            self.assertTrue(any("different measures" in v for v in bad), (compared, bad))

    def test_acronym_sentences_missing_from_the_node(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = cases.nd02(Path(tmp), True, 1)  # merged, but only 1 of the 2 AMI sentences linked
            self.assertTrue(any("2 sentences use 'AMI'" in v for v in found("ND-02", store, cases.ami_pairs)))

    def test_default_pairs_pass_the_abbreviation_probe(self):
        self.assertEqual(found("ND-02", cases.Store(concepts=[], paper_ids=[])), [])

    def test_a_link_built_from_references(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, run = cases.nd07(Path(tmp), ["references"])
            self.assertTrue(any("references section" in v for v in found("ND-07", store, run)))

    def test_shown_in_vault_hiding_a_node_that_has_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, registry, collect = cases.node_case(cases.generated(), 0, 1)(Path(tmp))
            self.assertEqual(found("ND-05", vault, registry, collect), [])
            self.assertTrue(any("hides a node" in v for v in found("ND-05", vault, registry, collect, lambda links, units: False)))

    def test_no_source_sentence_is_a_violation_even_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, registry, collect = cases.node_case(cases.generated(), 0, 0)(Path(tmp))
            got = found("ND-06", vault, registry, collect)
            self.assertFalse(any("empty note" in v for v in got), got)
            self.assertTrue(any("no source sentence" in v for v in got), got)

    def test_a_retired_notice_is_an_empty_note(self):
        self.assertFalse(cg._has_content(f"{cg.notes.AUTO_START}\n> [!warning] No linked evidence\n{cg.notes.AUTO_END}"))
        self.assertTrue(cg._has_content(cases.generated()))

    def test_a_side_without_species_does_not_borrow_the_other(self):
        note = cases.conflict_note("Cells - Mouse - A 2020 vs B 2021", "Mouse", "—")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(any("only one side has a species" in v for v in found("RL-04", cases.conflicts_vault(Path(tmp), note))))

    def test_conflict_missing_from_the_note_the_tag_and_the_map(self):
        with tempfile.TemporaryDirectory() as tmp:
            vault, relations_of = cases.rl06(Path(tmp), ("tag",), False, True)
            got = found("RL-06", vault, relations_of)
            self.assertTrue(any("lacks the tag" in v for v in got) and any("not in Open conflicts" in v for v in got), got)
            self.assertTrue(any("no CONFLICT note" in v for v in found("RL-06", cases.Vault("v", Path(tmp), notes=[]), relations_of)))

    def test_result_note_problems(self):
        lookup = lambda paper_id: {"pa:E0001": "increase"}
        with tempfile.TemporaryDirectory() as tmp:
            vault, _ = cases.rl10(Path(tmp), "[[Alpha]]", back_link=False)
            got = found("RL-10", vault, lookup)
            self.assertEqual(sum("does not list this result" in v for v in got), 2, got)
            vault, _ = cases.rl10(Path(tmp), "[[Missing]]")
            self.assertTrue(any("not a link to a node note" in v for v in found("RL-10", vault, lookup)))

    def test_mixed_predicate_needs_a_direction(self):
        text = cases.evd_note("[[Alpha]]", "[[Beta]]", "mixed")
        back = "[[EVD - Alpha raises Beta]]"
        with tempfile.TemporaryDirectory() as tmp:
            vault = cases.Vault("v", Path(tmp), notes=[("Results/EVD - Alpha raises Beta.md", text), ("Concepts/Alpha.md", cases.generated("Alpha") + back),
                                                       ("Concepts/Beta.md", cases.generated("Beta") + back)])
            self.assertTrue(any("mixed for a unit without direction" in v for v in found("RL-10", vault, lambda paper_id: {"pa:E0001": None})))
            self.assertEqual(found("RL-10", vault, lambda paper_id: {"pa:E0001": "mixed"}), [])
            self.assertTrue(any("mixed for a unit without direction" in v for v in found("RL-10", vault, lambda paper_id: {"pa:E0001": "not_applicable"})))

    def test_result_note_subject_not_stated_and_predicate_list(self):
        back = "[[EVD - Alpha raises Beta]]"
        lookup = lambda paper_id: {"pa:E0001": "increase"}

        def run(subject, predicate, lookup=lookup):
            with tempfile.TemporaryDirectory() as tmp:
                vault = cases.Vault("v", Path(tmp), notes=[("Results/EVD - Alpha raises Beta.md", cases.evd_note(subject, "[[Beta]]", predicate)),
                                                           ("Concepts/Beta.md", cases.generated("Beta") + back)])
                return found("RL-10", vault, lookup)
        self.assertEqual(run("not stated", "reports"), [])
        self.assertEqual(run("not stated", "mixed", lambda paper_id: {"pa:E0001": "mixed"}), [])  # the unit states mixed
        self.assertTrue(any("not in the agreed list" in v for v in run("not stated", "raises")))
        self.assertEqual(run("", "raises\nretired: true"), [])  # a retired note kept for its human text is not checked
        self.assertTrue(any("not a link to a node note" in v for v in run("Muse cell", "reports")))
        self.assertTrue(any("mixed for a unit without direction" in v for v in run("not stated", "mixed", lambda paper_id: {"pa:E0001": None})))

    def test_result_note_subject_and_object_as_lists(self):
        back = "[[EVD - Alpha raises Beta]]"

        def run(subject, obj, linked=("Alpha", "Beta", "Gamma")):
            with tempfile.TemporaryDirectory() as tmp:
                nodes = [(f"Concepts/{name}.md", cases.generated(name) + (back if name in linked else "")) for name in ("Alpha", "Beta", "Gamma")]
                vault = cases.Vault("v", Path(tmp), notes=[("Results/EVD - Alpha raises Beta.md", cases.evd_note(subject, obj, "reports")), *nodes])
                return found("RL-10", vault, lambda paper_id: {"pa:E0001": "increase"})
        self.assertEqual(run(["[[Alpha]]", "[[Gamma]]"], ["[[Beta]]", "[[Gamma]]"]), [])  # a list passes
        self.assertEqual(run(["not stated"], ["[[Beta]]"]), [])
        self.assertEqual(run("[[Alpha]]", "[[Beta]]"), [])  # a scalar is still checked
        self.assertTrue(any("object 'Beta text'" in v and "not a link" in v for v in run(["[[Alpha]]"], ["[[Beta]]", "Beta text"])))
        self.assertTrue(any("empty object" in v for v in run(["[[Alpha]]"], [])))
        self.assertTrue(any("empty subject" in v for v in run([], ["[[Beta]]"])))
        self.assertTrue(any("not a link" in v for v in run(["not stated", "[[Alpha]]"], ["[[Beta]]"])))  # sole item only
        self.assertTrue(any("not a link" in v for v in run("Muse cell", ["[[Beta]]"])))
        got = run(["[[Alpha]]", "[[Gamma]]"], ["[[Beta]]"], linked=("Alpha", "Beta"))  # each item lists the result
        self.assertEqual(sum("does not list this result" in v and "'Gamma'" in v for v in got), 1, got)

    def test_scope_note_and_absence_wording_of_gap_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, scan = cases.rl08(Path(tmp), False)
            row = {"gap_id": "gap:2", "gap_type": "broken_chain", "vault": "one", "status": "needs_review", "entities": [], "scope_note": "",
                   "description": "No such chain exists in the literature."}
            cases.db.write_jsonl(store.root / "gap_candidates.jsonl", [row])
            got = found("RL-08", store, scan)
            self.assertTrue(any("no scope note" in v for v in got) and any("speaks of the literature" in v for v in got), got)

    def test_comparison_note_must_list_every_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, groups = cases.rl11(Path(tmp), True)
            note = ("Comparisons/Cell survival.md", "---\ntype: comparison\nmeasure_group: mg1\n---\n# Cell survival\n\n[[Paper pa]] only.\n")
            store._data["vaults"] = [cases.Vault("v", Path(tmp), notes=[note])]
            self.assertTrue(any("does not list pb" in v for v in found("RL-11", store, groups)))

    def test_edge_problems(self):
        etypes = {"observed_effect", "nav"}
        good = [{"source": "Results/E.md", "target": "Concepts/A.md", "status": "resolved", "etype": "observed_effect", "unit_id": "p:E0001",
                 "sentence_ids": '["p:S0001"]'},
                {"source": "Papers/P.md", "target": "Concepts/A.md", "status": "resolved", "etype": "nav", "unit_id": None, "sentence_ids": None}]
        cols = ["source", "target", "status", "etype", "unit_id", "sentence_ids"]
        yes = lambda uid: True
        self.assertEqual(list(cg._edge_problems(cols, good, etypes, yes)), [])
        self.assertTrue(any("no etype" in p for p in cg._edge_problems(["source"], good, etypes, yes)))
        self.assertTrue(any("not in the agreed list" in p for p in cg._edge_problems(cols, [dict(good[0], etype="x")], etypes, yes)))
        self.assertTrue(any("cites no evidence" in p for p in cg._edge_problems(cols, [dict(good[0], unit_id=None)], etypes, yes)))
        self.assertTrue(any("cites no evidence" in p for p in cg._edge_problems(cols, [dict(good[0], sentence_ids="[]")], etypes, yes)))
        self.assertTrue(any("typed nav" in p for p in cg._edge_problems(cols, [dict(good[0], etype="nav")], etypes, yes)))
        figure = dict(good[0], etype="nav", target="_Attachments/F1.png", unit_id=None, sentence_ids=None)  # an embed resolves, it is no node
        self.assertEqual(list(cg._edge_problems(cols, [figure], etypes, yes)), [])
        # D-PENDING-21: a not-significant result (B3) or a body link outside the kb:auto block (B1) may navigate; a typed place may not
        nav = dict(good[0], etype="nav", label="A")
        head, block = "---\ntype: result\n{}---\n# E\n\n", cg.notes.AUTO_START + "\n{}\n" + cg.notes.AUTO_END + "\n"
        for name, text, flagged in (("ns", head.format("significance: ns\n") + block.format("[[A]]"), False),
                                    ("outside", head.format("") + "[[A]]\n" + block.format(""), False),
                                    ("in block", head.format("") + "mine\n" + block.format("[[A]]"), True),
                                    ("property", head.format("subject: '[[A]]'\n") + block.format(""), True),
                                    ("significant", head.format("significance: significant\n") + block.format("[[A]]"), True)):
            got = list(cg._edge_problems(cols, [nav], etypes, yes, cg._may_navigate({"Results/E.md": text})))
            self.assertEqual(any("typed nav" in p for p in got), flagged, name)


class Probes(unittest.TestCase):
    def test_real_product_in_the_probes(self):
        self.assertEqual(found("ND-08"), [])
        self.assertEqual(found("RL-07"), [])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(found("RL-05", *cases.rl05(Path(tmp), "one")), [])
            for cid in ("ND-01", "ND-03", "RL-02", "RL-03", "RL-08", "RL-09"):  # the product keeps the probe cases
                self.assertEqual(found(cid, *cases.CASES[cid][0](Path(tmp))), [], cid)

    def test_a_hub_in_the_expansion_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(any("hub node" in v for v in found("RL-09", *cases.CASES["RL-09"][1](Path(tmp)))))

    def test_ordinary_neighbour_must_be_reached(self):
        self.assertTrue(any("ordinary neighbour" in v for v in found("RL-09", lambda: set())))
        self.assertEqual(found("RL-09", lambda: {"Unique One"}), [])

    def test_a_homonym_judge_that_blocks_everything_or_nothing(self):
        store = cases.Store(con=cases.mem_db(), concepts=[])
        self.assertTrue(any("is blocked for" in v for v in found("ND-01", store, lambda name, text: True)))
        self.assertTrue(any("is not blocked for" in v for v in found("ND-01", store, lambda name, text: False)))

    def test_fold_ignores_spaces_hyphens_apostrophes_and_case(self):
        self.assertEqual(cg._fold("Lyve-1"), cg._fold("LYVE 1"))
        self.assertEqual(cg._fold("Fisher's exact test"), cg._fold("Fisher’s exact test"))
        self.assertNotEqual(cg._fold("Type 1 diabetes"), cg._fold("Type 2 diabetes"))


if __name__ == "__main__":
    unittest.main()
