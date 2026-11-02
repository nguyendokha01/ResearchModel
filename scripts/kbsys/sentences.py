"""Sentences, in-text citations and figure/table mentions (Tier 1, contract §2.2).

Works on passage text only; nothing here calls an AI model. Sentence text is always an
exact slice of its passage, so every sentence stays traceable to the page it came from.
"""

import collections
import re
import unicodedata
from pathlib import Path

# Words that end with a period without ending the sentence (compared in lower case).
ABBREVIATIONS = {
    "al", "fig", "figs", "e.g", "i.e", "vs", "cf", "approx", "no", "nos", "ca", "eq", "eqs",
    "ref", "refs", "suppl", "dr", "prof", "mr", "mrs", "ms", "st", "inc", "ltd", "co", "corp",
    "jr", "sr", "vol", "pp", "resp", "sp", "spp", "var", "subsp", "ed", "eds", "viz", "max",
}
_END = re.compile(r"[.!?][\"”’')\]]*(?:\^[0-9][0-9,\-–—]*)?(?=\s+\S)")
_TOKEN_BEFORE = re.compile(r"([^\W\d_][\w.]*)[.!?]$")  # the word before the period ("μL" counts as a word)
_SENTENCE_START = re.compile(r"[A-Z0-9(\[\"“‘]")


def split_sentences(text):
    """Return [(start, end)] so that text[start:end] are the sentences, in order,
    separated only by whitespace."""
    spans, start = [], _skip_space(text, 0)
    for m in _END.finditer(text):
        end = m.end()
        nxt = _skip_space(text, end)
        if nxt >= len(text) or not _SENTENCE_START.match(text[nxt]):
            continue
        before = text[start:m.start() + 1]
        tok = _TOKEN_BEFORE.search(before)
        if tok:
            word = tok.group(1).lower()
            if word in ABBREVIATIONS or word.rstrip(".") in ABBREVIATIONS:
                continue
            if len(word) == 1 and word.isalpha():  # an initial: "M. Lindqvist", "E. coli"
                continue
        if end > start:
            spans.append((start, end))
            start = nxt
    if start < len(text.rstrip()):
        spans.append((start, len(text.rstrip())))
    return spans


def _skip_space(text, i):
    while i < len(text) and text[i].isspace():
        i += 1
    return i


# ---------------------------------------------------------------- citations

_NUM_LIST = r"\d{1,3}(?:\s*[-–—]\s*\d{1,3})?(?:\s*,\s*\d{1,3}(?:\s*[-–—]\s*\d{1,3})?)*"
BRACKET = re.compile(rf"\[({_NUM_LIST})\]")
PAREN = re.compile(rf"\(({_NUM_LIST})\)")
SUPERSCRIPT = re.compile(r"\^(\d{1,3}(?:\s*[-–—,]\s*\d{1,3})*)(?![\d+\-−])")
_NOT_A_CITATION_BEFORE_PAREN = re.compile(
    r"(?i)(?:fig(?:ure)?s?\.?|tables?|eq(?:uation)?s?\.?|steps?|panels?|lanes?|groups?|n\s*=|"
    r"p\s*[<=>]|passages?|scores?|grades?|stages?|types?|patients?|versions?)\s*$")
UNITS = {"m", "cm", "mm", "µm", "μm", "nm", "km", "ml", "l", "dl", "g", "kg", "mg", "µg", "μg",
         "ng", "s", "ft", "in", "mol", "mmol"}
_PREFIXES = r"(?:see(?:\s+also)?|e\.g\.,?|cf\.|i\.e\.,?|reviewed\s+in|for\s+(?:a\s+)?review,?\s+see)\s+"
_SURNAME = r"(?:(?:van|von|de|der|den|del|da|di|du|le|la)\s+)*[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]+"
AUTHOR_YEAR = re.compile(
    rf"^(?:{_PREFIXES})?({_SURNAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+{_SURNAME})?,?\s+"
    r"((?:19|20)\d{2}[a-z]?(?:\s*,\s*(?:(?:19|20)\d{2}[a-z]?|[a-z]\b))*)")
NARRATIVE = re.compile(
    rf"\b({_SURNAME})(?:\s+et\s+al\.?|\s+(?:and|&)\s+{_SURNAME})?\s+\(((?:19|20)\d{{2}}[a-z]?)\)")
_PARENS = re.compile(r"\(([^()]{4,300})\)")
_YEAR = re.compile(r"((?:19|20)\d{2})([a-z]?)")


def expand_numbers(text, limit=60):
    """'1,7–10' -> [1, 7, 8, 9, 10]; None if a range is implausible."""
    nums = []
    for part in re.split(r"\s*,\s*", text.strip()):
        bounds = [int(x) for x in re.split(r"\s*[-–—]\s*", part) if x]
        if len(bounds) == 2:
            lo, hi = bounds
            if not 0 < lo < hi or hi - lo > limit:
                return None
            nums += range(lo, hi + 1)
        elif len(bounds) == 1:
            nums.append(bounds[0])
    return nums or None


def _norm_name(name):
    text = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z]", "", text.lower())


class CitationContext:
    """What we know about one paper's reference list, for resolving in-text citations."""

    def __init__(self, references, style_counts=None):
        self.numbered = [r["num"] for r in references if r.get("num")]
        self.max_num = max(self.numbered) if self.numbered else 0
        self.nums = set(self.numbered)
        self.by_author_year = {}
        for i, r in enumerate(references, start=1):
            key = (_norm_name(r.get("first_author")), str(r.get("year") or ""))
            self.by_author_year.setdefault(key, []).append((r.get("num") or i, r.get("year_suffix") or ""))
        self.styles = style_counts or {}

    def uses(self, style):
        return self.styles.get(style, 0) >= 3


def count_styles(texts, ctx):
    """How often each citation style occurs in body text (used to decide what to look for)."""
    counts = {"numeric": 0, "numeric_paren": 0, "superscript": 0, "author_year": 0}
    for text in texts:
        counts["numeric"] += len(BRACKET.findall(text))
        counts["numeric_paren"] += sum(1 for m in PAREN.finditer(text) if _paren_ok(text, m, ctx))
        counts["superscript"] += sum(1 for m in SUPERSCRIPT.finditer(text) if _superscript_ok(text, m))
        counts["author_year"] += sum(len(_author_year_parts(m.group(1))) for m in _PARENS.finditer(text))
        counts["author_year"] += len(NARRATIVE.findall(text))
    return counts


def citation_style(counts, numbered):
    numeric = counts["numeric"] + counts["numeric_paren"]
    ranked = sorted((("numeric", numeric), ("superscript", counts["superscript"]),
                     ("author_year", counts["author_year"])), key=lambda kv: -kv[1])
    if ranked[0][1] < 3:
        return "none" if not numbered else "unknown"
    if ranked[1][1] >= 3 and ranked[1][1] >= ranked[0][1] / 3:
        return "mixed"
    return ranked[0][0]


def _paren_ok(text, m, ctx):
    """"(12)" is a citation when it stands apart from the previous word, is not a figure,
    step or statistic number, and every number exists in the reference list."""
    if m.start() > 0 and not text[m.start() - 1].isspace():  # "SSEA-3(1)", "hMit(1)": not citations
        return False
    before = text[max(0, m.start() - 20):m.start()]
    if _NOT_A_CITATION_BEFORE_PAREN.search(before):
        return False
    nums = expand_numbers(m.group(1))
    return bool(nums) and ctx.max_num > 0 and all(1 <= n <= ctx.max_num for n in nums)


def _superscript_ok(text, m):
    """A superscript number is a citation unless it is an exponent or a unit power."""
    before = text[:m.start()]
    if not before or before[-1].isdigit() or before[-1] in "×^-−+":
        return False
    word = re.search(r"([^\W\d_]+)$", before)
    if word and (word.group(1).lower() in UNITS or re.search(r"/(?:[a-zµμ]{1,3})$", before)):
        return False
    if word and (len(word.group(1)) == 1 or re.fullmatch(r"[α-ωΑ-Ω]+", word.group(1))):
        return False  # "χ^2", "r^2", "R^2", "σ^2": powers of a symbol, not citations
    return True


def _two_or_more(marker):
    return bool(re.search(r"\bet\s+al\b|\s(?:and|&)\s", marker))


def _author_year_parts(inner):
    return [p for p in (s.strip() for s in inner.split(";")) if AUTHOR_YEAR.match(p)]


def find_citations(text, ctx, section):
    """Citations in one sentence: [{marker, ref_nums, style, confidence, start, end}]."""
    if section in ("front_matter", "references"):
        return []
    found = []

    def add(marker_span, nums, style, confidence):
        start, end = marker_span
        found.append({"marker": text[start:end], "ref_nums": nums, "style": style,
                      "confidence": confidence, "start": start, "end": end})

    def numeric_confidence(nums):
        return "high" if nums and all(n in ctx.nums for n in nums) else "low"

    if ctx.numbered:
        for m in BRACKET.finditer(text):
            nums = expand_numbers(m.group(1)) or []
            add(m.span(), nums, "numeric", numeric_confidence(nums))
        if ctx.uses("numeric_paren"):
            for m in PAREN.finditer(text):
                if _paren_ok(text, m, ctx):
                    nums = expand_numbers(m.group(1))
                    add(m.span(), nums, "numeric", numeric_confidence(nums))
        if ctx.uses("superscript"):
            for m in SUPERSCRIPT.finditer(text):
                if _superscript_ok(text, m):
                    nums = expand_numbers(m.group(1).replace(" ", "")) or []
                    ok = nums and all(n <= ctx.max_num for n in nums)
                    if ok:
                        add(m.span(), nums, "superscript", numeric_confidence(nums))
    if ctx.by_author_year and (not ctx.numbered or ctx.uses("author_year")):
        for m in _PARENS.finditer(text):
            offset = m.start(1)
            for part in m.group(1).split(";"):
                stripped = part.strip()
                am = AUTHOR_YEAR.match(stripped)
                if am:
                    p_start = offset + part.index(stripped)
                    _add_author_year(add, ctx, am.group(1), am.group(2), (p_start, p_start + am.end()),
                                     _two_or_more(am.group(0)))
                offset += len(part) + 1
        for m in NARRATIVE.finditer(text):
            _add_author_year(add, ctx, m.group(1), m.group(2), m.span(), _two_or_more(m.group(0)))
    found.sort(key=lambda c: c["start"])
    return found


def _add_author_year(add, ctx, surname, years_text, span, looks_like_citation):
    """Resolve "Surname (et al.) year[, year]" by first-author surname + year (+ a/b suffix).
    Without a matching reference it is kept only if it clearly is a citation ("et al.", two authors)."""
    nums, confident, labels = [], True, []
    last_year = None
    for part in re.split(r"\s*,\s*", years_text):
        ym = _YEAR.fullmatch(part)
        if ym:
            last_year, suffix = ym.group(1), ym.group(2)
        elif re.fullmatch(r"[a-z]", part) and last_year:
            suffix = part
        else:
            continue
        labels.append(last_year + suffix)
        candidates = ctx.by_author_year.get((_norm_name(surname), last_year), [])
        if suffix:
            narrowed = [c for c in candidates if c[1] == suffix]
            candidates = narrowed or candidates
        if len(candidates) != 1:
            confident = False
        nums += [c[0] for c in candidates]
    if labels and (nums or looks_like_citation):
        add(span, nums, "author_year", "high" if confident and nums else "low")


# ---------------------------------------------------------------- numbers

# A number as printed; not part of a name such as "CD105", "SSEA-3", "IL-6" or "H2O2".
# A leading minus is retained only when it is a sign (start/space/"(" before it), not
# a range or a hyphenated name. A bare decimal (".5") may follow a letter because PDFs
# drop spaces ("from 0% to.5%"). PDF damage is tolerated: a comma not preceded by a digit
# ("P,0.005" for "P<0.005", ",10%" for "<10%", "Muse-poor,12.5%") and a run of 5+ lowercase
# letters glued to the digits ("injectionof150mg") still start a number.
NUMBER = re.compile(
    r"(?:(?P<sign>(?:(?<!\S)|(?<=\())[-−–])"
    r"(?P<signed_whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<signed_frac>\d+))?"
    r"|(?:(?<![\w.,])|(?<=[^\d\s],)|(?<=^,)|(?<=\s,)|(?<=[a-z]{5}))(?<![A-Za-z][-−–])"
    r"(?P<whole>\d{1,3}(?:,\d{3})+|\d+)(?:\.(?P<frac>\d+))?"
    r"|(?<![\d.,])\.(?P<bare>\d+))(?![\d])")


def _norm_number(m):
    whole = m.group("signed_whole") or m.group("whole")
    frac = m.group("signed_frac") or m.group("frac")
    bare = m.group("bare")
    sign = "-" if m.group("sign") else ""
    if bare is not None:
        whole, frac = "0", bare
    whole = (whole or "0").replace(",", "").lstrip("0") or "0"
    frac = (frac or "").rstrip("0")
    return sign + (f"{whole}.{frac}" if frac else whole)


def numbers(text):
    """The set of numbers in a text, normalised: "1,230" -> "1230", ".50" -> "0.5", "41.0" -> "41"."""
    return {_norm_number(m) for m in NUMBER.finditer(text or "")}


# ---------------------------------------------------------------- figure/table mentions

MENTION = re.compile(
    r"\b(Supplementa(?:ry|l)\s+|Suppl\.?\s+)?(Fig(?:ure)?s?\.?|Tables?)\s*(S?)(\d{1,2})[A-Za-z]?"
    r"((?:\s*(?:[,–\-]|and|&)\s*S?\d{1,2}[A-Za-z]?\b)*)")


def mentions(text, paper_id):
    ids = []
    for m in MENTION.finditer(text):
        supp = bool(m.group(1) or m.group(3))
        kind = "T" if m.group(2).lower().startswith("tab") else "F"
        numbers = [int(m.group(4))] + [int(n) for n in re.findall(r"\d{1,2}", m.group(5) or "")]
        if "–" in (m.group(5) or "") or "-" in (m.group(5) or ""):
            numbers = list(range(numbers[0], max(numbers) + 1)) if max(numbers) - numbers[0] < 20 else numbers
        for n in numbers:
            oid = f"{paper_id}:{kind}{'S' if supp else ''}{n}"
            if oid not in ids:
                ids.append(oid)
    return ids


# ---------------------------------------------------------------- building sentences

TERMINAL = re.compile(r"[.!?][\"”’')\]]*(?:\^[0-9][0-9,\-–—]*)?$")


def layout_slice(blocks, total, lo, hi):
    """The paragraph blocks `[n_chars, size, y0, y1]` of a text of `total` characters that overlap lo..hi, as
    (overlap, size, y0, y1). The stored lengths are scaled to `total` (hyphen resolution changes the text after they
    were counted), so the same blocks give the same answer for a passage and for the slices of it."""
    scale = total / (sum(b[0] for b in blocks) or 1)
    out, end = [], 0.0
    for n, *rest in blocks:
        begin, end = end, end + n * scale
        if min(end, hi) - max(begin, lo) > 1e-9:
            out.append((min(end, hi) - max(begin, lo), *rest))
    return out


_BODY_SIZE_SECTIONS = {"abstract", "introduction", "methods", "results", "results_discussion", "discussion", "conclusion",
                       "main_text"}


def _size_ref(blocks):
    """Reference font size of (size, n_chars) blocks: the character-weighted mean size of the blocks inside the modal
    0.5 pt bin (pt, 0.01); None when there are none."""
    votes = collections.Counter()
    for size, n in blocks:
        votes[round(size * 2) / 2] += n
    if not votes:
        return None
    mode = votes.most_common(1)[0][0]
    inside = [(size, n) for size, n in blocks if round(size * 2) / 2 == mode]
    return round(sum(size * n for size, n in inside) / (sum(n for _, n in inside) or 1), 2)


def layout_reference_sizes(passages):
    """{section: reference font size} for the layout signal (CH-08): a section is measured against its own text blocks,
    front matter (title, authors, affiliations: its own mode is the small print) against the paper body (all body
    sections together), a section without text blocks against the paper body (key None). Text passages with `lay` only."""
    by_section = collections.defaultdict(list)
    for p in passages:
        if p.get("kind") == "text":
            by_section[p["section"]] += [(b[1], b[0]) for b in p.get("lay") or ()]
    body = _size_ref([v for s in _BODY_SIZE_SECTIONS for v in by_section.get(s, ())])
    refs = {s: _size_ref(v) or body for s, v in by_section.items()}
    if body:
        refs["front_matter"] = body
    refs[None] = body
    return refs


def _layout_fields(p, start, end, ref):
    """lay_size (the largest block size among the blocks that cover 20 % of the sentence, else the mean of its blocks;
    pt, 0.01), lay_ref (the section reference) and lay_y ([top, bottom] share of the page) of one sentence."""
    blocks = layout_slice(p["lay"], len(p["text"]), start, end)
    total = sum(b[0] for b in blocks)
    if not blocks or not ref or not total:
        return {}
    big = [b[1] for b in blocks if b[0] >= LAYOUT_SHARE * total]
    return {"lay_size": round(max(big) if big else sum(b[0] * b[1] for b in blocks) / total, 2), "lay_ref": ref,
            "lay_y": [min(b[2] for b in blocks), max(b[3] for b in blocks)]}


def build(paper_id, passages, ctx):
    """Sentence records (without seq/ids) for every text and caption passage, in order. A sentence of a passage that
    carries `lay` (a PDF read since CH-08) also gets `lay_size` / `lay_ref` / `lay_y`; other rows get none."""
    out = []
    refs = layout_reference_sizes(passages)
    for p in passages:
        if p.get("kind") not in ("text", "caption"):
            continue
        for start, end in split_sentences(p["text"]):
            text = p["text"][start:end]
            source = p.get("source", "main")
            out.append({
                "passage_id": p["passage_id"], "source": source, "page": p["page"],
                "section": p["section"], "subsection": p.get("subsection"), "kind": p["kind"],
                "text": text, "start": start, "end": end,
                "citations": find_citations(text, ctx, p["section"]) if source == "main" else [],
                "mentions": mentions(text, paper_id),
                **(_layout_fields(p, start, end, refs.get(p["section"], refs[None])) if p.get("lay") else {}),
            })
    return out


NOT_BODY = {"references", "front_matter", "acknowledgements", "funding", "author_contributions", "conflicts",
            "data_availability", "notes"}
_CONTINUATION_START = re.compile(r"[a-z(\[,;:)–\-/]|\d+(?:\.\d+)?\s*(?:%|[a-zµμ°]|[,;)]|and\b|or\b|$)")


_LAYOUT_KINDS = {"caption", "figure", "figure_block", "figure_caption", "figure_legend",
                 "table", "table_block", "table_caption", "table_row"}
_READ_LINK_CACHE = {}


# CHR-04: the one table of look-alike symbols; canonical forms are "°C" and "Δ" (the first of each tuple).
_DEGREE_C = re.compile(r"\^?[°˚◦º]C|℃")
_DEGREE_FORMS = ("°C", "˚C", "◦C", "^◦C", "ºC", "℃")
_DELTA_FORMS = ("Δ", "∆")  # U+0394 | U+2206


def fold_search_glyphs(value):
    """Canonicalize look-alike glyphs for comparisons without changing source text."""
    return _DEGREE_C.sub("°C", str(value or "").replace(_DELTA_FORMS[1], _DELTA_FORMS[0]))


# U6d (DECISIONS 2026-10-24): British/American spellings, applied to the whole term in one direction at a time. Anchored to
# word parts; a generated non-word ("precize") only adds an OR alternative that matches nothing.
_SUFFIX = r"(?=(?:e|es|ed|ing|er|ers|ation|ations)\b)"
_TO_US = ((rf"(?<=\w{{3}})is{_SUFFIX}", "iz"), (rf"(?<=\w{{2}})ys{_SUFFIX}", "yz"), (r"\bhaem(?=\w)", "hem"),
          (r"aemi(?=[ac])", "emi"), (r"\boedem", "edem"), (r"\boestr", "estr"), (r"\banaesth", "anesth"),
          (r"\bpaediatr", "pediatr"), (r"\bfoet", "fet"))
_TO_UK = ((rf"(?<=\w{{3}})iz{_SUFFIX}", "is"), (rf"(?<=\w{{2}})yz{_SUFFIX}", "ys"), (r"\bhem(?=[ao])", "haem"),
          (r"(?<=[^\W\da_])emi(?=[ac])", "aemi"), (r"\bedem", "oedem"), (r"\bestr(?=[aio])", "oestr"),
          (r"\banesth", "anaesth"), (r"\bpediatr", "paediatr"), (r"\bfet(?=[au])", "foet"))
# A Greek letter name and its symbol, only when attached to a neighbouring token (gamma-H2A.X, γH2A.X, TNF-alpha, IL-1β):
# a lone "delta" never becomes a one-letter token. Lower-case symbols only, so CHR-04's Δ/∆ stays as it is.
_GREEK = {"alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "kappa": "κ", "mu": "μ"}
_GREEK_NAME = {**{s: n for n, s in _GREEK.items()}, "µ": "mu"}  # U+00B5 micro sign is the same letter as U+03BC
# ponytail: mu only as a hyphenated lead (mu-opioid); glued μM/µg are units, a μ-M form would read as "μ m" in FTS, and a
# trailing mu (4-MU, AMU) would become 4-μ, which matches "4-μm".
_G = "(?i:alpha|beta|gamma|delta|kappa|mu)|[αβγδκμµ]"
_GREEK_PART = re.compile(rf"(?<![^\W\d_])(?P<lead>{_G})(?:-(?=\w)|(?<!(?i:mu))(?<![μµ])(?=[A-Z0-9]))"
                         rf"|(?:(?<=\w)-|(?<=[A-Z0-9]))(?P<trail>(?i:alpha|beta|gamma|delta|kappa)|[αβγδκ])(?![^\W\d_])")


def _greek_forms(value):
    """Name and symbol, each with a hyphen and glued (unicode61 splits at the hyphen: γ-H2A.X and γH2A.X differ)."""
    def form(name, hyphen):
        def sub(m):
            letter = (m["lead"] or m["trail"]).lower()
            letter = _GREEK_NAME.get(letter, letter)
            out = letter if name else _GREEK[letter]
            return (out + hyphen) if m["lead"] else (hyphen + out)
        return _GREEK_PART.sub(sub, value)
    return [form(n, h) for n in (True, False) for h in ("-", "")] if _GREEK_PART.search(value) else []


def spelling_variants(value):
    """The term, its all-American and all-British spellings, and the Greek name/symbol forms of each."""
    us, uk = value, value
    for pattern, repl in _TO_US:
        us = re.sub(pattern, repl, us, flags=re.I)
    for pattern, repl in _TO_UK:
        uk = re.sub(pattern, repl, uk, flags=re.I)
    return list(dict.fromkeys(v for s in (value, us, uk) for v in (s, *_greek_forms(s))))


def glyph_variants(value):
    """Search-only spellings of a query term: NFC (CHR-01), folded, every look-alike form (CHR-04) and the
    spelling variants above.

    The one place all query paths (FTS expressions, highlight, regex patterns) expand a term."""
    value = unicodedata.normalize("NFC", str(value or ""))
    out = []
    for spelled in spelling_variants(value):
        folded = fold_search_glyphs(spelled)
        base = [folded, *(folded.replace("°C", form) for form in _DEGREE_FORMS[1:] if "°C" in folded)]
        out += [spelled, *base, *(v.replace("Δ", "∆") for v in base)]
    return list(dict.fromkeys(out))


def fts_variants(value, glyph_tokens=False):
    """glyph_variants for an FTS5 phrase. An index made before db.FTS_TOKENIZE drops U+2206 and U+2103 as separators, so
    there a generated form with them is a shorter phrase ("∆G" is the token "g", "37 ℃" is "37") that matches unrelated
    text and only the form the researcher typed keeps them. With them as token characters (glyph_tokens, read from
    db.fts_glyph_tokens) "37℃" is one token and "37 ℃" two, so both spacings are searched."""
    value, *rest = glyph_variants(value)
    if not glyph_tokens:
        return [value, *(v for v in rest if _DELTA_FORMS[1] not in v and _DEGREE_FORMS[-1] not in v)]
    return list(dict.fromkeys([value, *rest, *(re.sub(r"(?<=\d)\s*℃", gap + "℃", v)
                                              for v in (value, *rest) for gap in ("", " "))]))


_LAYOUT_TEXT = re.compile(
    r"(?ix)(?:\b(?:licen[cs]e|open\s+access|creative\s+commons|affiliation|department|"
    r"university|institute|correspondence|e[- ]?mail)\b|\u00a9|\bdoi\.org/|@)"
)
_SOURCE_GLYPH_SUSPECT = re.compile(r"(?<!\w)\d+\?\d+(?!\w)")
_VERB_WORDS = {
    "am", "are", "be", "been", "being", "can", "could", "did", "do", "does", "doing",
    "exhibit", "exhibited", "found", "has", "have", "had", "is", "may", "might", "occur",
    "observed", "remain", "remains", "result", "show", "showed", "shows", "were", "was",
    "will", "would", "using", "used", "yield", "yields",
}


# ponytail: a block of at least this share of the sentence sets its size (a body tail glued to a 7 pt legend stays body);
# a sentence of many tiny blocks (table cells) has none and is measured by its mean. Chosen on runtime/g4-2/ch08/redo/rescore.md:
# 0.10-0.20 keep 5 mixed body rows, 0.30 loses two; the mean fallback is what separates paper A S0189 (table cells).
LAYOUT_SHARE = 0.20
# ponytail: 0.92 = the largest lay_size / lay_ref that still catches the page furniture of 23 papers (paper C affiliations
# 0.91) while a smaller-font Methods section (ratio 1.0 against its own reference) is never cut; rescore.md, probe.md.
# Per-journal tuning would need a calibration set per layout.
LAYOUT_SIZE_RATIO = 0.92
# ponytail: a journal/citation line at the foot of page 1 can print at or above body size (paper G 1.06, paper H 1.18);
# position alone would also cut the last body lines of page 1 (top 0.86-0.87 in three papers: ratio 1.00), so both
# must hold (DECISIONS 2026-10-20, runtime/g4-2/ch08/foot/p1_rows.tsv). Calibrated on 23 papers.
LAYOUT_FOOTER_TOP = 0.85
LAYOUT_FOOTER_RATIO = 1.03


def _layout_interruption(sentence):
    """Return whether a row is a non-body layout item that may sit inside a sentence.

    A row that carries `lay_size` and `lay_ref` (extracted since CH-08) is judged by structure alone: its kind or flags,
    a font smaller than its section's, or a page-1 footer line (top below LAYOUT_FOOTER_TOP of the page and a font larger
    than its section's). A row without them (stored before the re-extraction) keeps the text rule below."""
    kind = str(sentence.get("kind") or "").casefold()
    if kind in _LAYOUT_KINDS or kind.endswith("_caption") or kind.endswith("_legend"):
        return True
    # Older/synthetic inventories use explicit flags instead of kind.
    if any(sentence.get(flag) for flag in (
        "caption", "is_caption", "figure", "is_figure", "table", "is_table")) or bool(sentence.get("in_table")):
        return True
    if sentence.get("lay_size") is not None and sentence.get("lay_ref"):
        ratio, top = sentence["lay_size"] / sentence["lay_ref"], (sentence.get("lay_y") or (0,))[0]
        return ratio < LAYOUT_SIZE_RATIO or (sentence.get("page") == 1 and top > LAYOUT_FOOTER_TOP
                                             and ratio > LAYOUT_FOOTER_RATIO)
    if table_text_suspect(sentence) and not _lower_sentence(sentence):
        return True
    text = str(sentence.get("text") or "")
    words = re.findall(r"[A-Za-z\u00c0-\u00ff]+", text)
    # many numbers make a row layout, unless it is a verb-bearing sentence of mostly lower-case words (a figure panel is not)
    return bool(_LAYOUT_TEXT.search(text) or len(text) > 600 or
                (len(words) >= 20 and len(numbers(text)) / max(len(words), 1) >= 0.20
                 and not (_has_verb(text) and 2 * sum(w.islower() for w in words) >= len(words))))


def _lower_sentence(sentence):
    """A lower-case start of 6+ words that ends a sentence is a Methods sentence, not a table row (its numbers are doses)."""
    text = str(sentence.get("text") or "").strip()
    return len(text.split()) >= 6 and text[:1].islower() and bool(TERMINAL.search(text))


_AFFILIATION = re.compile(r"(?i)\b(?:university|institute|department)\b")


def _affiliation_prose(sentence):
    """Layout only through "University/Institute/Department" while it is a plain sentence with a verb.

    Half of its words are lower-case: an affiliation line ("Department of Applied Biology, Kyoto University") is not."""
    text = str(sentence.get("text") or "")
    words = re.findall(r"[A-Za-zÀ-ÿ]+", text)
    return (_AFFILIATION.search(text) is not None and str(sentence.get("kind") or "text").casefold() == "text"
            and not sentence.get("in_table") and _has_verb(text) and len(text.split()) >= 8
            and 2 * sum(w.islower() for w in words) >= len(words)
            and not _layout_interruption({**sentence, "text": _AFFILIATION.sub(" ", text)}))


def _body_fragment(sentence, head=None):
    """Only ordinary body text can be either side of a persisted continuation.

    A sentence that is layout only through an affiliation word is body as a head, and as a target (head given)
    when it reads as the rest of that head."""
    return str(sentence.get("kind") or "text").casefold() == "text" and (
        not _layout_interruption(sentence)
        or (_affiliation_prose(sentence)
            and (head is None or _starts_like_continuation(sentence.get("text"), head.get("text")))))


def _next_body_fragment(sentences, start):
    """Find the next body row, but do not jump over unrelated text."""
    # A front-matter block (citation line, licence, received/accepted dates) can also sit inside a body sentence.
    from_front = sentences[start].get("section") == "front_matter"
    for candidate in sentences[start + 1:]:
        if candidate.get("section") == "front_matter" and not from_front:
            continue
        if _body_fragment(candidate, sentences[start]):
            return candidate
        if not _layout_interruption(candidate):
            return None
    return None


_FUNCTION_WORDS = {"the", "a", "an", "of", "in", "on", "at", "by", "for", "to", "with", "and", "or", "although", "while",
                   "whereas", "that", "which", "from", "as", "than", "between", "among", "into", "its", "their", "this",
                   "these", "those", "containing"}  # no "because": the checker would see a true link skip a figure legend


def _starts_like_continuation(text, previous):
    text = str(text or "").lstrip()
    previous = str(previous or "").rstrip()
    if _CONTINUATION_START.match(text) or (text and previous.endswith("-")):
        return True
    # A PDF can capitalise the first word after a page/column boundary. Keep
    # this conservative: a complete-looking short row must not absorb the
    # next sentence, while real fragments in the sweep have at least six words
    # or end in a function word ("The", "Otherwise, a", "a change of").
    words = re.findall(r"\b\w+\b", previous)
    if not re.match(r"[A-Z][A-Za-z0-9-]*", text) or (len(words) < 6 and (words[-1:] or [""])[0].casefold() not in _FUNCTION_WORDS):
        return False
    return bool(previous.endswith(",") or previous[-1:].islower())


def source_glyph_suspect(sentence):
    """Whether a source row contains a likely PDF glyph defect (read time only)."""
    return bool(_SOURCE_GLYPH_SUSPECT.search(str(sentence.get("text") or "")))


def _has_verb(text):
    for word in re.findall(r"[A-Za-z]+", str(text or "").casefold()):
        if word in _VERB_WORDS or (len(word) >= 6 and word.endswith(("ed", "ing"))):
            return True
    return False


def table_text_suspect(sentence):
    """Return a read-time diagnostic for body text that resembles a table row."""
    if str(sentence.get("kind") or "text").casefold() != "text":
        return False
    text = str(sentence.get("text") or "").strip()
    if not text or _has_verb(text):
        return False
    cells = [part.strip() for part in re.split(r"\s*[|\u00a6]\s*", text)]
    numeric = numbers(text)
    return (len(cells) >= 3 or len(numeric) >= 4 or
            (len(numeric) >= 3 and len(text) <= 220) or
            (len(text) > 600 and len(numeric) >= 2))


_DISPLAY_HEADING = re.compile(
    r"(?i)^(?:abstract|introduction|background|materials?\s+and\s+methods?|methods?|"
    r"results?(?:\s+and\s+discussion)?|discussion|conclusions?|acknowledg(?:e)?ments?|"
    r"funding|data\s+availability(?:\s+statement)?|figure\s+legends?|supplementary)"
    r"(?=\s*[:.\-\u2014\u2013]?\s+[A-Z(])"
)


def display_text(sentence):
    """Return a UI-only rendering with a glued leading section heading separated."""
    text = str(sentence.get("text") if isinstance(sentence, dict) else sentence or "")
    match = _DISPLAY_HEADING.match(text.strip())
    if not match or not text[match.end():].strip():
        return text
    heading = text[:match.end()].rstrip(" :.-\u2014\u2013")
    body = text[match.end():].lstrip(" :.-\u2014\u2013")
    return f"{heading}\n{body}" if body else text


def read_time_flags(sentence):
    """Return non-persisted diagnostics used by readers and renderers."""
    flags = {}
    if source_glyph_suspect(sentence):
        flags["source_glyph_suspect"] = True
    if table_text_suspect(sentence):
        flags["table_text_suspect"] = True
    return flags


def join_fragments(left, right, left_row=None, right_row=None):
    """Join source fragments while preserving a word cut within one passage."""
    left, right = str(left or "").rstrip(), str(right or "").lstrip()
    same_span = (left_row and right_row and left_row.get("passage_id") == right_row.get("passage_id")
                 and isinstance(left_row.get("end"), int) and isinstance(right_row.get("start"), int)
                 and left_row["end"] == right_row["start"])
    right_word = re.match(r"[a-z]+", right)
    likely_word_cut = bool(right_word and len(right_word.group(0)) <= 2)
    if (same_span or likely_word_cut) and left[-1:].isalpha() and right[:1].islower():
        return left + right
    return (left + " " + right).strip()


def link_page_breaks_copy(sentences):
    """Return an in-memory copy with page/layout continuations linked.

    The copy is deliberately detached from the stored sentence rows.  This lets
    readers repair old extraction output at read time without changing the
    source revision or any accepted analysis that refers to it.
    """
    linked = [dict(row) for row in sentences]
    for row in linked:
        row.update(read_time_flags(row))
    _link_page_breaks(linked)
    return linked


def cached_linked_rows(rows, path=None):
    """Link rows in memory, caching by the source file's current signature.

    ``path`` is the per-paper ``sentences.jsonl`` path.  A changed mtime or size
    gets a fresh cache entry; no cache result is written back to disk.
    """
    if path is None:
        return link_page_breaks_copy(rows)
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return link_page_breaks_copy(rows)
    path_key = str(path)
    key = (path_key, stat.st_mtime_ns, stat.st_size)
    cached = _READ_LINK_CACHE.get(key)
    if cached is None:
        # Keep only the current signature for a paper so a replaced file is
        # genuinely invalidated rather than merely bypassing an old entry.
        for old_key in list(_READ_LINK_CACHE):
            if old_key[0] == path_key and old_key != key:
                del _READ_LINK_CACHE[old_key]
        cached = tuple(link_page_breaks_copy(rows))
        _READ_LINK_CACHE[key] = cached
    return [dict(row) for row in cached]


def _link_page_breaks(sentences):
    """Link body fragments split by a page/layout boundary without changing IDs.

    Caption, figure and table rows may occur between the fragments. They are skipped,
    but unrelated body text is a hard boundary. Each explicit link is marked with
    ``context_basis=source_continuation``; this is source order provenance, not an
    inferred scientific relationship.
    """
    by_id = {row.get("sentence_id"): row for row in sentences}
    for row in sentences:
        target = by_id.get(row.get("continues"))
        if row.get("continues") and (_layout_interruption(row) or target is None
                                     or _layout_interruption(target)):
            row.pop("continues", None)
            if row.get("context_basis") == "source_continuation":
                row.pop("context_basis", None)
    for i, a in enumerate(sentences):
        if not _body_fragment(a) or a.get("continues") or a.get("section") in NOT_BODY:
            continue
        if TERMINAL.search(str(a.get("text") or "").strip()):
            continue
        b = _next_body_fragment(sentences, i)
        if b is None or b.get("source", "main") != a.get("source", "main") \
                or b.get("section") != a.get("section"):
            continue
        if _is_fragment(b) and re.search(r"[.!?)\]]$", str(b.get("text") or "").strip()) \
                and (b.get("citations") or b.get("mentions") or re.match(r"[(\[^\d]", str(b.get("text") or ""))
                 or (re.search(r"[A-Za-z]{2,}", str(b.get("text") or "")) and _different_source_fragment(a, b)
                     and not re.search(r"\b[A-Z][a-z]+\b", str(b.get("text") or "")))):
            # "[28]", "(Rudenko and Tsai, 2014).", "3C).", "SHL.": the tail of the sentence before it
            # (a capitalised word, "Antibodies." or "S1 Dataset.", is a run-in heading or a label)
            a["continues"] = b["sentence_id"]
            a["context_basis"] = "source_continuation"
        elif (_different_source_fragment(a, b) or _adjacent_same_passage(a, b)) \
                and _starts_like_continuation(b.get("text"), a.get("text"))                 and (not _front_matter_between(sentences, i, b) or _CONTINUATION_START.match(str(b.get("text") or "").lstrip())):
            # across a front-matter block only a lower-case/punctuation start counts (a keyword line is not a sentence start)
            a["continues"] = b["sentence_id"]
            a["context_basis"] = "source_continuation"


def _front_matter_between(sentences, i, b):
    for row in sentences[i + 1:]:
        if row is b:
            return False
        if row.get("section") == "front_matter" and sentences[i].get("section") != "front_matter":
            return True
    return False


def _different_source_fragment(left, right):
    return left.get("passage_id") != right.get("passage_id")


def _adjacent_same_passage(left, right):
    return (left.get("passage_id") == right.get("passage_id")
            and isinstance(left.get("end"), int) and isinstance(right.get("start"), int)
            and left["end"] == right["start"])


def link_page_breaks(sentences):
    """Mutating compatibility wrapper around :func:`link_page_breaks_copy`."""
    linked = link_page_breaks_copy(sentences)
    for original, updated in zip(sentences, linked):
        original.clear()
        original.update(updated)


def _is_fragment(sentence):
    """Fewer than 2 words once citation markers and figure/table mentions are removed."""
    text = sentence["text"]
    for c in sorted(sentence.get("citations") or [], key=lambda c: -c["start"]):
        text = text[:c["start"]] + " " + text[c["end"]:]
    text = MENTION.sub(" ", text)
    return len(re.findall(r"[A-Za-z]{2,}", text)) < 2
