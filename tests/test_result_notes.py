"""Offline tests for D3.47 generated result notes and media copies."""

import json
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kbsys import config, db, media, notes, result_notes


class ResultNotesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", LOGS_DIR=root / "store" / "logs",
            CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.paths.start()
        self.con = db.connect()
        self.vault = "v"
        config.vault_dir(self.vault).mkdir(parents=True)

    def tearDown(self):
        self.con.close()
        self.paths.stop()
        self.temp.cleanup()

    def paper(self, pid, *, review=False, author=None):
        folder = db.paper_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        meta = {
            "paper_id": pid, "title": f"Paper {pid}", "note_name": f"Paper {pid}",
            "authors": [{"name": author or pid.title(), "family": author or pid.title()}],
            "first_author": author or pid.title(), "year": 2024,
            "publication_types": ["review"] if review else ["primary research"],
            "evidence_type": "review" if review else "primary_research", "source_files": [],
        }
        sentences = [
            {"sentence_id": f"{pid}:S0001", "seq": 1, "page": 2, "section": "results",
             "kind": "text", "text": f"The treatment changed the marker in {pid}.", "source": "main"},
            {"sentence_id": f"{pid}:S0002", "seq": 2, "page": 2, "section": "results",
             "kind": "caption", "text": "Figure 1. Marker caption, verbatim.", "source": "main"},
            {"sentence_id": f"{pid}:S0003", "seq": 3, "page": 3, "section": "conclusion",
             "kind": "text", "text": f"The authors conclude that the treatment matters in {pid}.", "source": "main"},
        ]
        unit = {
            "unit_id": f"{pid}:E0001", "paper_id": pid, "quote": "", "seq": 1, "unit_type": "result", "status": "ok",
            "statement": "The treatment changed the marker.", "sentence_ids": [f"{pid}:S0001"],
            "model": {"species": "Mouse", "stage": "MII"},
            "intervention": {"agent": "Treatment", "protocol": "Protocol A"},
            "outcome": {"measure": "Marker", "direction": "increase", "significance": "ns",
                         "value_source": "figure_only", "values": [{"group": "A", "value": "12.5", "n": 3}],
                         "source_objects": [f"{pid}:F1"]},
        }
        conclusion = {
            "unit_id": f"{pid}:E0002", "paper_id": pid, "quote": "", "seq": 2, "unit_type": "conclusion", "status": "ok",
            "statement": "The authors conclude that the treatment matters.",
            "sentence_ids": [f"{pid}:S0003"],
        }
        db.write_json(folder / "metadata.json", meta)
        db.write_jsonl(folder / "sentences.jsonl", sentences)
        db.write_jsonl(folder / "evidence.jsonl", [unit, conclusion])
        db.write_json(folder / "analysis.json", {"version": 2, "summary": [{"text": "Main", "units": [1]}]})
        db.load_paper(self.con, pid)
        self.con.execute("INSERT INTO paper_vaults VALUES (?, ?)", (pid, self.vault))
        return folder, unit, conclusion

    def bundle(self, pid, crop_name="F1.png"):
        folder = db.paper_dir(pid)
        crop = folder / "crops" / crop_name
        crop.parent.mkdir(parents=True, exist_ok=True)
        crop.write_bytes(b"verified crop bytes")
        return {
            "object_id": f"{pid}:F1", "kind": "figure", "label": "Figure 1",
            "caption": [{"sentence_id": f"{pid}:S0002", "page": 2,
                         "text": "Figure 1. Marker caption, verbatim."}],
            "mentions": [{"sentence_id": f"{pid}:S0001", "page": 2,
                          "text": "The treatment changed the marker."}],
            "data": [], "anchors": [], "associated_units": [{"unit_id": f"{pid}:E0001"}],
            "source_locator": {"source": "main", "page": 2, "region_kind": "object_region"},
            "crop": {"path": f"crops/{crop_name}", "sha256": media._hash(crop), "status": "available"},
        }

    def test_verified_crop_manifest_and_existing_crop_reuse(self):
        folder, _, _ = self.paper("one")
        bundle = self.bundle("one")
        first = result_notes.copy_crops(self.vault, "one", [bundle])
        target = config.vault_dir(self.vault) / "_Attachments/one/F1.png"
        self.assertTrue(target.is_file())
        manifest = json.loads((target.parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["objects"][0]["sha256"], media._hash(folder / "crops/F1.png"))
        before = target.read_bytes()
        with patch.object(result_notes.shutil, "copy2", side_effect=AssertionError("re-copied")):
            result_notes.copy_crops(self.vault, "one", [bundle])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(first["entries"][0]["source_page"], 2)

    def test_render_caption_stays_under_embed_and_table_data_has_no_local_link(self):
        self.paper("one")
        bundle = self.bundle("one")
        bundle["kind"] = "table"
        with patch.object(result_notes.media, "bundles", return_value=[bundle]):
            text = "\n".join(result_notes.render_figures_and_tables(self.paper_meta("one"), self.vault))
        self.assertIn("![[_Attachments/one/F1.png|640]]", text)
        self.assertLess(text.index("![[_Attachments/one/F1.png|640]]"), text.index("Figure 1. Marker caption, verbatim."))
        self.assertNotIn("dashboard", text)  # VN-08: no local link in a note
        self.assertNotIn("tables.jsonl", text)
        self.assertNotIn("```json", text)

    def paper_meta(self, pid):
        return db.read_json(db.paper_dir(pid) / "metadata.json")

    def evd(self, registry=None, **parts):
        unit = {"unit_id": "p:E1", "unit_type": "result", "outcome": {"measure": "Marker"}, **parts}
        return result_notes._evidence_frontmatter(unit, {"paper_id": "p", "note_name": "Paper p"}, self.vault, "x", registry=registry)

    def test_evd_subject_and_predicate_rl10(self):
        self.assertTrue(config.STORE.is_relative_to(tempfile.gettempdir()))
        cells = config.vault_dir(self.vault) / "Cell types"
        cells.mkdir()
        (cells / "Muse cell.md").write_text("---\ntype: cell_type\n---\n", encoding="utf-8", newline="\n")
        agent = {"agent": "Drug"}
        out = {"measure": "Marker", "direction": "increase"}
        got = self.evd(intervention=agent, model={"cell_type": "Muse cell"}, outcome=out)
        self.assertEqual((got["subject"], got["predicate"]), (["Drug"], "increases"))  # a stated agent wins
        self.assertEqual((got["agent"], got["measure"]), ("Drug", "Marker"))  # the phrases as written
        self.assertEqual(self.evd(model={"cell_type": "Muse cell"}, outcome=out)["subject"], ["[[Muse cell]]"])
        self.assertNotIn("agent", self.evd(model={"cell_type": "Muse cell"}, outcome=out))  # a cell type is no stated agent
        self.assertEqual(self.evd(model={"cell_type": "Unknown cell"}, outcome=out)["subject"], ["Unknown cell"])
        self.assertEqual(self.evd(model={"cell_type": ""}, outcome=out)["subject"], ["not stated"])
        self.assertEqual(self.evd(statement="Muse cells rose.")["subject"], ["not stated"])  # never read from the text
        quiet = self.evd()  # no direction stated
        self.assertEqual((quiet["predicate"], quiet["knowledge_level"]), ("reports", "observation"))
        self.assertEqual(self.evd(outcome={"measure": "M", "direction": "mixed"})["predicate"], "mixed")
        for stated in ("not_applicable", "variable"):  # a stated value that is no direction never becomes `mixed`
            self.assertEqual(self.evd(outcome={"measure": "M", "direction": stated})["predicate"], "reports")
        self.assertEqual(self.evd(outcome={"measure": "M", "direction": "correlated"})["predicate"], "associated_with")
        self.assertEqual(self.evd(knowledge_level="statistical_association")["predicate"], "associated_with")

    def nodes(self, *names):
        folder = config.vault_dir(self.vault) / "Concepts"
        folder.mkdir(exist_ok=True)
        for name in names:
            (folder / f"{name}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8", newline="\n")

    def test_evd_object_links_every_node_named_in_the_measure_rl10_a4(self):
        self.assertTrue(config.STORE.is_relative_to(tempfile.gettempdir()))
        self.nodes("Blastocyst", "Embryo", "Histone", "Histone H3", "H3K9me3", "β-catenin", "Mouse", "Western blot", "Healthy controls",
                   "Whole measure phrase", "Noted concept")
        rec = lambda cid, name, kind="concept", parents=(), synonyms=(): {
            "concept_id": cid, "name": name, "type": kind, "parents": list(parents), "synonyms": list(synonyms)}
        registry = [rec("c1", "Blastocyst"), rec("c2", "Embryo"), rec("c3", "Histone"), rec("c4", "Histone H3", parents=["c3"]),
                    rec("c5", "H3K9me3"), rec("c6", "β-catenin", synonyms=["beta-catenin"]), rec("c7", "Mouse", "species"),
                    rec("c8", "Western blot", "technique"), rec("c9", "Healthy controls"), rec("c10", "Noted concept"),
                    rec("c11", "Whole measure"), rec("c12", "Unnoted")]
        (config.CONFIG_DIR).mkdir(parents=True, exist_ok=True)
        (config.CONFIG_DIR / "generic_terms.yaml").write_text("generic_terms:\n- Healthy controls\n", encoding="utf-8", newline="\n")
        links = lambda measure, reg=registry: self.evd(outcome={"measure": measure}, registry=reg)["object"]
        self.assertEqual(links("Whole measure phrase"), ["[[Whole measure phrase]]"])  # the whole phrase wins over a partial match
        self.assertEqual(links("H3K9me3 in Mouse embryo and blastocyst"), ["[[H3K9me3]]", "[[Embryo]]", "[[Blastocyst]]"])  # phrase order
        self.assertEqual(links("blastocyst and embryo"), ["[[Blastocyst]]", "[[Embryo]]"])
        self.assertEqual(links("β-catenin level by Western blot in healthy controls"), ["[[β-catenin]]"])  # species, method, generic out
        confirmed = [{**r, "status": "human_confirmed"} if r["name"] == "Healthy controls" else r for r in registry]
        self.assertEqual(links("β-catenin in healthy controls", confirmed), ["[[β-catenin]]", "[[Healthy controls]]"])  # ND-04: a confirmed node stays
        self.assertEqual(links("Histone H3 acetylation"), ["[[Histone H3]]"])  # the ancestor Histone is dropped
        self.assertEqual(links("Unnoted level and Noted concept"), ["[[Noted concept]]"])  # only a concept with a note
        self.assertEqual(links("Plain phrase with nothing"), ["Plain phrase with nothing"])  # no match: the phrase as text
        self.assertEqual(links("blastocyst", None), ["[[Blastocyst]]"])  # no registry: only the whole phrase
        self.assertEqual(links("blastocyst rate", None), ["blastocyst rate"])
        shuffled = self.evd(outcome={"measure": "embryo and blastocyst"}, registry=registry[::-1])["object"]
        self.assertEqual(shuffled, links("embryo and blastocyst"))  # deterministic
        self.assertEqual(self.evd(outcome={"measure": "Mouse embryo"}, registry=registry)["measure"], "Mouse embryo")
        # most specific by text (no parents): a name only inside a longer match is dropped, also when that one has no note
        self.nodes("Pregnancy", "Clinical pregnancy", "Muse cell", "non-Muse cell")
        texts = registry + [rec("c13", "Pregnancy"), rec("c14", "Clinical pregnancy"), rec("c15", "Muse cell"),
                            rec("c16", "non-Muse cell"), rec("c17", "Embryo quality")]
        self.assertEqual(links("clinical pregnancy rate", texts), ["[[Clinical pregnancy]]"])
        self.assertEqual(links("Ki67+ cells in non-Muse cell-transplanted liver", texts), ["[[non-Muse cell]]"])
        self.assertEqual(links("Muse cell and non-Muse cell counts", texts), ["[[Muse cell]]", "[[non-Muse cell]]"])
        self.assertEqual(links("embryo quality", texts), ["embryo quality"])  # Embryo sits only inside the unnoted concept

    def test_evd_compound_subject_is_split_when_every_part_is_a_node_rl10_a5(self):
        self.nodes("Melatonin", "Fatty acids", "Glucose")
        out = {"measure": "Marker"}
        subject = lambda agent: self.evd(intervention={"agent": agent}, outcome=out)
        for agent in ("Fatty acids and melatonin", "Fatty acids, Melatonin", "Fatty acids/melatonin", "Fatty acids + melatonin",
                      "Fatty acids & melatonin"):
            self.assertEqual(subject(agent)["subject"], ["[[Fatty acids]]", "[[Melatonin]]"], agent)
        self.assertEqual(subject("Melatonin and melatonin")["subject"], ["[[Melatonin]]"])  # deduped
        self.assertEqual(subject("Melatonin and Insulin")["subject"], ["Melatonin and Insulin"])  # one part is no node: kept whole
        self.assertEqual(subject("Melatonin")["subject"], ["[[Melatonin]]"])
        self.assertEqual(subject("Fatty acids and melatonin")["agent"], "Fatty acids and melatonin")  # still readable as one agent
        self.assertEqual(self.evd(outcome=out)["subject"], ["not stated"])  # sole item

    def test_claim_and_conflict_side_of_a_unit_without_subject_or_direction_are_unchanged(self):
        unit = {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result", "outcome": {"measure": "Marker"}}
        meta = {"paper_id": "p", "note_name": "Paper p"}
        claim = result_notes._claim_values(unit, meta, self.vault, [])
        self.assertEqual((claim["subject"], claim["predicate"], claim["object"]), ([], "mixed", ["Marker"]))  # the same shape, as lists
        self.assertEqual(result_notes._side(unit, meta, {})["direction"], "mixed")

    def test_a_result_without_a_measure_gets_no_evd_note_and_the_old_note_retires(self):
        self.paper("one")  # its unit is a summary unit and has a figure bundle
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        results = lambda: sorted((config.vault_dir(self.vault) / "Results").glob("EVD - *.md"))
        self.assertEqual(len(results()), 2)
        folder = db.paper_dir("one")
        units = list(db.read_jsonl(folder / "evidence.jsonl"))
        units[0] = {**units[0], "outcome": {**units[0]["outcome"], "measure": ""}}
        db.write_jsonl(folder / "evidence.jsonl", units)
        self.refresh()
        self.assertEqual(len(results()), 1)
        self.assertTrue(list((config.STORE / "removed").glob("result_notes_*/v/Results/EVD - *.md")))
        vdir = config.vault_dir(self.vault)
        stems = {path.stem for path in vdir.rglob("*.md")}
        for path in vdir.rglob("*.md"):  # no generated note points to the retired EVD note
            for link in re.findall(r"\[\[(EVD - [^\]|#]+)", path.read_text(encoding="utf-8")):
                self.assertIn(link, stems, path.name)
        self.assertTrue(self.conflict_files())  # the relation and its CONFLICT note stay
        meta = {"publication_types": ["primary research"], "evidence_type": "primary_research"}
        marked = [{"unit_id": f"p:E{i}", "unit_type": "result", "outcome": {"measure": m, "direction": "increase"}}
                  for i, m in enumerate(("outcome measure not annotated", "   "))]
        self.assertEqual(result_notes._main_units("p", meta, marked, {}, [], []), [])  # a marker or blank is no measure

    def test_evd_claim_conflict_and_figure_only_redaction(self):
        self.paper("one", author="Alpha")
        self.paper("two", author="Beta")
        first = self.bundle("one")
        second = self.bundle("two")
        relation = {"relation_id": "rel-1", "from_unit": "one:E0001", "to_unit": "two:E0001",
                    "vault": self.vault, "type": "contradicts", "status": "ai_proposed",
                    "differences": [{"field": "model.species", "from": "Mouse", "to": "Rat"}]}
        db.write_jsonl(config.STORE / "relations.jsonl", [relation])
        with patch.object(result_notes.media, "bundles", side_effect=lambda pid: [first if pid == "one" else second]):
            result_notes.refresh(self.con, self.vault)
        results = sorted((config.vault_dir(self.vault) / "Results").glob("EVD - *.md"))
        claims = sorted((config.vault_dir(self.vault) / "Claims").glob("CLM - *.md"))
        conflicts = sorted((config.vault_dir(self.vault) / "Claims").glob("CONFLICT - *.md"))
        self.assertEqual(len(results), 2)
        self.assertEqual(len(claims), 2)
        self.assertEqual(len(conflicts), 1)
        evd = results[0].read_text(encoding="utf-8")
        self.assertIn("predicate: increases", evd)
        self.assertIn("## Finding", evd)
        self.assertIn("## Context", evd)
        self.assertIn("only in the figure", evd)
        self.assertNotIn("12.5", evd)
        conflict = conflicts[0].read_text(encoding="utf-8")
        self.assertIn("type: conflict", conflict)
        self.assertIn("[!conflict]", conflict)
        self.assertIn("[[EVD -", conflict)

    def test_titles_end_at_a_clause_or_word_boundary_and_stay_unique(self):
        cut = result_notes._short("Before warming, embryo-stage and morphological-quality distributions were similar "
                                  "between the groups and across all donors", 80)
        self.assertEqual(cut, "Before warming, embryo-stage and morphological-quality distributions…")
        self.assertEqual(result_notes._short("H3K9me3 increased after vitrification", 80), "H3K9me3 increased after vitrification")
        clause = result_notes._short("H3K9me3 was enriched in repressive chromatin, whereas H3K4me3 marked active promoters", 50)
        self.assertEqual(clause, "H3K9me3 was enriched in repressive chromatin…")
        open_paren = result_notes._short("Survival rose after warming (n = 12 embryos per group in two replicates)", 50)
        self.assertNotIn("(", open_paren)
        for text in (cut, clause, open_paren):
            self.assertNotRegex(text, r"(?i)\b(?:were|the|and|of|in|was|with)…$")

    def test_evd_claim_conflict_sections_are_the_approved_sets_and_empty_ones_are_hidden(self):
        self.paper("one", author="Alpha")
        self.paper("two", author="Beta")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        root = config.vault_dir(self.vault)
        headings = {}
        for folder, pattern in (("Results", "EVD - *.md"), ("Claims", "CLM - *.md"), ("Claims", "CONFLICT - *.md")):
            for path in sorted((root / folder).glob(pattern)):
                text = path.read_text(encoding="utf-8")
                self.assertEqual(result_notes.heading_problems(text), [], path.name)
                headings.setdefault(path.name.split(" - ")[0], re.findall(r"(?m)^## (.+)$", text))
                for placeholder in ("Dữ liệu", "Bối cảnh nền", "Ngữ cảnh diễn ngôn", "No linked claim", "No grouped data",
                                    "Supports:", "Opposes:", "Quy trình", "chưa tìm thấy", "Differences were not annotated",
                                    "Review guidance"):
                    self.assertNotIn(placeholder, text, f"{placeholder} in {path.name}")
        self.assertEqual(headings["EVD"], ["Finding", "Context", "Data", "Source sentences", "Related claims / conflicts", "My notes"])
        self.assertEqual(headings["CLM"], ["Claim", "Source sentences", "My notes"])  # no support / oppose edges here
        self.assertEqual(headings["CONFLICT"], ["Summary", "Side-by-side comparison", "Sources", "My notes"])  # sides do not differ here: "What differs" hidden
        conflict = next((root / "Claims").glob("CONFLICT - *.md")).read_text(encoding="utf-8")
        self.assertIn("papers: Alpha 2024 vs Beta 2024", conflict)
        self.assertIn("| Species | Mouse | Mouse |", conflict)
        self.assertNotRegex(conflict, r"\| [^|]+ \|\s*\|\s*\|")  # no row with empty A / B cells

    def test_claim_opposes_only_through_open_same_measure_relations(self):
        for pid in ("one", "two"):  # make each conclusion share its sentence with the paper's result
            self.paper(pid, author=pid.title())
            folder = db.paper_dir(pid)
            units = list(db.read_jsonl(folder / "evidence.jsonl"))
            units[1]["sentence_ids"] = [f"{pid}:S0001"]
            db.write_jsonl(folder / "evidence.jsonl", units)
        for status, expect in (("ai_proposed", True), ("rejected", False), ("withdrawn_by_rule", False)):
            db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("r", "one:E0001", "two:E0001", status=status)])
            self.refresh()
            claims = list((config.vault_dir(self.vault) / "Claims").glob("CLM - *.md"))
            self.assertEqual(len(claims), 2)
            for claim in claims:
                text = claim.read_text(encoding="utf-8")
                self.assertIn("## Supporting evidence", text)  # the result that shares the claim's own sentence
                self.assertEqual("## Opposing evidence" in text, expect, f"{status} {claim.name}")
                self.assertEqual("opposes: []" in text, not expect, f"{status} {claim.name}")

    def test_changed_evd_title_renames_the_note_and_records_an_undo_map(self):
        self.paper("one", author="Alpha")
        self.refresh()
        evd = next((config.vault_dir(self.vault) / "Results").glob("EVD - *.md"))
        old = evd.with_name("EVD - The treatment changed the (Alpha 2024).md")  # a title cut mid-clause by the old code
        evd.rename(old)
        self.refresh()
        self.assertFalse(old.exists())
        renamed = next((config.vault_dir(self.vault) / "Results").glob("EVD - *.md"))
        self.assertEqual(renamed.stem, evd.stem)
        self.assertIn(f"# {evd.stem}", renamed.read_text(encoding="utf-8"))
        self.assertTrue(list((config.STORE / "migrations").glob("*_result_notes*.json")))

    def test_callouts_and_quotes_are_never_glued(self):
        glued = "> [!abstract] A\n> text\n> [!conflict] B\n> body\n> caption\nMention sentence.\n- item\n> q\n<!-- kb:auto:end -->"
        self.assertEqual(notes.separate_callouts(glued),
                         "> [!abstract] A\n> text\n\n> [!conflict] B\n> body\n> caption\n\nMention sentence.\n- item\n> q\n<!-- kb:auto:end -->")

    def test_rejected_relation_no_new_conflict_and_human_text_survives_retirement(self):
        self.paper("one")
        self.paper("two")
        relation = {"relation_id": "rejected", "from_unit": "one:E0001", "to_unit": "two:E0001",
                    "vault": self.vault, "type": "contradicts", "status": "rejected"}
        db.write_jsonl(config.STORE / "relations.jsonl", [relation])
        with patch.object(result_notes.media, "bundles", side_effect=lambda pid: [self.bundle(pid)]):
            result_notes.refresh(self.con, self.vault)
        self.assertFalse(list((config.vault_dir(self.vault) / "Claims").glob("CONFLICT - *.md")))
        old = config.vault_dir(self.vault) / "Results" / "old.md"
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_text("---\ntype: result\nid: old\nagent_type: automated\n---\n# old\n\n"
                       f"{notes.AUTO_START}\nold block\n{notes.AUTO_END}\n\nHuman review.\n", encoding="utf-8")
        with patch.object(result_notes.media, "bundles", return_value=[]):
            result_notes.refresh(self.con, self.vault)
        self.assertTrue(old.is_file())
        self.assertIn("Human review.", old.read_text(encoding="utf-8"))

    def extra_unit(self, pid, direction="decrease"):
        folder = db.paper_dir(pid)
        units = list(db.read_jsonl(folder / "evidence.jsonl"))
        units.append({**units[0], "unit_id": f"{pid}:E0003", "seq": 3, "statement": "A second marker finding.",
                      "outcome": {**units[0]["outcome"], "direction": direction}})
        db.write_jsonl(folder / "evidence.jsonl", units)

    def refresh(self):
        with patch.object(result_notes.media, "bundles", side_effect=lambda pid: [self.bundle(pid)]):
            return result_notes.refresh(self.con, self.vault)

    def conflict_files(self):
        return sorted((config.vault_dir(self.vault) / "Claims").glob("CONFLICT - *.md"))

    def rel(self, ident, a, b, status="ai_proposed", type_="contradicts"):
        return {"relation_id": ident, "from_unit": a, "to_unit": b, "vault": self.vault, "type": type_,
                "status": status, "shared_concept_name": "Marker", "directions": ["increase", "decrease"]}

    def test_refresh_keeps_a_filled_review_field(self):
        self.paper("one")
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        note = self.conflict_files()[0]
        text = note.read_text(encoding="utf-8")
        text = text.replace('reviewed_by: ""', "reviewed_by: Kha").replace("reviewed_by: ''", "reviewed_by: Kha")
        text = text.replace('reviewed_on: ""', "reviewed_on: 2026-10-20").replace("reviewed_on: ''", "reviewed_on: 2026-10-20")
        note.write_text(text, encoding="utf-8")
        self.assertIn("reviewed_by: Kha", text)
        self.refresh()
        after = note.read_text(encoding="utf-8")
        self.assertIn("reviewed_by: Kha", after)
        self.assertIn("reviewed_on: 2026-10-20", after)

    def rename_conflict(self, move_works):
        self.paper("one")
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        note = self.conflict_files()[0]
        note.write_text(note.read_text(encoding="utf-8").rstrip() + "\nHuman opinion kept.\n", encoding="utf-8")
        db.write_jsonl(config.STORE / "relations.jsonl",
                       [{**self.rel("live", "one:E0001", "two:E0001"), "shared_concept_name": "Other marker"}])
        if move_works:
            self.refresh()
        else:
            with patch.object(result_notes, "_move_conflict_note", return_value=None):
                self.refresh()
        files = self.conflict_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("Human opinion kept.", text)
        body = text.split("---\n", 2)[2].lstrip()
        self.assertTrue(body.startswith(f"# {files[0].stem}\n"), body[:120])
        for evd in (config.vault_dir(self.vault) / "Results").glob("EVD - *.md"):
            self.assertIn(f"[[{files[0].stem}]]", evd.read_text(encoding="utf-8"))
        return files[0], note

    def test_conflict_rename_that_cannot_move_keeps_the_old_note(self):
        kept, old = self.rename_conflict(move_works=False)
        self.assertEqual(kept, old)

    def test_conflict_rename_moves_the_note_with_its_human_text(self):
        moved, old = self.rename_conflict(move_works=True)
        self.assertNotEqual(moved, old)

    def test_withdrawn_relation_is_history_not_an_open_conflict(self):
        self.paper("one")
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        old = self.conflict_files()
        self.assertEqual(len(old), 1)
        withdrawn = {**self.rel("live", "one:E0001", "two:E0001"), "status": "withdrawn_by_rule"}
        db.write_jsonl(config.STORE / "relations.jsonl", [withdrawn])
        summary = self.refresh()
        self.assertEqual(self.conflict_files(), [])  # note retired, not open/red
        self.assertEqual(summary["conflicts"], 0)
        self.assertEqual(summary["retired"], 1)
        removed = list((config.STORE / "removed").rglob("CONFLICT - *.md"))  # reversible: moved, not deleted
        self.assertEqual(len(removed), 1)
        for evd in (config.vault_dir(self.vault) / "Results").glob("EVD - *.md"):
            self.assertIn("has_conflict: false", evd.read_text(encoding="utf-8"))
        self.assertEqual(len(list(db.read_jsonl(config.STORE / "relations.jsonl"))), 1)  # store row kept

    def test_withdrawn_conflict_note_with_human_text_is_marked_withdrawn(self):
        self.paper("one")
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        note = self.conflict_files()[0]
        note.write_text(note.read_text(encoding="utf-8").rstrip() + "\n\nMy opinion.\n", encoding="utf-8")
        db.write_jsonl(config.STORE / "relations.jsonl",
                       [{**self.rel("live", "one:E0001", "two:E0001"), "status": "withdrawn_by_rule"}])
        self.refresh()
        text = note.read_text(encoding="utf-8")
        self.assertIn("My opinion.", text)
        self.assertIn("status: withdrawn", text)
        self.assertIn("type: conflict_history", text)
        self.assertIn("has_conflict: false", text)
        self.assertIn("retired: true", text)

    def test_withdrawn_note_with_human_frontmatter_is_never_moved(self):
        self.paper("one")
        self.paper("two")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("live", "one:E0001", "two:E0001")])
        self.refresh()
        note = self.conflict_files()[0]
        note.write_text(note.read_text(encoding="utf-8").replace("agent_type:", "my_tag: keep\nagent_type:", 1), encoding="utf-8")
        db.write_jsonl(config.STORE / "relations.jsonl",
                       [{**self.rel("live", "one:E0001", "two:E0001"), "status": "withdrawn_by_rule"}])
        self.refresh()
        self.assertTrue(note.is_file())
        self.assertIn("my_tag: keep", note.read_text(encoding="utf-8"))
        self.assertFalse(list((config.STORE / "removed").rglob("CONFLICT - *.md")))

    def test_merged_note_keeps_its_id_when_a_lower_relation_id_joins(self):
        self.paper("one")
        self.paper("two")
        self.extra_unit("one")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("r2", "one:E0001", "two:E0001")])
        self.refresh()
        note = self.conflict_files()[0]
        note.write_text(note.read_text(encoding="utf-8").rstrip() + "\n\nMy opinion.\n", encoding="utf-8")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("r1", "one:E0003", "two:E0001"),
                                                          self.rel("r2", "one:E0001", "two:E0001")])
        self.refresh()
        files = self.conflict_files()
        self.assertEqual(len(files), 1)  # renamed (merged title), not split into a second note
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("My opinion.", text)
        self.assertIn("id: r2", text)
        self.assertNotIn("retired", text)

    def test_conflict_relations_reader_hides_withdrawn(self):
        from kbsys import relations
        rows = [self.rel("a", "one:E0001", "two:E0001"),
                {**self.rel("b", "one:E0001", "two:E0001"), "status": "withdrawn_by_rule"},
                {**self.rel("c", "one:E0001", "two:E0001"), "status": "rejected"},
                {**self.rel("d", "one:E0001", "two:E0001"), "status": "weird"},
                {**self.rel("e", "one:E0001", "two:E0001"), "vault": "other"}]
        db.write_jsonl(config.STORE / "relations.jsonl", rows)
        self.assertEqual([r["relation_id"] for r in relations.conflict_relations(self.vault)], ["a"])
        self.assertEqual([r["relation_id"] for r in relations.conflict_relations(self.vault, history=True)], ["a", "c"])

    def test_same_pair_and_measure_merge_into_one_note_with_matching_h1(self):
        self.paper("one")
        self.paper("two")
        self.extra_unit("one")
        db.write_jsonl(config.STORE / "relations.jsonl", [
            self.rel("r1", "one:E0001", "two:E0001"),
            self.rel("r2", "one:E0003", "two:E0001", type_="direction_differs")])
        self.refresh()
        files = self.conflict_files()
        self.assertEqual(len(files), 1)
        text = files[0].read_text(encoding="utf-8")
        self.assertIn("relation_ids:", text)
        self.assertIn("### Comparison 2", text)
        self.assertEqual(text.count("\n# "), 1)
        self.assertIn(f"\n# {files[0].stem}\n", text)
        before = text
        self.refresh()
        self.assertEqual(files[0].read_text(encoding="utf-8"), before)

    def test_h1_follows_file_name_after_rename_and_titles_are_unique(self):
        self.paper("one")
        self.paper("two")
        self.paper("three")
        db.write_jsonl(config.STORE / "relations.jsonl", [self.rel("r1", "one:E0001", "two:E0001"),
                                                          self.rel("r2", "one:E0001", "three:E0001")])
        self.refresh()
        first = self.conflict_files()
        self.assertEqual(len(first), 2)
        old = first[0]
        text = old.read_text(encoding="utf-8")
        old.write_text(text.replace(f"# {old.stem}", "# CONFLICT - stale old title"), encoding="utf-8")
        self.refresh()
        h1s = []
        for path in self.conflict_files():
            body = path.read_text(encoding="utf-8")
            self.assertIn(f"\n# {path.stem}\n", body)
            h1s += [line for line in body.splitlines() if line.startswith("# ")]
        self.assertEqual(len(h1s), len(set(h1s)))

    def test_second_refresh_is_idempotent(self):
        self.paper("one")
        bundle = self.bundle("one")
        with patch.object(result_notes.media, "bundles", return_value=[bundle]):
            result_notes.refresh(self.con, self.vault)
            before = {p.relative_to(config.vault_dir(self.vault)).as_posix(): p.read_bytes()
                      for p in config.vault_dir(self.vault).rglob("*") if p.is_file()}
            result_notes.refresh(self.con, self.vault)
        after = {p.relative_to(config.vault_dir(self.vault)).as_posix(): p.read_bytes()
                 for p in config.vault_dir(self.vault).rglob("*") if p.is_file()}
        self.assertEqual(before, after)


class ShortTitleTests(unittest.TestCase):
    def test_leading_open_bracket_does_not_collapse_title(self):
        text = "(Vitrified oocytes showed lower survival than fresh oocytes and reduced blastocyst rates in all groups"
        title = result_notes._short(text, 60)
        self.assertTrue(title.startswith("Vitrified oocytes"), title)


class ConflictTagTests(unittest.TestCase):
    def test_researcher_tags_survive_and_conflict_tag_is_machine_owned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "EVD.md"
            values = {"type": "evidence", "id": "p:E0001", "has_conflict": True, "tags": ["conflict"]}
            self.assertTrue(result_notes._write_managed(path, values, "body", title="T"))
            path.write_text(path.read_text(encoding="utf-8").replace("- conflict", "- conflict" + chr(10) + "- mine", 1), encoding="utf-8")
            self.assertTrue(result_notes._human_text(path.read_text(encoding="utf-8")))  # a researcher tag is human
            result_notes._write_managed(path, {**values, "has_conflict": False, "tags": None}, "body")
            fm, _ = notes._split_frontmatter(path.read_text(encoding="utf-8"))
            self.assertEqual(fm["tags"], ["mine"])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class NodeLinkCaseTests(unittest.TestCase):
    def test_node_link_uses_the_exact_note_title(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as temp:
            (Path(temp) / "Substances").mkdir()
            (Path(temp) / "Substances" / "Aphidicolin.md").write_text("x", encoding="utf-8")
            with patch.object(result_notes.config, "vault_dir", return_value=Path(temp)):
                self.assertEqual(result_notes._node_link("aphidicolin", "v"), "[[Aphidicolin]]")
                self.assertEqual(result_notes._node_link("unknown", "v"), "unknown")
