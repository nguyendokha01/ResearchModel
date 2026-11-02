"""v2 search (M3.1, docs/CONTRACTS.md §4.1).

Searches evidence units, sentences and table rows of the current vault and returns
ranked hits that always carry the verbatim text and where it is (paper, section, page).

- Natural questions work: stop words are dropped, all words are required first (AND); when that
  finds too little, any word may match (OR) and hits matching more of the words rank higher.
- Synonyms: groups in config/synonyms.yaml, then MeSH concept synonyms (mesh.py, M3.4).
- A sentence that is part of an evidence unit is shown as that unit (type label, AI statement and
  the exact quote), never twice.
- Ranking: bm25 x weights (a result of the paper itself above a sentence that repeats another
  paper; primary research above a review) x share of the query words matched.
- Typed biomedical identity terms also get a scoped Results/Results-and-Discussion coverage pass,
  so conflicting papers cannot be hidden by candidate caps or unit weighting
  (search() inserts such sentences the page would otherwise leave out).

Paper retrieval is read-only.  The optional non-PDF source index is created
on demand by ``sources_misc`` when an older store has not created it yet.
"""

import json
import re
import unicodedata

from . import config, db, mesh, vectors, sources_misc, sentences, standard
from .db import paper_dir, read_jsonl

# section names that never answer a scientific question
SKIP_SECTIONS = ("references", "front_matter", "acknowledgements", "funding", "author_contributions",
                 "conflicts", "data_availability", "notes")
# analysts whose units came from rule scripts (section-position labelling), not an AI reading the paper (D3.18)
HEURISTIC_ANALYSTS = ('hermes/kr-auto',)
REVIEW_TYPES = ("review", "systematic_review", "meta_analysis", "commentary")
UNIT_WEIGHT = {"result": 1.3, "conclusion": 1.15, "method_detail": 1.0, "interpretation": 0.95,
               "limitation": 0.9, "hypothesis": 0.85, "cited_statement": 0.75, "background": 0.65}
SECTION_WEIGHT = {"results": 1.1, "results_discussion": 1.05, "abstract": 1.0, "conclusion": 1.0,
                  "supplementary": 1.0, "methods": 0.95, "discussion": 0.9, "main_text": 0.9,
                  "introduction": 0.8, "figure_legends": 0.85}
KIND_WEIGHT = {"text": 1.0, "table_row": 0.95, "caption": 0.75}
UNIT_LABELS = {  # CONTRACTS §8
    "result": "Kết quả (bài này)", "cited_statement": "Bài này trích lại", "interpretation": "Tác giả diễn giải",
    "hypothesis": "Giả thuyết", "method_detail": "Phương pháp", "conclusion": "Kết luận tác giả",
    "limitation": "Hạn chế", "background": "Kiến thức nền",
}

STOPWORDS = set("""
a an and are as at be been being but by can could did do does doing for from had has have how i if in
into is it its of on or our so such than that the their them then there these they this those to
was we were what when where which while who whom why will with would you your about after again
all any both each few more most other same some very just also between during before over under
up down out off only own s t don should now vs versus role roles effect effects effectively
affect affects affected affecting impact impacts impacted impacting influence influences influenced influencing
change changes changed changing
""".split())

PAPER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]{0,200}")
_TOKEN = re.compile(r'"([^"]+)"|([^\s"]+)')
# Vietnamese-only letters (incl. acute/grave-only á à é è í ì ó ò ú ù ý ã õ ĩ ũ and U+1EA0-1EF9), NFC form.
_VIETNAMESE = re.compile(r"[\u00e0-\u00e3\u00e8-\u00ea\u00ec\u00ed\u00f2-\u00f5\u00f9\u00fa\u00fd"
                         r"\u0103\u0111\u0129\u0169\u01a1\u01b0\u1ea0-\u1ef9]", re.I)


def looks_vietnamese(text):
    return bool(_VIETNAMESE.search(unicodedata.normalize("NFC", text or "")))


# ---------------------------------------------------------------- query

def _synonym_groups(cfg):
    groups = {}
    for group in [*(cfg.get("synonyms") or []), *(cfg.get("word_variants") or [])]:
        terms = [str(t).strip() for t in group if str(t).strip()]
        for t in terms:
            groups[t.lower()] = terms
    return groups


def _clean(term):
    term = re.sub(r'["*^():?!,;{}\[\]]', " ", term).strip()
    return re.sub(r"\s+", " ", term).strip(" .'")


def parse_detail(query, cfg, use_mesh=True):
    """User text -> (groups, expansions). Each group is a list of equivalent terms.

    "quoted text" stays one phrase; AND/OR/NOT and stop words are dropped; punctuation that means
    something to FTS5 is removed. Unquoted words are first matched against config/synonyms.yaml,
    then against MeSH (longest phrase first, synonyms of the same MeSH concept). A MeSH match is
    used only when it adds a real synonym (not just a plural)."""
    synonyms = _synonym_groups(cfg)
    query = unicodedata.normalize("NFC", query or "")  # CHR-01: the query entry of find.search / neural / answer
    groups, expansions, seen = [], [], set()

    def add(variants):
        key = variants[0].lower()
        if key not in seen:
            seen.add(key)
            groups.append(variants)

    def words_run(words):
        if not words:
            return
        spans = mesh.expand(words, lambda w: w.lower() in STOPWORDS) if use_mesh else []
        taken = {}
        for start, end, variants, info in spans:
            phrase = " ".join(words[start:end])
            if phrase.lower() in synonyms:        # the researcher's own synonyms win
                continue
            kept = variants
            if len(kept) > 1 or end - start > 1:
                taken[start] = (end, kept, info if len(kept) > 1 else None)
        i = 0
        while i < len(words):
            if i in taken:
                end, kept, info = taken[i]
                add(kept)
                if info:
                    expansions.append({"phrase": kept[0], "mesh": info, "added": kept[1:]})
                i = end
                continue
            w = words[i]
            i += 1
            if w.upper() in ("AND", "OR", "NOT") or w.lower() in STOPWORDS or len(sentences.fold_search_glyphs(w)) < 2:
                continue
            add(synonyms.get(w.lower(), [w]))

    pending = []
    for phrase, word in _TOKEN.findall(query or ""):
        if phrase:
            words_run(pending)
            pending = []
            term = _clean(phrase)
            if term:
                add(synonyms.get(term.lower(), [term]))
        else:
            term = _clean(word)
            pending += term.split() if term else []
    words_run(pending)
    return groups, expansions


def parse(query, cfg):
    return parse_detail(query, cfg)[0]


def fts_expression(groups, mode="and", glyph_tokens=False):
    parts = []
    for g in groups:
        values = [variant for v in g for variant in sentences.fts_variants(v, glyph_tokens)]
        exprs = ['"' + v.replace('"', " ") + '"' for v in values]
        parts.append(exprs[0] if len(exprs) == 1 else "(" + " OR ".join(exprs) + ")")
    return (" AND " if mode == "and" else " OR ").join(parts)


def _stem(word):
    """A short prefix used to highlight and to count matched words ("mobilized" -> "mobiliz")."""
    w = word.lower()
    for suffix in ("ations", "ation", "ings", "ing", "ies", "ied", "ed", "es", "s", "ly"):
        if w.endswith(suffix) and len(w) - len(suffix) >= 4:
            return w[: -len(suffix)]
    return w


def highlight_terms(groups):
    """Word prefixes for the web page to highlight (each variant of each group)."""
    out = []
    for g in groups:
        for v in g:
            for variant in sentences.glyph_variants(v):
                out.append(" ".join(_highlight_stem(w) for w in variant.split()))
    return sorted(set(out), key=len, reverse=True)


def _guarded_stem(word):
    """(stem, letters it must go on with). A word cut to -is/-ys (organised -> organis) must go on as the same verb, not
    as organisms/characteristic/analysis; only when the typed word itself goes on so: Swiss/abyss (cut -s) and iris/cis
    (not cut) stay plain prefixes."""
    stem = _stem(word)
    nxt = word.lower()[len(stem):len(stem) + 1]
    guard = "aei" if stem.endswith("is") else "ae" if stem.endswith("ys") else ""
    return stem, guard if nxt and nxt in guard else ""


def _stem_pattern(word):
    stem, guard = _guarded_stem(word)
    return re.escape(stem) + (f"(?=[{guard}])" if guard else "")


def _highlight_stem(word):
    """The web page highlights prefixes, which cannot carry the guard: a guarded stem keeps its next letter (organise)."""
    stem, guard = _guarded_stem(word)
    return word.lower()[:len(stem) + 1] if guard else stem


def _group_patterns(groups):
    pats = []
    for g in groups:
        alts = []
        for v in g:
            for variant in sentences.glyph_variants(v):
                words = [_stem_pattern(w) for w in variant.split()]
                alts.append(r"(?<!\w)" + r"\w*\W+".join(words))
        pats.append(re.compile("|".join(alts), re.I))
    return pats

EXACT_PER_PAPER = 2


def _looks_like_specific_term(term):
    """Whether a typed token should force source-result coverage.

    Lower-case prose words are intentionally excluded.  Digits, mixed-case
    biomedical names (for example H3K9me3 or mRNA), and all-capital
    abbreviations are useful identity signals that synonym expansion and
    ranking must not be allowed to hide.
    """
    token = term.strip()
    letters = [c for c in token if c.isalpha()]
    titlecase_word = bool(token) and token[0].isupper() and token[1:].islower()
    return (bool(re.search(r"\d", token)) or
            (bool(letters) and not titlecase_word and any(c.isupper() for c in letters) and any(c.islower() for c in letters)) or
            (token.isalpha() and token.isupper() and len(token) >= 2))


def _exact_query_terms(query, cfg=None):
    """Return typed identity terms, preserving quoted technical phrases.

    This is deliberately based on the query text rather than expanded groups:
    a synonym or MeSH variant is not an exact term the researcher typed.
    Quoted phrases are treated as exact technical terms; unquoted single
    tokens need an identity signal so ordinary prose queries keep their old
    ranking behavior.
    """
    terms, seen = [], set()

    def add(term):
        term = _clean(term)
        parts = term.split()
        if not term or (len(parts) == 1 and parts[0].lower() in STOPWORDS):
            return
        key = term.casefold()
        if key not in seen:
            seen.add(key)
            terms.append(term)

    synonym_groups = _synonym_groups(cfg or {})

    def process_words(words):
        for word in words:
            if _looks_like_specific_term(word):
                add(word)
        # Only promote recognized technical phrases.  This avoids turning an
        # ordinary query such as "vitrification change" into a paper-coverage
        # request while still honoring configured synonyms and MeSH phrases.
        for n in range(min(6, len(words)), 1, -1):
            for start in range(len(words) - n + 1):
                candidate = " ".join(words[start:start + n])
                if candidate.casefold() in synonym_groups:
                    add(candidate)
        for start, end, _, _ in mesh.expand(words, lambda w: w.lower() in STOPWORDS):
            if end - start > 1:
                add(" ".join(words[start:end]))

    pending = []
    for phrase, word in _TOKEN.findall(query or ""):
        if phrase:
            process_words(pending)
            pending = []
            raw = _clean(phrase)
            if raw:
                parts = raw.split()
                if len(parts) > 1:
                    add(raw)
                elif _looks_like_specific_term(parts[0]):
                    add(parts[0])
        else:
            raw = _clean(word)
            if raw:
                pending.extend(raw.split())
    process_words(pending)
    return terms


# ---------------------------------------------------------------- search

def _vault_clause(alias, vault, all_vaults, cfg, args):
    if all_vaults:
        return ""
    args.append(vault or config.default_vault(cfg))
    return f" AND {alias}.paper_id IN (SELECT paper_id FROM paper_vaults WHERE vault = ?)"


def effective_evidence_type(row):
    """Published article type takes precedence over a generated analysis label."""
    types = row.get('publication_types') or []
    if isinstance(types, str):
        try:
            types = json.loads(types)
        except ValueError:
            types = []
    if any(str(t).casefold() in ('review', 'review-article', 'systematic review', 'meta-analysis') for t in types):
        return 'review'
    return row.get('paper_evidence_type') or row.get('evidence_type') or ''


def _paper_filters(filters, args):
    sql = []
    for key, op in (("year_from", ">="), ("year_to", "<=")):
        year = str(filters.get(key) or "").strip()
        if year.isdigit():                 # anything else (typed text) is ignored, never an error
            sql.append(f"AND pa.year {op} ?")
            args.append(int(year))
    if filters.get("paper_id"):
        sql.append("AND pa.paper_id = ?")
        args.append(filters["paper_id"])
    review_meta = "(lower(COALESCE(pa.publication_types, '')) LIKE '%\"review\"%' OR lower(COALESCE(pa.publication_types, '')) LIKE '%review-article%' OR lower(COALESCE(pa.publication_types, '')) LIKE '%systematic review%' OR lower(COALESCE(pa.publication_types, '')) LIKE '%meta-analysis%')"
    if filters.get("evidence") == "primary":
        sql.append("AND NOT " + review_meta + " AND COALESCE(pa.evidence_type, '') NOT IN (%s)" % ",".join("?" * len(REVIEW_TYPES)))
        args += REVIEW_TYPES
    elif filters.get("evidence") == "review":
        sql.append("AND (" + review_meta + " OR pa.evidence_type IN (%s))" % ",".join("?" * len(REVIEW_TYPES)))
        args += REVIEW_TYPES
    return " ".join(sql)


def _units(con, cfg, match, vault, all_vaults, filters, cap):
    args = [match]
    sql = ("""SELECT e.*, bm25(evidence_fts, 1.0, 0.8) AS bm25, pa.evidence_type AS paper_evidence_type, pa.publication_types
              FROM evidence_fts JOIN evidence e ON e.rowid = evidence_fts.rowid
              JOIN papers pa ON pa.paper_id = e.paper_id
              WHERE evidence_fts MATCH ?""")
    sql += _vault_clause("e", vault, all_vaults, cfg, args)
    sql += " " + _paper_filters(filters, args)
    sql += " ORDER BY bm25 LIMIT ?"
    args.append(cap)
    return [dict(r) for r in con.execute(sql, args)]


def _sentences(con, cfg, match, vault, all_vaults, filters, cap):
    args = [match]
    sql = ("""SELECT s.sentence_id, s.paper_id, s.seq, s.page, s.section, s.subsection, s.kind, s.text,
                     bm25(sentences_fts) AS bm25, pa.evidence_type AS paper_evidence_type, pa.publication_types
              FROM sentences_fts JOIN sentences s ON s.rowid = sentences_fts.rowid
              JOIN papers pa ON pa.paper_id = s.paper_id
              WHERE sentences_fts MATCH ?""")
    sql += " AND s.section NOT IN (%s)" % ",".join("?" * len(SKIP_SECTIONS))
    args += SKIP_SECTIONS
    sql += _vault_clause("s", vault, all_vaults, cfg, args)
    sql += " " + _paper_filters(filters, args)
    sql += " ORDER BY bm25 LIMIT ?"
    args.append(cap)
    return [dict(r) for r in con.execute(sql, args)]


def _unit_rows(con, paper_ids):
    """Every evidence unit of these papers, with its sentence ids parsed."""
    if not paper_ids:
        return {}
    rows = con.execute("""SELECT e.*, pa.evidence_type AS paper_evidence_type, pa.publication_types FROM evidence e
                          JOIN papers pa ON pa.paper_id = e.paper_id
                          WHERE e.paper_id IN (%s)""" % ",".join("?" * len(paper_ids)), list(paper_ids))
    return {r["unit_id"]: dict(r) for r in rows}


def analysis_origin(analyst):
    """'heuristic_draft' for rule-script analysts, 'analysis' for others, 'unknown' when not recorded."""
    if not analyst:
        return "unknown"
    return "heuristic_draft" if str(analyst).startswith(HEURISTIC_ANALYSTS) else "analysis"


def _unit_hit(u):
    return {
        "kind": "unit", "id": u["unit_id"], "paper_id": u["paper_id"], "unit_type": u["unit_type"],
        "label": UNIT_LABELS.get(u["unit_type"], u["unit_type"]), "statement": u["statement"],
        "text": u["quote"], "sentence_ids": json.loads(u["sentence_ids"] or "[]"),
        "pages": json.loads(u["pages"] or "[]"), "sections": json.loads(u["sections"] or "[]"),
        "status": u["status"], "hedging": u["hedging"], "species": u["species"], "cell_type": u["cell_type"],
        "agent": u["agent"], "measure": u["measure"], "direction": u["direction"],
        "value_source": u["value_source"], "analyst": u["analyst"],
        "analysis_origin": analysis_origin(u["analyst"]),
        "uncertainty": __import__('kbsys.structure', fromlist=['uncertainty']).uncertainty(u['quote']),
        "review_paper": effective_evidence_type(u) in REVIEW_TYPES,
    }


def owner_unit(units):
    """The unit that stands for a sentence cited by several: only an accepted unit, the strongest type, then source order."""
    owners = [u for u in units if u["status"] in ("ok", "human_confirmed")]
    return max(owners, key=lambda u: (UNIT_WEIGHT.get(u["unit_type"], 0.85), -int(u["seq"] or 0), u["unit_id"]), default=None)


def sentence_owners(con, paper_ids):
    """{sentence_id: owning unit row} for the sentences of these papers that an accepted unit cites."""
    by_sentence = {}
    for u in _unit_rows(con, paper_ids).values():
        for sid in json.loads(u["sentence_ids"] or "[]"):
            by_sentence.setdefault(sid, []).append(u)
    return {sid: unit for sid, units in by_sentence.items() if (unit := owner_unit(units))}


def _sentence_hit(s):
    label = {"table_row": "Hàng bảng (nguyên văn)", "caption": "Chú thích hình/bảng (nguyên văn)"}.get(
        s["kind"], "Câu gốc (chưa phân tích)")
    flags = sentences.read_time_flags(s)
    return {
        "kind": "sentence", "id": s["sentence_id"], "paper_id": s["paper_id"], "unit_type": None,
        "label": label, "statement": None, "text": s["text"], "sentence_ids": [s["sentence_id"]],
        "pages": [s["page"]], "sections": [s["section"]], "subsection": s.get("subsection"),
        "sentence_kind": s["kind"], "review_paper": effective_evidence_type(s) in REVIEW_TYPES,
        "display_text": sentences.display_text(s), "flags": flags,
        "source_glyph_suspect": flags.get("source_glyph_suspect", False),
        "table_text_suspect": flags.get("table_text_suspect", False),
        "uncertainty": __import__('kbsys.structure', fromlist=['uncertainty']).uncertainty(s['text']),
    }


def _weight(hit):
    w = UNIT_WEIGHT.get(hit["unit_type"], 0.85) if hit["kind"] == "unit" else 0.85
    w *= max(SECTION_WEIGHT.get(s, 0.85) for s in hit["sections"] or [""])
    if hit["kind"] == "sentence":
        w *= KIND_WEIGHT.get(hit.get("sentence_kind"), 1.0)
    if hit["review_paper"]:
        w *= 0.8
    if hit.get("status") == "flagged":
        w *= 0.9
    return w


def _keep(hit, filters):
    types = filters.get("unit_types") or []
    if types:
        if hit["kind"] == "sentence" and "sentence" not in types:
            return False
        if hit["kind"] == "unit" and hit["unit_type"] not in types:
            return False
    sections = filters.get("sections") or []
    if sections and not set(sections) & set(hit["sections"] or []):
        return False
    species = (filters.get("species") or "").strip().lower()
    if species:
        if hit["kind"] != "unit" or species not in (hit.get("species") or "").lower():
            return False
    return True


def _exact_result_hits(con, cfg, query, vault, all_vaults, filters):
    """Best few exact-term sentences per paper in scope: two from Results, one from Discussion.

    FTS is not used here: its candidate cap is why a plain Results sentence (no evidence
    unit) could vanish before answer-item selection.  The scan is limited to the Results /
    Discussion sentences of the current vault and paper filters, and runs only for typed
    identity terms.  search() inserts the ones its page left out.
    """
    terms = _exact_query_terms(query, cfg)
    if not terms:
        return []
    patterns = []
    for term in terms:
        variants = sentences.glyph_variants(term)
        patterns.append((term, re.compile("|".join(
            r"(?<!\w)" + r"\W+".join(re.escape(part) for part in variant.split()) + r"(?!\w)"
            for variant in variants), re.I)))
    args = []
    sql = ("""SELECT s.sentence_id, s.paper_id, s.seq, s.page, s.section, s.subsection, s.kind, s.text,
                     pa.evidence_type AS paper_evidence_type, pa.publication_types
              FROM sentences s JOIN papers pa ON pa.paper_id = s.paper_id
              WHERE s.section IN ('results', 'results_discussion', 'discussion')""")
    sql += _vault_clause("s", vault, all_vaults, cfg, args)
    sql += " " + _paper_filters(filters, args)
    sql += " ORDER BY s.paper_id, s.seq"

    rows = []
    for row in con.execute(sql, args):
        sentence = dict(row)
        matched = [term for term, pattern in patterns
                   if pattern.search(sentences.fold_search_glyphs(sentence["text"] or ""))]
        if matched and _keep(_sentence_hit(sentence), filters):
            rows.append((matched, sentence))
    # A term found in Results of most papers (DNA, ROS) identifies nothing: drop it, or
    # every paper would be pushed into the answer.
    scope = papers_in_scope(con, cfg, vault, all_vaults)
    papers_of = {}
    for matched, sentence in rows:
        for term in matched:
            papers_of.setdefault(term, set()).add(sentence["paper_id"])
    common = {t for t, ps in papers_of.items() if scope >= 5 and len(ps) > 0.4 * scope}
    per_paper = {}
    for matched, sentence in rows:
        matched = [t for t in matched if t not in common]
        if matched:
            # Prefer more of the typed identity terms, ordinary text over a
            # caption/table row, then source order.
            rank = (len(matched), int(sentence["kind"] == "text"), -sentence["seq"])
            per_paper.setdefault(sentence["paper_id"], []).append((rank, sentence))
    if not per_paper:
        return []
    # A paper's single best sentence can be a method line that merely names the
    # marker (its finding is the next sentence), so keep the best two Results
    # sentences, then the best Discussion sentence (the authors' own reading, which
    # may be where a "not affected" is stated), per paper.
    # ponytail: fixed counts; raise EXACT_PER_PAPER if a paper with many mentions still hides its finding.
    candidates = {}
    for pid, rows in per_paper.items():
        rows = [s for _, s in sorted(rows, key=lambda r: r[0], reverse=True)]
        found = [s for s in rows if s["section"] != "discussion"][:EXACT_PER_PAPER]
        candidates[pid] = found + [s for s in rows if s["section"] == "discussion"][:1]

    all_units = _unit_rows(con, candidates.keys())
    unit_of = {}
    for unit in all_units.values():
        for sid in json.loads(unit["sentence_ids"] or "[]"):
            unit_of.setdefault(sid, []).append(unit)

    def priority(item):
        # A unit-less Results sentence is the most direct conflict-preserving source
        # item: keep it ahead of cited/background units from other papers.  Every
        # paper's best sentence comes before any paper's second one, so a small
        # answer cap cannot be filled by one paper.
        depth, hit = item
        if hit["kind"] == "sentence" or hit.get("status") not in (None, "ok", "human_confirmed"):
            tier = 0
        elif hit["unit_type"] in ("cited_statement", "background"):
            tier = 2
        else:
            tier = 1
        return depth > 0, tier, hit["paper_id"], hit["id"]

    out = []
    for paper_id in sorted(candidates):
        for depth, sentence in enumerate(candidates[paper_id]):
            # Only an accepted unit stands in for its sentence.  An unaccepted (draft) unit
            # would be expanded by answer.source_items into ALL its sentences, using up the
            # answer cap; the matched sentence itself is what the researcher asked for.
            unit = owner_unit([u for u in unit_of.get(sentence["sentence_id"], []) if _keep(_unit_hit(u), filters)])
            if unit:
                out.append((depth, _unit_hit(unit)))
            else:
                out.append((depth, _sentence_hit(sentence)))
    hits, seen = [], set()
    for _, hit in sorted(out, key=priority):
        if hit["id"] not in seen:  # two sentences of one unit give the unit once
            seen.add(hit["id"])
            hits.append(hit)
    return hits


def _allowed_papers(con, cfg, vault, all_vaults, filters):
    args = []
    sql = "SELECT pa.paper_id FROM papers pa WHERE 1=1"
    sql += _vault_clause("pa", vault, all_vaults, cfg, args)
    sql += " " + _paper_filters(filters, args)
    return [r[0] for r in con.execute(sql, args)]


def _semantic(con, cfg, query, vault, all_vaults, filters, patterns, k=60, passages=6, spread=0.06):
    """Hits by meaning (vectors.py): units directly, passages as their best sentence.

    The best sentence of a passage is the one matching most query words; only when none matches
    (e.g. a Vietnamese question) are the sentences embedded, which is slower."""
    found, qvec = vectors.search(query, _allowed_papers(con, cfg, vault, all_vaults, filters), k=k)
    if not found:
        return []
    best = found[0][3]
    found = [f for f in found if f[3] >= best - spread]
    unit_ids = [f[0] for f in found if f[1] == "unit"]
    passage_ids = [f[0] for f in found if f[1] == "passage"][:passages]
    cos = {f[0]: f[3] for f in found}
    all_units = _unit_rows(con, {f[2] for f in found})
    unit_of = {}
    for u in all_units.values():
        for sid in json.loads(u["sentence_ids"] or "[]"):
            unit_of.setdefault(sid, []).append(u["unit_id"])
    hits = {}
    for uid in unit_ids:
        if uid in all_units:
            h = _unit_hit(all_units[uid])
            h["cos"] = cos[uid]
            hits[uid] = h
    if passage_ids:
        rows = [dict(r) for r in con.execute(
            """SELECT s.sentence_id, s.passage_id, s.paper_id, s.seq, s.page, s.section, s.subsection, s.kind, s.text,
                      pa.evidence_type AS paper_evidence_type, pa.publication_types
               FROM sentences s JOIN papers pa ON pa.paper_id = s.paper_id
               WHERE s.passage_id IN (%s) AND s.kind != 'table_row'""" % ",".join("?" * len(passage_ids)),
            passage_ids)]
        by_id = {r["sentence_id"]: r for r in rows}
        picked, unmatched = {}, []
        by_passage = {}
        for r in rows:
            by_passage.setdefault(r["passage_id"], []).append(r)
        for pid, sents in by_passage.items():
            scored = [(sum(1 for pat in patterns if pat.search(sentences.fold_search_glyphs(r["text"]))), r) for r in sents]
            top = max(scored, key=lambda x: x[0]) if scored else (0, None)
            if top[0]:
                picked[pid] = [(top[1]["sentence_id"], cos[pid])]
            else:
                unmatched += sents
        picked.update(vectors.best_sentences(qvec, unmatched))
        for pid, picks in picked.items():
            for sid, sim in picks:
                c = (cos[pid] + sim) / 2
                for uid in unit_of.get(sid, []):
                    if uid not in hits or hits[uid]["cos"] < c:
                        h = hits.get(uid) or _unit_hit(all_units[uid])
                        h["cos"] = c
                        hits[uid] = h
                if sid not in unit_of:
                    h = _sentence_hit(by_id[sid])
                    h["cos"] = c
                    hits[sid] = h
    out = [h for h in hits.values() if _keep(h, filters)]
    out.sort(key=lambda h: -(h["cos"] + 0.005 * (_weight(h) - 1)))
    return out


def search(con, cfg, query, vault=None, all_vaults=False, filters=None, limit=20, offset=0, cap=400,
           semantic=True, depth='fast'):
    """Ranked hits for a query. Returns a dict (see CONTRACTS §4.1).

    Words (FTS5 + synonyms + MeSH) and meaning (vectors) each give a ranked list; the lists are
    joined by reciprocal-rank fusion. A Vietnamese question without translation relies on meaning."""
    filters = filters or {}
    if vault and not all_vaults:
        config.require_research_vault(vault)   # RT-06: a typo or '../x' raises, never an empty answer
    cap = max(cap, 800) if depth == 'deep' else cap
    groups, expansions = parse_detail(query, cfg)
    scope = "all vaults" if all_vaults else (vault or config.default_vault(cfg))
    use_meaning = semantic and vectors.index_ready()
    vietnamese = looks_vietnamese(query)
    out = {"query": query, "groups": groups, "expansions": expansions, "highlight": highlight_terms(groups),
           "mode": "and", "semantic": use_meaning, "scope": scope, "hits": [], "total": 0, "facets": {},
           "papers": {}, "papers_scanned": papers_in_scope(con, cfg, vault, all_vaults),
           "needs_translation": vietnamese and not use_meaning, "not_found": False, "best_groups_matched": 0,
           # Non-PDF sources are deliberately a separate group.  Keeping them
           # out of ``hits`` prevents answer.py from treating personal notes or
           # transcripts as published paper evidence.
           "source_hits": [], "source_total": 0}
    if not groups:   # only punctuation, operators or stop words: nothing to look for (meaning search would be noise)
        return out

    source_scope = None if all_vaults else scope
    try:
        source_rows = sources_misc.search_sources(
            con, query, vault=source_scope, limit=max(1, limit + offset))
        out["source_total"] = len(source_rows)
        out["source_hits"] = source_rows[offset:offset + limit]
    except Exception as error:  # a stale optional source index must not hide paper search
        out["source_error"] = f"source search unavailable ({type(error).__name__})"

    lexical = []
    if groups:
        def run(mode):
            match = fts_expression(groups, mode, db.fts_glyph_tokens(con))
            return (_units(con, cfg, match, vault, all_vaults, filters, cap),
                    _sentences(con, cfg, match, vault, all_vaults, filters, cap))

        units, sents = run("and")
        if len(groups) > 1 and len(units) + len(sents) < 20:
            every_group = units, sents   # RT-02: these match every group; the cap of the OR run must not drop them
            units, sents = run("or")
            units += [u for u in every_group[0] if u["unit_id"] not in {x["unit_id"] for x in units}]
            sents += [s for s in every_group[1] if s["sentence_id"] not in {x["sentence_id"] for x in sents}]
            out["mode"] = "or"

        # a sentence inside an evidence unit becomes that unit
        all_units = _unit_rows(con, {s["paper_id"] for s in sents} | {u["paper_id"] for u in units})
        unit_of = {}
        for u in all_units.values():
            for sid in json.loads(u["sentence_ids"] or "[]"):
                unit_of.setdefault(sid, []).append(u["unit_id"])

        hits = {}
        for u in units:
            h = _unit_hit(u)
            h["bm25"] = -u["bm25"]
            hits[h["id"]] = h
        for s in sents:
            owners = unit_of.get(s["sentence_id"])
            if owners:
                for uid in owners:
                    if uid in hits:
                        hits[uid]["bm25"] = max(hits[uid]["bm25"], -s["bm25"]) * 1.1
                        hits[uid].setdefault("matched_sentences", []).append(s["sentence_id"])
                    else:
                        h = _unit_hit(all_units[uid])
                        h["bm25"] = -s["bm25"]
                        h["matched_sentences"] = [s["sentence_id"]]
                        hits[uid] = h
            else:
                h = _sentence_hit(s)
                h["bm25"] = -s["bm25"]
                hits[h["id"]] = h

        patterns = _group_patterns(groups)
        for h in hits.values():
            if not _keep(h, filters):
                continue
            haystack = sentences.fold_search_glyphs(" ".join(x for x in (h.get("statement"), h["text"]) if x))
            h["matched_terms"] = sum(1 for p in patterns if p.search(haystack))
            # OR mode: the group count ranks first (below), so it is not also a multiplier (RT-02)
            coverage = h["matched_terms"] / len(patterns) if patterns else 1
            h["lexical"] = round(h.pop("bm25") * _weight(h) * (1 if out["mode"] == "or" else 0.4 + coverage), 4)
            lexical.append(h)
        lexical.sort(key=lambda h: (-h["matched_terms"] if out["mode"] == "or" else 0, -h["lexical"]))

    meaning = []
    if use_meaning:
        try:
            meaning = _semantic(con, cfg, query, vault, all_vaults, filters, _group_patterns(groups))
        except Exception as e:  # model/library/index broken or missing: keyword search still works
            out["semantic"] = False
            out["semantic_error"] = f"Tìm theo nghĩa tạm không dùng được ({type(e).__name__}); chỉ tìm theo từ khóa."
            out["needs_translation"] = vietnamese

    # A typed identity term must preserve Results coverage independently of
    # FTS/vector candidate caps: see "coverage" below.
    exact_hits = _exact_result_hits(con, cfg, query, vault, all_vaults, filters)

    # reciprocal-rank fusion; words count less for a Vietnamese question (few English words to match)
    # and when not all words could be found together
    k, w_words = 60, (0.3 if vietnamese else 0.7 if out["mode"] == "or" else 1.0)
    merged = {}
    for rank, h in enumerate(lexical, start=1):
        h["via"] = ["từ khóa"]
        h["score"] = w_words / (k + rank)
        merged[h["id"]] = h
    for rank, h in enumerate(meaning, start=1):
        if h["id"] in merged:
            m = merged[h["id"]]
            m["score"] += 1 / (k + rank)
            m["via"].append("nghĩa")
            m["cos"] = round(h["cos"], 3)
        else:
            h["via"] = ["nghĩa"]
            h["score"] = 1 / (k + rank)
            h["cos"] = round(h["cos"], 3)
            merged[h["id"]] = h


    patterns = _group_patterns(groups) if groups else []
    ranked = sorted(merged.values(), key=lambda h: -h["score"])
    for h in ranked:
        h["score"] = round(h["score"] * 1000, 2)

    # Coverage: an exact-term Results sentence (see _exact_result_hits) that the ranking left
    # out of the first `limit` hits is inserted after the first few, so a paper that reports a different
    # result cannot be hidden by the candidate cap or the unit weights.  The very best hits keep
    # their places and at most a third of the head is replaced, so a query with many acronyms
    # cannot push out the best-ranked hits.  (Inserted high rather than last because the answer
    # step expands an unaccepted unit into all its sentences before cutting to 12/45 items.)
    # Their scores sit between the neighbours: callers re-sort by score.  The insertion changes the
    # whole ordering once (RT-08), then offset/limit slice it, so paging repeats and skips nothing
    # (for one `limit`: the head the coverage works on is the first `limit` hits).
    head = ranked[:limit]
    if exact_hits and len(head) == limit > max(3, limit // 4):   # a page of 1-3 has no "first few" to keep
        # an unaccepted (draft/flagged) unit is expanded and cut by the answer step, so only a
        # plain sentence or an accepted unit counts as covering its sentences
        covered = {sid for h in head if h["kind"] == "sentence" or h.get("status") in ("ok", "human_confirmed")
                   for sid in h["sentence_ids"]}
        extra = [h for h in exact_hits if not covered & set(h["sentence_ids"])][:max(5, limit // 3)]
        if extra:
            keep = max(3, limit // 4)
            gap = max(head[keep - 1]["score"] - head[keep]["score"], 1e-6)
            for i, h in enumerate(extra):
                known = merged.get(h["id"])
                if known:
                    h = extra[i] = known
                else:
                    h["via"] = ["từ khóa"]
                h["score"] = head[keep - 1]["score"] - gap * (i + 1) / (len(extra) + 1)
            moved = {h["id"] for h in extra}
            rest = [h for h in ranked if h["id"] not in moved]   # a known hit leaves its old place
            ranked = rest[:keep] + extra + rest[keep:]
    for h in ranked:
        haystack = sentences.fold_search_glyphs(" ".join(x for x in (h.get("statement"), h["text"]) if x))
        h["matched_terms"] = sum(1 for p in patterns if p.search(haystack))

    # RT-05: the best item of the whole result (not of the page) must cover enough keyword groups; the hits stay
    # in the result either way.  A Vietnamese query has no English groups to match: needs_translation says so.
    out["best_groups_matched"] = max((h["matched_terms"] for h in ranked), default=0)
    out["not_found"] = not vietnamese and out["best_groups_matched"] / len(groups) < standard.param("RT-05", "min_group_coverage")
    if out["not_found"]:
        where = "trong các vault đã tìm" if all_vaults else "trong vault hiện tại"
        out["not_found_message"] = (f"Không tìm thấy {where} (not found in the {'searched vaults' if all_vaults else 'current vault'}): "
                                    f"mục tốt nhất chỉ khớp {out['best_groups_matched']}/{len(groups)} nhóm từ khóa.")
    out["total"] = len(ranked)
    out["facets"] = _facets(ranked)
    page = ranked[offset: offset + limit]
    out["hits"] = page
    out["papers"] = paper_cards(con, {h["paper_id"] for h in page})
    _attach_citations(con, page)
    out['depth'] = 'deep' if depth == 'deep' else 'fast'
    if depth == 'deep':
        for hit in page:
            hit['context'] = context(con, hit['sentence_ids'][0], 2) if hit.get('sentence_ids') else []
    return out


def _facets(hits):
    facets = {"type": {}, "section": {}, "species": {}, "paper": {}}
    for h in hits:
        key = h["unit_type"] or "sentence"
        facets["type"][key] = facets["type"].get(key, 0) + 1
        for s in set(h["sections"] or []):
            facets["section"][s] = facets["section"].get(s, 0) + 1
        if h.get("species"):
            facets["species"][h["species"]] = facets["species"].get(h["species"], 0) + 1
        facets["paper"][h["paper_id"]] = facets["paper"].get(h["paper_id"], 0) + 1
    return facets


def papers_in_scope(con, cfg, vault=None, all_vaults=False):
    if all_vaults:
        return con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    return con.execute("SELECT COUNT(*) FROM paper_vaults WHERE vault = ?",
                       (vault or config.default_vault(cfg),)).fetchone()[0]


def paper_cards(con, paper_ids):
    if not paper_ids:
        return {}
    rows = con.execute("""SELECT paper_id, title, first_author, year, journal, doi, pmid, evidence_type,
                                 classification, analysis_status, note_name, publication_types
                          FROM papers WHERE paper_id IN (%s)""" % ",".join("?" * len(paper_ids)),
                       list(paper_ids))
    cards = {}
    for r in rows:
        c = dict(r)
        c['evidence_type'] = effective_evidence_type(c)
        c["cite"] = f"{c['first_author'] or 'Unknown'} {c['year'] or 'n.d.'}"
        c["has_pdf"] = main_pdf_path(con, c["paper_id"]) is not None
        cards[c["paper_id"]] = c
    return cards


def _attach_citations(con, hits):
    """For each hit, the reference entries its sentences cite (so the page can show [n] at once)."""
    sids = sorted({sid for h in hits for sid in h["sentence_ids"]})
    if not sids:
        return
    refs = {}
    for i in range(0, len(sids), 500):
        chunk = sids[i:i + 500]
        for r in con.execute("""SELECT c.sentence_id, r.ref_id, r.num, r.first_author, r.year, r.title,
                                       r.resolved_paper_id
                                FROM citations c JOIN references_list r ON r.ref_id = c.ref_id
                                WHERE c.sentence_id IN (%s)""" % ",".join("?" * len(chunk)), chunk):
            refs.setdefault(r["sentence_id"], []).append(dict(r))
    for h in hits:
        seen, cited = set(), []
        for sid in h["sentence_ids"]:
            for r in refs.get(sid, []):
                if r["ref_id"] not in seen:
                    seen.add(r["ref_id"])
                    cited.append({k: r[k] for k in ("ref_id", "num", "first_author", "year", "title",
                                                    "resolved_paper_id")})
        h["cited_refs"] = cited


# ---------------------------------------------------------------- reading helpers

def main_pdf_path(con, paper_id):
    row = con.execute("""SELECT path FROM source_files WHERE paper_id = ?
                         AND status IN ('active', 'archived', 'external')
                         ORDER BY role = 'main' DESC,
                                  CASE status WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END
                         LIMIT 1""", (paper_id,)).fetchone()
    if row and row["path"] and (config.ROOT / row["path"]).is_file():
        return (config.ROOT / row["path"]).resolve()
    return None


def context(con, sentence_id, window=3):
    """The sentence and `window` sentences before and after it in the same paper (no references)."""
    row = con.execute("SELECT paper_id, seq FROM sentences WHERE sentence_id = ?", (sentence_id,)).fetchone()
    if not row:
        return []
    return [dict(r) for r in con.execute(
        """SELECT sentence_id, seq, page, section, kind, text FROM sentences
           WHERE paper_id = ? AND seq BETWEEN ? AND ? AND section != 'references' ORDER BY seq""",
        (row["paper_id"], row["seq"] - window, row["seq"] + window))]


def reference(con, ref_id):
    row = con.execute("SELECT * FROM references_list WHERE ref_id = ?", (ref_id,)).fetchone()
    if not row:
        return None
    ref = dict(row)
    if ref.get("resolved_paper_id"):
        ref["resolved"] = paper_cards(con, {ref["resolved_paper_id"]}).get(ref["resolved_paper_id"])
    return ref


def paper_view(con, paper_id):
    """Everything the reading page needs for one paper, read from its files (one paper at a time)."""
    if not PAPER_ID.fullmatch(paper_id or "") or ".." in paper_id:
        return None
    folder = paper_dir(paper_id)
    if not (folder / "metadata.json").is_file():
        return None
    meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
    card = paper_cards(con, {paper_id}).get(paper_id) or {}
    units = list(read_jsonl(folder / "evidence.jsonl"))
    unit_of = {}
    for u in units:
        for sid in u.get("sentence_ids") or []:
            unit_of.setdefault(sid, []).append(u["unit_id"])
    refs = list(read_jsonl(folder / "references.jsonl"))
    sentence_rows = sentences.cached_linked_rows(read_jsonl(folder / "sentences.jsonl"), folder / "sentences.jsonl")
    sentence_views = []
    for s in sentence_rows:
        flags = sentences.read_time_flags(s)
        sentence_views.append({
            "id": s["sentence_id"], "seq": s["seq"], "page": s["page"], "section": s["section"],
            "passage_id": s.get("passage_id"), "subsection": s.get("subsection"), "kind": s["kind"],
            "text": s["text"], "display_text": sentences.display_text(s),
            "object_id": s.get("object_id"), "units": unit_of.get(s["sentence_id"], []),
            "flags": flags, "source_glyph_suspect": flags.get("source_glyph_suspect", False),
            "table_text_suspect": flags.get("table_text_suspect", False),
            "continues": s.get("continues"),
            "citations": [{"start": c["start"], "end": c["end"], "marker": c["marker"],
                           "ref_nums": c.get("ref_nums") or [], "confidence": c.get("confidence")}
                          for c in s.get("citations") or [] if c.get("start") is not None],
        })
    tables = list(read_jsonl(folder / "tables.jsonl"))
    figures = list(read_jsonl(folder / "figures.jsonl"))
    return {
        "paper": {**card, "authors": [a.get("name") for a in meta.get("authors") or []],
                  "abstract": meta.get("abstract"), "summary": meta.get("summary"),
                  "publication_types": meta.get("publication_types") or []},
        "sentences": sentence_views,
        "units": [{k: u.get(k) for k in ("unit_id", "seq", "unit_type", "statement", "quote", "sentence_ids",
                                         "pages", "sections", "status", "flags", "hedging", "cited_refs",
                                         "model", "intervention", "outcome")}
                  | {"label": UNIT_LABELS.get(u.get("unit_type"), u.get("unit_type"))} for u in units],
        "references": [{k: r.get(k) for k in ("ref_id", "num", "raw", "first_author", "year", "title", "doi",
                                              "pmid", "resolved_paper_id")} for r in refs],
        "tables": [{k: t.get(k) for k in ("table_id", "label", "page", "header", "rows", "footnotes", "crop",
                                          "caption_sentence_ids", "source", "quality")} for t in tables],
        "figures": [{k: f.get(k) for k in ("figure_id", "label", "page", "crop", "caption_sentence_ids",
                                           "source")} for f in figures],
    }
