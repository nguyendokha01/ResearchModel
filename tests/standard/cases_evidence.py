"""PASS / FAIL cases of the stage EV (evidence units), see scripts/kbsys/conform_evidence.py.

One small synthetic paper `p` goes through the real validator (evidence.validate) in EV-01..EV-13: the PASS case is the
paper with its five good units, the FAIL case adds (or removes) the one unit that breaks the rule.
"""
import copy
import json

from kbsys import concepts, crosscheck, evidence, figure_reading
from kbsys.conform import Paper

from .cases_intake import put, store

REGISTRY = concepts.Registry([])  # the real registry would make the cases depend on the library
TERMS = {"topics": [], "concepts": [], "methods": [], "species": [], "molecules": [], "cell_types": []}


def row(n, section, text, passage, kind="text", **extra):
    return {"sentence_id": f"p:S{n:04d}", "seq": n, "passage_id": passage, "page": 1, "section": section, "kind": kind,
            "text": text, "citations": [], "mentions": [], **extra}


def sentences():
    cited = row(5, "introduction", "Smith et al. reported higher survival after warming [1].", "d")
    cited["citations"] = [{"marker": "[1]", "ref_nums": [1], "style": "numeric", "confidence": "high",
                           "start": cited["text"].index("[1]"), "end": cited["text"].index("[1]") + 3}]
    return [row(1, "results", "Survival increased to 82% in treated mouse embryos (P < 0.05) compared with controls.", "a"),
            row(2, "results", "Blastocyst rate may reflect the medium used.", "a"),
            row(3, "discussion", "These data suggest that trehalose may improve survival.", "b"),
            row(4, "results", "Cell counts did not differ between groups.", "c"),
            cited,
            row(6, "results", "Figure 1. Survival of embryos.", "e", kind="caption")]


def unit(n, unit_type, sids, statement, rows=None, **extra):
    """A stored unit (what evidence.jsonl holds) citing the sentences `sids` (numbers) of `rows`."""
    rows = [r for r in (rows or sentences()) if r["seq"] in sids]
    return {"unit_id": f"p:E{n:04d}", "seq": n, "unit_type": unit_type, "statement": statement,
            "sentence_ids": [r["sentence_id"] for r in rows], "quote": evidence._quote(rows), "pages": [1],
            "sections": sorted({r["section"] for r in rows}), "model": {}, "intervention": {}, "outcome": {}, "quality": {},
            "hedging": "none", "cited_refs": [], "terms": copy.deepcopy(TERMS), "flags": [], "status": "ok", **extra}


def good_units():
    return [
        unit(1, "result", [1], "Survival increased to 82% in treated mouse embryos (P < 0.05) compared with controls.",
             model={"species": "Mouse"}, outcome={"measure": "Survival", "direction": "increase", "values": ["82%"],
                                                  "p": "P < 0.05", "value_source": "text"},
             terms={**TERMS, "concepts": ["Survival"]}),
        unit(2, "interpretation", [2], "Blastocyst rate may reflect the medium used.", hedging="hedged"),
        unit(3, "hypothesis", [3], "The authors suggest that trehalose may improve survival.", hedging="hedged"),
        unit(4, "result", [4], "Cell counts did not differ between groups.", model={"species": "Mouse"},
             outcome={"measure": "Cell counts", "direction": "no_change", "value_source": "text"},
             terms={**TERMS, "concepts": ["Cell counts"]}),
        unit(5, "cited_statement", [5], "Smith et al. [R001] reported higher survival after warming.",
             cited_refs=["p:R001"])]


ANALYSIS = {"version": 2, "analyst": "test/analyst", "classification": "CORE", "classification_reason": "In scope.",
            "evidence_type": "primary_research", "skipped": []}


def paper(units=None, sents=None, **extra):
    return Paper("p", sentences=sents or sentences(), units=good_units() if units is None else units, analysis=dict(ANALYSIS),
                 references=[{"ref_id": "p:R001", "num": 1, "year": 2010, "first_author": "Smith"}], tables=[],
                 meta={"title": "Trehalose in mouse embryo freezing", "abstract": ""}, registry=REGISTRY, **extra)


def plus(n, base, **over):
    """The good paper plus a copy of good unit `base` (1-5), changed by `over`, as unit E0006."""
    units = good_units()
    units.append({**copy.deepcopy(units[base - 1]), "unit_id": "p:E0006", "seq": 6, **over})
    return paper(units)


def one(good, bad):
    return (lambda tmp: (good(),)), (lambda tmp: (bad(),))


CASES = {
    "EV-01": one(paper, lambda: plus(1, 1, sentence_ids=["p:S0099"])),
    "EV-02": one(paper, lambda: plus(1, 1, statement="Survival increased to 91% in treated mouse embryos.")),
    "EV-03": one(paper, lambda: plus(1, 1, model={"species": "zebrafish"})),
    "EV-04": one(paper, lambda: Paper("p", **{**paper()._data, "analysis": {**ANALYSIS, "evidence_type": "review"}})),
    "EV-05": one(paper, lambda: plus(2, 2, statement="Blastocyst rate is caused by the medium used.")),
    "EV-07": one(paper, lambda: plus(5, 5, unit_type="result", cited_refs=[])),
    "EV-08": one(paper, lambda: plus(1, 1, outcome={"direction": "decrease", "measure": "Survival"})),
    "EV-09": one(paper, lambda: paper(good_units()[1:])),
    "EV-10": one(paper, lambda: plus(1, 1, terms=copy.deepcopy(TERMS))),
    "EV-11": one(paper, lambda: Paper("p", **{**paper()._data, "analysis": {**ANALYSIS, "classification": None}})),
    "EV-12": one(paper, lambda: plus(1, 1, sentence_ids=["p:S0006"])),
    "EV-13": one(paper, lambda: plus(1, 1, outcome={"value_source": "figure_only", "values": ["82%"], "measure": "Survival"})),
}


# ---------------------------------------------------------------- EV-06, EV-14 .. EV-18, EV-23

def hedge_paper(units):
    return Paper("p", sentences=sentences(), units=units)


CASES["EV-06"] = one(
    lambda: hedge_paper(good_units()),
    lambda: hedge_paper([unit(1, "result", [1, 2], "Survival increased to 82% in treated mouse embryos.", hedging="hedged")]))


def with_report(verdict):
    units = good_units()
    eligible = [u for u in units if u["unit_type"] in crosscheck.ELIGIBLE]
    results = {u["unit_id"]: {"fingerprint": crosscheck._fingerprint(u), "unit_type": u["unit_type"], "model": crosscheck.MODEL,
                              "verdict": verdict if u is eligible[0] else "supported"} for u in eligible}
    report = {"version": 1, "model": crosscheck.MODEL, "eligible_types": sorted(crosscheck.ELIGIBLE), "results": results}
    return Paper("p", units=units, v13=report)


CASES["EV-14"] = one(lambda: with_report("supported"), lambda: with_report("not_supported"))


def drifted():
    units = good_units()
    units[0]["quote"] = "Survival decreased to 12% in treated mouse embryos."
    return paper(units)


CASES["EV-15"] = one(paper, drifted)


def chain(gap):
    rows = [row(1, "results", "Cells were frozen and then", "a", continues="p:S0003" if gap else "p:S0002"),
            row(2, "results", "Figure 1. Frozen cells." if gap else "thawed in warm medium.", "b" if gap else "a",
                kind="caption" if gap else "text"),
            row(3, "results", "thawed in warm medium.", "a"), row(4, "results", "Survival was high.", "a")]
    paper = Paper("p", sentences=rows, units=[unit(1, "result", [1, 3] if gap else [1, 2], "Cells were thawed.", rows=rows)])
    if gap:  # a quote stored before EV-16 was fixed: the marker sits between the halves of one sentence
        paper.units[0]["quote"] = "Cells were frozen and then […] thawed in warm medium."
    return paper


CASES["EV-16"] = one(lambda: chain(False), lambda: chain(True))


def results(directions, species="Mouse"):
    return Paper("p", units=[unit(n, "result", [1], f"Result {n}.", model={"species": species} if species else {},
                                  outcome={"direction": d} if d else {}) for n, d in enumerate(directions, 1)])


CASES["EV-17"] = one(lambda: results(["increase"] * 20 + ["decrease"]),
                     lambda: results(["increase", None, None]))


def contrast(direction):
    return Paper("p", units=[unit(1, "result", [1], "Survival increased in group A but decreased in group B.",
                                  outcome={"direction": direction})])


CASES["EV-18"] = one(lambda: contrast("mixed"), lambda: contrast("increase"))


def vietnamese(statement):
    return paper([unit(1, "result", [1], statement)])


CASES["EV-23"] = one(lambda: vietnamese("Survival increased to 82% in treated mouse embryos (P < 0.05)."),
                     lambda: vietnamese("Tỷ lệ sống sót tăng lên 82% ở phôi chuột được xử lý."))


# ---------------------------------------------------------------- EV-19, EV-20

def statement_case(source, statement):
    rows = [row(1, "results", source, "a")]
    return Paper("p", sentences=rows, units=[unit(1, "result", [1], statement, rows=rows)])


SYNONYMS = [("dimethyl sulfoxide", "DMSO")]
CASES["EV-19"] = (
    lambda tmp: (statement_case("Dimethyl sulfoxide was added to the medium at 3 mM.", "DMSO was added to the medium at 3 mM."), SYNONYMS),
    lambda tmp: (statement_case("Survival increased in treated embryos.", "Survival increased in treated embryos, mediated by HIF1A (P = 0.01)."), SYNONYMS))


def figure_paper(flagged):
    reading = {"object_id": "p:F1", "label": "Figure 1", "caption_consistency": [
        {"claim_sentence_id": "p:S0001", "status": "inconsistent", "note": "The text names another group."}]}
    flags = [{"code": "FG", "message": f"Figure 1: {figure_reading.WARNING_LABEL}"}] if flagged else []
    units = [unit(1, "result", [1], "Survival increased.", flags=flags), unit(2, "result", [4], "Counts were equal.")]
    return Paper("p", sentences=sentences(), units=units, readings=[reading])


CASES["EV-20"] = one(lambda: figure_paper(True), lambda: figure_paper(False))


# ---------------------------------------------------------------- EV-21, EV-22

def aliases_store(tmp, aliases):
    units = [unit(1, "result", [1], "Survival increased."), unit(2, "result", [4], "Counts were equal.")]
    return store(tmp, files={"papers/p1/evidence.jsonl": "".join(json.dumps(u) + "\n" for u in units),
                             "papers/p1/evidence_aliases.json": json.dumps(aliases)})


CASES["EV-21"] = (lambda tmp: (aliases_store(tmp, {"p:E0003": "p:E0001", "p:E0004": "p:E0003"}),),
                  lambda tmp: (aliases_store(tmp, {"p:E0003": "p:E0007"}),))


def tree(tmp, code):
    put(tmp / "scripts" / "kbsys" / "mod.py", code)
    put(tmp / "scripts" / "kbsys" / "hedges.py", 'WORDS = ["may", "might", "suggest", "seems"]\n')  # the one allowed list
    return (tmp,)


CASES["EV-22"] = (lambda tmp: tree(tmp, '"""Results may suggest, and the text might say so."""\nNAMES = ["alpha", "beta", "gamma", "delta"]\nPIPE = "a|b|c"\n'),
                  lambda tmp: tree(tmp, 'import re\nMODAL = re.compile(r"\\b(may|might|could|suggest)\\b")\n'))
