"""Typed node regressions use temporary stores; never modify the research library."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, conform, db, node_profile, concept_notes, concepts, notes, workflow
from kbsys.conform import Vault


class NodeProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(config, ROOT=self.root, STORE=self.root / "store",
                                    PAPERS_DIR=self.root / "store/papers", VAULTS_DIR=self.root / "vaults",
                                    CONFIG_DIR=self.root / "config", LOGS_DIR=self.root / "logs")
        self.paths.start()
        self.record = {"concept_id": "eg", "name": "EG", "type": "molecule", "synonyms": [], "parents": []}
        self.rows = [
            {"sentence_id": "p:S1", "seq": 1, "text": "Ethylene glycol (EG) is a chemical agent that may be important [2].",
             "passage_id": "p:1", "section": "introduction", "page": 1,
             "citations": [{"ref_nums": [2]}]},
            {"sentence_id": "p:S2", "seq": 2, "text": "It may have limitations in this model.",
             "passage_id": "p:1", "section": "introduction", "page": 1},
            {"sentence_id": "p:S3", "seq": 3, "text": "EG was used for freezing at 15 mg using a Cryotop for 5 min.",
             "passage_id": "p:2", "section": "methods", "subsection": "Freezing", "page": 2},
            {"sentence_id": "p:S4", "seq": 4, "text": "Samples were warmed at 37 °C.",
             "passage_id": "p:3", "section": "methods", "subsection": "Freezing", "page": 2},
            {"sentence_id": "p:S5", "seq": 5, "text": "EG and control groups did not differ significantly (42%, n = 8, p = 0.4; Fig. 1).",
             "passage_id": "p:4", "section": "results", "page": 3, "mentions": ["p:F1"]}]
        self.unit = {"paper_id": "p", "unit_id": "p:E1", "seq": 1, "unit_type": "result", "status": "ok",
                     "sentence_ids": ["p:S5"], "quote": "A WRONG STORED QUOTE", "statement": "An invented definition.",
                     "model": {"species": "mouse"}, "intervention": {"agent": "EG", "comparator": "control"},
                     "outcome": {"measure": "survival", "direction": "no_change", "values": ["42%"], "n": "8", "p": "0.4", "value_source": "text"},
                     "pages": [3], "sections": ["results"], "paper": {"paper_id": "p", "title": "Fixture", "note_name": "Fixture", "evidence_type": "primary_research"}}
        self.write_paper("p", self.rows)
        db.write_jsonl(db.paper_dir("p") / "evidence.jsonl", [self.unit])

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def write_paper(self, paper, rows):
        folder = db.paper_dir(paper)
        db.write_json(folder / "metadata.json", {"paper_id": paper, "title": "Fixture", "note_name": "Fixture",
                                               "doi": "10.fixture/source", "evidence_type": "primary_research"})
        db.write_jsonl(folder / "sentences.jsonl", rows)

    def build(self, **kwargs):
        return node_profile.build(self.record, "one", ["p"], [self.unit], **kwargs)

    def protocol(self, dose="15 mg"):
        from kbsys import structure
        task=structure.prepare_protocol("p")
        reply={k:task[k] for k in ("source_sha256","source_revision","revision_of")}
        reply.update(protocols=[{"name":"freezing", "sentence_ids":["p:S3","p:S4"],
                  "details":[{"field":"dose","value":"15 mg","sentence_ids":["p:S3"]},
                             {"field":"equipment","value":"Cryotop","sentence_ids":["p:S3"]},
                             {"field":"temperature","value":"37 °C","sentence_ids":["p:S4"]}],
                  "steps":[{"sentence_ids":["p:S3"]},{"sentence_ids":["p:S4"]}]}],skipped=[])
        if not structure.submit_protocol(None,"p",reply,"fixture")["ok"]: raise AssertionError("Invalid fixture")
        if dose != "15 mg":
            folder=db.paper_dir("p")
            profile=db.read_json(folder/"protocol_profile.json")
            profile["protocols"][0]["details"][0]["value"]=dose
            db.write_json(folder/"protocol_profile.json",profile)

    def test_source_identity_exact_qualifiers_and_no_statement_definition(self):
        p = self.build(persist=False)
        fields = p["papers"][0]["fields"]
        full = fields["full_names"]["items"][0]
        self.assertEqual(full["value"], "Ethylene glycol")
        ref = full["sources"][0]
        self.assertEqual(ref["text"], self.rows[0]["text"])
        self.assertEqual(ref["paper_id"], "p")
        self.assertEqual(ref["page"], 1)
        self.assertEqual(ref["doi"], "10.fixture/source")
        self.assertTrue(any(q["text"] == "may" for q in ref["qualifiers"]))
        out = "\n".join(node_profile.render(p))
        self.assertNotIn("invented definition", out)
        self.assertNotIn("WRONG STORED QUOTE", out)
        self.assertIn("[2]", out)
        self.assertEqual(p["scientific_acceptance"], "not_assessed")

    def test_no_memory_definitions_or_category_class_inference(self):
        self.write_paper("p", [{**self.rows[2], "text": "EG was added."}])
        p = self.build(persist=False)
        fields = p["papers"][0]["fields"]
        self.assertEqual(p["entity_kind"], "chemical")
        self.assertEqual(fields["definitions"]["status"], "not_annotated")
        self.assertEqual(fields["full_names"]["items"], [])
        self.assertEqual(fields["source_classes"]["items"], [])
        self.assertIn("Source-derived classes are listed separately", "\n".join(node_profile.render(p)))

    def test_review_publication_type_overrides_generated_primary_label_read_only(self):
        path = db.paper_dir("p") / "metadata.json"
        metadata = db.read_json(path)
        metadata.update(evidence_type="primary_research", publication_types=["Review"])
        db.write_json(path, metadata)
        original = path.read_bytes()
        p = self.build(persist=False)["papers"][0]
        self.assertEqual(p["mentions"][0]["evidence_type"], "review")
        self.assertEqual(p["results"][0]["evidence_type"], "review")
        self.assertEqual(p["results"][0]["sources"][0]["evidence_type"], "review")
        self.assertEqual(path.read_bytes(), original)

    def test_protocol_exact_fragments_steps_and_absence(self):
        self.protocol()
        p = self.build(persist=False)
        variant = p["papers"][0]["protocol_variants"][0]
        self.assertEqual([r["sentence_id"] for r in variant["steps"]], ["p:S3", "p:S4"])
        self.assertEqual(variant["details"]["dose"]["items"][0]["value"], "15 mg")
        self.assertEqual(variant["details"]["equipment"]["items"][0]["value"], "Cryotop")
        self.assertEqual(variant["details"]["chemical"]["status"], "not_annotated")
        out = "\n".join(node_profile.render(p))
        self.assertIn("37 °C", out)
        self.assertIn("p:S4", out)
        self.protocol("5 mg")
        self.assertIn("inventory",self.build(persist=False)["papers"][0]["protocol_variants"][0]["basis"])

    def test_variants_stay_paper_specific_and_current_vault_scope(self):
        self.protocol()
        rows = [{**self.rows[2], "sentence_id": "q:S3", "text": "EG freezing uses 20 mg for 10 min."}]
        self.write_paper("q", rows)
        profile = node_profile.build(self.record, "one", ["p", "q"], [self.unit], persist=False)
        self.assertEqual(len(profile["papers"]), 2)
        self.assertNotEqual(profile["papers"][0]["protocol_variants"][0]["steps"],
                            profile["papers"][1]["protocol_variants"][0]["steps"])
        self.write_paper("outside", [{**self.rows[0], "sentence_id": "outside:S1", "text": "EG outside secret."}])
        self.assertNotIn("outside secret", json.dumps(self.build(persist=False)))

    def test_stale_protocol_falls_back_without_reusing_fields(self):
        self.protocol()
        self.write_paper("p", [{**r, "text": r["text"] + " revised"} for r in self.rows])
        p = self.build(persist=False)["papers"][0]
        self.assertEqual(p["protocol_profile_status"], "missing_or_stale")
        self.assertEqual(p["protocol_variants"][0]["details"]["dose"]["items"][0]["value"], "15 mg")
        self.assertIn("unresolved", p["protocol_variants"][0]["details"]["dose"]["items"][0]["assignment"])
        self.assertIn("inventory", p["protocol_variants"][0]["basis"])

    def test_method_inventory_keeps_source_conditions_without_entity_assignment(self):
        self.write_paper("p", [{**self.rows[2], "text": "EG uses 7.5% solution and 3 mM buffer for 10 min at 37 °C."}])
        p = self.build(persist=False)["papers"][0]
        details = p["protocol_variants"][0]["details"]
        self.assertEqual([i["value"] for i in details["concentration"]["items"]], ["7.5%", "3 mM"])
        self.assertEqual(details["duration"]["items"][0]["value"], "10 min")
        self.assertEqual(details["temperature"]["items"][0]["value"], "37 °C")
        self.assertEqual(details["equipment"]["status"], "not_annotated")
        self.assertTrue(all("unresolved" in i["assignment"] for i in details["concentration"]["items"]))

    def test_result_statistics_groups_non_significance_media_and_figure_only(self):
        result = self.build(persist=False)["papers"][0]["results"][0]
        self.assertEqual(result["outcome"]["p"], "0.4")
        self.assertEqual(result["outcome"]["n"], "8")
        self.assertEqual(result["intervention"]["comparator"], "control")
        self.assertIsNone(result["model"]["origin"])
        self.assertEqual(result["source_objects"], ["p:F1"])
        self.assertIn("did not differ significantly", result["sources"][0]["text"])
        self.unit["outcome"]["value_source"] = "figure_only"
        result = self.build(persist=False)["papers"][0]["results"][0]
        self.assertIsNone(result["outcome"]["values"])
        self.assertIsNone(result["outcome"]["p"])

    def test_conflicts_require_existing_active_relation_and_preserve_history(self):
        self.assertEqual(node_profile.conflict_callout(self.build(persist=False)), [])
        units = [self.unit, {**self.unit, "unit_id": "p:E2"}]
        rows = [{"relation_id": "r", "from_unit": "p:E1", "to_unit": "p:E2", "type": "contradicts", "status": "ai_proposed", "vault": "one"}]
        db.write_jsonl(config.STORE / "relations.jsonl", rows)
        p = node_profile.build(self.record, "one", ["p"], units, persist=False)
        self.assertIn("Differing results across papers: 1", "\n".join(node_profile.conflict_callout(p)))
        self.assertEqual(p["conflicts"][0]["status"], "ai_proposed")
        for status in ("rejected", "stale"):
            db.write_jsonl(config.STORE / "relations.jsonl", [{**rows[0], "status": status}])
            p = node_profile.build(self.record, "one", ["p"], units, persist=False)
            self.assertEqual(node_profile.conflict_callout(p), [])
            self.assertIn(status, "\n".join(node_profile.render(p)))
        db.write_jsonl(config.STORE / "relations.jsonl", [{**rows[0], "status": "human_confirmed", "vault": "other"}])
        self.assertEqual(node_profile.build(self.record, "one", ["p"], units, persist=False)["conflicts"], [])

    def test_cache_validates_output_and_source_related_inputs(self):
        first = self.build()
        self.assertFalse(first["cache_hit"])
        self.assertTrue(self.build()["cache_hit"])
        for name in ("media_manifest.json", "section_profile.json", "protocol_profile.json", "structure.json", "tables.jsonl", "figures.jsonl"):
            path = db.paper_dir("p") / name
            path.write_text("{}\n", encoding="utf-8")
            self.assertFalse(self.build()["cache_hit"], name)
            self.assertTrue(self.build()["cache_hit"], name)
        target = next((config.STORE / "cache/node_profiles").glob("*.json"))
        cached = db.read_json(target)
        cached["profile"]["name"] = "corrupt"
        db.write_json(target, cached)
        self.assertEqual(self.build()["name"], "EG")
        self.assertTrue(self.build(persist=False)["cache_hit"])  # repaired derivative reused
        with patch.object(node_profile, "VERSION", 99):
            self.assertFalse(self.build()["cache_hit"])
        self.unit["intervention"]["dose"] = "new stored field"
        self.assertFalse(self.build()["cache_hit"])
        self.assertTrue(self.build()["cache_hit"])
        target.unlink()
        self.assertFalse(self.build()["cache_hit"])
        template = self.root / "template.py"
        template.write_text("template version 1", encoding="utf-8")
        with patch.object(node_profile, "__file__", str(template)):
            self.assertFalse(self.build()["cache_hit"])
            self.assertTrue(self.build()["cache_hit"])
            template.write_text("template version 2", encoding="utf-8")
            self.assertFalse(self.build()["cache_hit"])

    def test_read_only_and_missing_source_unavailable(self):
        self.build(persist=False)
        self.assertFalse((config.STORE / "cache").exists())
        db.paper_dir("p").joinpath("sentences.jsonl").unlink()
        p = self.build(persist=False)["papers"][0]
        self.assertEqual(p["source_availability"], "unavailable")
        self.assertEqual(p["results"][0]["source_status"], "unresolved")
        self.assertNotIn("WRONG STORED QUOTE", "\n".join(node_profile.render(self.build(persist=False))))

    def test_refresh_renders_typed_profile_and_keeps_human_content(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("CREATE TABLE concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id); CREATE TABLE paper_vaults(paper_id,vault); CREATE TABLE paper_terms(paper_id,term);")
        con.execute("INSERT INTO concept_links VALUES('eg','p','studied',3,'results','p:S1')")
        con.execute("INSERT INTO paper_vaults VALUES('p','one')")
        registry = concepts.Registry([self.record])
        with patch.object(concepts, "load_registry", return_value=registry):
            result = concept_notes.refresh(con, "one", "eg", language="en")
            self.assertEqual(result["created"], 1)
            path = config.vault_dir("one") / "Substances/EG.md"
            old = path.read_text(encoding="utf-8")
            for heading in ("## Concentrations used", "## Measured effects", "## Sources"):
                self.assertIn(heading, old)
            for gone in ("## Synthesis", "Synthesis pending", "## Notes"):
                self.assertNotIn(gone, old.split(notes.AUTO_END)[0])
            self.assertEqual(concept_notes.heading_problems(old), [])
            self.assertNotIn("Old source inventory", old)
            # Existing checked rows and payload stay intact through a template-only
            # update, without calling a provider or changing stored evidence.
            checked = {"text": "Previous checked source sentence.", "evidence": ["p:E1"]}
            state = concept_notes.SYNTHESIS_STATE.search(old)
            done = json.loads(state.group(1))
            done["status"] = "done"
            old = old.replace(state.group(0), "<!-- kb:synthesis-state:" + json.dumps(done, sort_keys=True) + " -->")
            old = old.replace("## Measured effects\n\n", "## Measured effects\n\n- " + checked["text"] +
                              " <!-- kb-synthesis:" + json.dumps(checked) + " -->\n\n")
            path.write_text(old + "Human idea remains.\n", encoding="utf-8")
            with patch.object(concept_notes, "FORMAT_VERSION", 999), patch.object(concept_notes, "_generate_synthesis", side_effect=AssertionError("No AI allowed")):
                result = concept_notes.refresh(con, "one", "eg", language="en")
            refreshed = path.read_text(encoding="utf-8")
            self.assertIn(checked["text"], refreshed)
            self.assertEqual(json.loads(concept_notes.SYNTHESIS_STATE.search(refreshed).group(1))["status"], "done")
            result = concept_notes.refresh(con, "one", "eg", language="en")
            self.assertEqual(result["updated"], 1)
            self.assertEqual(concept_notes.refresh(con, "one", "eg", language="en")["unchanged"], 1)
            self.write_paper("p", [{**r, "text": r["text"] + " source revision"} for r in self.rows])
            result = concept_notes.refresh(con, "one", "eg", language="en")
            self.assertEqual(result["updated"], 1)
            self.assertIn("Human idea remains.", path.read_text(encoding="utf-8"))
        con.close()

    def test_node_note_lists_its_result_notes_rl10_a6(self):
        from kbsys import conform_graph
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        results = config.vault_dir("one") / "Results"
        results.mkdir(parents=True)

        def evd(stem, body, **extra):
            front = {"type": "result", "id": f"p:{stem}", "predicate": "reports", "subject": ["not stated"], **extra, **body}
            (results / f"{stem}.md").write_text("---\n" + yaml_dump(front) + "---\n# x\n", encoding="utf-8")
        yaml_dump = lambda d: __import__("yaml").safe_dump(d, allow_unicode=True, sort_keys=False)
        evd("EVD-b β µ", {"subject": ["[[EG]]"], "object": ["[[Other]]"]})   # list subject
        evd("EVD-a γ", {"subject": ["not stated"], "object": "[[eg|EG]]"}, predicate="increases")  # scalar object, other casing
        evd("EVD-old", {"subject": ["[[EG]]"], "object": ["[[EG]]"]}, retired=True)
        (results / "CLM-1.md").write_text("---\ntype: claim\nsubject: '[[EG]]'\nobject: '[[EG]]'\n---\n", encoding="utf-8")
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript("CREATE TABLE concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id); CREATE TABLE paper_vaults(paper_id,vault); CREATE TABLE paper_terms(paper_id,term);")
        con.execute("INSERT INTO concept_links VALUES('eg','p','studied',3,'results','p:S1')")
        con.execute("INSERT INTO paper_vaults VALUES('p','one')")
        path = config.vault_dir("one") / "Substances/EG.md"
        with patch.object(concepts, "load_registry", return_value=concepts.Registry([self.record])):
            self.assertEqual(concept_notes.refresh(con, "one", "eg", language="en")["created"], 1)
            text = path.read_text(encoding="utf-8")
            section = text.split("## Results\n", 1)[1].split("\n\n", 1)[0]
            self.assertEqual(section.splitlines(), ["- [[EVD-a γ]] — increases", "- [[EVD-b β µ]] — reports"])
            self.assertNotIn("EVD-old", text)
            self.assertNotIn("CLM-1", text)
            self.assertEqual(concept_notes.heading_problems(text), [])
            self.assertEqual(concept_notes.refresh(con, "one", "eg", language="en")["unchanged"], 1)  # not a human edit
            evd("EVD-c", {"subject": ["[[EG]]"], "object": ["[[EG]]"]})
            self.assertEqual(concept_notes.refresh(con, "one", "eg", language="en")["updated"], 1)
            self.assertIn("- [[EVD-c]] — reports", path.read_text(encoding="utf-8"))
        con.close()
        pairs = [(f"Results/{p.name}", p.read_text(encoding="utf-8")) for p in results.glob("*.md")]
        pairs.append(("Substances/EG.md", path.read_text(encoding="utf-8")))
        pairs.append(("Substances/Other.md", "x"))
        problems = [e for e in conform_graph.result_notes_name_subject_predicate_object(Vault("one", config.vault_dir("one"), notes=pairs))
                    if "does not list" in e]
        self.assertEqual([e for e in problems if "'EG'" in e or "'eg'" in e], [])
        self.assertEqual(len(problems), 1, problems)  # only the Other node (a plain text fixture) lacks the link

    def test_results_section_keeps_not_significant_and_negated(self):  # evidence rule 4: a hedge is never dropped
        note = lambda **fm: "---\n" + "".join(f"{k}: {v}\n" for k, v in {"type": "result", "subject": "'[[EG]]'", **fm}.items()) + "---\n"
        index = concept_notes.result_back_links([
            ("Results/EVD-ns.md", note(predicate="increases", significance="ns")),
            ("Results/EVD-ns0.md", note(predicate="no_significant_change", significance="ns")),
            ("Results/EVD-neg.md", note(predicate="decreases", negated="true", significance="significant")),
            ("Results/EVD-nr.md", note(predicate="increases", significance="not_reported"))])
        self.assertEqual(concept_notes.result_link_lines(index["eg"]), [
            "- [[EVD-neg]] — decreases (negated)", "- [[EVD-nr]] — increases",
            "- [[EVD-ns]] — increases (not significant)", "- [[EVD-ns0]] — no_significant_change"])

    def test_results_section_is_the_last_to_be_cut_by_the_node_cap(self):
        rows = "\n\n".join(f"filler row {i} " + "x" * 200 for i in range(40))
        want = [f"- [[EVD-{i}]] — reports" for i in range(30)]
        block = "\n".join([notes.AUTO_START, "## Key findings", "", rows, "", "## Results", *want, "",
                           "## Sources", "", rows, "", notes.AUTO_END, ""])
        capped = concept_notes._cap_node_block(block, 6000)
        self.assertLessEqual(len(capped.encode("utf-8")), 6000)
        self.assertIn("## Results\n" + "\n".join(want), capped)
        self.assertNotIn("filler row 39", capped)

    def test_concept_ids_refresh_only_those_concepts_and_their_parents(self):
        records = [{**self.record, "concept_id": cid, "name": cid, "parents": parents}
                   for cid, parents in (("top", []), ("mid", ["top"]), ("leaf", ["mid"]), ("other", []))]
        seen = []
        con = sqlite3.connect(":memory:")
        with patch.object(concepts, "load_registry", return_value=concepts.Registry(records)), \
                patch.object(concept_notes, "_collect", side_effect=lambda c, rec, *a: seen.append(rec["concept_id"]) or (set(), [], [])):
            concept_notes.refresh(con, "one", concept_ids={"mid"})
            self.assertEqual(sorted(seen), ["mid", "top"])
            seen.clear()
            concept_notes.refresh(con, "one", concept_ids=set())
            self.assertEqual(seen, [])
            concept_notes.refresh(con, "one")
            self.assertEqual(len(seen), 4)
        con.close()


class _Registry:
    def __init__(self, *records):
        self.records = list(records)

    def lookup(self, value):
        value = str(value).casefold()
        return next((r for r in self.records if r["name"].casefold() == value), None)


class NodeContentTests(unittest.TestCase):
    def test_figure_render_uses_caption_and_page_link_without_unlocated_image(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            papers = root / "papers"
            vaults = root / "vaults"
            folder = papers / "chen"
            folder.mkdir(parents=True)
            (vaults / "v" / "_Attachments" / "chen").mkdir(parents=True)
            db.write_jsonl(folder / "sentences.jsonl", [
                {"sentence_id": "chen:S1", "text": "Figure 5 shows H3K9me3 level.", "page": 4},
                {"sentence_id": "chen:S2", "text": "H3K9me3 was higher in the treated group.", "page": 5},
            ])
            db.write_jsonl(folder / "figures.jsonl", [{"figure_id": "chen:F5", "label": "Figure 5",
                                                        "caption_sentence_ids": ["chen:S1"],
                                                        "mentioned_in": ["chen:S2"]}])
            db.write_jsonl(folder / "tables.jsonl", [])
            db.write_json(folder / "media_manifest.json", {"bundles": [{"object_id": "chen:F5",
                "source_locator": {"region_kind": "unlocated_object", "locator_basis": "unlocated", "page": 4},
                "crop": {"status": "unavailable"}}]})
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=papers,
                                VAULTS_DIR=vaults):
                out = "\n".join(concept_notes._related_figures(
                    {"name": "H3K9me3", "synonyms": []},
                    {"vault": "v", "papers": [{"paper_id": "chen", "cite": "Lee 2025",
                                                   "note_name": "Lee 2025", "results": [{"source_objects": ["chen:F5"]}]}]}))
            self.assertIn("### Figure 5 — ", out)
            self.assertIn("(Lee 2025)", out)
            self.assertNotIn("chen:F5", out)
            self.assertIn("image not in the PDF — see the original", out)
            self.assertIn("- H3K9me3 was higher in the treated group. ([[Lee 2025|Lee 2025, p. 5]])", out)
            self.assertNotIn("S2", out)
            self.assertNotIn("![[", out)

    def test_node_note_machine_text_is_english_with_no_local_link(self):
        """VN-08: a capped node note with a hedge line, an AI image reading and the papers report has no Vietnamese-letter machine line."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "papers" / "chen"
            folder.mkdir(parents=True)
            (root / "vaults" / "v" / "_Attachments" / "chen").mkdir(parents=True)
            db.write_jsonl(folder / "sentences.jsonl", [{"sentence_id": "chen:S1", "text": "Figure 5 shows H3K9me3 level.", "page": 4}])
            db.write_jsonl(folder / "figures.jsonl", [{"figure_id": "chen:F5", "label": "Figure 5", "caption_sentence_ids": ["chen:S1"]}])
            db.write_jsonl(folder / "tables.jsonl", [])
            db.write_json(folder / "media_manifest.json", {"bundles": [{"object_id": "chen:F5", "crop": {"status": "unavailable"},
                "source_locator": {"region_kind": "unlocated_object", "locator_basis": "unlocated", "page": 4}}]})
            reading = type("Reading", (), {"load": staticmethod(lambda pid, oid: {"summary": "Higher band.", "inconsistent": True}),
                                           "warning": staticmethod(lambda r: "")})
            unit = {"unit_id": "chen:E1", "paper_id": "chen", "statement": "H3K9me3 may rise.",
                    "paper": {"note_name": "Lee 2025", "evidence_type": "review"}}
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers", VAULTS_DIR=root / "vaults"), \
                    patch.object(concept_notes, "figure_reading", reading):
                figures = concept_notes._related_figures({"name": "H3K9me3", "synonyms": []}, {"vault": "v", "papers": [
                    {"paper_id": "chen", "note_name": "Lee 2025", "results": [{"source_objects": ["chen:F5"]}]}]})
                report = concept_notes._papers_report({}, {"papers": []}, [unit])
            rows = [f"> [!quote]- [[Lee 2025|Lee 2025, p. {i}]] · results\n> " + "text " * 60 + "\n" for i in range(12)]
            body = ["## Findings", "", concept_notes._node_hedge("It may rise."), "", *report, *figures, *rows]
            block = notes.cap_auto_block("\n".join([notes.AUTO_START, *body, notes.AUTO_END]), 2500, "concept")
            self.assertIn("not shown; the complete list is in the dashboard.", block)
            self.assertIn("Hedged wording", block)
            self.assertIn("read from the image by AI", block)
            self.assertNotIn("127.0.0.1", block)
            note = f"---\ntype: concept\n---\n# H3K9me3\n\n{block}\n## My notes\n"
            conform.load_checks()
            self.assertEqual(conform.violations("VN-08", Vault("v", root / "v", notes=[("Concepts/H3K9me3.md", note)], attachments=[])), [])

    def test_summary_is_the_definition_of_this_node_or_absent(self):
        record = {"name": "5mC", "synonyms": ["5-methylcytosine"]}
        units = [
            {"status": "ok", "quote": "H3K9me3 is a repressive mark found in heterochromatin."},
            {"status": "ok", "quote": "5mC levels decreased after warming."},
            {"status": "ok", "quote": "5mC is a methylated form of cytosine found in DNA."},
        ]
        self.assertEqual(concept_notes._source_summary(record, units), "5mC is a methylated form of cytosine found in DNA.")
        # a sentence that merely contains the name is not a summary
        self.assertEqual(concept_notes._source_summary(record, units[1:2]), "")
        self.assertEqual(concept_notes._source_summary(record, []), "")
        # the accepted card definition comes first
        profile = {"card": {"statements": [{"field": "definition", "text": "Card definition of 5mC.",
                                             "sentence_ids": ["p:S1"]}]}}
        self.assertEqual(concept_notes._source_summary(record, units, profile), "Card definition of 5mC.")

    def test_summary_is_trimmed_at_a_word_boundary_and_cleaned(self):
        record = {"name": "Chromatin", "synonyms": []}
        long_text = "Chromatin is a complex of DNA and proteins " + "that packages the genome^51 " * 12 + "in the nucleus."
        summary = concept_notes._source_summary(record, [{"status": "ok", "quote": long_text}])
        self.assertLessEqual(len(summary), concept_notes.SUMMARY_LIMIT)
        self.assertTrue(summary.endswith("…") and not summary[:-1].endswith(" "), summary)
        self.assertNotIn("^", summary)
        self.assertEqual(concept_notes._source_summary(record, [{"status": "ok", "quote": "Chromatin is a complex of DNA^1,2 and protein."}]),
                         "Chromatin is a complex of DNA and protein.")

    def test_moc_summary_counts_members_by_type(self):
        registry = _Registry(
            {"concept_id": "topic", "name": "Cryopreservation", "type": "topic", "parents": []},
            {"concept_id": "m", "name": "Vitrification", "type": "method", "parents": ["topic"]},
            {"concept_id": "s", "name": "EG", "type": "molecule", "parents": ["topic"]},
            {"concept_id": "s2", "name": "DMSO", "type": "molecule", "parents": ["topic"]},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for folder, name in (("Methods", "Vitrification"), ("Substances", "EG"), ("Substances", "DMSO")):
                (root / "vaults" / "v" / folder).mkdir(parents=True, exist_ok=True)
                (root / "vaults" / "v" / folder / f"{name}.md").write_text("", encoding="utf-8")
            with patch.multiple(config, ROOT=root, VAULTS_DIR=root / "vaults"):
                summary = concept_notes._moc_summary({"concept_id": "topic", "name": "Cryopreservation"}, registry, "v")
        self.assertIn("1 methods", summary)
        self.assertIn("2 substances", summary)

    def test_source_typed_sections_include_required_method_and_substance_fields(self):
        profile = {"papers": [{
            "paper_id": "p", "fields": {"roles_context": {"items": [{"roles": ["intervention"], "unit_id": "p:E1"}]}},
            "protocol_variants": [{
                "name": "Straw loading",
                "details": {
                    "chemical": {"items": [{"value": "EG", "sources": []}]},
                    "concentration": {"items": [{"value": "15%", "sources": [
                        {"sentence_id": "p:S1", "text": "Embryos were soaked in 15% EG for 5 min."}]}]},
                    "equipment": {"items": [{"value": "straw", "sources": []}]},
                    "temperature": {"items": [{"value": "-196 °C", "sources": []}]},
                },
                "steps": [{"text": "load embryos", "sentence_id": "p:S1"}],
                "basis": "Methods subsection inventory",
            }],
            "results": [{"statement": "EG increased survival."}],
        }]}
        with patch.object(concept_notes.node_profile, "graph_node_type", return_value="method"):
            method = "\n".join(concept_notes._typed_sections({"name": "Vitrification"}, profile, None))
        for heading in ("## Protocol steps", "## Reagents & concentrations", "## Equipment & parameters",
                        "## Variants across papers"):
            self.assertIn(heading, method)
        self.assertNotIn("## Purpose", method)  # no definition sentence -> hidden, no placeholder
        self.assertIn("-196 °C", method)
        self.assertNotIn("hưa tìm thấy", method)
        with patch.object(concept_notes.node_profile, "graph_node_type", return_value="substance"):
            substance = "\n".join(concept_notes._typed_sections({"name": "EG"}, profile, None))
        self.assertIn("## Concentrations used", substance)
        self.assertIn("15% EG", substance)
        self.assertNotIn("source roles", substance)

    def test_aliases_are_written_to_node_frontmatter(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults", LOGS_DIR=root / "logs"):
                record = {"concept_id": "local:h4", "name": "H4", "type": "molecule",
                          "synonyms": ["histone H4"], "parents": []}
                block = "\n".join([notes.AUTO_START, "## Summary", "", notes.AUTO_END])
                concept_notes._write_note("v", record, block, "f" * 40)
                text = (root / "vaults" / "v" / "Substances" / "H4.md").read_text(encoding="utf-8")
            self.assertIn("aliases:", text)
            self.assertIn("histone H4", text)


class NodeNoteFixTests(unittest.TestCase):
    def test_structured_values_are_readable_and_old_inventory_is_gone(self):
        profile = {
            "entity_kind": "chemical", "name": "EG", "aliases": [], "vault": "v", "card": None,
            "conflicts": [], "scope_note": "source scope",
            "papers": [{"paper_id": "p", "fields": {field: {"items": []} for field in node_profile.FIELDS},
                        "context": [], "protocol_variants": [], "results": [{
                            "unit_id": "p:E1", "status": "ok", "source_status": "resolved",
                            "model": {"species": "mouse", "stage": "MII"},
                            "intervention": {"agent": "EG"},
                            "outcome": {"measure": "survival", "values": ["42%"], "n": 8,
                                        "p": "0.4", "value_source": "text"},
                            "quality": {}, "source_objects": [],
                            "sources": [{"paper_id": "p", "note_name": "Paper", "sentence_id": "p:S1",
                                         "section": "results", "page": 1, "text": "EG increased survival."}],
                        }]}],
        }
        rendered = "\n".join(node_profile.render(profile))
        self.assertIn("species: mouse", rendered)
        self.assertIn("measure: survival", rendered)
        self.assertNotIn("stored model/intervention:", rendered)
        self.assertNotIn('{"model"', rendered)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults"):
                block = concept_notes._render(
                    {"concept_id": "eg", "name": "EG", "type": "molecule", "synonyms": []},
                    {}, [], [], [], [], "pending", {"status": "pending"}, "f" * 40, None,
                    profile={**profile, "papers": []})
            text = block if isinstance(block, str) else "\n".join(block)
        self.assertNotIn("Old source inventory", text)
        self.assertNotIn("## ", text)  # no data -> no section at all, not even "Sources"

    def test_caption_block_is_separated_from_figure_mentions(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            paper = root / "papers" / "p"
            paper.mkdir(parents=True)
            db.write_jsonl(paper / "sentences.jsonl", [
                {"sentence_id": "p:S1", "text": "Figure 1. Caption sentence.", "page": 1},
                {"sentence_id": "p:S2", "text": "The result is shown in Figure 1.", "page": 2},
            ])
            db.write_jsonl(paper / "figures.jsonl", [{"figure_id": "p:F1", "label": "Figure 1",
                                                       "caption_sentence_ids": ["p:S1"],
                                                       "mentioned_in": ["p:S2"]}])
            db.write_jsonl(paper / "tables.jsonl", [])
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults"):
                lines = concept_notes._related_figures(
                    {"name": "result", "synonyms": []},
                    {"vault": "v", "papers": [{"paper_id": "p", "cite": "Paper 2026",
                                                  "note_name": "Paper 2026", "results": []}]})
        text = "\n".join(lines)
        self.assertIn("> Figure 1. Caption sentence.\n\n- The result is shown in Figure 1. ([[Paper 2026|Paper 2026, p. 2]])", text)

    def test_species_and_stage_cells_are_short_and_follow_fallback_order(self):
        registry = _Registry(
            {"name": "Mouse", "type": "species", "synonyms": ["murine"]},
            {"name": "Bovine", "type": "species", "synonyms": ["cattle"]},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            folder = root / "papers" / "p"
            folder.mkdir(parents=True)
            db.write_jsonl(folder / "sentences.jsonl", [])
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers"):
                short = concept_notes._paper_model_labels(
                    "p", {"text": "Bovine MII oocytes"}, registry=registry)
                long = concept_notes._paper_model_labels(
                    "p", {"species": "Murine retinal neurons were investigated; these are mouse retinal cells.",
                          "stage": "MII oocytes from donors were analyzed."}, registry=registry)
        self.assertEqual(short, ("Bovine MII oocytes", "Bovine MII oocytes"))
        self.assertEqual(long, ("Mouse (from paper)", "MII (from paper)"))
        self.assertLess(len(long[0]), 30)
        self.assertLess(len(long[1]), 30)

    def test_conflict_callout_keeps_both_directions(self):
        text = "\n".join(node_profile.conflict_callout({
            "vault": "v", "name": "H3K9me3", "papers": [{"paper_id": "a", "results": [
                {"unit_id": "a:E1", "outcome": {"measure": "H3K9me3 level", "direction": "increase"}}
            ]}, {"paper_id": "b", "results": [
                {"unit_id": "b:E1", "outcome": {"measure": "H3K9me3 level", "direction": "decrease"}}
            ]}], "conflicts": [{"relation_id": "r", "from_unit": "a:E1", "to_unit": "b:E1",
                                 "type": "direction_differs", "status": "ai_proposed"}]}, {}))
        self.assertIn("direction: increase ↔ decrease", text)

    def test_its_canonical_name_and_case_only_migration_preserve_human_text(self):
        self.assertEqual(concepts._display_name("Its"), "ITS")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            old = root / "vaults" / "v" / "Substances" / "Its.md"
            old.parent.mkdir(parents=True)
            old.write_text("---\ntype: substance\n---\n# ITS\n\n" + notes.AUTO_START +
                           "\nold\n" + notes.AUTO_END + "\n\n## Notes\nHuman note\n", encoding="utf-8")
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults", LOGS_DIR=root / "logs"):
                concept_notes._write_note(
                    "v", {"concept_id": "its", "name": "ITS", "type": "molecule", "synonyms": []},
                    "<!-- kb:auto:start -->\n[[Its]]\n<!-- kb:auto:end -->", "f" * 40,
                    registry=concepts.Registry([{"concept_id": "its", "name": "ITS", "type": "molecule", "synonyms": []}]))
            target = root / "vaults" / "v" / "Substances" / "ITS.md"
            self.assertTrue(target.exists())
            self.assertNotIn("Its.md", [candidate.name for candidate in target.parent.glob("*.md")])
            self.assertIn("Human note", target.read_text(encoding="utf-8"))
            self.assertIn("[[ITS]]", target.read_text(encoding="utf-8"))

    def test_quote_refs_use_read_time_sentence_continuations(self):
        rows = {
            "p:S0276": {"sentence_id": "p:S0276", "text": "In group B, no significant change of",
                        "section": "results", "page": 8, "seq": 276, "continues": "p:S0286"},
            "p:S0286": {"sentence_id": "p:S0286", "text": "H3K9me3 was observed.",
                        "section": "results", "page": 9, "seq": 286},
        }
        refs = node_profile._refs(["p:S0276"], rows, "p", {"title": "Paper"})
        self.assertEqual({ref["sentence_id"] for ref in refs}, {"p:S0276", "p:S0286"})
        self.assertIn("H3K9me3 was observed.", " ".join(ref["text"] for ref in refs))

    def test_typed_fields_and_method_conditions_are_readable(self):
        fields = {
            "definitions": {"items": [{"sources": [{"text": "EG is a cryoprotectant."}]}]},
            "full_names": {"items": [{"value": "Ethylene glycol", "abbreviation": "EG"}]},
            "roles_context": {"items": [{"unit_id": "p:E1", "roles": ["intervention"]}]},
        }
        profile = {"papers": [{"paper_id": "p", "fields": fields, "protocol_variants": [{
            "details": {"chemical": {"items": [{"value": "Sucrose", "sources": []}]},
                         "concentration": {"items": [{"value": "5% CO2", "sources": []}]},
                         "duration": {"items": [{"value": "10 min", "sources": []}]}},
            "steps": [{"text": "load embryos", "sentence_id": "p:S1"}], "basis": "Methods"}],
            "results": []}]}
        with patch.object(concept_notes.node_profile, "graph_node_type", return_value="method"):
            text = "\n".join(concept_notes._typed_sections({"name": "Sucrose"}, profile, None))
        self.assertIn("## Purpose", text)
        self.assertIn("EG is a cryoprotectant.", text)
        self.assertIn("## Reagents & concentrations", text)
        self.assertIn("## Equipment & parameters", text)
        self.assertTrue(text.index("5% CO2") > text.index("## Equipment & parameters"))
        self.assertNotIn("5% CO2 Sucrose", text)
        self.assertNotIn("{'", text)
        self.assertNotIn("checked automatically", text)   # no card, no card line
        profile["card"] = {"card_check": "passed", "statements": [
            {"field": "definition", "text": "EG is a cryoprotectant.", "sentence_ids": ["p:S1", "p:S2", "p:S1"]}]}
        with patch.object(concept_notes.node_profile, "graph_node_type", return_value="method"):
            lines = concept_notes._typed_sections({"name": "Sucrose"}, profile, None)
        line = concept_notes.CARD_CHECK_LINE.format(n=2)   # NC-04: N = distinct cited sentence ids
        self.assertIn(line, lines)
        self.assertEqual(lines.index(line), next(i for i, x in enumerate(lines) if x.startswith("## ")) - 2)
        profile["card"]["card_check"] = None
        self.assertNotIn(line, concept_notes._typed_sections({"name": "Sucrose"}, profile, None))

    def test_moc_members_are_explicit_descendants_and_summary_is_grounded(self):
        registry = _Registry(
            {"concept_id": "topic", "name": "Cryo", "type": "topic", "parents": []},
            {"concept_id": "child", "name": "EG", "type": "molecule", "parents": ["topic"]},
            {"concept_id": "loose", "name": "Loose", "type": "concept", "parents": []},
        )
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("EG", "Loose"):
                (root / "vaults" / "v" / "Substances").mkdir(parents=True, exist_ok=True)
                (root / "vaults" / "v" / "Substances" / f"{name}.md").write_text("", encoding="utf-8")
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults"):
                text = "\n".join(concept_notes._render_moc(
                    {"concept_id": "topic", "name": "Cryo"}, {"p": True}, [], registry,
                    {"vault": "v", "conflicts": []}, "v"))
        self.assertIn("[[EG]]", text)
        self.assertNotIn("[[Loose]]", text)
        self.assertEqual(concept_notes._source_summary(
            {"name": "EG", "synonyms": ["ethylene glycol"]},
            [{"status": "ok", "quote": "Ethylene glycol was used."},
             {"status": "ok", "quote": "EG is a cryoprotectant."}]),
            "EG is a cryoprotectant.")


if __name__ == "__main__":
    unittest.main()


class ModelLabelsReviewTests(unittest.TestCase):
    def test_review_and_cited_units_use_only_own_wording_and_missing_is_blank(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "papers" / "p").mkdir(parents=True)
            db.write_jsonl(root / "papers" / "p" / "sentences.jsonl", [])
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers"), \
                    patch.object(concept_notes.structure, "_fresh_overview",
                                 return_value={"groups_models": [{"text": "Human oocytes"}]}):
                own = {"unit_id": "p:E1", "unit_type": "cited_statement",
                       "statement": "Copper stress in porcine oocytes was reported."}
                self.assertEqual(concept_notes._paper_model_labels("p", {}, unit=own),
                                 ("Pig (from paper)", "oocytes (from paper)"))
                review = {"unit_id": "p:E2", "unit_type": "result", "statement": "Marker rises.",
                          "paper": {"evidence_type": "review"}}
                self.assertEqual(concept_notes._paper_model_labels("p", {}, unit=review), ("", ""))
                result = {"unit_id": "p:E3", "unit_type": "result", "statement": "Marker rises."}
                self.assertEqual(concept_notes._paper_model_labels("p", {}, unit=result)[0], "Human oocytes")

    def test_antibody_host_is_not_the_species(self):
        self.assertIsNone(concept_notes._model_term("detected with goat anti-mouse IgG", "species"))


class SpeciesGuardTests(unittest.TestCase):
    def test_reagent_hosts_and_generic_words_are_not_species(self):
        for text in ("goat anti-mouse IgG", "rabbit polyclonal antibody", "mouse monoclonal anti-CD44", "raised in rabbit",
                     "normal goat serum", "donkey anti-rabbit IgG", "rat anti-mouse", "fetal bovine serum", "all animals", "patients", "primer CAT gene"):
            self.assertIsNone(concept_notes._model_term(text, "species"), text)
        self.assertEqual(concept_notes._model_term("stained with goat anti-mouse IgG in mice", "species"), "Mouse")
        registry = type("R", (), {"records": [{"name": "Animal", "type": "species"}]})()
        self.assertIsNone(concept_notes._model_term("animal oocytes", "species", registry))

    def test_missing_config_does_not_crash(self):
        with patch.object(concept_notes.config, "_read_yaml", return_value={}):
            self.assertIsNone(concept_notes._model_term("porcine oocytes", "species"))
