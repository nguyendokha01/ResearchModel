"""Write vault-facing notes for imported non-PDF sources.

Only the ``kb:auto`` block is machine-owned.  The rest of a source note is
left byte-for-byte untouched on refresh so a researcher can keep notes beside
the generated source summary.
"""

from __future__ import annotations

import re
import sqlite3
import shutil
from pathlib import Path

from . import concepts, config, db, notes, presentation, sources_misc


SOURCES_DIR = "Sources"
AUTO_START = sources_misc.AUTO_START
AUTO_END = sources_misc.AUTO_END
_SOURCE_ID = re.compile(r"(?m)^id:\s*['\"]?(src_[0-9a-f]{16})['\"]?\s*$")


def _registry():
    """Prefer the durable registry, then fall back to the local paper index."""
    registry = concepts.load_registry()
    if registry.records:
        return registry
    path = config.STORE / "library.db"
    if not path.is_file():
        return registry
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        con.executescript(db.SCHEMA)
        return concepts.load_registry(con)
    finally:
        con.close()


def _node_path(vault_dir: Path, record: dict):
    type_key = concepts.TYPE_KEYS.get(record.get("type"))
    folder = notes.TERM_FOLDERS.get(type_key)
    if folder not in ("Substances", "Methods", "Organisms", "Concepts"):
        return None
    expected = vault_dir / folder / f"{notes.safe_filename(record['name'])}.md"
    if expected.is_file():
        return expected
    # A human may have retained a case or punctuation variant of the canonical
    # filename.  Resolve it only within the registry-selected node folder.
    wanted = expected.stem.casefold()
    for candidate in sorted((vault_dir / folder).glob("*.md")) if (vault_dir / folder).is_dir() else ():
        if candidate.stem.casefold() == wanted:
            return candidate
    return None


def _links_for_source(source: dict, vault_dir: Path):
    text = "\n".join([source.get("title") or ""] + [row.get("text", "") for row in source.get("sentences") or []])
    links = []
    for record in _registry().records:
        target = _node_path(vault_dir, record)
        if target is None:
            continue
        if not presentation.matches_term(text, record["name"], record.get("synonyms") or ()):
            continue
        relative = target.relative_to(vault_dir).with_suffix("").as_posix()
        alias = str(record["name"]).replace("|", "/").replace("[", "(").replace("]", ")")
        links.append(f"[[{relative}|{alias}]]")
        if len(links) >= 15:
            break
    return links


def _with_links(rendered: str, source: dict, vault_dir: Path) -> str:
    end = rendered.find(AUTO_END)
    if end < 0:
        return rendered
    links = _links_for_source(source, vault_dir)
    if not links:
        return rendered
    section = "\n\n## Linked nodes\n\n" + "\n".join(f"- {link}" for link in links) + "\n\n"
    return rendered[:end].rstrip() + section + rendered[end:]


def _find_existing(source_dir: Path, source_id: str):
    if not source_dir.is_dir():
        return None
    for path in sorted(source_dir.glob("*.md")):
        text = path.read_text(encoding="utf-8")
        match = _SOURCE_ID.search(text)
        if match and match.group(1) == source_id:
            return path
    return None


def _copy_attachments(source_id: str, vault_dir: Path):
    source_dir = config.STORE / "sources" / source_id / "attachments"
    if not source_dir.is_dir():
        return 0
    target_dir = vault_dir / "_Attachments" / source_id
    target_dir.mkdir(parents=True, exist_ok=True)
    copied = 0
    for source in sorted(source_dir.iterdir()):
        if source.is_file():
            shutil.copy2(source, target_dir / source.name)
            copied += 1
    return copied


def _note_path(source: dict, source_dir: Path):
    # The ID is stable across title edits and guarantees one note per source.
    # The human-readable title remains the note heading from render_note().
    return source_dir / f"{source['source_id']}.md"


def write(vault):
    """Refresh one ``Sources`` note per source linked to *vault*.

    Returns the number of notes created or refreshed.  Attachment copies are
    always attempted, including when an existing note has malformed markers;
    malformed human content is never replaced.
    """
    vault_dir = config.vault_dir(vault)
    source_dir = vault_dir / SOURCES_DIR
    source_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for metadata in sources_misc.list_sources(str(vault)):
        source = sources_misc.show_source(metadata["source_id"])
        _copy_attachments(source["source_id"], vault_dir)
        rendered = _with_links(sources_misc.render_note(source["source_id"]), source, vault_dir)
        path = _find_existing(source_dir, source["source_id"]) or _note_path(source, source_dir)
        if path.exists():
            old = path.read_text(encoding="utf-8")
            start, end = old.find(AUTO_START), old.find(AUTO_END)
            if start < 0 or end < start:
                continue
            generated_start, generated_end = rendered.find(AUTO_START), rendered.find(AUTO_END)
            if generated_start < 0 or generated_end < generated_start:
                continue
            new = old[:start] + rendered[generated_start:generated_end + len(AUTO_END)] + old[end + len(AUTO_END):]
            if new != old:
                path.write_text(new, encoding="utf-8", newline="\n")
        else:
            path.write_text(rendered, encoding="utf-8", newline="\n")
        written += 1
    return written


__all__ = ["write"]
