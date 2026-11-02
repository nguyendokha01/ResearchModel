"""Figures and tables (Tier 1, contract §2.4): labels, table rows, positions and image crops.

A table is only kept when a "Table n" caption is on the same page, so that every table row
can be cited as "Table n, row …". Numbers are never read from figure images: a figure is
kept as its caption, its position on the page and a crop for visual checking.
"""

import re

import pymupdf

from . import extract

if hasattr(pymupdf, "no_recommend_layout"):  # find_tables() would print an install hint into the user's log
    pymupdf.no_recommend_layout()
LABEL = re.compile(r"^(?i:(supplementa(?:ry|l)\s+|suppl\.?\s+)?(fig(?:ure)?s?\.?|table|scheme))\s*(S?)(\d{1,3})")
FOOTNOTE = re.compile(r"^(?:[*†‡§¶#]|[a-z]\s|\(?[a-z]\)\s)|\b[Pp]\s?[<=>]|^(?:Data|Values|Abbreviations|Results)\b")
CROP_DPI = 110
CROP_QUALITY = 70


def label_of(text):
    """'Fig. 3B Muse cells…' -> ('figure', False, 3); None if the text is not a caption."""
    m = LABEL.match(text or "")
    if not m:
        return None
    kind = "table" if m.group(2).lower().startswith("tab") else "figure"
    return kind, bool(m.group(1) or m.group(3)), int(m.group(4))


def object_id(paper_id, kind, supplementary, number):
    return f"{paper_id}:{'T' if kind == 'table' else 'F'}{'S' if supplementary else ''}{number}"


def display_label(kind, supplementary, number):
    return f"{'Table' if kind == 'table' else 'Figure'} {'S' if supplementary else ''}{number}"


def _union(rects):
    out = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out


def _overlap_x(a, b):
    return min(a[2], b[2]) - max(a[0], b[0])


def _crop(page, rect, path):
    clip = (pymupdf.Rect(rect) + (-4, -4, 4, 4)) & page.rect
    if clip.is_empty:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    page.get_pixmap(clip=clip, dpi=CROP_DPI).save(str(path), jpg_quality=CROP_QUALITY)
    return True


def _clean(cell):
    text = extract.normalize_font_glyphs((cell or "").translate(extract.LIGATURES))
    # a PDF soft hyphen: joins a word broken inside a cell ("knock\xad\nout"), printed "-" at the cell end
    text = re.sub(r"\xad\s*(?=\w)", "", text).replace("\xad", "-")
    return extract.clean_spaces(text)


def _graphics(page):
    """Rectangles of images and vector drawings (figure parts) on a page."""
    rects = [pymupdf.Rect(info["bbox"]) for info in page.get_image_info()]
    try:
        rects += [pymupdf.Rect(r) for r in page.cluster_drawings()]
    except Exception:  # older PyMuPDF or unusual drawing content
        pass
    page_area = page.rect.width * page.rect.height
    return [r for r in rects if r.width > 20 and r.height > 20 and 1500 < r.width * r.height < 0.9 * page_area]


def _is_paragraph(block):
    """A block of running text (not a table column or figure label): long, with long lines."""
    lines = [extract.normalize_font_glyphs("".join(s["text"] for s in ln["spans"]))
             for ln in block.get("lines", [])]
    text = " ".join(lines)
    return len(text) >= 150 and len(text) / max(len(lines), 1) >= 40 and not _is_caption(text)


def _body_blocks(page, captions_on_page):
    return [pymupdf.Rect(b["bbox"]) for b in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]
            if _is_paragraph(b) and not any(pymupdf.Rect(b["bbox"]).intersects(pymupdf.Rect(c["bbox"]))
                                            for c in captions_on_page)]


MARGIN_BAND = 36  # pt: running headers/footers and banners ("ARTICLE IN PRESS") live here, never figure parts


def _figure_rect(page, caption, captions_on_page):
    cap = pymupdf.Rect(caption["bbox"])
    drawn = [r for r in _graphics(page)
             if r.y1 > page.rect.y0 + MARGIN_BAND and r.y0 < page.rect.y1 - MARGIN_BAND]
    graphics = [r for r in drawn if _overlap_x(r, cap) > 0]
    body = [r for r in _body_blocks(page, captions_on_page) if _overlap_x(r, cap) > 0]
    above_limit = max([r.y1 for r in body if r.y1 <= cap.y0 + 2] or [page.rect.y0])
    above = [r for r in graphics if r.y1 <= cap.y0 + 5 and r.y0 >= above_limit - 5]
    if above:
        return _union(above)
    below_limit = min([r.y0 for r in body if r.y0 >= cap.y1 - 2] or [page.rect.y1])
    below = [r for r in graphics if r.y0 >= cap.y1 - 5 and r.y1 <= below_limit + 5]
    if below:
        return _union(below)
    # Side caption (narrow caption column beside the figure): graphics sharing the caption's vertical band.
    # Only a narrow caption: a normal-width column caption must not grab the other column's figure.
    if cap.width > 0.3 * page.rect.width:
        return None
    beside = [r for r in drawn if _overlap_x(r, cap) <= 0 and min(r.y1, cap.y1) - max(r.y0, cap.y0) > 0]
    return _union(beside) if beside else None


def _footnotes(page, table_rect):
    notes = []
    for b in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]:
        r = pymupdf.Rect(b["bbox"])
        if b.get("lines") and table_rect.y1 - 2 <= r.y0 <= table_rect.y1 + 60 and _overlap_x(r, table_rect) > 0:
            text = extract.clean_spaces(" ".join(extract.line_text(ln) for ln in b["lines"]))
            if FOOTNOTE.search(text):
                notes.append(text)
    return " ".join(notes) or None


def _is_data_row(cells):
    """A table row with values: at least two filled cells and a digit after the first cell."""
    filled = [c for c in cells if c]
    return len(filled) >= 2 and any(re.search(r"\d", c) for c in cells[1:] if c)


def _sideways(page, rect):
    """True when most text lines inside rect are not horizontal: a table printed sideways (find_tables then
    returns it transposed, so row 0 is not its header). A watermark across a table is few lines, not most.
    A page with /Rotate counts too: find_tables reports displayed coordinates, get_text unrotated ones, so neither
    the line directions nor the header words above the table can be read; so does a rect holding no text line."""
    if page.rotation:
        return True
    dirs = [abs(line["dir"][1]) > 0.1 for block in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]
            for line in block.get("lines", []) if pymupdf.Point((line["bbox"][0] + line["bbox"][2]) / 2,
                                                                (line["bbox"][1] + line["bbox"][3]) / 2) in rect]
    return not dirs or sum(dirs) * 2 > len(dirs)


def _words(page, rect, exclude):
    """Words whose centre lies in rect and outside every rect in exclude: [(Rect, text)]."""
    out = []
    mapped_spans = []
    tilted = set()  # (block, line) of text that is not horizontal: watermarks ("ARTICLE IN PRESS")
    for b, block in enumerate(page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]):
        for n, line in enumerate(block.get("lines", [])):
            if abs(line["dir"][1]) > 0.1:
                tilted.add((b, n))
                continue
            for span in line["spans"]:
                mapped = extract.normalize_font_glyphs(span["text"], span.get("font"))
                if mapped != span["text"]:
                    mapped_spans.append((pymupdf.Rect(span["bbox"]), mapped.strip()))
    for w in page.get_text("words", flags=extract.TEXT_FLAGS):
        r = pymupdf.Rect(w[:4])
        centre = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        if (centre in rect and not any(centre in e for e in exclude) and (w[5], w[6]) not in tilted
                and not any(centre in span_rect for span_rect, _ in mapped_spans)):
            out.append((r, extract.normalize_font_glyphs(w[4].translate(extract.LIGATURES))))
    # PyMuPDF's word extractor drops control glyphs used as font-mapped symbols. Preserve
    # their span geometry so a table value such as "74.3 [BEL] 5.8" remains one cell.
    for r, mapped in mapped_spans:
        centre = pymupdf.Point((r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2)
        if centre in rect and not any(centre in e for e in exclude):
            out.append((r, mapped))
    return out


def _lines(words):
    """Group words into printed lines (by vertical centre), each sorted left to right."""
    lines = []
    for r, text in sorted(words, key=lambda w: ((w[0].y0 + w[0].y1) / 2, w[0].x0)):
        yc = (r.y0 + r.y1) / 2
        if lines and abs(lines[-1]["yc"] - yc) <= 0.45 * max(r.height, 1):
            lines[-1]["words"].append((r, text))
        else:
            lines.append({"yc": yc, "words": [(r, text)]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w[0].x0)
        ln["y0"] = min(r.y0 for r, _ in ln["words"])
        ln["y1"] = max(r.y1 for r, _ in ln["words"])
    return lines


def _cells(line, gap=6.0):
    """Split a printed line into cells at horizontal gaps wider than `gap` points."""
    cells = []
    for r, text in line["words"]:
        if cells and r.x0 - cells[-1]["x1"] <= gap:
            cells[-1]["text"] += " " + text
            cells[-1]["x1"] = r.x1
        else:
            cells.append({"x0": r.x0, "x1": r.x1, "text": text})
    return cells


def _names(lines, bounds):
    """Column names from header lines: the words above each column, top to bottom."""
    names = []
    for bound in bounds:
        if bound is None:
            names.append("")
            continue
        x0, x1 = bound
        parts = [text for ln in lines for r, text in ln["words"] if x0 - 2 <= (r.x0 + r.x1) / 2 <= x1 + 2]
        names.append(_clean(" ".join(parts)))
    return names


def _from_finder(page, table, cap_rect, promote):
    """Rows of a table found by PyMuPDF; column names are re-read from the words above the
    first data row, because headers are often merged into one cell or include the caption.
    When no word sits above the first row and that row itself reads as data (a digit in "Control (n = 117)"),
    it is the header (promote=False for a table printed sideways, whose first row is not). A first row whose number cells
    carry no letter ("41 | 22") is a record of a headerless table: it stays a row, the header stays empty (uncertain)."""
    data = [[_clean(c) for c in row] for row in table.extract()]
    first = next((i for i, row in enumerate(data) if _is_data_row(row)), None)
    if first is None:
        return [], []
    cells = [row.cells for row in table.rows]
    bounds = []
    for j in range(len(data[first])):
        spans = [cells[i][j] for i in range(first, len(data)) if j < len(cells[i]) and cells[i][j]]
        bounds.append((min(s[0] for s in spans), max(s[2] for s in spans)) if spans else None)
    top = cap_rect.y1 if cap_rect.y1 <= table.rows[first].bbox[1] else table.bbox[1]
    header_area = pymupdf.Rect(table.bbox[0] - 2, top, table.bbox[2] + 2, table.rows[first].bbox[1])
    header = _names(_lines(_words(page, header_area, [cap_rect])), bounds)
    if (promote and not any(header) and not any(any(row) for row in data[:first])
            and any(any(row) for row in data[first + 1:])
            and all(re.search(r"[^\W\d_]", c) for c in data[first] if re.search(r"\d", c))):
        return data[first], [row for row in data[first + 1:] if any(row)]
    return header, [row for row in data[first:] if any(row)]


def _from_words(page, cap_rect, others):
    """Fallback for tables without ruling lines: rebuild rows and columns from word positions.

    The table is read below its caption until a long text block, another caption or the page
    margin. Returns (header, rows, rect, footnotes); empty when no data rows are found.
    """
    body = [pymupdf.Rect(b) for b in _long_blocks(page)]
    stop = min([r.y0 for r in body if r.y0 > cap_rect.y1 + 5 and _overlap_x(r, cap_rect) > 0]
               + [pymupdf.Rect(o["bbox"]).y0 for o in others if o["bbox"][1] > cap_rect.y1]
               + [page.rect.y1 * (1 - extract.MARGIN)])
    area = pymupdf.Rect(max(page.rect.x0, cap_rect.x0 - 40), cap_rect.y1, page.rect.x1 - 20, stop)
    lines = _lines(_words(page, area, [cap_rect] + body))
    for ln in lines:
        ln["cells"] = _cells(ln)
    candidates = [i for i, ln in enumerate(lines) if _is_data_row([c["text"] for c in ln["cells"]])]
    left = min((lines[i]["cells"][0]["x0"] for i in candidates), default=0)
    data_idx = [i for i in candidates  # a data row starts with a row label in the leftmost column
                if lines[i]["cells"][0]["x0"] <= left + 5 and re.search(r"[A-Za-z]", lines[i]["cells"][0]["text"])]
    if len(data_idx) < 2:
        return [], [], None, None
    first, last = data_idx[0], data_idx[-1]
    # columns: merge the horizontal ranges of the cells of data lines
    spans = sorted((c["x0"], c["x1"]) for i in data_idx for c in lines[i]["cells"])
    bounds = []
    for x0, x1 in spans:
        if bounds and x0 <= bounds[-1][1] + 2:
            bounds[-1] = (bounds[-1][0], max(bounds[-1][1], x1))
        else:
            bounds.append((x0, x1))
    logical = []
    for index, ln in enumerate(lines[first:last + 1], start=first):
        starts_row = index in data_idx
        if starts_row or not logical:
            logical.append([])
        logical[-1].append(ln)
    rows = []
    for logical_lines in logical:
        row = [""] * len(bounds)
        for ln in logical_lines:
            for c in ln["cells"]:
                centre = (c["x0"] + c["x1"]) / 2
                j = min(range(len(bounds)), key=lambda k: 0 if bounds[k][0] <= centre <= bounds[k][1]
                        else min(abs(centre - bounds[k][0]), abs(centre - bounds[k][1])))
                row[j] = _clean(f"{row[j]} {c['text']}")
        if any(row):
            rows.append(row)
    header = _names(lines[:first], bounds)
    after = [" ".join(t for _, t in ln["words"]) for ln in lines[last + 1:]]
    notes = [a for a in after if FOOTNOTE.search(a)]
    rect = pymupdf.Rect(bounds[0][0], lines[first]["y0"], bounds[-1][1], lines[last]["y1"])
    if first:
        rect |= pymupdf.Rect(bounds[0][0], lines[0]["y0"], bounds[-1][1], lines[0]["y1"])
    return header, rows, rect, (" ".join(notes) or None)


def _from_rotated_words(page, cap_rect):
    """Read a table printed 90 degrees on a portrait page using span directions.

    PyMuPDF exposes these text spans with a vertical direction and page-space bboxes.
    In these orientations, page x identifies logical rows and page y identifies columns.
    """
    if cap_rect.height <= cap_rect.width * 1.5:
        return [], [], None, None
    rotated = []
    for block in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"]:
        for line in block.get("lines", []):
            dx, dy = line.get("dir", (1, 0))
            if abs(dx) >= 0.2 or abs(dy) <= 0.8:
                continue
            rect = pymupdf.Rect(line["bbox"])
            text = _clean(" ".join(span["text"] for span in line["spans"]))
            if text:
                rotated.append((rect, text, dy))
    caption_directions = [dy for rect, _, dy in rotated if rect.intersects(cap_rect)]
    if not caption_directions:
        return [], [], None, None
    direction = 1 if sum(caption_directions) > 0 else -1

    def frame(rect):
        # The 270-degree table's successive rows lie to the left of its caption.
        # Reflect only its row axis; the source y-axis still determines columns.
        if direction > 0:
            return pymupdf.Rect(page.rect.width - rect.x1, rect.y0,
                                page.rect.width - rect.x0, rect.y1)
        return rect

    cap = frame(cap_rect)
    rotated = [(frame(r), text, dy) for r, text, dy in rotated
               if not r.intersects(cap_rect) and dy * direction > 0]
    rotated = [(r, text, dy) for r, text, dy in rotated if cap.x0 + 12 <= r.x0 <= cap.x0 + 175]

    # The first table column contains row labels near the high-y edge. Those positions
    # identify each logical row without guessing at the values in the remaining cells.
    labels = [(r, text) for r, text, _ in rotated
              if ((r.y0 + r.y1) / 2 > page.rect.height - 110 if direction < 0
                  else (r.y0 + r.y1) / 2 < 110)]
    row_starts = sorted({round(r.x0, 1) for r, _ in labels})
    if len(row_starts) < 2:
        return [], [], None, None

    first_row_x = row_starts[0]
    header_parts = [(r, text) for r, text, _ in rotated if r.x0 < first_row_x - 4]
    header_parts.sort(key=lambda item: (direction * (item[0].y0 + item[0].y1) / 2, item[0].x0))
    header_groups = []
    for rect, text in header_parts:
        centre = (rect.y0 + rect.y1) / 2
        if header_groups and abs(header_groups[-1]["centre"] - centre) <= 16:
            header_groups[-1]["parts"].append((rect, text))
            centres = [((r.y0 + r.y1) / 2) for r, _ in header_groups[-1]["parts"]]
            header_groups[-1]["centre"] = sum(centres) / len(centres)
        else:
            header_groups.append({"centre": centre, "parts": [(rect, text)]})
    if len(header_groups) < 2:
        return [], [], None, None

    headers = [""]
    centres = []
    for group in header_groups:
        text = _clean(" ".join(text for _, text in sorted(group["parts"], key=lambda p: p[0].x0)))
        headers.append(re.sub(r"\s*/\s*", "/", text))
        centres.append(group["centre"])

    rows = []
    for i, x0 in enumerate(row_starts):
        x1 = row_starts[i + 1] if i + 1 < len(row_starts) else x0 + 26
        parts = [(r, text) for r, text, _ in rotated if x0 - 1 <= r.x0 < x1 - 1]
        cells = [[] for _ in headers]
        for rect, text in parts:
            centre = (rect.y0 + rect.y1) / 2
            is_row_label = (centre > max(centres) + 18 if direction < 0 else centre < min(centres) - 18)
            if is_row_label:
                column = 0
            else:
                column = 1 + min(range(len(centres)), key=lambda j: abs(centres[j] - centre))
                if abs(centres[column - 1] - centre) > 28:
                    continue
            cells[column].append((rect.x0, text))
        row = [re.sub(r"\s*/\s*", "/", _clean(" ".join(text for _, text in sorted(cell))))
               for cell in cells]
        if any(row):
            rows.append(row)
    if len(rows) < 2:
        return [], [], None, None

    rects = [r for r, _, _ in rotated]
    if not rects:
        return [], [], None, None
    table_rect = _union(rects)
    outer_notes = [text for r, text, _ in rotated if r.x0 >= row_starts[-1] + 25]
    return headers, rows, frame(table_rect), (" ".join(outer_notes) or None)


def _long_blocks(page):
    return [b["bbox"] for b in page.get_text("dict", flags=extract.TEXT_FLAGS)["blocks"] if _is_paragraph(b)]


def _is_caption(text):
    return extract._CAPTION.match(text.strip()) is not None


def build(paper_id, doc, captions, source, crop_dir):
    """Figures and tables of one PDF (main paper or a supplementary PDF).

    captions: [{page, bbox, text}] from extract.Extraction.captions.
    Returns (figures, tables); each has a private "_caption" (text) used to find its sentences.
    """
    figures, tables, seen = [], [], set()
    by_page = {}
    for c in captions:
        by_page.setdefault(c["page"], []).append(c)
    table_cache = {}
    for c in captions:
        lab = label_of(c["text"])
        if not lab:
            continue
        kind, supp, number = lab
        supp = supp or source != "main"
        oid = object_id(paper_id, kind, supp, number)
        if oid in seen:
            continue
        seen.add(oid)
        page = doc[c["page"] - 1]
        cap_rect = pymupdf.Rect(c["bbox"])
        crop_name = oid.split(":")[-1] + ".jpg"
        if kind == "table":
            if c["page"] not in table_cache:
                try:
                    table_cache[c["page"]] = list(page.find_tables().tables)
                except Exception:
                    table_cache[c["page"]] = []
            found = [t for t in table_cache[c["page"]] if len(t.extract()) >= 2 and t.col_count >= 2]
            below = sorted((t for t in found if t.bbox[1] >= cap_rect.y0 - 5), key=lambda t: t.bbox[1] - cap_rect.y1)
            above = sorted((t for t in found if t.bbox[3] <= cap_rect.y1 + 5), key=lambda t: cap_rect.y0 - t.bbox[3])
            chosen = (below or above or [None])[0]
            header, rows, rect, footnotes, method = [], [], None, None, "text_fallback"
            sideways = False
            if chosen is not None:
                table_cache[c["page"]].remove(chosen)
                sideways = _sideways(page, pymupdf.Rect(chosen.bbox))
                header, rows = _from_finder(page, chosen, cap_rect, promote=not sideways)
                if rows:
                    rect, method = pymupdf.Rect(chosen.bbox), "pymupdf_tables"
                    footnotes = _footnotes(page, rect)
            if not rows:
                header, rows, rect, footnotes = _from_rotated_words(page, cap_rect)
                method = "word_positions" if rows else "text_fallback"
            if not rows:
                others = [o for o in by_page[c["page"]] if o is not c]
                header, rows, rect, footnotes = _from_words(page, cap_rect, others)
                method = "word_positions" if rows else "text_fallback"
            crop = None
            if rect is not None and _crop(page, rect | cap_rect, crop_dir / crop_name):
                crop = f"crops/{crop_name}"
            tables.append({
                "table_id": oid, "label": display_label(kind, supp, number), "source": source, "page": c["page"],
                "caption_sentence_ids": [], "header": header, "rows": rows,
                "bbox": [round(v, 1) for v in rect] if rect is not None else None, "crop": crop,
                "footnotes": footnotes, "extraction": method,
                "quality": "ok" if method == "pymupdf_tables" and not sideways and any(header) else "uncertain",
                "mentioned_in": [],
                "_caption": c["text"]})
        else:
            rect = _figure_rect(page, c, by_page[c["page"]])
            crop = None
            if rect is not None and _crop(page, rect | cap_rect, crop_dir / crop_name):
                crop = f"crops/{crop_name}"
            figures.append({
                "figure_id": oid, "label": display_label(kind, supp, number), "source": source, "page": c["page"],
                "bbox": [round(v, 1) for v in rect] if rect is not None else None, "crop": crop,
                "caption_sentence_ids": [], "mentioned_in": [], "_caption": c["text"]})
    return figures, tables


def row_text(header, row, with_names=True):
    """'Group: Control | n: 120 | Blastocyst rate (%): 32.5 ± 2.1' (cells without a header stay bare).

    with_names=False (tables rebuilt from word positions, quality "uncertain"): cells only, in
    printed order, because a column could be misaligned and a wrong column name would be
    worse than none.
    """
    cells = []
    for i, value in enumerate(row):
        if not value:
            continue
        name = header[i] if with_names and i < len(header) else ""
        cells.append(f"{name}: {value}" if name else value)
    return " | ".join(cells)
