"""G6 unit 5: `kb.py canvas` - one node, its typed neighbours and the walk's steps as an Obsidian Canvas (JSON Canvas 1.0).
All data comes from neural.walk (NW-05); this module only lays it out and writes it."""
import hashlib
import itertools
import json
import math

from . import config, db, neural, notes

W, H = 320, 120          # file node size
HUB_COLOR = "1"          # canvas preset colour for hub neighbours (RL-09)
MARKER = "kb_machine"    # top-level key Obsidian ignores: sha256 of the nodes+edges we wrote (an edited canvas no longer matches)


def _digest(data):
    return hashlib.sha256(json.dumps([data.get("nodes"), data.get("edges")], sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def build(result):
    """Canvas dict from a found walk result: centre at (0,0), neighbours on one ring in walk order, one edge per step."""
    def file_node(nid, path, x, y):
        return {"id": nid, "type": "file", "file": path, "x": round(x - W / 2), "y": round(y - H / 2), "width": W, "height": H}

    nodes = [file_node("n0", result["node"]["path"], 0, 0)]
    ids = {}
    for step in result["steps"]:  # walk order: sorted by (target_path, via)
        if step["target_path"] not in ids:
            ids[step["target_path"]] = f"n{len(ids) + 1}"
    radius = max(400.0, len(ids) * (W + 80) / (2 * math.pi))
    for i, path in enumerate(ids):
        angle = 2 * math.pi * i / len(ids)
        node = file_node(ids[path], path, radius * math.cos(angle), radius * math.sin(angle))
        if any(s["hub"] for s in result["steps"] if s["target_path"] == path):
            node["color"] = HUB_COLOR
        nodes.append(node)
    edges = [{"id": f"e{i}", "fromNode": "n0", "toNode": ids[s["target_path"]], "label": "/".join(dict.fromkeys(s["etypes"])),
              "kb_via": s.get("via")}  # the note that carries the link (rule 3 trace); Obsidian ignores the key
             for i, s in enumerate(result["steps"], 1)]
    data = {"nodes": nodes, "edges": edges}
    return {MARKER: _digest(data), **data}


def _ours(path, centre):
    """Our canvas of this same node, untouched since we wrote it (any researcher edit changes the digest)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data[MARKER] == _digest(data) and data["nodes"][0]["file"] == centre
    except (OSError, ValueError, LookupError, TypeError):
        return False


def write(con, vault, node, limit=20):
    """Write <vault>/Canvas/<name>.canvas; (path, None or a note: name kept / walk cut) or (None, NOT_FOUND message).
    A canvas that is not our untouched canvas of this node is never overwritten: the next free <name>_N.canvas is used."""
    config.require_research_vault(vault)
    result = neural.walk(con, vault, node, limit)
    if not result["found"]:
        return None, result["message"]
    folder = config.vault_dir(vault) / "Canvas"
    if not folder.resolve().is_relative_to(config.vault_dir(vault).resolve()):  # a link/junction out of the vault
        raise ValueError(f"{folder} leads out of the vault")
    stem = notes.safe_filename(result["node"]["title"])
    out = folder / f"{stem}.canvas"
    for n in itertools.count(1):
        if not out.exists() or _ours(out, result["node"]["path"]):
            break
        out = folder / f"{stem}_{n}.canvas"
    db.write_json(out, build(result))
    said = [f"{stem}.canvas was edited or is not this node's: kept"] if out.stem != stem else []
    said += [f"{len(result['steps'])} of {result['total']} steps shown (raise --limit)"] if result["truncated"] else []
    return out, "; ".join(said) or None
