"""PASS / FAIL cases of the stage CH (chunking), see scripts/kbsys/conform_chunk.py."""
from kbsys.conform import Paper, Store

from .cases_intake import fake_check


def sent(n, text, **extra):
    return {"sentence_id": f"p:S{n:04d}", "passage_id": f"p:{n:04d}", "kind": "text", "section": "results", "source": "main",
            "page": 1, "text": text, "citations": [], "mentions": [], **extra}


def passage(n, text, **extra):
    return {"passage_id": f"p:{n:04d}", "kind": "text", "section": "results", "page": 1, "text": text, **extra}


def table(**extra):
    return {"table_id": "p:T1", "source": "main", "page": 1, "header": ["Group", "n", "Rate"], "quality": "ok",
            "rows": [["A", "120", "32.5 ± 2.1"], ["B", "98", "41.2 ± 3.3"], ["C", "110", "55.9 ± 2.8"]], **extra}


def one(*fns):
    """(pass, fail) fixtures that build a Paper -> cases that return the argument tuple."""
    return tuple((lambda tmp, f=f: (f(tmp),)) for f in fns)


# ---------------------------------------------------------------- CH-01 .. CH-04

CUT = "The cells were frozen and then"
REST = "thawed in warm medium."


def linked(tmp):
    """A sentence cut by a page break and a caption, linked across the caption; a second, complete sentence."""
    return Paper("p", sentences=[
        sent(1, CUT, continues="p:S0003", page=1),
        sent(2, "Figure 1. Frozen cells.", kind="caption", page=2),
        sent(3, REST, page=2),
        sent(4, "Survival was high.", page=2)])


def unlinked(tmp):
    return Paper("p", sentences=[sent(1, CUT), sent(2, REST), sent(3, "Survival was high.")])


def link_to_table_cell(tmp):
    return Paper("p", sentences=[sent(1, CUT, continues="p:S0002"), sent(2, "Group: A | n: 5", kind="table_row", passage_id=None),
                                 sent(3, REST)])


def passages_ok(tmp):
    return Paper("p", passages=[passage(1, "Cells were counted."), passage(2, CUT), passage(3, "Caption text", kind="caption")],
                 sentences=[sent(1, "Cells were counted."), sent(2, CUT, continues="p:S0003"), sent(3, REST)])


def passage_cut(tmp):
    return Paper("p", passages=[passage(1, "Cells were counted."), passage(2, CUT)],
                 sentences=[sent(1, "Cells were counted."), sent(2, CUT)])


CASES = {
    "CH-01": (lambda tmp: (), lambda tmp: (lambda text: [(0, len(text))],)),
    "CH-02": one(linked, unlinked),
    "CH-03": one(linked, link_to_table_cell),
    "CH-04": one(passages_ok, passage_cut),
}


# ---------------------------------------------------------------- CH-05, CH-06

def tables_clean(tmp):
    """A flagged table dump, a table row, a caption and ordinary prose."""
    return Paper("p", tables=[table()], sentences=[
        sent(1, "Table 1. Survival by group.", kind="caption"),
        sent(2, "Group: A | n: 120 | Rate: 32.5 ± 2.1", kind="table_row", passage_id=None, object_id="p:T1"),
        sent(3, "A 120 32.5 2.1 B 98 41.2 3.3 C 110 55.9 2.8", in_table=True),
        sent(4, "Survival was highest in group C.")])


def table_dumped(tmp):
    paper = tables_clean(tmp)
    paper.sentences[2].pop("in_table")
    return paper


def table_ok(tmp):
    return Paper("p", tables=[table(), table(table_id="p:T2", quality="uncertain", rows=[], header=[])])


CASES.update({
    "CH-05": one(tables_clean, table_dumped),
    "CH-06": one(table_ok, lambda tmp: Paper("p", tables=[table(header=["", ""])])),
})


# ---------------------------------------------------------------- CH-07

def sections_good(tmp):
    return Paper("p", passages=[passage(1, "Smith A, Lee J (2012) Cells and freezing. J Cell 5: 1-9.", section="references"),
                                passage(2, "Received: 5 May 2020", section="front_matter", sidebar=True)],
                 sentences=[sent(1, "Received: 5 May 2020", section="front_matter"),
                            sent(2, "Cells were frozen.", section="methods", subsection="Freezing"),
                            sent(3, "Survival was high.", section="results")])


def sections_bad(tmp):
    return Paper("p", passages=[passage(1, "We stained the sections with antibody and washed them in buffer.", section="references")],
                 sentences=[sent(1, "Cells were frozen.", section="main_text", subsection="Freezing"),
                            sent(2, "Survival was high.", section="main_text")])


CASES["CH-07"] = one(sections_good, sections_bad)


# ---------------------------------------------------------------- CH-08 .. CH-12

CASES.update({
    "CH-08": (lambda tmp: (), lambda tmp: (lambda row: True,)),
    "CH-09": (lambda tmp: (Store(paper_ids=["p1"]), fake_check(("p1:S0001", "section 'x'")), lambda report: None),
              lambda tmp: (Store(paper_ids=["p1"]), fake_check(("p1", "I3: ref_nums [9] not in references")), lambda report: None)),
    "CH-10": (lambda tmp: (), lambda tmp: (lambda raw: {"first_author": "X"},)),
    "CH-11": (lambda tmp: (), lambda tmp: (lambda old, new, passages, tables: {"lost_total": 0},)),
})


def pages_clean(tmp):
    """Prose, a heading, a header repeated on every page and a link line: only the prose has to be in the sentences."""
    return Paper("p", sections=[{"name": "results", "heading": "Results"}],
                 pages=[{"page": 1, "text": "Journal of Tests 1\nResults\nCells were frozen in medium and thawed in "
                                            "warm buf-\nfer.\nhttps://doi.org/10.1/x"},
                        {"page": 2, "text": "Journal of Tests 2\nSurvival was high."}],
                 passages=[passage(1, "Cells were frozen in medium and thawed in warm buffer. Survival was high.")],
                 sentences=[sent(1, "Cells were frozen in medium and thawed in warm buffer."), sent(2, "Survival was high.")])


def pages_lost(tmp):
    return Paper("p", pages=[{"page": 1, "text": "Mitochondria swelled dramatically."}], sentences=[sent(1, "Survival was high.")],
                 passages=[passage(1, "Survival was high.")])


CASES["CH-12"] = one(pages_clean, pages_lost)

RED = {
    "CH-08": "rows without lay_size/lay_ref (stored before the G5 re-extraction) are still layout because of 'University' or a length over 600",
}
