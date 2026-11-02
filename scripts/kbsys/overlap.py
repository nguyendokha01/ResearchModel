"""Lexical overlap search for pasted passages.

This is a wording-overlap aid, not a plagiarism verdict.  It deliberately uses
the stored sentence text and sentence FTS index, rather than embeddings or AI.
"""

from collections import defaultdict
import re
import unicodedata

from . import config
from .find import _vault_clause, paper_cards


LABEL = "lexical overlap"
_WORD = re.compile(r"[^\W_]+(?:[-'\u2010-\u2015\u2212\u2018\u2019][^\W_]+)*", re.UNICODE)
_DASHES = "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"
_QUOTES = "\u2018\u2019\u201a\u201b\u2032\u2035\u201c\u201d\u201e\u201f\u2033\u2036"
_TRANSLATION = str.maketrans({**{c: "-" for c in _DASHES},
                               **{c: "'" for c in _QUOTES[:5]},
                               **{c: '"' for c in _QUOTES[5:]}})


def normalize(text):
    """Return comparable text while retaining numbers, units, and Greek letters."""
    text = unicodedata.normalize("NFC", text or "").translate(_TRANSLATION).lower()
    return " ".join(text.split())


def _tokens(text, with_spans=False):
    """Tokenize after the same character normalization used for comparisons."""
    source = text or ""
    rows = [(normalize(m.group(0)), m.start(), m.end()) for m in _WORD.finditer(source)]
    return rows if with_spans else [row[0] for row in rows]


def _shingles(tokens, n):
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


def _query_profile(text, n):
    tokens = _tokens(text)
    size = min(n, len(tokens)) if tokens else 1
    return tokens, size, _shingles(tokens, size)


def _matched_spans(stored_text, query_tokens, query_shingles, n):
    """Return merged [start, end) spans in the original stored sentence."""
    token_rows = _tokens(stored_text, with_spans=True)
    tokens = [row[0] for row in token_rows]
    ranges = []
    for i in range(len(tokens) - n + 1):
        if tuple(tokens[i:i + n]) in query_shingles:
            ranges.append((token_rows[i][1], token_rows[i + n - 1][2]))
    if not ranges:
        return []
    merged = [list(ranges[0])]
    for start, end in ranges[1:]:
        if start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [{"start": start, "end": end} for start, end in merged]


def _longest_run(query_tokens, sentence_tokens):
    where = defaultdict(list)          # token -> its positions in the query (keeps a long paste fast)
    for j, token in enumerate(query_tokens):
        where[token].append(j)
    best, previous = 0, {}
    for token in sentence_tokens:
        current = {}
        for j in where.get(token, ()):
            current[j] = previous.get(j - 1, 0) + 1
            best = max(best, current[j])
        previous = current
    return best


def _score(query_tokens, query_shingles, sentence_text, n):
    sentence_tokens = _tokens(sentence_text)
    sentence_shingles = _shingles(sentence_tokens, n)
    shared = query_shingles & sentence_shingles
    containment = len(shared) / len(query_shingles) if query_shingles else 0.0
    union = query_shingles | sentence_shingles
    jaccard = len(shared) / len(union) if union else 0.0
    run = _longest_run(query_tokens, sentence_tokens)
    run_score = run / len(query_tokens) if query_tokens else 0.0
    score = 0.65 * containment + 0.25 * jaccard + 0.10 * run_score
    return score, shared


def _fts_tokens(tokens, maximum=10):
    """Choose informative tokens; longer tokens are a stable rarity proxy."""
    unique = {token for token in tokens if len(token) > 1}
    return sorted(unique, key=lambda token: (-len(token), token))[:maximum]


def find_overlaps(con, text, vault=None, limit=20, n=5):
    """Find stored sentences with wording overlap in the current vault.

    The FTS candidate scan is bounded. Rows are scored as the cursor streams;
    no sentence corpus is loaded into memory.
    """
    if not isinstance(text, str) or not text.strip():
        return {"label": LABEL, "hits": [], "papers": [], "query_shingle_count": 0}
    try:
        n = max(1, int(n))
        limit = min(max(0, int(limit)), 100)
    except (TypeError, ValueError):
        raise ValueError("n and limit must be integers")
    query_tokens, size, query_shingles = _query_profile(text, n)
    if not query_tokens or not query_shingles or not limit:
        return {"label": LABEL, "hits": [], "papers": [], "query_shingle_count": len(query_shingles)}

    cfg = config.load()
    args = []
    chosen = _fts_tokens(query_tokens)
    if not chosen:
        return {"label": LABEL, "hits": [], "papers": [],
                "query_shingle_count": len(query_shingles)}
    match = " OR ".join('"{}"'.format(token.replace('"', " ")) for token in chosen)
    sql = ("SELECT s.sentence_id, s.paper_id, s.section, s.page, s.text "
           "FROM sentences_fts JOIN sentences s ON s.rowid = sentences_fts.rowid "
           "WHERE sentences_fts MATCH ? AND s.section != 'references'")
    args.append(match)
    sql += _vault_clause("s", vault, False, cfg, args)
    sql += " ORDER BY bm25(sentences_fts) LIMIT ?"
    args.append(max(200, limit * 25))

    hits = []
    paper_shingles = defaultdict(set)
    for row in con.execute(sql, args):
        score, shared = _score(query_tokens, query_shingles, row[4], size)
        if not shared:                 # same words but no shared phrase: not an overlap
            continue
        hit = {"sentence_id": row[0], "paper_id": row[1], "section": row[2],
               "page": row[3], "score": round(score, 6),
               "matched_spans": _matched_spans(row[4], query_tokens, query_shingles, size),
               "text": row[4], "label": LABEL}
        hits.append(hit)
        paper_shingles[row[1]].update(shared)

    hits.sort(key=lambda hit: (-hit["score"], hit["paper_id"], hit["sentence_id"]))
    hits = hits[:limit]
    shown = {hit["paper_id"] for hit in hits}
    cards = paper_cards(con, shown)
    papers = [{"paper_id": paper_id, "cite": cards.get(paper_id, {}).get("cite"),
               "title": cards.get(paper_id, {}).get("title"), "year": cards.get(paper_id, {}).get("year"),
               "has_pdf": cards.get(paper_id, {}).get("has_pdf", False),
               "coverage": round(len(shingles) / len(query_shingles), 6),
               "sentence_ids": [hit["sentence_id"] for hit in hits if hit["paper_id"] == paper_id],
               "hit_count": sum(hit["paper_id"] == paper_id for hit in hits)}
              for paper_id, shingles in sorted(paper_shingles.items()) if paper_id in shown]
    papers.sort(key=lambda item: (-item["coverage"], item["paper_id"]))
    return {"label": LABEL, "hits": hits, "papers": papers,
            "query_shingle_count": len(query_shingles)}
