"""Dashboard read-path performance: manifests, caches, lazy pagination and warm-up."""

import unittest
from pathlib import Path
import shutil
import subprocess
import tempfile
from unittest.mock import patch
from kbsys import concepts, config, db, media, neural, notes, concept_notes, sentences, structure, node_profile
import threading


class ReadPathBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            VAULTS_DIR=root / "vaults", LOGS_DIR=root / "logs", DB_PATH=root / "store" / "library.db",
            CONFIG_DIR=Path(__file__).resolve().parents[1] / "config")
        self.paths.start()
        (root / "store").mkdir(parents=True)
        concepts.invalidate_registry()
        media._HASH_CACHE.clear()

    def tearDown(self):
        concepts.invalidate_registry()
        self.paths.stop()
        self.temp.cleanup()

    def _paper_files(self, paper_id, rows=()):
        folder = db.paper_dir(paper_id)
        folder.mkdir(parents=True, exist_ok=True)
        db.write_json(folder / "metadata.json", {"paper_id": paper_id, "title": paper_id,
                                                   "source_files": []})
        db.write_jsonl(folder / "sentences.jsonl", rows)
        db.write_jsonl(folder / "figures.jsonl", [])
        db.write_jsonl(folder / "tables.jsonl", [])
        db.write_jsonl(folder / "evidence.jsonl", [])
        return folder

    def test_bundles_unchanged_manifest_does_not_build(self):
        self._paper_files("paper")
        first = media.build("paper", detect=False)
        with patch.object(media, "_build", side_effect=AssertionError("read rebuilt media")):
            bundles = media.bundles("paper")
        self.assertEqual(bundles, first["bundles"])

    def test_second_node_reader_uses_d377_manifest_and_links_each_paper_once(self):
        con = db.connect()
        try:
            paper_id = "paper"
            sid = f"{paper_id}:S0001"
            row = {"sentence_id": sid, "paper_id": paper_id, "seq": 1, "page": 1,
                   "section": "results", "kind": "text", "source": "main",
                   "passage_id": f"{paper_id}:P1", "text": "EG was measured in the results.",
                   "mentions": []}
            folder = self._paper_files(paper_id, [row])
            db.reload_paper(con, paper_id)
            con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES(?,?)", (paper_id, "v"))
            con.execute("INSERT OR IGNORE INTO concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id) "
                        "VALUES(?,?,?,?,?,?)", ("local:eg", paper_id, "mentioned", 1,
                                                   '["results"]', sid))
            con.commit()
            db.write_jsonl(config.STORE / "concepts.jsonl", [{"concept_id": "local:eg", "name": "EG",
                                                               "type": "molecule", "synonyms": []}])
            note = config.vault_dir("v") / "Substances" / "EG.md"
            note.parent.mkdir(parents=True)
            note.write_text(f"---\nkb_kind: chemical\n---\nEG source {sid}\n", encoding="utf-8")

            manifest = media.build(paper_id, detect=False)
            manifest["version"] = 2
            manifest["status"] = "partial"
            # D3.77/D3.78 manifests predate stat stamps and contain historical
            # implementation fingerprints that must not invalidate a read.
            manifest.pop("input_stats", None)
            for key in ("media.py", "extract.py", "objects.py", "pymupdf"):
                manifest["input_hashes"][key] = "historical-runtime-value"
            manifest["bundles"] = [{
                "object_id": f"{paper_id}:F1", "kind": "figure", "label": "Figure 1",
                "caption": [dict(row)], "data": [], "mentions": [], "anchors": [],
                "unresolved_sentence_ids": [], "associated_units": [],
                "source_locator": {"source": "main", "page": 1, "bbox": None,
                                    "region_kind": "unlocated_object", "status": "unavailable",
                                    "expanded_with": [], "locator_basis": "stored_locator"},
                "crop": {"path": None, "sha256": None, "source_sha256": None, "status": "unavailable"},
                "caption_basis": "stored_caption_ids", "stored_caption_sentence_ids": [sid],
                "table": None,
            }]
            # Publish the digest over the manifest payload, excluding the
            # digest field itself, exactly as media.build does.
            manifest.pop("output_sha256", None)
            manifest.pop("cache_hit", None)
            manifest["output_sha256"] = media._digest(manifest)
            db.write_json(folder / "media_manifest.json", manifest)
            concepts.invalidate_registry()
            sentences._READ_LINK_CACHE.clear()
            with patch.object(media, "_build", side_effect=AssertionError("read rebuilt media")), \
                    patch.object(sentences, "link_page_breaks_copy",
                                 wraps=sentences.link_page_breaks_copy) as linked:
                neural.node_reader(con, "v", "Substances/EG.md", include_media=True)
                neural.node_reader(con, "v", "Substances/EG.md", include_media=True)
            self.assertLessEqual(linked.call_count, 1)
        finally:
            con.close()

    def test_registry_read_uses_persisted_file_without_rebuild(self):
        path = config.STORE / "concepts.jsonl"
        db.write_jsonl(path, [{"concept_id": "local:eg", "name": "EG", "type": "molecule",
                               "synonyms": [], "parents": []}])
        con = db.connect()
        try:
            with patch.object(concepts, "build_registry", side_effect=AssertionError("read rebuilt registry")):
                registry = concepts.load_registry(con)
        finally:
            con.close()
        self.assertEqual(registry.lookup("EG")["concept_id"], "local:eg")

    def test_completion_cache_skips_overview_validation_and_invalidates_on_file_change(self):
        paper_id = "paper"
        sid = f"{paper_id}:S0001"
        row = {"sentence_id": sid, "paper_id": paper_id, "seq": 1, "page": 1,
               "section": "introduction", "kind": "text", "source": "main",
               "passage_id": f"{paper_id}:P1", "text": "EG is a chemical.", "mentions": []}
        folder = self._paper_files(paper_id, [row])
        payload = {field: [] for field in structure.OVERVIEW_FIELDS}
        payload["research_question"] = [{"text": row["text"], "sentence_ids": [sid], "certainty": "stated"}]
        db.write_json(folder / "paper_overview.json", {
            "schema_version": 1, "paper_id": paper_id,
            "source_sha256": structure.workflow.fingerprint([folder / "sentences.jsonl"]),
            "source_revision": structure.source_revision(paper_id),
            **payload, "overview_sha256": structure._digest(payload), "reference_only": True})
        structure._COMPLETION_CACHE.clear()
        structure._OVERVIEW_CACHE.clear()
        with patch.object(structure, "_validate_overview", wraps=structure._validate_overview) as validate:
            first = structure.completion(paper_id)
            second = structure.completion(paper_id)
            self.assertEqual(first, second)
            self.assertEqual(validate.call_count, 1)
            db.write_jsonl(folder / "sentences.jsonl", [row, {**row, "sentence_id": f"{paper_id}:S0002", "seq": 2,
                                                               "text": "A second source sentence."}])
            changed = structure.completion(paper_id)
        self.assertFalse(changed["overview"]["ready"])
        self.assertEqual(validate.call_count, 1)

    def test_resolve_sources_batches_sentence_queries_per_paper(self):
        con = db.connect()
        try:
            source_ids = []
            for paper_id in ("p1", "p2"):
                rows = []
                for index in range(1, 7):
                    sid = f"{paper_id}:S{index:04d}"
                    source_ids.append(sid)
                    rows.append({"sentence_id": sid, "paper_id": paper_id, "seq": index, "page": 1,
                                 "section": "results", "kind": "text", "source": "main",
                                 "passage_id": f"{paper_id}:P1", "text": f"Source sentence {index}."})
                self._paper_files(paper_id, rows)
                con.execute("INSERT INTO papers(paper_id,title,evidence_type,publication_types) VALUES(?,?,?,?)",
                            (paper_id, paper_id, "primary", "[]"))
                con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES(?,?)", (paper_id, "v"))
                con.executemany(
                    "INSERT INTO sentences(sentence_id,paper_id,seq,passage_id,source,page,section,kind,text) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    [(r["sentence_id"], paper_id, r["seq"], r["passage_id"], r["source"], r["page"],
                      r["section"], r["kind"], r["text"]) for r in rows])
            con.commit()
            statements = []
            con.set_trace_callback(statements.append)
            result = neural.resolve_sources(con, "v", source_ids, limit=24)
            con.set_trace_callback(None)
        finally:
            con.close()
        self.assertEqual(len(result["items"]), len(source_ids))
        selects = [sql for sql in statements if sql.lstrip().upper().startswith("SELECT")]
        # One scope query and one batched sentence query per paper; citation
        # attachment is also batched and does not scale with sentence count.
        self.assertLessEqual(len(selects), 2 * 2 + 2)
        self.assertLess(len(selects), len(source_ids))

    def test_full_node_note_drops_stub_boilerplate_but_excerpt_drops_it_too(self):
        vault_root = config.vault_dir("v") / "Concepts"
        vault_root.mkdir(parents=True)
        path = vault_root / "EG.md"
        path.write_text(notes._stub_template("Concepts", "EG", "concepts"), encoding="utf-8")
        record = {"concept_id": "local:eg", "name": "EG", "type": "concept", "synonyms": []}
        block = "<!-- kb:auto:start -->\n## Synthesis\n\nGenerated.\n<!-- kb:auto:end -->\n"
        self.assertEqual(concept_notes._write_note("v", record, block, "fp"), "updated")
        text = path.read_text(encoding="utf-8")
        boilerplate = "Papers that mention this appear under **Backlinks** (right-hand panel in Obsidian)."
        self.assertNotIn(boilerplate, text)
        excerpt, _, _ = neural._clean_excerpt(f"# EG\n\n{boilerplate}\n\nGenerated source text.")
        self.assertNotIn("# EG", excerpt)
        self.assertNotIn(boilerplate, excerpt)
        self.assertIn("Generated source text.", excerpt)

    def test_node_card_view_renders_fixture_fields_and_quotes_not_metadata(self):
        node = shutil.which("node") or r"C:\Program Files\nodejs\node.exe"
        if not Path(node).is_file():
            self.skipTest("Node runtime absent; no dependency installation")
        path = Path(__file__).parents[1] / "scripts" / "kbsys" / "web" / "neural.js"
        script = r'''const fs=require('fs'),vm=require('vm');
const code=fs.readFileSync(process.argv[1],'utf8');
const ctx={document:{querySelector:()=>({content:'fixture'})}};
vm.createContext(ctx);const prefix=code.slice(0,code.indexOf('async function openRecord'));const d394=code.slice(code.indexOf('// D3.94 card display helpers'));vm.runInContext(prefix+'\n'+d394,ctx);
ctx.fixture={card:{status:'accepted',card_currency:'current',node_type:'substance',fields:{definition:{text:'EG may improve survival.',hedge:'none',sentence_ids:['smith2010_example_paper_title_e6e4:S0125']},cautions:{text:'Use with care.',hedge:'hedged',sentence_ids:['smith2010_example_paper_title_e6e4:S0125']},chem_roles:{value:'other',sentence_ids:['smith2010_example_paper_title_e6e4:S0125']}},quotes:{'smith2010_example_paper_title_e6e4:S0125':{sentence_id:'smith2010_example_paper_title_e6e4:S0125',paper_id:'smith2010_example_paper_title_e6e4',page:2,section:'results',text:'Ethylene glycol may improve survival.'}},new_paper_ids:[],new_unit_ids:[]},card_status:'accepted'};
console.log(vm.runInContext('nodeCardView(fixture)',ctx));'''
        run = subprocess.run([node, "-e", script, str(path)], capture_output=True,
                             text=True, encoding="utf-8", check=True)
        html = run.stdout
        self.assertIn("EG may improve survival.", html)
        self.assertIn("Ethylene glycol may improve survival.", html)
        self.assertIn("Smith 2010 · S0125", html)
        self.assertIn("/pdf/smith2010_example_paper_title_e6e4#page=2", html)
        self.assertIn("Định nghĩa", html)
        self.assertIn("Lưu ý", html)
        self.assertIn("Vai trò hóa học", html)
        self.assertNotIn("other", html)
        self.assertEqual(html.count("Có điều kiện"), 1)
        self.assertNotIn("card_currency", html)
        self.assertNotIn("new_paper_ids", html)


class ReadPathCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = __import__("tempfile").TemporaryDirectory()
        root = Path(self.temp.name)
        self.paths = patch.multiple(
            config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store" / "papers",
            LOGS_DIR=root / "logs", CACHE_DIR=root / "store" / "cache",
            VAULTS_DIR=root / "vaults", DB_PATH=root / "store" / "library.db",
            CONFIG_DIR=Path(__file__).resolve().parents[1] / "config")
        self.paths.start()
        (root / "store").mkdir(parents=True)
        (root / "vaults" / "v").mkdir(parents=True)
        concepts.invalidate_registry()
        neural.clear_node_cache()
        node_profile.clear_build_cache()

    def tearDown(self):
        concepts.invalidate_registry()
        neural.clear_node_cache()
        node_profile.clear_build_cache()
        self.paths.stop()
        self.temp.cleanup()

    def _paper(self, con, paper_id, rows):
        folder = db.paper_dir(paper_id)
        folder.mkdir(parents=True, exist_ok=True)
        db.write_json(folder / "metadata.json", {
            "paper_id": paper_id, "title": paper_id, "evidence_type": "primary",
            "source_files": [],
        })
        db.write_jsonl(folder / "sentences.jsonl", rows)
        db.write_jsonl(folder / "evidence.jsonl", [])
        db.write_json(folder / "structure.json", {})
        db.write_json(folder / "protocol_profile.json", {})
        db.write_json(folder / "section_profile.json", {})
        db.write_jsonl(folder / "figures.jsonl", [])
        db.write_jsonl(folder / "tables.jsonl", [])
        db.write_jsonl(folder / "references.jsonl", [])
        con.execute("INSERT INTO papers(paper_id,title,evidence_type,publication_types) VALUES(?,?,?,?)",
                    (paper_id, paper_id, "primary", "[]"))
        con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES(?,?)", (paper_id, "v"))
        con.executemany(
            "INSERT INTO sentences(sentence_id,paper_id,seq,passage_id,source,page,section,kind,text) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            [(row["sentence_id"], paper_id, row["seq"], row["passage_id"], row["source"],
              row["page"], row["section"], row["kind"], row["text"]) for row in rows])

    def _row(self, paper_id, seq, text=None):
        sid = f"{paper_id}:S{seq:04d}"
        return {"sentence_id": sid, "paper_id": paper_id, "seq": seq, "page": 1,
                "section": "results", "kind": "text", "source": "main",
                "passage_id": f"{paper_id}:P1",
                "text": text or f"Embryo source sentence {seq}.", "mentions": []}

    def _registry(self, name="Embryo", concept_id="local:embryo"):
        db.write_jsonl(config.STORE / "concepts.jsonl", [{
            "concept_id": concept_id, "name": name, "type": "species",
            "synonyms": [], "parents": [],
        }])

    def _note(self, name="Embryo", source_ids=()):
        folder = config.vault_dir("v") / "Organisms"
        folder.mkdir(parents=True, exist_ok=True)
        body = f"{name} hub.\n" + "\n".join(source_ids)
        path = folder / f"{name}.md"
        path.write_text(f"---\nkb_kind: model\n---\n{body}\n", encoding="utf-8")
        return path

    def test_second_node_reader_reuses_fourteen_paper_profiles(self):
        con = db.connect()
        try:
            for index in range(14):
                paper_id = f"paper{index:02d}"
                row = self._row(paper_id, 1)
                self._paper(con, paper_id, [row])
                con.execute("INSERT INTO concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id) "
                            "VALUES(?,?,?,?,?,?)", ("local:embryo", paper_id, "mentioned", 1,
                                                       '["results"]', row["sentence_id"]))
            con.commit()
            self._registry()
            path = self._note()
            with patch.object(node_profile, "_paper", wraps=node_profile._paper) as paper:
                first = neural.node_reader(con, "v", "Organisms/Embryo.md", query="x", include_media=False)
                second = neural.node_reader(con, "v", "Organisms/Embryo.md", query="x", include_media=False)
            self.assertEqual(len(first["profile"]["papers"]), 14)
            self.assertEqual(len(second["profile"]["papers"]), 14)
            self.assertEqual(paper.call_count, 14)
        finally:
            con.close()

    def test_lazy_pagination_has_same_items_as_eager_resolution(self):
        con = db.connect()
        try:
            rows = [self._row("paper", index, f"Embryo source sentence {index}.")
                    for index in range(1, 36)]
            self._paper(con, "paper", rows)
            con.execute("INSERT INTO concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id) "
                        "VALUES(?,?,?,?,?,?)", ("local:embryo", "paper", "mentioned", 35,
                                                   '["results"]', rows[0]["sentence_id"]))
            con.commit()
            self._registry()
            path = self._note(source_ids=[row["sentence_id"] for row in rows])
            eager = neural.resolve_sources(
                con, "v", [row["sentence_id"] for row in rows], limit=80, context_limit=4)
            eager_ids = [row["id"] for row in eager["items"]]
            lazy_ids = []
            offset = 0
            while True:
                page = neural.node_reader(con, "v", str(path.relative_to(config.vault_dir("v"))),
                                          query="x", include_media=False, source_offset=offset,
                                          source_limit=20)
                lazy_ids.extend(row["id"] for row in page["sources"]["items"])
                if not page["sources"]["has_more"]:
                    break
                offset = page["sources"]["next_offset"]
            self.assertEqual(lazy_ids, eager_ids)
        finally:
            con.close()

    def test_server_answers_while_warmup_runs_in_background(self):
        import web

        started = threading.Event()
        release = threading.Event()
        request_done = threading.Event()
        stop_server = threading.Event()

        def slow_warmup():
            started.set()
            release.wait(2)

        class FakeServer:
            def __init__(self, address, handler):
                self.server_address = (address[0], address[1])

            def serve_forever(self):
                request_done.set()
                stop_server.wait(2)

            def server_close(self):
                stop_server.set()

        with patch.object(web, "warm_default_vault", slow_warmup):
            main_thread = None
            try:
                with patch.object(web, "Server", FakeServer), \
                        patch.object(web.sys, "argv", ["web.py", "--no-prepare", "--no-browser"]):
                    main_thread = threading.Thread(target=web.main, daemon=True)
                    main_thread.start()
                    self.assertTrue(started.wait(1))
                    self.assertTrue(request_done.wait(1))
            finally:
                release.set()
                stop_server.set()
                if main_thread is not None:
                    main_thread.join(1)
                self.assertFalse(main_thread.is_alive() if main_thread else True)
