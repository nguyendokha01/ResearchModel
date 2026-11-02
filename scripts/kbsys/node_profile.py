"""Source-copied typed node inventories; no AI or scientific acceptance."""
import functools
import hashlib
import json
import re
import sqlite3
import copy
import threading
from collections import OrderedDict
from pathlib import Path

import yaml

from . import config, db, find, presentation, structure, workflow
from . import sentences as sentence_utils

try:
    from . import node_cards
except ImportError:  # D3.46 is allowed to land after this renderer.
    node_cards = None

VERSION = 1
RESULT_FIELDS = {
    "model": ("species", "cell_type", "stage", "origin", "setting", "condition"),
    "intervention": ("agent", "dose", "timing", "duration", "protocol", "comparator", "route"),
    "outcome": ("measure", "method", "direction", "values", "value_source", "n", "p", "source_objects"),
    "quality": ("design", "replicates", "is_primary")}
FILES = ("sentences.jsonl", "metadata.json", "structure.json", "protocol_profile.json",
         "section_profile.json", "evidence.jsonl", "figures.jsonl", "tables.jsonl",
         "media_manifest.json", "analysis.json", "references.jsonl")
FIELDS = ("definitions", "full_names", "source_classes", "purpose", "roles_context",
          "significance", "limitations", "markers")
DETAIL_FIELDS = ("technique", "chemical", "concentration", "dose", "equipment", "model",
                 "temperature", "duration", "timing", "comparator", "route")
# Exact fragments in Methods; their assignment to a chemical/step is deliberately
# unannotated. A percentage candidate is not asserted to be the node's dose.
CONDITION_PATTERNS = {
    "concentration": r"(?<![\w.+\-−])(?:[-−]?\d+(?:\.\d+)?)\s*(?:%|[munpμµ]?M\b|(?:mg|µg|μg|ng|g)/m[lL]\b)",
    "dose": r"(?<![\w.+\-−])(?:[-−]?\d+(?:\.\d+)?)\s*(?:mg|µg|μg|ng|g)(?:/kg)?\b",
    "duration": r"(?<![\w.+\-−])(?:[-−]?\d+(?:\.\d+)?)\s*(?:s|sec(?:onds?)?|min(?:utes?)?|h|hours?|days?)\b",
    "temperature": r"(?<![\w.+\-−])(?:[-−]?\d+(?:\.\d+)?)\s*°\s*[CF]\b"}


# --- N3d: deterministic sentence typing.  A sentence enters a note section only when it has the section's own form.
def _name_regex(term, aliases=()):
    """Whole-token regex for the node name / aliases (plural -s/-es allowed); acronyms stay case-sensitive."""
    names = sorted({str(n).strip() for n in (term, *aliases) if n and str(n).strip()}, key=len, reverse=True)
    if not names:
        return None
    alt = "|".join(re.escape(n) for n in names)
    flags = 0 if all(re.fullmatch(r"[A-Z][A-Z0-9]+", n) for n in names) else re.I
    return re.compile(r"(?<![\w-])(?:" + alt + r")(?:s|es)?(?!\w)", flags)


_PAREN_ABBR = r"(?:\s*\([^)]{1,60}\))?"
_COPULA = (r"(?:is|are)\s+(?:an?|the|one|any|those|that|defined|known|called|termed|referred|considered|"
           r"characteri[sz]ed|composed|comprised)\b")
_PURPOSE = r"(?:is|are)\s+(?:typically\s+|commonly\s+|widely\s+)?used\s+(?:to|for)\b"


_NOT_SUBJECT = frozenset("of and in for with by to on from between at as than or".split())


def _is_subject(prefix):
    """What stands before the name: nothing, or at most two words that are not prepositions / conjunctions, so the
    name is the subject (\"Cryopreservation of oocytes is an…\" is about cryopreservation, not oocytes)."""
    words = re.findall(r"[A-Za-z0-9'-]+", re.split(r"[;,:]", str(prefix or ""))[-1])  # the clause the name opens
    return len(words) <= 2 and not any(word.casefold() in _NOT_SUBJECT for word in words)


def _subject_matches(patterns, text, flags, opening=()):
    """A pattern counts when the name is the subject; patterns in ``opening`` also need the name to open the sentence."""
    text = str(text or "")
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags):
            prefix = text[:match.start()]
            if _is_subject(prefix) and (pattern not in opening or re.fullmatch(r"[\s\W]*(?:the|an?)?[\s\W]*", prefix, re.I)):
                return True
    return False


def definitional(text, term, aliases=()):
    """``X is/are a…``, ``X refers to…``, ``X, a…`` with THIS node's name or alias X as the subject, or ``…defines X as``."""
    name = _name_regex(term, aliases)
    if name is None:
        return False
    n = name.pattern
    copula = n + _PAREN_ABBR + r"\s+(?:" + _COPULA + r"|refers?\s+to\b|means\b)"
    apposition = n + _PAREN_ABBR + r",\s+an?\s+\w"
    return (_subject_matches((copula, apposition), text, name.flags, opening=(apposition,))
            or bool(re.search(r"\bdefin(?:es?|ed)\s+(?:the\s+)?(?:term\s+)?" + n + _PAREN_ABBR + r"\s+as\b", str(text or ""), name.flags)))


def purposive(text, term, aliases=()):
    """``X is used to/for…`` (X = THIS node as subject) or ``the purpose of X``."""
    name = _name_regex(term, aliases)
    if name is None:
        return False
    n = name.pattern
    return (_subject_matches((n + _PAREN_ABBR + r"\s+" + _PURPOSE,), text, name.flags)
            or bool(re.search(r"\b(?:purpose|aim|goal)\s+of\s+(?:the\s+)?" + n, str(text or ""), name.flags)))


def states_marker(text, term, aliases=()):
    """The sentence states a marker OF this cell type: ``marker(s) of/for X``, ``X marker(s)``, ``X (cells) express…`` or
    ``X … CD34+``.  A sentence that only contains the name and the word negative / positive is not one."""
    name = _name_regex(term, aliases)
    if name is None:
        return False
    n, text = name.pattern, str(text or "")
    patterns = (r"\bmarkers?\s+(?:of|for)\s+(?:the\s+)?" + n,
                n + r"(?:-specific|-derived)?\s+markers?\b",
                n + r"(?:\s+cells?)?\s+(?:also\s+)?express(?:es|ed|ing)?\b",
                n + r"[^.;]{0,40}?(?<=\w)[+](?![\w+])")
    return any(re.search(p, text, name.flags) for p in patterns)


def concentration_near(text, value, term, aliases=(), window=30):
    """``value`` (a concentration/dose fragment) has THIS node's name inside it or within ``window`` characters."""
    name = _name_regex(term, aliases)
    if name is None:
        return False
    value, text = str(value or ""), str(text or "")
    if name.search(value):
        return True
    for match in re.finditer(re.escape(value), text):
        if name.search(text[max(0, match.start() - window):match.end() + window]):
            return True
    return False


STATUS_LABELS = {"ai_proposed": "proposed — needs human review", "proposed": "proposed — needs human review",
                 "human_confirmed": "human-confirmed", "rejected": "rejected",
                 "human_rejected": "rejected", "stale": "stale — source/review needs update"}
OPEN_CONFLICT_STATUSES = {"ai_proposed", "proposed", "human_confirmed"}
_BUILD_CACHE = OrderedDict()
_BUILD_CACHE_LIMIT = 24
_BUILD_CACHE_GUARD = threading.RLock()


def clear_build_cache():
    """Clear process-local read profiles; used by tests and embedded callers."""
    with _BUILD_CACHE_GUARD:
        _BUILD_CACHE.clear()


def card_for(vault, concept_id):
    """Load only an accepted/current card; a missing D3.46 module is normal."""
    if node_cards is None or not hasattr(node_cards, "load"):
        return None
    try:
        card = node_cards.load(vault, concept_id)
    except (OSError, ValueError, TypeError, AttributeError, sqlite3.Error):
        return None
    return card if isinstance(card, dict) else None


def graph_node_type(profile, record_type=None, record=None):
    # A registry type set by a rule, a decision or a person (classified_by not `terms`) beats an older card's node_type (ND-09).
    card = {} if (record or {}).get("classified_by", "terms") != "terms" else profile.get("card") or {}
    raw = card.get("node_type") or profile.get("entity_kind") or record_type or "concept"
    raw = str(raw).strip().casefold()
    special = {"topics": "moc", "moc": "moc", "cell type": "cell_type", "substance": "substance",
               "organism": "organism", "result": "result", "claim": "claim", "conflict": "conflict"}
    # One shared kind->graph-type table (presentation.GRAPH_TYPES) so every kind gets a palette colour.
    return special.get(raw) or presentation.graph_type_for_kind(raw) or raw


def _card_status(card):
    if not card:
        return "pending"
    raw = str(card.get("card_status") or card.get("status") or "accepted").strip().casefold()
    if raw in ("stale", "outdated"):
        return "stale"
    if raw in ("pending", "proposed", "ai_proposed", "draft"):
        return "pending"
    return "accepted"


def card_currency_notice(card):
    """Return a non-blocking notice when a readable card has new evidence."""
    if not isinstance(card, dict) or card.get("card_currency") != "current_with_new_evidence":
        return []
    count = len(card.get("new_paper_ids") or [])

    return [presentation.TEXT_NEW_EVIDENCE.format(count=count), ""]


def result_quality_label(result):
    """Carry review/heuristic provenance into every rendered result row."""
    source = " ".join(str(ref.get("text") or "") for ref in result.get("sources") or ())
    if re.search(r"\b(?:no significant(?: difference| change)?|not significantly|did not significantly|non-significant|non significant)\b", source, re.I):
        return presentation.TEXT_NO_DIFFERENCE
    flags = result.get("flags") or []
    codes = []
    for flag in flags if isinstance(flags, list) else [flags]:
        code = (flag.get("code") or flag.get("label")) if isinstance(flag, dict) else flag
        if code:
            codes.append(str(code))
    if result.get("status") in ("review", "needs_review") or codes or result.get("provenance") == "heuristic_draft":
        return "review" + (" · " + ", ".join(dict.fromkeys(codes)) if codes else "")
    return ""


def _card_field_value(card, name):
    fields = card.get("fields") if isinstance(card, dict) else {}
    value = fields.get(name) if isinstance(fields, dict) else None
    if value is None and isinstance(card, dict):
        value = card.get(name)
    if isinstance(value, dict):
        return value.get("value", value.get("text"))
    return value


def _link_names(value):
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = value.get("value", value.get("names", value.get("links", [])))
    if not isinstance(value, (list, tuple, set)):
        return []
    result = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("name", item.get("target", item.get("value")))
        if item:
            text = str(item).strip()
            text = re.sub(r"^\[\[|\]\]$", "", text)
            if text:
                result.append(text)
    return list(dict.fromkeys(result))


def _readable_value(value):
    """Render structured values without leaking Python/JSON containers into notes."""
    if value is None or value == "":
        return "—"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            shown = _readable_value(item)
            if shown != "—":
                parts.append(f"{key}: {shown}")
        return "; ".join(parts) or "—"
    if isinstance(value, (list, tuple, set)):
        parts = [_readable_value(item) for item in value]
        return ", ".join(item for item in parts if item != "—") or "—"
    return " ".join(str(value).split()) or "—"


def _readable_fields(value):
    """Return one readable ``key: value`` line per structured field."""
    if not isinstance(value, dict):
        return [_readable_value(value)]
    lines = []
    for key, item in value.items():
        shown = _readable_value(item)
        if shown != "—":
            lines.append(f"{key}: {shown}")
    return lines or ["—"]


def _vault_note_names(vault):
    root = config.vault_dir(vault)
    names = {}
    if not root.is_dir():
        return names
    for path in root.rglob("*.md"):
        names.setdefault(path.stem.casefold(), path.stem)
    return names


def _vault_conflict_ids(vault):
    """Map exact conflict frontmatter IDs to note stems; filenames are not IDs."""
    root = config.vault_dir(vault)
    claims = root / "Claims"
    result = {}
    ambiguous = set()
    for path in sorted(claims.glob("*.md")) if claims.is_dir() else []:
        try:
            text = path.read_text(encoding="utf-8")
            if not text.startswith("---\n"):
                continue
            end = text.find("\n---\n", 4)
            if end < 0:
                continue
            values = yaml.safe_load(text[4:end]) or {}
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if (isinstance(values, dict) and values.get("type") == "conflict" and
                values.get("id") and not values.get("retired")):
            ident = str(values["id"])
            if ident in result:
                ambiguous.add(ident)
            result[ident] = path.stem
    return {ident: stem for ident, stem in result.items() if ident not in ambiguous}


def _quoted_links(names, known):
    resolved, unresolved = [], []
    for name in _link_names(names):
        stem = known.get(name.casefold())
        if stem:
            resolved.append(f'[[{stem}]]')
        else:
            unresolved.append(name)
    return list(dict.fromkeys(resolved)), list(dict.fromkeys(unresolved))


def typed_machine_properties(record, profile, vault, existing=None):
    """Return machine-owned node properties without replacing human keys."""
    existing = dict(existing or {})
    card = profile.get("card") or {}
    values = {"type": graph_node_type(profile, record.get("type"), record), "agent_type": "automated",
              "evidence_count": profile.get("evidence_count", 0),
              "papers_n": profile.get("papers_n", len(profile.get("papers", []))),
              "card_status": _card_status(card)}
    open_rows = [r for r in profile.get("conflicts", [])
                 if r.get("status") in OPEN_CONFLICT_STATUSES]
    values["conflict_open"] = len(open_rows)
    values["has_conflict"] = bool(open_rows)
    known = _vault_note_names(vault)
    unresolved = []
    typed = {"measures": "measures", "measured_by": "measured_by", "in_taxon": "in_taxon",
             "uses_technique": "uses_technique", "reagent_of": "reagent_of", "marker_of": "marker_of",
             "derived_from": "derived_from", "parent_technique": "parent_technique"}
    for key, field in typed.items():
        links, missing = _quoted_links(_card_field_value(card, field), known)
        if links:
            values[key] = links
        elif field in card or (isinstance(card.get("fields"), dict) and field in card["fields"]):
            values[key] = []
        unresolved.extend(missing)
    moc_links, missing = _quoted_links(_card_field_value(card, "moc") or
                                       _card_field_value(card, "topics") or card.get("moc"), known)
    if moc_links:
        values["moc"] = moc_links
    unresolved.extend(missing)
    if unresolved or "related_unresolved" in existing:
        values["related_unresolved"] = list(dict.fromkeys(unresolved))

    raw_type = graph_node_type(profile)
    field_map = {
        "substance": ("substance_class", "chem_roles", "study_role", "abbreviation", "full_name"),
        "technique": ("category",), "organism": ("taxon_rank", "common_name"),
        "cell_type": ("developmental_stage",),
    }
    for field in field_map.get(raw_type, ()):
        value = _card_field_value(card, field)
        if value not in (None, "", []):
            values[field] = value
    if "status" not in existing:
        values["status"] = "machine"
    # ``status`` is human-owned once present.  Unknown frontmatter is retained by
    # the caller; this function only returns keys owned by the renderer.
    return values


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _field(items=()):
    return {"status": "source_candidates" if items else "not_annotated", "items": list(items),
            "reason": None if items else "No matching source annotation found in this vault."}


@functools.lru_cache(maxsize=50000)  # the same sentence is shown for every concept it supports
def _qualifiers(text):
    return tuple(structure.uncertainty(text))


def _ref(row, paper_id, meta):
    text = row.get("text") or ""
    return {"paper_id": paper_id, "paper_title": meta.get("title"), "note_name": meta.get("note_name"),
            "doi": meta.get("doi"), "pmid": meta.get("pmid"), "evidence_type": meta.get("evidence_type"),
            "sentence_id": row.get("sentence_id"), "passage_id": row.get("passage_id"),
            "page": row.get("page"), "section": row.get("section"), "subsection": row.get("subsection"),
            "source": row.get("source", "main"), "text": text,
            "citations": row.get("citations") or [],
            "cited_refs": [meta["_references"][n] for n in dict.fromkeys(n for c in row.get("citations") or [] for n in c.get("ref_nums", [])) if n in meta.get("_references", {})],
            "mentions": row.get("mentions") or [],
            "continues": row.get("continues"), "qualifiers": [dict(q) for q in _qualifiers(text)]}


_SOURCE_IDS_CACHE = {}


def _source_ids(ids, sentences, cache=None, paper_id=None, process_cache=True):
    """Bounded explicit chains plus labelled adjacent-page fragments; never edits source IDs."""
    cache_key = tuple(dict.fromkeys(ids or ()))
    if isinstance(cache, dict) and cache_key in cache:
        wanted, basis = cache[cache_key]
        return set(wanted), dict(basis)
    candidate = paper_id or next((str(sid).split(':', 1)[0] for sid in cache_key if ':' in str(sid)), None)
    global_key = None
    sentence_path = db.paper_dir(candidate) / "sentences.jsonl" if candidate else None
    if process_cache and candidate and sentence_path.is_file():
        try:
            global_key = (structure.paper_read_stamp(candidate), cache_key)
            cached = _SOURCE_IDS_CACHE.get(global_key)
            if cached is not None:
                wanted, basis = cached
                if isinstance(cache, dict):
                    cache[cache_key] = (set(wanted), dict(basis))
                return set(wanted), dict(basis)
        except (OSError, ValueError, TypeError):
            global_key = None
    wanted=set(ids or []);basis={sid:(sentences.get(sid, {}).get("context_basis") or "source_anchor_or_continues")
                                     for sid in wanted}
    if isinstance(cache, dict):
        index = cache.get("__source_index__")
        if index is None:
            backwards, groups = {}, {}
            for row in sentences.values():
                nxt=row.get("continues")
                if nxt in sentences and row.get("source","main")==sentences[nxt].get("source","main"):
                    backwards.setdefault(nxt,[]).append(row["sentence_id"])
                if (row.get("kind", "text") == "text" and not row.get("in_table")
                        and isinstance(row.get("seq"), int)):
                    groups.setdefault((row.get("source", "main"), row.get("section")), []).append(row)
            preceding = {}
            for rows in groups.values():
                rows.sort(key=lambda row: row["seq"])
                prior = []
                for row in rows:
                    for previous in reversed(prior):
                        delta = row["seq"] - previous["seq"]
                        if delta > 16:
                            break
                        if delta > 0:
                            preceding[row["sentence_id"]] = previous
                            break
                    prior.append(row)
            index = {"backwards": backwards, "preceding": preceding}
            cache["__source_index__"] = index
        backwards = index["backwards"]
    else:
        backwards={}
        for row in sentences.values():
            nxt=row.get("continues")
            if nxt in sentences and row.get("source","main")==sentences[nxt].get("source","main"):
                backwards.setdefault(nxt,[]).append(row["sentence_id"])
    for anchor in list(wanted):
        for reverse in (False,True):
            current=anchor;seen=set()
            while current in sentences and current not in seen and len(seen)<8:
                seen.add(current);wanted.add(current)
                basis.setdefault(current, sentences[current].get("context_basis") or "source_anchor_or_continues")
                previous=backwards.get(current,[])
                nxt=previous[0] if reverse and len(previous)==1 else None if reverse else sentences[current].get("continues")
                if nxt not in sentences or sentences[nxt].get("source","main")!=sentences[current].get("source","main"):break
                current=nxt
        row=sentences.get(anchor,{})
        # Older extraction can omit continues across a Figure/caption at a page break.
        # Copy the last body fragment on the preceding page as CONTEXT, not an inferred
        # scientific relationship or a rewritten combined sentence.
        if row.get("kind","text")!="text" or not isinstance(row.get("page"),int):continue
        if isinstance(cache, dict):
            prev = index["preceding"].get(anchor)
        else:
            before=[s for s in sentences.values() if s.get("kind","text")=="text"
                    and not s.get("in_table") and s.get("section")==row.get("section")
                    and s.get("source","main")==row.get("source","main")
                    and isinstance(s.get("seq"),int) and isinstance(row.get("seq"),int)
                    and 0<row["seq"]-s["seq"]<=16]
            prev=max(before,key=lambda s:s["seq"],default=None)
        if prev and prev.get("page")==row["page"]-1 and not re.search(r'[.!?:]["”\']?$',(prev.get("text") or "").strip()):
            sid=prev["sentence_id"];wanted.add(sid);basis.setdefault(sid,"preceding_source_fragment")
    if isinstance(cache, dict):
        cache[cache_key] = (set(wanted), dict(basis))
    if process_cache and global_key is not None:
        _SOURCE_IDS_CACHE[global_key] = (set(wanted), dict(basis))
    return wanted,basis


def _refs(ids, sentences, paper_id, meta, linked=False, source_cache=None):
    # Direct callers may provide legacy rows without the D3.40 links.  Keep
    # this defensive read-time repair in memory; normal paper loading below
    # uses the signature-keyed cache.
    if not linked:
        sentences = {row["sentence_id"]: row for row in sentence_utils.link_page_breaks_copy(sentences.values())}
    wanted,basis=_source_ids(ids, sentences, source_cache,
                             paper_id=paper_id if linked else None,
                             process_cache=linked)
    predecessors = {s.get("continues") for s in sentences.values() if s.get("continues")}
    refs = []
    for s in sentences.values():
        sid = s.get("sentence_id")
        if sid not in wanted:
            continue
        ref = {**_ref(s, paper_id, meta), "context_basis": basis[sid]}
        text = str(ref.get("text") or "").lstrip()
        linked = (sid in predecessors or ref.get("context_basis") == "source_continuation"
                  or bool(s.get("continues")))
        if not linked and text[:1].isalpha() and text[:1].islower():
            ref["text"] = "[…] " + text
        refs.append(ref)
    return refs


def _copied_fragment(value, refs):
    if not isinstance(value, str) or not value.strip():
        return None
    for ref in refs:
        match = re.search(r"(?<![\w.+\-−])" + re.escape(value.strip()) + r"(?!\w|\.\d|/\w)",
                          ref["text"], re.I)
        if match:
            return match.group()
    return None


def _inventory_details(refs):
    details = {field: _field() for field in DETAIL_FIELDS}
    for field, pattern in CONDITION_PATTERNS.items():
        for ref in refs:
            for match in re.finditer(pattern, ref["text"]):
                details[field]["items"].append({"value": match.group(), "sources": [ref],
                                               "assignment": "unresolved — source fragment; chemical/step association not annotated"})
        if details[field]["items"]:
            details[field]["status"], details[field]["reason"] = "source_candidates", None
    return details


def _protocols(folder, sentences, paper_id, meta, record, linked=False, completion_cache=None,
               source_cache=None):
    term, aliases = record["name"], record.get("synonyms") or []
    profile = structure._read_profile(folder / "protocol_profile.json")
    if isinstance(completion_cache, dict) and paper_id in completion_cache:
        readiness = completion_cache[paper_id]["protocol"]
    else:
        completion = structure.completion(paper_id)
        if isinstance(completion_cache, dict):
            completion_cache[paper_id] = completion
        readiness = completion["protocol"]
    current = readiness["ready"] and readiness["status"] == "current"
    variants = []
    if current:
        for i, protocol in enumerate(profile.get("protocols") or []):
            refs = _refs(protocol.get("sentence_ids"), sentences, paper_id, meta, linked, source_cache)
            if not any(presentation.matches_term(r["text"], term, aliases) for r in refs):
                continue
            if not refs or any(r["section"] != "methods" for r in refs):
                continue
            details = {f: _field() for f in DETAIL_FIELDS}
            for detail in protocol.get("details") or []:
                cited = _refs(detail.get("sentence_ids"), sentences, paper_id, meta, linked, source_cache)
                key = detail.get("field")
                copied = _copied_fragment(detail.get("value"), cited)
                if key in details and cited and all(r["section"] == "methods" for r in cited) and copied is not None:
                    details[key]["items"].append({"value": copied, "sources": cited})
                    details[key]["status"], details[key]["reason"] = "source_candidates", None
            step_ids = {sid for step in protocol.get("steps") or [] for sid in step.get("sentence_ids") or []}
            steps = [r for r in _refs(step_ids, sentences, paper_id, meta, linked, source_cache) if r["section"] == "methods"]
            variants.append({"variant_id": f"{paper_id}:protocol:{i+1}", "paper_id": paper_id,
                             "name": _copied_fragment(protocol.get("name"), refs),
                             "basis": "current structured protocol; source recopied",
                             "status": profile.get("status", "not_annotated"), "sources": refs,
                             "steps": steps, "details": details})
    # Complete relevant Methods subsection fallback, explicitly an inventory.
    covered = {r["sentence_id"] for p in variants for r in p["sources"]}
    groups = {}
    for s in sentences.values():
        if s.get("section") == "methods" and not s.get("in_table"):
            groups.setdefault((s.get("source", "main"), s.get("subsection")), []).append(s)
    for (source, heading), rows in groups.items():
        if not any(presentation.matches_term(s.get("text"), term, aliases) and s["sentence_id"] not in covered for s in rows):
            continue
        refs = [_ref(s, paper_id, meta) for s in rows]
        variants.append({"variant_id": f"{paper_id}:methods:{len(variants)+1}", "paper_id": paper_id,
                         "name": heading, "basis": "Methods subsection inventory; procedure boundaries not annotated",
                         "status": "not_annotated", "sources": refs, "steps": refs,
                         "details": _inventory_details(refs)})
    return variants, "current" if current else "missing_or_stale"


def _paper(record, paper_id, selected_units, completion_cache=None, source_cache=None):
    folder = db.paper_dir(paper_id)
    meta = dict(db.read_json(folder / "metadata.json", {}) or {})
    # Publication metadata takes precedence over generated analysis labels; this
    # changes only the derivative view, never the paper's stored metadata.
    meta["evidence_type"] = find.effective_evidence_type(meta)
    meta["_references"] = {r["num"]:r for r in db.read_jsonl(folder/"references.jsonl") if r.get("num") is not None}
    sentence_path = folder / "sentences.jsonl"
    paper_source_cache = (source_cache.setdefault(paper_id, {})
                          if isinstance(source_cache, dict) else None)
    sentences = {s["sentence_id"]: s for s in sentence_utils.cached_linked_rows(db.read_jsonl(sentence_path), sentence_path)
                 if s.get("sentence_id") and s.get("section") not in ("references", "front_matter")
                 and (not s.get("in_table") or s.get("kind") == "table_row")}
    term, aliases = record["name"], record.get("synonyms") or []
    mentions = [s for s in sentences.values() if presentation.matches_term(s.get("text"), term, aliases)]
    fields = {k: [] for k in FIELDS}
    for s in mentions:
        text, ref = s.get("text") or "", _ref(s, paper_id, meta)
        # N3d: each field takes only sentences of its own form (see definitional / purposive / states_marker);
        # Methods and Results sentences are never definitions, and a Methods sentence is never a marker statement.
        section = s.get("section")
        if section not in ("methods", "results") and definitional(text, term, aliases):
            fields["definitions"].append(ref)
            if re.search(r"\b(?:is|are)\s+(?:an?\s+)?(?:\w+[ -]){0,5}(?:protein|hormone|marker|enzyme|chemical|agent|class|family|technique|method)\b", text, re.I):
                fields["source_classes"].append(ref)
        for match in structure.PAREN.finditer(text):
            expansion = structure._definition(match[1], match[2])
            if expansion and (presentation.matches_term(match[2], term, aliases) or
                              presentation.matches_term(expansion, term, aliases)):
                fields["full_names"].append({"value": expansion, "abbreviation": match[2], "sources": [ref]})
        if section not in ("methods", "results") and purposive(text, term, aliases):
            fields["purpose"].append(ref)
        if section != "methods" and states_marker(text, term, aliases):
            fields["markers"].append(ref)
        if re.search(r"\b(?:important|essential|critical|significance|role|necessary)\b", text, re.I):
            fields["significance"].append(ref)
        if re.search(r"\b(?:limitation|limited|caution|however|cannot|not necessarily)\b", text, re.I):
            fields["limitations"].append(ref)
    passages = {(s.get("source", "main"), s.get("passage_id")) for s in mentions if s.get("passage_id")}
    context = [_ref(s, paper_id, meta) for s in sentences.values()
               if (s.get("source", "main"), s.get("passage_id")) in passages]
    results, role_rows = [], []
    for unit in selected_units:
        if unit.get("paper_id") != paper_id:
            continue
        refs = _refs(unit.get("sentence_ids"), sentences, paper_id, meta, linked=True,
                     source_cache=paper_source_cache)
        source_unit = {**unit, "quote": " ".join(r["text"] for r in refs)}
        roles = presentation.roles_for_unit(term, source_unit, aliases)
        if roles:
            role_rows.append({"unit_id": unit.get("unit_id"), "roles": roles, "status": unit.get("status"),
                              "intervention": unit.get("intervention"), "model": unit.get("model"), "sources": refs})
        if unit.get("unit_type") == "limitation" and refs:
            fields["limitations"].extend(refs)
        if unit.get("unit_type") == "result":
            scientific = {group: {**dict.fromkeys(keys), **(unit.get(group) or {})}
                          for group, keys in RESULT_FIELDS.items()}
            outcome = scientific["outcome"]
            if outcome.get("value_source") == "figure_only":
                for key in ("values", "n", "p"):
                    outcome[key] = None
            objects = sorted(set(outcome.get("source_objects") or []) |
                             {obj for r in refs for obj in r["mentions"]})
            results.append({"unit_id": unit.get("unit_id"), "paper_id": paper_id, "status": unit.get("status"),
                            "source_status": "resolved" if refs and set(unit.get("sentence_ids") or []).issubset({r["sentence_id"] for r in refs}) else "unresolved",
                            "statement": unit.get("statement") or "", "quote": unit.get("quote") or "",
                            "sentence_ids": unit.get("sentence_ids") or [],
                            "evidence_type": meta.get("evidence_type"), "model": scientific["model"],
                            "intervention": scientific["intervention"], "outcome": outcome,
                            "quality": {**scientific["quality"], "is_primary": False} if meta["evidence_type"] in find.REVIEW_TYPES else scientific["quality"],
                            "stored_quality": scientific["quality"], "hedging": unit.get("hedging"),
                            "flags": unit.get("flags") or [], "provenance": unit.get("provenance"),
                            "sources": refs, "source_objects": objects,
                            "field_status": "stored annotation; verify against copied sources"})
    fields["roles_context"] = role_rows
    variants, protocol_status = _protocols(folder, sentences, paper_id, meta, record, linked=True,
                                           completion_cache=completion_cache, source_cache=paper_source_cache)
    return {"paper_id": paper_id, "paper_title": meta.get("title"), "note_name": meta.get("note_name"),
            "cite": (str(meta.get("first_author") or meta.get("note_name") or paper_id) + " " + str(meta.get("year") or "")).strip(),
            "evidence_type": meta["evidence_type"], "source_availability": "extracted_text_available" if sentences else "unavailable",
            "fields": {k: _field(v) for k, v in fields.items()}, "context": context,
            "mentions": [_ref(s, paper_id, meta) for s in mentions], "protocol_variants": variants,
            "protocol_profile_status": protocol_status, "results": results}


def _relations(vault, paper_ids, units, con=None, source_cache=None):
    from . import evidence
    allowed = {r[0] for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault=?", (vault,))} if con is not None else set(paper_ids)
    known={u["unit_id"]:u for u in units}
    selected=set(known)
    rows=[]
    from . import relations as relation_store
    for raw in relation_store.conflict_relations(vault, history=True):
        relation=dict(raw); sides=[]; resolved=[]
        for key in ("from_unit", "to_unit"):
            uid=relation.get(key) or ""
            unit=known.get(uid)
            if unit is None and con is not None:
                try: unit=evidence.resolve_unit(con,uid)
                except ValueError:
                    unit=None
            if unit: relation[key]=unit["unit_id"]
            resolved.append(unit)
        if not selected.intersection({relation.get("from_unit"),relation.get("to_unit")}): continue
        if not all(str(relation.get(k) or "").rsplit(":",1)[0] in allowed for k in ("from_unit","to_unit")): continue
        for key,unit in zip(("from_unit","to_unit"),resolved):
            uid=relation.get(key) or ""; pid=uid.rsplit(":",1)[0]
            if unit is None:
                unit=next((u for u in db.read_jsonl(db.paper_dir(pid)/"evidence.jsonl") if u.get("unit_id")==uid),None)
            meta=db.read_json(db.paper_dir(pid)/"metadata.json",{}) or {}
            meta={**meta,"evidence_type":find.effective_evidence_type(meta)}
            sentence_path = db.paper_dir(pid) / "sentences.jsonl"
            source={s["sentence_id"]:s for s in sentence_utils.cached_linked_rows(db.read_jsonl(sentence_path), sentence_path) if s.get("section") not in ("abstract","references","front_matter")}
            ids=(unit or {}).get("sentence_ids") or []
            paper_source_cache = (source_cache.setdefault(pid, {})
                                  if isinstance(source_cache, dict) else None)
            refs=_refs(ids,source,pid,meta,linked=True,source_cache=paper_source_cache)
            sides.append({"items":refs,"unresolved_ids":[sid for sid in ids if sid not in source] if ids else [uid]})
        relation["source_sides"]=sides
        relation["display_status"]=relation.get("status") if all(s["items"] and not s["unresolved_ids"] for s in sides) else "stale"
        rows.append(relation)
    return rows,allowed


def build(record, vault, paper_ids, units=(), persist=True, con=None, card=None, card_loaded=False):
    """Caller supplies only vault-linked IDs. persist=False is completely read-only.

    Byte-valid cache reuse is not scientific acceptance. Read each paper separately;
    retain only relevant source snippets. Full searchable text stays in the store.
    """
    paper_ids = sorted(set(paper_ids))
    units = list(units)
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", p) for p in paper_ids):
        raise ValueError("Invalid paper ID")
    if not card_loaded:
        card = card_for(vault, record["concept_id"])
    completion_cache = {}
    source_cache = {}
    conflicts,allowed=_relations(vault,paper_ids,units,con,source_cache)
    dependency_papers=set(paper_ids)|{str(r[k]).rsplit(":",1)[0] for r in conflicts for k in ("from_unit","to_unit")}
    paths = [db.paper_dir(p) / f for p in sorted(dependency_papers) for f in FILES]
    for pid in sorted(dependency_papers):
        meta=db.read_json(db.paper_dir(pid)/"metadata.json",{}) or {}
        paths += [(Path(s["path"]) if Path(s["path"]).is_absolute() else config.ROOT/Path(s["path"])) for s in meta.get("source_files",[]) if s.get("path")]
    paths += [config.STORE / "relations.jsonl", config.STORE / "measure_groups.json"]
    paths += [Path(__file__), Path(presentation.__file__), Path(structure.__file__),
              Path(find.__file__), Path(__file__).with_name("concept_notes.py")]
    extra = {"version": VERSION, "record": record, "vault": vault, "papers": paper_ids, "units": units,
             "vault_membership": sorted(allowed), "card": card}
    signature = workflow.fingerprint(paths, extra)
    target = config.STORE / "cache" / "node_profiles" / (_digest([vault, record["concept_id"]])[:32] + ".json")
    try:
        cached = db.read_json(target, {}) or {}
        payload = cached.get("profile")
        if cached.get("input_sha256") == signature and isinstance(payload, dict) and cached.get("output_sha256") == _digest(payload):
            with _BUILD_CACHE_GUARD:
                _BUILD_CACHE.pop(signature, None)
                _BUILD_CACHE[signature] = {
                    "profile": copy.deepcopy(payload),
                    "output_sha256": workflow.fingerprint([target]),
                }
            return {**payload, "cache_hit": True}
    except (OSError, ValueError, TypeError):
        pass
    with _BUILD_CACHE_GUARD:
        cached_entry = _BUILD_CACHE.pop(signature, None)
        if cached_entry is not None:
            _BUILD_CACHE[signature] = cached_entry
    if isinstance(cached_entry, dict) and "profile" in cached_entry:
        payload = cached_entry.get("profile")
        output_sha256 = cached_entry.get("output_sha256")
    else:
        payload = None
        output_sha256 = None
    # The in-process entry is only an acceleration for the durable derivative;
    # preserve the established output-invalidation contract when that file was
    # removed, moved, or changed by another process.
    if (payload is not None and
            (not target.is_file() or output_sha256 != workflow.fingerprint([target]))):
        payload = None
    if payload is not None:
        if persist:
            workflow.atomic_json(target, {"input_sha256": signature,
                                          "output_sha256": _digest(payload),
                                          "profile": payload})
        return {**copy.deepcopy(payload), "cache_hit": True}
    metadata = presentation.metadata_for_term(record["name"], record.get("type"), units, record.get("synonyms") or [])
    profile = {"schema_version": VERSION, "node_id": record["concept_id"], "vault": vault,
               "name": record["name"], "aliases": record.get("synonyms") or [],
               "entity_kind": metadata["entity_kind"], "category_basis": metadata["category_basis"],
               "status": "source_inventory", "scientific_acceptance": "not_assessed",
               "evidence_count": len(units), "papers_n": len(paper_ids),
               "source_sha256": signature,
               "papers": [_paper(record, p, units, completion_cache, source_cache) for p in paper_ids],
               "conflicts": conflicts, "card": card,
               "scope_note": "Lexical candidates and stored annotations require source review. Full extracted paper text remains in the machine store; this view does not replace it."}
    if workflow.fingerprint(paths, extra) != signature:
        raise ValueError("Node source changed during build; retry against the current revision")
    if persist:
        workflow.atomic_json(target, {"input_sha256": signature, "output_sha256": _digest(profile), "profile": profile})
        with _BUILD_CACHE_GUARD:
            _BUILD_CACHE.pop(signature, None)
            _BUILD_CACHE[signature] = {
                "profile": copy.deepcopy(profile),
                "output_sha256": workflow.fingerprint([target]),
            }
            while len(_BUILD_CACHE) > _BUILD_CACHE_LIMIT:
                _BUILD_CACHE.popitem(last=False)
    return {**profile, "cache_hit": False}


def _quote(ref, cite=None):
    """Quote callout.  Notes pass ``cite`` (``[[Paper|Author Year, p. N]]``); the dashboard view keeps the exact IDs."""
    label = f"[[{ref['note_name']}]]" if ref.get("note_name") else ref["paper_id"]
    head = (f"{cite} · {ref.get('section') or 'section unavailable'}" if cite else
            f"{label} · {ref.get('sentence_id') or ref.get('id')} · {ref.get('section') or 'section unavailable'} · p. {ref.get('page') if ref.get('page') is not None else 'unavailable'}")
    return [f"> [!quote]- {head}",
            *(["> Preceding-page source fragment; context only, not a validated continuation."] if ref.get("context_basis")=="preceding_source_fragment" else []),
            "> " + ref["text"].replace("\n", "\n> ")]


def _protocol_variant_table(profile, cap=20):
    """Render a compact, unfolded comparison of current source protocols."""
    variants = []
    for paper in profile.get("papers", []):
        # Keep the note navigable when one paper has many source-specific
        # variants.  The complete profiles remain in the machine store.
        for variant in paper.get("protocol_variants", [])[:3]:
            details = variant.get("details") or {}

            def values(fields):
                return list(dict.fromkeys(str(item.get("value") or "").strip()
                                          for field in fields
                                          for item in (details.get(field, {}).get("items") or [])
                                          if item.get("value")))

            refs = []
            for ref in (variant.get("sources") or []) + (variant.get("steps") or []):
                sid = ref.get("sentence_id") or ref.get("id")
                if sid and sid not in refs:
                    refs.append(str(sid))
            step_text = " ".join(str(ref.get("text") or "").strip() for ref in (variant.get("steps") or [])[:2])
            if len(step_text) > 180:
                step_text = step_text[:177].rstrip() + "..."
            variants.append({
                "paper": " ".join(str(x) for x in (paper.get("cite") or paper.get("paper_id"), variant.get("name") or "")).strip(),
                "chemicals": values(("chemical", "concentration", "dose")),
                "time_temperature": values(("duration", "timing", "temperature")),
                "equipment": values(("equipment",)),
                "steps": step_text or "not annotated",
                "refs": ", ".join(refs[:12]) or "not annotated",
            })
    variants = variants[:cap]
    if not variants:
        return []
    for key in ("chemicals", "time_temperature", "equipment"):
        present = {tuple(row[key]) for row in variants if row[key]}
        differs = len(present) > 1 or len(present) < len(variants)
        for row in variants:
            if not row[key]:
                row[key] = "not annotated (present/different in other papers)" if differs else "not annotated"
            elif differs:
                row[key] = "; ".join(row[key]) + " [differs across papers]"
            else:
                row[key] = "; ".join(row[key])
    lines = ["| paper | chemicals and concentrations | time/temperature | equipment | step order (short) | refs |",
             "|---|---|---|---|---|---|"]
    lines.extend("| " + " | ".join(str(row[key]).replace("|", "\\|").replace("\n", " ")
                                    for key in ("paper", "chemicals", "time_temperature", "equipment", "steps", "refs")) + " |"
                 for row in variants)
    return lines


def protocol_variants_table(profile):
    return _protocol_variant_table(profile)


def render(profile):
    """Compact identity top, followed by folded source-specific details."""
    kind = profile["entity_kind"]
    title = "Technique identity and purpose" if kind == "method" else "Entity identity and source context" if kind in ("chemical", "chemical_class", "biological_entity", "molecule") else "Concept identity and source context"
    out = ["## " + title, "", "Source inventory; scientific acceptance has not been assessed."]
    out += card_currency_notice(profile.get("card"))
    out += [
           f"**Presentation kind:** {kind}. Source-derived classes are listed separately.",
           "**Canonical name:** " + profile["name"], "**Aliases:** " + (", ".join(profile["aliases"]) or "not annotated"), ""]
    labels = {"definitions": "Definition candidates", "full_names": "Full names and abbreviations",
              "source_classes": "Source-derived class candidates", "purpose": "Purpose and use",
              "roles_context": "Experimental roles and models", "significance": "Reported significance and importance",
              "limitations": "Reported limitations and cautions"}
    for field, label in labels.items():
        count = sum(len(p["fields"][field]["items"]) for p in profile["papers"])
        out.append(f"- **{label}:** {count} source candidates; expand below." if count else f"- **{label}:** unavailable — not annotated in matched sources.")
    identity = [item for p in profile["papers"] for item in p["fields"]["full_names"]["items"]]
    if identity:
        out += ["", "**First source-named identity (candidate):** " + identity[0]["value"] + " (" + identity[0]["abbreviation"] + ")"]
        out += _quote(identity[0]["sources"][0]) + [""]
    else:
        definitions = [item for p in profile["papers"] for item in p["fields"]["definitions"]["items"]]
        if definitions:
            out += ["", "**First definition context (candidate; source wording):**"] + _quote(definitions[0]) + [""]
    out += ["", "<details>", "<summary>Identity, roles, and surrounding source context</summary>", ""]
    for paper in profile["papers"]:
        out += ["### " + paper["paper_id"], ""]
        for field, label in labels.items():
            out += ["**" + label + "**"]
            for item in paper["fields"][field]["items"]:
                if field == "full_names":
                    out.append(f"- {item['value']} ({item['abbreviation']}); copied source candidate")
                elif field == "roles_context":
                    out.append(f"- {item['unit_id']} · {', '.join(item['roles'])} · {item['status']}")
                    for group in ("model", "intervention"):
                        values = _readable_fields(item.get(group))
                        if values != ["—"]:
                            out.extend(f"  - {group}: {line}" for line in values)
                for ref in item.get("sources", [item] if "text" in item else []):
                    out += _quote(ref) + [""]
            if not paper["fields"][field]["items"]:
                out.append("Unavailable — not annotated; no inference supplied.")
        out += ["**Same-paragraph context (not an inferred relationship)**"]
        for ref in paper["context"]:
            out += _quote(ref) + [""]
    out += ["", "</details>", ""]
    if kind in ("method", "technique") or any(p["protocol_variants"] for p in profile["papers"]):
        out += ["### Protocol variants by paper", "", "A shared method name does not establish equivalent procedures.", ""]
        out += protocol_variants_table(profile)
        out += [""]
        omitted_variants = sum(max(0, len(paper["protocol_variants"]) - 3)
                               for paper in profile["papers"])
        if omitted_variants:
            out.append(presentation.TEXT_VARIANTS_HIDDEN.format(count=omitted_variants))
            out.append("")
        for paper in profile["papers"]:
            for variant in paper["protocol_variants"][:3]:
                out += ["<details>", f"<summary>{paper['paper_id']} · {variant['name'] or 'name unavailable'} · {variant['status']}</summary>", "",
                        variant["basis"], "",
                        "Condition fragments are source candidates; assignment to a chemical or step is unavailable unless structured source annotations establish it.",
                        "", "**Materials, equipment, doses and conditions**"]
                for field, data in variant["details"].items():
                    out.append(f"- **{field}:** " + ("; ".join(i["value"] for i in data["items"]) + " (source candidates)" if data["items"] else "unavailable — not annotated"))
                    for item in data["items"]:
                        for ref in item["sources"]:
                            out += _quote(ref) + [""]
                out += ["", "**Source steps in paper order**"]
                for ref in variant["steps"] or variant["sources"]:
                    out += _quote(ref) + [""]
                out += ["</details>", ""]
        if not any(p["protocol_variants"] for p in profile["papers"]):
            out.append("Protocol variants unavailable — no matched Methods source.")
    out += ["### Result groups, statistics and media references", "", "Stored structured annotations retain their original quality status; copied sources are authoritative.", ""]
    for paper in profile["papers"]:
        for result in paper["results"][:12]:
            out += ["<details>", f"<summary>{result['unit_id']} · {result['status']} · {result['source_status']}</summary>", ""]
            quality_label = result_quality_label(result)
            if quality_label:
                out.append(f"- {presentation.TEXT_QUALITY} {quality_label}")
            for key in ("model", "intervention", "outcome", "quality"):
                out.append(f"- **{key}:**")
                shown = dict(result[key] or {})
                if key == "outcome" and result["outcome"].get("value_source") == "figure_only":
                    shown.update({f: "figure only; verify original — not transcribed" for f in ("values", "n", "p")})
                for field, value in shown.items():
                    readable = _readable_value(value)
                    if readable == "—":
                        readable = "unavailable — not annotated"
                    out.append(f"  - {field}: {readable}")
            media_ids = list(dict.fromkeys(str(value) for value in result["source_objects"]
                                          if value not in (None, "")))
            out.append("- **Media IDs:** " + (", ".join(media_ids) or "unavailable — not annotated"))
            out.append("- Non-significance must be read from the complete quoted result; it is not proof of no effect.")
            for ref in result["sources"]:
                out += _quote(ref) + [""]
            out += ["</details>", ""]
    omitted_results = sum(max(0, len(paper["results"]) - 12) for paper in profile["papers"])
    if omitted_results:
        out.append(presentation.TEXT_RESULTS_HIDDEN.format(count=omitted_results))
        out.append("")
    if not any(p["results"] for p in profile["papers"]):
        out.append("Result groups/statistics/media unavailable — no matched structured result.")
    history = [r for r in profile.get("conflicts") or [] if r.get("display_status",r.get("status")) not in ("ai_proposed", "proposed", "human_confirmed")]
    if history:
        out += ["<details>", "<summary>Relation review history</summary>", ""]
        for r in history:
            out.append(f"- {r.get('relation_id')} · {r.get('type')} · {STATUS_LABELS.get(r.get('status'), 'review status unavailable')}")
    if history: out += ["", "</details>"]
    return out + ["", profile["scope_note"], ""]


# D3.69 keeps this public helper compact.  Full side-by-side quotes remain only
# in the generated CONFLICT notes.
def _compact_conflict_callout(profile, rows, conflict_ids=None):
    markers = {"contradicts": "🔴", "direction_differs": "🟠", "effect_vs_no_effect": "🟡"}
    if conflict_ids is None:
        conflict_ids = _vault_conflict_ids(profile.get("vault", ""))
    units = {}
    for paper in profile.get("papers", []):
        paper_meta = {"note_name": paper.get("note_name"), "paper_id": paper.get("paper_id")}
        for row in paper.get("results", []):
            if row.get("unit_id"):
                units[row["unit_id"]] = {**row, "paper": row.get("paper") or paper_meta}
    out = [presentation.TEXT_CONFLICT_HEAD.format(count=len(rows))]
    for relation in rows[:5]:
        left = units.get(relation.get("from_unit"), {})
        right = units.get(relation.get("to_unit"), {})
        measure = (relation.get("shared_concept_name") or relation.get("measure_group") or
                   (left.get("outcome") or {}).get("measure") or
                   (right.get("outcome") or {}).get("measure") or profile.get("name") or "result")
        papers = []
        for index, unit in enumerate((left, right)):
            paper = unit.get("paper") or {}
            sides = relation.get("source_sides") or []
            refs = (sides[index].get("items") or []) if index < len(sides) else []
            source = refs[0] if refs else {}
            endpoint = str(relation.get(("from_unit", "to_unit")[index]) or "").rsplit(":", 1)[0]
            label = paper.get("note_name") or unit.get("paper_id") or source.get("note_name") or source.get("paper_id") or endpoint or "paper"
            papers.append(" ".join(str(label).split()))
        stored_directions = relation.get("directions") or []
        directions = []
        for index, unit in enumerate((left, right)):
            direction = stored_directions[index] if isinstance(stored_directions, (list, tuple)) and index < len(stored_directions) \
                else (unit.get("outcome") or {}).get("direction")
            directions.append(str(direction).strip() if direction not in (None, "") else "")
        relation_id = str(relation.get("relation_id") or "")
        link = f" · [[{conflict_ids[relation_id]}]]" if relation_id in conflict_ids else ""
        measure = " ".join(str(measure).split())
        direction_text = f" · direction: {directions[0]} ↔ {directions[1]}" if all(directions) else ""
        out.append(f"> {markers.get(relation.get('type'), '🟡')} {measure} · {papers[0]} vs {papers[1]}{direction_text}{link}")
    remaining = len(rows) - min(len(rows), 5)
    if remaining:
        out.append(presentation.TEXT_CONFLICT_MORE.format(count=remaining))
    return out + [""]


# D3.45 keeps this public helper as the single conflict renderer.  It is defined
# after the legacy inventory renderer so older inventory formatting can remain
# source-compatible while the note itself uses the new red conflict callout.
def conflict_callout(profile, conflict_ids=None):
    rows = [r for r in profile.get("conflicts") or []
            if r.get("display_status", r.get("status")) in OPEN_CONFLICT_STATUSES]
    if not rows:
        return []
    return _compact_conflict_callout(profile, rows, conflict_ids)
