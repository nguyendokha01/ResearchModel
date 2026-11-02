"""Offline typed-node presentation tests.

Cards are deliberately supplied through the D3.46 public load seam.  These tests
never call a provider and never use the research checkout.
"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from kbsys import config, db, node_profile, concept_notes, notes, concepts, presentation


class _Cards:
    cards = {}

    @classmethod
    def load(cls, vault, concept_id):
        return cls.cards.get(concept_id)


class TypedNodeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(config, ROOT=root, STORE=root / "store",
                                    PAPERS_DIR=root / "store/papers", VAULTS_DIR=root / "vaults",
                                    CONFIG_DIR=root / "config", LOGS_DIR=root / "logs")
        self.paths.start()
        self.record = {"concept_id": "eg", "name": "EG", "type": "molecule",
                       "synonyms": [], "parents": []}
        self.rows = [
            {"sentence_id": "p:S1", "seq": 1, "text": "Ethylene glycol is a cryoprotectant.",
             "section": "introduction", "page": 1},
            {"sentence_id": "p:S2", "seq": 2, "text": "Ethylene glycol was used at 10% for 5 min.",
             "section": "methods", "page": 2},
            {"sentence_id": "p:S3", "seq": 3, "text": "EG increased survival.",
             "section": "results", "page": 3},
        ]
        folder = db.paper_dir("p")
        db.write_json(folder / "metadata.json", {"paper_id": "p", "title": "Fixture",
                                                  "note_name": "Fixture", "first_author": "Doe", "year": 2026,
                                                  "evidence_type": "primary_research"})
        db.write_jsonl(folder / "sentences.jsonl", self.rows)
        self.unit = {"paper_id": "p", "unit_id": "p:E1", "unit_type": "result", "status": "ok",
                     "sentence_ids": ["p:S3"], "statement": "Stored result statement", "quote": "wrong card quote",
                     "model": {"species": "mouse", "stage": "MII"},
                     "outcome": {"measure": "survival", "direction": "increase"},
                     "terms": {"molecules": ["EG"]}, "pages": [3], "sections": ["results"],
                     "paper": {"paper_id": "p", "title": "Fixture", "note_name": "Fixture",
                               "evidence_type": "primary_research"}}
        db.write_jsonl(folder / "evidence.jsonl", [self.unit])
        (config.vault_dir("one") / "Substances").mkdir(parents=True)
        (config.vault_dir("one") / "Concepts").mkdir(parents=True)
        (config.vault_dir("one") / "Methods").mkdir(parents=True)
        (config.vault_dir("one") / "Organisms").mkdir(parents=True)
        (config.vault_dir("one") / "_Maps").mkdir(parents=True)
        (config.vault_dir("one") / "Substances" / "Existing.md").write_text("---\ntype: substance\n---\n", encoding="utf-8")
        self.card = {"node_type": "substance", "status": "accepted",
                     "fields": {"substance_class": "small_molecule", "chem_roles": ["cryoprotectant"],
                                "study_role": ["intervention"], "measures": ["Existing", "Missing"]},
                     "statements": [
                         {"field": "definition", "text": "AI card definition", "sentence_ids": ["p:S1"]},
                         {"field": "function", "text": "AI card function", "sentence_ids": ["p:S3"], "hedge": "hedged"},
                     ]}
        _Cards.cards = {"eg": self.card}
        self.card_patch = patch.object(node_profile, "node_cards", _Cards)
        self.card_patch.start()

    def tearDown(self):
        self.card_patch.stop()
        self.paths.stop()
        self.temp.cleanup()

    def build(self, record=None, units=None):
        return node_profile.build(record or self.record, "one", ["p"],
                                  [self.unit] if units is None else units, persist=False)

    def render(self, profile=None, record=None, units=None):
        profile = profile or self.build(record, units)
        return concept_notes._render(record or self.record, {"p": [{"paper_id": "p"}]},
                                     [self.unit] if units is None else units, [], [], [],
                                     "pending", {"status": "pending"}, "f" * 40, None, profile=profile)

    def test_substance_card_uses_typed_order_and_source_quotes(self):
        text = self.render()
        self.assertLess(text.index("## Definition"), text.index("## Measured effects"))
        self.assertLess(text.index("## Measured effects"), text.index("## Sources"))
        self.assertIn("AI card definition", text)
        self.assertIn("Ethylene glycol is a cryoprotectant.", text)
        self.assertNotIn("p. 1\n> AI card definition", text)  # card text is never inside a quote box
        props = node_profile.typed_machine_properties(self.record, self.build(), "one")
        self.assertIn("Missing", props["related_unresolved"])

    def test_node_note_hides_machine_ids_and_folds_machine_properties(self):
        # N3b: readable "(Author Year, p. N)" links in the body, exact IDs in ONE Source IDs callout, five machine
        # Properties folded under kb_machine.
        profile = self.build()
        text = self.render(profile)
        self.assertEqual(presentation.machine_ids(text), [])
        self.assertEqual(text.count("[!info]- Source IDs"), 1)
        self.assertIn("[[Fixture|Doe 2026, p. 1]]", text)
        callout = text.split("[!info]- Source IDs", 1)[1]
        self.assertIn("p:S3", callout)
        self.assertIn("p:E1", callout)
        self.assertEqual(presentation.machine_ids("# Fixture\n\n" + text), [])
        concept_notes._write_note("one", self.record, text, "f" * 40, [self.unit], profile)
        written = (config.vault_dir("one") / "Substances" / "EG.md").read_text(encoding="utf-8")
        front = yaml.safe_load(written.split("---\n", 2)[1])
        self.assertEqual(set(front["kb_machine"]), set(presentation.NODE_MACHINE_KEYS))
        for key in presentation.NODE_MACHINE_KEYS:
            self.assertNotIn(key, front)
        for key in ("type", "aliases", "kb_kind", "kb_roles", "evidence_count", "source_papers", "has_conflict"):
            self.assertIn(key, front)
        # idempotent for a note that already carries the nested key
        self.assertEqual(presentation.fold_machine_keys(dict(front)), front)

    def test_typed_links_and_human_text(self):
        profile = self.build()
        path = config.vault_dir("one") / "Substances" / "EG.md"
        path.write_text("---\nstatus: human\ncustom: keep\n---\n# EG\n\n" +
                        "<!-- kb:auto:start — this block is updated by the software; write your own notes outside it -->\nold\n<!-- kb:auto:end -->\n\n## Notes\nHuman text\n", encoding="utf-8")
        old_tail = path.read_text(encoding="utf-8").split("## Notes", 1)[1]
        concept_notes._write_note("one", self.record, self.render(profile), "f" * 40, [self.unit], profile)
        text = path.read_text(encoding="utf-8")
        self.assertIn("[[Existing]]", text)
        self.assertNotIn("[[Missing]]", text)
        self.assertIn("custom: keep", text)
        self.assertIn("status: human", text)
        self.assertEqual(text.split("## Notes", 1)[1], old_tail)

    def test_no_card_fallback_and_conflict_fields(self):
        _Cards.cards = {}
        units = [self.unit, {**self.unit, "unit_id": "p:E2", "sentence_ids": ["p:S1"]}]
        db.write_jsonl(config.STORE / "relations.jsonl", [{"relation_id": "r1", "from_unit": "p:E1",
                      "to_unit": "p:E2", "type": "contradicts", "status": "ai_proposed", "vault": "one"}])
        profile = self.build(units=units)
        text = self.render(profile=profile, units=units)
        self.assertNotIn("[!todo]", text)
        self.assertNotIn("Chưa có hồ sơ AI", text)
        self.assertIn("## Conflicting findings", text)
        self.assertLess(text.index("## Conflicting findings"), text.index("[!conflict]"))
        props = node_profile.typed_machine_properties(self.record, profile, "one")
        self.assertEqual(props["conflict_open"], 1)
        self.assertTrue(props["has_conflict"])

    def test_moc_and_other_card_types_have_type_specific_headings(self):
        stated = lambda text: {"text": text, "sentence_ids": ["p:S1"]}
        for node_type, heading, record, fields in (
                ("technique", "Purpose", {**self.record, "concept_id": "tech", "name": "Assay", "type": "method"},
                 {"principle": stated("Principle text")}),
                ("method", "Protocol steps", {**self.record, "concept_id": "prot", "name": "Protocol", "type": "method"},
                 {"steps": [{"paper": "p", "order": 1, "step": "Load the straw", "sentence_ids": ["p:S2"]}]}),
                ("organism", "Description", {**self.record, "concept_id": "mouse", "name": "Mouse", "type": "species"},
                 {"model_use": [stated("Model use text")]}),
                ("moc", "Overview", {**self.record, "concept_id": "topic", "name": "Topic", "type": "topic"},
                 {"members": ["Existing"]})):
            card = {"node_type": node_type, "status": "accepted", "statements": [], "fields": fields}
            _Cards.cards = {record["concept_id"]: card}
            profile = self.build(record)
            text = self.render(profile=profile, record=record)
            self.assertIn("## " + heading, text)
            if node_type != "moc":
                self.assertEqual(concept_notes.heading_problems(
                    f"---\ntype: {node_profile.graph_node_type(profile)}\n---\n" + text), [])
            if node_type == "moc":
                self.assertEqual(node_profile.graph_node_type(profile), "moc")
                concept_notes._write_note("one", record, text, "f" * 40, [self.unit], profile)
                self.assertTrue((config.vault_dir("one") / "_Maps" / "Topic.md").is_file())

    def test_cap_keeps_typed_content_without_old_inventory(self):
        profile = self.build()
        with patch.object(notes, "NODE_NOTE_MAX_BYTES", 1800):
            block = self.render(profile=profile)
            capped = notes.cap_auto_block(block, 1800, "concept")
        self.assertIn("## Definition", capped)
        self.assertNotIn("Old source inventory", capped)
        self.assertNotIn("127.0.0.1", capped)


class NodeSectionSchemaTests(unittest.TestCase):
    """N3a: one approved section set per node type; empty sections hidden; human zone survives."""

    APPROVED = {
        "concept": ["Definition", "Key findings", "Conflicting findings", "Figures", "Results", "Related notes", "Sources", "My notes"],
        "method": ["Purpose", "Protocol steps", "Reagents & concentrations", "Equipment & parameters",
                   "Variants across papers", "Outcomes", "Conflicting findings", "Figures", "Results", "Sources", "My notes"],
        "substance": ["Definition", "Concentrations used", "Measured effects", "Conflicting findings", "Figures", "Results",
                      "Sources", "My notes"],
        "organism": ["Description", "Key findings", "Conflicting findings", "Figures", "Results", "Sources", "My notes"],
        "cell_type": ["Description", "Markers", "Key findings", "Conflicting findings", "Figures", "Results", "Sources", "My notes"],
    }

    def sections(self, kind, data):
        """H2 list of _typed_sections when every data source returns `data` (a line list)."""
        names = ("_papers_report", "_conflict_lines", "_related_figures", "_related_notes", "_sources_lines", "result_link_lines")
        items = lambda *a, **k: ([{"sids": set(), "key": ("x", id(data)), "pair": lambda: ("real data", "")}]
                                 if any(line.strip() for line in data) else [])
        with patch.multiple(concept_notes, **{n: (lambda *a, **k: list(data)) for n in names},
                            _section_items=items, _card_items=items, finding_units=lambda *a, **k: []):
            lines = concept_notes._typed_sections({"name": "N", "type": "molecule"}, {"entity_kind": kind}, None)
        return [line[3:] for line in lines if line.startswith("## ")]

    def test_headings_equal_the_approved_list_per_type_and_empty_ones_are_hidden(self):
        for kind, approved in self.APPROVED.items():
            self.assertEqual(self.sections(kind, ["- real data", ""]) + ["My notes"], approved, kind)
            self.assertEqual(concept_notes.node_headings(kind), tuple(approved))
            self.assertEqual(self.sections(kind, []), [], kind)
        self.assertEqual(concept_notes.NODE_SECTIONS["technique"], concept_notes.NODE_SECTIONS["method"])

    def test_stale_card_warning_and_class_field_survive(self):
        card = {"card_currency": "current_with_new_evidence", "new_paper_ids": ["a"]}
        with patch.object(concept_notes, "_statement_rows", lambda p, c, f: []):
            lines = concept_notes._typed_sections({"name": "N", "type": "molecule"}, {"entity_kind": "substance", "card": card}, None)
        self.assertTrue(any("New evidence from other papers" in line for line in lines))
        self.assertIn("class", concept_notes.NODE_SECTIONS["substance"][0][1])

    def test_every_card_field_belongs_to_at_most_one_section_of_a_type(self):
        for kind, sections in concept_notes.NODE_SECTIONS.items():
            fields = [f for _, names, _ in sections for f in names]
            self.assertEqual(len(fields), len(set(fields)), kind)

    def test_human_zone_is_my_notes_and_old_notes_heading_text_is_kept(self):
        record = {"concept_id": "eg", "name": "EG", "type": "molecule", "synonyms": []}
        block = f"{notes.AUTO_START}\n<!-- kb:concept-fingerprint:{'f' * 40} -->\n\n## Sources\n\n- x\n\n{notes.AUTO_END}\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults", LOGS_DIR=root / "logs"):
                path = config.vault_dir("v") / "Substances" / "EG.md"
                self.assertEqual(concept_notes._write_note("v", record, block, "f" * 40), "created")
                text = path.read_text(encoding="utf-8")
                self.assertTrue(text.endswith(notes.AUTO_END + "\n\n## My notes\n\n"), text[-80:])
                path.write_text(text + "My idea stays.\n", encoding="utf-8")
                concept_notes._write_note("v", record, block.replace("- x", "- y"), "e" * 40)
                kept = path.read_text(encoding="utf-8")
                self.assertIn("- y", kept)
                self.assertTrue(kept.endswith("## My notes\n\nMy idea stays.\n"), kept[-80:])
                # an old, still empty "## Notes" becomes "## My notes"; one with text is left as the human wrote it
                path.write_text(text.replace("## My notes", "## Notes"), encoding="utf-8")
                concept_notes._write_note("v", record, block.replace("- x", "- z"), "d" * 40)
                self.assertTrue(path.read_text(encoding="utf-8").endswith("\n\n## My notes\n\n"))
                path.write_text(text.replace("## My notes\n\n", "## Notes\nOld human text\n"), encoding="utf-8")
                concept_notes._write_note("v", record, block.replace("- x", "- w"), "c" * 40)
                final = path.read_text(encoding="utf-8")
                self.assertTrue(final.endswith("\n\n## Notes\nOld human text\n"), final[-80:])
                self.assertIn("- w", final)

    def test_heading_problems_flag_unapproved_and_misordered_sections(self):
        def note(kind, *headings):
            return f"---\ntype: {kind}\n---\n{notes.AUTO_START}\n" + "\n".join(f"## {h}" for h in headings) + f"\n{notes.AUTO_END}\n## Notes\n"
        self.assertEqual(concept_notes.heading_problems(note("method", "Purpose", "Sources")), [])
        self.assertEqual(len(concept_notes.heading_problems(note("method", "Sources", "Purpose"))), 1)
        self.assertEqual(len(concept_notes.heading_problems(note("substance", "Definition", "Synthesis"))), 1)
        self.assertEqual(len(concept_notes.heading_problems(note("substance", "Purpose"))), 1)


class SpeciesLinkTests(unittest.TestCase):
    def test_reagent_hosts_are_not_species_links(self):
        bovine = {"type": "species"}
        mouse = {"type": "species"}
        self.assertFalse(concepts._pattern_matches("fetal bovine serum was added.",
                                                    concepts._regex("bovine"), bovine))
        self.assertTrue(concepts._pattern_matches("bovine embryos were vitrified.",
                                                   concepts._regex("bovine"), bovine))
        self.assertFalse(concepts._pattern_matches("mouse anti-H3K9me3 antibody was used.",
                                                    concepts._regex("mouse"), mouse))
        self.assertTrue(concepts._pattern_matches("mouse oocytes were collected.",
                                                   concepts._regex("mouse"), mouse))


class TypedSectionConcentrationTests(unittest.TestCase):
    def test_concentration_and_material_tables_render_without_name_error(self):
        from kbsys import concept_notes
        record = {"concept_id": "s", "name": "Sucrose", "type": "molecule", "synonyms": []}
        variant = {"details": {
            "concentration": {"items": [{"value": "0.5 M", "sources": [
                {"sentence_id": "p:S1", "text": "Embryos were soaked in 0.5 M Sucrose for 3 min."}]}]},
            "chemical": {"items": [{"value": "Sucrose", "sources": [{"sentence_id": "p:S1"}]}]}}}
        # the concentration / reagent sections render for a node with an AI card too (they crashed live: NameError)
        profile = {"vault": "v", "card": {"statements": []},
                   "papers": [{"paper_id": "p", "cite": "P 2020", "protocol_variants": [variant]}]}
        for kind, heading in (("substance", "Concentrations used"), ("method", "Reagents & concentrations")):
            with patch.object(concept_notes.node_profile, "graph_node_type", return_value=kind):
                text = "\n".join(concept_notes._typed_sections(record, profile, None))
            self.assertIn(heading, text)
            # a bare amount keeps its substance name; in a method's reagent list an amount without a reagent is dropped
            self.assertIn("0.5 M Sucrose" if kind == "substance" else "- Sucrose", text)


if __name__ == "__main__":
    unittest.main()


class GraphTypeMappingTests(unittest.TestCase):
    def test_every_entity_kind_maps_to_a_palette_type(self):
        for kind, expected in (("biological_entity", "substance"), ("chemical", "substance"),
                               ("chemical_class", "substance"), ("molecule", "substance"),
                               ("species", "organism"), ("topic", "moc"), ("protocol", "method"),
                               ("technique", "technique"), ("cell_type", "cell_type")):
            self.assertEqual(node_profile.graph_node_type({"entity_kind": kind}), expected, kind)
        self.assertEqual(node_profile.graph_node_type({"card": {"node_type": "substance"}, "entity_kind": "chemical"}), "substance")
