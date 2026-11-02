"""Conformance checks of the stages IN (intake) and CHR (characters) of config/standard.yaml, see conform.py.

CHR-01..03 are also the character lint of the whole project: stored text, vault notes and the project's own code.
Not implemented (no honest automatic check): IN-04 (memory use of the steps), CHR-05 (the data model has no
needs_pdf_check flag yet).
"""
import ast
import contextlib
import hashlib
import re
import shutil
import sqlite3
import tempfile
import unicodedata
from pathlib import Path
from unittest import mock

from . import answer, config, db, extract, ghosts, ingest, quality, selftest, sentences, tier1
from .conform import check

SOURCE_STATUSES = ("active", "archived", "external", "unavailable")  # db.py source_files.status
OFF_DISK = ("archived", "external", "unavailable")  # statuses that need no path on this machine
TEXT_SUFFIXES = {".md", ".json", ".jsonl", ".yaml", ".yml", ".py"}
REPO_DIRS = ("scripts", "tests", "config", "docs", "tools", ".claude")


# ---------------------------------------------------------------- helpers

@contextlib.contextmanager
def _sandbox(vaults=("one", "two")):
    """A throw-away project (config paths, database, empty vaults) for probes that run product code which writes files."""
    names = ("ROOT", "CONFIG_DIR", "STORE", "DB_PATH", "PAPERS_DIR", "LOGS_DIR", "CACHE_DIR", "VAULTS_DIR", "REVIEW_PENDING")
    saved = {n: getattr(config, n) for n in names}
    with tempfile.TemporaryDirectory(prefix="conform-") as tmp:
        root = Path(tmp).resolve()  # config.rel() resolves paths: an 8.3 or junction TEMP would not match an unresolved root
        config.ROOT, config.CONFIG_DIR, config.STORE = root, root / "config", root / "knowledge_store"
        config.DB_PATH, config.PAPERS_DIR = config.STORE / "library.db", config.STORE / "papers"
        config.LOGS_DIR, config.CACHE_DIR = config.STORE / "logs", config.STORE / "cache"
        config.VAULTS_DIR, config.REVIEW_PENDING = root / "vaults", root / "review" / "pending"
        con = None
        try:
            for v in vaults:
                config.vault_dir(v).mkdir(parents=True)
                (config.vault_dir(v) / "Home.md").write_text(f"# {v}\n", encoding="utf-8", newline="\n")
            con = db.connect()
            yield root, con
        finally:
            if con:
                con.close()
            for n, value in saved.items():
                setattr(config, n, value)


def _seed(con, paper_id, vaults, **meta):
    """One minimal paper record (files and database rows)."""
    db.write_json(db.paper_dir(paper_id) / "metadata.json", {
        "paper_id": paper_id, "title": "Cells in test medium", "year": 2020, "vaults": list(vaults), "authors": [],
        "note_name": "Cells in test medium", "source_files": [], "extraction_version": 2, **meta})
    db.load_paper(con, paper_id)
    con.commit()


def _papers(store):
    for paper_id in store.paper_ids:  # one at a time
        yield store.paper(paper_id)


def _notes(store):
    """(vault/relative path, text) of every vault note, one vault in memory at a time."""
    for vault in store.vaults:
        for path, text in vault.notes:
            yield f"{vault.name}/{path}", text
        vault._data.pop("notes", None)


def _source_path(source):
    if not source.get("path"):
        return None
    path = Path(source["path"])
    return path if path.is_absolute() else config.ROOT / path


def _name(source):
    return source.get("original_filename") or source.get("path") or "?"


def _codes(chars):
    return " ".join(f"U+{ord(c):04X}" for c in chars)


# ---------------------------------------------------------------- IN: intake

@check("IN-01", "store")
def no_duplicate_records(store, judge=ingest.find_existing):
    """Data: no two papers share a DOI, PMID, normalised title + year or file hash. Probe: `judge` finds an existing record by each key."""
    for label, key in (("DOI", "lower(doi)"), ("PMID", "pmid"), ("title + year", "norm_title || '|' || year")):
        where = "norm_title <> '' AND year IS NOT NULL" if label == "title + year" else f"{key} IS NOT NULL AND {key} <> ''"
        for value, ids in store.con.execute(f"SELECT {key}, group_concat(paper_id) FROM papers WHERE {where} "
                                            f"GROUP BY {key} HAVING count(*) > 1"):
            yield f"same {label} {str(value)[:40]!r} in {ids}"
    # The database cannot show a shared file hash (source_files is INSERT OR REPLACE on the SHA-256): read the records.
    owner = {}
    for paper in _papers(store):
        for source in paper.meta.get("source_files") or []:
            sha = source.get("sha256")
            if sha and owner.setdefault(sha, paper.id) != paper.id:
                yield f"same file hash {sha[:12]} in {owner[sha]}, {paper.id}"
    title = "Freezing of oocytes in a probe medium"
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    con.execute("INSERT INTO papers (paper_id, doi, pmid, norm_title, year) VALUES ('a', '10.1/abc', '123', ?, 2020)",
                (extract.normalize_title(title),))
    for case, info, want in (("DOI, any case", {"doi": "10.1/ABC"}, ("a", "DOI")),
                             ("PMID", {"pmid": "123"}, ("a", "PMID")),
                             ("title + year", {"title": title, "year": 2020}, ("a", "title + year")),
                             ("other year", {"title": title, "year": 2019}, (None, None)),
                             ("other DOI", {"doi": "10.1/other"}, (None, None))):
        got = tuple(judge(con, info))
        if got != want:
            yield f"probe {case}: find_existing returned {got}, expected {want}"
    con.close()


@check("IN-02", "probe")
def probe_two_vaults(judge=ingest._attach_paper_to_vault):
    with _sandbox() as (root, con):
        _seed(con, "p1", ["one"])
        judge(con, "p1", "two")
        n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        vaults = sorted(r[0] for r in con.execute("SELECT vault FROM paper_vaults WHERE paper_id = 'p1'"))
    if n != 1:
        yield f"attaching a paper to a second vault left {n} paper rows"
    if vaults != ["one", "two"]:
        yield f"paper_vaults lists {vaults} for a paper in two vaults"


@check("IN-03", "paper")
def sources_unchanged(paper):
    for source in paper.meta.get("source_files") or []:
        path = _source_path(source)
        if path is None:
            continue
        if not path.is_file():
            if source.get("status") == "active":
                yield f"{_name(source)}: recorded active but the file is missing"
        elif source.get("sha256") and extract.sha256_file(path) != source["sha256"]:  # streamed in 1 MB chunks
            yield f"{_name(source)}: SHA-256 differs from the recorded one"


@check("IN-05", "store", via_selftest=True)
def ids_follow_contract(store, run=selftest.check_paper):
    # ponytail: re-runs check_paper per clause (IN-05, CHR-08); cache per paper if the library grows large
    for paper_id in store.paper_ids:
        report = selftest.Report()
        run(report, store.con, paper_id)
        yield from (e for e in report.errors
                    if re.search(r"sentence_id format|I2:|ref_id format|duplicate ref_id|object id format|unit_id/seq", e))


@check("IN-06", "paper")
def passages_only_says_so(paper):
    tier = paper.meta.get("tier1")
    if not tier:
        return
    if tier.get("from") not in ("pdf", "passages_only"):
        yield f"metadata.tier1.from is {tier.get('from')!r}"
    elif tier["from"] == "pdf" and paper.meta.get("source_files") and tier1.main_pdf(paper.meta) is None:
        yield "tier1.from is pdf but the original is unreachable (passages_only expected; original unavailable)"


@check("IN-07", "paper")
def sources_can_be_located(paper):
    for source in paper.meta.get("source_files") or []:
        status = source.get("status")
        if status not in SOURCE_STATUSES:
            yield f"{_name(source)}: status {status!r}"
        if not source.get("sha256"):
            yield f"{_name(source)}: no SHA-256"
        if not source.get("path") and status not in OFF_DISK:
            yield f"{_name(source)}: no path and status {status!r}"


@check("IN-08", "probe")
def probe_ghost_ids(judge=ghosts.ghost_id):
    digest = hashlib.sha1("a study cells 2020".encode()).hexdigest()[:12]
    for ref, want in (({"doi": " 10.1234/ABC. "}, "ghost:doi:10.1234/abc"),
                      ({"title": "A Study: Cells", "year": 2020}, f"ghost:t:{digest}"),
                      ({"title": "Title only"}, None), ({"year": 2020}, None), ({}, None)):
        if judge(ref) != want:
            yield f"ghost_id({ref}) returned {judge(ref)!r}, expected {want!r}"


@check("IN-09", "probe")
def probe_supplement(attach=tier1.attach_supplement, parent=ingest._supplement_parent):
    cfg = {"ingest": {"active_folder": "sources/active"}, "passages": {}}
    with _sandbox() as (root, con):
        _seed(con, "p1", ["one"])
        original = root / "inbox" / "article_supp.txt"
        original.parent.mkdir()
        original.write_bytes(b"supplement probe\n")
        sha = extract.sha256_file(original)
        try:
            attach(con, cfg, "p1", original)
        except Exception as err:
            yield f"attaching a supplement failed: {type(err).__name__}: {err}"
        meta = db.read_json(db.paper_dir("p1") / "metadata.json")
        stored = [s for s in meta.get("source_files", []) if s.get("role") == "supplement"]
        copy = _source_path(stored[0]) if stored else None
        if not original.is_file() or extract.sha256_file(original) != sha:
            yield "the original supplement was moved or modified"
        if not copy or not copy.is_file() or extract.sha256_file(copy) != sha or stored[0].get("sha256") != sha:
            yield "the stored supplement copy is missing or its SHA-256 differs from the original"

        real_copy, copied = shutil.copy2, []

        def corrupt(src, dest):  # a copy that does not match
            real_copy(src, dest)
            copied.append(dest)
            Path(dest).write_bytes(b"damaged")
        other = root / "inbox" / "other.txt"
        other.write_bytes(b"another supplement\n")
        with mock.patch.object(tier1.shutil, "copy2", corrupt):
            try:
                attach(con, cfg, "p1", other)
                yield "a copy with a different SHA-256 was kept"
            except IOError:  # also FileNotFoundError: only a refusal after the copy was made proves the check
                if not copied:
                    yield "the damaged-copy probe never reached the copy"
                elif any(Path(d).exists() for d in copied):
                    yield "a copy with a different SHA-256 stays in the library"
        key = ingest._filename_key("article")
        for case, batch, want in (("ambiguous", {("b", key): {"p1", "p2"}}, None), ("unique", {("b", key): {"p1"}}, "p1")):
            got = parent(con, "b", root / "inbox" / "article_supp.txt", batch)[0]
            if got != want:
                yield f"batch match ({case}) returned {got!r}, expected {want!r}"


@check("IN-10", "probe")
def probe_identical_pdf(judge=ingest.ingest_one):
    data = b"%PDF-1.4 conformance probe"
    sha = hashlib.sha256(data).hexdigest()
    with _sandbox() as (root, con):
        _seed(con, "p1", ["one"], source_files=[{"sha256": sha, "path": "sources/active/p1/a.pdf", "status": "active",
                                                  "role": "main", "original_filename": "a.pdf"}])
        pdf = root / "inbox" / "copy.pdf"
        pdf.parent.mkdir()
        pdf.write_bytes(data)
        outcome = judge({}, con, pdf, "b1", "two")[0]
        n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        kept = [p for p in config.REVIEW_PENDING.rglob("*") if p.is_file() and extract.sha256_file(p) == sha]
        left_in_inbox = pdf.exists()
    if outcome != "duplicate" or n != 1:
        yield f"an identical PDF gave outcome {outcome!r} and {n} paper rows"
    if not kept or left_in_inbox:
        yield "the extra copy is not in the review quarantine (or is still in the inbox)"


# ---------------------------------------------------------------- CHR: characters

@check("CHR-01", "store")
def all_text_is_nfc(store, judge=answer.check_sentence):
    """Data: sentences, passages and vault notes are NFC. Probe: an NFD answer is judged like its NFC form."""
    for paper in _papers(store):
        for kind, rows, key in (("sentence", paper.sentences, "sentence_id"), ("passage", paper.passages, "passage_id")):
            for row in rows:
                if unicodedata.normalize("NFC", row.get("text") or "") != (row.get("text") or ""):
                    yield f"{kind} {row.get(key)} is not NFC"
    for path, text in _notes(store):
        if unicodedata.normalize("NFC", text) != text:
            yield f"note {path} is not NFC"
    items = [{"text": "Treatment may improve survival of cells.", "paper_id": "p1", "kind": "sentence",
              "unit_type": "result", "hedging": "hedged"}]
    for text in ("Điều trị có thể cải thiện sự sống sót của tế bào [#1].", "Điều trị chắc chắn cải thiện sự sống sót [#1]."):
        verdicts = {form: judge({"text": unicodedata.normalize(form, text)}, items, {"p1": {}})["check"]
                    for form in ("NFC", "NFD")}
        if verdicts["NFC"] != verdicts["NFD"]:
            yield f"answer checker: NFC {verdicts['NFC']!r} but NFD {verdicts['NFD']!r} for {text[:30]!r}"


@check("CHR-02", "store")
def no_broken_characters(store, root=None):
    for paper in _papers(store):
        for row in paper.sentences:
            text = row.get("text") or ""
            bad = quality.suspicious_characters(text)
            if bad:
                yield f"sentence {row.get('sentence_id')} has {_codes(bad)}"
            if any(m in text for m in selftest.MOJIBAKE):
                yield f"sentence {row.get('sentence_id')} has mojibake"
    for path, text in _notes(store):
        bad = quality.suspicious_characters(text)
        if bad:
            yield f"note {path} has {_codes(bad)}"
        if any(m in text for m in selftest.MOJIBAKE):
            yield f"note {path} has mojibake"
    # The project's own code: "\b" in a non-raw regex string is silently 0x08. A control character the author
    # spelled out as \xNN or \uNNNN (the symbol-font maps in extract.py) is deliberate and not reported.
    root = Path(root or config.ROOT)
    for path in sorted((root / "scripts").rglob("*.py")):
        name = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as err:
            yield f"{name}: cannot be parsed ({err})"
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                bad = [c for c in quality.suspicious_characters(node.value) if unicodedata.category(c) == "Cc"]
                spelled = ast.get_source_segment(source, node) if bad else ""  # slow: only for the rare hit
                bad = [c for c in bad if not re.search(rf"\\x{ord(c):02x}|\\u{ord(c):04x}", spelled, re.I)]
                if bad:
                    yield f"{name}:{node.lineno} string constant has {_codes(bad)}"


@check("CHR-03", "store")
def only_lf(store, root=None):
    def has_cr(path):
        with open(path, "rb") as f:
            return any(b"\r" in chunk for chunk in iter(lambda: f.read(1 << 20), b""))

    def files(base, recurse):
        return [p for p in sorted(base.rglob("*") if recurse else base.glob("*"))
                if p.is_file() and p.suffix.lower() in TEXT_SUFFIXES]
    for p in files(store.root, False):
        if has_cr(p):
            yield f"store file {p.name} has a carriage return"
    for pid in store.paper_ids:
        for p in files(store.root / "papers" / pid, True):
            if has_cr(p):
                yield f"{pid}/{p.relative_to(store.root / 'papers' / pid).as_posix()} has a carriage return"
    from .conform_vault import HUMAN_FOLDERS  # Mine/ is human content (rule 5); the clause is about generated notes
    for path, text in _notes(store):  # path = <vault>/<folder>/...
        if "\r" in text and path.split("/")[1] not in HUMAN_FOLDERS:
            yield f"note {path} has a carriage return"
    root = Path(root or config.ROOT)
    repo = files(root, False) + [p for d in REPO_DIRS if (root / d).is_dir() for p in files(root / d, True)]
    for p in repo:
        if has_cr(p):
            yield f"{p.relative_to(root).as_posix()} has a carriage return"


@check("CHR-04", "probe")
def probe_lookalikes(judge=sentences.fold_search_glyphs):
    for group in (("37 °C", "37 ◦C", "37 ^◦C", "37 ˚C"), ("ΔG", "∆G")):
        if len({judge(form) for form in group}) > 1:
            yield f"look-alike forms {' | '.join(group)} are not matched as one"


@check("CHR-06", "probe", via_selftest=True)
def probe_font_repairs(judge=selftest.unit_checks):
    report = selftest.Report()
    judge(report)
    yield from (e for e in report.errors if e.startswith("text/font_glyphs:"))


@check("CHR-07", "probe", via_selftest=True)
def probe_superscripts(judge=selftest.unit_checks):
    report = selftest.Report()
    judge(report)
    yield from (e for e in report.errors if e.startswith(("citations/superscript:", "extract/line_text:")))


@check("CHR-08", "store", via_selftest=True)
def ocr_pages_recorded(store, run=selftest.check_paper):
    for paper_id in store.paper_ids:
        report = selftest.Report()
        run(report, store.con, paper_id)
        yield from (e for e in report.errors if ":OCR1:" in e)
