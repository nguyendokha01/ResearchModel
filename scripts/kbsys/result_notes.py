"""Generated EVD, CLM and conflict notes for the typed research vault.

This module is deliberately source-bound.  Stored statements are displayed as
annotations, while the sentences copied into notes always come from
``sentences.jsonl``.  Crops are copied only after the media bundle's hash is
checked against the store file.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from . import concepts, config, db, media, presentation, relations as relations_mod, standard, structure, vault_layout

CONFLICT_TYPES = relations_mod.CONFLICT_TYPES
ACTIVE_RELATION_STATUSES = relations_mod.ACTIVE_RELATION_STATUSES
MAX_RESULTS = 15
MAX_CLAIMS = 5
MAX_CROP_BYTES = 3 * 1024 * 1024
PAPER_LEVEL = presentation.TEXT_FROM_PAPER  # species / stage taken from the paper's model, not from the unit itself
MISSING_ANNOTATIONS = {"not annotated", "intervention not annotated", "outcome measure not annotated"}


def _is_missing_annotation(value):
    return isinstance(value, str) and value.strip().casefold() in MISSING_ANNOTATIONS


def _property_value(value):
    return None if value in (None, "") or _is_missing_annotation(value) else value


def _sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def _paper_folder(paper_id):
    return db.paper_dir(paper_id)


def _read_units(paper_id):
    return list(db.read_jsonl(_paper_folder(paper_id) / "evidence.jsonl"))


def _read_sentences(paper_id):
    rows = list(db.read_jsonl(_paper_folder(paper_id) / "sentences.jsonl"))
    return {row.get("sentence_id"): row for row in rows if row.get("sentence_id")}


def _meta(paper_id):
    value = dict(db.read_json(_paper_folder(paper_id) / "metadata.json", {}) or {})
    value.setdefault("paper_id", paper_id)
    return value


def _citation(meta):
    return presentation.author_year(meta)


def _clean_title_part(value):
    return _short(value, 48).strip(" .;,:-_") if value not in (None, "") else ""


def _title_context(species, stage):
    """``Species/stage`` of one side for a title; the paper-level mark is dropped, an empty side gives ``""``."""
    return "/".join(dict.fromkeys(part for part in (_clean_title_part(str(value or "").replace(PAPER_LEVEL, ""))
                                                    for value in (species, stage)) if part))


def _conflict_disambiguators(relation, left, right):
    """Return human-readable content available to distinguish equal pair titles."""
    values = []
    left_measure, right_measure = _measure(left), _measure(right)
    if left_measure and right_measure and str(left_measure).casefold() != str(right_measure).casefold():
        values.append(f"{_clean_title_part(left_measure)} vs {_clean_title_part(right_measure)}")
    directions = relation.get("directions") or [_direction(left), _direction(right)]
    if len(directions) >= 2 and directions[0] and directions[1] and directions[0] != directions[1]:
        values.append(f"{_clean_title_part(directions[0])} vs {_clean_title_part(directions[1])}")
    sections = relation.get("sections") or relation.get("source_sections") or []
    if not sections:
        sections = [left.get("section"), right.get("section")]
    if len(sections) >= 2 and sections[0] and sections[1] and sections[0] != sections[1]:
        values.append(f"{_clean_title_part(sections[0])} vs {_clean_title_part(sections[1])}")
    fields = []
    for difference in relation.get("differences") or []:
        field = _clean_title_part(difference.get("field") or difference.get("from") or difference.get("to"))
        if field and field.casefold() not in {item.casefold() for item in fields}:
            fields.append(field)
    if fields:
        values.append(" / ".join(fields[:2]))
    return values


def _conflict_title(relation, left, right, left_meta, right_meta, merged=False, sides=None):
    """Stable, descriptive title; relation IDs remain the frontmatter identity.

    ``merged`` = one note for several relations of the same paper pair + measure, so the
    title cannot name one relation's direction. ``sides`` = the note's A/B sides (``_side``), so the
    title shows exactly the table's species/stage; without them the same chain (``_side_model``) is run."""
    left_measure, right_measure = _measure(left), _measure(right)
    shared = relation.get("shared_concept_name") or relation.get("measure_group")
    if shared:
        measure = shared
    elif left_measure and right_measure and str(left_measure).casefold() != str(right_measure).casefold():
        measure = f"{left_measure} vs {right_measure}"
    else:
        measure = left_measure or right_measure or "result"
    directions = relation.get("directions") or []
    da = directions[0] if len(directions) > 0 and directions[0] else _direction(left)
    db = directions[1] if len(directions) > 1 and directions[1] else _direction(right)
    models = ([(side.get("species"), side.get("stage")) for side in sides] if sides else
              [_side_model(unit, meta) for unit, meta in ((left, left_meta), (right, right_meta))])
    left_context, right_context = (_title_context(*model) for model in models)
    # Never borrow: a side that states nothing leaves the pair without a context.
    context = ""
    if left_context and right_context:
        context = left_context if left_context == right_context else f"{left_context} vs {right_context}"
    parts = [_short(measure, 72)]
    if context:
        parts.append(context)
    parts.append(f"{_citation(left_meta)} vs {_citation(right_meta)}")
    # Directions are part of the content identity when they differ, preventing
    # otherwise identical H1 titles without exposing the opaque relation ID.
    if merged:
        return " - ".join(_short(part, 90) for part in parts)
    if da and db and da != db:
        parts.append(f"({da} vs {db})")
    disambiguators = _conflict_disambiguators(relation, left, right)
    if disambiguators and not (da and db and da != db):
        parts.append(f"[{disambiguators[0]}]")
    return " - ".join(_short(part, 90) for part in parts)


def _about_links(statement, vault):
    """Link existing node notes whose canonical names occur in a claim statement."""
    root = config.vault_dir(vault)
    candidates = []
    from . import notes
    paths = [path for folder in notes.NODE_FOLDERS for path in (root / folder).glob("*.md")]
    for path in paths:
        name = path.stem
        if name.casefold() in {"index", "home", "log", "open conflicts"}:
            continue
        pattern = r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])"
        if re.search(pattern, str(statement or ""), re.I):
            candidates.append((len(name), name.casefold(), name))
    return [f"[[{name}]]" for _, _, name in sorted(candidates, key=lambda row: (-row[0], row[1]))[:8]]


# Words a title must not end on (it would read as cut mid-clause).
_DANGLING = frozenset("a an the of in on at to for by from with without and or but nor than that which whose while "
                      "as is are was were be been being not no vs versus between among after before during "
                      "under over per via both either".split())


def _short(text, limit):
    """Title of at most ~``limit`` characters that ends at a clause or word boundary; ``…`` only when cut."""
    value = presentation.clean_pdf_text(" ".join(str(text or "").split()))
    if len(value) <= limit:
        return value or "unlabelled finding"
    head = value[:limit + 1]
    clause = [m.start() for m in re.finditer(r"[,;:](?=\s)|[.](?=\s)", head) if m.start() >= limit * 0.5]
    cut = head[:clause[-1]] if clause else head[:limit].rsplit(" ", 1)[0] if " " in head[:limit] else head[:limit]
    if cut.count("(") > cut.count(")"):  # never leave a bracket open
        cut = cut[:cut.rindex("(")] if cut.rindex("(") > limit * 0.3 else cut.replace("(", "")
    words = cut.rstrip(" ,;:-–—").split(" ")
    if not clause:  # a word cut: back up to the last phrase boundary (a function word) within the final three words
        for back in (3, 2):
            if len(words) > back + 2 and words[-back].casefold() in _DANGLING:
                del words[-back:]
                break
    while len(words) > 1 and words[-1].casefold().strip(",;:") in _DANGLING:
        words.pop()
    return " ".join(words).rstrip(" ,;:-–—(") + "…"


def _paper_link(meta):
    return f"[[{meta.get('note_name') or meta['paper_id']}]]"


def _node_link(value, vault):
    """Link a node only when its note exists in this vault."""
    if value in (None, "") or _is_missing_annotation(value):
        return ""
    text = str(value)
    if text.startswith("[[") and text.endswith("]]"):
        return text
    from . import notes
    candidate = notes.safe_filename(text)
    vdir = config.vault_dir(vault)
    # A link must use the exact casing of the existing note title (Obsidian resolves case-insensitively,
    # but exports and the selftest compare exactly): return the stem on disk, not the spelling in the source.
    stems = sorted({path.stem for path in vdir.rglob("*.md")
                    if "_Attachments" not in path.parts and path.stem.casefold() == candidate.casefold()},
                   key=lambda x: (x != candidate, x))  # an exact-case title wins over a case-only variant
    stem = stems[0] if stems else None
    return f"[[{stem}]]" if stem else text


def _list(value):
    if value in (None, ""):
        return []
    if isinstance(value, (list, tuple, set)):
        return [x for x in value if x not in (None, "") and not _is_missing_annotation(x)]
    return [] if _is_missing_annotation(value) else [value]


def _sentence_ids(unit):
    return [str(sid) for sid in _list(unit.get("sentence_ids"))]


def _source_rows(unit, sentences):
    from . import node_profile
    wanted, _ = node_profile._source_ids(_sentence_ids(unit), sentences,
                                         paper_id=str(unit.get("paper_id") or unit.get("unit_id") or "").rsplit(":", 1)[0])
    rows = [sentences[sid] for sid in wanted if sid in sentences]
    return sorted(rows, key=lambda row: (row.get("seq", 0), row.get("sentence_id", "")))


def _source_text(unit, paper_id=None):
    """Read the copied source text used for display-derived presentation fields."""
    pid = paper_id or unit.get("paper_id") or str(unit.get("unit_id") or "").rsplit(":", 1)[0]
    return " ".join(str(row.get("text") or "") for row in _source_rows(unit, _read_sentences(pid)))


NOT_SIGNIFICANT = {"ns", "non-significant", "not significant", "non_significant"}


def is_not_significant(value):
    """True for a significance value that means 'not significant' (also read by the vault index, neural._edge_rows)."""
    return str(value).strip().casefold() in NOT_SIGNIFICANT


def _significance(unit, meta=None):
    outcome = unit.get("outcome") or {}
    explicit = outcome.get("significance") or unit.get("significance")
    source = _source_text(unit, (meta or {}).get("paper_id"))
    # Source wording wins over stale/default annotations such as not_tested.
    if re.search(r"\b(?:not|no)\s+(?:statistically\s+)?significant(?:ly)?\b|\bdid\s+not\s+(?:significantly\s+)?differ\b", source, re.I):
        return "ns"
    if re.search(r"\b(?:not\s+tested|no\s+statistical\s+test|not\s+statistically\s+tested)\b", source, re.I):
        return "not_tested"
    if explicit and str(explicit).casefold() not in {"not_tested", "not_reported", ""}:
        return explicit
    p_values = re.findall(r"\bp\s*([<=>≤≥])\s*(\d*\.?\d+(?:[eE][-+]?\d+)?)", source, re.I)
    if p_values:
        if any(op in ("<", "≤", "=") and float(value) <= 0.05 for op, value in p_values):
            return "significant"
        if any(op in (">", "≥", "=") and float(value) >= 0.05 for op, value in p_values):
            return "ns"
    if "***" in source:
        return "significant"
    if re.search(r"\bsignificant(?:ly)?\b", source, re.I):
        return "significant"
    return "not_reported"


def _is_association(unit, source_text=None):
    text = str(unit.get("statement") or "")
    text += " " + str(source_text or "")
    return bool(re.search(r"\bassociat(?:e|ed|es|ing|ion|ions)\b|\bcorrelat\w*\b",
                          text, re.I))


def _hedge_marker(text):
    hits = structure.uncertainty(str(text or ""))
    if not hits:
        return ""
    values = "/".join(dict.fromkeys(str(row.get("text")) for row in hits))
    return f"⚠️ Hedged wording: {values}"


def _row_cite(row, meta, link=True):
    """``[[Paper note|Author Year, p. N]]`` for one copied sentence row; its ID goes to the Source IDs callout."""
    sid = str(row.get("sentence_id") or "")
    return presentation.cite(meta, row.get("page"), [sid], sid.rsplit(":", 1)[0] if ":" in sid else meta.get("paper_id"), link)


def _quote_lines(unit, sentences, meta=None):
    rows = _source_rows(unit, sentences)
    if not rows:
        return ["> Source sentence unavailable in the current store."]
    meta = meta or _meta(str(unit.get("paper_id") or str(unit.get("unit_id") or "").rsplit(":", 1)[0]))
    out, previous = [], None
    presentation.note_source_ids(meta.get("paper_id"), [unit.get("unit_id")],
                                 presentation.cite_label(meta, rows[0].get("page")))
    for row in rows:
        cite = _row_cite(row, meta)
        if cite != previous:  # one citation line per page run, not one per sentence
            out.append(f"> **{cite}**")
        previous = cite
        text = str(row.get("text") or "")
        out.extend("> " + line if line else ">" for line in text.splitlines())
    return out


def _direction(unit):
    outcome = unit.get("outcome") or {}
    value = str(outcome.get("direction") or unit.get("direction") or "").casefold()
    if value in {"increase", "increases", "increased", "higher", "up"}:
        return "increases"
    if value in {"decrease", "decreases", "decreased", "lower", "down"}:
        return "decreases"
    if value in {"no_change", "no significant change", "no_significant_change", "unchanged", "none"}:
        return "no_significant_change"
    if value in {"associated", "association", "associated_with", "correlation", "correlated"}:
        return "associated_with"
    if value == "mixed":
        return "mixed"
    return "associated_with" if unit.get("knowledge_level") == "statistical_association" else "mixed"


def _measure(unit):
    return (unit.get("outcome") or {}).get("measure") or unit.get("measure") or None


def _has_measure(unit):
    """RL-10 A3: the unit states an outcome measure (not empty, blank or a missing-annotation marker)."""
    return bool(str(_property_value(_measure(unit)) or "").strip())


def _subject(unit):
    intervention = unit.get("intervention") or {}
    return intervention.get("agent") or intervention.get("name") or None


def _predicate(unit, source_text=None):
    return "associated_with" if _is_association(unit, source_text) else _direction(unit)


def _figure_ids(unit, paper_id):
    outcome = unit.get("outcome") or {}
    values = list(_list(unit.get("figure_ids"))) + list(_list(outcome.get("source_objects")))
    out = []
    for value in values:
        text = str(value)
        if ":" not in text and paper_id:
            text = f"{paper_id}:{text}"
        if text not in out:
            out.append(text)
    return out


def _unit_is_primary(meta):
    from . import find
    return find.effective_evidence_type(meta) not in find.REVIEW_TYPES


def _summary_units(analysis, units):
    by_seq = {u.get("seq"): u for u in units}
    by_id = {u.get("unit_id"): u for u in units}
    selected = set()
    for row in (analysis or {}).get("summary") or []:
        refs = row.get("units") if isinstance(row, dict) else []
        for ref in refs or []:
            unit = by_id.get(ref) or by_seq.get(ref) or by_seq.get(int(ref) if str(ref).isdigit() else None)
            if unit:
                selected.add(unit.get("unit_id"))
    return selected


def _relation_scope(rel, vault, paper_ids):
    if rel.get("vault") and rel.get("vault") != vault:
        return False
    endpoints = [str(rel.get(key) or "").rsplit(":", 1)[0] for key in ("from_unit", "to_unit")]
    return all(endpoint in paper_ids for endpoint in endpoints)


def _relations(vault, paper_ids):
    # Active + reviewed history only; withdrawn_by_rule rows never reach the vault.
    return sorted((row for row in relations_mod.conflict_relations(vault, history=True)
                   if _relation_scope(row, vault, paper_ids)),
                  key=lambda row: str(row.get("relation_id") or ""))


def _main_units(paper_id, meta, units, analysis, relations, bundles):
    if not _unit_is_primary(meta):
        return []
    summary = _summary_units(analysis, units)
    related = {rel.get(key) for rel in relations for key in ("from_unit", "to_unit")}
    figure_linked = {u.get("unit_id") for u in units
                     if set(_figure_ids(u, paper_id)) or
                     any(u.get("unit_id") == x.get("unit_id") for b in bundles for x in b.get("associated_units", []))}
    # RL-10: a result without an outcome measure gets no EVD note (a summary, relation or figure link does not change that)
    candidates = [u for u in units if u.get("unit_type") == "result" and _has_measure(u)]
    candidates = [u for u in candidates if u.get("unit_id") in summary or
                  u.get("unit_id") in related or u.get("unit_id") in figure_linked or
                  (u.get("outcome") or {}).get("direction") not in (None, "")]
    candidates.sort(key=lambda u: (0 if u.get("unit_id") in summary else 1,
                                  0 if u.get("unit_id") in related else 1,
                                  0 if u.get("unit_id") in figure_linked else 1,
                                  u.get("seq", 0), u.get("unit_id", "")))
    return candidates[:MAX_RESULTS]


def _media_filename(object_id, notes):
    token = str(object_id).rsplit(":", 1)[-1]
    return notes.safe_filename(token, 120) + ".png"


def copy_crops(vault, paper_id, bundles):
    """Copy verified crops and return object_id -> vault/store details."""
    from . import notes
    vdir = config.vault_dir(vault)
    attachment_dir = vdir / "_Attachments" / paper_id
    entries = []
    names = {}
    raw_names = [_media_filename(bundle.get("object_id"), notes) for bundle in bundles]
    for index, bundle in enumerate(bundles):
        oid = bundle.get("object_id")
        crop = bundle.get("crop") or {}
        loc = bundle.get("source_locator") or {}
        source = None
        actual_hash = None
        if crop.get("path"):
            try:
                source = media.crop_path(_paper_folder(paper_id), crop["path"])
                if source.is_file():
                    actual_hash = _sha(source)
            except (OSError, ValueError):
                source = None
        verified = bool(source and actual_hash and actual_hash == crop.get("sha256") and
                        crop.get("status") in ("available", "retained"))
        filename = raw_names[index] if raw_names.count(raw_names[index]) == 1 else notes.safe_filename(str(oid), 120) + ".png"
        target = attachment_dir / filename if oid else None
        copied = False
        mode = "unavailable"
        if verified and target is not None:
            if source.stat().st_size > MAX_CROP_BYTES:
                mode = "store_only"
            else:
                mode = "copied"
                if not target.is_file() or _sha(target) != actual_hash:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)
                    copied = True
                names[str(oid)] = target
        entries.append({"object_id": oid, "sha256": crop.get("sha256") if verified else None,
                        "source_page": loc.get("page"), "source": loc.get("source"),
                        "source_path": str(source) if source else None,
                        "vault_path": str(target.relative_to(vdir)).replace("\\", "/") if mode == "copied" else None,
                        "store_path": str(source) if source and source.is_file() else None,
                        "size": source.stat().st_size if source and source.is_file() else None,
                        "status": mode})
    manifest_path = attachment_dir / "manifest.json"
    payload = {"version": 1, "paper_id": paper_id, "objects": entries}
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if not manifest_path.is_file() or manifest_path.read_text(encoding="utf-8") != encoded:
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(encoded, encoding="utf-8", newline="\n")
    return {"entries": entries, "paths": names, "manifest": manifest_path}


def _load_figure_reading(paper_id, object_id):
    try:
        from . import figure_reading
    except ImportError:
        return None
    try:
        return figure_reading.load(paper_id, object_id)
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        return None


def _reading_text(reading):
    if not reading:
        return ""
    if isinstance(reading, str):
        return reading.strip()
    for key in ("summary", "reading", "text", "interpretation"):
        value = reading.get(key) if isinstance(reading, dict) else None
        if value:
            if isinstance(value, dict):
                value = value.get("text") or value.get("summary") or value.get("value")
            if value:
                return str(value).strip()
    return ""


def _caption(bundle):
    return " ".join(str(row.get("text")) for row in bundle.get("caption") or []
                    if row.get("text") not in (None, "")).strip()


def _unique_bundles(bundles):
    """Keep one deterministic media entry per object id in a rendered note."""
    seen, out = set(), []
    for bundle in bundles:
        oid = bundle.get("object_id")
        key = str(oid) if oid not in (None, "") else f"missing:{len(out)}"
        if key in seen:
            continue
        seen.add(key)
        out.append(bundle)
    return out


def _location_text(kind, locator):
    parts = []
    if kind not in (None, ""):
        parts.append(str(kind))
    if locator.get("source") not in (None, ""):
        parts.append(str(locator["source"]))
    if locator.get("page") is not None:
        parts.append(f"p. {locator['page']}")
    return " · ".join(parts)


def _is_page_preview(locator):
    return locator.get("region_kind") in {"source_page_preview", "page_preview"}


def _embed(entry, paper_id):
    if entry.get("status") == "copied" and entry.get("vault_path"):
        return f"![[_Attachments/{paper_id}/{Path(entry['vault_path']).name}|640]]"
    if entry.get("status") == "store_only" and entry.get("store_path"):
        return f"[Open source crop in store]({Path(entry['store_path']).resolve().as_uri()})"
    return ""


def _formula_lines(bundle, entry, paper_id):
    caption = _caption(bundle)
    embed = _embed(entry, paper_id)
    locator = bundle.get("source_locator") or {}
    page = f" (p. {locator['page']})" if locator.get("page") is not None else ""
    out = [f"- Formula{page} — {caption}" if caption else f"- Formula{page} — không có caption"]
    if embed:
        out.append(embed)
    return out


def render_figures_and_tables(meta, vault=None):
    """Render the paper's media section and copy attachments when a vault is given."""
    from . import notes
    paper_id = meta["paper_id"]
    bundles = _unique_bundles(list(media.bundles(paper_id)))
    copied = copy_crops(vault, paper_id, bundles) if vault else {"entries": [], "paths": {}}
    entry_by_id = {str(row.get("object_id")): row for row in copied["entries"]}
    out = ["", f"## {notes.PAPER_FIGURES_HEADING}", ""]
    if not bundles:
        return out + ["No source-linked media inventory available; verify the original source."]
    formula_bundles = [bundle for bundle in bundles if bundle.get("kind") == "formula"]
    ordinary_bundles = [bundle for bundle in bundles if bundle.get("kind") != "formula"]
    for bundle in ordinary_bundles:
        oid = str(bundle.get("object_id") or "media")
        loc = bundle.get("source_locator") or {}
        entry = entry_by_id.get(oid) or {}
        out.append("### " + presentation.object_title(bundle.get("kind"), oid, bundle.get("label"), _caption(bundle),
                                                     None, loc.get("page")))
        location = _location_text(bundle.get("kind"), loc)
        if location:
            out.append(location)
        if _is_page_preview(loc):
            out.append("Region completeness: not assessed. A source-page preview is not a located scientific graphic.")
        embed = _embed(entry, paper_id)
        out.append(embed or "Source region unavailable/unverified; inspect the original PDF.")
        caption = _caption(bundle)
        if caption:
            out.append("> [!figure] Caption (verbatim)")
            out.extend("> " + line if line else ">" for line in caption.splitlines())
        mention_rows = [] if bundle.get("kind") == "table" else list(bundle.get("mentions") or []) + list(bundle.get("data") or [])
        if mention_rows:
            out += ["", "**Mention/data sentences**"]
            for row in mention_rows:
                text = str(row.get("text") or "")
                if row.get("page") is not None:  # context sentences: page only (their IDs stay in sentences.jsonl)
                    out.append("- " + presentation.cite_label(meta, row["page"], page_only=True))
                if text:
                    out.extend("> " + line if line else ">" for line in text.splitlines())
        loaded = _load_figure_reading(paper_id, oid)
        reading = _reading_text(loaded)
        if reading and bundle.get("kind") != "table":
            out += ["", "**AI image reading**", reading]
        from . import figure_reading
        flag = figure_reading.warning(loaded)
        if flag:  # researcher 13/10: keep the object, label the mismatch everywhere
            out += ["", f"> [!warning] {flag}"]
        if loc.get("source") == "main":
            source_pdf = next((row.get("path") for row in meta.get("source_files") or []
                              if row.get("role") == "main" and row.get("path")), None)
            if source_pdf and loc.get("page") is not None:
                path = Path(source_pdf)
                if not path.is_absolute():
                    path = config.ROOT / path
                out.append(f"[Open source page]({path.resolve().as_uri()}#page={loc['page']})")
        out.append("")
    if formula_bundles:
        out += [f"### Formulas ({len(formula_bundles)})", ""]
        for bundle in formula_bundles:
            oid = str(bundle.get("object_id"))
            out += _formula_lines(bundle, entry_by_id.get(oid) or {}, paper_id)
        out.append("")
    return out


def _data_rows(unit, figure_only=False):
    if figure_only:
        return []
    outcome = unit.get("outcome") or {}
    values = outcome.get("values")
    if isinstance(values, dict) and isinstance(values.get("groups"), list):
        values = values["groups"]
    if not isinstance(values, list):
        values = [values] if values not in (None, "") else []
    rows = []
    for value in values:
        if isinstance(value, dict):
            group = value.get("group") or value.get("group_name") or value.get("name") or ""
            val = value.get("value", value.get("mean", value.get("values", "")))
            sd = value.get("sd", value.get("se", ""))
            n = value.get("n", "")
            p = value.get("p", "")
            rows.append([group, val, sd, n, p])
        else:
            rows.append(["", value, "", "", ""])
    return [row for row in rows if str(row[0]).strip() not in {"", "—", "-", "–"}]


def _markdown_cell(value):
    if value in (None, "") or _is_missing_annotation(value):
        return ""
    return str(value).replace("not annotated", "").replace("|", "\\|").replace("\n", " ").strip()


def _statistics(unit):
    """Merge supported statistical shapes without inventing absent values."""
    outcome = unit.get("outcome") or {}
    candidates = [unit.get("statistics"), unit.get("stats"), outcome.get("statistics"),
                  outcome.get("stats")]
    for container in (unit.get("result_group"), outcome.get("result_group")):
        if isinstance(container, dict):
            candidates.extend((container.get("statistics"), container.get("stats"), container))
    merged = {}
    for candidate in candidates:
        if isinstance(candidate, dict):
            merged.update({key: value for key, value in candidate.items() if value not in (None, "")})
    return merged


def _stat_value(unit, *keys):
    outcome = unit.get("outcome") or {}
    stats = _statistics(unit)
    for source in (unit, outcome, stats):
        for key in keys:
            value = _property_value(source.get(key)) if isinstance(source, dict) else None
            if value is not None:
                return value
    return None


_P_RE = re.compile(r"\bP(?:\s*[-‑]?\s*values?)?\s*[<=>≤≥]\s*\d*\.?\d+(?:\s*(?:[×x]\s*10\s*[-−–‑^]\s*\d+|[eE][-+]?\d+))?", re.I)
_N_RE = re.compile(r"\bn\s*=\s*\d+", re.I)
_TEST_RE = re.compile(r"\b(?:(?:Student[’']?s\s+|paired\s+|unpaired\s+|two-tailed\s+|one-tailed\s+)*t[-‑ ]?tests?"
                      r"|(?:one|two|three)[-‑ ]way\s+(?:repeated[-‑ ]measures\s+)?ANOVA|ANOVA|Mann[-‑–]Whitney(?:\s+U)?(?:\s+test)?"
                      r"|Kruskal[-‑–]Wallis(?:\s+test)?|Wilcoxon(?:\s+(?:rank[-‑ ]sum|signed[-‑ ]rank))?(?:\s+test)?"
                      r"|Fisher[’']?s\s+exact(?:\s+test)?|chi[-‑ ]?squared?(?:\s+test)?|Tukey(?:[’']?s)?(?:\s+(?:HSD|post[-‑ ]hoc))?(?:\s+test)?"
                      r"|(?:Pearson|Spearman)(?:[’']s)?\s+(?:rank\s+)?(?:correlation|test|coefficient))\b|[χΧ]\s*[2²](?:\s+test)?", re.I)


def _source_stat(unit, kind, paper_id=None):
    """Statistic written literally in the cited source sentences (kind: p/n/test); never computed."""
    pattern = {"p": _P_RE, "n": _N_RE, "test": _TEST_RE}[kind]
    found = list(dict.fromkeys(" ".join(m.group(0).split()) for m in pattern.finditer(_source_text(unit, paper_id))))
    return "; ".join(found[:3]) or None


def _stat_or_source(unit, kind, aliases, paper_id=None):
    """The unit's own annotated statistic wins; otherwise the literal text of its cited source sentences."""
    value = _stat_value(unit, *aliases)
    return value if value is not None else _source_stat(unit, kind, paper_id)


def _model_values(unit):
    model = unit.get("model") or {}
    species = _list(model.get("species") or model.get("taxon") or unit.get("species"))
    stage = _list(model.get("stage") or model.get("developmental_stage") or unit.get("stage"))
    return species, stage


def _real_link(text):
    return text.startswith("[[") and text.endswith("]]")


def _measure_links(measure, vault, registry=None):
    """RL-10 A4: the nodes a measure phrase names. The whole phrase when it is a node; else every registry concept found in
    it (most specific, species / methods / generic terms left to their own properties); else the phrase as text."""
    text = str(_property_value(measure) or "").strip()
    if not text:
        return []
    whole = _node_link(text, vault)
    if _real_link(whole):
        return [whole]
    generic = concepts.generic_keys()
    found = [r for r in relations_mod._registry_records(registry)  # not _shared_concepts: its stoplist drops embryo, histone, DNA
             if r.get("concept_id") and r.get("name") and not relations_mod._is_organism_concept(r)
             and not relations_mod._is_method_concept(r)
             and (r.get("status") == "human_confirmed" or concepts._key(r["name"]) not in generic)  # ND-04: a confirmed node stays
             and presentation.matches_term(text, r["name"], r.get("synonyms") or [])]
    kept = relations_mod._drop_registry_ancestors(found)
    spans = [[m.span() for m in relations_mod._concept_spans(text, r)] for r in kept]
    # most specific by text too (most concepts have no parents): a name found only inside a longer match is dropped
    # ("Pregnancy" in "Clinical pregnancy", "Muse cell" in "non-Muse cell")
    kept = [r for i, r in enumerate(kept) if not spans[i] or not all(
        any(j != i and a <= s and e <= b and b - a > e - s for j, other in enumerate(spans) for a, b in other)
        for s, e in spans[i])]
    links = {}
    for record in sorted(kept, key=lambda r: (
            min((m.start() for m in relations_mod._concept_spans(text, r)), default=len(text)), r["name"])):
        link = _node_link(record["name"], vault)
        if _real_link(link):
            links.setdefault(link.casefold(), link)
    return list(links.values()) or [text]


_SUBJECT_SPLIT = re.compile(r"\s+and\s+|\s*[,/&]\s*|\s+\+\s+", re.I)


def _subject_links(value, vault):
    """RL-10 A5: a compound subject text becomes one link per part when every part is a node; otherwise it stays whole."""
    parts = [p.strip() for p in _SUBJECT_SPLIT.split(value) if p.strip()] if not _real_link(value) else []
    links = [_node_link(p, vault) for p in parts] if len(parts) > 1 else []
    if links and all(_real_link(link) for link in links):
        return list(dict.fromkeys(links))
    return [value]


def _evidence_frontmatter(unit, meta, vault, name, conflict_links=(), registry=None):
    outcome = unit.get("outcome") or {}
    model = unit.get("model") or {}
    intervention = unit.get("intervention") or {}
    figure_ids = _figure_ids(unit, meta["paper_id"])
    figure_only = outcome.get("value_source") == "figure_only"
    species, stage = _model_values(unit)
    protocol = _list(intervention.get("protocol") or unit.get("uses_protocol"))
    technique = _list(outcome.get("method") or unit.get("uses_technique"))
    measure = _measure(unit)
    source_text = _source_text(unit, meta.get("paper_id"))
    predicate = _predicate(unit, source_text)
    if predicate == "mixed" and str(outcome.get("direction") or unit.get("direction") or "").casefold() != "mixed":
        predicate = "reports"  # RL-10: no stated direction (or not_applicable / unknown) is "reports"; a stated `mixed` stays
    # RL-10: the stated subject, else the unit's own cell type, else the "not stated" text; never read from the statement
    stated = unit.get("subject") or outcome.get("subject") or _subject(unit)
    stated_link = _node_link(stated, vault)  # "" when nothing (or only a missing-annotation marker) is stated
    subject = (stated_link or _node_link(model.get("cell_type"), vault)
               or standard.param("RL-10", "subject_not_stated"))
    values = {
        "type": "result", "id": unit.get("unit_id"), "source": _paper_link(meta),
        "subject": _subject_links(subject, vault),
        "predicate": predicate or None,
        "object": _measure_links(measure, vault, registry),
        # the phrases as written (a combined agent reads as one agent, not as two separate effects)
        **({"measure": str(measure)} if _has_measure(unit) else {}),
        **({"agent": str(stated)} if stated_link and not _real_link(str(stated)) else {}),
        "negated": bool(unit.get("negated") or outcome.get("negated", False)),
        "knowledge_level": ("statistical_association" if unit.get("knowledge_level") == "statistical_association"
                             or predicate == "associated_with" else "observation"),
        "significance": _significance(unit, meta),
        "in_taxon": [_node_link(value, vault) for value in species], "stage": stage,
        "uses_protocol": [_node_link(value, vault) for value in protocol],
        "uses_technique": [_node_link(value, vault) for value in technique],
        "figure_ids": figure_ids,
        "sentence_ids": _sentence_ids(unit), "conflicts_with": list(conflict_links),
        "has_conflict": bool(conflict_links), "agent_type": "automated", "status": "machine",
        "title": name, **({"tags": ["conflict"]} if conflict_links else {}),
    }
    if not figure_only:
        for key, aliases in (("n", ("n", "sample_size")),
                             ("p_value", ("p", "p_value")),
                             ("statistical_test", ("test", "statistical_test"))):
            value = _stat_or_source(unit, {"n": "n", "p_value": "p", "statistical_test": "test"}[key], aliases,
                                    meta.get("paper_id"))
            if value is not None:
                values[key] = value
        if "p_value" in values:
            values["p"] = values["p_value"]
        if "statistical_test" in values:
            values["test"] = values["statistical_test"]
    return values


def _with_source_ids(build):
    """Append the one collapsed Source IDs callout with every ID the wrapped body builder cited."""
    @functools.wraps(build)
    def wrapper(*args, **kwargs):
        with presentation.collect_source_ids() as source_ids:
            text = build(*args, **kwargs)
        callout = presentation.source_ids_callout(source_ids)
        return text.rstrip("\n") + "\n" + "\n".join(callout) + "\n" if callout else text
    return wrapper


@_with_source_ids
def _result_body(unit, meta, vault, media_by_id, claim_links=(), conflict_links=()):
    """EVD note: Finding · Context · Data · Source sentences · Related claims / conflicts (empty ones are not written)."""
    outcome = unit.get("outcome") or {}
    intervention = unit.get("intervention") or {}
    figure_only = outcome.get("value_source") == "figure_only"
    pid = meta.get("paper_id")
    out = []
    if conflict_links:
        out += ["> [!conflict] Conflicting findings exist for this result (see Related claims / conflicts).", ""]
    statement = " ".join(str(unit.get("statement") or "").split())
    if statement:
        out += ["## Finding", "", statement, ""]
    species, stage = _model_values(unit)
    context = [("Species", ", ".join(map(str, species))), ("Stage", ", ".join(map(str, stage))),
               ("Method", ", ".join(map(str, _list(outcome.get("method"))))),
               ("Protocol", ", ".join(map(str, _list(intervention.get("protocol"))))),
               ("Groups", ", ".join(map(str, _list(outcome.get("groups")))))]
    context = [f"- {label}: {value}" for label, value in context if value]
    if context:
        out += ["## Context", "", *context, ""]
    data = []
    rows = _data_rows(unit, figure_only)
    if rows:
        data += ["| Group | Value | SD/SE | n | p |", "|---|---|---|---|---|"]
        data += ["| " + " | ".join(_markdown_cell(value) for value in row) + " |" for row in rows]
    stats = []
    stat_n = None if figure_only else _stat_or_source(unit, "n", ("n", "sample_size"), pid)
    stat_p = None if figure_only else _stat_or_source(unit, "p", ("p", "p_value"), pid)
    stat_test = None if figure_only else _stat_or_source(unit, "test", ("test", "statistical_test"), pid)
    significance = _significance(unit, meta)
    if figure_only:
        stats.append("- n, p: only in the figure; read them from the image (values are not copied here).")
    for label, value in (("n", stat_n), ("p", stat_p), ("Test", stat_test)):
        if value is not None:
            stats.append(f"- {label}: {value}")
    if significance not in (None, "", "not_reported", "not_tested"):
        stats.append(f"- Significance: {significance}")
        if is_not_significant(significance):
            stats.append("- Not statistically significant is not the same as no effect.")
    if data and stats:
        data.append("")
    data += stats
    figures = []
    for oid in _figure_ids(unit, meta["paper_id"]):
        bundle = media_by_id.get(oid) or media_by_id.get(oid.rsplit(":", 1)[-1])
        if not bundle:
            continue
        entry = bundle.get("vault_entry") or {}
        locator = bundle.get("source_locator") or {}
        if bundle.get("kind") == "formula":
            figures += _formula_lines(bundle, entry, meta["paper_id"])
            continue
        image = _embed(entry, meta["paper_id"])
        if image:
            figures.append(image)
        caption = _caption(bundle)
        if caption:
            figures.append(f"> [!figure] {caption}")
        if _is_page_preview(locator):
            figures.append("Region completeness: not assessed. A source-page preview is not a located scientific graphic.")
    if figures:
        data += [""] * bool(data) + figures
    if data:
        out += ["## Data", "", *data, ""]
    source_text = _source_text(unit, pid)
    out += ["## Source sentences", ""]
    hedge = _hedge_marker(source_text)
    if hedge:
        out.append(f"> {hedge}")
    out += _quote_lines(unit, _read_sentences(meta["paper_id"]), meta)
    if claim_links:
        out += ["", "## Related claims / conflicts", "", "- Claims: " + " · ".join(claim_links)]
    if conflict_links:
        if not claim_links:
            out += ["", "## Related claims / conflicts", ""]
        out.append("- Conflicts: " + " · ".join(conflict_links))
    return "\n".join(out) + "\n"


# Approved H2 sets (STATUS §10), in reading order; a section without data is not written.
SECTIONS = {
    "result": ("Finding", "Context", "Data", "Source sentences", "Related claims / conflicts"),
    "claim": ("Claim", "Supporting evidence", "Opposing evidence", "Source sentences"),
    "conflict": ("Summary", "Side-by-side comparison", "What differs", "Sources"),
}


def heading_problems(text):
    """Unapproved or misordered H2 headings in the generated block of a Results/Claims note."""
    from . import notes
    fm, rest = notes._split_frontmatter(text)
    kind = (fm or {}).get("type")
    if kind not in SECTIONS:
        return []
    allowed = SECTIONS[kind]
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END)
    problems, last = [], -1
    for heading in re.findall(r"(?m)^## (.+?)\s*$", rest[start:end] if 0 <= start < end else ""):
        if heading not in allowed:
            problems.append(f"unapproved section {heading!r} for type {kind}")
        elif allowed.index(heading) <= last:
            problems.append(f"section {heading!r} out of order for type {kind}")
        else:
            last = allowed.index(heading)
    return problems


def _frontmatter_text(values):
    return "---\n" + yaml.safe_dump(values, sort_keys=False, allow_unicode=True, width=1000) + "---\n"


# Frontmatter keys the generators write; any other key (or a filled reviewed_by/on) is the researcher's.
_GENERATED_FM = {"type", "id", "source", "subject", "predicate", "object", "negated", "knowledge_level",
                 "significance", "in_taxon", "stage", "uses_protocol", "uses_technique", "figure_ids",
                 "sentence_ids", "conflicts_with", "has_conflict", "agent_type", "status", "title", "n",
                 "p_value", "p", "statistical_test", "test", "claim_type", "evidence_type", "derived_from",
                 "about", "supports", "opposes", "axis", "severity", "node_type", "reviewed_by", "reviewed_on",
                 "relation_ids", "retired", "papers", "measure", "agent"}


def _own_tags(fm):
    """Tags a person added: everything except the machine-owned `conflict` tag."""
    tags = fm.get("tags") or []
    return [t for t in ([tags] if isinstance(tags, str) else tags) if t != "conflict"]


def _human_text(text):
    from . import notes
    fm, rest = notes._split_frontmatter(text)
    if fm is None:
        return True
    if isinstance(fm, dict) and (set(fm) - _GENERATED_FM - {"tags"} or _own_tags(fm) or fm.get("reviewed_by") or fm.get("reviewed_on")):
        return True
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END)
    if start < 0 or end < start:
        return True
    outside = rest[:start] + rest[end + len(notes.AUTO_END):]
    outside = re.sub(r"^\s*# .*?(?:\r?\n|$)", "", outside, count=1)
    outside = re.sub(r"(?m)^## My notes\s*$", "", outside)  # the empty template heading is not human text
    return bool(outside.strip())


def _write_managed(path, values, body, title=None, retitle=False):
    from . import notes
    old = path.read_text(encoding="utf-8") if path.is_file() else None
    if old is None:
        title = title or values.get("title") or values.get("id")
        text = (_frontmatter_text(values) + f"\n# {title}\n\n{notes.AUTO_START}\n{body.rstrip()}\n{notes.AUTO_END}\n"
                "\n## My notes\n\n")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(notes.cap_managed_note(text, notes.NODE_NOTE_MAX_BYTES, "concept"), encoding="utf-8", newline="\n")
        return True
    fm, rest = notes._split_frontmatter(old)
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END)
    if fm is None or start < 0 or end < start:
        return False
    merged = dict(fm)
    merged.update(values)
    merged.update({k: fm[k] for k in ("reviewed_by", "reviewed_on") if fm.get(k)})  # a filled review field is the researcher's
    tags = [*_own_tags(fm), *(["conflict"] if "conflict" in (values.get("tags") or []) else [])]  # keep researcher tags
    if tags:
        merged["tags"] = tags
    else:
        merged.pop("tags", None)
    for key in ("n", "p_value", "p", "statistical_test", "test"):  # hide empty statistics
        if key not in values and merged.get(key) is None:
            merged.pop(key, None)
    if values.get("type") == "result" and "agent" not in values:  # no stated agent any more: drop the old phrase
        merged.pop("agent", None)
    block = f"{notes.AUTO_START}\n{body.rstrip()}\n{notes.AUTO_END}"
    head = rest[:start]
    if retitle and title:
        # The generated H1 follows the file name (renames/merges); other heading text is untouched.
        head = re.sub(r"(?m)^# (?:CONFLICT|EVD|CLM) - .*$", lambda _: f"# {title}", head, count=1)
    suffix = rest[end + len(notes.AUTO_END):]
    if not re.search(r"(?m)^## My notes\s*$", suffix):
        suffix = suffix.rstrip() + "\n\n## My notes\n\n"
    new = _frontmatter_text(merged) + head + block + suffix
    new = notes.cap_managed_note(new, notes.NODE_NOTE_MAX_BYTES, "concept")
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")
    return True


def _existing_notes(vault):
    from . import notes
    out = {}
    for folder in ("Results", "Claims"):
        root = config.vault_dir(vault) / folder
        for path in sorted(root.glob("*.md")) if root.is_dir() else []:
            try:
                fm, _ = notes._split_frontmatter(path.read_text(encoding="utf-8"))
            except OSError:
                continue
            if isinstance(fm, dict) and fm.get("id") and not fm.get("retired"):
                out[str(fm["id"])] = path
    return out


def _path_for(values, prefix, vault, used):
    from . import notes
    ident = str(values.get("id"))
    existing = used.get(ident)
    name = notes.safe_filename(f"{prefix} - {values.get('title') or values.get('statement') or ident}", 180)
    base = name
    if existing:
        tail = re.search(r" \(\d+\)$", existing.stem)
        if existing.stem.casefold() == base.casefold() or (tail and existing.stem.casefold() ==
                (notes.safe_filename(base, 180 - len(tail.group())) + tail.group()).casefold()):
            return existing
        if prefix == "CONFLICT" and re.fullmatch(r"duplicate(?: \d+)?", existing.stem.removeprefix(base).strip(" -")):
            # Keep a readable collision suffix stable across refreshes. The
            # relation ID is not part of the filename or title.
            return existing
    path = config.vault_dir(vault) / ("Results" if prefix == "EVD" else "Claims") / f"{name}.md"
    if prefix == "CONFLICT":
        # The descriptive measure/citation title is the stable filename. A
        # duplicate relation descriptor must still remain unique, without the
        # old opaque (2), (3) suffixes that hid which relation was reviewed.
        if existing and str(existing).casefold() == str(path).casefold():
            return existing
        occupied = path.exists() or any(
            str(candidate).casefold() == str(path).casefold()
            for candidate in used.values()
            if not existing or candidate != existing
        )
        if occupied:
            # A collision must remain readable; relation IDs stay only in
            # frontmatter and migration records, never in titles or filenames.
            name = notes.safe_filename(f"{base} - duplicate", 180)
            path = path.with_name(f"{name}.md")
            suffix = 2
            while path.exists() or str(path).casefold() in {str(x).casefold() for x in used.values()}:
                name = notes.safe_filename(f"{base} - duplicate {suffix}", 180)
                path = path.with_name(f"{name}.md")
                suffix += 1
        return path
    suffix = 2
    while path.exists() or str(path).casefold() in {str(x).casefold() for x in used.values()}:
        tail = f" ({suffix})"
        name = notes.safe_filename(base, 180 - len(tail)) + tail
        path = path.with_name(f"{name}.md")
        suffix += 1
    return path


def _move_conflict_note(old, new, vault):
    """Move a renamed generated conflict and record an undo-compatible map."""
    if old == new or not old.is_file() or new.exists():
        return None
    digest = _sha(old)
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    rel_old = old.relative_to(config.vault_dir(vault)).as_posix()
    rel_new = new.relative_to(config.vault_dir(vault)).as_posix()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    folder = config.STORE / "migrations"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stamp}_{vault}_result_notes.json"
    suffix = 1
    while target.exists():
        target = folder / f"{stamp}_{vault}_result_notes_{suffix}.json"
        suffix += 1
    target.write_text(json.dumps({
        "version": 1, "vault": vault, "created_at": config.now(), "snapshot": None,
        "entries": [{"action": "move", "from": rel_old, "to": rel_new, "sha256": digest}],
        "created_dirs": [], "removed_dirs": [], "collisions": [], "human_index_kept": [],
        "links_before": None, "links_after": None,
    }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return target


def _relocate(old, new, vault, migrations):
    """Rename a generated note whose title changed (undo map recorded); keep ``old`` when it cannot move."""
    if old is None or old == new:
        return new
    if old.is_file():
        moved = _move_conflict_note(old, new, vault)
        if not moved:
            return old
        migrations.append(moved)
    return new


def _same_measure(left, right, relation=None):
    """Require the two relation endpoints to measure the same specific thing."""
    left_measure = _measure(left or {})
    right_measure = _measure(right or {})
    if left_measure not in (None, "") and right_measure not in (None, ""):
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        return normalize(left_measure) == normalize(right_measure)
    relation = relation or {}
    first = relation.get("outcome_measure") or relation.get("measure")
    second = relation.get("outcome_measure_other") or relation.get("measure_other")
    if first not in (None, "") and second not in (None, ""):
        normalize = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).strip()
        return normalize(first) == normalize(second)
    # shared_concept_name/measure_group alone may be a broad bridge concept;
    # it is intentionally insufficient evidence for an opposition edge.
    return False


def _claim_relation_links(unit, relations, evd_paths, by_unit=None):
    """Return explicit or source-overlapping typed links for one conclusion."""
    supports, opposes = [], []
    for key, target in (("supports", supports), ("opposes", opposes)):
        for value in _list(unit.get(key)):
            if value in evd_paths:
                if key == "opposes" and not any(
                        value in {relation.get("from_unit"), relation.get("to_unit")} and
                        _same_measure((by_unit or {}).get(relation.get("from_unit")),
                                      (by_unit or {}).get(relation.get("to_unit")), relation)
                        for relation in relations):
                    continue
                target.append(f"[[{evd_paths[value].stem}]]")
    for relation in relations:
        for key, target in (("supports", supports), ("opposes", opposes)):
            for value in _list(relation.get(key)):
                if value in evd_paths:
                    if key == "opposes" and not _same_measure(
                            (by_unit or {}).get(relation.get("from_unit")),
                            (by_unit or {}).get(relation.get("to_unit")), relation):
                        continue
                    target.append(f"[[{evd_paths[value].stem}]]")
    claim_ids = set(_sentence_ids(unit)) | set(_list(unit.get("cited_sentence_ids")))
    for relation in relations:
        endpoints = [relation.get("from_unit"), relation.get("to_unit")]
        matched = []
        for endpoint in endpoints:
            if endpoint not in evd_paths:
                continue
            endpoint_ids = set(_sentence_ids((by_unit or {}).get(endpoint) or {}))
            normalized = {str(value) for value in claim_ids}
            pid = str(endpoint).rsplit(":", 1)[0]
            normalized |= {f"{pid}:{value}" for value in normalized if ":" not in value}
            if endpoint_ids.intersection(normalized):
                matched.append(endpoint)
        if len(matched) != 1 or relation.get("type") not in CONFLICT_TYPES:
            continue
        other = next((endpoint for endpoint in endpoints if endpoint != matched[0] and endpoint in evd_paths), None)
        link = f"[[{evd_paths[matched[0]].stem}]]"
        supports.append(link)
        if other and _same_measure((by_unit or {}).get(matched[0]), (by_unit or {}).get(other), relation):
            opposes.append(f"[[{evd_paths[other].stem}]]")
    return list(dict.fromkeys(supports)), list(dict.fromkeys(opposes))


def _claim_values(unit, meta, vault, derived, about=(), supports=(), opposes=()):
    outcome = unit.get("outcome") or {}
    subject = _node_link(unit.get("subject") or _subject(unit), vault) or None
    predicate = unit.get("predicate") or (_predicate(unit) if outcome else None)
    obj = _node_link(unit.get("object") or _measure(unit), vault) or None
    return {"type": "claim", "id": unit.get("unit_id"), "source": _paper_link(meta),
            "claim_type": "author_conclusion",
            "subject": [subject] if subject else [], "predicate": predicate, "object": [obj] if obj else [],
            "negated": bool(unit.get("negated") or outcome.get("negated", False)),
            "knowledge_level": "knowledge_assertion", "evidence_type": "primary" if _unit_is_primary(meta) else "secondary",
            "derived_from": derived, "about": list(about), "supports": list(supports), "opposes": list(opposes),
            "sentence_ids": _sentence_ids(unit), "agent_type": "automated", "status": "machine",
            "title": _short(unit.get("statement"), 90)}


@_with_source_ids
def _claim_body(unit, meta, derived, about=(), supports=(), opposes=()):
    """CLM note: Claim · Supporting evidence · Opposing evidence · Source sentences (empty ones are not written)."""
    out = ["## Claim", "", str(unit.get("statement") or "").strip()]
    if not _unit_is_primary(meta):
        out += ["", "> [!note] Secondary evidence: a review-paper statement, not primary experimental evidence."]
    if about:
        out += ["", "Entities: " + " · ".join(about)]
    for heading, links in (("Supporting evidence", supports), ("Opposing evidence", opposes)):
        if links:
            out += ["", f"## {heading}", "", *[f"- {link}" for link in dict.fromkeys(links)]]
    out += ["", "## Source sentences", "", *_quote_lines(unit, _read_sentences(meta["paper_id"]), meta)]
    return "\n".join(out) + "\n"


def _axis(relation):
    if relation.get("axis"):
        return relation["axis"]
    differences = relation.get("differences") or []
    fields = " ".join(str(d.get("field") or d.get("from") or "") for d in differences).casefold()
    for token, axis in (("species", "species"), ("stage", "stage"), ("dose", "concentration"),
                        ("concentration", "concentration"), ("protocol", "protocol"),
                        ("technique", "technique"), ("method", "technique"),
                        ("p", "statistics")):
        if token in fields:
            return axis
    return "unknown"


def _severity(relation):
    if relation.get("severity"):
        return relation["severity"]
    return {"contradicts": "🔴", "direction_differs": "🟠", "effect_vs_no_effect": "🟡"}.get(relation.get("type"), "🟡")


def _side_model(unit, meta, quote=None):
    """(species, stage) of one side: the single chain for the A/B table AND the CONFLICT title.
    Unit, then meta, then the paper-level model (marked PAPER_LEVEL; the title strips the mark).
    ``quote`` = the unit's own source sentences (read from the store when ``None``)."""
    model = unit.get("model") or {}
    meta_model = (meta or {}).get("model") or {}
    species = _property_value(model.get("species") or model.get("taxon") or unit.get("species") or
                              meta_model.get("species") or meta_model.get("taxon") or
                              (meta or {}).get("species") or (meta or {}).get("taxon"))
    stage = _property_value(model.get("stage") or model.get("developmental_stage") or unit.get("stage") or
                            meta_model.get("stage") or meta_model.get("developmental_stage") or
                            (meta or {}).get("stage") or (meta or {}).get("developmental_stage"))
    pid = str(unit.get("paper_id") or (meta or {}).get("paper_id") or "")
    if pid and not (species and stage):  # paper-level model (marked PAPER_LEVEL), term only, never a sentence
        from . import concept_notes
        quote = _source_text(unit, pid) if quote is None else quote
        own = {"results": [{"unit_id": unit.get("unit_id"), "sources": [{"text": quote}]}]}  # unit's own sentences first
        paper_species, paper_stage = concept_notes._paper_model_labels(pid, model, own, unit=unit)
        mark = lambda v: None if not v else v.replace(PAPER_LEVEL, "").strip() + PAPER_LEVEL
        species = species or mark(paper_species)
        stage = stage or mark(paper_stage)
    return species, stage


def _side(unit, meta, sentences):
    outcome = unit.get("outcome") or {}
    intervention = unit.get("intervention") or {}
    quote = " ".join(row.get("text", "") for row in _source_rows(unit, sentences))
    meta = meta or _meta(str(unit.get("paper_id") or str(unit.get("unit_id") or "").rsplit(":", 1)[0]))
    with presentation.collect_source_ids() as source_ids:
        sources = "; ".join(_row_cite(row, meta) for row in _source_rows(unit, sentences))
        quote_lines = _quote_lines(unit, sentences, meta)
    species, stage = _side_model(unit, meta, quote)
    return {"statement": unit.get("statement") or "", "direction": _direction(unit), "sources": sources,
            "species": species or None, "stage": stage or None,
            "protocol": intervention.get("protocol") or "", "quote": quote,
            "quote_lines": quote_lines, "source_ids": dict(source_ids),
            "measure": outcome.get("measure") or None}


def _conflict_group_key(relation, left, right):
    """Same paper pair + same measure => one note (several relations may share it)."""
    norm = lambda value: re.sub(r"[^a-z0-9]+", " ", str(value or "").casefold()).strip()
    measure = (relation.get("shared_concept_name") or relation.get("measure_group") or
               f"{_measure(left) or ''}|{_measure(right) or ''}")
    papers = tuple(sorted(str(relation[k]).rsplit(":", 1)[0] for k in ("from_unit", "to_unit")))
    return papers, norm(measure)


def _conflict_values(relation, sides, links):
    """Frontmatter of a CONFLICT note; ``title`` is set by refresh (the file name, from ``_conflict_title``)."""
    return {"type": "conflict", "id": relation.get("relation_id"), "status": _relation_status(relation),
            "axis": _axis(relation), "conflicts_with": links, "severity": _severity(relation),
            "node_type": relation.get("node_type") or relation.get("shared_concept_type") or "result",
            "papers": f"{sides[0].get('cite')} vs {sides[1].get('cite')}" if sides[0].get("cite") and sides[1].get("cite") else None,
            "measure": relation.get("shared_concept_name") or relation.get("measure_group") or sides[0]["measure"] or None,
            "reviewed_by": "", "reviewed_on": "", "agent_type": "automated",
            "has_conflict": True, "title": None, "tags": ["conflict"]}


def _relation_status(relation):
    status = relation.get("status")
    if status in ACTIVE_RELATION_STATUSES:
        return "open"
    if status in ("explained", "dismissed", "rejected", "human_rejected"):
        return "dismissed" if status in ("dismissed", "rejected", "human_rejected") else "explained"
    return "stale"


def _side_ids(sides):
    return {sid: label for side in sides for sid, label in side.get("source_ids", {}).items()}


_SIDE_ROWS = (("Statement", "statement"), ("Direction", "direction"), ("Species", "species"),
              ("Stage", "stage"), ("Protocol", "protocol"), ("Measure", "measure"), ("Source", "sources"))


def _side_value(side, key):
    value = side.get(key)
    return value if value not in (None, "") and not _is_missing_annotation(value) else None


def _comparison_table(sides):
    """A/B table; a row nobody reports is left out, a one-sided gap reads ``—``."""
    lines = ["| | A | B |", "|---|---|---|"]
    for label, key in _SIDE_ROWS:
        values = [_side_value(side, key) for side in sides]
        if any(value is not None for value in values):
            lines.append(f"| {label} | " + " | ".join(_markdown_cell(value) or "—" for value in values) + " |")
    return lines


def _differences(relation, sides):
    """Annotated differences of the relation; else the table rows whose A and B values differ (source-bound)."""
    rows = [f"- {d.get('field') or 'difference'}: {_markdown_cell(d.get('from')) or '—'} → "
            f"{_markdown_cell(d.get('to')) or '—'}" for d in relation.get("differences") or []]
    if rows:
        return rows
    for label, key in _SIDE_ROWS[1:6]:
        left, right = (_side_value(side, key) for side in sides)
        if left is not None and right is not None and str(left).casefold() != str(right).casefold():
            rows.append(f"- {label}: {_markdown_cell(left)} → {_markdown_cell(right)}")
    return rows


def _conflict_body(relation, sides, links, source_ids=True, extra=()):
    """Conflict note: Summary · Side-by-side comparison · What differs · Sources.

    ``extra`` = further ``(relation, sides, links)`` pairs of the same paper pair + measure (shown as Comparison 2…)."""
    pairs = [(relation, sides, links), *extra]
    many = len(pairs) > 1
    tag = lambda n: f"Comparison {n} — " if many else ""
    cite = lambda side, label: side.get("cite") or label
    measure = (relation.get("shared_concept_name") or relation.get("measure_group") or
               _side_value(sides[0], "measure") or "result")
    all_links = list(dict.fromkeys(link for _, _, pair_links in pairs for link in pair_links))
    out = [f"> [!conflict] Source conflict · {_relation_status(relation)} · {_severity(relation)}",
           "> Neither side is removed; change the status only after reviewing both."]
    if any(rel.get("type") == "effect_vs_no_effect" for rel, _, _ in pairs):
        out.append("> A non-significant result is not evidence of no effect.")
    out += ["", "## Summary", "",
           f"- Measure: {_markdown_cell(measure)}",
           f"- A: {cite(sides[0], 'A')}" + (f" — {sides[0]['direction']}" if sides[0].get("direction") else ""),
           f"- B: {cite(sides[1], 'B')}" + (f" — {sides[1]['direction']}" if sides[1].get("direction") else "")]
    if all_links:
        out.append("- Linked evidence: " + " · ".join(all_links))
    out += ["", "## Side-by-side comparison", ""]
    for number, (_, pair_sides, _) in enumerate(pairs, 1):
        out += ([f"### Comparison {number}", ""] if many else []) + _comparison_table(pair_sides) + [""]
    differs = [f"- {tag(n)}{row[2:]}" if many else row for n, (rel, pair_sides, _) in enumerate(pairs, 1)
               for row in _differences(rel, pair_sides)]
    if differs:
        out += ["## What differs", "", *differs, ""]
    out += ["## Sources", ""]
    for number, (_, pair_sides, _) in enumerate(pairs, 1):
        for label, side in zip(("A", "B"), pair_sides):
            out += [f"### {tag(number)}{label} — {cite(side, label)}" if side.get("cite") else f"### {tag(number)}{label}",
                    "", *side.get("quote_lines", []), ""]
    if source_ids:
        out += presentation.source_ids_callout(_side_ids([side for _, pair_sides, _ in pairs for side in pair_sides]))
    return "\n".join(out)


def _replace_retired_block(path, message):
    from . import notes
    text = path.read_text(encoding="utf-8")
    fm, rest = notes._split_frontmatter(text)
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END)
    if fm is None or start < 0 or end < start:
        return False
    fm["retired"] = True
    if fm.get("type") == "conflict":  # a withdrawn conflict is history, never an open/red node
        fm["type"], fm["status"], fm["has_conflict"] = "conflict_history", "withdrawn", False
        if _own_tags(fm):
            fm["tags"] = _own_tags(fm)
        else:
            fm.pop("tags", None)
    block = f"{notes.AUTO_START}\n> [!warning] {message}\n{notes.AUTO_END}"
    path.write_text(_frontmatter_text(fm) + rest[:start] + block + rest[end + len(notes.AUTO_END):], encoding="utf-8", newline="\n")
    return True


def _retire(vault, generated_ids, existing):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    moved = 0
    for ident, path in existing.items():
        if ident in generated_ids or not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        if _human_text(text):
            _replace_retired_block(path, "This generated note is no longer produced from the current store; human text was preserved.")
            continue
        vault_layout.retire(vault, path, "result_notes", stamp)
        moved += 1
    return moved


def _ensure_bases(vault):
    from . import notes
    vdir = config.vault_dir(vault)
    base_dir = vdir / "_Bases"
    base_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "Results": {"filters": {"and": ['file.inFolder("Results")', 'type == "result"']},
                    "views": [{"type": "table", "name": "Results", "order": ["file.name", "source", "predicate", "object", "significance", "has_conflict"]}]},
        "Conflicts": {"filters": {"and": ['status == "open"', 'type == "conflict"']},
                      "views": [{"type": "table", "name": "Conflicts", "order": ["file.name", "status", "axis", "severity", "conflict_open", "reviewed_on"]}]},
    }
    for name, value in values.items():
        path = base_dir / f"{name}.base"
        equivalents = [path]
        if name == "Conflicts":
            equivalents.append(base_dir / "Open conflicts.base")
        if any(candidate.is_file() for candidate in equivalents):
            continue
        path.write_text(yaml.safe_dump(value, sort_keys=False, allow_unicode=True, width=1000), encoding="utf-8", newline="\n")


def refresh(con, vault):
    """Refresh media manifests and generated result/claim/conflict notes."""
    from . import notes
    vdir = config.vault_dir(vault)
    vdir.mkdir(parents=True, exist_ok=True)
    paper_ids = [row[0] for row in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ? ORDER BY paper_id", (vault,))]
    metas = {pid: _meta(pid) for pid in paper_ids}
    units_by_paper = {pid: _read_units(pid) for pid in paper_ids}
    bundles_by_paper = {pid: _unique_bundles(list(media.bundles(pid))) for pid in paper_ids}
    relations = _relations(vault, set(paper_ids))
    by_unit = {u.get("unit_id"): u for units in units_by_paper.values() for u in units}
    existing = _existing_notes(vault)
    result_units = {}
    for pid in paper_ids:
        result_units[pid] = _main_units(pid, metas[pid], units_by_paper[pid],
                                        db.read_json(_paper_folder(pid) / "analysis.json", {}) or {},
                                        relations, bundles_by_paper[pid])
    selected = {u.get("unit_id"): u for units in result_units.values() for u in units}
    # Related endpoints are required EVDs even when the normal media/summary ranking omitted them.
    for uid in {rel.get(key) for rel in relations for key in ("from_unit", "to_unit")}:
        if uid in by_unit and by_unit[uid].get("unit_type") == "result" and _has_measure(by_unit[uid]):
            pid = str(uid).rsplit(":", 1)[0]
            if _unit_is_primary(metas.get(pid, {})):
                selected.setdefault(uid, by_unit[uid])
    # Keep the per-paper cap deterministic after adding relation endpoints.
    for pid in paper_ids:
        selected_ids = [u.get("unit_id") for u in result_units[pid]]
        extras = sorted(uid for uid, unit in selected.items() if str(uid).startswith(pid + ":") and uid not in selected_ids)
        selected_ids = (selected_ids + extras)[:MAX_RESULTS]
        result_units[pid] = [selected[uid] for uid in selected_ids]
    evd_paths = {}
    migration_paths = []
    media_by_paper = {}
    for pid in paper_ids:
        copied = copy_crops(vault, pid, bundles_by_paper[pid])
        media_by_paper[pid] = {
            str(b.get("object_id")): {
                **b,
                "vault_entry": next((e for e in copied["entries"]
                                      if str(e.get("object_id")) == str(b.get("object_id"))), {}),
            }
            for b in bundles_by_paper[pid]
        }
        for unit in result_units[pid]:
            target = _path_for({"id": unit["unit_id"],
                                "title": f"{_short(unit.get('statement') or _measure(unit), 80)} ({_citation(metas[pid])})"},
                               "EVD", vault, existing)
            evd_paths[unit["unit_id"]] = _relocate(existing.get(unit["unit_id"]), target, vault, migration_paths)
            existing[unit["unit_id"]] = evd_paths[unit["unit_id"]]
    claim_units = {pid: sorted((u for u in units_by_paper[pid] if u.get("unit_type") == "conclusion"),
                               key=lambda u: (u.get("seq", 0), u.get("unit_id", "")))[:MAX_CLAIMS]
                   for pid in paper_ids}
    claim_paths = {}
    for pid in paper_ids:
        for unit in claim_units[pid]:
            target = _path_for({"id": unit["unit_id"], "title": _short(unit.get("statement"), 90)}, "CLM", vault, existing)
            claim_paths[unit["unit_id"]] = _relocate(existing.get(unit["unit_id"]), target, vault, migration_paths)
            existing[unit["unit_id"]] = claim_paths[unit["unit_id"]]
    conflict_paths = {}
    conflict_titles = {}
    # One note per (paper pair, measure). Only open relations merge; reviewed history stays 1:1.
    groups, by_key = [], {}
    for relation in relations:
        ident = str(relation.get("relation_id"))
        if _relation_status(relation) in ("dismissed", "stale") and ident not in existing:
            continue
        left, right = by_unit.get(relation.get("from_unit")), by_unit.get(relation.get("to_unit"))
        if not left or not right:
            continue
        key = _conflict_group_key(relation, left, right) if _relation_status(relation) == "open" else ident
        if key not in by_key:
            by_key[key] = []
            groups.append(by_key[key])
        by_key[key].append(relation)
    for group in groups:  # keep the id of the note that already exists, so its human text stays on it
        known = next((r for r in group if str(r.get("relation_id")) in existing), group[0])
        group.insert(0, group.pop(group.index(known)))
    # Conflict notes whose relation is withdrawn (or merged into another note) are retired
    # reversibly now, so their file names do not block the notes that replace them.
    keep_ids = {str(g[0].get("relation_id")) for g in groups}
    keep_ids.update(str(r.get("relation_id")) for r in relations
                    if _relation_status(r) in ("dismissed", "stale") and str(r.get("relation_id")) in existing)
    stale_conflicts = {i: p for i, p in existing.items() if p.name.startswith("CONFLICT - ") and i not in keep_ids}
    retired_early = _retire(vault, set(), stale_conflicts)
    for ident in stale_conflicts:
        existing.pop(ident)

    def conflict_sides(relation):
        left, right = by_unit.get(relation.get("from_unit")), by_unit.get(relation.get("to_unit"))
        left_meta = metas.get(str(relation["from_unit"]).rsplit(":", 1)[0], {})
        right_meta = metas.get(str(relation["to_unit"]).rsplit(":", 1)[0], {})
        sides = [_side(left, left_meta, _read_sentences(left_meta["paper_id"])),
                 _side(right, right_meta, _read_sentences(right_meta["paper_id"]))]
        for side, m in zip(sides, (left_meta, right_meta)):
            side["cite"] = _citation(m)
        return left, right, left_meta, right_meta, sides

    def conflict_inputs(relation, known=None):
        left, right, left_meta, right_meta, sides = known or conflict_sides(relation)
        links = [f"[[{evd_paths[uid].stem}]]" for uid in (relation.get("from_unit"), relation.get("to_unit")) if uid in evd_paths]
        for value in (_subject(left), _measure(left), _subject(right), _measure(right)):
            linked = _node_link(value, vault)
            if linked.startswith("[[") and linked not in links:
                links.append(linked)
        return left, right, left_meta, right_meta, sides, links

    first_sides = {}  # the title and the body of a note read the same sides
    for group in groups:
        relation = group[0]
        ident = str(relation.get("relation_id"))
        first_sides[ident] = conflict_sides(relation)
        left, right, left_meta, right_meta, sides = first_sides[ident]
        title = _conflict_title(relation, left, right, left_meta, right_meta, merged=len(group) > 1, sides=sides)
        # A rename that cannot move keeps the old note (and its human text) as the path in use.
        conflict_paths[ident] = _relocate(existing.get(ident), _path_for({"id": ident, "title": title}, "CONFLICT", vault, existing),
                                          vault, migration_paths)
        # The H1 is the note's file name, so it is unique by construction.
        conflict_titles[ident] = conflict_paths[ident].stem.removeprefix("CONFLICT - ")
        existing[ident] = conflict_paths[ident]
        for other in group[1:]:
            conflict_paths[str(other.get("relation_id"))] = conflict_paths[ident]
    generated = set()
    registry = concepts.load_registry(con)  # A4: the nodes a measure phrase names
    # Historical rejected/stale relations remain recoverable even when one
    # endpoint is no longer present in the current store.
    generated.update(str(r.get("relation_id")) for r in relations
                     if _relation_status(r) in ("dismissed", "stale") and str(r.get("relation_id")) in existing)
    for pid in paper_ids:
        meta, sentences = metas[pid], _read_sentences(pid)
        for unit in result_units[pid]:
            related_claims = [f"[[{claim_paths[c['unit_id']].stem}]]" for c in claim_units[pid]
                              if set(_sentence_ids(unit)).intersection(_sentence_ids(c)) and c.get("unit_id") in claim_paths]
            links = []
            evd_links = []
            conflict_relations = [r for r in relations if unit.get("unit_id") in (r.get("from_unit"), r.get("to_unit")) and _relation_status(r) == "open"]
            for relation in conflict_relations:
                other = relation.get("to_unit") if relation.get("from_unit") == unit.get("unit_id") else relation.get("from_unit")
                if other in evd_paths:
                    link = f"[[{evd_paths[other].stem}]]"
                    links.append(link)
                    evd_links.append(link)
                if str(relation.get("relation_id")) in conflict_paths:
                    links.append(f"[[{conflict_paths[str(relation.get('relation_id'))].stem}]]")
            values = _evidence_frontmatter(unit, meta, vault, _short(unit.get("statement") or _measure(unit), 80), evd_links,
                                           registry)
            conflict_note_links = list(dict.fromkeys(link for link in links if link not in evd_links))
            body = _result_body(unit, meta, vault, media_by_paper[pid], related_claims,
                                conflict_note_links)
            path = evd_paths[unit["unit_id"]]
            _write_managed(path, values, body, path.stem, retitle=True)
            generated.add(unit["unit_id"])
    for pid in paper_ids:
        meta = metas[pid]
        for unit in claim_units[pid]:
            conclusion_sentences = set(_sentence_ids(unit))
            # Bibliography reference numbers are not evidence sentence IDs.
            conclusion_sentences.update(str(sid) for sid in _list(unit.get("cited_sentence_ids")))
            conclusion_sentences.update(str(sid) for sid in _list(unit.get("cited_refs"))
                                        if re.fullmatch(r"(?:[^:]+:)?S\d+", str(sid)))
            conclusion_sentences = {sid if ":" in sid else f"{pid}:{sid}" for sid in conclusion_sentences}
            derived_evd = [f"[[{evd_paths[u['unit_id']].stem}]]" for u in result_units[pid]
                           if u.get("unit_id") in evd_paths and
                           conclusion_sentences.intersection(
                               sid if ":" in sid else f"{pid}:{sid}" for sid in _sentence_ids(u))]
            derived = derived_evd or [_paper_link(meta)]
            about = _about_links(unit.get("statement"), vault)
            # Only live (open) relations can make a claim "opposed"; dismissed / withdrawn ones are history.
            supports, opposes = _claim_relation_links(unit, [r for r in relations if _relation_status(r) == "open"],
                                                     evd_paths, by_unit)
            supports = list(dict.fromkeys(derived_evd + supports))  # results sharing the claim's own sentences
            values = _claim_values(unit, meta, vault, derived, about, supports, opposes)
            values["claim_type"] = "review_statement" if not _unit_is_primary(meta) else "author_conclusion"
            body = _claim_body(unit, meta, derived, about, supports, opposes)
            path = claim_paths[unit["unit_id"]]
            _write_managed(path, values, body, path.stem, retitle=True)
            generated.add(unit["unit_id"])
    conflict_count = 0
    for group in groups:
        relation = group[0]
        ident = str(relation.get("relation_id"))
        left, right, left_meta, right_meta, sides, links = conflict_inputs(relation, first_sides[ident])
        values = _conflict_values(relation, sides, links)
        extra = [(other, *conflict_inputs(other)[4:6]) for other in group[1:]]
        body = _conflict_body(relation, sides, links, extra=extra)
        if extra:
            values["conflicts_with"] = list(dict.fromkeys(values["conflicts_with"] + [x for _, _, ls in extra for x in ls]))
        if len(group) > 1:
            values["relation_ids"] = [str(r.get("relation_id")) for r in group]
        values["title"] = conflict_titles[ident]
        path = conflict_paths[ident]
        _write_managed(path, values, body, path.stem, retitle=True)
        existing[ident] = path
        generated.add(ident)
        conflict_count += 1
    retired = _retire(vault, generated, existing)
    _ensure_bases(vault)
    return {"results": sum(len(rows) for rows in result_units.values()), "claims": sum(len(rows) for rows in claim_units.values()),
            "conflicts": conflict_count, "retired": retired + retired_early, "migrations": [str(path) for path in migration_paths]}
