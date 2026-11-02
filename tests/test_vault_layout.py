"""Vault layout v2 (D3.44): folders, reversible migration, maps, settings, also_in. Offline temp dirs."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from kbsys import config, db, notes, review_policy, vault_layout, result_notes

M = notes.AUTO_START
E = notes.AUTO_END
INDEX = f"{notes.INDEX_FRONTMATTER}{M}\n{notes.INDEX_MARK}\n# {{}}\n\n- item\n{E}\n"


def tree(folder):
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


class VaultLayoutTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", LOGS_DIR=root / "store" / "logs",
            CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.con = db.connect()
        self.vdir = config.vault_dir("v")

    def tearDown(self):
        self.con.close()
        self.patch.stop()
        self.temp.cleanup()

    def write(self, rel, text):
        path = self.vdir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def legacy_vault(self):
        self.write("Home.md", "# V\n\nResearcher scope.\n")
        self.write("Papers/P.md", "---\ntype: paper\n---\n[[EG]] [[Mouse]] [[Cryo]] [[Idea]] [[Missing]] [[Molecules/EG]]\n")
        self.write("Molecules/EG.md", "---\ntype: substance\n---\n# EG\n")
        self.write("Molecules/Molecules.md", INDEX.format("Molecules") + "my own remark\n")  # person wrote here
        self.write("Species/Mouse.md", "# Mouse species\n")
        self.write("Cell Types/Mouse.md", "# Mouse cell type\n")  # same target: collision
        self.write("Topics/Cryo.md", "---\ntype: concept\n---\n# Cryo\n")
        self.write("Topics/Topics.md", INDEX.format("Topics"))
        self.write("Views/Research Methods.md", INDEX.format("Methods").replace("- item", "- [[Research map]]"))
        self.write("Views/Results and media.md", INDEX.format("Results and media").replace("- item", "- [[Research map]]"))
        self.write("Research map.md", INDEX.format("Research map").replace("- item", "- [[Research Methods]]"))
        self.write("Chú giải graph.md", INDEX.format("Chú giải graph"))
        self.write("My Ideas/Idea.md", "My private idea [[EG]]\n")
        self.write("My Ideas/img/fig.png", "png-bytes")
        self.write("My Ideas/My Ideas.md", INDEX.format("My Ideas") + "typed idea line\n")
        self.write("Knowledge Gaps/Gap.md", "Gap text\n")

    def test_folder_mapping_and_stub_recognition(self):
        self.assertEqual(notes.TERM_FOLDERS["species"], "Organisms")
        self.assertEqual(notes.TERM_FOLDERS["cell_types"], "Organisms")
        self.assertEqual(notes.TERM_FOLDERS["molecules"], "Substances")
        self.assertEqual(notes.TERM_FOLDERS["topics"], "_Maps")
        self.assertEqual(notes.NODE_FOLDERS.count("Organisms"), 1)
        species = notes._stub_template("Organisms", "Mouse", "species")
        cell = notes._stub_template("Organisms", "Oocyte", "cell_types")
        self.assertIn("tags: [species]", species)
        self.assertIn("type: cell_type", cell)
        self.assertTrue(notes._is_pure_stub("Organisms", "Mouse", species))
        self.assertTrue(notes._is_pure_stub("Organisms", "Oocyte", cell))
        topic = notes._stub_template("_Maps", "Cryo")
        self.assertIn("type: moc", topic)  # topic notes are MOC hubs
        self.assertTrue(notes._is_pure_stub("_Maps", "Cryo", topic.replace("type: moc", "type: concept")))

    def test_migration_dry_run_and_link_refusal(self):
        self.legacy_vault()
        original = tree(self.vdir)
        with self.assertRaises(ValueError):
            vault_layout.require_current("v")

        dry = vault_layout.migrate("v", dry_run=True, con=self.con)
        self.assertEqual(tree(self.vdir), original)  # nothing touched
        # Species is processed before Cell Types: first source wins, the second is reported and left.
        self.assertEqual(dry["collisions"], [{"from": "Cell Types/Mouse.md", "to": "Organisms/Mouse.md"}])
        self.assertEqual(sorted(dry["retire"]), ["Research map.md", "Topics/Topics.md",
                                                 "Views/Research Methods.md", "Views/Results and media.md"])
        self.assertEqual(dry["human_index_kept"], ["Molecules/Molecules.md"])
        self.assertEqual(dry["per_folder"]["My Ideas -> Mine/Ideas"], 2)  # list note kept with its folder
        # [[Missing]] was already unresolved; the path link [[Molecules/EG]] would break -> refused.
        self.assertEqual(dry["links_before"]["unresolved"], 1)
        self.assertEqual(dry["links_after"]["unresolved"], 2)
        self.assertIn("refused", dry)
        with self.assertRaises(ValueError):
            vault_layout.migrate("v", con=self.con)
        self.assertEqual(tree(self.vdir), original)

    def test_migration_real_idempotent_undo(self):
        self.legacy_vault()
        # Drop the path link so the move does not increase unresolved links.
        self.write("Papers/P.md", "---\ntype: paper\n---\n[[EG]] [[Mouse]] [[Cryo]] [[Idea]] [[Missing]]\n")
        original = tree(self.vdir)
        report = vault_layout.migrate("v", con=self.con)
        self.assertEqual(report["links_after"]["unresolved"], report["links_before"]["unresolved"])
        self.assertTrue(report["human_unchanged"])
        self.assertTrue(Path(report["snapshot"]["snapshot"], "vault", "My Ideas", "Idea.md").is_file())
        moved = tree(self.vdir)
        self.assertEqual(moved["Mine/Ideas/Idea.md"], original["My Ideas/Idea.md"])
        self.assertEqual(moved["Mine/Ideas/img/fig.png"], original["My Ideas/img/fig.png"])
        self.assertEqual(moved["Mine/Ideas/My Ideas.md"], original["My Ideas/My Ideas.md"])
        self.assertEqual(moved["Organisms/Mouse.md"], original["Species/Mouse.md"])
        self.assertEqual(moved["Cell Types/Mouse.md"], original["Cell Types/Mouse.md"])  # collision left in place
        self.assertEqual(moved["Molecules/Molecules.md"], original["Molecules/Molecules.md"])  # human text kept
        self.assertIn("_Maps/Cryo.md", moved)
        self.assertIn("_Maps/Chú giải graph.md", moved)
        self.assertNotIn("Research map.md", moved)
        self.assertFalse((self.vdir / "Views").exists() or (self.vdir / "Topics").exists()
                         or (self.vdir / "My Ideas").exists())
        self.assertTrue(list((config.STORE / "removed").rglob("Research map.md")))
        vault_layout.require_current("v")  # only collisions / kept human index remain: not blocking

        again = vault_layout.migrate("v", con=self.con)
        self.assertEqual((again["moves"], again["retire"]), (0, []))
        self.assertNotIn("map", again)

        restored = vault_layout.undo(config.ROOT / report["map"])
        self.assertEqual(restored["conflicts"], [])
        self.assertEqual(tree(self.vdir), original)
        self.assertFalse((self.vdir / "Mine").exists() or (self.vdir / "Substances").exists())

    def test_maps_home_log_conflicts(self):
        self.write("Home.md", "# Vitrification\n\ntopic words\n")
        db.write_jsonl(config.STORE / "concepts.jsonl", [{"concept_id": "index", "name": "Index", "type": "concept"}])
        self.write("Substances/EG.md", "---\ntype: substance\nkb_summary: EG was added.\nevidence_count: 3\n"
                                       "source_papers: [p1, p2]\nhas_conflict: true\n---\n# EG\n")
        self.write("Substances/DMSO.md", f"---\ntype: substance\n---\n{M}\n> [!danger] Potential conflict\n{E}\n")
        self.write("Substances/Old.md", f"---\ntype: substance\nhas_conflict: false\n---\n{M}\n> [!danger] x\n{E}\n")
        self.write("Claims/C1.md", "---\ntype: conflict\n---\n")
        self.write("_Maps/Cryo.md", "---\ntype: moc\n---\n# Cryo\n")
        self.write("Papers/P1.md", "---\ntype: paper\nyear: 2020\nevidence_type: review\n---\n")
        self.write("Substances/Stub.md", "---\nkb_stub: true\n---\n")
        vault_layout.write_maps("v")
        index = (self.vdir / "_Maps/Index.md").read_text(encoding="utf-8")
        self.assertIn("- [[EG]] — EG was added. (3 đơn vị bằng chứng · 2 bài)", index)
        self.assertIn("- [[P1]] — 2020 · review", index)
        self.assertNotIn("[[Stub]]", index)
        conflicts = (self.vdir / "_Maps/Open conflicts.md").read_text(encoding="utf-8")
        for name in ("EG", "DMSO"):
            self.assertIn(f"[[{name}]]", conflicts)
        self.assertIn("[[C1|C1]]", conflicts)
        self.assertNotIn("[[Old]]", conflicts)
        home = (self.vdir / "Home.md").read_text(encoding="utf-8")
        self.assertTrue(home.startswith("---\ntype: home\n---\n# Vitrification\n\ntopic words\n"))
        self.assertIn("[[Cryo]]", home)
        self.assertIn("Mine/Hypotheses/", home)
        self.assertEqual(review_policy._scope("v"), ["Vitrification"])  # the machine block is not researcher scope
        # Human text added to Home and Index survives regeneration; one machine block only.
        self.write("Home.md", home + "\nresearcher line\n")
        (self.vdir / "_Maps/Index.md").write_text(index + "\nmy index note\n", encoding="utf-8")
        vault_layout.write_maps("v")
        home2 = (self.vdir / "Home.md").read_text(encoding="utf-8")
        self.assertEqual(home2.count(M), 1)
        self.assertIn("researcher line", home2)
        self.assertIn("my index note", (self.vdir / "_Maps/Index.md").read_text(encoding="utf-8"))
        # Log is append-only.
        vault_layout.append_log("v", "first run")
        first = (self.vdir / "_Maps/Log.md").read_text(encoding="utf-8")
        vault_layout.append_log("v", "second run")
        second = (self.vdir / "_Maps/Log.md").read_text(encoding="utf-8")
        self.assertTrue(second.startswith(first))
        self.assertIn("first run", first)
        self.assertIn("second run", second[len(first):])

    def test_obsidian_settings_merge_and_write_once(self):
        obsidian = self.vdir / ".obsidian"
        self.write(".obsidian/appearance.json", json.dumps({"enabledCssSnippets": ["mine"], "theme": "x"}))
        self.write(".obsidian/templates.json", json.dumps({"folder": "My templates"}))
        self.write(".obsidian/graph.json", json.dumps({"hideUnresolved": False,
                                                       "search": notes.LEGACY_GRAPH_FILTERS[0]}))
        self.write("_Bases/Papers.base", "researcher: edited\n")
        vault_layout.write_maps("v")
        notes.write_graph_settings("v")
        appearance = json.loads((obsidian / "appearance.json").read_text(encoding="utf-8"))
        self.assertEqual(appearance, {"enabledCssSnippets": ["mine", "kb-conflict", "kb-graph"], "theme": "x"})
        self.assertEqual(json.loads((obsidian / "templates.json").read_text(encoding="utf-8")),
                         {"folder": "My templates"})
        css = (obsidian / "snippets/kb-conflict.css").read_text(encoding="utf-8")
        self.assertIn('data-callout="conflict"', css)
        self.assertIn("220, 38, 38", css)
        self.assertIn(".plugin-extended-graph", (obsidian / "snippets/kb-graph.css").read_text(encoding="utf-8"))
        self.assertEqual((self.vdir / "_Bases/Papers.base").read_text(encoding="utf-8"), "researcher: edited\n")
        for name in ("Substances", "Methods", "Organisms", "Open conflicts", "Review queue"):
            data = yaml.safe_load((self.vdir / f"_Bases/{name}.base").read_text(encoding="utf-8"))
            self.assertEqual(data["views"][0]["type"], "table")
        templates = list((self.vdir / "_Templates").glob("Template - *.md"))
        self.assertEqual(len(templates), 5)
        for path in templates:
            fm, _ = notes._split_frontmatter(path.read_text(encoding="utf-8"))
            self.assertTrue({"type", "status", "created", "related", "evidence", "sources"} <= set(fm), path)
        graph = json.loads((obsidian / "graph.json").read_text(encoding="utf-8"))
        self.assertTrue(graph["hideUnresolved"])  # generated graph setting is deterministic
        self.assertFalse(graph["showAttachments"])
        self.assertEqual(graph["search"], notes.GRAPH_FILTER)
        self.assertIn('-path:"_Templates"', graph["search"])
        self.assertTrue((self.vdir / "_Maps/Chú giải graph.md").is_file())
        vault_layout.write_maps("v")  # second run changes nothing
        self.assertEqual(json.loads((obsidian / "appearance.json").read_text(encoding="utf-8"))["enabledCssSnippets"],
                         ["mine", "kb-conflict", "kb-graph"])

    def paper(self, pid, vaults, sentences=True):
        folder = db.paper_dir(pid)
        db.write_json(folder / "metadata.json", {
            "paper_id": pid, "title": "Vitrification study", "vaults": vaults, "authors": [],
            "source_files": [], "classification": "CONNECTED", "analysis_status": "done", "note_name": pid})
        if sentences:
            db.write_jsonl(folder / "sentences.jsonl", [{
                "sentence_id": pid + ":S0001", "paper_id": pid, "seq": 1, "passage_id": pid + ":0001", "page": 1,
                "section": "methods", "kind": "text", "text": "Oocytes underwent vitrification.", "source": "main"}])
        db.load_paper(self.con, pid)
        for vault in vaults:
            self.con.execute("INSERT OR IGNORE INTO paper_vaults VALUES (?,?)", (pid, vault))

    def test_also_in_and_other_vault_is_not_a_review_reason(self):
        for vault in ("v", "w"):
            self.write(f"../{vault}/Home.md", "# Vitrification\n")
        self.paper("shared", ["v", "w"])
        results = review_policy.refresh_paper(self.con, "shared")
        self.assertTrue(all(r["connected"] for r in results.values()))
        self.assertEqual(review_policy.pending(self.con), [])

        self.paper("note", ["v", "w"], sentences=False)
        meta = db.read_json(db.paper_dir("note") / "metadata.json")
        notes.write_paper_note(meta, "v", con=self.con)
        text = (self.vdir / "Papers/note.md").read_text(encoding="utf-8")
        fm, _ = notes._split_frontmatter(text)
        self.assertEqual(fm["also_in"], ["w"])
        self.assertIn("> **Cũng nằm trong vault:** w", text)


class VaultMapAndPluginStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            DB_PATH=root / "store" / "library.db", LOGS_DIR=root / "store" / "logs",
            CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.vault = "v"
        self.vdir = config.vault_dir(self.vault)
        self.vdir.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_conflict_title_and_undo_compatible_migration_map(self):
        left = {"authors": [{"family": "Alpha"}], "year": 2024}
        right = {"authors": [{"family": "Beta"}], "year": 2025}
        relation = {"shared_concept_name": "H3K9me3"}
        title = result_notes._conflict_title(relation, {}, {}, left, right)
        self.assertEqual(title, "H3K9me3 - Alpha 2024 vs Beta 2025")
        old = self.vdir / "Claims" / "CONFLICT - old.md"
        new = self.vdir / "Claims" / "CONFLICT - H3K9me3 - Alpha 2024 vs Beta 2025.md"
        old.parent.mkdir(parents=True)
        old.write_text("human conflict review\n", encoding="utf-8")
        result_notes._move_conflict_note(old, new, self.vault)
        self.assertFalse(old.exists())
        self.assertEqual(new.read_text(encoding="utf-8"), "human conflict review\n")
        maps = list((config.STORE / "migrations").glob("*_result_notes.json"))
        self.assertEqual(len(maps), 1)
        record = json.loads(maps[0].read_text(encoding="utf-8"))
        self.assertEqual(record["entries"][0]["from"], "Claims/CONFLICT - old.md")
        self.assertEqual(record["entries"][0]["to"], new.relative_to(self.vdir).as_posix())

    def test_evd_properties_and_result_table_have_empty_missing_cells(self):
        unit = {"unit_id": "p:E1", "unit_type": "result", "outcome": {}}
        meta = {"paper_id": "p", "note_name": "Paper", "authors": [{"family": "A"}], "year": 2024}
        values = result_notes._evidence_frontmatter(unit, meta, self.vault, "finding")
        self.assertEqual(values["subject"], ["not stated"])  # RL-10: no subject, no cell type
        self.assertEqual(values["object"], [])
        self.assertEqual(values["stage"], [])
        self.assertNotIn("not annotated", json.dumps(values))
        self.assertEqual(result_notes._data_rows(unit), [])
        table = notes._paper_results_table([
            "| Paper / source | Role | Intervention | Model | Outcome | Evidence / page |",
            "|---|---|---|---|---|---|",
            "| [[Paper#Results|Paper]] |  | not annotated | not annotated | n: not annotated | p. not annotated |",
        ])
        self.assertNotIn("Paper#Results", "\n".join(table))
        self.assertNotIn("not annotated", "\n".join(table))
        with patch.object(result_notes, "_source_text", return_value=""), \
                patch.object(result_notes, "_read_sentences", return_value={}):
            body = result_notes._result_body(unit, meta, self.vault, {}, ())
        self.assertNotIn("not annotated", body)
        self.assertTrue(result_notes._is_page_preview({"region_kind": "source_page_preview"}))
        self.assertFalse(result_notes._is_page_preview({"region_kind": "unlocated_object",
                                                        "object_region_status": "not_located"}))

    def test_generated_notes_protect_my_notes_and_never_write_mine(self):
        notes.write_graph_settings(self.vault)
        vault_layout.write_maps(self.vault)
        generated = [p for p in self.vdir.rglob("*.md") if notes.INDEX_MARK in p.read_text(encoding="utf-8")
                     or p.name == "Home.md" or "_Templates" in p.parts]
        self.assertTrue(generated)
        for path in generated:
            fm, _ = notes._split_frontmatter(path.read_text(encoding="utf-8"))
            self.assertTrue(fm.get("type"), path)
        self.assertFalse(any(p.is_file() for p in (self.vdir / "Mine").rglob("*") if (self.vdir / "Mine").exists()))
        self.assertFalse(any("Mine/" in str(value).replace("\\", "/") for value in notes.LIST_NOTES.values()))

        values = {"type": "result", "id": "E1"}
        path = self.vdir / "Results" / "E1.md"
        result_notes._write_managed(path, values, "## Data\n\nsource", "EVD - E1")
        result_notes._write_managed(path, values, "## Data\n\nupdated", "EVD - E1")
        text = path.read_text(encoding="utf-8")
        self.assertIn("## My notes", text)

    def test_index_cap_and_large_group_links_to_base(self):
        folder = self.vdir / "Substances"
        folder.mkdir()
        for i in range(1800):
            (folder / f"Substance {i}.md").write_text(
                f"---\ntype: substance\nkb_summary: summary {i} with enough text to make the group large\n---\n",
                encoding="utf-8")
        vault_layout.write_maps(self.vault)
        index = (self.vdir / "_Maps" / "Index.md").read_text(encoding="utf-8")
        self.assertLessEqual(len(index.encode("utf-8")), 60 * 1024)
        self.assertIn("[[_Bases/Substances.base|Base]]", index)
        self.assertIn("[[Substances/Substances|folder note]]", index)

    def test_graph_merge_keeps_researcher_groups_and_recolours(self):
        """U6g: researcher groups first, a recoloured machine group keeps its colour, legacy and duplicate machine
        queries dropped, missing machine groups added; VN-09 accepts the result."""
        from kbsys import conform_vault
        from kbsys.conform import Vault
        graph = self.vdir / ".obsidian" / "graph.json"
        graph.parent.mkdir(parents=True)
        mine = {"query": "tag:#mine", "color": {"a": 1, "rgb": 1}}
        recolour = {"query": 'path:"Papers/"', "color": {"a": 1, "rgb": 42}}
        graph.write_text(json.dumps({"zoom": 2, "colorGroups": [
            recolour, {"query": "[type:paper]", "color": {"a": 1, "rgb": 3}}, mine, "junk",
            {"query": 'path:"Papers/"', "color": {"a": 1, "rgb": 0x2563EB}}]}), encoding="utf-8")
        notes.write_graph_settings(self.vault)
        settings = json.loads(graph.read_text(encoding="utf-8"))
        groups = settings["colorGroups"]
        self.assertEqual(settings["zoom"], 2)
        self.assertEqual(groups[0], mine)
        self.assertEqual([g["query"] for g in groups[1:]], [q for q, _, _ in notes.GRAPH_GROUPS])
        self.assertEqual(next(g for g in groups if g["query"] == 'path:"Papers/"')["color"]["rgb"], 42)
        self.assertEqual(next(g for g in groups if g["query"] == 'path:"Concepts/"')["color"]["rgb"], 0x0EA5E9)
        self.assertEqual(list(conform_vault.graph_settings(Vault(self.vault, self.vdir))), [])
        notes.write_graph_settings(self.vault)  # idempotent
        self.assertEqual(json.loads(graph.read_text(encoding="utf-8"))["colorGroups"], groups)
        legend = (self.vdir / notes.MAPS / f"{notes.LEGEND_NOTE}.md").read_text(encoding="utf-8")
        self.assertIn("Khái niệm (Concepts)", legend)
        self.assertNotIn("Còn lại: khái niệm", legend)

    def test_graph_group_order_check_names_missing_and_out_of_order(self):
        queries = [q for q, _, _ in notes.GRAPH_GROUPS]
        groups = [{"query": q} for q in queries]
        self.assertEqual(notes.missing_graph_groups([{"query": "tag:#x"}] + groups), [])
        self.assertEqual(notes.missing_graph_groups(groups[1:]), [queries[0]])
        swapped = [groups[1], groups[0]] + groups[2:]
        self.assertEqual(notes.missing_graph_groups(swapped), [queries[1]])
        self.assertEqual(notes.missing_graph_groups(None), queries)

    def test_graph_colours_command_backs_up_and_checks(self):
        from kbsys import mother_vault
        self.assertTrue(str(config.VAULTS_DIR).startswith(tempfile.gettempdir()))
        graph = self.vdir / ".obsidian" / "graph.json"
        graph.parent.mkdir(parents=True)
        old = json.dumps({"colorGroups": [{"query": "[type:concept]", "color": {"a": 1, "rgb": 5}}]})
        graph.write_text(old, encoding="utf-8")
        report = mother_vault.graph_colours([self.vault])
        self.assertEqual(set(report), {self.vault, "mother"})
        self.assertEqual(report[self.vault]["problems"], "ok")
        self.assertEqual(report["mother"]["problems"], "ok")
        backup = Path(report[self.vault]["backups"][0])
        self.assertEqual(backup.parent, graph.parent)
        self.assertEqual(backup.read_text(encoding="utf-8"), old)
        mother = json.loads((config.VAULTS_DIR / ".obsidian" / "graph.json").read_text(encoding="utf-8"))
        self.assertEqual(notes.missing_graph_groups(mother["colorGroups"]), [])
        with self.assertRaises(ValueError):
            mother_vault.graph_colours(["nope"], mother=False)
        self.assertFalse(config.vault_dir("nope").exists())

    def test_graph_colours_refuses_paths_and_keeps_first_backup(self):
        """Review U6g: '..'/'.' never reach the project root or the mother vault; a same-second rerun keeps the
        pre-merge backup; a graph.json that is a JSON list neither crashes the writers nor VN-09."""
        from datetime import datetime
        from kbsys import conform_vault, mother_vault
        from kbsys.conform import Vault
        self.assertTrue(str(config.VAULTS_DIR).startswith(tempfile.gettempdir()))
        (self.vdir / ".obsidian").mkdir(parents=True)
        for name in ("..", ".", "v/../..", "nope"):
            with self.assertRaises(ValueError):
                mother_vault.graph_colours([self.vault, name], mother=False)
        self.assertEqual([p for p in config.ROOT.rglob("*") if p.is_file()], [])  # nothing written, not even for "v"
        graph = self.vdir / ".obsidian" / "graph.json"
        graph.write_text("[1]", encoding="utf-8")
        self.assertEqual(list(conform_vault.graph_settings(Vault(self.vault, self.vdir))),
                         [".obsidian/graph.json is not a JSON object"])
        fixed = datetime(2026, 10, 25, 4, 0, 0)
        with patch.object(mother_vault, "datetime") as clock:
            clock.now.return_value = fixed
            first = mother_vault.graph_colours([self.vault])
            second = mother_vault.graph_colours([self.vault])
        self.assertEqual(first[self.vault]["problems"], "ok")
        self.assertEqual(first["mother"]["problems"], "ok")
        self.assertEqual(Path(second[self.vault]["backups"][0]).read_text(encoding="utf-8"), "[1]")

    def test_extended_graph_default_state_follows_graph_json(self):
        """U6g root cause: with syncDefaultState off, Extended Graph applied its saved Default state (empty colour groups
        and filter) over graph.json; notes/graph-colours turn it on, keep other plugin keys, and VN-09 flags it off."""
        from kbsys import conform_vault, mother_vault
        from kbsys.conform import Vault
        self.assertTrue(str(config.VAULTS_DIR).startswith(tempfile.gettempdir()))
        data = self.vdir / ".obsidian" / "plugins" / "extended-graph" / "data.json"
        data.parent.mkdir(parents=True)
        old = {"syncDefaultState": False, "states": [{"id": "default-vault", "engineOptions": {"colorGroups": []}}],
               "interactiveSettings": {"link": {"colors": [
                   {"type": "conflicts_with", "color": "#dc2626"}, {"type": "opposes", "color": "#dc2626"},
                   {"type": "supports", "color": "#15803d"}]}}}
        data.write_text(json.dumps(old), encoding="utf-8")
        notes.write_graph_settings(self.vault)
        self.assertEqual(list(conform_vault.graph_settings(Vault(self.vault, self.vdir))), [])
        saved = json.loads(data.read_text(encoding="utf-8"))
        self.assertEqual(saved, {**old, "syncDefaultState": True})
        data.write_text(json.dumps(old), encoding="utf-8")
        self.assertIn("extended-graph: syncDefaultState is off (its saved Default state replaces graph.json colours and filter)",
                      list(conform_vault.graph_settings(Vault(self.vault, self.vdir))))
        report = mother_vault.graph_colours([self.vault], mother=False)
        self.assertEqual(report[self.vault]["problems"], "ok")
        backup = next(Path(b) for b in report[self.vault]["backups"] if "extended-graph" in b)
        self.assertEqual(json.loads(backup.read_text(encoding="utf-8")), old)
        self.assertEqual(json.loads(data.read_text(encoding="utf-8"))["syncDefaultState"], True)
        data.write_text("[1]", encoding="utf-8")  # not an object: left alone
        notes.sync_extended_graph(self.vdir)
        self.assertEqual(data.read_text(encoding="utf-8"), "[1]")
        self.assertIn("extended-graph/data.json is not a JSON object", list(conform_vault.graph_settings(Vault(self.vault, self.vdir))))
        mother = config.VAULTS_DIR / ".obsidian" / "plugins" / "extended-graph" / "data.json"
        mother.parent.mkdir(parents=True)
        mother.write_text(json.dumps(old), encoding="utf-8")
        mother_vault._merge_graph_settings()  # the mother vault turns the flag on too
        self.assertEqual(json.loads(mother.read_text(encoding="utf-8")), {**old, "syncDefaultState": True})

    def test_graph_and_plugin_states(self):
        graph = self.vdir / ".obsidian" / "graph.json"
        graph.parent.mkdir(parents=True)
        graph.write_text(json.dumps({"hideUnresolved": False}), encoding="utf-8")
        notes.write_graph_settings(self.vault)
        settings = json.loads(graph.read_text(encoding="utf-8"))
        self.assertTrue(settings["hideUnresolved"])
        self.assertLessEqual(len(notes.GRAPH_GROUPS), 10)  # researcher 2026-10-24: every visible note type coloured
        self.assertEqual([c for q, c, _ in notes.GRAPH_GROUPS].count("#DC2626"), 1)  # red only for conflicts
        self.assertEqual(len({c for _, c, _ in notes.GRAPH_GROUPS}), len(notes.GRAPH_GROUPS))
        self.assertTrue({'path:"Concepts/"', 'path:"_Maps/"'} <= {q for q, _, _ in notes.GRAPH_GROUPS})

        source = Path(__file__).parents[1] / "config" / "obsidian_plugins.yaml"
        spec = yaml.safe_load(source.read_text(encoding="utf-8"))["plugins"]["extended-graph"]["default_settings"]
        # states without the plugin's `id`/engineOptions made its settings load throw (no Default state); the plugin
        # makes its own Default state, which syncDefaultState fills from graph.json (U6g review, 2026-10-25)
        self.assertNotIn("states", spec)
        self.assertNotIn("colorGroups", spec)  # graph colours live only in graph.json (notes.GRAPH_GROUPS)
        self.assertIs(spec["syncDefaultState"], True)
        self.assertIn({"type": "conflicts_with", "color": "#dc2626"},
                      spec["interactiveSettings"]["link"]["colors"])


ROOT = Path(__file__).parents[1]


class VaultMapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(config, ROOT=root, STORE=root / "store",
                                    PAPERS_DIR=root / "store" / "papers",
                                    DB_PATH=root / "store" / "library.db",
                                    LOGS_DIR=root / "store" / "logs",
                                    CONFIG_DIR=root / "config", VAULTS_DIR=root / "vaults")
        self.patch.start()
        self.vdir = config.vault_dir("v")
        self.vdir.mkdir(parents=True)

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_maps_show_conflict_content_and_prune_only_known_empty_spills(self):
        claims = self.vdir / "Claims"
        claims.mkdir()
        (claims / "CONFLICT - Marker.md").write_text(
            "---\ntype: conflict\nstatus: open\naxis: species\ntitle: Marker, Bovine\n---\n", encoding="utf-8")
        (self.vdir / "crops").mkdir()
        (self.vdir / "Sources").mkdir()
        (self.vdir / "Mine" / "Decisions").mkdir(parents=True)
        vault_layout.write_maps("v")
        text = (self.vdir / "_Maps" / "Open conflicts.md").read_text(encoding="utf-8")
        self.assertIn("Marker, Bovine", text)
        self.assertFalse((self.vdir / "crops").exists())
        self.assertFalse((self.vdir / "Sources").exists())
        self.assertTrue((self.vdir / "Mine" / "Decisions").exists())
        self.assertIn('status == "open"', vault_layout.BASES["Open conflicts"])

    def test_open_conflicts_group_by_paper_pair_with_readable_titles(self):
        claims = self.vdir / "Claims"
        claims.mkdir()
        for stem, pair in (("CONFLICT - H3K9me3 - Lee 2025 vs Hale 2024", "Lee 2025 vs Hale 2024"),
                           ("CONFLICT - 5mC - Lee 2025 vs Hale 2024", "Lee 2025 vs Hale 2024"),
                           ("CONFLICT - 5mC - Lee 2023 vs Hale 2024", "Lee 2023 vs Hale 2024")):
            (claims / f"{stem}.md").write_text(
                f"---\ntype: conflict\nstatus: open\naxis: species\npapers: {pair}\ntitle: {stem[11:]}\nseverity: X\n---\n",
                encoding="utf-8")
        (claims / "CONFLICT - gone.md").write_text("---\ntype: conflict_history\nstatus: withdrawn\n---\n", encoding="utf-8")
        vault_layout.write_maps("v")
        text = (self.vdir / "_Maps" / "Open conflicts.md").read_text(encoding="utf-8")
        self.assertIn("## Lee 2025 vs Hale 2024 (2)", text)
        self.assertIn("## Lee 2023 vs Hale 2024 (1)", text)
        self.assertIn("[[CONFLICT - 5mC - Lee 2023 vs Hale 2024|5mC - Lee 2023 vs Hale 2024]]", text)
        self.assertNotIn("gone", text)
        self.assertNotIn("species", text)  # the internal axis is not a heading any more

    def test_lab_template_and_extended_graph_modes(self):
        vault_layout.write_templates(self.vdir)
        template = (self.vdir / "_Templates" / "Template - Lab note.md").read_text(encoding="utf-8")
        self.assertIn("type: lab_note", template)
        self.assertIn("tags: [lab_notes]", template)
        # the yaml graph modes (Bản đồ, Chi tiết) were removed in U6g: no plugin `id`, a fresh install's settings load threw


if __name__ == "__main__":
    unittest.main()
