"""AI work done by any MCP-connected agent (see `kb.py mcp-config` for the supported clients).

The agent acts as the AI provider. `get_*` returns exactly the prompt the built-in provider would
receive; `submit_*` feeds the agent's reply through the same code path, so every sentence, field or
verdict passes the same checks (A1–A6, whole-token source matching, V13 schema). Provenance is
recorded as provider `mcp_agent` with the model "<client> / <declared model>".

Kinds: synthesis (Concept Notes), enrichment (intervention/model fields), crosscheck (V13), answer.
"""

import copy
import hashlib
import json
import re
from contextlib import contextmanager

from . import answer, concept_notes, concepts, config, crosscheck, db, relations

KINDS = ("synthesis", "enrichment", "crosscheck", "answer")
_STATE = re.compile(r'kb:synthesis-state:(\{.*\})')


def agent_model(client, model=None):
    return " / ".join(x for x in (str(client or "mcp-client"), str(model or "").strip()) if x)


@contextmanager
def acting_as_provider(task, client, model=None, replies=None, capture=None):
    """Temporarily make the MCP agent the provider for `task`.

    replies: a callable (system, user) -> reply text; capture: a list that collects (system, user) and
    makes each call return an empty JSON object (nothing is written from an empty reply)."""
    real = (answer.settings, answer.provider_status, answer._chat)
    base = real[0](task)
    agent_settings = dict(base, provider="mcp_agent", mcp_agent={"model": agent_model(client, model)})

    def settings(t=None):
        return dict(agent_settings) if t in (None, task) else real[0](t)

    def status(s=None):
        return {"ready": True, "provider": "mcp_agent", "model": agent_settings["mcp_agent"]["model"],
                "message": "AI: agent qua MCP"}

    def chat(s, system, user):
        if capture is not None:
            capture.append({"system": system, "user": user})
            return "{}", {}
        return replies(system, user), {"model": agent_settings["mcp_agent"]["model"]}

    answer.settings, answer.provider_status, answer._chat = settings, status, chat
    try:
        yield agent_settings
    finally:
        answer.settings, answer.provider_status, answer._chat = real


def _note_state(vault, record):
    from . import notes
    folder = notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]]
    path = config.vault_dir(vault) / folder / f"{notes.safe_filename(record['name'])}.md"
    if not path.exists():
        return None, path
    m = _STATE.search(path.read_text(encoding="utf-8"))
    return (json.loads(m.group(1)) if m else None), path


# ---------------------------------------------------------------- listing

def list_tasks(con, vault, kind=None, limit=50):
    if kind and kind not in ("synthesis", "enrichment", "crosscheck"):
        raise ValueError(f"unknown kind {kind!r}: use synthesis, enrichment or crosscheck")
    kinds = [kind] if kind else ["synthesis", "enrichment", "crosscheck"]
    out = {}
    if "synthesis" in kinds:
        registry = concepts.load_registry(con)
        rows = []
        for record in registry.records:
            _, links, units = concept_notes._collect(con, record, registry, vault)
            eligible = [u for u in units if u.get("status") in ("ok", "human_confirmed")]
            if not links or not eligible:
                continue
            state, _ = _note_state(vault, record)
            if not state or state.get("status") in ("pending", "stale"):
                rows.append({"id": record["concept_id"], "name": record["name"], "eligible_units": len(eligible),
                             "note_status": (state or {}).get("status", "missing")})
        out["synthesis"] = rows[:limit]
    paper_ids = [r[0] for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ? ORDER BY paper_id",
                                           (vault,))]
    if "enrichment" in kinds:
        rows = []
        for pid in paper_ids:
            n = relations.enrich(con, pid, dry_run=True)["eligible"]
            if n:
                rows.append({"id": pid, "eligible_units": n})
        out["enrichment"] = rows[:limit]
    if "crosscheck" in kinds:
        rows = []
        for pid in paper_ids:
            units = list(db.read_jsonl(db.paper_dir(pid) / "evidence.jsonl"))
            done = crosscheck.results_for(pid, units)
            n = sum(1 for u in units if u.get("unit_type") in crosscheck.ELIGIBLE and u["unit_id"] not in done)
            if n:
                rows.append({"id": pid, "units_without_current_V13": n,
                             "verifier_so_far": crosscheck._load_report(pid).get("model")})
        out["crosscheck"] = rows[:limit]
    out["note"] = ("Pick one task, call get_ai_task, write the reply exactly in the requested JSON format using "
                   "only the supplied evidence, then call submit_ai_task. Every item is checked by code.")
    return out


# ---------------------------------------------------------------- get / submit

def get_task(con, kind, task_id, vault, client="mcp-client"):
    prompts = []
    if kind == "synthesis":
        registry = concepts.load_registry(con)
        record = registry.by_id.get(task_id)
        if record is None:
            raise ValueError(f"unknown concept id {task_id!r}: take ids from list_ai_tasks")
        _, _, units = concept_notes._collect(con, record, registry, vault)
        with acting_as_provider("synthesis", client, capture=prompts):
            concept_notes._generate_synthesis(record, units, _language())
        prompts = prompts[:1]
        how = 'Reply JSON: {"sentences":[{"text":..., "evidence":["<evidence_id>"]}], "questions":[...]}'
    elif kind == "enrichment":
        with acting_as_provider("enrichment", client, capture=prompts):
            relations.enrich(con, task_id)
        how = ('One reply per prompt, keyed by the unit_id inside it: {"<unit_id>": {"intervention": {...}, '
               '"model": {...}, "evidence_sentence_ids": [...]}}. Values must be copied from the sentences.')
    elif kind == "crosscheck":
        batch, unit_ids = crosscheck.pending_batch(task_id)
        prompts = [{"system": crosscheck.SYSTEM, "user": crosscheck.provider_prompt(batch)}] if batch else []
        how = 'Reply JSON: {"reviews":[{"ordinal":n,"verdict":"supported|partly|not_supported","reason":"..."}]}'
        existing = crosscheck._load_report(task_id).get("model")
        if existing and existing != "mcp:" + agent_model(client):
            how += (f" NOTE: this paper's V13 report was made by {existing}; a different verifier is refused "
                    "unless the researcher renames analysis_v13.json.")
    else:
        raise ValueError(f"unknown kind {kind}")
    return {"kind": kind, "id": task_id, "prompts": prompts, "reply_format": how,
            "rules": "Use only the supplied records; never outside knowledge. Keep IDs exactly."}


def submit_task(con, kind, task_id, vault, reply, client="mcp-client", model=None):
    if kind == "synthesis":
        if task_id not in concepts.load_registry(con).by_id:
            raise ValueError(f"unknown concept id {task_id!r}: take ids from list_ai_tasks")
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        with acting_as_provider("synthesis", client, model, replies=lambda s, u: text):
            totals = concept_notes.refresh(con, vault, concept_id=task_id, allow_ai=True)
        registry = concepts.load_registry(con)
        state, path = _note_state(vault, registry.by_id[task_id])
        return {"result": totals, "note_state": state, "note": config.rel(path)}
    if kind == "enrichment":
        by_unit = reply if isinstance(reply, dict) else json.loads(reply)

        def per_unit(system, user):
            unit_id = json.loads(user).get("unit_id")
            value = by_unit.get(unit_id) or {}
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        with acting_as_provider("enrichment", client, model, replies=per_unit):
            return relations.enrich(con, task_id)
    if kind == "crosscheck":
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        with acting_as_provider("crosscheck", client, model, replies=lambda s, u: text) as s:
            return crosscheck.run(con, [task_id], batch_size=10_000,
                                  override=(s, "mcp:" + agent_model(client, model)))
    raise ValueError(f"unknown kind {kind}")


def _language():
    import yaml
    path = config.CONFIG_DIR / "ai.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    lang = ((raw or {}).get("concept_notes") or {}).get("language", "vi")
    return lang if lang in ("vi", "en") else "vi"


# ---------------------------------------------------------------- answers

_PREPARED = {}   # AN-13: the items ask_prepare numbered, kept for ask_check; ponytail: in this process only, 16 asks, lost on restart


def _items_digest(items):
    return hashlib.sha256(json.dumps([i["id"] for i in items]).encode()).hexdigest()


def _asked(question, queries, view, vault, speed):
    return (str(config.DB_PATH), question, json.dumps(queries), view, vault, speed)


def answer_prompt(con, cfg, question, queries, view="google", vault=None, speed="fast", client="mcp-client",
                  index=False, numbers=None, offset=None):
    """Evidence items + the exact answering prompt; the agent must supply English search `queries`.
    `index` (NW-11): the same head fields and numbering, but one-line entries (neural.index_page, paged by `offset`)
    instead of the prompt and the links. `numbers`: those items in full with their links, no prompt. The items and the
    items_digest are the same in every mode (AN-13)."""
    prompts = []
    with acting_as_provider("answer", client, capture=prompts):
        out = answer.answer(con, cfg, question, view=view, speed=speed, vault=vault, queries=queries)
    key = _asked(question, queries, view, vault, speed)
    _PREPARED.pop(key, None)   # a repeated ask moves to the newest place and never evicts another ask
    if len(_PREPARED) >= 16:
        _PREPARED.pop(next(iter(_PREPARED)))
    _PREPARED[key] = (copy.deepcopy(out["items"]), copy.deepcopy(out["queries_run"]))
    from . import discussions, neural
    vault = vault or config.default_vault(cfg)
    notes = neural.paper_note_links(vault, {i["paper_id"] for i in out["items"]})
    decisions = neural.decisions_for(con, vault, question, {i["paper_id"] for i in out["items"]})   # AN-17/AN-19: not items
    links = [{"n": n, "id": i["id"], "item_type": neural.item_type(i.get("sentence_kind"), i["kind"] == "unit"), **notes[i["paper_id"]]}
             for n, i in enumerate(out["items"], 1)]   # NW-12: same numbers as the prompt; out["items"] stays untouched (AN-13 digest)
    head = {"items_found": len(out.get("items") or []), "items_digest": _items_digest(out["items"]),
            "answer_protocol": discussions.protocol(), "not_found": out["not_found"], "not_found_message": out["not_found_message"],
            "overview": out.get("overview"), "scope_note": out.get("scope_note"), **({"decisions": decisions} if decisions else {})}
    if numbers is not None:
        full = neural.fetch_items(head, out["items"], numbers, out.get("papers") or {})
        got = {item["n"] for item in full["items"]}
        return {**full, "links": [row for row in links if row["n"] in got]}
    if index:
        offset = max(int(offset or 0), 0)
        entries = neural.index_page({**head, "offset": offset, "has_more": False, "next_offset": 0},
                                    list(enumerate(out["items"], 1))[offset:])
        end = offset + len(entries)
        return {**head, "offset": offset, "index": entries, "has_more": end < len(out["items"]),
                "next_offset": end if end < len(out["items"]) else None}
    return {**head, "links": links, "prompts": prompts[-1:], "reply_format": "the JSON asked for in the prompt"}


def answer_check(con, cfg, question, queries, reply, view="google", vault=None, speed="fast",
                 client="mcp-client", model=None, items_digest=None, session=None, items_out=None):
    """Checks `reply` against the items ask_prepare numbered (kept; not retrieved again, so [#n] cannot move).
    Without a kept snapshot the items are retrieved again and `items_digest`, when given, must still match.
    With `session` (NW-11) the reply is checked by neural.check against the union of that research session's rounds,
    read from the session file (never from this process's memory).
    `items_out`: a list the checked items are copied into (the MCP dispatch writes its HTML page from them)."""
    if session is not None:
        from . import neural
        state = neural.load_session(session, vault or None)
        digest = _items_digest(state["union"])
        if items_digest and items_digest != digest:
            raise ValueError(f"items_digest {items_digest} is not the digest of research session {session} ({digest}).")
        checked = neural.check(con, session, answer._json(reply) if isinstance(reply, str) else reply)
        if items_out is not None:
            items_out.extend(state["union"])
        return {"answer": checked, "errors": [], "overview": [], "scope_note": checked.get("scope_note"),
                "items_digest": digest, "markdown": checked.get("markdown"), "not_found": False, "not_found_message": None}
    text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
    kept = _PREPARED.get(_asked(question, queries, view, vault, speed))
    with acting_as_provider("answer", client, model, replies=lambda s, u: text):
        out = answer.answer(con, cfg, question, view=view, speed=speed, vault=vault, queries=queries,
                            snapshot=copy.deepcopy(kept) if kept else None)
    digest = _items_digest(out["items"])
    if items_out is not None:
        items_out.extend(out["items"])
    if items_digest and items_digest != digest:
        raise ValueError(f"items_digest {items_digest} is not the digest of the items checked now ({digest}): the [#n] numbers "
                         "would point to other items. Run ask_prepare again and answer from its items.")
    return {"answer": out.get("answer"), "errors": out.get("errors"), "overview": out.get("overview"),
            "scope_note": out.get("scope_note"), "items_digest": digest, "markdown": out.get("markdown"),
            "not_found": out["not_found"], "not_found_message": out["not_found_message"]}
