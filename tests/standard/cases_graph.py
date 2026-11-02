"""PASS / FAIL cases of the stages ND (concepts and nodes) and RL (relations, conflicts, typed network), see
scripts/kbsys/conform_graph.py.

Data checks get small synthetic stores / vaults. Probes run the product function in a throw-away project in every PASS case, so a
product that is wrong today makes its PASS case report violations (listed in RED); the FAIL cases hand the check a deliberately
broken judge or a damaged store. test_graph.py runs the data parts and the probes apart from each other.
"""
import atexit
import json
import sqlite3

from kbsys import concepts, db, notes
from kbsys import conform_graph as cg
from kbsys.conform import Store, Vault

_DBS = []
KEY = concepts._key


def mem_db(sentences=(), links=(), evidence=()):
    """An in-memory library with these sentences (id, paper, text), concept links and evidence rows (unit id, sentence ids)."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript(db.SCHEMA)
    atexit.register(con.close)
    _DBS.append(con)
    con.executemany("INSERT INTO sentences VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(sid, pid, 1, None, "main", 1, "results", None, "text", text) for sid, pid, text in sentences])
    con.executemany("INSERT INTO concept_links VALUES (?,?,?,?,?,?)", links)
    con.executemany("INSERT INTO evidence (unit_id, paper_id, seq, unit_type, status, statement, quote, sentence_ids, pages, sections) "
                    "VALUES (?, 'p1', 1, 'result', 'ok', 's', 'q', ?, '[]', '[]')", [(uid, json.dumps(ids)) for uid, ids in evidence])
    return con


def record(name, type_="concept", **extra):
    return {"concept_id": "local:" + KEY(name).replace(" ", "-"), "name": name, "type": type_, "synonyms": [], "status": "ai_proposed",
            "mesh_ui": None, "mesh_tree_numbers": [], "parents": [], **extra}


def put(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def generated(name="Apoptosis", body="## Description\n\n- Cells were counted in group A.\n"):
    return (f"---\ntype: concept\n---\n# {name}\n\n{notes.AUTO_START}\n<!-- kb:concept-fingerprint:{'0' * 40} -->\n\n{body}\n"
            f"{notes.AUTO_END}\n\n## My notes\n")


def stub(name="Apoptosis"):
    return f"---\nkb_stub: true\ntype: concept\n---\n{notes.STUB_MARK}\n# {name}\n\n## Notes\n\n"


CASES = {}
RED = {}

# ---------------------------------------------------------------- ND-01 .. ND-04

def nd01(tmp, blocked):
    text = "Gene Ontology enrichment of the genes was done." if blocked else "Graphene oxide (GO) was added to the medium."
    con = mem_db([("p1:S0001", "p1", text)], [("local:go", "p1", "studied", 1, '["results"]', "p1:S0001")])
    return (Store(con=con, concepts=[record("GO", "molecule")]),)


CASES["ND-01"] = (lambda tmp: nd01(tmp, False), lambda tmp: nd01(tmp, True))

ABBREVIATIONS = {"abbreviations": [{"short": "AMI", "definitions": [{"text": "acute myocardial infarction"}]}]}


def nd02(tmp, merged, linked):
    root = tmp / "s"
    put(root / "papers" / "pa" / "structure.json", json.dumps(ABBREVIATIONS))
    put(root / "papers" / "pa" / "sentences.jsonl", "".join(json.dumps(row) + "\n" for row in cg._sentence_rows(
        "pa", ["Acute myocardial infarction (AMI) was induced.", "AMI size was measured.", "Weight was recorded."])))
    names = [record("Acute myocardial infarction", synonyms=["AMI"])] if merged else [record("Acute myocardial infarction"), record("AMI")]
    con = mem_db(links=[(names[0]["concept_id"], "pa", "studied", linked, '["results"]', "pa:S0001")])
    return Store(con=con, root=root, concepts=names, paper_ids=["pa"])


def ami_pairs(known):
    return [(KEY("AMI"), KEY("acute myocardial infarction"))] if KEY("AMI") in known else []


CASES["ND-02"] = (lambda tmp: (nd02(tmp, True, 3), ami_pairs), lambda tmp: (nd02(tmp, False, 1), ami_pairs))

def split_registry(con):
    """A broken build_registry: every spelling is a concept of its own."""
    return concepts.Registry([dict(record(v), concept_id=f"local:{n}") for n, v in enumerate(v for g in cg.VARIANT_GROUPS for v in g)])


GOOD_NAMES = [record("Apoptosis"), record("Cell survival"), record("Fisher's exact test", "method")]
BAD_NAMES = GOOD_NAMES + [record("Fisher’s exact test", "method")]
CASES["ND-03"] = (lambda tmp: (Store(concepts=GOOD_NAMES),), lambda tmp: (Store(concepts=BAD_NAMES), split_registry))


def nd04(tmp, generic):
    name = sorted(concepts.generic_keys())[0] if generic else "Apoptosis"
    put(tmp / "v" / "Concepts" / f"{name}.md", stub(name))
    return (Store(concepts=[record(name)] if generic else [record("Apoptosis")], vaults=[Vault("v", tmp / "v")], con=mem_db()),)


CASES["ND-04"] = (lambda tmp: nd04(tmp, False), lambda tmp: nd04(tmp, True))


# ---------------------------------------------------------------- ND-05 .. ND-08

def collect_with(mentions, units):
    return lambda con, rec, registry, vault: (set(), {"p1": [{"n_sentences": mentions}]} if mentions else {}, [{"unit_id": "p1:E0001"}] * units)


def node_case(text, mentions, units):
    return lambda tmp: (Vault("v", tmp / "v", notes=[("Concepts/Apoptosis.md", text)]), concepts.Registry([record("Apoptosis")]),
                        collect_with(mentions, units))


CASES["ND-05"] = (node_case(generated(), 5, 0), node_case(stub(), 1, 0))
CASES["ND-06"] = (node_case(generated(), 5, 0), node_case(stub(), 0, 0))


def nd07(tmp, sections, error=None):
    con = mem_db(links=[("local:apoptosis", "p1", "studied", 2, json.dumps(sections), "p1:S0001")])
    return Store(con=con), (lambda report, con: report.error("concepts", error) if error else None)


CASES["ND-07"] = (lambda tmp: nd07(tmp, ["results"]), lambda tmp: nd07(tmp, ["results"], "concept_id must be unique"))
CASES["ND-08"] = (lambda tmp: (), lambda tmp: (concepts.build_registry, lambda text, pattern, record: True))


# ---------------------------------------------------------------- RL-01 .. RL-03

def index_of(typed):
    def build(files):
        edge = {"source": "Results/EVD - Alpha raises Beta.md", "target": "Concepts/Alpha.md", "label": "Alpha", "status": "resolved"}
        if not typed:
            return ["source", "target", "label", "status"], [edge]
        return (["source", "target", "label", "status", "etype", "unit_id", "sentence_ids"],
                [dict(edge, etype="observed_effect", unit_id="p1:E0001", sentence_ids='["p1:S0001"]'),
                 dict(edge, source="Papers/Paper One.md", etype="nav", unit_id=None, sentence_ids=None)])
    return build


def open_index(typed):
    """A neural.py vault index in memory: edge rows as `index_of` builds them (the connection is new on every call)."""
    def open_(name, readonly=False):
        columns, edges = index_of(typed)(None)
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.execute(f"CREATE TABLE edges ({', '.join(columns)})")
        con.execute("CREATE TABLE notes (path, text)")  # the real index has it; RL-01 reads the result notes' text
        con.executemany(f"INSERT INTO edges VALUES ({', '.join('?' * len(columns))})", [[e[c] for c in columns] for e in edges])
        return con
    return open_


def rl01(tmp, typed):
    build = index_of(False) if not typed else cg._index_of  # the PASS case builds the index with the product
    return (Store(con=mem_db(evidence=[("p1:E0001", ["p1:S0001"])]), vaults=[Vault("v", tmp / "v")]), build, open_index(typed))


CASES["RL-01"] = (lambda tmp: rl01(tmp, True), lambda tmp: rl01(tmp, False))


def relation(**extra):
    return {"relation_id": "r1", "from_unit": "pa:E0001", "to_unit": "pb:E0001", "type": "contradicts", "status": "ai_proposed",
            "vault": "one", "outcome_measure": "Cell survival rate", "outcome_measure_other": "cell survival rate",
            "directions": ["increase", "decrease"], "differences": [{"field": "model.stage", "from": "a", "to": "b"}],
            "match_basis": "exact_measure", "shared_concept_id": None, **extra}


def rl02_clause(concept, compared, synonyms=()):
    """A clause pair of `concept`: `compared` names what the relation compares (the unit measure of one side, the entity name of the
    clause side); outcome_measure_other is the whole bundled measure of the clause unit, which must not be compared."""
    return (Store(relations=[relation(match_basis=f"shared_concept_clause:local:{KEY(concept)}", shared_concept_id=f"local:{KEY(concept)}",
                                      outcome_measure=compared[0], outcome_measure_other="Histone methylation and acetylation",
                                      compared_measures=list(compared))],
                  concepts=[record(concept, synonyms=list(synonyms))]),)


CASES["RL-02"] = (lambda tmp: (Store(relations=[relation()], concepts=[]),),
                  lambda tmp: (Store(relations=[relation(differences=[{"field": "intervention.agent", "from": "A", "to": "B"}])], concepts=[]),))
CASES["RL-03"] = (lambda tmp: (Store(relations=[relation(match_basis="shared_concept_clause:local:ros", outcome_measure="ROS, DNA damage, and cell number",
                                                          outcome_measure_other="ROS content")]),),
                  lambda tmp: (Store(relations=[relation(match_basis="shared_concept:local:ros", outcome_measure="ROS, DNA damage, and cell number",
                                                          outcome_measure_other="ROS content")]),))


# ---------------------------------------------------------------- RL-04 .. RL-08

def conflict_note(title, species_a, species_b, tags=("conflict",), relation_id="r1", caution=False):
    return (f"---\ntype: conflict\nid: {relation_id}\ntitle: {title}\ntags:\n" + "".join(f"- {t}\n" for t in tags) + f"---\n# {title}\n\n"
            f"{notes.AUTO_START}\n## Summary\n\n## Side-by-side comparison\n\n| | A | B |\n|---|---|---|\n| Species | {species_a} | {species_b} |\n\n"
            + ("## What differs\n\n- Non-significance is not evidence of no effect.\n\n" if caution else "") + "## Sources\n\npa:E0001 pb:E0001\n"
            f"{notes.AUTO_END}\n")


def conflicts_vault(tmp, note, listed=True):
    maps = "# Open conflicts\n\n- [[CONFLICT - Cells]]\n" if listed else "# Open conflicts\n"
    return Vault("v", tmp / "v", notes=[("Claims/CONFLICT - Cells.md", note), ("_Maps/Open conflicts.md", maps)])


CASES["RL-04"] = (lambda tmp: (conflicts_vault(tmp, conflict_note("Cells - Mouse vs Cattle - A 2020 vs B 2021", "Mouse", "Cattle (from paper)")),),
                  lambda tmp: (conflicts_vault(tmp, conflict_note("Cells - Mouse - A 2020 vs B 2021", "Mouse", "Cattle (from paper)")),))


def rl05(tmp, vault):
    root = tmp / "s"
    for pid in ("pa", "pb"):
        put(root / "papers" / pid / "metadata.json", json.dumps({"paper_id": pid, "vaults": ["one"]}))
    return (Store(root=root, relations=[relation(vault=vault)]),)


CASES["RL-05"] = (lambda tmp: rl05(tmp, "one"), lambda tmp: rl05(tmp, None))


def rl06(tmp, tags, listed, caution, kind="effect_vs_no_effect"):
    return (conflicts_vault(tmp, conflict_note("Cells", "Mouse", "Mouse", tags, caution=caution), listed),
            lambda vault: [relation(type=kind)])


CASES["RL-06"] = (lambda tmp: rl06(tmp, ("conflict",), True, True), lambda tmp: rl06(tmp, ("conflict",), True, False))


def broken_propose(con, vault):
    path = cg.relations.RELATIONS
    path.write_text("".join(json.dumps(dict(r, status="ai_proposed")) + "\n" for r in db.read_jsonl(path)), encoding="utf-8", newline="\n")


CASES["RL-07"] = (lambda tmp: (), lambda tmp: (broken_propose,))


def rl08(tmp, mention):
    """One unproven_hypothesis row that says `ros` is missing; the results mention reactive oxygen species only when `mention`."""
    root = tmp / "s"
    row = {"gap_id": "gap:1", "gap_type": "unproven_hypothesis", "vault": "one", "status": "needs_review", "entities": ["ros"],
           "description": "Hypothesis p:E0001: no accepted result unit in this vault mentions 'ros'.",
           "scope_note": "not found in the current vault - this does not mean it is absent from the literature"}
    db.write_jsonl(root / "gap_candidates.jsonl", [row])
    results = [{"id": "pa:E0001", "paper": "pa", "statement": "Cells were counted.", "entities": [], "agent": "", "species": [],
                "direction": "increase", "type": "result",
                "measure": "Reactive oxygen species level" if mention else "Cell survival rate"}]
    return (Store(root=root, concepts=[record("Reactive oxygen species", synonyms=["ROS"])]), lambda vault: (1, results, [], {"pa"}))


CASES["RL-08"] = (lambda tmp: rl08(tmp, False), lambda tmp: rl08(tmp, True))

CASES["RL-09"] = (lambda tmp: (), lambda tmp: (lambda: {"Unique One", "DNA"},))


# ---------------------------------------------------------------- RL-10, RL-11

def evd_note(subject, obj, predicate="increases"):
    return (f"---\ntype: result\nid: pa:E0001\nsubject: {subject!r}\npredicate: {predicate}\nobject: {obj!r}\n---\n# EVD - Alpha raises Beta\n\n"
            f"{notes.AUTO_START}\n## Finding\n\nAlpha raised Beta.\n{notes.AUTO_END}\n")


def rl10(tmp, subject, back_link=True):
    node = lambda name: generated(name) + ("See [[EVD - Alpha raises Beta]].\n" if back_link else "")
    return (Vault("v", tmp / "v", notes=[("Results/EVD - Alpha raises Beta.md", evd_note(subject, "[[Beta]]")), ("Concepts/Alpha.md", node("Alpha")),
                                       ("Concepts/Beta.md", node("Beta"))]),
            lambda paper_id: {"pa:E0001": "increase"})


CASES["RL-10"] = (lambda tmp: rl10(tmp, "[[Alpha]]"), lambda tmp: rl10(tmp, ""))

GROUP = {"group_id": "mg1", "name": "Cell survival", "members": ["Cell survival rate"], "status": "ai_proposed"}


def rl11(tmp, comparison):
    root = tmp / "s"
    for pid in ("pa", "pb"):
        put(root / "papers" / pid / "metadata.json", json.dumps({"paper_id": pid, "vaults": ["v"], "note_name": f"Paper {pid}"}))
        db.write_jsonl(root / "papers" / pid / "evidence.jsonl", [cg._unit(pid, 1, "Cell survival rate", "increase")])
    text = "---\ntype: comparison\nmeasure_group: mg1\n---\n# Cell survival\n\n[[Paper pa]] and [[Paper pb]] measure it.\n"
    notes_ = [("Comparisons/Cell survival.md", text)] if comparison else []
    return (Store(root=root, paper_ids=["pa", "pb"], vaults=[Vault("v", tmp / "v", notes=notes_)]),
            lambda: {cg.relations._raw_measure_key(m): GROUP for m in GROUP["members"]})


CASES["RL-11"] = (lambda tmp: rl11(tmp, True), lambda tmp: rl11(tmp, False))
