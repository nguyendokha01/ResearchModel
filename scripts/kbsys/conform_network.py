"""Conformance checks of the stages NW (the neural-network route) and SF (safety and privacy) of config/standard.yaml, see conform.py.

NW: a small synthetic library (invented sentences, vault notes with real [[links]]) is built in a throw-away project
(`conform_intake._sandbox` through `conform_answer._lab`) and `neural.prepare / search / check` run on it. A judge that
crashes is reported as a violation, never as a PASS.

NW-01, NW-02 and NW-09 are STORE checks: the synthetic probe runs always, and when the real project has the gold file
`runtime/audit_2026-10-16/D_gold.json` (a development file, not shipped) they also measure `neural.prepare` on the real
store for the 8 gold questions (fast with hops 1 and deep with hops 2, one run shared by the three clauses). When the gold
file is absent that half yields NOTHING (no data, not a pass): a release or a student install has no gold file. The real run
is read-only: the ticket write and the vault index refresh are switched off while it runs, and everything is restored on exit.
What the synthetic half can and cannot see: an 8-paper, 60-note library reproduces the size problem (items, hop notes, prompt) but
not the 0.7-1.1 MB of the real library or the real ranking; the real half is the authority for the budgets.

Targets that the product does not have yet (so the PASS case is RED until G4 builds them), named here once:
- NW-03: every item whose sentence belongs to a stored evidence unit carries `unit_type` (result, hypothesis, cited_statement,
  interpretation ...); today `find._sentence_hit` sets `unit_type: None` on every neural item.
- NW-05: an MCP tool `neural_path` (or `neural_walk`) with arguments `vault`, `source`, `target` (note titles) returning
  `found: bool` and `steps: [{source, target, sentence_ids}]`.
- NW-07: `neural.prepare` removes tickets (`cache/neural_sessions/<ticket>.json`) older than its retention; the probe plants
  one dated 1970 and expects it gone while the new ticket stays.
- NW-08 / NW-09: `neural.prepare` returns `needs_translation` (as find.search does, RT-04) and `not_found: True` when it
  has no evidence for an absence question (then it returns no items).

Only partly checked, and why:
- NW-04: "follows knowledge edges only" is checked through its visible effects (no expansion through an index note or a hub
  note, the second hop adds the note two links away); the product has no list of which edge types count as knowledge.
- NW-05: only the tool contract is probed (path through knowledge notes, at most max_steps, sentences on every link, not
  found otherwise); the real V3 chain is part of NW-09.
- NW-06: probes `neural.check` on a ticket (unknown note, altered note / sentence / snapshot, caller-supplied items, bad
  ticket); that an AI session does not treat note text as instructions is a behaviour.
- NW-07: the fingerprint functions are compared before and after a pure `touch`.

SF (not implemented, no honest automatic check):
- SF-03: "a snapshot or history copy exists for each data write" cannot be tied to a write: snapshots are taken by hand
  (tools/research_workspace.py, safe-rebuild) and a Mine/ manifest is not stored anywhere to compare with, so a check would
  either always pass or flag the researcher's own edits.
- SF-04: agents never delete sources, overwrite human notes or approve a gap: a rule about what other sessions do; the
  code-level guards are checked elsewhere (IN-03 sources unchanged, VN human blocks, SF-02 create-only saves).
SF-06 checks the library lock only (the Mine/ hash after a rebuild and "nothing edits code during a rebuild" are processes).
SF-07 checks the listeners, the API-key patterns and that every third-party import is declared in requirements.txt;
no list of APPROVED dependencies exists anywhere in the project (docs, config), so "approved" itself is not checked, and
"no paper is sent to an external AI without approval" is a behaviour of the sessions.
"""
import ast
import contextlib
import functools
import importlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from . import config, db, discussions, mcp_server, neural, notes, package, standard, workflow
from .conform import check
from .conform_answer import _add_paper, _lab, _raises, _unit
from .conform_intake import _sandbox, _seed

GOLD = Path("runtime") / "audit_2026-10-16" / "D_gold.json"  # the G0 gold questions, a development file
SPEEDS = (("fast", 1), ("deep", 2))  # speed and the hops answer_question uses with it
UNIT_TYPES = ("result", "hypothesis", "cited_statement", "interpretation")  # NW-03, as the clause lists them
PATH_TOOLS = ("neural_path", "neural_walk")  # NW-05
_GOLD_RUNS = {}  # (gold file, judge) -> rows of the real run, shared by NW-01, NW-02 and NW-09


def _bench():
    """benchmark_research (scripts/, on sys.path under kb.py and the tests): its own scoring functions are the judge of NW-01/02/09.
    A development file that package.RELEASE_NEVER does not ship: without it (a release) the checks that need it yield nothing and say so
    here, they are not a PASS of the benchmark; None then."""
    try:
        return importlib.import_module("benchmark_research")  # not a static import: SF-07 would ask requirements.txt for it
    except ImportError:
        return None


def _ids(items):
    return [i.get("id") for i in items]


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def _note(name, body, vault="one", kind="concept", **front):
    """A vault note with front matter; `[[links]]` in the body are what the neural index follows."""
    _write(config.vault_dir(vault) / name, "---\n" + yaml.safe_dump({"kb_kind": kind, **front}) + "---\n" + body + "\n")


def _refused(call, *args, **kwargs):
    """('refused', message) when the call raised ValueError (the product's refusal), ('accepted', result) when it returned,
    ('crashed', text) for any other error: the caller reports a crash, it is never read as a refusal or a pass."""
    try:
        return "accepted", call(*args, **kwargs)
    except ValueError as err:
        return "refused", str(err)[:80]
    except Exception as err:  # noqa: BLE001 - becomes a 'crashed' violation in the caller
        return "crashed", f"{type(err).__name__}: {str(err)[:80]}"


# ---------------------------------------------------------------- the synthetic libraries

def _budget_library(con):
    """8 papers x 18 sentences (units over some), a paper note each and 60 concept notes (the node cap plus ten) that cite them all: enough matching
    sentences and linked notes that a prepare without budgets goes far past the payload and token budgets."""
    papers = 8
    for p in range(papers):
        pid = f"p{p:02d}"
        rows = [("results", 1 + s, f"Alphaterm level number {s} in sample {p} was measured with standard plates and medium after washing twice in buffer.") for s in range(12)]
        rows += [("methods", 2, f"Cells in sample {p} were stored in plates.") for _ in range(6)]
        _add_paper(con, pid, rows, units=[_unit(pid, 1, [1, 2, 3, 4], rows, "result"), _unit(pid, 2, [5, 6], rows, "hypothesis")])
        _note(f"{pid}.md", f"# {pid}\nAlphaterm {pid}:S0001 {pid}:S0002 [[Concept0]]", kind="paper", paper_id=pid)
    cited = " ".join(f"p{p:02d}:S{s:04d}" for p in range(papers) for s in range(1, 13))
    notes = neural.MAX_GRAPH_NODES + 10  # more notes than the node cap holds
    for c in range(notes):
        _note(f"Concept{c}.md", f"# Concept{c}\nAlphaterm concept " + " ".join(f"[[Concept{(c + k) % notes}]]" for k in range(1, 5)) + " " + cited, entity_kind="molecule")


UNIT_QUERY, UNIT_MUST = "alphaterm betaterm", "u3:S0006"


def _unit_library(con):
    """6 papers with one 9-sentence result unit each; only u3's 6th sentence has both query words (the others have 'alphaterm'
    alone), so the unit order puts the matching sentence at rank 6 while relevance would put it first."""
    for p in range(6):
        pid = f"u{p}"
        rows = [("results", 1, f"Plates number {s} were washed in buffer.") for s in range(1, 6)]
        rows.append(("results", 2, "Alphaterm and betaterm were higher in group A." if p == 3 else "Alphaterm alone was measured here."))
        rows += [("results", 2, f"Medium number {s} was changed.") for s in range(7, 10)]
        _add_paper(con, pid, rows, units=[_unit(pid, 1, list(range(1, 10)), rows, "result")])
        _note(f"{pid}.md", f"# {pid}\nAlphaterm {pid}:S0006 [[Concept]]", kind="paper", paper_id=pid)
    _note("Concept.md", "# Concept\nAlphaterm betaterm " + " ".join(f"u{p}:S0006" for p in range(6)), entity_kind="molecule")


def _label_library(con):
    """One paper per unit type, a single 'alphaterm' sentence in a unit of that type: {sentence id: unit type}."""
    expected = {}
    for i, unit_type in enumerate(UNIT_TYPES):
        pid = f"t{i}"
        rows = [("results", 1, "Alphaterm was higher in group A.")]
        _add_paper(con, pid, rows, units=[_unit(pid, 1, [1], rows, unit_type)])
        expected[f"{pid}:S0001"] = unit_type
    return expected


# ---------------------------------------------------------------- NW-01 .. NW-03: payload, ranking, labels

def _duplicates(result):
    """The fields the clause names as repeated: `items` again in source_resolution, `media` again as `figures`."""
    def field(path):
        value = result
        for key in path:
            value = value.get(key) if isinstance(value, dict) else None
        return value
    return [f"{'.'.join(a)} is repeated as {'.'.join(b)}" for a, b in ((("items",), ("source_resolution", "items")), (("media",), ("figures",)))
            if field(a) and field(a) == field(b)]


def _size(result):
    """(payload bytes of the whole JSON the tool returns, estimated prompt tokens): the enforcer's own `neural.est_tokens`."""
    return len(json.dumps(result, ensure_ascii=False, default=str).encode()), neural.est_tokens(len(str(result.get("prompt") or "").encode()))


def _gold_rows(store, judge):
    """Rows of `neural.prepare` over the real gold questions (None when the project has no gold file): per speed and question
    the benchmark's gold metrics, bytes, tokens, `not_found`, duplicated fields and the V3 chain links found."""
    path = Path(store.root).parent / GOLD
    if not path.is_file():
        return None
    key = (str(path), judge)
    if key in _GOLD_RUNS:
        return _GOLD_RUNS[key]
    bench = _bench()
    gold = json.loads(path.read_text(encoding="utf-8"))
    cfg, rows = config.load(), []
    # read-only: no ticket file, no index refresh (the web app does that); restored when the block ends
    with mock.patch.object(workflow, "atomic_json", lambda *a, **k: None), \
            mock.patch.object(neural, "refresh", lambda vault, force=True: neural._snapshot_result(vault)):
        for speed, hops in SPEEDS:
            for qid, q in gold.items():
                row = {"id": qid, "speed": speed, "type": q.get("type"), "error": None}
                result, error = _raises(judge, store.con, cfg, q["vault"], q["question"], hops=hops, speed=speed)
                if error:
                    rows.append({**row, "error": error})
                    continue
                size = _size(result)
                got = {sid for item in result["items"] for sid in (item.get("sentence_ids") or [item.get("id")])}
                links = [c for c in q.get("chain") or [] if c.get("sentences")]
                rows.append({**row, "payload_bytes": size[0], "prompt_tokens_est": size[1], "not_found": bool(result.get("not_found")),
                             "duplicates": _duplicates(result), "chain_total": len(links),
                             "chain_found": sum(1 for c in links if set(c["sentences"]) & got),
                             **bench.gold_metrics(result["items"], q.get("must", []), q.get("distractors", []))})
    _GOLD_RUNS[key] = rows
    return rows


def _gold_problems(rows, topics):
    """What the rows of a gold run (the real run, or run_gold's rows with a 'speed' added) say wrong about `topics`:
    budget (NW-01), rank (NW-02), absence and chain (NW-09)."""
    bench = _bench()
    problems = [f"gold {r['id']} {r['speed']}: prepare crashed: {r['error']}" for r in rows if r.get("error")]
    ok = [r for r in rows if not r.get("error")]
    if "budget" in topics:
        max_bytes = standard.param("NW-01", "max_payload_kb") * 1024
        tokens = {"fast": standard.param("NW-01", "token_budget_fast"), "deep": standard.param("NW-01", "token_budget_deep")}
        for r in ok:
            if r["payload_bytes"] > max_bytes:
                problems.append(f"gold {r['id']} {r['speed']}: payload {r['payload_bytes'] // 1024} KB, budget {max_bytes // 1024} KB")
            if r["prompt_tokens_est"] > tokens[r["speed"]]:
                problems.append(f"gold {r['id']} {r['speed']}: prompt about {r['prompt_tokens_est']} tokens, budget {tokens[r['speed']]}")
    fast = [r for r in ok if r["speed"] == "fast"]
    if "rank" in topics and fast:
        summary = bench.summarize_gold(fast)
        target, minimum = standard.param("NW-02", "max_first_must_rank"), standard.param("NW-02", "min_idea_coverage")
        if summary["first_must_rank_median"] is None:
            problems.append("gold: no question has a must sentence in its items, so the first must rank cannot be measured")
        elif summary["first_must_rank_median"] > target:
            problems.append(f"gold: first must-sentence at median rank {summary['first_must_rank_median']}, target {target}")
        if summary["idea_coverage_mean"] < minimum:
            problems.append(f"gold: idea coverage {summary['idea_coverage_mean']}, minimum {minimum}")
        shown = next((r["duplicates"] for r in fast if r.get("duplicates")), [])
        problems += [f"gold: {d}" for d in shown]
    if "absence" in topics:
        problems += [f"gold {r['id']}: an absence question returns {r['distractors_returned']} confident item(s) instead of not found"
                     for r in fast if r["type"] == "e_absence" and r["distractors_returned"] and not r.get("not_found")]
    if "chain" in topics:
        needed = standard.param("NW-05", "min_chain_links_of_5")
        problems += [f"gold {r['id']}: {r['chain_found']} of {r['chain_total']} chain links have their sentences among the items, need {needed}"
                     for r in fast if r["id"] == "V3" and r["chain_found"] < needed]
    return problems


@check("NW-01", "store")
def payload_budget(store, judge=neural.prepare):
    if _bench() is None:
        return
    max_bytes = standard.param("NW-01", "max_payload_kb") * 1024
    tokens = {"fast": standard.param("NW-01", "token_budget_fast"), "deep": standard.param("NW-01", "token_budget_deep")}
    with _lab() as (root, con, cfg):
        _budget_library(con)
        for speed, hops in SPEEDS:
            result, error = _raises(judge, con, cfg, "one", "alphaterm level", hops=hops, speed=speed)
            if error:
                yield f"prepare ({speed}) crashed: {error}"
                continue
            payload, estimate = _size(result)
            if not result.get("items"):
                yield f"prepare ({speed}) found no items in the probe library, so the budgets cannot be tested"
            if payload > max_bytes:
                yield f"{speed} (hops {hops}): payload {payload // 1024} KB, budget {max_bytes // 1024} KB"
            if estimate > tokens[speed]:
                yield f"{speed} (hops {hops}): prompt about {estimate} tokens, budget {tokens[speed]}"
    rows = _gold_rows(store, judge)
    if rows is not None:
        yield from _gold_problems(rows, ("budget",))


@check("NW-02", "store")
def ranking_and_duplicates(store, judge=neural.prepare):
    if _bench() is None:
        return
    target, minimum = standard.param("NW-02", "max_first_must_rank"), standard.param("NW-02", "min_idea_coverage")
    with _lab() as (root, con, cfg):
        _unit_library(con)
        result, error = _raises(judge, con, cfg, "one", UNIT_QUERY, hops=1)
        if error:
            yield f"prepare crashed: {error}"
        else:
            metrics = _bench().gold_metrics(result["items"], [UNIT_MUST])
            if metrics["first_must_rank"] is None:
                yield f"the sentence that matches the whole question ({UNIT_MUST}) is not among the {metrics['items']} items"
            elif metrics["first_must_rank"] > target:
                yield (f"the sentence that matches the whole question is at rank {metrics['first_must_rank']} of {metrics['items']}, "
                       f"target {target} (the items follow the unit order, not relevance)")
            if metrics["idea_coverage"] < minimum:
                yield f"idea coverage {metrics['idea_coverage']}, minimum {minimum}"
            yield from _duplicates(result)
    rows = _gold_rows(store, judge)
    if rows is not None:
        yield from _gold_problems(rows, ("rank",))


@check("NW-03", "probe")
def probe_unit_labels(judge=neural.prepare):
    with _lab() as (root, con, cfg):
        expected = _label_library(con)
        result, error = _raises(judge, con, cfg, "one", "alphaterm", hops=0)
        if error:
            return [f"prepare crashed: {error}"]
        by_id = {item["id"]: item for item in result["items"]}
        bad = []
        for sid, unit_type in expected.items():
            if sid not in by_id:
                bad.append(f"the {unit_type} sentence {sid} is not among the items {sorted(by_id)}")
            elif by_id[sid].get("unit_type") != unit_type:
                bad.append(f"{sid} is in a unit of type {unit_type} but the item shows unit_type {by_id[sid].get('unit_type')!r} "
                           f"(label {by_id[sid].get('label')!r}): a tested result cannot be told from a proposal")
        return bad


# ---------------------------------------------------------------- NW-04: hops

@check("NW-04", "probe")
def probe_hops(judge=neural.search):
    fillers = neural.MAX_GRAPH_NODES + 10  # more links than the node cap holds
    with _lab() as (root, con, cfg):
        # Only a link in the one kb:auto block of a result / claim note to a node note is a knowledge edge; a plain body link navigates (RL-01).
        auto = lambda *names: f"{notes.AUTO_START}\n" + " ".join(f"[[{n}]]" for n in names) + f"\n{notes.AUTO_END}"
        _note("Seed.md", f"Seedterm result.\n{auto('Bridge', 'AHub', 'Overview')}\nPlain body link [[NavOnly]].", kind="result", type="result")
        _note("Concepts/AHub.md", "Hub node.", kind="concept")
        for i in range(fillers):  # AHub is linked by more notes than the node cap holds ...
            _note(f"Filler{i:03d}.md", f"Filler claim.\n{auto('AHub')}", kind="claim", type="claim")
        for i in range(4):  # ... and by 3 of 4 paper notes (more than hub_share_of_papers), so it is a hub
            _note(f"Papers/Paper{i}.md", "Paper note. " + ("[[AHub]]" if i < 3 else ""), kind="paper")
        _note("Concepts/Bridge.md", "Bridge node.", kind="concept")
        _note("Far.md", f"Far claim.\n{auto('Bridge')}", kind="claim", type="claim")
        _note("Concepts/NavOnly.md", "Linked from the seed by a plain body link only.", kind="concept")
        # an index note in a node folder: knowledge edges reach it (Seed -> Overview <- Leak), only the index skip keeps Leak out
        _note("Concepts/Overview.md", "Index note.", tags=["kb-index"])
        _note("Leak.md", f"Leak claim.\n{auto('Overview')}", kind="claim", type="claim")
        found = {}
        for hops in (0, 1, 2):
            result, error = _raises(judge, "one", "seedterm", hops=hops)
            if error:
                return [f"search with hops={hops} crashed: {error}"]
            found[hops] = {n["path"] for n in result["nodes"]}
    bad = []
    if "Seed.md" not in found[0]:
        return [f"the probe query did not find the seed note (nodes {sorted(found[0])[:3]})"]
    if "Far.md" not in found[2]:
        bad.append(f"hops=2 does not reach Far.md, two knowledge links from the seed ({len(found[2])} notes returned, node cap {neural.MAX_GRAPH_NODES})")
    if len(found[2]) <= len(found[1]):
        bad.append(f"hops=2 returns no more notes than hops=1 ({len(found[2])})")
    flood = [p for p in found[2] if p.startswith("Filler")]
    if flood:
        bad.append(f"hops=2 expands through the hub note AHub.md: {len(flood)} of its {fillers} linked notes are added")
    for hops, paths in found.items():
        if "Leak.md" in paths:
            bad.append(f"hops={hops} reaches Leak.md through the index note Overview.md")
        if "Concepts/NavOnly.md" in paths:
            bad.append(f"hops={hops} reaches NavOnly.md through a navigation (plain body) link: expansion follows knowledge edges only")
        if hops and "Concepts/AHub.md" in paths:
            bad.append(f"hops={hops} adds the hub note AHub.md (linked from most paper notes) as a node")
    return bad


# ---------------------------------------------------------------- NW-05: path between two entities

def _path_library(con):
    """Knowledge chain Aterm -> Bterm -> Dterm -> Cterm (a sentence on every link), a shorter route Aterm - paper note - Cterm,
    Zterm linked to the paper note only, a chain longer than max_steps, and Xterm -> Hubterm -> Yterm where Hubterm is linked from
    3 of the 4 paper notes (a hub, RL-09).  A link is a result note whose kb:auto block links both nodes and whose id and sentence_ids
    cite the stored sentence: a plain body link between nodes only navigates (RL-01)."""
    rows = [("results", 1, f"Link number {i} was observed in group A.") for i in range(1, 11)]
    _add_paper(con, "pa", rows)
    node = lambda name, text="node.": _note(f"Substances/{name}.md", f"{name} {text} Reported in [[Paper pa]].", entity_kind="molecule")
    link = lambda src, dst, n: _note(f"Results/{src}-{dst}.md", f"{src} raises {dst} pa:S{n:04d}.\n{notes.AUTO_START}\n[[{src}]] [[{dst}]]\n{notes.AUTO_END}",
                                     kind="result", type="result", id=f"pa:E{n:04d}", sentence_ids=[f"pa:S{n:04d}"])
    for name in ("Aterm", "Bterm", "Dterm", "Cterm", "Zterm", "Xterm", "Yterm", "Hubterm"):
        node(name)
    link("Aterm", "Bterm", 1)
    link("Bterm", "Dterm", 2)
    link("Dterm", "Cterm", 3)
    link("Xterm", "Hubterm", 8)
    link("Hubterm", "Yterm", 9)
    _note("Paper pa.md", "Paper note: [[Aterm]] [[Cterm]] [[Zterm]] [[Hubterm]]", kind="paper", paper_id="pa")
    for pid in ("pb", "pc", "pd"):
        _note(f"Paper {pid}.md", "Paper note: [[Hubterm]]" if pid != "pd" else "Paper note.", kind="paper", paper_id=pid)
    longest = standard.param("NW-05", "max_steps") + 1  # steps of the chain L0 .. L<longest>
    for i in range(longest + 1):
        node(f"L{i}")
    for i in range(longest):
        link(f"L{i}", f"L{i + 1}", i + 4)
    return longest


@check("NW-05", "probe")
def probe_path(call=mcp_server.call_tool, tools=None):
    names = [t["name"] for t in mcp_server.TOOLS] if tools is None else list(tools)
    tool = next((n for n in PATH_TOOLS if n in names), None)
    if tool is None:
        return [f"there is no {' or '.join(PATH_TOOLS)} tool, so no reasoning path between two entities can be asked for"]
    most = standard.param("NW-05", "max_steps")
    bad = []
    with _lab() as (root, con, cfg):
        longest = _path_library(con)

        def ask(source, target):
            result, error = _raises(call, con, cfg, tool, {"vault": "one", "source": source, "target": target})
            if error:
                bad.append(f"{tool} {source} -> {target} crashed: {error}")
            elif not isinstance(result, dict) or not isinstance(result.get("found"), bool) or not isinstance(result.get("steps", []), list):
                bad.append(f"{tool} {source} -> {target} did not return found (bool) and steps (list)")
            else:
                return result
            return None

        norm = lambda name: str(name or "").removesuffix(".md").casefold()
        result = ask("Aterm", "Cterm")
        if result is not None:
            steps = result.get("steps", [])
            if not result["found"] or not steps:
                bad.append("Aterm to Cterm: not found, although a knowledge chain of three links exists")
            else:
                if len(steps) > most:
                    bad.append(f"Aterm to Cterm: {len(steps)} steps, at most {most}")
                known = {r[0] for r in con.execute("SELECT sentence_id FROM sentences")}
                for n, step in enumerate(steps, 1):
                    sids = step.get("sentence_ids") or []
                    if not sids or not set(sids) <= known:
                        bad.append(f"Aterm to Cterm: step {n} ({step.get('source')} -> {step.get('target')}) has no stored source sentence ({sids})")
                    if "paper" in norm(step.get("source")) or "paper" in norm(step.get("target")):
                        bad.append(f"Aterm to Cterm: step {n} runs through a paper note ({step.get('source')} -> {step.get('target')})")
                    if not step.get("etypes") or not step.get("unit_id"):
                        bad.append(f"Aterm to Cterm: step {n} does not carry its edge types and unit id ({step.get('etypes')}, {step.get('unit_id')})")
                if norm(steps[0].get("source")) != "aterm" or norm(steps[-1].get("target")) != "cterm" \
                        or any(norm(a.get("target")) != norm(b.get("source")) for a, b in zip(steps, steps[1:])):
                    bad.append("Aterm to Cterm: the steps are not one connected path from Aterm to Cterm")
        result = ask("Xterm", "Yterm")  # the only route passes through a hub: a named pair is an explicit request, not expansion
        if result is not None and not (result["found"] and len(result.get("steps", [])) == 2):
            bad.append("Xterm to Yterm: not found, although a knowledge route of two steps exists through the hub note Hubterm")
        for label, source, target in (("only a paper note connects them", "Aterm", "Zterm"),
                                      (f"{longest} steps are needed, at most {most} allowed", "L0", f"L{longest}")):
            result = ask(source, target)
            if result is not None and result["found"]:
                bad.append(f"{source} to {target} ({label}): a path is returned instead of not found")
        for target, label in (("Aterm", "a path from a node to itself"), ("Results/Aterm-Bterm.md", "a Results note as endpoint")):
            error = _raises(call, con, cfg, tool, {"vault": "one", "source": "Aterm", "target": target})[1]
            if not str(error).startswith("ValueError"):
                bad.append(f"Aterm to {target}: {label} is not refused ({error or 'answered'})")
    return bad


# ---------------------------------------------------------------- NW-06 .. NW-08: tickets, translation

@contextlib.contextmanager
def _ticket_lab():
    """A throw-away project with paper pa, a concept note citing its first sentence, and `ask()` -> a fresh prepared ticket."""
    with _lab() as (root, con, cfg):
        rows = [("results", 1, "Alphaterm was higher in group A."), ("methods", 1, "Cells were washed twice."), ("methods", 2, "Plates were stored.")]
        _add_paper(con, "pa", rows)
        _note("Concept.md", "# Concept\nAlphaterm concept pa:S0001", entity_kind="molecule")
        yield con, cfg, lambda: neural.prepare(con, cfg, "one", "alphaterm", hops=1)


GOOD_REPLY = {"answer": [{"text": "Alphaterm was higher in group A. [#1]", "evidence": [1]}], "reasoning": [], "missing": []}


@check("NW-06", "probe")
def probe_untrusted_notes(judge=neural.check):
    bad = []
    with _ticket_lab() as (con, cfg, prepare):

        def refuses(label, mutate, reply=GOOD_REPLY):
            prepared = prepare()
            mutate(prepared)
            status, text = _refused(judge, con, prepared["ticket"], reply)
            if status == "accepted":
                bad.append(f"{label}: the check accepts it")
            elif status == "crashed":
                bad.append(f"{label}: the check crashed instead of refusing: {text}")

        status, result = _refused(judge, con, prepare()["ticket"], GOOD_REPLY)
        if status != "accepted":
            return [f"a reply that cites the snapshot's item 1 is {status}: {result}"]
        if not result.get("evidence") or not {e["id"] for e in result["evidence"]} <= {"pa:S0001"}:
            bad.append(f"the checked evidence is not the snapshot's item: {[e.get('id') for e in result.get('evidence', [])]}")
        ticket_file = lambda p: config.CACHE_DIR / "neural_sessions" / (p["ticket"] + ".json")
        refuses("a reply citing a note that is not in the snapshot", lambda p: None,
                {**GOOD_REPLY, "reasoning": [{"text": "Synthesis of the notes.", "basis": ["Ghost.md"]}]})
        refuses("a note altered after the ticket was made", lambda p: _write(config.vault_dir("one") / "Concept.md", "# Concept\nAlphaterm changed pa:S0001\n"))
        refuses("a stored sentence altered after the ticket was made",
                lambda p: con.execute("UPDATE sentences SET text = 'Alphaterm was lower in group A.' WHERE sentence_id = 'pa:S0001'") and con.commit())
        refuses("a snapshot file whose item text was altered", lambda p: _tamper_ticket(ticket_file(p)))
        for label, ticket in (("a ticket that is not 32 hex digits", "../x"), ("an unknown ticket", "0" * 32)):
            status, text = _refused(judge, con, ticket, GOOD_REPLY)
            if status != "refused":
                bad.append(f"{label} is {status}")
    with _ticket_lab() as (con, cfg, prepare):  # a caller-supplied item list must not replace the snapshot
        fake = {"id": "fake:S0001", "kind": "sentence", "paper_id": "pa", "text": "Fakeword proves everything.", "sentence_ids": ["fake:S0001"], "pages": [1], "sections": ["results"]}
        reply = {"answer": [{"text": "Fakeword proves everything. [#2]", "evidence": [2]}], "reasoning": [], "missing": [], "items": [fake], "papers": {}}
        status, result = _refused(judge, con, prepare()["ticket"], reply)
        if status == "accepted" and ("Fakeword" in json.dumps(result, ensure_ascii=False) and any(r.get("check") == "ok" for r in result.get("answer", []))):
            bad.append("an item list supplied by the caller replaces the snapshot: its sentence is checked as evidence")
        elif status == "crashed":
            bad.append(f"a reply with its own item list crashed the check: {result}")
    return bad


def _tamper_ticket(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    data["items"][0]["text"] = "Tampered alphaterm sentence."
    path.write_text(json.dumps(data), encoding="utf-8", newline="\n")


@check("NW-07", "probe")
def probe_ticket_fingerprint(profile=neural._profile_fingerprint, dependency=neural.dependency_fingerprint, prepare=neural.prepare):
    bad = []
    with _ticket_lab() as (con, cfg, ask):
        folder = db.paper_dir("pa")
        relations = config.STORE / "relations.jsonl"
        _write(relations, "{}\n")
        node = {"path": "Concept.md", "sha256": "x", "paper_id": "pa"}
        paths = [p for p in folder.iterdir() if p.is_file()] + [relations, Path(config.DB_PATH)]
        before = (_raises(profile, "one", node, None), _raises(dependency, "pa"))
        for result, error in before:
            if error:
                return [f"a fingerprint function crashed: {error}"]
        for p in paths:  # touch: the same bytes, a later modification time
            stat = p.stat()
            os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        after = (_raises(profile, "one", node, None), _raises(dependency, "pa"))
        for name, (b, a) in zip(("node profile", "source/profile/media"), zip(before, after)):
            if a[1]:
                bad.append(f"the {name} fingerprint crashed after a touch: {a[1]}")
            elif a[0] != b[0]:
                bad.append(f"touching a file without changing it changes the {name} fingerprint (it hashes modification times, not content)")
        with open(folder / "sentences.jsonl", "ab") as stream:  # a real change must still invalidate
            stream.write(b"\n")
        for name, old, new in (("node profile", before[0][0], _raises(profile, "one", node, None)[0]),
                               ("source/profile/media", before[1][0], _raises(dependency, "pa")[0])):
            if old == new:
                bad.append(f"changing the content of a paper file does not change the {name} fingerprint")
        # old tickets are cleaned: one dated 1970 must be gone after a new prepare, the new one stays
        sessions = config.CACHE_DIR / "neural_sessions"
        old_ticket = sessions / ("f" * 32 + ".json")
        _write(old_ticket, "{}\n")
        os.utime(old_ticket, (0, 0))
        result, error = _raises(prepare, con, cfg, "one", "alphaterm", hops=1)
        if error:
            bad.append(f"prepare crashed: {error}")
        else:
            if old_ticket.exists():
                bad.append("a ticket last used in 1970 is still in cache/neural_sessions after a new prepare")
            if not (sessions / (result["ticket"] + ".json")).is_file():
                bad.append("the ticket of the new prepare is missing")
        # NW-11: an open research session dated 1970 survives a new prepare; a closed one is gone and refused
        opened, error = _raises(prepare, con, cfg, "one", "alphaterm", hops=1, session="new")
        session_file = sessions / f"session-{(opened or {}).get('session')}.json"
        if error or not session_file.is_file():
            bad.append(f"prepare(session='new') did not open a session file: {error or opened}")
        else:
            os.utime(session_file, (0, 0))
            _, error = _raises(prepare, con, cfg, "one", "alphaterm", hops=1)
            if error:
                bad.append(f"prepare crashed: {error}")
            elif not session_file.is_file():
                bad.append("an open research session last used in 1970 was cleaned by a new prepare")
            else:
                neural.close_session(opened["session"])
                if session_file.exists():
                    bad.append("a closed research session is still in cache/neural_sessions")
                if _refused(neural.check, con, opened["session"], GOOD_REPLY)[0] != "refused":
                    bad.append("a closed research session is still accepted by neural_check")
    return bad


@check("NW-08", "probe")
def probe_translation(judge=neural.prepare):
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Cells treated with alphaterm survived.")])
        _add_paper(con, "pb", [("results", 1, "Plates were stored in buffer.")])
        for label, question in (("NFC", "tế bào alphaterm"), ("NFD", unicodedata.normalize("NFD", "tế bào alphaterm"))):
            result, error = _raises(judge, con, cfg, "one", question, hops=0)
            if error:
                bad.append(f"Vietnamese question ({label}) crashed prepare: {error}")
                continue
            expanded = str((result.get("vault_context") or {}).get("expanded_query") or "")
            if not result.get("needs_translation"):
                bad.append(f"Vietnamese question ({label}) is searched without a needs_translation flag (searched as {expanded!r})")
            if not expanded.isascii():
                bad.append(f"Vietnamese question ({label}) is searched literally, not with the English expansion ({expanded!r})")
            if "pa:S0001" not in _ids(result["items"]):
                bad.append(f"Vietnamese question ({label}): the English sentence pa:S0001 is not among the items {_ids(result['items'])}")
        result, error = _raises(judge, con, cfg, "one", "cells alphaterm", hops=0)
        if not error and result.get("needs_translation"):
            bad.append("an English question is flagged needs_translation")
    return bad


# ---------------------------------------------------------------- NW-09: the gold benchmark

@check("NW-09", "store")
def gold_benchmark(store, judge=neural.prepare):
    bench = _bench()
    if bench is None:
        return
    scored = bench.gold_metrics([{"id": "x:S1", "sentence_ids": ["x:S1"]}, {"id": "E1", "sentence_ids": ["x:S2", "x:S3"]}],
                                ["x:S3", "x:S9"], ["x:S1"])  # the benchmark's own scoring on a known case
    if (scored["idea_coverage"], scored["first_must_rank"], scored["distractors_returned"]) != (0.5, 2, 1):
        yield f"benchmark_research.gold_metrics scores a known case wrong: {scored}"
    with _lab() as (root, con, cfg):
        _unit_library(con)
        gold = {"Q1": {"vault": "one", "type": "a_single_paper_factual", "question": UNIT_QUERY, "queries": [UNIT_QUERY], "must": [UNIT_MUST]},
                "Q2": {"vault": "one", "type": "e_absence", "question": "alphaterm zetaterm", "queries": ["alphaterm zetaterm"],
                       "must": [], "distractors": [f"u{p}:S0006" for p in range(6)]}}
        with contextlib.redirect_stdout(io.StringIO()):  # run_gold prints one summary line per route
            run = bench.run_gold({"neural": SimpleNamespace(prepare=judge)}, con, cfg, gold, ["neural_h1"])
    rows = [{**r, "speed": "fast"} for r in run["neural_h1"]["questions"]]
    yield from _gold_problems(rows, ("budget", "rank", "absence"))
    real = _gold_rows(store, judge)
    if real is not None:
        yield from _gold_problems(real, ("budget", "rank", "absence", "chain"))


# ---------------------------------------------------------------- SF-01, SF-02, SF-05, SF-06

@check("SF-01", "probe")
def probe_check_args(judge=mcp_server._check_args):
    bad = []
    with _sandbox() as (root, con):
        _seed(con, "pa", ["one"])
        (config.VAULTS_DIR / "_Maps").mkdir()
        outside = config.STORE / "escape"
        outside.mkdir(parents=True)

        def verdict(args, tool="read_paper"):
            return _refused(judge, con, tool, args)

        for label, paper_id in (("a parent path", "../escape"), ("..", ".."), ("an absolute path", str(outside)), ("an unknown id", "no_such_paper_2026"),
                                ("an empty id", ""), ("a number", 7)):
            status, text = verdict({"paper_id": paper_id})
            if status != "refused":
                bad.append(f"paper_id {label} is {status}{': ' + text if status == 'crashed' else ''}")
        for label, vault in (("an unknown vault", "nonexistent_vault"), ("a path", "../x"), ("a list", ["one"]), ("a number", 5), ("a mother-vault folder", "_Maps")):
            status, text = verdict({"vault": vault}, "search_sentences")
            if status != "refused":
                bad.append(f"vault {label} is {status}{': ' + text if status == 'crashed' else ''}")
        for args in ({"paper_id": "pa"}, {"vault": "one"}):
            status, text = verdict(dict(args))
            if status != "accepted":
                bad.append(f"{args} is {status}: {text}")
        huge = 10 ** 9  # a fixture value far above any cap
        for tool in ("search_sentences", "read_paper"):
            for limit in (-1, 0, huge):
                args = {"limit": limit}
                status, text = verdict(args, tool)
                if status != "accepted":
                    bad.append(f"{tool}: limit {limit} is {status}: {text}")
                elif not isinstance(args.get("limit"), int) or args["limit"] < 1:
                    bad.append(f"{tool}: limit {limit} becomes {args.get('limit')!r}, not a positive number (SQLite reads a negative LIMIT as none)")
                elif limit == huge:
                    cap = standard.param("SF-01", "max_limit_read_paper" if tool == "read_paper" else "max_limit")
                    if args["limit"] > cap:
                        bad.append(f"{tool}: limit {huge} becomes {args['limit']}, above the cap {cap}")
            for wrong in ("abc", True, 2.5, [3]):
                status, text = verdict({"limit": wrong}, tool)
                if status != "refused":
                    bad.append(f"{tool}: limit {wrong!r} is {status}")
    source = Path(mcp_server.__file__).read_text(encoding="utf-8")
    handle = source[source.index("def handle("):]
    if "_check_args(" not in handle or handle.index("_check_args(") > handle.index("call_tool(con, cfg"):
        bad.append("mcp_server.handle does not call _check_args before call_tool")
    return bad


@check("SF-02", "probe")
def probe_discussion_saves(judge=discussions.save):
    bad, workers = [], 8  # the clause names eight concurrent saves
    with _sandbox() as (root, con):
        folder = config.vault_dir("one") / discussions.FOLDER
        _write(folder / "Human title.md", "HUMAN TEXT\n")
        status, text = _refused(judge, "one", "Human title", "New body.")
        if status != "accepted":
            return [f"a plain save is {status}: {text}"]
        if (folder / "Human title.md").read_text(encoding="utf-8") != "HUMAN TEXT\n":
            bad.append("a save over an existing note's title overwrote that note")
        outcomes = []

        def save(i):
            outcomes.append(_refused(judge, "one", "Same title", f"Body {i}."))

        threads = [threading.Thread(target=save, args=(i,)) for i in range(workers)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        failed = [text for status, text in outcomes if status != "accepted"]
        files = sorted(p for p in folder.glob("Same title*.md"))
        if failed:
            bad.append(f"{len(failed)} of {workers} concurrent saves failed: {failed[0]}")
        elif len(files) != workers:
            bad.append(f"{workers} concurrent saves of one title made {len(files)} file(s)")
        else:
            bodies = [f"Body {i}." for i in range(workers)]
            texts = [p.read_text(encoding="utf-8") for p in files]
            if any(sum(b in t for t in texts) != 1 for b in bodies):
                bad.append("two concurrent saves share a file: a body is missing or in two files")
        status, text = _refused(judge, "one", "Line one\n---\ntype: evidence\n---\nLine two", "Body.")
        if status != "accepted":
            bad.append(f"a title with line breaks is {status}: {text}")
        else:
            lines = (config.vault_dir("one") / text["saved"]).read_text(encoding="utf-8").splitlines()
            heading = next((i for i, line in enumerate(lines[1:], 1) if line.startswith("# ")), None)
            if heading is None or sum(1 for line in lines if line.strip() == "---") != 2:
                bad.append("a title with line breaks starts a second front matter or a second heading in the note")
        for name in ("_Maps", "Registry"):
            (config.VAULTS_DIR / name).mkdir(exist_ok=True)
        before = sorted(p for p in config.VAULTS_DIR.rglob("*") if p.is_file())
        for label, vault in (("_Maps", "_Maps"), ("Registry", "Registry"), ("a path", "../x"), ("an unknown vault", "nope"), ("none", None)):
            status, text = _refused(judge, vault, "Elsewhere", "Body.")
            if status != "refused":
                bad.append(f"a save into {label} is {status}")
        if sorted(p for p in config.VAULTS_DIR.rglob("*") if p.is_file()) != before:
            bad.append("a refused save left a file behind")
    return bad


def _private_shipped(shipped, secret_text, dest):
    """Names in a release that must never be there, and files that carry the secret value."""
    private = re.compile(r"(^|/)config/secrets\.ya?ml$|^docs/(PLAN|STATUS|REQUIREMENTS)\.md$|^docs/AUDIT_|^CLAUDE\.md$|^\.claude/(hooks|settings)"
                         r"|^(knowledge_store|vaults|sources|review|projects)/.", re.I)
    found = [f"{name} is shipped" for name in sorted(shipped) if private.search(name)]
    if secret_text:
        found += [f"{name} contains the API key" for name in sorted(shipped) if (dest / name).is_file() and secret_text.encode() in (dest / name).read_bytes()]
    return found


@check("SF-05", "store")
def release_leaks(store, judge=package.release):
    bad, secret = [], "sk-probe-0123456789abcdef0123456789abcdef"
    with _sandbox() as (root, con), tempfile.TemporaryDirectory(prefix="conform-release-") as temp:
        dest = Path(temp).resolve() / "release"
        library_id = "smith2020_probe_paper_abcd"
        _write(root / "config" / "secrets.yaml", f"deepseek_api_key: {secret}\n")
        _write(root / "config" / "ai.yaml", "ai:\n  provider: mcp_agent\n")
        _write(root / "config" / "vaults.yaml", "vaults: {}\n")
        _write(root / "docs" / "PLAN.md", "private plan quoting a paper\n")
        _write(root / "docs" / "AUDIT_probe.md", "audit quoting a paper\n")
        _write(root / "docs" / "STATUS.md", "private progress\n")
        _write(root / "docs" / "AGENT_TOOLS.md", "public tool guide\n")
        _write(root / "CLAUDE.md", "@AGENTS.md\n")
        _write(root / ".claude" / "hooks" / "gate.py", "x = 1\n")
        _write(root / "scripts" / "kbsys" / "tool.py", "VALUE = 1\n")
        _write(root / "README.md", "Start here.\n")
        db.write_json(config.PAPERS_DIR / library_id / "metadata.json", {"title": "A probe title that is long enough to be a marker", "year": 2020})
        _write(root / "knowledge_store" / "notes.md", "library note\n")
        _write(root / "sources" / "inbox" / "a.pdf", "%PDF probe\n")
        _write(root / "tests" / "test_leak.py", f"ID = '{library_id}'\n")
        result, error = _raises(judge, dest)
        if error:
            return [f"release crashed: {error}"]
        if not any(library_id in leak for leak in result["leaks"]):
            bad.append("a library paper id in a shipped file is not reported as a leak")
        (root / "tests" / "test_leak.py").unlink()
        result, error = _raises(judge, dest)
        if error:
            return bad + [f"second release crashed: {error}"]
        if result["leaks"]:
            bad.append(f"a clean tree reports {len(result['leaks'])} leak(s): {result['leaks'][0]}")
        shipped = set((dest / ".release_files").read_text(encoding="utf-8").split())
        if not {"scripts/kbsys/tool.py", "docs/AGENT_TOOLS.md"} <= shipped:
            bad.append(f"the release does not ship the public software ({sorted(shipped)[:4]})")
        bad += _private_shipped(shipped, secret, dest)
    if Path(store.root).parent.resolve() == config.ROOT.resolve() and (config.ROOT / "scripts").is_dir():  # the real project
        with tempfile.TemporaryDirectory(prefix="conform-release-") as temp:
            dest = Path(temp).resolve() / "release"
            result, error = _raises(judge, dest)
            if error:
                bad.append(f"release of the real project crashed: {error}")
            else:
                shipped = set((dest / ".release_files").read_text(encoding="utf-8").split())
                bad += [f"real release: {leak}" for leak in result["leaks"]] + [f"real release: {p}" for p in _private_shipped(shipped, "", dest)]
    return bad


@check("SF-06", "probe")
def probe_writer_lock(judge=workflow.writer_lock):
    bad = []

    def second_writer():
        """What another thread (another writer: the lock is per thread) gets while this one holds / after it let go."""
        def enter():
            with judge("probe", timeout=0):
                return "got it"
        outcome = []
        thread = threading.Thread(target=lambda: outcome.append(_refused(enter)))
        thread.start()
        thread.join()
        return outcome[0]

    def holder():
        with judge("probe"):
            return second_writer()

    def failing_holder():
        with contextlib.suppress(RuntimeError), judge("probe"):
            raise RuntimeError("the writer fails")

    with _sandbox() as (root, con):
        status, held = _refused(holder)
        if status != "accepted":
            return [f"the first writer could not take the lock: {status} {held}"]
        if held[0] == "accepted":
            bad.append("a second writer takes the library lock while another holds it")
        elif held[0] == "crashed":
            bad.append(f"a second writer crashed instead of waiting or erroring: {held[1]}")
        status, text = second_writer()
        if status != "accepted":
            bad.append(f"the lock is not released after the first writer finished: {status} {text}")
        status, text = _refused(failing_holder)
        if status != "accepted":
            return bad + [f"a failing writer: {status} {text}"]
        status, text = second_writer()
        if status != "accepted":
            bad.append(f"the lock is not released when the writer fails: {status} {text}")
    return bad


# ---------------------------------------------------------------- SF-07: listeners, keys, dependencies

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
SERVER_CALLS = {"HTTPServer", "ThreadingHTTPServer", "TCPServer", "ThreadingTCPServer", "UDPServer", "Server", "create_server", "start_server"}
KEY_PATTERNS = (  # technical shapes of keys, not thresholds
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"), re.compile(r"\bghp_[A-Za-z0-9]{30,}"),
    re.compile(r"(?i)(?:api[_-]?key|secret|token|password)[\"']?\s*[:=]\s*[\"'][A-Za-z0-9_\-./+]{20,}[\"']"))


def _text_files(root, folders, suffixes):
    for folder in folders:
        base = root / folder
        for path in sorted(base.rglob("*") if base.is_dir() else ()):
            if path.is_file() and path.suffix.lower() in suffixes and "__pycache__" not in path.parts:
                yield path
    yield from (p for p in sorted(root.glob("*")) if p.is_file() and p.suffix.lower() in suffixes)


def _listeners(root):
    """Every server constructor or bind() whose address is not a literal local host, and every 0.0.0.0 in code or .bat files."""
    for path in _text_files(root, ("scripts", "tools"), {".py"}):
        name = path.relative_to(root).as_posix()
        try:
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (SyntaxError, ValueError) as err:
            yield f"{name}: cannot be parsed ({err})"
            continue
        if path.name != Path(__file__).name and "0.0.0.0" in source:
            yield f"{name}: mentions 0.0.0.0"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
            socket_bind = func == "bind" and bool(node.args) and isinstance(node.args[0], ast.Tuple)  # socket.bind((host, port)); a Tk bind takes an event string
            if func in SERVER_CALLS or socket_bind:
                address = next((k.value for k in node.keywords if k.arg in ("server_address", "host")), node.args[0] if node.args else None)
                host = address.elts[0] if isinstance(address, ast.Tuple) and address.elts else address
                if not (isinstance(host, ast.Constant) and host.value in LOCAL_HOSTS):
                    yield f"{name}:{node.lineno}: {func}({ast.unparse(address) if address is not None else ''}) is not bound to a literal local address"
    for path in _text_files(root, (), {".bat", ".cmd", ".sh"}):
        if re.search(r"0\.0\.0\.0|--host\b", path.read_text(encoding="utf-8", errors="replace")):
            yield f"{path.name}: opens a listener beyond 127.0.0.1 or passes --host"


def _api_keys(root):
    skipped = {"secrets.yaml", "secrets.yml"}
    for path in _text_files(root, ("scripts", "tools", "config", ".claude"), {".py", ".yaml", ".yml", ".json", ".bat", ".cmd", ".sh", ".md", ".toml", ".cfg"}):
        if path.name in skipped or path.name == Path(__file__).name:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in KEY_PATTERNS:
            match = pattern.search(text)
            if match:
                yield f"{path.relative_to(root).as_posix()}: looks like an API key ({match.group(0)[:12]}...)"
                break


@functools.cache
def _top_level_modules():
    """{top-level module: [distributions]} of the running environment (scanning it takes seconds: once per process)."""
    from importlib import metadata
    return metadata.packages_distributions()


def _undeclared_imports(root):
    """Third-party top-level modules imported by scripts/ or tools/ whose distribution is not a line of requirements.txt."""
    requirements = root / "requirements.txt"
    if not requirements.is_file():
        yield "requirements.txt is missing"
        return
    declared = {re.split(r"[=<>~!\[; ]", line.strip(), maxsplit=1)[0].lower().replace("_", "-") for line in requirements.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.lstrip().startswith("#")}
    provided = {top for top, dists in _top_level_modules().items() if {d.lower().replace("_", "-") for d in dists} & declared}
    local = {p.stem for folder in ("scripts", "tools", "tests") for p in (root / folder).rglob("*.py")} | {p.name for folder in ("scripts", "tools", "tests") for p in (root / folder).rglob("*") if p.is_dir()}
    for path in _text_files(root, ("scripts", "tools"), {".py"}):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError):
            continue  # reported by _listeners
        for node in ast.walk(tree):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module] if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module else []
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top not in provided and top not in local:
                    yield f"{path.relative_to(root).as_posix()}:{node.lineno}: imports {top}, which no line of requirements.txt provides"


@check("SF-07", "probe")
def probe_local_only(root=None):
    root = Path(root or config.ROOT)
    yield from _listeners(root)
    yield from _api_keys(root)
    yield from _undeclared_imports(root)
    ignore = root / ".gitignore"
    if ignore.is_file() and not re.search(r"(?m)^/?config/secrets\.ya?ml\s*$", ignore.read_text(encoding="utf-8")):
        yield ".gitignore does not list config/secrets.yaml"
