"""Source-region media bundles. No AI, transcription repair or sentence renumbering.

Call build once per paper under the existing library writer guard. bundles is read-only
and can select related objects by object, sentence or evidence-unit IDs. PDF regions use
unrotated PyMuPDF page coordinates; crops are lossless source pixels, not recreated data.
"""
from contextlib import ExitStack, nullcontext
import hashlib
import json
import logging
import math
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import tempfile
import unicodedata
import uuid

import pymupdf

from . import config, db, extract, objects, workflow

VERSION = 1
READ_VERSIONS = {VERSION, 2}
DPI = 110
INPUTS = ("metadata.json", "sentences.jsonl", "figures.jsonl", "tables.jsonl",
          "evidence.jsonl", "media_regions.json")
KINDS = {"formula", "special_symbol_region"}
SCOPE_NOTE = ("Source-region inventory, not complete media/formula detection or scientific "
              "acceptance. Never infer numbers from chart pixels; verify uncertain table rows "
              "and formula/symbol candidates against the original. A source_page_preview "
              "shows only the source/caption page: object graphics have not been located "
              "and may be absent from the original. An automatically located object_region "
              "(locator_basis same_page_caption or manuscript_figure_page_*) is a layout reading "
              "to verify against the original; an unlocated object on a text-only caption page "
              "had no graphic found in this source PDF.")

# Read routes may inspect the same source PDF many times while a node is being
# assembled.  Keep the digest keyed by the cheap filesystem identity so a
# changed file is still rechecked without hashing an unchanged PDF repeatedly.
_HASH_CACHE = {}
_READ_FALLBACK_LOGGED = set()
_LOGGER = logging.getLogger(__name__)


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     allow_nan=False, separators=(",", ":")).encode()).hexdigest()


def _hash(path):
    path = Path(path)
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)), stat.st_size)
    cached = _HASH_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        with path.open("rb") as stream:
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
            value = h.hexdigest()
            _HASH_CACHE[key] = value
            return value
    except OSError:
        return None


def _stat(path):
    try:
        value = Path(path).stat()
    except OSError:
        return None
    return {"mtime_ns": getattr(value, "st_mtime_ns", int(value.st_mtime * 1e9)),
            "size": value.st_size}


def _input_stats(folder, sources):
    stats = {name: _stat(folder / name) for name in INPUTS}
    stats.update({"media.py": _stat(Path(__file__)),
                 "extract.py": _stat(Path(extract.__file__)),
                 "objects.py": _stat(Path(objects.__file__))})
    return {"inputs": stats,
            "sources": {name: _stat(row.get("_path")) if row.get("_path") else None
                        for name, row in sources.items()}}


def _data_input_stats(value):
    """Drop implementation fingerprints from the read-time stat contract."""
    if not isinstance(value, dict):
        return None
    inputs = value.get("inputs")
    sources = value.get("sources")
    if not isinstance(inputs, dict) or not isinstance(sources, dict):
        return None
    return {"inputs": {name: inputs.get(name) for name in INPUTS},
            "sources": sources}


def _manifest_fast_current(folder, manifest):
    """Return True/False from persisted data inputs, or None for old manifests."""
    expected = manifest.get("input_stats")
    if not isinstance(expected, dict):
        expected_hashes = manifest.get("input_hashes")
        if not isinstance(expected_hashes, dict):
            return None
        try:
            meta = db.read_json(folder / "metadata.json", {}) or {}
            sources = _sources(meta, folder)
            current_hashes = {name: _hash(folder / name) for name in INPUTS}
            expected_data = {name: expected_hashes.get(name) for name in INPUTS}
            public_sources = {key: {k: v for k, v in row.items() if not k.startswith("_")}
                              for key, row in sources.items()}
            return current_hashes == expected_data and public_sources == (manifest.get("sources") or {})
        except (OSError, TypeError, ValueError):
            return False
    current = _input_stats(folder, {
        name: {"_path": (config.ROOT / row["path"] if row.get("path") and not Path(row["path"]).is_absolute()
                           else Path(row["path"])) if row.get("path") else None}
        for name, row in (manifest.get("sources") or {}).items()
        if isinstance(row, dict)})
    return _data_input_stats(current) == _data_input_stats(expected)


def _bundle_read_issue(folder, bundle):
    """Return why a persisted bundle cannot satisfy the normal read shape."""
    for key in ('caption', 'data', 'mentions', 'anchors', 'associated_units'):
        if not isinstance(bundle.get(key), list):
            return f"missing {key} list"
    if not isinstance(bundle.get('source_locator'), dict) or not isinstance(bundle.get('crop'), dict):
        return "missing locator or crop"
    crop = bundle.get("crop") or {}
    crop_path_value = crop.get("path")
    if crop_path_value and crop.get("status") in {"available", "retained"}:
        try:
            if not crop_path(folder, crop_path_value).is_file():
                return "crop file unavailable"
        except (OSError, TypeError, ValueError):
            return "crop path unavailable"
    if bundle.get("kind") == "table":
        table = bundle.get("table")
        if (not isinstance(table, dict) or "header" not in table or "rows" not in table
                or (table.get("rows") and "data" not in bundle)):
            return "table cells unavailable"
    stored_caption_ids = set(bundle.get("stored_caption_sentence_ids") or [])
    if stored_caption_ids:
        caption_ids = {row.get("sentence_id") for row in bundle.get("caption") or []}
        if not stored_caption_ids.issubset(caption_ids):
            return "caption rows unavailable"
    return None


def _manifest_needs_legacy_read(folder, manifest, object_ids=None, sentence_ids=None, unit_ids=None):
    return _manifest_read_issue(folder, manifest, object_ids, sentence_ids, unit_ids) is not None


def _manifest_read_issue(folder, manifest, object_ids=None, sentence_ids=None, unit_ids=None):
    bundles = _select_bundles(manifest, object_ids, sentence_ids, unit_ids)
    for bundle in bundles:
        reason = _bundle_read_issue(folder, bundle)
        if reason:
            return reason
    return None


def _log_read_fallback(paper_id, reason):
    if paper_id in _READ_FALLBACK_LOGGED:
        return
    _READ_FALLBACK_LOGGED.add(paper_id)
    _LOGGER.info("media read fallback paper=%s reason=%s", paper_id, reason)


def _folder(paper_id):
    if not isinstance(paper_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", paper_id):
        raise ValueError("Invalid paper ID")
    root = config.PAPERS_DIR.resolve()
    folder = db.paper_dir(paper_id).resolve()
    if not folder.is_relative_to(root):
        raise ValueError("Paper folder escapes store")
    return folder


def crop_path(folder, relative):
    """Only local, contained crop paths; reject Windows paths even on other OSes."""
    if not isinstance(relative, str) or "\\" in relative or ":" in relative:
        raise ValueError("Invalid crop path")
    path = Path(relative)
    if path.is_absolute() or PureWindowsPath(relative).is_absolute() or ".." in path.parts or path.parts[:1] != ("crops",):
        raise ValueError("Crop path must stay under paper crops")
    target = (folder / path).resolve()
    if not target.is_relative_to((folder / "crops").resolve()) or not target.is_relative_to(folder.resolve()):
        raise ValueError("Crop path escapes paper")
    return target


def _read_manifest(path):
    try:
        value = db.read_json(path)
        payload = {k: v for k, v in value.items() if k not in ("output_sha256", "cache_hit")}
        if value.get("version") in READ_VERSIONS and _digest(payload) == value.get("output_sha256"):
            return value
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return None


def _sources(meta, folder):
    out = {}
    for row in meta.get("source_files", []):
        source = "main" if row.get("role") == "main" else row.get("supp_id")
        if not source or source in out:
            continue
        locator = row.get("path")
        path = Path(locator) if locator else None
        if path is not None and not path.is_absolute():
            path = config.ROOT / path
        actual = _hash(path) if path is not None else None
        expected = row.get("sha256")
        out[source] = {"source": source, "path": locator, "sha256": actual,
                       "recorded_sha256": expected,
                       "status": "unavailable" if actual is None else
                       "source_changed" if expected and expected != actual else
                       "available" if path.suffix.lower() == ".pdf" else "non_pdf",
                       "_path": path}
    return out


def _signature(folder, sources, regions, detect):
    hashes = {name: _hash(folder / name) for name in INPUTS}
    hashes["media.py"] = _hash(Path(__file__))
    hashes["extract.py"] = _hash(Path(extract.__file__))
    hashes["objects.py"] = _hash(Path(objects.__file__))
    hashes["pymupdf"] = pymupdf.VersionBind
    public = {key: {k: v for k, v in row.items() if not k.startswith("_")}
              for key, row in sources.items()}
    return _digest({"version": VERSION, "dpi": DPI, "inputs": hashes, "sources": public,
                    "regions": regions, "detect": detect}), hashes, public


def _rect(page, number, bbox):
    if isinstance(number, bool) or not isinstance(number, int) or number < 1:
        raise ValueError("Page must be a 1-based integer")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(
            isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in bbox):
        raise ValueError("Invalid region bbox")
    rect = pymupdf.Rect(bbox)
    # Text coordinates are unrotated and relative to the visible CropBox origin.
    bounds = pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
    if rect.is_empty or rect.is_infinite or not bounds.contains(rect):
        raise ValueError("Region bbox is outside the page")
    return rect


def _candidate_regions(doc, source):
    """Conservative line candidates only; omissions and false positives need manual QA."""
    for page in doc:
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                spans = line.get("spans", [])
                text = "".join(span.get("text", "") for span in spans)
                broken = any(c == "\ufffd" or (ord(c) < 32 and not c.isspace()) for c in text)
                symbol_font = any(re.search(r"Symbol|Math", span.get("font", ""), re.I) for span in spans)
                unmapped_font = any(extract.unmapped_advent_glyphs(span.get("text", ""), span.get("font", "")) for span in spans)
                mathematical = bool(re.search(r"[\u2200-\u22ff]", text) or
                                    (len(text) <= 160 and re.search(r"\b[A-Za-z]\w*\s*=\s*[-+\d(A-Za-z]", text)))
                if broken or unmapped_font or symbol_font or mathematical:
                    try:
                        _rect(page, page.number + 1, line["bbox"])
                    except ValueError:  # off-page PDF text is not a safe render target
                        continue
                    yield {"source": source, "page": page.number + 1, "bbox": list(line["bbox"]),
                           "kind": "special_symbol_region" if broken or unmapped_font else "formula",
                           "detection": "automatic_candidate", "optional_text": text,
                           "text_status": "unverified_extracted"}


def _refs(ids, sentences, paper_id, source=None, strict=False):
    out, missing = [], []
    for sid in dict.fromkeys(ids or []):
        if not isinstance(sid, str):
            raise ValueError("Sentence IDs must be strings")
        sid = sid if sid.startswith(paper_id + ":") else f"{paper_id}:{sid}"
        row = sentences.get(sid)
        if row is None or (source is not None and row.get("source", "main") != source):
            missing.append(sid)
        else:
            out.append(dict(row))  # exact source text, citations/qualifiers and context IDs
    if strict and missing:
        raise ValueError("Unknown or wrong-source sentence IDs: " + ", ".join(missing))
    return out, missing


def _old_crop(folder, old, fallback=None):
    crop = old.get("crop") if old else None
    if crop and crop.get("path"):
        target = crop_path(folder, crop["path"])
        actual = _hash(target)
        if actual and actual == crop.get("sha256"):
            return {**crop, "status": "retained"}
        return {**crop, "status": "missing" if actual is None else "corrupt", "actual_sha256": actual}
    if fallback:
        target = crop_path(folder, fallback)
        actual = _hash(target)
        if actual:
            try:
                pix = pymupdf.Pixmap(str(target))
                if pix.width and pix.height:
                    return {"path": fallback, "sha256": actual, "status": "retained_unverified",
                            "source_sha256": None}
            except (RuntimeError, ValueError, OSError, pymupdf.mupdf.FzErrorBase):
                return {"path": fallback, "sha256": actual, "source_sha256": None, "status": "corrupt"}
    return {"path": None, "sha256": None, "source_sha256": None, "status": "unavailable"}


# ---- D3.70 graphic and caption locator --------------------------------------------------
# Heuristic layout reading, not detection certainty: located regions keep
# region_completeness=not_assessed and record locator_basis.
GRAPHIC_PAGE_SHARE = 0.08  # a manuscript figure page: graphics cover >= this share of the page
_BARE_LABEL = re.compile(r"(?i)(?:supplementa(?:ry|l)\s+)?(?:fig(?:ure)?s?\.?|table)\s*S?\d+[A-Za-z]?\s*[.:]?")


def _norm(text):
    return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", text or "").casefold())


def _object_key(oid, source):
    m = re.search(r":([FT])(S?)(\d+)$", oid)
    return ("table" if m[1] == "T" else "figure", bool(m[2]) or source != "main", int(m[3])) if m else None


def _label_key(text, source):
    lab = objects.label_of(text) if extract._CAPTION.match(text or "") else None
    return (lab[0], lab[1] or source != "main", lab[2]) if lab else None


def _page_lines(page):
    """Horizontal text lines in y order; rotated watermarks and side text are left out."""
    out = []
    for block in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]:
        paragraph = objects._is_paragraph(block)
        for line in block.get("lines", []):
            spans = [s for s in line.get("spans", []) if s.get("text")]
            if spans and abs(line["dir"][1]) < 0.1:
                out.append({"rect": pymupdf.Rect(line["bbox"]), "size": max(s["size"] for s in spans),
                            "text": extract.clean_spaces(extract.line_text(line)), "paragraph": paragraph})
    return sorted(out, key=lambda l: (l["rect"].y0, l["rect"].x0))


def _merge(rects, pad=6):
    rects = [pymupdf.Rect(r) for r in rects]
    changed = True
    while changed:  # ponytail: quadratic merge, fine for the few figure clusters per page
        changed = False
        for i, a in enumerate(rects):
            for j in range(i + 1, len(rects)):
                if (a + (-pad, -pad, pad, pad)).intersects(rects[j]):
                    rects[i] = a | rects.pop(j)
                    changed = True
                    break
            if changed:
                break
    return rects


def _same_row(a, b):
    return min(a.y1, b.y1) - max(a.y0, b.y0) > 0.5 * min(a.height, b.height)


def _caption_lines(lines, start, source, table, height):
    """The label line, lines on its row, and continuation lines of one caption on one page.

    A caption ends at a blank line, the next caption label, a change of font size, a vertical
    gap clearly wider than its own line pitch, or (tables) a row of separate cells.
    Also returns whether only blank lines or page-foot furniture follow it in its column."""
    size = start["size"]
    out = [start]
    for line in lines:  # "Figure S1" | "DNA replication…" printed as two lines on one row
        if line is not start and line["text"] and _same_row(line["rect"], start["rect"]) \
                and 0 <= line["rect"].x0 - out[-1]["rect"].x1 < 1.2 * size and abs(line["size"] - size) <= 1:
            out.append(line)
    size = max(l["size"] for l in out)
    band = pymupdf.Rect(out[0]["rect"])
    for line in out:
        band |= line["rect"]
    pitch = None
    while True:
        prev = out[-1]["rect"]
        below = [l for l in lines if l["rect"].y0 >= prev.y1 - 2 and all(l is not o for o in out)
                 and l["rect"].x0 < band.x1 and l["rect"].x1 > band.x0]
        nxt = min(below, key=lambda l: l["rect"].y0, default=None)
        at_end = not any(l["text"] and l["rect"].y0 < 0.9 * height for l in below)
        if nxt is None:
            return out, True
        gap = nxt["rect"].y0 - prev.y1
        limit = 1.5 * size if pitch is None else max(1.5 * pitch, pitch + 3)
        crowded = table and any(l is not nxt and l["text"] and _same_row(l["rect"], nxt["rect"])
                                and l["rect"].x0 < band.x1 and l["rect"].x1 > band.x0 for l in lines)
        if not nxt["text"] or gap > limit or abs(nxt["size"] - size) > 0.6 or crowded \
                or _label_key(nxt["text"], source):
            return out, at_end
        pitch = gap if pitch is None else pitch
        out.append(nxt)
        band.x0, band.x1 = min(band.x0, nxt["rect"].x0), max(band.x1, nxt["rect"].x1)


def _caption_region(doc, page_no, key, source, first_text, cache):
    """{page: [lines]} of one caption; continues onto the next page only when the text clearly
    runs on (no final full stop, next page starts in lower case at the same font size)."""
    lines = cache(page_no)
    starts = [l for l in lines if _label_key(l["text"], source) == key]
    if not starts:
        return None
    head = _norm(first_text)

    def agrees(line):
        text = _norm(line["text"])
        n = min(len(text), len(head), 16)
        return n and text[:n] == head[:n]
    start = next((l for l in starts if agrees(l)), starts[0])
    height = doc[page_no - 1].rect.height
    found, at_end = _caption_lines(lines, start, source, key[0] == "table", height)
    region = {page_no: found}
    last = found[-1]
    if at_end and page_no < len(doc) and not re.search(r"[.!?:)\]]$", last["text"]):
        size = start["size"]
        following = [l for l in cache(page_no + 1) if l["text"] and abs(l["size"] - size) <= 0.6
                     and l["rect"].x0 < last["rect"].x1 + 20 and l["rect"].x1 > start["rect"].x0 - 20]
        if following and following[0]["text"][:1].islower():
            region[page_no + 1], _ = _caption_lines(cache(page_no + 1), following[0], source, key[0] == "table",
                                                    doc[page_no].rect.height)
    return region


def _caption_rows(captions, ordered, region):
    """Stored caption rows, then the source rows that follow them in reading order while their
    text lies inside the PDF caption region. Body sentences after the caption never qualify."""
    if not captions or region is None:
        return captions
    text = _norm(" ".join(l["text"] for lines in region.values() for l in lines))
    cursor = 0
    for row in captions:  # the stored rows must lie in the region, in order
        pos = text.find(_norm(row.get("text")), cursor)
        if pos < 0:
            return captions
        cursor = pos + len(_norm(row.get("text")))
    out = list(captions)
    taken = {r["sentence_id"] for r in captions}
    last = max(r.get("seq", 0) for r in captions)
    for row in ordered:
        if row.get("seq", 0) <= last or row["sentence_id"] in taken:
            continue
        if row.get("kind") == "table_row" or (row.get("kind") == "caption" and _label_key(row.get("text"), "main")):
            break
        key = _norm(row.get("text"))
        if not key:
            continue
        pos = text.find(key, cursor)
        # Each added row must start where the previous one ended (a few marks of slack).
        if row.get("page") not in region or pos < 0 or pos > cursor + 3:
            break
        cursor = pos + len(key)
        out.append(dict(row))
    return out


def _graphics(page):
    return _merge(objects._graphics(page))


def _distance(a, b):
    return math.hypot(max(0, a.x0 - b.x1, b.x0 - a.x1), max(0, a.y0 - b.y1, b.y0 - a.y1))


FIGURE_NEIGHBOR_GAP = 12.0


def _caption_bbox(lines):
    if not lines:
        return None
    rect = pymupdf.Rect(lines[0]["rect"])
    for line in lines[1:]:
        rect |= line["rect"]
    return rect


def _safe_rect(value):
    try:
        rect = pymupdf.Rect(value)
    except (TypeError, ValueError, RuntimeError):
        return None
    return None if rect.is_infinite else rect


def _figure_drawings(page):
    """Return drawable vector bounds without treating malformed paths as regions."""
    try:
        drawings = page.get_drawings()
    except Exception:
        return []
    out = []
    for drawing in drawings:
        rect = _safe_rect(drawing.get("rect"))
        if rect is None or (rect.width <= 0 and rect.height <= 0):
            continue
        # PDF line paths have a zero width or height bbox.  Give the intersection
        # operation a sub-point extent while preserving the source coordinates.
        out.append(pymupdf.Rect(rect.x0, rect.y0, max(rect.x1, rect.x0 + 0.1),
                                max(rect.y1, rect.y0 + 0.1)))
    return out


def _is_running_text(lines, column):
    """A block is running text when at least two of its lines are wide.

    Judge lines, not the block: PDF often groups scattered short labels
    ("SHL 3.5", "a", "b", "Class 2") into one wide block.
    """
    wide = 0
    for line in lines:
        rect = _safe_rect(line.get("bbox"))
        if rect is not None and rect.width > 0.6 * max(column.width, 1):
            wide += 1
    return len(lines) >= 2 and wide >= 2


def _figure_region_expansion(page, base, caption_rect=None):
    """Find nearby figure labels and vector paths without crossing a caption/body.

    This is deliberately a layout heuristic.  It never interprets text or drawings;
    it only extends a crop that already has a figure locator.
    """
    bounds = pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
    base = pymupdf.Rect(base) & bounds
    if not _valid_region(bounds, base):
        return pymupdf.Rect(base), {"expanded_with": [], "labels": []}
    cutoff = caption_rect.y0 if caption_rect is not None else bounds.y1
    if cutoff <= bounds.y0:
        return base, {"expanded_with": [], "labels": []}
    column = pymupdf.Rect(max(bounds.x0, base.x0 - FIGURE_NEIGHBOR_GAP),
                          bounds.y0, min(bounds.x1, base.x1 + FIGURE_NEIGHBOR_GAP), cutoff)
    image_area = base
    # A stored object bbox normally already is the union of its image blocks.  Include
    # a touching image block when a PDF stores several blocks with a tiny gap.
    for info in page.get_image_info():
        image = _safe_rect(info.get("bbox"))
        if image is not None and _distance(image, base) <= 1:
            image_area |= image
    expanded = image_area
    expanded_with = []
    selected_drawings = []
    for drawing in _figure_drawings(page):
        # Ignore page-sized clipping/background paths.  Small axes and panel borders
        # remain eligible, including zero-width/zero-height PDF lines.
        if drawing.width > max(base.width * 2.5, page.rect.width * 0.95) \
                or drawing.height > max(base.height * 2.5, page.rect.height * 0.75):
            continue
        if _distance(drawing, image_area) > FIGURE_NEIGHBOR_GAP:
            continue
        clipped = drawing & column
        if clipped.is_empty or clipped.y0 >= cutoff:
            continue
        expanded |= clipped
        selected_drawings.append(clipped)
    if any(_distance(d, base) > 0 for d in selected_drawings):
        expanded_with.append("drawings")

    labels = []
    text = page.get_text("dict", flags=extract.TEXT_FLAGS)
    for block in text.get("blocks", []):
        lines = block.get("lines", [])
        if not lines or objects._is_paragraph(block):
            continue
        if _is_running_text(lines, column):
            continue
        for line in lines:
            if abs(line.get("dir", (1, 0))[1]) > 0.1:
                continue
            line_rect = _safe_rect(line.get("bbox"))
            if line_rect is None or line_rect.y1 > cutoff + 0.1:
                continue
            if caption_rect is not None and line_rect.intersects(caption_rect):
                continue
            for span in line.get("spans", []):
                raw = span.get("text", "")
                label = extract.clean_spaces(extract.normalize_font_glyphs(raw, span.get("font")))
                rect = _safe_rect(span.get("bbox"))
                if rect is None or not label or len(label) > 64:
                    continue
                if _label_key(label, "main") is not None:
                    continue
                if _distance(rect, expanded) > FIGURE_NEIGHBOR_GAP:
                    continue
                if rect.x1 <= column.x0 or rect.x0 >= column.x1 or rect.y1 <= bounds.y0:
                    continue
                labels.append(rect)
                # A label that starts inside the figure band may stick out past it
                # ("SHL 1.5 B-DNA helix" at the right edge); keep it whole.
                expanded |= rect
    if any(not base.contains(label) for label in labels):
        expanded_with.append("labels")
    x0 = min([column.x0, *(label.x0 for label in labels)])
    x1 = max([column.x1, *(label.x1 for label in labels)])
    expanded = expanded & pymupdf.Rect(max(bounds.x0, x0), bounds.y0, min(bounds.x1, x1), cutoff)
    if not _valid_region(bounds, expanded):
        return base, {"expanded_with": [], "labels": []}
    return expanded, {"expanded_with": expanded_with, "labels": labels}


def _region_area(rect):
    return max(0.0, pymupdf.Rect(rect).width * pymupdf.Rect(rect).height)


def _valid_region(bounds, rect):
    rect = pymupdf.Rect(rect)
    return (math.isfinite(rect.x0) and math.isfinite(rect.y0) and
            math.isfinite(rect.x1) and math.isfinite(rect.y1) and
            rect.x0 < rect.x1 and rect.y0 < rect.y1 and bounds.contains(rect))


def _render_inputs(page, number, bbox, kind, source_sha256):
    """All inputs that affect the rendered crop pixels."""
    return {"source_sha256": source_sha256, "page": number, "bbox": list(bbox), "kind": kind,
            "dpi": DPI, "render_settings": {"format": "png", "clip": "rotation_matrix",
                                               "rotation": page.rotation, "cropbox": list(page.cropbox)}}


def _same_render_inputs(crop, expected):
    """Legacy crop entries without a fingerprint must be rendered once again."""
    return isinstance(crop, dict) and crop.get("render_inputs") == expected


def _material_figure_expansion(candidate, base, previous_bbox, old_crop, labels):
    """Apply the re-crop policy while keeping a valid old PNG byte-identical."""
    if previous_bbox is None or old_crop.get("status") != "retained":
        return candidate
    old = pymupdf.Rect(previous_bbox)
    added = (_region_area(candidate) - _region_area(old)) / max(_region_area(old), 1.0)
    recovers_label = any(not old.contains(label) for label in labels)
    return candidate if added > 0.10 or recovers_label else old


def _assign(page, caption_rects):
    """Each graphic cluster goes to its nearest caption on the page: figures prefer graphics
    above their caption and tables graphics below it."""
    out = {}
    if not caption_rects:
        return out
    for g in _graphics(page):
        def score(item):
            (kind, _, _), cap = item
            wrong_side = g.y0 >= cap.y1 - 2 if kind == "figure" else g.y1 <= cap.y0 + 2
            return _distance(g, cap) + (40 if wrong_side else 0)
        key, cap = min(caption_rects.items(), key=score)
        if _distance(g, cap) <= 0.5 * page.rect.height:
            out.setdefault(key, []).append(g)
    return out


def _table_text_rect(lines, cap_lines, source, page):
    """Lines of a text table below its caption until running text, the next caption, a wide
    gap or the page foot; within the caption's column (left/right half or full width).
    None unless at least two rows have separate cells."""
    cap = pymupdf.Rect(cap_lines[0]["rect"])
    for line in cap_lines:
        cap |= line["rect"]
    size, width, height = max(l["size"] for l in cap_lines), page.rect.width, page.rect.height
    mid = width / 2
    single = cap.x1 > mid + 10 or any(  # a running line across the middle: one-column page
        l["rect"].x0 < mid - 20 and l["rect"].x1 > mid + 20 and len(l["text"]) > 40
        and not extract._in_margin(l["rect"], height) for l in lines)
    x0, x1 = (0, width) if single else (0, mid) if cap.x0 < mid else (mid, width)
    rows = []
    for line in lines:
        r = line["rect"]
        if r.y0 < cap.y1 - 1 or not line["text"] or line in cap_lines or not x0 - 4 <= r.x0 < x1:
            continue
        if _label_key(line["text"], source) or line["paragraph"] or r.y0 > 0.94 * height \
                or (line["size"] > size + 1 and len(line["text"]) > 30) \
                or (rows and r.y0 - max(x.y1 for x in rows) > 3 * size):
            break
        rows.append(r)
    cells = sum(1 for r in rows if any(o is not r and _same_row(o, r) and (o.x0 >= r.x1 or o.x1 <= r.x0) for o in rows))
    if cells < 4:  # two rows of at least two cells
        return None
    out = pymupdf.Rect(rows[0])
    for r in rows:
        out |= r
    return out


def _locate_source(doc, source, items, sentences, ordered):
    """Locator results for one registered PDF: {object_id: info}.

    items: [(kind, record)] of this source; ordered: its sentence rows in seq order.
    info has caption (rows or None), caption_page, and, when a graphic was located, page,
    bbox and locator_basis; text_only_caption_page marks caption pages without graphics."""
    pages = {}

    def cache(number):
        if number not in pages:
            pages[number] = _page_lines(doc[number - 1])
        return pages[number]
    out, regions = {}, {}
    for kind, obj in items:
        oid = obj.get("figure_id") or obj.get("table_id") or obj.get("object_id")
        number, key = obj.get("page"), _object_key(oid or "", source)
        if obj.get("missing_object") or key is None or not isinstance(number, int) \
                or isinstance(number, bool) or not 1 <= number <= len(doc):
            continue
        captions, _ = _refs(obj.get("caption_sentence_ids"), sentences, oid.split(":")[0], source)
        region = _caption_region(doc, number, key, source, captions[0]["text"] if captions else obj.get("label", ""), cache)
        regions[oid] = (key, number, region, obj)
        out[oid] = {"caption": _caption_rows(captions, ordered, region) if captions else None,
                    "caption_page": number}
        caption_lines = region.get(number, []) if region else []
        caption_box = _caption_bbox(caption_lines)
        if caption_box is not None:
            out[oid]["_caption_bbox"] = list(caption_box)
    by_page = {}
    for oid, (key, number, region, obj) in regions.items():
        if region:
            rect = pymupdf.Rect(region[number][0]["rect"])
            for line in region[number]:
                rect |= line["rect"]
            by_page.setdefault(number, {})[oid] = (key, rect, region[number])
    legends = {}  # figure caption pages without any graphic: manuscript legend pages
    for number, caps in by_page.items():
        page = doc[number - 1]
        found = _assign(page, {(k[0], oid, k[2]): rect for oid, (k, rect, _) in caps.items()})
        graphics = _graphics(page)
        for oid, (key, rect, lines) in caps.items():
            obj = regions[oid][3]
            if obj.get("bbox") is not None:
                continue
            assigned = found.get((key[0], oid, key[2]), [])
            bbox = None
            if assigned:
                bbox = assigned[0]
                for g in assigned[1:]:
                    bbox |= g
            if key[0] == "table":
                text_rect = _table_text_rect(cache(number), lines, source, page)
                if text_rect is not None:
                    bbox = text_rect if bbox is None else bbox | text_rect
            if bbox is not None:
                bbox = (bbox + (-2, -2, 2, 2)) & pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
                out[oid].update(page=number, bbox=[round(v, 1) for v in bbox], locator_basis="same_page_caption")
            elif not graphics:
                out[oid]["text_only_caption_page"] = True
                if key[0] == "figure":
                    legends[oid] = (key, number)
    out_manuscript = _manuscript_pages(doc, source, legends, cache)
    for oid, info in out_manuscript.items():
        out[oid].update(info)
    return out


def _manuscript_pages(doc, source, legends, cache):
    """Accepted-manuscript layout: legends listed on text-only pages, one figure per later page.

    Figure N maps to a later graphic page labelled "Figure N", else by order, but only when the
    remaining figures and unlabelled graphic pages are equal in number and the order agrees
    with every label. Anything else stays unlocated."""
    out = {}
    for supp in (False, True):
        group = sorted((key[2], oid, number) for oid, (key, number) in legends.items() if key[1] == supp)
        if len(group) < 2:  # a single caption-only page is not a legends section
            continue
        last = max(number for _, _, number in group)
        later = [number for key, number in legends.values() if key[1] != supp and number > last]
        stop = min(later, default=len(doc) + 1)
        labelled, unlabelled = {}, []
        for number in range(last + 1, stop):
            page = doc[number - 1]
            lines = cache(number)
            labels = [_label_key(l["text"], source) for l in lines if _label_key(l["text"], source)]
            bare = [k for k, l in ((_label_key(l["text"], source), l) for l in lines)
                    if k and _BARE_LABEL.fullmatch(l["text"])]
            if len(labels) != len(bare):  # a page with a full caption is not a figure-only page
                continue
            graphics = _graphics(page)
            if sum(g.width * g.height for g in graphics) < GRAPHIC_PAGE_SHARE * page.rect.width * page.rect.height:
                continue
            region = graphics[0]
            for g in graphics[1:]:
                region |= g
            region &= pymupdf.Rect(0, 0, page.cropbox.width, page.cropbox.height)
            figure_labels = [k for k in bare if k[0] == "figure"]
            if figure_labels and figure_labels[0][1] == supp:
                labelled[figure_labels[0][2]] = (number, region)
            elif not figure_labels:
                unlabelled.append((number, region))
        numbers = {n for n, _, _ in group}
        hits = {n: v for n, v in labelled.items() if n in numbers}
        rest = [(n, oid) for n, oid, _ in group if n not in hits]
        ordered = len(rest) == len(unlabelled) and all(
            (n < m) == (page < hits[m][0]) for (n, _), (page, _) in zip(rest, unlabelled) for m in hits)
        for n, oid, _ in group:
            if n in hits:
                page, region = hits[n]
                out[oid] = {"page": page, "bbox": [round(v, 1) for v in region],
                            "locator_basis": "manuscript_figure_page_label"}
        if ordered:
            for (n, oid), (page, region) in zip(rest, unlabelled):
                out[oid] = {"page": page, "bbox": [round(v, 1) for v in region],
                            "locator_basis": "manuscript_figure_page_order"}
    return out


def _build(paper_id, regions, detect, persist):
    folder = _folder(paper_id)
    meta = db.read_json(folder / "metadata.json")
    if not meta:
        raise FileNotFoundError(f"No paper {paper_id}")
    previous = _read_manifest(folder / "media_manifest.json")
    if regions is None:
        region_file = folder / "media_regions.json"
        regions = db.read_json(region_file) if region_file.exists() else (previous or {}).get("manual_regions", [])
    if not isinstance(regions, list) or any(not isinstance(r, dict) for r in regions):
        raise ValueError("Manual regions must be a list of objects")
    sources = _sources(meta, folder)
    signature, inputs, public_sources = _signature(folder, sources, regions, detect)
    if (previous and previous.get("input_sha256") == signature
            and previous.get("status") in {"done", "partial"}
            and not _manifest_needs_legacy_read(folder, previous)):
        if all(_old_crop(folder, bundle).get("status") == "retained" for bundle in previous["bundles"]
               if bundle.get("crop", {}).get("path")):
            return {**previous, "cache_hit": True}
    sentences = {row["sentence_id"]: row for row in db.read_jsonl(folder / "sentences.jsonl")}
    if any(not isinstance(sid, str) or not sid.startswith(paper_id + ":") for sid in sentences):
        raise ValueError("Source sentences must belong to paper")
    units = list(db.read_jsonl(folder / "evidence.jsonl"))
    old = {b["object_id"]: b for b in (previous or {}).get("bundles", [])}
    objects = [("figure", r) for r in db.read_jsonl(folder / "figures.jsonl")]
    objects += [("table", r) for r in db.read_jsonl(folder / "tables.jsonl")]
    known = {r.get("figure_id") or r.get("table_id") for _, r in objects}
    referenced = {oid for s in sentences.values() for oid in s.get("mentions", [])}
    referenced.update(oid for u in units for oid in (u.get("outcome") or {}).get("source_objects", []) or [])
    for oid in sorted(referenced):
        if not isinstance(oid, str):
            raise ValueError("Source object IDs must be strings")
        full = oid if ":" in oid else f"{paper_id}:{oid}"
        if full not in known and re.fullmatch(re.escape(paper_id) + r":[FT]S?\d+", full):
            objects.append(("table" if full.split(":")[-1].startswith("T") else "figure",
                            {"object_id": full, "source": None, "page": None, "bbox": None,
                             "missing_object": True}))
            known.add(full)
    revision = uuid.uuid4().hex
    result = []
    with ExitStack() as stack:
        stage = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix=".media-stage-", dir=folder))) if persist else None
        docs = {}
        for source, row in sources.items():
            if row["status"] == "available":
                try:
                    docs[source] = stack.enter_context(pymupdf.open(row["_path"]))
                except (RuntimeError, OSError, ValueError):
                    row["status"] = public_sources[source]["status"] = "unreadable"
        candidates = []
        if detect:
            for source, doc in docs.items():
                candidates += list(_candidate_regions(doc, source))
        # Retain detected regions if their original is temporarily absent/changed.
        candidates += [{"kind": b["kind"], **b["region_input"]} for b in old.values()
                       if b["kind"] in KINDS and b.get("detection") == "automatic_candidate"
                       and b.get("region_input", {}).get("source") not in docs]
        for region in candidates + regions:
            kind = region.get("kind")
            if kind not in KINDS | {"figure", "table"}:
                raise ValueError("Unsupported manual region kind")
            source = region.get("source", "main")
            if source not in sources:
                raise ValueError("Manual region source must be registered in metadata")
            if region.get("source_sha256") and region["source_sha256"] != (sources[source]["sha256"] or sources[source]["recorded_sha256"]):
                raise ValueError("Manual region source hash is stale")
            if kind in {"figure", "table"}:
                oid = region.get("object_id")
                match = next((r for k, r in objects if k == kind and (r.get("figure_id") or r.get("table_id") or r.get("object_id")) == oid), None)
                if match is None:
                    raise ValueError("Manual figure/table region needs an existing full object ID")
                if match.get("source") and not match.get("missing_object") and match["source"] != source:
                    raise ValueError("Manual locator cannot change an existing object source")
                match.update({"source": source, "page": region.get("page"), "bbox": region.get("bbox"),
                              "_original_bbox": match.get("_original_bbox", match.get("bbox")),
                              "missing_object": False, "_manual_region": True})
                if match["bbox"] is None:
                    raise ValueError("Manual object region needs a bbox")
                continue
            oid = f"{paper_id}:M{_digest([kind, source, region.get('page'), region.get('bbox')])[:16]}"
            detection = region.get("detection", "manual_region") if region in candidates else "manual_region"
            optional_text = region.get("optional_text") if detection == "automatic_candidate" else old.get(oid, {}).get("optional_text")
            objects.append((kind, {**region, "object_id": oid, "optional_text": optional_text,
                                   "detection": detection}))
        ordered = {}
        for row in sorted(sentences.values(), key=lambda r: r.get("seq", 0)):
            ordered.setdefault(row.get("source", "main"), []).append(row)
        located = {}
        for source, doc in docs.items():
            located.update(_locate_source(doc, source, [(k, o) for k, o in objects if k in ("figure", "table")
                                                        and o.get("source", "main") == source],
                                          sentences, ordered.get(source, [])))
        seen = set()
        for kind, obj in objects:
            oid = obj.get("figure_id") or obj.get("table_id") or obj.get("object_id")
            if not isinstance(oid, str) or not oid.startswith(paper_id + ":"):
                raise ValueError("Object ID must belong to paper")
            if oid in seen:
                continue
            seen.add(oid)
            source = obj.get("source", "main")
            provenance = public_sources.get(source, {"source": source, "status": "unavailable", "path": None,
                                                      "sha256": None, "recorded_sha256": None})
            number, bbox = obj.get("page"), obj.get("bbox")
            object_bbox = obj.get("_original_bbox", bbox)
            # Reject unsafe stored paths even when a new crop could hide them.
            if obj.get("crop"):
                crop_path(folder, obj["crop"])
            anchors, unresolved = _refs(obj.get("sentence_ids"), sentences, paper_id, source, kind in KINDS)
            if kind in KINDS and not anchors and obj.get("optional_text"):
                anchors = [dict(s) for s in sentences.values() if s.get("source", "main") == source
                           and s.get("page") == number and obj["optional_text"] in s.get("text", "")]
            captions, missing = _refs(obj.get("caption_sentence_ids"), sentences, paper_id, source, kind in KINDS)
            unresolved += missing
            info = located.get(oid, {})
            if info.get("caption") is not None:
                captions = info["caption"]
            mention_ids = list(obj.get("mentioned_in") or []) + [sid for sid, s in sentences.items() if oid in s.get("mentions", [])]
            mentions, missing = _refs(mention_ids, sentences, paper_id)
            unresolved += missing
            data = [dict(s) for s in sentences.values() if s.get("object_id") == oid and s.get("kind") == "table_row"]
            linked_ids = {s["sentence_id"] for s in captions + mentions + data + anchors}
            associated = []
            for unit in units:
                object_ids = (unit.get("outcome") or {}).get("source_objects") or []
                if oid in object_ids or oid.rsplit(":", 1)[-1] in object_ids or linked_ids.intersection(unit.get("sentence_ids") or []):
                    associated.append({"unit_id": unit.get("unit_id"), "unit_type": unit.get("unit_type"),
                                       "status": unit.get("status")})
            crop = _old_crop(folder, old.get(oid), obj.get("crop"))
            old_crop_entry = (old.get(oid) or {}).get("crop")
            previous_locator = old.get(oid, {}).get("source_locator", {})
            rotation, cropbox = previous_locator.get("rotation"), previous_locator.get("cropbox")
            region_kind = "manual_object_region" if obj.get("_manual_region") else "object_region" if bbox is not None else "unlocated_object"
            doc = docs.get(source)
            auto = bbox is None and doc is not None and info.get("bbox") is not None
            locator_basis = (None if kind in KINDS else "manual_locator" if obj.get("_manual_region")
                             else "stored_object_region" if bbox is not None else "unlocated")
            if auto:
                number, bbox, region_kind, locator_basis = info["page"], info["bbox"], "object_region", info["locator_basis"]
            if bbox is None and doc is not None and number is not None:
                if isinstance(number, bool) or not isinstance(number, int) or not 1 <= number <= len(doc):
                    raise ValueError("Page is outside PDF")
                if not info.get("text_only_caption_page"):
                    # A page with unattributed graphics is shown whole; a text-only caption
                    # page would show only the caption, so that object stays unlocated.
                    page = doc[number - 1]
                    bbox = [0, 0, page.cropbox.width, page.cropbox.height]
                    region_kind = "source_page_preview"
            elif bbox is None and previous_locator.get("region_basis") in ("source_page_fallback", "automatic_locator"):
                # Original temporarily unavailable: keep the last derived locator.
                number, bbox = previous_locator.get("page"), previous_locator.get("bbox")
                region_kind, auto = previous_locator.get("region_kind"), previous_locator.get("region_basis") == "automatic_locator"
                locator_basis = previous_locator.get("locator_basis", locator_basis)
            expanded_with = []
            if kind == "figure" and doc is not None and bbox is not None and isinstance(number, int) \
                    and not isinstance(number, bool) and 1 <= number <= len(doc) and not obj.get("_manual_region") \
                    and locator_basis in {"stored_object_region", "same_page_caption",
                                          "manuscript_figure_page_label", "manuscript_figure_page_order"}:
                page = doc[number - 1]
                base_rect = _rect(page, number, bbox)
                caption_box = _safe_rect(info.get("_caption_bbox"))
                candidate, details = _figure_region_expansion(page, base_rect, caption_box)
                selected = _material_figure_expansion(candidate, base_rect, previous_locator.get("bbox"),
                                                       crop, details["labels"])
                if selected != base_rect:
                    bbox = [round(v, 1) for v in selected]
                    if selected == candidate:
                        expanded_with = details["expanded_with"]
            prior = (previous_locator.get("page"), previous_locator.get("bbox"), previous_locator.get("region_kind"))
            changed = oid in old and prior != (number, bbox, region_kind)
            render_inputs = None
            if doc is not None and bbox is not None and isinstance(number, int) and not isinstance(number, bool) \
                    and 1 <= number <= len(doc):
                render_inputs = _render_inputs(doc[number - 1], number, bbox, kind, provenance["sha256"])
            same_render_inputs = render_inputs is not None and _same_render_inputs(crop, render_inputs)
            reuse_old_crop = oid in old and not changed and crop.get("status") == "retained" \
                and doc is not None and same_render_inputs
            if reuse_old_crop:
                # A non-cache rebuild (for example, a detect-mode change) must not
                # create a new media revision for an unchanged, valid crop.
                crop = dict(old_crop_entry)
            if doc is not None and (bbox is None or changed):
                crop = {"path": None, "sha256": None, "source_sha256": None, "status": "unavailable"}
            history = {"page": prior[0], "bbox": prior[1], "region_kind": prior[2],
                       "locator_basis": previous_locator.get("locator_basis"),
                       "crop_path": old[oid].get("crop", {}).get("path"),
                       "crop_sha256": old[oid].get("crop", {}).get("sha256"),
                       "manifest_revision": previous.get("revision")} if changed else previous_locator.get("previous")
            locator_status = "missing_object" if obj.get("missing_object") else "missing_region" if bbox is None and provenance["status"] != "non_pdf" else provenance["status"]
            if bbox is not None:
                # Basic validation also applies when original is absent.
                if not isinstance(number, int) or isinstance(number, bool) or number < 1:
                    raise ValueError("Page must be a 1-based integer")
                if not isinstance(bbox, (list, tuple)) or len(bbox) != 4 or any(
                        isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v) for v in bbox) or not (0 <= bbox[0] < bbox[2] and 0 <= bbox[1] < bbox[3]):
                    raise ValueError("Invalid region bbox")
                if doc is not None:
                    if number > len(doc):
                        raise ValueError("Page is outside PDF")
                    page = doc[number - 1]
                    rect = _rect(page, number, bbox)
                    rotation, cropbox = page.rotation, list(page.cropbox)
                    if kind in KINDS and obj["detection"] == "manual_region":
                        obj["optional_text"] = page.get_text("text", clip=rect)
                    if persist and not reuse_old_crop:
                        # The JSON bundle couples captions; source pixels preserve the actual object.
                        name = _digest(oid)[:24] + ".png"
                        target = stage / name
                        if rect.width * rect.height * (DPI / 72) ** 2 > 25_000_000:
                            raise ValueError("Region exceeds safe render size")
                        page.get_pixmap(clip=rect * page.rotation_matrix, dpi=DPI).save(str(target))
                        crop = {"path": f"crops/media-{revision}/{name}", "sha256": _hash(target),
                                "source_sha256": provenance["sha256"], "status": "available",
                                "render_inputs": _render_inputs(page, number, bbox, kind,
                                                                 provenance["sha256"])}
                    elif crop["status"] == "unavailable":
                        crop["status"] = "not_built"
            bundle = {"object_id": oid, "kind": kind, "label": obj.get("label"),
                      "caption": captions, "data": data, "mentions": mentions, "anchors": anchors,
                      "unresolved_sentence_ids": unresolved, "associated_units": associated,
                      "source_locator": {**provenance, "page": number, "bbox": bbox,
                                         "object_bbox": object_bbox, "region_kind": region_kind,
                                         "object_region_status": "not_located" if region_kind in ("source_page_preview", "unlocated_object") else "located" if auto else "annotated",
                                         "region_basis": "automatic_locator" if auto else "manual_locator" if obj.get("_manual_region") or obj.get("detection") == "manual_region" else "automatic_candidate" if obj.get("detection") == "automatic_candidate" else "source_page_fallback" if region_kind == "source_page_preview" else "stored_locator" if object_bbox is not None else "unresolved",
                                         "region_completeness": "not_assessed",
                                         "expanded_with": expanded_with,
                                         "rotation": rotation, "cropbox": cropbox,
                                         "coordinate_system": "pymupdf_unrotated_cropbox_points",
                                         "status": locator_status, "locator_basis": locator_basis,
                                         "caption_page": info.get("caption_page", obj.get("page")),
                                         "locator_note": "caption_page_text_only" if info.get("text_only_caption_page") and bbox is None else None,
                                         "previous": history}, "crop": crop,
                      "caption_basis": "pdf_caption_region" if info.get("caption") is not None else "stored_caption_ids",
                      "stored_caption_sentence_ids": list(obj.get("caption_sentence_ids") or []),
                      "table": {key: obj.get(key) for key in ("header", "rows", "footnotes", "quality", "extraction")} if kind == "table" else None}
            if kind in KINDS:
                bundle.update({"detection": obj["detection"], "optional_text": obj.get("optional_text"),
                               "text_status": "unverified_extracted" if obj.get("optional_text") else "unavailable",
                               "region_input": {k: v for k, v in obj.items() if k not in ("object_id", "kind")}})
            result.append(bundle)
        # Detect source/input changes during rendering before publication. No old files changed yet.
        current_sources = _sources(meta, folder)
        if _signature(folder, current_sources, regions, detect)[0] != signature:
            raise RuntimeError("Media inputs changed during build; previous revision retained")
        manifest = {"version": VERSION, "paper_id": paper_id, "revision": revision,
                    "built_at": config.now(), "input_sha256": signature, "input_hashes": inputs,
                    "input_stats": _input_stats(folder, sources),
                    "sources": public_sources, "manual_regions": regions, "detect": detect,
                    "status": "done" if all(b["source_locator"]["status"] in ("available", "non_pdf")
                                               and b["source_locator"]["region_kind"] != "source_page_preview"
                                               and not b["unresolved_sentence_ids"]
                                               and (b["source_locator"]["bbox"] is None or b["crop"]["status"] in ("available", "retained")) for b in result)
                              and all(s["status"] in ("available", "non_pdf") for s in public_sources.values()) else "partial",
                    "bundles": result, "scope_note": SCOPE_NOTE}
        changed_objects = []
        for bundle in result:
            before = old.get(bundle["object_id"])
            if before is None:
                changed_objects.append(bundle["object_id"])
                continue
            before_loc = before.get("source_locator", {})
            after_loc = bundle["source_locator"]
            before_crop = before.get("crop", {})
            after_crop = bundle.get("crop", {})
            if any(before_loc.get(key) != after_loc.get(key)
                   for key in ("region_kind", "locator_basis", "page", "bbox")) \
                    or any(before_crop.get(key) != after_crop.get(key) for key in ("path", "sha256")):
                changed_objects.append(bundle["object_id"])
        manifest["changed_objects"] = changed_objects
        manifest["output_sha256"] = _digest(manifest)
        if persist:
            history = folder / "media_history" / revision
            prior = folder / "media_manifest.json"
            if prior.is_file():
                history.mkdir(parents=True, exist_ok=True)
                shutil.copy2(prior, history / "media_manifest.json")
            # A crash before atomic manifest publication leaves only an unused crop revision.
            crop_dir = crop_path(folder, f"crops/media-{revision}")
            crop_dir.parent.mkdir(parents=True, exist_ok=True)
            os.replace(stage, crop_dir)
            workflow.atomic_json(prior, manifest)
        return {**manifest, "cache_hit": False}


def build(paper_id, regions=None, detect=True, persist=True):
    """Add/rebuild a paper manifest without editing sentences, evidence or original PDFs.

    regions is an optional list of registered-source page/bbox formula/symbol locators,
    or figure/table locators refining an existing full object_id;
    otherwise media_regions.json or the last manifest's manual regions is used. persist=False
    does no writes and never renders new crops. Caller enforces current-vault paper membership.
    """
    with workflow.writer_lock() if persist else nullcontext():
        return _build(paper_id, regions, detect, persist)


def _select_bundles(manifest, object_ids=None, sentence_ids=None, unit_ids=None):
    objects, sids, units = set(object_ids or []), set(sentence_ids or []), set(unit_ids or [])
    if not (objects or sids or units):
        return manifest.get("bundles") or []
    return [b for b in manifest.get("bundles") or [] if b.get("object_id") in objects or b.get("object_id", "").rsplit(":", 1)[-1] in objects
            or any(s.get("sentence_id") in sids for key in ("caption", "data", "mentions", "anchors") for s in (b.get(key) or []))
            or any(u.get("unit_id") in units for u in (b.get("associated_units") or []))]


def bundles(paper_id, object_ids=None, sentence_ids=None, unit_ids=None):
    """Read stored bundles fast; use the legacy non-persisting path when incomplete/stale."""
    folder = _folder(paper_id)
    previous = _read_manifest(folder / "media_manifest.json")
    reason = None
    needs_legacy = not previous or previous.get("status") not in {"done", "partial"}
    if needs_legacy:
        reason = "missing or unpublished manifest"
    if previous and not needs_legacy:
        fast = _manifest_fast_current(folder, previous)
        if fast is None:
            # Manifests created before the stat stamps were introduced retain
            # the exact hash contract.  Hashes are process-cached by mtime/size.
            meta = db.read_json(folder / "metadata.json", {}) or {}
            sources = _sources(meta, folder)
            signature = _signature(folder, sources, previous.get("manual_regions", []),
                                   previous.get("detect", True))[0]
            fast = signature == previous.get("input_sha256")
        if not fast:
            reason = "media inputs changed"
        else:
            reason = _manifest_read_issue(folder, previous, object_ids, sentence_ids, unit_ids)
        needs_legacy = reason is not None
    if needs_legacy:
        _log_read_fallback(paper_id, reason or "manifest incomplete")
        # Compatibility path: it is read-only (persist=False), but retains the
        # old behavior for stale/incomplete manifests and missing derivatives.
        previous = build(paper_id, detect=(previous or {}).get("detect", True), persist=False)
    return _select_bundles(previous, object_ids, sentence_ids, unit_ids)


def relocate(paper_id, persist=False):
    """Before/after locator report for one paper; persist=True also publishes the new manifest.

    Dry run (default) writes nothing. "before" is the last published manifest (None if none).
    Applying is an ordinary build: old crops and media_history stay recoverable."""
    previous = _read_manifest(_folder(paper_id) / "media_manifest.json")
    manifest = build(paper_id, detect=(previous or {}).get("detect", True), persist=persist)

    def view(bundle):
        if bundle is None:
            return None
        loc = bundle["source_locator"]
        return {key: loc.get(key) for key in ("region_kind", "locator_basis", "page", "bbox")}
    old = {b["object_id"]: b for b in (previous or {}).get("bundles", [])}
    rows = []
    for bundle in manifest["bundles"]:
        if bundle["kind"] not in ("figure", "table"):
            continue
        before = old.get(bundle["object_id"])
        known = {r["sentence_id"] for r in (before or {}).get("caption", [])}
        before_view, after_view = view(before), view(bundle)
        before_crop, after_crop = (before or {}).get("crop", {}), bundle.get("crop", {})
        crop_changed = any(before_crop.get(key) != after_crop.get(key) for key in ("path", "sha256"))
        rows.append({"object_id": bundle["object_id"], "kind": bundle["kind"], "before": before_view,
                     "after": after_view, "changed": before_view != after_view or crop_changed,
                     "crop_changed": crop_changed, "crop_status": bundle["crop"]["status"],
                     "caption_added_sentence_ids": [r["sentence_id"] for r in bundle["caption"]
                                                    if before is not None and r["sentence_id"] not in known]})

    def counts(side):
        out = {}
        for r in rows:
            if r[side] is not None:
                key = f"{r['kind']}/{r[side]['region_kind']}/{r[side]['locator_basis']}"
                out[key] = out.get(key, 0) + 1
        return out
    return {"paper_id": paper_id, "persist": persist, "status": manifest["status"],
            "before": counts("before") if previous else None, "after": counts("after"),
            "changed_objects": [row["object_id"] for row in rows if row["changed"]], "objects": rows}
