"""Source-grounded, AI-filled cards for typed research nodes.

The analyst supplies short paraphrases and sentence IDs.  This module owns the
source lookup, quote copying, revision checks, and conservative validation.
"""

from __future__ import annotations

import argparse
import copy
import json
import re
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

import yaml

from . import answer, analysis_transport, concepts, config, db, find, node_profile, presentation, structure, workflow


VERSION = 1
MAX_SENTENCES = 500
ALLOWED_TYPES = ("substance", "technique", "protocol", "organism", "cell_type", "concept", "topic")
RELATIONS = {
    "measures", "measured_by", "in_taxon", "uses_technique", "reagent_of", "marker_of",
    "derived_from", "parent_technique", "part_of", "associated_with",
}
REGISTRY_TO_CARD = {
    "molecule": "substance", "method": "protocol", "species": "organism",
    "cell_type": "cell_type", "concept": "concept", "topic": "topic",
}
REVIEW_TYPES = set(find.REVIEW_TYPES)
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_NODE_INPUTS_CACHE = {}


def _stat_key(path):
    return structure._file_stat(path)


def _schema() -> Dict[str, Any]:
    path = config.CONFIG_DIR / "node_card_schema.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Invalid node card schema")
    return value


def _digest(value: Any) -> str:
    return __import__("hashlib").sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _registry(con):
    return concepts.load_registry(con)


def _record(registry, name: str) -> Dict[str, Any]:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Node name is required")
    record = registry.by_id.get(name) or registry.lookup(name)
    if not record:
        raise ValueError(f"Node is not in the registry: {name}")
    return record


def _candidate_type(record: Mapping[str, Any]) -> str:
    return REGISTRY_TO_CARD.get(record.get("type"), "concept")


def _links(con, vault: str, concept_id: str) -> List[Dict[str, Any]]:
    rows = con.execute(
        "SELECT cl.concept_id, cl.paper_id, cl.level, cl.n_sentences, cl.sections, cl.first_sentence_id "
        "FROM concept_links cl JOIN paper_vaults pv ON pv.paper_id = cl.paper_id "
        "WHERE cl.concept_id = ? AND pv.vault = ? ORDER BY cl.paper_id",
        (concept_id, vault),
    )
    return [dict(row) for row in rows]


def _metadata(paper_id: str) -> Dict[str, Any]:
    meta = dict(db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {})
    meta["evidence_type"] = find.effective_evidence_type(meta)
    return meta


def _source_rows(paper_id: str) -> List[Dict[str, Any]]:
    rows = []
    for row in structure.paper_rows(paper_id, "sentences.jsonl"):
        if not isinstance(row, dict) or not row.get("sentence_id"):
            continue
        if row.get("section") in ("references", "front_matter"):
            continue
        # Keep table rows and captions, but omit hidden extraction helpers.
        if row.get("in_table") and row.get("kind") != "table_row":
            continue
        rows.append(row)
    return sorted(rows, key=lambda row: (row.get("seq", 0), row.get("sentence_id", "")))


def _source_map(paper_id: str) -> Dict[str, Dict[str, Any]]:
    return {row["sentence_id"]: row for row in _source_rows(paper_id)}


def _unit_rows(paper_id: str) -> List[Dict[str, Any]]:
    rows = []
    for unit in db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl") or ():
        if isinstance(unit, dict):
            unit = dict(unit)
            unit["paper_id"] = paper_id
            rows.append(unit)
    return rows


def _match(text: str, record: Mapping[str, Any]) -> bool:
    return presentation.matches_term(text or "", record["name"], record.get("synonyms") or [])


def _paper_info(paper_id: str) -> Dict[str, Any]:
    meta = _metadata(paper_id)
    return {
        "paper_id": paper_id,
        "label": meta.get("title") or meta.get("note_name") or paper_id,
        "title": meta.get("title"),
        "year": meta.get("year"),
        "evidence_type": "review" if meta.get("evidence_type") in REVIEW_TYPES else "primary",
        "note_name": meta.get("note_name"),
    }


def _caption_rows(paper_id: str, rows: Mapping[str, Mapping[str, Any]], mandatory: Iterable[str]) -> Tuple[set, List[Dict[str, Any]]]:
    wanted_objects = set()
    for sid in mandatory:
        wanted_objects.update(rows[sid].get("mentions") or ())
    ids = set()
    for sid, row in rows.items():
        kind = str(row.get("kind") or "").casefold()
        if sid in wanted_objects or (wanted_objects and "caption" in kind):
            if sid in wanted_objects or row.get("mentions") and wanted_objects.intersection(row.get("mentions") or ()):
                ids.add(sid)
    objects = []
    for filename, kind, key in (("figures.jsonl", "figure", "figure_id"), ("tables.jsonl", "table", "table_id")):
        for obj in db.read_jsonl(db.paper_dir(paper_id) / filename) or ():
            if not isinstance(obj, dict) or obj.get(key) not in wanted_objects:
                continue
            item = {"paper_id": paper_id, "object_type": kind, **obj}
            if obj.get("caption"):
                item["caption"] = obj["caption"]
            item["sentence_ids"] = sorted(ids.intersection(rows))
            objects.append(item)
    return ids, objects


def _paper_payload(record: Mapping[str, Any], paper_id: str, units: Sequence[Mapping[str, Any]],
                   protocol_ids: Iterable[str] = (), related: Sequence[Mapping[str, Any]] = ()) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    rows = _source_map(paper_id)
    direct = {sid for sid, row in rows.items() if _match(row.get("text", ""), record)}
    # An accepted unit of this paper that NAMES the node in its statement belongs to the task even when its
    # source sentence only uses an abbreviation ("E2" for Estradiol) that the node match above cannot see.
    seen = {unit.get("unit_id") for unit in units}
    units = list(units) + [unit for unit in _unit_rows(paper_id)
                           if unit.get("unit_id") not in seen and unit.get("status") in ("ok", "human_confirmed")
                           and _match(str(unit.get("statement") or ""), record)]
    passages = {(rows[sid].get("source", "main"), rows[sid].get("passage_id")) for sid in direct
                if rows[sid].get("passage_id") is not None}
    context = {sid for sid, row in rows.items()
              if (row.get("source", "main"), row.get("passage_id")) in passages}
    relevant_units = []
    evidence_ids = set()
    all_evidence_ids = set()
    for unit in units:
        if unit.get("paper_id") != paper_id:
            continue
        unit_ids = set(unit.get("sentence_ids") or ())
        if unit_ids.intersection(rows) or any(_match(rows[sid].get("text", ""), record) for sid in unit_ids.intersection(rows)):
            relevant_units.append({k: unit.get(k) for k in (
                "unit_id", "paper_id", "unit_type", "status", "statement", "outcome", "model",
                "intervention", "sentence_ids", "sections", "pages", "hedging", "analyst")})
            all_evidence_ids.update(unit_ids.intersection(rows))
            if unit.get("unit_type") in ("result", "method_detail", "conclusion"):
                evidence_ids.update(unit_ids.intersection(rows))
    protocol_ids = set(protocol_ids).intersection(rows)
    mandatory = direct | all_evidence_ids | protocol_ids
    if not mandatory:
        # A paper that reached the node only through a descendant (Immunocytochemistry <- Immunofluorescence):
        # show its sentences about those children rather than listing the paper with nothing to read.
        direct = {sid for sid, row in rows.items() if any(_match(row.get("text", ""), child) for child in related)}
        mandatory = set(direct)
    caption_ids, captions = _caption_rows(paper_id, rows, mandatory)
    mandatory |= caption_ids
    # The source task must never ask an analyst to reconstruct missing source IDs.
    source_ids = sorted(mandatory | context, key=lambda sid: (rows[sid].get("seq", 0), sid))
    sources = [{
        "id": sid,
        "sentence_id": sid,
        "paper_id": paper_id,
        "section": rows[sid].get("section"),
        "subsection": rows[sid].get("subsection"),
        "page": rows[sid].get("page"),
        "text": rows[sid].get("text", ""),
        "seq": rows[sid].get("seq", 0),
        "kind": rows[sid].get("kind"),
        "hedges": structure.uncertainty(rows[sid].get("text", "")),
        "direct_mention": sid in direct,
        "evidence_sentence": sid in evidence_ids,
        "context": sid in context and sid not in mandatory,
    } for sid in source_ids]
    return {
        "paper": _paper_info(paper_id),
        "sentences": sources,
        "evidence_units": relevant_units,
        "captions": captions,
        "mandatory_ids": sorted(mandatory),
        "context_ids": sorted(context - mandatory),
        "priority_ids": {
            "evidence_unit": sorted({sid for sid in evidence_ids if sid in rows}),
            "other_evidence": sorted({sid for sid in all_evidence_ids - evidence_ids if sid in rows}),
            "protocol_variant": sorted(protocol_ids),
            "definition_abbreviation_caption": sorted({sid for sid in mandatory
                                                        if sid not in evidence_ids and sid not in protocol_ids
                                                        and (sid in caption_ids or (sid in direct and _definition_or_caption(rows[sid], record)))}),
            "direct_mention": sorted(direct),
            "context": sorted(context - mandatory),
        },
        "include_allowed_ids": sorted(direct | all_evidence_ids | protocol_ids),
        "caption_ids": sorted(caption_ids),
    }, rows


def _definition_or_caption(row: Mapping[str, Any], record: Mapping[str, Any]) -> bool:
    kind = str(row.get("kind") or "").casefold()
    if "caption" in kind:
        return True
    text = str(row.get("text") or "")
    if re.search(r"\b(?:is|are|refers? to|defined as|known as)\b", text, re.I):
        return True
    variants = [record.get("name"), *(record.get("synonyms") or [])]
    return bool(re.search(r"\b[A-Z][A-Za-z0-9-]{1,11}\b\s*\(", text)
                and any(_match(text, {"name": value, "synonyms": []}) for value in variants if value))


def _node_paths(paper_ids: Sequence[str], record: Mapping[str, Any], vault: str, units: Sequence[Mapping[str, Any]]) -> List[Path]:
    files = (
        "sentences.jsonl", "evidence.jsonl", "structure.json", "protocol_profile.json",
        "section_profile.json", "figures.jsonl", "tables.jsonl", "references.jsonl", "media_manifest.json",
    )
    # Source data only: code edits and AI/status fields in metadata.json must not make paid cards stale
    # (metadata enters through structure._source_metadata in _source_revision).
    paths = [config.CONFIG_DIR / "node_card_schema.yaml"]
    for paper_id in paper_ids:
        paths.extend(db.paper_dir(paper_id) / filename for filename in files)
        meta = _metadata(paper_id)
        for source in meta.get("source_files") or ():
            if isinstance(source, dict) and source.get("path"):
                source_path = Path(source["path"])
                paths.append(source_path if source_path.is_absolute() else config.ROOT / source_path)
    return paths


def _protocol_variants(record: Mapping[str, Any], vault: str, paper_ids: Sequence[str], units: Sequence[Mapping[str, Any]], con) -> List[Dict[str, Any]]:
    try:
        profile = node_profile.build(record, vault, paper_ids, units, persist=False, con=con)
    except (OSError, ValueError, TypeError, KeyError):
        return []
    variants = []
    for paper in profile.get("papers") or ():
        for variant in paper.get("protocol_variants") or ():
            variants.append({"paper_id": paper.get("paper_id"), **variant})
    return variants


def _reply_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "type": "object",
        "required": ["source_revision", "node_type", "fields"],
        "properties": {
            "source_revision": {"type": "string"},
            "node_type": {"enum": list(schema.get("allowed_types") or ALLOWED_TYPES)},
            "type_justification": schema.get("statement"),
            "fields": {"type": "object", "description": "Fields in schema.common_fields plus the selected type fields"},
            "related": {"type": "array"},
        },
    }


def _source_revision(record: Mapping[str, Any], vault: str, paper_ids: Sequence[str], units: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]]) -> str:
    paths = _node_paths(paper_ids, record, vault, units)
    extra = {"version": VERSION, "record": dict(record), "vault": vault, "links": list(links),
             "units": [dict(unit) for unit in units], "paper_ids": sorted(paper_ids),
             "metadata": {pid: structure._source_metadata(_metadata(pid)) for pid in sorted(paper_ids)}}
    return workflow.fingerprint(paths, extra)


def _node_inputs(con, vault: str, record: Mapping[str, Any], registry):
    """Links, papers and evidence units of one node — exactly the inputs of its source revision."""
    links = _links(con, vault, record["concept_id"])
    paper_ids = sorted({row["paper_id"] for row in links})
    vault_papers = sorted({row[0] for row in con.execute(
        "SELECT paper_id FROM paper_vaults WHERE vault=?", (vault,))})
    cache_key = (str(config.STORE.resolve()), str(Path(config.DB_PATH).resolve()),
                 _stat_key(config.DB_PATH), vault, record["concept_id"],
                 json.dumps(record, ensure_ascii=False, sort_keys=True, default=str),
                 id(registry), presentation.homonym_stamp(), tuple((paper_id, structure.paper_read_stamp(paper_id))
                                     for paper_id in vault_papers))
    cached = _NODE_INPUTS_CACHE.get(cache_key)
    if cached is not None:
        return copy.deepcopy(cached)
    # Match concept_notes' node set and evidence selection, including descendants.
    try:
        _, collected, units = __import__("kbsys.concept_notes", fromlist=["_collect"])._collect(con, record, registry, vault)
        paper_ids = sorted(set(paper_ids) | set(collected))
    except (OSError, ValueError, TypeError, KeyError):
        units = []
    result = (links, paper_ids, units)
    _NODE_INPUTS_CACHE[cache_key] = result
    return copy.deepcopy(result)


# When a task is capped, results reach the card before methods (document order put every Methods line first).
_SECTION_RANK = {"results": 0, "results_discussion": 0, "discussion": 1, "conclusion": 1, "abstract": 2,
                 "introduction": 3, "figure_legends": 3, "methods": 4}


def _capped_order(row):
    return _SECTION_RANK.get(str(row.get("section") or "").casefold(), 3), row.get("seq", 0), row["id"]


def _round_robin(rows_by_paper: Mapping[str, Sequence[Mapping[str, Any]]], limit: int) -> List[Mapping[str, Any]]:
    """Take rows in paper order, one row per paper per round."""
    buckets = {paper: list(rows) for paper, rows in sorted(rows_by_paper.items())}
    selected = []
    while len(selected) < limit and any(buckets.values()):
        for paper in sorted(buckets):
            if len(selected) >= limit:
                break
            if buckets[paper]:
                selected.append(buckets[paper].pop(0))
    return selected


def task(con, vault: str, name: str, include_sentence_ids: Iterable[str] | None = None) -> Dict[str, Any]:
    """Build the bounded, priority-ordered source task for one registry node.

    ``include_sentence_ids`` is an analyst escape hatch for a missing source
    sentence.  It can only select a direct node mention, an evidence-unit
    sentence, or a protocol-variant step; rejected IDs are reported in the
    payload rather than silently becoming evidence.
    """
    registry = _registry(con)
    record = _record(registry, name)
    links, paper_ids, units = _node_inputs(con, vault, record, registry)
    protocol_variants = _protocol_variants(record, vault, paper_ids, units, con)
    protocol_ids_by_paper = defaultdict(set)
    for variant in protocol_variants:
        for ref in (variant.get("sources") or []) + (variant.get("steps") or []):
            if ref.get("paper_id") and ref.get("sentence_id"):
                protocol_ids_by_paper[ref["paper_id"]].add(ref["sentence_id"])
    papers, all_sources, all_units, all_captions = [], [], [], []
    payloads = []
    related = []
    if hasattr(registry, "records"):
        descendants = __import__("kbsys.concept_notes", fromlist=["_descendants"])._descendants(registry, record["concept_id"])
        related = [registry.by_id[cid] for cid in sorted(descendants - {record["concept_id"]}) if cid in registry.by_id]
    for paper_id in paper_ids:
        payload, _ = _paper_payload(record, paper_id, units, protocol_ids_by_paper.get(paper_id, ()), related)
        payloads.append(payload)
        papers.append(payload["paper"])
        all_sources.extend(payload["sentences"])
        all_units.extend(payload["evidence_units"])
        all_captions.extend(payload["captions"])
    all_sources.sort(key=lambda row: (row["paper_id"], row.get("seq", 0), row.get("id", "")))

    categories = {}
    for payload in payloads:
        paper_id = payload["paper"]["paper_id"]
        priority = payload.get("priority_ids") or {}
        for sid in priority.get("evidence_unit", ()):
            categories[(paper_id, sid)] = "evidence_unit"
        for sid in priority.get("protocol_variant", ()):
            categories.setdefault((paper_id, sid), "protocol_variant")
        for sid in priority.get("definition_abbreviation_caption", ()):
            categories.setdefault((paper_id, sid), "definition_abbreviation_caption")
        for sid in priority.get("direct_mention", ()):
            categories.setdefault((paper_id, sid), "direct_mention")
        for sid in priority.get("other_evidence", ()):
            categories.setdefault((paper_id, sid), "direct_mention")
        for sid in priority.get("context", ()):
            categories.setdefault((paper_id, sid), "context")
    by_key = {(row["paper_id"], row["id"]): row for row in all_sources}
    requested = {part.strip() for sid in (include_sentence_ids or ())
                 for part in str(sid).split(",") if part.strip()}
    allowed = {str(sid) for payload in payloads for sid in payload.get("include_allowed_ids", ())}
    # IDs are normally globally qualified.  Accept exact source IDs only; a
    # short ID cannot be safely mapped across papers.
    include_kept = sorted(requested & allowed)
    include_rejected = sorted(requested - allowed)
    selected_keys = {key for key in by_key if key[1] in include_kept}
    remaining = max(0, MAX_SENTENCES - len(selected_keys))
    priority_groups = [
        ("evidence_unit", {key for key, value in categories.items() if value in ("evidence_unit", "protocol_variant")}),
        ("definition_abbreviation_caption", {key for key, value in categories.items() if value == "definition_abbreviation_caption"}),
        ("direct_mention", {key for key, value in categories.items() if value == "direct_mention"}),
        ("context", {key for key, value in categories.items() if value == "context"}),
    ]
    selected_by_priority = {"include_sentence_ids": len(selected_keys)}
    for label, keys in priority_groups:
        rows_by_paper = defaultdict(list)
        for key in keys - selected_keys:
            rows_by_paper[key[0]].append(by_key[key])
        for rows in rows_by_paper.values():
            rows.sort(key=_capped_order)
        taken = _round_robin(rows_by_paper, remaining)
        selected_keys.update((row["paper_id"], row["id"]) for row in taken)
        remaining -= len(taken)
        selected_by_priority[label] = len(taken)
    source_sentences = sorted((by_key[key] for key in selected_keys),
                              key=lambda row: (row["paper_id"], row.get("seq", 0), row["id"]))
    trimmed_by_reason = defaultdict(list)
    for key, row in by_key.items():
        if key in selected_keys:
            continue
        category = categories.get(key, "context")
        trimmed_by_reason[f"over_cap_{category}"].append(row["id"])
    trimmed_by_reason = {key: {"count": len(ids), "sentence_ids": sorted(ids)}
                         for key, ids in sorted(trimmed_by_reason.items())}
    all_context_keys = {key for key, value in categories.items() if value == "context"}
    kept_context_keys = selected_keys.intersection(all_context_keys)
    trimmed_context_ids = sorted(by_key[key]["id"] for key in all_context_keys - kept_context_keys)
    # Retain only unit/caption references that remain in the bounded task.
    visible_ids = {row["id"] for row in source_sentences}
    evidence = [unit for unit in all_units if set(unit.get("sentence_ids") or ()).intersection(visible_ids)]
    captions = [caption for caption in all_captions
                if not caption.get("sentence_ids") or set(caption.get("sentence_ids") or ()).intersection(visible_ids)]
    revision = _source_revision(record, vault, paper_ids, units, links)
    schema = _schema()
    candidate = _candidate_type(record)
    return {
        "schema_version": VERSION,
        "node": {"concept_id": record["concept_id"], "name": record["name"], "registry_type": record.get("type"),
                  "aliases": record.get("synonyms") or [], "candidate_type": candidate},
        "allowed_types": list(schema.get("allowed_types") or ALLOWED_TYPES),
        "schema": schema,
        "papers": sorted(papers, key=lambda row: row["paper_id"]),
        "sentences": source_sentences,
        "source_sentences": source_sentences,
        "evidence_units": evidence,
        "protocol_variants": protocol_variants,
        "captions": captions,
        "trimmed": {"sentence_cap": MAX_SENTENCES, "total_candidates": len(all_sources),
                     "selected": len(source_sentences), "trimmed": len(all_sources) - len(source_sentences),
                     "context_total": len(all_context_keys), "context_kept": len(kept_context_keys),
                     "context_trimmed": len(trimmed_context_ids),
                     "mandatory_kept": len(source_sentences) - len(kept_context_keys),
                     "truncated_context_ids": trimmed_context_ids,
                     "selected_by_priority": selected_by_priority,
                     "trimmed_by_reason": trimmed_by_reason,
                     "include_sentence_ids": include_kept,
                     "rejected_include_sentence_ids": include_rejected},
        "instructions": (
            "Use only the supplied sentence IDs. Write concise canonical-English paraphrases; do not type quotes. "
            "The code will copy verbatim quotes. Check every statement against its cited sentences. Preserve hedges, "
            "association, non-significance, and causal strength. Attribute review papers and cited studies as secondary "
            "evidence. Use status not_found_in_vault instead of guessing. A type correction requires a source-grounded "
            "type_justification."
        ),
        "reply_schema": _reply_schema(schema),
        "source_revision": revision,
    }


def _is_not_found(value: Any) -> bool:
    return isinstance(value, dict) and value.get("status") == "not_found_in_vault"


def _iter_statements(value: Any, path: str = "") -> Iterator[Tuple[str, Dict[str, Any]]]:
    if _is_not_found(value):
        return
    if isinstance(value, dict):
        if "text" in value:
            yield path, value
            return
        for key, child in value.items():
            if key in ("related", "quotes", "validation"):
                continue
            yield from _iter_statements(child, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _iter_statements(child, f"{path}[{index}]")


def _enum_value(value: Any) -> Tuple[Any, List[str]]:
    if isinstance(value, dict):
        return value.get("value"), list(value.get("sentence_ids") or [])
    return value, []


def _problem(code: str, path: str, detail: str) -> Dict[str, str]:
    return {"code": code, "path": path, "detail": detail}


def _paper_cards(payload: Mapping[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {paper["paper_id"]: {"year": paper.get("year"), "evidence_type": paper.get("evidence_type")}
            for paper in payload.get("papers") or ()}


def _check_statement(path: str, statement: Mapping[str, Any], source_map: Mapping[str, Mapping[str, Any]], payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    problems: List[Dict[str, str]] = []
    text = statement.get("text")
    ids = statement.get("sentence_ids")
    if not isinstance(text, str) or not text.strip():
        return [_problem("statement_text", path, "Statement text is required")]
    if not isinstance(ids, list) or not ids:
        return [_problem("statement_sentence_ids", path, "Every statement needs sentence_ids")]
    missing = [sid for sid in ids if sid not in source_map]
    if missing:
        return [_problem("unknown_sentence_id", path, ", ".join(map(str, missing)))]
    if presentation.machine_ids(text):   # VN-03: the id belongs in sentence_ids; the note shows the citation label
        problems.append(_problem("machine_id", path, "Do not write a sentence id in the text: sentence_ids carries it"))
    if statement.get("attribution") not in ("primary", "review", "author_hypothesis", "cited_study"):
        problems.append(_problem("attribution", path, "Use primary, review, author_hypothesis, or cited_study"))
    if statement.get("hedge") not in ("none", "hedged"):
        problems.append(_problem("hedge", path, "Use none or hedged"))
    cited = [source_map[sid] for sid in ids]
    if any(row.get("hedges") for row in cited) and statement.get("hedge") != "hedged":
        problems.append(_problem("hedge_dropped", path, "A cited source is hedged; the statement must retain a hedge"))
    papers = _paper_cards(payload)
    if any(papers.get(row["paper_id"], {}).get("evidence_type") == "review" for row in cited):
        if statement.get("attribution") not in ("review", "cited_study"):
            problems.append(_problem("review_attribution", path, "Review support must be attributed as review or cited_study"))
    identifier_terms = [payload.get("node", {}).get("name"),
                        *(payload.get("node", {}).get("aliases") or [])]
    items = []
    for row in cited:
        items.append({"paper_id": row["paper_id"], "text": row.get("text", ""), "kind": "sentence",
                      "unit_type": "background", "hedging": "hedged" if row.get("hedges") else None,
                      "review_paper": papers.get(row["paper_id"], {}).get("evidence_type") == "review",
                      "cited_refs": row.get("cited_refs") or [],
                      "identifier_terms": identifier_terms})
    checked = answer.check_sentence({"text": text, "evidence": list(range(1, len(items) + 1))}, items, papers,
                                    text_markers=False, scope="card")  # "[95,96]" is a literature reference, not an item marker
    for code in checked.get("problems") or ():
        if code in {"A1", "A2", "A3", "A4", "A6", "A7"}:
            problems.append(_problem(code, path, answer.CHECK_TEXT.get(code, "Source-grounding check failed")))
    return problems


def _check_row_ids(path: str, value: Any, source_map: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, str]]:
    if not isinstance(value, dict):
        return [_problem("row_shape", path, "Expected an object")]
    ids = value.get("sentence_ids")
    if not isinstance(ids, list) or not ids:
        return [_problem("row_sentence_ids", path, "Structured rows need sentence_ids")]
    return [_problem("unknown_sentence_id", path, str(sid)) for sid in ids if sid not in source_map]


def _numeric_row_text(row: Mapping[str, Any]) -> str:
    return " ".join(str(row.get(key) or "") for key in ("value", "unit", "time", "temperature", "concentration", "duration", "step"))


def _check_enums(fields: Mapping[str, Any], candidate: str, schema: Mapping[str, Any], source_map: Mapping[str, Mapping[str, Any]]) -> List[Dict[str, str]]:
    problems: List[Dict[str, str]] = []
    defs = {}
    defs.update((schema.get("common_fields") or {}))
    defs.update(((schema.get("types") or {}).get(candidate) or {}).get("fields") or {})
    for key, definition in defs.items():
        if not isinstance(definition, dict) or key not in fields or "allowed" not in definition:
            continue
        values = fields[key] if isinstance(fields[key], list) else [fields[key]]
        for index, raw in enumerate(values):
            if _is_not_found(raw):
                continue
            value, ids = _enum_value(raw)
            path = f"fields.{key}[{index}]" if isinstance(fields[key], list) else f"fields.{key}"
            if value not in definition["allowed"]:
                problems.append(_problem("enum_value", path, f"Unsupported value: {value}"))
            if value not in ("other", "unknown") and not ids:
                problems.append(_problem("enum_sentence_ids", path, "Enumerated values need sentence_ids"))
            problems.extend(_problem("unknown_sentence_id", path, str(sid)) for sid in ids if sid not in source_map)
    return problems


def _check_structured_rows(fields: Mapping[str, Any], candidate: str, source_map: Mapping[str, Mapping[str, Any]], payload: Mapping[str, Any]) -> List[Dict[str, str]]:
    problems: List[Dict[str, str]] = []
    type_defs = (((payload.get("schema") or {}).get("types") or {}).get(candidate) or {}).get("fields") or {}
    for key, definition in type_defs.items():
        value = fields.get(key)
        kind = definition.get("kind") if isinstance(definition, dict) else None
        if value is None or kind not in ("concentration_rows", "material_rows", "step_rows", "variant_rows", "usage_rows"):
            continue
        if _is_not_found(value):  # {"status": "not_found_in_vault"} is the agreed "no source" answer for any field
            continue
        if not isinstance(value, list):
            problems.append(_problem("row_shape", f"fields.{key}", "Expected a list"))
            continue
        for index, row in enumerate(value):
            path = f"fields.{key}[{index}]"
            problems.extend(_check_row_ids(path, row, source_map))
            if isinstance(row, dict) and row.get("text") is not None:
                problems.extend(_check_statement(path + ".text", row, source_map, payload))
            elif isinstance(row, dict) and _numeric_row_text(row).strip():
                pseudo = {"text": _numeric_row_text(row), "sentence_ids": row.get("sentence_ids"),
                          "attribution": row.get("attribution", "primary"), "hedge": row.get("hedge", "none")}
                problems.extend(_check_statement(path, pseudo, source_map, payload))
    return problems


def _safe_card_path(vault: str, concept_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", str(vault)):
        raise ValueError("Invalid vault")
    filename = _SAFE_ID.sub("_", str(concept_id)).strip("._") or "node"
    return config.STORE / "node_cards" / vault / (filename + ".json")


def _raw_card(vault: str, concept_id: str) -> Dict[str, Any] | None:
    path = _safe_card_path(vault, concept_id)
    value = db.read_json(path)
    return value if isinstance(value, dict) else None


_REGISTRY_CACHE = {}


def _registry_cache_key():
    path = config.STORE / "concepts.jsonl"
    try:
        stat = path.stat()
        stamp = (getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)), stat.st_size)
    except OSError:
        stamp = None
    return str(config.DB_PATH), stamp


def _card_sentence_ids(card: Mapping[str, Any]) -> List[str]:
    """Return sentence IDs explicitly cited by card fields, not its quote cache."""
    found = []

    def visit(value):
        if isinstance(value, Mapping):
            ids = value.get("sentence_ids")
            if isinstance(ids, str):
                ids = [ids]
            if isinstance(ids, (list, tuple)):
                found.extend(str(sid) for sid in ids if sid)
            for key, child in value.items():
                # ``quotes`` is a copied source cache, not a list of claims.
                if key != "quotes":
                    visit(child)
        elif isinstance(value, (list, tuple)):
            for child in value:
                visit(child)

    visit(card.get("fields") or card.get("statements") or {})
    return list(dict.fromkeys(found))


def _card_currency(stored: Mapping[str, Any], record: Mapping[str, Any], vault: str,
                   paper_ids: Sequence[str], units: Sequence[Mapping[str, Any]]) -> Tuple[str, Dict[str, Any]]:
    """Classify an accepted card without using the broad source revision.

    Cards retain copied quote text for the IDs they cite.  A changed/missing cited
    sentence is stale; changes elsewhere in the node are new evidence and remain
    readable until the researcher asks for an update.
    """
    card = stored.get("card") if isinstance(stored.get("card"), Mapping) else stored
    if not isinstance(card, Mapping) or not record:
        return "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}
    quotes = card.get("quotes") or {}
    if not isinstance(quotes, Mapping):
        quotes = {}
    cited = _card_sentence_ids(card)
    live_maps = {}  # one parse of each cited paper, not one per cited sentence
    for sid in cited:
        copied = quotes.get(sid)
        if not isinstance(copied, Mapping) or not isinstance(copied.get("text"), str):
            return "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}
        paper_id = copied.get("paper_id") or (str(sid).split(":", 1)[0] if ":" in str(sid) else "")
        if not paper_id:
            return "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}
        if str(paper_id) not in live_maps:
            live_maps[str(paper_id)] = _source_map(str(paper_id))
        live = live_maps[str(paper_id)].get(str(sid))
        if (live is None or live.get("text", "") != copied.get("text")
                or any(key in copied and live.get(key) != copied[key] for key in ("section", "page"))):
            return "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}

    payload = stored.get("payload") if isinstance(stored.get("payload"), Mapping) else {}
    recorded_papers = stored.get("paper_ids")
    if not isinstance(recorded_papers, list):
        recorded_papers = card.get("paper_ids") if isinstance(card.get("paper_ids"), list) else None
    if not isinstance(recorded_papers, list) and isinstance(payload.get("papers"), list):
        recorded_papers = [paper.get("paper_id") for paper in payload.get("papers") or ()
                           if isinstance(paper, Mapping) and paper.get("paper_id")]
    if not isinstance(recorded_papers, list):
        # Legacy cards have no explicit inventory.  Their copied payload is the
        # safest compatible baseline for detecting new papers.
        recorded_papers = sorted({str(row.get("paper_id")) for row in quotes.values()
                                  if isinstance(row, Mapping) and row.get("paper_id")})
    recorded_papers = {str(pid) for pid in recorded_papers if pid}
    current_papers = {str(pid) for pid in paper_ids if pid}
    new_papers = sorted(current_papers - recorded_papers)

    recorded_units = stored.get("unit_ids")
    if not isinstance(recorded_units, list):
        recorded_units = card.get("unit_ids") if isinstance(card.get("unit_ids"), list) else None
    if not isinstance(recorded_units, list) and isinstance(payload.get("evidence_units"), list):
        recorded_units = [unit.get("unit_id") for unit in payload.get("evidence_units") or ()
                          if isinstance(unit, Mapping) and unit.get("unit_id")]
    if not isinstance(recorded_units, list):
        # Old cards did not record the task's evidence inventory.  Do not mark
        # every existing unit as new; new-paper detection remains available.
        recorded_units = []
        current_units = set()
    else:
        current_units = {str(unit.get("unit_id")) for unit in units if unit.get("unit_id")}
    new_units = sorted(current_units - {str(uid) for uid in recorded_units if uid})
    currency = "current_with_new_evidence" if new_papers or new_units else "current"
    return currency, {"card_currency": currency, "new_paper_ids": new_papers,
                      "new_unit_ids": new_units}


def _current_card_state(vault: str, concept_id: str, request_cache=None) -> Tuple[Dict[str, Any] | None, str, Dict[str, Any]]:
    # Cheap exit first: most nodes have no card, and building the registry rescans all text.
    stored = _raw_card(vault, concept_id)
    if not stored:
        return None, "missing", {"card_currency": "missing", "new_paper_ids": [], "new_unit_ids": []}
    con = db.connect()
    try:
        # ponytail: one registry per process (a notes refresh renders hundreds of nodes); a new process sees new links.
        key = _registry_cache_key()
        if key not in _REGISTRY_CACHE:
            _REGISTRY_CACHE[key] = _registry(con)
        registry = _REGISTRY_CACHE[key]
        record = registry.by_id.get(concept_id)
        if not record:
            return None, "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}
        if stored.get("status") != "accepted":
            return None, "stale", {"card_currency": "stale", "new_paper_ids": [], "new_unit_ids": []}
        inputs_key = ("node_inputs", vault, concept_id, key)
        if isinstance(request_cache, dict) and inputs_key in request_cache:
            links, paper_ids, units = request_cache[inputs_key]
        else:
            links, paper_ids, units = _node_inputs(con, vault, record, registry)
            if isinstance(request_cache, dict):
                request_cache[inputs_key] = (links, paper_ids, units)
        currency, details = _card_currency(stored, record, vault, paper_ids, units)
        if currency == "stale":
            return None, currency, details
        card = dict(stored.get("card") or stored)
        card.update(details)
        card.setdefault("status", stored.get("status") or "accepted")
        card.pop("card_status", None)
        card["card_check"] = "passed"   # NC-04: a card is returned only if accepted, current and its quotes are unchanged
        return card, currency, details
    finally:
        con.close()


def load(vault: str, concept_id: str, request_cache=None) -> Dict[str, Any] | None:
    """Return a current accepted card; cards with new evidence remain readable."""
    card, _, _ = _current_card_state(vault, concept_id, request_cache)
    return card


def load_status(vault: str, concept_id: str, request_cache=None) -> Dict[str, Any]:
    card, currency, details = _current_card_state(vault, concept_id, request_cache)
    return {"card": card, "stale": currency == "stale", "card_currency": currency, **details}


def _validate(reply: Mapping[str, Any], payload: Mapping[str, Any]) -> Tuple[Dict[str, Any] | None, List[Dict[str, str]]]:
    problems: List[Dict[str, str]] = []
    if not isinstance(reply, Mapping):
        return None, [_problem("reply_shape", "reply", "Expected a JSON object")]
    if reply.get("source_revision") != payload.get("source_revision"):
        problems.append(_problem("source_revision", "source_revision", "Source changed; retrieve a fresh task"))
    candidate = reply.get("node_type") or reply.get("type")
    if candidate not in payload.get("allowed_types", ALLOWED_TYPES):
        problems.append(_problem("node_type", "node_type", "Unknown node card type"))
        candidate = payload["node"]["candidate_type"]
    fields = reply.get("fields")
    if fields is None and isinstance(reply.get("card"), Mapping):
        fields = reply["card"].get("fields")
    if not isinstance(fields, Mapping):
        return None, problems + [_problem("fields", "fields", "Expected fields object")]
    fields = copy.deepcopy(dict(fields))
    source_map = {row["id"]: row for row in payload.get("sentences") or ()}
    schema = payload.get("schema") or {}
    common = list((schema.get("common_fields") or {}).keys())
    for key in common:
        if key != "related" and key not in fields:
            problems.append(_problem("missing_field", f"fields.{key}", "Use a statement or not_found_in_vault"))
    for path, statement in _iter_statements(fields):
        problems.extend(_check_statement(path, statement, source_map, payload))
    if candidate != payload["node"]["candidate_type"]:
        justification = reply.get("type_justification")
        if not justification:
            problems.append(_problem("type_justification", "type_justification", "A type correction requires cited justification"))
        else:
            problems.extend(_check_statement("type_justification", justification, source_map, payload))
    problems.extend(_check_enums(fields, candidate, schema, source_map))
    problems.extend(_check_structured_rows(fields, candidate, source_map, payload))
    related = reply.get("related", fields.get("related", []))
    if related is None:
        related = []
    if not isinstance(related, list):
        problems.append(_problem("related_shape", "related", "Expected a list"))
        related = []
    normalized_related = []
    # Resolve names against the same registry snapshot used to build the task.
    con = db.connect()
    try:
        registry = _registry(con)
        for index, item in enumerate(related):
            path = f"related[{index}]"
            if not isinstance(item, Mapping) or not item.get("name") or item.get("relation") not in RELATIONS:
                problems.append(_problem("related_value", path, "Use {name, relation} with a controlled relation"))
                continue
            target = registry.lookup(str(item["name"]))
            normalized_related.append({"name": str(item["name"]), "relation": item["relation"],
                                       "status": "resolved" if target else "unresolved",
                                       "concept_id": target.get("concept_id") if target else None})
    finally:
        con.close()
    # Keep the machine-owned quote map separate from analyst text.
    quotes = {sid: {"sentence_id": sid, "paper_id": row["paper_id"], "section": row.get("section"),
                    "page": row.get("page"), "text": row.get("text", "")}
              for sid, row in source_map.items()}
    card = {"schema_version": VERSION, "concept_id": payload["node"]["concept_id"], "name": payload["node"]["name"],
            "aliases": payload["node"].get("aliases", []), "node_type": candidate, "fields": fields,
            "related": normalized_related, "quotes": quotes}
    return card, problems


def submit(con, vault: str, name: str, reply: Mapping[str, Any], analyst: str = "mcp-client",
           include_sentence_ids: Iterable[str] | None = None) -> Dict[str, Any]:
    """Validate and atomically store a card, retaining needs_fix results for repair.

    The task is rebuilt with the same includes the analyst used: the explicit argument plus the reply's
    ``include_sentence_ids`` (same eligibility rules as ``task``; rejected IDs stay unknown).
    """
    extra = reply.get("include_sentence_ids") if isinstance(reply, Mapping) else None
    if isinstance(extra, str):
        extra = [extra]
    includes = [*(include_sentence_ids or ()), *(extra if isinstance(extra, list) else ())]
    payload = task(con, vault, name, includes)
    card, problems = _validate(reply, payload)
    record = payload["node"]
    path = _safe_card_path(vault, record["concept_id"])
    status = "accepted" if not problems else "needs_fix"
    # Inventory = every unit the node had when the card was written, not only the capped task's units: otherwise a
    # node over the 500-sentence cap reads "current_with_new_evidence" forever right after a fresh card.
    registry = _registry(con)
    full = registry.by_id.get(record["concept_id"])
    all_units = _node_inputs(con, vault, full, registry)[2] if full else []
    task_units = [unit.get("unit_id") for unit in payload.get("evidence_units") or () if unit.get("unit_id")]
    stored = {"card": card, "analyst": str(analyst or "mcp-client"), "created_at": config.now(),
              "paper_ids": [paper.get("paper_id") for paper in payload.get("papers") or () if paper.get("paper_id")],
              "unit_ids": list(dict.fromkeys(task_units + [u.get("unit_id") for u in all_units if u.get("unit_id")])),
              "source_revision": payload["source_revision"], "validation": {"problems": problems}, "status": status}
    with workflow.writer_lock():  # single lock name "library" for all store/vault writes
        workflow.atomic_json(path, stored)
    return {"ok": not problems, "status": status, "problems": problems, "path": str(path),
            "source_revision": payload["source_revision"]}


def _reply_folder(vault: str, concept_id: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", str(vault)):
        raise ValueError("Invalid vault")
    filename = _SAFE_ID.sub("_", str(concept_id)).strip("._") or "node"
    folder = config.STORE / "ai_replies" / "node_cards" / vault / filename
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _reply_files(folder: Path) -> List[Path]:
    files = []
    for path in folder.glob("*.json"):
        try:
            int(path.stem)
        except ValueError:
            continue
        files.append(path)
    return sorted(files, key=lambda path: int(path.stem))


def _saved_reply(folder: Path, payload: Mapping[str, Any]):
    """Return the newest unprocessed reply that is current and locally valid."""
    for path in reversed(_reply_files(folder)):
        saved = db.read_json(path)
        if not (isinstance(saved, dict) and saved.get("processed") is not True
                and isinstance(saved.get("raw"), str)):
            continue
        if (saved.get("source_revision") is not None
                and saved.get("source_revision") != payload.get("source_revision")):
            continue
        try:
            reply = answer._json(saved["raw"])
            if not isinstance(reply, Mapping):
                continue
            _card, problems = _validate(reply, payload)
            if problems:
                continue
        except (answer.AIError, ValueError, TypeError, KeyError, IndexError):
            continue
        return path, saved
    return None, None


def _save_reply(folder: Path, raw: str, usage: Mapping[str, Any], attempt: int, provider: str,
                payload: Mapping[str, Any]) -> Path:
    files = _reply_files(folder)
    number = (int(files[-1].stem) + 1) if files else 1
    path = folder / f"{number}.json"
    analysis_transport.write_atomic(path, {"raw": raw, "usage": dict(usage or {}), "attempt": attempt,
                                           "provider": provider, "time": config.now(), "processed": False,
                                           "source_revision": payload.get("source_revision")})
    return path


def _mark_reply_processed(path: Path, result: Mapping[str, Any] | None = None):
    saved = db.read_json(path, {}) or {}
    saved["processed"] = True
    if result is not None:
        saved["submit_status"] = result.get("status")
        saved["submit_ok"] = bool(result.get("ok"))
    analysis_transport.write_atomic(path, saved)


def _mark_reply_blocked(path: Path, error: Exception):
    """Record the lock outcome without consuming the reusable raw reply."""
    saved = db.read_json(path, {}) or {}
    saved["status"] = "blocked_by_writer"
    saved["submit_status"] = "blocked_by_writer"
    saved["error"] = str(error)
    saved["processed"] = False
    analysis_transport.write_atomic(path, saved)


def _run_user(payload: Mapping[str, Any], previous=None, problems=None) -> str:
    request = {"task": payload, "reply_schema": payload.get("reply_schema"),
               "instructions": payload.get("instructions"), "json_only": True}
    if previous is not None or problems:
        request["repair"] = {"previous_reply": previous, "problems": problems or []}
        request["repair_instructions"] = (
            "Return a complete corrected reply, not a patch. Preserve every valid field and fix every listed problem."
        )
    return analysis_transport.dumps(request)


def _analyst(settings):
    return f"{settings['provider']}/{answer.model_of(settings) or 'default'}"


def run(con, vault, name, provider=None, model=None, reasoning=None, max_attempts=3, timeout=None):
    """Run a configured provider for one node card, retaining raw replies for reuse."""
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    started = time.monotonic()
    payload = task(con, vault, name)
    concept_id = payload["node"]["concept_id"]
    folder = _reply_folder(vault, concept_id)
    settings = analysis_transport.provider_settings(provider, model, reasoning, timeout, task="node_card")
    analyst = _analyst(settings)
    system = ("You are completing a source-grounded biomedical node card. Return JSON only. "
              "Use only the supplied task and sentence IDs; never invent quotes or evidence.\n" +
              analysis_transport.dumps(payload.get("reply_schema")))
    previous = None
    problems = []
    usage_total = {}
    attempts = 0
    saved_path, saved = _saved_reply(folder, payload)
    while attempts < max_attempts:
        attempts += 1
        if saved_path is not None and saved is not None:
            reply_path = saved_path
            raw = saved["raw"]
            usage = saved.get("usage") or {}
            saved_path, saved = None, None
        else:
            try:
                raw, usage = analysis_transport.logged_call(
                    analysis_transport.call, config.LOGS_DIR / "ai_usage.jsonl", "node_card", settings, system,
                    _run_user(payload, previous, problems), extra={"concept_id": concept_id})
            except answer.ProviderTimeout as exc:
                return {"status": "provider_timeout", "attempts": attempts, "tokens": usage_total,
                        "provider": exc.provider, "timeout": exc.timeout, "error": str(exc),
                        "seconds": round(time.monotonic() - started, 3)}
            except Exception as exc:
                return {"status": "failed", "attempts": attempts, "tokens": usage_total,
                        "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 3)}
            reply_path = _save_reply(folder, raw, usage, attempts, settings["provider"], payload)
        for key, value in analysis_transport.normalized_usage(usage).items():
            usage_total[key] = usage_total.get(key, 0) + value
        try:
            reply = answer._json(raw)
            if not isinstance(reply, Mapping):
                raise ValueError("AI reply must be a JSON object")
            result = submit(con, vault, name, reply, analyst)
        except workflow.WriterBusy as exc:
            _mark_reply_blocked(reply_path, exc)
            return {"status": "blocked_by_writer", "attempts": attempts, "tokens": usage_total,
                    "error": str(exc), "seconds": round(time.monotonic() - started, 3)}
        except Exception as exc:
            # Validation/parse failures are repairable; an unexpected submit exception is
            # deliberately propagated so the unprocessed raw reply can be reused after a crash.
            if isinstance(exc, (answer.AIError, ValueError, TypeError, KeyError, IndexError)):
                problems = [{"code": "reply_parse", "detail": str(exc)}]
                _mark_reply_processed(reply_path)
                previous = raw
                if attempts >= max_attempts:
                    return {"status": "needs_fix", "attempts": attempts, "tokens": usage_total,
                            "problems": problems, "seconds": round(time.monotonic() - started, 3)}
                continue
            raise
        _mark_reply_processed(reply_path, result)
        if result.get("ok"):
            return {**result, "status": result.get("status", "accepted"), "attempts": attempts,
                    "tokens": usage_total, "seconds": round(time.monotonic() - started, 3)}
        problems = result.get("problems") or [{"code": "validation", "detail": "Reply was rejected"}]
        previous = reply
        if attempts >= max_attempts:
            return {**result, "attempts": attempts, "tokens": usage_total,
                    "seconds": round(time.monotonic() - started, 3)}
    return {"status": "needs_fix", "attempts": attempts, "tokens": usage_total,
            "problems": problems, "seconds": round(time.monotonic() - started, 3)}


def _run_log(row):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with (config.LOGS_DIR / "node_cards_runs.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(analysis_transport.dumps(row) + "\n")


def run_pending(con, vault, limit=None, provider=None, model=None, reasoning=None, max_attempts=3, timeout=None):
    """Run pending cards in :func:`pending` order and append one audit line per node."""
    rows = pending(con, vault)
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        rows = rows[:limit]
    results = []
    for row in rows:
        started = time.monotonic()
        try:
            result = run(con, vault, row["name"], provider, model, reasoning, max_attempts, timeout)
        except Exception as exc:
            result = {"status": "failed", "attempts": 0, "tokens": {},
                      "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 3)}
        audit = {"name": row["name"], "concept_id": row["concept_id"], "status": result.get("status"),
                 "attempts": result.get("attempts", 0), "tokens": result.get("tokens") or {},
                 "provider": result.get("provider"), "timeout": result.get("timeout"),
                 "seconds": result.get("seconds", round(time.monotonic() - started, 3))}
        _run_log(audit)
        results.append({**audit, "result": result})
        if result.get("status") == "blocked_by_writer":
            done = sum(item.get("status") in {"accepted", "done", "review"} for item in results[:-1])
            remaining = len(rows) - len(results)
            results[-1]["result"]["run_summary"] = {
                "done": done,
                "remaining": remaining,
                "message": f"Writer lock is busy; {done} items done and {remaining} wait for the lock.",
            }
            break
    timeouts = sum(item.get("status") == "provider_timeout" for item in results)
    if results:
        summary = {"done": sum(item.get("status") in {"accepted", "done", "review"} for item in results),
                   "timeouts": timeouts, "remaining": len(rows) - len(results),
                   "message": f"{timeouts} mục quá thời gian chờ nhà cung cấp AI — sẽ thử lại lần chạy sau"}
        results[-1]["run_summary"] = summary
        results[-1]["result"].setdefault("run_summary", summary)
    return results


def pending(con, vault: str) -> List[Dict[str, Any]]:
    """Return vault-linked nodes lacking a current accepted card, by evidence then papers."""
    with structure.stamp_scope():  # read-only: hash each paper's files once, not once per evidence unit (10+ min -> seconds)
        return _pending(con, vault)


def _pending(con, vault: str) -> List[Dict[str, Any]]:
    registry = _registry(con)
    request_cache, cache_key = {}, _registry_cache_key()
    ids = [row[0] for row in con.execute(
        "SELECT DISTINCT cl.concept_id FROM concept_links cl JOIN paper_vaults pv ON pv.paper_id = cl.paper_id "
        "WHERE pv.vault = ?", (vault,))]
    rows = []
    for concept_id in ids:
        record = registry.by_id.get(concept_id)
        if not record:
            continue
        links, paper_ids, units = _node_inputs(con, vault, record, registry)  # the same inputs the currency check needs
        request_cache[("node_inputs", vault, concept_id, cache_key)] = (links, paper_ids, units)
        cn = __import__("kbsys.concept_notes", fromlist=["shown_in_vault"])
        if not cn.shown_in_vault(cn._collect(con, record, registry, vault)[1], units):  # same links as _refresh (descendants)
            continue  # below the vault threshold: no note, so no card
        state = _raw_card(vault, concept_id)
        currency = "missing"
        if state and state.get("status") == "accepted":
            _, currency, _ = _current_card_state(vault, concept_id, request_cache)
        if currency == "current":
            continue
        rows.append({"concept_id": concept_id, "name": record["name"], "type": record.get("type"),
                     "candidate_type": _candidate_type(record), "evidence_count": len(units),
                     "paper_count": len({row["paper_id"] for row in links}), "card_currency": currency})
    priority = {"stale": 0, "missing": 1, "current_with_new_evidence": 2}
    return sorted(rows, key=lambda row: (priority.get(row["card_currency"], 1),
                                         -row["evidence_count"], -row["paper_count"],
                                         row["name"].casefold(), row["concept_id"]))


def show(vault: str, name: str) -> Dict[str, Any] | None:
    """Print and return a current card; source quotes are always code-copied."""
    con = db.connect()
    try:
        record = _record(_registry(con), name)
        state = load_status(vault, record["concept_id"])
        if state["card"] is None:
            output = {"status": state["card_currency"], "stale": state["stale"],
                      "concept_id": record["concept_id"], "name": record["name"]}
        else:
            output = {"status": "accepted", "stale": False, **state["card"]}
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return output if output.get("status") == "accepted" else None
    finally:
        con.close()


def _read_reply(value: str) -> Any:
    path = Path(value)
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    return json.loads(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Source-grounded typed node cards")
    sub = parser.add_subparsers(dest="command", required=True)
    task_parser = sub.add_parser("task")
    task_parser.add_argument("--vault", required=True)
    task_parser.add_argument("--name", required=True)
    task_parser.add_argument("--include-sentence-id", "--include-sentence-ids", action="append", default=[],
                              help="Retain a direct/evidence/protocol sentence ID (repeatable)")
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--vault", required=True)
    submit_parser.add_argument("--name", required=True)
    submit_parser.add_argument("--reply", required=True, help="JSON text or a JSON file path")
    submit_parser.add_argument("--analyst", default="cli")
    submit_parser.add_argument("--include-sentence-id", "--include-sentence-ids", action="append", default=[],
                               help="Same includes used with `task` (also read from the reply's include_sentence_ids)")
    pending_parser = sub.add_parser("pending")
    pending_parser.add_argument("--vault", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("name")
    run_parser.add_argument("--vault", required=True)
    run_parser.add_argument("--provider")
    run_parser.add_argument("--model")
    run_parser.add_argument("--reasoning")
    run_parser.add_argument("--timeout", type=float)
    run_parser.add_argument("--max-attempts", type=int, default=3)
    run_pending_parser = sub.add_parser("run-pending")
    run_pending_parser.add_argument("--vault", required=True)
    run_pending_parser.add_argument("--limit", type=int)
    run_pending_parser.add_argument("--provider")
    run_pending_parser.add_argument("--model")
    run_pending_parser.add_argument("--reasoning")
    run_pending_parser.add_argument("--timeout", type=float)
    run_pending_parser.add_argument("--max-attempts", type=int, default=3)
    show_parser = sub.add_parser("show")
    show_parser.add_argument("--vault", required=True)
    show_parser.add_argument("--name", required=True)
    args = parser.parse_args(argv)
    con = db.connect()
    try:
        if args.command == "task":
            value = task(con, args.vault, args.name, args.include_sentence_id)
        elif args.command == "submit":
            value = submit(con, args.vault, args.name, _read_reply(args.reply), args.analyst,
                           args.include_sentence_id)
        elif args.command == "pending":
            value = pending(con, args.vault)
        elif args.command == "run":
            value = run(con, args.vault, args.name, args.provider, args.model, args.reasoning,
                        args.max_attempts, args.timeout)
        elif args.command == "run-pending":
            value = run_pending(con, args.vault, args.limit, args.provider, args.model, args.reasoning,
                                args.max_attempts, args.timeout)
        else:
            show(args.vault, args.name)
            return 0
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not isinstance(value, dict) or value.get("ok", True) else 1
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
