"""Vault layout v2 (D3.44): reversible folder migration, machine maps and Obsidian settings.

Rules (docs/ARCHITECTURE.md section 5):
- Files are moved, never edited or overwritten; an occupied target is a reported collision.
- Note file names never change, so [[Name]] links keep resolving after a move.
- Human notes (Mine/) are only moved. Machine-only legacy index notes are retired to
  knowledge_store/removed/ (never deleted); an index a person wrote in is left in place.
- Obsidian settings are merged: researcher keys and files are never clobbered.
"""

import hashlib
import importlib.util
import json
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

import yaml

from . import config, db, notes

# Old folder -> new folder, in a fixed order (first source wins a shared target).
LEGACY_MOVES = [
    ("Molecules", "Substances"), ("Species", "Organisms"), ("Cell Types", "Organisms"),
    ("Topics", "_Maps"), ("Views", "_Maps"),
    ("My Ideas", "Mine/Ideas"), ("Open Questions", "Mine/Questions"),
    ("Knowledge Gaps", "Mine/Knowledge Gaps"),
]
ROOT_MOVES = [("Chú giải graph.md", "_Maps/Chú giải graph.md"), ("Research map.md", "_Maps/Research map.md")]
HUMAN_FOLDERS = ("My Ideas", "Open Questions", "Knowledge Gaps")
# Legacy machine indexes replaced by _Maps/Index (retired when untouched by a person).
_RETIRED = re.compile(r"^(Research map\.md|Views/[^/]+\.md|Molecules/Molecules\.md|Species/Species\.md"
                      r"|Cell Types/Cell Types\.md|Topics/Topics\.md)$")

LOG_MARK = "<!-- kb:log — append-only; the software adds one line per update and never rewrites older lines -->"
CONFLICT_CALLOUT = re.compile(r"(?m)^> \[!(?:conflict|danger)\][+-]?")
INDEX_FOLDERS = ["_Maps", "Papers", "Substances", "Methods", "Organisms", "Concepts", "Results", "Claims"]
FOLDER_TYPES = {"Papers": "paper", "Substances": "substance", "Organisms": "organism", "Methods": "method",
                "Concepts": "concept", "_Maps": "moc", "Results": "result", "Claims": "claim"}
TYPE_LABELS = [("moc", "Bản đồ chủ đề (MOC)"), ("paper", "Bài báo"), ("method", "Phương pháp"),
               ("technique", "Kỹ thuật"), ("substance", "Chất / marker"), ("organism", "Sinh vật"),
               ("cell_type", "Loại tế bào"), ("concept", "Khái niệm"), ("result", "Kết quả"),
               ("claim", "Khẳng định"), ("conflict", "Xung đột")]
INDEX_NODE_CAP = 60 * 1024


# ---------------------------------------------------------------- migration

def _machine_only(text):
    """An index note with exactly the generated block and nothing a person added."""
    start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
    if (notes.INDEX_MARK not in text or text.count(notes.AUTO_START) != 1
            or text.count(notes.AUTO_END) != 1 or not 0 <= start < end):
        return False
    rest = text[:start] + text[end + len(notes.AUTO_END):]
    for frontmatter in (notes.INDEX_FRONTMATTER, "---\ntags: [kb-index]\n---\n"):
        if rest.startswith(frontmatter):
            rest = rest[len(frontmatter):]
            break
    return not rest.strip()


def plan(vault):
    """(moves [(src, dst)], retire [src], human_kept [src]) as vault-relative POSIX paths."""
    vdir = config.vault_dir(vault)
    moves, retire, kept = [], [], []

    def classify(rel, dst):
        if _RETIRED.match(rel):
            (retire if _machine_only((vdir / rel).read_text(encoding="utf-8")) else kept).append(rel)
        else:
            moves.append((rel, dst))

    for old, new in LEGACY_MOVES:
        src = vdir / old
        for path in sorted(p for p in src.rglob("*") if p.is_file()) if src.is_dir() else []:
            classify(path.relative_to(vdir).as_posix(), f"{new}/{path.relative_to(src).as_posix()}")
    for old, new in ROOT_MOVES:
        if (vdir / old).is_file():
            classify(old, new)
    return moves, retire, kept


def require_current(vault):
    """Refuse machine writes into a vault that still uses the old folders (would duplicate notes)."""
    vdir = config.vault_dir(vault)
    if not vdir.is_dir():
        return
    moves, retire, _ = plan(vault)
    if retire or any(not (vdir / dst).exists() for _, dst in moves):
        raise ValueError(f"Vault {vault} còn dùng bố cục thư mục cũ (Molecules, Species, Topics, My Ideas…). "
                         f"Xem trước: kb.py vault-migrate --vault {vault} --dry-run; "
                         f"rồi chạy: kb.py vault-migrate --vault {vault}")


def link_stats(vdir, renames=None):
    """Wikilinks and unresolved links. A plain link resolves when a note with that basename
    exists anywhere (Obsidian rule); a path link needs a file whose path ends with it.
    `renames` simulates a migration: {old rel: new rel or None (removed)}."""
    renames = renames or {}
    files = []
    for path in vdir.rglob("*"):
        if path.is_file() and ".obsidian" not in path.relative_to(vdir).parts:
            rel = path.relative_to(vdir).as_posix()
            new = renames.get(rel, rel)
            if new is not None:
                files.append((path, new))
    stems = {Path(rel).stem.casefold() for _, rel in files if rel.endswith(".md")}
    names = {Path(rel).name.casefold() for _, rel in files}
    paths = {rel.casefold().removesuffix(".md") for _, rel in files}
    total, missing = 0, Counter()
    for path, rel in files:
        if not rel.endswith(".md"):
            continue
        for target in notes.WIKI_LINK.findall(path.read_text(encoding="utf-8", errors="replace")):
            key = target.strip().replace("\\", "/").casefold()
            if not key:
                continue
            total += 1
            bare = key.removesuffix(".md")
            if "/" in key:
                ok = any(p == bare or p.endswith("/" + bare) for p in paths)
            else:
                ok = bare in stems or key in names
            if not ok:
                missing[target.strip()] += 1
    return {"files": len(files), "wikilinks": total, "unresolved": sum(missing.values()),
            "unresolved_targets": len(missing), "top_unresolved": missing.most_common(10)}


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _research_workspace():
    path = Path(__file__).resolve().parents[2] / "tools" / "research_workspace.py"
    spec = importlib.util.spec_from_file_location("research_workspace", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _ensure_snapshot(vault, con):
    """Reuse a snapshot whose vault notes equal the current ones; otherwise make one."""
    vdir = config.vault_dir(vault)
    current = {str(p.relative_to(vdir)): _sha(p) for p in vdir.rglob("*.md")}
    root = config.ROOT / "runtime" / "research-workspace"
    for manifest in sorted(root.glob("*/manifest.json"), key=lambda p: p.stat().st_mtime, reverse=True) \
            if root.is_dir() else []:
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
        except ValueError:
            continue
        if data.get("vault") == vault and data.get("notes") == current:
            return {"snapshot": str(manifest.parent), "reused": True}
    if con is None:
        raise ValueError("A snapshot is required before migrating; no database connection was given.")
    return {**_research_workspace().snapshot(con, vault), "reused": False}


def _new_dirs(vdir, folder):
    """Directories between vdir and folder that do not exist yet (outermost first)."""
    out = []
    while folder != vdir and not folder.exists():
        out.append(folder)
        folder = folder.parent
    return out[::-1]


def migrate(vault, dry_run=False, con=None):
    vdir = config.vault_dir(vault)
    if not vdir.is_dir():
        raise ValueError(f"Không có vault {vault}")
    moves, retire, kept = plan(vault)
    accepted, collisions, taken = [], [], set()
    for src, dst in moves:
        if (vdir / dst).exists() or dst.casefold() in taken:
            collisions.append({"from": src, "to": dst})  # never overwrite; the file stays where it is
            continue
        taken.add(dst.casefold())
        accepted.append((src, dst))
    before = link_stats(vdir)
    after = link_stats(vdir, {**dict(accepted), **{rel: None for rel in retire}})
    per_folder = Counter(f"{src.split('/')[0]} -> {dst.rsplit('/', 1)[0] if '/' in dst else '.'}"
                         for src, dst in accepted)
    report = {"vault": vault, "dry_run": dry_run, "moves": len(accepted), "per_folder": dict(per_folder),
              "retire": retire, "human_index_kept": kept, "collisions": collisions,
              "human_moved": [{"from": s, "to": d} for s, d in accepted if s.split("/")[0] in HUMAN_FOLDERS],
              "links_before": before, "links_after": after}
    if after["unresolved"] > before["unresolved"]:
        report["refused"] = "the move would add unresolved links"
        if not dry_run:
            raise ValueError(f"Không chuyển: số link hỏng sẽ tăng ({before['unresolved']} -> {after['unresolved']}).")
        return report
    if dry_run or not (accepted or retire):
        return report

    report["snapshot"] = _ensure_snapshot(vault, con)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    map_path = config.STORE / "migrations" / f"{stamp}_{vault}.json"
    record = {"version": 1, "vault": vault, "created_at": config.now(), "snapshot": report["snapshot"]["snapshot"],
              "entries": [], "created_dirs": [], "removed_dirs": [], "collisions": collisions,
              "human_index_kept": kept, "links_before": before}
    try:
        for src, dst in accepted:
            source, target = vdir / src, vdir / dst
            record["created_dirs"] += [d.relative_to(vdir).as_posix() for d in _new_dirs(vdir, target.parent)]
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = _sha(source)
            source.rename(target)  # rename never replaces an existing file on Windows
            record["entries"].append({"action": "move", "from": src, "to": dst, "sha256": sha})
        for src in retire:
            target = config.STORE / "removed" / f"vault_layout_{stamp}" / vault / src
            target.parent.mkdir(parents=True, exist_ok=True)
            sha = _sha(vdir / src)
            (vdir / src).rename(target)
            record["entries"].append({"action": "retire", "from": src, "to": config.rel(target), "sha256": sha})
        for old, _ in LEGACY_MOVES:
            folder = vdir / old
            for directory in sorted((d for d in folder.rglob("*") if d.is_dir()), key=lambda d: -len(d.parts)) \
                    if folder.is_dir() else []:
                if not any(directory.iterdir()):
                    directory.rmdir()
                    record["removed_dirs"].append(directory.relative_to(vdir).as_posix())
            if folder.is_dir() and not any(folder.iterdir()):
                folder.rmdir()
                record["removed_dirs"].append(old)
    finally:
        record["links_after"] = link_stats(vdir)
        map_path.parent.mkdir(parents=True, exist_ok=True)
        map_path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
        config.log(f"VAULT LAYOUT MIGRATED {vault}: {len(record['entries'])} files, "
                   f"{len(collisions)} collisions, map {config.rel(map_path)}")
    report.update(map=config.rel(map_path), links_after=record["links_after"],
                  human_unchanged=all(_sha(vdir / e["to"]) == e["sha256"] for e in record["entries"]
                                      if e["action"] == "move"))
    return report


def retire(vault, path, kind, stamp):
    """Move a machine note to knowledge_store/removed/<kind>_<stamp>/<vault>/<vault-relative path> (never overwrites:
    _1, _2 on a clash) and record it in <vault>.undo.json beside it, same map shape as migrate(), so undo() restores it."""
    path = Path(path)
    rel = path.relative_to(config.vault_dir(vault)).as_posix()
    folder = config.STORE / "removed" / f"{kind}_{stamp}"
    target = folder / vault / rel
    n = 0
    while target.exists():
        n += 1
        target = (folder / vault / rel).with_name(f"{path.stem}_{n}{path.suffix}")
    target.parent.mkdir(parents=True, exist_ok=True)
    sha = _sha(path)
    path.rename(target)  # rename never replaces an existing file on Windows; shutil.move would copy over it
    map_path = folder / f"{vault}.undo.json"
    record = db.read_json(map_path, {"version": 1, "vault": vault, "entries": [], "created_dirs": [], "removed_dirs": []})
    record["entries"].append({"action": "retire", "from": rel, "to": config.rel(target), "sha256": sha})
    db.write_json(map_path, record)
    return target


def undo(map_path):
    """Move every file back exactly as recorded; never overwrite a file that reappeared."""
    data = json.loads(Path(map_path).read_text(encoding="utf-8"))
    vdir = config.vault_dir(data["vault"])
    for rel in data.get("removed_dirs") or []:
        (vdir / rel).mkdir(parents=True, exist_ok=True)
    restored, conflicts = [], []
    for entry in reversed(data["entries"]):
        source = vdir / entry["from"]
        moved = vdir / entry["to"] if entry["action"] == "move" else config.ROOT / entry["to"]
        if source.exists() or not moved.is_file():
            conflicts.append(entry)
            continue
        source.parent.mkdir(parents=True, exist_ok=True)
        moved.rename(source)
        restored.append(entry["from"])
    for rel in reversed(data.get("created_dirs") or []):
        folder = vdir / rel
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()
    config.log(f"VAULT LAYOUT UNDONE {data['vault']}: {len(restored)} restored, {len(conflicts)} conflicts")
    return {"vault": data["vault"], "restored": len(restored), "conflicts": conflicts}


# ---------------------------------------------------------------- machine maps

def _iter_nodes(vdir):
    """(path, frontmatter, text) for node notes; machine lists and untouched stubs are skipped."""
    for folder in INDEX_FOLDERS:
        for path in sorted((vdir / folder).glob("*.md")) if (vdir / folder).is_dir() else []:
            text = path.read_text(encoding="utf-8", errors="replace")
            fm, _ = notes._split_frontmatter(text)
            fm = fm if isinstance(fm, dict) else {}
            tags = fm.get("tags") or []
            if notes.INDEX_MARK in text or "kb-index" in tags or fm.get("kb_stub"):
                continue
            yield path, {**fm, "type": fm.get("type") or FOLDER_TYPES[folder]}, text


def _has_conflict(fm, text):
    if fm.get("type") == "conflict" or fm.get("has_conflict") is True:
        return True
    if "has_conflict" in fm:
        return False
    start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
    return 0 <= start < end and bool(CONFLICT_CALLOUT.search(text[start:end]))


def _one_line(value, limit=160):
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " …"


def write_maps(vault):
    """Index, Open conflicts, Log header, Bases, templates, CSS and Home. Reads files only (no DB)."""
    vdir = config.vault_dir(vault)
    groups, conflicts, mocs = {}, [], []
    for path, fm, text in _iter_nodes(vdir):
        kind = str(fm["type"])
        if kind == "paper":
            line = f"- [[{path.stem}]] — {fm.get('year') or 'n.d.'} · {fm.get('evidence_type') or 'unknown'} · " \
                   f"{fm.get('classification') or 'unclassified'}"
        else:
            summary = _one_line(fm.get("kb_summary"))
            counts = f"{fm.get('evidence_count') or 0} đơn vị bằng chứng · {len(fm.get('source_papers') or [])} bài"
            line = f"- [[{path.stem}]] — " + (f"{summary} ({counts})" if summary else counts)
        groups.setdefault(kind, []).append(line)
        if kind == "moc":
            mocs.append(path.stem)
        if _has_conflict(fm, text) and str(fm.get("status", "open")).casefold() == "open":
            if kind == "conflict":  # a CONFLICT note: grouped by the paper pair it compares
                title = _one_line(fm.get("title") or path.stem.removeprefix("CONFLICT - "))
                conflicts.append((str(fm.get("papers") or "Other paper pairs"), None,
                                  f"- [[{path.stem}|{title}]] {fm.get('severity') or ''}".rstrip()))
            else:  # a node note that carries conflicting findings: grouped by its kind
                conflicts.append((None, dict(TYPE_LABELS).get(kind, kind),
                                  f"- [[{path.stem}]] — {_one_line(fm.get('kb_summary') or fm.get('title') or 'open conflict')}"))
    maps = vdir / notes.MAPS
    order = [k for k, _ in TYPE_LABELS if k in groups]
    labels = dict(TYPE_LABELS)
    lines = [notes.INDEX_MARK, "# Index", "",
             "Danh mục mọi ghi chú theo loại, mỗi ghi chú một dòng (tóm tắt = một câu bằng chứng đã lưu). "
             "Ghi chú là lối vào; bằng chứng gốc là câu có mã trong kho máy.", "",
             "[[Home]] · [[Open conflicts]] · [[Open hypotheses]] · [[Results and media]] · [[Log]]", ""]
    base_by_type = {"paper": "Papers", "method": "Methods", "technique": "Methods",
                    "substance": "Substances", "organism": "Organisms", "cell_type": "Organisms",
                    "concept": "Concepts", "moc": None, "result": "Results", "claim": "Claims",
                    "conflict": "Open conflicts"}
    folder_by_type = {"paper": ("Papers", "Papers"), "method": ("Methods", "Methods"),
                      "technique": ("Methods", "Methods"), "substance": ("Substances", "Substances"),
                      "organism": ("Organisms", "Organisms"), "cell_type": ("Organisms", "Organisms"),
                      "concept": ("Concepts", "Concepts"), "moc": ("_Maps", "Index"),
                      "result": ("Results", "Results"), "claim": ("Claims", "Claims"),
                      "conflict": ("Claims", "Claims")}
    for kind in order:
        entries = groups[kind]
        block = [f"## {labels[kind]} ({len(entries)})", ""]
        block.extend(entries)
        block.append("")
        candidate = "\n".join(lines + block) + "\n"
        if len(candidate.encode("utf-8")) > INDEX_NODE_CAP:
            folder, note = folder_by_type.get(kind, (None, None))
            base = base_by_type.get(kind)
            links = []
            if base:
                links.append(f"[[_Bases/{base}.base|Base]]")
            if folder and note:
                links.append(f"[[{folder}/{note}|folder note]]")
            block = [f"## {labels[kind]} ({len(entries)})", "",
                     "- Large group; browse the generated Base and folder note: " + " · ".join(links), ""]
        lines += block
    if len(("\n".join(lines) + "\n").encode("utf-8")) > INDEX_NODE_CAP:
        compact = lines[:7]
        for kind in order:
            folder, note = folder_by_type.get(kind, (None, None))
            base = base_by_type.get(kind)
            links = []
            if base:
                links.append(f"[[_Bases/{base}.base|Base]]")
            if folder and note:
                links.append(f"[[{folder}/{note}|folder note]]")
            compact += [f"## {labels[kind]} ({len(groups[kind])})", "",
                        "- Large group; browse: " + " · ".join(links), ""]
        lines = compact
    notes._write_index(maps / "Index.md", "\n".join(lines) + "\n")
    lines = [notes.INDEX_MARK, "# Open conflicts", "",
             "Conflicting results between papers that are still open. Nothing is deleted: read both sides "
             "side by side in each note. Live table: [[_Bases/Open conflicts.base|Open conflicts Base]].", ""]
    pairs = sorted({pair for pair, _, _ in conflicts if pair})
    kinds = sorted({label for pair, label, _ in conflicts if not pair})
    for pair in pairs:
        rows = sorted(line for item, _, line in conflicts if item == pair)
        lines += [f"## {pair} ({len(rows)})", "", *rows, ""]
    if kinds:
        lines += ["## Notes that list conflicting findings", ""]
        for label in kinds:
            rows = sorted(line for pair, item, line in conflicts if not pair and item == label)
            lines += [f"### {label} ({len(rows)})", "", *rows, ""]
    if not conflicts:
        lines.append("*No open conflicts recorded.*")
    notes._write_index(maps / "Open conflicts.md", "\n".join(lines) + "\n")
    _remove_empty_machine_dirs(vdir)
    _ensure_log(maps / "Log.md")
    write_bases(vdir)
    write_templates(vdir)
    write_obsidian_settings(vdir)
    _write_home(vdir, vault, sorted(set(mocs)))


def _remove_empty_machine_dirs(vdir):
    """Remove only empty machine spill directories; never touch Mine/ content."""
    for name in ("crops", "Sources"):
        folder = vdir / name
        if folder.is_dir() and not any(folder.iterdir()):
            folder.rmdir()


def _write_home(vdir, vault, mocs):
    def link(name, label=None, folder=""):
        if (vdir / folder / name).exists() or (vdir / folder / f"{name}.md").exists():
            return f"[[{name}|{label}]]" if label else f"[[{name}]]"
        return label or name
    mine = " · ".join([link("My Ideas", folder="Mine/Ideas"), link("Open Questions", folder="Mine/Questions"),
                       link("Knowledge Gaps", folder="Mine/Knowledge Gaps")]) + " · thư mục `Mine/Decisions/`"
    bases = " · ".join(link(f"{name}.base", label, "_Bases") for name, label in (
        ("Papers", "Bài báo"), ("Substances", "Chất"), ("Methods", "Phương pháp"), ("Organisms", "Sinh vật"),
        ("Open conflicts", "Xung đột"), ("Review queue", "Cần duyệt")))
    mine += " · folder `Mine/Hypotheses/`"
    content = "\n".join([
        "## Bắt đầu từ đây", "",
        "- [[Cần duyệt]] — **việc đang chờ bạn quyết định**: sai ở bài gốc, sai do AI chưa chắc cách sửa, bài mồ côi, nhận định không có nguồn",
        "- [[Index]] — danh mục mọi ghi chú theo loại, mỗi ghi chú một dòng",
        "- [[Papers/Papers|Bài báo]] — danh mục bài báo",
        "- [[Open conflicts]] — ghi chú có kết quả mâu thuẫn (callout đỏ)",
        "- [[Open hypotheses]] — giả thuyết mở để thảo luận (không phải knowledge gap)",
        f"- {link('Review Queue', folder='Review Queue')} — bài chưa nối được với chủ đề vault",
        "- Bản đồ chủ đề: " + (" · ".join(f"[[{m}]]" for m in mocs) or "chưa có"),
        f"- Của bạn (máy không sửa): {mine}",
        f"- Bảng động (Bases): {bases} · [[_Bases/Results.base|Kết quả]]",
        "- [[Chú giải graph]] — ý nghĩa màu trong Graph view · [[Log]] — nhật ký cập nhật", "",
        "### Cách dùng", "",
        "1. Mở **Index** hoặc một bản đồ chủ đề → chọn ghi chú → đi tới bài báo và câu gốc (mã `S####`).",
        "2. Callout đỏ = có kết quả mâu thuẫn: đọc cả hai phía; chưa bên nào bị coi là sai khi bạn chưa duyệt.",
        "3. Viết ý tưởng, câu hỏi, quyết định của bạn trong `Mine/` (mẫu ở `_Templates/`: Ctrl+P → "
        "\"Templates: Insert template\").",
        "4. Phần nằm giữa hai dòng `kb:auto` do phần mềm tạo lại; hãy viết ghi chú của bạn ở ngoài phần đó.",
    ])
    block = f"{notes.AUTO_START}\n{content}\n{notes.AUTO_END}"
    path = vdir / "Home.md"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old is None:
        new = f"---\ntype: home\n---\n# {vault}\n\n{block}\n"
    else:
        fm, body = notes._split_frontmatter(old)
        if isinstance(fm, dict) and not fm.get("type"):
            fm = {"type": "home", **fm}
            old = "---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000) + "---\n" + body
        start, end = old.find(notes.AUTO_START), old.find(notes.AUTO_END)
        if old.count(notes.AUTO_START) == 1 and old.count(notes.AUTO_END) == 1 and start < end:
            new = old[:start] + block + old[end + len(notes.AUTO_END):]
        elif notes.AUTO_START in old or notes.AUTO_END in old:
            config.log(f"HOME NOT UPDATED (kb:auto markers edited): {path}")
            return
        else:  # researcher text stays first and unchanged; the machine block is appended once
            new = old.rstrip("\n") + "\n\n" + block + "\n"
    if new != old:
        path.write_text(new, encoding="utf-8", newline="\n")


def _ensure_log(path):
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{notes.INDEX_FRONTMATTER}{LOG_MARK}\n# Log\n\nNhật ký chỉ-thêm: mỗi lần chạy "
                        "`kb.py notes` thêm một dòng. Dòng cũ không bao giờ bị sửa.\n\n", encoding="utf-8", newline="\n")
        return
    old = path.read_text(encoding="utf-8")
    if LOG_MARK not in old:
        return
    fm, body = notes._split_frontmatter(old)
    if isinstance(fm, dict) and not fm.get("type"):
        fm = {"type": "moc", **fm}
        path.write_text("---\n" + yaml.safe_dump(fm, sort_keys=False, allow_unicode=True, width=1000)
                        + "---\n" + body, encoding="utf-8", newline="\n")


def append_log(vault, text):
    """Append one dated line to _Maps/Log.md; older lines are never rewritten."""
    path = config.vault_dir(vault) / notes.MAPS / "Log.md"
    _ensure_log(path)
    old = path.read_text(encoding="utf-8")
    if LOG_MARK not in old:
        return  # a person's own note named Log
    with open(path, "a", encoding="utf-8", newline="\n") as handle:  # same line endings as write_text notes
        handle.write(("" if old.endswith("\n") else "\n") + f"- {datetime.now():%Y-%m-%d %H:%M} · {text}\n")


# ---------------------------------------------------------------- Obsidian files

def _base(folder_filter, order, name, extra_filters=()):
    data = {"filters": {"and": [*folder_filter, *extra_filters]},
            "formulas": {"papers_n": "if(source_papers, source_papers.length, 0)"},
            "properties": {"formula.papers_n": {"displayName": "papers"}},
            "views": [{"type": "table", "name": name, "order": order}]}
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=1000)


NODE_COLUMNS = ["file.name", "type", "kb_kind", "kb_roles", "evidence_count", "formula.papers_n", "has_conflict"]
BASES = {
    "Papers": _base(['file.inFolder("Papers")'], ["file.name", "year", "evidence_type", "classification",
                    "evidence_count", "also_in", "has_conflict"], "Bài báo", ['type == "paper"']),
    "Substances": _base(['file.inFolder("Substances")'], NODE_COLUMNS, "Chất / marker"),
    "Methods": _base(['file.inFolder("Methods")'], NODE_COLUMNS, "Phương pháp"),
    "Organisms": _base(['file.inFolder("Organisms")'], NODE_COLUMNS, "Sinh vật / tế bào"),
    "Concepts": _base(['file.inFolder("Concepts")'], NODE_COLUMNS, "Khái niệm"),
    "Results": _base(['file.inFolder("Results")'], NODE_COLUMNS, "Kết quả"),
    "Claims": _base(['file.inFolder("Claims")'], NODE_COLUMNS, "Khẳng định"),
    "Open conflicts": yaml.safe_dump({
        "filters": {"and": ['status == "open"', 'type == "conflict"']},
        "views": [{"type": "table", "name": "Xung đột đang mở",
                   "order": ["file.name", "file.folder", "type", "axis", "conflict_open", "status"]}]},
        sort_keys=False, allow_unicode=True),
    "Review queue": yaml.safe_dump({
        "filters": {"and": ['file.inFolder("Review Queue")', 'file.name != "Review Queue"']},
        "views": [{"type": "table", "name": "Cần duyệt",
                   "order": ["file.name", "kind", "status", "decision", "created"]}]},
        sort_keys=False, allow_unicode=True),
}

# Human templates (core Templates plugin: {{title}}, {{date}}), following SECOND_BRAIN_RESEARCH.md section 3.
# Common properties: type, status, created, related, evidence (links), sources, based_on.
_TEMPLATE_HEAD = ("---\ntype: {type}\nstatus: {status}\n{extra}based_on: []\nrelated: []\nevidence: []\n"
                  "sources: []\ntags: [{tag}]\ncreated: \"{{{{date}}}}\"\n---\n# {prefix}{{{{title}}}}\n\n")
TEMPLATES = {
    "Template - Idea": ("idea", "human", "maturity: seed\n", "ideas", "IDEA - ",
                        "**Một dòng:** \n\n## Vì sao tôi nghĩ vậy (dẫn based_on, mã câu S####)\n\n"
                        "## Nếu đúng thì sao\n\n## Điều có thể làm sai (bằng chứng ngược chiều)\n\n## Bước tiếp theo\n"),
    "Template - Question": ("question", "open", "gap_scope: vault_only\npriority: medium\n", "questions", "QUE - ",
                            "## Vì sao quan trọng\n\n## Đã biết gì trong vault (ghi \"chưa tìm thấy trong kho hiện tại\" "
                            "nếu rỗng)\n\n## Câu trả lời ứng viên\n\n## Cần gì để trả lời\n\n"
                            "> Chưa tìm thấy trong vault không có nghĩa là chưa có trong y văn.\n"),
    "Template - Hypothesis": ("hypothesis", "proposed", "prediction: \"\"\n", "hypotheses", "HYP - ",
                              "> [!warning] Đây là giả thuyết, chưa phải kết quả đã chứng minh.\n\n"
                              "## Cơ sở (bằng chứng, mã câu)\n\n## Dự đoán kiểm chứng được\n\n"
                              "## Thiết kế kiểm tra (nhóm, n, kỹ thuật đo, tiêu chí bác bỏ)\n\n"
                              "## Bằng chứng ủng hộ / chống (cạnh nhau)\n\n## Nhật ký trạng thái\n"),
    "Template - Decision": ("decision", "proposed", "decided_on: \nsupersedes: []\nsuperseded_by: []\n", "decisions",
                            "DEC - ", "## Bối cảnh\n\n## Quyết định\n\n## Phương án đã cân nhắc\n\n## Hệ quả\n\n"
                                      "## Xem lại khi\n"),
    "Template - Lab note": ("lab_note", "done", "", "lab_notes", "",
                            "Ngày: {{date}} · Người tham gia: \n\n## Nội dung\n\n## Kết quả / quan sát\n\n"
                            "## Việc cần làm\n"),
}

CONFLICT_CSS = """/* kb-conflict (D3.44): red callout for source conflicts. Written once; edit freely. */
.callout[data-callout="conflict"],
.callout[data-callout="danger"] {
  --callout-color: 220, 38, 38;
  --callout-icon: lucide-alert-triangle;
  background-color: rgba(220, 38, 38, 0.08);
  border-left: 4px solid #DC2626;
}
"""


def _write_once(path, text):
    """Researchers may edit Bases, templates and snippets in Obsidian; never overwrite them."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")


def write_bases(vdir):
    for name, text in BASES.items():
        _write_once(vdir / "_Bases" / f"{name}.base", text)


def write_templates(vdir):
    for name, (kind, status, extra, tag, prefix, body) in TEMPLATES.items():
        head = _TEMPLATE_HEAD.format(type=kind, status=status, extra=extra, tag=tag, prefix=prefix)
        _write_once(vdir / "_Templates" / f"{name}.md", head + body)


def _merge_json(path, update):
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        config.log(f"OBSIDIAN SETTING NOT CHANGED (unreadable JSON): {path}")
        return
    if not isinstance(data, dict):
        return
    before = json.dumps(data, sort_keys=True)
    update(data)
    if json.dumps(data, sort_keys=True) != before:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


GRAPH_CSS = """/* kb-graph: hides the Extended Graph status item ("0 Nodes"), which counts only nodes of its own
   renderer and reads as "the graph is empty" while the plugin is off. Written once; edit freely. */
.status-bar-item.plugin-extended-graph {
  display: none;
}
"""


def write_obsidian_settings(vdir):
    obsidian = vdir / ".obsidian"
    _write_once(obsidian / "snippets" / "kb-conflict.css", CONFLICT_CSS)
    _write_once(obsidian / "snippets" / "kb-graph.css", GRAPH_CSS)

    def enable_snippet(data):
        snippets = data.get("enabledCssSnippets")
        if not isinstance(snippets, list):
            snippets = data["enabledCssSnippets"] = []
        for name in ("kb-conflict", "kb-graph"):
            if name not in snippets:
                snippets.append(name)
    _merge_json(obsidian / "appearance.json", enable_snippet)
    _merge_json(obsidian / "templates.json",
                lambda data: data.update(folder="_Templates") if not data.get("folder") else None)
    if (obsidian / "core-plugins.json").exists():  # absent file = Obsidian defaults; do not create it
        _merge_json(obsidian / "core-plugins.json",
                    lambda data: [data.setdefault(k, True) for k in ("templates", "bases")])
