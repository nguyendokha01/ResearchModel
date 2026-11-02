"""Conformance checks of stage CH (sentences, passages, links, tables, sections) of config/standard.yaml, see conform.py.

Data checks read one paper at a time. CH-01, CH-08, CH-10 and CH-11 are probes on product functions.
CH-09 re-uses selftest. Not implemented: none (the refusal of a re-extraction with lost_total > 0 in CH-11 is G3 work,
the probe only judges the measuring function).
Heuristics (documented where used): CH-02 tails/heads, CH-04 cuts, CH-05 glued captions, CH-06 split rows, CH-07 body in
references and "whole body in one section", CH-12 page furniture.
"""
import collections
import re

from . import db, extract, references, renumber, selftest, sentences, tier1
from .conform import check

BODY_SECTIONS = {"introduction", "methods", "results", "results_discussion", "discussion", "main_text"}  # conclusion apart: see CH-07


# ---------------------------------------------------------------- helpers

def _body(row):
    """Running text of the paper: not a caption, table row, table text, reference, front matter or back matter."""
    return row.get("kind") == "text" and not row.get("in_table") and row.get("section") not in sentences.NOT_BODY


def _sections(paper):
    """sections.json of the paper ([{name, heading, page_start}]); tests give `sections=` directly."""
    got = getattr(paper, "sections", None)
    if got is None and paper.folder:
        got = db.read_json(paper.folder / "sections.json", [])
    return got or []


def _headings(paper):
    """Printed headings of the paper: section headings and sub-headings, lower case."""
    names = {(s.get("heading") or "").strip().casefold() for s in _sections(paper)}
    names |= {(r.get("subsection") or "").strip().casefold() for r in paper.sentences}
    return names - {""}


def _link_problem(rows, i, by_id, headings):
    """Why the `continues` link of rows[i] is not correct (None: it is correct or there is no link)."""
    row, target_id = rows[i], rows[i].get("continues")
    if not target_id:
        return None
    j = by_id.get(target_id)
    if j is None:
        return f"target {target_id} is not in this paper"
    target = rows[j]
    if j <= i:
        return "target does not come later"
    if row.get("kind") != "text" or row.get("in_table"):
        return "the head is not body text"
    if sentences.TERMINAL.search(str(row.get("text") or "").strip()):
        return "the head already ends with terminal punctuation"
    if target.get("kind") != "text" or target.get("in_table"):
        return f"target is a {'table cell' if target.get('kind') == 'table_row' or target.get('in_table') else target.get('kind')}"
    if str(target.get("text") or "").strip().casefold() in headings:
        return "target is a heading"
    if (row.get("source", "main"), row.get("section")) != (target.get("source", "main"), target.get("section")):
        return "target is in another section or source"
    # between head and target may sit captions, table rows/text and sidebar blocks; a complete body sentence that itself
    # reads as the rest of the head is the true next part, so a link past it is wrong
    skipped = next((r for r in rows[i + 1:j] if _body(r) and sentences.TERMINAL.search(str(r.get("text") or "").strip())
                    and not sentences.table_text_suspect(r)
                    and sentences._starts_like_continuation(r.get("text"), row.get("text"))), None)
    if skipped:
        return f"skips body text {skipped.get('sentence_id')}"
    if not (sentences._starts_like_continuation(target.get("text"), row.get("text")) or sentences._is_fragment(target)):
        return "target does not read as the rest of the head"
    return None


def _table_words(paper):
    """{(source, page): lower-case words of the table cells printed on that page}."""
    words = collections.defaultdict(set)
    for t in paper.tables:
        cells = [c for row in [t.get("header") or [], *(t.get("rows") or [])] for c in row]
        words[(t.get("source", "main"), t.get("page"))].update(w for c in cells for w in re.findall(r"\w+", str(c).casefold()))
    return words


def _mostly_table(row, table_words):
    """More than half of the words of the row are cells of a table on the same page (a table read as running text)."""
    tokens = re.findall(r"\w+", str(row.get("text") or "").casefold())
    cells = table_words.get((row.get("source", "main"), row.get("page")))
    return bool(cells and tokens and 2 * sum(w in cells for w in tokens) > len(tokens))


def _not_prose(row, table_words=None):
    """The row text is a label, list, e-mail/journal line or table text, not a sentence (an empty text is not a row at all)."""
    text = str(row.get("text") or "").strip()
    if not text:
        return False
    if table_words and _mostly_table(row, table_words):
        return True
    if not re.search(r"(?:[^\W\d_]|[,\-–—)])$", text) or "@" in text or not re.search(r"[a-z]", text):
        return True  # a number or bracket end, an e-mail line, "(TIF)"; a letter end may be Greek or µ ("TNF-α")
    words = text.split()
    if len(words) > 1 and 2 * sum(w[0].isupper() or w[0].isdigit() for w in words) > len(words):
        return True  # mostly capitalised words or numbers: an abbreviation list or table cells, not prose
    return _is_list(text)


def _cut_off(row, table_words=None):
    """A body sentence that ends in the middle: no terminal punctuation, and not a label, list, e-mail or table text."""
    text = str(row.get("text") or "").strip()
    return bool(_body(row) and text and not sentences.TERMINAL.search(text) and not sentences.table_text_suspect(row)
                and not _not_prose(row, table_words))  # CH-04 does not use table_text_suspect: it hid a real cut (paper A S0318)


def _is_list(text):
    """A keyword/affiliation list: no verb and 2+ separators, or an explicit "Keywords"/"Abbreviations" opener
    (":" or a capitalised item next; "Abbreviations were defined ..." stays prose)."""
    return (re.match(r"(?i:key\s*words?|abbreviations?)\s*(?::|[A-Z])", text) is not None
            or (not sentences._has_verb(text) and re.search(r"[,;·].*[,;·]", text) is not None))


def _starts_lower(row):
    """A body sentence whose first word is a plain lower-case word ("of", "detected"); not "mRNA", "pH" or "p<0.05"."""
    first = str(row.get("text") or "").split(None, 1)[:1]
    return _body(row) and bool(first) and re.fullmatch(r"[a-z]{2,}[,;:.)]*", first[0]) is not None and not sentences.table_text_suspect(row)


# ---------------------------------------------------------------- CH-01 .. CH-04: splitting, fragments, links

SPLIT_CASES = (  # (text, the sentences it must split into)
    ("Cells grew well (Smith et al. 2010). The yield was high.", ["Cells grew well (Smith et al. 2010).", "The yield was high."]),
    ("Cells were counted (Fig. 2A). The next step followed.", ["Cells were counted (Fig. 2A).", "The next step followed."]),
    ("Agents, e.g. DMSO, were used. Others were not.", ["Agents, e.g. DMSO, were used.", "Others were not."]),
    ("It was lower, i.e. below baseline. We repeated it.", ["It was lower, i.e. below baseline.", "We repeated it."]),
    ("Survival was 80% vs. 60% in controls. The gap was wide.", ["Survival was 80% vs. 60% in controls.", "The gap was wide."]),
    ("See cf. the earlier study for details. It differs.", ["See cf. the earlier study for details.", "It differs."]),
    ("Cells were isolated by M. Lindqvist and colleagues. They were stored.",
     ["Cells were isolated by M. Lindqvist and colleagues.", "They were stored."]),
    ("The pH was 7.4 in all groups. Cells were washed.", ["The pH was 7.4 in all groups.", "Cells were washed."]),
    ("Cells grew well. and then they stopped.", ["Cells grew well. and then they stopped."]),
    ("Volumes ranged from 0 to 10/μL. The mean was 3.5.", ["Volumes ranged from 0 to 10/μL.", "The mean was 3.5."]),
)


@check("CH-01", "probe")
def probe_split_sentences(judge=sentences.split_sentences):
    for text, want in SPLIT_CASES:
        got = [text[a:b] for a, b in judge(text)]
        if got != want:
            yield f"split_sentences broke or joined {text[:40]!r}: got {len(got)} sentences, expected {len(want)}"


@check("CH-02", "paper")
def no_orphan_fragments(paper):
    rows = paper.sentences
    by_id = {r.get("sentence_id"): i for i, r in enumerate(rows)}
    headings = _headings(paper)
    table_words = _table_words(paper)
    linked_in = {r["continues"] for i, r in enumerate(rows) if r.get("continues") and not _link_problem(rows, i, by_id, headings)}
    for i, row in enumerate(rows):
        if _starts_lower(row) and row.get("sentence_id") not in linked_in:
            yield f"head fragment {row.get('sentence_id')}: starts lower-case, nothing continues into it"
        if _cut_off(row, table_words) and (not row.get("continues") or _link_problem(rows, i, by_id, headings)):
            yield f"tail fragment {row.get('sentence_id')}: ends cut off, no correct continues link"


@check("CH-03", "paper")
def continues_links_are_real(paper):
    rows = paper.sentences
    by_id = {r.get("sentence_id"): i for i, r in enumerate(rows)}
    headings = _headings(paper)
    for i, row in enumerate(rows):
        problem = _link_problem(rows, i, by_id, headings)
        if problem:
            yield f"{row.get('sentence_id')} -> {row.get('continues')}: {problem}"


@check("CH-04", "paper")
def passages_end_at_a_sentence_boundary(paper):
    last = {}
    table_words = _table_words(paper)
    for row in paper.sentences:
        if row.get("passage_id"):
            last[row["passage_id"]] = row  # reading order: the last row of a passage is its last sentence
    for p in paper.passages:
        if p.get("kind") != "text" or p.get("section") in sentences.NOT_BODY or sentences.TERMINAL.search(str(p.get("text") or "").rstrip()):
            continue
        tail = last.get(p["passage_id"]) or {}
        if not tail.get("continues") and not tail.get("in_table") and not _not_prose(tail, table_words):
            yield f"passage {p['passage_id']} ends mid-sentence and no continues link is stored"


# ---------------------------------------------------------------- CH-05, CH-06: tables

@check("CH-05", "paper")
def tables_apart_from_running_text(paper):
    """Re-runs the extractor's own table-text marking on the rows (any text row it would flag now was not flagged
    when stored), and finds a caption sentence repeated inside a body sentence. A 0-row table's flattened text is
    found only by its place right after the caption (its cells are unknown)."""
    rows = [{"source": "main", "citations": [], **r} for r in paper.sentences]  # all rows: caption and table rows locate the text
    flagged = {r["sentence_id"] for r in rows if r.get("in_table")}
    tier1._mark_table_text(rows, paper.tables)
    for row in rows:
        if row.get("kind") == "text" and row.get("in_table") and row["sentence_id"] not in flagged:
            yield f"{row['sentence_id']}: table text is not flagged in_table"
    captions = [r for r in paper.sentences if r.get("kind") == "caption" and not sentences._is_fragment(r)]
    for row in paper.sentences:
        if _body(row):
            for cap in captions:
                if cap["text"] in row["text"] and cap["text"] != row["text"]:
                    yield f"{row['sentence_id']}: contains the caption sentence {cap['sentence_id']}"
                    break


@check("CH-06", "paper")
def tables_have_header_and_flag(paper):
    for t in paper.tables:
        name, quality = t.get("table_id"), t.get("quality")
        if quality not in ("ok", "uncertain"):
            yield f"{name}: quality {quality!r}"
        if not t.get("rows") and quality != "uncertain":
            yield f"{name}: 0 rows but not flagged uncertain"
        if quality != "ok":
            continue
        if not any(str(c).strip() for c in t.get("header") or []):
            yield f"{name}: quality ok but the header is empty"
        for n, row in enumerate(t.get("rows") or []):
            filled = sum(1 for c in row if str(c).strip())
            # an empty first cell under a merged (rowspan) first column starts a new record whose other cells are
            # mostly filled: 4 or more cells and over half of them. Fewer is a wrapped line of the row above.
            # ponytail: a rowspan record of a table of 4 or fewer columns (3 other cells) is indistinguishable and stays flagged.
            if row and not str(row[0]).strip() and filled and not (filled >= 4 and filled * 2 > len(row)):
                yield f"{name}: row {n} has an empty first cell and few filled cells (a record split over two rows)"


# ---------------------------------------------------------------- CH-07: sections

@check("CH-07", "paper")
def sections_follow_the_text(paper):
    """Body text in references: a references passage with a verb that ends like a sentence and has no year or
    volume:pages locator (extract._REF_LOCATOR), so it is no reference entry or piece of one. Body text in front
    matter: a non-sidebar front matter sentence after the first body sentence, or with a citation or a figure/table
    mention. Whole body in one section (a conclusion apart): only introduction in a paper with printed sub-headings
    (sentences with a subsection), or only main_text with no subsection at all; a review whose main_text sentences carry
    its sub-headings as `subsection` passes (DECISIONS 2026-10-19, D-PENDING-19)."""
    for p in paper.passages:
        text = str(p.get("text") or "")
        if (p.get("section") == "references" and p.get("kind") == "text" and sentences.TERMINAL.search(text.rstrip())
                and sentences._has_verb(text) and not extract._REF_LOCATOR.search(text)):
            yield f"passage {p['passage_id']}: body-like text in references"
    sidebar = {p["passage_id"] for p in paper.passages if p.get("sidebar")}
    began = False
    for row in paper.sentences:
        began = began or (row.get("kind") == "text" and row.get("section") in BODY_SECTIONS)
        if row.get("section") == "front_matter" and row.get("kind") == "text" and (
                row.get("citations") or row.get("mentions") or (began and row.get("passage_id") not in sidebar)):
            yield f"{row['sentence_id']}: body text in front_matter"
    body = [r for r in paper.sentences if r.get("kind") == "text" and r.get("source", "main") == "main"
            and r.get("section") in BODY_SECTIONS]
    sections = {r["section"] for r in body}
    headed = any(r.get("subsection") for r in body)
    if sections == {"introduction"} and headed:
        yield "whole body is in introduction although the paper has printed sub-headings"
    elif sections == {"main_text"} and not headed:
        yield "whole body is in main_text and no sentence carries a printed sub-heading"


# ---------------------------------------------------------------- CH-08: layout by words, length or digits

_UNIVERSITY = "Embryos were obtained from the University hospital and washed twice in medium."
_LONG = "Samples were collected over several weeks and washed gently in medium before use. " * 9
_DIGITS = ("Doses of 1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5 and 9.5 mg per kg were given to the mice in "
           "groups of 10, 20, 30 and 40 animals on days 1, 2, 3 and 4 of the study period.")
LAYOUT_PROBES = (  # (row, is layout?). A row with `lay_size`/`lay_ref` (extracted since CH-08) is judged by its font size alone.
    ({"kind": "text", "lay_size": 9.0, "lay_ref": 9.0, "text": _UNIVERSITY}, False),
    ({"kind": "text", "lay_size": 9.0, "lay_ref": 9.0, "text": _LONG}, False),
    ({"kind": "text", "lay_size": 9.0, "lay_ref": 9.0, "text": _DIGITS}, False),
    ({"kind": "text", "lay_size": 7.65, "lay_ref": 9.0, "text": "Cells were counted in group A."}, True),
    # type B, no `lay_size` (stored before the G5 re-extraction): the text rule still applies, so these three stay red
    ({"kind": "text", "text": _UNIVERSITY}, False),
    ({"kind": "text", "text": _LONG}, False),
    ({"kind": "text", "text": _DIGITS}, False),
    ({"kind": "text", "text": "Cells were counted in group A."}, False),
    ({"kind": "caption", "text": "Figure 1. Cells in group A."}, True),
    ({"kind": "table_row", "text": "Group: A | n: 5"}, True),
    ({"kind": "text", "in_table": True, "text": "Group A 5 B 6"}, True),
)


@check("CH-08", "probe")
def probe_layout_signals(judge=sentences._layout_interruption):
    for row, want in LAYOUT_PROBES:
        if bool(judge(dict(row))) != want:
            reason = "judged layout" if not want else "not judged layout"
            kind = "" if "lay_size" in row else " (no lay_size: text rule)"
            yield f"{row['kind']} row {row['text'][:30]!r} ({len(row['text'])} chars){kind} {reason}, expected the opposite"


# ---------------------------------------------------------------- CH-09: invariants I1-I4 and citations (selftest)

@check("CH-09", "store", via_selftest=True)
def citation_invariants(store, run=selftest.check_paper, unit=selftest.unit_checks):
    for paper_id in store.paper_ids:
        report = selftest.Report()
        run(report, store.con, paper_id)
        yield from (e for e in report.errors
                    if re.search(r"I1:|I2:|I3:|I4:|citation fields|citation marker offsets|continues must", e))
    report = selftest.Report()
    unit(report)
    yield from (e for e in report.errors if e.startswith("citations/"))


# ---------------------------------------------------------------- CH-10: reference parser

HYPHEN_REFERENCES = (  # (raw reference, first author it must give)
    ("Smith-Jones A, Lee J (2012) Cells and freezing of oocytes. J Cell 5: 1-9.", "Smith-Jones"),
    ("Lee-Smith, A.; Kim, B. Cells and freezing of oocytes in practice. Ann. Test. 12(3):101-109; 2005.", "Lee-Smith"),
    ("(2012) Cells and freezing of oocytes. J Cell 5: 1-9.", None),  # no author: null, not a guess
)


@check("CH-10", "probe")
def probe_hyphenated_names(judge=references.parse_fields):
    for raw, want in HYPHEN_REFERENCES:
        got = judge(raw)["first_author"]
        if got != want:
            yield f"parse_fields({raw[:30]!r}) gave first_author {got!r}, expected {want!r}"


# ---------------------------------------------------------------- CH-11: text preservation

def _row(n, text):
    return {"sentence_id": f"p:S{n:04d}", "source": "main", "page": 1, "section": "results", "kind": "text", "text": text}


@check("CH-11", "probe")
def probe_text_preservation(judge=renumber.text_preservation):
    old = [_row(1, "Embryos were cooled in liquid nitrogen."), _row(2, "Survival was higher in the treated group.")]
    for case, new, passages, want in (
            ("same text, new ids", [_row(5, old[0]["text"]), _row(6, old[1]["text"])], [], 0),
            ("same words, other split and case", [_row(7, "EMBRYOS were cooled in")], [{"text": "liquid nitrogen. Survival was "
             "higher in the treated group."}], 0),
            ("a sentence is gone", [_row(8, old[0]["text"])], [], 1)):
        got = judge(old, new, passages, [])["lost_total"]
        if got != want:
            yield f"text_preservation ({case}): lost_total {got}, expected {want}"


# ---------------------------------------------------------------- CH-12: page text is kept

def _without_broken_words(lines):
    """Drop the two halves of a word broken at a line end ("suc-" / "cessful"): they are not words of the stored text."""
    out, cut = [], False
    for line in lines:
        text = re.sub(r"^\S+\s*", "", line) if cut else line
        cut = bool(re.search(r"\w[-\u00ad]$", text))
        out.append(re.sub(r"\s*\S+$", "", text) if cut else text)
    return out


@check("CH-12", "paper")
def page_text_is_in_sentences(paper):
    """Each line of pages.jsonl must have its words in the stored text: renumber.text_preservation looks them up in
    passages, sentences, headings and table cells, ignoring case, hyphenation and punctuation. Page furniture is left
    out first: a line that repeats on another page (digits ignored), a link-only line and a line without any word (letters
    only count, so a price or a digit ruler is not text); printed section headings
    are found through sections.json; words broken at a line end are not compared."""
    pages = [(page.get("page"), [ln.strip() for ln in str(page.get("text") or "").splitlines() if ln.strip()])
             for page in paper.pages]
    key = lambda line: re.sub(r"\d+", "#", line.casefold())
    seen = collections.defaultdict(set)
    for number, lines in pages:
        for line in lines:
            seen[key(line)].add(number)
    sections = _sections(paper)
    for number, lines in pages:
        kept = [ln for ln in lines if len(seen[key(ln)]) == 1 and not extract.is_link_only(ln)
                and re.search(r"[^\W\d_]{2,}", ln)]  # a line without a word (a price, a digit ruler, a page number) has no text to keep
        old = [_row(n, ln) for n, ln in enumerate(_without_broken_words(kept)) if ln]
        for item in renumber.text_preservation(old, paper.sentences, paper.passages, paper.tables, sections)["lost"]:
            yield f"page {number}: no sentence has the words {item['missing_words'][:3]}"
