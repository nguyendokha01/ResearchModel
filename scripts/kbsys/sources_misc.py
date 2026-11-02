"""Ingest non-PDF personal sources without adding extraction dependencies.

The durable record for these inputs lives below ``knowledge_store/sources`` and
is intentionally separate from the paper catalogue.  This module does not
claim that OCR or speech recognition happened: images and media without an
approved sidecar remain explicitly pending.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

import yaml

from . import config, db
from .sentences import fts_variants

try:
    from .sentences import split_sentences as _project_split_sentences
except (ImportError, ModuleNotFoundError):  # pragma: no cover - defensive fallback
    _project_split_sentences = None


SUPPORTED = {
    ".docx": "docx",
    ".md": "note",
    ".txt": "note",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".gif": "image",
    ".webp": "image",
    ".tif": "image",
    ".mp4": "video",
    ".mov": "video",
    ".mkv": "video",
    ".mp3": "audio",
    ".wav": "audio",
    ".m4a": "audio",
}
TRANSCRIPT_SUFFIXES = (".vtt", ".srt", ".txt")
SOURCE_ID_RE = re.compile(r"src_[0-9a-f]{16}")
TIMESTAMP_RE = re.compile(
    r"(?P<start>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})\s+-->\s+"
    r"(?P<end>\d{1,2}:\d{2}(?::\d{2})?[.,]\d{3})(?:\s+.*)?$"
)
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
Q = lambda name: "{" + W_NS + "}" + name

# Keep these markers compatible with the existing note convention while not
# importing the paper-note renderer (which would make this small adapter
# depend on the rest of the vault renderer).
AUTO_START = "<!-- kb:auto:start -->"
AUTO_END = "<!-- kb:auto:end -->"

__all__ = [
    "import_source", "list_sources", "show_source", "index_sources",
    "search_sources", "render_note",
]

NOT_PUBLISHED_LABEL = "không phải bằng chứng đã công bố"


def _source_root() -> Path:
    return config.STORE / "sources"


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _source_id(sha256: str) -> str:
    """Return a stable, path-safe identifier derived only from file bytes."""
    return "src_" + sha256[:16]


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temporary.replace(path)


def _read_jsonl(path: Path):
    if not path.is_file():
        return []
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    temporary.replace(path)


def _split(text: str):
    text = text.strip()
    if not text:
        return []
    if _project_split_sentences is not None:
        spans = _project_split_sentences(text)
        return [text[start:end].strip() for start, end in spans if text[start:end].strip()]
    # A deliberately small fallback for a future checkout where the project
    # helper is unavailable.  It never rewrites the stored source paragraph.
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]


def _paragraphs(text: str):
    return [part.strip() for part in re.split(r"(?:\r?\n){2,}", text) if part.strip()]


def _sentence_rows(source_id: str, text: str, locator: dict, seq: int, **extra):
    rows = []
    for sentence in _split(text):
        row = {
            "sentence_id": f"{source_id}:S{seq:04d}",
            "seq": seq,
            "text": sentence,
            "locator": locator,
        }
        row.update(extra)
        rows.append(row)
        seq += 1
    return rows, seq


def _docx_text(element) -> str:
    parts = []
    for node in element.iter():
        if node.tag == Q("t"):
            parts.append(node.text or "")
        elif node.tag == Q("tab"):
            parts.append("\t")
        elif node.tag in (Q("br"), Q("cr")):
            parts.append("\n")
    return "".join(parts).strip()


def _docx_heading(paragraph):
    properties = paragraph.find(Q("pPr"))
    style = properties.find(Q("pStyle")) if properties is not None else None
    value = style.get(Q("val"), "") if style is not None else ""
    match = re.fullmatch(r"Heading(\d+)", value, re.I)
    return int(match.group(1)) if match else None


def _docx_cell(cell):
    paragraphs = [_docx_text(p) for p in cell.findall(Q("p"))]
    return " ".join(part for part in paragraphs if part)


def _parse_docx(path: Path, source_id: str, attachment_dir: Path):
    rows, seq, paragraph_index, table_index = [], 1, 0, 0
    attachments = []
    with zipfile.ZipFile(path) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
        body = root.find(Q("body"))
        if body is None:
            raise ValueError("DOCX has no word/document.xml body")
        for child in body:
            if child.tag == Q("p"):
                paragraph_index += 1
                text = _docx_text(child)
                if not text:
                    continue
                heading_level = _docx_heading(child)
                locator = {
                    "kind": "paragraph",
                    "index": paragraph_index,
                    "paragraph": paragraph_index,
                    "paragraph_index": paragraph_index,
                }
                if heading_level is not None:
                    locator["heading_level"] = heading_level
                new_rows, seq = _sentence_rows(source_id, text, locator, seq, heading_level=heading_level)
                rows.extend(new_rows)
            elif child.tag == Q("tbl"):
                table_index += 1
                for row_index, tr in enumerate(child.findall(Q("tr")), start=1):
                    cells = [_docx_cell(tc) for tc in tr.findall(Q("tc"))]
                    if not any(cells):
                        continue
                    text = " | ".join(cells)
                    locator = {
                        "kind": "table",
                        "index": table_index,
                        "table": table_index,
                        "table_index": table_index,
                        "row": row_index,
                        "row_index": row_index,
                    }
                    new_rows, seq = _sentence_rows(source_id, text, locator, seq, cells=cells)
                    rows.extend(new_rows)
        for name in sorted(archive.namelist()):
            posix = PurePosixPath(name)
            if not name.startswith("word/media/") or not posix.name:
                continue
            filename = posix.name
            attachment_dir.mkdir(parents=True, exist_ok=True)
            (attachment_dir / filename).write_bytes(archive.read(name))
            attachments.append(filename)
    return rows, attachments


def _sidecar(path: Path, suffixes):
    for suffix in suffixes:
        candidate = path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def _parse_text_source(path: Path, source_id: str):
    rows, seq = [], 1
    text = path.read_text(encoding="utf-8")
    for index, paragraph in enumerate(_paragraphs(text), start=1):
        new_rows, seq = _sentence_rows(
            source_id, paragraph,
            {"kind": "paragraph", "index": index, "paragraph": index,
             "paragraph_index": index}, seq,
        )
        rows.extend(new_rows)
    return rows


def _parse_timestamp(value: str) -> float:
    value = value.replace(",", ".")
    parts = value.split(":")
    if len(parts) == 2:
        minutes, seconds = parts
        return float(minutes) * 60 + float(seconds)
    hours, minutes, seconds = parts
    return float(hours) * 3600 + float(minutes) * 60 + float(seconds)


def _parse_transcript(path: Path, source_id: str):
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".txt":
        return _parse_text_source(path, source_id), False
    rows, seq = [], 1
    blocks = re.split(r"\r?\n\s*\r?\n", text.replace("\ufeff", ""))
    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        match_index = next((i for i, line in enumerate(lines) if TIMESTAMP_RE.match(line)), None)
        if match_index is None:
            continue
        match = TIMESTAMP_RE.match(lines[match_index])
        caption = " ".join(lines[match_index + 1:]).strip()
        if not caption:
            continue
        start, end = match.group("start"), match.group("end")
        locator = {
            "kind": "time_range",
            "time_start": start,
            "time_end": end,
            "time_start_seconds": _parse_timestamp(start),
            "time_end_seconds": _parse_timestamp(end),
        }
        new_rows, seq = _sentence_rows(
            source_id, caption, locator, seq,
            time_start=start, time_end=end,
        )
        rows.extend(new_rows)
    return rows, True


def _find_by_sha256(sha256: str):
    root = _source_root()
    if not root.is_dir():
        return None
    for metadata_path in root.glob("*/metadata.json"):
        metadata = _read_json(metadata_path)
        if metadata and metadata.get("sha256") == sha256:
            return metadata_path.parent, metadata
    return None


def _metadata_for(path: Path, source_id: str, sha256: str, vault: str, title: str):
    kind = SUPPORTED[path.suffix.lower()]
    evidence_class = "human_note" if kind in ("docx", "note", "image") else "own_data"
    return {
        "source_id": source_id,
        "source_kind": kind,
        "title": title,
        "original_path": str(path.resolve()),
        "sha256": sha256,
        "size": path.stat().st_size,
        "imported_at": datetime.now().isoformat(timespec="seconds"),
        "vaults": [vault],
        "ingest_status": "ready",
        "evidence_class": evidence_class,
        "published_evidence": False,
    }


def _index_connection():
    """Open the store-local index without relying on an import-time DB path.

    Tests and isolated workspaces patch ``config.STORE`` but intentionally do
    not need to patch every derived config path.  The production database also
    lives at this location.
    """
    path = config.STORE / "library.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(path)


def _index_one_source(source_id: str, con=None):
    """Use the caller's writer, or own one connection for a standalone import."""
    if con is not None:
        # The caller owns this transaction. Never open a competing writer or
        # commit unrelated pending changes on the caller's behalf.
        return index_sources(con, source_ids=[source_id])

    owned_con = _index_connection()
    try:
        with owned_con:
            return index_sources(owned_con, source_ids=[source_id])
    finally:
        owned_con.close()


def import_source(file, vault: str, title: str | None = None, con=None):
    """Import one non-PDF source and return its metadata plus duplicate status.

    Callers holding a library connection must pass ``con=their_connection``;
    indexing then uses that transaction and the caller commits or rolls back.
    A standalone import owns, commits and closes its indexing connection.
    """
    path = Path(file).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    suffix = path.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"unsupported source type: {suffix or '<none>'}")
    if not vault or not isinstance(vault, str):
        raise ValueError("vault is required")

    sha256 = _hash(path)
    duplicate = _find_by_sha256(sha256)
    if duplicate is not None:
        folder, metadata = duplicate
        vaults = list(metadata.get("vaults") or [])
        if vault not in vaults:
            vaults.append(vault)
            metadata["vaults"] = vaults
            _write_json(folder / "metadata.json", metadata)
        _index_one_source(metadata["source_id"], con)
        return {**metadata, "duplicate": True}

    source_id = _source_id(sha256)
    folder = _source_root() / source_id
    metadata = _metadata_for(path, source_id, sha256, vault, title or path.stem)
    original_dir = folder / "original"
    attachment_dir = folder / "attachments"
    original_dir.mkdir(parents=True, exist_ok=False)
    attachment_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, original_dir / path.name)

    if suffix == ".docx":
        sentences, attachments = _parse_docx(path, source_id, attachment_dir)
        metadata["attachments"] = attachments
    elif suffix in (".md", ".txt"):
        sentences = _parse_text_source(path, source_id)
    elif suffix in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif"):
        shutil.copy2(path, attachment_dir / path.name)
        sidecar = _sidecar(path, (".txt", ".md"))
        if sidecar is None:
            sentences = []
            metadata["ingest_status"] = "needs_ocr"
            metadata["dependency_note"] = "needs dependency approval"
        else:
            sentences = _parse_text_source(sidecar, source_id)
            metadata["sidecar_path"] = str(sidecar.resolve())
    else:
        sidecar = _sidecar(path, TRANSCRIPT_SUFFIXES)
        if sidecar is None:
            sentences = []
            metadata["ingest_status"] = "needs_transcript"
            metadata["dependency_note"] = "needs dependency approval"
        else:
            sentences, timestamped = _parse_transcript(sidecar, source_id)
            metadata["transcript_path"] = str(sidecar.resolve())
            metadata["transcript_format"] = sidecar.suffix.lower().lstrip(".")
            metadata["transcript_timestamped"] = timestamped

    _write_json(folder / "metadata.json", metadata)
    _write_jsonl(folder / "sentences.jsonl", sentences)
    _index_one_source(source_id, con)
    return {**metadata, "duplicate": False}


def _metadata_paths():
    return sorted(_source_root().glob("*/metadata.json")) if _source_root().is_dir() else []


def list_sources(vault: str):
    """Return source metadata linked to one vault, in stable ID order."""
    result = []
    for path in _metadata_paths():
        metadata = _read_json(path)
        if metadata and vault in (metadata.get("vaults") or []):
            result.append(metadata)
    return result


def show_source(source_id: str):
    if not SOURCE_ID_RE.fullmatch(source_id or ""):
        raise ValueError(f"invalid source id: {source_id!r}")
    folder = _source_root() / source_id
    metadata = _read_json(folder / "metadata.json")
    if metadata is None:
        raise FileNotFoundError(source_id)
    return {
        **metadata,
        "sentences": _read_jsonl(folder / "sentences.jsonl"),
        "attachments": sorted(p.name for p in (folder / "attachments").iterdir())
        if (folder / "attachments").is_dir() else [],
    }


def _ensure_source_fts(con):
    con.execute(
        f"""CREATE VIRTUAL TABLE IF NOT EXISTS source_sentences_fts USING fts5(
               sentence_id UNINDEXED,
               source_id UNINDEXED,
               vault UNINDEXED,
               text,
               locator UNINDEXED,
               source_kind UNINDEXED,
               {db.FTS_TOKENIZE}
           )"""
    )


def index_sources(con, source_ids=None):
    """Index imported source sentences, rebuilding only requested sources.

    ``source_ids=None`` retains the D3.52 full-rebuild helper used by explicit
    maintenance commands.  Imports pass one ID so an incremental ingest never
    rewrites unrelated source rows.
    """
    selected = None if source_ids is None else {str(value) for value in source_ids}
    if selected is None:  # recreated, so a full rebuild takes the current tokenizer (CHR-04)
        con.execute("DROP TABLE IF EXISTS source_sentences_fts")
    _ensure_source_fts(con)
    if selected:
        placeholders = ",".join("?" for _ in selected)
        con.execute(f"DELETE FROM source_sentences_fts WHERE source_id IN ({placeholders})",
                    sorted(selected))
    count = 0
    for metadata_path in _metadata_paths():
        metadata = _read_json(metadata_path)
        if not metadata:
            continue
        if selected is not None and metadata.get("source_id") not in selected:
            continue
        sentences = _read_jsonl(metadata_path.parent / "sentences.jsonl")
        for vault in metadata.get("vaults") or []:
            for sentence in sentences:
                con.execute(
                    "INSERT INTO source_sentences_fts "
                    "(sentence_id, source_id, vault, text, locator, source_kind) VALUES (?,?,?,?,?,?)",
                    (sentence["sentence_id"], metadata["source_id"], vault,
                     sentence.get("text", ""), json.dumps(sentence.get("locator") or {}, ensure_ascii=False),
                     metadata.get("source_kind", "")),
                )
                count += 1
    return count


def _source_kind_label(source_kind: str) -> str:
    """Map storage types to the deliberately non-evidence search labels."""
    return {
        "docx": "docx",
        "video": "transcript",
        "audio": "transcript",
        "note": "human_note",
        "image": "human_note",
    }.get(source_kind or "", "own_data")


def _locator_label(locator: dict) -> str:
    kind = locator.get("kind")
    if kind == "time_range":
        start, end = locator.get("time_start"), locator.get("time_end")
        if start or end:
            return f"{start or '?'}–{end or '?'}"
    if kind == "table":
        table = locator.get("table", locator.get("index", "?"))
        row = locator.get("row", locator.get("row_index", "?"))
        return f"table {table}, row {row}"
    if kind == "paragraph":
        return f"paragraph {locator.get('paragraph', locator.get('index', '?'))}"
    return ", ".join(f"{key}={value}" for key, value in sorted(locator.items())) or "source"


def _source_metadata(source_id: str):
    return _read_json(_source_root() / source_id / "metadata.json") or {}


def search_sources(con, query: str, vault: str | None = None, limit: int = 20):
    """Small provider-free search helper for later ``kb.py``/dashboard wiring."""
    _ensure_source_fts(con)
    terms, glyph_tokens = [], db.fts_glyph_tokens(con, ("source_sentences_fts",))
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', query or ""):
        term = (phrase or word).strip()
        if not term or term.upper() in ("AND", "OR", "NOT"):
            continue
        exprs = ['"' + v.replace('"', ' ') + '"' for v in fts_variants(term, glyph_tokens)]  # CHR-01/04
        terms.append(exprs[0] if len(exprs) == 1 else "(" + " OR ".join(exprs) + ")")
    if not terms:
        return []
    match = " AND ".join(terms)
    args = [match]
    sql = ("SELECT sentence_id, source_id, vault, text, locator, source_kind, "
           "bm25(source_sentences_fts) AS bm25 FROM source_sentences_fts "
           "WHERE source_sentences_fts MATCH ?")
    if vault is not None:
        sql += " AND vault = ?"
        args.append(vault)
    sql += " ORDER BY bm25 LIMIT ?"
    args.append(max(1, int(limit)))
    result = []
    for row in con.execute(sql, args):
        metadata = _source_metadata(row[1])
        locator = json.loads(row[4] or "{}")
        result.append({
            "sentence_id": row[0], "source_id": row[1], "vault": row[2], "text": row[3],
            "locator": locator, "source_kind": row[5], "bm25": row[6],
            "title": metadata.get("title") or row[1],
            "kind": _source_kind_label(row[5]),
            "evidence_class": metadata.get("evidence_class") or "own_data",
            "published_evidence": False,
            "not_published_evidence": NOT_PUBLISHED_LABEL,
            "label": NOT_PUBLISHED_LABEL,
            "locator_label": _locator_label(locator),
            "locator_text": _locator_label(locator),
        })
    return result


def render_note(source_id: str) -> str:
    """Return an Obsidian source note without writing to the human-facing vault."""
    source = show_source(source_id)
    frontmatter = yaml.safe_dump({
        "type": "source",
        "source_kind": source["source_kind"],
        "ingest_status": source["ingest_status"],
        "id": source["source_id"],
        "original_path": source["original_path"],
        "sha256": source["sha256"],
    }, allow_unicode=True, sort_keys=False).rstrip()
    body = ["---", frontmatter, "---", "", f"# {source['title']}", "", AUTO_START, "", "## Summary", ""]
    if source["sentences"]:
        body.extend(f"- {sentence['text']}" for sentence in source["sentences"])
    else:
        body.append("- No text layer is available.")
    attachments = source.get("attachments") or []
    if attachments:
        body.extend(["", "## Attachments", ""])
        body.extend(f"- ![](_Attachments/{source_id}/{name})" for name in attachments)
    body.extend(["", AUTO_END, "", "## My notes", ""])
    return "\n".join(body)


def _cli():
    parser = argparse.ArgumentParser(prog="python -m kbsys.sources_misc")
    commands = parser.add_subparsers(dest="command", required=True)
    import_parser = commands.add_parser("import")
    import_parser.add_argument("file")
    import_parser.add_argument("--vault", required=True)
    import_parser.add_argument("--title")
    list_parser = commands.add_parser("list")
    list_parser.add_argument("--vault", required=True)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("source_id")
    args = parser.parse_args()
    try:
        if args.command == "import":
            result = import_source(args.file, args.vault, args.title)
        elif args.command == "list":
            result = list_sources(args.vault)
        else:
            result = show_source(args.source_id)
    except (FileNotFoundError, ValueError, zipfile.BadZipFile) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":  # pragma: no cover - exercised through subprocess by users
    _cli()
