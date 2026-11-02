"""Evidence-grounded, regenerable four-part Obsidian Concept Notes."""

import hashlib
import json
import copy
import re
from datetime import datetime
from pathlib import Path

import yaml

from . import answer, concepts, config, db, find, notes, relations, presentation, structure, node_profile, vault_layout
from . import sentences as sentence_utils

try:
    from . import figure_reading
except ImportError:  # D3.51 is optional at render time.
    figure_reading = None


FINGERPRINT = re.compile(r"(?m)^<!-- kb:concept-fingerprint:([0-9a-f]{40}) -->$")
SYNTHESIS_STATE = re.compile(r"(?m)^<!-- kb:synthesis-state:(\{.*\}) -->$")
FORMAT_VERSION = 19  # 19: node and topic (MOC) notes list the result notes that link them (RL-10 A6, "Results" section); 18: English-only machine text, no local dashboard link in the note (VN-08); 17: typed sections (N3d), kb_summary = definition; 16: MOC sections Overview / Member notes / Key papers / Open conflicts
# Version of the synthesis payload fingerprint. Change only when the evidence sent to the AI changes shape;
# bumping it marks every checked synthesis as stale.
SYNTHESIS_PAYLOAD_VERSION = 5
# Provider-level failures (answer.AIError messages): stop calling the AI for the rest of the run.
_AI_UNAVAILABLE_MARKERS = ("Không tìm thấy lệnh", "hết lượt dùng", "chưa đăng nhập", "Không có AI tự chạy",
                           "Thiếu key", "Thiếu tên model", "Chưa ghi lệnh AI", "Không gọi được",
                           "AI chạy qua MCP", "Chưa chọn AI")

MAX_UNITS_PER_PAPER = 8
MAX_UNITS_PER_NOTE = 60
_COLLECT_CACHE = {}


def _stat_key(path):
    return structure._file_stat(path)


def _collect_key(record, registry, vault, concept_ids, paper_ids):
    paper_ids = sorted(set(paper_ids))
    db_path = Path(config.DB_PATH).resolve()
    concept_path = (config.STORE / 'concepts.jsonl').resolve()
    registry_key = hashlib.sha1(json.dumps(registry.records, ensure_ascii=False,
                                           sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return (str(config.STORE.resolve()), str(db_path), _stat_key(db_path),
            str(concept_path), _stat_key(concept_path), vault, record.get('concept_id'),
            json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
            tuple(sorted(concept_ids)), registry_key, presentation.homonym_stamp(),
            tuple((paper_id, structure.paper_read_stamp(paper_id)) for paper_id in paper_ids))
SUMMARY_LIMIT = 160


def _summary_text(text):
    """A summary candidate: one clean sentence that starts like a sentence, or ''."""
    text = " ".join(presentation.clean_pdf_text(text).split())
    if len(text.split()) < 4 or text[:1].islower() or "\ufffd" in text:
        return ""
    return _trim(text, SUMMARY_LIMIT)


def _source_summary(record, units, profile=None):
    """``kb_summary``: the accepted node card's definition, else the first definitional sentence about THIS node
    (a profile field or a source sentence of its evidence units), cut at a word boundary; '' (key omitted) otherwise.
    A sentence that merely contains the name is not a summary."""
    term, aliases = record.get("name", ""), record.get("synonyms") or []
    profile = profile or {}
    for row in _statement_rows(profile, profile.get("card") or {}, ("definition", "definitions")):
        if _summary_text(row["text"]) and not presentation.machine_ids(row["text"]):   # kb_summary reaches Index bodies (VN-03)
            return _summary_text(row["text"])
    for paper in profile.get("papers") or []:
        for field in ("definitions", "source_classes"):
            for ref in ((paper.get("fields") or {}).get(field, {}).get("items") or []):
                if _summary_text(ref.get("text")):
                    return _summary_text(ref["text"])
    for unit in units or ():
        if unit.get("status") not in ("ok", "human_confirmed") or unit.get("sections") in (["methods"], ["results"]):
            continue
        for fragment in re.split(r"(?<=[.!?])\s+", str(unit.get("quote") or unit.get("statement") or "")):
            if node_profile.definitional(fragment, term, aliases) and _summary_text(fragment):
                return _summary_text(fragment)
    return ""


def _node_hedge(text):
    """Return uncertainty markers that qualify a finding, not a no-change label."""
    hits = structure.uncertainty(str(text or ""))
    hits = [row for row in hits if row.get("category") != "non_significance"]
    if not hits:
        return ""
    values = "/".join(dict.fromkeys(str(row.get("text")) for row in hits))
    return presentation.TEXT_HEDGED + values


_ALL_TEXT_PATTERNS = {}


def _text_patterns(concept_ids, registry):
    """(concept_id, regex) for every unambiguous name of `concept_ids`; compiled once per registry object."""
    cached = _ALL_TEXT_PATTERNS.get("registry")
    if not cached or cached[0] is not registry:
        _ALL_TEXT_PATTERNS["registry"] = cached = (registry, _all_text_patterns(registry))
    return tuple(item for item in cached[1] if item[0] in concept_ids)


def _all_text_patterns(registry):
    owners = {}
    variants = {}
    for record in registry.records:
        for name in (record["name"], *(record.get("synonyms") or [])):
            name = str(name).strip()
            if len(name) >= 2:
                match = re.search(r"[A-Za-z]+$", name)
                forms = [name]
                if match:
                    plural = concepts._plural_token(match.group(0))
                    if plural != match.group(0):
                        forms.append(name[:match.start()] + plural)
                for form in forms:
                    folded = form.casefold()
                    variants.setdefault(folded, form)
                    owners.setdefault(folded, set()).add(record["concept_id"])
    patterns = []
    for folded, ids in owners.items():
        if len(ids) != 1:
            continue
        patterns.append((next(iter(ids)), concepts._regex(variants[folded],concepts._case_sensitive(variants[folded]))))
    return tuple(patterns)


def _text_concept_ids(unit, concept_ids, registry, extra_text="", patterns=None):
    """Match evidence prose with registry names using whole-word boundaries."""
    text = " ".join([*(str(unit.get(key) or "") for key in ("quote", "statement")), str(extra_text or "")])
    compiled = _text_patterns(concept_ids, registry) if patterns is None else patterns
    return {concept_id for concept_id, pattern in compiled
            if pattern.search(text) and not presentation.homonym_blocked(registry.by_id[concept_id]["name"], text)}


def _unit_priority(unit):
    priority = {"result": 0, "conclusion": 0, "method_detail": 1,
                "interpretation": 2, "limitation": 3}
    values = (unit.get("outcome") or {}).get("values") or []
    return priority.get(unit.get("unit_type"), 4), not bool(values), unit.get("unit_id", "")


def _select_payload_units(units):
    by_paper = {}
    for unit in units:
        by_paper.setdefault(unit["paper_id"], []).append(unit)
    ordered = {paper_id: sorted(by_paper[paper_id], key=_unit_priority)[:MAX_UNITS_PER_PAPER]
               for paper_id in sorted(by_paper)}
    selected = []
    for rank in range(MAX_UNITS_PER_PAPER):
        for paper_id in ordered:
            if rank < len(ordered[paper_id]):
                selected.append(ordered[paper_id][rank])
                if len(selected) == MAX_UNITS_PER_NOTE:
                    return selected
    return selected
def _descendants(registry, root_id):
    out, todo = {root_id}, [root_id]
    while todo:
        parent_id = todo.pop()
        for record in registry.records:
            if record["concept_id"] not in out and parent_id in (record.get("parents") or []):
                out.add(record["concept_id"])
                todo.append(record["concept_id"])
    return out


def _linked_papers(con, concept_ids, vault):
    if not concept_ids:
        return []
    marks = ",".join("?" for _ in concept_ids)
    args = sorted(concept_ids)
    sql = ("SELECT cl.concept_id, cl.paper_id, cl.level, cl.n_sentences, cl.sections, cl.first_sentence_id "
           "FROM concept_links cl JOIN paper_vaults pv ON pv.paper_id = cl.paper_id "
           f"WHERE cl.concept_id IN ({marks}) AND pv.vault = ? ORDER BY cl.paper_id, cl.concept_id")
    return [dict(r) for r in con.execute(sql, [*args, vault])]


# Researcher decision 13/10 (option C): one shared concept registry, but a node gets a note in a vault only when
# that vault's papers have enough evidence units or mentions (a passing mention in one paper of another topic does not
# create a node; it stays findable by search). The numbers are the params of ND-05, applied by `concepts.meets`.


def shown_in_vault(links, units):
    """Whether a node passes the vault threshold. `links`: list of concept_links rows, or {paper_id: [rows]}."""
    rows = [r for group in links.values() for r in group] if isinstance(links, dict) else list(links or ())
    return concepts.meets(len(units or ()), sum(int(r.get("n_sentences") or 0) for r in rows))


def _collect(con, record, registry, vault):
    concept_ids = _descendants(registry, record["concept_id"])
    links = _linked_papers(con, concept_ids, vault)
    vault_papers = {row[0] for row in con.execute(
        "SELECT paper_id FROM paper_vaults WHERE vault=?", (vault,))}
    cache_key = _collect_key(record, registry, vault, concept_ids,
                             vault_papers | {row['paper_id'] for row in links})
    cached = _COLLECT_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    by_paper = {}
    for link in links:
        by_paper.setdefault(link["paper_id"], []).append(link)
    # Legacy/manual term tags may have direct evidence units even when the full-text
    # matcher did not produce a sentence link. Preserve those paper/evidence rows.
    for row in con.execute("SELECT pt.paper_id, pt.term FROM paper_terms pt JOIN paper_vaults pv "
                           "ON pv.paper_id = pt.paper_id WHERE pv.vault = ?", (vault,)):
        matched = registry.lookup(row["term"])
        if matched and matched["concept_id"] in concept_ids:
            by_paper.setdefault(row["paper_id"], [])
    units = []
    text_patterns = _text_patterns(concept_ids, registry)
    for paper_id in sorted(by_paper):
        linked_sentence_text = {}
        raw_sentences = {}
        for sentence in structure.paper_rows(paper_id, "sentences.jsonl"):
            if sentence.get("section") == "references" or not (
                    sentence.get("kind") == "table_row" or not sentence.get("in_table")):
                continue
            sid=sentence.get("sentence_id") or sentence.get("id")
            linked_sentence_text[sid] = sentence.get("text") or ""
            raw_sentences[sid] = sentence
        if not any(pattern.search(text) for text in linked_sentence_text.values() for _,pattern in text_patterns):
            by_paper.pop(paper_id, None)
            continue
        for unit in structure.paper_rows(paper_id, "evidence.jsonl"):
            wanted,_ = node_profile._source_ids(unit.get("sentence_ids"), raw_sentences, paper_id=paper_id)
            sentence_text = " ".join(linked_sentence_text.get(sid, "") for sid in linked_sentence_text if sid in wanted)
            # AI tags alone do not establish a source match (e.g. generic fallback
            # Vitrification/GO or EG inside epigenetics).
            source_unit = {"quote": sentence_text}
            if _text_concept_ids(source_unit, concept_ids, registry, sentence_text, text_patterns) & concept_ids:
                item = dict(unit)
                item["quote"] = sentence_text
                item["paper_id"] = paper_id
                item["paper"] = db.read_json(db.paper_dir(paper_id) / "metadata.json", {})
                from . import find
                item["paper"]["evidence_type"] = find.effective_evidence_type(item["paper"])
                units.append(item)
    result = (concept_ids, by_paper, units)
    _COLLECT_CACHE[cache_key] = result
    return copy.deepcopy(result)


def _payload_fingerprint(record, units, language, provider, model):
    """Fingerprint only the evidence sent to synthesis and its generation settings."""
    selected = [u for u in units if u.get("status") in ("ok", "human_confirmed")]
    selected = _select_payload_units(selected)
    payload = {"format_version": SYNTHESIS_PAYLOAD_VERSION, "concept": record["name"], "language": language,
               "provider": provider, "model": model,
               "units": [{k: u.get(k) for k in ("unit_id", "statement", "quote", "unit_type",
                                                   "hedging", "outcome", "model", "intervention",
                                                   "sections", "pages")}
                         for u in selected]}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _fingerprint(record, concept_ids, links, units, gaps, synthesis_state, synthesis=None, questions=None):
    payload = {"format_version": FORMAT_VERSION, "record": record, "concept_ids": sorted(concept_ids), "links": links,
               "gaps": gaps, "synthesis": synthesis_state,
               "synthesis_rows": synthesis or [], "questions": questions or [],
               "units": [{k: u.get(k) for k in ("unit_id", "status", "statement", "quote", "terms", "outcome",
                                                  "model", "intervention", "sections", "pages")}
                         for u in units]}
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _synthesis(record, units, language="vi"):
    if not units:
        return [], [], "No structured evidence units; this note lists full-text mentions only.", \
               {"status": "not_applicable", "provider": "none", "model": ""}
    settings = answer.settings("synthesis")
    provider = settings.get("provider", "none")
    model = (settings.get(provider) or {}).get("model", "") if provider != "none" else ""
    return [], [], "Synthesis pending.", {"status": "pending", "provider": provider, "model": model}


def _generate_synthesis(record, units, language="vi"):
    """Send only approved evidence units for this concept to the configured provider."""
    eligible = [u for u in units if u.get("status") in ("ok", "human_confirmed")]
    considered_count = len(eligible)
    minimum = min(5, considered_count)
    eligible = _select_payload_units(eligible)
    settings = answer.settings("synthesis")
    provider = settings.get("provider", "none")
    model = (settings.get(provider) or {}).get("model", "") if provider != "none" else ""
    state = {"status": "pending", "provider": provider, "model": model}
    stats = {"passed": 0, "failed": 0, "considered": considered_count, "sent": len(eligible)}
    if not eligible:
        return [], [], ("Synthesis pending: no ok or human_confirmed evidence units are available. "
                        f"Units considered: {considered_count}; sent: 0."), state, stats
    status = answer.provider_status(settings)
    if not status.get("ready"):
        return [], [], (f"Synthesis pending: {status.get('message', 'provider unavailable')}. "
                        f"Units considered: {considered_count}; sent: 0."), state, stats
    items, cards, wire = [], {}, []
    for i, unit in enumerate(eligible, 1):
        paper = unit.get("paper") or {}
        paper_id = unit["paper_id"]
        cards[paper_id] = {"year": paper.get("year"), "evidence_type": paper.get("evidence_type")}
        item = {"kind": "unit", "text": unit.get("quote") or unit.get("statement") or "",
                "statement": unit.get("statement") or "", "unit_type": unit.get("unit_type", "result"),
                "hedging": unit.get("hedging"), "direction": (unit.get("outcome") or {}).get("direction"),
                "paper_id": paper_id, "review_paper": paper.get("evidence_type") in
                ("review", "systematic_review", "meta_analysis"), "cited_refs": []}
        items.append(item)
        wire.append({"evidence_id": unit["unit_id"], "paper_id": paper_id,
                     "paper_title": paper.get("title"), "year": paper.get("year"),
                     "unit_type": unit.get("unit_type"), "statement": unit.get("statement"),
                     "quote": unit.get("quote"), "model": unit.get("model"),
                     "intervention": unit.get("intervention"), "outcome": unit.get("outcome")})
    locale = "Vietnamese" if language == "vi" else "English"
    system = ("You synthesize biomedical evidence strictly from the supplied records. Return JSON only. "
              f"Write synthesis sentences in {locale}; preserve biomedical terms and quoted source text in English. "
              "Never add facts, causal claims, or numbers absent from evidence. Cover every paper represented "
              f"in the supplied records. Return at least {minimum} and at most 15 concise synthesis "
              "sentences, each with evidence as a list of exact evidence_id strings. Also return zero or more "
              "candidate open questions, each with evidence IDs. If evidence cannot support the required minimum, return "
              "only supported sentences. Format: {\"sentences\":[{\"text\":\"...\",\"evidence\":[\"unit-id\"]}],"
              "\"questions\":[{\"text\":\"...\",\"evidence\":[\"unit-id\"]}]}. "
              "Do not put citation markers in sentence text. Frame second-hand/review claims with attribution "
              "and hypotheses as hypotheses.")
    user = json.dumps({"concept": record["name"], "evidence": wire}, ensure_ascii=False)
    try:
        raw, usage = answer._chat(settings, system, user)
        data = answer._json(raw)
        if not isinstance(data, dict):
            raise answer.AIError("Provider reply was not a JSON object")
        by_id = {u["unit_id"]: i + 1 for i, u in enumerate(eligible)}
        failed_rows = []
        def checked_rows(key, rows=None, count_stats=True):
            kept = []
            for row in (data.get(key) if rows is None else rows) or []:
                if not isinstance(row, dict):
                    continue
                ids = row.get("evidence") or []
                converted = [by_id[x] for x in ids if x in by_id] if isinstance(ids, list) else []
                check = answer.check_sentence({"text": row.get("text", ""), "evidence": converted}, items, cards, scope="note")
                if check["check"] == "ok":
                    if count_stats:
                        stats["passed"] += 1
                    # one line per sentence: the note stores each row on its line
                    kept.append({"text": " ".join(str(row["text"]).split()), "evidence": [eligible[n - 1]["unit_id"]
                                                                                  for n in check["evidence"]]})
                else:
                    if count_stats:
                        stats["failed"] += 1
                        if key == "sentences":
                            failed_rows.append({"text": str(row.get("text") or ""), "problems": check["problems"]})
            return kept
        synthesis, questions = checked_rows("sentences"), checked_rows("questions")
        synthesis = synthesis[:15]
        if len(synthesis) < minimum and failed_rows:
            retry_user = json.dumps({"concept": record["name"], "evidence": wire,
                                     "failed_sentences": failed_rows,
                                     "instruction": "Correct each failed sentence using only supplied evidence. Return JSON with sentences and evidence IDs."},
                                    ensure_ascii=False)
            retry_raw, _ = answer._chat(settings, system, retry_user)
            retry_data = answer._json(retry_raw)
            if isinstance(retry_data, dict):
                repaired = checked_rows("sentences", retry_data.get("sentences"), count_stats=False)
                known = {json.dumps(row, sort_keys=True) for row in synthesis}
                synthesis.extend(row for row in repaired if json.dumps(row, sort_keys=True) not in known)
        synthesis = synthesis[:15]
        if len(synthesis) < minimum:
            return synthesis, questions, (f"Synthesis pending: {len(synthesis)} checked sentences passed A1-A6; "
                                          f"{minimum} required. Units considered: {considered_count}; sent: {len(eligible)}."), state, stats
        state.update(status="done")
        return synthesis, questions, (f"Synthesis checked: {len(synthesis)} sentences passed A1-A6. "
                                      f"Units considered: {considered_count}; sent: {len(eligible)}."), state, stats
    except Exception as exc:
        return [], [], (f"Synthesis pending: {type(exc).__name__}: {exc}. "
                        f"Units considered: {considered_count}; sent: {len(eligible)}."), state, stats


def _paper_label(u):
    meta = u["paper"]
    author = (meta.get("authors") or [{}])[0].get("family") or u["paper_id"]
    return f"{author} {meta.get('year') or 'n.d.'}"


def _conflict_signature(units):
    """Relations and measure groups that change the conflict display, for the note fingerprint."""
    ids = {u["unit_id"] for u in units}
    rels = sorted((r.get("relation_id"), r.get("type"), r.get("status"))
                  for r in (db.read_jsonl(config.STORE / "relations.jsonl") or [])
                  if r.get("from_unit") in ids and r.get("to_unit") in ids)
    groups = relations.load_measure_groups()
    grouped = sorted({(u["unit_id"], (relations.measure_group((u.get("outcome") or {}).get("measure"), groups)
                                      or {}).get("group_id")) for u in units})
    return {"relations": rels, "measure_groups": grouped}


def _citation(u):
    """``[[Paper note|Author Year, p. N]]`` for an evidence unit; its unit / sentence IDs go to the Source IDs callout."""
    return presentation.cite({**u["paper"], "paper_id": u["paper_id"]}, u.get("pages"),
                             [u.get("unit_id"), *(u.get("sentence_ids") or [])], u["paper_id"])


def _md_cell(value):
    return " ".join(str(value or "").replace("|", "\\|").replace("\r", " ").replace("\n", " ").split())


# The ONE approved section set per node type (researcher decision 2026-10-12, docs/CONTRACTS.md "Node note
# sections").  Entry = (heading, card fields shown there, profile "fields" used when the card has none there).
# A section with no real data is not written at all.  "My notes" is the human zone after the auto block.
HUMAN_HEADING = "My notes"
_DEFINING = ("definition", "definitions", "class", "classification", "substance_class", "function_or_purpose", "function",
             "purpose", "purpose_scope", "scope", "full_name", "abbreviation")
_FINDINGS = ("importance", "significance", "how_cited", "citation", "cross_paper", "comparison", "same_different",
             "cautions", "caution", "limitations")
_TAIL = (("Conflicting findings", (), ()), ("Figures", (), ()), (notes.RESULTS_HEADING, (), ()))
_ORGANISM_FINDINGS = _FINDINGS + ("interspecies_cautions",)
_METHOD_SECTIONS = (
    ("Purpose", _DEFINING + ("principle", "readout", "measures"), ("definitions", "purpose")),
    ("Protocol steps", ("steps",), ()),
    ("Reagents & concentrations", ("materials", "concentrations"), ()),
    ("Equipment & parameters", ("equipment", "key_parameters"), ()),
    ("Variants across papers", ("variants", "cross_paper", "comparison", "same_different", "parent_technique"), ()),
    ("Outcomes", ("importance", "significance", "how_cited", "citation", "strengths", "limitations", "cautions",
                  "caution"), ()),
) + _TAIL + (("Sources", (), ()),)
NODE_SECTIONS = {
    "concept": (("Definition", _DEFINING, ("definitions", "source_classes")),
                ("Key findings", ("key_points", "points") + _FINDINGS, ()),
                ) + _TAIL + (("Related notes", (), ()), ("Sources", (), ())),
    "method": _METHOD_SECTIONS,
    "technique": _METHOD_SECTIONS,
    "substance": (("Definition", _DEFINING, ("definitions", "source_classes")),
                  ("Concentrations used", ("concentration", "concentrations_seen"), ()),
                  ("Measured effects", _FINDINGS, ()),
                  ) + _TAIL + (("Sources", (), ()),),
    "organism": (("Description", _DEFINING + ("taxon_rank", "common_name", "strain_or_breed", "model_use",
                                              "model_role", "usage", "papers", "use"),
                  ("definitions", "source_classes")),
                 ("Key findings", _ORGANISM_FINDINGS, ()),
                 ) + _TAIL + (("Sources", (), ()),),
    "cell_type": (("Description", _DEFINING + ("developmental_stage", "stage", "in_taxon", "species", "derived_from",
                                               "origin"), ("definitions", "source_classes")),
                  ("Markers", ("markers", "marker"), ("markers",)),
                  ("Key findings", _ORGANISM_FINDINGS, ()),
                  ) + _TAIL + (("Sources", (), ()),),
}
# The per-paper table (paper / species / stage / finding / direction) sits under the type's findings heading.
_FINDINGS_HEADING = {"concept": "Key findings", "organism": "Key findings", "cell_type": "Key findings",
                     "method": "Outcomes", "technique": "Outcomes", "substance": "Measured effects"}


def node_kind(profile, record_type=None, record=None):
    """Section-set key for a node: its graph type, with unknown types read as concept."""
    kind = node_profile.graph_node_type(profile or {}, record_type, record)
    return kind if kind in NODE_SECTIONS else "concept"


def node_headings(kind):
    """Approved H2 headings of a node note, in order, human zone last."""
    return tuple(h for h, _, _ in NODE_SECTIONS.get(kind, NODE_SECTIONS["concept"])) + (HUMAN_HEADING,)


MOC_SECTIONS = ("Overview", "Member notes", "Key papers", "Open conflicts", notes.RESULTS_HEADING)


def heading_problems(text, kind=None):
    """Problems with the H2 headings of a generated node note: unapproved or out of order (hidden is fine).
    ``kind="moc"`` checks a topic hub against MOC_SECTIONS."""
    fm, rest = notes._split_frontmatter(text)
    kind = kind or node_profile.graph_node_type({"entity_kind": (fm or {}).get("type")}, (fm or {}).get("type"))
    allowed = list(MOC_SECTIONS + (HUMAN_HEADING,) if kind == "moc"
                   else node_headings(kind if kind in NODE_SECTIONS else "concept"))
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END)
    block = rest[start:end] if 0 <= start < end else ""
    seen = [m.group(1).strip() for m in re.finditer(r"(?m)^## (.+)$", block)]
    problems, last = [], -1
    for heading in seen:
        if heading not in allowed:
            problems.append(f"unapproved section {heading!r} for type {kind}")
        elif allowed.index(heading) <= last:
            problems.append(f"section {heading!r} out of order for type {kind}")
        else:
            last = allowed.index(heading)
    return problems


def _card_statements(card):
    """Normalize D3.46 card statements while accepting early fixture shapes."""
    if not isinstance(card, dict):
        return []
    rows = []
    statements = card.get("statements") or []
    if isinstance(statements, dict):
        statements = [{"field": key, **(value if isinstance(value, dict) else {"text": value})}
                      for key, value in statements.items()]
    if isinstance(statements, list):
        rows.extend(x for x in statements if isinstance(x, dict))
    fields = card.get("fields") or {}
    if isinstance(fields, dict):
        for field, value in fields.items():
            values = value if isinstance(value, list) else [value]
            for item in values:
                if isinstance(item, dict):
                    row = dict(item)
                    row.setdefault("field", field)
                    rows.append(row)
                elif isinstance(item, str) and item.strip():
                    rows.append({"field": field, "text": item})
    for field, value in card.items():
        if field in {"node_type", "status", "card_status", "fields", "statements"}:
            continue
        values = value if isinstance(value, list) else [value]
        for item in values:
            if isinstance(item, dict) and (item.get("text") or item.get("statement") or item.get("value")):
                row = dict(item)
                row.setdefault("field", field)
                rows.append(row)
    seen, result = set(), []
    for row in rows:
        text = str(row.get("text") or row.get("statement") or row.get("value") or row.get("step")
                   or row.get("use") or "").strip()
        if not text:
            continue
        ids = row.get("sentence_ids") or row.get("evidence") or []
        if isinstance(ids, str):
            ids = [ids]
        ids = tuple(str(x) for x in ids if x)
        key = (text, ids)
        if key in seen:
            continue
        seen.add(key)
        result.append({**row, "text": text, "sentence_ids": list(ids),
                       "field": str(row.get("field") or row.get("section") or "general")})
    return result


def _linked_rows(paper_id):
    """The paper's sentences with page/layout continuations linked (cached per file signature)."""
    path = db.paper_dir(paper_id) / "sentences.jsonl"
    return sentence_utils.cached_linked_rows(db.read_jsonl(path), path)


def _card_refs(profile, sentence_ids):
    wanted = {str(x) for x in sentence_ids or []}
    if not wanted:
        return []
    found = []
    for paper in profile.get("papers", []):
        pid = paper["paper_id"]
        meta = db.read_json(db.paper_dir(pid) / "metadata.json", {}) or {}
        meta["_references"] = {r["num"]: r for r in db.read_jsonl(db.paper_dir(pid) / "references.jsonl")
                                if r.get("num") is not None}
        rows = {str(row.get("sentence_id")): row for row in _linked_rows(pid) if row.get("sentence_id")}
        selected = [sid for sid in rows
                    if sid in wanted or sid.rsplit(":", 1)[-1] in wanted]
        # _refs applies the D3.85/D3.40 continuation join at read time and
        # labels any added context; card quotes must use the same path as node
        # and conflict quotes.
        found.extend(node_profile._refs(selected, rows, pid, meta, linked=True))  # rows are already linked
    return found


def _ref_cite(ref):
    """``[[Paper note|Author Year, p. N]]`` for a copied source sentence (its ID is kept for the Source IDs callout)."""
    meta = db.read_json(db.paper_dir(ref["paper_id"]) / "metadata.json", {}) or {}
    meta = {**meta, "paper_id": ref["paper_id"], "note_name": meta.get("note_name") or ref.get("note_name")}
    return presentation.cite(meta, ref.get("page"), [ref.get("sentence_id") or ref.get("id")], ref["paper_id"])


def _compact_refs(refs):
    return " · ".join(dict.fromkeys(_ref_cite(ref) for ref in refs))


def _statement_rows(profile, card, fields=()):
    allowed = {str(x).casefold() for x in fields}
    return [row for row in _card_statements(card)
            if not allowed or str(row.get("field", "")).casefold() in allowed]


def _cite_bare_ids(text, refs):
    """VN-03: a sentence id of this statement's own sources written in AI card text ("Ko's compaction (S0127)") shows as
    that sentence's citation label; the card on disk stays unchanged."""
    by_id = {}
    for ref in refs:
        sid = _ref_sid(ref)
        for key in {sid, sid.rsplit(":", 1)[-1]} - {""}:
            by_id.setdefault(key, []).append(ref)
    # a bare id that two papers share stays as it is (ambiguous)
    # a catalogue number ("Cat. No. S1888") that equals a sentence id stays: machine_ids on the id with its 14 characters of context skips it
    return presentation.MACHINE_ID.sub(
        lambda m: _ref_cite(by_id[m.group(0)][0]) if len(by_id.get(m.group(0), ())) == 1
        and presentation.machine_ids(text[max(0, m.start() - 14):m.end()]) else m.group(0), text)


def _render_card_statement(row, profile, refs=None, quote_ids=None):
    """A card statement with its citations; ``quote_ids`` limits which source sentences are quoted below it (a sentence
    is quoted in one section only - the citation stays on the statement line)."""
    refs = _card_refs(profile, row.get("sentence_ids")) if refs is None else refs
    if not refs:  # an untraceable statement is not shown
        return []
    source_text = " ".join(str(ref.get("text") or "") for ref in refs)
    from . import result_notes
    hedge = _node_hedge(source_text)
    qualifier = hedge + " · " if hedge else ""
    line = f"- {qualifier}{_cite_bare_ids(row['text'], refs)} ({_compact_refs(refs)})"
    out = [line]
    for ref in refs:
        if quote_ids is None or _ref_sid(ref) in quote_ids:
            out.extend(node_profile._quote(ref, _ref_cite(ref)))
            out.append("")
    return out


def _cite(paper, refs=()):
    """``[[Paper note|Author Year, p. N]]`` for a source line; the refs' sentence IDs go to the Source IDs callout."""
    paper = paper or {}
    refs = [refs] if isinstance(refs, dict) else [r for r in refs or () if isinstance(r, dict)]
    return presentation.cite(paper, [r.get("page") for r in refs if r.get("page") is not None],
                             [r.get("sentence_id") or r.get("id") for r in refs], paper.get("paper_id"))


def _bullets(pairs, limit=30):
    """Bullet lines ``- text (cite)`` for (text, cite) pairs; nothing at all when there are none."""
    pairs = list(dict.fromkeys(pairs))[:limit]
    if not pairs:
        return []
    return [f"- {text}" + (f" ({cite})" if cite else "") for text, cite in pairs] + [""]


# ---------------------------------------------------------------------------------------------------------------
# N3d - each section of a node note shows only its own kind of information.
# A section is a list of ITEMS: {"sids": source sentence IDs the item consumes, "key": dedupe key,
# "pair": () -> (text, cite)  or  "lines": () -> rendered lines}.  Nothing is rendered (and no source ID is
# registered) until the item has survived the one-sentence-one-section rule in _typed_sections.
# Rules (deterministic, explainable; sentence forms live in node_profile.definitional / purposive / states_marker):
#   Protocol steps               Methods-section sentences of the paper's protocol (accepted protocol profile first)
#   Reagents & concentrations    protocol chemical / concentration / dose values; gas mixes go to parameters
#   Equipment & parameters       protocol equipment + duration / temperature values that carry a number and a unit
#   Concentrations used          a concentration / dose with THIS substance's name inside it or right next to it
#   Markers                      sentences that name THIS cell type and state a marker (marker, positive, negative, X+)
#   Purpose / Definition / Description   definitional sentences ("X is a…", "X refers to…", "X, a…") about THIS node
#   Key findings / Outcomes / Measured effects   accepted result / conclusion / interpretation units tagged with THIS node
# ---------------------------------------------------------------------------------------------------------------
# most specific first: a sentence stays in the first of these sections that wants it
_SPECIFICITY = ("Markers", "Protocol steps", "Reagents & concentrations", "Equipment & parameters",
                "Concentrations used", "Variants across papers", "Purpose", "Definition", "Description",
                "Outcomes", "Measured effects", "Key findings")
FINDING_UNIT_TYPES = ("result", "conclusion", "interpretation", "cited_statement", "hypothesis")  # last two: labelled in the table
_CONCENTRATION = re.compile(node_profile.CONDITION_PATTERNS["concentration"] + "|" + node_profile.CONDITION_PATTERNS["dose"])
_PARAMETER = re.compile("|".join((node_profile.CONDITION_PATTERNS["duration"], node_profile.CONDITION_PATTERNS["temperature"],
                                  r"(?<![\w.+\-−])\d+(?:\.\d+)?\s*ms\b")))
_GAS_MIX = re.compile(r"\b(?:CO2|CO₂|O2|O₂|N2|N₂)\b")
_NUMBER_THEN_FILLER = re.compile(r"\d\s*(?:%|[munpμµ]?M\b|(?:mg|µg|μg|ng|g)\b)\s+(?:and|or|of|to|in|at|for|the)\b", re.I)
_BARE_AMOUNT = re.compile(r"[-+−]?\d+(?:\.\d+)?\s*(?:%|[munpμµ]?M|(?:mg|μg|µg|ng|g)(?:/mL|/ml|/kg)?)")


def _ref_sid(ref):
    return str(ref.get("sentence_id") or ref.get("id") or "")


def _trim(text, limit):
    """At most ``limit`` characters, cut at a word boundary (``…`` only when cut)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    return text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:-–—(") + "…"


def _ref_item(paper, ref, text=None):
    sid = _ref_sid(ref)
    text = text or " ".join(str(ref.get("text") or "").split())
    return {"sids": {sid} if sid else set(), "key": ("s", sid) if sid else ("t", text),
            "pair": lambda paper=paper, ref=ref, text=text: (text, _cite(paper, ref))}


def _source_items(profile, fields):
    """Source sentences stored in the node profile's ``fields`` (each already typed by node_profile)."""
    items = []
    for paper in profile.get("papers") or []:
        for field in fields:
            for entry in ((paper.get("fields") or {}).get(field, {}).get("items") or []):
                for ref in (entry["sources"] if entry.get("sources") is not None else [entry]):
                    if " ".join(str(ref.get("text") or "").split()):
                        items.append(_ref_item(paper, ref))
    return items


def _card_items(profile, card, fields, accept=None):
    """Accepted node-card statements for ``fields``; ``accept(source text)`` may reject a statement."""
    items = []
    for row in _statement_rows(profile, card, fields):
        refs = _card_refs(profile, row.get("sentence_ids"))
        if not refs or (accept is not None and not accept(" ".join(str(r.get("text") or "") for r in refs))):
            continue
        items.append({"sids": {_ref_sid(r) for r in refs if _ref_sid(r)}, "key": ("card", row["text"], tuple(row.get("sentence_ids") or ())),
                      "definitional": any(node_profile.definitional(r.get("text"), profile.get("name", ""),
                                                                    profile.get("aliases") or []) for r in refs),
                      "lines": lambda own, row=row, refs=refs: _render_card_statement(row, profile, refs, own)})
    return items


def _variants(profile):
    return [(paper, v) for paper in profile.get("papers") or [] for v in paper.get("protocol_variants") or []]


def _step_items(profile):
    """Methods-section sentences of each protocol variant (the accepted protocol profile's steps)."""
    items = []
    for paper, variant in _variants(profile):
        for ref in (variant.get("steps") or variant.get("sources") or []):
            if " ".join(str(ref.get("text") or "").split()):
                items.append(_ref_item(paper, ref))
    return items


def _variant_name_items(record, profile):
    items = []
    for paper, variant in _variants(profile):
        name = _trim(variant.get("name"), 120)
        if name and name.casefold() != str(record.get("name", "")).casefold():
            items.append({"sids": set(), "key": ("name", paper.get("paper_id"), name.casefold()),
                          "pair": lambda paper=paper, v=variant, name=name: (name, _cite(paper, v.get("sources")))})
    return items


def _route_detail(field, value):
    """The section a protocol detail value belongs to, or None when it is neither a reagent, equipment nor parameter."""
    if field in ("chemical", "concentration", "dose"):
        if _GAS_MIX.search(value):
            return "Equipment & parameters"  # a gas mix is a culture condition, not a concentration of a reagent
        if _NUMBER_THEN_FILLER.search(value) or _BARE_AMOUNT.fullmatch(value):  # "between 50% and 100% of…", "0.5 μM"
            return None  # an amount without a reagent says nothing
        return "Reagents & concentrations"
    if field == "equipment":
        return "Equipment & parameters"
    if field in ("temperature", "duration", "timing"):
        return "Equipment & parameters" if _PARAMETER.search(value) else None  # not "one month", "day 5", dates
    return None


def _detail_items(record, profile, heading):
    """Short values (not sentences) from the papers' protocol details."""
    term, aliases = record.get("name", ""), record.get("synonyms") or []
    items = []
    for paper, variant in _variants(profile):
        for field, entry in (variant.get("details") or {}).items():
            for item in (entry or {}).get("items") or []:
                value = " ".join(str(item.get("value") or "").split())
                refs = item.get("sources") or []
                if not value:
                    continue
                if heading == "Concentrations used":
                    if field not in ("concentration", "dose") or not _CONCENTRATION.search(value) or not any(
                            node_profile.concentration_near(r.get("text"), value, term, aliases) for r in refs):
                        continue
                    value = _attach_node_name(record, value)
                elif _route_detail(field, value) != heading:
                    continue
                items.append({"sids": set(), "key": ("detail", paper.get("paper_id"), value.casefold(),
                                                      tuple(sorted(_ref_sid(r) for r in refs))),
                              "pair": lambda paper=paper, value=value, refs=refs: (value, _cite(paper, refs))})
    return items


def _section_items(heading, record, profile, card, fields, source_fields):
    """The candidate items of one fallback-able section: accepted card statements first, else typed source data."""
    term, aliases = record.get("name", ""), record.get("synonyms") or []
    accept = None
    if heading == "Markers":
        accept = lambda text: node_profile.states_marker(text, term, aliases)
    elif heading == "Concentrations used":
        accept = lambda text: any(node_profile.concentration_near(text, m.group(), term, aliases)
                                  for m in _CONCENTRATION.finditer(text))
    items = _card_items(profile, card, fields, accept)
    if items:
        return items
    if heading == "Protocol steps":
        return _step_items(profile)
    if heading in ("Reagents & concentrations", "Equipment & parameters", "Concentrations used"):
        return _detail_items(record, profile, heading)
    if heading == "Variants across papers":
        return _variant_name_items(record, profile)
    return _source_items(profile, source_fields) if source_fields else []


def _claim(items, claimed):
    """Drop items whose sentences a more specific section already shows, and repeats inside the section."""
    kept, seen = [], set()
    for item in items:
        if item["sids"] and item["sids"] <= claimed or item["key"] in seen:
            continue
        seen.add(item["key"])
        item["own"] = item["sids"] - claimed  # the sentences this item may quote
        claimed |= item["sids"]
        kept.append(item)
    return kept


def _item_lines(items, limit=30):
    out = []
    for item in items:
        if "lines" in item:
            out += item["lines"](item["own"])
    return out + _bullets([item["pair"]() for item in [i for i in items if "pair" in i][:limit]])


def _unit_about(unit, record, concept_ids, registry):
    """The evidence unit is tagged with THIS node: one of its entity terms is the node (or a child) or its structured
    intervention / measure / model fields name it - a bare word in the quote is not enough."""
    names = {str(n).casefold() for n in (record.get("name"), *(record.get("synonyms") or [])) if n}
    for group in (unit.get("terms") or {}).values():
        for value in group or ():
            found = registry.lookup(value) if registry is not None else None
            if str(value).casefold() in names or (found and found.get("concept_id") in concept_ids):
                return True
    structured = [(unit.get("intervention") or {}).get("agent"), (unit.get("outcome") or {}).get("measure"),
                  *[v for v in (unit.get("model") or {}).values() if isinstance(v, str)]]
    return any(presentation.matches_term(text, record.get("name", ""), record.get("synonyms") or [])
               for text in structured if text)


def finding_units(units, record, registry):
    """Accepted result / conclusion / interpretation units whose entity is this node (feeds the findings table)."""
    concept_ids = _descendants(registry, record["concept_id"]) if registry is not None and record.get("concept_id") else set()
    return [u for u in units if u.get("status") in ("ok", "human_confirmed") and u.get("unit_type") in FINDING_UNIT_TYPES
            and _unit_about(u, record, concept_ids, registry)]


def _findings_lines(record, profile, units, registry, synthesis, questions, stale, table_units=None):
    """Checked synthesis sentences, candidate questions, then the per-paper table of ``table_units``."""
    out = []
    unit_by_id = {u["unit_id"]: u for u in units}

    def cites(row):
        return " ".join(f"({_citation(unit_by_id[uid])})" for uid in row.get("evidence") or [] if uid in unit_by_id)
    for row in synthesis or ():
        label = " **needs update — evidence changed**" if stale else ""
        out.append(f"- {row['text']}{label} {cites(row)}".rstrip())
    if synthesis and stale:
        out.append("> Previous checked synthesis retained; evidence changed and regeneration is needed.")
    if questions:
        out += (["", "Candidate open questions:"] if out else ["Candidate open questions:"])
        out += [f"- {row['text']} {cites(row)}".rstrip() for row in questions]
    if out:
        out.append("")
    return out + _papers_report(record, profile, units if table_units is None else table_units, registry)


def _conflict_lines(profile, conflict_ids):
    callout = node_profile.conflict_callout(profile, conflict_ids)
    if not callout:
        return []
    return ["Legend: 🔴 stored contradiction candidate · 🟠 direction differs · 🟡 effect / no-change pattern", "",
            *callout, presentation.TEXT_CONFLICT_DETAIL, ""]


def _related_notes(record, profile, registry):
    """Existing notes linked by the node card or by the concept hierarchy (never an unresolved name)."""
    card = profile.get("card") or {}
    names = node_profile._link_names(card.get("related") or (card.get("fields") or {}).get("related"))
    if registry is not None:
        parents, cid = set(record.get("parents") or []), record.get("concept_id")
        names += [r["name"] for r in registry.records
                  if r.get("concept_id") in parents or cid in (r.get("parents") or [])]
    links, _ = node_profile._quoted_links(names, node_profile._vault_note_names(profile.get("vault", "")))
    own = f"[[{notes.safe_filename(record['name'])}]]".casefold()
    links = [link for link in links if link.casefold() != own][:30]
    return [f"- {link}" for link in links] + [""] if links else []


def _sources_lines(record, profile, units, links):
    """The papers behind this node (note links, evidence-sentence counts)."""
    counts, names = {}, {}
    for unit in units:
        counts[unit.get("paper_id")] = counts.get(unit.get("paper_id"), 0) + 1
        names.setdefault(unit.get("paper_id"), (unit.get("paper") or {}).get("note_name"))
    for paper in profile.get("papers") or []:
        counts.setdefault(paper.get("paper_id"), 0)
        names.setdefault(paper.get("paper_id"), paper.get("note_name"))
    for paper_id in links or ():
        counts.setdefault(paper_id, 0)
    rows = []
    for paper_id, count in counts.items():
        name = names.get(paper_id) or (db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}).get("note_name")
        rows.append((str(name or paper_id).casefold(), f"[[{name}]]" if name else str(paper_id), count))
    if not rows:
        return []
    rows.sort()
    out = [f"- {label}" + (presentation.TEXT_SENTENCES.format(count=count, s="s" * (count != 1)) if count else "")
           for _, label, count in rows[:60]]
    if len(rows) > 60:
        out.append(presentation.TEXT_MORE_PAPERS.format(count=len(rows) - 60, s="s" * (len(rows) - 60 != 1)))
    return out + [""]


def _table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    out.extend("| " + " | ".join(_md_cell(v) for v in row) + " |" for row in rows)
    return out


def _attach_node_name(record, value):
    value = str(value or "").strip()
    if not value or presentation.matches_term(value, record.get("name", ""), record.get("synonyms") or []):
        return value
    # Only attach the node to a bare numeric/unit fragment.  Text such as
    # ``5% CO2`` is an unresolved method condition, not ``5% CO2 Sucrose``.
    if re.fullmatch(r"[-+−]?\d+(?:\.\d+)?\s*(?:%|[munpμµ]?M|(?:mg|μg|µg|ng|g)(?:/mL|/ml|/kg)?)", value):
        return f"{value} {record.get('name', '')}".strip()
    return value


CARD_CHECK_LINE = "*AI-written summary from {n} source sentences, checked automatically.*"   # NC-04 wording; English for VN-08 (language was open under OD-11)


def result_back_links(texts):
    """RL-10 A6 index from (path, text) pairs: node stem casefold -> sorted [(EVD file stem, predicate)] of every current
    result note (`type: result`, not retired) that links the node as subject or object (a scalar or a list of items).
    A not-significant or negated result keeps that qualifier next to its predicate (evidence rule 4)."""
    from . import result_notes
    index = {}
    for path, text in texts:
        if not path.startswith("Results/"):
            continue
        fm, _ = notes._split_frontmatter(text)
        if not isinstance(fm, dict) or fm.get("type") != "result" or fm.get("retired"):
            continue
        predicate = str(fm.get("predicate") or "")
        if fm.get("negated") is True:
            predicate += " (negated)"
        if result_notes.is_not_significant(fm.get("significance")) and predicate != "no_significant_change":
            predicate += " (not significant)"
        for role in ("subject", "object"):
            raw = fm.get(role)
            for item in raw if isinstance(raw, list) else [raw]:
                for link in notes.WIKI_LINK.finditer(str(item or "")):
                    index.setdefault(link[1].strip().casefold(), set()).add((Path(path).stem, predicate))
    return {key: sorted(rows) for key, rows in index.items()}


def vault_result_back_links(vault):
    """`result_back_links` of the Results folder of a vault (read once per refresh run)."""
    root = config.vault_dir(vault) / "Results"
    return result_back_links((f"Results/{p.name}", p.read_text(encoding="utf-8", errors="replace"))
                             for p in sorted(root.glob("*.md"))) if root.is_dir() else {}


def result_link_lines(rows):
    """The lines of a node's Results section: one link per result note, its predicate as plain text."""
    return [f"- [[{stem}]]" + (f" — {predicate}" if predicate else "") for stem, predicate in rows]


def _typed_sections(record, profile, registry, units=(), links=None, synthesis=(), questions=(),
                    synthesis_stale=False, conflict_ids=None, result_links=()):
    """The approved sections of NODE_SECTIONS for this node; a section without real data is not written.

    Two passes: (1) in _SPECIFICITY order every section picks its items, so a source sentence stays in the most specific
    section that wants it; (2) the sections are written in their reading order."""
    kind = node_kind(profile, record.get("type"), record)
    card = profile.get("card") or {}
    units = list(units)
    sections = NODE_SECTIONS[kind]
    findings_heading = _FINDINGS_HEADING[kind]
    claimed, picked = set(), {}
    about = finding_units(units, record, registry)
    # a card statement that rests only on result sentences is a finding, not a definition (unless it states one)
    result_sids = {sid for u in about for sid in u.get("sentence_ids") or ()}
    order = sorted(range(len(sections)), key=lambda i: (_SPECIFICITY.index(sections[i][0])
                                                        if sections[i][0] in _SPECIFICITY else len(_SPECIFICITY), i))
    for i in order:
        heading, fields, source_fields = sections[i]
        if heading in ("Conflicting findings", "Figures", notes.RESULTS_HEADING, "Related notes", "Sources"):
            continue
        if heading == findings_heading:
            picked[heading] = _claim(_card_items(profile, card, fields), claimed)
            table = [u for u in about if not (u.get("sentence_ids") and set(u["sentence_ids"]) <= claimed)]
            picked[("table", heading)] = table
            continue
        items = _section_items(heading, record, profile, card, fields, source_fields)
        if heading in ("Purpose", "Definition", "Description"):
            items = [i for i in items if not (i["key"][0] == "card" and i["sids"] <= result_sids
                                              and not i["definitional"])]
        picked[heading] = _claim(items, claimed)
    out = list(node_profile.card_currency_notice(card))  # stale-card warning (was in the old summary callout)
    for heading, fields, source_fields in sections:
        if heading == "Conflicting findings":
            lines = _conflict_lines(profile, conflict_ids)
        elif heading == "Figures":
            lines = _related_figures(record, profile)
        elif heading == notes.RESULTS_HEADING:
            lines = result_link_lines(result_links)
            if lines:  # heading and list in one chunk: the byte capper keeps the section whole (notes._cap_complete_blocks)
                out += [f"## {heading}", *lines, ""]
            continue
        elif heading == "Related notes":
            lines = _related_notes(record, profile, registry)
        elif heading == "Sources":
            lines = _sources_lines(record, profile, units, links)
        elif heading == findings_heading:
            lines = (_findings_lines(record, profile, units, registry, synthesis, questions, synthesis_stale,
                                     table_units=picked[("table", heading)]) + _item_lines(picked[heading]))
        else:
            lines = _item_lines(picked[heading])
        if any(line.strip() for line in lines):
            out += [f"## {heading}", "", *lines]
            if lines[-1].strip():
                out.append("")
    first = next((i for i, line in enumerate(out) if line.startswith("## ")), None)
    cited = {sid for row in _card_statements(card) for sid in row.get("sentence_ids") or ()}
    if first is not None and card.get("card_check") == "passed" and cited:   # NC-04: shown card says it was checked
        out[first:first] = [CARD_CHECK_LINE.format(n=len(cited)), ""]
    return out


def _model_text(value):
    if isinstance(value, dict):
        return " ".join(_model_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_model_text(item) for item in value)
    return " ".join(str(value or "").split())


_CANONICAL_LINK = re.compile(r"\[\[([^\]|#^]+)([#^][^\]|]*)?(?:\|([^\]]+))?\]\]")


def _canonical_note_links(text, registry):
    """Canonicalize generated concept links without rewriting human text."""
    if not registry or not isinstance(text, str):
        return text

    def replace(match):
        target, suffix, alias = match.groups()
        record = registry.lookup(target)
        if not record:
            return match.group(0)
        canonical = notes.safe_filename(concepts._display_name(record.get("name") or target))
        rendered = f"[[{canonical}{suffix or ''}"
        if alias is not None:
            rendered += f"|{alias}"
        return rendered + "]]"

    return _CANONICAL_LINK.sub(replace, text)


def _model_config():
    """(term -> species, never-species words, reagent-host words) from config/model_terms.yaml."""
    data = config._read_yaml("model_terms.yaml") or {}
    lists = {k: [str(t) for t in v] if isinstance(v, (list, tuple)) else [] for k, v in data.items()}
    never = {t.casefold() for t in lists.pop("_never_species", [])}
    hosts = {t.casefold() for t in lists.pop("_reagent_hosts", [])}
    terms = {t.casefold(): name for name, values in lists.items() for t in values}
    return terms, never, hosts | set(terms)


def _strip_reagents(text, hosts):
    """Remove antibody/serum wording whose animal is only the reagent host."""
    h = "(?:%s)s?" % "|".join(sorted(map(re.escape, hosts), key=len, reverse=True)) if hosts else "(?!)"
    ab = r"(?:\s+(?:IgG|IgM|IgY|IgA|antibod(?:y|ies)|secondary|conjugated))*"
    patterns = (
        r"\b%s[- ]anti[- ]\w+%s" % (h, ab), r"\banti[- ]%s%s" % (h, ab),
        r"\b%s\s+(?:poly|mono)clonal(?:\s+antibod(?:y|ies))?" % h, r"\b%s\s+(?:IgG|IgM|IgY|antiserum|antibod(?:y|ies))\b" % h,
        r"\b(?:raised|produced|generated|made|developed|hosted)\s+(?:in|from)\s+(?:a\s+|an\s+|the\s+)?%s" % h,
        r"\b(?:normal\s+)?%s\s+serum\b" % h, r"\b(?:fetal |foetal )?(?:bovine|calf)\s+serum(?:\s+albumin)?",
    )
    for pattern in patterns:
        text = re.sub(r"\b(?:%s)" % pattern, " ", text, flags=re.I)
    return text


def _model_term(text, family, registry=None):
    """Extract one organism/stage term from source wording, never a whole sentence."""
    text = _model_text(text)
    if not text:
        return None
    terms, never, hosts = _model_config() if family == "species" else ({}, set(), set())
    if family == "species":
        text = _strip_reagents(text, hosts)
    records = list(getattr(registry, "records", ()) or ()) if registry else []
    allowed = {"species", "organism"} if family == "species" else {"cell_type", "concept"}
    matches = []
    for record in records:
        if str(record.get("type") or record.get("entity_kind") or "").casefold() not in allowed:
            continue
        names = [record.get("name"), *(record.get("synonyms") or [])]
        if str(record.get("name") or "").casefold() in never:
            continue
        names = [n for n in names if str(n or "").casefold() not in never]
        if any(presentation.matches_term(text, name, ()) for name in names if name):
            matches.append((len(str(record.get("name") or "")), str(record.get("name"))))
    if matches:
        return sorted(matches, key=lambda item: (-item[0], item[1].casefold()))[0][1]
    if family == "species":
        pattern = re.compile(r"\b(?:%s)\b" % "|".join(sorted(map(re.escape, terms), key=len, reverse=True)), re.I) if terms else None
        # an all-capital short hit is a gene/reagent abbreviation (CAT, RAT), not an animal
        found = next((m for m in pattern.finditer(text) if not (m.group(0).isupper() and len(m.group(0)) <= 3)), None) if pattern else None
        return terms[found.group(0).casefold()] if found else None
    patterns = {
        "stage": r"\b(?:MII|GV|MI|oocyte|embryo|morula|blastocyst|cleavage|zygote|blastomere|(?:2|two|4|four|8|eight)[- ]cell|pronucle(?:us|i)|(?:preantral |antral |primordial )?follicle|fetus|neonate)s?\b",
    }
    match = re.search(patterns[family], text, re.I)
    return match.group(0) if match else None


def _short_model_phrase(value):
    """Return concise source wording that can be shown verbatim."""
    text = _model_text(value)
    if not text or len(text) > 60 or re.search(r"[.;:!?]", text):
        return None
    return text


def _paper_model_labels(paper_id, model, paper_profile=None, unit=None, registry=None):
    """Return source wording verbatim when short, otherwise concise inferred terms."""
    model = model or {}
    model_fields = model if isinstance(model, dict) else {}
    values = {"species": None, "stage": None}
    inferred = {"species": False, "stage": False}

    def take(text, *, mark_inferred, family_hint=None, preserve=False):
        for family in values:
            if family_hint and family != family_hint:
                continue
            if values[family] is None:
                normalized = _model_text(text)
                short = _short_model_phrase(normalized) if preserve else None
                if short:
                    term = (family == "species" and " " not in short and _model_term(short, "species", registry)) or short
                else:
                    term = _model_term(normalized, family, registry)
                if term:
                    values[family] = term
                    inferred[family] = mark_inferred

    def take_source(text):
        phrase = _short_model_phrase(text)
        if phrase and values["species"] is None and values["stage"] is None:
            values["species"] = values["stage"] = phrase
            return True
        take(text, mark_inferred=True)
        return False

    # A unit may carry one concise model phrase instead of separate fields.
    if isinstance(model, str):
        take_source(model)
    elif isinstance(model_fields, dict):
        for key in ("text", "phrase", "context", "description", "model_context"):
            if model_fields.get(key) and take_source(model_fields[key]):
                break

    # 1. Explicit structured model fields on the unit.  Some enrichments put
    # one concise combined phrase in ``species``; preserve it for both cells.
    structured_values = [model_fields.get(key) for key in
                         ("species", "taxon", "stage", "developmental_stage") if model_fields.get(key)]
    combined = next((_short_model_phrase(value) for value in structured_values
                     if _short_model_phrase(value) and _model_term(value, "species", registry)
                     and _model_term(value, "stage", registry)), None)
    if combined:
        values["species"] = values["stage"] = combined
    else:
        for family, keys in (("species", ("species", "taxon")),
                             ("stage", ("stage", "developmental_stage"))):
            for key in keys:
                if model_fields.get(key):
                    short = _short_model_phrase(model_fields[key])
                    take(model_fields[key], mark_inferred=short is None, family_hint=family, preserve=True)
                    break

    if unit:
        for key in ("model_text", "model_context", "model_phrase"):
            if unit.get(key):
                take_source(unit[key])
                break

    # 2. The unit's copied source sentences (not the stored free-text quote).
    if unit and paper_profile:
        for result in paper_profile.get("results") or []:
            if result.get("unit_id") == unit.get("unit_id"):
                take(" ".join(str(ref.get("text") or "") for ref in result.get("sources") or ()),
                     mark_inferred=True)
                break
    if unit:
        take(unit.get("statement"), mark_inferred=True)  # the unit's own wording ("porcine oocytes")

    # A review's overall topic or a cited study's model says nothing about the unit: no paper-level fallback.
    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}
    evidence_type = meta.get("evidence_type") or ((unit or {}).get("paper") or {}).get("evidence_type")
    if unit and (unit.get("unit_type") == "cited_statement" or evidence_type in find.REVIEW_TYPES):
        return tuple((values[f] + presentation.TEXT_FROM_PAPER if values[f] and inferred[f] else values[f]) or "" for f in values)

    # 3. Model values in the paper's structured protocol profile.
    protocol_text = " ".join(
        _model_text(item.get("value"))
        for variant in (paper_profile or {}).get("protocol_variants") or []
        for item in ((variant.get("details") or {}).get("model") or {}).get("items", []))
    take(protocol_text, mark_inferred=True)

    # 4. Whole-paper overview model groups, then 5. checked WH who.
    overview = structure._fresh_overview(paper_id) or {}
    overview_rows = [str(row.get("text") or "") for row in overview.get("groups_models") or []
                     if isinstance(row, dict) and row.get("text")]
    for row in overview_rows:
        if take_source(row):
            break
    wh = db.read_json(db.paper_dir(paper_id) / "section_profile.json", {}) or {}
    sentences = list(db.read_jsonl(db.paper_dir(paper_id) / "sentences.jsonl"))
    who = ((wh.get("answers") or {}).get("who") or {}) if structure._profile_report(
        paper_id, wh, sentences, "wh")["ready"] else {}
    take_source(who.get("text"))
    # 6. Paper metadata: the title often names the model ("... in rat ovarian tissue").
    # A review's title may name another organism than the unit cited from it, so reviews are skipped.
    if evidence_type not in find.REVIEW_TYPES:
        take(meta.get("title"), mark_inferred=True)

    result = []
    for family in ("species", "stage"):
        value = values[family] or ""
        result.append(value + presentation.TEXT_FROM_PAPER if value and inferred[family] else value)
    return tuple(result)


def _papers_report(record, profile, units, registry=None):
    aliases = record.get("synonyms") or []
    ordered = sorted(units, key=lambda u: ((u.get("paper") or {}).get("evidence_type") in ("review", "systematic_review", "meta_analysis"),
                                          u.get("unit_type") in ("cited_statement", "hypothesis"), u.get("unit_type") != "result", u.get("unit_id", "")))
    rows = []
    for unit in ordered[:12]:
        model = unit.get("model") or {}
        paper = unit.get("paper") or {}
        paper_profile = next((p for p in profile.get("papers", []) if p.get("paper_id") == unit.get("paper_id")), {})
        species, stage = _paper_model_labels(unit.get("paper_id"), model, paper_profile,
                                             unit=unit, registry=registry)
        sentences = {s["sentence_id"]: s for s in _linked_rows(unit["paper_id"])
                     if s.get("sentence_id")} if unit.get("paper_id") else {}
        refs = node_profile._refs(unit.get("sentence_ids"),
                                  sentences,
                                  unit.get("paper_id"), paper, linked=True) if unit.get("paper_id") else []
        source_text = " ".join(str(ref.get("text") or "") for ref in refs)
        hedge = structure.uncertainty(source_text)
        finding = unit.get("statement") or presentation.TEXT_NOT_FOUND
        non_significant = re.search(r"\b(?:no significant(?: difference| change)?|not significantly|did not significantly|non-significant|non significant)\b", source_text, re.I)
        if non_significant:
            finding = presentation.TEXT_NO_DIFFERENCE + " · " + finding
        else:
            flags = unit.get("flags") or []
            flag_codes = []
            for flag in flags if isinstance(flags, list) else [flags]:
                if isinstance(flag, dict):
                    code = flag.get("code") or flag.get("label")
                else:
                    code = flag
                if code:
                    flag_codes.append(str(code))
            if unit.get("status") in ("review", "needs_review") or flag_codes or unit.get("provenance") == "heuristic_draft":
                finding = "[review" + (": " + ", ".join(dict.fromkeys(flag_codes)) if flag_codes else "") + "] " + finding
            elif hedge:
                finding = presentation.TEXT_HEDGED + "/".join(dict.fromkeys(h["text"] for h in hedge)) + " · " + finding
        evidence_type = paper.get("evidence_type") or ""
        label = presentation.TEXT_SECONDARY if evidence_type in ("review", "systematic_review", "meta_analysis") else {"cited_statement": "cited study — not this paper's result", "hypothesis": "hypothesis — not a finding"}.get(unit.get("unit_type"), unit.get("unit_type") or "")
        context = species if species == stage else " / ".join(dict.fromkeys(value for value in (species, stage) if value))
        rows.append((presentation.cite(paper) if paper.get("note_name") else str(unit.get("paper_id")), context or "—",
                     finding, (unit.get("outcome") or {}).get("direction") or "", label, _citation(unit)))
    if not rows:
        return []
    return _table(("paper", "species/stage", "finding", "direction", "type", "source"), rows) + [""]


def _related_figures(record, profile, limit=6):
    term, aliases = record["name"], record.get("synonyms") or []
    out = []
    found = 0
    for paper in profile.get("papers", []):
        pid = paper["paper_id"]
        sentence_path = db.paper_dir(pid) / "sentences.jsonl"
        sentences = {s.get("sentence_id"): s for s in
                     sentence_utils.cached_linked_rows(db.read_jsonl(sentence_path), sentence_path)}
        units = paper.get("results", [])
        object_ids = {oid for u in units for oid in u.get("source_objects", [])}
        objects = [("figure", x) for x in db.read_jsonl(db.paper_dir(pid) / "figures.jsonl")]
        objects += [("table", x) for x in db.read_jsonl(db.paper_dir(pid) / "tables.jsonl")]
        manifest = db.read_json(db.paper_dir(pid) / "media_manifest.json", {}) or {}
        bundles = {b.get("object_id"): b for b in manifest.get("bundles", [])}
        for kind, obj in objects:
            if found >= limit:
                break
            oid = obj.get("figure_id") or obj.get("table_id") or obj.get("object_id")
            caption_ids = list(obj.get("caption_sentence_ids") or [])
            mention_ids = [sid for sid in (obj.get("mentioned_in") or []) if sid not in caption_ids]
            caption_texts = [sentences.get(sid, {}).get("text", "") for sid in caption_ids]
            mention_texts = [sentences.get(sid, {}).get("text", "") for sid in mention_ids]
            texts = caption_texts + mention_texts
            if not (object_ids.intersection({oid, str(oid).rsplit(":", 1)[-1]}) or any(presentation.matches_term(t, term, aliases) for t in texts)):
                continue
            caption = " ".join(t for t in caption_texts if t) or str(obj.get("caption") or "Caption unavailable")
            bundle = bundles.get(oid) or bundles.get(str(oid).rsplit(":", 1)[-1]) or {}
            locator = bundle.get("source_locator") or {}
            region_kind = locator.get("region_kind") or bundle.get("region_kind")
            locator_basis = locator.get("locator_basis") or bundle.get("locator_basis")
            crop = bundle.get("crop") or {}
            unavailable_image = (region_kind in ("source_page_preview", "unlocated_object") or
                                 locator_basis == "unlocated" or
                                 crop.get("status") in ("unavailable", "missing_region"))
            attach_dir = config.vault_dir(profile["vault"]) / "_Attachments" / pid
            candidates = [attach_dir / f"{oid}.png", attach_dir / f"{str(oid).rsplit(':', 1)[-1]}.png"]
            image = next((p for p in candidates if p.is_file()), None)
            if image and not unavailable_image:
                link = f"![[{image.relative_to(config.vault_dir(profile['vault'])).as_posix()}|480]]"
            elif unavailable_image:
                page = locator.get("caption_page") or locator.get("page") or obj.get("page")
                note = paper.get("note_name") or pid
                alias = presentation.TEXT_PAGE_LINK.format(page=page) if page else presentation.TEXT_SEE_ORIGINAL
                link = presentation.TEXT_IMAGE_MISSING.format(link=f"[[{note}#{notes.PAPER_FIGURES_HEADING}|{alias}]]")
            else:
                link = f"[verified crop]({crop.get('path')})" if crop.get("path") else "verified crop unavailable"
            out += [f"### {presentation.object_title(kind, oid, obj.get('label'), caption, paper, obj.get('page'))}",
                    link, f"> {caption}", ""]
            presentation.note_source_ids(pid, caption_ids, presentation.cite_label(paper, obj.get("page")))
            for sid, text in zip(mention_ids, mention_texts):
                if text:
                    out.append(f"- {text} ({_cite(paper, {**sentences.get(sid, {}), 'sentence_id': sid})})")
            if figure_reading is not None and hasattr(figure_reading, "load"):
                try:
                    reading = figure_reading.load(pid, oid)
                except (OSError, ValueError, TypeError, AttributeError):
                    reading = None
                if isinstance(reading, dict):
                    summary = reading.get("summary") or reading.get("text")
                    if isinstance(summary, dict):  # {"text": ..., "panel_refs": [...]}
                        panels = ", ".join(str(p) for p in summary.get("panel_refs") or [])
                        summary = (summary.get("text") or "") + (f" (panel {panels})" if panels else "")
                    if summary:
                        out.append(presentation.TEXT_IMAGE_READ.format(summary=summary))
                    flag = figure_reading.warning(reading) if hasattr(figure_reading, "warning") else ""
                    if flag or reading.get("inconsistent") or reading.get("status") == "conflict":
                        out.append("> " + presentation.TEXT_IMAGE_MISMATCH.format(flag=flag or presentation.TEXT_IMAGE_INCONSISTENT))
            out.append("")
            found += 1
        if found >= limit:
            break
    return out


# Reader-facing group names of a MOC's member list (registry type -> label).
MOC_GROUPS = {"concept": "Concepts", "method": "Methods", "technique": "Methods", "molecule": "Substances",
              "substance": "Substances", "species": "Organisms", "organism": "Organisms", "cell_type": "Cell types",
              "topic": "Topics"}


def _moc_members(record, units, registry, vault):
    """Existing node notes that belong to this topic: its registry hierarchy (children / descendants) plus the
    concepts named in the topic's OWN evidence sentences (``units``), never in whole linked papers.
    Returns ``{group label: [note stem, ...]}``."""
    topic_id = record["concept_id"]
    descendants = _descendants(registry, topic_id) if registry else {topic_id}
    known = node_profile._vault_note_names(vault)
    member_records = {}
    for row in (registry.records if registry else []):
        if row.get("concept_id") != topic_id and (topic_id in (row.get("parents") or [])
                                                   or row.get("concept_id") in descendants):
            member_records[row.get("concept_id")] = row
    blob = "\n".join(str(u.get(key) or "") for u in units or () for key in ("quote", "statement"))
    if blob and registry:
        # One search per concept over the joined evidence text (a term never contains a newline).
        blob_lower = blob.lower()
        for row in registry.records:
            if row.get("concept_id") == topic_id or row.get("concept_id") in member_records:
                continue
            names = [row.get("name", ""), *(row.get("synonyms") or [])]
            if any(str(n).strip() and str(n).strip().lower() in blob_lower for n in names) and                     presentation.matches_term(blob, row.get("name", ""), row.get("synonyms") or []):
                member_records[row.get("concept_id")] = row
    groups = {}
    for row in member_records.values():
        stem = known.get(notes.safe_filename(row["name"]).casefold())
        if stem:
            groups.setdefault(MOC_GROUPS.get(row.get("type") or "concept", "Concepts"), []).append(stem)
    return {label: sorted(dict.fromkeys(stems), key=str.casefold) for label, stems in sorted(groups.items())}


def _moc_count_text(groups):
    return ", ".join(f"{len(stems)} {label.lower()}" for label, stems in groups.items()) or "no member notes yet"


def _render_moc(record, links, units, registry, profile, vault, conflict_ids=None, result_links=()):
    """Topic hub: Overview · Member notes · Key papers · Open conflicts · Results (empty ones hidden). Navigation only:
    every count and list is about THIS topic, never the whole vault; Results = the result notes that link the topic (RL-10 A6)."""
    groups = _moc_members(record, units, registry, vault)
    if conflict_ids is None:
        conflict_ids = node_profile._vault_conflict_ids(vault)
    conflict_rows = [r for r in (profile or {}).get("conflicts", [])
                     if r.get("display_status", r.get("status")) in node_profile.OPEN_CONFLICT_STATUSES]
    conflicts = list(dict.fromkeys(conflict_ids[str(r.get("relation_id"))] for r in conflict_rows
                                   if str(r.get("relation_id")) in conflict_ids))
    out = ["## Overview", "",
           f"{record['name']}: {len(links)} paper{'s' * (len(links) != 1)} in this vault, {_moc_count_text(groups)}, "
           f"{len(conflicts)} open conflict{'s' * (len(conflicts) != 1)}.", ""]
    if groups:
        out += ["## Member notes", ""]
        for label, stems in groups.items():
            out.append(f"- **{label}** ({len(stems)}): " + ", ".join(f"[[{name}]]" for name in stems[:40])
                       + (f" … and {len(stems) - 40} more" if len(stems) > 40 else ""))
        out.append("")
    papers = []
    for paper_id in sorted(links):
        meta = db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}
        rows = links[paper_id] if isinstance(links[paper_id], list) else []
        mentions = sum(int(row.get("n_sentences") or 0) for row in rows if isinstance(row, dict))
        cite = presentation.cite({**meta, "paper_id": paper_id, "note_name": meta.get("note_name") or paper_id})
        papers.append((-mentions, presentation.author_year(meta), cite, mentions))
    if papers:
        out += ["## Key papers", ""]
        out += [f"- {cite} — {mentions} sentence{'s' * (mentions != 1)}" if mentions else f"- {cite}"
                for _, _, cite, mentions in sorted(papers)[:40]]
        out.append("")
    if conflicts:
        out += ["## Open conflicts", "", *[f"- [[{stem}|{stem.removeprefix('CONFLICT - ')}]]" for stem in conflicts], ""]
    if result_links:  # heading and list in one chunk: the byte capper keeps the section whole (notes._cap_complete_blocks)
        out += [f"## {notes.RESULTS_HEADING}", *result_link_lines(result_links), ""]
    return out


def _moc_summary(record, registry=None, vault="", units=()):
    """One-line ``kb_summary`` of a topic: the same member counts as its body."""
    if registry is not None:
        groups = _moc_members(record, units, registry, vault)
    else:  # no registry: nothing topic-specific to count
        groups = {}
    return f"{record.get('name', '')} — {_moc_count_text(groups)}"


def _render(record, links, units, gaps, synthesis, questions, synthesis_status, synthesis_state,
            fingerprint, registry, synthesis_stale=False, profile=None, conflict_ids=None, result_links=()):
    if record.get("type") == "topic" or node_profile.graph_node_type(profile or {}, record.get("type"), record) == "moc":
        block = [notes.AUTO_START, f"<!-- kb:concept-fingerprint:{fingerprint} -->", ""]
        block += _render_moc(record, links, units, registry, profile, (profile or {}).get("vault", ""), conflict_ids,
                             result_links)
        return notes.cap_auto_block("\n".join(block + [notes.AUTO_END, ""]),
                                    notes.NODE_NOTE_MAX_BYTES, "concept")
    out = [notes.AUTO_START, f"<!-- kb:concept-fingerprint:{fingerprint} -->",
           "<!-- kb:synthesis-state:" + json.dumps(synthesis_state, ensure_ascii=False, sort_keys=True) + " -->", ""]
    with presentation.collect_source_ids() as source_ids:
        out += _typed_sections(record, profile or {}, registry, units=units, links=links, synthesis=synthesis,
                               questions=questions, synthesis_stale=synthesis_stale, conflict_ids=conflict_ids,
                               result_links=result_links)
    out += presentation.source_ids_callout(source_ids)
    out.append(notes.AUTO_END)
    return _cap_node_block("\n".join(out) + "\n", notes.NODE_NOTE_MAX_BYTES)


def _cap_node_block(block, cap):
    """Cap the typed note while preserving whole rows."""
    return notes.cap_auto_block(block, cap, "concept")


def _unify_notes_heading(tail):
    """An empty old '## Notes' (the human zone) becomes '## My notes'; text under it is human-owned and kept as is."""
    match = re.fullmatch(r"(\s*)## Notes(\s*)", tail)
    return f"{match.group(1)}## {HUMAN_HEADING}{match.group(2)}" if match else tail


def _set_summary(fm, record, registry, vault, units, profile):
    summary = (_moc_summary(record, registry, vault, units) if record.get("type") == "topic"
               else _source_summary(record, units, profile))
    if summary:
        fm["kb_summary"] = summary
    else:
        fm.pop("kb_summary", None)


def _write_note(vault, record, block, fingerprint, units=(), profile=None, registry=None):
    block = _cap_node_block(block, notes.NODE_NOTE_MAX_BYTES)
    block = _canonical_note_links(block, registry)
    folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
    name = notes.safe_filename(record["name"])
    path = config.vault_dir(vault) / folder / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    # Canonical acronym casing is part of the filename as well as the display
    # name.  A case-only legacy path is moved before the managed block is
    # spliced, so human text outside that block survives the migration.
    exact = next((candidate for candidate in path.parent.glob("*.md")
                  if candidate.name == path.name), None)
    legacy = next((candidate for candidate in path.parent.glob("*.md")
                   if candidate.name.casefold() == path.name.casefold()), None)
    if exact is None and legacy is not None and legacy.name != path.name:
        # Windows treats a case-only destination as the same path.  Rename
        # through a temporary sibling so the canonical acronym spelling is
        # materialised without losing the human-managed tail.
        temporary = path.with_name(path.name + ".d389-migration")
        suffix = 1
        while temporary.exists():
            temporary = path.with_name(path.name + f".d389-migration-{suffix}")
            suffix += 1
        legacy.replace(temporary)
        temporary.replace(path)
    if path.exists():
        old = path.read_text(encoding="utf-8")
        starts, ends = [m.start() for m in re.finditer(re.escape(notes.AUTO_START), old)], \
                       [m.start() for m in re.finditer(re.escape(notes.AUTO_END), old)]
        if len(starts) == 1 and len(ends) == 1 and ends[0] > starts[0]:
            # the block ends with AUTO_END + newline; splice without it so reruns add no blank lines
            new = old[:starts[0]] + block.rstrip("\n") + _unify_notes_heading(old[ends[0] + len(notes.AUTO_END):])
        elif notes.STUB_MARK in old and notes._is_pure_stub(folder, name, old):
            base = old.replace(notes.STUB_MARK + "\n", "", 1).rstrip()
            # This paragraph belongs to pure stubs only.  Do not carry the
            # obsolete backlink hint into a full generated node note.
            base = base.replace(
                "Papers that mention this appear under **Backlinks** (right-hand panel in Obsidian).",
                "", 1)
            marker = base.find("\n## Notes")
            new = (base[:marker] + "\n\n" + block + "\n" + _unify_notes_heading(base[marker + 1:])
                   if marker >= 0 else base + "\n\n" + block)
        else:
            return "protected"
        fm, rest = notes._split_frontmatter(new)
        if fm is None:
            return "protected"
        metadata = presentation.metadata_for_term(record["name"], record["type"], units, record.get("synonyms") or [])
        # kb_kind/kb_roles/source_papers/evidence_count stay (Index, neural, views read them); typed keys on top.
        fm.update(presentation.machine_properties(metadata,units,record["name"],record.get("synonyms") or []))
        _set_summary(fm, record, registry, vault, units, profile)
        fm["aliases"] = list(record.get("synonyms") or [])
        fm.update(node_profile.typed_machine_properties(record, profile or {}, vault, fm))
        fm.pop("kb_stub", None)
        _conflict_tag(fm)
        presentation.fold_machine_keys(fm)  # one nested kb_machine key instead of five machine-only Properties
        # Existing user tags remain untouched; graph colours use machine-owned type.
        new = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + rest
        new = notes.cap_managed_note(new, notes.NODE_NOTE_MAX_BYTES, "concept")
        if new == old:
            return "unchanged"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        history = config.STORE / "concept_history" / stamp / vault / folder / path.name
        history.parent.mkdir(parents=True, exist_ok=True)
        history.write_text(old, encoding="utf-8", newline="\n")
        path.write_text(new, encoding="utf-8", newline="\n")
        return "updated"
    metadata = presentation.metadata_for_term(record["name"], record["type"], units, record.get("synonyms") or [])
    fm = {"type": record["type"], "tags": [concepts.TYPE_KEYS[record["type"]], *metadata["tags"]],
          **presentation.machine_properties(metadata,units,record["name"],record.get("synonyms") or []),
          **node_profile.typed_machine_properties(record, profile or {}, vault)}
    _set_summary(fm, record, registry, vault, units, profile)
    fm["aliases"] = list(record.get("synonyms") or [])
    _conflict_tag(fm)
    presentation.fold_machine_keys(fm)
    header = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + f"# {record['name']}\n\n"
    text = notes.cap_managed_note(
        header + block + f"\n## {HUMAN_HEADING}\n\n", notes.NODE_NOTE_MAX_BYTES, "concept")
    path.write_text(text, encoding="utf-8", newline="\n")
    return "created"


CONFLICT_TAG = "conflict"  # machine-owned tag (researcher 13/10): every note with an open conflict, searchable as #conflict


def _conflict_tag(fm):
    """Add `conflict` to tags when the note has an open conflict, drop it when not; other tags are untouched."""
    tags = fm.get("tags") or []
    tags = [tags] if isinstance(tags, str) else list(tags)
    tags = [t for t in tags if t != CONFLICT_TAG] + ([CONFLICT_TAG] if fm.get("has_conflict") is True else [])
    if tags:
        fm["tags"] = tags
    else:
        fm.pop("tags", None)


def _editable(path, folder, name):
    if not path.exists():
        return True
    text = path.read_text(encoding="utf-8")
    starts, ends = text.count(notes.AUTO_START), text.count(notes.AUTO_END)
    if starts == 1 and ends == 1 and text.find(notes.AUTO_END) > text.find(notes.AUTO_START):
        return True
    return notes.STUB_MARK in text and notes._is_pure_stub(folder, name, text)


def _revalidate(units, synthesis):
    """Re-run A1–A6 on retained sentences against the CURRENT evidence (all units of the concept)."""
    items, cards, index = [], {}, {}
    for unit in units:
        if unit.get("status") not in ("ok", "human_confirmed"):
            continue
        paper = unit.get("paper") or {}
        cards[unit["paper_id"]] = {"year": paper.get("year"), "evidence_type": paper.get("evidence_type")}
        items.append({"kind": "unit", "text": unit.get("quote") or unit.get("statement") or "",
                      "statement": unit.get("statement") or "", "unit_type": unit.get("unit_type", "result"),
                      "hedging": unit.get("hedging"), "direction": (unit.get("outcome") or {}).get("direction"),
                      "paper_id": unit["paper_id"], "review_paper": paper.get("evidence_type") in
                      ("review", "systematic_review", "meta_analysis"), "cited_refs": []})
        index[unit["unit_id"]] = len(items)
    for row in synthesis:
        nums = [index.get(uid) for uid in row.get("evidence") or []]
        if not nums or None in nums:
            return False
        if answer.check_sentence({"text": row["text"], "evidence": nums}, items, cards, scope="note")["check"] != "ok":
            return False
    return bool(synthesis)


_QUESTIONS_HEADING = "\nCandidate open questions:\n"


def _embed_rows(block, synthesis, questions, payload):
    """Keep machine-readable checked sentences and questions inside the managed block so an
    offline render can preserve their exact text and all citation IDs."""
    head, sep, tail = block.partition(_QUESTIONS_HEADING)
    for tag, rows in (("synthesis", synthesis), ("question", questions if sep else [])):
        for row in rows:
            encoded = json.dumps(row, ensure_ascii=False, sort_keys=True).replace("-->", "--\\u003e")
            pattern = rf"(?m)^(- {re.escape(row['text'])}.*)$"
            marker = lambda m, value=encoded, tag=tag: f"{m.group(1)} <!-- kb-{tag}:{value} -->"
            if tag == "synthesis":
                head = re.sub(pattern, marker, head, count=1)
            else:
                tail = re.sub(pattern, marker, tail, count=1)
    return re.sub(r"(?m)(^<!-- kb:synthesis-state:.* -->$)",
                  lambda m: m.group(1) + "\n<!-- kb:synthesis-payload:" + payload + " -->", head + sep + tail, count=1)


def _saved_rows(block, unit_ids):
    """(synthesis rows, question rows) stored by _embed_rows whose evidence units still exist."""
    rows = {"synthesis": [], "question": []}
    for line in (block or "").splitlines():
        match = re.search(r"<!-- kb-(synthesis|question):(\{.*\}) -->$", line) if line.startswith("- ") else None
        if not match:
            continue
        try:
            saved = json.loads(match.group(2))
        except (TypeError, ValueError):
            continue
        if all(uid in unit_ids for uid in saved.get("evidence") or []):
            rows[match.group(1)].append(saved)
    return rows["synthesis"], rows["question"]


_RETIRED_BLOCK = (f"{notes.AUTO_START}\n\n> [!warning] No linked evidence in this vault\n> No paper in this vault is "
                  "linked to this concept any more (it was renamed, re-typed, or its papers were removed). "
                  f"Your own notes outside this block are kept.\n{notes.AUTO_END}")


def _retire_orphans(vault, live):
    """Machine Concept Notes that no linked concept owns any more (renamed/re-typed concept, papers removed).

    Without text a person added they are moved to knowledge_store/removed/ (reversible), so a stale
    note never shadows the canonical one or keeps citing removed evidence; with human text only the
    auto block is replaced by a short notice."""
    retired = []
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    for folder in notes.NODE_FOLDERS:
        directory = config.vault_dir(vault) / folder
        for path in sorted(directory.glob("*.md")) if directory.is_dir() else []:
            if (folder, path.stem.casefold()) in live:
                continue
            text = path.read_text(encoding="utf-8")
            start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
            if not FINGERPRINT.search(text) or text.count(notes.AUTO_START) != 1 or not 0 <= start < end:
                continue
            outside = text[:start] + text[end + len(notes.AUTO_END):]
            _, body = notes._split_frontmatter(outside)
            human = [line for line in (body or "").splitlines() if line.strip() and not line.startswith("# ")
                     and line.strip() not in ("## Notes", "## " + HUMAN_HEADING) and notes.STUB_MARK not in line
                     and not line.startswith("Papers that mention this appear under")]
            if human:
                path.write_text(text[:start] + _RETIRED_BLOCK + text[end + len(notes.AUTO_END):], encoding="utf-8", newline="\n")
            else:
                target = vault_layout.retire(vault, path, "concept_notes", stamp)
            config.log(f"CONCEPT NOTE RETIRED {vault}/{folder}/{path.name} ({'block cleared' if human else 'moved to ' + config.rel(target)})")
            retired.append(path.name)
    return retired


def _selfcheck():
    """Offline refresh keeps checked sentences/questions; re-splicing a note adds no blank lines."""
    import tempfile
    from unittest.mock import patch
    rows = [{"text": "Muse cells rose --> twice.", "evidence": ["p:E0001"]}]
    questions = [{"text": "Why did they rise?", "evidence": ["p:E0001"]}]
    block = "\n".join([notes.AUTO_START, "<!-- kb:synthesis-state:{} -->", "## Key findings", "",
                       "- Muse cells rose --> twice. (cite)", "", "Candidate open questions:",
                       "- Why did they rise? (cite)", notes.AUTO_END]) + "\n"
    embedded = _embed_rows(block, rows, questions, "0" * 40)
    assert _saved_rows(embedded, {"p:E0001"}) == (rows, questions), embedded
    assert _saved_rows(embedded, set()) == ([], [])
    record = {"name": "Muse cell", "type": "cell_type"}
    with tempfile.TemporaryDirectory() as folder, patch.object(config, "VAULTS_DIR", Path(folder)), \
            patch.object(config, "STORE", Path(folder) / "store"), patch.object(config, "ROOT", Path(folder)), \
            patch.object(config, "LOGS_DIR", Path(folder) / "logs"):
        assert _write_note("v", record, embedded, "") == "created"
        path = Path(folder) / "v" / "Organisms" / "Muse cell.md"
        path.write_text(path.read_text(encoding="utf-8") + "my own line\n", encoding="utf-8", newline="\n")
        assert _write_note("v", record, embedded, "") == "unchanged"
        changed = embedded.replace("(cite)", "(cite 2)")
        assert _write_note("v", record, changed, "") == "updated"
        text = path.read_text(encoding="utf-8")
        assert text.endswith(changed + "\n## My notes\n\nmy own line\n"), text[-200:]
        fingerprinted = changed.replace("<!-- kb:synthesis-state", f"<!-- kb:concept-fingerprint:{'0' * 40} -->\n"
                                                                   "<!-- kb:synthesis-state")
        _write_note("v", record, fingerprinted, "")
        _write_note("v", {"name": "Old name", "type": "cell_type"}, fingerprinted, "")
        assert _retire_orphans("v", {("Organisms", "old name"), ("Organisms", "muse cell")}) == []
        assert _retire_orphans("v", {("Organisms", "muse cell")}) == ["Old name.md"]
        assert _retire_orphans("v", set()) == ["Muse cell.md"]
        assert _retire_orphans("v", set()) == []  # a cleared note is left alone afterwards
        assert not (Path(folder) / "v" / "Organisms" / "Old name.md").exists()  # machine-only: moved
        kept = path.read_text(encoding="utf-8")
        assert "my own line" in kept and "kb:concept-fingerprint" not in kept and _RETIRED_BLOCK in kept
        assert list((Path(folder) / "store" / "removed").rglob("Old name.md"))
    return 10


def _with_ancestors(registry, ids):
    """`ids` plus every concept that has one of them as a descendant (a parent's note lists its children's papers)."""
    out, todo = set(ids), list(ids)
    parents = {r["concept_id"]: r.get("parents") or [] for r in registry.records}
    while todo:
        for parent in parents.get(todo.pop(), []):
            if parent not in out:
                out.add(parent)
                todo.append(parent)
    return out


def refresh(con, vault, *args, **kwargs):
    """Rewrite the concept notes of `vault` (see `_refresh`); paper file stamps are read once per paper per run."""
    with structure.stamp_scope():
        return _refresh(con, vault, *args, **kwargs)


def _refresh(con, vault, concept_id=None, allow_ai=False, language=None, revalidate=False, concept_ids=None):
    """`revalidate`: stale syntheses whose every sentence still passes A1–A6 against the current evidence
    become `done` again (use when only structured fields such as intervention/model were added).
    `concept_ids`: refresh only these concepts and their ancestors (what one submitted paper can change);
    like `concept_id`, it skips retiring orphaned notes - `kb.py notes --vault V` does that."""
    ai_config_path = config.CONFIG_DIR / "ai.yaml"
    if language is None:
        raw_config = yaml.safe_load(ai_config_path.read_text(encoding="utf-8")) if ai_config_path.is_file() else {}
        language = ((raw_config or {}).get("concept_notes") or {}).get("language", "vi")
    if language not in ("vi", "en"):
        language = "vi"
    from . import vault_layout
    vault_layout.require_current(vault)
    registry = concepts.load_registry(con)
    wanted = {concept_id} if concept_id else _with_ancestors(registry, concept_ids) if concept_ids is not None else None
    records = [r for r in registry.records if wanted is None or r["concept_id"] in wanted]
    totals = {k: 0 for k in ("created", "updated", "unchanged", "protected", "no_evidence",
                             "mentions_only", "synthesis_pending", "synthesis_passed", "synthesis_failed")}
    ai_unavailable = False
    ai_settings = answer.settings("synthesis")
    written = set()  # (folder, lower-case name) of every concept linked in this vault
    conflict_ids = node_profile._vault_conflict_ids(vault)
    result_index = vault_result_back_links(vault)
    for record in records:
        concept_ids, links, units = _collect(con, record, registry, vault)
        folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
        name = notes.safe_filename(record["name"])
        path = config.vault_dir(vault) / folder / f"{name}.md"
        old_text = path.read_text(encoding="utf-8") if path.exists() else ""
        if not links:
            totals["no_evidence"] += 1
            continue
        if not shown_in_vault(links, units):
            totals["below_threshold"] = totals.get("below_threshold", 0) + 1
            continue
        if links and not units:
            totals["mentions_only"] += 1
        if (folder, name.casefold()) in written:
            # Two concepts map to one Windows file name; the first keeps it (no flip-flopping).
            config.log(f"CONCEPT NOTE NAME CLASH {record['concept_id']} -> {folder}/{name}.md")
            totals["name_clash"] = totals.get("name_clash", 0) + 1
            continue
        written.add((folder, name.casefold()))
        gaps = []  # candidate gaps live in _Maps/Candidate gaps.md, never in node notes
        if not _editable(path, folder, name):
            totals["protected"] += 1
            continue
        profile = node_profile.build(record, vault, links, units, con=con)
        # Machine markers may sit inside folded callouts (D3.79); read them without "> " prefixes.
        old_text = re.sub(r"(?m)^(?:> ?)+", "", old_text) if old_text else old_text
        old_fp = FINGERPRINT.search(old_text) if path.exists() else None
        old_state_match = SYNTHESIS_STATE.search(old_text)
        old_state = json.loads(old_state_match.group(1)) if old_state_match else None
        _, _, pending_status, pending_state = _synthesis(record, units, language)
        old_block = ""
        if old_text and notes.AUTO_START in old_text and notes.AUTO_END in old_text:
            old_block = old_text.split(notes.AUTO_START, 1)[1].split(notes.AUTO_END, 1)[0]
        old_synthesis, old_questions = _saved_rows(old_block, {u["unit_id"] for u in units})
        old_payload_match = re.search(r"(?m)^<!-- kb:synthesis-payload:([0-9a-f]{40}) -->$", old_block)
        current_payload = _payload_fingerprint(record, units, language, pending_state.get("provider", "none"),
                                                pending_state.get("model", ""))
        # Staleness is about the evidence, not about which provider wrote the text: compare with the
        # provider/model that generated the existing synthesis.
        written_payload = _payload_fingerprint(record, units, language, (old_state or {}).get("provider", "none"),
                                               (old_state or {}).get("model", "")) if old_state else current_payload
        same_payload = bool(old_payload_match and old_payload_match.group(1) in (current_payload, written_payload))
        if old_payload_match and not same_payload and old_state and old_state.get("status") == "done":
            # Notes written before 2026-09-26 stamped the payload with whichever provider was configured
            # then; accept any configured provider so an unchanged synthesis is not reported stale.
            same_payload = old_payload_match.group(1) in {
                _payload_fingerprint(record, units, language, p,
                                     (ai_settings.get(p) or {}).get("model", "") if p != "none" else "")
                for p in answer.PROVIDERS}
        synthesis_stale = False
        if old_state and old_state.get("status") == "done" and same_payload:
            synthesis, questions, synthesis_state = old_synthesis, old_questions, old_state
            synthesis_status = "Synthesis checked and retained; selected evidence payload unchanged."
        elif old_state and old_state.get("status") in ("done", "stale") and old_synthesis:
            synthesis, questions = old_synthesis, old_questions
            synthesis_state = {**old_state, "status": "stale"}
            synthesis_status = "Synthesis needs update — evidence changed. Previous checked sentences are retained."
            synthesis_stale = True
            if revalidate and _revalidate(units, old_synthesis):
                synthesis_state = {**old_state, "status": "done", "revalidated_at": config.now()[:10]}
                synthesis_status = ("Synthesis re-checked against the current evidence (A1–A6); "
                                    "all sentences still pass.")
                synthesis_stale = False
        else:
            synthesis, questions, synthesis_status, synthesis_state = [], [], pending_status, pending_state
        already_current = bool(old_state and old_state.get("status") == "done" and same_payload)
        if units and allow_ai and not already_current and not ai_unavailable:
            new = _generate_synthesis(record, units, language)
            totals["synthesis_passed"] += new[4]["passed"]
            totals["synthesis_failed"] += new[4]["failed"]
            if new[3]["status"] == "done" or not synthesis:
                synthesis, questions, synthesis_status, synthesis_state = new[:4]
                synthesis_stale = False
            else:
                # A failed or incomplete AI run never deletes previously checked sentences.
                synthesis_status += f" Latest AI attempt did not replace it: {new[2]}"
            if any(marker in new[2] for marker in _AI_UNAVAILABLE_MARKERS):
                ai_unavailable = True
                totals["ai_unavailable"] = new[2]
        if units and synthesis_state["status"] == "pending":
            totals["synthesis_pending"] += 1
        result_links = result_index.get(name.casefold(), [])
        fingerprint = _fingerprint(record, concept_ids, links, units, {"gaps": gaps, "conflicts": _conflict_signature(units),
                                   "node_profile": profile["source_sha256"], "conflict_notes": conflict_ids,
                                   "result_notes": result_links},
                                   synthesis_state, synthesis, questions)
        # The stored payload belongs to the provider/model that wrote the synthesis (or would write it),
        # so switching providers later does not make a checked synthesis look stale.
        stored_payload = _payload_fingerprint(record, units, language, synthesis_state.get("provider", "none"),
                                              synthesis_state.get("model", ""))
        if (old_fp and old_fp.group(1) == fingerprint and old_payload_match
                and old_payload_match.group(1) == stored_payload
                and not (units and allow_ai and synthesis_state["status"] == "pending")
                and not (record["type"] == "topic" and "\ntype: moc\n" not in old_text)):  # D3.44: topics -> MOC
            totals["unchanged"] += 1
            continue
        block = _embed_rows(_render(record, links, units, gaps, synthesis, questions, synthesis_status,
                                    synthesis_state, fingerprint, registry, synthesis_stale, profile, conflict_ids,
                                    result_links),
                            synthesis, questions, stored_payload)
        result = _write_note(vault, record, block, fingerprint, units, profile, registry)
        totals[result] += 1
    if wanted is None:
        retired = _retire_orphans(vault, written)
        if retired:
            totals["retired"] = len(retired)
            notes.write_indexes(con, vault)  # folder lists must not link the moved notes
    return totals
