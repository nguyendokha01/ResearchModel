"""Ingest new PDFs from the inbox, one paper at a time.

For each PDF:
  1. exact-duplicate check by file hash
  2. text extraction (pages, sections, passages)
  3. metadata (DOI in PDF -> Crossref -> PubMed; offline fallback)
  4. duplicate check by DOI, PMID, normalized title + year -> merge
  5. move the original (unchanged) to sources/active/<paper_id>/
  6. write machine files, load the database, write the Obsidian note
Original source files are never deleted. Problem files are moved to review/pending/;
identical inbox copies are automatically associated and retained in quarantine.
"""

import gc
import hashlib
import re
import shutil
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

import pymupdf
import yaml

from . import __version__, concepts, config, db, extract, metadata, notes, tier1

STOPWORDS = set("""a an the of in on at to for and or with without by from into via during after before
between under over is are was were be been its their this that these those as than using use""".split())
SUPPLEMENT_MARKER = re.compile(
    r"(?i)(?:^|[\s._-])(?:supp(?:lement(?:ary|al)?)?(?:[\s._-]*\d+)?|"
    r"supporting[\s._-]*(?:information|info|data)|si(?:[\s._-]*\d+)?|"
    r"s\d{1,3}|mmc\d{1,3}|sd[ct]\d{1,3})(?=$|[\s._-])")
SUPPORTED_SUPPLEMENT_EXTENSIONS = {".pdf", ".csv", ".xlsx", ".xlsm"}


def _slug(text, maxlen=40):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:maxlen] or "x"


def _unique_path(path):
    if not path.exists():
        return path
    for i in range(2, 1000):
        candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


def unpack_zips(cfg, say=print):
    """Extract PDFs from .zip files in the inbox, then keep the zip in sources/archive/packed/."""
    inbox = config.ROOT / cfg["ingest"].get("inbox_folder", "sources/inbox")
    for zpath in sorted(p for p in inbox.rglob("*") if p.is_file() and p.suffix.lower() == ".zip"):
        target = _unique_path(zpath.with_suffix(""))
        n_files, skipped = 0, []
        with zipfile.ZipFile(zpath) as zf:
            for member in zf.infolist():
                name = Path(member.filename).name  # never trust folder names inside a zip
                if member.is_dir() or not name:
                    continue
                candidate = Path(name)
                is_pdf = candidate.suffix.lower() == ".pdf"
                is_marked_supplement = (candidate.suffix.lower() in SUPPORTED_SUPPLEMENT_EXTENSIONS
                                        and _supplement_base(candidate.stem) is not None)
                if not (is_pdf or is_marked_supplement):
                    skipped.append(name)
                    continue
                target.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(_unique_path(target / name), "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                n_files += 1
        packed = config.ROOT / "sources" / "archive" / "packed"
        packed.mkdir(parents=True, exist_ok=True)
        kept = _unique_path(packed / zpath.name)
        _move(zpath, kept)
        config.log(f"UNZIPPED {zpath.name}: {n_files} PDFs/supplements -> {config.rel(target)}; zip kept at {config.rel(kept)}; "
                   f"non-PDF files left inside the zip: {skipped}")
        say(f"Đã giải nén {zpath.name}: {n_files} PDF/tệp bổ sung. Tệp zip gốc được cất ở {config.rel(kept)}"
            + (f" ({len(skipped)} tệp không phải PDF vẫn nằm trong zip)." if skipped else "."))


def _move(src, dest):
    """Rename within the project (same drive): atomic, never copies, never overwrites."""
    Path(src).rename(dest)


def find_inbox_pdfs(cfg):
    """Return [(batch_label, path)] for every PDF in the inbox."""
    inbox = config.ROOT / cfg["ingest"].get("inbox_folder", "sources/inbox")
    found = []
    for path in sorted(inbox.rglob("*")):
        if not (path.is_file() and path.suffix.lower() == ".pdf"):
            continue
        parts = path.relative_to(inbox).parts
        if parts[0] == "batches" and len(parts) > 2:
            label = parts[1]
        elif parts[0] != "batches" and len(parts) > 1:
            label = parts[0]
        else:
            label = "loose"
        found.append((label, path))
    return found


def _supplement_base(stem):
    """Return a filename stem's likely parent stem, or None if it has no supplement marker."""
    match = SUPPLEMENT_MARKER.search(stem)
    if not match:
        return None
    base = stem[:match.start()].strip(" ._- ")
    return _filename_key(base) if base else ""


def _filename_key(stem):
    return re.sub(r"[^a-z0-9]+", "", unicodedata.normalize("NFKD", stem).encode(
        "ascii", "ignore").decode().lower())


def find_inbox_supplements(cfg):
    """Return marked supplementary PDFs/CSVs/XLSX files with their batch labels."""
    inbox = config.ROOT / cfg["ingest"].get("inbox_folder", "sources/inbox")
    found = []
    if not inbox.is_dir():
        return found
    for path in sorted(inbox.rglob("*")):
        if not (path.is_file() and path.suffix.lower() in SUPPORTED_SUPPLEMENT_EXTENSIONS):
            continue
        parts = path.relative_to(inbox).parts
        if parts[0] == "batches" and len(parts) > 2:
            label = parts[1]
        elif parts[0] != "batches" and len(parts) > 1:
            label = parts[0]
        else:
            label = "loose"
        if _supplement_base(path.stem) is not None:
            found.append((label, path))
    return found


def _doi_key(value):
    return re.sub(r"^https?://(?:dx\.)?doi\.org/", "", (value or "").strip().lower())


def _file_dois(path):
    """Read DOI candidates from a filename and, when needed, from one PDF or a small text sample."""
    stem = path.stem
    marker = SUPPLEMENT_MARKER.search(stem)
    if marker:
        stem = stem[:marker.start()].strip(" ._- ")
    candidates = set(extract.DOI_RE.findall(stem))
    if path.suffix.lower() == ".pdf":
        try:
            ex = extract.extract(path, 1000, 2000, ocr={"enabled": False})  # DOI sniffing only
            candidates.update(extract.doi_candidates(ex))
        except Exception:
            pass
    elif path.suffix.lower() in {".csv", ".xlsx", ".xlsm"}:
        try:
            if path.suffix.lower() == ".csv":
                sample = path.read_bytes()[:1 << 20].decode("utf-8", errors="ignore")
                candidates.update(extract.DOI_RE.findall(sample))
        except OSError:
            pass
    return {_doi_key(doi) for doi in candidates if _doi_key(doi)}


def _supplement_parent(con, label, supplement, batch_papers):
    """Resolve by DOI first, then by an exact normalized filename stem in the same batch/library."""
    for doi in _file_dois(supplement):
        row = con.execute("SELECT paper_id FROM papers WHERE lower(doi) = ?", (doi,)).fetchone()
        if row:
            return row["paper_id"], "DOI"

    base = _supplement_base(supplement.stem)
    if base is None or not base:
        return None, None
    same_batch = batch_papers.get((label, base), set())
    if len(same_batch) == 1:
        return next(iter(same_batch)), "filename + batch"
    if len(same_batch) > 1:
        return None, None
    rows = con.execute("SELECT paper_id, original_filename FROM source_files WHERE role = 'main'").fetchall()
    matches = {row["paper_id"] for row in rows
               if _filename_key(Path(row["original_filename"] or "").stem) == base}
    if len(matches) == 1:
        return next(iter(matches)), "filename"
    return None, None


def _stored_supplement_verified(paper_id, sha):
    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json") or {}
    for source in meta.get("source_files", []):
        if source.get("role") != "supplement" or source.get("sha256") != sha or not source.get("path"):
            continue
        path = Path(source["path"])
        path = path if path.is_absolute() else config.ROOT / path
        return path.is_file() and extract.sha256_file(path) == sha
    return False


def make_paper_id(info, sha):
    family = info["authors"][0]["family"] if info.get("authors") else "unknown"
    words = [w for w in extract.normalize_title(info.get("title")).split()
             if w not in STOPWORDS and len(w) > 2][:3]
    suffix = hashlib.sha1((info.get("doi") or sha).encode()).hexdigest()[:4]
    base = f"{_slug(family, 20).replace('_', '')}{info.get('year') or 'nd'}_{'_'.join(words) or 'untitled'}_{suffix}"
    pid, i = base, 2
    while db.paper_dir(pid).exists():
        pid, i = f"{base}{i}", i + 1
    return pid


def resolve_metadata(cfg, ex):
    timeout = cfg["ingest"].get("online_timeout_seconds", 15)
    online = cfg["ingest"].get("online_metadata", True)
    info = {
        "title": ex.title_guess or ex.pdf_metadata.get("title"),
        "authors": [], "year": extract.year_guess(ex), "journal": None,
        "doi": None, "pmid": None, "pmcid": None, "publication_types": [],
        "abstract": None, "abstract_source": None,
        "metadata_source": "pdf_text", "metadata_confidence": "low",
    }
    dois = extract.doi_candidates(ex)
    chosen = None
    if online:
        fallback = None
        for doi in dois[:4]:
            cr = metadata.crossref_by_doi(doi, timeout)
            if not cr or not cr.get("title"):
                continue
            if extract.title_found_in_text(cr["title"], ex.first_pages_text) >= 0.7:
                chosen, info["metadata_confidence"] = cr, "high"
                break
            fallback = fallback or cr
        if not chosen and info["title"]:
            cr = metadata.crossref_by_title(info["title"], timeout)
            if cr and extract.title_found_in_text(cr["title"], ex.first_pages_text) >= 0.7:
                chosen, info["metadata_confidence"] = cr, "medium"
        if not chosen and fallback:
            chosen = fallback  # DOI resolved, but its title was not found in the PDF text
        if chosen:
            for key in ("title", "authors", "year", "journal", "doi"):
                if chosen.get(key):
                    info[key] = chosen[key]
            info["metadata_source"] = "crossref"
            if chosen.get("abstract"):
                info["abstract"], info["abstract_source"] = chosen["abstract"], "Crossref"
            if chosen.get("crossref_type"):
                info["publication_types"] = [chosen["crossref_type"]]
            pm = metadata.pubmed_info(info["doi"], timeout) if info["doi"] else None
            if pm:
                info["pmid"], info["pmcid"] = pm["pmid"], pm["pmcid"]
                if pm["publication_types"]:
                    info["publication_types"] = pm["publication_types"]
                if pm["abstract"]:
                    info["abstract"], info["abstract_source"] = pm["abstract"], "PubMed"
                info["metadata_source"] = "crossref+pubmed"
    if not chosen and dois:
        info["doi"] = dois[0]  # found in the PDF, not verified online

    if not info["abstract"]:
        parts = [p["text"] for p in ex.passages if p["section"] == "abstract"]
        if parts:
            info["abstract"], info["abstract_source"] = " ".join(parts)[:4000], "PDF text"
    return info


def find_existing(con, info):
    """Duplicate check in the order DOI, PMID, normalized title + year."""
    if info.get("doi"):
        row = con.execute("SELECT paper_id FROM papers WHERE lower(doi) = lower(?)", (info["doi"],)).fetchone()
        if row:
            return row["paper_id"], "DOI"
    if info.get("pmid"):
        row = con.execute("SELECT paper_id FROM papers WHERE pmid = ?", (info["pmid"],)).fetchone()
        if row:
            return row["paper_id"], "PMID"
    norm = extract.normalize_title(info.get("title"))
    if len(norm) > 20 and info.get("year"):
        row = con.execute("SELECT paper_id FROM papers WHERE norm_title = ? AND year = ?",
                          (norm, info["year"])).fetchone()
        if row:
            return row["paper_id"], "title + year"
    return None, None


def _move_to_active(cfg, pdf, paper_id):
    dest_dir = config.ROOT / cfg["ingest"].get("active_folder", "sources/active") / paper_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(dest_dir / pdf.name)
    _move(pdf, dest)
    return dest


def _quarantine(pdf, kind, batch_id):
    dest_dir = config.REVIEW_PENDING / kind / batch_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = _unique_path(dest_dir / pdf.name)
    _move(pdf, dest)
    return dest


def _source_record(dest, pdf, sha, size, batch_id, role):
    return {"sha256": sha, "path": config.rel(dest), "original_filename": pdf.name,
            "original_inbox_path": config.rel(pdf), "size_bytes": size, "status": "active",
            "role": role, "batch_id": batch_id, "added_at": config.now()}


def _review(con, kind, detail, vault, paper_id=None, file_path=None):
    item_id = db.add_review_item(con, kind, detail, paper_id, file_path)
    notes.write_review_note(con, item_id, vault)


def _attach_paper_to_vault(con, paper_id, vault):
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if not meta:
        raise FileNotFoundError(f"Missing machine record for {paper_id}")
    meta.setdefault("vaults", [])
    if vault not in meta["vaults"]:
        meta["vaults"].append(vault)
        meta["updated_at"] = config.now()
        db.write_json(folder / "metadata.json", meta)
        db.reload_paper(con, paper_id)
        con.commit()
    notes.write_paper_note(meta, vault)
    notes.write_indexes(con, vault)


def ingest_one(cfg, con, pdf, batch_id, vault):
    """Returns (outcome, paper_id or message)."""
    sha = extract.sha256_file(pdf)
    size = pdf.stat().st_size

    row = con.execute("SELECT paper_id, path, status FROM source_files WHERE sha256 = ?", (sha,)).fetchone()
    if row:
        _attach_paper_to_vault(con, row["paper_id"], vault)
        dest = _quarantine(pdf, "duplicates", batch_id)
        # Preserve the incoming original; exact copies are automatically associated,
        # never another researcher review flag or a second canonical paper.
        meta_path = db.paper_dir(row['paper_id']) / 'metadata.json'
        meta = db.read_json(meta_path)
        meta.setdefault('duplicate_sources', []).append({'sha256': sha, 'path': config.rel(dest),
            'original_filename': pdf.name, 'batch_id': batch_id, 'added_at': config.now()})
        db.write_json(meta_path, meta)
        con.commit()
        config.log(f"DUPLICATE {pdf.name} = {row['paper_id']} -> {config.rel(dest)}")
        return "duplicate", row["paper_id"]

    failure = None
    try:
        if size == 0:
            raise ValueError("empty file (0 bytes)")
        with open(pdf, "rb") as f:
            if b"%PDF" not in f.read(1024):
                raise ValueError("not a PDF file (no PDF header)")
        ex = extract.extract(pdf, cfg["passages"].get("target_chars", 1000),
                             cfg["passages"].get("max_chars", 2000))
    except Exception as err:  # corrupt, encrypted, not really a PDF...
        failure = f"{type(err).__name__}: {err}"
    if failure:
        # Outside the except block: the traceback (whose frames hold the half-open PyMuPDF document) is
        # gone, so collecting releases the file handle and Windows lets the file be moved (WinError 32).
        gc.collect()
        try:
            where = config.rel(_quarantine(pdf, "failed", batch_id))
        except OSError as err:
            where = config.rel(pdf)
            failure += f" (could not move the file to review, it stays in the inbox: {err})"
        _review(con, "failed_pdf", failure, vault, None, where)
        con.commit()
        config.log(f"FAILED {pdf.name}: {failure}")
        return "failed", failure

    info = resolve_metadata(cfg, ex)
    existing, matched_by = find_existing(con, info)
    if existing:
        return _merge(cfg, con, existing, matched_by, pdf, sha, size, batch_id, vault, info)
    return _create(cfg, con, pdf, sha, size, batch_id, vault, info, ex)


def _merge(cfg, con, paper_id, matched_by, pdf, sha, size, batch_id, vault, info):
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    dest = _move_to_active(cfg, pdf, paper_id)
    meta["source_files"].append(_source_record(dest, pdf, sha, size, batch_id, "additional_file"))
    filled = []
    for key in ("doi", "pmid", "pmcid", "journal", "year", "abstract", "abstract_source"):
        if not meta.get(key) and info.get(key):
            meta[key] = info[key]
            filled.append(key)
    if vault not in meta["vaults"]:
        meta["vaults"].append(vault)
    meta["updated_at"] = config.now()
    db.write_json(folder / "metadata.json", meta)
    db.reload_paper(con, paper_id)
    con.commit()
    notes.write_paper_note(meta, vault)
    config.log(f"MERGED {pdf.name} into {paper_id} (matched by {matched_by}; filled {filled})")
    return "merged", f"{paper_id} (matched by {matched_by})"


def _vault_names():
    return [p.name for p in config.VAULTS_DIR.iterdir() if p.is_dir()] if config.VAULTS_DIR.is_dir() else []


def _create(cfg, con, pdf, sha, size, batch_id, vault, info, ex):
    paper_id = make_paper_id(info, sha)
    folder = db.paper_dir(paper_id)
    extract.refine_sections(ex, info.get("abstract"))  # label abstract/introduction without headings
    dest = _move_to_active(cfg, pdf, paper_id)
    try:
        passages = []
        for p in ex.passages:
            passages.append({"passage_id": f"{paper_id}:{p['seq']:04d}", **p})
        db.write_jsonl(folder / "pages.jsonl", ex.pages)
        db.write_json(folder / "sections.json", ex.sections)
        db.write_jsonl(folder / "passages.jsonl", passages)
        meta = {
            "paper_id": paper_id,
            **{k: info[k] for k in ("title", "authors", "year", "journal", "doi", "pmid", "pmcid",
                                    "publication_types", "abstract", "abstract_source",
                                    "metadata_source", "metadata_confidence")},
            "first_author": info["authors"][0]["family"] if info.get("authors") else None,
            "norm_title": extract.normalize_title(info.get("title")),
            "n_pages": ex.n_pages,
            "n_passages": len(passages),
            "text_quality": ex.text_quality, "ocr_pages": ex.ocr_pages,
            "classification": "UNREVIEWED",
            "analysis_status": "pending",
            "evidence_type": None,
            "summary": None,
            "vaults": [vault],
            "source_status": "active",
            "source_files": [_source_record(dest, pdf, sha, size, batch_id, "main")],
            "batch_id": batch_id,
            "ingested_at": config.now(),
            "updated_at": config.now(),
            "provenance": {"app_version": __version__, "extractor": f"PyMuPDF {pymupdf.__version__}",
                           "pdf_metadata": ex.pdf_metadata},
        }
        name = notes.choose_note_name(meta)
        # one note name per paper in every vault: avoid a name already used in any vault or by another paper
        if any((config.VAULTS_DIR / v / "Papers" / f"{name}.md").exists() for v in _vault_names()) \
                or con.execute("SELECT 1 FROM papers WHERE lower(note_name) = lower(?)", (name,)).fetchone():
            name = f"{name} ({paper_id[-4:]})"
        meta["note_name"] = name
        db.write_json(folder / "metadata.json", meta)
        db.load_paper(con, paper_id)
        con.commit()
    except Exception:
        con.rollback()
        _move(dest, pdf)  # put the original back where it was
        shutil.rmtree(folder, ignore_errors=True)
        raise

    try:  # v2 Tier 1: sentences, references, citations, figures, tables
        tier1.build(con, paper_id, cfg, ex=ex, pdf_path=dest)
        con.commit()
    except Exception as err:  # the paper itself is kept; Tier 1 can be rebuilt with `kb.py reextract`
        con.rollback()
        db.reload_paper(con, paper_id)
        _review(con, "tier1_failed", f"{type(err).__name__}: {err}", vault, paper_id)
        con.commit()
        config.log(f"TIER1 FAILED {paper_id}: {type(err).__name__}: {err}")
    notes.write_paper_note(meta, vault)
    if ex.text_quality == "no_text":
        _review(con, "needs_ocr", f"{ex.n_pages} pages, {ex.total_chars} characters extracted.", vault, paper_id)
    elif ex.text_quality == "low_text":
        _review(con, "low_text", f"{ex.n_pages} pages, {ex.total_chars} characters extracted.", vault, paper_id)
    if ex.ocr_pages:
        low = sum(p["low_confidence_lines"] for p in ex.ocr_pages)
        _review(con, "ocr_used", f"OCR text on pages {[p['page'] for p in ex.ocr_pages]} "
                f"({ex.ocr_pages[0]['engine']}); {low} low-confidence lines. Check numbers against the PDF.",
                vault, paper_id)
    if info["metadata_confidence"] == "low":
        _review(con, "metadata_uncertain",
                f"Source: {info['metadata_source']}. DOI candidates in PDF: {extract.doi_candidates(ex)[:3] or 'none'}. "
                f"Title used: {info.get('title')!r}", vault, paper_id)
    con.commit()
    config.log(f"NEW {pdf.name} -> {paper_id} ({len(passages)} passages, metadata {info['metadata_confidence']})")
    return "new", paper_id


def run(cfg, con, vault=None, say=print):
    vault = vault or config.default_vault(cfg)
    if not vault or not config.vault_dir(vault).is_dir():
        raise FileNotFoundError(
            f"Vault '{vault or '(none)'}' does not exist. Create/select a vault on the Library page first.")
    unpack_zips(cfg, say)
    supplements = find_inbox_supplements(cfg)
    supplement_paths = {path.resolve() for _, path in supplements}
    items = [(label, path) for label, path in find_inbox_pdfs(cfg) if path.resolve() not in supplement_paths]
    inbox = config.ROOT / cfg["ingest"].get("inbox_folder", "sources/inbox")
    ignored = [p for p in sorted(inbox.rglob("*")) if p.is_file() and p.suffix.lower() not in (".pdf", ".zip")
               and p.resolve() not in supplement_paths and p.name.lower() not in ("readme.txt", "desktop.ini")
               and not {"discovered", "refmanager"} & set(p.parts)]
    if ignored:
        say(f"Bỏ qua {len(ignored)} tệp không phải PDF (giữ nguyên trong inbox): "
            + ", ".join(config.rel(p) for p in ignored[:10]) + (" …" if len(ignored) > 10 else ""))
    if not items and not supplements:
        say("Không có PDF mới trong sources/inbox/. Hãy chép PDF vào sources/inbox/batches/<tên đợt>/ rồi chạy lại.")
        return {}
    today = datetime.now().strftime("%Y-%m-%d")
    totals = {"new": 0, "merged": 0, "duplicate": 0, "failed": 0,
              "supplements_attached": 0, "supplements_unmatched": 0}
    say(f"Tìm thấy {len(items)} PDF bài chính và {len(supplements)} tệp supplementary. "
        f"Xử lý từng tệp một (vault: {vault}).\n")
    labels = sorted({label for label, _ in items} | {label for label, _ in supplements})
    n = 0
    for label in labels:
        batch_id = f"{today}_{_slug(label)}"
        batch_files = [p for lb, p in items if lb == label]
        batch_supplements = [p for lb, p in supplements if lb == label]
        counts = {"new": 0, "merged": 0, "duplicate": 0, "failed": 0}
        con.execute("INSERT OR IGNORE INTO batches (batch_id, folder, started_at) VALUES (?,?,?)",
                    (batch_id, label, config.now()))
        con.commit()
        manifest = []
        batch_papers = {}
        for pdf in batch_files:
            n += 1
            say(f"[{n}/{len(items) + len(supplements)}] {pdf.name}")
            try:
                sha = extract.sha256_file(pdf)
                outcome, detail = ingest_one(cfg, con, pdf, batch_id, vault)
                row = con.execute("SELECT paper_id FROM source_files WHERE sha256 = ?", (sha,)).fetchone()
                if row:
                    batch_papers.setdefault((label, _filename_key(pdf.stem)), set()).add(row["paper_id"])
            except Exception as err:
                outcome, detail = "failed", f"{type(err).__name__}: {err}"
                config.log(f"ERROR {pdf}: {detail}")
            counts[outcome] += 1
            manifest.append({"file": pdf.name, "outcome": outcome, "detail": detail})
            say(f"      -> {LABELS[outcome]}: {detail}")

        for supp in batch_supplements:
            n += 1
            say(f"[{n}/{len(items) + len(supplements)}] supplementary: {supp.name}")
            try:
                paper_id, matched_by = _supplement_parent(con, label, supp, batch_papers)
                if not paper_id:
                    outcome = "supplement_unmatched"
                    totals["supplements_unmatched"] += 1
                    detail = "Không xác định được duy nhất bài chính; giữ nguyên trong inbox để gắn thủ công."
                    config.log(f"UNMATCHED SUPPLEMENT {supp.name}: left in inbox")
                else:
                    supp_sha = extract.sha256_file(supp)
                    detail = tier1.attach_supplement(con, cfg, paper_id, supp)
                    con.commit()
                    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json") or {}
                    if vault in meta.get("vaults", []):
                        notes.write_paper_note(meta, vault, con=con)
                    # Consume the staged copy only after the durable copy is independently verified.
                    if supp.is_file() and _stored_supplement_verified(paper_id, supp_sha):
                        supp.unlink()
                    elif supp.is_file():
                        detail += "; source remains in inbox because the stored copy could not be verified"
                    outcome = "supplement_attached"
                    totals["supplements_attached"] += 1
                    detail = f"{detail} (matched by {matched_by})"
                    config.log(f"ATTACHED SUPPLEMENT {supp.name} -> {paper_id} ({matched_by})")
            except Exception as err:
                con.rollback()
                outcome = "supplement_failed"
                detail = f"{type(err).__name__}: {err}; file left in inbox"
                config.log(f"SUPPLEMENT FAILED {supp}: {detail}")
            manifest.append({"file": supp.name, "outcome": outcome, "detail": detail})
            say(f"      -> {detail}")
        con.execute(
            """UPDATE batches SET finished_at = ?, n_files = COALESCE(n_files, 0) + ?,
               n_new = COALESCE(n_new, 0) + ?, n_merged = COALESCE(n_merged, 0) + ?,
               n_duplicate = COALESCE(n_duplicate, 0) + ?, n_failed = COALESCE(n_failed, 0) + ?
               WHERE batch_id = ?""",
            (config.now(), len(batch_files) + len(batch_supplements), counts["new"], counts["merged"], counts["duplicate"],
             counts["failed"], batch_id))
        con.commit()
        manifest_path = config.LOGS_DIR / "batches" / f"{batch_id}.yaml"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(manifest_path, "a", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump([{"batch_id": batch_id, "folder": label, "target_vault": vault,
                             "run_at": config.now(), "files": manifest}],
                           f, sort_keys=False, allow_unicode=True)
        for k in ("new", "merged", "duplicate", "failed"):
            totals[k] += counts[k]
    notes.write_indexes(con, vault)
    return totals


LABELS = {"new": "bài mới", "merged": "đã có sẵn, gộp thêm tệp", "duplicate": "tệp trùng, đã gộp; giữ bản nguồn",
          "failed": "lỗi, chuyển sang review"}
