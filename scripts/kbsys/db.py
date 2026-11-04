"""SQLite catalogue (knowledge_store/library.db).

The per-paper files in knowledge_store/papers/<paper_id>/ are the durable
record. The database is a fast index built from them and can be rebuilt at any
time with `kb.py rebuild-db` (review items and batch history live only here).
"""

import json
import os
import sqlite3

from . import config

# CHR-04 (D-PENDING-14): U+2206 ∆ and U+2103 ℃ are token characters, so "∆G" is the token "∆g", not "g". A table keeps
# the tokenizer it was created with, so its stored CREATE statement is the version marker (fts_glyph_tokens); only
# rebuild() (`kb.py rebuild-db`) recreates the tables.
FTS_TOKENIZE = "tokenize=\"porter unicode61 remove_diacritics 2 tokenchars '∆℃'\""
FTS_TABLES = ("passages_fts", "sentences_fts", "evidence_fts")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS papers (
    paper_id TEXT PRIMARY KEY,
    title TEXT,
    authors TEXT,               -- JSON list
    first_author TEXT,
    year INTEGER,
    journal TEXT,
    doi TEXT,
    pmid TEXT,
    pmcid TEXT,
    publication_types TEXT,     -- JSON list (from PubMed / Crossref)
    abstract TEXT,
    abstract_source TEXT,
    norm_title TEXT,
    n_pages INTEGER,
    n_passages INTEGER,
    text_quality TEXT,          -- ok | low_text | no_text
    metadata_source TEXT,
    metadata_confidence TEXT,   -- high | medium | low
    classification TEXT,        -- UNREVIEWED | CORE | CONNECTED | POTENTIAL_BRIDGE | UNCERTAIN | OUT_OF_SCOPE
    analysis_status TEXT,       -- pending | done
    evidence_type TEXT,
    summary TEXT,
    note_name TEXT,
    batch_id TEXT,
    ingested_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS papers_doi ON papers(doi);
CREATE INDEX IF NOT EXISTS papers_pmid ON papers(pmid);
CREATE INDEX IF NOT EXISTS papers_title ON papers(norm_title, year);

CREATE TABLE IF NOT EXISTS source_files (
    sha256 TEXT PRIMARY KEY,
    paper_id TEXT,
    path TEXT,                  -- relative to project root
    original_filename TEXT,
    size_bytes INTEGER,
    status TEXT,                -- active | archived | external | unavailable
    role TEXT,                  -- main | alternate_version
    batch_id TEXT,
    added_at TEXT
);
CREATE INDEX IF NOT EXISTS source_files_paper ON source_files(paper_id);

CREATE TABLE IF NOT EXISTS paper_vaults (
    paper_id TEXT,
    vault TEXT,
    PRIMARY KEY (paper_id, vault)
);

CREATE TABLE IF NOT EXISTS sections (
    paper_id TEXT,
    seq INTEGER,
    name TEXT,
    heading TEXT,
    page_start INTEGER,
    PRIMARY KEY (paper_id, seq)
);

CREATE TABLE IF NOT EXISTS passages (
    passage_id TEXT PRIMARY KEY,
    paper_id TEXT,
    seq INTEGER,
    page INTEGER,
    section TEXT,
    kind TEXT,                  -- text | caption
    text TEXT
);
CREATE INDEX IF NOT EXISTS passages_paper ON passages(paper_id, seq);

CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
    text, content='passages', content_rowid='rowid',
    {FTS_TOKENIZE}
);
CREATE TRIGGER IF NOT EXISTS passages_ai AFTER INSERT ON passages BEGIN
    INSERT INTO passages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS passages_ad AFTER DELETE ON passages BEGIN
    INSERT INTO passages_fts(passages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TABLE IF NOT EXISTS claims (
    claim_id TEXT PRIMARY KEY,
    paper_id TEXT,
    seq INTEGER,
    statement TEXT,
    claim_type TEXT,            -- result | discussion | hypothesis | background | method
    quote TEXT,
    passage_id TEXT,
    page INTEGER,
    section TEXT
);
CREATE INDEX IF NOT EXISTS claims_paper ON claims(paper_id);

CREATE TABLE IF NOT EXISTS paper_terms (
    paper_id TEXT,
    term_type TEXT,             -- topics | concepts | methods | species | molecules | cell_types
    term TEXT,
    PRIMARY KEY (paper_id, term_type, term)
);
CREATE INDEX IF NOT EXISTS paper_terms_term ON paper_terms(term);

CREATE TABLE IF NOT EXISTS concept_links (
    concept_id TEXT,
    paper_id TEXT,
    level TEXT,                -- studied | mentioned
    n_sentences INTEGER,
    sections TEXT,             -- JSON list
    first_sentence_id TEXT,
    PRIMARY KEY (concept_id, paper_id)
);
CREATE INDEX IF NOT EXISTS concept_links_paper ON concept_links(paper_id);
CREATE INDEX IF NOT EXISTS concept_links_concept ON concept_links(concept_id);

CREATE TABLE IF NOT EXISTS review_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_id TEXT,
    kind TEXT,
    detail TEXT,
    file_path TEXT,
    status TEXT DEFAULT 'pending',
    note_name TEXT,
    created_at TEXT,
    resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id TEXT PRIMARY KEY,
    folder TEXT,
    started_at TEXT,
    finished_at TEXT,
    n_files INTEGER,
    n_new INTEGER,
    n_merged INTEGER,
    n_duplicate INTEGER,
    n_failed INTEGER
);

-- v2 Tier 1 (docs/CONTRACTS.md §2.6); rebuilt from the per-paper JSONL files
CREATE TABLE IF NOT EXISTS sentences (
    sentence_id TEXT PRIMARY KEY,
    paper_id TEXT,
    seq INTEGER,
    passage_id TEXT,
    source TEXT,
    page INTEGER,
    section TEXT,
    subsection TEXT,
    kind TEXT,                  -- text | caption | table_row
    text TEXT
);
CREATE INDEX IF NOT EXISTS sentences_paper ON sentences(paper_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS sentences_fts USING fts5(
    text, content='sentences', content_rowid='rowid',
    {FTS_TOKENIZE}
);
CREATE TRIGGER IF NOT EXISTS sentences_ai AFTER INSERT ON sentences BEGIN
    INSERT INTO sentences_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS sentences_ad AFTER DELETE ON sentences BEGIN
    INSERT INTO sentences_fts(sentences_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;

CREATE TABLE IF NOT EXISTS references_list (
    ref_id TEXT PRIMARY KEY,
    paper_id TEXT,
    num INTEGER,
    raw TEXT,
    first_author TEXT,
    year INTEGER,
    title TEXT,
    doi TEXT,
    pmid TEXT,
    parse_confidence TEXT,
    resolved_paper_id TEXT,
    ghost_id TEXT
);
CREATE INDEX IF NOT EXISTS references_paper ON references_list(paper_id, num);
CREATE INDEX IF NOT EXISTS references_doi ON references_list(doi);

CREATE TABLE IF NOT EXISTS citations (
    sentence_id TEXT,
    paper_id TEXT,
    ref_id TEXT,
    style TEXT,
    confidence TEXT
);
CREATE INDEX IF NOT EXISTS citations_sentence ON citations(sentence_id);
CREATE INDEX IF NOT EXISTS citations_ref ON citations(ref_id);

CREATE TABLE IF NOT EXISTS figures_tables (
    object_id TEXT PRIMARY KEY,
    paper_id TEXT,
    label TEXT,
    kind TEXT,                  -- figure | table
    page INTEGER,
    source TEXT,
    crop TEXT
);

-- v2 Tier 2 evidence units (docs/CONTRACTS.md §3); rebuilt from evidence.jsonl
CREATE TABLE IF NOT EXISTS evidence (
    unit_id TEXT PRIMARY KEY,
    paper_id TEXT,
    seq INTEGER,
    unit_type TEXT,
    status TEXT,                -- ok | flagged | human_confirmed
    statement TEXT,
    quote TEXT,
    sentence_ids TEXT,          -- JSON list
    pages TEXT,                 -- JSON list
    sections TEXT,              -- JSON list
    species TEXT,
    cell_type TEXT,
    agent TEXT,
    measure TEXT,
    direction TEXT,
    value_source TEXT,
    hedging TEXT,
    analyst TEXT
);
CREATE INDEX IF NOT EXISTS evidence_paper ON evidence(paper_id, seq);
CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
    statement, quote, content='evidence', content_rowid='rowid',
    {FTS_TOKENIZE}
);
CREATE TRIGGER IF NOT EXISTS evidence_ai AFTER INSERT ON evidence BEGIN
    INSERT INTO evidence_fts(rowid, statement, quote) VALUES (new.rowid, new.statement, new.quote);
END;
CREATE TRIGGER IF NOT EXISTS evidence_ad AFTER DELETE ON evidence BEGIN
    INSERT INTO evidence_fts(evidence_fts, rowid, statement, quote) VALUES ('delete', old.rowid, old.statement, old.quote);
END;
"""

PAPER_COLUMNS = [
    "paper_id", "title", "authors", "first_author", "year", "journal", "doi", "pmid",
    "pmcid", "publication_types", "abstract", "abstract_source", "norm_title", "n_pages",
    "n_passages", "text_quality", "metadata_source", "metadata_confidence",
    "classification", "analysis_status", "evidence_type", "summary", "note_name",
    "batch_id", "ingested_at", "updated_at",
]


def connect():
    config.STORE.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def fts_glyph_tokens(con, tables=FTS_TABLES):
    """Whether every table was created with FTS_TOKENIZE; an older index drops ∆/℃ until `kb.py rebuild-db`."""
    sql = {r[0]: r[1] for r in con.execute(f"SELECT name, sql FROM sqlite_master WHERE name IN ({','.join('?' * len(tables))})",
                                           tables)}
    return all(FTS_TOKENIZE in (sql.get(t) or "") for t in tables)


def paper_dir(paper_id):
    return config.PAPERS_DIR / paper_id


def read_json(path, default=None):
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def read_jsonl(path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".py"}  # CHR-03: written with LF only


def _parsed(suffix, raw):
    text = raw.decode("utf-8-sig")
    if suffix == ".json":
        return json.loads(text)
    if suffix == ".jsonl":
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    return text.replace("\r\n", "\n")


def store_lf(apply=False):
    """CHR-03: store text files (store root, papers/**, history copies included) that older code wrote with CRLF, as LF.
    A file is rewritten only when CRLF -> LF leaves no CR and its JSON / JSONL content is identical; otherwise it is
    left as it is and reported. Dry run unless apply. Returns {"files", "refused", "applied"}."""
    paths = [p for p in config.STORE.glob("*") if p.is_file()] + \
            ([p for p in config.PAPERS_DIR.rglob("*") if p.is_file()] if config.PAPERS_DIR.is_dir() else [])
    files, refused = [], []
    for path in sorted(paths):
        if path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        raw = path.read_bytes()
        if b"\r" not in raw:
            continue
        new = raw.replace(b"\r\n", b"\n")
        name = path.relative_to(config.STORE).as_posix()
        try:
            same = b"\r" not in new and _parsed(path.suffix.lower(), raw) == _parsed(path.suffix.lower(), new)
        except ValueError as err:  # UnicodeDecodeError and JSONDecodeError included
            same, name = False, f"{name} ({type(err).__name__})"
        if not same:
            refused.append(name)
            continue
        if apply:
            st = path.stat()
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_bytes(new)
            tmp.replace(path)
            os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))  # analysis_runner compares checkpoint/reply mtimes
        files.append(name)
    return {"files": files, "refused": refused, "applied": apply}


# Tables whose rows all belong to one paper (and are rebuilt from its files).
PAPER_TABLES = ("papers", "source_files", "paper_vaults", "sections", "passages", "claims", "paper_terms",
                "concept_links", "sentences", "references_list", "citations", "figures_tables", "evidence")


def delete_paper_rows(con, paper_id):
    for table in PAPER_TABLES:
        con.execute(f"DELETE FROM {table} WHERE paper_id = ?", (paper_id,))


def load_paper(con, paper_id):
    """(Re)load one paper from its files into the database. Caller commits."""
    folder = paper_dir(paper_id)
    meta = read_json(folder / "metadata.json")
    if meta is None:
        raise FileNotFoundError(f"No metadata.json for {paper_id}")
    delete_paper_rows(con, paper_id)

    row = {c: meta.get(c) for c in PAPER_COLUMNS}
    row["authors"] = json.dumps(meta.get("authors") or [], ensure_ascii=False)
    row["publication_types"] = json.dumps(meta.get("publication_types") or [], ensure_ascii=False)
    con.execute(
        f"INSERT INTO papers ({', '.join(PAPER_COLUMNS)}) VALUES ({', '.join('?' * len(PAPER_COLUMNS))})",
        [row[c] for c in PAPER_COLUMNS],
    )
    for src in meta.get("source_files", []):
        con.execute(
            "INSERT OR REPLACE INTO source_files VALUES (?,?,?,?,?,?,?,?,?)",
            (src["sha256"], paper_id, src.get("path"), src.get("original_filename"),
             src.get("size_bytes"), src.get("status"), src.get("role"),
             src.get("batch_id"), src.get("added_at")),
        )
    for vault in meta.get("vaults", []):
        con.execute("INSERT OR IGNORE INTO paper_vaults VALUES (?,?)", (paper_id, vault))

    for i, sec in enumerate(read_json(folder / "sections.json", [])):
        con.execute("INSERT INTO sections VALUES (?,?,?,?,?)",
                    (paper_id, i, sec["name"], sec.get("heading"), sec.get("page_start")))

    con.executemany(
        "INSERT INTO passages VALUES (?,?,?,?,?,?,?)",
        ((p["passage_id"], paper_id, p["seq"], p["page"], p["section"], p["kind"], p["text"])
         for p in read_jsonl(folder / "passages.jsonl")),
    )

    load_tier1(con, paper_id, folder)
    load_evidence(con, paper_id, folder)
    analysis = read_json(folder / "analysis.json")
    if analysis:
        for i, c in enumerate(analysis.get("claims", [])):
            con.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?,?,?,?,?)",
                (f"{paper_id}:c{i + 1:03d}", paper_id, i + 1, c["statement"], c["claim_type"],
                 c["quote"], c["passage_id"], c.get("page"), c.get("section")),
            )
        for term_type in TERM_TYPES:
            for term in analysis.get(term_type, []) or []:
                con.execute("INSERT OR IGNORE INTO paper_terms VALUES (?,?,?)",
                            (paper_id, term_type, term))


def reload_paper(con, paper_id):
    """Reload one paper's indexed rows and restore its derived concept links.

    Use this for every single-paper refresh. Bulk database rebuilds should keep
    using `load_paper` in a loop and rebuild concept links once at the end.
    """
    before_concepts = [r[0] for r in con.execute("SELECT DISTINCT concept_id FROM concept_links WHERE paper_id = ?", (paper_id,))]
    before_terms = [r[0] for r in con.execute("SELECT term FROM paper_terms WHERE paper_id = ?", (paper_id,))]
    load_paper(con, paper_id)  # wipes this paper's links and terms
    from . import concepts
    return concepts.relink_paper(con, paper_id, before_terms, before_concepts)


def load_tier1(con, paper_id, folder):
    """Sentences, references, citations, figures and tables (v2 Tier 1), if built."""
    sentences = list(read_jsonl(folder / "sentences.jsonl"))
    con.executemany(
        "INSERT INTO sentences VALUES (?,?,?,?,?,?,?,?,?,?)",
        ((s["sentence_id"], paper_id, s["seq"], s.get("passage_id"), s.get("source", "main"), s["page"],
          s["section"], s.get("subsection"), s["kind"], s["text"]) for s in sentences))
    con.executemany(
        "INSERT OR REPLACE INTO references_list VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ((r["ref_id"], paper_id, r.get("num"), r["raw"], r.get("first_author"), r.get("year"), r.get("title"),
          r.get("doi"), r.get("pmid"), r.get("parse_confidence"), r.get("resolved_paper_id"), r.get("ghost_id"))
         for r in read_jsonl(folder / "references.jsonl")))
    rows = []
    for s in sentences:
        for c in s.get("citations", []):
            for n in c.get("ref_nums") or []:
                rows.append((s["sentence_id"], paper_id, f"{paper_id}:R{n:03d}", c["style"], c["confidence"]))
    con.executemany("INSERT INTO citations VALUES (?,?,?,?,?)", rows)
    for kind, name, key in (("figure", "figures.jsonl", "figure_id"), ("table", "tables.jsonl", "table_id")):
        con.executemany(
            "INSERT OR REPLACE INTO figures_tables VALUES (?,?,?,?,?,?,?)",
            ((o[key], paper_id, o.get("label"), kind, o.get("page"), o.get("source"), o.get("crop"))
             for o in read_jsonl(folder / name)))


def load_evidence(con, paper_id, folder):
    """Evidence units of a v2 analysis (evidence.jsonl), if any."""
    def text(value):
        return None if value in (None, "") else str(value)
    con.executemany(
        "INSERT OR REPLACE INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ((u["unit_id"], paper_id, u["seq"], u["unit_type"], u["status"], u["statement"], u["quote"],
          json.dumps(u["sentence_ids"]), json.dumps(u.get("pages", [])), json.dumps(u.get("sections", [])),
          text((u.get("model") or {}).get("species")), text((u.get("model") or {}).get("cell_type")),
          text((u.get("intervention") or {}).get("agent")), text((u.get("outcome") or {}).get("measure")),
          text((u.get("outcome") or {}).get("direction")), text((u.get("outcome") or {}).get("value_source")),
          u.get("hedging"), u.get("analyst"))
         for u in read_jsonl(folder / "evidence.jsonl")))


TERM_TYPES = ["topics", "concepts", "methods", "species", "molecules", "cell_types"]


def add_review_item(con, kind, detail, paper_id=None, file_path=None):
    cur = con.execute(
        "INSERT INTO review_items (paper_id, kind, detail, file_path, created_at) VALUES (?,?,?,?,?)",
        (paper_id, kind, detail, file_path, config.now()),
    )
    return cur.lastrowid


def rebuild(con):
    """Rebuild paper tables from the per-paper files (review/batch tables kept)."""
    for table in PAPER_TABLES:
        con.execute(f"DELETE FROM {table}")
    for table in FTS_TABLES:  # recreated, not 'rebuild': a 'rebuild' keeps the old tokenizer (CHR-04)
        con.execute(f"DROP TABLE {table}")
    con.executescript(SCHEMA)
    count = 0
    if config.PAPERS_DIR.exists():
        for folder in sorted(config.PAPERS_DIR.iterdir()):
            if (folder / "metadata.json").exists():
                load_paper(con, folder.name)
                con.commit()
                count += 1
    # concept_links is a derived library index, rebuilt after all paper files are loaded.
    from . import concepts, sources_misc
    concepts.rebuild_links(con)
    sources_misc.index_sources(con)  # imported sources too, so their FTS table takes the current tokenizer (CHR-04)
    con.commit()
    return count
