"""Persistent node decisions (ND-09 and the AI node pass): knowledge_store/node_decisions.jsonl, read by concepts.build_registry.

One JSON object per line, every row live (undo = delete the row):
{key, name, kind: type|no_mesh|not_a_node|add, value (a node type for type/add, else null), rule, classified_by, sentence_ids, at, reason}.
Read side: `rows`/`by_key`/`keys`. Write side (the only writer of the file): `validate`, `submit`, `remove`, `check`
(`kb.py node-decision submit FILE | remove KEY KIND | check`)."""

import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from . import config, db, presentation

KINDS = ("type", "no_mesh", "not_a_node", "add")
FIELDS = ("key", "name", "kind", "value", "rule", "classified_by", "sentence_ids", "at", "reason")
_SID = re.compile(r"([A-Za-z0-9][A-Za-z0-9_.-]*):S\d+")  # CONTRACTS §3 full sentence id
_CACHE = {}  # (path, mtime_ns, size) -> rows: build_registry and mesh_hits read this once per record


def path():
    return config.STORE / "node_decisions.jsonl"  # at call time: tests patch config.STORE


_OVERRIDE = None  # rows injected by using() (the preview); every reader goes through rows()


@contextmanager
def using(injected):
    """Read-only preview: rows() returns *injected* instead of the file, restored even after an exception."""
    global _OVERRIDE
    old, _OVERRIDE = _OVERRIDE, list(injected)
    try:
        yield
    finally:
        _OVERRIDE = old


def rows():
    if _OVERRIDE is not None:
        return _OVERRIDE
    p = path()
    if not p.exists():
        return []
    st = p.stat()
    key = (str(p), st.st_mtime_ns, st.st_size)
    if key not in _CACHE:
        _CACHE.clear()
        _CACHE[key] = list(db.read_jsonl(p))
    return _CACHE[key]


def by_key(kind):
    return {r["key"]: r for r in rows() if r.get("kind") == kind}


def keys(kind):
    return set(by_key(kind))


def _sentence(sid, con, papers):
    """(row, in_vault) of a full sentence id, or (None, False); one read per paper."""
    m = _SID.fullmatch(sid) if isinstance(sid, str) else None
    if not m:
        return None, False
    pid = m[1]
    if pid not in papers:
        papers[pid] = ({r["sentence_id"]: r for r in db.read_jsonl(db.paper_dir(pid) / "sentences.jsonl")},
                       con.execute("SELECT 1 FROM paper_vaults WHERE paper_id=?", (pid,)).fetchone() is not None)
    return papers[pid][0].get(sid), papers[pid][1]


def _row_errors(r, ctx, stored):
    from . import concepts
    clauses, generic, registry, con, papers, pairs, grouped, terms = ctx
    if not isinstance(r, dict):
        return ["row is not an object"]
    if missing := [f for f in FIELDS if f not in r]:
        return [f"missing fields: {', '.join(missing)}"]
    kind, name, value, by, ids = r["kind"], r["name"], r["value"], r["classified_by"], r["sentence_ids"]
    if kind not in KINDS:
        return [f"kind {kind!r} is not one of {'|'.join(KINDS)}"]
    if not isinstance(name, str) or not name or name != concepts._clean(name):
        return ["name is empty or not cleaned (concepts._clean)"]
    if not isinstance(ids, list):
        return ["sentence_ids is not a list"]
    errs = [] if r["key"] == concepts._key(name) else ["key is not concepts._key(name)"]
    if not isinstance(r["key"], str):
        return errs  # the set lookups below need a hashable key
    if kind in ("type", "add"):
        if not isinstance(value, str) or value not in concepts.TYPE_PRIORITY:
            errs.append(f"value {value!r} is not a node type")
    elif value is not None:
        errs.append("value must be null")
    if not isinstance(r["rule"], str) or r["rule"] not in clauses:
        errs.append(f"rule {r['rule']!r} is not a clause id of config/standard.yaml")
    ai = isinstance(by, str) and by.startswith("ai:")
    if not (isinstance(by, str) and by in clauses or ai and by[3:].strip()):
        errs.append("classified_by must be a clause id or ai:<model>")
    if (ai or kind == "add") and not ids:
        errs.append("needs at least one sentence id")
    record = registry.lookup(name)
    if kind == "add":
        if record is not None and not stored:  # stored: the add row itself made the node
            errs.append("name already names a node")
        if r["key"] in generic:
            errs.append("key is a generic term")
        if merged := ({b if a == r["key"] else a for a, b in pairs if r["key"] in (a, b)} - {r["key"]}) & registry.by_key.keys():
            errs.append(f"key merges with node {registry.by_key[min(merged)]['name']!r} through a paper abbreviation (use a type row)")
    elif record is None:
        if not (stored and kind == "not_a_node"):  # stored: the row itself removed the node
            errs.append("name names no node")
    elif kind != "no_mesh" and record.get("status") == "human_confirmed" or kind == "type" and record.get("type") == "topic":
        errs.append(f"a {kind} row is ignored on a human_confirmed node, a type row on a topic node (ND-09)")
    if kind == "not_a_node" and r["key"] in grouped:
        errs.append("key is in a config/synonyms.yaml group, which keeps the node: edit the group instead")
    if kind == "not_a_node" and record is not None and (members := sorted(  # keys of the node's own terms, not of their spelling variants
            k for k, v in registry.by_key.items() if v["concept_id"] == record["concept_id"] and k in terms)) != [r["key"]]:
        others = [k for k in members if k != r["key"]]
        errs.append(f"key is one of several terms of node {record['name']!r} (other keys: {', '.join(others)}): the build would split "
                    "the node under a new id, not remove it, and a row per key is refused the same way; edit the node's terms/synonyms instead")
    synonyms = [] if kind == "add" or record is None else record.get("synonyms") or []
    for sid in ids:
        row, in_vault = _sentence(sid, con, papers)
        if row is None:
            errs.append(f"sentence {sid!r} is not a full id of an existing sentence")
        elif not presentation.body_sentence(row):
            errs.append(f"sentence {sid} is not body text")
        elif not in_vault:
            errs.append(f"sentence {sid}: paper is in no vault")
        elif not presentation.matches_term(row.get("text"), name, synonyms):
            errs.append(f"sentence {sid} does not match the node")
    return errs


def _node(r, registry):
    """concept_id of the node whose type a type/add row sets, or None."""
    if isinstance(r, dict) and r.get("kind") in ("type", "add") and isinstance(r.get("name"), str):
        return (registry.lookup(r["name"]) or {}).get("concept_id")


def validate(rows, con, registry, stored=False):
    """[(index, key, reason)] for every problem in *rows*; pure. stored=True re-checks rows already built into *registry*."""
    from . import concepts, standard
    adds = {r["key"] for r in rows if isinstance(r, dict) and r.get("kind") == "add" and isinstance(r.get("key"), str)}
    generic = concepts.generic_keys()
    ctx = ({c["id"] for c in standard.load()["clauses"]}, generic, registry, con, {},
           concepts.abbreviation_pairs(set(registry.by_key) | adds) if adds else [],  # the build merges an add through these
           {concepts._key(t) for g in concepts.synonym_groups() for t in g},
           ({concepts._key(t) for _, t in concepts._raw_terms(con)} | keys("add")) - generic  # the terms a build drops by key
           if any(isinstance(r, dict) and r.get("kind") == "not_a_node" for r in rows) else set())
    new_add = {r["key"] for r in rows if isinstance(r, dict) and r.get("kind") == "add"
               and isinstance(r.get("key"), str) and r["key"] not in registry.by_key}  # add rows that are not a node yet
    out, seen, typed = [], set(), {}  # typed: node -> key of its type/add row (the build would pick the smallest key)
    for i, r in enumerate(rows):
        key = r.get("key") if isinstance(r, dict) else None
        out += [(i, key, why) for why in _row_errors(r, ctx, stored)]
        if isinstance(r, dict) and r.get("kind") == "add" and isinstance(key, str) and key in new_add and (clash := sorted(
                {b if a == key else a for a, b in ctx[5] if key in (a, b)} & new_add)):  # the partners the build merges it with
            out.append((i, key, f"key merges with new add row {clash[0]!r} through a paper abbreviation (the build keeps one row's "
                                "type and sentence ids): submit one add row"))
        if (node := _node(r, registry)) and typed.setdefault(node, key) != key:
            out.append((i, key, "the node already has a type or add row under another key"))
        if isinstance(r, dict) and (pair := (str(key), str(r.get("kind")))) in seen:
            out.append((i, key, "duplicate key+kind in the file"))
        elif isinstance(r, dict):
            seen.add(pair)
    return out


def _write(rows_):
    from . import concepts
    db.write_jsonl(path(), rows_)  # temp file in the same folder + replace
    concepts.invalidate_registry()


def read_file(file):
    """The rows of FILE (.jsonl or a JSON list), `at` filled in."""
    text = Path(file).read_text(encoding="utf-8-sig")  # PowerShell 5.1 Out-File writes a BOM
    new = json.loads(text) if str(file).endswith(".json") else [json.loads(line) for line in text.splitlines() if line.strip()]
    if not isinstance(new, list):
        raise ValueError("the file must hold a JSON list or one JSON object per line")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return [{**r, "at": r.get("at") or now} if isinstance(r, dict) else r for r in new]


def file_errors(new, con, registry):
    """[(index, key, reason)] of *new* rows against the rules and against the stored rows; pure (submit and preview share it)."""
    errors = validate(new, con, registry)
    have = {(r["key"], r["kind"]) for r in rows()}
    typed = {_node(r, registry): r["key"] for r in rows()}
    errors += [(i, r["key"], "already stored; remove first") for i, r in enumerate(new)
               if isinstance(r, dict) and (str(r.get("key")), str(r.get("kind"))) in have]
    errors += [(i, r.get("key"), "the node already has a stored type or add row; remove it first") for i, r in enumerate(new)
               if (node := _node(r, registry)) and typed.get(node, r.get("key")) != r.get("key")]
    return sorted(errors, key=lambda e: e[0])


def submit(file, con, registry):
    """Validate every row of FILE (.jsonl or a JSON list) and append them all, or write nothing. Returns (errors, added)."""
    new = read_file(file)
    errors = file_errors(new, con, registry)
    if not errors:
        _write([*rows(), *new])
    return errors, new


def remove(key, kind):
    """Drop the one live (key, kind) row; ValueError when absent. Undo of a decision = remove + rebuild."""
    old = rows()
    gone = [r for r in old if r.get("key") == key and r.get("kind") == kind]
    if not gone:
        raise ValueError(f"no {kind} row with key {key!r}")
    _write([r for r in old if r not in gone])
    return gone[0]


def check(con, registry):
    """Re-validate every stored row against today's data (reextract renumbers sentences but does not rewrite this file)."""
    return validate(rows(), con, registry, stored=True)
