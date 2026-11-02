"""Evidence-bound intervention enrichment and deterministic relation candidates (M4.2)."""

import hashlib
import json
import re
import unicodedata
from datetime import date

from . import answer, concepts, config, db, presentation, standard

FIELDS = {
    "intervention": ("agent", "dose", "timing", "duration", "route"),
    "model": ("species", "cell_type", "origin", "condition"),
}
RELATIONS = config.STORE / "relations.jsonl"


def _norm(text):
    return " ".join(unicodedata.normalize("NFKC", str(text or "")).casefold().split())


def _read_sentences(paper_id):
    rows = list(db.read_jsonl(db.paper_dir(paper_id) / "sentences.jsonl"))
    return {s["sentence_id"]: s for s in rows}, rows


def _context(unit, sentence_map, ordered):
    ids = list(unit.get("sentence_ids") or [])
    # Add at most two adjacent same-paragraph sentences, while retaining the unit's citations.
    indices = [i for i, sentence in enumerate(ordered) if sentence.get("sentence_id") in ids]
    chosen = set(ids)
    for i in indices:
        current = ordered[i]
        for j in (i - 1, i + 1):
            if 0 <= j < len(ordered):
                candidate = ordered[j]
                if (candidate.get("passage_id") == current.get("passage_id") and
                        candidate.get("section") == current.get("section") and
                        candidate.get("source", "main") == current.get("source", "main")):
                    chosen.add(candidate["sentence_id"])
    return sorted((sentence_map[sid] for sid in chosen if sid in sentence_map),
                  key=lambda s: s.get("seq", 0))


def _source_contains(value, ids, sentence_map):
    value = " ".join(unicodedata.normalize("NFKC", str(value or "")).split())
    value_norm = _norm(value)
    if len(value_norm) < 2:
        return False
    # Match complete tokens like the concept linker, allowing its conservative plural variant.
    variants = {value}
    last = re.search(r"[A-Za-z]+$", value)
    if last:
        token = last.group(0)
        # Follow the Concept Note's final-token plural suffix patterns, including
        # short regular words such as rat -> rats.
        plural = concepts._plural_token(token)
        if plural == token and token.isalpha() and not token.isupper():
            low = token.lower()
            if low.endswith("y") and len(low) > 3 and low[-2] not in "aeiou":
                plural = token[:-1] + "ies"
            elif low.endswith(("s", "x", "z", "ch", "sh")):
                plural = token + "es"
            else:
                plural = token + "s"
        if plural != last.group(0):
            variants.add(value[:last.start()] + plural)
    pattern = re.compile(r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(v) for v in
                            sorted(variants, key=len, reverse=True)) + r")(?![A-Za-z0-9])", re.I)
    return any(pattern.search(unicodedata.normalize("NFKC", sentence_map[sid].get("text", "")))
               for sid in ids if sid in sentence_map)


def enrich(con, paper_id=None, dry_run=False):
    """Fill missing structured fields from exact cited sentence text via approved answer provider."""
    if paper_id:
        paper_ids = [paper_id]
    else:
        paper_ids = [r[0] for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]
    totals = {"papers": 0, "eligible": 0, "filled_fields": 0, "dropped_fields": 0, "skipped_human": 0}
    settings = answer.settings("enrichment")
    provider = settings.get("provider", "none")
    model = ((settings.get(provider) or {}).get("model", "") if provider != "none" else "")
    if not dry_run and not answer.provider_status(settings).get("ready"):
        raise answer.AIError(answer.provider_status(settings).get("message", "provider unavailable"))
    for pid in paper_ids:
        folder = db.paper_dir(pid)
        path = folder / "evidence.jsonl"
        if not path.is_file():
            continue
        units = list(db.read_jsonl(path))
        sentence_map, ordered = _read_sentences(pid)
        changed = False
        for unit in units:
            outcome = unit.get("outcome") or {}
            if unit.get("unit_type") != "result" or outcome.get("direction") not in ("increase", "decrease"):
                continue
            intervention, model_fields = unit.setdefault("intervention", {}), unit.setdefault("model", {})
            # Human-confirmed fields are immutable; other existing values are also never overwritten.
            if unit.get("status") == "human_confirmed":
                totals["skipped_human"] += 1
                continue
            missing = {group: [field for field in fields if
                               (intervention if group == "intervention" else model_fields).get(field) in (None, "")]
                       for group, fields in FIELDS.items()}
            if not any(missing.values()) or unit.get("status") not in ("ok", "human_confirmed"):
                continue
            ctx = _context(unit, sentence_map, ordered)
            allowed_ids = [s["sentence_id"] for s in ctx]
            totals["eligible"] += 1
            if dry_run:
                continue
            payload = {"unit_id": unit["unit_id"], "quote": unit.get("quote", ""),
                       "statement": unit.get("statement", ""),
                       "sentences": [{"sentence_id": s["sentence_id"], "text": s.get("text", "")} for s in ctx],
                       "missing_fields": missing}
            system = ("Extract only explicitly stated intervention and model values from the supplied sentences. "
                      "Return JSON only with intervention, model, and evidence_sentence_ids. Use null for unknown. "
                      "Do not infer; sentence IDs must be supplied IDs.")
            raw, _ = answer._chat(settings, system, json.dumps(payload, ensure_ascii=False))
            data = answer._json(raw)
            if not isinstance(data, dict):
                continue
            ids = data.get("evidence_sentence_ids")
            if not isinstance(ids, list) or not ids or any(sid not in allowed_ids for sid in ids):
                totals["dropped_fields"] += sum(len(v) for v in missing.values())
                continue
            provenance = unit.setdefault("field_provenance", {})
            filled = False
            for group, fields in FIELDS.items():
                target, returned = (intervention if group == "intervention" else model_fields), data.get(group)
                if not isinstance(returned, dict):
                    returned = {}
                group_prov = provenance.setdefault(group, {})
                for field in fields:
                    value = returned.get(field)
                    if target.get(field) not in (None, "") or field not in missing[group] or value in (None, ""):
                        continue
                    if not _source_contains(value, ids, sentence_map):
                        totals["dropped_fields"] += 1
                        continue
                    target[field] = str(value).strip()
                    group_prov[field] = {"source": "ai_enriched", "sentence_ids": ids,
                                         "provider": provider, "model": model, "date": date.today().isoformat()}
                    totals["filled_fields"] += 1
                    changed = filled = True
            if filled:  # only the units that actually received a value
                unit["enrichment_status"] = "ai_enriched"
        if changed:
            history = folder / "analysis_history"
            history.mkdir(exist_ok=True)
            stamp = date.today().strftime("%Y%m%d") + "_" + config.now()[11:19].replace(":", "")
            archive = history / f"evidence_{stamp}.jsonl"
            suffix = 1
            while archive.exists():
                archive = history / f"evidence_{stamp}_{suffix}.jsonl"
                suffix += 1
            path.replace(archive)
            db.write_jsonl(path, units)
            db.reload_paper(con, pid)
            con.commit()
            totals["papers"] += 1
    return totals


MEASURE_GROUPS = config.STORE / "measure_groups.json"
# Relation types shown as disagreements. Never used to hide or delete evidence.
CONFLICT_TYPES = ("contradicts", "direction_differs", "effect_vs_no_effect")
# Only these statuses are live conflicts. `withdrawn_by_rule` and unknown statuses are
# audit history: the store row stays (recoverable) but no consumer shows it.
ACTIVE_RELATION_STATUSES = frozenset(("ai_proposed", "proposed", "human_confirmed"))
# Human review outcomes kept visible as history (never as an open conflict).
REVIEWED_RELATION_STATUSES = frozenset(("explained", "dismissed", "rejected", "human_rejected", "stale"))


def conflict_relations(vault=None, history=False):
    """THE reader of conflict relations: active ones, plus reviewed history when asked.

    Rows with other statuses (e.g. ``withdrawn_by_rule``) are never returned; they stay in
    ``relations.jsonl``. A vault is matched strictly: a row without a ``vault`` belongs to none
    (``backfill_vault`` fills the legacy ones).
    """
    keep = ACTIVE_RELATION_STATUSES | REVIEWED_RELATION_STATUSES if history else ACTIVE_RELATION_STATUSES
    return [r for r in db.read_jsonl(config.STORE / "relations.jsonl") or []
            if r.get("type") in CONFLICT_TYPES and r.get("status") in keep
            and (vault is None or r.get("vault") == vault)]


def _paper_of(unit_id):
    return str(unit_id or "").rsplit(":", 1)[0]


def _paper_vaults(paper_id):
    meta = db.read_json(config.PAPERS_DIR / paper_id / "metadata.json", {}) if paper_id and "/" not in paper_id \
        and "\\" not in paper_id and paper_id not in (".", "..") else {}
    return set((meta or {}).get("vaults") or [])


def _shared_vault(unit_a, unit_b):
    """The one vault both units' papers belong to; None when there is none or several."""
    shared = _paper_vaults(_paper_of(unit_a)) & _paper_vaults(_paper_of(unit_b))
    return next(iter(shared)) if len(shared) == 1 else None


def backfill_vault(dry_run=False):
    """Give legacy relation rows without ``vault`` the vault both of their papers belong to.

    Only a single shared vault is written; a pair with none or several shared vaults is left alone and
    reported. Everything else in the row is untouched (a data write: take a backup first)."""
    rows = list(db.read_jsonl(RELATIONS) or [])
    filled, ambiguous = 0, []
    for r in rows:
        if r.get("vault"):
            continue
        shared = _shared_vault(r.get("from_unit"), r.get("to_unit"))
        if shared:
            r["vault"] = shared
            filled += 1
        else:
            ambiguous.append(r.get("relation_id"))
    if filled and not dry_run:
        db.write_jsonl(RELATIONS, rows)
    return {"rows": len(rows), "filled": filled, "ambiguous": ambiguous, "dry_run": dry_run}

# Registry entries in this set are useful for retrieval but never identify a
# measured entity for a cross-paper relation.  Keep the names explicit and
# conservative: specific marks, markers, genes, proteins, and outcome rates
# remain eligible. Method-like registry types are filtered separately below.
MEASURE_ENTITY_STOPLIST = frozenset({
    "warming", "cooling", "vitrification", "embryo", "embryos", "oocyte", "oocytes",
    "cell", "cells", "dna", "rna", "histone", "histones", "h3", "h4", "chromatin",
    "gene expression", "mrna", "blastocyst", "blastocysts", "zygote", "zygotes",
    "morula", "morulae", "cleavage", "stage", "stages",
})
_INTERVENTION_CONTEXT = (
    ("vitrification", re.compile(r"\bvitrif(?:ication|ied|y|ying)\b", re.I)),
    ("cryopreservation", re.compile(r"\bcryo(?:preservation|preserved|storage|stored)\b", re.I)),
    ("freezing", re.compile(r"\bfreez(?:ing|e|en)\b", re.I)),
    ("thawing", re.compile(r"\bthaw(?:ing|ed)?\b", re.I)),
)
METHOD_CONCEPT_TYPES = frozenset({
    "method", "methods", "technique", "techniques", "protocol", "protocols",
    "assay", "assays", "analysis", "analyses", "procedure", "procedures",
})
MEASURE_DIRECTIONS = ("increase", "decrease", "no_change", "mixed")
EFFECT_DIRECTIONS = frozenset(("increase", "decrease"))
MAX_PAIRS_PER_CONCEPT = 30


def _raw_measure_key(value):
    return re.sub(r"[^a-z0-9α-ωΔδ]+", " ", _norm(value)).strip()


def load_measure_groups():
    """{normalized member measure: group} from measure_groups.json (reviewable synonym groups)."""
    data = db.read_json(MEASURE_GROUPS, {}) if MEASURE_GROUPS.exists() else {}
    out = {}
    for group in data.get("groups") or []:
        if group.get("status") == "human_rejected":
            continue
        for member in group.get("members") or []:
            out[_raw_measure_key(member)] = group
    return out


def measure_group(value, groups=None):
    """The synonym group of an outcome measure, or None when it is not grouped."""
    groups = load_measure_groups() if groups is None else groups
    return groups.get(_raw_measure_key(value))


def _relation_id(a, b, relation_type):
    raw = "|".join(sorted((a, b)) + [relation_type])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _unit_rows(vault=None):
    rows = []
    sentence_cache = {}
    for paper in sorted(config.PAPERS_DIR.iterdir()) if config.PAPERS_DIR.exists() else []:
        if not (paper / "metadata.json").is_file():
            continue
        meta = db.read_json(paper / "metadata.json", {})
        if vault and vault not in (meta.get("vaults") or []):
            continue
        sentence_path = paper / "sentences.jsonl"
        if sentence_path.is_file():
            sentence_cache[paper.name] = {
                row.get("sentence_id"): row.get("text", "")
                for row in db.read_jsonl(sentence_path)
                if row.get("sentence_id")
            }
        # Only unit fields are compared; sentence text is not kept (low RAM with thousands of papers).
        for unit in db.read_jsonl(paper / "evidence.jsonl") or []:
            outcome = unit.get("outcome") or {}
            if (unit.get("unit_type") == "result" and unit.get("status") in ("ok", "human_confirmed") and
                    outcome.get("direction") in MEASURE_DIRECTIONS and outcome.get("measure")):
                # Keep source sentence text only in this in-memory relation row. It is
                # never written back to evidence and avoids rereading a paper for each
                # candidate pair.
                row_unit = dict(unit)
                sentence_map = sentence_cache.get(paper.name, {})
                row_unit["_relation_source_sentences"] = [
                    sentence_map[sid] for sid in (unit.get("sentence_ids") or [])
                    if sentence_map.get(sid)
                ]
                rows.append((paper.name, row_unit, meta))
    return rows


def _comparison(a, b, require_match=True):
    ia, ib = a[1].get("intervention") or {}, b[1].get("intervention") or {}
    ma, mb = a[1].get("model") or {}, b[1].get("model") or {}
    # RL-02: only a same named agent makes a comparison strict (a matching cell type is not the agent).
    agent_match = bool(ia.get("agent") and ib.get("agent") and _norm(ia["agent"]) == _norm(ib["agent"]))
    if require_match and not agent_match:
        return None
    differences = []
    # Keep the comparison deliberately source-structured.  These fields are
    # present in the v2 evidence contract, while the extra technique aliases
    # accommodate older/demo evidence without inventing values.
    fields_by_group = (
        ("model", ma, mb, ("species", "stage", "cell_type", "origin", "condition", "setting")),
        ("intervention", ia, ib, ("agent", "protocol", "dose", "timing", "duration", "route", "comparator", "technique")),
    )
    for group, left, right, fields in fields_by_group:
        for field in fields:
            x, y = left.get(field), right.get(field)
            if _norm(x) != _norm(y) and (x not in (None, "") or y not in (None, "")):
                differences.append({"field": f"{group}.{field}", "from": x, "to": y})
    outcomes = (a[1].get("outcome") or {}, b[1].get("outcome") or {})
    for field in ("method", "technique"):
        x, y = outcomes[0].get(field), outcomes[1].get(field)
        if _norm(x) != _norm(y) and (x not in (None, "") or y not in (None, "")):
            differences.append({"field": f"outcome.{field}", "from": x, "to": y})
    return differences


def _is_organism_concept(record):
    """Return whether a registry concept is only an organism/model axis."""
    kind = _norm(record.get("type") or record.get("kind") or record.get("entity_kind"))
    return kind in {"species", "organism"}


def _is_organism_measure(value, registry):
    """Whether a measure is just an organism name or synonym (a model axis, never a measured outcome)."""
    key = _raw_measure_key(value)
    return bool(key) and any(_is_organism_concept(record) and key in {
        _raw_measure_key(name) for name in (record.get("name"), *(record.get("synonyms") or ()))}
                             for record in _registry_records(registry))


_BUNDLE = re.compile(r"[,;/&]|\s(?:and|or|versus|vs\.?)\s", re.I)


def _is_bundled(measure):
    """RL-03: a measure that names several outcomes; it pairs only through its clauses."""
    return bool(_BUNDLE.search(str(measure or "")))


def _registry_records(registry):
    """Return registry records while tolerating the small test doubles used offline."""
    return list(getattr(registry, "records", registry or ()))


def _is_generic_measure_concept(record, measure_text=None):
    name = _norm(record.get("name"))
    # A context noun such as ``blastocyst`` is not a measured entity merely
    # because another word in the measure says ``frequency`` or ``rate``.
    # Exact full-measure matching still permits legitimate pairs such as the
    # same ``blastocyst formation rate`` on both sides.
    return name in MEASURE_ENTITY_STOPLIST


def _is_method_concept(record):
    kind = _norm(record.get("type") or record.get("kind") or record.get("entity_kind"))
    return kind in METHOD_CONCEPT_TYPES


def _is_stoplisted_measure_concept(record, measure_text=None):
    # A species/organism word inside a measure ("Human Golgi-positive cells", "human Mash1") is the model, never the
    # measured entity: two unrelated measures were paired as conflicts on "Human".
    return (_is_generic_measure_concept(record, measure_text) or _is_method_concept(record)
            or _is_organism_concept(record))


def _is_generic_measure_text(value):
    return _norm(value).strip() in MEASURE_ENTITY_STOPLIST


def _is_method_measure_text(value, registry):
    """Whether a measure contains a registry concept classified as a method."""
    text = str(value or "")
    return any(_is_method_concept(record) and presentation.matches_term(
        text, record["name"], record.get("synonyms") or [])
               for record in _registry_records(registry)
               if record.get("concept_id") and record.get("name"))


def _normalised_measure_text(value, groups):
    """Use the reviewed measure-group label when one exists for the measure."""
    group = measure_group(value, groups)
    return str((group or {}).get("name") or value or "")


def _drop_registry_ancestors(matches):
    """Keep the most-specific matched registry concepts using parent IDs."""
    by_id = {record.get("concept_id"): record for record in matches}

    def ancestors(record_id):
        seen, pending = set(), list((by_id.get(record_id) or {}).get("parents") or [])
        while pending:
            parent_id = pending.pop()
            if parent_id in seen:
                continue
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            if parent:
                pending.extend(parent.get("parents") or [])
        return seen

    descendants = set()
    for record in matches:
        descendants.update(ancestors(record.get("concept_id")))
    return [record for record in matches if record.get("concept_id") not in descendants]


def _shared_concepts(row, registry, groups=None):
    """Find the most-specific eligible concepts in ``outcome.measure`` only."""
    outcome = row[1].get("outcome") or {}
    text = _normalised_measure_text(outcome.get("measure"), groups or {})
    records = [record for record in _registry_records(registry)
               if record.get("concept_id") and record.get("name") and
               not _is_stoplisted_measure_concept(record, text)]
    matches = [record for record in records
               if presentation.matches_term(text, record["name"], record.get("synonyms") or [])]
    return _drop_registry_ancestors(matches)


_CLAUSE_DIRECTIONS = (
    ("no_change", re.compile(
        r"\b(?:did\s+not\s+(?:significantly\s+)?(?:change|differ)|"
        r"was\s+not\s+(?:significantly\s+)?(?:different|changed)|"
        r"were\s+not\s+(?:significantly\s+)?(?:different|changed)|"
        r"(?:was|were|remained|remain)\s+unchanged|"
        r"(?:showed|showing)\s+no\s+(?:significant\s+)?change)\b", re.I)),
    ("increase", re.compile(
        r"\b(?:increased|increasing|rose|elevated|higher|greater|up-?regulated|"
        r"was\s+higher|were\s+higher|was\s+greater|were\s+greater)\b", re.I)),
    ("decrease", re.compile(
        r"\b(?:decreased|decreasing|fell|lower|reduced|down-?regulated|"
        r"was\s+lower|were\s+lower)\b", re.I)),
)
_CLAUSE_BREAK = re.compile(r";|,\s*(?:whereas|while|but)\b|\b(?:whereas|while|but)\b", re.I)


def _concept_spans(text, record):
    """Yield source offsets for a registry name or alias, preserving exact text."""
    names = (record.get("name"), *(record.get("synonyms") or ()))
    for name in names:
        name = str(name or "").strip()
        if not name:
            continue
        flags = 0 if re.fullmatch(r"[A-Z][A-Z0-9]+", name) else re.I
        pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", flags)
        yield from pattern.finditer(text)


def _names_word(text, words):
    """True when ``text`` names one of ``words`` (matched like a registry name: all-caps = case-sensitive)."""
    return any(next(_concept_spans(text, {"name": word}), None) for word in words)


def _entity_spans(text, record):
    """RL-03 rule B: spans of ``record`` that are not a modifier (inside parentheses, or "pyroptosis-related").
    A suffix that is one letter or not lower case is an isoform, not a modifier ("NGF-β", "PDGF-BB", "VEGF-A")."""
    return [span for span in _concept_spans(text, record)
            if text.count("(", 0, span.start()) <= text.count(")", 0, span.start())
            and not ((suffix := re.match(r"-([^\W\d_]{2,})", text[span.end():])) and suffix.group(1).islower())]


def _clause_for_match(text, entity_match, direction_match):
    """Return the smallest source clause containing both spans, or None.

    Commas inside a coordinated entity list remain part of one clause. Explicit
    contrast/concession markers and semicolons always delimit clauses.
    """
    start = 0
    end = len(text)
    for boundary in _CLAUSE_BREAK.finditer(text):
        if boundary.start() < entity_match.start() and boundary.end() <= entity_match.start():
            start = boundary.end()
        elif boundary.start() >= entity_match.end() and boundary.start() < direction_match.start():
            return None
        elif boundary.start() >= direction_match.end():
            end = boundary.start()
            break
    if not (start <= entity_match.start() and direction_match.end() <= end):
        return None
    clause = text[start:end].strip(" ,;\t")
    return clause or None


def _clause_matches(row, registry, groups=None):
    """Find explicit entity directions in an aggregate/mixed unit's text."""
    unit = row[1]
    outcome = unit.get("outcome") or {}
    measure_records = _shared_concepts(row, registry, groups)
    # A specifically measured entity already has a unit-level direction unless
    # that direction is mixed or the measure is bundled (RL-03: a bundled measure
    # pairs only per outcome, through its clauses).
    if measure_records and outcome.get("direction") != "mixed" and not _is_bundled(outcome.get("measure")):
        return {}
    # RL-03 rule A: an enrichment / baseline-table unit (not bundled) has no per-entity outcome.
    non_outcome = standard.param("RL-03", "non_outcome_words")
    predicates = "|".join(re.escape(word) for word in standard.param("RL-03", "predicate_words"))
    other_predicate = re.compile(rf"\b(?:{predicates})\b[^,;:]*[,;:]", re.I)
    if not _is_bundled(outcome.get("measure")) and _names_word(str(outcome.get("measure") or ""), non_outcome):
        return {}
    agent = str((unit.get("intervention") or {}).get("agent") or "")
    # RL-03: the unit's own intervention agent is never a clause entity.
    records = [record for record in _registry_records(registry)
               if record.get("concept_id") and record.get("name") and
               not _is_method_concept(record)]
    texts = [unit.get("statement", "")] + list(unit.get("_relation_source_sentences") or [])
    found = {}
    for text in texts:
        text = str(text or "")
        if not text or _names_word(text, non_outcome):
            continue
        # the agent test runs only on records the text names (cheap)
        matched_records = _drop_registry_ancestors(
            [record for record in records if not _is_stoplisted_measure_concept(record, text)
             and _entity_spans(text, record) and not (agent and list(_concept_spans(agent, record)))]
        )
        for record in matched_records:
            if record.get("concept_id") in found:
                continue
            matches = _entity_spans(text, record)
            if not matches:
                continue
            directions = []
            for direction, pattern in _CLAUSE_DIRECTIONS:
                directions.extend((direction, match) for match in pattern.finditer(text))
            directions.sort(key=lambda item: item[1].start())
            for entity_match in sorted(matches, key=lambda match: match.start()):
                for direction, direction_match in directions:
                    if direction_match.start() < entity_match.end():
                        continue
                    # RL-03 rule C: another predicate between the entity and the direction word owns it.
                    if other_predicate.search(text, entity_match.end(), direction_match.start()):
                        break
                    clause = _clause_for_match(text, entity_match, direction_match)
                    if clause:
                        # RL-03: direction words of more than one direction in the clause leave
                        # the entity without a direction (which one belongs to it is not known).
                        if len({name for name, pattern in _CLAUSE_DIRECTIONS if pattern.search(clause)}) == 1:
                            found[record["concept_id"]] = {
                                "match_basis": f"shared_concept_clause:{record['concept_id']}",
                                "measure_group": None,
                                "shared_concept_id": record["concept_id"],
                                "shared_concept_name": record["name"],
                                "clause": clause,
                                "entity_direction": direction,
                            }
                        break
                if record.get("concept_id") in found:
                    break
    return found


def _text_intervention_context(unit):
    text = " ".join([str(unit.get("statement") or ""),
                      *[str(value) for value in unit.get("_relation_source_sentences") or ()]])
    return {label for label, pattern in _INTERVENTION_CONTEXT if pattern.search(text)}


def _agent_of(row):
    return (row[1].get("intervention") or {}).get("agent")


def _same_agent(left, right):
    """RL-02: equal named agents; a named agent against an empty one is different; two empty agents
    are the same only when both texts name a shared intervention (e.g. vitrification)."""
    first, second = _norm(_agent_of(left)), _norm(_agent_of(right))
    if first or second:
        return first == second
    return bool(_text_intervention_context(left[1]) & _text_intervention_context(right[1]))


def _reduced_measure(text, record, quantity_words):
    """The measure text without the shared concept (name and synonyms) and the quantity words."""
    text = str(text or "")
    # blank every covered character: a name and a synonym can overlap ("Protein HMG 14" / "HMG 14")
    covered = {i for match in _concept_spans(text, record) for i in range(*match.span())}
    text = "".join(" " if i in covered else char for i, char in enumerate(text))
    return " ".join(word for word in _raw_measure_key(text).split() if word not in quantity_words)


def _same_outcome(match, texts, records_by_id):
    """RL-02: a shared-concept pair measures one outcome only when the compared texts agree once the
    concept and the quantity words are removed. exact_measure / measure_group are one outcome by definition.
    ponytail: naive word list (RL-02 outcome_quantity_words); upgrade = reviewed measure groups."""
    if not str(match.get("match_basis") or "").startswith("shared_concept"):
        return True
    record = records_by_id.get(match.get("shared_concept_id"))
    quantity = {_raw_measure_key(word) for word in standard.param("RL-02", "outcome_quantity_words")}
    return bool(record) and len({_reduced_measure(text, record, quantity) for text in texts}) == 1


def _match_tokens(row, groups, registry):
    """Return comparable keys and display metadata for one result unit."""
    outcome = row[1].get("outcome") or {}
    measure = outcome.get("measure")
    tokens = {}
    bundled = _is_bundled(measure)  # RL-03: no exact / shared-concept token, only clauses
    shared_records = [] if bundled else _shared_concepts(row, registry, groups)
    group = measure_group(measure, groups)
    if (group and not _is_generic_measure_text(group.get("name")) and
            not _is_method_measure_text(group.get("name"), registry)):
        tokens[("measure_group", group.get("group_id"))] = {
            "match_basis": "measure_group", "measure_group": group.get("group_id"),
            "shared_concept_id": None, "shared_concept_name": None,
        }
    exact = _raw_measure_key(measure)
    if (exact and not bundled and not shared_records and not _is_generic_measure_text(measure) and
            not _is_method_measure_text(measure, registry) and not _is_organism_measure(measure, registry)):
        tokens[("exact_measure", exact)] = {
            "match_basis": "exact_measure", "measure_group": None,
            "shared_concept_id": None, "shared_concept_name": None,
        }
    for record in shared_records:
        concept_id = record["concept_id"]
        tokens[("shared_concept", concept_id)] = {
            "match_basis": f"shared_concept:{concept_id}", "measure_group": None,
            "shared_concept_id": concept_id, "shared_concept_name": record["name"],
        }
    return tokens


def _match_priority(match):
    if str(match.get("match_basis") or "").startswith("shared_concept_clause:"):
        return -1
    return {"measure_group": 0, "exact_measure": 1}.get(match.get("match_basis"), 2)


def _measure_match_score(match, left, right):
    """Score how directly both outcome.measure values identify one quantity."""
    basis = str(match.get("match_basis") or "")
    left_measure = _raw_measure_key((left.get("outcome") or {}).get("measure"))
    right_measure = _raw_measure_key((right.get("outcome") or {}).get("measure"))
    if basis == "exact_measure" or left_measure == right_measure:
        return 1.0
    if basis == "measure_group":
        return 0.9
    if basis.startswith("shared_concept:"):
        return 0.8
    if basis.startswith("shared_concept_clause:"):
        return 0.6
    return 0.0


def _compatible_readout(left, right, match):
    """Reject shared-entity pairs whose explicit readouts are incompatible."""
    left_outcome, right_outcome = left.get("outcome") or {}, right.get("outcome") or {}
    for field in ("readout", "measure_type", "endpoint", "metric"):
        first, second = left_outcome.get(field), right_outcome.get(field)
        if first not in (None, "") and second not in (None, "") and _norm(first) != _norm(second):
            return False
    return True


def _candidate_records(rows, groups, registry):
    """Build deterministic, deduplicated cross-paper candidate records."""
    token_rows = []
    entity_rows = {}
    records_by_id = {record.get("concept_id"): record for record in _registry_records(registry)}
    for row in rows:
        tokens = _match_tokens(row, groups, registry)
        token_rows.append((row, tokens))
        direct = {key[1]: match for key, match in tokens.items() if key[0] == "shared_concept"}
        clauses = _clause_matches(row, registry, groups)
        for concept_id, match in clauses.items():
            entity_rows.setdefault(concept_id, {"direct": [], "clause": []})["clause"].append((row, match))
        for concept_id, match in direct.items():
            entity_rows.setdefault(concept_id, {"direct": [], "clause": []})["direct"].append((row, match))
    by_token = {}
    for row, tokens in token_rows:
        for token in tokens:
            by_token.setdefault(token, []).append(row)
    pairs = {}
    for token in sorted(by_token):
        entries = sorted(by_token[token], key=lambda row: (row[0], row[1].get("unit_id", "")))
        for index, left in enumerate(entries):
            direction_left = (left[1].get("outcome") or {}).get("direction")
            for right in entries[index + 1:]:
                if left[0] == right[0]:
                    continue
                direction_right = (right[1].get("outcome") or {}).get("direction")
                # RL-02: mixed is not opposite to anything; a mixed unit pairs only through its clauses
                if direction_left == direction_right or "mixed" in (direction_left, direction_right):
                    continue
                unit_ids = tuple(sorted((left[1].get("unit_id", ""), right[1].get("unit_id", ""))))
                if not all(unit_ids):
                    continue
                match = dict(_match_tokens(left, groups, registry).get(token) or {})
                if not match:
                    continue
                if not _compatible_readout(left[1], right[1], match):
                    continue
                if not _same_agent(left, right):
                    continue
                compared = {left[1].get("unit_id"): (left[1].get("outcome") or {}).get("measure"),
                            right[1].get("unit_id"): (right[1].get("outcome") or {}).get("measure")}
                if not _same_outcome(match, compared.values(), records_by_id):
                    continue
                previous = pairs.get(unit_ids)
                if previous is None or _match_priority(match) < _match_priority(previous["match"]):
                    pairs[unit_ids] = {"left": left, "right": right, "match": match, "compared": compared}

    # Pair a specifically measured entity with an explicit clause in the other
    # unit. The clause side supplies the per-entity direction, not the aggregate
    # unit direction (which may be mixed).
    for concept_id in sorted(entity_rows):
        bucket = entity_rows[concept_id]
        for direct_row, direct_match in bucket["direct"]:
            direct_direction = (direct_row[1].get("outcome") or {}).get("direction")
            if direct_direction == "mixed":
                direct_clause = _clause_matches(direct_row, registry, groups).get(concept_id)
                direct_direction = (direct_clause or {}).get("entity_direction")
            if direct_direction not in MEASURE_DIRECTIONS[:-1]:
                continue
            for clause_row, clause_match in bucket["clause"]:
                if direct_row[0] == clause_row[0]:
                    continue
                if not _same_agent(direct_row, clause_row):
                    continue
                if not _compatible_readout(direct_row[1], clause_row[1], clause_match):
                    continue
                # the clause side names only the entity: its compared text is the concept name
                compared = {direct_row[1].get("unit_id"): (direct_row[1].get("outcome") or {}).get("measure"),
                            clause_row[1].get("unit_id"): clause_match["shared_concept_name"]}
                if not _same_outcome(clause_match, compared.values(), records_by_id):
                    continue
                clause_direction = clause_match.get("entity_direction")
                if clause_direction not in MEASURE_DIRECTIONS[:-1] or direct_row[1].get("unit_id") == clause_row[1].get("unit_id"):
                    continue
                unit_ids = tuple(sorted((direct_row[1].get("unit_id", ""), clause_row[1].get("unit_id", ""))))
                if not all(unit_ids) or direct_direction == clause_direction:
                    continue
                left, right = (direct_row, clause_row) if direct_row[1].get("unit_id") == unit_ids[0] else (clause_row, direct_row)
                directions = {direct_row[1].get("unit_id"): direct_direction,
                              clause_row[1].get("unit_id"): clause_direction}
                previous = pairs.get(unit_ids)
                candidate = {"left": left, "right": right, "match": dict(clause_match),
                             "directions": directions, "compared": compared}
                if previous is None or _match_priority(candidate["match"]) < _match_priority(previous["match"]):
                    pairs[unit_ids] = candidate
    return [pairs[key] for key in sorted(pairs)]


def _relation_type(direction_a, direction_b):
    if {direction_a, direction_b} <= EFFECT_DIRECTIONS and direction_a != direction_b:
        return "contradicts"
    if ((direction_a in EFFECT_DIRECTIONS and direction_b == "no_change") or
            (direction_b in EFFECT_DIRECTIONS and direction_a == "no_change")):
        return "effect_vs_no_effect"
    return "direction_differs"


def _effect_no_effect_difference(first, second):
    return {"field": "outcome.direction", "from": (first.get("outcome") or {}).get("direction"),
            "to": (second.get("outcome") or {}).get("direction"),
            "note": "Non-significance is not evidence of no effect; a recorded no_change outcome is not proof of no effect."}


HUMAN_RELATION_STATUSES = frozenset(("human_confirmed", "rejected", "human_rejected"))


def _relation_pair(record):
    return tuple(sorted((record.get("from_unit"), record.get("to_unit"))))


def _withdraw_by_rule(record, reason):
    """Retire one machine proposal without removing its audit record."""
    withdrawn_at = config.now()
    history = record.setdefault("history", [])
    if not isinstance(history, list):
        history = record["history"] = []
    history.append({"event": "withdrawn_by_rule", "from_status": record.get("status"),
                    "to_status": "withdrawn_by_rule", "reason": reason, "at": withdrawn_at})
    record["status"] = "withdrawn_by_rule"
    record["withdrawn_reason"] = reason
    record["withdrawn_at"] = withdrawn_at


def _stale_reason(relation, rows_by_unit, registry):
    """The first rule a stored machine proposal fails now, as the reason written on its withdrawal."""
    left, right = (rows_by_unit.get(unit) for unit in _relation_pair(relation))
    if not (left and right):
        return "unit is no longer an eligible result"
    if not _same_agent(left, right):
        first, second = _agent_of(left), _agent_of(right)
        return (f"RL-02: different intervention agents ({first!r} vs {second!r})" if first or second
                else "RL-02: no named agent and no shared intervention in the texts")
    if "mixed" in (relation.get("directions") or ()):
        return "RL-02: a mixed direction is not opposite"
    basis =str(relation.get("match_basis") or "")
    measures = [(row[1].get("outcome") or {}).get("measure") for row in (left, right)]
    if (basis == "exact_measure" or basis.startswith("shared_concept:")) and any(map(_is_bundled, measures)):
        return "RL-03: bundled measure paired as a whole"
    if basis == "exact_measure" and any(_is_organism_measure(measure, registry) for measure in measures):
        return "RL-02: a species name is not a measure"
    record = next((r for r in _registry_records(registry) if r.get("concept_id") == relation.get("shared_concept_id")), None)
    if basis.startswith("shared_concept") and record:
        # the side that holds the concept in its measure is the direct side; the other names it in a clause
        texts = [measure if list(_concept_spans(str(measure or ""), record)) else record["name"] for measure in measures]
        if not _same_outcome({"match_basis": basis, "shared_concept_id": record["concept_id"]}, texts,
                             {record["concept_id"]: record}):
            return "RL-02: different measured outcomes"
    return "no longer a candidate under RL-02/RL-03"


def _candidate_directions(candidate, first, second):
    directions = candidate.get("directions") or {}
    return (directions.get(first.get("unit_id"), (first.get("outcome") or {}).get("direction")),
            directions.get(second.get("unit_id"), (second.get("outcome") or {}).get("direction")))


def propose(con, vault=None):
    """Write deterministic, reviewable cross-paper relation proposals.

    Units are paired by a reviewed measure group, an exact eligible measure,
    or the most-specific non-stoplisted registry concept found in both
    ``outcome.measure`` values. Evidence and human decisions are untouched;
    stale shared-concept proposals are withdrawn with an audit reason.
    """
    rows = _unit_rows(vault)
    rows_by_unit = {row[1].get("unit_id"): row for row in rows}
    groups = load_measure_groups()
    registry = concepts.load_registry(con)
    candidates = _candidate_records(rows, groups, registry)
    existing = list(db.read_jsonl(RELATIONS) or [])
    by_id = {r.get("relation_id"): r for r in existing}
    # One row per pair, ranked human > active > withdrawn (a later-sorting withdrawn row must not hide a human decision).
    pair_index = {_relation_pair(r): r for r in sorted(existing, key=lambda r: (
        r.get("status") in HUMAN_RELATION_STATUSES, r.get("status") != "withdrawn_by_rule"))
                  if r.get("type") in CONFLICT_TYPES}

    # A pair can remain valid but fall outside the cap.  Only pairs absent
    # from the uncapped candidate set are stale under the measured-entity rule.
    candidate_pairs = {
        tuple(sorted((candidate["left"][1].get("unit_id"), candidate["right"][1].get("unit_id"))))
        for candidate in candidates
    }
    withdrawn = 0
    changed = False
    for relation in by_id.values():
        pair = _relation_pair(relation)
        if relation.get("status") in HUMAN_RELATION_STATUSES:
            continue
        # candidate_pairs only covers this vault: a relation of another vault is not stale, it is out of scope.
        if vault and not all(vault in _paper_vaults(_paper_of(unit)) for unit in pair):
            continue
        # RL-02/RL-03: a machine proposal whose pair no rule keeps any more is withdrawn (any match_basis).
        if (relation.get("status") == "ai_proposed" and relation.get("type") in CONFLICT_TYPES
                and pair not in candidate_pairs):
            _withdraw_by_rule(relation, _stale_reason(relation, rows_by_unit, registry))
            withdrawn += 1
            changed = True

    # Assign each pair to one deterministic measured-entity key before the
    # cap. This keeps a pair that matches both a free-text measure and a
    # registry concept from consuming two slots for the same proposal.
    by_concept = {}
    for candidate in candidates:
        match = candidate["match"]
        basis = match.get("match_basis") or "exact_measure"
        if basis == "measure_group":
            concept_key = f"measure_group:{match.get('measure_group')}"
        elif basis.startswith("shared_concept_clause:"):
            concept_key = basis
        elif basis.startswith("shared_concept:"):
            concept_key = basis
        else:
            measure = (candidate["left"][1].get("outcome") or {}).get("measure")
            concept_key = f"exact_measure:{_raw_measure_key(measure)}"
        by_concept.setdefault(concept_key, []).append(candidate)

    selected = []
    capped = []
    for concept_key in sorted(by_concept):
        concept_candidates = sorted(
            by_concept[concept_key],
            key=lambda item: tuple(sorted((item["left"][1].get("unit_id", ""),
                                           item["right"][1].get("unit_id", ""))))
        )
        selected.extend(concept_candidates[:MAX_PAIRS_PER_CONCEPT])
        if len(concept_candidates) > MAX_PAIRS_PER_CONCEPT:
            match = concept_candidates[0]["match"]
            capped.append({"concept": concept_key,
                           "name": match.get("shared_concept_name") or match.get("measure_group") or concept_key,
                           "limit": MAX_PAIRS_PER_CONCEPT,
                           "considered": len(concept_candidates),
                           "skipped": len(concept_candidates) - MAX_PAIRS_PER_CONCEPT})

    added = upgraded = revived = 0
    for candidate in selected:
        left, right = candidate["left"], candidate["right"]
        ua, ub = left[1], right[1]
        a_id, b_id = sorted((ua["unit_id"], ub["unit_id"]))
        first, second = (ua, ub) if ua["unit_id"] == a_id else (ub, ua)
        direction_a, direction_b = _candidate_directions(candidate, first, second)
        if direction_a not in MEASURE_DIRECTIONS[:-1] or direction_b not in MEASURE_DIRECTIONS[:-1]:
            continue
        relation_type = _relation_type(direction_a, direction_b)
        prior = pair_index.get((a_id, b_id))
        if prior and prior.get("status") == "withdrawn_by_rule":  # revive the row of this type, if there is one
            prior = by_id.get(_relation_id(a_id, b_id, relation_type), prior)
        if prior:
            # Human decisions are authoritative even if a later source update
            # would classify this pair differently. Never create a second
            # relation for the pair or downgrade its decision.
            if prior.get("status") in HUMAN_RELATION_STATUSES:
                continue

        strict = _comparison(left, right)
        # Opposite directions become a red contradiction only when both units name
        # the same agent (RL-02); two units without an agent stay direction_differs.
        if relation_type == "contradicts" and strict is None:
            relation_type = "direction_differs"
        differences = strict if strict is not None else _comparison(left, right, require_match=False)
        if relation_type == "effect_vs_no_effect":
            differences = list(differences or [])
            differences.append(_effect_no_effect_difference(first, second))
            rationale = ("One paper reports an increase/decrease while another records no_change for the same "
                         "measured entity. Non-significance is not evidence of no effect; review the source "
                         "statistics, model and protocol before interpreting this as agreement or contradiction.")
        elif relation_type == "contradicts":
            rationale = ("Opposite outcome directions for the same measured entity, with the same named agent. "
                         "Review dose, timing, model, protocol and technique differences.")
        else:
            rationale = ("Outcome directions differ for the same measured entity; comparability of intervention, "
                         "model, protocol and technique is NOT established. Shown side by side for review.")
        match = candidate["match"]
        record = {"relation_id": _relation_id(a_id, b_id, relation_type), "from_unit": a_id, "to_unit": b_id,
                  "type": relation_type, "rationale": rationale, "differences": differences,
                  "status": "ai_proposed", "proposed_by": "kb.py propose-relations",
                  "created_at": config.now(), "vault": vault or _shared_vault(a_id, b_id),  # a library-wide run must not write an invisible vault-less row
                  "outcome_measure": (first.get("outcome") or {}).get("measure"),
                  "outcome_measure_other": (second.get("outcome") or {}).get("measure"),
                  "measure_group": match.get("measure_group"),
                  "match_basis": match.get("match_basis"),
                  "shared_concept_id": match.get("shared_concept_id"),
                  "shared_concept_name": match.get("shared_concept_name"),
                  "directions": [direction_a, direction_b],
                  "compared_measures": [candidate["compared"][a_id], candidate["compared"][b_id]]}
        record["measure_score"] = _measure_match_score(match, first, second)
        if match.get("clause"):
            record["clause"] = match["clause"]
            record["match_clause"] = match["clause"]
            record["comparability"] = ("entity named in a statement clause; measure granularity differs — review")
        if prior and prior.get("type") == relation_type and prior.get("status") == "ai_proposed":
            # Preserve the original creation time and avoid rewriting an
            # unchanged JSONL file on an idempotent rerun.  This also refreshes
            # match_basis when a more-specific registry concept supersedes an
            # ancestor.
            record["created_at"] = prior.get("created_at", record["created_at"])
            record.update({key: prior[key] for key in ("history", "supersedes") if key in prior})  # audit trail kept
            if record != prior:
                by_id[record["relation_id"]] = record
                changed = True
            pair_index[(a_id, b_id)] = record
            continue
        if prior and prior.get("type") == relation_type and prior.get("status") == "withdrawn_by_rule":
            history = list(prior.get("history") or [])
            history.append({"event": "revived", "from_status": "withdrawn_by_rule",
                            "to_status": "ai_proposed", "withdrawn_reason": prior.get("withdrawn_reason"),
                            "withdrawn_at": prior.get("withdrawn_at"), "at": config.now()})
            record["history"] = history
            record["created_at"] = prior.get("created_at", record["created_at"])
            revived += 1
            changed = True
            by_id[record["relation_id"]] = record
            pair_index[(a_id, b_id)] = record
            continue
        if prior and prior.get("status") == "ai_proposed":
            _withdraw_by_rule(prior, "Relation classification changed under the current measured-entity rules.")
            withdrawn += 1
            changed = True
        if prior:
            record["supersedes"] = prior.get("relation_id")
            upgraded += 1
        else:
            added += 1
        by_id[record["relation_id"]] = record
        pair_index[(a_id, b_id)] = record
        changed = True
    if added or upgraded or changed:
        db.write_jsonl(RELATIONS, sorted(by_id.values(), key=lambda r: r.get("relation_id", "")))
    active = {t: sum(1 for r in by_id.values()
                     if r.get("type") == t and r.get("status") != "withdrawn_by_rule")
              for t in CONFLICT_TYPES}
    return {"units": len(rows), "added": added, "upgraded": upgraded, "total": len(by_id),
            "withdrawn": withdrawn, "revived": revived, "by_type": active,
            "capped": capped, "cap": MAX_PAIRS_PER_CONCEPT}


def candidate_pairs(rows=None, con=None):
    rows = rows if rows is not None else _unit_rows()
    groups = load_measure_groups()
    registry = concepts.load_registry(con)
    return [(candidate["left"], candidate["right"])
            for candidate in _candidate_records(rows, groups, registry)
            if _relation_type(*_candidate_directions(candidate, candidate["left"][1], candidate["right"][1])) == "contradicts"
            and _comparison(candidate["left"], candidate["right"]) is not None]
