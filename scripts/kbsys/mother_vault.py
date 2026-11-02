"""Build the ``vaults/`` mother vault.

The mother vault is a generated index over the independent sub-vaults.  It is
deliberately kept separate from :mod:`kbsys.notes`: the latter owns notes in a
single research vault, while this module only creates cross-vault navigation
and registry pages.
"""

from collections import defaultdict
from datetime import date, datetime
import json
from pathlib import Path
import re
import shutil

import yaml

from . import config, db, notes


DATE_RE = re.compile(r"(?<!\d)(20\d{2}-\d{2}-\d{2})(?!\d)")
MOTHER_GRAPH_FILTER = 'path:"_Maps" OR path:"Registry" OR [type:moc] OR [type:home]'
LEGACY_MOTHER_GRAPH_FILTER = ('-path:"Registry/Papers" -path:"_Inbox" -tag:#kb-index '
                             '-path:"_Bases" -path:"_Templates" -path:"_Attachments"')


def _as_dict(row):
    if row is None:
        return {}
    if hasattr(row, "keys"):
        return {key: row[key] for key in row.keys()}
    return dict(row)


def _wiki(path, label=None):
    """Return a path-qualified Obsidian link relative to the mother vault."""
    target = Path(path).as_posix()
    return f"[[{target}|{label or Path(target).name}]]"


def _load_vault_config():
    path = config.ROOT / "config" / "vaults.yaml"
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        raw = {}
    raw = raw.get("vaults", raw) if isinstance(raw, dict) else {}
    result = {}
    if isinstance(raw, list):
        raw = {item.get("name"): item for item in raw if isinstance(item, dict) and item.get("name")}
    if isinstance(raw, dict):
        for name, values in raw.items():
            if not isinstance(name, str) or not isinstance(values, dict):
                continue
            result[name] = {
                "vault_kind": values.get("vault_kind") or "research",
                "inputs": values.get("inputs") if isinstance(values.get("inputs"), list) else ["pdf"],
            }
    return result


def _sub_vaults():
    return sorted((config.vault_dir(n) for n in config.research_vaults()), key=lambda p: p.name.casefold())


def _read_frontmatter(path):
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return {}, ""
    fm, body = notes._split_frontmatter(text)
    return (fm if isinstance(fm, dict) else {}), body


def _iter_notes(vault_dir):
    for path in sorted(vault_dir.rglob("*.md"), key=lambda p: p.as_posix().casefold()):
        if any(part.startswith(".") for part in path.relative_to(vault_dir).parts):
            continue
        fm, body = _read_frontmatter(path)
        yield path, fm, body


def _relative_note(vault_dir, path):
    return path.relative_to(vault_dir).with_suffix("").as_posix()


def _write_indexed(path, body, frontmatter=None):
    """Write a generated page through notes._write_index and merge its YAML."""
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is not None and notes.INDEX_MARK not in old:
        return False
    notes._write_index(path, body if body.startswith(notes.INDEX_MARK) else notes.INDEX_MARK + "\n" + body)
    if not frontmatter:
        return True
    text = path.read_text(encoding="utf-8")
    current, rest = notes._split_frontmatter(text)
    if current is None:
        return True
    merged = dict(current or {})
    merged.update(frontmatter)
    tags = merged.get("tags")
    if isinstance(tags, str):
        tags = [tags]
    elif not isinstance(tags, list):
        tags = list(tags or [])
    if "kb-index" not in tags:
        tags.append("kb-index")
    merged["tags"] = tags
    prefix = "---\n" + yaml.safe_dump(merged, sort_keys=False, allow_unicode=True, width=1000) + "---\n"
    updated = prefix + rest
    if updated != text:
        path.write_text(updated, encoding="utf-8", newline="\n")
    return True


def _format_date(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()[:10]
    match = DATE_RE.search(str(value or ""))
    return match.group(1) if match else ""


def _latest_updated(vault_dir):
    latest = 0.0
    for path in vault_dir.rglob("*"):
        if path.is_file():
            try:
                latest = max(latest, path.stat().st_mtime)
            except OSError:
                pass
    return datetime.fromtimestamp(latest).date().isoformat() if latest else ""


def _vault_metadata(name, configured):
    values = configured.get(name, {})
    inputs = values.get("inputs") if isinstance(values, dict) else None
    if not isinstance(inputs, list):
        inputs = ["pdf"]
    return {
        "vault_kind": (values.get("vault_kind") if isinstance(values, dict) else None) or "research",
        "inputs": [str(item) for item in inputs],
        "path": f"vaults/{name}",
    }


def _paper_rows(con):
    cursor = con.execute(
        """SELECT p.*, pv.vault FROM papers p
           JOIN paper_vaults pv ON pv.paper_id = p.paper_id
           ORDER BY p.paper_id, pv.vault"""
    )
    column_names = [description[0] for description in cursor.description]
    grouped = {}
    for row in cursor.fetchall():
        data = _as_dict(row) if hasattr(row, "keys") else dict(zip(column_names, row))
        item = grouped.setdefault(data["paper_id"], {"paper": data, "vaults": []})
        item["vaults"].append(data["vault"])
    return grouped


def _write_home():
    body = """# Second brain

Năm trục của vault mẹ:

- **Atlas** — bản đồ tri thức và các vault con.
- **Efforts** — dự án và việc đang làm.
- **Calendar** — mốc thời gian và quyết định.
- **Registry** — sổ đăng ký vault và bài báo chuẩn.
- **Inbox** — nơi nhận tệp chưa xử lý.

Mở thư mục `vaults` trong Obsidian để xem toàn bộ hệ thống; mỗi sub-vault vẫn có thể mở riêng. Obsidian ẩn các thư mục `.obsidian` của sub-vault vì đây là thư mục bắt đầu bằng dấu chấm.

- [[_Maps/Atlas|Atlas]]
- [[_Maps/Efforts|Efforts]]
- [[_Maps/Calendar|Calendar]]
- [[Registry/Vaults|Registry]]
- [[_Inbox/README|Inbox]]
"""
    _write_indexed(config.VAULTS_DIR / "Home.md", body, {"type": "home"})


def _write_atlas(vaults):
    lines = ["# Atlas", "", "Bản đồ các vault và các hub tri thức của chúng.", ""]
    for vault_dir in vaults:
        name = vault_dir.name
        lines += [f"## {name}", "", f"- {_wiki(f'{name}/Home', name)}", f"- {_wiki(f'{name}/_Maps/Index', 'Index')}"]
        maps = []
        maps_dir = vault_dir / "_Maps"
        if maps_dir.is_dir():
            for path in sorted(maps_dir.glob("*.md"), key=lambda p: p.name.casefold()):
                if path.stem.casefold() in {"index", "log", "open conflicts", "chú giải graph", "results and media"}:
                    continue
                fm, _ = _read_frontmatter(path)
                tags = fm.get("tags") or []
                if fm.get("type") != "moc" or "kb-index" in (tags if isinstance(tags, list) else [tags]):
                    continue
                maps.append(f"- {_wiki(f'{name}/_Maps/{path.stem}', path.stem)}")
        lines.extend(maps or ["- *No topic MOCs yet.*"])
        lines.append("")
    _write_indexed(config.VAULTS_DIR / "_Maps" / "Atlas.md", "\n".join(lines))


def _write_efforts(vaults):
    rows = []
    for vault_dir in vaults:
        for path, fm, _ in _iter_notes(vault_dir):
            para = fm.get("para")
            is_project = para == "project" or (isinstance(para, list) and "project" in para)
            if is_project or str(fm.get("type", "")).casefold() == "effort":
                rel = _relative_note(vault_dir, path)
                rows.append((vault_dir.name, rel.casefold(), f"- {_wiki(f'{vault_dir.name}/{rel}', path.stem)}"))
    lines = ["# Efforts", "", "Ghi chú dự án và effort được phát hiện từ `para: project` hoặc `type: effort`.", ""]
    lines.extend(row[2] for row in sorted(rows, key=lambda row: (row[0].casefold(), row[1])))
    if not rows:
        lines.append("*No efforts yet.*")
    _write_indexed(config.VAULTS_DIR / "_Maps" / "Efforts.md", "\n".join(lines))


def _write_calendar(vaults):
    rows = []
    for vault_dir in vaults:
        for path, fm, _ in _iter_notes(vault_dir):
            if str(fm.get("type", "")).casefold() != "decision":
                continue
            when = next((_format_date(fm.get(key)) for key in ("date", "decision_date", "created", "updated") if _format_date(fm.get(key))), "")
            if when:
                rel = _relative_note(vault_dir, path)
                rows.append((when, vault_dir.name, rel.casefold(), f"- {when} — {_wiki(f'{vault_dir.name}/{rel}', path.stem)}"))
        log_path = vault_dir / "_Maps" / "Log.md"
        if log_path.is_file():
            try:
                log_lines = log_path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeError):
                log_lines = []
            for line in log_lines:
                match = DATE_RE.search(line)
                if match:
                    when = match.group(1)
                    detail = DATE_RE.sub("", line, count=1).strip(" -*:|\t")
                    label = f"{vault_dir.name} log" + (f" — {detail}" if detail else "")
                    rows.append((when, vault_dir.name, f"_maps/log:{line.casefold()}", f"- {when} — {_wiki(f'{vault_dir.name}/_Maps/Log', label)}"))
    lines = ["# Calendar", "", "Quyết định và các mục nhật ký có ngày.", ""]
    lines.extend(row[3] for row in sorted(rows, key=lambda row: (row[0], row[1].casefold(), row[2])))
    if not rows:
        lines.append("*No dated entries yet.*")
    _write_indexed(config.VAULTS_DIR / "_Maps" / "Calendar.md", "\n".join(lines))


def _write_conflicts(vaults):
    grouped = defaultdict(list)
    for vault_dir in vaults:
        for path, fm, _ in _iter_notes(vault_dir):
            conflict = fm.get("has_conflict") is True or str(fm.get("has_conflict", "")).casefold() == "true"
            conflict = conflict or (str(fm.get("type", "")).casefold() == "conflict"
                                    and str(fm.get("status", "open")).casefold() not in ("explained", "dismissed", "withdrawn", "stale"))
            if conflict:
                rel = _relative_note(vault_dir, path)
                grouped[vault_dir.name].append((rel.casefold(), f"- {_wiki(f'{vault_dir.name}/{rel}', path.stem)}"))
    lines = ["# Open conflicts - all vaults", "", "Các note có `has_conflict: true` hoặc `type: conflict`, nhóm theo vault.", ""]
    for vault in sorted(grouped, key=str.casefold):
        lines += [f"## {vault}", ""]
        lines.extend(item[1] for item in sorted(grouped[vault], key=lambda item: item[0]))
        lines.append("")
    if not grouped:
        lines.append("*No open conflicts yet.*")
    _write_indexed(config.VAULTS_DIR / "_Maps" / "Open conflicts - all vaults.md", "\n".join(lines))


def _write_registry(vaults, configured, papers):
    registry = config.VAULTS_DIR / "Registry"
    vault_lines = ["# Vaults", "", "| vault | kind | inputs | papers | last updated |", "|---|---|---|---:|---|"]
    for vault_dir in vaults:
        name = vault_dir.name
        meta = _vault_metadata(name, configured)
        count = sum(1 for item in papers.values() if name in item["vaults"])
        vault_lines.append(f"| {name} | {meta['vault_kind']} | {', '.join(meta['inputs'])} | {count} | {_latest_updated(vault_dir)} |")
        body = f"# Vault — {name}\n\n- Path: `{meta['path']}`\n- Kind: `{meta['vault_kind']}`\n- Inputs: {', '.join(meta['inputs'])}\n"
        _write_indexed(registry / f"Vault - {name}.md", body, {
            "type": "vault", "vault_kind": meta["vault_kind"], "inputs": meta["inputs"], "path": meta["path"],
        })
    _write_indexed(registry / "Vaults.md", "\n".join(vault_lines), {"type": "vault_registry"})

    for paper_id, item in sorted(papers.items()):
        paper = item["paper"]
        in_vaults = sorted(set(item["vaults"]), key=str.casefold)
        links = []
        if paper.get("note_name"):
            note_name = paper["note_name"]
            for vault in in_vaults:
                links.append(f"- {_wiki(f'{vault}/Papers/{note_name}', vault)}")
        body_lines = [f"# {paper.get('title') or paper_id}", "", "In vaults:", ""]
        body_lines.extend(links or ["*No paper note name is recorded.*"])
        fm = {
            "type": "paper_ref", "paper_id": paper_id, "title": paper.get("title"), "year": paper.get("year"),
            "doi": paper.get("doi"), "pmid": paper.get("pmid"), "in_vaults": in_vaults,
        }
        _write_indexed(registry / "Papers" / f"{paper_id}.md", "\n".join(body_lines), fm)


def _merge_graph_settings():
    path = config.VAULTS_DIR / ".obsidian" / "graph.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        settings = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        settings = {}
    if not isinstance(settings, dict):  # e.g. a JSON list: treated like invalid JSON
        settings = {}
    settings["colorGroups"] = notes.merge_graph_groups(settings.get("colorGroups"))
    machine_filters = {"", None, notes.GRAPH_FILTER, MOTHER_GRAPH_FILTER, LEGACY_MOTHER_GRAPH_FILTER}
    if settings.get("search") in machine_filters:
        settings["search"] = MOTHER_GRAPH_FILTER
    settings["hideUnresolved"] = True
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    notes.sync_extended_graph(config.VAULTS_DIR)


def graph_colours(names, mother=True):
    """`kb.py graph-colours`: back up each graph.json (and the graph legend note), re-merge the colour groups without a
    notes rebuild, then check them: full VN-09 for a vault, the group order only for the mother vault (its filter
    shows maps on purpose)."""
    from . import conform_vault
    from .conform import Vault
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    # every name checked before any write: '..' or '.' must not reach the project root or the mother vault
    targets = [(config.require_research_vault(name), False) for name in names] + ([("mother", True)] if mother else [])
    report = {}
    for name, is_mother in targets:
        root = config.VAULTS_DIR if is_mother else config.vault_dir(name)
        if not root.is_dir():
            raise ValueError(f"no vault folder {root}")
        files = [root / ".obsidian" / "graph.json", root / ".obsidian" / "plugins" / "extended-graph" / "data.json"] + (
            [] if is_mother else [root / notes.MAPS / f"{notes.LEGEND_NOTE}.md"])
        backups = []
        for path in files:
            if path.is_file():
                backup = path.with_name(f"{path.name}.bak-{stamp}")
                if not backup.exists():  # a rerun in the same second keeps the pre-merge copy
                    shutil.copy2(path, backup)
                backups.append(str(backup))
        if is_mother:
            _merge_graph_settings()
            settings = json.loads(files[0].read_text(encoding="utf-8"))
            problems = [f"missing {q}" for q in notes.missing_graph_groups(settings.get("colorGroups"))]
        else:
            notes.write_graph_settings(name)
            problems = list(conform_vault.graph_settings(Vault(name, root)))
        report[name] = {"backups": backups, "problems": problems or "ok"}
    return report


def _merge_conflict_css(vaults):
    source = next((vault / ".obsidian" / "snippets" / "kb-conflict.css" for vault in vaults
                   if (vault / ".obsidian" / "snippets" / "kb-conflict.css").is_file()), None)
    if source is None:
        return False
    target = config.VAULTS_DIR / ".obsidian" / "snippets" / "kb-conflict.css"
    target.parent.mkdir(parents=True, exist_ok=True)
    data = source.read_bytes()
    if not target.exists() or target.read_bytes() != data:
        target.write_bytes(data)
    appearance = config.VAULTS_DIR / ".obsidian" / "appearance.json"
    try:
        settings = json.loads(appearance.read_text(encoding="utf-8")) if appearance.is_file() else {}
    except (OSError, ValueError):
        settings = {}
    snippets = settings.get("enabledCssSnippets")
    if isinstance(snippets, str):
        snippets = [snippets]
    elif not isinstance(snippets, list):
        snippets = list(snippets or [])
    if "kb-conflict" not in snippets:
        snippets.append("kb-conflict")
    settings["enabledCssSnippets"] = snippets
    appearance.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    return True


def _write_inbox():
    _write_indexed(config.VAULTS_DIR / "_Inbox" / "README.md", """# Inbox

Đây là drop zone cho các tệp Word, PDF, video, ảnh và ghi chú chưa được xử lý. Máy không bao giờ tự xoá tệp trong thư mục này; hãy xử lý hoặc di chuyển chúng bằng quyết định của nhà nghiên cứu.
""")


def refresh(con):
    """Refresh all mother-vault pages and return a small generation summary."""
    config.VAULTS_DIR.mkdir(parents=True, exist_ok=True)
    vaults = _sub_vaults()
    configured = _load_vault_config()
    papers = _paper_rows(con)
    _write_home()
    _write_atlas(vaults)
    _write_efforts(vaults)
    _write_calendar(vaults)
    _write_conflicts(vaults)
    _write_registry(vaults, configured, papers)
    _write_inbox()
    _merge_graph_settings()
    css = _merge_conflict_css(vaults)
    return {"vaults": len(vaults), "papers": len(papers), "css": css}


def main():
    con = db.connect()
    try:
        result = refresh(con)
    finally:
        con.close()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
