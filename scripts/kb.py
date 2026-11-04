"""Biomedical Knowledge System — command line.

Run with the project's Python:  .venv\\Scripts\\python.exe scripts\\kb.py <command>
See docs/AGENT_TOOLS.md for every command.
"""

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kbsys import (analysis, concept_notes, concepts, config, db, discovery, evidence, evidence_fill, ghosts, ingest, manage,
                   mother_vault, notes, refmanager, relations, search, selftest, source_notes, tier1)  # noqa: E402
from kbsys.notes import section_label  # noqa: E402
from kbsys.answer import AIError  # noqa: E402
from kbsys import answer, conform, gaps, library, neural, overlap, pipeline, standard, structure, vault_layout, workflow  # noqa: E402


def _out_json(data):
    print(json.dumps(data, ensure_ascii=False, indent=2))


def cmd_ingest(args, cfg, con):
    totals = ingest.run(cfg, con, args.vault)
    if totals:
        ghost_totals = ghosts.rebuild(con)
        vault = args.vault or config.default_vault(cfg)
        concept_totals = concept_notes.refresh(con, vault, allow_ai=True) if vault else None
        print("\nXong. Bài mới: {new} · Gộp vào bài đã có: {merged} · Tệp trùng: {duplicate} · Lỗi: {failed}"
              " · Supplementary đã gắn: {supplements_attached} · Chưa ghép được: {supplements_unmatched}"
              .format(**totals))
        print(f"Ghost references: {ghost_totals['ghosts']} mục · {ghost_totals['linked_refs']} liên kết.")
        if concept_totals:
            print("Concept notes:", concept_totals)
        pending = con.execute("SELECT COUNT(*) FROM review_items WHERE status = 'pending' AND kind = 'topic_relevance'").fetchone()[0]
        if pending:
            print(f"Có {pending} mục cần bạn xem trong Obsidian: thư mục 'Review Queue'.")


def _find_paper(con, key):
    for col in ("paper_id", "doi", "pmid"):
        row = con.execute(f"SELECT * FROM papers WHERE {col} = ?", (key.lower() if col == "doi" else key,)).fetchone()
        if row:
            return row
    return None


def cmd_search(args, cfg, con):
    query = " ".join(args.query)
    rows, highlight = search.search(con, cfg, query, vault=args.vault, all_vaults=args.all_vaults,
                                    section=args.section, year_from=args.year_from, year_to=args.year_to,
                                    paper_id=args.paper, include_references=args.refs or None,
                                    limit=args.limit)
    if args.json:
        _out_json({"query": query, "scope": "all_vaults" if args.all_vaults else (args.vault or config.default_vault(cfg)),
                   "results": rows})
        return
    search.print_results(rows, query)
    if args.html:
        search.open_file(search.write_html(rows, query, highlight))


def cmd_overlap(args, cfg, con):
    """Stored sentences that share wording with a pasted passage (lexical overlap, not a plagiarism verdict)."""
    _out_json(overlap.find_overlaps(con, " ".join(args.text), vault=args.vault, limit=args.limit))


def cmd_ask(args, cfg, con):
    """Interactive search for people (used by 'Tim kiem.bat')."""
    print("TÌM KIẾM TRONG KHO KIẾN THỨC")
    print('Gõ từ khóa tiếng Anh, ví dụ: mitochondrial membrane   hoặc   "membrane potential"')
    print("Nhấn Enter khi để trống để thoát.\n")
    while True:
        try:
            query = input("Tìm: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not query:
            break
        rows, highlight = search.search(con, cfg, query, vault=args.vault)
        search.print_results(rows, query)
        if cfg["search"].get("open_results_in_browser", True):
            search.open_file(search.write_html(rows, query, highlight))
            print("(Kết quả đầy đủ đã mở trong trình duyệt.)\n")


def cmd_context(args, cfg, con):
    rows = search.context(con, args.passage_id, args.window)
    if args.json:
        _out_json(rows)
        return
    if not rows:
        print(f"Passage {args.passage_id} not found.")
    for r in rows:
        marker = ">>" if r["passage_id"] == args.passage_id else "  "
        print(f"{marker} [{r['passage_id']}] {section_label(r['section'])}, page {r['page']} ({r['kind']})")
        print(f"   {r['text']}\n")


def cmd_paper(args, cfg, con):
    row = _find_paper(con, args.key)
    if not row:
        print(f"Paper {args.key} not found in the current knowledge base.")
        return
    pid = row["paper_id"]
    meta = db.read_json(db.paper_dir(pid) / "metadata.json")
    meta["sections"] = db.read_json(db.paper_dir(pid) / "sections.json", [])
    meta["analysis"] = db.read_json(db.paper_dir(pid) / "analysis.json")
    meta["pdf_available_locally"] = search.main_pdf(con, pid) is not None
    meta.pop("provenance", None)
    _out_json(meta)


def cmd_text(args, cfg, con):
    """All passages of one paper, with ids — for agents that analyse the paper."""
    row = _find_paper(con, args.key)
    if not row:
        print(f"Paper {args.key} not found.")
        return
    q = "SELECT passage_id, page, section, kind, text FROM passages WHERE paper_id = ?"
    if not args.refs:
        q += " AND section != 'references'"
    for r in con.execute(q + " ORDER BY seq", (row["paper_id"],)):
        print(f"[{r['passage_id']}] ({section_label(r['section'])}, p. {r['page']}{', caption' if r['kind'] == 'caption' else ''})")
        print(r["text"] + "\n")


def cmd_sentences(args, cfg, con):
    """Every sentence of one paper with its id, section, page and resolved citations (v2)."""
    row = _find_paper(con, args.key)
    if not row:
        print(f"Paper {args.key} not found.")
        return
    folder = db.paper_dir(row["paper_id"])
    refs = {r.get("num") or i: r for i, r in enumerate(db.read_jsonl(folder / "references.jsonl"), start=1)}
    rows = list(db.read_jsonl(folder / "sentences.jsonl"))
    if not rows:
        print("No sentences yet: run `kb.py reextract " + row["paper_id"] + "`.")
        return
    for s in rows:
        if (s["section"] == "references" and not args.refs) or (args.section and s["section"] != args.section):
            continue
        if args.json:
            print(json.dumps(s, ensure_ascii=False))
            continue
        short = s["sentence_id"].split(":")[1]
        extra = f", {s['kind']}" if s["kind"] != "text" else ""
        print(f"[{short}] ({section_label(s['section'])}, p. {s['page']}{extra}) {s['text']}")
        for c in s["citations"]:
            names = "; ".join(f"[{n}] {refs[n].get('first_author') or '?'} {refs[n].get('year') or ''}".strip()
                              for n in c["ref_nums"] if n in refs)
            print(f"      cites {c['marker']} -> {names or 'unresolved'}{' (uncertain)' if c['confidence'] == 'low' else ''}")


def cmd_ref(args, cfg, con):
    """Look up a numbered reference of a paper (what "[12]" points to)."""
    row = _find_paper(con, args.key)
    if not row:
        print(f"Paper {args.key} not found.")
        return
    for i, r in enumerate(db.read_jsonl(db.paper_dir(row["paper_id"]) / "references.jsonl"), start=1):
        if (r.get("num") or i) == args.num:
            _out_json(r)
            return
    print(f"Reference {args.num} not found in {row['paper_id']}.")


def cmd_reextract(args, cfg, con):
    """Tier 1 again. One paper: only the concepts it touches are refreshed (as in submit-analysis);
    --all: the whole library is relinked and every concept note refreshed."""
    from kbsys import media
    if args.all:
        ids = [r["paper_id"] for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]
    else:
        ids = [args.paper_id]
    affected, vaults, done = set(), set(), []
    for pid in ids:
        try:
            if args.dry_run:
                _out_json(tier1.reextract_dry_run(con, cfg, pid))
                continue
            info, how = tier1.reextract(con, cfg, pid)
            con.commit()
            print(f"{pid}: {info['sentences']} sentences, {info['references']} references, "
                  f"{info['figures']} figures, {info['tables']} tables ({how})")
            done.append(pid)
            try:  # a stale media manifest makes every concept note re-read the PDF (minutes per paper)
                media.build(pid)
            except Exception as err:
                print(f"{pid}: media manifest not rebuilt ({type(err).__name__}: {err}); run `kb.py steps`")
        except Exception as err:
            con.rollback()
            print(f"{pid}: FAILED {type(err).__name__}: {err}")
        finally:  # relink may have committed before a later step failed: its concepts still need a refresh
            relink = tier1.LAST_RELINK.pop(pid, None) or {}
            affected.update(relink.get("affected_concepts", []))
            vaults.update(relink.get("affected_vaults", []))
            if relink or pid in done:
                vaults.update(r[0] for r in con.execute("SELECT vault FROM paper_vaults WHERE paper_id = ?", (pid,)))
    if args.dry_run or not (done or affected):
        return
    print("Ghost registry:", ghosts.rebuild(con))
    if args.all:
        print("Concept links:", concepts.rebuild_links(con))
        con.commit()
        vaults = {r[0] for r in con.execute("SELECT DISTINCT vault FROM paper_vaults")}
    for vault in sorted(vaults):
        print("Concept notes:", vault, concept_notes.refresh(con, vault, **({} if args.all else {"concept_ids": sorted(affected)})))


def cmd_attach_supplement(args, cfg, con):
    print(tier1.attach_supplement(con, cfg, args.paper_id, args.file))
    con.commit()
    meta = db.read_json(db.paper_dir(args.paper_id) / "metadata.json") or {}
    for vault in meta.get("vaults", []):
        notes.write_paper_note(meta, vault, con=con)
        notes.write_indexes(con, vault)


def cmd_read_paper(args, cfg, con):
    """The paper as an analysing agent reads it (v2): numbered sentences, citations, must_cover."""
    view = evidence.reading_view(con, args.paper_id, args.section, args.start, args.limit)
    if args.compact:
        from kbsys.analysis_transport import compact_view
        view = compact_view(view)
    _out_json(view)


def cmd_submit_analysis(args, cfg, con):
    """Check an agent's v2 analysis (V1-V12) and store what passes."""
    path = Path(args.file) if args.file else db.paper_dir(args.paper_id) / "analysis_v2.draft.json"
    try:
        submission = json.loads(path.read_text(encoding="utf-8-sig"))
    except OSError as error:
        sys.exit(f"Cannot read {path}: {error}")
    except ValueError as error:  # nothing is checked or stored
        sys.exit(f"{path} is not valid JSON ({error}); nothing was stored. Fix the file and submit again.")
    if args.dry_run:  # the same checks in ~30 s, nothing stored: fix everything, then submit once
        try:
            result, accepted, rejected = evidence.validate(con, args.paper_id, submission)
        except (FileNotFoundError, ValueError) as error:
            sys.exit(str(error))
        con.rollback()
        blocking = bool(result.get("errors") or result.get("coverage_missing"))
        _out_json({"dry_run": True, "nothing_stored": True, "would_accept": len(accepted),
                   "would_reject": len(rejected), "blocking": blocking, **result})
        sys.exit(2 if blocking else 0)
    try:
        result = evidence.submit(con, args.paper_id, submission)
    except (FileNotFoundError, ValueError) as error:  # unknown paper, or no sentence ids yet
        sys.exit(str(error))
    con.commit()
    _out_json(result)
    sys.exit(0 if result["status"] != "needs_fix" else 2)


def cmd_reset_analysis(args, cfg, con):
    """Take AI analyses out of use (files kept in analysis_history/removed_<time>/)."""
    if args.all:
        ids = [r["paper_id"] for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]
    elif args.paper_id:
        ids = [args.paper_id]
    else:
        sys.exit("reset-analysis needs a paper_id or --all")
    for pid in ids:
        _out_json(evidence.reset(con, pid, args.reason))
        con.commit()


def cmd_recheck_analysis(args, cfg, con):
    """Run the current checks again on stored analyses (no AI involved)."""
    if args.all:
        # needs_fix too: a recheck under newer rules can leave a stored analysis needs_fix
        ids = [r["paper_id"] for r in con.execute(
            "SELECT paper_id FROM papers WHERE analysis_status IN ('done', 'needs_fix') ORDER BY paper_id")]
    else:
        ids = [args.paper_id]
    for pid in ids:
        try:
            result = evidence.recheck(con, pid)
        except (FileNotFoundError, ValueError) as error:  # e.g. a v1 analysis: nothing to recheck
            print(f"{pid}: skipped ({error})")
            continue
        con.commit()
        print(f"{pid}: {result['status']}, {result['accepted_units']} units, {result['flagged_units']} flagged, "
              f"{len(result['errors'])} errors, {len(result['coverage_missing'])} uncovered, "
              f"{len(result.get('legacy_term_warnings', []))} term warnings")


def cmd_fill_evidence_fields(args, cfg, con):
    """EV-17: fill an empty direction / species of stored result units from the cited source. Dry run unless --apply."""
    report = evidence_fill.papers_report(con, [args.paper] if args.paper else None, apply=args.apply)
    limit = lambda key: standard.param("EV-17", key)
    total = evidence_fill.summary(report, limit)
    for r in report:
        if r["results"]:
            print(f"{r['paper_id']}: {r['results']} result units; empty direction {r['empty']['direction']} "
                  f"(fill {r['filled']['direction']}), empty species {r['empty']['species']} (fill {r['filled']['species']})")
    print(f"TOTAL {total['papers']} papers, {total['results']} result units")
    for field in ("direction", "species"):
        t = total[field]
        print(f"  {field}: empty {t['empty']} ({t['empty_share']:.1%}), rule fills {t['filled']}, "
              f"predicted empty {t['predicted_share']:.1%} (limit {t['limit']:.0%})")
    samples = [x for r in report for x in r["samples"]]
    for x in samples[::max(1, len(samples) // 5)][:5]:  # spread over the papers
        print(f"  sample {x['unit_id']} {x['field']}={x['value']!r} from {x['source']}: {x['text']}")
    print("applied: evidence.jsonl rewritten where filled" if args.apply else "dry run: nothing written (use --apply)")
    if args.apply:
        con.commit()


def cmd_export_analysis(args, cfg, con):
    """Write a stored v2 analysis as a complete submission file and list what needs repair (reads only)."""
    try:
        submission = evidence.export_submission(args.paper_id)
    except (FileNotFoundError, ValueError) as error:
        sys.exit(str(error))
    out = Path(args.out) if args.out else config.ROOT / "runtime" / "ai-repair" / f"{args.paper_id}.submit.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(submission, indent=1, ensure_ascii=False), encoding="utf-8", newline="\n")
    print(f"wrote {out} ({len(submission['units'])} units)")
    print("\n".join(evidence.repair_report(args.paper_id)))


def cmd_export_docx(args, cfg, con):
    """Write one vault note as a Word file with its figure images (reads the vault only)."""
    from kbsys import docx_export
    out, images = docx_export.export(args.vault or config.default_vault(cfg), args.note, args.out)
    print(f"wrote {out} ({out.stat().st_size} bytes, {images} images)")


def cmd_canvas(args, cfg, con):
    """Write the walk of one node as an Obsidian Canvas in <vault>/Canvas/ (never over a canvas without our marker)."""
    from kbsys import canvas
    try:
        out, message = canvas.write(con, args.vault or config.default_vault(cfg), args.node, args.limit)
    except RuntimeError as error:  # the vault index is not ready: no answer, not "not found"
        sys.exit(str(error))
    print(f"wrote {out}" + (f"; {message}" if message else "") if out else message)


def cmd_crosscheck_analysis(args, cfg, con):
    from kbsys import crosscheck
    if args.all:
        ids = [r["paper_id"] for r in con.execute(
            "SELECT paper_id FROM papers WHERE analysis_status = 'done' ORDER BY paper_id")]
    elif args.paper_id:
        ids = [args.paper_id]
    else:
        sys.exit("crosscheck-analysis needs a paper_id or --all")
    result = crosscheck.run(con, ids, batch_size=args.batch_size, timeout=args.timeout)
    _out_json(result)
    if result["errors"]:
        sys.exit(2)


def cmd_analysis_status(args, cfg, con):
    _out_json(evidence.status(args.paper_id))


def cmd_analysis_guide(args, cfg, con):
    print(config.analyst_prompt())


def cmd_mcp(args, cfg, con):
    from kbsys import mcp_server
    mcp_server.TOOLSET = mcp_server.BRAINSTORM_TOOLS if args.tools == 'brainstorm' else None
    mcp_server.MEMORY = bool(args.memory)   # AN-20: only a client that may keep the researcher's memory gets it
    mcp_server.serve()


def cmd_tool(args, cfg, con):
    """CLI bridge for Claude Code: one MCP tools/call through mcp_server.handle (same argument checks, writer lock and
    errors). The full result goes to --out; the screen gets the result when short, else a preview."""
    from kbsys import discussions, mcp_server
    mcp_server.CLIENT['name'], mcp_server.MEMORY = 'claude-code', True
    mcp_server.TOOLSET = None if args.all else mcp_server.BRAINSTORM_TOOLS
    raw = sys.stdin.buffer.read().decode('utf-8-sig') if args.args == '-' else Path(args.args).read_text(encoding='utf-8-sig') if args.args else ''
    try:
        arguments = json.loads(raw) if raw.strip() else {}
    except ValueError as error:
        sys.exit(f'Error: --args is not valid JSON ({error})')
    reply = mcp_server.handle(con, cfg, {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                                         'params': {'name': args.name, 'arguments': arguments}})
    if 'error' in reply:
        print(reply['error']['message'] + ('' if args.all else ' (this bridge serves the brainstorm tools; --all adds the rest)'))
        sys.exit(1)
    text = reply['result']['content'][0]['text']
    if reply['result']['isError']:
        print(text)
        sys.exit(1)
    out = Path(args.out) if args.out else discussions.MEMORY_DIR / f'last_{args.name}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open('w', encoding='utf-8', newline='\n') as handle:
        handle.write(text)
    if len(text) <= 4000:
        print(text)
        return
    try:
        value = json.loads(text)
    except ValueError:
        value = None
    print(json.dumps({'full': str(out), 'chars': len(text), 'keys': list(value) if isinstance(value, dict) else [],
                      'preview': text[:2500]}, ensure_ascii=False))


def cmd_analyze(args, cfg, con):
    from kbsys import analysis_runner
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("analyze needs --vault")
    _out_json(analysis_runner.run(con, vault, limit=args.limit))


def cmd_analysis_usage(args, cfg, con):
    from kbsys import analysis_runner
    _out_json(analysis_runner.usage_report(args.paper_id))


def cmd_quality_check(args, cfg, con):
    from kbsys import quality
    ids = [args.paper_id] if args.paper_id else [r[0] for r in con.execute('SELECT paper_id FROM papers ORDER BY paper_id')]
    failed = 0
    for pid in ids:
        result = quality.inspect(pid)
        db.write_json(db.paper_dir(pid) / 'analysis_quality.json', result)
        _out_json(result)
        failed += not result['ready_for_analysis']
    if failed:
        raise SystemExit(2)


def cmd_package(args, cfg, con):
    from kbsys import package
    _out_json(package.build(args.out, with_models=not args.no_models, with_library=args.with_library))


def cmd_discussion(args, cfg, con):
    from kbsys import agent_tasks, discussions
    vault = args.vault or config.default_vault(cfg)
    if args.action == "rules":
        print(discussions.protocol())
    elif args.action == "save":
        _out_json(discussions.save(vault, args.title, Path(args.file).read_text(encoding="utf-8"), kind=args.kind, con=con,
                                         researcher_words=args.words, citations=args.cite, earlier=args.earlier))
    else:  # pack: rules + evidence items to paste into an AI without MCP (ChatGPT web, ...)
        out = agent_tasks.answer_prompt(con, cfg, args.question, args.query or [args.question], "evidence4", vault)
        items = "\n\n".join((p.get("user") or "").split("Answer format (JSON):")[0].rstrip() for p in out["prompts"])
        print(out["answer_protocol"] + "\n\n---\n\n" + (items or "Not found in the current library."))


def cmd_release(args, cfg, con):
    from kbsys import package
    result = package.release(args.dest)
    _out_json({**result, "leaks": result["leaks"][:200], "leak_count": len(result["leaks"])})
    if result["leaks"]:
        sys.exit(2)


def cmd_ai_check(args, cfg, con):
    _out_json(answer.ai_check(args.call))


def cmd_ai_bench(args, cfg, con):
    from kbsys import bench
    if not args.ai:
        print("ai-bench gọi AI khoảng 4-8 lần trên 2 bài mẫu tự tạo (không dùng bài của bạn). Thêm --ai để chạy thật.")
        return
    result = bench.run(args.provider, args.model)
    if args.out:
        Path(args.out).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    _out_json(result)
    print(bench.summary_vi(result), file=sys.stderr)


def cmd_init(args, cfg, con):
    """First run, safe to repeat: folders, database, optional vault. Nothing existing is overwritten."""
    created, existing = [], []
    for path in (config.STORE, config.PAPERS_DIR, config.LOGS_DIR, config.CACHE_DIR, config.VAULTS_DIR, config.REVIEW_PENDING,
                 config.ROOT / "sources" / "inbox"):
        (existing if (args.store_existed if path == config.STORE else path.is_dir()) else created).append(config.rel(path))
        path.mkdir(parents=True, exist_ok=True)
    (existing if args.db_existed else created).append(config.rel(config.DB_PATH))   # db.connect() made it before this command ran
    vault = None
    if args.vault:
        name = library.folder_name(args.vault)
        if name in library.list_vaults():
            vault = {"vault": name, "status": "existing"}
        else:
            vault = {**library.create_vault(con, args.vault, args.topic or ""), "status": "created"}
    _out_json({"created": created, "existing": existing, "vault": vault, "ai": answer.ai_check(),
               "next": ['Mở "1 - Mo trang chinh.bat" (trang tìm kiếm).', 'Nối ứng dụng AI bằng "2 - Ket noi AI (MCP).bat".']})


def cmd_mcp_config(args, cfg, con):
    from kbsys import mcp_setup
    if args.install:
        try:
            _out_json(mcp_setup.install(args.install, args.tools))
        except ValueError as e:
            sys.exit(f"Chưa cài được: {e}")
    else:
        print(mcp_setup.report_text(args.tools))


def cmd_enrich_interventions(args, cfg, con):
    result = relations.enrich(con, args.paper_id, args.dry_run)
    _out_json(result)


def cmd_propose_relations(args, cfg, con):
    result = relations.propose(con, args.vault)
    if args.vault:
        concept_notes.refresh(con, args.vault)
        result["concept_notes_refreshed"] = True
    _out_json(result)


def cmd_standard(args, cfg, con):
    try:
        data = standard.load()
    except ValueError as err:
        sys.exit(f"Error: config/standard.yaml\n{err}")
    if args.write_docs:
        standard.write_docs(data)
    problems = standard.unresolved(data) + [f"{name} is out of sync" for name in standard.out_of_sync(data)]
    _out_json({"version": data["version"], "status": data["status"], "stages": standard.counts(data),
               "open_decisions": len(data.get("open_decisions") or []), "problems": problems})
    sys.exit(1 if problems else 0)


def cmd_backfill_relation_vault(args, cfg, con):
    _out_json(relations.backfill_vault(args.dry_run))


def cmd_gaps(args, cfg, con):
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("Error: choose a vault with --vault")
    _out_json(gaps.generate(vault, args.dry_run, cfg))


def cmd_selftest(args, cfg, con):
    report, n_papers = selftest.run(con, args.paper, unit=not args.no_unit, strict_conform=args.strict_conform)
    for w in report.warnings[:args.show]:
        print(f"warning  {w}")
    if len(report.warnings) > args.show:
        print(f"warning  ... {len(report.warnings) - args.show} more warnings")
    for e in report.errors[:args.show]:
        print(f"ERROR    {e}")
    if len(report.errors) > args.show:
        print(f"ERROR    ... {len(report.errors) - args.show} more errors")
    print(f"\n{report.checked} checks on {n_papers} papers: {len(report.errors)} errors, {len(report.warnings)} warnings")
    sys.exit(1 if report.errors else 0)


def cmd_conform(args, cfg, con):
    result = conform.run(con, args.stage, args.paper, args.vault)
    if args.out:
        conform.write_json(result, args.out)
    if args.json:
        _out_json(result)
    else:
        print(conform.render_table(result))
    sys.exit(1 if result["summary"].get("FAIL") or result["summary"].get("ERROR") else 0)


def cmd_list(args, cfg, con):
    q = "SELECT p.paper_id, p.first_author, p.year, p.title, p.classification, p.analysis_status, p.text_quality, p.metadata_confidence FROM papers p"
    filters = []
    values = []
    if args.vault:
        q += " JOIN paper_vaults v ON v.paper_id = p.paper_id"
        filters.append("v.vault = ?")
        values.append(args.vault)
    if args.pending:
        filters.append("p.analysis_status != 'done'")
    if filters:
        q += " WHERE " + " AND ".join(filters)
    rows = [dict(r) for r in con.execute(q + " ORDER BY p.ingested_at, p.paper_id", values)]
    if args.json:
        _out_json(rows)
        return
    for r in rows:
        print(f"{r['paper_id']:<55} {r['classification']:<16} analysed={r['analysis_status']:<8} "
              f"{(r['first_author'] or '?')} {r['year'] or ''} — {(r['title'] or '')[:70]}")
    print(f"\n{len(rows)} papers")


def cmd_import_analysis(args, cfg, con):
    ok, messages = analysis.import_analysis(con, args.paper_id)
    print(("OK" if ok else "REJECTED — nothing was changed. Fix the draft and try again:"))
    for m in messages:
        print(f"  - {m}")
    sys.exit(0 if ok else 1)


def cmd_notes(args, cfg, con):
    vault = args.vault or config.default_vault(cfg)
    if not vault:  # otherwise the note folders would be written straight into vaults/
        sys.exit("notes needs --vault when no vault exists or is selected")
    pipeline.rebuild_vault_notes(con, vault)  # shared with `kb.py steps notes` and the paper delete


def _passthrough(module):
    """`kb.py <name> ...` forwards the remaining words to `python -m kbsys.<module> ...`."""
    def run(args, cfg, con):
        target_module = __import__(f"kbsys.{module}", fromlist=["main"])
        target = getattr(target_module, "main", None)
        if target is not None:
            result = target(args.rest)
        else:
            # sources_misc predates the kb.py passthrough and exposes a CLI
            # named _cli which parses sys.argv. Reuse that parser unchanged.
            target = getattr(target_module, "_cli", None)
            if target is None:
                raise AttributeError(f"kbsys.{module} has no CLI entry point")
            saved_argv = sys.argv
            sys.argv = [f"python -m kbsys.{module}", *args.rest]
            try:
                result = target()
            finally:
                sys.argv = saved_argv
        if isinstance(result, int) and result:
            sys.exit(result)
    return run


def cmd_mother_vault(args, cfg, con):
    _out_json(mother_vault.refresh(con))


def cmd_graph_colours(args, cfg, con):
    _out_json(mother_vault.graph_colours(args.vault or conform.vault_names(con), mother=not args.vault))


def cmd_vault_migrate(args, cfg, con):
    if args.undo:
        _out_json(vault_layout.undo(args.undo))
        return
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("vault-migrate needs --vault when no vault is selected")
    _out_json(vault_layout.migrate(vault, dry_run=args.dry_run, con=con))


def cmd_link_concepts(args, cfg, con):
    result = concepts.rebuild_links(con, args.vault)
    _out_json(result)
    vaults = [args.vault] if args.vault else sorted(r[0] for r in con.execute("SELECT DISTINCT vault FROM paper_vaults"))
    for vault in vaults:
        _out_json({"vault": vault, "concept_notes": concept_notes.refresh(con, vault)})
        pipeline.prewarm_neural(vault)


def cmd_concept_notes(args, cfg, con):
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("concept-notes needs --vault when no vault is selected")
    _out_json(concept_notes.refresh(con, vault, args.concept_id, allow_ai=not args.revalidate,
                                    revalidate=args.revalidate))


def cmd_image_audit(args, cfg, con):
    from kbsys import image_audit
    names = conform.vault_names(con, args.vault)
    if args.vault and not config.vault_dir(args.vault).is_dir():
        raise ValueError(f"Unknown vault: {args.vault!r}")
    vaults = [conform.Vault(n, config.vault_dir(n), con) for n in names]
    print(image_audit.summary(image_audit.run(vaults, config.STORE / "papers", args.out)))


def cmd_retire_machine_files(args, cfg, con):
    if not config.vault_dir(args.vault).is_dir():
        raise ValueError(f"Unknown vault: {args.vault!r}")
    _out_json(vault_layout.retire_machine_files(args.vault, con, args.apply))


def cmd_tidy_vault(args, cfg, con):
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("tidy-vault needs --vault when no vault is selected")
    _out_json(notes.tidy_vault(con, vault, args.dry_run))


def cmd_node_decision(args, cfg, con):
    from kbsys import node_decisions
    rest, action = args.rest, args.action
    if len(rest) not in {"submit": (1,), "remove": (2,), "check": (0,), "preview": (0, 1)}[action]:
        sys.exit("usage: node-decision submit FILE | remove KEY KIND | check | preview [FILE] [--out PATH]")
    if action == "preview":  # read-only: no store write, no writer lock
        from kbsys import node_preview
        counts, out, failed = node_preview.run(con, rest[0] if rest else None, args.out or config.ROOT / "runtime/g4-4/node_preview.md")
        print(f"{counts}\nreport: {out}")
        if failed:
            sys.exit(1)
        return
    if action == "remove":
        print(json.dumps(node_decisions.remove(*rest), ensure_ascii=False))
        return
    errors, added = node_decisions.submit(rest[0], con, concepts.build_registry(con)) if action == "submit" \
        else (node_decisions.check(con, concepts.build_registry(con)), None)
    for index, key, why in errors:
        print(f"row {index} [{key}]: {why}")
    if errors:
        sys.exit(1)
    print(f"{len(node_decisions.rows())} rows OK" if action == "check" else
          "added " + ", ".join(f"{k}={sum(r['kind'] == k for r in added)}" for k in node_decisions.KINDS))


def cmd_rebuild(args, cfg, con):
    print(f"Rebuilt the database from {db.rebuild(con)} paper folders.")
    _out_json(ghosts.rebuild(con))


def cmd_store_lf(args, cfg, con):
    _out_json(db.store_lf(args.apply))


def cmd_rebuild_ghosts(args, cfg, con):
    _out_json(ghosts.rebuild(con))


def cmd_discover_citations(args, cfg, con):
    vault = args.vault or config.default_vault(cfg)
    if not vault:
        sys.exit("discover-citations needs --vault when no vault is selected")
    _out_json(discovery.discover(con, args.paper_id, vault, args.limit))


def cmd_stage_discovery(args, cfg, con):
    _out_json(discovery.stage(args.vault, args.candidate_id, con))


def cmd_import_refs(args, cfg, con):
    report = refmanager.import_refs(args.file, con, args.vault, args.dry_run)
    if args.dry_run:
        _out_json(report)
        return
    print(f"Đã đọc {report['records']} mục: {len(report['matched'])} bài đã có, "
          f"{len(report['candidates'])} ứng viên mới, {len(report['malformed'])} mục lỗi, "
          f"{len(report['attachments'])} tệp đính kèm có sẵn (không thay đổi).")

def cmd_status(args, cfg, con):
    one = lambda q: con.execute(q).fetchone()[0]
    data = {
        "papers": one("SELECT COUNT(*) FROM papers"),
        "passages": one("SELECT COUNT(*) FROM passages"),
        "analysed": one("SELECT COUNT(*) FROM papers WHERE analysis_status = 'done'"),
        "claims": one("SELECT COUNT(*) FROM claims"),
        "evidence_units": one("SELECT COUNT(*) FROM evidence"),
        "analysis_needs_fix": one("SELECT COUNT(*) FROM papers WHERE analysis_status = 'needs_fix'"),
        "pending_review_items": one("SELECT COUNT(*) FROM review_items WHERE status = 'pending' AND kind = 'topic_relevance'"),
        "pdfs_waiting_in_inbox": len(ingest.find_inbox_pdfs(cfg)),
        "by_classification": dict(con.execute("SELECT classification, COUNT(*) FROM papers GROUP BY 1").fetchall()),
        "vaults": dict(con.execute("SELECT vault, COUNT(*) FROM paper_vaults GROUP BY 1").fetchall()),
    }
    _out_json(data)


def cmd_refresh_review_policy(args, cfg, con):
    from kbsys import review_policy
    vault = args.vault or config.default_vault(cfg)
    if not vault or not config.vault_dir(vault).is_dir():
        raise ValueError("Select an existing vault")
    _out_json(review_policy.refresh(con, vault, apply=args.apply))


def cmd_review_list(args, cfg, con):
    _out_json(manage.pending_reviews(con))


def cmd_review_decide(args, cfg, con):
    _out_json(manage.decide_classification(con, args.item_id, args.decision))


def cmd_archive_source(args, cfg, con):
    print(manage.move_source(con, args.paper_id, args.destination))


def cmd_restore_source(args, cfg, con):
    print(manage.move_source(con, args.paper_id, restore=True))


def cmd_demo3(args, cfg, con):
    vault = getattr(args, 'vault', None) or config.default_vault(cfg)
    if args.command == 'steps':
        _out_json(workflow.status() if args.status else pipeline.run(con, cfg, vault, args.step, limit=args.limit,
                                                                   structured_ai=args.structured_ai, structured_max_chunks=args.max_chunks))
    elif args.command == 'run-all':
        report = pipeline.run_all(con, cfg, vault, ai=args.ai, until=args.until)
        _out_json(report)
        if report.get('failed'):
            sys.exit(1)
    elif args.command == 'build-media':
        _out_json(pipeline.build_media(args.paper_id, args.regions))
    elif args.command == 'media-status':
        _out_json(pipeline.build_media(args.paper_id, persist=False))
    elif args.command == 'structured-prepare':
        _out_json(structure.prepare_structured(args.paper_id, args.kind, args.chunk))
    elif args.command == 'structured-status':
        _out_json(structure.structured_status(args.paper_id))
    elif args.command == 'structured-submit':
        reply = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        _out_json(structure.submit_structured(con, args.paper_id, args.task_id, args.chunk_id, reply, args.analyst))
    elif args.command == 'structured-run':
        from kbsys import analysis_runner
        _out_json(analysis_runner.run_structured(con, args.paper_id, args.kind, args.ai, args.max_chunks,
                                                args.provider, args.model, args.reasoning))
    elif args.command == 'structure':
        _out_json(structure.build(args.paper_id, persist=not args.read_only))
    elif args.command == 'section-profile':
        _out_json(structure.prepare(args.paper_id))
    elif args.command == 'protocol-profile':
        _out_json(structure.prepare_protocol(args.paper_id))
    elif args.command == 'submit-protocol-profile':
        reply = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        _out_json(structure.submit_protocol(con, args.paper_id, reply, args.analyst))
    elif args.command == 'submit-section-profile':
        reply = json.loads(Path(args.file).read_text(encoding='utf-8-sig'))
        _out_json(structure.submit_profile(con, args.paper_id, reply, args.analyst))
    elif args.command == 'neural-search':
        _out_json(neural.search(vault, args.query, args.hops))
    elif args.command == 'neural-prepare':
        _out_json(neural.prepare(con, cfg, vault, args.question, args.hops, offset=args.offset))
    elif args.command == 'neural-check':
        _out_json(neural.check(con, args.ticket, json.loads(Path(args.file).read_text(encoding='utf-8-sig'))))
    elif args.command == 'compare-protocols':
        _out_json(structure.compare_methods(con, vault, args.term))


def main():
    for stream in (sys.stdout, sys.stderr):  # Vietnamese messages on any console
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="kb", description="Biomedical Knowledge System")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser('steps', help='demo3: run/restart one durable stage')
    p.add_argument('step', nargs='?', choices=pipeline.STEPS, default='quality')
    p.add_argument('--vault')
    p.add_argument('--limit', type=int)
    p.add_argument('--status', action='store_true')
    p.add_argument('--structured-ai', action='store_true', help='explicitly run bounded structured input AI before evidence')
    p.add_argument('--max-chunks', type=int, default=1)
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('run-all', help='G6: snapshot (runtime/research-workspace, one copy per run), then every stage in order (ingest takes sources/inbox; analyze only with --ai); reports Mine/ changes')
    p.add_argument('--vault')
    p.add_argument('--ai', action='store_true', help='also run the analyze stage (calls the configured AI provider)')
    p.add_argument('--until', choices=pipeline.STEPS, help='stop after this stage')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('build-media', help='build one paper source-region inventory without source re-extraction')
    p.add_argument('paper_id')
    p.add_argument('--regions', help='manual registered-source regions JSON path')
    p.set_defaults(func=cmd_demo3)
    for name in ('media-status', 'structured-status'):
        p = sub.add_parser(name, help='read one paper inventory/status without AI')
        p.add_argument('paper_id')
        p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('structured-prepare', help='prepare bounded per-paper overview, WH, or Methods task')
    p.add_argument('paper_id')
    p.add_argument('--kind', choices=('overview', 'wh', 'protocol'), default='wh')
    p.add_argument('--chunk')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('structured-submit', help='validate and checkpoint one source-backed chunk')
    for name in ('paper_id', 'task_id', 'chunk_id', 'file'):
        p.add_argument(name)
    p.add_argument('--analyst', default='codex')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('structured-run', help='optional explicit per-paper structured AI; no answer fallback')
    p.add_argument('paper_id')
    p.add_argument('--kind', choices=('overview', 'wh', 'protocol', 'both', 'all'), default='both')
    p.add_argument('--ai', action='store_true')
    p.add_argument('--max-chunks', type=int, default=1)
    p.add_argument('--provider', help='explicit analysis provider override; does not change config')
    p.add_argument('--model')
    p.add_argument('--reasoning')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('structure', help='demo3: full section/abbreviation/protocol inventory')
    p.add_argument('paper_id')
    p.add_argument('--read-only', action='store_true')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('section-profile', help='source context for five Introduction WH answers')
    p.add_argument('paper_id')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('submit-section-profile', help='validate and save source-grounded WH answers')
    p.add_argument('paper_id')
    p.add_argument('file')
    p.add_argument('--analyst', default='codex')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('protocol-profile', help='source context for complete protocol extraction')
    p.add_argument('paper_id')
    p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('submit-protocol-profile', help='validate protocol details and preserve source order')
    p.add_argument('paper_id')
    p.add_argument('file')
    p.add_argument('--analyst', default='codex')
    p.set_defaults(func=cmd_demo3)
    for command in ('neural-search', 'neural-prepare', 'compare-protocols'):
        p = sub.add_parser(command)
        p.add_argument('question' if command == 'neural-prepare' else 'query' if command == 'neural-search' else 'term')
        p.add_argument('--vault')
        p.add_argument('--hops', type=int, default=0)
        if command == 'neural-prepare':
            p.add_argument('--offset', type=int, default=0, help='next_offset of the previous page')
        p.set_defaults(func=cmd_demo3)
    p = sub.add_parser('neural-check', help='check against saved retrieval, refusing changed sources')
    p.add_argument('ticket')
    p.add_argument('file')
    p.set_defaults(func=cmd_demo3)

    p = sub.add_parser("ingest", help="process new PDFs in sources/inbox/")
    p.add_argument("--vault")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("search", help="full-text search with citations")
    p.add_argument("query", nargs="+")
    p.add_argument("--vault")
    p.add_argument("--all-vaults", action="store_true")
    p.add_argument("--section", help="e.g. results, methods, discussion, abstract")
    p.add_argument("--from", dest="year_from", type=int)
    p.add_argument("--to", dest="year_to", type=int)
    p.add_argument("--paper", help="limit to one paper_id")
    p.add_argument("--refs", action="store_true", help="include reference lists")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--html", action="store_true", help="also open results in the browser")
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("overlap", help="find stored sentences sharing wording with a pasted passage")
    p.add_argument("text", nargs="+")
    p.add_argument("--vault")
    p.add_argument("--limit", type=int, default=20)
    p.set_defaults(func=cmd_overlap)

    p = sub.add_parser("ask", help="interactive search for people")
    p.add_argument("--vault")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("context", help="a passage with its neighbours")
    p.add_argument("passage_id")
    p.add_argument("--window", type=int, default=2)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("paper", help="metadata, sections and analysis of one paper")
    p.add_argument("key", help="paper_id, DOI or PMID")
    p.set_defaults(func=cmd_paper)

    p = sub.add_parser("text", help="all passages of one paper, with ids")
    p.add_argument("key", help="paper_id, DOI or PMID")
    p.add_argument("--refs", action="store_true")
    p.set_defaults(func=cmd_text)

    p = sub.add_parser("sentences", help="every sentence of one paper with ids and citations (v2)")
    p.add_argument("key", help="paper_id, DOI or PMID")
    p.add_argument("--section")
    p.add_argument("--refs", action="store_true")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_sentences)

    p = sub.add_parser("ref", help="the reference a citation number points to")
    p.add_argument("key", help="paper_id, DOI or PMID")
    p.add_argument("num", type=int)
    p.set_defaults(func=cmd_ref)

    p = sub.add_parser("reextract", help="rebuild sentences/references/figures/tables (no AI)")
    p.add_argument("paper_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--dry-run", action="store_true", help="preview old-to-new sentence mapping without writes")
    p.set_defaults(func=cmd_reextract)

    p = sub.add_parser("attach-supplement", help="copy a supplementary PDF/CSV to a paper and index it")
    p.add_argument("paper_id")
    p.add_argument("file")
    p.set_defaults(func=cmd_attach_supplement)

    p = sub.add_parser("read-paper", help="a paper as numbered sentences for analysis (v2, JSON)")
    p.add_argument("--compact", action="store_true", help="lossless compact reading for lower prompt overhead")
    p.add_argument("paper_id")
    p.add_argument("--section")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--limit", type=int, default=100000)
    p.set_defaults(func=cmd_read_paper)

    p = sub.add_parser("submit-analysis", help="check and store a v2 analysis (evidence units)")
    p.add_argument("paper_id")
    p.add_argument("file", nargs="?", help="JSON file (default: analysis_v2.draft.json in the paper folder)")
    p.add_argument("--dry-run", action="store_true", help="run every check (~30 s) and store nothing")
    p.set_defaults(func=cmd_submit_analysis)

    p = sub.add_parser("reset-analysis", help="remove a paper's AI analysis (kept in analysis_history)")
    p.add_argument("paper_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--reason", default="reset by the researcher")
    p.set_defaults(func=cmd_reset_analysis)

    p = sub.add_parser("recheck-analysis", help="run the current checks again on stored analyses (no AI)")
    p.add_argument("paper_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_recheck_analysis)

    p = sub.add_parser("fill-evidence-fields", help="EV-17: fill empty direction/species of stored result units by rule (dry run unless --apply)")
    p.add_argument("--paper", help="one paper id (default: every analysed paper)")
    p.add_argument("--apply", action="store_true", help="write evidence.jsonl (default: dry run, writes nothing)")
    p.set_defaults(func=cmd_fill_evidence_fields)

    p = sub.add_parser("export-analysis", help="write a stored analysis as a complete submission file + repair report")
    p.add_argument("paper_id")
    p.add_argument("--out", help="default: runtime/ai-repair/<id>.submit.json")
    p.set_defaults(func=cmd_export_analysis)

    p = sub.add_parser("export-docx", help="write one vault note as a Word file with its figure images (default runtime/exports/<note>.docx)")
    p.add_argument("note", help="path inside the vault (Concepts/X.md) or a note name")
    p.add_argument("--vault", help="default: the selected vault")
    p.add_argument("--out", help="default: runtime/exports/<note name>.docx; an existing file is never overwritten (_1, _2)")
    p.set_defaults(func=cmd_export_docx)

    p = sub.add_parser("canvas", help="write one node, its typed neighbours and their links as an Obsidian Canvas (<vault>/Canvas/<node>.canvas)")
    p.add_argument("node", help="node note name or path inside the vault")
    p.add_argument("--vault", help="default: the selected vault")
    p.add_argument("--limit", type=int, default=20, help="neighbour steps, 1-100 (default 20)")
    p.set_defaults(func=cmd_canvas)

    p = sub.add_parser("crosscheck-analysis", help="independent V13 statement/quote review (Codex CLI)")
    p.add_argument("paper_id", nargs="?")
    p.add_argument("--all", action="store_true")
    p.add_argument("--batch-size", type=int, default=10)
    p.add_argument("--timeout", type=int, default=180)
    p.set_defaults(func=cmd_crosscheck_analysis)

    p = sub.add_parser("analysis-status", help="status of a paper's v2 analysis")
    p.add_argument("paper_id")
    p.set_defaults(func=cmd_analysis_status)

    sub.add_parser("analysis-guide", help="print the rules for analysing a paper (v2)").set_defaults(
        func=cmd_analysis_guide)
    p = sub.add_parser("analysis-usage", help="local recorded AI token usage of a paper, or of node cards when no "
                                              "paper_id is given (no AI calls)")
    p.add_argument("paper_id", nargs="?")
    p.set_defaults(func=cmd_analysis_usage)
    p = sub.add_parser("quality-check", help="offline font/encoding/OCR preflight before AI analysis")
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("paper_id", nargs="?")
    group.add_argument("--all", action="store_true")
    p.set_defaults(func=cmd_quality_check)
    p = sub.add_parser("analyze", help="Tier-2 analysis of pending papers with the configured AI (ai.tasks.analysis)")
    p.add_argument("--vault")
    p.add_argument("--limit", type=int)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser("package", help="zip the software to share (no API keys; your library only with --with-library)")
    p.add_argument("--out", help="zip file path (default: next to the project folder)")
    p.add_argument("--no-models", action="store_true", help="leave out the MeSH dictionary and embedding model")
    p.add_argument("--with-library", action="store_true", help="also include your papers, notes and vaults")
    p.set_defaults(func=cmd_package)

    p = sub.add_parser("discussion", help="answer protocol, save a discussion note, or a context pack for an AI without MCP")
    p.add_argument("action", choices=["rules", "save", "pack"])
    p.add_argument("--vault")
    p.add_argument("--title", help="save: note title")
    p.add_argument("--file", help="save: markdown file with the discussion")
    p.add_argument("--kind", choices=["discussion", "knowledge_gap", "decision"], default="discussion",
                   help="save: knowledge_gap = evidence + labelled AI knowledge + the researcher's judgment; decision = the researcher's 'chốt'")
    p.add_argument("--words", help="save decision: the researcher's own words, verbatim (must hold 'chốt')")
    p.add_argument("--cite", action="append", help="save decision: an item (sentence) id; repeat")
    p.add_argument("--earlier", help="save decision: an earlier decision note in Discussions/")
    p.add_argument("--question", help="pack: the question")
    p.add_argument("--query", action="append", help="pack: English search query (repeat; default = the question)")
    p.set_defaults(func=cmd_discussion)

    p = sub.add_parser("release", help="copy the shareable software into a separate git folder and scan it for private traces")
    p.add_argument("dest", help="release folder outside the project, e.g. ../my_release")
    p.set_defaults(func=cmd_release)

    p = sub.add_parser("ai-check", help="read-only: provider and readiness of every AI task (key only present/missing); --call sends one tiny prompt to each ready provider")
    p.add_argument("--call", action="store_true", help="really call each ready provider once (nothing is sent without it)")
    p.set_defaults(func=cmd_ai_check)

    p = sub.add_parser("ai-bench", help="score an AI on a fixed synthetic task set (OD-13); calls the AI only with --ai; the real store is never opened")
    p.add_argument("--provider", help="provider to score (default: the ai.tasks.analysis route)")
    p.add_argument("--model", help="model name for that provider")
    p.add_argument("--out", help="also write the JSON result to this file")
    p.add_argument("--ai", action="store_true", help="really call the provider (nothing is sent without it)")
    p.set_defaults(func=cmd_ai_bench)

    p = sub.add_parser("init", help="first run, safe to repeat: store folders, database, optional first vault; prints the AI check and next steps")
    p.add_argument("--vault", help="also create this vault if it does not exist")
    p.add_argument("--topic", help="text for the new vault's Home note")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("mcp-config", help="print MCP connection settings for AI apps (Claude, Codex, Gemini, Cursor)")
    p.add_argument("--install", choices=["claude-desktop", "codex", "gemini-cli", "cursor"],
                   help="merge into that app's config file (a backup is saved first)")
    p.add_argument("--tools", choices=["all", "brainstorm"], default="all", help="brainstorm = only the brainstorm toolset (printed snippets and --install)")
    p.set_defaults(func=cmd_mcp_config)

    p = sub.add_parser("mcp", help="run the MCP server on stdio (started by the connected AI app)")
    p.add_argument("--tools", choices=["all", "brainstorm"], default="all", help="brainstorm = only the brainstorm toolset")
    p.add_argument("--memory", action="store_true", help="serve and write the AN-20 brainstorm memory (not for retrieval-only clients)")
    p.set_defaults(func=cmd_mcp)

    p = sub.add_parser("tool", help="call one MCP tool from the shell (bridge for Claude Code); result also saved to --out")
    p.add_argument("name", help="tool name, e.g. neural_prepare")
    p.add_argument("--args", help="JSON file with the tool arguments, or - for stdin")
    p.add_argument("--out", help="where the full result is saved (default runtime/brainstorm/last_<name>.json)")
    p.add_argument("--all", action="store_true", help="every tool, not only the brainstorm toolset")
    p.set_defaults(func=cmd_tool)

    p = sub.add_parser("selftest", help="check the v2 data contracts")
    p.add_argument("--paper", help="only this paper_id")
    p.add_argument("--no-unit", action="store_true", help="skip the parser examples")
    p.add_argument("--show", type=int, default=40, help="how many problems to print")
    p.add_argument("--strict-conform", action="store_true", help="count failing standard clauses as errors, not warnings")
    p.set_defaults(func=cmd_selftest)

    p = sub.add_parser("conform", help="run the Analysis Standard against the data (read-only): PASS/FAIL/WARN per clause")
    p.add_argument("--stage", help="only this stage, e.g. EV")
    p.add_argument("--paper", help="only this paper_id (paper-level clauses)")
    p.add_argument("--vault", help="only this vault")
    p.add_argument("--json", action="store_true", help="print the full result as JSON")
    p.add_argument("--out", help="also write the full JSON result to this file")
    p.set_defaults(func=cmd_conform)

    p = sub.add_parser("list", help="list papers")
    p.add_argument("--pending", action="store_true", help="only papers not yet analysed")
    p.add_argument("--vault", help="only papers linked to this vault")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("import-analysis", help="validate and import analysis.draft.json")
    p.add_argument("paper_id")
    p.set_defaults(func=cmd_import_analysis)

    p = sub.add_parser("notes", help="refresh Obsidian notes and index notes")
    p.add_argument("--vault")
    p.set_defaults(func=cmd_notes)

    p = sub.add_parser("vault-migrate", help="move a vault to layout v2 (snapshot, map, link check; --undo MAP)")
    p.add_argument("--vault")
    p.add_argument("--dry-run", action="store_true", help="report only; nothing is moved")
    p.add_argument("--undo", help="knowledge_store/migrations/<stamp>_<vault>.json or knowledge_store/removed/<kind>_<stamp>/<vault>.undo.json to restore")
    p.set_defaults(func=cmd_vault_migrate)

    p = sub.add_parser("link-concepts", help="rebuild the code-derived concept registry and paper links")
    p.add_argument("--vault", help="only rebuild links for papers in this vault")
    p.set_defaults(func=cmd_link_concepts)

    p = sub.add_parser("concept-notes", help="refresh evidence-grounded four-part Concept Notes")
    p.add_argument("--vault")
    p.add_argument("--concept-id")
    p.add_argument("--revalidate", action="store_true",
                   help="no AI: re-check stale syntheses against current evidence; passing ones become done")
    p.set_defaults(func=cmd_concept_notes)

    p = sub.add_parser("enrich-interventions", help="fill missing intervention/model fields from cited sentences")
    p.add_argument("paper_id", nargs="?")
    p.add_argument("--all", action="store_true", help="process every paper (default when paper_id is omitted)")
    p.add_argument("--dry-run", action="store_true", help="count eligible units without calling the provider")
    p.set_defaults(func=cmd_enrich_interventions)

    p = sub.add_parser("propose-relations", help="write deterministic cross-paper relation candidates")
    p.add_argument("--vault")
    p.set_defaults(func=cmd_propose_relations)

    p = sub.add_parser("standard", help="check config/standard.yaml; --write-docs regenerates docs/STANDARD.md and STANDARD_vi.md")
    p.add_argument("--write-docs", action="store_true")
    p.set_defaults(func=cmd_standard)

    p = sub.add_parser("backfill-relation-vault", help="fill `vault` in legacy relation rows from their papers (back up first)")
    p.add_argument("--dry-run", action="store_true", help="count without writing")
    p.set_defaults(func=cmd_backfill_relation_vault)

    p = sub.add_parser("gaps", help="write candidate knowledge gaps (needs_review; not found in the vault != absent from the literature)")
    p.add_argument("--vault")
    p.add_argument("--dry-run", action="store_true", help="count and show examples without writing")
    p.set_defaults(func=cmd_gaps)

    p = sub.add_parser("rebuild-ghosts", help="rebuild unresolved cited-paper registry from local references")
    p.set_defaults(func=cmd_rebuild_ghosts)

    p = sub.add_parser("discover-citations", help="find works citing one local paper via OpenAlex (sends DOI only)")
    p.add_argument("paper_id")
    p.add_argument("--vault")
    p.add_argument("--limit", type=int, default=100)
    p.set_defaults(func=cmd_discover_citations)
    p = sub.add_parser("stage-discovery", help="stage selected OpenAlex metadata in inbox; no PDF is downloaded")
    p.add_argument("vault")
    p.add_argument("candidate_id")
    p.set_defaults(func=cmd_stage_discovery)

    p = sub.add_parser("import-refs", help="read-only reference-manager metadata import")
    p.add_argument("file")
    p.add_argument("--vault")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_import_refs)

    p = sub.add_parser("image-audit", help="read-only: give every vault image an address (class, paper, part, clone or trace)")
    p.add_argument("--vault")
    p.add_argument("--out", help="write the full row list as JSON to this file")
    p.set_defaults(func=cmd_image_audit)

    p = sub.add_parser("tidy-vault", help="reversibly quarantine unlinked machine stubs")
    p.add_argument("vault", nargs="?", help="vault name")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_tidy_vault)

    p = sub.add_parser("node-decision", help="node_decisions.jsonl, its only writer: submit FILE | remove KEY KIND | check | preview [FILE] --out PATH")
    p.add_argument("action", choices=("submit", "remove", "check", "preview"))
    p.add_argument("rest", nargs="*")
    p.add_argument("--out", help="preview: the Markdown report (default runtime/g4-4/node_preview.md)")
    p.set_defaults(func=cmd_node_decision)

    p = sub.add_parser("retire-machine-files", help="G5: move machine-only Mine/ index notes and orphan traced images to knowledge_store/removed/ (dry run unless --apply; undo with vault-migrate --undo)")
    p.add_argument("--vault", required=True)
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_retire_machine_files)
    sub.add_parser("rebuild-db", help="rebuild library.db from knowledge_store/papers/").set_defaults(func=cmd_rebuild)
    p = sub.add_parser("store-lf", help="CHR-03: store text files written with CRLF -> LF (content checked; dry run unless --apply)")
    p.add_argument("--apply", action="store_true")
    p.set_defaults(func=cmd_store_lf)
    sub.add_parser("status", help="counts").set_defaults(func=cmd_status)

    p = sub.add_parser("refresh-review-policy", help="preview or apply source-grounded relevance and exact evidence dedup; archives machine records")
    p.add_argument("--vault")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_refresh_review_policy)
    sub.add_parser("review-list", help="pending human review items").set_defaults(func=cmd_review_list)
    p = sub.add_parser("review-decide", help="record a human decision: keep/archive (classification) or resolved (other kinds)")
    p.add_argument("item_id", type=int)
    p.add_argument("decision", choices=("keep", "archive", "resolved"))
    p.set_defaults(func=cmd_review_decide)
    p = sub.add_parser("archive-source", help="archive a verified original PDF")
    p.add_argument("paper_id")
    p.add_argument("--destination", help="existing archive folder, including an external drive")
    p.set_defaults(func=cmd_archive_source)
    p = sub.add_parser("restore-source", help="restore an archived PDF to sources/active")
    p.add_argument("paper_id")
    p.set_defaults(func=cmd_restore_source)

    for name, module, text in (
            ("node-card", "node_cards", "AI node cards: task NAME --vault V | submit NAME FILE --vault V --analyst A | pending --vault V | show NAME --vault V"),
            ("figure-reading", "figure_reading", "AI figure/table reading: task | submit | pending | show"),
            ("source", "sources_misc", "non-PDF sources: import FILE --vault V | list --vault V | show SOURCE_ID"),
            ("obsidian-plugins", "vault_plugins", "approved Obsidian plugins: status | download | install [vault...]")):
        p = sub.add_parser(name, help=text, add_help=False)
        p.add_argument("rest", nargs=argparse.REMAINDER)
        p.set_defaults(func=_passthrough(module))
    p = sub.add_parser("mother-vault", help="refresh the mother vault vaults/ (Home, Atlas, Registry, conflicts)")
    p.set_defaults(func=cmd_mother_vault)
    p = sub.add_parser("graph-colours", help="back up and re-merge Obsidian graph colour groups (no notes rebuild); "
                                             "default: every vault plus the mother vault")
    p.add_argument("vault", nargs="*")
    p.set_defaults(func=cmd_graph_colours)

    args = parser.parse_args()
    if args.command == "reextract" and not (args.all or args.paper_id):
        parser.error("reextract needs a paper_id or --all")
    if args.command == "enrich-interventions" and not (args.all or args.paper_id):
        args.all = True
    cfg = config.load()
    args.db_existed = config.DB_PATH.is_file()
    args.store_existed = config.STORE.is_dir()  # db.connect() below creates it; init reports what it found
    con = None if args.command == 'ai-bench' else db.connect()  # the bench works in its own temp store
    try:
        from contextlib import nullcontext
        readonly = {'mcp', 'tool', 'status', 'paper', 'search', 'sentences', 'read-paper', 'ref', 'get-context',
                    'analysis-status', 'analysis-guide', 'review-list', 'section-profile', 'neural-search',
                    'neural-prepare', 'neural-check', 'compare-protocols', 'mcp-config', 'ai-check', 'ai-bench', 'selftest', 'conform', 'analysis-usage',
                    'media-status', 'structured-status', 'export-analysis', 'export-docx', 'image-audit',
                    'reextract' if args.command == 'reextract' and args.dry_run else '__never__',
                    'gaps' if args.command == 'gaps' and args.dry_run else '__never__',
                    'store-lf' if args.command == 'store-lf' and not args.apply else '__never__',
                    'retire-machine-files' if args.command == 'retire-machine-files' and not args.apply else '__never__',
                    'node-decision' if args.command == 'node-decision' and args.action in ('check', 'preview') else '__never__',
                    # these take the `library` writer lock themselves, only around store writes, so a slow AI
                    # call never blocks other writers (`steps` locks per stage inside pipeline.run)
                    'node-card', 'figure-reading', 'analyze', 'structured-run', 'steps', 'run-all'}
        with nullcontext() if args.command in readonly else workflow.writer_lock():
            args.func(args, cfg, con)
    except AIError as error:  # e.g. no AI chosen: a clear sentence, not a traceback
        sys.exit(f"AI: {error}")
    except ValueError as error:  # a wrong ID, choice or state: one line for the researcher
        sys.exit(f"Error: {error}")
    finally:
        if con:
            con.close()


if __name__ == "__main__":
    main()
