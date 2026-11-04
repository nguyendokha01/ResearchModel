"""kb.py export-docx: one vault note -> a Word file with its figure images (reads the vault, writes only the .docx).

No python-docx: a .docx is a zip of small XML parts. pymupdf (already required) reads image pixel sizes.
Markdown subset only (headings, lists, quotes/callouts, tables, bold/italic, links, embeds); any other text stays plain text.
"""
import re
import zipfile
from pathlib import Path
from urllib.parse import unquote
from xml.sax.saxutils import escape

import pymupdf

from . import config, node_profile

EMU_PX = 9525                      # 1 px at 96 dpi
MAX_W, MAX_H = 6 * 914400, 8 * 914400   # 6 x 8 inch: fits a page with 1 inch margins
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tif", ".tiff"}
_BAD_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")
_TOKEN = re.compile(
    r"!\[\[(?P<we>[^\]]+?)\]\]|!\[[^\]]*\]\((?P<me>[^)]+)\)|\[\[(?P<wl>[^\]]+?)\]\]|\[(?P<mt>[^\]]+)\]\((?P<mu>[^)]*)\)"
    r"|\*\*(?=\S)(?P<b>.+?)(?<=\S)\*\*|(?<![\w*])\*(?=[^\s*])(?P<i>.+?)(?<=[^\s*])\*(?![\w*])")
_PIPE = re.compile(r"\\?\|")        # an alias starts at a pipe, escaped (\|) inside a table cell
_CELL_SPLIT = re.compile(r"(?<!\\)\|")   # an escaped \| is not a column break
_LIST = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
       'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"')
_HEAD = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _styles():
    heads = "".join(
        f'<w:style w:type="paragraph" w:styleId="Heading{n}"><w:name w:val="heading {n}"/><w:basedOn w:val="Normal"/>'
        f'<w:next w:val="Normal"/><w:qFormat/><w:pPr><w:keepNext/><w:spacing w:before="240" w:after="80"/>'
        f'<w:outlineLvl w:val="{n - 1}"/></w:pPr><w:rPr><w:b/><w:sz w:val="{40 - 4 * n}"/></w:rPr></w:style>' for n in range(1, 5))
    return (_HEAD + '<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:qFormat/>'
            '<w:pPr><w:spacing w:after="80"/></w:pPr></w:style>' + heads +
            '<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders>'
            + "".join(f'<w:{s} w:val="single" w:sz="4" w:space="0" w:color="808080"/>'
                      for s in ("top", "left", "bottom", "right", "insideH", "insideV")) +
            '</w:tblBorders></w:tblPr></w:style></w:styles>')


def find_note(vault, note):
    """Path of a note given as a vault-relative path or an Obsidian note name; ValueError outside the vault or unknown."""
    root = config.vault_dir(vault)
    rel = Path(str(note).strip().replace("\\", "/"))
    if rel.anchor or ".." in rel.parts or not rel.parts:
        raise ValueError(f"Note path must stay inside the vault: {note!r}")
    for cand in (rel, rel.with_name(rel.name + ".md")):
        path = root / cand
        if path.is_file() and path.suffix.lower() == ".md":
            if not path.resolve().is_relative_to(root.resolve()):
                raise ValueError(f"Note path must stay inside the vault: {note!r}")
            return path
    stem = node_profile._vault_note_names(vault).get(rel.name.removesuffix(".md").casefold()) if len(rel.parts) == 1 else None
    # no glob pattern (a name may hold [ ]); the checks above run again on the hit (a link out of the vault)
    path = next((p for p in root.rglob("*.md") if p.stem == stem and p.is_file()), None) if stem else None
    if path:
        return find_note(vault, path.relative_to(root))
    raise ValueError(f"Note not found in vault {vault}: {note}")


class _Doc:
    def __init__(self, root, note_dir):
        self.root, self.note_dir = root, note_dir
        self.media, self.rels, self.files = {}, [], None   # media: path -> (zip name, bytes)

    def _index(self):
        if self.files is None:  # name -> paths, _Attachments first (Obsidian resolves a bare name anywhere in the vault)
            paths = [p for p in self.root.rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_EXT]
            paths.sort(key=lambda p: ("_Attachments" not in p.relative_to(self.root).parts, p.as_posix()))
            self.files = {}
            for p in paths:
                self.files.setdefault(p.name.casefold(), p)
        return self.files

    def find_image(self, name):
        rel = Path(unquote(name.strip()).replace("\\", "/"))
        if rel.anchor or ".." in rel.parts or rel.suffix.lower() not in IMAGE_EXT:
            return None
        for base in (self.root, self.note_dir, self.root / "_Attachments"):
            path = base / rel
            if path.is_file() and path.resolve().is_relative_to(self.root.resolve()):
                return path
        path = self._index().get(rel.name.casefold())
        return path if path and path.resolve().is_relative_to(self.root.resolve()) else None   # a link/junction out

    def image_run(self, target, hint):
        """Run XML for the image, or a text run saying it is missing / unreadable."""
        path = self.find_image(target)
        if path is None:
            return run(f"[image not found: {target}]")
        try:
            if path not in self.media:
                pix = pymupdf.Pixmap(str(path))
                ext = {".png": "png", ".jpg": "jpeg", ".jpeg": "jpeg", ".gif": "gif"}.get(path.suffix.lower())
                data = path.read_bytes() if ext else pix.tobytes("png")   # Word reads these three in the parts we declare
                self.media[path] = (f"image{len(self.media) + 1}.{ext or 'png'}", data, (pix.width, pix.height))
        except (OSError, ValueError, RuntimeError, pymupdf.mupdf.FzErrorBase):   # an unreadable image file
            return run(f"[image not found: {target}]")
        name, _, (w, h) = self.media[path]
        self.rels.append(name)
        cx = min(hint * EMU_PX, MAX_W) if hint else min(w * EMU_PX, MAX_W)
        cy = round(cx * h / w) if w else cx
        if cy > MAX_H:
            cx, cy = round(cx * MAX_H / cy), MAX_H
        n = len(self.rels)
        return (f'<w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/>'
                f'<wp:docPr id="{n}" name="Picture {n}" descr="{escape(Path(name).name)}"/><a:graphic>'
                f'<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>'
                f'<pic:nvPicPr><pic:cNvPr id="{n}" name="{escape(path.name, {chr(34): "&quot;"})}"/><pic:cNvPicPr/></pic:nvPicPr>'
                f'<pic:blipFill><a:blip r:embed="rId{n + 2}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>'
                f'<pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
                f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic>'
                f'</wp:inline></w:drawing></w:r>')

    def inline(self, text, bold=False, italic=False):
        out, pos = [], 0
        for m in _TOKEN.finditer(text):
            out.append(run(text[pos:m.start()], bold, italic))
            pos = m.end()
            if m["we"] or m["me"]:
                target, _, hint = (m["we"] or m["me"]).partition("|")
                target = target.split("#")[0]
                if Path(target).suffix.lower() in IMAGE_EXT:
                    out.append(self.image_run(target, int(h.group()) if (h := re.match(r"\d+", hint.strip())) else 0))
                else:  # a note embed: the name stays as plain text
                    out.append(run(target, bold, italic))
            elif m["wl"]:
                parts = _PIPE.split(m["wl"], maxsplit=1)
                out.append(run(parts[1] if len(parts) > 1 else parts[0], bold, italic))
            elif m["mt"]:
                out.append(run(m["mt"], bold, italic))
            elif m["b"]:
                out.append(self.inline(m["b"], True, italic))
            else:
                out.append(self.inline(m["i"], bold, True))
        out.append(run(text[pos:], bold, italic))
        return "".join(out)


def run(text, bold=False, italic=False):
    text = _BAD_XML.sub("", text.replace("\\|", "|"))
    if not text:
        return ""
    props = ("<w:b/>" if bold else "") + ("<w:i/>" if italic else "")
    return f'<w:r>{f"<w:rPr>{props}</w:rPr>" if props else ""}<w:t xml:space="preserve">{escape(text)}</w:t></w:r>'


def para(runs, style=None, left=0):
    ppr = (f'<w:pStyle w:val="{style}"/>' if style else "") + (f'<w:ind w:left="{left}"/>' if left else "")
    return f'<w:p>{f"<w:pPr>{ppr}</w:pPr>" if ppr else ""}{runs}</w:p>'


def _table(doc, rows):
    rows = [r for r in rows if r is not None]
    cols = max(len(r) for r in rows)
    xml = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/></w:tblPr><w:tblGrid>'
           + '<w:gridCol w:w="2000"/>' * cols + '</w:tblGrid>']
    for n, row in enumerate(rows):
        cells = row + [""] * (cols - len(row))
        xml.append("<w:tr>" + "".join(
            f'<w:tc><w:tcPr><w:tcW w:w="0" w:type="auto"/></w:tcPr>{para(doc.inline(c.strip(), bold=n == 0))}</w:tc>'
            for c in cells) + "</w:tr>")
    return "".join(xml) + "</w:tbl>" + para("")   # Word needs a paragraph after a table


def body(doc, text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if text.startswith("---\n") and (end := text.find("\n---", 3)) > 0:
        text = text[end + 4:]
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    out, rows = [], []
    for line in text.split("\n"):
        if line.lstrip().startswith("|"):
            if len(rows) == 1 and re.fullmatch(r"\s*\|?[\s:|-]*-[\s:|-]*\|?\s*", line):   # the |---| row under the header
                rows.append(None)   # only this one: a later '| - | - |' is data
                continue
            cells = _CELL_SPLIT.split(line.strip())
            rows.append(cells[1:-1] if line.rstrip().endswith("|") and not line.rstrip().endswith("\\|") else cells[1:])
            continue
        if rows:
            out.append(_table(doc, rows))
            rows = []
        if not line.strip() or re.fullmatch(r"\s*([-*_])\1\1+\s*", line):
            continue
        if m := re.match(r"(#{1,6})\s+(.*)", line):
            out.append(para(doc.inline(m[2].strip()), f"Heading{min(len(m[1]), 4)}"))
        elif m := re.match(r"\s*>[>\s]*(.*)", line):
            quote = re.sub(r"^\[![\w-]+\][+-]?\s*", "", m[1])
            if quote.strip():
                out.append(para(doc.inline(quote, italic=True), left=567))
        elif m := _LIST.match(line):
            mark = "•" if m[2] in "-*+" else m[2]
            out.append(para(run(mark + " ") + doc.inline(m[3]), left=360 + 360 * (len(m[1].replace("\t", "    ")) // 2)))
        else:
            out.append(para(doc.inline(line.strip())))
    if rows:
        out.append(_table(doc, rows))
    return "".join(out)


def unique_path(path):
    path = Path(path)
    n = 0
    while True:
        cand = path if n == 0 else path.with_name(f"{path.stem}_{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def export(vault, note, out=None):
    """Write the note as .docx (never overwriting); returns (path, number of images). Reads the vault only."""
    config.require_research_vault(vault)
    src = find_note(vault, note)
    doc = _Doc(config.vault_dir(vault), src.parent)
    xml = body(doc, src.read_text(encoding="utf-8"))
    target = unique_path(out or config.ROOT / "runtime" / "exports" / f"{src.stem}.docx")
    if any(target.resolve().is_relative_to(d.resolve()) for d in (config.VAULTS_DIR, config.STORE)):
        raise ValueError(f"--out {target} is inside vaults/ or knowledge_store/ (export-docx runs without the writer lock): choose another folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    rels = (f'<Relationship Id="rId1" Type="{_REL}/styles" Target="styles.xml"/>'
            + "".join(f'<Relationship Id="rId{n + 2}" Type="{_REL}/image" Target="media/{name}"/>'
                      for n, name in enumerate(doc.rels, 1)))
    ct = ('<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
          '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
          '<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>'
          '<Default Extension="jpeg" ContentType="image/jpeg"/><Default Extension="gif" ContentType="image/gif"/>'
          '<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
          '<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>')
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _HEAD + ct)
        z.writestr("_rels/.rels", _HEAD + f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   f'<Relationship Id="rId1" Type="{_REL}/officeDocument" Target="word/document.xml"/></Relationships>')
        z.writestr("word/document.xml", _HEAD + f"<w:document {_NS}><w:body>{xml}</w:body></w:document>")
        z.writestr("word/_rels/document.xml.rels",
                   _HEAD + f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{rels}</Relationships>')
        z.writestr("word/styles.xml", _styles())
        for name, data, _ in doc.media.values():
            z.writestr(f"word/media/{name}", data)
    return target, len(doc.rels)
