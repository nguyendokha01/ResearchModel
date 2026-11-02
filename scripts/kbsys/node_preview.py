"""`kb.py node-decision preview [FILE] --out PATH`: read-only. Shows every node change the G5 rebuild (stored concepts.jsonl S -> a
fresh build B without decision rows) and the decision rows (B -> F, a fresh build with the stored rows + FILE) would make, each with its
cause and its undo. Also the dry-run validation of a rows FILE. Nothing is written except the Markdown report."""

from pathlib import Path

from . import concepts, config, db, node_decisions, structure

REBUILD_UNDO = "retire undo map at G5 (kb.py vault-migrate --vault V --undo MAP)"
FIELDS = ("type", "classified_by", "mesh_ui", "name", "synonyms", "parents", "source_sentence_ids")
CAUSE_KINDS = {"type": ("type", "add"), "classified_by": ("type", "add"), "source_sentence_ids": ("add",),
               "mesh_ui": ("no_mesh",), "synonyms": ("no_mesh",), "parents": ("no_mesh",)}  # any other field: every kind


def _view(r):
    return {f: len(r.get(f) or []) if f in ("synonyms", "parents") else r.get(f, "terms" if f == "classified_by" else None) for f in FIELDS}


def _diff(old, new):
    """(changed [(old, new, fields)], gone [old], added [new]) by concept_id."""
    o, n = {r["concept_id"]: r for r in old}, {r["concept_id"]: r for r in new}
    changed = []
    for cid in o.keys() & n.keys():
        a, b = _view(o[cid]), _view(n[cid])
        if fields := [f for f in FIELDS if a[f] != b[f]]:
            changed.append((o[cid], n[cid], fields))
    gone = [r for r in old if r["concept_id"] not in n or o[r["concept_id"]] is not r]  # also a record shadowed by a later one with its id
    return (sorted(changed, key=lambda c: c[0]["concept_id"]), sorted(gone, key=lambda r: r["concept_id"]),
            [n[c] for c in sorted(n.keys() - o.keys())])


def _build(con, rows):
    """The registry a build would produce with exactly *rows* as node_decisions.jsonl; the cache is dropped before and after."""
    with node_decisions.using(rows):
        concepts.invalidate_registry()
        try:
            return concepts.build_registry(con)
        finally:
            concepts.invalidate_registry()


def _shown(con, records, registry):
    """{concept_id} of *records* that pass ND-05 (`concepts.meets`) in at least one vault, counted from the papers' sentences:
    the concept_links table does not know a new node. ponytail: one pass over every vault paper per call, fine for a few add rows."""
    from . import concept_notes, node_profile
    ids = {r["concept_id"]: concept_notes._descendants(registry, r["concept_id"]) for r in records}
    pats = {c: concept_notes._text_patterns(i, registry) for c, i in ids.items()}
    totals = {}  # (concept_id, vault) -> [mentions, evidence units]
    for paper_id, vault in con.execute("SELECT paper_id, vault FROM paper_vaults").fetchall():
        sentences = {(s.get("sentence_id") or s.get("id")): s for s in structure.paper_rows(paper_id, "sentences.jsonl")
                     if s.get("section") != "references" and (s.get("kind") == "table_row" or not s.get("in_table"))}  # as concept_notes._collect
        units = list(structure.paper_rows(paper_id, "evidence.jsonl"))
        for cid, pat in pats.items():
            hit = {sid for sid, s in sentences.items()
                   if concept_notes._text_concept_ids({"quote": s.get("text") or ""}, ids[cid], registry, "", pat) & ids[cid]}
            n = totals.setdefault((cid, vault), [0, 0])
            n[0] += len(hit)
            n[1] += sum(bool(node_profile._source_ids(u.get("sentence_ids"), sentences, paper_id=paper_id)[0] & hit) for u in units)
    return {cid for (cid, _), (mentions, units) in totals.items() if concepts.meets(units, mentions)}


def _line(record, change, cause, undo, sids=()):
    ids, name, rest = sorted(set(sids)), record["name"], f" | {change} | cause: {cause} | undo: {undo}"
    if (over := len(f"- `{record['concept_id']}` {name}{rest}") - 200) > 0:  # cut the name, never the concept_id or the undo
        name = name[:max(len(name) - over - 1, 1)] + "…"
    head = f"- `{record['concept_id']}` {name}{rest}"
    for n in range(min(len(ids), 3), 0, -1):  # as many sentence ids as fit in 200 chars (the ids themselves are never cut)
        tail = f" | sentences: {', '.join(ids[:n])}{f' +{len(ids) - n}' if len(ids) > n else ''}"
        if len(head + tail) <= 200:
            return head + tail
    return head


def _decision_line(record, rows, change, kinds, file_keys, before=None):
    """One B -> F line: the rows (of *kinds*, else of any kind) that name the node in F or *before* (its B node) are the cause;
    `node-decision remove` is the undo. A not_a_node row on one term of a node splits or shrinks it."""
    keys = {concepts._key(v) for r in (record, before or {}) for v in [r.get("name"), *(r.get("synonyms") or ())] if v}
    named = [r for r in rows if r.get("kind") in kinds and r.get("key") in keys] or [r for r in rows if r.get("key") in keys]
    cause = "; ".join(f"{r['kind']} {r['rule']}/{r['classified_by']}" for r in named) or "unknown row"
    undo = "; ".join(f"remove {r['key']} {r['kind']}" + (" (in FILE: drop it)" if (r["key"], r["kind"]) in file_keys else "")
                     for r in named) or "-"
    return _line(record, change, cause, undo, [s for r in named for s in r.get("sentence_ids") or ()])


def run(con, file, out):
    """Write the report to *out*; returns (counts text, out path, failed). Never writes the store."""
    out = Path(out).resolve()
    if any(out.is_relative_to(d.resolve()) for d in (config.STORE, config.VAULTS_DIR, config.ROOT / "sources", config.ROOT / "review")):
        raise ValueError("--out must not be under knowledge_store/, vaults/, sources/ or review/")
    stored = list(node_decisions.rows())
    new = node_decisions.read_file(file) if file else []
    old_path = config.STORE / "concepts.jsonl"
    s_records = list(db.read_jsonl(old_path)) if old_path.exists() else []
    base = _build(con, [])
    current = _build(con, stored) if stored else base  # what submit validates against
    errors = node_decisions.file_errors(new, con, current) if new else []
    final = current if not new else None if errors else _build(con, [*stored, *new])
    file_keys = {(r.get("key"), r.get("kind")) for r in new if isinstance(r, dict)}

    sb_changed, sb_gone, sb_added = _diff(s_records, base.records)
    sections = {"changed": [], "merged": [], "removed": [], "added": []}
    for old, now, fields in sb_changed:
        nd09 = now.get("classified_by") == "ND-09" and old.get("classified_by", "terms") != "ND-09"
        sections["changed"].append(_line(now, "; ".join(f"{f} {_view(old)[f]} -> {_view(now)[f]}" for f in fields),
                                         "ND-09" if nd09 else "rebuild", "a clause change (ND-09)" if nd09 else REBUILD_UNDO))
    for old in sb_gone:
        into = next((t for v in [old["name"], *(old.get("synonyms") or ())] if (t := base.lookup(v))), None)
        key = "merged" if into else "removed"
        sections[key].append(_line(old, f"{key.upper()} into `{into['concept_id']}` {into['name']}" if into else "REMOVED", "rebuild", REBUILD_UNDO))
    sections["added"] = [_line(r, "ADDED", "rebuild", REBUILD_UNDO) for r in sb_added]

    bf = {"changed": [], "removed": [], "added": []}
    if final is not None:
        rows = [*stored, *new]
        changed, gone, added = _diff(base.records, final.records)
        for old, now, fields in changed:
            kinds = {k for f in fields for k in CAUSE_KINDS.get(f, node_decisions.KINDS)}
            bf["changed"].append(_decision_line(now, rows, "; ".join(f"{f} {_view(old)[f]} -> {_view(now)[f]}" for f in fields), kinds, file_keys, old))
        bf["removed"] = [_decision_line(r, rows, "REMOVED (not_a_node)", ("not_a_node",), file_keys) for r in gone]
        low = {r["concept_id"] for r in added} - (_shown(con, added, final) if added else set())
        bf["added"] = [_decision_line(r, rows, "ADDED (" + r["type"] + ")" + (" - below ND-05: no note will link it" if r["concept_id"] in low else ""),
                                      ("add",), file_keys, base.lookup(r["name"])) for r in added]

    counts = (f"S -> B (G5 rebuild): changed={len(sections['changed'])} merged={len(sections['merged'])} "
              f"removed={len(sections['removed'])} added={len(sections['added'])}; registry {len(s_records)} -> {len(base.records)}\n"
              f"B -> F (decision rows): " + ("not computed (file errors)" if final is None else
                                             f"changed={len(bf['changed'])} removed={len(bf['removed'])} added={len(bf['added'])}")
              + f"\nfile errors: {len(errors)}")
    text = ["# Node preview (read-only)", "", "```", counts, "```", "",
            "Undo of a decision row: `kb.py node-decision remove KEY KIND`, then rebuild. Undo of a rebuild effect: see each line.", ""]
    if errors:
        text += ["## FILE errors", "", *(f"- row {i} [{k}]: {why}" for i, k, why in errors), ""]
    for title, parts in (("S -> B: what the G5 rebuild changes", sections), ("B -> F: what the decision rows change", bf)):
        for name, lines in parts.items():
            text += [f"## {title} - {name} ({len(lines)})", "", *(lines or ["(none)"]), ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(text), encoding="utf-8", newline="\n")
    return counts, out, bool(errors)
