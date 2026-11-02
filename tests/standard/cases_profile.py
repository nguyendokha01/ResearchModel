"""PASS / FAIL cases of the stages SP, FG and NC, see scripts/kbsys/conform_profile.py.

SP and FG data cases store the synthetic paper with the real submit functions (config patched to the temporary store), then
the FAIL case damages one stored file. The probes of SP-07, SP-08, SP-09, FG-01, FG-04, FG-05, NC-03 and NC-05 run in every
case on the real product code, so the PASS case needs the product to be right; test_profile.py feeds them broken judges.
"""
import json
from unittest import mock

from kbsys import config, db, figure_reading, node_cards, structure
from kbsys import conform_profile as cp
from kbsys.conform import Paper, Store, Vault

PID = cp.PID


def stored_store(tmp, damage=None, make=None):
    """A store `tmp/s` holding the synthetic paper with good profiles (or the figure paper with a good reading) made by the product
    code; `damage(folder)` then edits a stored file."""
    root = tmp / "s"
    with mock.patch.multiple(config, STORE=root, PAPERS_DIR=root / "papers"):
        (make or (lambda: (cp.make_paper(root / "papers"), cp.submit_good())))()
    if damage:
        damage(root / "papers" / PID)
    return Store(root=root, paper_ids=[PID])


def edit(name, fn):
    def run(folder):
        data = db.read_json(folder / name)
        fn(data)
        db.write_json(folder / name, data)
    return run


def paper_case(damage=None):
    return lambda tmp: (stored_store(tmp, damage).paper(PID),)


def store_case(damage=None, *judges):
    return lambda tmp: (stored_store(tmp, damage),) + judges


def pair(good, bad):
    return good, bad


CASES = {}
RED = {}

# ---------------------------------------------------------------- SP
CASES["SP-01"] = pair(paper_case(), paper_case(edit("section_profile.json", lambda d: d["answers"].pop("when"))))
CASES["SP-02"] = pair(paper_case(), paper_case(edit("section_profile.json", lambda d: d["answers"]["why"].update(
    text="ROS seems to increase survival in the example model in zebrafish."))))
CASES["SP-03"] = pair(paper_case(), paper_case(edit("section_profile.json", lambda d: d["answers"]["who"].update(
    reason="No study has been reported in the literature."))))
CASES["SP-04"] = pair(paper_case(), paper_case(edit("protocol_profile.json", lambda d: d["protocols"][0]["details"][0].update(value="5 m"))))


def drop_last_methods_sentence(data):
    protocol = data["protocols"][0]
    last = f"{PID}:S0004"
    protocol["sentence_ids"].remove(last)
    protocol["steps"] = [s for s in protocol["steps"] if last not in s["sentence_ids"]]


CASES["SP-05"] = pair(paper_case(), paper_case(edit("protocol_profile.json", drop_last_methods_sentence)))
CASES["SP-06"] = pair(paper_case(), paper_case(edit("paper_overview.json", lambda d: d["research_question"][0].update(certainty="shown"))))


def task_file(chunk_ids):
    """A structured task of the synthetic paper (current source revision) with the given chunks."""
    def damage(folder):
        with mock.patch.object(config, "PAPERS_DIR", folder.parent):
            revision = structure.source_revision(PID)
        db.write_json(folder / "structured_tasks" / ("a" * 32 + ".json"), {
            "kind": "wh", "source_revision": revision,
            "chunks": [{"chunk_id": f"wh-{n:04d}", "sentence_ids": ids} for n, ids in enumerate(chunk_ids, 1)]})
    return damage


ONE, TWO = [f"{PID}:S0001"], [f"{PID}:S0002"]
CASES["SP-07"] = pair(store_case(), store_case(edit("section_profile.json", lambda d: d.pop("source_revision"))))
CASES["SP-08"] = pair(store_case(), store_case(edit("protocol_profile.json", lambda d: d.update(scientific_acceptance="accepted"))))
CASES["SP-09"] = pair(store_case(task_file([ONE, TWO])), store_case(task_file([ONE * 40])))


# ---------------------------------------------------------------- FG
def figure_make():
    def make():
        root = config.PAPERS_DIR
        cp.make_figure_paper(root)
        figure_reading.submit(PID, "F1", cp._good_reading(), "test")
    return mock.patch.object(figure_reading, "_bundle", lambda paper_id, object_id: cp._bundle_for(paper_id)), make


def figure_store(tmp, damage=None, units=None, notes=None):
    """A store holding the figure paper and its good reading; `damage(folder)` edits a stored file; units / notes for FG-03."""
    root = tmp / "s"
    patch, make = figure_make()
    with mock.patch.multiple(config, STORE=root, PAPERS_DIR=root / "papers"), patch:
        make()
    folder = root / "papers" / PID
    if damage:
        damage(folder)
    if units:
        db.write_jsonl(folder / "evidence.jsonl", units)
    return Store(root=root, paper_ids=[PID], vaults=[Vault("v", tmp / "v", notes=list(notes or ()))])


def reading_edit(fn):
    return edit("figure_readings/F1.json", fn)


def figure_case(damage=None, paper=False, **extra):
    def case(tmp):
        store = figure_store(tmp, damage, **extra)
        return (store.paper(PID),) if paper else (store,)
    return case


CASES["FG-01"] = pair(figure_case(), figure_case(reading_edit(lambda d: d.update(crop_wrong=True, note=""))))
CASES["FG-02"] = pair(figure_case(reading_edit(lambda d: d["caption_consistency"][0].update(note="Panel A shows 12 cells of the Tuj-3 group.")), paper=True),
                      figure_case(reading_edit(lambda d: d["caption_consistency"][0].update(note="Panel A shows 91 cells.")), paper=True))

INCONSISTENT = lambda d: d["caption_consistency"][0].update(status="inconsistent", note="Panel A shows 12 cells for group B.")
WARNING = lambda: figure_reading.warning({"caption_consistency": [{"status": "inconsistent", "note": "Panel A shows 12 cells for group B."}]})


def flagged_unit(flag):
    return [{"unit_id": f"{PID}:E0001", "seq": 1, "sentence_ids": [f"{PID}:S0002"],
             "flags": [{"code": "FG", "message": f"Figure 1: {figure_reading.WARNING_LABEL}"}] if flag else []}]


def fg03(flag):
    def case(tmp):
        note = f"---\npaper_id: {PID}\n---\n> [!warning] {WARNING()}\n" if flag else f"---\npaper_id: {PID}\n---\nNo warning here.\n"
        return (figure_store(tmp, reading_edit(INCONSISTENT), units=flagged_unit(flag), notes=[("Papers/Test.md", note)]),)
    return case


CASES["FG-03"] = pair(fg03(True), fg03(False))
CASES["FG-04"] = pair(lambda tmp: (figure_store(tmp), figure_reading.load),
                      lambda tmp: (figure_store(tmp, reading_edit(lambda d: d.update(crop_sha256="0" * 64))),
                                   lambda paper_id, object_id: {"status": "current"}))
CASES["FG-05"] = pair(figure_case(), figure_case(reading_edit(lambda d: d["summary"].update(text="Bars show 77 cells."))))


# ---------------------------------------------------------------- NC
def card_store(tmp, change=None):
    """A store holding paper p and one accepted card for node EG built by hand from the schema (quotes as the code copies them)."""
    root = tmp / "s"
    cp.make_paper(root / "papers", "p", cp.NODE_ROWS)
    fields = {k: {"status": "not_found_in_vault"} for k in node_cards._schema()["common_fields"] if k != "related"}
    fields["definition"] = {"text": "EG may increase survival by 10%.", "sentence_ids": ["p:S0001"], "attribution": "primary", "hedge": "hedged"}
    card = {"schema_version": 1, "concept_id": "local:eg", "name": "EG", "aliases": ["ethylene glycol"], "node_type": "concept",
            "fields": fields, "related": [],
            "quotes": {"p:S0001": {"sentence_id": "p:S0001", "paper_id": "p", "section": "introduction", "page": 1, "text": cp.NODE_ROWS[0][1]}}}
    stored = {"card": card, "analyst": "test", "created_at": "2026-10-17T00:00:00", "paper_ids": ["p"], "unit_ids": ["p:E1"],
              "source_revision": "rev", "validation": {"problems": []}, "status": "accepted"}
    if change:
        change(stored)
    db.write_json(root / "node_cards" / "v" / "local_eg.json", stored)
    return Store(root=root, paper_ids=["p"])


def card_case(change=None, *judges):
    return lambda tmp: (card_store(tmp, change),) + judges


CASES["NC-01"] = pair(card_case(), card_case(lambda s: s["card"]["fields"].pop("classification")))
CASES["NC-02"] = pair(card_case(), card_case(lambda s: s["card"]["fields"]["definition"].update(hedge="none")))
CASES["NC-03"] = pair(card_case(), card_case(lambda s: s.pop("unit_ids")))
CASES["NC-04"] = (lambda tmp: (), lambda tmp: (lambda vault, concept_id: {"card_status": "accepted"},))
CASES["NC-05"] = (lambda tmp: (), lambda tmp: (lambda con, vault, name, include: node_cards.task(con, vault, name, None),))
