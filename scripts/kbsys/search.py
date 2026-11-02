"""Full-text search over passages, with citation (paper, section, page)."""

import html
import json
import os
import re
import webbrowser
from pathlib import Path

from . import config, db, sources_misc, sentences
from .notes import section_label

_TERM = re.compile(r'"([^"]+)"|(\S+)')


def _synonym_groups(cfg):
    groups = {}
    for group in [*(cfg.get("synonyms") or []), *(cfg.get("word_variants") or [])]:
        terms = [str(t) for t in group if str(t).strip()]
        for t in terms:
            groups[t.lower()] = terms
    return groups


def parse_query(query, cfg, glyph_tokens=False):
    """User text -> (FTS5 MATCH expression, list of highlight terms).

    Words are combined with AND; "quoted text" is an exact phrase.
    A term that belongs to a synonym group matches any term of the group.
    """
    synonyms = _synonym_groups(cfg)
    parts, highlight = [], []
    for phrase, word in _TERM.findall(query):
        term = (phrase or word).strip()
        term = re.sub(r'["*^():]', " ", term).strip()
        if not term or term.upper() in ("AND", "OR", "NOT"):
            continue
        values = synonyms.get(term.lower(), [term])
        highlight += [variant for value in values for variant in sentences.glyph_variants(value)]
        exprs = ['"' + v.replace('"', " ") + '"' for value in values for v in sentences.fts_variants(value, glyph_tokens)]
        parts.append(exprs[0] if len(exprs) == 1 else "(" + " OR ".join(exprs) + ")")
    return " AND ".join(parts), highlight


def search(con, cfg, query, vault=None, all_vaults=False, section=None, year_from=None,
           year_to=None, paper_id=None, include_references=None, limit=None):
    match, highlight = parse_query(query, cfg, db.fts_glyph_tokens(con))
    if not match:
        return [], highlight
    if include_references is None:
        include_references = cfg["search"].get("include_references_by_default", False)
    limit = limit or cfg["search"].get("results_per_query", 30)
    sql = ["""SELECT p.passage_id, p.paper_id, p.seq, p.page, p.section, p.kind, p.text,
                     snippet(passages_fts, 0, '«', '»', ' … ', 32) AS snippet,
                     bm25(passages_fts) AS score,
                     pa.title, pa.first_author, pa.year, pa.journal, pa.doi, pa.pmid,
                     pa.note_name, pa.publication_types, pa.classification
              FROM passages_fts
              JOIN passages p ON p.rowid = passages_fts.rowid
              JOIN papers pa ON pa.paper_id = p.paper_id
              WHERE passages_fts MATCH ?"""]
    args = [match]
    if not all_vaults:
        sql.append("AND p.paper_id IN (SELECT paper_id FROM paper_vaults WHERE vault = ?)")
        args.append(vault or config.default_vault(cfg))
    if section:
        sql.append("AND p.section = ?")
        args.append(section)
    elif not include_references:
        sql.append("AND p.section != 'references'")
    if year_from:
        sql.append("AND pa.year >= ?")
        args.append(year_from)
    if year_to:
        sql.append("AND pa.year <= ?")
        args.append(year_to)
    if paper_id:
        sql.append("AND p.paper_id = ?")
        args.append(paper_id)
    sql.append("ORDER BY score LIMIT ?")
    args.append(limit)
    rows = [dict(r) for r in con.execute("\n".join(sql), args).fetchall()]
    for r in rows:
        r["publication_types"] = json.loads(r["publication_types"] or "[]")
        r["pdf_path"] = main_pdf(con, r["paper_id"])
    source_rows = sources_misc.search_sources(
        con, query, vault=None if all_vaults else (vault or config.default_vault(cfg)),
        limit=limit)
    for source in source_rows:
        # Keep this legacy result shape safe for the desktop caller while
        # marking it clearly enough for renderers and downstream consumers.
        rows.append({
            "source_hit": True,
            "kind": source["kind"],
            "source_id": source["source_id"],
            "sentence_id": source["sentence_id"],
            "title": source["title"],
            "text": source["text"],
            "snippet": source["text"],
            "section": source["locator_label"],
            "page": None,
            "passage_id": source["sentence_id"],
            "first_author": "",
            "year": None,
            "journal": "",
            "doi": None,
            "pmid": None,
            "publication_types": [],
            "classification": None,
            "pdf_path": None,
            "locator": source["locator"],
            "locator_label": source["locator_label"],
            "evidence_class": source["evidence_class"],
            "published_evidence": False,
            "not_published_evidence": source["not_published_evidence"],
            "label": source["label"],
        })
    return rows, highlight


def main_pdf(con, paper_id):
    row = con.execute("""SELECT path FROM source_files WHERE paper_id = ?
                         AND status IN ('active', 'archived', 'external')
                         ORDER BY role = 'main' DESC,
                                  CASE status WHEN 'active' THEN 0 WHEN 'archived' THEN 1 ELSE 2 END
                         LIMIT 1""", (paper_id,)).fetchone()
    if row and row["path"] and (config.ROOT / row["path"]).is_file():
        return str((config.ROOT / row["path"]).resolve())
    return None


def context(con, passage_id, window=2):
    """The passage plus `window` passages before and after it (same paper)."""
    row = con.execute("SELECT paper_id, seq FROM passages WHERE passage_id = ?", (passage_id,)).fetchone()
    if not row:
        return []
    return [dict(r) for r in con.execute(
        """SELECT passage_id, seq, page, section, kind, text FROM passages
           WHERE paper_id = ? AND seq BETWEEN ? AND ? ORDER BY seq""",
        (row["paper_id"], row["seq"] - window, row["seq"] + window)).fetchall()]


def cite(r):
    author = r.get("first_author") or "Unknown"
    return f"{author} {r.get('year') or 'n.d.'} — {r.get('title') or r['paper_id']}"


def print_results(rows, query):
    if not rows:
        print(f'Không tìm thấy "{query}" trong kho kiến thức hiện tại.')
        return
    print(f'{len(rows)} kết quả cho "{query}":\n')
    for i, r in enumerate(rows, start=1):
        if r.get("source_hit"):
            print(f"{i:>2}. [{r['kind']}] {r['title']}")
            print(f"    {r['not_published_evidence']} · {r['section']} · {r['sentence_id']}")
            print(f"    {r['text']}\n")
            continue
        print(f"{i:>2}. {cite(r)}")
        print(f"    {section_label(r['section'])}, trang {r['page']} · {r['passage_id']}")
        print(f"    {r['snippet']}\n")


def _highlight(text, terms):
    out = html.escape(text)
    for t in sorted(set(terms), key=len, reverse=True):
        words = [re.escape(html.escape(w)) for w in t.split()]
        if words:
            # match the word and its common endings (the search uses stemming)
            pattern = r"\s+".join(words[:-1] + [words[-1] + r"\w*"])
            out = re.sub(rf"(?i)\b({pattern})", r"<mark>\1</mark>", out)
    return out


def write_html(rows, query, highlight, path=None):
    path = path or (config.CACHE_DIR / "search_results.html")
    path.parent.mkdir(parents=True, exist_ok=True)
    items = []
    for i, r in enumerate(rows, start=1):
        if r.get("source_hit"):
            items.append(f"""
<article>
  <div class="n">{i}</div>
  <div>
    <h2>[{html.escape(r['kind'])}] {html.escape(r['title'])}</h2>
    <div class="meta">{html.escape(r['not_published_evidence'])} · {html.escape(r['section'])} · <code>{html.escape(r['sentence_id'])}</code></div>
    <blockquote>{_highlight(r['text'], highlight)}</blockquote>
  </div>
</article>""")
            continue
        pdf_link = ""
        if r.get("pdf_path"):
            uri = Path(r["pdf_path"]).as_uri()
            pdf_link = f'<a href="{uri}#page={r["page"]}">Mở PDF ở trang {r["page"]}</a>'
        ids = []
        if r.get("doi"):
            ids.append(f'<a href="https://doi.org/{html.escape(r["doi"])}">DOI</a>')
        if r.get("pmid"):
            ids.append(f'<a href="https://pubmed.ncbi.nlm.nih.gov/{r["pmid"]}/">PubMed</a>')
        review = any("review" in t.lower() for t in r["publication_types"])
        badge = '<span class="badge">Review article</span>' if review else ""
        caption = '<span class="badge">Figure/table caption</span>' if r["kind"] == "caption" else ""
        items.append(f"""
<article>
  <div class="n">{i}</div>
  <div>
    <h2>{html.escape(cite(r))} {badge}</h2>
    <div class="meta">{html.escape(r.get('journal') or '')} · <b>{section_label(r['section'])}</b>, page {r['page']} {caption}
      · <code>{r['passage_id']}</code></div>
    <blockquote>{_highlight(r['text'], highlight)}</blockquote>
    <div class="links">{pdf_link} {' · '.join(ids)}</div>
  </div>
</article>""")
    body = "\n".join(items) if items else "<p class='empty'>Không tìm thấy trong kho kiến thức hiện tại.</p>"
    page = f"""<!doctype html><html lang="vi"><head><meta charset="utf-8">
<title>Tìm kiếm: {html.escape(query)}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root {{ --bg:#fbfaf7; --fg:#1f2328; --muted:#626a73; --line:#e3e0d8; --accent:#2f6f5e; --mark:#fde68a; --card:#fff; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#16181b; --fg:#e6e6e6; --muted:#9aa3ad; --line:#2c3035; --accent:#7cc4ad; --mark:#6b5a12; --card:#1d2024; }} }}
body {{ background:var(--bg); color:var(--fg); font:16px/1.6 system-ui, "Segoe UI", sans-serif; margin:0; padding:24px 16px; }}
main {{ max-width:900px; margin:auto; }}
header p {{ color:var(--muted); margin-top:-8px; }}
article {{ display:flex; gap:14px; background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin:12px 0; }}
.n {{ color:var(--muted); font-weight:600; min-width:1.6em; }}
h2 {{ font-size:1rem; margin:0 0 4px; }}
.meta {{ color:var(--muted); font-size:.9rem; }}
blockquote {{ margin:10px 0; padding:8px 12px; border-left:3px solid var(--accent); font-family:Georgia, serif; }}
mark {{ background:var(--mark); color:inherit; padding:0 2px; border-radius:2px; }}
a {{ color:var(--accent); }}
.links {{ font-size:.9rem; }}
.badge {{ font-size:.75rem; border:1px solid var(--line); border-radius:999px; padding:1px 8px; color:var(--muted); font-weight:500; }}
code {{ font-size:.8rem; }}
</style></head><body><main>
<header><h1>“{html.escape(query)}”</h1>
<p>{len(rows)} passages found · exact text from the original papers · sorted by relevance</p></header>
{body}
</main></body></html>"""
    path.write_text(page, encoding="utf-8", newline="\n")
    return path


def open_file(path):
    try:
        os.startfile(path)  # Windows
    except AttributeError:
        webbrowser.open(path.as_uri())
