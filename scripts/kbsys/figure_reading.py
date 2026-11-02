"""Vision-reading tasks for source-bound figures and tables.

This module is deliberately a separate layer above :mod:`kbsys.media`.  It
does not transcribe pixels itself and it never changes text evidence.  A
vision-capable analyst receives a verified crop plus the exact source
sentences that describe or caption the object, then submits a structured
reading which remains tied to those inputs.
"""
from __future__ import annotations

import argparse
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import sys
import time
from pathlib import Path

from . import analysis_transport, answer, config, db, media, workflow


VERSION = 1
ALLOWED_READ_QUALITY = {"exact", "approximate", "uncertain"}
ALLOWED_CONSISTENCY_STATUS = {"consistent", "inconsistent", "not_checkable"}
_NUMBER = re.compile(
    r"(?<![A-Za-z0-9_])[-+]?(?:(?:\d+(?:\.\d*)?)|(?:\.\d+))"
    r"(?:[eE][-+]?\d+)?%?"
)
# Capitalised words that carry a real quantity ("Day-3", "Group-91"): a hyphen after them does not make a marker name.
_QUANTITY = r"(?!(?:Day|Week|Group|Dose|Sample|Well|Passage|Patient|Case|Cell|Line|Batch|Run|Trial|Figure|Table|Panel)-)"
_LABEL = re.compile(r"\b(?:Fig(?:ure)?s?\.?|Tables?|Panels?)\s*S?\d+[A-Za-z]?(?:\s*[-–,]\s*S?\d+[A-Za-z]?(?![\d.%]))*"
                    r"|(?-i:\b" + _QUANTITY + r"[A-Z][A-Za-z]+-\d+[A-Za-z0-9]*\b|\b\d+[A-Z]{1,2}\b(?![.,]?\d))", re.I)
# Digits inside a name (SIRT2/5/6, Tuj-1, 5,6-dihydro...) are not a quantity.
_NAME_NUMBER = re.compile(r"\b" + _QUANTITY + r"\w*[A-Za-z]\w*(?:[-/,]\d+)+|\b\d+(?:,\d+)*-[A-Za-z]")
_RESULT_SECTIONS = {
    "result", "results", "results_discussion", "results_and_discussion",
    "resultsdiscussion", "result_and_discussion",
}
_OBJECT_KINDS = {"figure", "table"}
_FORMULA_KINDS = {"formula", "special_symbol_region", "anchor", "inline_graphic"}


def _full_object_id(paper_id, object_id):
    if not isinstance(paper_id, str) or not paper_id:
        raise ValueError("Invalid paper ID")
    if not isinstance(object_id, str) or not object_id:
        raise ValueError("Invalid object ID")
    return object_id if ":" in object_id else f"{paper_id}:{object_id}"


def _folder(paper_id):
    folder = db.paper_dir(paper_id).resolve()
    if not folder.is_relative_to(config.PAPERS_DIR.resolve()):
        raise ValueError("Paper folder escapes the paper store")
    return folder


def _revision(folder):
    """Fingerprint all source records which can change a reading task."""
    return workflow.fingerprint([
        folder / "metadata.json",
        folder / "sentences.jsonl",
        folder / "figures.jsonl",
        folder / "tables.jsonl",
        folder / "media_manifest.json",
    ])


def _cited_digest(captions, mentions):
    """Hash of the sentence TEXTS a reading was shown (caption + mentions), without IDs, so a pure
    renumbering keeps a reading current while any change to those sentences makes it stale."""
    texts = [row.get("text", "") for row in list(captions) + list(mentions)]
    return hashlib.sha256(json.dumps(texts, ensure_ascii=False).encode("utf-8")).hexdigest()


def current_cited_digest(paper_id, object_id):
    bundle = _bundle(paper_id, object_id)
    rows = _sentence_rows(_folder(paper_id))
    return _cited_digest(bundle.get("caption") or [], _mention_sentences(paper_id, bundle, rows))


def legacy_digest(paper_id, saved):
    """Digest to add to a reading that predates `cited_text_sha256`, or None when it already has one or is
    not current right now (a stale reading must not become current by being re-stamped)."""
    if saved.get("cited_text_sha256") or _current_state(paper_id, saved.get("object_id"), saved):
        return None
    return current_cited_digest(paper_id, saved["object_id"])


def _sentence_ref(row):
    return {"sentence_id": row.get("sentence_id"), "text": row.get("text", "")}


def _sentence_rows(folder):
    return list(db.read_jsonl(folder / "sentences.jsonl"))


def _bundle(paper_id, object_id):
    full = _full_object_id(paper_id, object_id)
    bundles = media.bundles(paper_id, object_ids=[full])
    found = next((item for item in bundles if item.get("object_id") == full), None)
    if found is None:
        raise ValueError(f"Unknown media object: {full}")
    return found


def _verified_crop(paper_id, bundle):
    folder = _folder(paper_id)
    crop = bundle.get("crop") or {}
    path_value = crop.get("path")
    expected = crop.get("sha256")
    if not path_value or not expected:
        raise ValueError("Object crop has no path or SHA256")
    path = media.crop_path(folder, path_value)
    actual = media._hash(path)
    if not path.is_file() or actual != expected:
        raise ValueError("Crop SHA256 mismatch; retrieve or rebuild the media crop")
    if crop.get("status") not in {"available", "retained"}:
        raise ValueError("Object does not have a verified available crop")
    return path, expected


def _mention_sentences(paper_id, bundle, rows):
    """Return exact non-caption source rows linked to the object.

    D3.13's explicit mention anchors are authoritative.  The exact source
    label is a conservative fallback for older inventories whose sentence
    row has not yet received a ``mentions`` anchor; it is never used to copy
    or rewrite text.
    """
    selected = {}
    for row in bundle.get("mentions") or []:
        sid = row.get("sentence_id")
        if sid and row.get("kind") not in {"caption", "table_row"}:
            selected[sid] = row
    label = str(bundle.get("label") or "").strip()
    if label:
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(label) + r"(?![A-Za-z0-9])", re.I)
        for row in rows:
            sid = row.get("sentence_id")
            if (sid and row.get("kind") not in {"caption", "table_row"}
                    and pattern.search(str(row.get("text") or ""))):
                selected.setdefault(sid, row)
    return sorted(selected.values(), key=lambda row: (row.get("seq", 0), row.get("sentence_id", "")))


def _reply_format():
    return {
        "panels": [{
            "panel": "A",
            "chart_kind": "bar|box|scatter|line|image|table|other",
            "groups": [],
            "measure": "exact source measure or null",
            "unit": "exact source unit or null",
            "values": [{
                "group": "exact label",
                "value": "number or null",
                "error": "number or null",
                "n": "integer or null",
                "read_quality": "exact|approximate|uncertain",
            }],
            "significance_marks": [{
                "between": [], "mark": "exact mark", "legend_meaning": "exact legend text or null",
            }],
        }],
        "caption_consistency": [{
            "claim_sentence_id": "paper:S####",
            "status": "consistent|inconsistent|not_checkable",
            "note": "source-grounded note; numbers must be visible in the cited sentence or panel values",
        }],
        "summary": {"text": "brief visual summary; every number must be in a shown sentence or a panel value", "panel_refs": []},
    }


INSTRUCTIONS = (
    "Use a vision-capable model such as Claude Sonnet to read the image file at crop_path "
    "and compare it with the supplied source sentences. "
    "Transcribe only what is legible. Mark anything uncertain as uncertain. "
    "Never estimate bar heights as exact numbers. A value read from a plot axis "
    "is approximate and must include the axis reading. Copy panel labels exactly. "
    "Keep captions with their data. Do not replace or rewrite text evidence; "
    "report caption consistency separately."
)


def task(paper_id, object_id):
    """Prepare a source-bound vision task for one media object."""
    folder = _folder(paper_id)
    bundle = _bundle(paper_id, object_id)
    crop_path, crop_sha256 = _verified_crop(paper_id, bundle)
    rows = _sentence_rows(folder)
    captions = [_sentence_ref(row) for row in bundle.get("caption") or []]
    mentions = [_sentence_ref(row) for row in _mention_sentences(paper_id, bundle, rows)]
    data = [_sentence_ref(row) for row in bundle.get("data") or []]
    allowed = sorted({row["sentence_id"] for row in captions + mentions + data if row.get("sentence_id")})
    return {
        "schema_version": VERSION,
        "paper_id": paper_id,
        "object_id": bundle["object_id"],
        "label": bundle.get("label"),
        "kind": bundle.get("kind"),
        "crop_path": str(crop_path),
        "crop_sha256": crop_sha256,
        "caption_sentences": captions,
        "caption": captions,
        "mention_sentences": mentions,
        "text_sentences": mentions,
        "mentions": mentions,
        "table_extraction": bundle.get("table"),
        "table": bundle.get("table"),
        "table_data_sentences": data,
        "source_locator": bundle.get("source_locator"),
        "allowed_sentence_ids": allowed,
        "source_revision": _revision(folder),
        "cited_text_sha256": _cited_digest(captions, mentions),
        "media_revision": bundle.get("source_locator", {}).get("revision"),
        "instructions": INSTRUCTIONS,
        "reply_format": _reply_format(),
    }


def _number_tokens(value):
    if value is None or isinstance(value, bool):
        return set()
    tokens = set()
    # "1,903,900" is one number; thousands separators must not split it.
    text = re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", str(value).replace("−", "-"))
    for raw in _NUMBER.findall(text):
        suffix = "%" if raw.endswith("%") else ""
        number = raw[:-1] if suffix else raw
        try:
            normal = format(Decimal(number), "f")
            if "." in normal:  # only fractional zeros are insignificant: 1903900 stays 1903900
                normal = normal.rstrip("0").rstrip(".") or "0"
            if normal == "-0":
                normal = "0"
            tokens.add((normal, suffix))
        except InvalidOperation:
            tokens.add((number, suffix))
    return tokens


def _validate_reply(reply, payload):
    if not isinstance(reply, dict):
        raise ValueError("Reply must be an object")
    if reply.get("crop_wrong") is True:  # the crop does not show the object: record it, read nothing
        if not isinstance(reply.get("note"), str) or not reply["note"].strip():
            raise ValueError("crop_wrong needs a note saying what the crop shows instead")
        return {"crop_wrong": True, "note": reply["note"].strip(), "panels": [], "caption_consistency": [],
                "summary": {"text": "", "panel_refs": []}}
    panels = reply.get("panels")
    if not isinstance(panels, list):
        raise ValueError("Reply panels must be a list")
    allowed_ids = set(payload["allowed_sentence_ids"])
    panel_numbers = set()
    for panel in panels:
        if not isinstance(panel, dict):
            raise ValueError("Each panel must be an object")
        values = panel.get("values")
        if not isinstance(values, list):
            raise ValueError("Each panel values field must be a list")
        for value in values:
            if not isinstance(value, dict):
                raise ValueError("Each panel value must be an object")
            quality = value.get("read_quality")
            if quality not in ALLOWED_READ_QUALITY:
                raise ValueError("read_quality must be exact, approximate, or uncertain")
            for key in ("value", "error", "n"):
                panel_numbers.update(_number_tokens(value.get(key)))
    checks = reply.get("caption_consistency")
    if not isinstance(checks, list):
        raise ValueError("caption_consistency must be a list")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each caption consistency item must be an object")
        sid = check.get("claim_sentence_id")
        if sid not in allowed_ids:
            raise ValueError(f"Unknown sentence ID in caption_consistency: {sid}")
        if check.get("status") not in ALLOWED_CONSISTENCY_STATUS:
            raise ValueError("caption_consistency status is invalid")
        note = check.get("note", "")
        if not isinstance(note, str):
            raise ValueError("caption_consistency note must be text")
        cited = next((row.get("text", "") for group in (
            payload["caption_sentences"], payload["mention_sentences"], payload["table_data_sentences"])
                      for row in group if row.get("sentence_id") == sid), "")
        available = _number_tokens(cited) | panel_numbers
        # A note may say "41.67" for a source "41.67%": compare the numbers, not the % sign.
        # Labels are not claims: "Figure 1", "panel 2", SSEA-3 / IL-6 / Tuj-1, time-point labels such as "2W".
        claims = _LABEL.sub(" ", note)
        missing = {token for token in _number_tokens(claims) if token[0] not in {n for n, _ in available}}
        if missing:
            shown = ", ".join(number + suffix for number, suffix in sorted(missing))
            raise ValueError(f"Invented number in caption_consistency note: {shown}")
    summary = reply.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("text"), str) or not isinstance(summary.get("panel_refs"), list):
        raise ValueError("summary must contain text and panel_refs")
    # FG-05: a number of the summary comes from a cited sentence or a panel, never from the pixels
    shown = [row.get("text", "") for key in ("caption_sentences", "mention_sentences", "table_data_sentences") for row in payload[key]]
    shown += [str(panel.get(key) or "") for panel in panels for key in ("measure", "unit", "groups")]
    shown += [str(v.get("group") or "") for panel in panels for v in panel["values"]]
    known = {n for text in shown for n, _ in _number_tokens(text)} | {n for n, _ in panel_numbers}
    missing = {n for n, _ in _number_tokens(_NAME_NUMBER.sub(" ", _LABEL.sub(" ", summary["text"])))} - known
    if missing:
        raise ValueError(f"Invented number in summary: {', '.join(sorted(missing))}")
    return reply


def _reading_path(paper_id, object_id):
    folder = _folder(paper_id)
    full = _full_object_id(paper_id, object_id)
    local = full.rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", local):
        raise ValueError("Unsafe media object ID")
    return folder / "figure_readings" / f"{local}.json"


def submit(paper_id, object_id, reply, analyst):
    """Validate and atomically store one current visual reading."""
    if not isinstance(analyst, str) or not analyst.strip():
        raise ValueError("Analyst is required")
    with workflow.writer_lock():
        prepared = task(paper_id, object_id)
        reply = _validate_reply(reply, prepared)
        report = {
            **reply,
            "schema_version": VERSION,
            "paper_id": prepared["paper_id"],
            "object_id": prepared["object_id"],
            "kind": prepared["kind"],
            "label": prepared["label"],
            "analyst": analyst,
            "timestamp": config.now(),
            "crop_sha256": prepared["crop_sha256"],
            "source_revision": prepared["source_revision"],
            "cited_text_sha256": prepared["cited_text_sha256"],
            "red_warning_candidate": any(
                item.get("status") == "inconsistent" for item in reply["caption_consistency"]
            ),
            "provenance": {
                "crop_path": prepared["crop_path"],
                "crop_sha256": prepared["crop_sha256"],
                "source_revision": prepared["source_revision"],
                "caption_sentence_ids": [row["sentence_id"] for row in prepared["caption_sentences"]],
                "mention_sentence_ids": [row["sentence_id"] for row in prepared["mention_sentences"]],
            },
        }
        workflow.atomic_json(_reading_path(paper_id, prepared["object_id"]), report)
    return {"ok": True, **report}


def _current_state(paper_id, object_id, saved):
    reasons = []
    try:
        bundle = _bundle(paper_id, object_id)
        folder = _folder(paper_id)
        crop = bundle.get("crop") or {}
        path = media.crop_path(folder, crop.get("path")) if crop.get("path") else None
        actual = media._hash(path) if path else None
        if actual != saved.get("crop_sha256"):
            reasons.append("crop changed or is unavailable")
        if saved.get("cited_text_sha256"):
            # Content-based: only the sentences the reading was shown (and the crop) can make it stale.
            if _cited_digest(bundle.get("caption") or [],
                             _mention_sentences(paper_id, bundle, _sentence_rows(folder))) != saved["cited_text_sha256"]:
                reasons.append("source caption or mention sentences changed")
        elif _revision(folder) != saved.get("source_revision"):
            reasons.append("source sentences or media records changed")
    except (OSError, TypeError, ValueError, KeyError):
        reasons.append("media object is no longer available")
    return reasons


def load(paper_id, object_id):
    """Load a reading and label it stale when its source or crop changed."""
    path = _reading_path(paper_id, object_id)
    if not path.is_file():
        return None
    value = db.read_json(path)
    if not isinstance(value, dict):
        raise ValueError("Stored figure reading is not an object")
    reasons = _current_state(paper_id, object_id, value)
    out = dict(value)
    out["stale"] = bool(reasons)
    out["status"] = "stale" if reasons else "current"
    out["stale_reasons"] = reasons
    return out


WARNING_LABEL = "⚠ Chú thích/văn bản không khớp hình (AI đọc ảnh)"


def warning(reading):
    """The researcher-facing warning of a reading ('' when none): caption/text vs figure mismatch or red warning.
    Kept as data everywhere (notes, dashboard, AI search); never hides or changes the source."""
    if not isinstance(reading, dict) or reading.get("crop_wrong") is True:
        return ""
    bad = [c for c in reading.get("caption_consistency") or [] if isinstance(c, dict) and c.get("status") == "inconsistent"]
    if not bad and not reading.get("red_warning_candidate"):
        return ""
    notes = "; ".join(str(c.get("note") or "").strip() for c in bad if str(c.get("note") or "").strip())
    return f"{WARNING_LABEL}: {notes}" if notes else WARNING_LABEL


_SENTENCE_WARNINGS = {}


def sentence_warnings(paper_id):
    """{sentence_id: 'Figure 3: <warning>'} for sentences an AI reading flagged as not matching the figure/table."""
    folder = _folder(paper_id) / "figure_readings"
    stamp = tuple(sorted((p.name, p.stat().st_mtime_ns) for p in folder.glob("*.json"))) if folder.is_dir() else ()
    cached = _SENTENCE_WARNINGS.get(paper_id)
    if cached and cached[0] == stamp:
        return cached[1]
    out = {}
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        reading = db.read_json(path, {}) or {}
        if not isinstance(reading, dict) or reading.get("crop_wrong") is True:
            continue
        for check in reading.get("caption_consistency") or []:
            if isinstance(check, dict) and check.get("status") == "inconsistent" and check.get("claim_sentence_id"):
                out[check["claim_sentence_id"]] = (f"{reading.get('label') or path.stem}: {WARNING_LABEL} — "
                                                   f"{str(check.get('note') or '').strip()}").rstrip(" —")
    _SENTENCE_WARNINGS[paper_id] = (stamp, out)
    return out


def readings(paper_id):
    """All stored readings of the paper (dicts, no staleness label)."""
    folder = _folder(paper_id) / "figure_readings"
    return [r for r in (db.read_json(p, {}) for p in sorted(folder.glob("*.json"))) if isinstance(r, dict)] if folder.is_dir() else []


def unit_warnings(readings, sentences):
    """{sentence_id: '<label>: <warning>'} for the sentences an evidence unit must not cite without a figure flag (EV-20):
    the claim sentence of every inconsistent caption check and every sentence of the warned object (claim sentences win).
    One definition for the validator and the conform check."""
    out = {}
    for reading in readings:
        text = warning(reading)
        if not text:
            continue
        text = f"{reading.get('label') or reading.get('object_id')}: {text}"
        for c in reading.get("caption_consistency") or ():
            if isinstance(c, dict) and c.get("status") == "inconsistent" and c.get("claim_sentence_id"):
                out[c["claim_sentence_id"]] = text
        for s in sentences:
            if reading.get("object_id") and s.get("object_id") == reading["object_id"]:  # no id: not every id-less sentence
                out.setdefault(s["sentence_id"], text)
    return out


def show(paper_id, object_id):
    """Return a human-readable reading with its mandatory provenance header."""
    value = load(paper_id, object_id)
    if value is None:
        raise FileNotFoundError(f"No figure reading for {paper_id}:{object_id}")
    header = f"figure_read by {value.get('analyst', 'unknown')} — machine reading of an image; verify against the original"
    return header + "\n" + json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)


def _section_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").lower()).strip("_")


def _object_local_id(bundle):
    return str(bundle.get("object_id") or "").rsplit(":", 1)[-1]


def _is_supplementary(bundle):
    local = _object_local_id(bundle).upper()
    if re.match(r"^[FT]S\d", local):
        return True
    label = str(bundle.get("label") or "")
    if re.search(r"\bsupp(?:lementary)?\b", label, re.I) or re.search(
            r"\b(?:fig(?:ure)?|table)\s*\.?\s*S\d", label, re.I):
        return True
    source = (bundle.get("source_locator") or {}).get("source")
    return isinstance(source, str) and source.casefold().startswith("supp")


def _is_main_object(bundle):
    local = _object_local_id(bundle).upper()
    if re.match(r"^[FT]\d", local):
        return True
    label = str(bundle.get("label") or "")
    return bool(re.search(r"\b(?:fig(?:ure)?|table)\s*\.?\s*\d", label, re.I))


def _label_patterns(bundle):
    """Build conservative text patterns for an object's non-caption mentions."""
    values = []
    for value in (bundle.get("label"), _object_local_id(bundle)):
        value = str(value or "").strip()
        if value and value not in values:
            values.append(value)
    patterns = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(value) + r"(?![A-Za-z0-9])", re.I)
                for value in values]
    local = _object_local_id(bundle)
    match = re.fullmatch(r"([FT])(S?)(\d+[A-Za-z]?)", local, re.I)
    if match:
        number = match.group(3)
        supplement = match.group(2).upper() == "S"
        prefix = r"(?:supplementary\s+)?" if supplement else r""
        suffix = r"\s*S" if supplement else r""
        patterns.append(re.compile(
            r"(?<![A-Za-z0-9])" + prefix + r"(?:fig(?:ure)?|table)\s*\.?\s*" + suffix +
            re.escape(number) + r"(?![A-Za-z0-9])", re.I))
        # A supplementary caption is sometimes written as "Figure S1" without
        # the word supplementary.
        if supplement:
            patterns.append(re.compile(
                r"(?<![A-Za-z0-9])(?:fig(?:ure)?|table)\s*\.?\s*S" +
                re.escape(number) + r"(?![A-Za-z0-9])", re.I))
    return patterns


def _mention_rows(bundle, rows):
    """Return source sentences that mention an object, excluding its caption."""
    by_id = {row.get("sentence_id"): row for row in rows if row.get("sentence_id")}
    selected = {}
    object_id = bundle.get("object_id")
    for row in bundle.get("mentions") or []:
        sid = row.get("sentence_id")
        if sid and row.get("kind") not in {"caption", "figure_caption", "table_caption", "table_row"}:
            selected[sid] = by_id.get(sid, row)
    patterns = _label_patterns(bundle)
    for row in rows:
        if row.get("kind") in {"caption", "figure_caption", "table_caption", "table_row"}:
            continue
        if object_id in (row.get("mentions") or []):
            selected[row["sentence_id"]] = row
            continue
        text = str(row.get("text") or "")
        if patterns and any(pattern.search(text) for pattern in patterns):
            selected[row["sentence_id"]] = row
    return sorted(selected.values(), key=lambda row: (row.get("seq", 10**9), row.get("sentence_id", "")))


def _page_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _caption_page(bundle):
    locator = bundle.get("source_locator") or {}
    page = _page_number(locator.get("page"))
    if page is not None:
        return page
    for row in (bundle.get("caption") or []) + (bundle.get("data") or []):
        page = _page_number(row.get("page"))
        if page is not None:
            return page
    return None


def _section_from_page(folder, bundle):
    page = _caption_page(bundle)
    sections = db.read_json(folder / "sections.json", [])
    candidates = []
    if page is not None and isinstance(sections, list):
        for section in sections:
            start = _page_number(section.get("page_start")) if isinstance(section, dict) else None
            name = section.get("name") if isinstance(section, dict) else None
            if name and start is not None and start <= page:
                candidates.append((start, name))
    if candidates:
        return max(candidates, key=lambda item: item[0])[1]
    return None


def _source_section(paper_id, bundle, rows, mention_rows=None):
    """Resolve section from object mentions, then caption page position."""
    mention_rows = _mention_rows(bundle, rows) if mention_rows is None else mention_rows
    result_mentions = [row for row in mention_rows
                       if _section_key(row.get("section")) in _RESULT_SECTIONS]
    chosen = (result_mentions or mention_rows)
    if chosen:
        section = chosen[0].get("section")
        if section:
            return section
    folder = _folder(paper_id)
    section = _section_from_page(folder, bundle)
    if section:
        return section
    # A small compatibility fallback for fixtures/old papers without sections.json.
    caption_ids = {row.get("sentence_id") for row in bundle.get("caption") or []}
    structure = db.read_json(folder / "structure.json", {})
    if isinstance(structure, dict) and isinstance(structure.get("sections"), dict):
        for name, ids in structure["sections"].items():
            if caption_ids.intersection(ids or []):
                return name
    for row in bundle.get("caption") or []:
        if row.get("section"):
            return row["section"]
    return None


def _object_label_key(bundle):
    label = str(bundle.get("label") or _object_local_id(bundle)).strip()
    numbers = tuple(int(value) for value in re.findall(r"\d+", label))
    return (numbers or (10**9,), label.casefold(), _object_local_id(bundle).casefold())


def _priority(bundle, mention_rows=None):
    kind = bundle.get("kind")
    if kind in _FORMULA_KINDS:
        return 4
    if kind not in _OBJECT_KINDS:
        return 4
    if _is_supplementary(bundle):
        return 2
    if not _is_main_object(bundle):
        return 3
    mention_rows = mention_rows or bundle.get("mentions") or []
    if any(row.get("section") and _section_key(row.get("section")) in _RESULT_SECTIONS
           for row in mention_rows):
        return 0
    return 1 if mention_rows else 3


def pending(con, vault, include_formulas=False):
    """List current-vault objects with a verified crop and no current reading."""
    if con is None or not isinstance(vault, str) or not vault:
        raise ValueError("A database connection and vault are required")
    paper_ids = [row[0] for row in con.execute(
        "SELECT paper_id FROM paper_vaults WHERE vault=? ORDER BY paper_id", (vault,)
    ).fetchall()]
    found = []
    paper_order = {paper_id: index for index, paper_id in enumerate(paper_ids)}
    for paper_id in paper_ids:
        try:
            bundles = media.bundles(paper_id)
        except (OSError, TypeError, ValueError, KeyError):
            continue
        try:
            sentence_rows = _sentence_rows(_folder(paper_id))
        except (OSError, TypeError, ValueError, KeyError):
            sentence_rows = []
        for bundle in bundles:
            kind = bundle.get("kind")
            if kind not in _OBJECT_KINDS and not (include_formulas and kind in _FORMULA_KINDS):
                continue
            try:
                crop_path, crop_sha256 = _verified_crop(paper_id, bundle)
            except (OSError, TypeError, ValueError, KeyError):
                continue
            try:
                saved = load(paper_id, bundle["object_id"])
            except (OSError, TypeError, ValueError):
                saved = None
            if saved is not None and not saved.get("stale"):
                continue
            mention_rows = _mention_rows(bundle, sentence_rows)
            rows = (bundle.get("caption") or []) + mention_rows + (bundle.get("data") or [])
            first_seq = min((row.get("seq", 10**9) for row in rows), default=10**9)
            found.append({
                "paper_id": paper_id,
                "object_id": bundle["object_id"],
                "label": bundle.get("label"),
                "kind": bundle.get("kind"),
                "priority": _priority(bundle, mention_rows),
                "source_section": _source_section(paper_id, bundle, sentence_rows, mention_rows),
                "crop_path": str(crop_path),
                "crop_sha256": crop_sha256,
                "source_revision": _revision(_folder(paper_id)),
                "first_sentence_seq": first_seq,
                "_paper_order": paper_order[paper_id],
                "_label_order": _object_label_key(bundle),
            })
    ordered = sorted(found, key=lambda row: (row["priority"], row["_paper_order"],
                                              row["_label_order"], row["object_id"]))
    for row in ordered:
        row.pop("_paper_order", None)
        row.pop("_label_order", None)
    return ordered


def _reply_folder(paper_id, object_id):
    full = _full_object_id(paper_id, object_id)
    local = full.rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", local):
        raise ValueError("Unsafe media object ID")
    paper = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(paper_id)).strip("._") or "paper"
    folder = config.STORE / "ai_replies" / "figure_reading" / paper / local
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _reply_files(folder):
    files = []
    for path in folder.glob("*.json"):
        try:
            int(path.stem)
        except ValueError:
            continue
        files.append(path)
    return sorted(files, key=lambda path: int(path.stem))


def _saved_reply(folder, payload):
    """Return the newest unprocessed reply that is current and locally valid."""
    for path in reversed(_reply_files(folder)):
        saved = db.read_json(path)
        if not (isinstance(saved, dict) and saved.get("processed") is not True
                and isinstance(saved.get("raw"), str)):
            continue
        if (saved.get("source_revision") is not None
                and saved.get("source_revision") != payload.get("source_revision")):
            continue
        if (saved.get("crop_sha256") is not None
                and saved.get("crop_sha256") != payload.get("crop_sha256")):
            continue
        try:
            reply = answer._json(saved["raw"])
            if not isinstance(reply, dict):
                continue
            _validate_reply(reply, payload)
        except (answer.AIError, ValueError, TypeError, KeyError, IndexError):
            continue
        return path, saved
    return None, None


def _save_reply(folder, raw, usage, attempt, provider, payload):
    files = _reply_files(folder)
    number = (int(files[-1].stem) + 1) if files else 1
    path = folder / f"{number}.json"
    analysis_transport.write_atomic(path, {"raw": raw, "usage": dict(usage or {}), "attempt": attempt,
                                           "provider": provider, "time": config.now(), "processed": False,
                                           "source_revision": payload.get("source_revision"),
                                           "crop_sha256": payload.get("crop_sha256")})
    return path


def _mark_reply_processed(path, result=None):
    saved = db.read_json(path, {}) or {}
    saved["processed"] = True
    if result is not None:
        saved["submit_status"] = result.get("status", "accepted" if result.get("ok") else "needs_fix")
        saved["submit_ok"] = bool(result.get("ok"))
    analysis_transport.write_atomic(path, saved)


def _mark_reply_blocked(path, error):
    """Record the lock outcome without consuming the reusable raw reply."""
    saved = db.read_json(path, {}) or {}
    saved["status"] = "blocked_by_writer"
    saved["submit_status"] = "blocked_by_writer"
    saved["error"] = str(error)
    saved["processed"] = False
    analysis_transport.write_atomic(path, saved)


def _run_user(payload, previous=None, problems=None):
    request = {"task": payload, "reply_schema": payload.get("reply_format"),
               "instructions": payload.get("instructions"), "json_only": True,
               "image": {"path": payload["crop_path"], "sha256": payload["crop_sha256"]}}
    if previous is not None or problems:
        request["repair"] = {"previous_reply": previous, "problems": problems or []}
        request["repair_instructions"] = (
            "Return a complete corrected reply, not a patch. Preserve valid panel readings and fix every listed problem."
        )
    return analysis_transport.dumps(request)


def _analyst(settings):
    return f"{settings['provider']}/{answer.model_of(settings) or 'default'}"


def run(paper_id, object_id, provider=None, model=None, reasoning=None, max_attempts=3, timeout=None):
    """Run a configured vision provider for one verified figure/table crop."""
    if not isinstance(max_attempts, int) or max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    started = time.monotonic()
    payload = task(paper_id, object_id)
    crop = Path(payload["crop_path"])
    if not crop.is_file() or media._hash(crop) != payload["crop_sha256"]:
        raise ValueError("Crop SHA256 mismatch; refusing to send image")
    folder = _reply_folder(paper_id, object_id)
    settings = analysis_transport.provider_settings(provider, model, reasoning, timeout, task="figure_reading")
    analyst = _analyst(settings)
    system = ("You are completing a source-grounded biomedical figure/table reading. Return JSON only. "
              "Read the attached crop and use only the supplied source sentences.\n" +
              analysis_transport.dumps(payload.get("reply_format")))
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
                raw, usage = analysis_transport.call(settings, system, _run_user(payload, previous, problems),
                                                     image_paths=[crop])
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
            if not isinstance(reply, dict):
                raise ValueError("AI reply must be a JSON object")
            result = submit(paper_id, object_id, reply, analyst)
        except workflow.WriterBusy as exc:
            _mark_reply_blocked(reply_path, exc)
            return {"status": "blocked_by_writer", "attempts": attempts, "tokens": usage_total,
                    "error": str(exc), "seconds": round(time.monotonic() - started, 3)}
        except Exception as exc:
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
            return {**result, "status": "accepted", "attempts": attempts, "tokens": usage_total,
                    "seconds": round(time.monotonic() - started, 3)}
        problems = result.get("problems") or [{"code": "validation", "detail": "Reply was rejected"}]
        previous = reply
        if attempts >= max_attempts:
            return {**result, "attempts": attempts, "tokens": usage_total,
                    "seconds": round(time.monotonic() - started, 3)}
    return {"status": "needs_fix", "attempts": attempts, "tokens": usage_total,
            "problems": problems, "seconds": round(time.monotonic() - started, 3)}


def _run_log(row):
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    with (config.LOGS_DIR / "figure_reading_runs.jsonl").open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(analysis_transport.dumps(row) + "\n")


def run_pending(con, vault, limit=None, provider=None, model=None, reasoning=None, max_attempts=3, timeout=None):
    """Run pending verified objects in :func:`pending` order."""
    rows = pending(con, vault)
    if limit is not None:
        if not isinstance(limit, int) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        rows = rows[:limit]
    results = []
    for row in rows:
        started = time.monotonic()
        try:
            result = run(row["paper_id"], row["object_id"], provider, model, reasoning, max_attempts, timeout)
        except Exception as exc:
            result = {"status": "failed", "attempts": 0, "tokens": {},
                      "error": f"{type(exc).__name__}: {exc}", "seconds": round(time.monotonic() - started, 3)}
        audit = {"paper_id": row["paper_id"], "object_id": row["object_id"], "status": result.get("status"),
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


def _read_reply(path):
    if path == "-":
        return json.load(sys.stdin)
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Prepare and validate source-bound figure/table readings")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("task", "show"):
        command = sub.add_parser(name)
        command.add_argument("paper_id")
        command.add_argument("object_id")
    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("paper_id")
    submit_parser.add_argument("object_id")
    submit_parser.add_argument("reply_json", help="JSON file, or - for stdin")
    submit_parser.add_argument("--analyst", required=True)
    pending_parser = sub.add_parser("pending")
    pending_parser.add_argument("--vault", default=None)
    pending_parser.add_argument("--include-formulas", action="store_true",
                                help="Include formula/special-symbol crops in a separate explicit pass")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("paper_id")
    run_parser.add_argument("object_id")
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
    args = parser.parse_args(argv)
    if args.command == "task":
        print(json.dumps(task(args.paper_id, args.object_id), ensure_ascii=False, indent=2))
    elif args.command == "show":
        print(show(args.paper_id, args.object_id))
    elif args.command == "submit":
        print(json.dumps(submit(args.paper_id, args.object_id, _read_reply(args.reply_json), args.analyst),
                         ensure_ascii=False, indent=2))
    elif args.command == "pending":
        con = db.connect()
        try:
            vault = args.vault or config.default_vault(config.load())
            print(json.dumps(pending(con, vault, args.include_formulas), ensure_ascii=False, indent=2))
        finally:
            con.close()
    elif args.command == "run":
        print(json.dumps(run(args.paper_id, args.object_id, args.provider, args.model, args.reasoning,
                             args.max_attempts, args.timeout), ensure_ascii=False, indent=2))
    else:
        con = db.connect()
        try:
            print(json.dumps(run_pending(con, args.vault, args.limit, args.provider, args.model, args.reasoning,
                                         args.max_attempts, args.timeout), ensure_ascii=False, indent=2))
        finally:
            con.close()
    return 0


if __name__ == "__main__":
    main()
