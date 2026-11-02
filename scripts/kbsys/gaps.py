"""Candidate knowledge gaps (STATUS A11): deterministic, no AI, no network.

Reads accepted evidence units of one vault and writes `knowledge_store/gap_candidates.jsonl`.
Every row is a CANDIDATE (`status: needs_review`): "not found in the current vault" is never
"does not exist in the literature". Only the researcher moves a gap to `Knowledge Gaps/`.
Regeneration never touches a row whose status the researcher changed.
"""

import hashlib
import itertools
import re

from . import concept_notes, concepts, config, db, evidence, relations

GAP_TYPES = ("unexplained_conflict", "unproven_hypothesis", "untested_combination", "broken_chain")
SCOPE_NOTE = "not found in the current vault — this does not mean it is absent from the literature"
MACHINE_STATUSES = ("needs_review", "withdrawn_by_rule")  # any other status is a human decision
ACCEPTED = ("ok", "human_confirmed")
DEFAULTS = {"max_per_type": 100, "max_untested_combinations": 15}


def path():
    return config.STORE / "gap_candidates.jsonl"


def _key(value):
    return relations._raw_measure_key(value)


def _gap_id(gap_type, vault, key):
    return "gap:" + hashlib.sha1(f"{gap_type}|{vault}|{key}".encode("utf-8")).hexdigest()[:12]


def _record(gap_type, vault, key, description, supporting_units, queries_run, papers_scanned, **extra):
    return {"gap_id": _gap_id(gap_type, vault, key), "gap_type": gap_type, "vault": vault,
            "description": description, "supporting_units": list(supporting_units), "queries_run": list(queries_run),
            "papers_scanned": papers_scanned, "scope_note": SCOPE_NOTE, "status": "needs_review",
            "provenance": "generator", "created_at": config.now(), **extra}


def _scan(vault):
    """(papers scanned, slim accepted result units, slim hypothesis / hedged-interpretation units)."""
    n, results, claims, papers = 0, [], [], set()
    for folder in sorted(config.PAPERS_DIR.iterdir()) if config.PAPERS_DIR.exists() else []:
        meta = db.read_json(folder / "metadata.json", None) if (folder / "metadata.json").is_file() else None
        if not meta or vault not in (meta.get("vaults") or []):
            continue
        n += 1
        papers.add(folder.name)
        for u in db.read_jsonl(folder / "evidence.jsonl") or []:
            if u.get("status") not in ACCEPTED:
                continue
            terms, outcome = u.get("terms") or {}, u.get("outcome") or {}
            slim = {"id": u["unit_id"], "paper": folder.name, "statement": u.get("statement") or "",
                    "type": u.get("unit_type"), "measure": outcome.get("measure") or "",
                    "direction": outcome.get("direction"),
                    "agent": (u.get("intervention") or {}).get("agent") or "",
                    "species": [s for s in [(u.get("model") or {}).get("species")] if s] or list(terms.get("species") or []),
                    "entities": [e for e in [*(terms.get("concepts") or []), *(terms.get("molecules") or [])] if e]}
            if u.get("unit_type") == "result":
                results.append(slim)
            elif u.get("unit_type") == "hypothesis" or (u.get("unit_type") == "interpretation"
                                                         and u.get("hedging") == "hedged"):
                claims.append(slim)
    return n, results, claims, papers


def _unexplained_conflicts(vault, n, papers):
    for r in relations.conflict_relations(vault):
        if not all(str(u).rsplit(":E", 1)[0] in papers for u in (r["from_unit"], r["to_unit"])):
            continue  # a relation without a vault field is listed for every vault; keep this vault's papers only
        if r.get("explanation") or r.get("human_explanation"):
            continue
        a, b = r["from_unit"], r["to_unit"]
        measure = r.get("shared_concept_name") or r.get("measure_group") or r.get("outcome_measure") or "an outcome"
        d = list(r.get("directions") or [None, None]) + [None, None]
        yield _record("unexplained_conflict", vault, r.get("relation_id") or "|".join(sorted((a, b))),
                      f"Active conflict ({r['type']}) on '{measure}' between {a} ({d[0]}) and {b} ({d[1]}) has no "
                      f"recorded human explanation (status {r.get('status')}).", [a, b],
                      ["relations.conflict_relations: active conflict relations of the vault",
                       f"relation {r.get('relation_id')}: no explanation / human explanation field"], n,
                      relation_id=r.get("relation_id"))


def _index(results):
    """Padded normalised text of each result (measure, terms, statement) and a token -> result index."""
    texts, index = [], {}
    for i, r in enumerate(results):
        texts.append(" " + _key(" ".join([r["measure"], *r["entities"], r["statement"]])) + " ")
        for token in set(texts[-1].split()):
            index.setdefault(token, []).append(i)
    return texts, index


def _about(entity, texts, index):
    """Whole-token match of an entity (or its simple plural/singular) in any result text."""
    for variant in {entity, entity + "s", entity[:-1] if entity.endswith("s") else entity}:
        rare = min(variant.split(), key=lambda t: len(index.get(t, ())))
        if any(f" {variant} " in texts[i] for i in index.get(rare, ())):
            return True
    return False


def _unproven_hypotheses(vault, n, results, claims, registry):
    """RL-08: an entity is mentioned under its own name or any name of its registry concept or a child concept (never a parent)."""
    texts, index = _index(results)

    def mentioned(entity, raw):
        record = registry.lookup(raw) or registry.lookup(entity)  # raw first: _key drops "." "+" (IL-1β, H3.3); none = string only
        ids = concept_notes._descendants(registry, record["concept_id"]) if record else ()
        names = [entity] + [_key(v) for cid in ids for v in (registry.by_id[cid]["name"], *registry.by_id[cid].get("synonyms", []))]
        return any(_about(v, texts, index) for v in names if v)

    for c in sorted(claims, key=lambda c: c["id"]):
        raw = {}
        for e in c["entities"]:
            raw.setdefault(_key(e), e)
        entities = [e for e in raw if len(e) >= 3]
        missing = [e for e in entities if not mentioned(e, raw[e])]
        if not missing:  # no entity to check, or every entity has result evidence
            continue
        kind = "Hypothesis" if c["type"] == "hypothesis" else "Hedged interpretation"
        yield _record("unproven_hypothesis", vault, c["id"],
                      f"{kind} {c['id']}: \"{c['statement']}\" — no accepted result unit in this vault mentions "
                      f"{', '.join(repr(e) for e in missing)} in its measure, terms or statement.", [c["id"]],
                      ["evidence: accepted hypothesis / hedged interpretation units with concept or molecule terms",
                       f"evidence: accepted result units (measure, terms, statement) mentioning {', '.join(missing)}"
                       " under its own name, a registry synonym or a child concept"],
                      n, entities=missing)


def _untested_combinations(vault, n, results, limit):
    """Intervention agent A and species S both have results for measure M, but never in one paper's results."""
    by_measure, paper_agents, paper_species = {}, {}, {}
    for r in results:
        a = _key(r["agent"])
        species = {_key(s): s for s in r["species"] if _key(s)}
        if a:
            paper_agents.setdefault(r["paper"], set()).add(a)
        paper_species.setdefault(r["paper"], set()).update(species)
        group = relations.measure_group(r["measure"])
        m = (group or {}).get("group_id") or _key(r["measure"])
        if not m or relations._is_generic_measure_text(r["measure"]):
            continue
        entry = by_measure.setdefault(m, {"name": (group or {}).get("name") or r["measure"], "agent": {}, "species": {}})
        if a:
            entry["agent"].setdefault(a, {"name": r["agent"], "units": []})["units"].append(r["id"])
        for s, raw in species.items():
            entry["species"].setdefault(s, {"name": raw, "units": []})["units"].append(r["id"])
    together = {pair for paper in paper_agents
                for pair in itertools.product(paper_agents[paper], paper_species.get(paper, ()))}
    found = []
    for m, entry in by_measure.items():
        for (a, ea), (s, es) in itertools.product(entry["agent"].items(), entry["species"].items()):
            if (a, s) in together:
                continue
            units = sorted(set(ea["units"]))[:5] + sorted(set(es["units"]))[:5]
            found.append((-(len(set(ea["units"])) + len(set(es["units"]))), f"{m}|{a}|{s}",
                          entry["name"], ea["name"], es["name"], units))
    for _, key, measure, agent, species, units in sorted(found)[:limit]:
        yield _record("untested_combination", vault, key,
                      f"'{agent}' and '{species}' each have accepted results for '{measure}' in this vault, but no "
                      f"paper reports them together (ranked by evidence count; top {limit} only).", units,
                      [f"evidence: accepted result units with intervention.agent = {agent} and outcome '{measure}'",
                       f"evidence: accepted result units on species {species} and outcome '{measure}'",
                       "per paper: agent and species never both present in one paper's results"], n,
                      entities=[agent, species], measure=measure)


def _broken_chains(vault, n, results):
    """A -> B (agent A changes a measure naming B) and B -> C (agent B changes C), but no A -> C unit.

    Subject = intervention.agent, object = outcome.measure, direction = increase / decrease only.
    """
    by_agent = {}
    for r in results:
        if _key(r["agent"]) and r["direction"] in ("increase", "decrease") and _key(r["measure"]):
            by_agent.setdefault(_key(r["agent"]), []).append(r)
    for a, first in sorted(by_agent.items()):
        for u1 in first:
            m1 = f" {_key(u1['measure'])} "
            for b in sorted(k for k in by_agent if k != a and len(k) >= 3 and f" {k} " in m1):
                for u2 in by_agent[b]:
                    m2 = _key(u2["measure"])
                    if f" {m2} " in m1 or any(f" {m2} " in f" {_key(u.get('measure'))} " for u in first):
                        continue
                    yield _record("broken_chain", vault, f"{u1['id']}|{u2['id']}",
                                  f"'{u1['agent']}' {u1['direction']} '{u1['measure']}' ({u1['id']}) and '{u2['agent']}' "
                                  f"{u2['direction']} '{u2['measure']}' ({u2['id']}), but no accepted result links "
                                  f"'{u1['agent']}' to '{u2['measure']}'.", [u1["id"], u2["id"]],
                                  [f"evidence: result units with agent '{u1['agent']}' whose measure names '{b}'",
                                   f"evidence: result units with agent '{b}'",
                                   f"evidence: no result unit with agent '{u1['agent']}' and measure '{u2['measure']}'"],
                                  n, entities=[u1["agent"], b, u2["measure"]])


def _merge(existing, found, vault, stats):
    """Merge by gap_id: human-decided and analyst rows are kept as is; generator rows follow the new run."""
    out, seen = [], {row.get("gap_id") for row in existing}
    used = set()
    for row in existing:
        rec, gid = found.get(row.get("gap_id")), row.get("gap_id")
        mine = row.get("vault") == vault and row.get("provenance") == "generator"
        if row.get("status") not in MACHINE_STATUSES:
            stats["kept_human"] += rec is not None
        elif mine and rec and gid not in used:
            new = {**row, **rec, "created_at": row.get("created_at") or rec["created_at"], "status": "needs_review"}
            for k in ("withdrawn_at", "withdrawn_reason"):
                new.pop(k, None)
            stats["revived" if row.get("status") == "withdrawn_by_rule" else "updated" if new != row else "unchanged"] += 1
            row = new
        elif mine and row.get("status") == "needs_review":
            row = {**row, "status": "withdrawn_by_rule", "withdrawn_at": config.now(),
                   "withdrawn_reason": "no longer derivable from the current evidence"}
            stats["withdrawn"] += 1
        used.add(gid)
        out.append(row)
    for gid in sorted(set(found) - seen):
        out.append(found[gid])
        stats["new"] += 1
    return out


def generate(vault, dry_run=False, cfg=None):
    settings = {**DEFAULTS, **((cfg or {}).get("gaps") or {})}
    n, results, claims, papers = _scan(vault)
    cap = int(settings["max_per_type"])
    streams = {"unexplained_conflict": _unexplained_conflicts(vault, n, papers),
               "unproven_hypothesis": _unproven_hypotheses(vault, n, results, claims, concepts.load_registry(None)),
               "untested_combination": _untested_combinations(vault, n, results, int(settings["max_untested_combinations"])),
               "broken_chain": _broken_chains(vault, n, results)}
    found, counts, examples = {}, {}, {}
    for gap_type, stream in streams.items():
        rows = sorted(stream, key=lambda r: r["gap_id"]) if gap_type != "untested_combination" else list(stream)
        counts[gap_type] = len(rows)
        examples[gap_type] = [r["description"] for r in rows[:3]]
        found.update({r["gap_id"]: r for r in rows[:cap]})
    stats = dict.fromkeys(("new", "updated", "unchanged", "revived", "withdrawn", "kept_human"), 0)
    existing = list(db.read_jsonl(path()) or [])
    merged = _merge(existing, found, vault, stats)
    if merged != existing and not dry_run:
        db.write_jsonl(path(), merged)
    return {"vault": vault, "dry_run": dry_run, "papers_scanned": n, "candidates_by_type": counts, **stats,
            "examples": examples, "scope_note": SCOPE_NOTE}


def list_candidates(vault, include_withdrawn=False):
    """Candidate rows of one vault (strict match; unknown vault -> ValueError); withdrawn ones are history."""
    config.require_research_vault(vault)
    return [r for r in db.read_jsonl(path()) or []
            if r.get("vault") == vault
            and (include_withdrawn or r.get("status") != "withdrawn_by_rule")]


def propose(con, vault, description, supporting_units, gap_type, analyst="mcp-client", queries_run=None,
            papers_scanned=None):
    """An agent-proposed candidate: same file, `needs_review`, provenance `analyst`. Never a verdict."""
    if gap_type not in GAP_TYPES:
        raise ValueError(f"gap_type must be one of {', '.join(GAP_TYPES)}")
    description = " ".join(str(description or "").split())
    if not description:
        raise ValueError("description is required")
    units = list(dict.fromkeys(supporting_units if isinstance(supporting_units, list) else []))
    if not units:
        raise ValueError("supporting_units must list at least one evidence unit ID")
    for uid in units:
        unit = evidence.resolve_unit(con, uid)
        meta = db.read_json(db.paper_dir(str(uid).rsplit(":", 1)[0]) / "metadata.json", {}) or {}
        if not unit or unit.get("status") not in ACCEPTED or vault not in (meta.get("vaults") or []):
            raise ValueError(f"supporting unit {uid} is not an accepted evidence unit of vault {vault}")
    record = _record(gap_type, vault, _key(description), description, units,
                     [str(q) for q in queries_run or []], papers_scanned if isinstance(papers_scanned, int) else None,
                     proposed_by=analyst)
    record["provenance"] = "analyst"
    existing = list(db.read_jsonl(path()) or [])
    prior = next((r for r in existing if r.get("gap_id") == record["gap_id"]), None)
    if prior is None:
        db.write_jsonl(path(), existing + [record])
    return {"gap": prior or record, "created": prior is None}


# Researcher 13/10: these are NOT knowledge gaps. A knowledge gap is made in a discussion: evidence from the papers +
# the AI's own knowledge (labelled, never evidence) + the researcher's judgment, saved with save_discussion(kind=
# "knowledge_gap"). The machine list below is raw material for that brainstorm.
CATALOGUE_NOTE = "Open hypotheses"
OLD_CATALOGUE_NOTE = "Candidate gaps"
CATALOGUE_BANNER = ("> [!info] Giả thuyết mở — nguyên liệu để thảo luận, không phải knowledge gap\n"
                    "> Tác giả nêu giả thuyết/diễn giải mà chưa có kết quả nào trong vault này kiểm chứng. Vắng trong vault "
                    "không có nghĩa là vắng trong y văn. Knowledge gap chỉ hình thành khi bạn thảo luận với AI "
                    "(bằng chứng + kiến thức của AI có ghi rõ + nhận định của bạn) và bảo lưu lại.")
CATALOGUE_TITLES = {"unproven_hypothesis": "Giả thuyết chưa có kết quả kiểm chứng (Unproven hypotheses)",
                    "untested_combination": "Tổ hợp chưa được thử cùng nhau (Untested combinations)",
                    "broken_chain": "Chuỗi suy luận còn thiếu mắt xích (Broken chains)"}
_UNIT_ID = re.compile(r"[A-Za-z0-9_-]+:E[0-9]{4,}")


def write_catalogue(vault):
    """`_Maps/Open hypotheses.md`: machine catalogue grouped by type (reads files only). Unexplained conflicts are
    listed in Open conflicts (tag #conflict), so here they are only counted."""
    from . import notes, presentation, vault_layout
    metas, units = {}, {}

    def unit(uid):
        pid = str(uid).rsplit(":E", 1)[0]
        if pid not in units:
            units[pid] = {u.get("unit_id"): u for u in db.read_jsonl(db.paper_dir(pid) / "evidence.jsonl") or []}
            metas[pid] = {**(db.read_json(db.paper_dir(pid) / "metadata.json", {}) or {}), "paper_id": pid}
        return units[pid].get(uid), metas[pid]

    def cite(uid):
        u, meta = unit(uid)
        return presentation.cite(meta, (u or {}).get("pages"), [uid], meta["paper_id"]) if u else "a removed evidence unit"

    rows = sorted(list_candidates(vault), key=lambda r: (r.get("gap_type") or "", str(r.get("gap_id"))))
    lines = [notes.INDEX_MARK, f"# {CATALOGUE_NOTE}", "", CATALOGUE_BANNER, ""]
    n_conflicts = sum(1 for r in rows if r.get("gap_type") == "unexplained_conflict")
    if n_conflicts:
        lines += [f"Xung đột chưa có lời giải thích ({n_conflicts}): xem [[Open conflicts]] hoặc tìm tag #conflict.", ""]
    with presentation.collect_source_ids() as source_ids:
        for gap_type in GAP_TYPES:
            group = [r for r in rows if r.get("gap_type") == gap_type]
            if not group or gap_type == "unexplained_conflict":
                continue
            lines += [f"## {CATALOGUE_TITLES.get(gap_type, gap_type)} ({len(group)})", ""]
            for r in group:
                text = _UNIT_ID.sub(lambda m: cite(m.group(0)), str(r.get("description") or ""))
                cites = ", ".join(dict.fromkeys(cite(uid) for uid in r.get("supporting_units") or []))
                lines.append(f"- {text} Evidence: {cites or 'none'}. Status: {r.get('status')}"
                             f" ({r.get('provenance') or 'generator'}).")
            lines.append("")
        if not rows:
            lines.append("*Chưa có giả thuyết mở nào.*")
        lines += presentation.source_ids_callout(source_ids)
    maps = config.vault_dir(vault) / notes.MAPS
    notes._write_index(maps / f"{CATALOGUE_NOTE}.md", "\n".join(lines) + "\n")
    old = maps / f"{OLD_CATALOGUE_NOTE}.md"  # renamed 13/10: retire the old machine-only note (kept, recoverable)
    if old.is_file() and vault_layout._machine_only(old.read_text(encoding="utf-8")):
        vault_layout.retire(vault, old, "renamed_maps", config.now()[:10])
