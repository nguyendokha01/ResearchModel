"""Text extraction from PDFs: pages, sections, passages, DOI and title hints.

Only one PDF is open at a time; everything here works page by page.
"""

import hashlib
import re
import unicodedata

import pymupdf

from .sentences import layout_slice

# No automatic de-hyphenation: raw page text stays faithful to the PDF, and
# line-break hyphens in passages are resolved by resolve_hyphens() below.
TEXT_FLAGS = pymupdf.TEXTFLAGS_TEXT

# Canonical section name -> regex matching a whole heading line.
SECTION_PATTERNS = [
    ("abstract", r"abstract|summary|graphical abstract"),
    ("introduction", r"introduction|background"),
    ("methods", r"(materials?|patients|subjects|animals)\s*(and|&)\s*methods?|methods?\s*(and|&)\s*materials?"
                r"|methods?|methodology|experimental\s+(procedures?|section|design)|study\s+design"
                r"|star\W*methods|online\s+methods"),
    ("results_discussion", r"results\s*(and|&)\s*discussion"),
    ("results", r"results?"),
    ("discussion", r"discussion|general\s+discussion"),
    ("conclusion", r"conclusions?|concluding\s+remarks|summary\s+and\s+conclusions?"),
    ("acknowledgements", r"acknowledge?ments?"),
    ("funding", r"funding(\s+information|\s+sources?)?|financial\s+support|grant\s+support"),
    ("author_contributions", r"author\W?s?\W?\s*contributions?|credit\s+authorship.*"),
    ("conflicts", r"(conflicts?|declarations?)\s+of\s+(competing\s+)?interests?|competing\s+interests?"
                  r"|disclosures?|declaration\s+of\s+competing\s+interest|declarations"
                  r"|ethics\s+(approval\s+)?and\s+consent(\s+to\s+participate)?|consent\s+(for|to)\s+publication"),
    ("data_availability", r"data\s+availability(\s+statement)?|availability\s+of\s+data.*"),
    ("references", r"references(\s+and\s+notes)?|bibliography|literature\s+cited|works\s+cited"),
    ("supplementary", r"supplementary\s+(materials?|information|data|figures?)|supporting\s+information|appendix"
                      r"|supplementary\s+figures?,?\s+(?:and\s+)?tables?(?:,?\s+and\s+files?)?"),
    ("figure_legends", r"figure\s+legends?|legends?\s+to\s+figures?|figure\s+captions?|"
                       r"figures?,?\s+tables?,?\s+and\s+(?:supplemental|supplementary)\s+files?"),
]
_SECTION_RES = [(name, re.compile(rf"(?:{pat})", re.I)) for name, pat in SECTION_PATTERNS]
_NUMBERING = re.compile(r"^\s*(?:\d+(?:\.\d+)*\.?|[IVX]+\.|[A-H]\.)\s*")
# A caption starts with its label followed by punctuation or a capitalised word:
# "Figure 2. Muse cells…", "Table 1 Patients' characteristics". Body text such as
# "Figure 1C shows…" is not a caption.
_CAPTION = re.compile(r"^(?i:(?:supplementa(?:ry|l)\s+)?(?:fig(?:ure)?s?\.?|table|scheme))\s*S?\d+[A-Za-z]?"
                      r"(?:\s*[.:|—–-]|\s+[A-Z(]|\s*$)")
_SENTENCE_END = re.compile(
    r"(?<=[.!?])(?:\^[0-9]+(?:[,\-\u2013\u2014][0-9]+)*)?(?=\s+[A-Z(\[])"
)

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>]+)", re.I)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def clean_spaces(text):
    return re.sub(r"\s+", " ", text).strip()


SOFT = chr(0xAD)  # marks a hyphen at a line break, resolved once the whole paper is read
_LINE_HYPHEN = re.compile(r"(?<=\w)-$")
_SOFT_WORD = re.compile(rf"(\w+){SOFT}(\w+)")
_KEEP_SPACE_AFTER = {"and", "or", "to", "nor", "versus", "vs"}


def join_lines(lines):
    """Join lines (or consecutive text blocks). A line ending in "word-":
    - followed by a lowercase letter is marked with SOFT for later resolution;
    - followed by a capital or digit keeps the hyphen ("Sigma-Aldrich", "100-200")."""
    out = ""
    for line in lines:
        if out.endswith("-") and _LINE_HYPHEN.search(out) and line[:1].islower():
            out = out[:-1] + SOFT + line
        elif out.endswith("-") and _LINE_HYPHEN.search(out) and line[:1].isalnum():
            out = out + line
        else:
            out = f"{out} {line}" if out else line
    return out


def build_vocabulary(page_texts):
    words, compounds = set(), set()
    for raw in page_texts:
        # ignore words broken across lines (by "-" or by a PDF soft hyphen U+00AD)
        text = re.sub(r"\w+[-\xad]\s*\n\s*\w+", " ", raw).replace("\xad", "")
        words.update(w.lower() for w in re.findall(r"[A-Za-z]{2,}", text))
        compounds.update(c.lower() for c in re.findall(r"[A-Za-z]+-[A-Za-z]+", text))
    return words, compounds


def resolve_hyphens(text, vocab):
    """Decide for each line-break hyphen whether it belongs to the word."""
    words, compounds = vocab

    def pick(m):
        a, b = m.group(1), m.group(2)
        if (a + b).lower() in words:
            return a + b                   # "mito-" + "chondrial"
        if f"{a}-{b}".lower() in compounds:
            return f"{a}-{b}"              # "vitrified-" + "warmed"
        if b.lower() in _KEEP_SPACE_AFTER:
            return f"{a}- {b}"             # "pre- and post-"
        if len(a) >= 3 and len(b) >= 3 and a.lower() in words and b.lower() in words:
            return f"{a}-{b}"
        return a + b

    return _SOFT_WORD.sub(pick, text).replace(SOFT, "")


def normalize_title(title):
    if not title:
        return ""
    text = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_heading(line):
    """Return (section_name, remainder) if the line is a section heading."""
    raw = line.strip()
    if not raw or len(raw) > 70:
        return None
    text = _NUMBERING.sub("", raw).strip().rstrip(":.").strip()
    for name, rx in _SECTION_RES:
        if rx.fullmatch(text):
            return name, ""
    # Back-matter headings are sometimes printed as a run-in label followed by
    # their first sentence.  This affects new imports only; old sentence IDs
    # are repaired by the read-time layer.
    for name, prefix in (
            ("acknowledgements", r"acknowledge?ments?"),
            ("funding", r"funding(?:\s+(?:information|sources?))?"),
            ("data_availability", r"data\s+availability(?:\s+statement)?"),
            ("figure_legends", r"figure\s+legends?|figures?,?\s+tables?,?\s+and\s+(?:supplemental|supplementary)\s+files?"),
            ("supplementary", r"supplementary\s+(?:materials?|information|data|figures?)")):
        match = re.match(rf"^(?:{prefix})(?:\s*[:.\-\u2014\u2013]\s*|\s+)(.+)$", raw, re.I)
        if match:
            return name, match.group(1).strip()
    # "Abstract  Background: ..." on one line
    m = re.match(r"^(abstract|summary)\b[\s:.\-—]+(.{20,})$", raw, re.I)
    if m:
        return "abstract", m.group(2)
    return None


def _split_long(text, max_chars):
    if len(text) <= max_chars:
        return [text]
    parts, current = [], ""
    sentence_parts, start = [], 0
    for match in _SENTENCE_END.finditer(text):
        sentence_parts.append(text[start:match.end()].strip())
        start = match.end()
        while start < len(text) and text[start].isspace():
            start += 1
    sentence_parts.append(text[start:].strip())
    for sentence in (part for part in sentence_parts if part):
        if current and len(current) + len(sentence) + 1 > max_chars:
            parts.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
        while len(current) > max_chars:  # a single enormous "sentence"
            parts.append(current[:max_chars])
            current = current[max_chars:]
    if current:
        parts.append(current)
    return parts


class Extraction:
    """Everything extracted from one PDF."""

    def __init__(self):
        self.pages = []        # [{"page": n, "text": raw_text}]
        self.sections = []     # [{"name", "heading", "page_start"}]
        self.passages = []     # [{"seq", "page", "section", "subsection", "kind", "text"}] (ids added later)
        self.reference_lines = []  # [{"page", "bbox", "text"}] lines of the reference list
        self.captions = []     # [{"page", "bbox", "text"}] figure/table caption blocks
        self.raw_sections = None
        self.vocab = (set(), set())
        self.body_size = 10.0
        self.n_pages = 0
        self.pdf_metadata = {}
        self.first_pages_text = ""
        self.title_guess = None
        self.text_quality = "ok"
        self.total_chars = 0
        self.ocr_pages = []    # [{"page", "engine", "dpi", "n_lines", "mean_confidence", "low_confidence_lines"}]


# Ligatures are spelled out; zero-width characters (Springer puts them inside URLs and DNA
# sequences as line-break opportunities) are removed so that words stay searchable.
LIGATURES = str.maketrans({"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi",
                           "ﬄ": "ffl", "ﬅ": "st", "ﬆ": "st",
                           "​": None, "‌": None, "‍": None, "⁠": None, "﻿": None})

_POSITIVE_GLYPH = re.compile(r"(?<=[A-Za-z0-9)])þ(?=$|[\s,.;:%)/])|(?<!\w)þ(?!\w)")
# Font -> {code as extracted: character as printed}; each pair checked against the rendered glyph
# (contract §2.0). A character mapped to itself is verified as printed.
_ADVENT_GLYPHS = {
    "AdvPS586B": {"6": "±", "3": "×", "2": "−", ",": "<", ".": ">"},
    "AdvP80675": {"1": "+", "2": "-", "5": "=", "6": "±"},
    "AdvPS3D56D5": {"a": "α", "b": "β", "m": "μ"},
    "AdvPS7DA6": {"9": "′"},
    "AdvPS7DC6": {"p": "*"},
    "AdvPSMP10": {"l": "μ", "p": "β"},
    "AdvP4C4E74": {"0": "′", "¼": "=", "\x01": "*", "\x02": "∼", "\x03": "≈", "\x05": "−", "\x06": "×",
                   "\x07": "±"},
    "AdvP7DA6": {"a": "α", "b": "β", "m": "μ", "8": "°", ",": "<", "2": "−", "9": "′"},
    "AdvP7DB7": {",": "∼"},
    "AdvP3EAA99": {"a": "α", "m": "μ", "s": "σ"},
    "AdvP44E6F4": {"u": "°"},
    "AdvP697C": {"m": "μ", "+": "+"},
    "AdvPi1": {"8": "°", "6": "×"},
    "AdvT377": {"\x02": "©"},
    "AdvOT53816ac6": {"x": "•"},
    "AdvP4C4E51": {"<": "<", ">": ">"},
    "AdvP4C4E59": {"#": "#", "\x02": "`"},  # "vis-\x02a-vis": a spacing grave accent
    "AdvPSSymbol": {"•": "•"},
    "AdvPS_SSYB": {"~": "~"},
    # Adobe Symbol encoding, for a Symbol font without a Unicode map ("a-MEM" printed "α-MEM").
    "Symbol": dict(zip("abcdefghiklmnopqrstuwxyzABDFGLPQSWXY", "αβχδεφγηικλμνοπθρστυωξψζΑΒΔΦΓΛΠΘΣΩΞΨ"),
                   **{"Ó": "©", "Ò": "®", "£": "≤", "³": "≥", "´": "×", "¥": "∞", "»": "≈", "¹": "≠"}),
}

# Exact font bases verified in source PDFs as ordinary text/labels, not symbol maps.
_VERIFIED_ADVENT_TEXT_FONTS = {
    "AdvP2A67", "AdvP2A71", "AdvP2AA1", "AdvP2AAB", "AdvP7DED",
    "AdvPSUnv-B", "AdvPSUnv-L", "AdvPSUnv-LO", "AdvPTimesI",
    "AdvP6A16", "AdvP80516", "AdvPSMP11", "AdvPSMP13",
    "AdvP403A40", "AdvP40319B", "AdvP4041BD", "AdvP41461E", "AdvP4980F", "AdvP49811", "AdvP49812",
}


def _font_base(font):
    return str(font or "").rsplit("+", 1)[-1]


def unmapped_advent_glyphs(text, font):
    """Return characters from Advent-style symbol fonts without a verified mapping. Punctuation
    counts too: these fonts print "<" as "," (a P value printed "P,0.005")."""
    base = _font_base(font)
    if not base.startswith(("AdvP", "AdvPS")):
        return set()
    if base in _VERIFIED_ADVENT_TEXT_FONTS:
        return set()
    known = _ADVENT_GLYPHS.get(base, {})
    # Thorn is handled by the source-verified global positive-marker normalizer above.
    return {char for char in text if not char.isspace() and char not in known and char != "þ"}


def scan_unmapped_advent_glyphs(pdf_path):
    """Scan one PDF at a time and summarize unverified Advent-font characters."""
    import pymupdf
    from collections import Counter

    found = {}
    with pymupdf.open(pdf_path) as doc:
        for page in doc:
            for block in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        base = _font_base(span.get("font"))
                        chars = unmapped_advent_glyphs(span.get("text", ""), base)
                        if chars:
                            counts = found.setdefault(base, Counter())
                            counts.update(c for c in span["text"] if c in chars)
    return {font: dict(sorted(counts.items())) for font, counts in sorted(found.items())}


def is_verified_font_pair(source_char, normalized_char):
    return any(mapping.get(source_char) == normalized_char for mapping in _ADVENT_GLYPHS.values())


def normalize_font_glyphs(text, font=None):
    """Repair verified PDF text-map glyphs without changing source files.

    U+00FE is used for a positive marker (attached to a marker name or as a table cell).
    BEL (U+0007) is "±" only in the verified Advent font (mapped above); in other fonts it is an
    invisible tab leader (Springer abbreviation lists "ART<BEL>Assisted reproductive…") and
    becomes a space. Without a font (stored text) BEL is left for the caller.
    """
    text = _POSITIVE_GLYPH.sub("+", text)
    mapping = _ADVENT_GLYPHS.get(_font_base(font), {})
    if font is not None and "\x07" not in mapping:
        text = text.replace("\x07", " ")
    return "".join(mapping.get(char, char) for char in text)


SUP = "^"  # a superscript run is written as ^run: "stroke.^1", "10^5", "SSEA-3^+"
MARGIN = 0.10  # top/bottom fraction of a page where running headers and footers live
IMRAD = {"methods", "results", "results_discussion", "discussion"}
# Once the reference list starts, only these headings may follow it.
BACK_MATTER = {"acknowledgements", "funding", "author_contributions", "conflicts",
               "data_availability", "supplementary", "figure_legends", "notes"}
# Notes printed after the last reference have no heading name of their own (a bold "Publisher's Note" line
# beside its text, a centred "See www.example-journal.org for supporting information ..." line): they end the list.
_TAIL_NOTES = (("notes", re.compile(r"Publisher.s\s+note\b", re.I)),
               ("supplementary", re.compile(r"See\s+www\.\S+\s+for\s+supporting\s+information\b", re.I)))
_BOLD_FONT = re.compile(r"(?i:bold|black|blk|heavy|semibold|demi)|\.BI?$")  # Advent subsets: "AdvTT2ffda40f.B"
_SUP_RUN = re.compile(r"\^\S*")


def _raised(span, size, baseline):
    small_and_high = span["size"] < 0.8 * size and span["origin"][1] < baseline - 0.2 * size
    return bool(span["flags"] & 1) or small_and_high


def line_text(line):
    """Text of one PDF line (dict format); each superscript run is written as ^run."""
    spans = [s for s in line["spans"] if s["text"]]
    if not spans:
        return ""
    size = max(s["size"] for s in spans)
    baseline = max(spans, key=lambda s: (s["size"], len(s["text"].strip())))["origin"][1]
    out, in_sup, space_after = "", False, False
    for s in spans:
        text = normalize_font_glyphs(s["text"].translate(LIGATURES), s.get("font"))
        if not text.strip():
            out += text
        elif _raised(s, size, baseline):
            out += ("" if in_sup else SUP) + text.strip()
            in_sup, space_after = True, text[-1].isspace()
        else:
            if in_sup and space_after and not text[0].isspace():
                out += " "  # "fertility.^2 The": the space printed after a superscript is kept
            out += text
            in_sup = False
    out = out.strip()
    # A PDF soft hyphen (U+00AD) is printed only at a line break: there it is a line-break
    # hyphen ("combina-" + "tion"); anywhere else it is invisible.
    if out.endswith("\xad"):
        out = out[:-1] + "-"
    return out.replace("\xad", "")


def _block_layout(pairs, paragraph, page_height):
    """[n_chars, mean span size, y0, y1] of one paragraph block: its text length, the character-weighted mean size of
    its spans (0.01 pt) and its top/bottom as a share of the page height. CONTRACTS 2: passages `lay`."""
    spans = [s for _, ln in pairs for s in ln["spans"] if s["text"].strip()]
    weight = sum(len(s["text"].strip()) for s in spans) or 1
    size = sum(s["size"] * len(s["text"].strip()) for s in spans) / weight
    return [len(paragraph), round(size, 2), round(min(ln["bbox"][1] for _, ln in pairs) / page_height, 2),
            round(max(ln["bbox"][3] for _, ln in pairs) / page_height, 2)]


def _plain(line):
    return "".join(normalize_font_glyphs(s["text"], s.get("font")) for s in line["spans"]).strip()


def _normalize_page_text(page, raw):
    """Apply font-mapped repairs to PyMuPDF's plain page text using span locations."""
    blocks = page.get_text("dict", flags=TEXT_FLAGS)["blocks"]
    cursor = 0
    for block in blocks:
        for line in block.get("lines", []):
            source = "".join(s["text"] for s in line["spans"])
            normalized = "".join(normalize_font_glyphs(s["text"], s.get("font")) for s in line["spans"])
            if source == normalized or not source:
                continue
            pos = raw.find(source, cursor)
            if pos < 0:
                # Text blocks can be emitted in a different order by the plain-text extractor.
                pos = raw.find(source)
            if pos < 0:
                continue
            raw = raw[:pos] + normalized + raw[pos + len(source):]
            cursor = pos + len(normalized)
    return raw


def _margin_key(text):
    return re.sub(r"\d+", "#", text.lower())


def _in_margin(bbox, height):
    return bbox[3] < height * MARGIN or bbox[1] > height * (1 - MARGIN)


def _in_side_margin(bbox, width):
    return bbox[2] < width * MARGIN or bbox[0] > width * (1 - MARGIN)


# A gap wider than 0.10 em between two glyphs is a word space the PDF did not encode. Measured on
# the test PDFs: glyphs inside a word are at most 0.09 em apart (after a prime: "4′,6-"), word
# gaps without a space glyph 0.11–0.15 em (paper G).
SPACE_GAP = 0.10
_LONG_RUN = re.compile(r"\S{20,}")


def page_blocks(page):
    """The page's text blocks in PyMuPDF dict shape. Some PDFs position words without space
    glyphs ("injectionof150mg/kgSTZ"); in a horizontal line containing a 20+ character run
    without spaces, a space is restored wherever the gap between two glyphs (also across font
    changes) exceeds SPACE_GAP em."""
    blocks = page.get_text("rawdict", flags=TEXT_FLAGS)["blocks"]
    for block in blocks:
        for line in block.get("lines", []):
            spans = line["spans"]
            chars = [span.pop("chars", []) for span in spans]
            glued = abs(line["dir"][1]) < 0.1 and _LONG_RUN.search("".join(c["c"] for cs in chars for c in cs))
            prev = None
            for span, span_chars in zip(spans, chars):
                out = []
                for c in span_chars:
                    if glued and prev is not None and not c["c"].isspace() and not prev["c"].isspace() \
                            and c["bbox"][0] - prev["bbox"][2] > SPACE_GAP * span["size"]:
                        out.append(" ")
                    out.append(c["c"])
                    prev = c
                span["text"] = "".join(out)
    return blocks


def _layout_profile(doc):
    """Body font size, the header/footer lines repeated in the page margins, and the offset
    between printed page numbers and PDF page indexes (None when pages carry no numbers)."""
    sizes, margin_pages, offsets = {}, {}, {}
    for page in doc:
        seen, page_offsets = set(), set()
        for block in page_blocks(page):
            for line in block.get("lines", []):
                text = _plain(line)
                if not text:
                    continue
                for s in line["spans"]:
                    key = round(s["size"], 1)
                    sizes[key] = sizes.get(key, 0) + len(s["text"].strip())
                if _in_margin(line["bbox"], page.rect.height):
                    seen.add(_margin_key(text))
                    if re.fullmatch(r"\d{1,4}", text):
                        page_offsets.add(int(text) - page.number - 1)
        for key in seen:
            margin_pages[key] = margin_pages.get(key, 0) + 1
        for off in page_offsets:
            offsets[off] = offsets.get(off, 0) + 1
    body = max(sizes, key=sizes.get) if sizes else 10.0
    need = max(3, 0.3 * doc.page_count)
    repeated = {k for k, n in margin_pages.items() if n >= need}
    best = max(offsets, key=offsets.get) if offsets else None
    page_offset = best if best is not None and offsets[best] >= need else None
    return body, repeated, page_offset


def _is_furniture_line(line, plain, page, repeated, page_offset, text_top, text_bottom):
    """Running headers/footers, printed page numbers and text printed sideways in the side margins
    (download banners, journal sidebars) and diagonal text (watermarks such as "ARTICLE IN PRESS").
    These never become passages; page text keeps them."""
    bbox, width, height = line["bbox"], page.rect.width, page.rect.height
    if abs(line["dir"][1]) > 0.9 and _in_side_margin(bbox, width):
        return True
    if 0.1 < abs(line["dir"][1]) < 0.9 and re.search(
            r"\b(?:ARTICLE\s+IN\s+PRESS|ACCEPTED\s+MANUSCRIPT|UNCORRECTED\s+PROOF)\b", plain, re.I):
        return True
    if not _in_margin(bbox, height):
        return False
    if re.fullmatch(r"\d{1,4}", plain):
        # a bare number is a page number when it matches the document's page numbering, or when it is
        # repeated at the very top or bottom; not e.g. reference "16" printed at the top of a column
        return (page_offset is not None and int(plain) == page.number + 1 + page_offset) or (
            "#" in repeated and (bbox[1] <= text_top + 2 or bbox[3] >= text_bottom - 2))
    return _margin_key(plain) in repeated


URL_OR_DOI = re.compile(r"https?://\S+|www\.\S+|\bdoi:\s*\S+|\b10\.\d{4,9}/\S+", re.I)


def is_link_only(text):
    """A block that is only a link or DOI (e.g. PLOS "https://doi.org/…g001" under each figure)."""
    return bool(URL_OR_DOI.search(text)) and len(re.findall(r"[A-Za-z]{2,}", URL_OR_DOI.sub(" ", text))) < 2


# First-page sidebars and footnotes (PLOS, Frontiers, Elsevier…): labelled blocks that are not body text.
SIDEBAR = re.compile(
    r"^\s*(?:(?P<funding>funding(?:\s+information)?|financial\s+support)|"
    r"(?P<conflicts>competing\s+interests?|conflicts?\s+of\s+interests?|declaration\s+of\s+competing\s+interests?)|"
    r"(?P<data_availability>data\s+availability(?:\s+statement)?|availability\s+of\s+data(?:\s+and\s+materials)?)|"
    r"(?P<front>citation|academic\s+editor|editor|edited\s+by|reviewed\s+by|received|accepted|published|"
    r"copyright|abbreviations|correspondence|specialty\s+section|\*\s*e-?mail|e-?mail))"
    r"\s*[:.]|^\s*open\s+access\b"
    r"|^\s*received:?\s+(?:\d{1,2}\s+[A-Za-z]+\.?|[A-Za-z]+\.?\s+\d{1,2}),?\s+(?:19|20)\d{2}", re.I)
# Run-in labels of a structured abstract ("Conclusions: …") start a new passage.
RUN_IN_LABEL = re.compile(r"^(?:background|objectives?|aims?|purpose|goal|methods?(?:\s+and\s+results)?|results|"
                          r"findings|conclusions?|key\s*words?)\s*:", re.I)
SIDEBAR_PAGES = 2
SIDEBAR_MAX_WIDTH = 0.35  # a sidebar column is narrower than a body column of a two-column page
# First-page footnotes: "© The Author(s) 2020 Open Access…" (BMC/Springer licence), "^†These authors
# contributed equally… *Correspondence: …". Body text never starts with ©, *, †, ‡, § or ¶.
FIRST_PAGE_NOTE = re.compile(r"^\s*(?:\^?[*†‡§¶]|©|\(c\)\s)")


def _caption_in_references(probe, captions, page_no):
    """Inside the reference list: "start" for a figure/table caption block, "continue" for a lower-case block
    that finishes the unfinished caption above it on this page (a caption can run across the columns, with
    the reference list in between). Reference entries themselves are never captions."""
    if looks_like_references(probe):
        return None
    if _CAPTION.match(probe):
        return "start"
    last = captions[-1] if captions else None
    if last and last["page"] == page_no and probe[:1].islower() and not re.search(r"[.!?]$", last["text"]):
        return "continue"
    return None


# Journal footer / licence line printed on the first pages ("Journal 2026:19 616217 © 2026 Author et al. This work is
# published and licensed by …"); body text never carries a copyright mark or licence statement in its first words.
LICENCE_NOTE = re.compile(r"©\s*(?:19|20)\d{2}|\(c\)\s*(?:19|20)\d{2}|\bcreative\s+commons\b|\ball\s+rights\s+reserved\b|"
                          r"\bthis\s+(?:work|article)\s+is\s+(?:published\s+and\s+)?licen[cs]ed\b", re.I)


def sidebar_section(text, first_pages=False):
    """Section of a sidebar/footnote block ("Funding: …" -> funding), or None for body text.
    A leading link ("https://doi.org/… Received: …") is ignored. On the first pages a block that
    starts with ©, or with a footnote mark, is front matter too."""
    m = SIDEBAR.match(URL_OR_DOI.sub("", text, count=1) if URL_OR_DOI.match(text) else text)
    if not m:
        return "front_matter" if first_pages and (FIRST_PAGE_NOTE.match(text) or LICENCE_NOTE.search(text[:250])) else None
    return next((name for name in ("funding", "conflicts", "data_availability") if m.group(name)), "front_matter")


_REF_NUMBERED = re.compile(r"(?:^|[\s.])\[?\d{1,3}[.\]]\s+[A-Z][A-Za-z'’\-]+,?\s+(?:[A-Z]{1,3}\b|[A-Z]\.)")
_REF_AUTHOR = re.compile(r"\b[A-Z][A-Za-z'’\-]+,?\s+(?:[A-Z]\.?\s?){1,3}[,.(]")
_REF_LOCATOR = re.compile(r"\b\d{1,4}\s*(?:\(\d+\))?\s*:\s*\d+[–\-]\d+|\b(?:19|20)\d{2}\b")


def looks_like_references(text):
    """Text made of reference entries: numbered entries or several "Surname AB," authors, with a year
    or a volume:pages locator."""
    if not _REF_LOCATOR.search(text):
        return False
    return bool(_REF_NUMBERED.search(text)) or len(_REF_AUTHOR.findall(text)) >= 3


def reading_order(blocks, width):
    """Text blocks in reading order for pages with one or more columns.

    PDFs do not always store the columns in order (some put the right column first).
    Blocks wider than ~half the page split the page into bands. Inside a band the blocks are
    cut recursively along empty vertical strips (columns, read left to right) and, where no
    column cut exists, along the widest empty horizontal strip (e.g. a two-column text above
    a three-column reference list).
    """
    def split_columns(block):
        # PyMuPDF may put lines from both columns in one wide block, in insertion
        # order. A genuine empty gutter separates their line envelopes.
        lines = block["lines"]
        cut = _widest_gap(lines, 0, 2, 18.0) if len(lines) > 1 else None
        if cut is None:
            return [block]
        parts = []
        for group in ([ln for ln in lines if ln["bbox"][2] <= cut],
                      [ln for ln in lines if ln["bbox"][2] > cut]):
            rect = pymupdf.Rect(group[0]["bbox"])
            for line in group[1:]:
                rect |= line["bbox"]
            parts.extend(split_columns({**block, "bbox": tuple(rect), "lines": group}))
        return parts

    text_blocks = [part for b in blocks if b.get("type") == 0 and b.get("lines")
                   for part in split_columns(b)]
    wide = sorted((b for b in text_blocks if b["bbox"][2] - b["bbox"][0] > 0.55 * width),
                  key=lambda b: b["bbox"][1])
    narrow = [b for b in text_blocks if b["bbox"][2] - b["bbox"][0] <= 0.55 * width]
    ordered, top = [], float("-inf")
    for w in wide + [None]:
        limit = w["bbox"][1] if w else float("inf")
        ordered += _xy_cut([b for b in narrow if top <= b["bbox"][1] < limit])
        if w:
            ordered.append(w)
            top = limit
    return ordered


def _widest_gap(blocks, lo, hi, min_gap):
    """Middle of the widest empty strip between the blocks along one axis, or None."""
    spans = sorted((b["bbox"][lo], b["bbox"][hi]) for b in blocks)
    best, reach = None, spans[0][1]
    for start, end in spans[1:]:
        if start - reach >= min_gap and (best is None or start - reach > best[0]):
            best = (start - reach, (start + reach) / 2)
        reach = max(reach, end)
    return best[1] if best else None


def _xy_cut(blocks):
    if len(blocks) <= 1:
        return list(blocks)
    for lo, hi, min_gap in ((0, 2, 4.0), (1, 3, 1.0)):  # columns first, then horizontal strips
        cut = _widest_gap(blocks, lo, hi, min_gap)
        if cut is not None:
            first = [b for b in blocks if b["bbox"][hi] <= cut]
            second = [b for b in blocks if b["bbox"][hi] > cut]
            return _xy_cut(first) + _xy_cut(second)
    return sorted(blocks, key=lambda b: (b["bbox"][1], b["bbox"][0]))


def _heading_type(line, body_size, next_line=None):
    """Section headings are bold, at least body size, or set in another font than the text after them
    (BMC back matter: small "Acknowledgements" in its own font). A table column header ("Conclusions",
    "References" above cells in the same small font) is none of these."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    following = {s["font"] for s in (next_line or {}).get("spans", []) if s["text"].strip()}
    return bool(spans) and (all(s["flags"] & 16 or _BOLD_FONT.search(s["font"]) for s in spans)
                            or min(s["size"] for s in spans) >= body_size * 0.95
                            or not following or spans[0]["font"] not in following)


def _in_header_row(line, page_lines):
    """True when the line is one cell of a table header row: at least two other lines in the same font and
    size sit at the same height (Frontiers "Table 3": Species | Cryoprotectant | Outcome | References, all in
    9 pt sans). A section heading is alone on its row. OCR lines share one pseudo font, so they never count."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    if not spans or spans[0]["font"] == "OCR":
        return False
    font, size = spans[0]["font"], round(spans[0]["size"], 1)
    y0, y1 = line["bbox"][1], line["bbox"][3]
    same_row = 0
    for other in page_lines:
        if other is line:
            continue
        o = next((s for s in other["spans"] if s["text"].strip()), None)
        if o and o["font"] == font and round(o["size"], 1) == size \
                and min(y1, other["bbox"][3]) - max(y0, other["bbox"][1]) > 0.5 * (y1 - y0) \
                and (other["bbox"][2] < line["bbox"][0] or other["bbox"][0] > line["bbox"][2]):
            same_row += 1
    return same_row >= 2


def _is_heading_line(line, text, body_size, first=True):
    """A short, fully bold line in body-size (or larger) type: a subsection heading. The second or
    third line of a wrapped heading may start in lower case and end with a period."""
    spans = [s for s in line["spans"] if s["text"].strip()]
    return bool(spans) and abs(line["dir"][1]) < 0.1 and 3 <= len(text) <= 120 \
        and (not first or (not text.endswith(".") and (text[0].isupper() or text[0].isdigit()))) \
        and not _CAPTION.match(text) \
        and all(s["flags"] & 16 or _BOLD_FONT.search(s["font"]) for s in spans) \
        and min(s["size"] for s in spans) >= body_size * 0.85


OCR_DEFAULTS = {"enabled": True, "min_page_chars": 30, "dpi": 200, "low_confidence": 0.8}
_OCR_ENGINE = None


def ocr_settings(override=None):
    """OCR settings: `ocr:` in config/ingest.yaml, then an explicit override (e.g. {"enabled": False})."""
    from . import config
    settings = dict(OCR_DEFAULTS)
    try:
        settings.update(config.load().get("ocr") or {})
    except Exception:
        pass
    settings.update(override or {})
    return settings


def ocr_engine_name():
    try:
        from importlib.metadata import version
        return f"rapidocr-onnxruntime {version('rapidocr-onnxruntime')}"
    except Exception:
        return "rapidocr-onnxruntime"


def _ocr_lines(page, dpi):
    """OCR one rendered page. Returns [(bbox_in_pdf_points, text, confidence)] or None if OCR is unavailable."""
    global _OCR_ENGINE
    if _OCR_ENGINE is None:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except ImportError:
            return None
        _OCR_ENGINE = RapidOCR()
    pix = page.get_pixmap(dpi=dpi)
    image = pix.tobytes("png")
    del pix  # keep only one rendered page in memory
    result, _ = _OCR_ENGINE(image)
    scale = 72.0 / dpi
    lines = []
    for box, text, score in result or []:
        text = clean_spaces(str(text))
        if not text:
            continue
        xs, ys = [pt[0] * scale for pt in box], [pt[1] * scale for pt in box]
        lines.append(((min(xs), min(ys), max(xs), max(ys)), text, float(score)))
    return lines


def _ocr_line(bbox, text, score, size):
    """An OCR line in PyMuPDF's dict-line shape, so the normal block pipeline can process it."""
    return {"bbox": bbox, "dir": (1.0, 0.0), "ocr_confidence": score,
            "spans": [{"text": text, "size": size, "flags": 0, "font": "OCR", "bbox": bbox,
                       "origin": (bbox[0], bbox[3])}]}


def _ocr_blocks(lines, size):
    """Group OCR lines into pseudo text blocks (paragraphs) so `reading_order` can order columns."""
    lines = sorted(lines, key=lambda ln: (ln[0][1], ln[0][0]))
    heights = sorted(ln[0][3] - ln[0][1] for ln in lines) or [10.0]
    gap_limit = 0.9 * heights[len(heights) // 2]
    blocks = []
    for bbox, text, score in lines:
        for block in reversed(blocks[-6:]):
            bx0, by0, bx1, by1 = block["bbox"]
            overlaps = min(bx1, bbox[2]) - max(bx0, bbox[0]) > 0.3 * min(bx1 - bx0, bbox[2] - bbox[0])
            if overlaps and 0 <= bbox[1] - by1 <= gap_limit:
                block["bbox"] = (min(bx0, bbox[0]), by0, max(bx1, bbox[2]), bbox[3])
                block["lines"].append(_ocr_line(bbox, text, score, size))
                break
        else:
            blocks.append({"type": 0, "bbox": bbox, "lines": [_ocr_line(bbox, text, score, size)]})
    return blocks


def extract(pdf_path, target_chars=1000, max_chars=2000, ocr=None):
    """Extract text and structure. `ocr` overrides OCR settings (see `ocr_settings`)."""
    ocr = ocr_settings(ocr)
    ex = Extraction()
    with pymupdf.open(pdf_path) as doc:
        if doc.needs_pass:
            raise PermissionError("PDF is password-protected")
        ex.n_pages = doc.page_count
        ex.pdf_metadata = {k: v for k, v in (doc.metadata or {}).items() if v}
        ex.title_guess = _title_from_fonts(doc)
        ex.body_size, repeated, page_offset = _layout_profile(doc)

        section, subsection = "front_matter", None
        abstract_chars = 0
        references_seen = False
        sidebar = None  # (page, x0, x1, section) of the last first-page sidebar block
        heading_open = False  # the last block was only a subsection heading
        buffer, buffer_lay, buffer_page, buffer_sub = [], [], None, None

        def flush():
            nonlocal buffer, buffer_lay
            if buffer:
                text = clean_spaces(join_lines(buffer))
                at = 0
                for part in _split_long(text, max_chars):
                    at = max(text.find(part, at), 0)  # a part is a slice of the text; its blocks are the ones it overlaps
                    ex.passages.append({"page": buffer_page, "section": section, "subsection": buffer_sub,
                                        "kind": "text", "text": part,
                                        "lay": [[max(1, round(k)), *rest] for k, *rest in layout_slice(
                                            buffer_lay, len(text), at, at + len(part))]})
                    at += len(part)
            buffer, buffer_lay = [], []

        for page_index in range(doc.page_count):
            page = doc[page_index]
            page_no = page_index + 1
            raw = _normalize_page_text(page, page.get_text("text", flags=TEXT_FLAGS))
            blocks = page_blocks(page)
            page_record = {"page": page_no, "text": raw}
            first_passage = len(ex.passages)
            ocr_info = None
            if len(raw.strip()) < ocr["min_page_chars"] and page.get_images():
                attempt = {"status": "disabled", "embedded_chars": len(raw.strip())}
                page_record["ocr_attempt"] = attempt
                lines = None
                if ocr.get("enabled"):
                    try:
                        lines = _ocr_lines(page, ocr["dpi"])
                    except Exception as error:
                        # Keep the source's embedded text and let preflight report this page.
                        attempt.update(status="failed", detail=f"{type(error).__name__}: {error}")
                    else:
                        attempt["status"] = "unavailable" if lines is None else "no_text"
                if lines:
                    attempt["status"] = "used"
                    if raw.strip():
                        page_record["embedded_text"] = raw
                    blocks = _ocr_blocks(lines, ex.body_size)
                    ordered = reading_order(blocks, page.rect.width)
                    raw = "\n".join(ln["spans"][0]["text"] for b in ordered for ln in b["lines"])
                    scores = [s for _, _, s in lines]
                    ocr_info = {"engine": ocr_engine_name(), "dpi": ocr["dpi"], "n_lines": len(lines),
                                "mean_confidence": round(sum(scores) / len(scores), 3),
                                "low_confidence_lines": sum(s < ocr["low_confidence"] for s in scores)}
                    page_record["text"], page_record["ocr"] = raw, ocr_info
                    ex.ocr_pages.append({"page": page_no, **ocr_info})
            ex.pages.append(page_record)
            ex.total_chars += len(raw.strip())
            if page_index < 2:
                ex.first_pages_text += raw + "\n"

            height = page.rect.height or 1
            ys = [ln["bbox"] for b in blocks for ln in b.get("lines", []) if _plain(ln)]
            text_top = min((y[1] for y in ys), default=0)
            text_bottom = max((y[3] for y in ys), default=0)
            ordered = reading_order(blocks, page.rect.width)
            page_lines = [ln for b in ordered for ln in b["lines"]]
            for block_index, block in enumerate(ordered):
                pairs = []  # (text, line), page furniture removed
                for line in block["lines"]:
                    text = line_text(line)
                    if not text:
                        continue
                    if _is_furniture_line(line, _plain(line), page, repeated, page_offset,
                                          text_top, text_bottom):
                        continue
                    pairs.append((text, line))
                if not pairs:
                    continue
                probe = clean_spaces(join_lines([text for text, _ in pairs]))
                if section != "references" and is_link_only(probe):
                    continue  # e.g. the figure DOI link printed under every PLOS figure
                bx0, bx1 = block["bbox"][0], block["bbox"][2]
                # labelled blocks: anywhere on the first pages; later only short ones ("Received: … Accepted: …")
                side = sidebar_section(probe, page_no <= SIDEBAR_PAGES) if section != "references" and (
                    page_no <= SIDEBAR_PAGES or len(probe) < 300) else None
                # the sidebar column continues below on this page, or at the top of the next page
                # with the rest of its sentence (PLOS: "Funding: … data collection" | "and analysis…")
                if side is None and sidebar and (sidebar[0] == page_no or (
                        sidebar[0] == page_no - 1 and probe[:1].islower())) \
                        and (abs(bx0 - sidebar[1]) <= 3 or abs(bx1 - sidebar[2]) <= 3) \
                        and bx1 - bx0 <= SIDEBAR_MAX_WIDTH * page.rect.width and not match_heading(probe):
                    side = sidebar[3]  # continuation of the sidebar column
                if side:
                    # a sidebar/footnote block is its own passage and never changes the running section
                    flush()
                    sidebar = (page_no, bx0, bx1, side)
                    lay = _block_layout(pairs, probe, height)
                    for part in _split_long(probe, max_chars):
                        ex.passages.append({"page": page_no, "section": side, "subsection": None,
                                            "kind": "text", "text": part, "sidebar": True, "lay": [[len(part), *lay[1:]]]})
                    continue
                horizontal = abs(pairs[0][1]["dir"][1]) < 0.1  # rotated text (e.g. a table) has no headings
                heading = match_heading(_SUP_RUN.sub("", pairs[0][0])) if horizontal else None
                if heading and _in_header_row(pairs[0][1], page_lines):
                    heading = None  # a table column header ("References" beside "Outcome"), not a section
                if heading and section == "references" and heading[0] not in BACK_MATTER:
                    heading = None  # e.g. a reference whose first line is "Methods"
                if heading and heading[0] in ("supplementary", "figure_legends") and _CAPTION.match(probe):
                    heading = None  # "Supplementary Figure 1" labels a caption; it is not the section heading
                following = pairs[1][1] if len(pairs) > 1 else next(
                    (ln for b in ordered[block_index + 1:block_index + 2] for ln in b.get("lines", [])), None)
                if heading and not _heading_type(pairs[0][1], ex.body_size, following) and not (
                        heading[0] == "references" and looks_like_references(" ".join(t for t, _ in pairs[1:]))):
                    # a table column "Conclusions" / "References" in small plain type is not a section heading
                    # (paper D); a small "References" line followed by the list itself is
                    heading = None
                if heading:
                    name, remainder = heading
                    # Structured abstracts contain "Introduction", "Methods", "Results"...
                    # subheadings. Stay in the abstract until ~3000 characters, or until an
                    # introduction heading that comes after some abstract text. A "Conclusion(s)"
                    # heading soon after the abstract text is the abstract's own subheading: the
                    # paper's Conclusion section never directly follows the abstract.
                    limit = 6000 if name == "conclusion" else 3000
                    in_short_abstract = section == "abstract" and abstract_chars < limit
                    real_intro = name == "introduction" and abstract_chars >= 300
                    if not in_short_abstract or real_intro:
                        flush()
                        section, subsection, heading_open = name, None, False
                        ex.sections.append({"name": name, "heading": pairs[0][0][:120],
                                            "page_start": page_no})
                        pairs = ([(remainder, pairs[0][1])] if remainder else []) + pairs[1:]
                elif references_seen and section in BACK_MATTER and section not in (
                        "supplementary", "figure_legends") and looks_like_references(probe):
                    # two-column back matter: Acknowledgments beside the reference list; the list resumes
                    flush()
                    section, subsection = "references", None
                elif section == "references" and (note := next((n for n, rx in _TAIL_NOTES if rx.match(probe)), None)):
                    flush()
                    section, subsection = note, None
                    ex.sections.append({"name": note, "heading": pairs[0][0][:120], "page_start": page_no})
                references_seen = references_seen or section == "references"
                cap_block = _caption_in_references(probe, ex.captions, page_no) if section == "references" else None
                if section == "references" and not cap_block:
                    ex.reference_lines += [{"page": page_no, "bbox": [round(v, 1) for v in ln["bbox"]],
                                            "text": text} for text, ln in pairs]
                elif section != "references":
                    # Leading bold lines form a subsection heading (at most 3 lines, 150
                    # characters), unless the whole block is bold: then it is a bold paragraph.
                    # A heading-only block may continue in the next block ("…exhibiting a" | "prolonged…").
                    wrapped = heading_open and pairs[0][0][:1].islower()
                    n_head = 0
                    while n_head < min(3, len(pairs)) and _is_heading_line(
                            pairs[n_head][1], pairs[n_head][0], ex.body_size, n_head == 0 and not wrapped):
                        n_head += 1
                    head_text = clean_spaces(" ".join(text for text, _ in pairs[:n_head]))
                    run_in = RUN_IN_LABEL.match(head_text)  # "Key words: a; b" is a paragraph, not a heading
                    if n_head and len(head_text) <= 150 and not (n_head == len(pairs) and len(pairs) > 3) \
                            and not (run_in and len(head_text) > run_in.end()):
                        flush()
                        # a parent heading directly followed by a child heading in the body: keep both (paper K
                        # "HDAC Isoforms" + "Class I HDACs"). Page-1 front matter (kicker, journal, wrapped title)
                        # is not joined: it would chain into the stale subsection.
                        joined = heading_open and (page_no > 1 or section != "front_matter")
                        subsection = f"{subsection} {head_text}" if wrapped or joined else head_text
                        pairs = pairs[n_head:]
                        heading_open = not pairs
                        if heading_open:
                            continue
                heading_open = False
                lines = [text for text, _ in pairs]
                if not lines:
                    continue
                paragraph = clean_spaces(join_lines(lines))
                if section == "abstract":
                    abstract_chars += len(paragraph)
                caption = ex.captions[-1] if ex.captions else None
                bx = block["bbox"]
                if caption and caption["page"] == page_no and not re.search(r"[.!?]$", caption["text"]) \
                        and paragraph[:1].islower() \
                        and (cap_block == "continue" or (abs(bx[1] - caption["bbox"][1]) <= 5 and bx[3] <= caption["bbox"][3] + 5
                                                          and bx[0] >= caption["bbox"][2] - 5)):
                    # a caption printed across two columns: the block beside it, starting at the same height and
                    # no taller, continues it ("…represents the regulation" | "of the OCT4…"). A lower-case block
                    # below a caption is body text continuing from the previous column, not the caption.
                    last = next(p for p in reversed(ex.passages) if p["kind"] == "caption")
                    caption["text"] = clean_spaces(join_lines([caption["text"], paragraph]))
                    caption["bbox"] = [round(v, 1) for v in pymupdf.Rect(caption["bbox"]) | block["bbox"]]
                    last["text"] = clean_spaces(join_lines([last["text"], paragraph]))
                    last["lay"].append(_block_layout(pairs, paragraph, height))
                    continue
                if _CAPTION.match(paragraph):
                    flush()
                    ex.captions.append({"page": page_no, "bbox": [round(v, 1) for v in block["bbox"]],
                                        "text": paragraph})
                    # a caption printed among the references (two-column back matter) is not a reference
                    label = ("supplementary" if re.match(r"(?i)(?:supplementa(?:ry|l)\s+)?\w+\.?\s*S\d", paragraph) else
                             "figure_legends") if cap_block else section
                    lay = _block_layout(pairs, paragraph, height)
                    for part in _split_long(paragraph, max_chars):
                        ex.passages.append({"page": page_no, "section": label, "subsection": subsection,
                                            "kind": "caption", "text": part, "lay": [[len(part), *lay[1:]]]})
                    continue
                if buffer and (buffer_page != page_no or buffer_sub != subsection or RUN_IN_LABEL.match(paragraph)):
                    flush()
                buffer_page, buffer_sub = page_no, subsection
                buffer.append(paragraph)
                buffer_lay.append(_block_layout(pairs, paragraph, height))
                if sum(len(b) for b in buffer) >= target_chars:
                    flush()
            flush()  # passages never cross a page boundary
            if ocr_info:  # OCR provenance travels with every passage (and, via passage_id, each sentence)
                for passage in ex.passages[first_passage:]:
                    passage["ocr"] = {"engine": ocr_info["engine"], "mean_confidence": ocr_info["mean_confidence"]}
                    passage.pop("lay", None)  # an OCR line's size is the body size we gave it, not a measured one: no CH-08 signal

    ex.vocab = build_vocabulary(p["text"] for p in ex.pages)
    for p in ex.passages:
        p["text"] = resolve_hyphens(p["text"], ex.vocab)
    for c in ex.captions:
        c["text"] = resolve_hyphens(c["text"], ex.vocab)
    refine_sections(ex)
    for i, p in enumerate(ex.passages, start=1):
        p["seq"] = i
    per_page = ex.total_chars / max(ex.n_pages, 1)
    if per_page < 100:
        ex.text_quality = "no_text"
    elif per_page < 800:
        ex.text_quality = "low_text"
    return ex


def _words(text):
    return re.findall(r"[a-z0-9]{3,}", (text or "").lower())


def refine_sections(ex, abstract=None):
    """Label passages that have no heading of their own.

    - With the paper's abstract (from Crossref/PubMed), front-matter passages that repeat it
      are labelled "abstract", and unlabelled text between it and the first IMRaD section
      becomes "introduction" (journals that print no "Introduction" heading).
    - Reviews and essays use topical headings, not Methods/Results/Discussion: their body
      text is labelled "main_text".
    Works from the labels found at extraction time, so it can be re-run once the abstract is known.
    """
    if ex.raw_sections is None:
        ex.raw_sections = [p["section"] for p in ex.passages]
    labels = list(ex.raw_sections)
    ex.sections = [s for s in ex.sections if not s.get("inferred")]
    found = {s["name"] for s in ex.sections}
    abstract_words = set(_words(abstract))
    last_abstract = None
    if abstract_words and "abstract" not in found:
        for i, p in enumerate(ex.passages):
            words = _words(p["text"])
            if labels[i] == "front_matter" and p["kind"] == "text" and len(words) >= 12 \
                    and sum(w in abstract_words for w in words) / len(words) >= 0.7:
                labels[i], last_abstract = "abstract", i
    if not found & IMRAD:
        for i, p in enumerate(ex.passages):
            after_front = p["page"] > 1 or (last_abstract is not None and i > last_abstract)
            if labels[i] == "introduction" or (labels[i] == "front_matter" and after_front):
                labels[i] = "main_text"
        for s in ex.sections:
            if s["name"] == "introduction":
                s["name"] = "main_text"
    elif "introduction" not in found and last_abstract is not None:
        for i in range(last_abstract + 1, len(labels)):
            if labels[i] in IMRAD or labels[i] in BACK_MATTER or labels[i] == "references":
                break
            if labels[i] == "front_matter":
                labels[i] = "introduction"
    for p, label in zip(ex.passages, labels):
        if not p.get("sidebar"):  # sidebar/footnote blocks keep their own label
            p["section"] = label
    present = {s["name"] for s in ex.sections}
    for name in ("abstract", "introduction", "main_text"):
        pages = [p["page"] for p, label in zip(ex.passages, labels) if label == name]
        if pages and name not in present:
            ex.sections.append({"name": name, "heading": None, "page_start": min(pages), "inferred": True})
    ex.sections.sort(key=lambda s: s["page_start"])


def _title_from_fonts(doc):
    """Guess the title as the largest-font text in the top part of page 1."""
    if doc.page_count == 0:
        return None
    page = doc[0]
    height = page.rect.height
    spans = []
    for block in page.get_text("dict", flags=TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                text = normalize_font_glyphs(span["text"], span.get("font")).strip()
                if len(text) >= 2 and span["bbox"][1] < height * 0.7:
                    spans.append((round(span["size"], 1), span["bbox"][1], text))
    if not spans:
        return None
    for size in sorted({s[0] for s in spans}, reverse=True):
        chosen = [s for s in spans if abs(s[0] - size) <= 0.6]
        title = clean_spaces(" ".join(t for _, _, t in sorted(chosen, key=lambda s: s[1])))
        if 15 <= len(title) <= 400 and len(title.split()) >= 3:
            return title
    return None


def doi_candidates(ex):
    """DOIs found in PDF metadata and the first two pages, best first."""
    scores = {}
    sources = [(" ".join(str(v) for v in ex.pdf_metadata.values()), 3)]
    sources.append((ex.first_pages_text, 1))
    for text, weight in sources:
        for m in DOI_RE.finditer(text):
            doi = m.group(1).rstrip(".,;:)]}'\"")
            if doi.count("(") < doi.count(")"):
                doi = doi.rsplit(")", 1)[0]
            doi = doi.lower()
            before = text[max(0, m.start() - 12):m.start()].lower()
            bonus = 2 if ("doi" in before or "doi.org" in before) else 0
            scores[doi] = scores.get(doi, 0) + weight + bonus
    return [d for d, _ in sorted(scores.items(), key=lambda kv: -kv[1])]


def year_guess(ex):
    years = re.findall(r"(?:©|\(c\)|copyright|received|published|accepted)\D{0,40}((?:19|20)\d{2})",
                       ex.first_pages_text, re.I)
    if years:
        return int(max(set(years), key=years.count))
    return None


def title_found_in_text(title, text):
    """Fraction of the title's meaningful words that appear in the text."""
    words = [w for w in normalize_title(title).split() if len(w) > 3]
    if not words:
        return 0.0
    haystack = normalize_title(text)
    return sum(1 for w in words if w in haystack) / len(words)
