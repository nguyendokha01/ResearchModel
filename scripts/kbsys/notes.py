"""Human-facing Obsidian notes.

Rules that protect human writing:
- A paper note is created once. Later updates only replace the text between
  the kb:auto markers and the machine keys in the frontmatter.
- Index and stub notes are only (re)written if they still carry their
  kb marker, so a note a person wrote with the same name is never overwritten.
"""

import json
import re
from pathlib import Path

import yaml

from . import concepts, config, db, presentation
from .db import TERM_TYPES, paper_dir, read_json, read_jsonl

# Ponytail ceiling: keep machine-managed note bodies compact while retaining
# the complete evidence in knowledge_store and the dashboard.
PAPER_NOTE_MAX_BYTES = 120 * 1024
NODE_NOTE_MAX_BYTES = 60 * 1024
RESULTS_HEADING = "Results"  # node note section listing its result notes (RL-10 A6); the capper never drops it (VN-07)

AUTO_START = "<!-- kb:auto:start — this block is updated by the software; write your own notes outside it -->"
AUTO_END = "<!-- kb:auto:end -->"
INDEX_MARK = "<!-- kb:index — auto-generated list, recreated by the software -->"
STUB_MARK = "<!-- kb:stub — auto-created; you may edit or extend this note freely -->"

# Vault layout v2 (D3.44, docs/ARCHITECTURE.md section 5). Node file names never change,
# so [[Name]] links keep resolving when a note moves between these folders.
FOLDERS = ["Papers", "_Maps", "Concepts", "Methods", "Substances", "Organisms", "Results", "Claims",
           "Review Queue", "_Bases", "_Templates", "_Attachments"]
TERM_FOLDERS = {"topics": "_Maps", "concepts": "Concepts", "methods": "Methods",
                "species": "Organisms", "molecules": "Substances", "cell_types": "Organisms"}
NODE_FOLDERS = list(dict.fromkeys(TERM_FOLDERS.values()))  # unique, stable order
# Labels shown in paper notes (unchanged by the folder move, so paper notes do not churn).
TERM_LABELS = {"topics": "Topics", "concepts": "Concepts", "methods": "Methods",
               "species": "Species", "molecules": "Molecules", "cell_types": "Cell Types"}
# Folder -> name of its machine list note (kb:index). Mine/ is human-owned and
# deliberately absent: its catalogs are written under _Maps instead.
LIST_NOTES = {"Concepts": "Concepts", "Methods": "Methods", "Substances": "Substances",
              "Organisms": "Organisms", "Results": "Results", "Claims": "Claims"}
MINE_LISTS = {"Mine/Ideas": "Ideas index", "Mine/Questions": "Questions index",
              "Mine/Knowledge Gaps": "Knowledge Gaps index"}
MAPS = "_Maps"
# H2 names of the paper note that other notes link to ([[Paper#Heading]]); generators and links share these.
PAPER_RESULTS_HEADING = "Kết quả (Results)"
PAPER_FIGURES_HEADING = "Hình và bảng (Figures and tables)"
PAPER_STRUCTURE_HEADING = "Cấu trúc (Structure)"
MACHINE_KEYS = ["type", "paper_id", "title", "authors", "year", "journal", "doi", "pmid",
                "publication_types", "evidence_type", "classification", "analysis_status",
                "text_quality", "source_status", "also_in"]


_CONCEPT_QUOTE_ROW = re.compile(
    r"(?m)^> \[!quote\]- [^\n]*(?:\n>(?! \[!)[^\n]*)*(?:\n|\Z)")
_PAPER_QUOTE_ROW = re.compile(
    r"(?ms)^> \*\*(?:[^*\n]+ )?\[[^]\n]+\]\*\*.*?(?=^> \*\*(?:[^*\n]+ )?\[[^]\n]+\]\*\*|^> \[!|^## |^<!-- kb:auto:end -->|\Z)")


# the notice written by _overflow_notice, and the old Vietnamese one with a dashboard link that stored notes may still carry
_OVERFLOW_LINE = re.compile(r"(?m)^> (?:.*?\[mở (?:trên )?dashboard\]\([^\n)]*\)|(?:More content|\d+ more items?) not shown; the complete list is in the dashboard\.)\s*\n?")


def _overflow_notice(count=None):
    """Return one stable, human-readable pointer for omitted machine content (no link: VN-08 allows no local URL in a note)."""
    detail = "More content" if count is None else f"{count} more item{'s' * (count != 1)}"
    return f"> {detail} not shown; the complete list is in the dashboard."


def cap_auto_block(block, cap, row_kind):
    """Drop whole, least-prioritised quote rows until an auto block fits.

    Renderers append evidence in priority order. Removing complete rows from
    the end therefore preserves the first rows and never cuts a quote, ID, or
    markdown table row in half. Human text is outside this block and is not
    examined or rewritten here.
    """
    if not block or len(block.encode("utf-8")) <= cap:
        return block
    block = _OVERFLOW_LINE.sub("", block)  # one notice only, with the new count
    pattern = _CONCEPT_QUOTE_ROW if row_kind == "concept" else _PAPER_QUOTE_ROW
    matches = list(pattern.finditer(block))
    if not matches:
        return _cap_complete_blocks(block, cap)

    def without(removed):
        pieces, cursor = [], 0
        for index, match in enumerate(matches):
            if index in removed:
                pieces.append(block[cursor:match.start()])
                cursor = match.end()
        pieces.append(block[cursor:])
        return "".join(pieces)

    def with_notice(text, count):
        marker = "<!-- kb:auto:end -->"
        notice = _overflow_notice(count)
        position = text.rfind(marker)
        if position < 0:
            return text.rstrip() + "\n\n" + notice + "\n"
        return text[:position].rstrip() + "\n\n" + notice + "\n" + text[position:]

    def trimmed(count):  # drop the last `count` rows (lowest priority)
        return with_notice(without(set(range(len(matches) - count, len(matches)))), count)

    if len(trimmed(len(matches)).encode("utf-8")) > cap:
        return _cap_complete_blocks(trimmed(len(matches)), cap, len(matches))
    low, high = 1, len(matches)  # binary search: fewest dropped rows that fit (O(log n) rebuilds, not O(n))
    while low < high:
        mid = (low + high) // 2
        if len(trimmed(mid).encode("utf-8")) <= cap:
            high = mid
        else:
            low = mid + 1
    return trimmed(low)


_SOURCE_IDS_HEAD = f"> [!info]- {presentation.SOURCE_IDS_TITLE}"


def _cap_complete_blocks(block, cap, omitted=None):
    """Final byte guard: omit complete paragraphs/quotes/tables, never slice text.

    This guard is used only after the renderer's priority trimming. Structural
    markers survive; a quote paragraph (including every continuation line) is
    an indivisible item even when one source sentence exceeds the entire cap.
    """
    # A previous priority pass may already have inserted its overflow line;
    # replace it once with the final count rather than duplicating it.
    # (stored notes still carry the old Vietnamese notice with a dashboard link: remove both forms)
    block = _OVERFLOW_LINE.sub("", block)
    chunks = re.split(r"\n[ \t]*\n", block.rstrip())
    structural, optional = [], []
    for index, chunk in enumerate(chunks):
        # Split only standalone wrappers; quoted lines remain together.
        lines = chunk.splitlines(keepends=True)
        pieces, pending = [], []
        for line in lines:
            if line.strip() in (AUTO_START, AUTO_END, "<details>", "</details>") or line.startswith("<summary>"):
                if pending:
                    pieces.append((False, "".join(pending).rstrip()))
                    pending = []
                pieces.append((True, line.rstrip()))
            else:
                pending.append(line)
        if pending:
            pieces.append((False, "".join(pending).rstrip()))
        for fixed, piece in pieces:
            # the Source IDs callout is the note's traceability: body content is trimmed before it, never it
            fixed = fixed or piece.lstrip().startswith((_SOURCE_IDS_HEAD, f"## {RESULTS_HEADING}\n"))
            target = structural if fixed else optional
            target.append((index, len(structural) + len(optional), piece))
    selected = list(structural)

    def render(rows, count=None):
        notice = _overflow_notice(omitted if count is None else count)
        text = "\n\n".join(piece for _, _, piece in sorted(rows, key=lambda r: (r[0], r[1])))
        position = text.rfind(AUTO_END)
        if position >= 0:
            return text[:position].rstrip() + "\n\n" + notice + "\n" + text[position:] + "\n"
        return text.rstrip() + "\n\n" + notice + "\n"

    for row in optional:
        candidate = render(selected + [row])
        if len(candidate.encode("utf-8")) <= cap:
            selected.append(row)
    omitted_count = omitted if omitted is not None else len(optional) - sum(row in selected for row in optional)
    result = render(selected, omitted_count)
    selected_optional = [row for row in selected if row in optional]
    while len(result.encode("utf-8")) > cap and selected_optional:
        dropped = selected_optional.pop()
        selected.remove(dropped)
        omitted_count += 1
        result = render(selected, omitted_count)
    if len(result.encode("utf-8")) <= cap:
        return result
    # Pathologically many wrappers: retain the auto markers, navigation and the Source IDs callout only.
    ids = "".join(piece + "\n\n" for _, _, piece in structural if piece.lstrip().startswith(_SOURCE_IDS_HEAD))
    return AUTO_START + "\n" + _overflow_notice(omitted_count) + "\n\n" + ids + AUTO_END + "\n"


def details_to_callouts(block):
    """Turn HTML ``<details><summary>T</summary>`` folds into collapsed Obsidian callouts.

    Obsidian does not render Markdown inside HTML blocks, so folds written as
    ``<details>`` showed raw tags and literal ``##`` headings.  A fold becomes
    ``> [!note]- T``; nesting adds one ``>`` per level, so callouts already inside
    a fold (``> [!quote]-``) become nested callouts.
    """
    out, depth = [], 0
    lines = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    def summary_text(line):
        match = re.fullmatch(r"(?:\s*>\s*)*\s*<summary>(.*?)</summary>\s*", line,
                             re.IGNORECASE)
        return match.group(1).strip() if match else None

    i = 0
    while i < len(lines):
        line = lines[i]
        opening = re.fullmatch(
            r"(?P<prefix>(?:\s*>\s*)*)\s*<details>\s*(?:<summary>(?P<title>.*?)</summary>)?\s*",
            line, re.IGNORECASE)
        if opening:
            title = (opening.group("title") or "").strip() or "Details"
            # Legacy notes sometimes put blank lines between the two tags.
            look = i + 1
            while look < len(lines) and not lines[look].strip():
                look += 1
            if look < len(lines):
                found = summary_text(lines[look])
                if found is not None:
                    title = found or title
                    i = look
            if out and out[-1].strip("> ").strip():
                out.append(">" * depth)
            out.append(("> " * depth) + f"> [!note]- {title}")
            depth += 1
        else:
            closing = re.fullmatch(r"(?:\s*>\s*)*\s*</details>\s*", line,
                                   re.IGNORECASE)
            if closing and depth:
                depth -= 1
                out.append(">" * depth)
            else:
                # Stray legacy tags are removed while their human text remains.
                clean = re.sub(r"</?(?:details|summary)(?:\s[^>]*)?>", "", line,
                               flags=re.IGNORECASE)
                if depth:
                    out.append(("> " * depth).rstrip() if not clean.strip() else
                               ("> " * depth) + clean)
                else:
                    out.append(clean)
        i += 1
    # Do not allow malformed input to leak HTML into a managed block.
    return "\n".join(re.sub(r"</?(?:details|summary)(?:\s[^>]*)?>", "", line,
                            flags=re.IGNORECASE) for line in out)


def separate_callouts(block):
    """Start every callout as its own block.

    ``> [!abstract] …`` followed directly by ``> [!conflict] …`` is ONE callout
    in Obsidian (the red conflict box vanished inside the summary card).  Insert
    a parent-level separator line when a callout header follows quoted text of
    the same or deeper level.
    """
    out = []
    for line in block.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        # A plain line right under a quote line is a lazy continuation of that quote (a figure's caption swallowed
        # the sentence that mentions it): end the quote first.  HTML comments (the kb markers) are not text.
        if out and line.strip() and not line.startswith((">", "<!--")) and out[-1].startswith(">"):
            out.append("")
        m = re.match(r"^((?:> ?)+)\[!", line)
        if m and out:
            depth = m.group(1).count(">")
            previous = out[-1]
            prev = re.match(r"^((?:> ?)*)", previous).group(1).count(">")
            # A top-level callout after an embed/caption/plain line also needs
            # an empty line; otherwise Obsidian treats it as lazy continuation.
            if previous.strip(" >") and (prev >= depth or (depth == 1 and prev < depth)):
                out.append("" if depth == 1 else ">" * (depth - 1))
        out.append(line)
    return "\n".join(out)


def _finish_managed(prefix, block, suffix, cap, row_kind):
    # Convert folds after capping (the capper keys on <details> markers); if the
    # added "> " prefixes push the note over the cap, cap again with less room.
    budget = max(0, cap - len((prefix + suffix).encode("utf-8")))
    final = separate_callouts(details_to_callouts(block))
    for _ in range(4):
        if len((prefix + final + suffix).encode("utf-8")) <= cap or budget == 0:
            break
        capped = cap_auto_block(block, budget, row_kind)
        final = separate_callouts(details_to_callouts(capped))
        over = len((prefix + final + suffix).encode("utf-8")) - cap
        budget = max(0, budget - max(over, 0) - 64)
    return prefix + final + suffix


def cap_managed_note(text, cap, row_kind):
    """Cap a complete note while keeping front matter and human text byte-identical.

    The cap applies to the final UTF-8 note, not merely to the managed block.  The
    caller may therefore use this for both newly-created notes and updates where
    a researcher has added text before or after ``kb:auto``.
    """
    start = text.find(AUTO_START)
    end = text.find(AUTO_END, start + len(AUTO_START)) if start >= 0 else -1
    if start < 0 or end < start:
        return text
    end += len(AUTO_END)
    prefix, block, suffix = text[:start], text[start:end], text[end:]
    return _finish_managed(prefix, block, suffix, cap, row_kind)

SECTION_LABELS = {
    "front_matter": "Front matter", "abstract": "Abstract", "introduction": "Introduction",
    "main_text": "Main text (no standard sections)",
    "methods": "Methods", "results": "Results", "results_discussion": "Results and Discussion",
    "discussion": "Discussion", "conclusion": "Conclusion", "acknowledgements": "Acknowledgements",
    "funding": "Funding", "author_contributions": "Author contributions",
    "conflicts": "Conflicts of interest", "data_availability": "Data availability",
    "references": "References", "supplementary": "Supplementary", "figure_legends": "Figure legends",
    "notes": "Publisher notes",
}


_WINDOWS_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                     *(f"lpt{i}" for i in range(1, 10))}


def safe_filename(text, maxlen=90):
    text = re.sub(r'[\\/:*?"<>|#^\[\]\x00-\x1f]', " ", text or "")
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > maxlen:
        text = text[:maxlen].rsplit(" ", 1)[0].rstrip(" ,;-")
    # Windows drops trailing dots/spaces (the link would then miss the file); Obsidian hides
    # names starting with a dot; CON, NUL, COM1… cannot be file names.
    text = text.strip(" .")
    if text.split(".")[0].casefold() in _WINDOWS_RESERVED:
        text += "_"
    return text or "Untitled"


def choose_note_name(meta):
    author = "Unknown"
    if meta.get("authors"):
        author = meta["authors"][0].get("family") or author
    year = meta.get("year") or "n.d."
    return safe_filename(f"{author} {year} - {meta.get('title') or meta['paper_id']}")


def pdf_uri(meta):
    for src in meta.get("source_files", []):
        if (src.get("role") == "main" and src.get("status") in ("active", "archived", "external")
                and src.get("path") and (config.ROOT / src["path"]).is_file()):
            return (config.ROOT / src["path"]).resolve().as_uri()
    return None


def section_label(name):
    return SECTION_LABELS.get(name, name)


def _canonical_analysis_terms(analysis, registry, paper_id=None):
    normalized = concepts.normalize_terms({key: analysis.get(key) or [] for key in TERM_TYPES}, registry)
    generic = concepts.generic_keys()  # a study word (Clinical data) gets no link and no stub; a registry node (researcher-confirmed) stays
    normalized = {key: [t for t in values if registry.lookup(t) or concepts._key(t) not in generic] for key, values in normalized.items()}
    if paper_id is None:
        return normalized
    candidates={term for values in normalized.values() for term in values}
    found=set()
    for row in read_jsonl(paper_dir(paper_id)/"sentences.jsonl"):
        if not presentation.body_sentence(row):
            continue
        for term in candidates-found:
            record=registry.lookup(term)
            if presentation.matches_term(row.get("text"), term, (record or {}).get("synonyms") or []):
                found.add(term)
    return {key:[term for term in values if term in found] for key,values in normalized.items()}


def _concept_rows(paper_id, con, registry):
    rows = []
    for row in con.execute("SELECT concept_id, level, n_sentences FROM concept_links WHERE paper_id = ?",
                           (paper_id,)):
        record = registry.by_id.get(row["concept_id"])
        if record:
            rows.append({"name": record["name"], "level": row["level"],
                         "n_sentences": row["n_sentences"], "type": record["type"]})
    return rows


def _quote_block(text):
    return "\n".join("> " + line if line.strip() else ">" for line in text.splitlines())


def vault_gate(con, registry, vault):
    """`shown(term)`: True when the registry node of `term` passes the ND-05 threshold in `vault` (`concepts.meets`), so a
    paper note may link it and a stub may exist; a term that is no registry node is not judged. Built once per run:
    one query (at first use) gives the mentions per concept; the evidence units of a node (an exact count needs the matched
    sentences, `concept_notes._collect`) are counted only for a node that fails the mentions test, once per node."""
    from . import concept_notes
    memo, mentions = {}, {}

    def shown(term):
        record = registry.lookup(term)
        if not record:
            return True
        cid = record["concept_id"]
        if cid not in memo:
            if not mentions:
                mentions.update((r[0], r[1] or 0) for r in con.execute(
                    "SELECT cl.concept_id, SUM(cl.n_sentences) FROM concept_links cl JOIN paper_vaults pv "
                    "ON pv.paper_id = cl.paper_id WHERE pv.vault = ? GROUP BY cl.concept_id", (vault,)))
            ids = concept_notes._descendants(registry, cid)
            memo[cid] = concepts.meets(0, sum(mentions.get(i, 0) for i in ids)) or \
                concept_notes.shown_in_vault(*concept_notes._collect(con, record, registry, vault)[1:])
        return memo[cid]
    return shown


def _links(terms, shown=None):
    """Wikilinks of the terms; a term that fails `shown` stays plain text."""
    links = []
    for term in terms:
        if shown and not shown(term):
            links.append(term)
            continue
        filename = safe_filename(term)
        # "|" and brackets would end the link or its alias early
        alias = term.replace("|", "/").replace("[", "(").replace("]", ")")
        links.append(f"[[{filename}|{alias}]]" if filename != term else f"[[{term}]]")
    return " · ".join(links)


def _note_context(con=None, registry=None, shown=None, vault=None):
    owns_con = con is None
    con = con or db.connect()
    if registry is None:
        registry = concepts.load_registry(con)
    if shown is None:
        shown = vault_gate(con, registry, vault) if vault else (lambda term: True)  # no vault, no vault threshold
    return con, registry, shown, owns_con


def render_auto_block(meta, sections, analysis, con=None, registry=None, shown=None, vault=None):
    con, registry, shown, owns_con = _note_context(con, registry, shown, vault)
    try:
        return _render_auto_block(meta, sections, analysis, con, registry, shown, vault)
    finally:
        if owns_con:
            con.close()


def _render_auto_block(meta, sections, analysis, con, registry, shown, vault=None):
    with presentation.collect_source_ids() as source_ids:
        text = _render_auto_block_body(meta, sections, analysis, con, registry, shown, vault)
    callout = presentation.source_ids_callout(source_ids)
    if not callout:
        return text
    head, marker, tail = text.rpartition(AUTO_END)
    return head.rstrip("\n") + "\n" + "\n".join(callout) + "\n" + marker + tail


def _paper_cite(meta, sentence_ids, sentences, register=True):
    """``Author Year, p. N`` for sentence IDs of this paper (their IDs are registered for the Source IDs callout)."""
    pages = sorted({sentences[sid]["page"] for sid in sentence_ids if sid in sentences
                    and sentences[sid].get("page") is not None})
    return presentation.cite_label(meta, pages, sentence_ids if register else (), meta["paper_id"])


def _render_auto_block_body(meta, sections, analysis, con, registry, shown, vault=None):
    out = [AUTO_START, ""]
    authors = [a["name"] for a in meta.get("authors") or []]
    author_text = ", ".join(authors[:6]) + (f" … ({len(authors)} authors)" if len(authors) > 6 else "")
    out.append("> [!info] Source")
    out.append(f"> **Authors:** {author_text or 'unknown'}")
    out.append(f"> **Journal:** {meta.get('journal') or 'unknown'} · **Year:** {meta.get('year') or 'unknown'}")
    ids = []
    if meta.get("doi"):
        ids.append(f"**DOI:** [{meta['doi']}](https://doi.org/{meta['doi']})")
    if meta.get("pmid"):
        ids.append(f"**PMID:** [{meta['pmid']}](https://pubmed.ncbi.nlm.nih.gov/{meta['pmid']}/)")
    if ids:
        out.append("> " + " · ".join(ids))
    if meta.get("publication_types"):
        out.append(f"> **Publication type:** {', '.join(meta['publication_types'])}")
    uri = pdf_uri(meta)
    status = meta.get("source_status", "active")
    out.append(f"> **Original PDF:** [Open PDF]({uri}) (status: {status})" if uri
               else f"> **Original PDF:** not available locally (status: {status})")
    out.append(f"> **Metadata:** {meta.get('metadata_source')} "
               f"(confidence: {meta.get('metadata_confidence')})")
    if meta.get("also_in"):
        out.append(f"> **Cũng nằm trong vault:** {', '.join(meta['also_in'])}")
    out.append("")
    out += [f"[[#{PAPER_RESULTS_HEADING}|{PAPER_RESULTS_HEADING}]] · [[#{PAPER_FIGURES_HEADING}|{PAPER_FIGURES_HEADING}]] · [[#{PAPER_STRUCTURE_HEADING}|Sections]]", ""]

    if meta.get("text_quality") == "no_text":
        out += ["> [!warning] No extractable text",
                "> This PDF looks like a scanned image. It is stored, but its content is not searchable yet (needs OCR).", ""]
    elif meta.get("text_quality") == "low_text":
        out += ["> [!warning] Little extractable text",
                "> Only part of this PDF produced text. Search results may be incomplete.", ""]
    if any("review" in (t or "").lower() for t in meta.get("publication_types") or []) \
            or (analysis or {}).get("evidence_type") in ("review", "systematic_review", "meta_analysis"):
        out += ["> [!note] Review article",
                "> Statements summarise other studies; they are not primary experimental evidence.", ""]

    out.append("## Tóm tắt (Abstract)")
    if meta.get("abstract"):
        out.append(f"*Source: {meta.get('abstract_source')}*")
        out.append("")
        out.append(_quote_block(meta["abstract"]))
    else:
        out.append("*No abstract found.*")
    out.append("")

    out.append("## Phân tích AI (AI analysis)")
    if not analysis:
        out += ["> [!todo] Not analysed yet",
                "> Ask an AI agent to \"analyse the new papers\" (see `docs/AGENT_TOOLS.md`).", ""]
    elif analysis.get("version") == 2:
        out += _render_v2(meta, analysis, con, registry, shown, vault)
    else:
        out.append(f"**Classification:** {analysis['classification']} — {analysis.get('classification_reason', '')}")
        out.append(f"**Evidence type:** {analysis.get('evidence_type', 'unknown')}")
        out.append(f"*Analysed by {analysis.get('analyst', 'AI')} on {analysis.get('analysed_at', '?')}. "
                   "AI-generated: verify against the quoted passages.*")
        out += _heuristic_callout(analysis)
        out.append("")
        if analysis.get("summary"):
            out += ["**Summary**", "", analysis["summary"], ""]
        normalized = _canonical_analysis_terms(analysis, registry, meta["paper_id"])
        for term_type in TERM_TYPES:
            terms = normalized.get(term_type) or []
            if terms:
                out.append(f"**{TERM_LABELS[term_type]}:** {_links(terms, shown)}  ")
        mentioned = [r["name"] for r in _concept_rows(meta["paper_id"], con, registry)
                     if r["level"] == "mentioned" and shown(r["name"])]
        if mentioned:
            out.append(f"Mentioned in text: {_links(sorted(set(mentioned)))}  ")
        out.append("")
        claims = analysis.get("claims") or []
        if claims:
            out.append("### Khẳng định chính (Key claims)")
            out.append("*Each claim links to the exact original passage (section, page).*")
            out.append("")
            for i, c in enumerate(claims, start=1):
                out.append(f"{i}. **[{c['claim_type'].capitalize()}]** {c['statement']}")
                out.append(f"    > \"{c['quote']}\"")
                out.append(f"    > — *{section_label(c.get('section'))}, p. {c.get('page')}*")
            out.append("")

    out.append(f"## {PAPER_STRUCTURE_HEADING}")
    if sections:
        out += ["| Section | Starts on page |", "|---|---|"]
        out += [f"| {section_label(s['name'])} | {s.get('page_start')} |" for s in sections]
    else:
        out.append("*No section headings detected.*")
    out.append("")
    out.append(f"Full text: {meta.get('n_passages', 0)} searchable passages from {meta.get('n_pages', 0)} pages "
               f"(text quality: {meta.get('text_quality')}).")
    from . import structure
    if (paper_dir(meta['paper_id']) / 'sentences.jsonl').is_file():
        inventory = structure.build(meta['paper_id'], persist=False)
        out += ['', '> [!info]- Section coverage and source terminology',
                f"> Body sentences to review: {inventory['coverage']['required']}; uncovered: {len(inventory['coverage']['uncovered'])}. Inventory completeness is not analysis completeness.", '>']
        page_rows = {r.get("sentence_id"): r for r in read_jsonl(paper_dir(meta['paper_id']) / "sentences.jsonl")}
        for term in inventory['abbreviations']:
            definitions = '; '.join(d['text'] for d in term['definitions']) or 'definition not found; review required'
            out.append(f"> **{term['short']}** — {definitions} · " + _paper_cite(meta, term['sentence_ids'], page_rows, register=False))
        out += ['>', '> **Introduction WH profile**']
        for key, row in inventory['introduction_wh'].items():
            out.append(f"> {key}: " + (row.get('text') or row.get('reason') if row else 'not analysed / source changed'))
        out += ['>', '> Uncertainty words are preserved in source quotes; same-paragraph terms do not prove a causal relation.']
    out.append("")
    out.append(AUTO_END)
    return "\n".join(out)


V2_GROUPS = [  # (callout title, callout type, unit types)
    ("Results of this paper", "success", ("result",)),
    ("Methods, doses and protocols", "info", ("method_detail",)),
    ("Authors' interpretations and hypotheses", "tip", ("interpretation", "hypothesis")),
    ("Conclusions and limitations", "abstract", ("conclusion", "limitation")),
    ("What this paper says about other studies (cited)", "quote", ("cited_statement",)),
    ("Background", "note", ("background",)),
]
UNIT_LABELS = {"result": "Result", "method_detail": "Method", "interpretation": "Interpretation",
               "hypothesis": "Hypothesis", "conclusion": "Conclusion", "limitation": "Limitation",
               "cited_statement": "Cited", "background": "Background"}


def _cited_paper_links(meta):
    """ref_id -> 'Marlow 2010 [[note of that paper]]' (link only when the cited paper is in the library)."""
    out = {}
    for r in read_jsonl(paper_dir(meta["paper_id"]) / "references.jsonl"):
        label = f"{r.get('first_author') or '?'} {r.get('year') or ''}".strip()
        target = r.get("resolved_paper_id")
        target_meta = read_json(paper_dir(target) / "metadata.json") if target else None
        if target_meta and target_meta.get("note_name"):
            label += f" [[{target_meta['note_name']}]]"
        out[r["ref_id"]] = f"[{r['ref_id'].split(':')[-1]}] {label}"
    return out


def _heuristic_callout(analysis):
    """Warning shown when units were labelled by a rule script, not by an AI reading the paper (D3.18)."""
    from . import find  # local import: find pulls in vectors/workflow
    if find.analysis_origin(analysis.get("analyst")) != "heuristic_draft":
        return []
    return ["", "> [!warning] Phân tích nháp tự động (heuristic_draft)",
            f"> Các đơn vị bằng chứng của bài này được gán loại bằng quy tắc theo vị trí câu (script {analysis['analyst']}), "
            "chưa phải AI đọc hiểu toàn bài. Trích dẫn nguyên văn vẫn đúng nguồn; loại đơn vị "
            "(result/interpretation/...) và câu tóm tắt có thể sai. Cần phân tích lại trước khi dùng làm kết luận."]


def _render_v2(meta, analysis, con, registry, shown, vault=None):
    """Two-tier view: a one-screen summary, then collapsible groups of evidence units.
    Every unit shows the verbatim sentences it rests on, their section and page."""
    units = list(read_jsonl(paper_dir(meta["paper_id"]) / "evidence.jsonl"))
    for unit in units:
        unit["paper"] = meta
        unit.setdefault("paper_id", meta["paper_id"])
    sentences = {s.get("sentence_id"): s for s in read_jsonl(paper_dir(meta["paper_id"]) / "sentences.jsonl")}
    unit_by_seq = {u["seq"]: u for u in units}
    out = [f"**Classification:** {analysis['classification']} — {analysis.get('classification_reason', '')}",
           f"**Evidence type:** {analysis.get('evidence_type', 'unknown')} · {len(units)} evidence units"
           + (f" ({analysis.get('n_flagged')} with warnings)" if analysis.get("n_flagged") else ""),
           f"*Extracted by {analysis.get('analyst', 'AI')} on {analysis.get('analysed_at', '?')}; every quote is "
           "copied by the software from the paper and checked (V1–V13). Verify against the quote before citing.*", ""]
    out[-1:-1] = _heuristic_callout(analysis)
    if analysis.get("status") == "needs_fix":
        out += ["> [!warning] Analysis not finished", "> The agent still has errors to fix; units below are partial.", ""]
    rq = analysis.get("research_question")
    if rq:
        out += ["> [!question] Research question", f"> {rq['text']}",
                f"> — {_paper_cite(meta, rq['sentence_ids'], sentences)}", ""]
    if analysis.get("summary"):
        out += ["**Summary**", ""]
        for s in analysis["summary"]:
            refs = ", ".join(dict.fromkeys(
                presentation.cite_label(meta, unit_by_seq[n].get("pages"), [unit_by_seq[n]["unit_id"]], meta["paper_id"], page_only=True)
                for n in s["units"] if n in unit_by_seq)) or "p. ?"
            out.append(f"- {s['text']} *({refs})*")
        out.append("")
    normalized = _canonical_analysis_terms(analysis, registry, meta["paper_id"])
    for term_type in TERM_TYPES:
        terms = normalized.get(term_type) or []
        if terms:
            out.append(f"**{TERM_LABELS[term_type]}:** {_links(terms, shown)}  ")
    mentioned = [r["name"] for r in _concept_rows(meta["paper_id"], con, registry)
                 if r["level"] == "mentioned" and shown(r["name"])]
    if mentioned:
        out.append(f"Mentioned in text: {_links(sorted(set(mentioned)))}  ")
    out.append("")
    out += [f"## {PAPER_RESULTS_HEADING}", ""]
    results = [u for u in units if u.get("unit_type") == "result"]
    from . import find
    if find.effective_evidence_type(meta) in find.REVIEW_TYPES:
        results=[]
    if results:
        out += _paper_results_table(presentation.evidence_table(results))
    else:
        out += ["No structured result units found in the current knowledge base."]
    from . import result_notes
    out += result_notes.render_figures_and_tables(meta, vault)
    # The legacy renderer below is retained as a source-compatible no-op while
    # this section is owned by result_notes.  It must not re-add table JSON.
    out += []
    objects = list(read_jsonl(paper_dir(meta["paper_id"]) / "figures.jsonl")) + list(read_jsonl(paper_dir(meta["paper_id"]) / "tables.jsonl"))
    from . import media
    bundles=[]
    if False and not bundles:
        out.append("No source-linked media inventory available; verify the original source.")
    for bundle in bundles:
        oid=bundle["object_id"]; loc=bundle["source_locator"]; crop=bundle["crop"]
        out += [f"### {bundle.get('label') or oid}",
                f"`{oid}` · {bundle['kind']} · {loc.get('source')} · p. {loc.get('page')} · {loc['region_kind']}"]
        if result_notes._is_page_preview(loc):
            out.append("Region completeness: not assessed. A source-page preview is not a located scientific graphic.")
        text=" ".join(r["text"] for r in bundle["caption"])
        out.append(_quote_block(text) if text else "Caption not extracted.")
        if crop.get("path") and crop.get("status")=="available":
            target=media.crop_path(paper_dir(meta["paper_id"]),crop["path"])
            if target.is_file() and media._hash(target)==crop.get("sha256"):
                out.append(f"[Open source region]({target.resolve().as_uri()})")
        else:
            out.append("Source region unavailable/unverified; inspect the original PDF.")
        if loc.get("source")=="main" and pdf_uri(meta):
            out.append(f"[Open source page]({pdf_uri(meta)}#page={loc.get('page') or 1})")
        if bundle.get("table"):
            tab=bundle["table"]
            out.append(f"Table extraction: {tab.get('quality')}; verify columns against the image.")
            out += ["<details>","<summary>Table data and footnotes</summary>",""]
            out.append("```json\n"+json.dumps({k:tab.get(k) for k in ('header','rows','footnotes')},ensure_ascii=False,indent=2)+"\n```")
            out += ["</details>"]
        if bundle["data"] or bundle["mentions"] or bundle["anchors"]:
            out += ["<details>","<summary>Source-linked data, mentions and formula anchors</summary>",""]
            for row in bundle["data"]+bundle["mentions"]+bundle["anchors"]:
                out += [_quote_block(row["text"]),f"`{row['sentence_id']}` · p. {row.get('page')}"]
            out += ["</details>"]
        out.append("")
    cited = _cited_paper_links(meta) if any(u.get("cited_refs") for u in units) else {}
    for title, kind, types in V2_GROUPS:
        group = [u for u in units if u["unit_type"] in types]
        if types==('result',) and find.effective_evidence_type(meta) in find.REVIEW_TYPES:
            title='Stored review statements labelled as results — secondary evidence'
            kind='quote'
        if not group:
            continue
        out.append(f"> [!{kind}]- {title} ({len(group)})")
        for u in group:
            mark = ""
            out.append(f"> **[{UNIT_LABELS[u['unit_type']]}]**{mark} {u['statement']}")
            if u.get("cited_refs"):
                out.append("> *Cites:* " + "; ".join(cited.get(r, r.split(':')[-1]) for r in u["cited_refs"]))
            refs = __import__("kbsys.node_profile", fromlist=["_refs"])._refs(u.get("sentence_ids"), sentences, meta["paper_id"], meta)
            out.append("> > " + " ".join(r["text"] for r in refs) if refs else "> > Source passage unavailable; stored interpretation requires review.")
            if any(r.get("context_basis")=="preceding_source_fragment" for r in refs):
                out.append("> > Context includes a separately copied preceding-page fragment; not an inferred source link. Verify the stored interpretation against both pages.")
            where = ", ".join(section_label(s) for s in dict.fromkeys(r.get("section") for r in refs if r.get("section")))
            pages = ", ".join(str(p) for p in sorted({r["page"] for r in refs if r.get("page") is not None}))
            # the unit ID is enough to recover its sentence IDs (evidence.jsonl); keeps the callout of a capped note small
            presentation.note_source_ids(meta["paper_id"], [u["unit_id"]],
                                         presentation.cite_label(meta, [p for p in pages.split(", ") if p]))
            if refs:
                out.append(f"> > — *{where}, p. {pages}*")
            out.append(">")
        out.append("")
    flagged = [u for u in units if u["status"] == "flagged"]
    if flagged:
        out.append(f"> [!info]- Source check details ({len(flagged)})")
        for u in flagged:
            for f in u.get("flags", []):
                out.append(f"> - **{_short_statement(u)}** {f['code']}: {f['message']}")
        out.append("")
    return out


def _short_statement(unit, limit=60):
    text = " ".join(str(unit.get("statement") or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + "…"


def _paper_results_table(table):
    """Remove the paper self-link and render unavailable cells as empty."""
    if len(table) < 2:
        return table

    def cells(row):
        return re.split(r"(?<!\\)\|", row)[1:-1]  # an escaped \| inside a link alias is not a column break

    return [
        "| " + " | ".join(cells(table[0])[1:]) + " |",
        "| " + " | ".join(cells(table[1])[1:]) + " |",
    ] + [
        "| " + " | ".join(value.replace("not annotated", "").strip()
                            for value in cells(row)[1:]) + " |"
        for row in table[2:]
    ]


def _split_frontmatter(text):
    if text.startswith("---\n"):
        end = text.find("\n---\n", 4)
        if end != -1:
            try:
                return yaml.safe_load(text[4:end]) or {}, text[end + 5:]
            except yaml.YAMLError:
                return None, text
    return {}, text


def _frontmatter(meta, analysis, existing=None):
    fm = dict(existing or {})
    values = {
        "type": "paper",
        "paper_id": meta["paper_id"],
        "title": meta.get("title"),
        "authors": [a["name"] for a in meta.get("authors") or []][:20],
        "year": meta.get("year"),
        "journal": meta.get("journal"),
        "doi": meta.get("doi"),
        "pmid": meta.get("pmid"),
        "publication_types": meta.get("publication_types") or [],
        "evidence_type": __import__("kbsys.find", fromlist=["effective_evidence_type"]).effective_evidence_type(meta),
        "classification": meta.get("classification"),
        "analysis_status": meta.get("analysis_status"),
        "text_quality": meta.get("text_quality"),
        "source_status": meta.get("source_status", "active"),
        "also_in": meta.get("also_in") or [],
    }
    for key in MACHINE_KEYS:
        fm[key] = values[key]
    fm.setdefault("tags", ["paper"])
    units = list(read_jsonl(paper_dir(meta["paper_id"]) / "evidence.jsonl"))
    for u in units:
        u.setdefault("paper_id", meta["paper_id"])
    fm.update(presentation.machine_properties({"entity_kind": "paper", "roles": [],
              "category_basis": "paper record"}, units))
    return "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n"


def write_paper_note(meta, vault, ensure_stubs=False, con=None, registry=None, shown=None):
    """Create or update the paper note. Returns a status string."""
    from . import find
    meta=dict(meta)
    meta['evidence_type']=find.effective_evidence_type(meta)
    con, registry, shown, owns_con = _note_context(con, registry, shown, vault)
    try:
        # One canonical paper record; other vaults holding it are shown, never a reason to flag it.
        meta["also_in"] = [r[0] for r in con.execute(
            "SELECT vault FROM paper_vaults WHERE paper_id = ? AND vault <> ? ORDER BY vault",
            (meta["paper_id"], vault))]
        folder = config.vault_dir(vault) / "Papers"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{meta['note_name']}.md"
        pdir = paper_dir(meta["paper_id"])
        sections = read_json(pdir / "sections.json", [])
        analysis = read_json(pdir / "analysis.json")
        if analysis:
            analysis={**analysis,'evidence_type':meta.get('evidence_type') or analysis.get('evidence_type')}
        block = render_auto_block(meta, sections, analysis, con, registry, shown, vault)
        if not path.exists():
            prefix = _frontmatter(meta, analysis) + f"\n# {meta.get('title') or meta['paper_id']}\n\n"
            suffix = "\n\n## My notes\n\n"
            path.write_text(cap_managed_note(prefix + block + suffix, PAPER_NOTE_MAX_BYTES, "paper"), encoding="utf-8", newline="\n")
            if analysis and ensure_stubs:
                for term_type in TERM_TYPES:
                    for term in _canonical_analysis_terms(analysis, registry, meta["paper_id"]).get(term_type) or []:
                        if shown(term):
                            ensure_term_stub(vault, term_type, term, registry)
            return "created"

        text = path.read_text(encoding="utf-8")
        start, end = text.find(AUTO_START), text.find(AUTO_END)
        fm, rest = _split_frontmatter(text)
        if start == -1 or end == -1 or fm is None:
            config.log(f"SKIP note update (markers or frontmatter missing): {path}")
            return "skipped (auto markers missing)"
        if fm.get("paper_id") not in (None, meta["paper_id"]):
            config.log(f"SKIP note update (note belongs to {fm.get('paper_id')}): {path}")
            return "skipped (note name used by another paper)"
        start_r, end_r = rest.find(AUTO_START), rest.find(AUTO_END) + len(AUTO_END)
        prefix = _frontmatter(meta, analysis, fm) + rest[:start_r]
        suffix = rest[end_r:]
        new_text = cap_managed_note(prefix + block + suffix, PAPER_NOTE_MAX_BYTES, "paper")
        if new_text != text:
            path.write_text(new_text, encoding="utf-8", newline="\n")
        if analysis and ensure_stubs:
            for term_type in TERM_TYPES:
                for term in _canonical_analysis_terms(analysis, registry, meta["paper_id"]).get(term_type) or []:
                    if shown(term):
                        ensure_term_stub(vault, term_type, term, registry)
        return "updated"
    finally:
        if owns_con:
            con.close()


_NOTE_NAME_CACHE = {}
_LAST_TIDY_REPORT = {}


def _existing_note_names(vault):
    if vault not in _NOTE_NAME_CACHE:
        _NOTE_NAME_CACHE[vault] = {p.stem.lower() for p in config.vault_dir(vault).rglob("*.md")}
    return _NOTE_NAME_CACHE[vault]


def _clear_note_cache(vault):
    _NOTE_NAME_CACHE.pop(vault, None)


def ensure_term_stub(vault, term_type, term, registry):
    record = registry.lookup(term)
    if record:
        name = safe_filename(record["name"])
        term_type = concepts.TYPE_KEYS[record["type"]]
        term = record["name"]
    else:
        name = safe_filename(term)
    folder = config.vault_dir(vault) / TERM_FOLDERS[term_type]
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{name}.md"
    if path.exists():
        _existing_note_names(vault).add(name.lower())
        return
    # A different-folder human note with this name is never overwritten.  A pure
    # wrong-case stub is handled by refresh_all's reversible tidy pass first.
    if name.lower() in _existing_note_names(vault):
        config.log(f"STUB NOT CREATED (name already exists): {vault}/{TERM_FOLDERS[term_type]}/{name}.md")
        return
    kind = _stub_kind(term_type)
    path.write_text(
        f"---\ntype: {kind}\ntags: [{term_type}]\n---\n{STUB_MARK}\n# {term}\n\n"
        "Papers that mention this appear under **Backlinks** (right-hand panel in Obsidian).\n\n"
        "## Notes\n\n",
        encoding="utf-8", newline="\n")
    _existing_note_names(vault).add(name.lower())


WIKI_LINK = re.compile(r"\[\[([^\]|#^]+)(?:[#^][^\]|]*)?(?:\|[^\]]*)?\]\]")


def _stub_kind(key):
    return presentation.graph_type_for_kind({
        "molecules": "molecule", "methods": "method", "concepts": "concept",
        "topics": "topic", "species": "species", "cell_types": "cell_type",
    }.get(key, key)) or key.rstrip("s").lower()


def _stub_template(folder, name, key=None):
    # Organisms/ holds two term types (species, cell_types); pass `key` to choose one.
    keys = [k for k, value in TERM_FOLDERS.items() if value == folder]
    key = key if key in keys else (keys[0] if keys else None)
    if not key:
        return None
    return (f"---\ntype: {_stub_kind(key)}\ntags: [{key}]\n---\n{STUB_MARK}\n# {name}\n\n"
            "Papers that mention this appear under **Backlinks** (right-hand panel in Obsidian).\n\n"
            "## Notes\n\n")


def _is_pure_stub(folder, name, text):
    return any(_is_pure_stub_for(_stub_template(folder, name, key), name, text)
               for key, value in TERM_FOLDERS.items() if value == folder)


def _is_pure_stub_for(expected, name, text):
    # The sole added machine property is safe to strip for template recognition;
    # any other user addition still makes the stub protected.
    text = text.replace("\nkb_stub: true\n", "\n", 1)
    if text == expected:
        return True
    # Legacy machine stubs used the scientific kind (for example ``molecule``)
    # in ``type``. They remain pure machine templates and may be upgraded to
    # the fixed graph presentation type on the next refresh.
    expected_type = re.search(r"(?m)^type: ([^\n]+)$", expected or "")
    if expected_type:
        normalized_type = re.sub(r"(?m)^type: [^\n]+$", f"type: {expected_type.group(1)}", text, count=1)
        if normalized_type == expected:
            return True
    # Older machine stubs used the unsanitised AI term in the heading (for
    # example a slash became a space in the filename).  The heading itself is
    # still machine text; only content outside this template is human text.
    heading = re.search(r"(?m)^# (.*)\n", text)
    if not heading or safe_filename(heading.group(1)).lower() != name.lower():
        return False
    normalized = re.sub(r"(?m)^# .*\n", "# <TERM>\n", text)
    expected = re.sub(r"(?m)^# .*\n", "# <TERM>\n", expected or "")
    return bool(expected) and normalized == expected


def _canonical_stub_target(folder, name, registry):
    record = registry.lookup(name)
    if not record:
        # Legacy stubs may have the safe filename while the canonical name keeps
        # an otherwise invalid character such as '/'.
        for candidate in registry.records:
            if safe_filename(candidate["name"]).casefold() == name.casefold():
                record = candidate
                break
    if not record:
        return None
    canonical_folder = TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
    canonical_name = safe_filename(record["name"])
    if folder == canonical_folder and name == canonical_name:
        return None
    return canonical_folder, canonical_name


def _human_protected(path, vdir, target):
    if not target:
        return path.as_posix()
    folder, name = target
    return {"from": path.as_posix(),
            "suggested_target": config.rel(vdir / folder / f"{name}.md")}


def tidy_vault(con, vault, dry_run=False, registry=None):
    """Quarantine untouched, unlinked machine stubs; never delete or edit human notes."""
    if registry is None:
        registry = concepts.load_registry(con)
    vdir = config.vault_dir(vault)
    linked = set()
    papers = vdir / "Papers"
    if papers.exists():
        for path in papers.glob("*.md"):
            linked.update(x.strip().lower() for x in WIKI_LINK.findall(path.read_text(encoding="utf-8")))

    stamp = __import__("datetime").datetime.now().strftime("%Y%m%d_%H%M%S")
    moved, human, linked_kept, concept_notes_kept = [], [], [], []
    for folder in NODE_FOLDERS:
        directory = vdir / folder
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.md")):
            if path.stem == LIST_NOTES.get(folder):
                continue
            text = path.read_text(encoding="utf-8")
            if INDEX_MARK in text:
                continue  # machine maps/lists (e.g. _Maps/Index), not stubs
            pure = STUB_MARK in text and _is_pure_stub(folder, path.stem, text)
            if not pure and "kb:concept-fingerprint:" in text and text.count(AUTO_START) == 1:
                concept_notes_kept.append(path.as_posix())  # machine Concept Note (text outside its block is kept)
                continue
            if not pure:
                if STUB_MARK in text or text.strip():
                    human.append(_human_protected(path, vdir,
                                                  _canonical_stub_target(folder, path.stem, registry)))
                continue
            canonical_target = _canonical_stub_target(folder, path.stem, registry)
            # A canonical migration is a repair, not ordinary orphan cleanup:
            # refresh rewrites paper links to the canonical name, and the old
            # pure stub is safe to quarantine even if a stale link remains.
            if canonical_target is None and path.stem.lower() in linked:
                linked_kept.append(path.as_posix())
                continue
            target = config.STORE / "removed" / f"vault_stubs_{stamp}" / vault / folder / path.name
            if not dry_run:
                from . import vault_layout
                target = vault_layout.retire(vault, path, "vault_stubs", stamp)
            moved.append({"from": path.as_posix(), "to": config.rel(target)})
            if not dry_run:
                config.log(f"VAULT STUB MOVED {config.rel(path)} -> {config.rel(target)}")
    if not dry_run:
        _clear_note_cache(vault)
    report = {"vault": vault, "dry_run": dry_run, "moved": moved,
              "human_protected": human, "linked_kept": linked_kept, "concept_notes": len(concept_notes_kept)}
    _LAST_TIDY_REPORT[vault] = report
    return report


def last_tidy_report(vault):
    return _LAST_TIDY_REPORT.get(vault, {"vault": vault, "moved": [], "human_protected": [], "linked_kept": []})


def ensure_linked_stubs(con, vault, registry=None, shown=None):
    """Create stubs only for concepts displayed in paper notes, which are those that pass the vault threshold (ND-05)."""
    if registry is None:
        registry = concepts.load_registry(con)
    if shown is None:
        shown = vault_gate(con, registry, vault)
    for (concept_id,) in con.execute("""SELECT DISTINCT cl.concept_id FROM concept_links cl
                                       JOIN paper_vaults pv ON pv.paper_id = cl.paper_id
                                       WHERE pv.vault = ?""", (vault,)).fetchall():
        record = registry.by_id.get(concept_id)
        if record and shown(record["name"]):
            ensure_term_stub(vault, concepts.TYPE_KEYS[record["type"]], record["name"], registry)


def ensure_analysis_stubs(meta, vault, registry=None, shown=None):
    """Keep every displayed studied term resolvable, including legacy AI tags that
    have no full-text match and therefore do not get a concept_links row. A term below the vault threshold has no stub."""
    analysis = read_json(paper_dir(meta["paper_id"]) / "analysis.json")
    if not analysis:
        return
    if registry is None:
        registry = concepts.load_registry()
    if shown is None:
        con = db.connect()  # the gate reads it lazily, so it stays open until the stubs are made
        try:
            return ensure_analysis_stubs(meta, vault, registry, vault_gate(con, registry, vault))
        finally:
            con.close()
    normalized = _canonical_analysis_terms(analysis, registry, meta["paper_id"])
    for key in TERM_TYPES:
        for term in normalized[key]:
            if shown(term):
                ensure_term_stub(vault, key, term, registry)


INDEX_FRONTMATTER = "---\ntype: moc\ntags: [kb-index]\n---\n"


_MACHINE_INDEX_LINE = re.compile(r"^(- \[\[.*\]\]|\|.*\||\d+ papers in this vault\.|\*Empty for now\.\*|---|tags: .*)$")


def _write_index(path, content):
    """Write a machine index note (tagged kb-index, hidden from the graph).

    The generated list sits between the kb:auto markers; text a person adds outside them
    (for example ideas typed into My Ideas.md) is kept. Notes with their own frontmatter
    (Review notes) are written whole, once."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and INDEX_MARK not in old:
        return  # a person wrote a note with this name; leave it alone
    path.parent.mkdir(parents=True, exist_ok=True)
    if content.startswith("---\n"):
        new = content
    else:
        block = f"{AUTO_START}\n{content.rstrip()}\n{AUTO_END}"
        start, end = (old.find(AUTO_START), old.find(AUTO_END)) if old else (-1, -1)
        if old and old.count(AUTO_START) == 1 and old.count(AUTO_END) == 1 and start < end:
            new = old[:start] + block + old[end + len(AUTO_END):]
        else:
            # New note, or an older index without markers: keep any line a person added.
            generated = set(content.splitlines())
            human = [line for line in (old or "").splitlines() if line.strip() and line not in generated
                     and INDEX_MARK not in line and not _MACHINE_INDEX_LINE.match(line)]
            new = INDEX_FRONTMATTER + block + "\n" + ("\n" + "\n".join(human) + "\n" if human else "")
    fm, rest = _split_frontmatter(new)
    if isinstance(fm, dict) and not fm.get("type"):
        fm = {"type": "moc", **fm}
        new = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + rest
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")


GRAPH_FILTER = ('-tag:#kb-index -[kb_stub:true] -path:"Review Queue" -path:"_Bases" -path:"_Templates" '
                '-path:"_Attachments" -file:Home')
# Earlier machine defaults: replaced by GRAPH_FILTER; any other value is the researcher's.
LEGACY_GRAPH_FILTERS = ('-tag:#kb-index -[kb_stub:true] -path:"Review Queue" -file:Home',
                        '-tag:#kb-index -path:"Review Queue" -file:Home')
LEGEND_NOTE = "Chú giải graph"


# Fixed, first-match-wins palette (docs/ARCHITECTURE.md section 5). Researcher 2026-10-12: few groups, queries that
# read as folder names, personal notes in one loud colour; 2026-10-24: every visible note type has a colour.
GRAPH_GROUPS = [
    ('path:"Mine/"', '#FF00A8', 'Ghi chú của bạn (Mine)'),
    ('path:"Discussions/"', '#FACC15', 'Thảo luận đã lưu (Discussions)'),
    ('file:"CONFLICT -"', '#DC2626', 'Xung đột giữa các bài (CONFLICT)'),
    ('path:"Papers/"', '#2563EB', 'Bài báo (Papers)'),
    ('path:"Methods/"', '#0D9488', 'Phương pháp (Methods)'),
    ('path:"Substances/"', '#7C3AED', 'Chất / marker (Substances)'),
    ('path:"Organisms/"', '#16A34A', 'Loài / tế bào (Organisms)'),
    ('path:"Results/" OR path:"Claims/"', '#D97706', 'Kết quả / nhận định (Results, Claims)'),
    ('path:"Concepts/"', '#0EA5E9', 'Khái niệm (Concepts)'),
    ('path:"_Maps/"', '#84CC16', 'Chủ đề / bản đồ (_Maps)'),
]
# The same colours by note type, for the web graph (neural.py).
GRAPH_TYPE_COLORS = {"discussion": "#FACC15", "conflict": "#DC2626", "paper": "#2563EB", "method": "#0D9488",
                     "technique": "#0D9488", "substance": "#7C3AED", "organism": "#16A34A", "cell_type": "#16A34A",
                     "result": "#D97706", "claim": "#D97706", "concept": "#0EA5E9", "moc": "#84CC16"}
# Queries emitted by previous versions are machine-owned. Other graph queries
# are researcher-owned and must survive regeneration.
LEGACY_GRAPH_QUERIES = {
    '[type:conflict]', '[type:paper]', '[type:method] OR [type:technique]', '[type:substance]',
    '[type:organism] OR [type:cell_type]', '[type:result] OR [type:claim]', '[type:concept]', '[type:moc]',
    '[type:home]', '[type:lab_note]', 'path:"My Ideas" OR path:"Mine"', '[type:question] OR path:"Review Queue"',
    'content:"[!danger] Potential conflict / differing findings"',
    'path:"Papers"', 'path:"Topics"', 'path:"Concepts"', 'path:"Methods"',
    'path:"Molecules"', 'path:"Cell Types"', 'path:"Species"', 'path:"My Ideas"',
    'path:"Knowledge Gaps" OR path:"Open Questions"',
    *{f'[kb_kind:/^{kind}$/]' for kind in presentation.COLORS},
    *{f'[kb_kind:{kind}]' for kind in presentation.COLORS},
    'path:"Mine"',
}


def merge_graph_groups(existing):
    """Researcher groups first (their order, so an edited machine query wins), then GRAPH_GROUPS; a machine query the
    file already holds keeps its colour (the researcher's recolour); legacy machine queries and duplicates are dropped."""
    managed = LEGACY_GRAPH_QUERIES | {q for q, _, _ in GRAPH_GROUPS}
    groups = [g for g in existing or [] if isinstance(g, dict)] if isinstance(existing, list) else []
    kept = {}
    for g in groups:
        colour = g.get("color")
        # ponytail: a colour once written counts as the researcher's, so a later change of a machine colour reaches
        # only files without that group; add a machine-colour history when a machine colour changes.
        if g.get("query") in managed and isinstance(colour, dict) and type(colour.get("rgb")) is int:
            kept.setdefault(g["query"], colour)
    own = [{"query": q, "color": kept.get(q) or {"a": 1, "rgb": int(c[1:], 16)}} for q, c, _ in GRAPH_GROUPS]
    return [g for g in groups if g.get("query") not in managed] + own


def missing_graph_groups(groups):
    """GRAPH_GROUPS queries not found, in order, as a subsequence of a graph.json colorGroups list (VN-09)."""
    queries = [g.get("query") for g in groups if isinstance(g, dict)] if isinstance(groups, list) else []
    missing, pos = [], 0
    for q, _, _ in GRAPH_GROUPS:
        try:
            pos = queries.index(q, pos) + 1
        except ValueError:
            missing.append(q)
    return missing


def sync_extended_graph(root):
    """Turn on Extended Graph's `syncDefaultState`, so the plugin opens its Default state with the core graph.json
    options. Off (the plugin default), switching the plugin on applied its saved Default state (empty colour groups
    and filter) over graph.json: the colours the researcher saw vanish (2026-10-24). Other plugin settings untouched;
    nothing is written when the plugin has no valid data.json."""
    path = Path(root) / ".obsidian" / "plugins" / "extended-graph" / "data.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict) and data.get("syncDefaultState") is not True:
        data["syncDefaultState"] = True
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def write_graph_settings(vault):
    """Merge the machine palette into graph.json without touching other settings."""
    path = config.vault_dir(vault) / ".obsidian" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):  # e.g. a JSON list: treated like invalid JSON
        settings = {}
    settings["colorGroups"] = merge_graph_groups(settings.get("colorGroups"))
    if settings.get("search") in (None, "", *LEGACY_GRAPH_FILTERS):
        settings["search"] = GRAPH_FILTER
    settings.setdefault("showAttachments", False)
    settings["hideUnresolved"] = True
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    sync_extended_graph(config.vault_dir(vault))
    lines = [INDEX_MARK, f"# {LEGEND_NOTE}", "",
             "Màu của các chấm trong Graph view (tự tạo bởi phần mềm):", ""]
    lines += [f"- <span style=\"color:{c}\">●</span> **{label}** — `{q}`" for q, c, label in GRAPH_GROUPS]
    lines += ["- <span style=\"color:#94A3B8\">●</span> **Còn lại** — màu xám mặc định",
              "", "Màu bạn đổi trong Graph view → Groups được giữ khi phần mềm làm mới ghi chú; nhóm bạn tự thêm "
              "đứng trước nhóm của phần mềm nên được ưu tiên."]
    lines += ["", "Colors show machine-owned note types, not experimental roles or scientific definitions.",
              "kb_roles can contain intervention, measured_marker, model, or source_mention together on one canonical note.",
              "Roles require copied source matches and stored structured fields. Co-occurrence does not establish a mechanism.",
              "Quality details stay beside evidence in paper notes; review flags concern scope/relevance."]
    lines += ["", "Các trang mục lục (Papers, Concepts…), Home và Review Queue được ẩn khỏi graph để graph chỉ "
              "hiện quan hệ thật giữa bài báo và kiến thức. Xem/sửa bộ lọc trong Graph view → Filters.", ""]
    _write_index(config.vault_dir(vault) / MAPS / f"{LEGEND_NOTE}.md", "\n".join(lines) + "\n")


def write_research_views(con, vault):
    """_Maps/Results and media (the old Research map / Views are replaced by _Maps/Index)."""
    vdir=config.vault_dir(vault)
    papers=con.execute("SELECT p.* FROM papers p JOIN paper_vaults v USING(paper_id) WHERE v.vault=? ORDER BY p.year DESC",(vault,)).fetchall()
    results=[INDEX_MARK,"# Results and media","","[[Index]]","", "Each link opens the paper's results or figures/tables. Reviews are secondary evidence.",""]
    from . import find
    for row in papers:
        kind=find.effective_evidence_type(dict(row))
        results.append(f"- [[{row['note_name']}#{PAPER_RESULTS_HEADING}|{row['note_name']}]] — {kind}; [[{row['note_name']}#{PAPER_FIGURES_HEADING}|figures, tables and source regions]]")
    _write_index(vdir/MAPS/"Results and media.md","\n".join(results)+"\n")


def write_indexes(con, vault):
    from . import vault_layout
    vault_layout.require_current(vault)
    vdir = config.vault_dir(vault)
    for folder in FOLDERS:
        (vdir / folder).mkdir(parents=True, exist_ok=True)
    # Mark only byte-recognized, untouched machine templates. Keep a recoverable
    # copy before adding the property; no human writing is rewritten or moved.
    for folder in NODE_FOLDERS:
        for path in (vdir / folder).glob("*.md"):
            old = path.read_text(encoding="utf-8")
            if _is_pure_stub(folder, path.stem, old) and "\nkb_stub: true\n" not in old:
                history = config.STORE / "stub_history" / config.now().replace(":", "-") / vault / folder / path.name
                history.parent.mkdir(parents=True, exist_ok=True)
                history.write_text(old, encoding="utf-8", newline="\n")
                path.write_text(old.replace("---\n", "---\nkb_stub: true\n", 1), encoding="utf-8", newline="\n")

    rows = con.execute(
        """SELECT p.* FROM papers p JOIN paper_vaults v USING(paper_id)
           WHERE v.vault = ? ORDER BY p.year DESC, p.first_author""", (vault,)).fetchall()
    lines = [INDEX_MARK, "# Papers", "", f"{len(rows)} papers in this vault.", "",
             "| Paper | Year | Journal | Classification | Analysed |", "|---|---|---|---|---|"]
    for r in rows:
        journal = (r["journal"] or "").replace("|", "/")
        lines.append(f"| [[{r['note_name']}]] | {r['year'] or ''} | {journal} | {r['classification']} | "
                     f"{'yes' if r['analysis_status'] == 'done' else 'no'} |")
    _write_index(vdir / "Papers" / "Papers.md", "\n".join(lines) + "\n")

    # Only this vault's items (or items without a paper); link a note only when it exists here.
    from . import review_policy
    items = review_policy.pending(con, vault)
    lines = [INDEX_MARK, "# Review Queue", "",
             "Items without a supported connection to this vault topic. Evidence quality is shown in paper details.", ""]
    if not items:
        lines.append("*Nothing to review.*")
    for it in items:
        lines.append(f"- [[{it['note_name']}]]" if it["note_name"] and (vdir / "Review Queue" / f"{it['note_name']}.md").is_file()
                     else f"- {it['kind']}: {it['detail']}")
    _write_index(vdir / "Review Queue" / "Review Queue.md", "\n".join(lines) + "\n")
    from . import review_digest
    review_digest.write(con, vault)  # Cần duyệt.md at the vault root: the four-group queue (RQ-01..RQ-03, RQ-06)

    for folder, list_name in LIST_NOTES.items():
        notes = sorted(p.stem for p in (vdir / folder).glob("*.md") if p.stem != list_name
                       and not _is_pure_stub(folder, p.stem, p.read_text(encoding="utf-8")))
        lines = [INDEX_MARK, f"# {list_name}", ""]
        lines += [f"- [[{n}]]" for n in notes] or ["*Empty for now.*"]
        _write_index(vdir / folder / f"{list_name}.md", "\n".join(lines) + "\n")
    for source_folder, list_name in MINE_LISTS.items():
        source = vdir / source_folder
        names = sorted(p.stem for p in source.glob("*.md") if p.stem not in {
            "My Ideas", "Open Questions", "Knowledge Gaps", list_name
        }) if source.is_dir() else []
        lines = [INDEX_MARK, f"# {list_name}", "",
                 "Human-owned Mine/ content; this is a generated navigation catalog in _Maps.", ""]
        lines += [f"- [[{source_folder}/{n}|{n}]]" for n in names] or ["*Empty for now.*"]
        _write_index(vdir / MAPS / f"{list_name}.md", "\n".join(lines) + "\n")
    write_graph_settings(vault)
    write_research_views(con, vault)
    vault_layout.write_maps(vault)


REVIEW_TEXT = {
    "topic_relevance": "No supported connection to the vault topic was found. Retained as POTENTIAL_BRIDGE; the researcher may keep or archive it.",
    "duplicate_file": "Tệp này giống hệt (cùng nội dung) một bài đã có trong thư viện. "
                      "Bản trùng đã được chuyển tạm sang thư mục review, KHÔNG bị xóa. "
                      "Nếu đồng ý, bạn có thể tự xóa bản trùng đó.",
    "failed_pdf": "Không mở hoặc đọc được PDF này. Tệp đã được chuyển tạm sang thư mục review, KHÔNG bị xóa.",
    "needs_ocr": "PDF gần như không có chữ (có thể là bản scan). Bài đã được lưu nhưng nội dung chưa tìm kiếm được. Cần OCR.",
    "low_text": "Chỉ trích xuất được ít chữ từ PDF này. Kết quả tìm kiếm với bài này có thể thiếu.",
    "metadata_uncertain": "Chưa xác minh chắc chắn được thông tin bài (tên, tác giả, năm, DOI). "
                          "Hãy mở ghi chú của bài và kiểm tra lại.",
    "classification": "AI đánh giá bài này là UNCERTAIN hoặc OUT_OF_SCOPE. Bạn quyết định giữ hay lưu trữ.",
}


def render_review_note(con, it):
    """Pure original machine template, used for byte-recognized reversible retirement."""
    item_id = it["item_id"]
    paper_link = ""
    if it["paper_id"]:
        p = con.execute("SELECT note_name FROM papers WHERE paper_id = ?", (it["paper_id"],)).fetchone()
        if p and p["note_name"]:
            paper_link = f"**Related paper:** [[{p['note_name']}]]\n\n"
    body = (f"---\ntype: question\nreview_item: {item_id}\nkind: {it['kind']}\nstatus: pending\ntags: [kb-index]\n"
            f"decision: \ncreated: {it['created_at']}\n---\n{INDEX_MARK}\n"
            f"# Review {item_id:04d}: {it['kind']}\n\n"
            f"{REVIEW_TEXT.get(it['kind'], '')}\n\n{paper_link}"
            f"**Details:** {it['detail']}\n\n"
            + (f"**File:** `{it['file_path']}`\n\n" if it["file_path"] else "")
            + "Để quyết định: ghi vào dòng `decision:` ở trên (ví dụ `keep`, `archive`, `delete`) "
              "hoặc nói với AI agent. Không có gì bị xóa khi chưa có sự đồng ý của bạn.\n")
    return body


def write_review_note(con, item_id, vault):
    it = con.execute("SELECT * FROM review_items WHERE item_id = ?", (item_id,)).fetchone()
    name = safe_filename(f"Review {item_id:04d} - {it['kind']}")
    body = render_review_note(con, it)
    path = config.vault_dir(vault) / "Review Queue" / f"{name}.md"
    base, suffix = name, 1
    while path.exists():
        old = path.read_text(encoding="utf-8")
        if re.search(rf"(?m)^review_item:[ \t]*{item_id}[ \t]*$", old):
            con.execute("UPDATE review_items SET note_name = ? WHERE item_id = ?", (name, item_id))
            return  # An existing review note can contain human decisions/writing.
        name = safe_filename(f"{base} ({suffix})")
        path = config.vault_dir(vault) / "Review Queue" / f"{name}.md"
        suffix += 1
    _write_index(path, body)
    con.execute("UPDATE review_items SET note_name = ? WHERE item_id = ?", (name, item_id))


def _selfcheck():
    """Windows-safe names; index notes keep text a person added (new and pre-marker formats)."""
    import tempfile
    assert safe_filename("Foo et al.") == "Foo et al" and safe_filename(".NET") == "NET"
    assert safe_filename("CON") == "CON_" and safe_filename("a: b?") == "a b"
    assert safe_filename("x" * 89 + ". yz").endswith("x")
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "My Ideas.md"
        _write_index(path, f"{INDEX_MARK}\n# My Ideas\n\n*Empty for now.*\n")
        path.write_text(path.read_text(encoding="utf-8") + "\nmy idea\n", encoding="utf-8", newline="\n")
        _write_index(path, f"{INDEX_MARK}\n# My Ideas\n\n- [[Idea one]]\n")
        text = path.read_text(encoding="utf-8")
        assert "my idea" in text and "- [[Idea one]]" in text and "*Empty" not in text, text
        path.write_text(f"{INDEX_FRONTMATTER}{INDEX_MARK}\n# My Ideas\n\n- [[Old]]\nlegacy idea\n", encoding="utf-8", newline="\n")
        _write_index(path, f"{INDEX_MARK}\n# My Ideas\n\n- [[Idea one]]\n")
        text = path.read_text(encoding="utf-8")
        assert "legacy idea" in text and "[[Old]]" not in text and text.count(AUTO_START) == 1, text
    return 7


def refresh_all(con, vault):
    """Rewrite every paper note's auto block and all indexes for a vault."""
    from . import vault_layout
    vault_layout.require_current(vault)
    _clear_note_cache(vault)
    registry = concepts.load_registry(con)
    shown = vault_gate(con, registry, vault)  # one lookup for the whole run
    count = 0
    for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ?", (vault,)).fetchall():
        meta = read_json(paper_dir(r["paper_id"]) / "metadata.json")
        if meta:
            # Rewrite machine links first, so old pure stubs become unlinked before
            # the reversible tidy pass evaluates them.
            write_paper_note(meta, vault, ensure_stubs=False, con=con, registry=registry, shown=shown)
            count += 1
    tidy_vault(con, vault, dry_run=False, registry=registry)
    ensure_linked_stubs(con, vault, registry, shown)
    for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ?", (vault,)).fetchall():
        meta = read_json(paper_dir(r["paper_id"]) / "metadata.json")
        if meta:
            ensure_analysis_stubs(meta, vault, registry, shown)
    write_indexes(con, vault)
    # Results/claims/conflicts depend on the concept links and indexes above;
    # result_notes also refreshes the two optional Bases files.  Rebuild the
    # folder indexes once more so the newly generated notes are listed.
    from . import result_notes
    result_notes.refresh(con, vault)
    write_indexes(con, vault)
    return count
