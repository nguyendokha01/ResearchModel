"""Conformance checks of the stage EV (evidence units) of config/standard.yaml, see conform.py.

Data checks read one paper at a time. EV-01..EV-13 re-run the product validator `evidence.validate` (V1-V12) on the
stored units of the paper, with the paper context built from the paper's own files (nothing written; the concept registry is read from the library database read-only),
and report every item of the clause's code: rejected, flagged or warned. EV-14 and EV-15 re-use crosscheck / evidence
(V13, V14), EV-16 and EV-23 rebuild the quote with `evidence._quote`. The synthetic good/bad units of the tests go
through the same validator, so a validator that accepts a bad unit makes the FAIL case pass and the PASS case fail.

Heuristics (what they cannot see is said where used): EV-06 (the main-claim sentence is guessed, the data model has
no `hedged_sentence_ids`), EV-17 (only the empty shares are measured), EV-18, EV-19, EV-20.
EV-22 scans the code. EV-21 is a store check: the stored aliases plus a probe of the merge and of `resolve_unit`.
"""
import ast
import functools
import re
import sqlite3
from pathlib import Path
from unittest import mock

from . import concepts, config, crosscheck, db, evidence, figure_reading, hedges, review_policy, standard
from .conform import check
from .conform_intake import _sandbox
from .sentences import MENTION, numbers

NON_HEDGE = hedges.NON_HEDGE  # EV-06: lexicon categories that are not a hedge of the claim (one copy, in hedges.py)


# ---------------------------------------------------------------- helpers

def _extra(paper, name, filename, default):
    """A file of the paper folder that `Paper` does not list (sections.json ...); tests give `<name>=` directly."""
    got = getattr(paper, name, None)
    if got is None and paper.folder:
        got = db.read_json(paper.folder / filename, default)
    return default if got is None else got


@functools.lru_cache(maxsize=None)
def _registry():
    """The concept registry as the validator sees it: with the paper terms of the library database, opened read-only."""
    if not config.DB_PATH.is_file():
        return concepts.build_registry()
    con = sqlite3.connect(f"{config.DB_PATH.as_uri()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        return concepts.build_registry(con)
    finally:
        con.close()


def _context(paper):
    """The validator's PaperContext made from the paper's own rows (what PaperContext.__init__ reads from disk)."""
    ctx = evidence.PaperContext.__new__(evidence.PaperContext)
    ctx.paper_id, ctx.meta = paper.id, paper.meta
    ctx.sentences = {s["sentence_id"]: s for s in paper.sentences}
    ctx.refs = {r["ref_id"]: r for r in paper.references}
    ctx.abbreviations = paper.structure.get("abbreviations") or []
    ctx.table_caption_ids = {sid for t in paper.tables for sid in t.get("caption_sentence_ids") or []}
    ctx._index(" ".join([paper.meta.get("title") or "", paper.meta.get("abstract") or ""]))
    ctx.registry = getattr(paper, "registry", None) or _registry()  # tests give a registry= of their own
    ctx.canonical = evidence._canonical_terms(None, ctx.registry)
    return ctx


def _validated(paper):
    """evidence.validate over the stored units of the paper (once per paper); None for a paper without units."""
    if "_validation" not in paper._data:
        result = None  # stored only after validate returned: a crash must reach every clause as an ERROR, never as a PASS
        if paper.units:
            submission = evidence.submission_from(paper.id, paper.analysis, paper.units)
            ctx = _context(paper)
            with mock.patch.object(evidence, "PaperContext", lambda con, paper_id: ctx):
                result = evidence.validate(None, paper.id, submission)[0]
        paper._data["_validation"] = result
    return paper._data["_validation"]


def _unit_id(paper, index):
    return paper.units[index - 1].get("unit_id") if 1 <= index <= len(paper.units) else "paper"


def _by_code(paper, *codes):
    result = _validated(paper)
    for key, label in (("errors", "rejected"), ("flags", "flagged"), ("warnings", "warning")):
        for item in (result or {}).get(key, ()):
            if item["code"] in codes:
                yield f"{_unit_id(paper, item['unit_index'])}: {item['code']} {label}: {item['message'][:100]}"


def _code_check(code):
    def run(paper):
        yield from _by_code(paper, code)
    run.__doc__ = f"Every {code} rejection, flag or warning of evidence.validate on the stored units."
    return run


for _clause, _code in (("EV-01", "V1"), ("EV-02", "V2"), ("EV-03", "V3"), ("EV-04", "V4"), ("EV-07", "V6"),
                       ("EV-08", "V7"), ("EV-12", "V11"), ("EV-13", "V12")):
    check(_clause, "paper")(_code_check(_code))


def _rows(paper, unit):
    by_id = {s["sentence_id"]: s for s in paper.sentences}
    return [by_id.get(s) for s in unit.get("sentence_ids") or []]


# ---------------------------------------------------------------- EV-01 .. EV-13: validator rules V1-V12

@check("EV-05", "paper")
def hedges_are_kept(paper):
    """V5 flags, plus a unit stored `hedging: none` over hedged source wording (validate corrects it silently)."""
    yield from _by_code(paper, "V5")
    for item in (_validated(paper) or {}).get("normalized_terms", ()):
        if item.get("field") == "hedging":
            yield f"{_unit_id(paper, item['unit_index'])}: hedging stored as none but the source says {item['because']!r}"


@check("EV-09", "paper")
def must_cover_sentences_are_used(paper):
    result = _validated(paper) or {}
    yield from _by_code(paper, "V8")
    for short in result.get("coverage_missing", ()):
        yield f"{short}: must-cover sentence neither cited nor skipped with a reason"


@check("EV-10", "paper")
def terms_are_canonical(paper):
    """V9 (a result/method_detail unit without a term list, a malformed list) and a stored term the registry spells otherwise."""
    yield from _by_code(paper, "V9")
    for item in (_validated(paper) or {}).get("normalized_terms", ()):
        if not item.get("field"):  # a `field` entry is a hedging correction or a rule fill (EV-17), not a term
            yield f"{_unit_id(paper, item['unit_index'])}: term {item.get('from') or 'type duplicate'!r} is not canonical ({item['to']!r})"


@check("EV-11", "paper")
def submission_fields(paper):
    """V10 on the analysis.json of the paper: classification, reason, evidence_type, analyst, units."""
    yield from _by_code(paper, "V10")


# ---------------------------------------------------------------- EV-06: hedging on the main-claim sentence

def _real_hedges(text):
    return [h for h in hedges.uncertainty(text) if h["category"] not in NON_HEDGE]


def _main_claim(unit, rows):
    """The sentences that carry the claim: `hedged_sentence_ids` when the unit has them, else the cited sentence that
    shares the most words with the statement. ponytail: a guess; replace by the stored field when G3 adds it."""
    named = [r for r in rows if r["sentence_id"] in (unit.get("hedged_sentence_ids") or ())]
    if named or len(rows) < 2:
        return named or rows
    words = set(evidence._tokens(unit.get("statement")))
    return [max(rows, key=lambda r: len(words & set(evidence._tokens(r["text"]))))]


@check("EV-06", "paper")
def hedged_only_by_the_main_claim(paper):
    """A stored `hedging: hedged` unit whose main-claim sentence is not hedged although (a) another cited sentence is,
    or (b) the only thing in it that the lexicon knows is non-significance (p > 0.05). A unit hedged with no lexicon
    word anywhere is not reported (analyst judgement or a gap of the lexicon). Cannot see: which sentence the analyst
    meant (the data has no `hedged_sentence_ids`) and "associated with" used as a location (same lexicon hit)."""
    for unit in paper.units:
        rows = _rows(paper, unit)
        if unit.get("hedging") != "hedged" or not rows or None in rows:
            continue
        main = _main_claim(unit, rows)
        hits = [h for r in main for h in hedges.uncertainty(evidence._masked(r))]
        if any(h["category"] not in NON_HEDGE for h in hits):
            continue
        others = [r["sentence_id"].rsplit(":", 1)[-1] for r in rows if r not in main and _real_hedges(evidence._masked(r))]
        if others or hits:
            where = main[0]["sentence_id"].rsplit(":", 1)[-1]
            yield f"{unit['unit_id']}: hedged but claim sentence {where} " + (f"has no hedge, {others} do" if others else "only reports non-significance")


# ---------------------------------------------------------------- EV-14, EV-15: V13 and V14

@check("EV-14", "paper")
def second_model_verdicts_are_flagged(paper):
    """Re-uses crosscheck.validate_report_units on analysis_v13.json: a partly / not_supported verdict needs exactly
    one V13 flag, a supported one none. Units without a current report are not judged."""
    report = _extra(paper, "v13", crosscheck.REPORT, None)
    if report:
        yield from crosscheck.validate_report_units(paper.units, report)["errors"]


@check("EV-15", "paper")
def drifted_units_are_flagged(paper):
    """Re-uses evidence.source_drift (a cited sentence is missing, or the stored quote or sections differ from the
    current sentences): such a unit must carry a V14 flag. Cannot see whether an ID was once rewritten to a guess."""
    by_id = {s["sentence_id"]: s for s in paper.sentences}
    for index in evidence.source_drift(paper.units, by_id, paper.id):
        unit = paper.units[index - 1]
        if not any(f.get("code") == "V14" for f in unit.get("flags") or ()):
            yield f"{unit.get('unit_id')}: cited source drifted and the unit has no V14 flag"


# ---------------------------------------------------------------- EV-16, EV-23: quotes and language

def _quote_problems(paper):
    for unit in paper.units:
        rows = _rows(paper, unit)
        if not rows or None in rows:
            continue  # a missing source sentence is EV-15
        ordered = sorted(rows, key=lambda r: r["seq"])
        gaps = [(a, b) for a, b in zip(ordered, ordered[1:]) if b["seq"] != a["seq"] + 1 and a.get("continues") == b["sentence_id"]]
        rebuilt, stored = evidence._quote(rows), unit.get("quote")
        # a stored marker between the halves of one sentence is reported once, as the gap marker (below), not also as EV-23
        marker_only = (bool(gaps) and isinstance(stored, str) and stored != rebuilt and stored.replace(" […] ", " ") == rebuilt.replace(" […] ", " ")
                       and stored.count("[…]") == rebuilt.count("[…]") + len(gaps))  # exactly the markers the old rebuild added
        if stored != rebuilt and not marker_only:
            yield unit["unit_id"], "quote differs from the text rebuilt from the cited sentences"
        for a, b in gaps:
            if marker_only:
                yield unit["unit_id"], f"gap marker inside one sentence ({a['sentence_id'].rsplit(':', 1)[-1]} continues into {b['sentence_id'].rsplit(':', 1)[-1]})"


@check("EV-16", "paper")
def quotes_are_copied_by_code(paper):
    for unit_id, problem in _quote_problems(paper):
        yield f"{unit_id}: {problem}"


@check("EV-23", "paper")
def canonical_text_is_english(paper):
    for unit in paper.units:
        terms = [t for values in (unit.get("terms") or {}).values() for t in values or ()]
        for what, text in (("statement", unit.get("statement")), ("terms", " ".join(map(str, terms)))):
            if evidence.VIETNAMESE.search(str(text or "")):
                yield f"{unit['unit_id']}: {what} has Vietnamese letters"
    for unit_id, problem in _quote_problems(paper):
        if problem.startswith("quote differs"):
            yield f"{unit_id}: {problem}"


# ---------------------------------------------------------------- EV-17 .. EV-20: one finding, no added content, shares

@check("EV-17", "paper")
def results_state_direction_and_species(paper):
    """Share of result units without outcome.direction or model.species, against the params of the clause. The empty
    fields are filled by `evidence_fill` (new submissions; stored units by `kb.py fill-evidence-fields --apply`, G5)."""
    results = [u for u in paper.units if u.get("unit_type") == "result"]
    for label, empty, limit in (
            ("direction", [u for u in results if not (u.get("outcome") or {}).get("direction")], "max_empty_direction_share"),
            ("species", [u for u in results if not (u.get("model") or {}).get("species")], "max_empty_species_share")):
        share, allowed = len(empty) / len(results) if results else 0, standard.param("EV-17", limit)
        if share > allowed:
            yield f"{len(empty)} of {len(results)} result units have no {label} ({share:.0%}, limit {allowed:.0%})"


@check("EV-18", "paper")
def one_finding_per_unit(paper):
    """A result unit with one direction (increase, decrease, no_change) whose statement joins opposite outcomes with
    but / while / whereas. Cannot see opposite outcomes without direction words ("rose in A but not in B")."""
    for unit in paper.units:
        if unit.get("unit_type") != "result" or (unit.get("outcome") or {}).get("direction") not in ("increase", "decrease", "no_change"):
            continue
        parts = evidence.CONTRAST.split(str(unit.get("statement") or ""))
        for left, right in zip(parts, parts[1:]):
            a, b = evidence.directions(left), evidence.directions(right)
            if a and b and a.isdisjoint(b):
                yield f"{unit['unit_id']}: direction {unit['outcome']['direction']!r} but the statement turns from {sorted(a)} to {sorted(b)}"
                break


@check("EV-19", "paper")
def statement_adds_no_content(paper, groups=None):
    """Numbers and entities (see _entities) of a statement must occur in the cited sentences, or in a registered
    synonym of them (config/synonyms.yaml), or be an author of a cited reference. Cannot see added plain words
    (a mechanism phrase in lower case): only numbers and named things."""
    groups = groups if groups is not None else evidence.synonym_groups()
    abbreviations = paper.structure.get("abbreviations") or []
    refs = {r["ref_id"]: r for r in paper.references}
    sections = {str(s.get("section")).replace("_", " ") for s in paper.sentences}
    for unit in paper.units:
        rows = _rows(paper, unit)
        if not rows or None in rows:
            continue
        source = " ".join(evidence._masked(r) for r in rows)
        cited_ids = set(unit.get("cited_refs") or ()) | {f"{paper.id}:R{n:03d}" for r in rows for c in r.get("citations") or () for n in c["ref_nums"]}
        cited = [refs.get(r) or {} for r in cited_ids]
        years = {str(r.get("year")) for r in cited if r.get("year")}
        statement = str(unit.get("statement") or "")
        missing, _ = evidence._missing_numbers(numbers(evidence.REF_ID.sub(" ", MENTION.sub(" ", statement))) - years, numbers(source))
        for n in sorted(missing):
            yield f"{unit['unit_id']}: number {n} is not in the cited sentences"
        for word in evidence.added_entities(statement, rows, refs, cited_ids, sections, groups, abbreviations):
            yield f"{unit['unit_id']}: {word!r} is not in the cited sentences"


@check("EV-20", "paper")
def figure_warnings_reach_units(paper):
    """A reading with a warning (figure_reading.warning: inconsistent caption check or red candidate, not a wrong
    crop) marks its claim sentences and the sentences of the object itself; a unit citing one of them must carry a
    flag whose message has the warning label. Cannot see units that only mention the figure in a sentence of their own."""
    readings = getattr(paper, "readings", None)  # tests give readings= directly
    if readings is None:
        folder = paper.folder / "figure_readings" if paper.folder else None
        readings = [db.read_json(p, {}) or {} for p in sorted(folder.glob("*.json"))] if folder and folder.is_dir() else []
    marked = figure_reading.unit_warnings(readings, paper.sentences)  # the validator's own definition
    for unit in paper.units:
        labels = sorted({marked[s] for s in unit.get("sentence_ids") or () if s in marked})
        if labels and not any(figure_reading.WARNING_LABEL in str(f.get("message")) for f in unit.get("flags") or ()):
            yield f"{unit['unit_id']}: cites {', '.join(labels)} (reading warning) but carries no figure warning"


# ---------------------------------------------------------------- EV-21: duplicates and aliases

def _merge_units():
    base = {"unit_type": "result", "statement": "Cells grew in group A.", "sentence_ids": ["p:S0001"],
            "quote": "Cells grew in group A.", "model": {"species": "mouse"}, "intervention": {"dose": "5 mM"},
            "outcome": {"direction": "increase"}, "quality": {}, "hedging": "none", "cited_refs": [],
            "terms": {"concepts": ["Cell"]}, "status": "ok", "flags": []}
    units = [dict(base, unit_id="p:E0001", seq=1), dict(base, unit_id="p:E0002", seq=2)]
    differing = ({"intervention": {"dose": "10 mM"}}, {"hedging": "hedged"}, {"outcome": {"direction": "decrease"}},
                 {"model": {"species": "rat"}})
    return units + [dict(base, unit_id=f"p:E{n:04d}", seq=n, **change) for n, change in enumerate(differing, 3)]


@check("EV-21", "store")
def duplicates_merge_reversibly(store, merge=review_policy.deduplicate, resolve=evidence.resolve_unit):
    """Data: every stored alias ends, without a loop, at a unit of its own paper. Probe: the merge joins the exact
    copy and nothing that differs in dose, hedging, direction or species; `resolve` follows an alias chain."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        aliases, ids = _extra(paper, "aliases", "evidence_aliases.json", {}), {u.get("unit_id") for u in paper.units}
        for old in aliases:
            seen, now = {old}, aliases[old]
            while now in aliases and now not in seen:
                seen.add(now)
                now = aliases[now]
            if now in seen:
                yield f"{old}: alias chain loops"
            elif now not in ids:
                yield f"{old}: alias chain ends at {now}, which is no unit of {paper_id}"
    kept, aliases = merge(_merge_units())
    if aliases != {"p:E0002": "p:E0001"} or [u["unit_id"] for u in kept] != ["p:E0001", "p:E0003", "p:E0004", "p:E0005", "p:E0006"]:
        yield f"merge kept {[u['unit_id'] for u in kept]} with aliases {aliases}; expected only E0002 merged into E0001"
    with _sandbox(vaults=()) as (root, con):
        folder = db.paper_dir("p")
        db.write_jsonl(folder / "evidence.jsonl", [dict(u) for u in _merge_units()[:1]])
        db.write_json(folder / "evidence_aliases.json", {"p:E0003": "p:E0002", "p:E0002": "p:E0001", "p:E0009": "p:E0010", "p:E0010": "p:E0009"})
        got = resolve(con, "p:E0003")
        if not got or got.get("unit_id") != "p:E0001" or got.get("requested_unit_id") != "p:E0003":
            yield f"resolve_unit(p:E0003) did not follow the alias chain to p:E0001: {got and got.get('unit_id')}"
        try:
            resolve(con, "p:E0009")
            yield "resolve_unit followed a cyclic alias chain"
        except ValueError:
            pass


# ---------------------------------------------------------------- EV-22: one hedge vocabulary

@check("EV-22", "probe")
def probe_one_hedge_vocabulary(root=None):
    """Scans scripts/**/*.py (hedges.py apart) for a list or a regex alternation that spells out min_hedge_words or more
    distinct words (and at least min_hedge_share of its entries) that config/hedge_lexicon.yaml (hedges.uncertainty) knows,
    except a collection listed in the clause `exceptions` (file + one exact item + reason). Cannot see a list built at run time."""
    root = Path(root or config.ROOT)
    exempt = {(e["file"], e["item"]) for e in standard.param("EV-22", "exceptions")}  # a listed collection, not the whole file
    for path in sorted((root / "scripts").rglob("*.py")):
        if path.name == "hedges.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, ValueError) as err:
            yield f"{path.relative_to(root).as_posix()}: cannot be parsed ({err})"
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
                items = [c.value for c in node.elts if isinstance(c, ast.Constant) and isinstance(c.value, str)]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and "|" in node.value:
                items = node.value.split("|")  # a regex alternation
            else:
                continue
            if any((path.relative_to(root).as_posix(), item) in exempt for item in items):
                continue
            found = {h["text"].casefold() for h in hedges.uncertainty(" ".join(items))}
            if len(found) >= standard.param("EV-22", "min_hedge_words") and len(found) >= standard.param("EV-22", "min_hedge_share") * len(items):
                yield f"{path.relative_to(root).as_posix()}:{node.lineno}: its own hedge list ({', '.join(sorted(found)[:4])} ...)"
