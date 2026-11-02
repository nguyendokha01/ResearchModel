"""Reference lists (Tier 1, contract §2.3): split into entries and parse locally.

No network access: every field is read from the paper's own reference list, and a field that
cannot be read with confidence stays null. `raw` keeps the entry text as printed.
"""

import re
import unicodedata

from . import extract

_NUMBER_ONLY = re.compile(r"^\[?(\d{1,3})[\].)]?$")
_NUMBER_INLINE = re.compile(r"^\[?(\d{1,3})(?:[\].)]\s*|\s+)(?=\S)")
_AUTHOR_START = re.compile(  # accented surnames too (Fehér, Köhler, Šamaj, Łukasiewicz, Erdős); matched on NFC text
    r"^(?:(?:van|von|de|der|den|del|da|di|du|le|la)\s+)*[A-ZÀ-ÖØ-ÞĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’\-]+,?\s+(?:[A-ZÀ-ÖØ-ÞĀ-ž]\.?\s?-?){1,3}(?:[,.;]|\s|$)")
_LONE_SURNAME = re.compile(r"^[A-ZÀ-ÖØ-ÞĀ-ž][A-Za-zÀ-ÖØ-öø-ÿĀ-ž'’\-]+,$")  # "Hirochika," with the initials on the next line
_DOI_END = re.compile(r"\b10\.\d{4,9}/\S*$")
DOI = re.compile(r"\b10\.\s?\d{4,9}/\S+", re.I)
PMID = re.compile(r"\bPMID:?\s*(\d{6,9})")
_YEAR_PAREN = re.compile(r"\(((?:19|20)\d{2})([a-z]?)\)")
_YEAR_SEMI = re.compile(r"\b((?:19|20)\d{2})([a-z]?)\s*[;:]")
_YEAR_ANY = re.compile(r"\b((?:19|20)\d{2})([a-z]?)\b")
_FIRST_AUTHOR = re.compile(  # "Smith-Jones", "De-la-Cañas", "Müllert", "McGowan", "O'Brien" stay whole; a cut name is null
    r"^((?:(?:van|von|de|der|den|del|da|di|du|le|la)\s+)*[A-ZÀ-ÖØ-Þ](?:[a-zß-öø-ÿ]+|['’](?=[A-ZÀ-ÖØ-Þ]))"
    r"(?:['’\-]?[A-ZÀ-ÖØ-Þ][a-zß-öø-ÿ]+|['’\-][a-zß-öø-ÿ]+)*)(?![\w'’\-–̀-ͯ])")
# a token that can continue an author list: an initial ("Y.") or "Surname, X."
_AUTHOR_CONTINUES = re.compile(r"^(?:[A-Z]\.|[A-Z][A-Za-z'’\-]+,\s[A-Z]\.|and\b|&)")


def parse_lines(lines, vocab):
    """Split reference-list lines [{page, bbox, text}] into entries [{num, page, raw}].

    Numbered lists: an entry starts at "12." / "[12]" / "12" — either at the start of a line or
    on a separate line level with the entry's first line (some layouts print the number after
    it). Numbers must follow 1, 2, 3…; a line such as "228." that continues a page range is
    therefore not mistaken for a new entry. Unnumbered lists start an entry at "Surname, X."
    after a line that ended an entry.
    """
    text_lines, starts, numbers = [], [], []
    for ln in lines:
        m = _NUMBER_ONLY.match(ln["text"].strip())
        if m:  # kept as text too: "228." may be the end of a page range, not entry 228
            numbers.append((int(m.group(1)), len(text_lines)))
            text_lines.append(dict(ln, number_only=True))
            continue
        m = _NUMBER_INLINE.match(ln["text"])
        if m:
            starts.append((len(text_lines), int(m.group(1)), None))
            ln = dict(ln, text=ln["text"][m.end():])
        text_lines.append(ln)
    for num, index in numbers:
        starts.append((_same_row(text_lines[index], text_lines, index + 1), num, index))
    entries = _numbered(starts, text_lines)
    if entries:
        return [_finish(e, vocab) for e in entries]
    return [_finish(e, vocab) for e in _unnumbered(lines)]


def _same_row(num_line, text_lines, next_index):
    """Index of the text line printed level with a stand-alone number, else the next line."""
    x0, y0, x1, y1 = num_line["bbox"]
    mid = (y0 + y1) / 2
    best = None
    for i, ln in enumerate(text_lines):
        if ln["page"] != num_line["page"] or ln.get("number_only"):
            continue
        lx0, ly0, lx1, ly1 = ln["bbox"]
        if abs((ly0 + ly1) / 2 - mid) <= max(2.0, 0.6 * (y1 - y0)) and lx0 >= x1 - 2 and lx0 - x1 < 40:
            if best is None or abs(i - next_index) < abs(best - next_index):
                best = i
    return best if best is not None else min(next_index, len(text_lines) - 1)


def _numbered(starts, text_lines):
    """starts: (index of the entry's first line, number, index of a stand-alone number line or None)."""
    accepted, markers, expected = [], set(), 1
    for index, num, marker in sorted(set(starts), key=lambda s: (s[0], s[1])):
        if num == expected or (accepted and expected < num <= expected + 2):
            if accepted and accepted[-1][0] == index:
                continue
            accepted.append((index, num))
            if marker is not None:
                markers.add(marker)
            expected = num + 1
    entries = []
    for k, (index, num) in enumerate(accepted):
        end = accepted[k + 1][0] if k + 1 < len(accepted) else len(text_lines)
        chunk = [ln for i, ln in enumerate(text_lines[index:end], start=index) if i not in markers]
        if chunk:
            entries.append({"num": num, "page": chunk[0]["page"], "lines": [c["text"] for c in chunk],
                            "gap": k > 0 and num != accepted[k - 1][1] + 1})
    return entries


def _ends_in_doi(lines):
    """The entry so far ends in a DOI, also one wrapped over two lines ("10.1046/j.1365-" + "313X.2003.01702.x",
    "10.1016/S0092-8674(03)" + "00401-X")."""
    last = lines[-1].strip()
    return bool(_DOI_END.search(last)) or (len(lines) > 1 and " " not in last and bool(_DOI_END.search(lines[-2].strip())))


def _unnumbered(lines):
    entries, current = [], None
    for ln in lines:
        text = ln["text"].strip()
        doi_end = current is not None and _ends_in_doi(current["lines"])
        ends_entry = current is None or doi_end or re.search(r"[.)]\s*$|\d\s*$|doi\S*$", current["lines"][-1])
        nfc = unicodedata.normalize("NFC", text)  # a decomposed accent ("e" + U+0301) reads as "é"
        if (_AUTHOR_START.match(nfc) and ends_entry) or (doi_end and _LONE_SURNAME.match(nfc)):
            current = {"num": None, "page": ln["page"], "lines": [text], "gap": False}
            entries.append(current)
        elif current is not None:
            current["lines"].append(text)
    # An entry with no year whose first word has no comma ("Indian J. Exp. Biol. 43, 740–755.", "PLoS ONE 8:e5") is the
    # journal tail of the entry before it, not "Smith, J.", "Smith J," or "van der Berg, A." (in press): it goes back to it, text kept.
    merged = []
    for entry in entries:
        if merged and not _YEAR_ANY.search(DOI.sub("", " ".join(entry["lines"]))) and not re.match(r"(?:[a-z]+\s+)*\S+(?: [A-Z]{1,3})?,", entry["lines"][0]):
            merged[-1]["lines"] += entry["lines"]
        else:
            merged.append(entry)
    return merged


def _finish(entry, vocab):
    raw = extract.clean_spaces(extract.resolve_hyphens(extract.join_lines(entry["lines"]), vocab))
    ref = {"num": entry["num"], "page": entry["page"], "raw": raw}
    ref.update(parse_fields(raw))
    known = sum(bool(ref[k]) for k in ("first_author", "year"))
    if entry["gap"] or len(raw) > 1000 or known == 0:
        ref["parse_confidence"] = "low"
    elif known == 2 and (entry["num"] or ref["title"]):
        ref["parse_confidence"] = "high"
    else:
        ref["parse_confidence"] = "medium"
    return ref


def parse_fields(raw):
    """first_author, year (+ a/b suffix), title, journal, doi, pmid — null when unsure."""
    text = re.sub(r"\s+", " ", raw).strip()
    doi = None
    m = DOI.search(text)
    if m:
        doi = re.sub(r"\s", "", m.group(0)).rstrip(".,;)]}").lower()
    pmid = PMID.search(text)
    year = suffix = None
    for rx in (_YEAR_PAREN, _YEAR_SEMI):
        m = rx.search(text)
        if m:
            year, suffix = int(m.group(1)), m.group(2)
            break
    else:
        found = list(_YEAR_ANY.finditer(text))
        if found:
            year, suffix = int(found[-1].group(1)), found[-1].group(2)
    first = _FIRST_AUTHOR.match(text)
    title, journal = _title_journal(text)
    return {"first_author": first.group(1) if first else None, "year": year, "year_suffix": suffix or None,
            "title": title, "journal": journal, "doi": doi, "pmid": pmid.group(1) if pmid else None}


def _title_journal(text):
    """Title = the sentence after the author list; journal = the text after it, up to the year."""
    body, authors_end = text, None
    year = _YEAR_PAREN.search(text[:400])
    if year:  # "Authors (2014) Title. Journal 10: 1-5."
        body, authors_end = text[year.end():].lstrip(". "), 0
    for m in ([] if year else re.finditer(r"\.\s+", body)):
        before = body[:m.start()]
        if not re.search(r"(?:\b[A-Z]{1,3}|\bal|\b[A-Z]\.[A-Z]?)$", before):
            continue
        rest = body[m.end():]
        if _AUTHOR_CONTINUES.match(rest):
            continue
        authors_end = m.end()
        break
    if authors_end is None or authors_end > 600:
        return None, None
    rest = body[authors_end:]
    m = re.search(r"[.?!]\s+(?=[A-Z])", rest)
    if not m or m.end() < 15:
        return None, None
    title = rest[:m.end()].strip().rstrip(".")
    after = rest[m.end():]
    j = re.match(r"(.+?)(?:\.?\s+(?:19|20)\d{2}\b|\.?\s*\d+\s*[(:;]|\s*[;,]\s*\d)", after)
    journal = j.group(1).strip().rstrip(".,") if j and len(j.group(1)) <= 120 else None
    return (title if len(title) <= 400 else None), journal


def from_passages(passages, vocab):
    """Fallback when the PDF is not available: split reference passages at 1, 2, 3…"""
    text = " ".join(p["text"] for p in passages if p["section"] == "references" and p.get("source", "main") == "main")
    positions, pos, n = [], 0, 1
    while True:
        m = re.compile(rf"(?:(?<=\s)|^)\[?{n}[\].]?\s+(?=[A-Z])").search(text, pos)
        if not m:
            break
        positions.append((m.start(), m.end(), n))
        pos, n = m.end(), n + 1
    entries = []
    for k, (start, body_start, num) in enumerate(positions):
        end = positions[k + 1][0] if k + 1 < len(positions) else len(text)
        entries.append(_finish({"num": num, "page": None, "lines": [text[body_start:end].strip()], "gap": False},
                               vocab))
    pages = [p["page"] for p in passages if p["section"] == "references"]
    for e in entries:
        e["page"] = min(pages) if pages else None
    return entries


def _norm_name(name):
    return re.sub(r"[^a-z]", "", extract.normalize_title(name or ""))


def _title_words(text):
    return {w for w in extract.normalize_title(text).split() if len(w) > 2}


def matches_paper(ref, paper):
    """Does this reference entry describe this library paper?

    DOI or PMID equal; or same first-author surname, year within one (online vs print),
    and at least 85% of the paper's title words present in the reference text."""
    if ref.get("doi") and paper.get("doi"):
        return ref["doi"].lower() == paper["doi"].lower()
    if ref.get("pmid") and paper.get("pmid"):
        return str(ref["pmid"]) == str(paper["pmid"])
    if not (ref.get("year") and paper.get("year") and abs(ref["year"] - paper["year"]) <= 1):
        return False
    if not ref.get("first_author") or _norm_name(ref["first_author"]) != _norm_name(paper.get("first_author")):
        return False
    words = _title_words(paper.get("title"))
    in_ref = _title_words(ref.get("raw"))
    return bool(words) and len(words & in_ref) / len(words) >= 0.85


def resolve(con, refs, own_paper_id=None):
    """Set resolved_paper_id for references to papers already in the library."""
    for r in refs:
        r["resolved_paper_id"] = None
        candidates = []
        if r.get("doi"):
            candidates += con.execute("SELECT * FROM papers WHERE lower(doi) = ?", (r["doi"],)).fetchall()
        if r.get("pmid"):
            candidates += con.execute("SELECT * FROM papers WHERE pmid = ?", (r["pmid"],)).fetchall()
        if r.get("year"):
            candidates += con.execute("SELECT * FROM papers WHERE year BETWEEN ? AND ?",
                                      (r["year"] - 1, r["year"] + 1)).fetchall()
        for row in candidates:
            if row["paper_id"] != own_paper_id and matches_paper(r, dict(row)):
                r["resolved_paper_id"] = row["paper_id"]
                break
    return refs


def link_back(con, paper_id):
    """A newly added paper may already be cited by papers in the library: link those references.
    Updates the other papers' references.jsonl and the database. Caller commits."""
    from . import db  # local import: db does not depend on this module
    row = con.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    if row is None or not row["year"]:
        return 0
    paper = dict(row)
    hits = {}
    for ref in con.execute("""SELECT * FROM references_list WHERE resolved_paper_id IS NULL AND paper_id != ?
                              AND (lower(doi) = lower(?) OR pmid = ? OR year BETWEEN ? AND ?)""",
                           (paper_id, paper.get("doi") or "", paper.get("pmid") or "", paper["year"] - 1,
                            paper["year"] + 1)):
        if matches_paper(dict(ref), paper):
            hits.setdefault(ref["paper_id"], set()).add(ref["ref_id"])
    for citing, ref_ids in hits.items():
        path = db.paper_dir(citing) / "references.jsonl"
        refs = list(db.read_jsonl(path))
        for r in refs:
            if r["ref_id"] in ref_ids:
                r["resolved_paper_id"] = paper_id
        db.write_jsonl(path, refs)
        con.executemany("UPDATE references_list SET resolved_paper_id = ? WHERE ref_id = ?",
                        [(paper_id, rid) for rid in ref_ids])
    return sum(len(v) for v in hits.values())


def unlink(con, paper_id):
    """A paper was removed from the library: references that pointed at it point nowhere again.
    Caller commits."""
    from . import db
    citing = [r["paper_id"] for r in con.execute(
        "SELECT DISTINCT paper_id FROM references_list WHERE resolved_paper_id = ?", (paper_id,))]
    for pid in citing:
        path = db.paper_dir(pid) / "references.jsonl"
        refs = list(db.read_jsonl(path))
        for r in refs:
            if r.get("resolved_paper_id") == paper_id:
                r["resolved_paper_id"] = None
        db.write_jsonl(path, refs)
    con.execute("UPDATE references_list SET resolved_paper_id = NULL WHERE resolved_paper_id = ?", (paper_id,))
    return len(citing)
