"""Tier 1 of the v2 design (docs/CONTRACTS.md §2): for one paper, build

  sentences.jsonl   every sentence with its id, page, section, citations and mentions
  references.jsonl  the paper's own reference list, parsed locally
  figures.jsonl     figure captions, positions and crops
  tables.jsonl      tables as rows with column names, positions and crops

from the machine record and, when available, the original PDF. No AI is involved.
"""

import collections
import csv
import hashlib
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import pymupdf

from . import concepts, config, db, extract, objects, references, renumber, sentences

EXTRACTION_VERSION = 3


LAST_RELINK = {}  # paper_id -> concepts.relink_paper result of the last build (affected_concepts / affected_vaults)


def main_pdf(meta):
    """Path of the paper's main PDF if it is reachable on this machine, else None."""
    for src in meta.get("source_files", []):
        if src.get("role") == "main" and src.get("status") in ("active", "archived", "external") and src.get("path"):
            path = Path(src["path"])
            path = path if path.is_absolute() else config.ROOT / path
            if path.is_file():
                return path
    return None


def _supplement_path(meta, supp):
    for src in meta.get("source_files", []):
        if src.get("supp_id") == supp["supp_id"] and src.get("path"):
            path = Path(src["path"])
            path = path if path.is_absolute() else config.ROOT / path
            return path if path.is_file() else None
    return None


def build(con, paper_id, cfg=None, ex=None, pdf_path=None, before=None, history=None, old_passages=None):
    """(Re)build every Tier 1 file of one paper and load it into the database.

    ex/pdf_path: a fresh extraction of the main PDF (from ingestion). If not given, the PDF is
    re-read when available; otherwise sentences and references come from stored passages.
    Objects/crops from unavailable sources are retained. Crop revisions are staged and old
    machine records archived before publication. Passages are never changed here; `reextract`
    rewrites them first and passes what only the old files can tell: `before` (renumber.snapshot),
    `history` (the archive directory) and `old_passages` (to carry passage IDs of retained objects).
    Caller commits.
    """
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    old_sentences = list(db.read_jsonl(folder / "sentences.jsonl"))
    old_refs = list(db.read_jsonl(folder / "references.jsonl"))  # read before the new entries replace them
    if before is None and old_sentences:
        before = renumber.snapshot(paper_id)  # which saved artifacts are current NOW
    passages = list(db.read_jsonl(folder / "passages.jsonl"))
    if pdf_path is None:
        pdf_path = main_pdf(meta)
    if ex is None and pdf_path is not None:
        ex = extract.extract(pdf_path, *_passage_sizes(cfg))
    vocab = ex.vocab if ex is not None else extract.build_vocabulary(p["text"] for p in db.read_jsonl(folder / "pages.jsonl"))

    # references
    refs = references.parse_lines(ex.reference_lines, vocab) if ex is not None else []
    if not refs:
        refs = references.from_passages(passages, vocab)
    for i, r in enumerate(refs, start=1):
        r["ref_id"] = f"{paper_id}:R{(r['num'] or i):03d}"
    references.resolve(con, refs, own_paper_id=paper_id)
    for r in refs:
        r.setdefault("ghost_id", None)

    # citation style, then sentences
    ctx = sentences.CitationContext(refs)
    body = [p["text"] for p in passages if p.get("source", "main") == "main"
            and p["section"] not in ("front_matter", "references")]
    ctx.styles = sentences.count_styles(body, ctx)
    style = sentences.citation_style(ctx.styles, bool(ctx.numbered))
    sents = sentences.build(paper_id, passages, ctx)

    # Never delete a derivative before a replacement has been built successfully.
    passage_map = renumber.id_map(old_passages, passages, "passage_id") if old_passages is not None else None
    figures, tables, retained_sources = _build_objects(folder, paper_id, meta, old_sentences, ex, pdf_path, cfg,
                                                       passage_map=passage_map)

    sents = _insert_table_rows(sents, tables, passages)
    _mark_table_text(sents, tables)
    for seq, s in enumerate(sents, start=1):
        s["seq"] = seq
        s["sentence_id"] = f"{paper_id}:S{seq:04d}"
    sentences.link_page_breaks(sents)
    _link_objects(sents, figures, tables, passages)
    history = _archive_tier1(folder, history)

    db.write_jsonl(folder / "sentences.jsonl", (_ordered(s) for s in sents))
    db.write_jsonl(folder / "references.jsonl", refs)
    db.write_jsonl(folder / "figures.jsonl", ({k: v for k, v in f.items() if not k.startswith("_")} for f in figures))
    db.write_jsonl(folder / "tables.jsonl", ({k: v for k, v in t.items() if not k.startswith("_")} for t in tables))
    meta.update({
        "extraction_version": EXTRACTION_VERSION, "citation_style": style,
        "tier1": {"built_at": config.now(), "from": "pdf" if ex is not None else "passages_only",
                  "sentences": len(sents), "references": len(refs), "figures": len(figures),
                  "tables": len(tables), "citation_counts": ctx.styles,
                  "retained_object_sources": retained_sources},
        "updated_at": config.now(),
    })
    meta.setdefault("supplements", [])
    db.write_json(folder / "metadata.json", meta)
    if old_sentences:  # after sentences/metadata are final: re-stamping hashes needs the new files
        carried = renumber.carry_over(paper_id, old_sentences, sents, before, history, old_refs, refs)
        meta["tier1"]["sentence_ids"] = {"renumbered": carried["renumbered"], "unmapped": carried["unmapped"]}
        meta["tier1"]["reference_ids"] = carried["reference_ids"]
    before_concepts = [r[0] for r in (con.execute("SELECT DISTINCT concept_id FROM concept_links WHERE paper_id = ?", (paper_id,)) if con else ())]
    before_terms = [r[0] for r in (con.execute("SELECT term FROM paper_terms WHERE paper_id = ?", (paper_id,)) if con else ())]
    db.load_paper(con, paper_id)
    meta["tier1"]["cited_by_links"] = references.link_back(con, paper_id)
    db.write_json(folder / "metadata.json", meta)
    LAST_RELINK[paper_id] = concepts.relink_paper(con, paper_id, before_terms, before_concepts)  # read by kb.py reextract
    from . import quality
    db.write_json(folder / "analysis_quality.json", quality.inspect(paper_id))
    return meta["tier1"]


def _archive_tier1(folder, history=None):
    """Keep exact prior files, including saved analysis references, before multi-file writes.
    With `history` (an earlier archive of the same rebuild) nothing more is copied."""
    if history is not None:
        return history
    names = ("sentences.jsonl", "references.jsonl", "figures.jsonl", "tables.jsonl", "metadata.json",
             "analysis.json", "analysis_state.json", "analysis_rejected.json", "analysis_v2.draft.json",
             "analysis_v2.parts.json", "evidence.jsonl", "analysis_quality.json", "pages.jsonl",
             "passages.jsonl", "sections.json", *renumber.PROFILES)
    existing = [folder / name for name in names if (folder / name).is_file()]
    if existing:
        history = folder / "tier1_history" / uuid.uuid4().hex
        history.mkdir(parents=True)
        for path in existing:
            shutil.copy2(path, history / path.name)
        return history  # carry_over adds the other files it rewrites (readings, node cards, ...)


def _build_objects(folder, paper_id, meta, old_sentences, ex, pdf_path, cfg, dry_run=False, passage_map=None):
    """Build available sources in isolation; retain missing-source objects and all old crops.
    dry_run: stage in the system temp folder and publish nothing (used by reextract_dry_run)."""
    from .media import crop_path
    old = {"figure": list(db.read_jsonl(folder / "figures.jsonl")),
           "table": list(db.read_jsonl(folder / "tables.jsonl"))}
    sentence_map = {s["sentence_id"]: s for s in old_sentences}
    figures, tables, rebuilt = [], [], set()
    revision = uuid.uuid4().hex
    destination = crop_path(folder, f"crops/tier1-{revision}")
    with tempfile.TemporaryDirectory(prefix=".tier1-stage-", dir=None if dry_run else folder) as temporary:
        stage = Path(temporary)

        def read_pdf(path, captions, source):
            source_dir = hashlib.sha256(source.encode()).hexdigest()[:12]
            with pymupdf.open(path) as doc:
                f, t = objects.build(paper_id, doc, captions, source, stage / source_dir)
            for obj in f + t:
                if obj.get("crop"):
                    # objects.build emits crops/<name>; remap to an immutable source-local revision.
                    rendered = stage / source_dir / Path(obj["crop"]).name
                    if not rendered.is_file():
                        raise IOError("Object crop was not rendered")
                    obj["crop"] = f"crops/tier1-{revision}/{source_dir}/{rendered.name}"
            figures.extend(f)
            tables.extend(t)
            rebuilt.add(source)

        if ex is not None and pdf_path is not None:
            read_pdf(pdf_path, ex.captions, "main")
        for supp in meta.get("supplements", []):
            path = _supplement_path(meta, supp)
            if path is None or supp.get("status") != "extracted":
                continue
            if supp["kind"] == "pdf":
                sex = extract.extract(path, *_passage_sizes(cfg))
                read_pdf(path, sex.captions, supp["supp_id"])
            elif supp["kind"] == "csv":
                tables.append(_csv_table(paper_id, supp, path, tables))
                rebuilt.add(supp["supp_id"])
            elif supp["kind"] == "xlsx":
                tables.extend(_xlsx_tables(paper_id, supp, path, tables))
                rebuilt.add(supp["supp_id"])
        retained = set()
        for kind, target in (("figure", figures), ("table", tables)):
            for obj in old[kind]:
                source = obj.get("source", "main")
                if source in rebuilt:
                    continue
                if obj.get("crop"):
                    crop_path(folder, obj["crop"])
                ids = [sentence_map[sid]["passage_id"] for sid in obj.get("caption_sentence_ids", [])
                       if sid in sentence_map and sentence_map[sid].get("passage_id")]
                # passages were re-read: follow them by exact text; an unmapped one is dropped, never re-pointed
                obj["_caption_passage_ids"] = ids if passage_map is None else [passage_map[i] for i in ids if i in passage_map]
                target.append(obj)
                retained.add(source)
        # Rendering/extraction failures above have left prior records and crops untouched.
        # Promotion is additive; interruption afterwards leaves an unused derivative, never
        # removes the previous crop or its active record. Publication is not all-files atomic.
        if not dry_run and any(stage.iterdir()):
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, destination)
    return figures, tables, sorted(retained)


SENTENCE_KEYS = ["sentence_id", "seq", "passage_id", "source", "page", "section", "subsection", "kind",
                 "text", "start", "end", "citations", "mentions", "object_id", "continues", "context_basis",
                 "in_table", "lay_size", "lay_ref", "lay_y"]


def _ordered(s):
    return {k: s[k] for k in SENTENCE_KEYS if k in s}


def _passage_sizes(cfg):
    p = (cfg or {}).get("passages", {})
    return p.get("target_chars", 1000), p.get("max_chars", 2000)


def _caption_passages(obj, passages):
    """Caption passages of one figure/table: same source and page, text starting like the caption."""
    if "_caption_passage_ids" in obj:
        return obj["_caption_passage_ids"]
    head = extract.clean_spaces(obj.get("_caption", ""))[:40]
    if not head:
        return []
    return [p["passage_id"] for p in passages if p.get("kind") == "caption" and p["page"] == obj["page"]
            and p.get("source", "main") == obj["source"] and p["text"].startswith(head)]


def _insert_table_rows(sents, tables, passages):
    """Each table's rows become kind "table_row" sentences, placed after its caption."""
    out = list(sents)
    for t in tables:
        if not t["rows"]:
            continue
        caption_ids = set(_caption_passages(t, passages))
        where = [i for i, s in enumerate(out) if s["passage_id"] in caption_ids]
        if not where:
            where = [i for i, s in enumerate(out) if s["page"] == t["page"] and s["source"] == t["source"]]
        anchor = where[-1] if where else len(out) - 1
        if t["source"] != "main":  # supplementary tables always belong to the supplementary section
            section, subsection = "supplementary", None
        else:
            section = out[anchor]["section"] if out else "results"
            subsection = out[anchor].get("subsection") if out else None
        rows = [{"passage_id": None, "source": t["source"], "page": t["page"], "section": section,
                 "subsection": subsection, "kind": "table_row", "text": objects.row_text(t["header"], row, t["quality"] == "ok"),
                 "start": None, "end": None, "citations": [], "mentions": [], "object_id": t["table_id"]}
                for row in t["rows"]]
        rows = [r for r in rows if r["text"]]
        out[anchor + 1:anchor + 1] = rows
    return out


def _mark_table_text(sents, tables):
    """A table is also read as running text, which mixes cells and columns. Mark such text
    sentences with "in_table": true so that evidence cites the rows instead. Four tests, same page:
    numeric tables: most of the sentence's numbers are in the table rows (no citations in it);
    text tables (reviews listing species, methods, references): a sentence of 8+ words of which 85% occur
    in the table cells -- citations do not count, such tables carry them inside the cells;
    a table of 12+ cell words, 80% of which the sentence holds (dumped columns, single digits included);
    position: the text right after a table's caption or rows is that table printed again (also for a 0-row table,
    whose cells are unknown; a caption on a page without a table record only continues a table of an earlier page);
    when the cells are known at least half of its words must be cells, else it is a body sentence with a header glued
    on. Text after such a flagged row that reads like table text (`table_text_suspect`) belongs to it too. With known
    cells, a verb after the last cell word is a body sentence glued after the table: no flag by the last two tests."""
    by_page, words, labels, bags = {}, {}, {}, {}
    for t in tables:
        if t["rows"]:
            nums = sentences.numbers(" ".join(" ".join(r) for r in t["rows"]))
            by_page.setdefault((t["source"], t["page"]), set()).update(nums)
        cells = [c for row in [t.get("header") or [], *(t["rows"] or [])] for c in row]
        words.setdefault((t["source"], t["page"]), set()).update(w for c in cells for w in re.findall(r"\w+", c.casefold()))
        bag = collections.Counter(w for c in cells for w in re.findall(r"\w+", c.casefold()))
        if sum(bag.values()) >= 12:
            bags.setdefault((t["source"], t["page"]), []).append(bag)
        first = labels.setdefault(t["source"], {})  # label -> first page of the table
        name = str(t.get("label") or "table").casefold()
        first[name] = min(first.get(name, t["page"]), t["page"])
    after = None  # ("table" | "dump", page key): the previous row is a table's caption or row | flagged table text
    for s in sents:
        key = (s["source"], s["page"])
        if s["kind"] != "text":
            # a table caption on a page without a table record only continues a table of an earlier page
            caption = s["kind"] == "caption" and (after == ("table", key) or any(
                s["text"].casefold().startswith(name) and (key in words or page < s["page"])
                for name, page in labels.get(s["source"], {}).items()))
            after = ("table", key) if s["kind"] == "table_row" or caption else None
            continue
        nums = by_page.get(key)
        if nums and not s["citations"]:
            # only "significant" numbers count: decimals or 2+ digits (small integers are everywhere)
            mine = {n for n in sentences.numbers(s["text"]) if "." in n or len(n) >= 2}
            if len(mine) >= 5 and len(mine & nums) / len(mine) >= 0.7:
                s["in_table"] = True
        tokens = re.findall(r"\w+", s["text"].casefold())
        if words.get(key) and len(tokens) >= 8 and sum(w in words[key] for w in tokens) / len(tokens) >= 0.85:
            s["in_table"] = True
        known = words.get(key) or set()
        last = max((i for i, w in enumerate(tokens) if w in known), default=-1)
        glued = sentences._has_verb(" ".join(tokens[last + 1:]))  # a body sentence glued after the last cell word
        have = collections.Counter(tokens)
        if not glued and any(sum(min(n, have[w]) for w, n in bag.items()) >= 0.8 * sum(bag.values())
                             for bag in bags.get(key, ())):
            s["in_table"] = True
        if after and after[1] == key and not s.get("in_table"):
            if after[0] == "dump" and sentences.table_text_suspect(s) or after[0] == "table" and (
                    not known or not glued and sum(w in known for w in tokens) / max(len(tokens), 1) >= 0.5):
                s["in_table"] = True
        after = ("dump", key) if s.get("in_table") else None


def _link_objects(sents, figures, tables, passages):
    by_passage = {}
    for s in sents:
        by_passage.setdefault(s["passage_id"], []).append(s["sentence_id"])
    for obj in figures + tables:
        oid = obj.get("figure_id") or obj.get("table_id")
        obj["caption_sentence_ids"] = [sid for pid in _caption_passages(obj, passages)
                                       for sid in by_passage.get(pid, [])]
        obj["mentioned_in"] = [s["sentence_id"] for s in sents if oid in s["mentions"]]


def _next_ts(existing):
    return 1 + max([int(t["table_id"].rsplit("TS", 1)[1]) for t in existing if ":TS" in t["table_id"]] or [0])


def _cell(value):
    """Spreadsheet cell as text: integers without '.0', floats in shortest exact form."""
    if value is None:
        return ""
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return extract.clean_spaces(str(value))


def _sheet_table(paper_id, supp, number, label, data, extraction):
    data = [row for row in data if any(row)]
    return {"table_id": f"{paper_id}:TS{number}", "label": label, "source": supp["supp_id"], "page": 1,
            "caption_sentence_ids": [], "header": data[0] if data else [], "rows": data[1:], "bbox": None,
            "crop": None, "footnotes": None, "extraction": extraction,
            "quality": "ok" if len(data) > 1 else "uncertain", "mentioned_in": [], "_caption": ""}


def _xlsx_tables(paper_id, supp, path, existing):
    """One table per non-empty worksheet (values as last saved by Excel; formulas are not re-run)."""
    import openpyxl
    out = []
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet in book.worksheets:
            data = [[_cell(v) for v in row] for row in sheet.iter_rows(values_only=True)]
            if any(any(row) for row in data):
                number = _next_ts(existing + out)
                out.append(_sheet_table(paper_id, supp, number,
                                        f"Table S{number} ({supp['original_filename']}, sheet {sheet.title})",
                                        data, "xlsx"))
    finally:
        book.close()
    return out


def _csv_table(paper_id, supp, path, existing):
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(4096)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        data = [[extract.clean_spaces(c) for c in row] for row in csv.reader(f, dialect)]
    number = _next_ts(existing)
    return _sheet_table(paper_id, supp, number, f"Table S{number} ({supp['original_filename']})", data, "csv")


# ---------------------------------------------------------------- supplementary files

SUPPORTED = {".pdf": "pdf", ".csv": "csv", ".xlsx": "xlsx", ".xlsm": "xlsx"}  # old .xls: kept, not read


def _append_supplement_passages(folder, supp_id, pdf_path, cfg):
    ex = extract.extract(pdf_path, *_passage_sizes(cfg))
    passages = [p for p in db.read_jsonl(folder / "passages.jsonl") if p.get("source", "main") != supp_id]
    seq = max([p["seq"] for p in passages] or [0])
    paper_id = folder.name
    for p in ex.passages:
        seq += 1
        passages.append({"passage_id": f"{paper_id}:{seq:04d}", "page": p["page"], "section": "supplementary",
                         "subsection": p.get("subsection"), "kind": p["kind"], "text": p["text"],
                         "seq": seq, "source": supp_id,
                         **({"ocr": p["ocr"]} if "ocr" in p else {})})
    db.write_jsonl(folder / "passages.jsonl", passages)
    # Each source keeps its own page numbers and OCR outcome; main-page IDs stay unchanged.
    pages_path = folder / "supplements" / supp_id.rsplit(":", 1)[-1] / "pages.jsonl"
    db.write_jsonl(pages_path, ex.pages)
    meta = db.read_json(folder / "metadata.json")
    meta["ocr_pages"] = [p for p in meta.get("ocr_pages", []) if p.get("source", "main") != supp_id]
    meta["ocr_pages"].extend({**p, "source": supp_id} for p in ex.ocr_pages)
    db.write_json(folder / "metadata.json", meta)
    return len(ex.passages)


def attach_supplement(con, cfg, paper_id, file_path):
    """Copy a supplementary file next to the paper's PDF and make it searchable.
    The file given is copied, never moved or modified. Caller commits."""
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(src)
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if meta is None:
        raise FileNotFoundError(f"No paper {paper_id}")
    sha = extract.sha256_file(src)
    if any(s.get("sha256") == sha for s in meta.get("source_files", [])):
        return f"Already attached: {src.name}"
    kind = SUPPORTED.get(src.suffix.lower(), "other")
    supplements = meta.setdefault("supplements", [])
    supp_id = f"{paper_id}:X{len(supplements) + 1}"
    dest_dir = config.ROOT / cfg["ingest"].get("active_folder", "sources/active") / paper_id / "supp"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.name
    i = 2
    while dest.exists():
        dest = dest_dir / f"{src.stem}_{i}{src.suffix}"
        i += 1
    shutil.copy2(src, dest)
    if extract.sha256_file(dest) != sha:
        dest.unlink()
        raise IOError(f"Copy of {src.name} does not match the original; nothing was attached")
    status = "extracted" if kind in ("pdf", "csv", "xlsx") else "unsupported"
    meta.setdefault("source_files", []).append({
        "sha256": sha, "path": config.rel(dest), "original_filename": src.name, "size_bytes": dest.stat().st_size,
        "status": "active", "role": "supplement", "supp_id": supp_id, "batch_id": None, "added_at": config.now()})
    supplements.append({"supp_id": supp_id, "sha256": sha, "original_filename": src.name, "kind": kind,
                        "status": status})
    db.write_json(folder / "metadata.json", meta)
    n = _append_supplement_passages(folder, supp_id, dest, cfg) if kind == "pdf" else 0
    info = build(con, paper_id, cfg)
    note = " (this file type is not read; the file is kept)" if status == "unsupported" else ""
    return f"Attached {src.name} as {supp_id}: {n} passages, now {info['sentences']} sentences, {info['tables']} tables{note}"


# ---------------------------------------------------------------- re-extraction of stored papers

def _reread_passages(paper_id, meta, ex, cfg, old_passages):
    """Passages after reading the PDFs again: the main PDF (`ex`), then each supplement in order, re-extracted
    when its file is reachable, otherwise the stored passages of that source (renumbered, text unchanged).
    Pure: returns (passages, {supp_id: extraction}); nothing is written."""
    passages = [{"passage_id": f"{paper_id}:{p['seq']:04d}", **p} for p in ex.passages]
    seq = len(passages)
    extracted = {}

    def add(source, rows):
        nonlocal seq
        for row in rows:
            seq += 1
            passages.append({**row, "passage_id": f"{paper_id}:{seq:04d}", "seq": seq, "source": source})

    known = {"main"}
    for supp in meta.get("supplements", []):
        source = supp["supp_id"]
        known.add(source)
        path = _supplement_path(meta, supp)
        if supp.get("kind") == "pdf" and path is not None:
            sex = extract.extract(path, *_passage_sizes(cfg))
            extracted[source] = sex
            add(source, [{"page": p["page"], "section": "supplementary", "subsection": p.get("subsection"),
                          "kind": p["kind"], "text": p["text"], **({"ocr": p["ocr"]} if "ocr" in p else {})}
                         for p in sex.passages])
        else:
            add(source, [p for p in old_passages if p.get("source") == source])
    for source in sorted({p.get("source", "main") for p in old_passages} - known):
        add(source, [p for p in old_passages if p.get("source") == source])  # unknown source: kept, not dropped
    return passages, extracted


def reextract(con, cfg, paper_id):
    """Run Tier 1 again for a stored paper, without any AI.

    With the original PDF available, pages, sections and passages are read again for EVERY paper, analysed
    or not (so extractor fixes reach saved papers). Saved analysis follows by exact text
    (renumber.carry_over); what no longer matches stays as written and is flagged or reads stale.
    No stored analysis cites passage IDs (v2 cites sentence IDs); derived indexes (database FTS is reloaded
    here; structure.json, media manifest and vectors are rebuilt by `steps`) follow CONTRACTS section 2.7.
    The old files go to tier1_history/. Caller commits.
    """
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if meta is None:
        raise FileNotFoundError(f"No paper {paper_id}")
    pdf = main_pdf(meta)
    if pdf is None:
        _normalize_saved_glyphs(folder)
        return build(con, paper_id, cfg), "original PDF not available: sentences rebuilt from stored passages"
    ex = extract.extract(pdf, *_passage_sizes(cfg))
    extract.refine_sections(ex, meta.get("abstract"))
    # Everything the old files can tell must be read before any of them is replaced.
    before = renumber.snapshot(paper_id) if (folder / "sentences.jsonl").is_file() else None
    history = _archive_tier1(folder)
    old_passages = list(db.read_jsonl(folder / "passages.jsonl"))
    passages, extracted = _reread_passages(paper_id, meta, ex, cfg, old_passages)
    try:  # from here on files are replaced: a failure must tell where the exact previous copies are
        db.write_jsonl(folder / "pages.jsonl", ex.pages)
        db.write_json(folder / "sections.json", ex.sections)
        db.write_jsonl(folder / "passages.jsonl", passages)
        meta = db.read_json(folder / "metadata.json")
        # OCR outcome of supplements that were not read again stays; re-read ones are replaced.
        ocr = [p for p in meta.get("ocr_pages", []) if p.get("source", "main") != "main" and p["source"] not in extracted]
        for source, sex in extracted.items():
            db.write_jsonl(folder / "supplements" / source.rsplit(":", 1)[-1] / "pages.jsonl", sex.pages)
            ocr.extend({**p, "source": source} for p in sex.ocr_pages)
        meta.update({"n_passages": len(passages), "text_quality": ex.text_quality, "ocr_pages": [*ex.ocr_pages, *ocr]})
        db.write_json(folder / "metadata.json", meta)
        return (build(con, paper_id, cfg, ex=ex, pdf_path=pdf, before=before, history=history, old_passages=old_passages),
                "re-read from the original PDF (passages included)")
    except Exception as err:
        raise RuntimeError(f"{err} -- the database was rolled back but some files of {paper_id} may already be new; exact "
                           f"copies of the previous ones are in {history} (top level: {folder}; carry_over/ holds the "
                           f"rewritten readings, node cards and drafts under their store path joined by '__'). "
                           f"Copy them back, then run `kb.py rebuild-db`.") from err


def _simulate(con, cfg, paper_id, refs_out=None):
    """In-memory result of `reextract` for one paper, nothing written: (old sentences, new sentences,
    new passages, tables, section list); the new parts are None when the original PDF is not available.
    `refs_out` (a list) receives the new reference entries."""
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if meta is None:
        raise FileNotFoundError(f"No paper {paper_id}")
    old = list(db.read_jsonl(folder / "sentences.jsonl"))
    pdf = main_pdf(meta)
    if pdf is None:
        return old, None, None, None, None
    ex = extract.extract(pdf, *_passage_sizes(cfg))
    extract.refine_sections(ex, meta.get("abstract"))
    old_passages = list(db.read_jsonl(folder / "passages.jsonl"))
    passages, _ = _reread_passages(paper_id, meta, ex, cfg, old_passages)
    refs = references.parse_lines(ex.reference_lines, ex.vocab)
    if not refs:
        refs = references.from_passages(passages, ex.vocab)
    for i, ref in enumerate(refs, start=1):
        ref["ref_id"] = f"{paper_id}:R{(ref['num'] or i):03d}"
    if refs_out is not None:
        refs_out.extend(refs)
    ctx = sentences.CitationContext(refs)
    body = [p["text"] for p in passages if p.get("source", "main") == "main"
            and p["section"] not in ("front_matter", "references")]
    ctx.styles = sentences.count_styles(body, ctx)
    fresh = sentences.build(paper_id, passages, ctx)
    _, tables, _ = _build_objects(folder, paper_id, meta, old, ex, pdf, cfg, dry_run=True)
    fresh = _insert_table_rows(fresh, tables, passages)
    _mark_table_text(fresh, tables)
    for seq, row in enumerate(fresh, start=1):
        row["seq"] = seq
        row["sentence_id"] = f"{paper_id}:S{seq:04d}"
    return old, fresh, passages, tables, ex.sections


def reextract_dry_run(con, cfg, paper_id):
    """Preview what `reextract` would do to sentences and saved analysis, writing nothing.

    Runs the same derivation as `reextract` + `build` in memory (passages read again from the PDF,
    table-row insertion) and reports the old-to-new sentence map. Renumbered sentences are carried over
    automatically (renumber.carry_over), so only artifacts that cite an UNMAPPED old sentence are counted
    as needing AI re-analysis (`needs_reanalysis`). `text_preservation` proves no source text disappears."""
    folder = db.paper_dir(paper_id)
    new_refs = []
    old, fresh, passages, tables, sections = _simulate(con, cfg, paper_id, new_refs)
    if fresh is None:
        return {"paper_id": paper_id, "available": False, "reason": "original PDF not available",
                "old_sentences": len(old), "new_sentences": None, "mapping": {},
                "stale_analysis_count": None, "stale_evidence_units": None}
    mapping = renumber.id_map(old, fresh)
    unmapped_ids = {row["sentence_id"] for row in old} - set(mapping)
    changed = {old_id: new_id for old_id, new_id in mapping.items() if old_id != new_id}
    need = renumber.impact(paper_id, old, mapping, fresh)
    artifacts = [name for name in ("analysis.json", *renumber.PROFILES) if (folder / name).is_file()]
    old_refs = list(db.read_jsonl(folder / "references.jsonl"))
    ref_map = renumber.ref_id_map(old_refs, new_refs)
    units = list(db.read_jsonl(folder / "evidence.jsonl")) if (folder / "evidence.jsonl").is_file() else []
    return {"paper_id": paper_id, "available": True, "old_sentences": len(old),
            "new_sentences": len(fresh), "mapped_sentences": len(mapping), "renumbered_sentences": len(changed),
            "unmapped_old_sentence_ids": sorted(unmapped_ids),
            "affected_old_sentence_ids": sorted(unmapped_ids | set(changed)), "mapping": changed,
            "stale_analysis_count": len(need["evidence_units"]), "stale_evidence_units": need["evidence_units"],
            "needs_reanalysis": {"evidence_units": len(need["evidence_units"]), "profiles": len(need["profiles"]),
                                 "figure_readings": len(need["figure_readings"]),
                                 "node_cards": len(need["node_cards"]), "relations": need["relations"]},
            "needs_reanalysis_detail": {"profiles": need["profiles"], "figure_readings": need["figure_readings"],
                                        "node_cards": need["node_cards"], "methods_added": need["methods_added"]},
            "change_summary": renumber.change_summary(old, fresh),
            "text_preservation": renumber.text_preservation(old, fresh, passages, tables, sections),
            "analysis_artifacts_present": artifacts,
            "reference_ids": {**renumber.ref_counts(old_refs, ref_map), "old_entries": len(old_refs),
                              "new_entries": len(new_refs),
                              "evidence_units_flagged": renumber.carry_refs(units, old_refs, ref_map, paper_id)[1]},
            "note": "Preview only; nothing was written. 'mapping' lists only IDs that change; they are carried "
                    "over automatically. Passages are read again from the PDF, as reextract does."}


def _glyph_repaired(folder, name, ex=None):
    """Records of pages.jsonl / passages.jsonl with one-codepoint-safe PDF glyph repairs applied.
    Nothing is written (reextract_dry_run uses this to see the same text the real run will store)."""
    fresh = {}
    if ex is not None:
        fresh["pages.jsonl"] = {str(r["page"]): r["text"] for r in ex.pages}
        fresh["passages.jsonl"] = {str(i + 1): r["text"] for i, r in enumerate(ex.passages)}
    records = list(db.read_jsonl(folder / name))
    for record in records:
        text = record.get("text")
        if isinstance(text, str):
            key = str(record.get("page")) if name == "pages.jsonl" else str(record.get("seq"))
            source_text = fresh.get(name, {}).get(key)
            normalized = extract.normalize_font_glyphs(text)
            if source_text is not None and len(source_text) == len(text):
                diffs = [i for i, (a, b) in enumerate(zip(text, source_text)) if a != b]
                if all(extract.is_verified_font_pair(text[i], source_text[i]) for i in diffs):
                    chars = list(normalized)
                    for i in diffs:
                        chars[i] = source_text[i]
                    normalized = "".join(chars)
            if normalized != text:
                record["text"] = normalized
    return records


def _normalize_saved_glyphs(folder, ex=None):
    """Apply one-codepoint-safe PDF glyph repairs to stored source-derived text.

    Re-extraction normally preserves passages for analysed papers. Verified one-codepoint mappings
    preserve string length, sentence IDs, and offsets, so they can be repaired without remapping
    references or changing the meaning of any analysis.
    """
    for name in ("pages.jsonl", "passages.jsonl"):
        path = folder / name
        if path.exists():
            records = _glyph_repaired(folder, name, ex)
            if records != list(db.read_jsonl(path)):
                db.write_jsonl(path, records)
