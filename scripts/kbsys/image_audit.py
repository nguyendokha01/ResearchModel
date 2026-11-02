"""`kb.py image-audit [--vault V] [--out FILE]`: give every image of the research vaults an address. READ-ONLY.

Decision 2026-10-18 (docs/DECISIONS.md): no image may stay without an address. Each image file of a vault (anywhere except
`.obsidian`) gets one row {vault, path, sha256, bytes, class, paper_id, part, referenced_by, cause, action, ...}.

Classes with an address: `figure`, `table`, `supplement` (FS*/TS* objects), `source_media` (any other manifest object), all four
embedded by a note AND matched to an object of the paper's `media_manifest.json` by crop SHA-256 or file name; `wrong_crop`
(a `figure_readings/<object>.json` says `crop_wrong` and the object's crop is this file). Without an address: `referenced_unknown`
(a note uses it, no object matches) and `orphan` (no note embeds or links it). Their cause is `clone` (a byte-identical image of
the same vault has an address: `twin`), else `trace` (paper from the folder, `part` = manifest object or `unmatched`,
`node_to_recheck` = the paper note that should embed it; `part_crop_status` = the crop status of that object now,
`identical_unaddressed` = other unaddressed images with the same bytes: a group of stale page previews is not a clone), or `unreadable` (file or manifest cannot be read), or `human` (the file is under Mine/ or _Templates/: never a candidate). A file that a note embeds is never a `clone`.
Nothing is decided here: every action says "candidate". Links are read with VN-06's helpers (conform_vault.Files / links / _sha).
"""
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from . import config
from . import conform_vault as cv
from . import notes, result_notes

ADDRESSED = ("figure", "table", "supplement", "source_media", "wrong_crop")
MAX_REFS = 3
MAX_LINES = 60  # printed list; the full list goes to --out
ACT_CLONE = "delete candidate at G5 after backup, researcher approval"
ACT_TRACE = "re-link at next notes run or report to researcher"
ACT_UNREADABLE = "report to researcher"
ACT_HUMAN = "researcher's own file (Mine/, _Templates/): not touched"


def _json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None


def _token(object_id):
    return str(object_id).rsplit(":", 1)[-1]


def _paper(folder):
    """(objects, crop_wrong local ids, readable) of one paper folder. objects: [(object_id, kind, {crop sha256s}, crop status)]."""
    manifest = _json(Path(folder) / "media_manifest.json")
    if not isinstance(manifest, dict) or not isinstance(manifest.get("bundles"), list):
        return [], {}, False
    objects = []
    for b in manifest["bundles"]:
        if isinstance(b, dict) and b.get("object_id"):
            crop = b.get("crop") if isinstance(b.get("crop"), dict) else {}
            old = (b.get("source_locator") or {}).get("previous") if isinstance(b.get("source_locator"), dict) else None
            shas = {s for s in (crop.get("sha256"), (old or {}).get("crop_sha256")) if s}
            objects.append((str(b["object_id"]), b.get("kind"), shas, crop.get("status")))
    readings = Path(folder) / "figure_readings"
    wrong = {}  # local id -> crop SHA-256 the reading was made on (None: not recorded)
    for f in sorted(readings.glob("*.json")) if readings.is_dir() else ():
        reading = _json(f)
        if isinstance(reading, dict) and reading.get("crop_wrong") is True:
            wrong[f.stem] = reading.get("crop_sha256")
    return objects, wrong, True


def _match(name, sha, objects):
    """The manifest object of an image: crop SHA-256 and/or file name (name decides between several equal hashes)."""
    by_name = [o for o in objects if result_notes._media_filename(o[0], notes) == name]
    by_sha = [o for o in objects if sha and sha in o[2]]
    return next(iter([o for o in by_sha if o in by_name] or by_name or by_sha), None)


def _class(obj, referenced, wrong, sha):
    if obj and _token(obj[0]) in wrong and wrong[_token(obj[0])] in (None, sha):  # a reading of an older crop says nothing about this file
        return "wrong_crop"
    if not referenced:
        return "orphan"
    if not obj:
        return "referenced_unknown"
    if _token(obj[0]).upper().startswith(("FS", "TS")):
        return "supplement"
    return {"figure": "figure", "table": "table"}.get(obj[1], "source_media")


def audit(vault, papers):
    """Rows (list of dicts) for one conform.Vault; `papers`: the folder holding <paper_id>/media_manifest.json. Writes nothing."""
    files = cv.Files(vault)
    images = sorted(p for p in files.other if Path(p).suffix.lower() in cv.IMAGE_SUFFIXES)
    refs = defaultdict(list)
    paper_note = {}
    for path, text in vault.notes:
        for _, target, kind in cv.links(text):
            for hit in files.resolve(target, path if kind == "md" else None):
                if path not in refs[hit]:
                    refs[hit].append(path)
        if path.startswith("Papers/") and path.count("/") == 1:
            paper_note.setdefault(cv._fm(text).get("paper_id"), path)
    groups = defaultdict(list)
    for img in images:
        parts = img.split("/")
        groups[parts[1] if parts[0] == "_Attachments" and len(parts) > 2 else None].append(img)
    rows, readable = [], {}
    for pid, group in sorted(groups.items(), key=lambda kv: str(kv[0])):  # one paper at a time
        objects, wrong, ok = _paper(Path(papers) / pid) if pid else ([], {}, True)
        readable[pid] = ok
        for img in group:
            sha = cv._sha(vault.root / img)
            sha = sha if re.fullmatch("[0-9a-f]{64}", sha) else None
            try:
                size = (vault.root / img).stat().st_size
            except OSError:
                size = None
            obj = _match(Path(img).with_suffix(".png").name, sha, objects)
            rows.append({"vault": vault.name, "path": img, "sha256": sha, "bytes": size, "class": _class(obj, refs.get(img), wrong, sha) if sha else "unreadable",
                         "paper_id": pid, "part": obj[0] if obj else None, "referenced_by": refs.get(img, [])[:MAX_REFS],
                         "part_crop_status": obj[3] if obj else None, "cause": None, "twin": None, "identical_unaddressed": [],
                         "node_to_recheck": None, "action": None})
    twins, open_twins = defaultdict(list), defaultdict(list)
    for r in rows:
        (twins if r["class"] in ADDRESSED else open_twins)[r["sha256"]].append(r["path"])
    for r in rows:
        if r["class"] in ADDRESSED:
            continue
        r["identical_unaddressed"] = [p for p in open_twins[r["sha256"]] if p != r["path"]][:MAX_REFS] if r["sha256"] else []
        same = sorted(twins.get(r["sha256"], ()), key=lambda p: (p.rsplit("/", 1)[0] != r["path"].rsplit("/", 1)[0], p))
        if not r["sha256"]:
            r["cause"], r["action"] = "unreadable", ACT_UNREADABLE
        elif r["path"].split("/")[0] in cv.HUMAN_FOLDERS:
            r["cause"], r["action"] = "human", ACT_HUMAN
        elif same and r["class"] == "orphan":  # a note that embeds the file would lose its image if it were deleted
            r["cause"], r["twin"], r["action"] = "clone", same[0], ACT_CLONE
        elif not readable.get(r["paper_id"], True):
            r["cause"], r["action"] = "unreadable", ACT_UNREADABLE  # no media_manifest.json (or not JSON): the part cannot be traced
        else:
            r["cause"], r["action"] = "trace", ACT_TRACE
            r["part"] = r["part"] or "unmatched"
            r["node_to_recheck"] = paper_note.get(r["paper_id"]) if r["part"] != "unmatched" else None
            r["node_to_recheck"] = r["node_to_recheck"] or "unmatched"
    return rows


def summary(rows):
    """Plain-English text: counts per class and cause per vault, then the images without an address (capped)."""
    out = []
    for name in sorted({r["vault"] for r in rows}):
        mine = [r for r in rows if r["vault"] == name]
        out += [f"vault {name}: {len(mine)} images",
                "  class: " + ", ".join(f"{k} {v}" for k, v in sorted(Counter(r["class"] for r in mine).items())),
                "  cause: " + (", ".join(f"{k} {v}" for k, v in sorted(Counter(r["cause"] for r in mine if r["cause"]).items())) or "none")]
    open_rows = [r for r in rows if r["cause"]]
    out.append(f"images without an address: {len(open_rows)}")
    for r in open_rows[:MAX_LINES]:
        detail = f"twin {r['twin']}" if r["twin"] else f"part {r['part']} (crop {r['part_crop_status']}); recheck {r['node_to_recheck']}" if r["cause"] == "trace" else ""
        out.append(f"  {r['vault']}/{r['path']}: {r['class']}, {r['cause']}{'; ' + detail if detail else ''}")
    if len(open_rows) > MAX_LINES:
        out.append(f"  ... {len(open_rows) - MAX_LINES} more (use --out FILE for the full list)")
    return "\n".join(out)


def _check_out(out):
    """The one file this command writes: never inside the data folders, never over a file that is not an earlier audit."""
    path = Path(out).resolve()
    root = config.ROOT.resolve()
    for folder in (config.VAULTS_DIR, config.STORE, root / "sources", root / "review"):
        if path.is_relative_to(Path(folder).resolve()):
            raise ValueError(f"--out must not be inside {Path(folder).name}/: {out}")
    old = _json(path) if path.exists() else {"rows": []}
    if path.is_dir() or not (isinstance(old, dict) and isinstance(old.get("rows"), list)):
        raise ValueError(f"--out exists and is not an earlier image-audit file: {out}")


def run(vaults, papers, out=None):
    """Audit each conform.Vault in turn; optionally write the rows to the JSON file `out`. Returns the rows."""
    if out:
        _check_out(out)
    rows = [r for v in vaults for r in audit(v, papers)]
    if out:
        Path(out).write_text(json.dumps({"version": 1, "rows": rows}, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    return rows
