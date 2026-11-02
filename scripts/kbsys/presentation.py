"""Deterministic display categories and source-grounded cards; no scientific inference."""
import contextlib
import contextvars
import functools
import re

COLORS = {"chemical": "#D97706", "chemical_class": "#6366F1", "biological_entity": "#9333EA",
          "method": "#0891B2", "concept": "#16A34A", "topic": "#475569",
          "species": "#A0522D", "cell_type": "#0D9488", "paper": "#4A90E2", "molecule": "#64748B", "unknown": "#475569"}
# These are presentation mappings requested by the researcher, not definitions or evidence.
DISPLAY_KINDS = {"eg": "chemical", "ethylene glycol": "chemical", "dmso": "chemical",
                 "dimethyl sulfoxide": "chemical", "cpa": "chemical_class", "cpas": "chemical_class",
                 "cryoprotectant": "chemical_class", "cryoprotectants": "chemical_class",
                 "cryoprotective agents": "chemical_class", "histone": "biological_entity",
                 "histones": "biological_entity"}
TYPE_KINDS = {"molecule": "molecule", "molecules": "molecule", "method": "method",
              "methods": "method", "concept": "concept", "concepts": "concept", "topic": "topic",
              "topics": "topic", "species": "species", "cell_type": "cell_type", "cell_types": "cell_type"}

# ``kb_kind`` remains the scientific/display classification.  ``type`` is the
# small, stable presentation taxonomy used by Obsidian Graph groups.  In
# particular, mapping several entity kinds to ``substance`` is not a scientific
# reclassification; it only prevents the graph from growing a colour per synonym.
GRAPH_TYPES = {
    "paper": "paper", "method": "method", "protocol": "method", "technique": "technique",
    "chemical": "substance", "chemical_class": "substance",
    "biological_entity": "substance", "molecule": "substance",
    "species": "organism", "organism": "organism", "cell_type": "cell_type",
    "concept": "concept", "topic": "moc",  # topic notes are the MOC hubs in _Maps/ (D3.44)
}

# Species words inside these reagent/antibody names are materials, not the
# organism model.  Keep this list deliberately small and explicit: a normal
# phrase such as ``bovine embryos`` must remain a species link.
SPECIES_REAGENT_PHRASES = (
    "fetal bovine serum", "foetal bovine serum", "fetus bovine serum", "bovine serum albumin", "human serum albumin",
    "human chorionic gonadotropin", "human recombinant", "goat anti",
    "rabbit anti", "mouse anti", "donkey anti", "horse serum",
    "fbs", "bsa", "hsa", "hcg",
)
_SPECIES_REAGENT_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(re.escape(value) for value in SPECIES_REAGENT_PHRASES) + r")(?!\w)", re.I)


def graph_type_for_kind(kind):
    """Return the machine-owned note type used by the fixed graph palette."""
    return GRAPH_TYPES.get(str(kind or "").strip().casefold(), "")


def species_reagent_spans(text):
    """Return spans where an organism word names a reagent or antibody host."""
    return [(match.start(), match.end()) for match in _SPECIES_REAGENT_RE.finditer(str(text or ""))]

@functools.lru_cache(maxsize=None)
def _term_regex(name):
    """Compiled token matcher per distinct name (re's own cache holds 512, graph.build needs ~2500)."""
    return re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", 0 if re.fullmatch(r"[A-Z][A-Z0-9]+", name) else re.I)


_HOMONYMS = {}


def homonym_stamp():
    """Changes when config/homonyms.yaml is edited: caches of homonym-filtered results must include it."""
    from . import config
    path = config.CONFIG_DIR / "homonyms.yaml"
    try:
        return (str(path), path.stat().st_mtime_ns)
    except OSError:
        return None


def homonym_blocked(term, text):
    """True when config/homonyms.yaml says this sentence uses `term` (a node name) in another sense."""
    from . import config
    stamp = homonym_stamp()
    if stamp is None:
        return False
    if stamp not in _HOMONYMS:
        _HOMONYMS.clear()
        rules = config._read_yaml("homonyms.yaml")
        rules = rules if isinstance(rules, dict) else {}
        _HOMONYMS[stamp] = {str(name).strip().casefold(): [re.compile(p, re.I) for p in patterns or ()]
                            for name, patterns in rules.items()}
    patterns = _HOMONYMS[stamp].get(str(term or "").strip().casefold())
    return bool(patterns) and any(p.search(str(text or "")) for p in patterns)


def body_sentence(row):
    """A sentence a term may be linked from: not references/front matter, not a table cell outside a table row."""
    return not (row.get("section") in ("references", "front_matter") or row.get("in_table") and row.get("kind") != "table_row")


def matches_term(text, term, aliases=()):
    """Match complete tokens, including short acronyms, never arbitrary substrings."""
    text = str(text or "")
    if homonym_blocked(term, text):
        return False
    for name in (term, *aliases, *({"CPA": ("CPAs",), "Histone": ("histones",)}.get(str(term), ()))):
        if name and _term_regex(str(name).strip()).search(text):
            return True
    return False

def roles_for_unit(term, unit, aliases=()):
    # A structured field can establish a contextual role only if the term also occurs in
    # the copied source. AI term tags, statement prose and co-occurrence are insufficient.
    if not matches_term(unit.get("quote"), term, aliases):
        return []
    roles = []
    for role, text in (("intervention", (unit.get("intervention") or {}).get("agent")),
                       ("measured_marker", (unit.get("outcome") or {}).get("measure")),
                       ("model", " ".join(str(v) for v in (unit.get("model") or {}).values() if v))):
        if matches_term(text, term, aliases):
            roles.append(role)
    return roles or ["source_mention"]

def metadata_for_term(term, term_type=None, units=(), aliases=()):
    kind = DISPLAY_KINDS.get(str(term).strip().casefold()) or TYPE_KINDS.get(term_type, "unknown")
    if re.fullmatch(r"(?:histone\s+)?H[1-4](?:[AB])?(?:[.][0-9]+)?(?:K[0-9]+(?:me[123]?|ac))?",str(term).strip(),re.I):
        kind='biological_entity'
    role_evidence = {}
    for unit in units:
        for role in roles_for_unit(term, unit, aliases):
            role_evidence.setdefault(role, []).append(unit.get("unit_id"))
    roles = sorted(role_evidence)
    return {"entity_kind": kind, "roles": roles, "role_evidence": role_evidence,
            "category_basis": "presentation mapping; not a scientific definition",
            "color": COLORS[kind], "tags": ["kb-kind/" + kind] + ["kb-role/" + r for r in roles]}

def _fields(data, names):
    return "; ".join(str(data[n]) for n in names if data.get(n) is not None and data.get(n) != "") or "not annotated"

def evidence_card(unit, paper=None, term=None, aliases=()):
    """JSON-safe card, with exact stored statement/quote and explicit absent fields."""
    paper = paper or unit.get("paper") or {}
    outcome = unit.get("outcome") or {}
    # Figure-only values must not be converted into an extracted numeric result.
    shown = dict(outcome)
    if outcome.get("value_source") == "figure_only":
        shown["values"] = "figure only; verify original"
    return {"unit_id": unit.get("unit_id"), "paper_id": unit.get("paper_id") or paper.get("paper_id"),
            "paper_title": paper.get("title"), "note_name": paper.get("note_name"),
            "doi": paper.get("doi"), "pmid": paper.get("pmid"),
            "evidence_type": paper.get("evidence_type") or "not annotated",
            "unit_type": unit.get("unit_type") or "not annotated", "status": unit.get("status"),
            "statement": unit.get("statement") or "", "quote": unit.get("quote") or "",
            "roles": roles_for_unit(term, unit, aliases) if term else [],
            "intervention": _fields(unit.get("intervention") or {}, ("agent", "dose", "timing", "duration")),
            "model": _fields(unit.get("model") or {}, ("species", "cell_type", "origin")),
            "outcome": _fields(shown, ("measure", "direction", "values", "p", "n")),
            "pages": unit.get("pages") or [], "sections": unit.get("sections") or [],
            "sentence_ids": unit.get("sentence_ids") or [], "flags": unit.get("flags") or []}

def note_summary(units, limit=3, term=None, aliases=()):
    """Select a few accepted stored results; never generate or shorten scientific prose."""
    eligible = [u for u in units if u.get("status") in ("ok", "human_confirmed") and u.get("statement")]
    priority = {"result": 0, "conclusion": 1, "method_detail": 2}
    eligible.sort(key=lambda u: (not bool(set(roles_for_unit(term,u,aliases)) & {"intervention","measured_marker"}) if term else False,not matches_term(u.get("statement"),term,aliases) if term else False,priority.get(u.get("unit_type"),3),u.get("unit_id","")))
    return [evidence_card(u) for u in eligible[:limit]]

def machine_properties(metadata, units, term=None, aliases=()):
    summary = note_summary(units,1,term,aliases)
    values = {"kb_kind": metadata["entity_kind"], "kb_roles": metadata["roles"],
            "kb_category_basis": metadata["category_basis"],
            "kb_summary": summary[0]["statement"] if summary else "",
            "evidence_count": len(units), "source_papers": sorted({u["paper_id"] for u in units if u.get("paper_id")})}
    graph_type = graph_type_for_kind(metadata.get("entity_kind"))
    if graph_type:
        values["type"] = graph_type
    return values

def evidence_table(units, term=None, aliases=()):
    def cell(value):
        return str(value or "not annotated").replace("|", "\\|").replace("\n", " ").replace("\r", " ")
    lines = ["| Paper / source | Role | Intervention | Model | Outcome | Stored statement | Evidence / page |",
             "|---|---|---|---|---|---|---|"]
    for u in units:
        c = evidence_card(u, term=term, aliases=aliases)
        paper = f"[[{c['note_name']}|{c['paper_title'] or c['note_name']}]]" if c["note_name"] else c["paper_id"]  # `cell` escapes the pipe
        source = f"{c['unit_type']} / {c['evidence_type']}"
        reference = cite_label(u.get("paper") or {"paper_id": c["paper_id"]}, c["pages"], [c["unit_id"]],  # the unit ID recovers its sentence IDs
                               paper_id=c["paper_id"], page_only=True) or "p. not annotated"
        lines.append("| " + " | ".join(cell(v) for v in (f"{paper} · {source}", ", ".join(c["roles"]),
                     c["intervention"], c["model"], c["outcome"], c["statement"], reference)) + " |")
    return lines


# ---------------------------------------------------------------------------------------------------------------
# Reader-facing citations.  The reader sees "(Author Year, p. N)" linked to the paper note; the exact sentence /
# evidence IDs go into ONE collapsed "Source IDs" callout at the end of the note (docs/CONTRACTS.md "Citations").
# Every generator uses these helpers; none formats an ID itself.
# ---------------------------------------------------------------------------------------------------------------
SOURCE_IDS_TITLE = "Source IDs"
# Machine-written wording of node notes (Concepts, Substances, Methods, Organisms, MOC, Results, Claims). English only: VN-08
# (config/standard.yaml) allows Vietnamese only in list/navigation notes and paper-note headings. One place for all of it.
TEXT_HEDGED = "⚠️ Hedged wording: "                      # + "may/suggest"
TEXT_NO_DIFFERENCE = "no significant difference — does not prove no effect"
TEXT_SECONDARY = "review — secondary"
TEXT_FROM_PAPER = " (from paper)"                       # species / stage taken from the paper's model, not from the unit itself
TEXT_NOT_FOUND = "not found in the current knowledge base"
TEXT_SENTENCES = " ({count} sentence{s})"
TEXT_MORE_PAPERS = "- … and {count} more paper{s}"
TEXT_CONFLICT_DETAIL = "Details: [[_Maps/Open conflicts]]."
TEXT_CONFLICT_HEAD = "> [!conflict] Differing results across papers: {count} — review"
TEXT_CONFLICT_MORE = "> + more pairs: {count} — see [[_Maps/Open conflicts]]"
TEXT_NEW_EVIDENCE = "> [!warning] New evidence from other papers ({count}) since the card was written — consider updating the card"
TEXT_QUALITY = "**Quality / review:**"
TEXT_VARIANTS_HIDDEN = "> Protocol variants not shown: {count}."
TEXT_RESULTS_HIDDEN = "> Result groups not shown: {count}."
TEXT_IMAGE_MISSING = "image not in the PDF — see the original ({link})"
TEXT_PAGE_LINK, TEXT_SEE_ORIGINAL = "p. {page}", "see the original"
TEXT_IMAGE_READ = "- read from the image by AI — check the original: {summary}"
TEXT_IMAGE_MISMATCH = "[!warning] {flag} — check the original"
TEXT_IMAGE_INCONSISTENT = "Image reading does not match the source text"
# A machine ID: "paper_id:S0123" / ":E0077" / ":F3" / ":TS5" / ":Mc0ffee..." or a bare S0123 / E0077.
MACHINE_ID = re.compile(r"\b[a-z]+\d{4}_[a-z0-9_]+:(?:[SE]\d+|[FT]S?\d+|M[0-9a-f]{16})\b|\b[SE]\d{4}\b")
_SOURCE_IDS = contextvars.ContextVar("kb_source_ids", default=None)


@contextlib.contextmanager
def collect_source_ids():
    """Collect every ID cited while a note body is rendered: ``with collect_source_ids() as ids: ...``."""
    ids = {}
    token = _SOURCE_IDS.set(ids)
    try:
        yield ids
    finally:
        _SOURCE_IDS.reset(token)


def note_source_ids(paper_id, sentence_ids, label):
    """Register IDs (bare ``S0123`` are qualified with ``paper_id``) under their reader-facing citation."""
    store = _SOURCE_IDS.get()
    if store is None:
        return
    for sid in sentence_ids or ():
        sid = str(sid or "").strip()
        if sid:
            store.setdefault(sid if ":" in sid or not paper_id else f"{paper_id}:{sid}", label)


def author_year(meta):
    """``Smith 2026`` from paper metadata (or a profile paper dict that already carries ``cite``)."""
    meta = meta or {}
    if meta.get("cite"):
        return str(meta["cite"]).strip()
    authors = meta.get("authors") or []
    first = authors[0] if authors else meta.get("first_author")
    if isinstance(first, dict):
        first = first.get("family") or first.get("name")
    first = first or meta.get("note_name") or meta.get("paper_id") or "Unknown"
    return f"{first} {meta.get('year') or 'n.d.'}"


def _pages(pages):
    pages = [str(p) for p in (pages if isinstance(pages, (list, tuple, set)) else [pages]) if p not in (None, "", "?")]
    pages = list(dict.fromkeys(pages))
    return ("p. " if len(pages) == 1 else "pp. ") + ", ".join(pages) if pages else ""


def cite_label(meta, pages=None, ids=(), paper_id=None, page_only=False):
    """``Smith 2026, p. 4`` (``p. 4`` with ``page_only``); registers ``ids`` under the full label."""
    page = _pages(pages)
    full = ", ".join(x for x in (author_year(meta), page) if x)
    note_source_ids(paper_id or (meta or {}).get("paper_id"), ids, full)
    return (page or full) if page_only else full


def cite(meta, pages=None, ids=(), paper_id=None, link=True):
    """``[[Paper note|Smith 2026, p. 4]]`` (plain label without ``link`` or without a note); registers ``ids``."""
    label = cite_label(meta, pages, ids, paper_id)
    name = (meta or {}).get("note_name")
    return f"[[{name}|{re.sub(r'[][|]', '', label)}]]" if link and name else label  # no ] or | inside a link alias


def source_ids_callout(ids):
    """The one discreet place for exact IDs: a collapsed callout, ID lines grouped by citation (empty -> nothing)."""
    groups = {}
    for sid, label in (ids or {}).items():
        groups.setdefault(label, []).append(sid)
    if not groups:
        return []
    out = [f"> [!info]- {SOURCE_IDS_TITLE}", "> Exact sentence / evidence IDs behind the citations in this note."]
    out += [f"> - {label}: " + ", ".join(sorted(group)) for label, group in groups.items()]
    return ["", *out, ""]


def machine_ids(text):
    """Machine IDs visible in a note's headings and body, ignoring front matter and the Source IDs callout."""
    text = re.sub(r"\A---\n.*?\n---\n", "", str(text or ""), count=1, flags=re.S)
    lines, skip = [], False
    for line in text.splitlines():
        if line.startswith(f"> [!info]- {SOURCE_IDS_TITLE}"):
            skip = True
            continue
        if skip and line.startswith(">"):
            continue
        skip = False
        lines.append(line)
    body, found = "\n".join(lines), []
    for match in MACHINE_ID.finditer(body):
        # a bare S1888 right after "Cat. No." / "#" is a reagent catalogue number in copied source text, not an ID
        if ":" not in match.group(0) and re.search(r"(?:\bno\.?|\bcat\.?|catalog(?:ue)?|\bref\.?|#)\s*$",
                                                   body[max(0, match.start() - 14):match.start()], re.I):
            continue
        found.append(match.group(0))
    return found


# PDF text carries superscript artefacts: citation markers ("machinery^51,", "cooling^1,5,6"), a caret inside a name
# ("m^6A") and caret unit exponents ("ml^-1").  Titles / headings / summaries are cleaned with clean_pdf_text();
# quoted evidence is never touched (it stays verbatim) - has_pdf_artefact() only counts such quotes.
_CARET_NAME = re.compile(r"(?<=[A-Za-z])\^(?=\d+[A-Za-z])")
_CARET_EXPONENT = re.compile(r"\^(?=[−-]\d)")
_CARET_CITATION = re.compile(r"(?<![\d.])\^\d+(?:\s*[,–-]\s*\d+)*(?!\w)")


def clean_pdf_text(text):
    """``m^6A`` -> ``m6A``, ``ml^−1`` -> ``ml−1``, ``machinery^51,`` -> ``machinery,``; µ / μ stay as in the source."""
    text = _CARET_CITATION.sub("", _CARET_EXPONENT.sub("", _CARET_NAME.sub("", str(text or ""))))
    return text


def has_pdf_artefact(text):
    return clean_pdf_text(text) != str(text or "")


def object_title(kind, object_id, label=None, caption="", meta=None, page=None):
    """Reader heading for a figure / table / supplementary item: ``Figure S3 — caption start (Smith 2026)``."""
    tail = str(object_id or "").rsplit(":", 1)[-1]
    found = re.fullmatch(r"([FT])(S?)(\d+)", tail)
    if found:
        name = f"{'Figure' if found[1] == 'F' else 'Table'} {found[2]}{found[3]}"
    elif label and not MACHINE_ID.search(str(label)):
        name = str(label)
    else:
        name = {"formula": "Formula", "special_symbol_region": "Symbol region"}.get(kind, str(kind or "Item").title())
        if page not in (None, ""):
            name += f" (p. {page})"
    start = re.sub(r"^\s*(?:Supplementary\s+)?(?:Fig(?:ure)?|Table)\.?\s*S?\d+[A-Za-z]?\s*[.:|-]?\s*", "",
                   clean_pdf_text(" ".join(str(caption or "").split())), flags=re.I)
    first = re.match(r"(.{12,90}?)[.?!](?=\s+[A-Z(]|\s*$)", start)  # the caption's title sentence, not its panel list
    if first:
        start = first.group(1)
    if len(start) > 70:
        start = start[:70].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    start = re.sub(r"[\[\]|#^]", "", start)
    return name + (f" — {start}" if start else "") + (f" ({author_year(meta)})" if meta else "")


# Machine keys a researcher never uses in Obsidian: kept (not dropped) under ONE nested property.
NODE_MACHINE_KEYS = ("kb_category_basis", "agent_type", "papers_n", "card_status", "conflict_open")


def fold_machine_keys(front):
    """Move the machine-only Properties keys of a node note under a single ``kb_machine`` mapping (idempotent)."""
    folded = dict(front.get("kb_machine") or {}) if isinstance(front.get("kb_machine"), dict) else {}
    for key in NODE_MACHINE_KEYS:
        if key in front:
            folded[key] = front.pop(key)
    if folded:
        front["kb_machine"] = folded
    return front
