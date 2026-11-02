"""Read-only import of Zotero, Mendeley, and EndNote bibliographic records."""
import hashlib
import json
import re
import shutil
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

from . import config, db, extract, ingest


def _doi(value):
    value = str(value or "").strip().lower()
    value = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", value)
    return value.rstrip(" .,;}") or None


def _year(value):
    match = re.search(r"\b(18|19|20|21)\d{2}\b", str(value or ""))
    return int(match.group()) if match else None


def _authors(value):
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"\s+and\s+|\s*;\s*", value, flags=re.I) if part.strip()]
    if isinstance(value, list):
        names = [(" ".join(str(x) for x in (a.get("given", ""), a.get("family", "")) if x).strip()
                  or str(a.get("literal") or "").strip()) if isinstance(a, dict) else str(a).strip()
                 for a in value if a]
        return [n for n in names if n]
    return []


def _author(name):
    """Library metadata stores authors as {name, family}; "Smith, John" and "John Smith" both work."""
    family, _, given = name.partition(",") if "," in name else (name.rsplit(" ", 1)[-1], "", "")
    display = f"{given.strip()} {family.strip()}".strip() if "," in name else name
    return {"name": display, "family": family.strip()}


def _record(item, source, manager, attachment=None, item_key=None):
    title = " ".join(str(item.get("title") or "").split()) or None
    date = item.get("issued") or item.get("date") or item.get("year")
    if isinstance(date, dict):
        date = "-".join(str(x) for x in (date.get("date-parts") or [[None]])[0] if x)
    doi = _doi(item.get("DOI") or item.get("doi"))
    pmid = re.search(r"\b\d{6,9}\b", str(item.get("PMID") or item.get("pmid") or ""))
    return {"title": title, "doi": doi, "pmid": pmid.group() if pmid else None,
            "year": _year(date), "authors": _authors(item.get("author") or item.get("authors")),
            "journal": item.get("container-title") or item.get("journal") or item.get("publicationTitle"),
            "manager": manager, "item_key": str(item_key or item.get("id") or item.get("key") or "") or None,
            "attachment_path": str(attachment) if attachment else None, "source": source}


def parse_csl(path):
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        raise ValueError("CSL JSON must be an object or array")
    out, errors = [], []
    for i, item in enumerate(data, 1):
        if not isinstance(item, dict) or not (item.get("title") or item.get("DOI") or item.get("PMID")):
            errors.append({"record": i, "error": "record needs a title, DOI, or PMID"})
        else:
            out.append(_record(item, str(path), "CSL JSON"))
    return out, errors


def _bib_value(body, start, opening):
    """Value after `field = {` / `"`, honouring nested {braces} (Zotero writes title = {{Muse} cells})."""
    depth, i = 1, start
    while i < len(body):
        c = body[i]
        if c == "{":
            depth += 1
        elif c == "}" and (depth := depth - 1) == 0 and opening == "{":
            break
        elif c == '"' and opening == '"' and depth == 1:
            break
        i += 1
    return re.sub(r"\s+", " ", body[start:i].replace("{", "").replace("}", "")).strip()


def parse_bib(path):
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    chunks = re.split(r"@\w+\s*\{", text)
    out, errors = [], []
    for i, chunk in enumerate(chunks[1:], 1):
        body = chunk.rsplit("}", 1)[0]
        key = body.split(",", 1)[0].strip()
        fields = {}
        for m in re.finditer(r"(?is)(?:^|,)\s*([\w-]+)\s*=\s*(\{|\"|\d+)", body):
            name, opening = m.group(1).lower(), m.group(2)
            if name in fields:
                continue
            fields[name] = opening if opening[0].isdigit() else _bib_value(body, m.end(), opening)
        rec = _record({"title": fields.get("title"), "DOI": fields.get("doi"),
                       "PMID": fields.get("pmid"), "year": fields.get("year"),
                       "author": fields.get("author"), "journal": fields.get("journal")},
                      str(path), "BibTeX", item_key=key)
        if rec["title"] or rec["doi"] or rec["pmid"]:
            out.append(rec)
        else:
            errors.append({"record": i, "error": "record needs a title, DOI, or PMID"})
    return out, errors


def parse_ris(path):
    out, errors, row, start = [], [], {}, 0
    for line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines() + ["ER  - "]:
        m = re.match(r"^([A-Z0-9]{2})  -\s?(.*)$", line)
        if not m:
            continue
        tag, value = m.groups()
        if tag == "TY":
            if row:
                errors.append({"record": start, "error": "record missing ER terminator"})
            row, start = {"TY": value}, start + 1
        elif tag == "ER":
            if row:
                rec = _record({"title": row.get("TI") or row.get("T1"), "DOI": row.get("DO"),
                               "PMID": row.get("PM"), "year": row.get("PY") or row.get("Y1"),
                               "author": row.get("AU", []), "journal": row.get("JO") or row.get("JF")},
                              str(path), "RIS", item_key=row.get("ID"))
                (out if rec["title"] or rec["doi"] or rec["pmid"] else errors).append(
                    rec if rec["title"] or rec["doi"] or rec["pmid"] else {"record": start, "error": "record needs a title, DOI, or PMID"})
            row = {}
        elif tag != "TY":
            if tag in ("AU", "A1"):
                row.setdefault(tag, []).append(value)
            else:
                row[tag] = value
    return out, errors


def parse_zotero(path):
    tmp = tempfile.NamedTemporaryFile(prefix="kb-zotero-", suffix=".sqlite", delete=False)
    tmp.close()
    try:
        shutil.copy2(path, tmp.name)
        con = sqlite3.connect(f"file:{Path(tmp.name).as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            data, errors = [], []
            query = """SELECT i.itemID,i.key,i.itemTypeID,f.fieldName,v.value
                       FROM items i LEFT JOIN itemData d ON d.itemID=i.itemID
                       LEFT JOIN fields f ON f.fieldID=d.fieldID LEFT JOIN itemDataValues v ON v.valueID=d.valueID
                       WHERE i.itemID NOT IN (SELECT itemID FROM deletedItems) ORDER BY i.itemID"""
            grouped = {}
            for row in con.execute(query):
                grouped.setdefault(row["itemID"], {"key": row["key"], "fields": {}})
                if row["fieldName"]:
                    grouped[row["itemID"]]["fields"][row["fieldName"]] = row["value"]
            for itemid, item in grouped.items():
                f = item["fields"]
                if not f:
                    continue
                doi = f.get("DOI") or f.get("doi")
                att = None
                attachments = con.execute("SELECT path FROM itemAttachments WHERE parentItemID=?", (itemid,)).fetchall()
                if attachments:
                    att = attachments[0]["path"]
                    if att and att.startswith("storage:"):
                        att = str(Path(path).parent / "storage" / att.split(":", 1)[1])
                rec = _record({"title": f.get("title"), "DOI": doi, "PMID": f.get("PMID"),
                               "year": f.get("date"), "journal": f.get("publicationTitle")},
                              str(path), "Zotero", att, item["key"])
                if rec["title"] or rec["doi"] or rec["pmid"]:
                    data.append(rec)
                else:
                    errors.append({"record": item["key"], "error": "record needs a title, DOI, or PMID"})
            return data, errors
        finally:
            con.close()
    finally:
        Path(tmp.name).unlink(missing_ok=True)


def _parse(path):
    suffix = path.suffix.lower()
    if suffix == ".json":
        return parse_csl(path)
    if suffix == ".bib":
        return parse_bib(path)
    if suffix == ".ris":
        return parse_ris(path)
    if suffix in (".sqlite", ".sqlite3", ".db"):
        return parse_zotero(path)
    raise ValueError("supported inputs: CSL JSON, .bib, .ris, or Zotero .sqlite")


def import_refs(path, con, vault=None, dry_run=False):
    path = Path(path)
    records, errors = _parse(path)
    report = {"records": len(records), "matched": [], "candidates": [], "malformed": errors,
              "attachments": [], "dry_run": dry_run}
    planned = []
    for rec in records:
        paper_id, method = ingest.find_existing(con, rec)
        if paper_id:
            meta = db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}
            provenance = {k: rec.get(k) for k in ("manager", "item_key", "attachment_path") if rec.get(k)}
            report["matched"].append({"paper_id": paper_id, "match": method, "item": rec["item_key"]})
            planned.append((paper_id, rec, meta, provenance))
        else:
            key = rec.get("doi") or rec.get("pmid") or (extract.normalize_title(rec.get("title")) + str(rec.get("year")))
            cid = hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]
            report["candidates"].append({"candidate_id": cid, "title": rec.get("title")})
            planned.append((None, rec, None, {"candidate_id": cid}))
        attachment = rec.get("attachment_path")
        if attachment:
            candidate = Path(attachment)
            if not candidate.is_absolute() and rec["manager"] == "Zotero":
                candidate = path.parent / candidate
            if candidate.is_file():
                report["attachments"].append(str(candidate))
    if dry_run:
        return report
    changed = set()
    for paper_id, rec, meta, provenance in planned:
        if paper_id:
            entries = meta.setdefault("reference_managers", [])
            if not any(e.get("manager") == provenance.get("manager") and e.get("item_key") == provenance.get("item_key") for e in entries):
                entries.append(provenance)
            for field in ("doi", "pmid", "title", "year", "journal", "authors"):
                if (not meta.get(field)) and rec.get(field):
                    meta[field] = [_author(a) for a in rec[field]] if field == "authors" else rec[field]
            if not meta.get("first_author") and meta.get("authors"):
                meta["first_author"] = meta["authors"][0].get("family")
            db.write_json(db.paper_dir(paper_id) / "metadata.json", meta)
            changed.add(paper_id)
        else:
            folder = config.ROOT / "sources" / "inbox" / "refmanager"
            folder.mkdir(parents=True, exist_ok=True)
            candidate_id = provenance["candidate_id"]
            dest = folder / f"{candidate_id}.json"
            if dest.exists():
                continue
            db.write_json(dest, {"schema_version": 1, "status": "metadata_only_waiting_for_researcher",
                                 "source": "reference_manager", "record": rec,
                                 "staged_at": datetime.now().astimezone().isoformat(timespec="seconds")})
    for paper_id in sorted(changed):
        db.reload_paper(con, paper_id)
    con.commit()
    return report


def _selfcheck():
    import tempfile
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        csl = root / "fixture.json"
        csl.write_text(json.dumps([{"id": "doi-match", "title": "DOI match", "DOI": "10.1/a"},
                                   {"id": "title-match", "title": "A unique scientific title for year match", "issued": {"date-parts": [[2020]]}},
                                   {"id": "new", "title": "A new candidate"}, {"id": "bad"}]), encoding="utf-8")
        parsed, errors = parse_csl(csl)
        assert len(parsed) == 3 and len(errors) == 1
        bib = root / "fixture.bib"
        bib.write_text('@article{doi, title={DOI match}, doi={10.1/a}}\n@article{title, title={Title year match}, year={2020}}\n@article{new, title={New candidate}}\n@article{bad, author={Nobody}}', encoding="utf-8")
        parsed, errors = parse_bib(bib)
        assert len(parsed) == 3 and len(errors) == 1
        bib.write_text('@article{k, title = {{Muse} cells in {AMI}}, year = 2016, author = {Lindqvist, Mari and Marlow, Y.}}',
                       encoding="utf-8")
        parsed, _ = parse_bib(bib)
        assert parsed[0]["title"] == "Muse cells in AMI" and parsed[0]["year"] == 2016, parsed
        assert [_author(a) for a in parsed[0]["authors"]][0] == {"name": "Mari Lindqvist", "family": "Lindqvist"}
        assert _author("Mari Lindqvist") == {"name": "Mari Lindqvist", "family": "Lindqvist"}
        ris = root / "fixture.ris"
        ris.write_text("TY  - JOUR\nTI  - DOI match\nDO  - 10.1/a\nER  -\nTY  - JOUR\nTI  - Title year match\nPY  - 2020\nER  -\nTY  - JOUR\nTI  - New candidate\nER  -\nTY  - JOUR\nER  -", encoding="utf-8")
        parsed, errors = parse_ris(ris)
        assert len(parsed) == 3 and len(errors) == 1
        zot = root / "zotero.sqlite"
        z = sqlite3.connect(zot)
        z.executescript("CREATE TABLE items(itemID INTEGER,key TEXT,itemTypeID INTEGER); CREATE TABLE deletedItems(itemID INTEGER); CREATE TABLE fields(fieldID INTEGER,fieldName TEXT); CREATE TABLE itemData(itemID INTEGER,fieldID INTEGER,valueID INTEGER); CREATE TABLE itemDataValues(valueID INTEGER,value TEXT); CREATE TABLE itemAttachments(itemID INTEGER,parentItemID INTEGER,path TEXT);")
        z.execute("INSERT INTO items VALUES(1,'ZKEY',1)")
        z.executemany("INSERT INTO fields VALUES(?,?)", [(1,"title"),(2,"DOI"),(3,"date")])
        z.executemany("INSERT INTO itemDataValues VALUES(?,?)", [(1,"DOI match"),(2,"10.1/a"),(3,"2020")])
        z.executemany("INSERT INTO itemData VALUES(?,?,?)", [(1,1,1),(1,2,2),(1,3,3)])
        z.commit(); z.close()
        parsed, errors = parse_zotero(zot)
        assert len(parsed) == 1 and not errors and parsed[0]["item_key"] == "ZKEY"
        # Verify metadata enrichment is reloaded into the live SQLite papers table.
        metadata = root / "papers" / "local-doi-paper" / "metadata.json"
        metadata.parent.mkdir(parents=True)
        metadata.write_text(json.dumps({"paper_id": "local-doi-paper", "title": "A unique scientific title for year match",
                                        "doi": None, "pmid": None, "year": 2020, "authors": [],
                                        "vaults": []}), encoding="utf-8")
        (root / "concepts.jsonl").write_text("", encoding="utf-8")
        for filename, content in (("sections.json", "[]"), ("analysis.json", "{}"),
                                  ("passages.jsonl", ""), ("sentences.jsonl", ""),
                                  ("references.jsonl", ""), ("figures.jsonl", ""),
                                  ("tables.jsonl", ""), ("evidence.jsonl", "")):
            (metadata.parent / filename).write_text(content, encoding="utf-8")
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        con.execute("INSERT INTO papers(paper_id,doi,pmid,norm_title,year,title,authors,publication_types) "
                    "VALUES(?,?,?,?,?,?,?,?)", ("local-doi-paper", None, None,
                    extract.normalize_title("A unique scientific title for year match"), 2020, "A unique scientific title for year match", "[]", "[]"))
        con.execute("INSERT INTO paper_terms VALUES(?,?,?)", ("local-doi-paper", "topics", "Cell therapy"))
        con.commit()
        def metadata_path(_paper_id):
            return metadata.parent
        with patch.object(config, "ROOT", root), patch.object(config, "STORE", root), \
                patch.object(config, "PAPERS_DIR", root / "papers"), patch.object(db, "paper_dir", metadata_path):
            imported = root / "import.json"
            imported.write_text(json.dumps([{"id": "new-doi", "title": "A unique scientific title for year match",
                                               "DOI": "10.1234/filled-doi", "issued": {"date-parts": [[2020]]}}]),
                                 encoding="utf-8")
            report = import_refs(imported, con)
            assert report["matched"][0]["paper_id"] == "local-doi-paper"
            assert con.execute("SELECT doi FROM papers WHERE paper_id='local-doi-paper'").fetchone()[0] == "10.1234/filled-doi"
        con.close()
    return 14
