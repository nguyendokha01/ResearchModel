"""Offline regression tests for the D3.69 vault rendering fixes."""

import tempfile
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import concepts, config, concept_notes, db, node_profile, notes, presentation, result_notes, vault_layout
import ast
import shutil


class VaultRenderV2Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", CONFIG_DIR=root / "config",
            LOGS_DIR=root / "logs", VAULTS_DIR=root / "vaults")
        self.paths.start()
        config.vault_dir("v").mkdir(parents=True)
        (root / "config").mkdir()
        shutil.copy(Path(__file__).parents[1] / "config" / "model_terms.yaml", root / "config")

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def paper(self, pid, units, source_text="The marker may improve survival."):
        folder = db.paper_dir(pid)
        folder.mkdir(parents=True, exist_ok=True)
        db.write_json(folder / "metadata.json", {
            "paper_id": pid, "note_name": f"Paper {pid}", "first_author": pid.title(),
            "authors": [{"family": pid.title(), "name": pid.title()}], "year": 2025,
            "evidence_type": "primary_research"})
        db.write_jsonl(folder / "sentences.jsonl", [
            {"sentence_id": f"{pid}:S1", "seq": 1, "page": 1, "section": "results", "text": source_text},
            {"sentence_id": f"{pid}:S2", "seq": 2, "page": 2, "section": "conclusion", "text": "The authors suggest a benefit."}])
        db.write_jsonl(folder / "evidence.jsonl", units)

    def connection(self, *paper_ids):
        con = sqlite3.connect(":memory:")
        self.addCleanup(con.close)
        patcher = patch.object(result_notes.concepts, "load_registry", return_value=[])  # this fake db has no paper_terms
        patcher.start()
        self.addCleanup(patcher.stop)
        con.execute("CREATE TABLE paper_vaults (paper_id, vault)")
        con.executemany("INSERT INTO paper_vaults VALUES (?, 'v')", [(pid,) for pid in paper_ids])
        return con

    def test_conflict_links_use_exact_id_and_callout_is_compact(self):
        claims = config.vault_dir("v") / "Claims"
        claims.mkdir()
        (claims / "CONFLICT - H3K9me3 wrong.md").write_text(
            "---\ntype: conflict\nid: other\n---\n", encoding="utf-8")
        (claims / "CONFLICT - correct pair.md").write_text(
            "---\ntype: conflict\nid: rel-0\n---\n", encoding="utf-8")
        units = []
        rows = []
        for index in range(7):
            left, right = f"p{index}:E1", f"q{index}:E1"
            units.extend([
                {"unit_id": left, "paper_id": f"p{index}", "paper": {"note_name": f"Paper A{index}"},
                 "outcome": {"measure": "H3K9me3", "direction": "increase"}},
                {"unit_id": right, "paper_id": f"q{index}", "paper": {"note_name": f"Paper B{index}"},
                 "outcome": {"measure": "H3K9me3", "direction": "decrease"}},
            ])
            rows.append({"relation_id": f"rel-{index}", "from_unit": left, "to_unit": right,
                         "type": "contradicts", "status": "ai_proposed"})
        text = "\n".join(node_profile.conflict_callout({
            "vault": "v", "name": "H3K9me3", "papers": [{"results": units}], "conflicts": rows}))
        self.assertIn("[[CONFLICT - correct pair]]", text)
        self.assertNotIn("[[CONFLICT - H3K9me3 wrong]]", text)
        self.assertLessEqual(sum(line.startswith("> 🔴") for line in text.splitlines()), 5)
        self.assertIn("+ more pairs: 2", text)

    def test_evd_statistics_come_from_cited_source_sentences(self):
        text = "Methylation fell (P < 0.05, n = 12; two-way ANOVA) but not with sham (p = 0.709)."
        self.paper("p", [], text)
        meta = {"paper_id": "p", "note_name": "Paper p", "first_author": "P", "year": 2025}
        unit = {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result", "sentence_ids": ["p:S1"],
                "statement": "Methylation fell.", "outcome": {"measure": "Methylation"}}
        values = result_notes._evidence_frontmatter(unit, meta, "v", "EVD - x")
        self.assertEqual((values["p_value"], values["n"], values["statistical_test"]),
                         ("P < 0.05; p = 0.709", "n = 12", "two-way ANOVA"))
        body = result_notes._result_body(unit, meta, "v", {})
        self.assertIn("- p: P < 0.05; p = 0.709", body)
        self.assertIn("- Test: two-way ANOVA", body)
        own = result_notes._evidence_frontmatter({**unit, "outcome": {"measure": "M", "p_value": "0.001"}}, meta, "v", "EVD - x")
        self.assertEqual(own["p_value"], "0.001")  # the unit's own field wins
        self.paper("q", [], "Methylation fell markedly.")
        bare = {**unit, "unit_id": "q:E1", "paper_id": "q", "sentence_ids": ["q:S1"]}
        meta_q = {**meta, "paper_id": "q"}
        self.assertNotIn("p_value", result_notes._evidence_frontmatter(bare, meta_q, "v", "EVD - y"))
        body = result_notes._result_body(bare, meta_q, "v", {})
        self.assertNotIn("- p:", body)
        self.assertNotIn("- Test:", body)
        self.assertNotIn("- n:", body)

    def test_evd_rewrite_drops_old_empty_statistic_keys(self):
        path = config.vault_dir("v") / "EVD - old.md"
        values = {"type": "result", "id": "p:E1", "title": "x"}
        result_notes._write_managed(path, {**values, "p_value": None, "n": None, "p": None}, "body")
        self.assertIn("p_value: null", path.read_text(encoding="utf-8"))
        result_notes._write_managed(path, values, "body")
        self.assertNotIn("p_value", path.read_text(encoding="utf-8"))

    def test_conflict_side_falls_back_to_paper_level_term_and_marks_it(self):
        self.paper("p", [], "Marker rose.")
        folder = db.paper_dir("p")
        meta = db.read_json(folder / "metadata.json", {})
        meta["title"] = "Marker rises in mouse blastocysts after vitrification"
        db.write_json(folder / "metadata.json", meta)
        unit = {"unit_id": "p:E1", "paper_id": "p", "sentence_ids": ["p:S1"], "outcome": {"measure": "Marker"}}
        with patch.object(concept_notes.structure, "_fresh_overview", return_value=None),                 patch.object(concept_notes.structure, "_profile_report", return_value={"ready": False}):
            side = result_notes._side(unit, meta, result_notes._read_sentences("p"))
            bare = result_notes._side({**unit, "paper_id": "q"}, {"paper_id": "q"}, {})
        self.assertEqual((side["species"], side["stage"]), ("Mouse (from paper)", "blastocysts (from paper)"))
        self.assertIsNone(bare["species"])  # no placeholder text: the renderer hides the row

    def test_conflict_side_prefers_unit_sentence_skips_review_title_and_ignores_author_names(self):
        self.paper("p", [], "Marker rose in bovine oocytes (Pearson, 2001).")
        folder = db.paper_dir("p")
        meta = db.read_json(folder / "metadata.json", {})
        meta["title"] = "Cryopreservation of mouse embryos"
        db.write_json(folder / "metadata.json", meta)
        unit = {"unit_id": "p:E1", "paper_id": "p", "sentence_ids": ["p:S1"], "outcome": {"measure": "Marker"}}
        with patch.object(concept_notes.structure, "_fresh_overview", return_value=None), \
                patch.object(concept_notes.structure, "_profile_report", return_value={"ready": False}):
            side = result_notes._side(unit, meta, result_notes._read_sentences("p"))
            db.write_json(folder / "metadata.json", {**meta, "evidence_type": "review", "title": "Mouse embryos: a review"})
            review = result_notes._side({**unit, "sentence_ids": []}, meta, {})
        self.assertEqual(side["species"], "Bovine (from paper)")  # the unit's own sentence beats the title
        self.assertIsNone(review["species"])  # a review title is not the unit's organism
        self.assertIsNone(result_notes._source_stat(unit, "test", "p"))  # a cited author name is not a test

    def test_conflict_title_without_sides_reads_the_units_own_sentences_like_the_table(self):
        self.paper("p", [], "Marker rose in bovine oocytes.")
        folder = db.paper_dir("p")
        meta = {**db.read_json(folder / "metadata.json", {}), "title": "Cryopreservation of mouse embryos"}
        db.write_json(folder / "metadata.json", meta)
        unit = {"unit_id": "p:E1", "paper_id": "p", "sentence_ids": ["p:S1"], "outcome": {"measure": "Marker"}}
        other = {"model": {"species": "Human", "stage": "oocytes"}, "outcome": {"measure": "Marker"}}
        relation = {"relation_id": "r", "shared_concept_name": "Marker"}
        with patch.object(concept_notes.structure, "_fresh_overview", return_value=None), \
                patch.object(concept_notes.structure, "_profile_report", return_value={"ready": False}):
            sides = [result_notes._side(unit, meta, result_notes._read_sentences("p")), result_notes._side(other, meta, {})]
            title = result_notes._conflict_title(relation, unit, other, meta, meta)
        self.assertIn("Bovine", title)  # the unit's own sentence, as in the A/B table; not the paper title
        self.assertEqual(title, result_notes._conflict_title(relation, unit, other, meta, meta, sides=sides))
        self.assertEqual(result_notes._title_context("Mouse blastocysts", "Mouse blastocysts" + result_notes.PAPER_LEVEL),
                         "Mouse blastocysts")  # one combined phrase is not repeated

    def test_conflict_lookup_ignores_retired_notes_and_never_guesses_ambiguous_ids(self):
        claims = config.vault_dir("v") / "Claims"
        claims.mkdir()
        for stem, ident, retired in (("old", "r", True), ("current", "r", False),
                                     ("duplicate one", "ambiguous", False), ("duplicate two", "ambiguous", False)):
            (claims / f"{stem}.md").write_text(
                f"---\ntype: conflict\nid: {ident}\nretired: {'true' if retired else 'false'}\n---\n", encoding="utf-8")
        self.assertEqual(node_profile._vault_conflict_ids("v"), {"r": "current"})

    def test_duplicate_media_names_are_path_qualified(self):
        bundles = {
            "one": [{"object_id": "one:F1", "kind": "figure", "label": "F1",
                      "source_locator": {"source": "main", "page": 1}, "caption": []}],
            "two": [{"object_id": "two:F1", "kind": "figure", "label": "F1",
                      "source_locator": {"source": "main", "page": 1}, "caption": []}],
        }

        def copied(vault, paper_id, rows):
            return {"entries": [{"object_id": f"{paper_id}:F1", "status": "copied",
                                  "vault_path": f"_Attachments/{paper_id}/F1.png"}], "paths": {}}

        with patch.object(result_notes.media, "bundles", side_effect=lambda pid: bundles[pid]), \
                patch.object(result_notes, "copy_crops", side_effect=copied):
            one = "\n".join(result_notes.render_figures_and_tables({"paper_id": "one"}, "v"))
            two = "\n".join(result_notes.render_figures_and_tables({"paper_id": "two"}, "v"))
        self.assertIn("![[_Attachments/one/F1.png|640]]", one)
        self.assertIn("![[_Attachments/two/F1.png|640]]", two)
        self.assertNotIn("![[F1.png|640]]", one + two)
        for pid in bundles:
            unit = {"sentence_ids": [], "outcome": {"source_objects": [f"{pid}:F1"]}}
            body = result_notes._result_body(unit, {"paper_id": pid}, "v", {
                f"{pid}:F1": {**bundles[pid][0], "vault_entry": copied("v", pid, [])['entries'][0]}})
            self.assertIn(f"![[_Attachments/{pid}/F1.png|640]]", body)

    def test_auto_block_drops_whole_quote_rows(self):
        block = notes.AUTO_START + "\n" + "\n".join(
            f"> [!quote]- [[P{i}]] · S{i}\n> {'x' * 700}" for i in range(10)) + "\n" + notes.AUTO_END
        trimmed = notes.cap_auto_block(block, 5000, "concept")
        self.assertLessEqual(len(trimmed.encode("utf-8")), 5000)
        for match in notes._CONCEPT_QUOTE_ROW.finditer(trimmed):
            self.assertEqual(match.group().splitlines()[1], "> " + "x" * 700)

    def test_capping_twice_or_over_an_old_notice_leaves_one_notice_and_no_link(self):
        block = notes.AUTO_START + "\n" + "\n".join(
            f"> [!quote]- [[P{i}]] · S{i}\n> {'x' * 700}" for i in range(10)) + "\n" + notes.AUTO_END
        old = "> Còn 3 mục chưa hiển thị — [mở trên dashboard](http://127.0.0.1:8765/neural?tab=note)"
        for start in (notes.cap_auto_block(block, 6000, "concept"), block.replace(notes.AUTO_END, old + "\n" + notes.AUTO_END)):
            again = notes.cap_auto_block(start, 3000, "concept")
            self.assertEqual(again.count("not shown; the complete list is in the dashboard."), 1)
            self.assertNotIn("dashboard](", again)
            self.assertNotIn("127.0.0.1", again)

    def test_nested_legacy_trim_first_and_every_block_stays_under_cap(self):
        important = "## Các bài nói gì\n\n| paper | finding |\n|---|---|\n| P | may improve |\n"
        legacy = "<details>\n<summary>Old source inventory (folded)</summary>\n\n"
        legacy += "<details>\n<summary>Nested</summary>\n" + "x" * 80000 + "\n</details>\n"
        block = notes.AUTO_START + "\n\n" + important + legacy + "\n</details>\n" + notes.AUTO_END
        trimmed = concept_notes._cap_node_block(block, 1200)
        self.assertLessEqual(len(trimmed.encode("utf-8")), 1200)
        self.assertIn("may improve", trimmed)
        self.assertIn(notes.AUTO_END, trimmed)
        oversized = notes.AUTO_START + "\n\n> [!quote]- [[P]]\n> " + "y" * 80000 + "\n\n" + notes.AUTO_END
        trimmed = notes.cap_auto_block(oversized, 1200, "concept")
        self.assertLessEqual(len(trimmed.encode("utf-8")), 1200)
        self.assertNotIn("> y", trimmed)
        paper = notes.AUTO_START + "\n\n## Summary\n\nKeep this.\n\n" + "\n\n".join(
            f"> **Result [p:E{i}]**\n> {'é' * 3000}\n> `{i}`" for i in range(30)) + "\n" + notes.AUTO_END
        trimmed = notes.cap_auto_block(paper, notes.PAPER_NOTE_MAX_BYTES, "paper")
        self.assertLessEqual(len(trimmed.encode("utf-8")), notes.PAPER_NOTE_MAX_BYTES)
        self.assertIn("Keep this.", trimmed)
        for match in notes._PAPER_QUOTE_ROW.finditer(trimmed):
            self.assertEqual(match.group().splitlines()[1], "> " + "é" * 3000)

    def test_descriptive_conflict_title_and_body_keep_directions(self):
        relation = {"shared_concept_name": "H3K9me3", "directions": ["increase", "no_change"]}
        left = {"outcome": {"measure": "intensity"}}
        right = {"outcome": {"measure": "intensity"}}
        title = result_notes._conflict_title(relation, left, right,
                                              {"first_author": "Alpha", "year": 2025},
                                              {"first_author": "Beta", "year": 2026})
        self.assertEqual(title, "H3K9me3 - Alpha 2025 vs Beta 2026 - (increase vs no_change)")
        body = result_notes._conflict_body(
            {"relation_id": "r1"},
            [{"statement": "", "direction": "increases", "species": "", "stage": "",
              "protocol": "", "measure": "H3K9me3", "sources": "", "quote": ""},
             {"statement": "", "direction": "no_significant_change", "species": "", "stage": "",
              "protocol": "", "measure": "H3K9me3", "sources": "", "quote": ""}],
            [])
        self.assertIn("| Direction | increases | no_significant_change |", body)
        self.assertIn("## What differs", body)
        self.assertNotIn("| Species |", body)  # nobody reports it: row omitted, no placeholder cells

    def test_descriptive_titles_regenerate_and_retire_old_names_reversibly(self):
        for pid, direction in (("alpha", "increase"), ("beta", "decrease")):
            self.paper(pid, [{"paper_id": pid, "unit_id": f"{pid}:E1", "seq": 1, "unit_type": "result",
                              "sentence_ids": [f"{pid}:S1"], "statement": "The marker changed.",
                              "outcome": {"measure": "marker", "direction": direction}}])
        con = self.connection("alpha", "beta")
        claims = config.vault_dir("v") / "Claims"
        claims.mkdir()
        old = claims / "CONFLICT - marker (2).md"
        result_notes._write_managed(old, {"type": "conflict", "id": "r1"}, "Old machine text.")
        human = claims / "CONFLICT - marker (3).md"
        result_notes._write_managed(human, {"type": "conflict", "id": "r2"}, "Old machine text.")
        human_note = "Human interpretation " + chr(0x2014) + " preserve."
        with human.open("a", encoding="utf-8") as stream:
            stream.write("\n" + human_note + "\n")
        base = {"from_unit": "alpha:E1", "to_unit": "beta:E1", "type": "contradicts", "status": "ai_proposed", "vault": "v"}
        db.write_jsonl(config.STORE / "relations.jsonl", [
            {**base, "relation_id": "r1", "shared_concept_name": "marker"},
            {**base, "relation_id": "r2", "shared_concept_name": "survival"},
            {**base, "relation_id": "r3", "shared_concept_name": "marker"}])
        with patch.object(result_notes.media, "bundles", return_value=[]):
            first_refresh = result_notes.refresh(con, "v")
            stems = node_profile._vault_conflict_ids("v")
            # r3 = same paper pair + same measure as r1: merged into r1's note, no "- duplicate" twin.
            self.assertEqual(set(stems), {"r1", "r2"})
            self.assertIn("marker", stems["r1"])
            self.assertIn("survival", stems["r2"])
            self.assertNotIn("(2)", stems["r2"])
            self.assertNotIn("duplicate", stems["r1"])
            self.assertIn("r3", (claims / f"{stems['r1']}.md").read_text(encoding="utf-8"))  # relation_ids
            self.assertIn("Alpha 2025 vs Beta 2025", stems["r1"])
            conflict_text = (claims / f"{stems['r1']}.md").read_text(encoding="utf-8")
            self.assertIn("increases", conflict_text)
            self.assertIn("decreases", conflict_text)
            self.assertFalse(old.exists())
            human_destination = None
            for migration in first_refresh["migrations"]:
                record = db.read_json(Path(migration))
                for entry in record["entries"]:
                    if entry["from"] == "Claims/CONFLICT - marker (3).md":
                        human_destination = config.vault_dir("v") / entry["to"]
            self.assertIsNotNone(human_destination)
            self.assertTrue(human_destination.is_file())
            self.assertIn(human_note.encode("utf-8"), human_destination.read_bytes())
            before = {p.name: p.read_bytes() for p in claims.glob("*.md")}
            result_notes.refresh(con, "v")
            self.assertEqual(before, {p.name: p.read_bytes() for p in claims.glob("*.md")})
            for migration in reversed(first_refresh["migrations"]):
                restored = vault_layout.undo(Path(migration))
                self.assertEqual(restored["conflicts"], [])
            self.assertTrue(old.exists())
            self.assertTrue(human.exists())
            self.assertIn(human_note.encode("utf-8"), human.read_bytes())

    def test_topic_render_is_moc_without_quote_dump_and_caps_members(self):
        records = [{"concept_id": "topic", "name": "Cryopreservation", "type": "topic", "parents": [], "synonyms": []}]
        for i in range(45):
            records.append({"concept_id": f"c{i}", "name": f"Node {i}", "type": "concept",
                            "parents": ["topic"], "synonyms": []})
            (config.vault_dir("v") / "Concepts").mkdir(parents=True, exist_ok=True)
            (config.vault_dir("v") / "Concepts" / f"Node {i}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8")
        registry = concepts.Registry(records)
        text = "\n".join(concept_notes._render_moc(
            records[0], {"p": [{}]}, [], registry, {"vault": "v", "conflicts": []}, "v"))
        self.assertIn("## Overview", text)
        self.assertIn("## Member notes", text)
        self.assertIn("[[Node 0]]", text)
        self.assertNotIn("Source passages", text)
        self.assertNotIn("[!quote]", text)
        self.assertLessEqual(text.count("[[Node "), 40)

    def test_topic_note_lists_its_results_and_the_cap_keeps_them(self):  # RL-10 A6 for MOC notes (researcher 2026-10-21)
        records = [{"concept_id": "topic", "name": "Gene expression", "type": "topic", "parents": [], "synonyms": []}]
        (config.vault_dir("v") / "Concepts").mkdir(parents=True, exist_ok=True)
        for i in range(45):
            records.append({"concept_id": f"c{i}", "name": f"Node {i}", "type": "concept", "parents": ["topic"], "synonyms": []})
            (config.vault_dir("v") / "Concepts" / f"Node {i}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8")
        registry = concepts.Registry(records)
        rows = [(f"EVD - paper E{i:04d}", "increases (not significant)" if i == 0 else "reports") for i in range(30)]
        text = concept_notes._render(records[0], {}, [], [], [], [], "", {}, "fp", registry,
                                     profile={"vault": "v", "conflicts": []}, conflict_ids={}, result_links=rows[:2])
        self.assertIn("## Results\n- [[EVD - paper E0000]] — increases (not significant)\n- [[EVD - paper E0001]] — reports\n", text)
        self.assertEqual(concept_notes.heading_problems(text, "moc"), [])
        misordered = text.replace("## Overview", "## Tmp").replace("## Results", "## Overview").replace("## Tmp", "## Results")
        self.assertTrue(concept_notes.heading_problems(misordered, "moc"))  # Results before Overview is flagged
        self.assertNotIn("## Results", concept_notes._render(records[0], {}, [], [], [], [], "", {}, "fp", registry,
                                                             profile={"vault": "v", "conflicts": []}, conflict_ids={}))
        block = "\n".join([notes.AUTO_START, *concept_notes._render_moc(records[0], {}, [], registry, {"vault": "v", "conflicts": []},
                                                                         "v", {}, rows), notes.AUTO_END, ""])
        capped = notes.cap_auto_block(block, 1400, "concept")
        self.assertLessEqual(len(capped.encode("utf-8")), 1400)
        self.assertNotIn("[[Node 0]]", capped)  # member list dropped first
        self.assertIn("## Results\n" + "\n".join(concept_notes.result_link_lines(rows)), capped)

    def test_moc_members_include_source_mentions_without_parent_or_ai_tags(self):
        topic = {"concept_id": "t", "name": "Cryopreservation", "type": "topic", "parents": [], "synonyms": []}
        member = {"concept_id": "m", "name": "Marker", "type": "molecule", "parents": [], "synonyms": []}
        absent = {"concept_id": "a", "name": "Absent", "type": "molecule", "parents": [], "synonyms": []}
        (config.vault_dir("v") / "Substances").mkdir()
        for row in (member, absent):
            (config.vault_dir("v") / "Substances" / f"{row['name']}.md").write_text("---\ntype: substance\n---\n", encoding="utf-8")
        self.paper("p", [], "Marker may be involved in cryopreservation.")
        own_evidence = [{"quote": "Marker may be involved in cryopreservation.", "statement": ""}]  # the topic's OWN evidence
        text = "\n".join(concept_notes._render_moc(topic, {"p": []}, own_evidence, concepts.Registry([topic, member, absent]),
                                                   {"vault": "v", "conflicts": []}, "v"))
        self.assertIn("[[Marker]]", text)
        self.assertIn("**Substances** (1)", text)  # counts are about this topic only
        elsewhere = "\n".join(concept_notes._render_moc(topic, {"p": []}, [], concepts.Registry([topic, member, absent]),
                                                        {"vault": "v", "conflicts": []}, "v"))
        self.assertNotIn("[[Marker]]", elsewhere)  # named in a linked paper but not in the topic's own evidence
        self.assertNotIn("## Member notes", elsewhere)  # empty section hidden
        self.assertNotIn("[[Absent]]", text)
        self.assertNotIn("Marker may be involved", text)
        self.assertNotIn("[!quote]", text)

    def test_paper_byte_budget_includes_frontmatter_and_preserves_human_tail(self):
        self.paper("p", [])
        meta = db.read_json(db.paper_dir("p") / "metadata.json")
        meta.update(title="Paper title", source_status="active")
        block = notes.AUTO_START + "\n\n## Summary\n\nKeep summary.\n\n" + "\n\n".join(
            f"> **Result [p:E{i}]**\n> {'é' * 1500}\n> `{i}`" for i in range(80)) + "\n" + notes.AUTO_END
        con = self.connection("p")
        with patch.object(notes, "render_auto_block", return_value=block):
            notes.write_paper_note(meta, "v", con=con, registry=concepts.Registry([]))
            path = config.vault_dir("v") / "Papers" / "Paper p.md"
            self.assertLessEqual(path.stat().st_size, notes.PAPER_NOTE_MAX_BYTES)
            with path.open("a", encoding="utf-8") as stream:
                stream.write("Human notes — preserve.\n")
            tail = path.read_text(encoding="utf-8").split(notes.AUTO_END, 1)[1]
            notes.write_paper_note(meta, "v", con=con, registry=concepts.Registry([]))
            self.assertLessEqual(path.stat().st_size, notes.PAPER_NOTE_MAX_BYTES)
            self.assertEqual(path.read_text(encoding="utf-8").split(notes.AUTO_END, 1)[1], tail)

    def test_evd_significance_association_hedge_and_grouped_data(self):
        folder = db.paper_dir("p")
        folder.mkdir(parents=True)
        db.write_jsonl(folder / "sentences.jsonl", [{
            "sentence_id": "p:S1", "seq": 1, "page": 2, "section": "results",
            "text": "The marker was associated with survival (P < 0.01) and may improve outcome."}])
        unit = {"paper_id": "p", "unit_id": "p:E1", "statement": "The marker was associated with survival.",
                "sentence_ids": ["p:S1"], "model": {}, "intervention": {"agent": "marker"},
                "outcome": {"measure": "survival", "values": [{"value": 4}, {"group": "treated", "value": 5}]}}
        meta = {"paper_id": "p", "note_name": "Paper P", "first_author": "P", "year": 2025}
        values = result_notes._evidence_frontmatter(unit, meta, "v", "finding")
        self.assertEqual(values["significance"], "significant")
        self.assertEqual(values["predicate"], "associated_with")
        self.assertEqual(values["knowledge_level"], "statistical_association")
        self.assertEqual(result_notes._data_rows(unit), [["treated", 5, "", "", ""]])
        body = result_notes._result_body(unit, meta, "v", {})
        self.assertIn("⚠️ Hedged wording", body)

    def test_statistics_missing_defaults_negations_and_source_association(self):
        unit = {"unit_id": "p:E1", "paper_id": "p", "sentence_ids": ["p:S1"],
                "statement": "The marker increases survival.", "outcome": {"direction": "increase", "significance": "not_tested"}}
        for source, expected in (("P<0.01", "significant"), ("P<0.05", "significant"), ("p = 0.002", "significant"),
                                 ("An increase ***", "significant"), ("significantly increased", "significant"),
                                 ("not significant", "ns"), ("no significant change", "ns"),
                                 ("did not differ significantly (p = 0.4)", "ns"),
                                 ("p > 0.05", "ns"), ("not statistically tested", "not_tested"),
                                 ("No statistical information is given.", "not_reported")):
            with self.subTest(source=source):
                self.paper("p", [unit], source)
                self.assertEqual(result_notes._significance(unit), expected)
        self.paper("p", [unit], "Survival correlated with the marker.")
        fm = result_notes._evidence_frontmatter(unit, {"paper_id": "p", "note_name": "Paper p"}, "v", "Result")
        self.assertEqual(fm["predicate"], "associated_with")
        self.assertEqual(fm["knowledge_level"], "statistical_association")
        self.assertEqual(result_notes._data_rows({"outcome": {"values": [{"group": "—", "value": 2}]}}), [])

    def test_claim_derivation_uses_same_paper_sentences_not_shared_bibliography(self):
        (config.vault_dir("v") / "Substances").mkdir()
        (config.vault_dir("v") / "Substances" / "Marker.md").write_text("---\ntype: substance\n---\n", encoding="utf-8")
        for pid in ("p", "q"):
            self.paper(pid, [
                {"unit_id": f"{pid}:E1", "paper_id": pid, "seq": 1, "unit_type": "result", "sentence_ids": [f"{pid}:S1"],
                 "statement": "Marker may increase survival.", "outcome": {"direction": "increase", "measure": "survival"}, "cited_refs": [1]},
                {"unit_id": f"{pid}:C1", "paper_id": pid, "seq": 2, "unit_type": "conclusion", "sentence_ids": [f"{pid}:S2"],
                 "statement": "Marker may improve survival.", "cited_sentence_ids": [f"{pid}:S1"]},
                {"unit_id": f"{pid}:C2", "paper_id": pid, "seq": 3, "unit_type": "conclusion", "sentence_ids": [f"{pid}:S2"],
                 "statement": "An unrelated conclusion.", "cited_refs": [1]}])
        with patch.object(result_notes.media, "bundles", return_value=[]):
            result_notes.refresh(self.connection("p", "q"), "v")
        paths = result_notes._existing_notes("v")
        fm, _ = notes._split_frontmatter(paths["p:C1"].read_text(encoding="utf-8"))
        self.assertEqual(fm["derived_from"], [f"[[{paths['p:E1'].stem}]]"])
        self.assertEqual(fm["about"], ["[[Marker]]"])
        fm, _ = notes._split_frontmatter(paths["p:C2"].read_text(encoding="utf-8"))
        self.assertEqual(fm["derived_from"], ["[[Paper p]]"])

    def test_claim_sentence_overlap_and_existing_about_links_are_capped(self):
        folder = config.vault_dir("v") / "Concepts"
        folder.mkdir()
        for i in range(10):
            (folder / f"Marker{i}.md").write_text("---\ntype: concept\n---\n", encoding="utf-8")
        statement = " ".join(f"Marker{i}" for i in range(11))
        about = result_notes._about_links(statement, "v")
        self.assertEqual(len(about), 8)
        self.assertNotIn("[[Marker10]]", about)
        self.paper("p", [
            {"unit_id": "p:E1", "paper_id": "p", "seq": 1, "unit_type": "result", "sentence_ids": ["p:S1"],
             "statement": "Marker may increase survival.", "outcome": {"direction": "increase", "measure": "survival"}},
            {"unit_id": "p:C1", "paper_id": "p", "seq": 2, "unit_type": "conclusion", "sentence_ids": ["p:S1", "p:S2"],
             "statement": statement}])
        with patch.object(result_notes.media, "bundles", return_value=[]):
            result_notes.refresh(self.connection("p"), "v")
        paths = result_notes._existing_notes("v")
        fm, _ = notes._split_frontmatter(paths["p:C1"].read_text(encoding="utf-8"))
        self.assertEqual(fm["derived_from"], [f"[[{paths['p:E1'].stem}]]"])
        self.assertEqual(fm["about"], about)

    def test_report_and_card_mark_copied_source_hedges_and_current_model_context(self):
        unit = {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result", "sentence_ids": ["p:S1"],
                "statement": "Marker improves survival.", "paper": {"note_name": "Paper p", "evidence_type": "review"}}
        self.paper("p", [unit], "Marker may improve survival.")
        with patch.object(concept_notes.structure, "_fresh_overview", return_value={
                "groups_models": [{"text": "Bovine MII oocytes"}]}):
            report = "\n".join(concept_notes._papers_report({}, {}, [unit]))
            self.assertIn("Bovine MII oocytes", report)
            self.assertIn("review — secondary", report)
            self.assertIn("⚠️ Hedged wording: may", report)
            self.assertEqual(concept_notes._paper_model_labels("p", {"species": "Mouse", "stage": "blastocyst"}),
                             ("Mouse", "blastocyst"))
        profile = {"vault": "v", "papers": [{"paper_id": "p"}], "card": {"statements": [
            {"field": "definition", "text": "Marker improves survival.", "sentence_ids": ["p:S1"]}]}}
        self.assertIn("⚠️ Hedged wording: may", "\n".join(concept_notes._typed_sections(
            {"name": "Marker", "type": "concept"}, profile, None)))
        self.assertIn("⚠️ Hedged wording: may", "\n".join(concept_notes._render_card_statement(
            profile["card"]["statements"][0], profile)))
        db.write_json(db.paper_dir("p") / "section_profile.json", {"answers": {
            "who": {"text": "Mouse blastocysts"}}})
        with patch.object(concept_notes.structure, "_fresh_overview", return_value=None), \
                patch.object(concept_notes.structure, "_profile_report", return_value={"ready": True}):
            self.assertEqual(concept_notes._paper_model_labels("p", {}), ("Mouse blastocysts", "Mouse blastocysts"))
        with patch.object(concept_notes.structure, "_fresh_overview", return_value=None), \
                patch.object(concept_notes.structure, "_profile_report", return_value={"ready": False}):
            self.assertNotIn("Mouse", " / ".join(concept_notes._paper_model_labels("p", {})))

    def test_claim_links_and_review_label_and_model_fallback(self):
        (config.vault_dir("v") / "Substances").mkdir(parents=True)
        (config.vault_dir("v") / "Substances" / "Marker.md").write_text("---\ntype: substance\n---\n", encoding="utf-8")
        about = result_notes._about_links("Marker increases survival", "v")
        values = result_notes._claim_values({"unit_id": "p:C1", "statement": "Marker increases survival"},
                                             {"paper_id": "p", "note_name": "Paper P", "first_author": "P", "year": 2025},
                                             "v", ["[[EVD - p]]"], about)
        self.assertEqual(values["derived_from"], ["[[EVD - p]]"])
        self.assertIn("[[Marker]]", values["about"])
        self.assertIn("[[Marker]]", result_notes._claim_body(
            {"statement": "Marker increases survival", "sentence_ids": []},
            {"paper_id": "p", "note_name": "Paper P"}, values["derived_from"], values["about"]))
        unit = {"paper_id": "p", "paper": {"note_name": "Review P", "evidence_type": "review"},
                "unit_type": "result", "statement": "A review statement", "sentence_ids": []}
        report = "\n".join(concept_notes._papers_report({}, {}, [unit]))
        self.assertIn("review — secondary", report)


class NoteCapAndMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=self.root, STORE=self.root / "store",
            PAPERS_DIR=self.root / "store/papers", VAULTS_DIR=self.root / "vaults",
            CONFIG_DIR=self.root / "config", LOGS_DIR=self.root / "logs")
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.temp.cleanup()

    def test_final_note_caps_include_frontmatter_and_preserve_human_text(self):
        rows = "\n\n".join(
            f"> **S{i:04d} [E{i:04d}]**\n> " + ("verbatim evidence " * 100)
            for i in range(500))
        block = notes.AUTO_START + "\n## Results\n\n" + rows + "\n" + notes.AUTO_END
        human = "\n## My notes\n\nHuman wording — preserve exactly.\n"
        paper = "---\ntype: paper\npaper_id: paper\n---\n\n# Synthetic\n\n" + block + human
        capped = notes.cap_managed_note(paper, notes.PAPER_NOTE_MAX_BYTES, "paper")
        self.assertLessEqual(len(capped.encode("utf-8")), notes.PAPER_NOTE_MAX_BYTES)
        self.assertIn(human, capped)
        self.assertIn("more items not shown; the complete list is in the dashboard.", capped)
        self.assertNotIn("127.0.0.1", capped)

        record = {"concept_id": "synthetic", "name": "Synthetic", "type": "molecule",
                  "synonyms": [], "parents": []}
        node_block = notes.AUTO_START + "\n" + "\n\n".join(
            f"> [!quote]- [[P{i}]] · S{i:04d}\n> " + ("source text " * 120)
            for i in range(500)) + "\n" + notes.AUTO_END + "\n"
        self.assertEqual(concept_notes._write_note("v", record, node_block, "fp"), "created")
        path = config.vault_dir("v") / "Substances/Synthetic.md"
        first = path.read_text(encoding="utf-8")
        self.assertLessEqual(len(first.encode("utf-8")), notes.NODE_NOTE_MAX_BYTES)
        # Exceed the spare space left by whole-row trimming so this update
        # must reduce machine content rather than merely preserve it as-is.
        human = "\n## My notes\n\n" + "Human wording — preserve exactly.\n" * 128
        self.assertGreater(len((first + human).encode("utf-8")), notes.NODE_NOTE_MAX_BYTES)
        path.write_bytes((first + human).encode("utf-8"))  # exact bytes; write_text would add CRLF on Windows
        self.assertEqual(concept_notes._write_note("v", record, node_block, "fp2"), "updated")
        second = path.read_text(encoding="utf-8")
        self.assertTrue(second.endswith(human))
        self.assertTrue(path.read_bytes().endswith(human.encode("utf-8")))
        self.assertLessEqual(len(second.encode("utf-8")), notes.NODE_NOTE_MAX_BYTES)
        before = path.read_bytes()
        self.assertEqual(concept_notes._write_note("v", record, node_block, "fp2"), "unchanged")
        self.assertEqual(path.read_bytes(), before)

    def test_media_is_deduplicated_grouped_and_omits_missing_fields(self):
        formula = {
            "object_id": "paper:M1234567890abcdef",
            "kind": "formula",
            "source_locator": {"source": "main", "page": 12,
                                "region_kind": "manual_object_region",
                                "sentence_ids": ["paper:S0042"]},
            "caption": [], "mentions": [], "data": [],
        }
        duplicate = {**formula, "source_locator": {"source": "main", "page": 13,
                                                     "region_kind": "manual_object_region"}}
        table = {
            "object_id": "paper:T1", "label": "Table 1", "kind": "table",
            "source_locator": {"source": "main", "page": 4, "region_kind": "manual_object_region"},
            "caption": [{"text": "Exact table caption."}], "mentions": [], "data": [],
        }
        entries = [
            {"object_id": formula["object_id"], "status": "copied", "vault_path": "_Attachments/paper/M1234567890abcdef.png"},
            {"object_id": table["object_id"], "status": "copied", "vault_path": "_Attachments/paper/T1.png"},
        ]
        meta = {"paper_id": "paper", "source_files": []}
        with patch.object(result_notes.media, "bundles", return_value=[formula, duplicate, table]), \
             patch.object(result_notes, "copy_crops", return_value={"entries": entries, "paths": {}}):
            first = "\n".join(result_notes.render_figures_and_tables(meta, "v"))
            second = "\n".join(result_notes.render_figures_and_tables(meta, "v"))
        self.assertEqual(first, second)
        self.assertIn("### Formulas (1)", first)
        headings = [line for line in first.splitlines() if line.startswith("### ")]
        self.assertEqual(headings, ["### Table 1 — Exact table caption", "### Formulas (1)"])
        self.assertEqual(first.count("M1234567890abcdef"), 1)  # only the embed path; no hash in the visible label
        self.assertIn("Formula (p. 12) — không có caption", first)
        self.assertEqual(presentation.machine_ids(first), [])
        self.assertNotIn("p. 13", first)
        self.assertNotIn("None", first)
        self.assertNotIn("Region completeness: not assessed", first)
        self.assertIn("Exact table caption.", first)
        self.assertNotIn("dashboard", first)  # VN-08: no local link in a note
        self.assertNotIn("Store table data:", first)


BLOCK = "\n".join([
    notes.AUTO_START, "## Evidence", "",
    "<details>", "<summary>Old inventory</summary>", "",
    "## Entity", "",
    "> [!quote]- Paper", "> text", "",
    "<details>", "<summary>Inner</summary>", "", "### p1", "- row", "</details>", "",
    "<details>", "<summary>Inner 2</summary>", "x", "</details>",
    "</details>", "after", notes.AUTO_END])


class DetailsToCalloutTests(unittest.TestCase):
    def test_folds_become_nested_callouts(self):
        out = notes.details_to_callouts(BLOCK)
        self.assertNotIn("<details>", out)
        self.assertNotIn("<summary>", out)
        lines = out.split("\n")
        self.assertIn("> [!note]- Old inventory", lines)
        self.assertIn("> ## Entity", lines)
        self.assertIn("> > [!quote]- Paper", lines)
        self.assertIn("> > [!note]- Inner", lines)
        self.assertIn("> > ### p1", lines)
        self.assertIn("> > x", lines)
        # siblings are separated by a parent-level line so they stay two callouts
        i = lines.index("> > [!note]- Inner 2")
        self.assertEqual(lines[i - 1], ">")
        # text after the outer fold is outside every callout
        self.assertEqual(lines[lines.index("after") - 1], "")

    def test_managed_note_is_converted_and_human_text_untouched(self):
        human = "\n## My notes\n<details>human html stays</details>\n"
        text = "---\ntype: substance\n---\n" + BLOCK + human
        out = notes.cap_managed_note(text, 60 * 1024, "concept")
        self.assertTrue(out.endswith(human))
        self.assertNotIn("<summary>", out[:-len(human)])

    def test_converted_note_still_respects_cap(self):
        rows = []
        for i in range(400):
            rows += [f"> [!quote]- Paper p · `p:E{i}`", f"> Evidence sentence {i} with a complete source passage.", ""]
        block = "\n".join([notes.AUTO_START, "## Summary", "", "<details>", "<summary>All</summary>", "",
                           *rows, "</details>", notes.AUTO_END])
        out = notes.cap_managed_note("---\ntype: concept\n---\n" + block, 8000, "concept")
        self.assertLessEqual(len(out.encode("utf-8")), 8000)
        self.assertNotIn("<details>", out)


class SeparateCalloutTests(unittest.TestCase):
    def test_conflict_callout_after_summary_card_is_its_own_block(self):
        out = notes.separate_callouts("> [!abstract] Summary\n> text\n> [!conflict] Open conflict\n> both sides")
        self.assertEqual(out.split("\n")[2:4], ["", "> [!conflict] Open conflict"])

    def test_nested_sibling_and_child_callouts(self):
        out = notes.separate_callouts("> [!note]- A\n> > [!quote]- q1\n> > a\n> > [!quote]- q2")
        lines = out.split("\n")
        self.assertEqual(lines[1], "> > [!quote]- q1")  # first child: no separator needed
        self.assertEqual(lines[lines.index("> > [!quote]- q2") - 1], ">")


ROOT = Path(__file__).parents[1]


class NoteNormalizerTests(unittest.TestCase):
    def test_details_shapes_and_stray_tags_are_removed(self):
        shapes = (
            "<details><summary>Inline title</summary>\nbody\n</details>",
            "<details>\n\n<summary>Separated title</summary>\nbody\n</details>",
            "<summary>Stray title</summary>\nplain\n</summary>",
            "> [!note]- Details\n> > <summary>Legacy title</summary>",
        )
        for source in shapes:
            with self.subTest(source=source):
                converted = notes.details_to_callouts(source)
                self.assertNotRegex(converted, r"</?(?:details|summary)>")
        self.assertIn("> [!note]- Inline title", notes.details_to_callouts(shapes[0]))
        self.assertIn("> [!note]- Separated title", notes.details_to_callouts(shapes[1]))

    def test_callouts_are_separated_after_quoted_text_and_embeds(self):
        quoted = notes.separate_callouts("> [!abstract] Summary\n> quoted\n> [!conflict] Conflict")
        self.assertEqual(quoted.splitlines()[-2:], ["", "> [!conflict] Conflict"])
        embed = notes.separate_callouts("![[Figure.png]]\n> Caption\n> [!note] Details")
        self.assertEqual(embed.splitlines()[-2:], ["", "> [!note] Details"])

    def test_managed_paper_block_uses_the_same_normalizer(self):
        text = "---\ntype: paper\n---\n" + notes.AUTO_START + "\n" \
               "> [!abstract] Summary\n> body\n> [!info] Info\n" + notes.AUTO_END
        rendered = notes.cap_managed_note(text, notes.PAPER_NOTE_MAX_BYTES, "paper")
        self.assertIn("> [!abstract] Summary\n> body\n\n> [!info] Info", rendered)

    def test_all_targeted_write_text_calls_pin_lf(self):
        files = ("notes.py", "result_notes.py", "vault_layout.py", "vault_plugins.py")
        for name in files:
            tree = ast.parse((ROOT / "scripts/kbsys" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                    continue
                if node.func.attr != "write_text":
                    continue
                newline = next((kw.value for kw in node.keywords if kw.arg == "newline"), None)
                self.assertIsNotNone(newline, f"{name}:{node.lineno} write_text lacks newline")
                self.assertEqual(getattr(newline, "value", None), "\n", f"{name}:{node.lineno}")

    def test_result_stats_are_carried_without_blank_properties(self):
        base = {"unit_id": "p:E1", "paper_id": "p", "unit_type": "result",
                "outcome": {"measure": "marker"}}
        with patch.object(result_notes, "_source_text", return_value=""), \
                patch.object(result_notes, "_node_link", side_effect=lambda value, vault: value or ""):
            missing = result_notes._evidence_frontmatter(base, {"paper_id": "p", "note_name": "P"}, "v", "x")
            self.assertNotIn("n", missing)
            self.assertNotIn("p", missing)
            self.assertNotIn("test", missing)
            populated = {**base, "result_group": {"statistics": {"n": 12, "p": "0.02", "test": "ANOVA"}}}
            values = result_notes._evidence_frontmatter(populated, {"paper_id": "p", "note_name": "P"}, "v", "x")
        self.assertEqual(values["n"], 12)
        self.assertEqual(values["p"], "0.02")
        self.assertEqual(values["test"], "ANOVA")

    def test_conflict_titles_use_content_and_never_relation_hex(self):
        relation = {"relation_id": "e15e0ad46efff651", "directions": ["increase", "decrease"]}
        left = {"model": {"species": "Bovine", "stage": "blastocyst"},
                "outcome": {"measure": "H3K9me3"}}
        right = {"model": {"species": "Mouse", "stage": "MII"},
                 "outcome": {"measure": "H3K9me3"}}
        title = result_notes._conflict_title(relation, left, right,
                                              {"first_author": "Alpha", "year": 2024},
                                              {"first_author": "Beta", "year": 2025})
        self.assertIn("Bovine/blastocyst vs Mouse/MII", title)
        self.assertIn("increase vs decrease", title)
        self.assertNotIn(relation["relation_id"], title)

    def test_conflict_title_names_each_sides_own_species_never_borrowed(self):
        relation = {"relation_id": "r1", "shared_concept_name": "H3K9me3"}
        meta = [{"first_author": "Alpha", "year": 2024}, {"first_author": "Beta", "year": 2025}]
        one_sided = result_notes._conflict_title(relation, {"model": {"species": "Bovine"}}, {}, *meta)
        self.assertNotIn("Bovine", one_sided)  # the other side states nothing: no species for the pair
        side = lambda species: {"species": species, "stage": None}
        filled = result_notes._conflict_title(relation, {}, {}, *meta, sides=[side("Bovine" + result_notes.PAPER_LEVEL), side("Mouse")])
        self.assertIn("Bovine vs Mouse", filled)  # the table's own values, paper-level mark stripped
        self.assertNotIn(result_notes.PAPER_LEVEL.strip(), filled)
        same = result_notes._conflict_title(relation, {}, {}, *meta, sides=[side("Mouse"), side("Mouse")])
        self.assertEqual(same.count("Mouse"), 1)
        self.assertEqual(result_notes._conflict_title(relation, {}, {}, *meta, sides=[side("Mouse"), side(None)]),
                         "H3K9me3 - Alpha 2024 vs Beta 2025")

    def test_effect_vs_no_effect_conflict_carries_the_non_significance_caution(self):
        sides = [{"statement": "", "direction": "", "species": "", "stage": "", "protocol": "", "measure": "m",
                  "sources": "", "quote": ""}] * 2
        caution = "not evidence of no effect"
        self.assertIn(caution, result_notes._conflict_body({"relation_id": "r", "type": "effect_vs_no_effect"}, sides, []))
        self.assertNotIn(caution, result_notes._conflict_body({"relation_id": "r", "type": "direction_differs"}, sides, []))
        merged = result_notes._conflict_body({"relation_id": "r", "type": "direction_differs"}, sides, [],
                                             extra=[({"relation_id": "q", "type": "effect_vs_no_effect"}, sides, [])])
        self.assertEqual(merged.count(caution), 1)

    def test_opposes_requires_relation_and_same_specific_measure(self):
        evd = {"p:E1": Path("E1.md"), "p:E2": Path("E2.md")}
        claim = {"unit_id": "p:C1", "sentence_ids": ["p:S1"]}
        related = {"p:E1": {"unit_id": "p:E1", "sentence_ids": ["p:S1"], "outcome": {"measure": "marker"}},
                   "p:E2": {"unit_id": "p:E2", "sentence_ids": ["q:S1"], "outcome": {"measure": "broad concept"}}}
        relation = {"from_unit": "p:E1", "to_unit": "p:E2", "type": "contradicts"}
        supports, opposes = result_notes._claim_relation_links(claim, [relation], evd, related)
        self.assertEqual(opposes, [])
        related["p:E2"]["sentence_ids"] = ["p:S2"]
        related["p:E2"]["outcome"]["measure"] = "marker"
        claim["opposes"] = ["p:E2"]
        supports, opposes = result_notes._claim_relation_links(claim, [relation], evd, related)
        self.assertEqual(opposes, ["[[E2]]"])
        unrelated = {**related, "p:E2": {**related["p:E2"], "outcome": {"measure": "species count"}}}
        _, opposes = result_notes._claim_relation_links(claim, [relation], evd, unrelated)
        self.assertEqual(opposes, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
