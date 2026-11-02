"""Source, review, and paper removal actions explicitly chosen by the researcher."""

import hashlib
import shutil
import tempfile
from datetime import datetime
from pathlib import Path

import yaml

from . import config, db, notes, references


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path(value):
    path = Path(value)
    return path if path.is_absolute() else config.ROOT / path


def _stored_path(path):
    try:
        return path.resolve().relative_to(config.ROOT.resolve()).as_posix()
    except ValueError:
        return str(path.resolve())


def _main_source(meta):
    return next((source for source in meta.get("source_files", [])
                 if source.get("role") == "main"), None)


def _unique_destination(folder, name):
    folder.mkdir(parents=True, exist_ok=True)
    candidate = folder / name
    if not candidate.exists():
        return candidate
    for number in range(2, 1000):
        candidate = folder / f"{Path(name).stem}_{number}{Path(name).suffix}"
        if not candidate.exists():
            return candidate
    raise FileExistsError(f"No free destination name in {folder}")


def _transfer_verified(source, destination):
    """Copy, verify, then remove the old copy. The new copy survives a failed cleanup."""
    if source.resolve() == destination.resolve():
        raise ValueError("Source and destination are the same file")
    expected = _sha256(source)
    temporary = destination.with_name(destination.name + ".copying")
    if temporary.exists():
        raise FileExistsError(temporary)
    try:
        with open(source, "rb") as incoming, open(temporary, "xb") as outgoing:
            shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
        if _sha256(temporary) != expected:
            raise IOError("Copied PDF failed SHA-256 verification")
        temporary.rename(destination)
    finally:
        if temporary.exists():
            temporary.unlink()
    return expected


def _update_source_record(con, meta, source, destination, status):
    metadata_path = db.paper_dir(meta["paper_id"]) / "metadata.json"
    previous = metadata_path.read_bytes()
    source["path"] = _stored_path(destination)
    source["status"] = status
    meta["source_status"] = status
    meta["updated_at"] = config.now()
    try:
        db.write_json(metadata_path, meta)
        db.reload_paper(con, meta["paper_id"])
        con.commit()
    except Exception:
        con.rollback()
        metadata_path.write_bytes(previous)
        raise
    for vault in meta.get("vaults", []):
        try:
            notes.write_paper_note(meta, vault)
        except Exception as error:
            config.log(f"NOTE REFRESH NEEDED {meta['paper_id']} in {vault}: {error}")


def move_source(con, paper_id, destination_folder=None, restore=False):
    """Archive a main PDF locally/externally or restore it to active storage.

    The caller must be acting on an explicit human choice. No PDF is purged.
    """
    if not con.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
        raise ValueError(f"Unknown paper: {paper_id}")
    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json")
    if not meta:
        raise ValueError(f"Unknown paper: {paper_id}")
    record = _main_source(meta)
    if not record or not record.get("path"):
        raise ValueError("No main PDF is recorded for this paper")
    expected_status = "archived" if restore else "active"
    if record.get("status") != expected_status:
        raise ValueError(f"Main PDF status must be {expected_status}")
    source = _path(record["path"])
    if not source.is_file():
        raise FileNotFoundError(f"Original PDF is unavailable: {source}")
    if _sha256(source) != record["sha256"]:
        raise IOError("Original PDF no longer matches its recorded SHA-256")
    if restore:
        target_folder = config.ROOT / "sources" / "active" / paper_id
        new_status = "active"
    else:
        target_folder = (Path(destination_folder).expanduser() if destination_folder
                         else config.ROOT / "sources" / "archive" / "papers" / paper_id)
        if not target_folder.exists() and destination_folder:
            raise FileNotFoundError(f"Archive folder does not exist: {target_folder}")
        active_root = (config.ROOT / "sources" / "active").resolve()
        if target_folder.resolve().is_relative_to(active_root):
            raise ValueError("Archive folder cannot be inside sources/active")
        new_status = "archived"
    destination = _unique_destination(target_folder, source.name)
    if target_folder.resolve() == source.parent.resolve():
        raise ValueError("Choose a different archive folder")
    _transfer_verified(source, destination)
    try:
        _update_source_record(con, meta, record, destination, new_status)
    except Exception:
        # Keep the verified old source and remove only the newly created copy.
        destination.unlink()
        raise
    try:
        source.unlink()
    except OSError as error:
        config.log(f"EXTRA COPY retained at {source}: {error}")
        return f"Recorded {new_status} PDF at {destination}; old copy remains at {source}"
    config.log(f"SOURCE {paper_id}: {source} -> {destination} ({new_status})")
    return f"PDF {new_status}: {destination}"


def pending_reviews(con):
    from . import review_policy
    return review_policy.pending(con)


def delete_paper(con, paper_id):
    """Take one paper out of the library after explicit confirmation (reversible).

    Its store folder, notes, managed PDFs and review files are moved to
    knowledge_store/removed/deleted_<paper_id>_<time>/ (manifest.json lists where each came from)."""
    paper = con.execute("SELECT * FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()
    if not paper:
        raise ValueError(f"Unknown paper: {paper_id}")
    meta = db.read_json(db.paper_dir(paper_id) / "metadata.json", {}) or {}
    reviews = [dict(row) for row in con.execute(
        "SELECT item_id, note_name, file_path FROM review_items WHERE paper_id = ?", (paper_id,))]
    vaults = {row[0] for row in con.execute(
        "SELECT vault FROM paper_vaults WHERE paper_id = ?", (paper_id,))}
    vaults.update(meta.get("vaults", []))

    managed_roots = [
        config.ROOT / "sources" / "active",
        config.ROOT / "sources" / "archive" / "papers",
        config.ROOT / "sources" / "archive" / "rejected",
    ]
    targets = set()

    def add_managed_file(value, roots):
        if not value:
            return
        candidate = _path(value).resolve()
        if not candidate.is_file():
            return
        for root in roots:
            root = root.resolve()
            if root in candidate.parents:
                targets.add(candidate)
                return

    paper_folder = db.paper_dir(paper_id).resolve()
    papers_root = config.PAPERS_DIR.resolve()
    if paper_folder.exists() and papers_root in paper_folder.parents:
        targets.add(paper_folder)

    source_rows = [dict(row) for row in con.execute(
        "SELECT path, status FROM source_files WHERE paper_id = ?", (paper_id,))]
    source_rows.extend(item for item in meta.get("source_files", []) if item.get("path"))
    for source in source_rows:
        if source.get("status") != "external":
            add_managed_file(source.get("path"), managed_roots)

    review_root = config.ROOT / "review" / "pending"
    for item in reviews:
        add_managed_file(item.get("file_path"), [review_root])
        if item.get("item_id"):
            decision = config.ROOT / "review" / "decisions" / f"review_{item['item_id']:04d}.yaml"
            if decision.is_file():
                targets.add(decision.resolve())

    paper_note = meta.get("note_name") or paper["note_name"]
    for vault in vaults:
        vault_dir = config.vault_dir(vault).resolve()
        try:
            vault_dir.relative_to(config.VAULTS_DIR.resolve())
        except ValueError:
            continue
        if paper_note:
            note_path = (vault_dir / "Papers" / f"{paper_note}.md").resolve()
            if note_path.is_file() and vault_dir in note_path.parents:
                targets.add(note_path)
        for item in reviews:
            if item.get("note_name"):
                note_path = (vault_dir / "Review Queue" / f"{item['note_name']}.md").resolve()
                if note_path.is_file() and vault_dir in note_path.parents:
                    targets.add(note_path)

    embeddings_root = (config.STORE / "embeddings").resolve()
    embeddings = (embeddings_root / paper_id).resolve()
    if embeddings.is_dir() and embeddings_root in embeddings.parents:
        targets.add(embeddings)

    staged = config.STORE / "removed" / f"deleted_{paper_id}_{datetime.now():%Y%m%d_%H%M%S}"
    staged.mkdir(parents=True, exist_ok=False)
    staged_items = []
    committed = False
    try:
        for index, original in enumerate(sorted(targets, key=lambda p: (len(p.parts), str(p)))):
            destination = staged / str(index) / original.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(original), str(destination))
            staged_items.append((original, destination))
        db.write_json(staged / "manifest.json", {"paper_id": paper_id, "title": paper["title"],
                                                "removed_at": config.now(),
                                                "moved": [{"from": str(a), "to": str(b)} for a, b in staged_items]})

        con.execute("BEGIN IMMEDIATE")
        for table in db.PAPER_TABLES + ("review_items",):
            con.execute(f"DELETE FROM {table} WHERE paper_id = ?", (paper_id,))
        con.commit()
        committed = True

        try:  # other papers' reference lists no longer point at the deleted paper
            references.unlink(con, paper_id)
            con.commit()
        except Exception as error:
            config.log(f"PAPER DELETE {paper_id}: run `kb.py reextract --all` to refresh citation links: {error}")
        _refresh_after_removal(con, vaults, paper_id)
    except Exception:
        if not committed:
            con.rollback()
            for original, saved in reversed(staged_items):
                if saved.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(saved), str(original))
            shutil.rmtree(staged, ignore_errors=True)
        raise

    title = paper["title"] or paper_id
    config.log(f"PAPER REMOVED {paper_id}: {title} -> {config.rel(staged)}")
    return {"paper_id": paper_id, "title": title, "files_moved": len(targets), "moved_to": config.rel(staged)}


def _refresh_after_removal(con, vaults, paper_id=None):
    """After papers leave the library: concept registry/links, cited-paper registry, relation records and
    notes must not keep pointing at them. Relation records of removed units are archived (reversible)."""
    from . import concepts, ghosts, pipeline
    where = f"PAPER DELETE {paper_id}" if paper_id else "LIBRARY RESET"
    try:
        if paper_id:
            concepts.relink_paper(con, paper_id)  # drops concepts only this paper supported
        else:
            concepts.rebuild_links(con)
        ghosts.rebuild(con)
        path = config.STORE / "relations.jsonl"
        rows = list(db.read_jsonl(path)) if path.exists() else []
        prefix = f"{paper_id}:" if paper_id else ""
        gone = [r for r in rows if any(str(r.get(k) or "").startswith(prefix) for k in ("from_unit", "to_unit"))]
        if gone:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            db.write_jsonl(config.STORE / "removed" / f"relations_{paper_id or 'library'}_{stamp}.jsonl", gone)
            db.write_jsonl(path, [r for r in rows if r not in gone])
        con.commit()
    except Exception as error:
        config.log(f"{where}: run `kb.py link-concepts` and `kb.py rebuild-ghosts`: {error}")
    for vault in vaults:
        try:
            pipeline.rebuild_vault_notes(con, vault, say=lambda *_: None)  # same routine as `kb.py notes`
        except Exception as error:
            config.log(f"{where}: note refresh needed for {vault} (`kb.py notes --vault {vault}`): {error}")


def _mark_review_note(item, decision, vault):
    if not item.get("note_name"):
        return
    path = config.vault_dir(vault) / "Review Queue" / f"{item['note_name']}.md"
    if not path.exists():
        return
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return
    end = text.find("\n---\n", 4)
    if end < 0:
        return
    frontmatter = yaml.safe_load(text[4:end]) or {}
    rest = text[end + 5:]
    # Older review notes got a second frontmatter block under a `tags: [kb-index]` one; merge it.
    second = rest.find("\n---\n", 4) if rest.startswith("---\n") else -1
    if second >= 0:
        frontmatter = {**(yaml.safe_load(rest[4:second]) or {}), **frontmatter}
        rest = rest[second + 5:]
    frontmatter["status"] = "resolved"
    frontmatter["decision"] = decision
    path.write_text("---\n" + yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True)
                    + "---\n" + rest, encoding="utf-8", newline="\n")


def decide_classification(con, item_id, decision):
    """Record a researcher's decision on a review item.

    Classification items: keep | archive (archive keeps a verified PDF). Every other kind (analysis_flags,
    analysis_incomplete, needs_ocr, …): `resolved` once the researcher has checked it."""
    row = con.execute("SELECT * FROM review_items WHERE item_id = ?", (item_id,)).fetchone()
    if not row or row["status"] != "pending":
        raise ValueError(f"Review item {item_id} is not pending")
    item = dict(row)
    allowed = {"keep", "archive"} if item["kind"] in ("classification", "topic_relevance") else {"resolved"}
    if decision not in allowed:
        raise ValueError(f"Review item {item_id} ({item['kind']}) takes: {' | '.join(sorted(allowed))}")
    if item["kind"] in ("classification", "topic_relevance") and not item["paper_id"]:
        raise ValueError(f"Review item {item_id} has no paper")
    decision_dir = config.ROOT / "review" / "decisions"
    decision_file = decision_dir / f"review_{item_id:04d}.yaml"
    if decision_file.exists():
        raise FileExistsError(decision_file)
    if decision == "archive":
        meta = db.read_json(db.paper_dir(item["paper_id"]) / "metadata.json", {})
        source = _main_source(meta)
        if source and source.get("status") == "active":
            move_source(con, item["paper_id"])
        elif not source or source.get("status") != "archived":
            raise ValueError("Main PDF is neither active nor archived")
    record = {"review_item": item_id, "paper_id": item["paper_id"],
              "decision": decision, "resolved_at": config.now(), "detail": item["detail"]}
    decision_dir.mkdir(parents=True, exist_ok=True)
    with open(decision_file, "x", encoding="utf-8", newline="\n") as stream:
        yaml.safe_dump(record, stream, sort_keys=False, allow_unicode=True)
    try:
        con.execute("UPDATE review_items SET status = 'resolved', resolved_at = ? WHERE item_id = ?",
                    (record["resolved_at"], item_id))
        con.commit()
    except Exception:
        con.rollback()
        decision_file.unlink()
        raise
    vaults = ([r["vault"] for r in con.execute("SELECT vault FROM paper_vaults WHERE paper_id = ?", (item["paper_id"],))]
              if item["paper_id"] else [d.name for d in config.VAULTS_DIR.iterdir() if d.is_dir()]
              if config.VAULTS_DIR.is_dir() else [])
    for vault in vaults:
        _mark_review_note(item, decision, vault)
        notes.write_indexes(con, vault)
    config.log(f"REVIEW {item_id}: researcher chose {decision}")
    return record
