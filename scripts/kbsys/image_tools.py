"""Read-only MCP image tools (NW-12): a figure/table crop and one PDF page, each with caption/page and note links.

Both return a dict; `image` holds the PNG bytes (mcp_server moves them into an MCP image block, never into the JSON
text). Nothing here writes a file: the crop is read, the PDF is opened read-only and one page is rendered in memory.
"""
import hashlib

from . import figure_reading, find, neural, standard

DPI_MIN, DPI_MAX, DPI_DEFAULT = 50, 200, 110   # AI reading (2026-10-25): below 50 the text is unreadable, above 200 a page passes the cap
MAX_PIXELS = 16_000_000   # ~48 MB raw RGB; an A4 page at 200 dpi is ~3.9 Mpx (a 200-inch poster at 110 dpi needed ~1.4 GB)
PIXEL_NUMBERS = ("Numbers read from the image are not evidence unless they occur in the caption, a table row or a stored "
                 "figure reading (NW-12); say they were read from the image.")


def max_image_bytes():
    return int(standard.param("NW-12", "max_image_mb") * 1_000_000)


def get_figure(con, vault, paper_id, object_id):
    neural.require_paper(con, vault, paper_id)
    prefix, _, local = object_id.rpartition(":")
    if prefix and prefix != paper_id:
        raise ValueError(f"object_id {object_id!r} belongs to another paper than {paper_id!r}")
    full = f"{paper_id}:{local}"
    group = neural.media_reader(con, vault, paper_id, [full], limit=None)
    bundle = next((b for b in group["bundles"] if b["object_id"] == full), None)
    if bundle is None:
        raise ValueError(f"Unknown media object: {full}")
    image, note = None, None
    try:
        path, sha = figure_reading._verified_crop(paper_id, bundle)
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            note = "crop changed while it was read; no image returned"
        elif len(data) > max_image_bytes():
            note = f"crop is larger than {standard.param('NW-12', 'max_image_mb')} MB (NW-12); no image returned"
        else:
            image = data
    except (ValueError, OSError) as err:
        note = f"no image: {err}"
    reading = bundle.get("figure_reading")
    link = neural.paper_note_links(vault, [paper_id])[paper_id]
    return {"paper_id": paper_id, "object_id": full, "label": bundle.get("label"), "kind": bundle.get("kind"),
            "page": (bundle.get("source_locator") or {}).get("page"),
            "caption": [{"sentence_id": s.get("sentence_id"), "text": s.get("text")} for s in bundle.get("caption") or []],
            "table_rows": [{"sentence_id": s.get("sentence_id"), "text": s.get("text")} for s in bundle.get("data") or []],
            **link, "image_note": note,
            "figure_reading": {k: reading.get(k) for k in neural._MEDIA_READING_KEYS} if reading else None,
            "reading_inconsistent": bundle.get("reading_inconsistent"), "pixel_numbers": PIXEL_NUMBERS, "image": image}


def get_pdf_page(con, vault, paper_id, page, dpi=None):
    import pymupdf   # installed; render only, never save
    neural.require_paper(con, vault, paper_id)
    out = {"paper_id": paper_id, "page": page, **neural.paper_note_links(vault, [paper_id])[paper_id],
           "pixel_numbers": PIXEL_NUMBERS, "image": None}
    pdf = find.main_pdf_path(con, paper_id)
    if pdf is None:
        return {**out, "image_note": "source file unavailable (not on this machine); nothing rendered"}
    try:
        doc = pymupdf.open(pdf)
    except Exception as err:   # corrupt or not a PDF: reported, not hidden
        return {**out, "image_note": f"source file unavailable: it cannot be opened ({type(err).__name__})"}
    with doc:
        if doc.needs_pass:
            return {**out, "image_note": "source file unavailable: the PDF is password-protected"}
        out["page_count"] = doc.page_count
        if not 1 <= page <= doc.page_count:
            raise ValueError(f"page {page} is out of range: the PDF has {doc.page_count} pages")
        dpi, cap, shown = dpi or DPI_DEFAULT, max_image_bytes(), doc.load_page(page - 1)
        # RAM guard (8 GB machine): never build a pixmap over MAX_PIXELS; a page that needs one even at DPI_MIN is too large
        dpi = min(dpi, int(72 * (MAX_PIXELS / max(shown.rect.width * shown.rect.height, 1.0)) ** 0.5))
        if dpi < DPI_MIN:
            out["dpi"] = DPI_MIN
            return {**out, "image_note": f"too large: page {page} measures {shown.rect.width / 72:.0f} x "
                                         f"{shown.rect.height / 72:.0f} inches; open the PDF itself"}
        while True:
            png = shown.get_pixmap(dpi=dpi).tobytes("png")
            if len(png) <= cap or dpi <= DPI_MIN:
                break
            dpi = max(DPI_MIN, int(dpi * 0.7))
    out["dpi"] = dpi
    if len(png) > cap:
        return {**out, "image_note": f"too large: page {page} is {len(png) / 1e6:.1f} MB at {dpi} dpi, over the "
                                     f"{standard.param('NW-12', 'max_image_mb')} MB cap (NW-12); open the PDF itself"}
    return {**out, "image": png}
