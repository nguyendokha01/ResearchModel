import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concepts, node_decisions

# Foo cell / Bar assay / Baz mouse as in test_concepts_hygiene; every term is typed `concepts`, so no entry has the type a row asks for.
TERMS = [("concepts", "Foo cell"), ("concepts", "Bar assay"), ("concepts", "Baz mouse"), ("topics", "Qux topic"),
         ("concepts", "Healthy controls")]


def row(name, kind, value=None, **extra):
    return {"key": concepts._key(name), "name": name, "kind": kind, "value": value, "rule": "ND-04",
            "classified_by": "ND-04", "sentence_ids": ["s1"], "at": "2026-10-22T00:00:00Z", "reason": "t", **extra}


class NodeDecisionTests(unittest.TestCase):
    def build(self, rows=None, previous=None):
        store = Path(tempfile.mkdtemp())
        if rows is not None:
            (store / "node_decisions.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        if previous:
            (store / "concepts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in previous), encoding="utf-8")
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
        con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        con.executemany("INSERT INTO paper_terms VALUES (?, ?)", TERMS)
        (store / "generic_terms.yaml").write_text("generic_terms: [Healthy controls]\n", encoding="utf-8")
        with patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}), \
                patch.object(concepts.config, "CONFIG_DIR", store), patch.object(concepts.config, "STORE", store), \
                patch.object(concepts.mesh, "available", return_value=False):
            registry = concepts.build_registry(con)
            self.generic = concepts.generic_keys()
        return {r["name"]: r for r in registry.records}

    def test_absent_and_empty_file_change_nothing(self):
        base = self.build()
        self.assertEqual(sorted(base), ["Bar assay", "Baz mouse", "Foo cell", "Qux topic"])
        self.assertEqual(self.build([]), base)
        self.assertTrue(all(r["classified_by"] == "terms" and "source_sentence_ids" not in r for r in base.values()))

    def test_type_row_without_a_matching_term_entry(self):
        built = self.build([row("Foo cell", "type", "molecule", classified_by="ai:test")])  # all entries are `concepts`
        self.assertEqual((built["Foo cell"]["type"], built["Foo cell"]["classified_by"]), ("molecule", "ai:test"))
        self.assertTrue(concepts.rule_typed(built["Foo cell"]))
        self.assertEqual(built["Bar assay"]["type"], "concept")

    def test_type_row_skips_human_confirmed_and_topic(self):
        confirmed = {"concept_id": "local:baz-mouse", "name": "Baz mouse", "type": "concept", "status": "human_confirmed", "synonyms": []}
        built = self.build([row("Baz mouse", "type", "species"), row("Qux topic", "type", "method")], previous=[confirmed])
        self.assertEqual((built["Baz mouse"]["type"], built["Baz mouse"]["classified_by"]), ("concept", "human"))
        self.assertEqual((built["Qux topic"]["type"], built["Qux topic"]["classified_by"]), ("topic", "terms"))

    def test_not_a_node_and_no_mesh(self):
        built = self.build([row("Bar assay", "not_a_node")])
        self.assertNotIn("Bar assay", built)
        self.assertIn(concepts._key("Bar assay"), self.generic)
        with patch.object(concepts.config, "STORE", Path(tempfile.mkdtemp())):
            (concepts.config.STORE / "node_decisions.jsonl").write_text(json.dumps(row("PUMA", "no_mesh")) + "\n", encoding="utf-8")
            with patch.object(concepts.mesh, "lookup", return_value=[{"descriptor_ui": "D1"}]):
                self.assertEqual(concepts.mesh_hits({"name": "PUMA", "synonyms": []}), [])
                self.assertEqual(concepts.mesh_hits({"name": "Other", "synonyms": ["puma"]}), [])
                self.assertEqual(len(concepts.mesh_hits({"name": "Other", "synonyms": []})), 1)

    def test_add_row_and_idempotence(self):
        rows = [row("Gate added", "add", "method", classified_by="ai:test", sentence_ids=["s9", "s8"])]
        built = self.build(rows)
        self.assertEqual((built["Gate added"]["type"], built["Gate added"]["classified_by"], built["Gate added"]["source_sentence_ids"]),
                         ("method", "ai:test", ["s9", "s8"]))
        self.assertNotIn("source_sentence_ids", built["Foo cell"])
        self.assertEqual(self.build(rows), built)

    def test_type_and_add_rows_need_a_valid_type(self):
        for rows in ([row("Foo cell", "type", "gene")], [row("Foo cell", "type")], [row("New thing", "add", "gene")]):
            with self.assertRaises(ValueError):  # an invalid type never reaches concepts.jsonl
                self.build(rows)

    def test_add_row_on_an_existing_term_keeps_its_name_and_topic(self):
        built = self.build([row("Qux topic", "add", "molecule"), row("Foo cells", "add", "molecule")])
        self.assertEqual((built["Qux topic"]["type"], built["Qux topic"]["classified_by"]), ("topic", "terms"))
        self.assertEqual((built["Foo cell"]["type"], built["Foo cell"]["source_sentence_ids"]), ("molecule", ["s1"]))
        self.assertNotIn("Foo cells", built)

    def test_rows_cache_follows_the_file(self):
        store = Path(tempfile.mkdtemp())
        with patch.object(node_decisions.config, "STORE", store):
            self.assertEqual(node_decisions.rows(), [])
            (store / "node_decisions.jsonl").write_text(json.dumps(row("A", "no_mesh")) + "\n", encoding="utf-8")
            self.assertEqual(node_decisions.keys("no_mesh"), {concepts._key("A")})
            (store / "node_decisions.jsonl").write_text("", encoding="utf-8")
            self.assertEqual(node_decisions.rows(), [])


SENTENCES = [("S0001", "intro", "We used the Foo cell line."), ("S0002", "references", "Foo cell reference."),
             ("S0003", "intro", "Nothing relevant here."), ("S0004", "results", "A Bar assay and Gate added were used.")]


class WriterTests(unittest.TestCase):
    """node_decisions.validate / submit / remove / check on a temp STORE (never the real one)."""

    def setUp(self):
        self.store = Path(tempfile.mkdtemp())
        for pid in ("p1", "p2"):
            (self.store / "papers" / pid).mkdir(parents=True)
            (self.store / "papers" / pid / "sentences.jsonl").write_text("".join(
                json.dumps({"sentence_id": f"{pid}:{sid}", "section": sec, "kind": "text", "text": text}) + "\n"
                for sid, sec, text in SENTENCES), encoding="utf-8")
        confirmed = {"concept_id": "local:baz-mouse", "name": "Baz mouse", "type": "concept", "status": "human_confirmed", "synonyms": []}
        (self.store / "concepts.jsonl").write_text(json.dumps(confirmed) + "\n", encoding="utf-8")
        (self.store / "generic_terms.yaml").write_text("generic_terms: [Healthy controls]\n", encoding="utf-8")
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.execute("CREATE TABLE paper_terms (term_type TEXT, term TEXT)")
        self.con.execute("CREATE TABLE sentences (text TEXT, section TEXT)")
        self.con.execute("CREATE TABLE paper_vaults (paper_id TEXT, vault TEXT)")
        self.con.executemany("INSERT INTO paper_terms VALUES (?, ?)", TERMS)
        self.con.execute("INSERT INTO paper_vaults VALUES ('p1', 'v')")  # p2 is in no vault
        for patcher in (patch.object(concepts.config, "load", return_value={"synonyms": [], "concept_parents": {}}),
                        patch.object(concepts.config, "CONFIG_DIR", self.store), patch.object(concepts.config, "STORE", self.store),
                        patch.object(concepts.config, "PAPERS_DIR", self.store / "papers"),
                        patch.object(concepts.mesh, "available", return_value=False)):
            patcher.start()
            self.addCleanup(patcher.stop)
        concepts.invalidate_registry()
        self.addCleanup(concepts.invalidate_registry)

    def registry(self):
        concepts.invalidate_registry()
        return concepts.build_registry(self.con)

    def errors(self, rows):
        return node_decisions.validate(rows, self.con, self.registry())

    def reasons(self, rows):
        return " | ".join(why for _, _, why in self.errors(rows))

    def submit(self, rows):
        file = self.store / "in.jsonl"
        file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        return node_decisions.submit(file, self.con, self.registry())[0]

    def test_valid_rows_of_every_kind(self):
        rows = [row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"]),
                row("Foo cell", "no_mesh", classified_by="ai:m", sentence_ids=["p1:S0001"]),
                row("Bar assay", "not_a_node", sentence_ids=[]),
                row("Gate added", "add", "method", classified_by="ai:m", sentence_ids=["p1:S0004"])]
        self.assertEqual(self.errors(rows), [])
        self.assertEqual(self.submit([{k: v for k, v in r.items() if k != "at"} for r in rows]), [])
        stored = node_decisions.rows()
        self.assertEqual([r["kind"] for r in stored], ["type", "no_mesh", "not_a_node", "add"])
        self.assertTrue(all(r["at"].endswith("Z") for r in stored))
        self.assertEqual(node_decisions.check(self.con, self.registry()), [])  # the add row's own node does not fail it

    def test_each_refusal(self):
        ok = dict(sentence_ids=["p1:S0001"])
        for why, bad in {
            "kind": row("Foo cell", "bogus", **ok), "key is not": {**row("Foo cell", "type", "cell_type", **ok), "key": "x"},
            "not a node type": row("Foo cell", "type", "gene", **ok), "value must be null": row("Foo cell", "no_mesh", "method", **ok),
            "rule": row("Foo cell", "type", "cell_type", rule="ZZ-99", **ok),
            "classified_by": row("Foo cell", "type", "cell_type", classified_by="terms", **ok),
            "ai:<model>": row("Foo cell", "type", "cell_type", classified_by="ai:", **ok),
            "at least one sentence": row("Foo cell", "type", "cell_type", classified_by="ai:m", sentence_ids=[]),
            "at least one sentence ": row("Gate added", "add", "method", sentence_ids=[]),
            "names no node": row("Nope", "no_mesh", **ok), "ND-09": row("Baz mouse", "type", "species", sentence_ids=[]),
            "already names a node": row("Foo cell", "add", "method", **ok),
            "generic": row("Healthy controls", "add", "method", sentence_ids=["p1:S0001"]),
            "not cleaned": row("Foo  cell", "no_mesh", **ok), "missing fields": {"key": "foo cell", "kind": "type"},
            "full id": row("Foo cell", "type", "cell_type", sentence_ids=["S0001"]),
            "existing sentence": row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0099"]),
            "not body text": row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0002"]),
            "no vault": row("Foo cell", "type", "cell_type", sentence_ids=["p2:S0001"]),
            "does not match": row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0003"]),
        }.items():
            self.assertIn(why.strip(), self.reasons([bad]), why)
        self.assertIn("topic", self.reasons([row("Qux topic", "type", "method", sentence_ids=[])]))
        self.assertIn("duplicate", self.reasons([row("Foo cell", "type", "cell_type", **ok)] * 2))

    def test_all_or_nothing_and_remove_first(self):
        good = row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"])
        self.assertTrue(self.submit([good, row("Nope", "no_mesh")]))
        self.assertFalse((self.store / "node_decisions.jsonl").exists())
        self.assertEqual(self.submit([good]), [])
        self.assertIn("remove first", self.submit([good])[0][2])
        with self.assertRaises(ValueError):
            node_decisions.remove("nope", "type")

    def test_remove_is_undo(self):
        before = {n: (r["type"], r["classified_by"]) for n, r in self.build_names().items()}
        self.assertEqual(self.submit([row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"])]), [])
        self.assertNotEqual({n: (r["type"], r["classified_by"]) for n, r in self.build_names().items()}, before)
        node_decisions.remove(concepts._key("Foo cell"), "type")
        self.assertEqual({n: (r["type"], r["classified_by"]) for n, r in self.build_names().items()}, before)

    def build_names(self):
        return {r["name"]: r for r in self.registry().records}

    def test_rows_the_build_would_ignore_or_misapply(self):  # review 19f6cc5: each passed the validator before
        self.assertIn("human_confirmed", self.reasons([row("Baz mouse", "not_a_node", sentence_ids=[])]))  # the build keeps it
        with patch.object(concepts.config, "load", return_value={"synonyms": [["Foo cell", "Quux"]], "concept_parents": {}}):
            self.assertIn("synonyms.yaml", self.reasons([row("Foo cell", "not_a_node", sentence_ids=[])]))  # the group re-adds it
            self.assertIn("another key", self.reasons([row("Foo cell", "type", "cell_type", sentence_ids=[]),
                                                       row("Quux", "type", "molecule", sentence_ids=[])]))  # build picks one
            self.assertEqual(self.submit([row("Foo cell", "type", "cell_type", sentence_ids=[])]), [])
            self.assertIn("stored type or add row", self.submit([row("Quux", "type", "molecule", sentence_ids=[])])[0][2])
        (self.store / "papers" / "p1" / "structure.json").write_text(json.dumps(
            {"abbreviations": [{"short": "FC", "definitions": [{"text": "Foo cell"}]}]}), encoding="utf-8")
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text(json.dumps(
            {"sentence_id": "p1:S0001", "section": "intro", "kind": "text", "text": "The Foo cell (FC) line."}) + "\n", encoding="utf-8")
        # the add would rename and retype the existing Foo cell node
        self.assertIn("abbreviation", reasons := self.reasons([row("FC", "add", "molecule", sentence_ids=["p1:S0001"])]))
        self.assertNotIn("new add row", reasons)  # its partner is a node, not a new add row
        # two NEW add rows paired by an abbreviation: no existing node is hijacked, and no KeyError (set precedence, 2026-10-22)
        (self.store / "papers" / "p1" / "structure.json").write_text(json.dumps(
            {"abbreviations": [{"short": "FX", "definitions": [{"text": "Fox wing"}]}]}), encoding="utf-8")
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text(json.dumps(
            {"sentence_id": "p1:S0001", "section": "intro", "kind": "text", "text": "The Fox wing (FX) line."}) + "\n", encoding="utf-8")
        reasons = self.reasons([row("FX", "add", "molecule", sentence_ids=["p1:S0001"]),
                                row("Fox wing", "add", "molecule", sentence_ids=["p1:S0001"])])
        self.assertNotIn("merges with node", reasons)  # the pair itself is refused: test_add_pair_merged_by_an_abbreviation

    def abbreviation(self, short, long_):
        (self.store / "papers" / "p1" / "structure.json").write_text(json.dumps(
            {"abbreviations": [{"short": short, "definitions": [{"text": long_}]}]}), encoding="utf-8")
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text(json.dumps(
            {"sentence_id": "p1:S0001", "section": "intro", "kind": "text", "text": f"The {long_} ({short}) line."}) + "\n", encoding="utf-8")

    def test_not_a_node_on_one_term_of_a_merged_node(self):  # the build would split the node under a new id
        self.assertEqual(self.errors([row("Bar assay", "not_a_node", sentence_ids=[])]), [])  # single-key node: accepted
        self.con.execute("INSERT INTO paper_terms VALUES ('concepts', 'FC')")
        self.abbreviation("FC", "Foo cell")
        by_key = self.registry().by_key
        self.assertIs(by_key["fc"], by_key["foocell"])  # one node, two keys
        for name in ("Foo cell", "FC"):
            self.assertIn("one of several terms", self.reasons([row(name, "not_a_node", sentence_ids=[])]))
        self.assertIn("fc", self.reasons([row("Foo cell", "not_a_node", sentence_ids=[])]))  # names the other key

    def test_add_pair_merged_by_an_abbreviation(self):  # the build merges the pair and keeps one row's type and sentence ids
        self.abbreviation("FX", "Fox wing")
        sid = ["p1:S0001"]
        for other in ("method", "molecule"):
            reasons = self.reasons([row("FX", "add", "molecule", sentence_ids=sid), row("Fox wing", "add", other, sentence_ids=sid)])
            self.assertEqual(reasons.count("submit one add row"), 2)  # both rows of the pair, same type too
        self.assertEqual(self.errors([row("FX", "add", "molecule", sentence_ids=sid)]), [])  # alone it is fine

    def test_not_a_node_on_a_term_with_spelling_variant_keys(self):  # review 20d1549: variant keys are not terms of their own
        self.con.execute("INSERT INTO paper_terms VALUES ('concepts', 'Type-2 diabetes')")  # variants: type 2 diabete / type2 diabete
        self.assertEqual(len({k for k, v in self.registry().by_key.items() if v["name"] == "Type-2 diabetes"}), 2)
        self.assertIn("one of several terms", self.reasons([row("Type 2 diabetes", "not_a_node", sentence_ids=[])]))  # the build ignores it
        self.assertEqual(self.submit([row("Type-2 diabetes", "not_a_node", sentence_ids=[])]), [])
        self.assertIsNone(self.registry().lookup("Type-2 diabetes"))  # the build removes the node
        self.assertEqual(node_decisions.check(self.con, self.registry()), [])

    def test_bom_crlf_file_and_unhashable_key(self):
        file = self.store / "in.jsonl"
        file.write_text(json.dumps(row("Foo cell", "no_mesh", sentence_ids=["p1:S0001"])) + "\r\n", encoding="utf-8-sig")
        self.assertEqual(node_decisions.submit(file, self.con, self.registry())[0], [])
        for kind, value in (("type", "cell_type"), ("no_mesh", None), ("not_a_node", None), ("add", "method")):  # add/not_a_node raised TypeError
            self.assertIn("key is not", self.submit([{**row("Foo cell", kind, value, sentence_ids=["p1:S0001"]), "key": ["x"]}])[0][2])

    def test_check_flags_a_vanished_sentence(self):
        self.assertEqual(self.submit([row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"])]), [])
        self.assertEqual(node_decisions.check(self.con, self.registry()), [])
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text("", encoding="utf-8")  # reextract renumbered it
        self.assertIn("existing sentence", node_decisions.check(self.con, self.registry())[0][2])

    # ---- node-decision preview (read-only) ----
    def preview(self, rows=None, patches=()):
        from kbsys import node_preview
        file = self.store / "in.jsonl"
        if rows is not None:
            file.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        out = Path(tempfile.mkdtemp()) / "preview.md"  # not under STORE: preview refuses that
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        failed = node_preview.run(self.con, file if rows is not None else None, out)[2]
        return failed, out.read_text(encoding="utf-8")

    def test_preview_every_kind_once_with_its_undo(self):
        mesh = [patch.object(concepts.mesh, "available", return_value=True),
                patch.object(concepts.mesh, "lookup", side_effect=lambda t: [{"descriptor_ui": "D" + t[:3], "synonyms": []}]),
                patch.object(concepts.mesh, "descriptor", side_effect=lambda ui: {"ui": ui, "tree_numbers": []}),
                patch.object(concepts.mesh, "parent_descriptors", return_value=[])]
        rows = [row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"]), row("Baz mouse", "no_mesh", sentence_ids=[]),
                row("Bar assay", "not_a_node", sentence_ids=[]),
                row("Gate added", "add", "method", classified_by="ai:m", sentence_ids=["p1:S0004"])]
        failed, text = self.preview(rows, mesh)
        self.assertFalse(failed)
        section = text.split("## B -> F", 1)[1]
        for name, kind in (("Foo cell", "type"), ("Baz mouse", "no_mesh"), ("Bar assay", "not_a_node"), ("Gate added", "add")):
            self.assertEqual(section.count(f"| undo: remove {concepts._key(name)} {kind} (in FILE"), 1, name)
        self.assertIn("mesh_ui DBaz -> None", section)
        self.assertIn("type concept -> cell_type", section)
        self.assertIn("p1:S0004", section)
        self.assertFalse((self.store / "node_decisions.jsonl").exists())

    def test_preview_bad_row_reported_store_untouched(self):
        failed, text = self.preview([row("Nope", "no_mesh")])
        self.assertTrue(failed)
        self.assertIn("names no node", text.split("## S -> B", 1)[0])
        self.assertFalse((self.store / "node_decisions.jsonl").exists())  # still absent
        good = row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"])
        self.assertEqual(self.submit([good]), [])
        before = (self.store / "node_decisions.jsonl").read_bytes()
        self.assertTrue(self.preview([row("Nope", "no_mesh"), good])[0])  # the good row is already stored
        self.assertEqual((self.store / "node_decisions.jsonl").read_bytes(), before)

    def test_preview_shows_a_stored_record_the_build_merges(self):
        (self.store / "concepts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (
            {"concept_id": "local:baz-mouse", "name": "Baz mouse", "type": "concept", "status": "human_confirmed", "synonyms": []},
            {"concept_id": "local:foo-cell", "name": "Foo cell", "type": "concept", "status": "ai_proposed", "synonyms": []},
            {"concept_id": "local:quux", "name": "Quux", "type": "concept", "status": "ai_proposed", "synonyms": []})), encoding="utf-8")
        cfg = patch.object(concepts.config, "load", return_value={"synonyms": [["Foo cell", "Quux"]], "concept_parents": {}})
        failed, text = self.preview(None, [cfg])
        self.assertFalse(failed)
        self.assertRegex(text, r"MERGED into `local:foo-cell` Quux \| cause: rebuild \| undo: retire undo map")
        self.assertIn("merged=1", text)
        self.assertIn("B -> F (decision rows): changed=0 removed=0 added=0", text)

    def test_preview_flags_an_add_below_nd_05(self):
        add = row("Gate added", "add", "method", classified_by="ai:m", sentence_ids=["p1:S0004"])
        self.assertIn("below ND-05", self.preview([add])[1])  # one mention in the vault
        sentences = [{"sentence_id": f"p1:S000{i}", "section": "results", "kind": "text", "text": "Gate added was used."} for i in (1, 2, 3, 4)]
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text("".join(json.dumps(s) + "\n" for s in sentences), encoding="utf-8")
        self.assertNotIn("below ND-05", self.preview([add])[1])  # three or more mentions
        (self.store / "papers" / "p1" / "sentences.jsonl").write_text(json.dumps(sentences[0]) + "\n", encoding="utf-8")
        (self.store / "papers" / "p1" / "evidence.jsonl").write_text(json.dumps({"unit_id": "u1", "sentence_ids": ["p1:S0001"]}) + "\n", encoding="utf-8")
        self.assertNotIn("below ND-05", self.preview([add])[1])  # one evidence unit

    def test_preview_review_4a9f1d8(self):  # review 4a9f1d8: each was lost or cut before
        from kbsys import node_preview
        line = node_preview._line({"concept_id": "local:x", "name": "N" * 180}, "ADDED", "rebuild", node_preview.REBUILD_UNDO)
        self.assertTrue(line.startswith("- `local:x` N") and line.endswith(node_preview.REBUILD_UNDO) and len(line) <= 200, line)
        with self.assertRaises(ValueError):  # sources/ is immutable too
            node_preview.run(self.con, None, node_preview.config.ROOT / "sources" / "x.md")
        (self.store / "concepts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in (  # two stored records, one id
            {"concept_id": "local:foo-cell", "name": "Foo-cell", "type": "concept", "status": "ai_proposed", "synonyms": []},
            {"concept_id": "local:foo-cell", "name": "Foo cell", "type": "concept", "status": "ai_proposed", "synonyms": []})), encoding="utf-8")
        self.assertRegex(self.preview(None)[1], r"`local:foo-cell` Foo-cell \| MERGED into `local:foo-cell` Foo cell")
        (self.store / "concepts.jsonl").write_text("", encoding="utf-8")
        self.con.execute("INSERT INTO paper_terms VALUES ('concepts', 'FC')")  # FC = Foo cell: one node FC
        (self.store / "papers" / "p1" / "structure.json").write_text(json.dumps(
            {"abbreviations": [{"short": "FC", "definitions": [{"text": "Foo cell"}]}]}), encoding="utf-8")
        with patch.object(node_decisions, "validate", return_value=[]):  # validate now refuses this row (hole closed after 4a9f1d8); the preview still shows what a stored one did
            text = self.preview([row("FC", "not_a_node", sentence_ids=[])])[1]  # splits the node: Foo cell comes back as a new id
        self.assertRegex(text, r"`local:foo-cell` Foo cell \| ADDED .* cause: not_a_node ND-04/ND-04 \| undo: remove fc not_a_node")

    def test_preview_restores_rows(self):
        self.assertEqual(self.submit([row("Foo cell", "type", "cell_type", sentence_ids=["p1:S0001"])]), [])
        with self.assertRaises(RuntimeError), node_decisions.using([]):
            self.assertEqual(node_decisions.rows(), [])
            raise RuntimeError
        self.assertEqual(len(node_decisions.rows()), 1)
        self.preview([row("Bar assay", "not_a_node", sentence_ids=[])])
        self.assertIsNone(node_decisions._OVERRIDE)
        self.assertEqual(len(node_decisions.rows()), 1)


if __name__ == "__main__":
    unittest.main()
