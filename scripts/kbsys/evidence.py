"""Tier 2 (docs/CONTRACTS.md §3): validate an agent's analysis of one paper and store it.

An agent (Hermes, Codex, Claude…) reads a paper sentence by sentence and submits "evidence
units" that point at sentence ids. It never types quotes: the quote of every unit is copied
here from the stored sentences. The checks V1-V12 run in code; units that fail are sent back
with precise messages, at most `max_attempts` times, then go to the Review Queue.
"""

import functools
import json
import re
import time
from datetime import datetime

from . import concepts, config, db, domain, hedges, mesh, notes
from .sentences import MENTION, numbers

UNIT_TYPES = {"result", "method_detail", "interpretation", "hypothesis", "conclusion", "limitation",
              "cited_statement", "background"}
CLASSIFICATIONS = {"CORE", "CONNECTED", "POTENTIAL_BRIDGE", "UNCERTAIN", "OUT_OF_SCOPE"}
EVIDENCE_TYPES = {"primary_research", "review", "systematic_review", "meta_analysis", "protocol",
                  "case_report", "commentary", "other"}
REVIEW_TYPES = {"review", "systematic_review", "meta_analysis", "commentary"}
DIRECTIONS = {"increase", "decrease", "no_change", "mixed", "not_applicable"}
VALUE_SOURCES = {"text", "table", "figure_only"}
TERM_TYPES = ["topics", "concepts", "methods", "species", "molecules", "cell_types"]
_VALIDATION_STATS = {"files_read": 0, "metadata_files": 0, "sentence_files": 0,
                     "reference_files": 0, "table_files": 0}


def _reset_validation_stats():
    _VALIDATION_STATS.clear()
    _VALIDATION_STATS.update({"files_read": 0, "metadata_files": 0, "sentence_files": 0,
                              "reference_files": 0, "table_files": 0})


def validation_stats():
    """Return the counters for the most recent validate() operation."""
    return dict(_VALIDATION_STATS)
RESULT_SECTIONS = {"results", "results_discussion", "supplementary"}
DATA_SECTIONS = RESULT_SECTIONS | {"methods", "abstract", "main_text"}  # main_text: papers without IMRaD headings
MAX_SENTENCES = 6
HEDGE_KEPT = {"interpretation", "hypothesis", "conclusion", "background", "cited_statement"}  # V5 hedge dropped
MAX_UNITS = 1000  # per submission; a real paper needs far fewer (runaway or garbage input otherwise)
MAX_GAP = 5  # cited sentences must lie within this many sentences of each other (or share a passage/table)
MIXED_DIRECTION_GAP = 5
ANATOMICAL_NOUN = re.compile(
    r"\b(?:dermis|epidermis|skin|layer|layers|limb|limbs|lobe|lobes|zone|zones|part|parts|"
    r"region|regions|segment|segments|tissue|tissues|organ|organs|cortex|medulla|"
    r"vessel|vessels|muscle|muscles|bone|bones|joint|joints)\b", re.I)
LOCATION_WORDS = {"lower", "upper", "higher", "deep", "superficial"}
RETENTION = re.compile(r"\b(?:retain\w*|retention|maintain\w*|preserv\w*|no (?:loss|decrease)|not (?:lost|decreased?))\b", re.I)

# Hedged-source detection reads the one lexicon (config/hedge_lexicon.yaml, via hedges.py).
# MODAL_HEDGE = word-level hedges (may, suggest, slightly...); HEDGE = any hedge incl. association/trend/non-significance.
HEDGE = hedges.Finder()
CLAIM_HEDGE = hedges.Finder(exclude=hedges.NON_HEDGE)  # the stored `hedging` field: a null result is not a hedge (EV-06, OD-12)
MODAL_HEDGE = hedges.Finder(hedges.MODAL_CATEGORIES)
ASSOCIATION_WORD = hedges.Finder({"association"})
# (?<!-): "laser-induced", "drug-induced" are compounds, not a causal claim
DEFINITIVE = re.compile(r"(?<!-)\b(causes?|caused|causing|induces?|induced|leads? to|led to|results? in|resulted in|"
                        r"demonstrates?|demonstrated|proves?|proved|proven|establish(?:es|ed)?|is responsible|"
                        r"drives?|determines?|due to|because of|confirms?|confirmed)\b", re.I)
# active effect verbs: from a source that only reports an association they state causation (V5)
EFFECT_VERB = re.compile(r"\b(increas|decreas|reduc|improv|enhanc|promot|inhibit|prevent|protect|impair|suppress|"
                         r"lower|rais|mediat|regulat|trigger|stimulat|block|restor)\w*", re.I)
INCREASE = re.compile(r"\b(increas\w*|elevat\w*|higher|high(?!-)|enhanc\w*|improv\w*|promot\w*|up[- ]?regulat\w*|"
                      r"augment\w*|greater|more|rais\w*|accelerat\w*|boost\w*|stimulat\w*|larger|"
                      r"bigger|longer|faster|stronger|better|superior|thicker|peak\w*)\b", re.I)
DECREASE = re.compile(r"\b(decreas\w*|reduc\w*|lower\w*|low(?!-)|declin\w*|inhibit\w*|suppress\w*|impair\w*|loss|"
                      r"lost|attenuat\w*|diminish\w*|down[- ]?regulat\w*|less|fewer|shorter|delay\w*|"
                      r"smaller|slower|weaker|worse|poorer|inferior|thinner)\b", re.I)
NO_CHANGE = re.compile(r"\b(no (?:statistically )?(?:significan\w*|difference|change|effect)s?|not (?:statistically |"
                       r"significantly )?(?:significan\w*|differ\w*|chang\w*|affect\w*|alter\w*)|"
                       r"did not (?:significantly )?\w+|similar|comparable|unchanged|unaffected|equally (?!\w*(?:ed|ing)\b)\w+|n\.?s\.?)\b", re.I)
# statement side only (EV-05): "not seeming to", "potential biomarker" keep the source's hedge (the lexicon is source side)
# "potential" only as the adjective ("a potential target"), not the noun ("membrane potential", "the potential for")
STATEMENT_HEDGE = re.compile(r"(?<![\w-])(?:seem(?:s|ed|ing)?|(?:a|an|as|is|are|be|other|another)\s+potential(?!\s+(?:of|for|difference|energy)\b))(?![\w])", re.I)
NEGATED = re.compile(r"(?:\bnot|n't|\bnever|\bcannot)\s+(?:(?!only\b)\w+\s+)?$", re.I)  # "not only caused" asserts it
LIST_ADJECTIVE = re.compile(r"\b(?:to|with|of)\s+$", re.I)  # "linked to reduced motility": a participle, not a verb
LIST_AFTER = re.compile(r"(?:\band|,)\s+$", re.I)  # "linked to A and reduced B" continues the list; "X increased A and reduced B" does not
QUANTIFIED = re.compile(r"%|\bp\s*[<=>]|significan|\bfold\b", re.I)
ATTRIBUTION = re.compile(r"\b(report\w*|previous\w*|showed|shown|found|described|demonstrated|observed|"
                         r"according|et al|authors?|studies|study)\b", re.I)
REF_MARKER = re.compile(r"\[R\d")  # "[R004]" in a unit statement names the cited source (V6)
VIETNAMESE = re.compile(r"[ăâđêôơưạảấầẩẫậắằẳẵặẹẻẽếềểễệỉịọỏốồổỗộớờởỡợụủứừửữựỳỵỷỹ]", re.I)
DOSE_UNIT = re.compile(r"\d\s*(?:%|°C|µ[gMLl]|μ[gMLl]|mM|nM|pM|mg|ng|g/|kg|mL|ml|µl|μl|IU|U/|h\b|hr|hours?|"
                       r"min|minutes?|days?|weeks?|months?|×|x\s?10)", re.I)
STOPWORDS = {"the", "and", "for", "with", "from", "into", "that", "this", "cell", "cells", "total", "number",
             "level", "levels", "rate", "ratio", "value", "values", "after", "before", "per", "via", "using"}
SPECIES_SYNONYMS = domain.profile()["species"]  # config/domain/<domain>.yaml


# ---------------------------------------------------------------- helpers

def _stem(word):
    for suffix in ("ies", "es", "s"):
        if len(word) > 4 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def _tokens(text):
    return [_stem(w) for w in re.findall(r"[a-z0-9]+", (text or "").lower())]


def _phrase_in(term, token_text):
    """Is the (stemmed) phrase present as consecutive tokens in the (stemmed) text?"""
    toks = _tokens(term)
    return bool(toks) and f" {' '.join(toks)} " in token_text


def _masked(sentence):
    """Sentence text with citation markers blanked, so '[12]' is never taken as data."""
    text = sentence["text"]
    for c in sentence.get("citations") or []:
        text = text[:c["start"]] + " " * (c["end"] - c["start"]) + text[c["end"]:]
    return text


def _short(sid):
    return sid.split(":")[-1]


def _s(value):
    """A stripped string, or "" for anything that is not a string (malformed agent JSON)."""
    return value.strip() if isinstance(value, str) else ""


def _as_list(value):
    """A list as given; a single string or number becomes a one-item list; null becomes []."""
    if value is None:
        return []
    return value if isinstance(value, list) else [value] if isinstance(value, (str, int, float)) else None


def _missing_numbers(expected, source):
    """Return missing signed numbers and positive numbers accepted as dropped signs."""
    missing = set(expected) - set(source)
    dropped = {n for n in missing if not n.startswith("-") and f"-{n}" in source}
    return missing - dropped, dropped


# ---------------------------------------------------------------- EV-19 / V15: a statement adds no entity

REF_ID = re.compile(r"\bR\d{3}\b")  # "[R004]" names the cited source
SENTENCE_ID = re.compile(r"\bS\d{4}\b")  # "(sentence continues in S0034)": an id, not a name
SKIP_WORDS = {"of", "for", "the", "and", "in", "on", "de"}  # left out of an acronym's letters (NIH = National Institutes of Health)


@functools.lru_cache(maxsize=None)
def synonym_groups():
    return tuple(tuple(g) for g in concepts.synonym_groups())


def _spelled(text):
    """Greek letters written as the statement writes them (delta, mu ...)."""
    for symbol, name in {**concepts._GREEK_NAMES, "∆": "delta", "Δ": "delta"}.items():
        text = text.replace(symbol, name)
    return text


def _entities(statement, skip=()):
    """Words of the statement that name something: acronyms, mixed-case or letter+digit names, capitalised words that
    do not open a sentence (hyphenated: each part on its own). Labels (Table 1), the R001 markers, sentence ids,
    'et al.' and the names in `skip` (section names) are not entities."""
    text = SENTENCE_ID.sub(" ", REF_ID.sub(" ", MENTION.sub(" ", statement)))
    for m in re.finditer(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9-]*", text):  # not "uM" of "10uM"
        before = text[:m.start()].rstrip()
        opens = not before or before[-1] in ".!?:;("
        for n, word in enumerate(m.group(0).split("-")):
            if len(word) > 1 and word.casefold() not in skip and (  # one letter: a group or panel label
                    any(c.isupper() for c in word[1:]) or any(c.isdigit() for c in word)
                    or (word[0].isupper() and not (opens and n == 0))):
                yield word


def _spells(acronym, words):
    """Do the initials of `words` (none an acronym or a mixed-case name like PAFs) spell the acronym?"""
    return "".join(w[0] for w in words).lower() == acronym.lower() and not any(w.isupper() or any(c.isupper() for c in w[1:]) for w in words)


def _runs(text):
    """Words of `text` (skip words left out) in runs: a run ends at punctuation other than a hyphen or apostrophe, so the
    initials of an acronym never span a comma or a sentence."""
    return [[w for w in re.findall(r"[A-Za-z0-9]+", part) if w.lower() not in SKIP_WORDS] for part in re.split(r"[^\w\s'-]+", text)]


def added_entities(statement, rows, refs, cited_ref_ids, sections, groups, abbreviations=()):
    """The entities (see _entities) of `statement` that the cited sentences `rows` do not hold, once each. A word is held
    when it is in them (also inside a longer name, plural, Greek letter spelled out), a registered synonym of
    something in them, an author of a cited reference, a name split by the PDF (HDAC+4, Theo+broma: 2-3 consecutive
    tokens), one number of a list that shares a prefix (HDAC 4, 5, 6), a Latin binomial the text abbreviates (A. thaliana),
    an abbreviation the paper itself defines (`abbreviations`: structure.json) or an acronym and its spelled-out form."""
    cited = _spelled(" ".join(r["text"] for r in rows))
    raw = cited.lower()
    known = f" {' '.join(_tokens(cited))} "
    seen = set(known.split()) | {t.rstrip("s") for t in known.split()}
    authors = {w for r in cited_ref_ids for w in _tokens((refs.get(r) or {}).get("first_author"))}
    pieces = re.findall(r"[a-z0-9]+", raw)
    joined = {"".join(w) for n in (2, 3) for i in range(len(pieces) - n + 1) for w in [pieces[i:i + n]]  # a1 (but "A, B, C" is not ABC)
              if sum(len(p) == 1 and p.isalpha() for p in w) < 2}
    content = _runs(raw)
    acronyms = set(re.findall(r"(?<![A-Za-z0-9])[A-Z]{2,6}(?![A-Za-z0-9])", cited))
    spelled_out = {w.lower() for run in _runs(statement) for a in acronyms for i in range(len(run) - len(a) + 1)  # b2: the statement
                   if all(x[0].isupper() for x in run[i:i + len(a)]) and _spells(a, run[i:i + len(a)])  # spells it out (Max Planck Institute)
                   for w in run[i:i + len(a)]}
    held = set()  # b1: words of an abbreviation the paper defines, when the cited text has its short form or its definition
    for entry in abbreviations:
        short = str(entry.get("short") or "").strip()
        definitions = [str(d.get("text") or "") for d in entry.get("definitions") or ()]
        if short and re.search(rf"(?<![A-Za-z0-9]){re.escape(short)}s?(?![A-Za-z0-9])", cited):
            held.update(t for d in definitions for t in _tokens(d))
        if short and any(_phrase_in(d, known) for d in definitions):
            held.update(_tokens(short))

    def is_held(word):
        low, tokens = word.lower(), _tokens(word)
        if low in raw or all(t in seen or t.rstrip("s") in seen or t in authors or t in held for t in tokens):
            return True
        if any(_phrase_in(x, known) for g in groups if low in {x.lower() for x in g} for x in g):
            return True
        if low in joined or low in spelled_out:  # a1, b2
            return True
        prefix = re.fullmatch(r"([a-z]+)(\d+)", low)  # a1: HDAC5 in "HDAC 4, 5, 6" / "HDAC1,2,3,8"
        if prefix and any(prefix[2] in re.findall(r"\d+", m.group(0)) for m in re.finditer(
                rf"(?<![a-z0-9]){re.escape(prefix[1])}\s?\d+(?:\s*(?:,\s*(?:and\s+|or\s+)?|/|&|and\s+|or\s+|-)\s*\d+)*", raw)):
            return True
        epithet = word[0].isupper() and re.search(rf"\b{re.escape(word)}\s+([a-z]{{3,}})\b", statement)  # a2
        if epithet and re.search(rf"(?<![A-Za-z]){word[0]}\.\s?{epithet[1]}\b", cited):  # case kept: not "group A. Cells"
            return True
        n = len(word)
        if word.isupper() and word.isalpha() and 3 <= n <= 6 and any(  # b2: MEM = (alpha-)minimum essential medium; 2 letters: by chance
                _spells(word, run[i:i + n]) for run in content for i in range(len(run) - n + 1)):
            return True
        return word.isalpha() and any(len(a) >= 3 and len(a) + 1 < len(word) <= 2 * len(a) + 1 and concepts._initialism(a, word)
                   and low.rstrip("s")[-1:] == a[-1].lower() for a in acronyms)  # b2: CTRL ~ Control(s): both end in L

    return [w for w in dict.fromkeys(_entities(statement, sections)) if not is_held(w)]


def _source_uses(word, src):
    """Does the (lower-case) source use this verb in some inflection? "reduces" ~ "reduced",
    "leads to" ~ "led to", "due to" ~ "because of"; a noun ("induction", "reduction") does not count."""
    first, *rest = word.lower().split()
    stems = {"lead", "led"} if first in ("lead", "leads", "led", "leading") else \
        {re.sub(r"(?:es|ed|ing|s|d|e)$", "", first)}
    tail = "".join(r"\s+" + re.escape(w) for w in rest)
    if any(re.search(rf"\b{re.escape(stem)}(?:e|es|ed|ing|s|d)?{tail}\b", src) for stem in stems):
        return True
    return word.lower() in ("due to", "because of") and bool(re.search(r"\b(?:due to|because of)\b", src))


def _stronger_words(statement, source):
    """Definitive or causal words in the statement that the source does not use and that are not
    themselves hedged in the statement by a modal word ("may be due to", "possibly induced").
    When the source only reports an association ("associated", "correlated", "linked"), active
    effect verbs ("reduces", "improves") it does not use also count: association is not causation.
    An association word in the statement does not excuse causal wording."""
    src = source.lower()
    patterns = [DEFINITIVE] + ([EFFECT_VERB] if ASSOCIATION_WORD.search(source) else [])
    out = []
    for pattern in patterns:
        for m in pattern.finditer(statement):
            before, word = statement[:m.start()], m.group(0).lower()
            if _source_uses(m.group(0), src) or MODAL_HEDGE.search(before[-30:]) or NEGATED.search(before):
                continue
            participle = word.endswith(("ed", "ing"))
            if pattern is EFFECT_VERB and (word.endswith(("ion", "ions")) or participle and (  # a noun / participle is no causal verb
                    LIST_ADJECTIVE.search(before)
                    or LIST_AFTER.search(before) and re.search(r"\b(?:to|with)\b", re.split(r"[.;]", before)[-1], re.I)
                    or before.endswith("-") and not re.search(r"\b(?:up|down)-$", before, re.I))):  # "Polycomb-mediated"; "up-regulated" may be a verb
                continue
            out.append(m.group(0))
    return out


def _direction_matches(pattern, text):
    """Find direction words, excluding location words used with anatomical nouns."""
    matches = []
    for match in pattern.finditer(text):
        word = match.group(0).lower()
        if (word in ("loss", "lost") and RETENTION.search(text)  # "its loss" in a text about what was retained
                and not QUANTIFIED.search(text[max(0, match.start() - 40):match.end() + 40])):  # but "a loss of 40%" counts
            continue
        if word in LOCATION_WORDS:
            following = text[match.end():]
            if re.match(r"\s+(?:(?:in|of|the|a|an)\s+)?" + ANATOMICAL_NOUN.pattern, following, re.I):
                continue
        matches.append(match)
    return matches


CONTRAST = re.compile(r"\b(?:but|while|whereas)\b", re.I)  # joins two findings in one statement (EV-18)


def directions(text):
    """The directions a text states: a subset of {"increase", "decrease", "no_change"}. One detector for the
    filler (evidence_fill), the EV-17 / EV-18 checks and the bundled-finding flag."""
    found = {name for name, pattern in (("increase", INCREASE), ("decrease", DECREASE)) if _direction_matches(pattern, text)}
    return found | ({"no_change"} if NO_CHANGE.search(text) else set())


def _direction_near_measure(matches, text, measure):
    """Whether a direction word is within a few words of the stated outcome measure."""
    measure_words = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", measure or "") if len(w) >= 2}
    if not measure_words:
        return False
    words = list(re.finditer(r"[A-Za-z0-9]+", text.lower()))
    for match in matches:
        direction_word = next((i for i, w in enumerate(words) if w.start() <= match.start() < w.end()), None)
        if direction_word is None:
            continue
        nearby = range(max(0, direction_word - MIXED_DIRECTION_GAP),
                       min(len(words), direction_word + MIXED_DIRECTION_GAP + 1))
        if any(words[i].group(0) in measure_words for i in nearby):
            return True
    return False


def _is_title_sentence(ctx, sentence):
    title = re.sub(r"\s+", " ", str(ctx.meta.get("title") or "")).strip().lower()
    text = re.sub(r"\s+", " ", str(sentence.get("text") or "")).strip().lower()
    return sentence.get("section") == "title" or bool(title and text == title)


class PaperContext:
    """Everything the validator needs about one paper."""

    def __init__(self, con, paper_id):
        self.paper_id = paper_id
        folder = db.paper_dir(paper_id)
        _VALIDATION_STATS["files_read"] += 1
        _VALIDATION_STATS["metadata_files"] += 1
        self.meta = db.read_json(folder / "metadata.json")
        if not self.meta:
            raise FileNotFoundError(f"No paper {paper_id}")
        if (self.meta.get("extraction_version") or 0) < 2:
            raise ValueError(f"{paper_id} has no sentence ids yet: run `kb.py reextract {paper_id}` first")
        _VALIDATION_STATS["files_read"] += 1
        _VALIDATION_STATS["sentence_files"] += 1
        self.sentences = {s["sentence_id"]: s for s in db.read_jsonl(folder / "sentences.jsonl")}
        _VALIDATION_STATS["files_read"] += 1
        _VALIDATION_STATS["reference_files"] += 1
        self.refs = {r["ref_id"]: r for r in db.read_jsonl(folder / "references.jsonl")}
        self.abbreviations = (db.read_json(folder / "structure.json", {}) or {}).get("abbreviations") or []  # V15: the paper's own
        _VALIDATION_STATS["files_read"] += 1
        _VALIDATION_STATS["table_files"] += 1
        self.table_caption_ids = {sid for table in db.read_jsonl(folder / "tables.jsonl")
                                  for sid in table.get("caption_sentence_ids") or []}
        self._index(" ".join([self.meta.get("title") or "", self.meta.get("abstract") or ""]))
        self.registry = concepts.build_registry(con)
        self.canonical = _canonical_terms(con, self.registry)
        from . import figure_reading  # late: figure_reading imports answer, which imports this module
        self.figure_warnings = figure_reading.unit_warnings(figure_reading.readings(paper_id), self.sentences.values())

    def _index(self, extra_text=""):
        """Word/number lookups and sentence positions derived from self.sentences."""
        paper_text = " ".join([extra_text] + [s["text"] for s in self.sentences.values()])
        self.token_text = f" {' '.join(_tokens(paper_text))} "
        self.prefixes = {w[:6] for w in re.findall(r"[a-z0-9]+", paper_text.lower()) if len(w) >= 3}
        self.paper_numbers = numbers(" ".join(_masked(s) for s in self.sentences.values()
                                               if s["section"] not in ("references", "front_matter")))
        # a sentence cut by a page break or a figure is one sentence: head -> [parts], part -> head
        self.head, self.members, self.text_pos = {}, {}, {}
        pos = 0
        for s in sorted(self.sentences.values(), key=lambda x: x["seq"]):
            sid = s["sentence_id"]
            if sid not in self.head:
                self.head[sid], self.members[sid] = sid, [sid]
                if s["kind"] == "text":
                    pos += 1  # body positions: captions/table rows in between and continuation parts do not count
            self.text_pos[sid] = pos
            nxt = s.get("continues")
            if nxt in self.sentences and nxt not in self.head:
                self.head[nxt] = self.head[sid]
                self.members[self.head[sid]].append(nxt)

    def sid(self, value):
        value = str(value or "").strip()
        return value if value.startswith(self.paper_id + ":") else f"{self.paper_id}:{value}"

    def grounded(self, term, strict=False):
        """Is the term in this paper? Exact (stemmed) phrase, a species or synonym-group variant,
        or else word by word: a word counts when a paper word shares its first 6 letters
        ("differentiation" ~ "differentiate"). strict (agents): every content word must match;
        otherwise (measures, methods, terms) at least half of them."""
        if not term:
            return True
        if _phrase_in(term, self.token_text):
            return True
        for word in SPECIES_SYNONYMS.get(term.strip().lower(), []):
            if _phrase_in(word, self.token_text):
                return True
        for variant in self.canonical.get("_groups", {}).get(term.strip().lower(), []):
            if _phrase_in(variant, self.token_text):
                return True
        # MeSH entry terms of the same descriptor: "Flow cytometry" ~ "fluorescence-activated cell sorting"
        if any(_phrase_in(variant, self.token_text) for variant in _mesh_variants(term)):
            return True
        if any(_phrase_in(variant, self.token_text) for variant in _chi_variants(term)) or self._acronym_held(term):
            return True
        words =[w for w in re.findall(r"[a-z0-9]+", term.lower()) if len(w) >= 3 and w not in STOPWORDS]
        if not words:
            return True  # only generic words ("cells", "level"): nothing specific to check
        roots = self._roots()
        found = sum(1 for w in words if w[:6] in self.prefixes or (len(w) >= 6 and _root(w) in roots))
        return found == len(words) if strict else found / len(words) >= 0.5

    def _roots(self):
        """Roots of the paper's -ed/-ing words (cached per indexed text): filtration ~ filtered; a noun ("filters") is not the process."""
        if getattr(self, "_roots_of", (None,))[0] is not self.token_text:
            self._roots_of = (self.token_text, {_root(w) for w in self.token_text.split() if len(w) >= 6 and w.endswith(("ed", "ing"))})
        return self._roots_of[1]

    def _acronym_held(self, term):
        """An acronym whose expansion the paper holds ("MRI" ~ "magnetic resonance imaging"), or the expansion
        of an acronym the paper holds. Initials of consecutive words; ponytail: no 'of'/'and' skipping."""
        words = re.findall(r"[a-z0-9]+", term.lower())
        toks = self.token_text.split()
        if len(words) == 1 and 3 <= len(words[0]) <= 6 and words[0].isalpha():  # 2 letters: ~80% of random pairs spell it
            n = len(words[0])
            return any("".join(w[0] for w in g) == words[0] and all(len(w) >= 3 and w not in STOPWORDS for w in g)
                       for g in (toks[i:i + n] for i in range(len(toks) - n + 1)))
        initials = "".join(w[0] for w in words if w not in STOPWORDS)
        return 3 <= len(words) <= 6 and all(w.isalpha() for w in words) and len(initials) == len(words) and initials in toks


_CHI = re.compile(r"\bchi[\s^-]*(?:square[d]?|2)\b", re.I)


def _chi_variants(term):
    """"Chi-square test" ~ "chi^2 test" ~ "chi2 test" ~ "χ^2 test" (the paper index drops the Greek letter: "2 test")."""
    return [_CHI.sub(v, term) for v in ("chi 2", "chi2", "chi square", "2")] if _CHI.search(term) else []


def _root(word):
    """A word with one derivational/inflectional ending and its e's removed: filtration, filtered -> filtr."""
    return re.sub(r"(?:ations?|ions?|ings?|ed|es|s|e)$", "", word).replace("e", "")


_MESH_CACHE = {}


def _mesh_variants(term):
    """Usable entry terms of every MeSH descriptor that has `term` as a term (empty without the index)."""
    key = term.strip().lower()
    if key not in _MESH_CACHE:
        variants = set()
        try:
            if mesh.available():
                con = mesh._db()
                for (dui,) in con.execute("SELECT DISTINCT descriptor_ui FROM terms WHERE norm = ?", (mesh.norm(term),)):
                    variants.update(t for (t,) in con.execute(
                        "SELECT DISTINCT term FROM terms WHERE descriptor_ui = ?", (dui,)) if mesh._usable(t))
        except Exception:
            variants = set()
        _MESH_CACHE[key] = sorted(variants)
    return _MESH_CACHE[key]


def _canonical_terms(con, registry=None):
    """The same canonical registry used by concept links and Obsidian notes."""
    registry = registry or concepts.build_registry(con)
    canon = {}
    groups = {}
    for record in registry.records:
        values = [record["name"], *record.get("synonyms", [])]
        for value in values:
            canon[value.lower()] = record["name"]
            groups[value.lower()] = values
    canon["_groups"] = groups
    return canon


INTERPRETIVE = re.compile(r"\b(our|present|current|these) (study|studies|results?|findings?|data|observations?)\b|"
                          r"\bwe (propose|speculate|hypothesi[sz]e|suggest|believe|conclude)|\blimitations?\b|"
                          r"\bfurther (studies|research|investigation)", re.I)


# Sections outside the body text: never must-cover.
NON_BODY = {"references", "front_matter", "acknowledgements", "funding", "author_contributions", "conflicts",
            "data_availability", "figure_legends", "notes"}
LEGEND_TEXT = re.compile(r"^\s*scale\s+bars?\b", re.I)  # figure legend text that was not detected as a caption
# publisher/front-matter text that leaked into the body (abbreviation lists, "supplementary material is
# available at", download/licence banners, correspondence and affiliation blocks): never demanded
BOILERPLATE = re.compile(r"^\W*abbreviations?\b|\bonline version\b[^.]{0,40}\bcontains\b|"
                         r"\bsupplementa\w+ (?:material|information|data)\b[^.]{0,60}\b(?:available|found)\b|"
                         r"\bsupporting information\b[^.]{0,60}\bavailable\b|"
                         r"\bdownloaded from\b|\bpublished online\b|©|\bcorrespondence:|\bcontributed equally\b|"
                         r"\be-?mail:|^\W*TYPE\s+(?:original research|review|article)\b", re.I)
_NUMERIC_TOKEN = re.compile(r"^[\(\[<>≤≥=±~−–\-+]*\d[\d.,]*(?:[%)\]*,;:]|±\S*)*$")


def table_like(text):
    """A table printed as one line of running text (its table object was not detected): at least
    15 numeric tokens, making up at least 35 % of the tokens."""
    tokens = text.split()
    numeric = sum(1 for tok in tokens if _NUMERIC_TOKEN.match(tok))
    return numeric >= 15 and numeric >= 0.35 * len(tokens)


def required_sentences(ctx):
    """Sentences every analysis must cover (V8): numeric results, dosed/timed methods, every
    sentence that cites other work, every Conclusion sentence, and Discussion sentences that
    interpret, hypothesise or state limitations. Only body text counts: never captions, table
    rows, tables read as text (`in_table` or table-like), legend text, the reference list or
    front/back matter. A sentence cut by a page break or a figure (`continues`) is judged as one
    sentence; the requirement is placed on its first part (`ctx.head`), and citing any part covers it."""
    out = []
    full = (config.load().get('analysis') or {}).get('coverage_policy') == 'full_sections'
    full_sections = {'introduction', 'methods', 'results', 'results_discussion', 'discussion', 'conclusion', 'main_text', 'supplementary'}
    for head, members in ctx.members.items():
        parts = [ctx.sentences[m] for m in members]
        first = parts[0]
        if first["kind"] != "text" or any(p.get("in_table") for p in parts) or first["section"] in NON_BODY:
            continue
        masked = " ".join(_masked(p) for p in parts)
        if LEGEND_TEXT.match(masked) or BOILERPLATE.search(masked) or table_like(masked):
            continue
        if (full and first['section'] in full_sections) or _qualifies(first["section"], masked, any(p["citations"] for p in parts)):
            out.append(head)
    return sorted(out, key=lambda i: ctx.sentences[i]["seq"])


def _qualifies(section, masked, cites):
    if cites:
        return True
    if section in RESULT_SECTIONS and numbers(MENTION.sub(" ", masked)):
        return True
    if section == "methods" and DOSE_UNIT.search(masked):
        return True
    if section == "conclusion" and len(masked) > 40:
        return True
    return bool(section in ("discussion", "results_discussion", "main_text")
                and (HEDGE.search(masked) or INTERPRETIVE.search(masked)))


# ---------------------------------------------------------------- validation

def validate(con, paper_id, submission, legacy_terms_warning=False):
    """Check a submission. Returns (result, accepted_units, rejected_units).

    result = {errors: [...], flags: [...], warnings: [...], coverage_missing: [...], normalized_terms: [...]}.
    Each accepted unit is a stored record (quote copied from the sentences)."""
    started = time.perf_counter()
    _reset_validation_stats()
    ctx = PaperContext(con, paper_id)
    errors, flags, warnings, normalized = [], [], [], []

    def err(index, code, message):
        errors.append({"unit_index": index, "code": code, "severity": "reject", "message": message})

    def flag(index, code, message):
        flags.append({"unit_index": index, "code": code, "severity": "flag", "message": message})

    def warn(index, code, message):
        warnings.append({"unit_index": index, "code": code, "severity": "warning", "message": message})

    # V10 and submission-level structure (wrong types are reported, never raised)
    if not isinstance(submission, dict):
        err(0, "V10", "the submission must be one JSON object {paper_id, analyst, classification, …, units}")
        submission = {}
    if submission.get("paper_id") not in (None, paper_id):
        err(0, "V10", f"paper_id {submission.get('paper_id')!r} does not match {paper_id}")
    if _s(submission.get("classification")) not in CLASSIFICATIONS:
        err(0, "V10", f"classification must be one of {sorted(CLASSIFICATIONS)}")
    if not _s(submission.get("classification_reason")):
        err(0, "V10", "classification_reason is required (one sentence, relative to the vault's scope)")
    if _s(submission.get("evidence_type")) not in EVIDENCE_TYPES:
        err(0, "V10", f"evidence_type must be one of {sorted(EVIDENCE_TYPES)}")
    if not _s(submission.get("analyst")):
        err(0, "V10", "analyst is required (e.g. 'hermes/deepseek-chat')")
    units = submission.get("units")
    if not isinstance(units, list) or not units:
        err(0, "V10", "units must be a non-empty list")
        units = []
    elif len(units) > MAX_UNITS:
        err(0, "V10", f"{len(units)} units: at most {MAX_UNITS} per paper; merge repeated statements or skip sentences")
        units = []
    review_paper = _s(submission.get("evidence_type")) in REVIEW_TYPES

    accepted, accepted_by_index, rejected, covered = [], {}, [], set()
    for index, unit in enumerate(units, start=1):
        before = len(errors)
        record = _check_unit(ctx, index, unit, review_paper, err, flag, normalized,
                             warn=warn,
                             legacy_terms_warning=legacy_terms_warning)
        unit_errors = [e for e in errors[before:]]
        # EV-20: the AI reading of a cited figure/table disagrees with its caption or text. Also for a unit that
        # recheck retains although it is rejected now (its flags are rebuilt from these).
        warned = getattr(ctx, "figure_warnings", {})  # conform builds a context without __init__
        cited = (record or {}).get("sentence_ids") or [ctx.sid(s) for s in (_as_list(unit.get("sentence_ids")) if isinstance(unit, dict) else None) or ()]
        fg = sorted({warned[s] for s in cited if s in warned})
        if fg:
            flag(index, "FG", " | ".join(fg))
        if record is None or unit_errors:
            rejected.append({"unit_index": index, "unit": unit, "errors": unit_errors})
        else:
            record["flags"] = [f for f in flags if f["unit_index"] == index]
            record["status"] = "flagged" if record["flags"] else "ok"
            record["_validation_unit_index"] = index
            accepted.append(record)
            accepted_by_index[index] = record
            covered.update(record["sentence_ids"])

    # skipped sentences
    skipped = {}
    skipped_items = _as_list(submission.get("skipped"))
    if skipped_items is None:
        err(0, "V8", "skipped must be a list of {sentence_id, reason}")
        skipped_items = []
    for item in skipped_items:
        sid = ctx.sid(item.get("sentence_id") if isinstance(item, dict) else item)
        reason = _s(item.get("reason")) if isinstance(item, dict) else ""
        if sid not in ctx.sentences:
            err(0, "V8", f"skipped sentence {_short(sid)} does not exist")
        elif not reason:
            err(0, "V8", f"skipped sentence {_short(sid)} needs a reason")
        else:
            skipped[sid] = reason

    # V8 coverage (only counted over accepted units, so rejected units must be fixed)
    covered_heads = {ctx.head.get(s, s) for s in covered} | {ctx.head.get(s, s) for s in skipped}
    missing = [_short(s) for s in required_sentences(ctx) if s not in covered_heads]

    # research question and summary: every sentence grounded like a unit
    rq = _check_research_question(ctx, submission.get("research_question"), err, flag)
    summary = _check_summary(ctx, submission.get("summary"), accepted_by_index, err, flag)

    result = {"errors": errors, "flags": flags, "warnings": warnings, "coverage_missing": missing, "normalized_terms": normalized,
              "skipped": [{"sentence_id": k, "reason": v} for k, v in skipped.items()],
              "research_question": rq, "summary": summary,
              "timing_seconds": round(time.perf_counter() - started, 6),
              "file_reads": validation_stats()}
    return result, accepted, rejected


def _check_unit(ctx, i, unit, review_paper, err, flag, normalized, warn=None, legacy_terms_warning=False):
    if not isinstance(unit, dict):
        err(i, "V1", "unit must be an object")
        return None
    utype = _s(unit.get("unit_type"))
    if utype not in UNIT_TYPES:
        err(i, "V1", f"unit_type must be one of {sorted(UNIT_TYPES)}")
        return None
    statement = _s(unit.get("statement"))
    if len(statement) < 10:
        err(i, "V1", "statement is missing or too short")
        return None
    if VIETNAMESE.search(statement):
        err(i, "V1", "statement must be written in English (canonical language); explanations in Vietnamese belong elsewhere")

    # V1: sentence ids exist, belong to the paper, and form one place in the text
    raw_ids = _as_list(unit.get("sentence_ids"))
    if not raw_ids or not all(isinstance(s, (str, int)) for s in raw_ids):
        err(i, "V1", "sentence_ids must list 1-6 sentence ids (e.g. ['S0142'])")
        return None
    sids = list(dict.fromkeys(ctx.sid(s) for s in raw_ids))  # a repeated id counts once
    unknown = [_short(s) for s in sids if s not in ctx.sentences]
    if unknown:
        err(i, "V1", f"sentence ids not found in this paper: {unknown}")
        return None
    if len(sids) > MAX_SENTENCES:
        err(i, "V1", f"at most {MAX_SENTENCES} sentences per unit; split it into several units")
    sents = [ctx.sentences[s] for s in sids]
    positions = [ctx.text_pos[s["sentence_id"]] for s in sents]
    same_passage = len({s["passage_id"] for s in sents}) == 1 and sents[0]["passage_id"]
    tables = {s.get("object_id") for s in sents if s["kind"] == "table_row"}
    all_same_table = bool(sents) and len(tables) == 1 and all(
        s["kind"] == "table_row" and s.get("object_id") in tables for s in sents)
    if not same_passage and max(positions) - min(positions) > MAX_GAP and not all_same_table:
        err(i, "V1", f"sentences {[_short(s) for s in sids]} are far apart; cite one place per unit "
                     "(split into several units)")
    if tables and not all_same_table:
        unrelated_text = [s for s in sents if s["kind"] != "table_row" and not
                          (tables.intersection(s.get("mentions") or [])
                           or (s.get("kind") == "caption" and s["sentence_id"] in ctx.table_caption_ids))]
        if unrelated_text:
            err(i, "V1", "a text sentence cited with table rows must mention that table or be its caption; "
                         "split into several units")
    if any(s["section"] == "references" for s in sents):
        err(i, "V1", "a unit cannot cite the reference list; cite the sentence that uses the reference")

    model = unit.get("model") or {}
    intervention = unit.get("intervention") or {}
    outcome = unit.get("outcome") or {}
    quality = unit.get("quality") or {}
    for name, part in (("model", model), ("intervention", intervention), ("outcome", outcome), ("quality", quality)):
        if not isinstance(part, dict):
            err(i, "V1", f"{name} must be an object (or omitted)")
            return None

    # EV-17: an empty direction / species of a result is filled by code from the cited source; an AI value is never replaced.
    # A later AI fill of a stored unit (G5) records {"by": "ai", "source": ...} so every filled field keeps its provenance.
    from . import evidence_fill  # late: evidence_fill imports this module
    prior = unit.get("filled_by") if isinstance(unit.get("filled_by"), dict) else {}
    sources = {_short(s) for s in sids} | {"paper:title", "paper:abstract"}  # a claimed source must be one the unit cites
    filled_by = {k: v for k, v in prior.items() if k in ("direction", "species") and isinstance(v, dict)  # a recheck keeps them
                 and v.get("by") in ("rule", "ai") and v.get("source") in sources and (outcome if k == "direction" else model).get(k)}
    for field, hit in evidence_fill.fill_fields(unit, sents, ctx.meta).items():
        if field == "direction":
            outcome = {**outcome, field: hit["value"]}
        else:
            model = {**model, field: hit["value"]}
        filled_by[field] = {"by": "rule", "source": hit["source"]}
        normalized.append({"unit_index": i, "field": ("outcome." if field == "direction" else "model.") + field,
                           "from": None, "to": hit["value"], "because": "rule: " + hit["source"]})

    source_text = " ".join(_masked(s) for s in sents)
    source_numbers = numbers(source_text)
    cited_in_sentences = {f"{ctx.paper_id}:R{n:03d}" for s in sents for c in s["citations"] for n in c["ref_nums"]}
    text_sents = [s for s in sents if s["kind"] == "text"]

    # V2: numbers in the statement and the outcome come from the cited sentences;
    # numbers in context fields (dose, timing, duration, model) may come from elsewhere in the paper.
    labels_removed = MENTION.sub(" ", statement)  # "Table 1", "Fig. 3B" are labels, not data
    values = _as_list(outcome.get("values"))
    if values is None:
        err(i, "V2", "outcome.values must be a list of strings, e.g. ['41.0 ± 1.8%']")
        values = []
    strict = numbers(labels_removed) | numbers(" ".join(str(v) for v in values)) \
        | numbers(str(outcome.get("n") or "")) | numbers(str(outcome.get("p") or ""))
    # a year written with a cited reference ("Marlow et al. 2010") names the source, it is not data
    cited_years = {str(ctx.refs[r].get("year")) for r in cited_in_sentences if (ctx.refs.get(r) or {}).get("year")}
    missing, dropped = _missing_numbers(strict - (numbers(statement) & cited_years), source_numbers)
    if dropped:
        flag(i, "V2", f"sign dropped for number(s) {sorted(dropped, key=float)}; source contains the signed value")
    if missing:
        err(i, "V2", f"number(s) {missing} not found in the cited sentences {[_short(s) for s in sids]}; "
                     "cite the sentence or table row that contains them, or remove them")
    context_numbers = numbers(" ".join(str(intervention.get(k) or "") for k in ("dose", "timing", "duration"))
                              + " " + " ".join(str(v or "") for v in model.values()))
    missing, dropped = _missing_numbers(context_numbers, source_numbers | ctx.paper_numbers)
    if dropped:
        flag(i, "V2", f"sign dropped for number(s) {sorted(dropped, key=float)}; source contains the signed value")
    if missing:
        err(i, "V2", f"number(s) {missing} in model/intervention do not appear anywhere in this paper")

    # V3: named things must appear in this paper
    for label, value in (("species", model.get("species")), ("cell_type", model.get("cell_type")),
                         ("agent", intervention.get("agent")), ("measure", outcome.get("measure")),
                         ("method", outcome.get("method"))):
        if value and not ctx.grounded(str(value), strict=label == "agent"):
            flag(i, "V3", f"{label} {value!r} is not found in this paper (use the paper's own wording)")

    # V4: results come from this paper's Results
    if utype == "result":
        if review_paper:
            flag(i, "V4", "a review paper has no results of its own: use cited_statement/background")
        elif text_sents and not any(s["section"] in RESULT_SECTIONS for s in text_sents) \
                and not any(s["kind"] == "table_row" for s in sents):
            flag(i, "V4", f"result cited from {sorted({s['section'] for s in text_sents})}, not from Results")

    # V5: do not state more strongly than the source (all hedge categories: "not significant" is never upgraded)
    hedged = bool(HEDGE.search(source_text))
    stronger = _stronger_words(statement, source_text)
    modal = MODAL_HEDGE.search(source_text)
    if hedged and stronger:
        flag(i, "V5", f"source is hedged ({HEDGE.search(source_text).group(0)!r}) but the statement says "
                      f"{stronger}: keep the authors' wording")
    elif modal and utype in HEDGE_KEPT and not (MODAL_HEDGE.search(statement) or STATEMENT_HEDGE.search(statement)):
        flag(i, "V5", f"source is hedged ({modal.group(0)!r}) but the statement is not: a hypothesis or "
                      "suggestion is not a finding; keep 'may'/'suggest' in the statement")
    # the `hedging` field is judged on the sentence(s) that carry the claim (`hedged_sentence_ids`, default all cited)
    # and without non-significance (EV-06)
    claim_ids = None
    if unit.get("hedged_sentence_ids") is not None:
        claim_ids = _as_list(unit["hedged_sentence_ids"])
        claim_ids = [ctx.sid(s) for s in claim_ids] if claim_ids and all(isinstance(s, (str, int)) for s in claim_ids) else None
        if not claim_ids or not set(claim_ids) <= set(sids):
            err(i, "V1", "hedged_sentence_ids must be a non-empty list of sentence ids that are among sentence_ids")
            claim_ids = None
        else:
            claim_ids = [s for s in sids if s in claim_ids]  # one order (that of sentence_ids), each id once
    claim_hedge = CLAIM_HEDGE.search(" ".join(_masked(ctx.sentences[s]) for s in claim_ids) if claim_ids else source_text)
    hedging = unit.get("hedging") or ("hedged" if claim_hedge else "none")  # null = not stated
    if hedging not in ("none", "hedged"):
        err(i, "V5", "hedging must be 'none' or 'hedged'")
    elif claim_hedge and hedging == "none":  # corrected automatically; the wording check above stays a warning
        hedging = "hedged"
        normalized.append({"unit_index": i, "field": "hedging", "from": "none", "to": "hedged",
                           "because": claim_hedge.group(0)})

    # V6: citations ("R004", "4", "R4" or the full id are accepted)
    cited_refs = []
    raw_refs = _as_list(unit.get("cited_refs"))
    if raw_refs is None:
        err(i, "V6", "cited_refs must be a list such as ['R004']")
        raw_refs = []
    for r in raw_refs:
        m = re.fullmatch(r"R?0*(\d+)", str(r).split(":")[-1].strip())
        cited_refs.append(f"{ctx.paper_id}:R{int(m.group(1)):03d}" if m else str(r))
    if cited_in_sentences and utype == "result":
        err(i, "V6", "a sentence that cites other papers is not a result of this paper: use cited_statement, "
                     "background, interpretation or split the unit")
    if utype == "cited_statement":
        if not cited_in_sentences:
            err(i, "V6", "these sentences have no resolved citation, so this cannot be a cited_statement: "
                         "use background or interpretation (write 'Author et al. reported …' in the statement)")
        elif not cited_refs:
            err(i, "V6", f"cited_statement needs cited_refs, e.g. {sorted(_short(r) for r in cited_in_sentences)[:3]}")
        elif not set(cited_refs) <= cited_in_sentences:
            err(i, "V6", f"cited_refs {[_short(r) for r in cited_refs]} are not cited in these sentences "
                         f"(cited there: {sorted(_short(r) for r in cited_in_sentences)})")
        if not (ATTRIBUTION.search(statement) or REF_MARKER.search(statement)):
            flag(i, "V6", "write a cited_statement as attribution, e.g. 'Marlow et al. [R004] reported that …'")
    elif cited_refs and not set(cited_refs) <= cited_in_sentences:
        err(i, "V6", f"cited_refs {[_short(r) for r in cited_refs]} are not cited in these sentences")

    # V15 (EV-19): the statement names nothing that the cited sentences do not contain (a referent of a neighbouring
    # sentence, "these cells" -> "Muse cells", must be cited too: D-PENDING-10)
    added = added_entities(statement, sents, ctx.refs, set(cited_refs) | cited_in_sentences,
                           {str(s.get("section")).replace("_", " ") for s in ctx.sentences.values()}, synonym_groups(),
                           getattr(ctx, "abbreviations", ()))
    if added:
        shown = ", ".join(repr(w) for w in added[:5]) + (f" (+{len(added) - 5} more)" if len(added) > 5 else "")
        flag(i, "V15", f"statement names {shown} which the cited sentences do not contain: cite the sentence that names it "
                       "(up to 6 ids within 5 sentences are allowed) or remove it")

    # V7: direction
    direction = outcome.get("direction")
    if direction is not None and (not isinstance(direction, str) or direction not in DIRECTIONS):
        err(i, "V7", f"direction must be one of {sorted(DIRECTIONS)}")
    elif direction in ("increase", "decrease", "no_change"):
        # judge the direction on the data sentences, not on a caption or table title
        data_text = " ".join(_masked(s) for s in sents if s["kind"] != "caption") or source_text
        up, down, same = _direction_matches(INCREASE, data_text), _direction_matches(DECREASE, data_text), \
            NO_CHANGE.search(data_text)
        measure = str(outcome.get("measure") or "")
        relevant = up if direction == "increase" else down if direction == "decrease" else []
        if up and down and not _direction_near_measure(relevant, data_text, measure):
            flag(i, "V7", f"mixed direction in source; check that {direction} refers to {measure or 'the stated measure'}")
        if direction == "increase" and down and not up and not same:
            flag(i, "V7", f"direction 'increase' but the source says {down[0].group(0)!r}")
        elif direction == "decrease" and up and not down and not same:
            flag(i, "V7", f"direction 'decrease' but the source says {up[0].group(0)!r}")
        elif direction == "no_change" and not same and (up or down):
            flag(i, "V7", f"direction 'no_change' but the source says {(up or down)[0].group(0)!r}")
        elif direction in ("increase", "decrease") and same and not up and not down:
            flag(i, "V7", f"direction '{direction}' but the source says {same.group(0)!r}")
    if utype == "result" and direction in ("increase", "decrease", "no_change"):  # EV-18: one finding per unit
        parts = CONTRAST.split(statement)
        for left, right in zip(parts, parts[1:]):
            a, b = directions(left), directions(right)
            if a and b and a.isdisjoint(b):
                flag(i, "V7", f"bundled finding: the statement turns from {'/'.join(sorted(a))} to {'/'.join(sorted(b))}; "
                              "split into one unit per finding, each citing the same sentence")
                break

    # V11: results and methods rest on data, not on a label
    if utype in ("result", "method_detail"):
        data = [s for s in sents if s["kind"] == "table_row"
                or (s["kind"] == "text" and s["section"] in DATA_SECTIONS and not s.get("in_table"))]
        if not data:
            err(i, "V11", f"a result/method_detail must cite a sentence with the data (Results/Methods text or a "
                          f"table row); these are {sorted({s['section'] + ' ' + s['kind'] for s in sents})}. "
                          "A caption, title or table text read as one line is not data (cite the table_row "
                          "sentences); Introduction/Discussion statements are interpretation, background or "
                          "cited_statement")

    # V12: figure-only values
    value_source = outcome.get("value_source")
    if value_source is not None and (not isinstance(value_source, str) or value_source not in VALUE_SOURCES):
        err(i, "V12", f"value_source must be one of {sorted(VALUE_SOURCES)}")
    if value_source == "figure_only" and (outcome.get("values") or outcome.get("n") or outcome.get("p")):
        err(i, "V12", "value_source 'figure_only': leave values, n and p empty (never estimate numbers from a figure)")
    if value_source == "table" and not any(s["kind"] == "table_row" for s in sents):
        err(i, "V12", "value_source 'table' requires citing the table row(s)")

    # V9: terms, normalised to the library's canonical names
    terms = unit.get("terms") or {}
    if utype in ("result", "method_detail"):
        has_term = isinstance(terms, dict) and any(  # topics alone do not count (§3.2)
            isinstance(values, list) and any(str(v).strip() for v in values)
            for key, values in terms.items() if key != "topics")
        if not has_term:
            message = ("result/method_detail must provide at least one non-empty terms list among "
                       "concepts, methods, molecules, cell_types or species")
            if legacy_terms_warning:
                (warn or flag)(i, "V9", message + "; stored legacy unit retained for recheck")
            else:
                err(i, "V9", message)
    if "terms" not in unit and utype not in ("result", "method_detail"):
        terms = {}
    if not isinstance(terms, dict):
        err(i, "V9", f"terms must be an object with lists: {TERM_TYPES}")
        terms = {}
    clean_terms = {key: [] for key in TERM_TYPES}
    for key, values in terms.items():
        if key not in TERM_TYPES or not isinstance(values, list):
            err(i, "V9", f"terms.{key}: allowed keys are {TERM_TYPES}, each a list of strings")
            continue
        out = []
        for value in values:
            if not isinstance(value, str):
                err(i, "V9", f"terms.{key}: each term is a string, not {value!r}")
                continue
            value = re.sub(r"\s+", " ", value).strip()
            if not value:
                continue
            canonical = ctx.canonical.get(value.lower(), concepts.capitalize_first(value) if value.islower() else value)
            if canonical != value:
                normalized.append({"unit_index": i, "from": value, "to": canonical})
            if key != "topics" and not ctx.grounded(value) and not ctx.grounded(canonical):
                flag(i, "V3", f"term {value!r} ({key}) is not found in this paper")
            if canonical not in out:
                out.append(canonical)
        clean_terms[key] = out

    # Collapse spelling variants and duplicate type proposals using the shared registry.
    collapsed = concepts.normalize_terms(clean_terms, ctx.registry, keep_rule_types=True)
    for key in TERM_TYPES:
        for value in collapsed[key]:
            if value not in clean_terms[key]:
                normalized.append({"unit_index": i, "from_type": "duplicate", "to": value})
    clean_terms = collapsed

    pages = sorted({s["page"] for s in sents})
    sections = sorted({s["section"] for s in sents})
    stored = {
        "unit_type": utype, "statement": statement, "sentence_ids": sids,
        "quote": _quote(sents), "pages": pages, "sections": sections,
        "model": model, "intervention": intervention, "outcome": outcome, "quality": quality,
        "hedging": hedging, "cited_refs": cited_refs, "terms": clean_terms,
    }
    if claim_ids:  # the key exists only when the analyst gave it (old units and fingerprints stay as they were)
        stored["hedged_sentence_ids"] = claim_ids
    if filled_by:  # only when a field was filled (EV-17: rule or a later AI fill); crosscheck._fingerprint does not hash it
        stored["filled_by"] = filled_by
    return stored


def _quote(sents):
    """The verbatim evidence: sentence texts in order; ' […] ' marks a gap between them."""
    parts, last = [], None
    for s in sorted(sents, key=lambda s: s["seq"]):
        # no marker between the two halves of ONE sentence cut by a figure/table (EV-16)
        if last is not None and s["seq"] != last["seq"] + 1 and _short(last.get("continues") or "") != _short(s["sentence_id"]):
            parts.append("[…]")
        parts.append(s["text"])
        last = s
    return " ".join(parts)


def _check_research_question(ctx, rq, err, flag):
    if not rq:
        return None
    raw_ids = _as_list(rq.get("sentence_ids")) if isinstance(rq, dict) else None
    if not raw_ids or not _s(rq.get("text")) or not all(isinstance(s, (str, int)) for s in raw_ids):
        err(0, "V2", "research_question must be {text, sentence_ids} (sentences from the abstract/introduction)")
        return None
    sids = [ctx.sid(s) for s in raw_ids]
    if any(s not in ctx.sentences for s in sids):
        err(0, "V1", "research_question cites unknown sentence ids")
        return None
    # a paper whose body has no Introduction/Methods/Results headings (a review: all `main_text`) has no better section
    structured = {s["section"] for s in ctx.sentences.values()} & {"introduction", "methods", "results", "results_discussion"}
    allowed = ("abstract", "introduction") if structured else ("abstract", "introduction", "main_text")
    outside = [s for s in sids if ctx.sentences[s]["section"] not in allowed
               and not _is_title_sentence(ctx, ctx.sentences[s])]
    if outside:
        flag(0, "V1", f"research_question cites {[_short(s) for s in outside]} outside abstract/introduction/title")
    missing = numbers(rq["text"]) - numbers(" ".join(_masked(ctx.sentences[s]) for s in sids))
    if missing:
        err(0, "V2", f"research_question has numbers {sorted(missing)} not in its sentences")
        return None
    return {"text": rq["text"].strip(), "sentence_ids": sids}


def _check_summary(ctx, summary, accepted_by_index, err, flag):
    """summary: [{text, units: [1-based indexes of submitted units]}], each sentence grounded in its units."""
    if not summary:
        return []
    if not isinstance(summary, list):
        err(0, "V2", "summary must be a list of {text, units}")
        return []
    out = []
    for k, item in enumerate(summary, start=1):
        text = _s(item.get("text")) if isinstance(item, dict) else ""
        refs = item.get("units") if isinstance(item, dict) else None
        if not text or not refs:
            err(0, "V2", f"summary sentence {k} needs text and the units it summarises")
            continue
        if not isinstance(refs, list) or not refs:
            err(0, "V2", f"summary sentence {k} needs a non-empty list of accepted unit indices")
            continue
        bad = [r for r in refs if not isinstance(r, int) or isinstance(r, bool) or r not in accepted_by_index]
        if bad:
            err(0, "V2", f"summary sentence {k} refers to units {bad} that were rejected or are not accepted unit indices")
            continue
        duplicate = sorted({r for r in refs if refs.count(r) > 1})
        if duplicate:
            err(0, "V2", f"summary sentence {k} repeats unit indices {duplicate}")
            continue
        source = " ".join(accepted_by_index[r]["quote"] for r in refs)
        missing = numbers(text) - numbers(source)
        if missing:
            err(0, "V2", f"summary sentence {k} has numbers {sorted(missing)} not in its units' sources")
            continue
        if HEDGE.search(source) and _stronger_words(text, source):
            flag(0, "V5", f"summary sentence {k} states more strongly than its sources")
        out.append({"text": text, "units": refs})
    return out


# ---------------------------------------------------------------- submitting and storing

def _blocking(errors):
    """Only submission-level V10 errors store nothing; a bad summary, skip or research question is
    sent back, but the accepted units are still stored (§3.4)."""
    return [e for e in errors if e["code"] == "V10"]


def _state_path(paper_id):
    return db.paper_dir(paper_id) / "analysis_state.json"


def submit(con, paper_id, submission, max_attempts=None):
    """Validate, store what passed, and say what to do next. Caller commits.

    Each submission is the complete analysis of the paper (corrections included). While there
    are rejected units or uncovered sentences, the agent gets them back (status needs_fix).
    After `max_attempts` submissions, whatever is still wrong goes to the Review Queue."""
    from . import quality
    preflight = quality.inspect(paper_id)
    if not preflight["ready_for_analysis"]:
        db.write_json(db.paper_dir(paper_id) / "analysis_quality.json", preflight)
        raise ValueError("Extraction needs font/OCR repair; see analysis_quality.json. No analysis was replaced.")
    cfg = config.load()
    max_attempts = max_attempts or (cfg.get("analysis") or {}).get("max_attempts", 3)
    folder = db.paper_dir(paper_id)
    state = db.read_json(_state_path(paper_id), {}) or {}
    attempt = 1 if state.get("status") in (None, "done", "review") else state.get("attempt", 0) + 1
    result, accepted, rejected = validate(con, paper_id, submission)
    blocking = _blocking(result["errors"])
    submission = submission if isinstance(submission, dict) else {}
    problems = bool(result["errors"] or result["coverage_missing"])
    if not problems:
        status = "done"
    elif attempt >= max_attempts:
        status = "review"
    else:
        status = "needs_fix"

    now = datetime.now().isoformat(timespec="seconds")
    stored = 0
    if not blocking:  # a submission-level error (e.g. no classification) stores nothing
        stored = _store(con, paper_id, submission, result, accepted, attempt, status, now)
    db.write_json(_state_path(paper_id), {"attempt": attempt, "max_attempts": max_attempts, "status": status,
                                          "updated_at": now, "analyst": submission.get("analyst"),
                                          "errors": result["errors"], "flags": result["flags"],
                                          "coverage_missing": result["coverage_missing"]})
    if status == "review":
        db.write_json(folder / "analysis_rejected.json", {"attempt": attempt, "rejected": rejected,
                                                          "errors": result["errors"],
                                                          "coverage_missing": result["coverage_missing"]})
    _review_items(con, paper_id, status, result, len(rejected))

    next_step = {
        "done": "Stored. Nothing else to do for this paper.",
        "needs_fix": f"Fix the listed errors and cover the missing sentences (add units or list them in 'skipped' "
                     f"with a reason), then submit the complete analysis again. Attempt {attempt} of {max_attempts}.",
        "review": "Maximum attempts reached: accepted units are stored, unresolved quality failures remain in analysis_rejected.json "
                  "and processing status. Move on to the next paper.",
    }[status]
    if blocking:
        next_step = "Nothing stored: fix the submission-level errors (unit_index 0) and submit again. " + next_step
    return {"ok": status == "done", "status": status, "attempt": attempt, "max_attempts": max_attempts,
            "accepted_units": stored, "flagged_units": sum(1 for a in accepted if a["status"] == "flagged"),
            "rejected_units": len(rejected), "errors": result["errors"], "flags": result["flags"],
            "coverage_missing": result["coverage_missing"], "normalized_terms": result["normalized_terms"],
            "next_step": next_step}


def _store(con, paper_id, submission, result, accepted, attempt, status, now):
    folder = db.paper_dir(paper_id)
    analyst = submission.get("analyst")
    records = []
    submitted_indices = {}
    for seq, unit in enumerate(accepted, start=1):
        submitted_indices[unit.get("_validation_unit_index", seq)] = seq
        unit = {key: value for key, value in unit.items() if key != "_validation_unit_index"}
        records.append({"unit_id": f"{paper_id}:E{seq:04d}", "seq": seq, **unit, "analyst": analyst,
                        "attempt": attempt, "created_at": now})
    from . import review_policy
    records, aliases = review_policy.deduplicate(records)
    seqs = {r['unit_id']: r['seq'] for r in records}
    for sentence in result.get('summary') or []:
        sentence['units'] = list(dict.fromkeys(seqs.get(aliases.get(f"{paper_id}:E{i:04d}", f"{paper_id}:E{i:04d}"), i)
                                                for i in (submitted_indices.get(j, j) for j in sentence.get('units', []))))
    terms = {t: [] for t in TERM_TYPES}
    for r in records:
        for key, values in r["terms"].items():
            for v in values:
                if v not in terms[key]:
                    terms[key].append(v)
    registry = concepts.build_registry(con)
    canonical = _canonical_terms(con)
    for t in _as_list(submission.get("topics")) or []:
        value = re.sub(r"\s+", " ", t).strip() if isinstance(t, str) else ""
        if value:
            value = canonical.get(value.lower(), concepts.capitalize_first(value) if value.islower() else value)
            if value.lower() not in {x.lower() for x in terms["topics"]}:
                terms["topics"].append(value)
    terms = concepts.normalize_terms(terms, registry, keep_rule_types=True)
    analysis = {
        "version": 2, "analyst": analyst, "analysed_at": now[:10], "status": status, "attempt": attempt,
        "classification": submission.get("classification"),
        "classification_reason": submission.get("classification_reason"),
        "evidence_type": submission.get("evidence_type"),
        "research_question": result["research_question"], "summary": result["summary"],
        "n_units": len(records), "n_flagged": sum(1 for r in records if r["status"] == "flagged"),
        "coverage_missing": result["coverage_missing"], "skipped": result["skipped"],
        **terms, "claims": [],
    }
    current = folder / "analysis.json"
    if current.exists():
        history = folder / "analysis_history"
        history.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        current.replace(history / f"analysis_{stamp}.json")
        if (folder / "evidence.jsonl").exists():
            (folder / "evidence.jsonl").replace(history / f"evidence_{stamp}.jsonl")
    db.write_json(folder / "evidence_aliases.json", aliases)
    db.write_jsonl(folder / "evidence.jsonl", records)
    db.write_json(current, analysis)

    meta = db.read_json(folder / "metadata.json")
    meta.update({"classification": analysis["classification"],
                 "analysis_status": "done" if status in ("done", "review") else "needs_fix",
                 "evidence_type": analysis["evidence_type"],
                 "summary": " ".join(s["text"] for s in analysis["summary"]) or None,
                 "updated_at": config.now()})
    db.write_json(folder / "metadata.json", meta)
    relink = db.reload_paper(con, paper_id)
    from . import concept_notes  # local import: concept_notes depends on notes/concepts
    for vault in meta.get("vaults", []):
        notes.write_paper_note(meta, vault)
        notes.ensure_analysis_stubs(meta, vault)
    affected = (relink or {}).get("affected_concepts", [])  # only these concept notes (+ ancestors) can differ
    for vault in sorted(set(meta.get("vaults", [])) | set((relink or {}).get("affected_vaults", []))):
        notes.ensure_linked_stubs(con, vault)
        concept_notes.refresh(con, vault, concept_ids=affected)
    return len(records)


def resolve_unit(con, unit_id):
    """Resolve a preserved exact-dedup ID; return canonical record with requested ID audit.

    Callers must still apply vault membership, quality and current-source checks.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+:E[0-9]{4,}", str(unit_id)):
        raise ValueError("Invalid evidence ID")
    paper_id = str(unit_id).rsplit(':E', 1)[0]
    folder = db.paper_dir(paper_id)
    aliases = db.read_json(folder / 'evidence_aliases.json', {}) or {}
    requested, visited = unit_id, set()
    while unit_id in aliases:
        if unit_id in visited:
            raise ValueError('Cyclic evidence alias')
        visited.add(unit_id)
        unit_id = aliases[unit_id]
        if str(unit_id).rsplit(':E', 1)[0] != paper_id:
            raise ValueError('Cross-paper evidence alias')
    record = next((u for u in db.read_jsonl(folder / 'evidence.jsonl') if u['unit_id'] == unit_id), None)
    return {**record, 'requested_unit_id': requested} if record else None


def duplicate_claim_count(con, paper_id):
    """Count exact repeated claim/source groups without merging scientific metadata."""
    row = con.execute(
        "SELECT COALESCE(SUM(n - 1), 0) FROM ("
        "SELECT COUNT(*) AS n FROM evidence WHERE paper_id = ? "
        "GROUP BY unit_type, statement, sentence_ids HAVING COUNT(*) > 1)", (paper_id,)).fetchone()
    return row[0] or 0


def _review_items(con, paper_id, status, result, n_rejected, when="After the last allowed attempt"):
    """Evidence quality remains on units/state; human queue is relevance only."""
    from . import review_policy
    review_policy.refresh_paper(con, paper_id)
    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}
    for vault in meta.get("vaults", []):
        notes.write_indexes(con, vault)


def status(paper_id):
    return db.read_json(_state_path(paper_id), {"status": "pending", "attempt": 0})


DERIVED_UNIT_KEYS = ("unit_id", "seq", "quote", "pages", "sections", "status", "flags", "analyst", "attempt",
                     "created_at", "recheck_errors", "aliases")


def _retain_rechecked_units(stored, accepted, rejected, flags, current_sentences=None, paper_id=None):
    """Refresh derived fields without allowing a validator regression to delete a unit."""
    accepted_by_index = {record.get("_validation_unit_index"): record for record in accepted}
    rejected_by_index = {item["unit_index"]: item for item in rejected}
    refreshed = []
    extra_flags = []
    for index, original in enumerate(stored, start=1):
        record = dict(original)
        if index in accepted_by_index:
            # Keep identity/audit fields from the stored record; replace only the fields
            # that validation derives afresh (quote, location, terms, flags, status, etc.).
            identity = {key: record[key] for key in ("unit_id", "seq", "analyst", "attempt", "created_at")
                        if key in record}
            record.update(accepted_by_index[index])
            record.update(identity)
            record.pop("_validation_unit_index", None)
            record.pop("recheck_errors", None)
        else:
            item = rejected_by_index.get(index, {})
            errors = item.get("errors") or [{"code": "RECHECK", "message": "unit was not accepted on recheck"}]
            if current_sentences is not None:
                current_quote_sentences = []
                for sentence_id in record.get("sentence_ids", []):
                    sid = str(sentence_id)
                    if not sid.startswith(f"{paper_id}:"):
                        sid = f"{paper_id}:{sid}"
                    sentence = current_sentences.get(sid)
                    if sentence is None:
                        current_quote_sentences = []
                        break
                    current_quote_sentences.append(sentence)
                if current_quote_sentences:
                    record["quote"] = _quote(current_quote_sentences)
            retained = [{"unit_index": index, "code": error.get("code", "RECHECK"),
                         "severity": "flag", "message": "Recheck retained stored unit: "
                         + str(error.get("message") or error)} for error in errors]
            record["flags"] = [flag for flag in flags if flag.get("unit_index") == index] + retained
            record["status"] = "flagged"
            record["recheck_errors"] = errors
            extra_flags.extend(retained)
        refreshed.append(record)
    return refreshed, extra_flags


def source_drift(units, sentences, paper_id):
    """1-based indexes of units whose cited sentences vanished or no longer read as when the unit was
    made: the stored quote or sections differ from the current sentences (a re-extraction can leave an
    old sentence ID unmapped while the same number now names another sentence)."""
    drifted = []
    for index, unit in enumerate(units, start=1):
        rows = [sentences.get(s if ":" in str(s) else f"{paper_id}:{s}") for s in unit.get("sentence_ids") or []]
        if (not rows or any(row is None for row in rows)
                or ("quote" in unit and _quote(rows).replace(" […] ", " ") != unit["quote"].replace(" […] ", " "))
                or ("sections" in unit and sorted({row["section"] for row in rows}) != unit["sections"])):
            drifted.append(index)
    return drifted


def _drift_flag(index):
    return {"unit_index": index, "code": "V14", "severity": "flag",
            "message": "A cited sentence is missing or changed since this unit was made (stored quote or section "
                       "differs from the current source). Re-read the source; re-analyse the unit."}


REF_GONE = "A cited reference no longer exists"  # V6 flag set by renumber.carry_over; recheck keeps it (sticky)


def ref_gone_flag(index, names):
    return {"unit_index": index, "code": "V6", "severity": "flag",
            "message": f"{REF_GONE}: {names} is not an entry of the new reference list (the entry was split or "
                       "re-read), and its number may now name another entry. Re-read the source; re-cite the unit."}


def flag_source_drift(units, sentences, paper_id):
    """Units with V14 added (status flagged) where the cited source drifted; others unchanged."""
    drifted = set(source_drift(units, sentences, paper_id))
    return [{**unit, "flags": [f for f in unit.get("flags") or [] if f.get("code") != "V14"] + [_drift_flag(i)],
             "status": "flagged"} if i in drifted else unit for i, unit in enumerate(units, start=1)]


def export_submission(paper_id):
    """The stored v2 analysis as a COMPLETE submission (all units, summary, skipped; short IDs), exactly what
    `recheck` validates and what `kb.py submit-analysis` accepts. Reads only; shared by recheck and export-analysis."""
    folder = db.paper_dir(paper_id)
    analysis = db.read_json(folder / "analysis.json")
    if not analysis or analysis.get("version") != 2:
        raise ValueError(f"{paper_id} has no v2 analysis to export")
    return submission_from(paper_id, analysis, db.read_jsonl(folder / "evidence.jsonl"))


def submission_from(paper_id, analysis, stored_units):
    """The pure part of export_submission (stored analysis.json + evidence.jsonl rows in, submission out)."""
    units = []
    for u in stored_units:
        unit = {k: v for k, v in u.items() if k not in DERIVED_UNIT_KEYS}
        unit["sentence_ids"] = [_short(s) for s in unit["sentence_ids"]]
        if unit.get("cited_refs"):
            unit["cited_refs"] = [_short(r) for r in unit["cited_refs"]]
        units.append(unit)
    submission = {k: analysis.get(k) for k in ("analyst", "classification", "classification_reason",
                                                "evidence_type", "research_question", "summary", "topics")}
    submission.update({"paper_id": paper_id, "units": units, "skipped": analysis.get("skipped") or []})
    if submission["research_question"]:
        submission["research_question"] = {"text": submission["research_question"]["text"],
                                           "sentence_ids": [_short(s) for s in
                                                            submission["research_question"]["sentence_ids"]]}
    return submission


def repair_report(paper_id):
    """Lines for a human/agent: stored units that carry flags or whose cited sentences no longer match the stored
    quote (drift), the best new sentences for that quote, and the uncovered must_cover sentences."""
    folder = db.paper_dir(paper_id)
    sentences = {_short(s["sentence_id"]): s for s in db.read_jsonl(folder / "sentences.jsonl")}
    tokens = lambda t: set(re.findall(r"[a-z0-9]{3,}", t.lower()))
    lines = []
    for i, u in enumerate(db.read_jsonl(folder / "evidence.jsonl"), start=1):
        ids = [_short(s) for s in u["sentence_ids"]]
        rows = [sentences.get(s) for s in ids]
        quote = tokens(u.get("quote", "").replace(" […] ", " "))
        drift = any(r is None for r in rows) or tokens(" ".join(r["text"] for r in rows if r)) != quote
        codes = sorted({f["code"] for f in u.get("flags", [])})
        if not (codes or drift):
            continue
        best = sorted(((len(quote & tokens(s["text"])) / (len(quote) or 1), k) for k, s in sentences.items()),
                      reverse=True)[:6]
        lines += [f"UNIT {i} {u['unit_type']} flags={codes} drift={drift} ids={ids}",
                  f"  stmt: {u['statement'][:160]}",
                  f"  best new sentences: {[(k, round(v, 2), sentences[k]['section']) for v, k in best if v > 0.15]}"]
    state = db.read_json(_state_path(paper_id), {}) or {}
    lines.append("UNCOVERED:")
    for sid in state.get("coverage_missing", []):
        r = sentences.get(_short(sid))
        if r:
            lines.append(f"  {_short(sid)} [{r['section']} p{r['page']}] {r['text'][:200]}")
    lines.append("ERRORS: " + str([(e["unit_index"], e["code"], e["message"][:100]) for e in state.get("errors", [])]))
    return lines


def recheck(con, paper_id):
    """Run and persist the current checks without dropping stored evidence units.

    Legacy units missing the later terms requirement are warnings.  Any unit rejected by
    another newly tightened rule remains in evidence.jsonl as a flagged record, with the
    rejection details returned and written to the review queue.
    """
    folder = db.paper_dir(paper_id)
    analysis = db.read_json(folder / "analysis.json")
    if not analysis or analysis.get("version") != 2:
        raise ValueError(f"{paper_id} has no v2 analysis to recheck")
    stored = list(db.read_jsonl(folder / "evidence.jsonl"))
    submission = export_submission(paper_id)
    # Rechecking must not replace a stored analysis.  Old analyses predate G3 and
    # therefore report missing terms as warnings while all other checks remain active.
    result, accepted, rejected = validate(con, paper_id, submission, legacy_terms_warning=True)
    current_sentences = {sentence["sentence_id"]: sentence for sentence in
                         db.read_jsonl(db.paper_dir(paper_id) / "sentences.jsonl")}
    drifted = set(source_drift(stored, current_sentences, paper_id))  # before refreshing overwrites the stored quotes
    refreshed, retained_flags = _retain_rechecked_units(
        stored, accepted, rejected, result["flags"], current_sentences=current_sentences, paper_id=paper_id)
    if retained_flags:
        result["flags"].extend(retained_flags)
    # V13 is an independent second-model review. Its durable source is a separate
    # machine report so routine V1-V12 rechecks can reproduce these flags.
    from . import crosscheck
    v13 = crosscheck.results_for(paper_id, refreshed)
    v13_flags = []
    for index, unit in enumerate(refreshed, start=1):
        review = v13.get(unit.get("unit_id"))
        previous_v13 = [flag for flag in stored[index - 1].get("flags", []) if flag.get("code") == "V13"]
        if not review:
            # A missing or stale second-model result is not evidence that the old
            # verification flag was wrong. Keep it visible until a current review replaces it.
            if previous_v13:
                unit["flags"] = [flag for flag in unit.get("flags", []) if flag.get("code") != "V13"] + previous_v13
                unit["status"] = "flagged"
                v13_flags.extend(previous_v13)
            continue
        unit["flags"] = [flag for flag in unit.get("flags", []) if flag.get("code") != "V13"]
        if unit.get("status") == "flagged" and previous_v13 and not unit["flags"]:
            unit["status"] = "ok"
        if review.get("verdict") not in ("partly", "not_supported"):
            continue
        verdict = review["verdict"]
        flag = {"unit_index": index, "code": "V13", "severity": "flag",
                "message": f"Independent verifier: {verdict}. {review.get('reason', '')}".strip()}
        unit["flags"] = [f for f in unit.get("flags", []) if f.get("code") != "V13"] + [flag]
        unit["status"] = "flagged"
        v13_flags.append(flag)
    if v13_flags:
        result["flags"] = [f for f in result["flags"] if f.get("code") != "V13"] + v13_flags
    # V14 (source drift) is sticky like V13: the refreshed quote now matches the current sentence, so only the
    # flag remembers that the unit was made from different source text. It ends when the unit is re-analysed.
    v14_flags = []
    for index, unit in enumerate(refreshed, start=1):
        previous_v14 = [flag for flag in stored[index - 1].get("flags", []) if flag.get("code") == "V14"]
        if previous_v14 or index in drifted:
            kept = previous_v14 or [_drift_flag(index)]
            unit["flags"] = [flag for flag in unit.get("flags", []) if flag.get("code") != "V14"] + kept
            unit["status"] = "flagged"
            v14_flags.extend(kept)
    if v14_flags:
        result["flags"] = [f for f in result["flags"] if f.get("code") != "V14"] + v14_flags
    # The V6 flag "a cited reference no longer exists" is sticky too: validation cannot tell that a kept number
    # now names another entry, so it would pass whenever that entry happens to be cited in the same sentences.
    for index, unit in enumerate(refreshed, start=1):
        gone = [f for f in stored[index - 1].get("flags", []) if f.get("code") == "V6" and REF_GONE in f.get("message", "")]
        if gone:
            unit["flags"] = [f for f in unit.get("flags", []) if f not in gone] + gone
            unit["status"] = "flagged"
            result["flags"] = [f for f in result["flags"] if f not in gone] + gone
    # Preserve the normal recheck status semantics.  A retained rejected unit is
    # still reported to Review Queue below, but the stored analysis remains in the
    # same needs_fix state a regular submit would have used.
    result["status"] = "done" if not result["errors"] and not result["coverage_missing"] else "needs_fix"
    now = datetime.now().isoformat(timespec="seconds")
    n_flagged = sum(1 for unit in refreshed if unit.get("status") == "flagged")
    analysis.update({"status": result["status"], "n_units": len(refreshed), "n_flagged": n_flagged,
                     "coverage_missing": result["coverage_missing"]})
    db.write_jsonl(folder / "evidence.jsonl", refreshed)
    db.write_json(folder / "analysis.json", analysis)
    # attempt 0: the next agent submission starts a fresh cycle instead of inheriting used-up attempts
    db.write_json(_state_path(paper_id), {**(db.read_json(_state_path(paper_id), {}) or {}),
                                          "status": result["status"], "updated_at": now,
                                          **({"attempt": 0} if result["status"] == "needs_fix" else {}),
                                          "errors": result["errors"], "flags": result["flags"],
                                          "coverage_missing": result["coverage_missing"]})
    if rejected or result["coverage_missing"]:
        db.write_json(folder / "analysis_rejected.json", {"recheck": True, "rejected": rejected,
                                                          "errors": result["errors"],
                                                          "coverage_missing": result["coverage_missing"]})
    meta = db.read_json(folder / "metadata.json") or {}
    meta.update({"analysis_status": "done" if result["status"] in ("done", "review") else "needs_fix",
                 "updated_at": config.now()})
    db.write_json(folder / "metadata.json", meta)
    db.reload_paper(con, paper_id)
    # nobody is fixing a recheck: stored flagged units always keep their review item, and units or
    # sentences the current rules no longer accept are reported as incomplete
    _review_items(con, paper_id, "review" if rejected or result["coverage_missing"] else "done", result,
                  len(rejected), when="Recheck with the current rules")
    for vault in meta.get("vaults", []):
        notes.write_paper_note(meta, vault, con=con)
    result["accepted_units"] = len(accepted)
    result["flagged_units"] = n_flagged
    result["v13_reviewed_units"] = len(v13)
    result["v13_flagged_units"] = len(v13_flags)
    # Legacy V9 term omissions are diagnostics for this recheck only.  They
    # must not become stored unit flags or review-queue items.
    result["legacy_term_warnings"] = result.get("warnings", [])
    result["rejected_units"] = len(rejected)
    result["stored_units"] = len(refreshed)
    result["stored_unchanged"] = True
    result["stored"] = True
    return result


ANALYSIS_FILES = ("analysis.json", "evidence.jsonl", "analysis_state.json", "analysis_rejected.json",
                  "analysis_v2.parts.json", "analysis_v2.draft.json", "analysis.draft.json",
                  "analysis_runner.json", "analysis_runner_reply.json")


def reset(con, paper_id, reason="reset by the researcher"):
    """Take a paper's AI analysis out of use, reversibly: the files move to
    analysis_history/removed_<time>/, the paper becomes 'pending' again, its note and the
    database are refreshed, and its open analysis review items are closed. Caller commits."""
    from . import manage
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if meta is None:
        raise FileNotFoundError(f"No paper {paper_id}")
    target = folder / "analysis_history" / f"removed_{datetime.now():%Y%m%d_%H%M%S}"
    moved = []
    for name in ANALYSIS_FILES:
        if (folder / name).exists():
            target.mkdir(parents=True, exist_ok=True)
            (folder / name).replace(target / name)
            moved.append(name)
    if moved:
        (target / "REASON.txt").write_text(reason + "\n", encoding="utf-8", newline="\n")
    meta.update({"classification": "UNREVIEWED", "analysis_status": "pending", "evidence_type": None,
                 "summary": None, "updated_at": config.now()})
    db.write_json(folder / "metadata.json", meta)
    db.reload_paper(con, paper_id)
    for item in [dict(r) for r in con.execute(
            "SELECT * FROM review_items WHERE paper_id = ? AND status = 'pending' "
            "AND kind IN ('analysis_flags', 'analysis_duplicates', 'analysis_incomplete', 'classification')", (paper_id,))]:
        con.execute("UPDATE review_items SET status = 'superseded', resolved_at = ? WHERE item_id = ?",
                    (config.now(), item["item_id"]))
        for vault in meta.get("vaults", []):
            manage._mark_review_note(item, "analysis removed", vault)
    # relations of the removed units are archived, concept links/ghosts rebuilt and the notes refreshed
    manage._refresh_after_removal(con, meta.get("vaults", []), paper_id)
    return {"paper_id": paper_id, "moved": moved, "to": config.rel(target) if moved else None}


# ---------------------------------------------------------------- building an analysis in small steps
# Free or small models cannot write one huge JSON reliably. They add units in batches
# (each unit is checked at once) and then call finish_analysis, which runs the complete
# validation exactly as submit() does. Only finish_analysis counts as an attempt.

def _draft_path(paper_id):
    return db.paper_dir(paper_id) / "analysis_v2.parts.json"


def _draft(paper_id):
    return db.read_json(_draft_path(paper_id), {"units": [], "skipped": []}) or {"units": [], "skipped": []}


def _draft_progress(ctx, draft):
    covered = {ctx.sid(s) for u in draft["units"] for s in u.get("sentence_ids") or []}
    covered |= {ctx.sid(s["sentence_id"]) for s in draft["skipped"]}
    covered = {ctx.head.get(s, s) for s in covered}
    missing = [_short(s) for s in required_sentences(ctx) if s not in covered]
    return {"draft_units": len(draft["units"]), "skipped": len(draft["skipped"]),
            "must_cover_remaining": len(missing), "next_uncovered": missing[:40]}


def add_units(con, paper_id, units):
    """Check units one by one and keep those without errors in the paper's draft.
    Returns per-unit results with the draft number of each kept unit (used by summary)."""
    ctx = PaperContext(con, paper_id)
    draft = _draft(paper_id)
    results = []
    for unit in units if isinstance(units, list) else [units]:
        errors, flags, normalized = [], [], []
        _check_unit(ctx, 0, unit,
                    False, lambda i, code, msg: errors.append({"code": code, "message": msg}),
                    lambda i, code, msg: flags.append({"code": code, "message": msg}), normalized)
        if errors:
            results.append({"kept": False, "errors": errors})
        else:
            draft["units"].append(unit)
            results.append({"kept": True, "draft_number": len(draft["units"]), **({"warnings": flags} if flags else {})})
    db.write_json(_draft_path(paper_id), draft)
    return {"results": results, **_draft_progress(ctx, draft)}


def skip_sentences(con, paper_id, skipped):
    """Record must-cover sentences that deserve no unit, each with a reason."""
    ctx = PaperContext(con, paper_id)
    draft = _draft(paper_id)
    known = {s["sentence_id"] for s in draft["skipped"]}
    problems = []
    for item in skipped if isinstance(skipped, list) else [skipped]:
        sid = item.get("sentence_id") if isinstance(item, dict) else item
        reason = _s(item.get("reason")) if isinstance(item, dict) else ""
        if ctx.sid(sid) not in ctx.sentences:
            problems.append(f"{sid}: no such sentence")
        elif not reason:
            problems.append(f"{sid}: a reason is required")
        elif _short(ctx.sid(sid)) not in known:
            draft["skipped"].append({"sentence_id": _short(ctx.sid(sid)), "reason": reason.strip()})
            known.add(_short(ctx.sid(sid)))
    db.write_json(_draft_path(paper_id), draft)
    return {"problems": problems, **_draft_progress(ctx, draft)}


def remove_units(con, paper_id, draft_numbers):
    """Remove draft units by their draft numbers (later numbers shift down)."""
    ctx = PaperContext(con, paper_id)
    draft = _draft(paper_id)
    drop = {int(n) for n in (_as_list(draft_numbers) or []) if str(n).strip().isdigit()}
    draft["units"] = [u for k, u in enumerate(draft["units"], start=1) if k not in drop]
    db.write_json(_draft_path(paper_id), draft)
    return {"removed": sorted(drop), **_draft_progress(ctx, draft)}


def draft_status(con, paper_id):
    ctx = PaperContext(con, paper_id)
    draft = _draft(paper_id)
    units = [{"draft_number": k, "unit_type": u.get("unit_type"), "sentence_ids": u.get("sentence_ids"),
              "statement": (u.get("statement") or "")[:120]} for k, u in enumerate(draft["units"], start=1)]
    return {**_draft_progress(ctx, draft), "units": units}


def finish(con, paper_id, fields):
    """Submit the draft (units + skipped) with the paper-level fields; same checks as submit().
    The draft is kept while the status is needs_fix, and removed once done or review."""
    draft = _draft(paper_id)
    submission = {k: v for k, v in fields.items() if k not in ("units", "skipped")}
    submission["paper_id"] = paper_id
    submission["units"] = draft["units"]
    submission["skipped"] = draft["skipped"] + [s for s in fields.get("skipped") or []]
    result = submit(con, paper_id, submission)
    if result["status"] in ("done", "review"):
        _draft_path(paper_id).unlink(missing_ok=True)
    else:
        result["next_step"] = ("Use add_units for the uncovered sentences (or skip_sentences with reasons), "
                               "remove_units for units with errors and add them corrected, then call "
                               "finish_analysis again. Unit numbers in errors are draft numbers. " + result["next_step"])
    return result


def reading_view(con, paper_id, section=None, start=1, limit=200):
    """The paper as the agent reads it: numbered sentences with their citations resolved,
    which sentences must be covered (V8), and the reference list size."""
    ctx = PaperContext(con, paper_id)
    tables = {t['table_id']: t for t in db.read_jsonl(db.paper_dir(paper_id) / 'tables.jsonl')}
    heads = required_sentences(ctx)
    required = {m for h in heads for m in ctx.members[h]}  # every part of a cut sentence is marked
    refs = _reference_by_number(ctx.refs.values())
    rows = []
    for sid, s in sorted(ctx.sentences.items(), key=lambda kv: kv[1]["seq"]):
        if s["seq"] < start or (section and s["section"] != section) or s["section"] == "references":
            continue
        cites = []
        for c in s["citations"]:
            for n in c["ref_nums"]:
                r = refs.get(n) or {}
                cites.append(f"R{n:03d} {r.get('first_author') or '?'} {r.get('year') or ''}".strip()
                             + (" (uncertain)" if c["confidence"] == "low" else ""))
        rows.append({"id": _short(sid), "section": s["section"], "page": s["page"], "kind": s["kind"],
                     "source": s.get("source", "main"),
                     **({"subsection": s["subsection"]} if s.get("subsection") else {}),
                     **({"object_id": _short(s["object_id"]),
                         "table_quality": tables.get(s["object_id"], {}).get("quality", "uncertain")}
                        if s.get("object_id") else {}),
                     "text": s["text"], **({"cites": cites} if cites else {}),
                     **({"in_table": True} if s.get("in_table") else {}),
                     **({"continues": _short(s["continues"])} if s.get("continues") else {}),
                     **({"must_cover": True} if sid in required else {})})
        if len(rows) >= limit:
            break
    meta = ctx.meta
    last_seq = ctx.sentences[ctx.sid(rows[-1]["id"])]["seq"] if rows else None
    more = last_seq is not None and len(rows) >= limit and last_seq < len(ctx.sentences)
    return {"paper_id": paper_id, "title": meta.get("title"), "year": meta.get("year"),
            "journal": meta.get("journal"), "publication_types": meta.get("publication_types"),
            "n_sentences": len(ctx.sentences), "n_must_cover": len(heads),
            "next_start": last_seq + 1 if more else None, "sentences": rows}


def _reference_by_number(refs):
    """Map numeric citations to explicit numbers, or reading-order indices for author-year lists."""
    return {(ref.get("num") or i): ref for i, ref in enumerate(refs, start=1)}


def _selfcheck():
    """Validator behaviour on a small fixture paper (no files, no database). Returns the number of checks."""
    def s(n, section, text, passage, kind="text", cites=()):
        cit = [{"marker": m, "ref_nums": [r], "style": "numeric", "confidence": "high",
                "start": text.index(m), "end": text.index(m) + len(m)} for m, r in cites]
        return {"sentence_id": f"P:S{n:04d}", "seq": n, "passage_id": passage, "page": 1, "section": section,
                "kind": kind, "text": text, "citations": cit, "mentions": []}
    ctx = PaperContext.__new__(PaperContext)
    ctx.paper_id, ctx.meta, ctx.table_caption_ids = "P", {}, set()
    ctx.refs = {"P:R001": {"ref_id": "P:R001", "num": 1, "year": 2010}}
    ctx.sentences = {x["sentence_id"]: x for x in [
        s(1, "results", "Muse cells increased to 12.5 ± 1.1% (P < 0.05) compared with −2.0 in controls.", "a"),
        s(2, "results", "Smoking was associated with lower CD34 cells.", "a"),
        s(3, "discussion", "These data suggest that resveratrol may improve development.", "b"),
        s(4, "discussion", "Marlow et al. reported that Muse cells repair tissue [1].", "c", cites=[("[1]", 1)]),
        s(5, "results", "Cells were counted.", "d"), s(6, "results", "Cells were fixed.", "e"),
        s(7, "results", "Cells were stained.", "f"), s(8, "results", "Cells were imaged.", "g"),
        s(9, "results", "No significant difference was found in ATP content.", "h"),
        s(10, "results", "Abbreviations: ATP, adenosine triphosphate; P, probability 5.", "i"),
        s(11, "supplementary", "The online version of this article contains extra files at 10.1/x.", "j"),
        s(12, "supplementary", "See www.example-journal.org for supporting information that is available online.", "k")]}
    ctx._index()
    ctx.registry, ctx.canonical = concepts.Registry([]), {"_groups": {}}

    def check(unit, review=False):
        errors, flags = [], []
        _check_unit(ctx, 1, {"terms": {"concepts": ["Muse cell"]}, **unit}, review,
                    lambda i, c, m: errors.append(c), lambda i, c, m: flags.append(c), [])
        return errors, flags
    result = {"unit_type": "result", "sentence_ids": ["S0001"]}
    assert check({**result, "statement": "Muse cells increased to 12.5 ± 1.1% (P < 0.05)."}) == ([], [])
    assert check({**result, "statement": "Muse cells increased to 13%."})[0] == ["V2"]
    assert check({**result, "statement": "Controls were 2.0 lower."})[1] == ["V2"]  # dropped sign: warning only
    assert check({**result, "statement": "Muse cells increased.", "outcome": {"values": "P < 0.05"}}) == ([], [])
    cause = check({"unit_type": "result", "sentence_ids": ["S0002"], "statement": "Smoking reduces CD34 cells."})
    assoc = check({"unit_type": "result", "sentence_ids": ["S0002"],
                   "statement": "Smoking was associated with fewer CD34 cells, causing poor outcome."})
    assert cause[1] == ["V5"] and assoc[1] == ["V5"]  # association written as causation
    assert check({"unit_type": "result", "sentence_ids": ["S0002"],
                  "statement": "Smoking was associated with lower CD34 cells."}) == ([], [])
    assert check({"unit_type": "conclusion", "sentence_ids": ["S0003"],
                  "statement": "Resveratrol improves development."})[1] == ["V5"]  # hypothesis written as finding
    assert check({"unit_type": "conclusion", "sentence_ids": ["S0003"],
                  "statement": "The authors suggest resveratrol may improve development."}) == ([], [])
    assert "V6" in check({"unit_type": "result", "sentence_ids": ["S0004"],
                          "statement": "Muse cells repair tissue."})[0]  # citing others: not this paper's result
    assert check({"unit_type": "cited_statement", "sentence_ids": ["S0004"], "cited_refs": "R001",
                  "statement": "Marlow et al. 2010 [R001] reported that Muse cells repair tissue."}) == ([], [])
    assert check({**result, "sentence_ids": ["S0001", "S0009"], "statement": "Muse cells increased."})[0] == ["V1"]
    assert check({**result, "statement": "Muse cells increased.", "outcome": {"value_source": "figure_only",
                                                                           "values": ["12.5"]}})[0] == ["V12"]
    assert check({**result, "statement": "Muse cells increased.", "terms": {"topics": ["Muse"]}})[0] == ["V9"]
    assert check({"unit_type": "result", "sentence_ids": ["S0009"], "statement": "ATP content did not change.",
                  "outcome": {"direction": "no_change", "measure": "ATP content"}}) == ([], [])
    assert check({"unit_type": ["result"], "sentence_ids": "S0001", "statement": 5})[0] == ["V1"]  # bad types
    must = {x.split(":")[-1] for x in required_sentences(ctx)}
    assert {"S0001", "S0004"} <= must and not {"S0010", "S0011", "S0012"} & must  # boilerplate never demanded
    assert not _blocking([{"unit_index": 0, "code": "V2"}, {"unit_index": 0, "code": "V8"}]) \
        and _blocking([{"unit_index": 0, "code": "V10"}])  # a bad summary must not discard accepted units
    return 17
