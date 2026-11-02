"""Conformance checks of the stages ND (concepts and nodes) and RL (relations, conflicts, typed network) of
config/standard.yaml, see conform.py.

ND: ND-01..ND-04 and ND-08 re-use the product functions on the registry and the links (homonym_blocked,
abbreviation_pairs, build_registry, the linker) with built-in synthetic cases, plus data checks of
`concepts.jsonl`, `concept_links` and the vault node notes; ND-05 / ND-06 judge the node notes of a vault with
`concept_notes.shown_in_vault`; ND-07 wraps `selftest.check_concepts`.
RL: data checks over `relations.jsonl`, the CONFLICT / EVD notes and `gap_candidates.jsonl`, plus probes of
`relations.propose`, `gaps.generate` and the vault index of `neural.py` that run in a throw-away project.
RL-01, RL-09, RL-10 and RL-11 are written against the target data model of the typed network (edges with `etype` and
`unit_id` in the neural vault index, EVD notes with subject / predicate / object that are real node links and that the
node lists, `Comparisons/` notes of type `comparison` with `measure_group`), so they report today's gaps as violations.

Not implemented / only partly checked, and why:
- ND-01: whether a MeSH sense fits a paper cannot be decided by code; only the homonym list is checked (the cases of
  the clause, and the links still starting at a blocked sentence).
- ND-04: the phrase fragments of the clause (Mater from dura mater) have no list to test against; the check covers
  generic terms (registry and node notes) and organism words that exist only inside reagent phrases.
- RL-03: only bundled measures are checked (data and probe); rule A (no clause direction from `non_outcome_words`
  text) is covered by unit tests, not by this check.
- RL-08: "a gap is promoted only by the researcher" cannot be seen in the data (no field says who set a status).
- RL-10: "a unit without an outcome has no EVD note" is enforced by result_notes (no note is made), not read back here.
"""
import contextlib
import json
import re
from pathlib import Path
from unittest import mock

from . import concept_notes, concepts, config, db, gaps, neural, notes, presentation, relations, result_notes, selftest, standard, structure
from .conform import check
from .conform_intake import _seed, _sandbox
from .conform_profile import ABSENCE_CLAIM, DENIAL

# ---------------------------------------------------------------- helpers


def _fold(name):
    """The registry merge key with spaces, hyphens and apostrophes ignored: the names of one concept written two ways."""
    return re.sub(r"[\s'’ʼ`-]+", "", concepts._key(name))


def _record_note_index(registry):
    """(node folder, lower-case note name) -> registry record; the first record of a clashing name keeps it (as the writer does)."""
    index = {}
    for record in registry.records:
        index.setdefault((notes.TERM_FOLDERS[concepts.TYPE_KEYS[record["type"]]], notes.safe_filename(record["name"]).casefold()), record)
    return index


def _node_notes(vault, registry):
    """(path, text, record) of every machine-owned node note of a vault that belongs to a registry concept."""
    index = _record_note_index(registry)
    for path, text in vault.notes:
        parts = path.split("/")
        if len(parts) != 2 or parts[0] not in notes.NODE_FOLDERS or parts[1][:-3] == notes.LIST_NOTES.get(parts[0]) or notes.INDEX_MARK in text:
            continue
        record = index.get((parts[0], parts[1][:-3].casefold()))
        if record:
            yield path, text, record


def _frontmatter(text):
    fm, _ = notes._split_frontmatter(text)
    return fm if isinstance(fm, dict) else {}


def _auto_block(text):
    start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
    return text[start:end] if 0 <= start < end else ""


def _has_content(text):
    """A generated note has at least one `## ` section inside its auto block; a stub or a retired notice has none."""
    return re.search(r"(?m)^## \S", _auto_block(text)) is not None


def _mentions(links):
    rows = [r for group in links.values() for r in group] if isinstance(links, dict) else list(links or ())
    return sum(int(r.get("n_sentences") or 0) for r in rows)


@contextlib.contextmanager
def _world(registry=()):
    """_sandbox with the real word lists (synonyms, generic terms, homonyms) and a registry file for the throw-away store."""
    real = config.CONFIG_DIR
    with _sandbox() as (root, con):
        config.CONFIG_DIR = real  # restored by _sandbox on exit
        if registry:
            db.write_jsonl(config.STORE / "concepts.jsonl", [dict({"synonyms": [], "status": "ai_proposed", "mesh_ui": None,
                                                                    "mesh_tree_numbers": [], "parents": []}, **r) for r in registry])
        concepts.invalidate_registry()
        try:
            yield root, con
        finally:
            concepts.invalidate_registry()


def _sentence_rows(pid, texts, section="results"):
    return [{"sentence_id": f"{pid}:S{n:04d}", "seq": n, "passage_id": None, "source": "main", "page": 1, "section": section,
             "subsection": None, "kind": "text", "text": text} for n, text in enumerate(texts, 1)]


def _put_paper(con, pid, vaults, sentences=(), terms=(), structure=None, units=()):
    """One throw-away paper (files and database rows) with sentences, concept terms, abbreviations and evidence units."""
    folder = db.paper_dir(pid)
    db.write_jsonl(folder / "sentences.jsonl", _sentence_rows(pid, sentences))
    db.write_json(folder / "analysis.json", {"claims": [], "concepts": list(terms)})
    if structure:
        db.write_json(folder / "structure.json", structure)
    db.write_jsonl(folder / "evidence.jsonl", list(units))
    _seed(con, pid, vaults)


def _unit(pid, n, measure, direction, agent=None, cell_type=None, species=None, unit_type="result", terms=None, statement=None):
    return {"unit_id": f"{pid}:E{n:04d}", "seq": n, "unit_type": unit_type, "status": "ok",
            "statement": statement or f"{measure} was {direction}.", "quote": "", "sentence_ids": [f"{pid}:S0001"],
            "pages": [1], "sections": ["results"], "outcome": {"measure": measure, "direction": direction},
            "intervention": {"agent": agent}, "model": {"species": species, "cell_type": cell_type},
            **({"terms": terms} if terms else {})}


def _concept(name, type_="concept", **extra):
    return {"concept_id": "local:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-"), "name": name, "type": type_, **extra}


# ---------------------------------------------------------------- ND-01: homonyms

HOMONYM_CASES = (  # (node name, sentence, must be blocked)
    ("GO", "Gene Ontology enrichment of the genes was done.", True),
    ("GO", "Graphene oxide (GO) was added to the medium.", False),
    ("SIRT", "Slices were rebuilt by an iterative reconstruction step in tomography.", True),
    ("SIRT", "SIRT activity was measured in cells.", False),
    ("PCA", "Groups were compared by principal component analysis (PCA).", True),
    ("PCA", "Passive cutaneous anaphylaxis (PCA) was induced in mice.", False),
    ("Vitrification", "The glass transition temperature of the polymer was measured.", True),
    ("Vitrification", "Oocytes were vitrified in a cryoprotectant solution.", False),
)


@check("ND-01", "store")
def senses_follow_the_homonym_list(store, judge=presentation.homonym_blocked):
    """Data: a link of a node that config/homonyms.yaml has rules for must not start at a sentence the list blocks (a stale
    link). Probe: `judge` blocks the other-field senses of the clause (Vitrification / glass transition, PCA / principal
    component analysis, SIRT / reconstruction, GO / Gene Ontology) and still lets the right sense through."""
    rules = config._read_yaml("homonyms.yaml")
    ruled = {str(name).strip().casefold() for name in (rules if isinstance(rules, dict) else {})}
    for record in store.concepts:
        if record["name"].casefold() in ruled:
            for paper_id, text in store.con.execute(
                    "SELECT c.paper_id, s.text FROM concept_links c JOIN sentences s ON s.sentence_id = c.first_sentence_id "
                    "WHERE c.concept_id = ?", (record["concept_id"],)):
                if judge(record["name"], text):
                    yield f"{record['concept_id']} is linked in {paper_id} at a sentence that homonyms.yaml blocks"
    for name, text, blocked in HOMONYM_CASES:
        if bool(judge(name, text)) != blocked:
            yield f"probe: node {name!r} is {'not blocked' if blocked else 'blocked'} for {text!r}"


# ---------------------------------------------------------------- ND-02: abbreviations defined by a paper

ABBREVIATION_PAPERS = (  # (paper, structure.json abbreviations)
    ("pa", [{"short": "AMI", "definitions": [{"text": "acute myocardial infarction"}]},
            {"short": "CPA", "definitions": [{"text": "cryoprotective agent"}]}]),
    ("pb", [{"short": "CPA", "definitions": [{"text": "cyclophosphamide"}]}]))


@check("ND-02", "store")
def defined_acronyms_are_one_concept(store, pairs=concepts.abbreviation_pairs):
    """Data: every (acronym, expansion) pair that `pairs` finds in the papers is one registry concept, and in each paper that
    defines it the acronym sentences are all linked to it. Probe: `pairs` merges AMI with its expansion and does not merge
    CPA, which two papers expand in two ways."""
    registry = concepts.Registry(store.concepts)
    known = {concepts._key(v) for r in store.concepts for v in (r["name"], *r.get("synonyms", []))}
    for short, long in pairs(known):
        if registry.by_key.get(short) is not registry.by_key.get(long):
            yield f"the pair {short!r} = {long!r} defined by a paper is two registry concepts"
    for paper_id in store.paper_ids:
        paper, rows = store.paper(paper_id), None
        for row in paper.structure.get("abbreviations") or []:
            record = registry.lookup(row.get("short") or "")
            if not record or not any(registry.lookup(d.get("text") or "") is record for d in row.get("definitions") or []):
                continue
            if rows is None:
                rows = [s for s in paper.sentences if s.get("section") != "references" and (s.get("kind") == "table_row" or not s.get("in_table"))]
            short = concepts._regex(row["short"], concepts._case_sensitive(row["short"]))
            n = sum(1 for s in rows if short.search(s.get("text") or ""))
            linked = store.con.execute("SELECT n_sentences FROM concept_links WHERE concept_id = ? AND paper_id = ?",
                                       (record["concept_id"], paper_id)).fetchone()
            if n and (not linked or linked[0] < n):
                yield f"{paper_id}: {n} sentences use {row['short']!r}, node {record['concept_id']} links {linked[0] if linked else 0}"
    with _sandbox(vaults=()) as (root, con):
        for pid, abbreviations in ABBREVIATION_PAPERS:
            db.write_json(db.paper_dir(pid) / "structure.json", {"abbreviations": abbreviations})
        probe_known = {concepts._key(v) for v in ("AMI", "acute myocardial infarction", "CPA", "cryoprotective agent", "cyclophosphamide")}
        found = set(pairs(probe_known))
    if (concepts._key("AMI"), concepts._key("acute myocardial infarction")) not in found:
        yield "probe: AMI defined as acute myocardial infarction is not merged"
    if any(concepts._key("CPA") == short for short, _ in found):
        yield "probe: CPA, expanded in two ways by two papers, is merged"


# ---------------------------------------------------------------- ND-03: variants are one node

VARIANT_GROUPS = (("m6A", "m 6A"), ("RT-qPCR", "RTqPCR", "rt qpcr"), ("beta-tubulin", "β-tubulin", "Beta-tubulin"),
                  ("Blastocoel", "Blastocoele"), ("Lyve-1", "Lyve1", "LYVE-1"),
                  ("Fisher's exact test", "Fisher’s exact test"))


def _variants_merge(build, groups):
    """The groups of variants that `build` (concepts.build_registry) leaves in more than one registry concept."""
    with _world() as (root, con):
        for n, variant in enumerate(v for group in groups for v in group):
            con.execute("INSERT INTO paper_terms VALUES (?, 'concepts', ?)", (f"p{n}", variant))
        registry = build(con)
        for group in groups:
            found = {(registry.lookup(v) or {}).get("concept_id", v) for v in group}  # a variant the registry lost is a concept of its own
            if len(found) > 1:
                yield group, found


@check("ND-03", "store")
def variants_merge_into_one_node(store, build=concepts.build_registry):
    """Data: no two names of `concepts.jsonl` are one name once case, spaces, hyphens, Greek letters and apostrophes are
    ignored. Probe: `build` merges the variants the clause lists (case, hyphen, spaces between letters, Greek symbol versus name)."""
    names = {}
    for record in store.concepts:
        names.setdefault(_fold(record["name"]), set()).add((record["concept_id"], record["name"]))
    for key, group in sorted(names.items()):
        if len(group) > 1:
            yield "one concept written two ways: " + " | ".join(f"{name!r} ({cid})" for cid, name in sorted(group))
    for group, found in _variants_merge(build, VARIANT_GROUPS):
        yield f"probe: {' / '.join(group)} give {len(found)} concepts"


# ---------------------------------------------------------------- ND-04: no generic or fragment nodes

def _reagent_only(store, record):
    """True when a node of an organism word is linked (first sentence) only inside reagent phrases (fetal bovine serum ...)."""
    pattern = concepts._regex(record["name"])
    rows = [r[0] for r in store.con.execute(
        "SELECT s.text FROM concept_links c JOIN sentences s ON s.sentence_id = c.first_sentence_id WHERE c.concept_id = ?",
        (record["concept_id"],))]
    return bool(rows) and all(
        all(any(s <= m.start() and m.end() <= e for s, e in presentation.species_reagent_spans(text)) for m in pattern.finditer(text))
        for text in rows if pattern.search(text))


@check("ND-04", "store")
def no_generic_nodes(store):
    """Registry names and node notes of a vault that equal a term of config/generic_terms.yaml (a researcher-confirmed node
    stays); nodes named by an organism word of a reagent phrase whose links all sit inside such phrases."""
    confirmed = {concepts._key(r["name"]) for r in store.concepts if r.get("status") == "human_confirmed"}
    generic = concepts.generic_keys() - confirmed
    for record in store.concepts:
        if concepts._key(record["name"]) in generic:
            yield f"registry node {record['concept_id']} is the generic term {record['name']!r}"
    words = {w for phrase in presentation.SPECIES_REAGENT_PHRASES for w in phrase.split()}
    for record in store.concepts:
        if record["type"] in ("species", "cell_type", "concept") and record["name"].casefold() in words and _reagent_only(store, record):
            yield f"registry node {record['concept_id']} comes only from reagent phrases"
    for vault in store.vaults:
        for folder in notes.NODE_FOLDERS:
            directory = vault.root / folder
            for path in sorted(directory.glob("*.md")) if directory.is_dir() else ():
                if concepts._key(path.stem) in generic:
                    yield f"{vault.name}/{folder}/{path.name}: a node note for the generic term {path.stem!r}"


# ---------------------------------------------------------------- ND-05 / ND-06: shown nodes

def _states(vault, registry, collect, shown):
    """(path, text, record, links, units, passes) of every node note of the vault."""
    with structure.stamp_scope():  # paper file stamps are read once per paper, as concept_notes.refresh does
        for path, text, record in _node_notes(vault, registry):
            _, links, units = collect(vault.con, record, registry, vault.name)
            yield path, text, record, links, units, shown(links, units)


@check("ND-05", "vault")
def shown_nodes_meet_the_threshold(vault, registry=None, collect=concept_notes._collect, shown=concept_notes.shown_in_vault):
    """Every node note of the vault (generated or stub) belongs to a node with at least min_evidence evidence units or
    min_mentions mentions in this vault's papers (`collect` = concept_notes._collect), and `shown` (shown_in_vault) agrees."""
    registry = registry or concepts.load_registry(vault.con)
    min_units, min_mentions = standard.param("ND-05", "min_evidence"), standard.param("ND-05", "min_mentions")
    for path, text, record, links, units, passes in _states(vault, registry, collect, shown):
        meets = len(units) >= min_units or _mentions(links) >= min_mentions
        if not meets:
            yield f"{path}: {len(units)} evidence units and {_mentions(links)} mentions are below the threshold"
        elif not passes:
            yield f"{path}: shown_in_vault hides a node with {len(units)} evidence units and {_mentions(links)} mentions"


@check("ND-06", "vault")
def no_orphan_or_empty_nodes(vault, registry=None, collect=concept_notes._collect, shown=concept_notes.shown_in_vault):
    """Every node note of the vault has source sentences in this vault (links or evidence units) and a content section."""
    registry = registry or concepts.load_registry(vault.con)
    for path, text, record, links, units, passes in _states(vault, registry, collect, shown):
        if not _has_content(text):
            yield f"{path}: empty note ({len(text.encode('utf-8'))} bytes, no generated section)"
        if not units and not _mentions(links):
            yield f"{path}: no source sentence of this vault"


# ---------------------------------------------------------------- ND-07: concept links

@check("ND-07", "store", via_selftest=True)
def concept_ids_and_links(store, run=selftest.check_concepts):
    """selftest.check_concepts: unique concept_id and the relink_paper comparison of every link; data: no link built from a
    references section."""
    report = selftest.Report()
    run(report, store.con)
    yield from (e for e in report.errors if e.startswith("concept_links:") or "concept_id must be unique" in e)
    for concept_id, paper_id, sections in store.con.execute("SELECT concept_id, paper_id, sections FROM concept_links"):
        if "references" in json.loads(sections or "[]"):
            yield f"concept_links: {concept_id} / {paper_id} is built from the references section"


# ---------------------------------------------------------------- ND-08: acronym case

@check("ND-08", "probe")
def probe_acronym_case(build=concepts.build_registry, match=concepts._pattern_matches):
    """EG does not match eg; ITS (verbatim in the paper) beats the pronoun Its as the node name and the pronoun never links."""
    record = {"name": "EG", "type": "molecule"}
    pattern = concepts._regex("EG", concepts._case_sensitive("EG"))
    if match("Cells were frozen in eg medium.", pattern, record):
        yield "EG matches the lower-case word eg"
    if not match("Cells were frozen in EG medium.", pattern, record):
        yield "EG does not match EG"
    with _world() as (root, con):
        _put_paper(con, "pa", ["one"], ["ITS medium was used for the cells.", "Its effect was clear.", "ITS supplement was added."], terms=["ITS", "Its"])
        registry = build(con)
        its = registry.lookup("ITS")
        if not its or its["name"] != "ITS":
            yield f"ITS is named {its and its['name']!r}"
        elif any(v.casefold() == "its" for v in its["synonyms"]):
            yield "the pronoun Its is a synonym of ITS"
        elif match("Its effect was clear.", concepts._regex("ITS", True), its):
            yield "the pronoun Its links to ITS"


# ================================================================ RL: relations, conflicts, typed network

# ---------------------------------------------------------------- RL-01: typed, sourced edges

INDEX_FILES = {  # a throw-away vault: a paper note, two nodes, one EVD note naming them
    "Papers/Paper One.md": "---\ntype: paper\npaper_id: p1\n---\n# Paper One\n\nThis paper uses [[Alpha]].\n",
    "Concepts/Alpha.md": "# Alpha\n\nA node.\n",
    "Concepts/Beta.md": "# Beta\n\nA node.\n",
    "Results/EVD - Alpha raises Beta.md": (
        "---\ntype: result\nid: p1:E0001\nsource: '[[Paper One]]'\nsubject: '[[Alpha]]'\npredicate: increases\nobject: '[[Beta]]'\n"
        "sentence_ids:\n- p1:S0001\n---\n# EVD - Alpha raises Beta\n\n## Finding\n\nAlpha raised Beta.\n")}


def _may_navigate(texts):
    """Whether a result -> node edge may be nav (D-PENDING-21), read from the note text {path: text} by the checker itself (not
    neural._edge_rows): the result is not significant (B3), or its link text stands in the body outside the single kb:auto block (B1)."""
    def allowed(edge):
        text = texts.get(edge["source"]) or ""
        front, end = neural._front(text)
        body = text[end:]
        s, e = body.find(notes.AUTO_START), body.find(notes.AUTO_END)
        outside = body[:s] + body[e:] if body.count(notes.AUTO_START) == body.count(notes.AUTO_END) == 1 and 0 <= s < e else body
        return result_notes.is_not_significant(front.get("significance")) or f"[[{edge.get('label')}]]" in outside
    return allowed


def _edge_problems(columns, edges, etypes, has_sentences, may_navigate=lambda edge: False):
    """The problems of the rows of a vault index `edges` table (`edges`: list of dicts)."""
    if not {"etype", "unit_id", "sentence_ids"} <= set(columns):
        yield f"the edges table has columns {sorted(columns)}: no etype / unit_id / sentence_ids, so no edge is typed or sourced"
        return
    for edge in edges:
        where = f"edge {edge['source']} -> {edge['target']}"
        if edge["etype"] not in etypes:
            yield f"{where}: etype {edge['etype']!r} is not in the agreed list"
        elif (edge["etype"] == "nav" and edge["source"].startswith("Results/") and edge["status"] == "resolved"
              and edge["target"].split("/")[0] in set(notes.NODE_FOLDERS) - {notes.TERM_FOLDERS["topics"]}  # not a figure, not a topic map
              and not may_navigate(edge)):
            yield f"{where}: a link from a result note to a node is typed nav"
        elif edge["etype"] != "nav" and not (edge["unit_id"] and json.loads(edge["sentence_ids"] or "[]") and has_sentences(edge["unit_id"])):
            yield f"{where}: knowledge edge {edge['etype']} cites no evidence unit with sentences"


def _index_of(files):
    """Build the vault index of a throw-away vault holding `files` {relative path: text}: (edge columns, edge rows)."""
    with _world() as (root, con):
        for rel, text in files.items():
            path = config.vault_dir("one") / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        neural.refresh("one")
        index = neural._cache("one", readonly=True)
        try:
            return [r["name"] for r in index.execute("PRAGMA table_info(edges)")], [dict(r) for r in index.execute("SELECT * FROM edges")]
        finally:
            index.close()


@check("RL-01", "store")
def edges_are_typed_and_sourced(store, build=_index_of, open_index=neural._cache):
    """Data: the vault index (cache/vault_index/<vault>.sqlite, read-only) has an etype from the agreed list on every edge and a
    unit_id with sentences on every knowledge edge (`open_index` = neural._cache), and no result -> node link typed nav unless
    `_may_navigate` allows it; nothing is reported for a vault without an index. Probe: `build` makes the
    index of a small vault and the same rules apply to it."""
    etypes = set(standard.param("RL-01", "etypes"))

    def has_sentences(unit_id):
        row = store.con.execute("SELECT sentence_ids FROM evidence WHERE unit_id = ?", (unit_id,)).fetchone()
        return bool(row and json.loads(row[0] or "[]"))
    for vault in store.vaults:
        try:
            index = open_index(vault.name, readonly=True)
        except FileNotFoundError:
            continue
        try:
            columns = [r["name"] for r in index.execute("PRAGMA table_info(edges)")]
            edges = [dict(r) for r in index.execute("SELECT * FROM edges")] if "etype" in columns else []
            texts = {r["path"]: r["text"] for r in index.execute("SELECT path, text FROM notes WHERE path LIKE 'Results/%'")}
        finally:
            index.close()
        for problem in _edge_problems(columns, edges, etypes, has_sentences, _may_navigate(texts)):
            yield f"{vault.name}: {problem}"
    columns, edges = build(INDEX_FILES)
    for problem in _edge_problems(columns, edges, etypes, lambda unit_id: unit_id == "p1:E0001"):
        yield f"probe: {problem}"
    if "etype" in columns and not any(e["source"].startswith("Results/") and e["etype"] != "nav" for e in edges):
        yield "probe: no edge of the result note is a knowledge edge"


# ---------------------------------------------------------------- RL-02, RL-03, RL-05, RL-07: relations

BUNDLE = re.compile(r",|;|\s(?:and|or|&)\s", re.I)  # a measure that names several outcomes


def _live(store):
    return [r for r in store.relations if r.get("type") in relations.CONFLICT_TYPES and r.get("status") in relations.ACTIVE_RELATION_STATUSES]


def _measure_key(value):
    return relations._raw_measure_key(value)


def _reduced_measure(value, concept, quantity):
    """The checker's own reduction (not the product's): the measure without the shared concept's name and synonyms and the quantity words."""
    text = str(value or "")
    for name in ((concept or {}).get("name"), *((concept or {}).get("synonyms") or ())):
        if name:
            text = re.sub(r"(?<!\w)" + re.escape(str(name)) + r"(?!\w)", " ", text, flags=re.I)
    return " ".join(word for word in _measure_key(text).split() if word not in quantity)


@contextlib.contextmanager
def _relations_world(registry=()):
    """A throw-away project whose relations.jsonl and measure_groups.json are its own (relations.RELATIONS is bound at import)."""
    with _world(registry) as (root, con):
        with mock.patch.object(relations, "RELATIONS", config.STORE / "relations.jsonl"), \
                mock.patch.object(relations, "MEASURE_GROUPS", config.STORE / "measure_groups.json"):
            assert relations.RELATIONS.resolve().is_relative_to(root), "the probe would write outside its throw-away project"
            yield root, con


def _propose_rows(papers, propose, registry=(), vault=None):
    """Run `propose` over throw-away papers {paper: (vaults, [units])}; the relation rows it writes."""
    with _relations_world(registry) as (root, con):
        for pid, (vaults, units) in papers.items():
            _put_paper(con, pid, vaults, ["Cells were measured."], units=units)
        propose(con, vault)
        return list(db.read_jsonl(relations.RELATIONS))


def _pairs(rows):
    return {tuple(sorted((r["from_unit"], r["to_unit"]))) for r in rows}


@check("RL-02", "store")
def contradictions_have_one_outcome_and_agent(store, propose=relations.propose):
    """Data: every live conflict relation has equal measures (same text or a reviewed measure group), no agent difference,
    different outcome directions, and its shared concept is a measured outcome (no species, method or generic word). Probe:
    with equal measure and agent a pair is proposed; with another agent, or with a species name as the measure, none is."""
    registry = concepts.Registry(store.concepts)
    quantity = {_measure_key(word) for word in standard.param("RL-02", "outcome_quantity_words")}
    for r in _live(store):
        who = f"relation {r.get('relation_id')} ({r.get('type')})"
        concept = registry.by_id.get(r.get("shared_concept_id"))
        # what the relation compares: compared_measures when the row has them, else the two unit measures
        first, second = r.get("compared_measures") or [r.get("outcome_measure"), r.get("outcome_measure_other")]
        if (_reduced_measure(first, concept, quantity) != _reduced_measure(second, concept, quantity)
                and not r.get("measure_group")):
            yield f"{who}: different measures {str(first)[:50]!r} and {str(second)[:50]!r}"
        for d in r.get("differences") or []:
            if d.get("field") == "intervention.agent":
                yield f"{who}: different agents {d.get('from')!r} and {d.get('to')!r}"
        directions = r.get("directions") or []
        if len(directions) != 2 or not all(directions) or directions[0] == directions[1]:
            yield f"{who}: directions {directions} are not opposite"
        elif r.get("type") == "contradicts" and set(directions) != {"increase", "decrease"}:
            yield f"{who}: contradicts with directions {directions}"
        if concept and relations._is_stoplisted_measure_concept(concept):
            yield f"{who}: the shared {concept['name']!r} ({concept['type']}) is not a measured outcome"
    papers = {"pa": (["one"], [_unit("pa", 1, "Cell survival rate", "increase", "Agent A", "Oocyte")]),
              "pb": (["one"], [_unit("pb", 1, "Cell survival rate", "decrease", "Agent A", "Oocyte")]),
              "pc": (["one"], [_unit("pc", 1, "Spindle length", "increase", "Agent A", "Oocyte")]),
              "pd": (["one"], [_unit("pd", 1, "Spindle length", "decrease", "Agent B", "Oocyte")]),
              "pe": (["one"], [_unit("pe", 1, "Human", "increase", "Agent A", "Oocyte")]),
              "pf": (["one"], [_unit("pf", 1, "Human", "decrease", "Agent A", "Oocyte")])}
    pairs = _pairs(_propose_rows(papers, propose, registry=[_concept("Human", "species")]))
    if ("pa:E0001", "pb:E0001") not in pairs:
        yield "probe: equal measure, equal agent and opposite directions give no proposal"
    if ("pc:E0001", "pd:E0001") in pairs:
        yield "probe: two different agents on one measure are paired"
    if ("pe:E0001", "pf:E0001") in pairs:
        yield "probe: a species name alone is taken as the measured outcome"


@check("RL-03", "store")
def bundled_conclusions_are_split(store, propose=relations.propose):
    """Data: no live relation pairs a measure that names several outcomes at unit level (only a clause-level or equal-measure
    pair is allowed). Probe: a unit whose measure lists three outcomes gives no unit-level pair with a unit on one of them."""
    for r in _live(store):
        first, second = r.get("outcome_measure"), r.get("outcome_measure_other")
        if _measure_key(first) != _measure_key(second) and str(r.get("match_basis") or "").startswith("shared_concept:"):
            for measure in (first, second):
                if BUNDLE.search(str(measure or "")):
                    yield f"relation {r.get('relation_id')}: the bundled measure {str(measure)[:60]!r} is paired as a whole ({r.get('match_basis')})"
    bundled = "Implantation, pregnancy, and live pup frequency"
    papers = {"pg": (["one"], [_unit("pg", 1, bundled, "decrease", "Agent A", "Embryo")]),
              "ph": (["one"], [_unit("ph", 1, "Pregnancy", "increase", "Agent A", "Embryo")])}
    rows = _propose_rows(papers, propose, registry=[_concept("Implantation"), _concept("Pregnancy")])
    if any(str(r.get("match_basis") or "").startswith("shared_concept:") for r in rows):
        yield "probe: a bundled conclusion of three outcomes is paired with one of them at unit level"


@check("RL-05", "store")
def relations_stay_in_their_vault(store, propose=relations.propose):
    """Data: every live conflict relation names a vault that both of its papers belong to. Probe: a run for vault one leaves
    the rows of vault two byte-identical (even a stale-looking one) and writes only rows of vault one; conflict_relations('one')
    does not return vault two; list_conflicts of an unknown vault errors."""
    for r in _live(store):
        who = f"relation {r.get('relation_id')}"
        if not r.get("vault"):
            yield f"{who}: no vault"
            continue
        for unit in (r["from_unit"], r["to_unit"]):
            if r["vault"] not in (store.paper(unit.rsplit(":", 1)[0]).meta.get("vaults") or []):
                yield f"{who}: {unit} is not in vault {r['vault']}"
    papers = {"pa": (["one"], [_unit("pa", 1, "Cell survival rate", "increase", "Agent A")]),
              "pb": (["one"], [_unit("pb", 1, "Cell survival rate", "decrease", "Agent A")]),
              "pc": (["two"], [_unit("pc", 1, "Spindle length", "increase", "Agent A")]),
              "pd": (["two"], [_unit("pd", 1, "Spindle length", "decrease", "Agent A")])}
    stale = {"relation_id": "stale", "from_unit": "pc:E0001", "to_unit": "pd:E0001", "type": "direction_differs",
             "status": "ai_proposed", "vault": "two", "match_basis": "shared_concept:local:gone", "directions": ["increase", "decrease"]}
    with _relations_world() as (root, con):
        for pid, (vaults, units) in papers.items():
            _put_paper(con, pid, vaults, ["Cells were measured."], units=units)
        db.write_jsonl(relations.RELATIONS, [stale])
        propose(con, "one")
        rows = list(db.read_jsonl(relations.RELATIONS))
        if [r for r in rows if r.get("relation_id") == "stale"] != [stale]:
            yield "probe: a run for vault one changed the relation of vault two"
        mine = [r for r in rows if r.get("relation_id") != "stale"]
        if not mine or any(r.get("vault") != "one" for r in mine):
            yield f"probe: a run for vault one wrote {len(mine)} relations, vaults {sorted({str(r.get('vault')) for r in mine})}"
        if any(r.get("relation_id") == "stale" for r in relations.conflict_relations("one")):
            yield "probe: conflict_relations('one') returns a relation of vault two"
        from . import mcp_server
        try:
            mcp_server._conflicts("no-such-vault", con)
            yield "probe: list_conflicts of an unknown vault does not error"
        except ValueError:
            pass


@check("RL-07", "probe")
def probe_human_decisions_survive(propose=relations.propose):
    """A relation a researcher confirmed or rejected is byte-identical after propose, and the pair gets no second relation."""
    papers = {"pa": (["one"], [_unit("pa", 1, "Cell survival rate", "increase", "Agent A", "Oocyte"),
                               _unit("pa", 2, "Spindle length", "increase", "Agent A", "Oocyte")]),
              "pb": (["one"], [_unit("pb", 1, "Cell survival rate", "decrease", "Agent A", "Oocyte"),
                               _unit("pb", 2, "Spindle length", "decrease", "Agent A", "Oocyte")])}
    mine = [{"relation_id": f"h{n}", "from_unit": f"pa:E000{n}", "to_unit": f"pb:E000{n}", "type": "direction_differs", "status": status,
             "vault": "one", "rationale": "my own words", "researcher_note": "keep this", "directions": ["increase", "decrease"]}
            for n, status in ((1, "human_confirmed"), (2, "rejected"))]
    with _relations_world() as (root, con):
        for pid, (vaults, units) in papers.items():
            _put_paper(con, pid, vaults, ["Cells were measured."], units=units)
        db.write_jsonl(relations.RELATIONS, mine)
        propose(con, "one")
        rows = list(db.read_jsonl(relations.RELATIONS))
    for row in mine:
        if row not in rows:
            yield f"probe: the {row['status']} relation {row['relation_id']} was changed by propose"
    if len(rows) != len(mine):
        yield f"probe: propose added {len(rows) - len(mine)} relations for pairs a researcher decided"


# ---------------------------------------------------------------- RL-04, RL-06: conflict notes

SPECIES_ROW = re.compile(r"(?m)^\| Species \| (.*?) \| (.*?) \|\s*$")


def _conflict_notes(vault):
    for path, text in vault.notes:
        fm = _frontmatter(text)
        if fm.get("type") == "conflict":
            yield path, text, fm


@check("RL-04", "vault")
def conflict_titles_name_both_species(vault):
    """In the first A / B table of every CONFLICT note: the species of each side (its own, a paper-level one marked) is in the
    title, and a side without species does not borrow the other side's."""
    for path, text, fm in _conflict_notes(vault):
        row = SPECIES_ROW.search(text)
        if not row:
            continue
        sides = [None if cell.strip() in ("", "—") else cell.replace(result_notes.PAPER_LEVEL, "").strip() for cell in row.groups()]
        title = str(fm.get("title") or "").casefold()
        for label, species in zip("AB", sides):
            if species and species.casefold() not in title:
                yield f"{path}: side {label} is {species!r}, the title does not name it"
        if sum(1 for s in sides if s) == 1 and any(s.casefold() in title for s in sides if s):
            yield f"{path}: only one side has a species, the title names it for both sides"


@check("RL-06", "vault")
def active_conflicts_are_shown(vault, relations_of=relations.conflict_relations):
    """Every active conflict relation of the vault has a CONFLICT note tagged conflict, listed in `_Maps/Open conflicts.md`;
    an effect_vs_no_effect note carries the non-significance caution."""
    found = list(_conflict_notes(vault))
    open_list = dict(vault.notes).get("_Maps/Open conflicts.md")
    for r in relations_of(vault.name):
        who = f"relation {r.get('relation_id')} ({r.get('type')})"
        mine = [n for n in found if n[2].get("id") == r.get("relation_id") or all(u in n[1] for u in (r["from_unit"], r["to_unit"]))]
        if not mine:
            yield f"{who}: no CONFLICT note"
            continue
        path, text, fm = mine[0]
        if "conflict" not in (fm.get("tags") or []):
            yield f"{who}: {path} lacks the tag conflict"
        if open_list is None or f"[[{Path(path).stem}" not in open_list:
            yield f"{who}: {path} is not in Open conflicts"
        if r.get("type") == "effect_vs_no_effect" and not re.search(r"not (?:an )?evidence of no effect", text, re.I):
            yield f"{who}: {path} carries no non-significance caution"


# ---------------------------------------------------------------- RL-08: gap candidates

def _false_absent(row, registry, texts, index):
    """The entities of an unproven_hypothesis row that a result mentions under another name of the same concept (a synonym or a child)."""
    for entity in row.get("entities") or []:
        record = registry.lookup(entity)
        if record and any(gaps._about(gaps._key(v), texts, index)
                          for cid in concept_notes._descendants(registry, record["concept_id"])
                          for v in (registry.by_id[cid]["name"], *registry.by_id[cid].get("synonyms", [])) if gaps._key(v)):
            yield entity


@check("RL-08", "store")
def gap_candidates_are_scoped(store, scan=gaps._scan, generate=gaps.generate):
    """Data: every row of gap_candidates.jsonl carries the scope note, never says "absent from the literature", and an
    unproven_hypothesis row names no entity that a result mentions under a synonym of the registry. Probe: `generate` on a
    small vault does the same."""
    registry, cache = concepts.Registry(store.concepts), {}
    for row in db.read_jsonl(store.root / "gap_candidates.jsonl"):
        who = f"{row.get('gap_id')} ({row.get('gap_type')}, {row.get('vault')})"
        if "not found in the current vault" not in str(row.get("scope_note") or ""):
            yield f"{who}: no scope note"
        if ABSENCE_CLAIM.search(DENIAL.sub(" ", str(row.get("description") or ""))):
            yield f"{who}: the description speaks of the literature"
        if row.get("gap_type") == "unproven_hypothesis" and row.get("status") != "withdrawn_by_rule":
            if row["vault"] not in cache:
                cache[row["vault"]] = gaps._index(scan(row["vault"])[1])
            for entity in _false_absent(row, registry, *cache[row["vault"]]):
                yield f"{who}: {entity!r} is said to be missing but a result mentions it under a synonym or a child concept"
    papers = {"pa": (["one"], [_unit("pa", 1, "Reactive oxygen species level", "decrease", "Agent A")]),
              "pb": (["one"], [_unit("pb", 1, "ROS", "increase", unit_type="hypothesis", terms={"concepts": ["ROS"]},
                                     statement="ROS may reduce survival of cells."),
                               _unit("pb", 2, "Zorblax", "increase", unit_type="hypothesis", terms={"concepts": ["Zorblax"]},
                                     statement="Zorblax may reduce survival of cells.")])}
    with _world([_concept("Reactive oxygen species", synonyms=["ROS"])]) as (root, con):
        for pid, (vaults, units) in papers.items():
            _put_paper(con, pid, vaults, ["Cells were measured."], units=units)
        generate("one")
        rows = list(db.read_jsonl(gaps.path()))
    entities = [str(e).casefold() for r in rows for e in r.get("entities") or []]
    if "zorblax" not in entities:
        yield "probe: a hypothesis nothing in the vault tests gives no candidate"
    if "ros" in entities:
        yield "probe: ROS is said to be missing although a result measures reactive oxygen species (string-only match)"
    for r in rows:
        if "not found in the current vault" not in str(r.get("scope_note") or ""):
            yield f"probe: gap {r.get('gap_id')} has no scope note"


# ---------------------------------------------------------------- RL-09: hubs out of the expansion

def _hub_search():
    """The notes a one-hop search for 'Zorblax' (a paper note and a result note) reaches in a vault where one node (DNA) is linked from most papers."""
    share, n = standard.param("RL-09", "hub_share_of_papers"), 4
    hub_papers = int(share * n) + 1
    files = {"Concepts/DNA.md": "# DNA\n\nA node.\n", "Concepts/Unique One.md": "# Unique One\n\nA node.\n",
             "Concepts/Plain Neighbour.md": "# Plain Neighbour\n\nA node.\n",
             # the seed's knowledge edges (a kb:auto block of a result note) go to both nodes; its plain body link only navigates
             "Results/Zorblax result.md": f"---\ntype: result\nid: p1:E0001\n---\n# Zorblax result\n\nZorblax. See [[Plain Neighbour]].\n\n"
                                          f"{notes.AUTO_START}\n[[Unique One]] [[DNA]]\n{notes.AUTO_END}\n"}
    for p in range(1, n + 1):
        links = ("[[DNA]] " if p <= hub_papers else "") + ("[[Unique One]]" if p == 1 else "")
        title = "Zorblax study" if p == 1 else f"Report {p}"
        files[f"Papers/{title}.md"] = f"---\ntype: paper\npaper_id: p{p}\n---\n# {title}\n\nThis paper mentions {links}.\n"
    with _world() as (root, con):
        for rel, text in files.items():
            path = config.vault_dir("one") / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8", newline="\n")
        neural.refresh("one")
        return {node["title"] for node in neural.search("one", "Zorblax", hops=1)["nodes"]}


@check("RL-09", "probe")
def probe_hubs_do_not_expand(search=_hub_search):
    """A node linked from more than hub_share_of_papers of the papers is not reached by hop expansion; a node linked from
    one paper still is."""
    reached = search()
    if "Unique One" not in reached:
        yield f"probe: the ordinary neighbour is not reached by the hop ({sorted(reached)})"
    if "DNA" in reached:
        yield "probe: a hub node (DNA, linked from most papers) takes part in the hop expansion"
    if "Plain Neighbour" in reached:
        yield "probe: a node linked from the seed by a plain (navigation) link only is reached by the hop"


# ---------------------------------------------------------------- RL-10: EVD notes

def _unit_directions(paper_id):
    return {u.get("unit_id"): (u.get("outcome") or {}).get("direction") for u in db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl")}


@check("RL-10", "vault")
def result_notes_name_subject_predicate_object(vault, directions=_unit_directions):
    """Every EVD note (type result) has an object, and a subject unless it is the `subject_not_stated` text, that are links
    to existing node notes of the vault, a predicate from `predicates` (`mixed` only for a unit that states a direction),
    and the notes of subject and object link back to it."""
    predicates, not_stated = standard.param("RL-10", "predicates"), standard.param("RL-10", "subject_not_stated")
    node_stems = {}
    for path, text in vault.notes:
        parts = path.split("/")
        if len(parts) == 2 and parts[0] in notes.NODE_FOLDERS:
            node_stems.setdefault(parts[1][:-3].casefold(), (path, text))
    cache = {}
    for path, text in vault.notes:
        fm = _frontmatter(text)
        if fm.get("type") != "result" or fm.get("retired"):  # a retired note kept for its human text is no current EVD note
            continue
        who = f"{path} ({fm.get('id')})"
        for role in ("subject", "object"):
            raw = fm.get(role)  # a scalar (older notes) or a list of items
            items = [str(item or "") for item in (raw if isinstance(raw, list) else [raw])]
            if not items:
                yield f"{who}: empty {role}"
            for value in items:
                link = re.fullmatch(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]", value)
                if not value:
                    yield f"{who}: empty {role}"
                elif role == "subject" and value == not_stated and len(items) == 1:
                    continue
                elif not link or link[1].casefold() not in node_stems:
                    yield f"{who}: {role} {value[:40]!r} is not a link to a node note"
                elif f"[[{Path(path).stem}" not in node_stems[link[1].casefold()][1]:
                    yield f"{who}: node {link[1]!r} does not list this result"
        if not fm.get("predicate"):
            yield f"{who}: no predicate"
        elif fm["predicate"] not in predicates:
            yield f"{who}: predicate {fm['predicate']!r} is not in the agreed list"
        elif fm["predicate"] == "mixed" and fm.get("id"):
            paper_id = str(fm["id"]).rsplit(":", 1)[0]
            if paper_id not in cache:
                cache[paper_id] = directions(paper_id)
            if str(cache[paper_id].get(fm["id"]) or "").casefold() != "mixed":  # not_applicable / unknown is no stated mixed
                yield f"{who}: predicate mixed for a unit without direction"


# ---------------------------------------------------------------- RL-11: comparison notes

@check("RL-11", "store")
def shared_measures_have_comparison_notes(store, groups=relations.load_measure_groups):
    """For every reviewed measure group (measure_groups.json) with result units in two or more papers of a vault, that vault
    has a note of type comparison with `measure_group: <group_id>` that names each of those papers."""
    by_member = groups()
    papers = {}  # (vault, group id) -> {paper id: its note name}
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        for unit in paper.units:
            outcome = unit.get("outcome") or {}
            group = relations.measure_group(outcome.get("measure"), by_member) if unit.get("unit_type") == "result" and outcome.get("measure") else None
            if group:
                for vault in paper.meta.get("vaults") or []:
                    papers.setdefault((vault, group["group_id"]), {})[paper_id] = paper.meta.get("note_name") or paper_id
    vaults = {v.name: v for v in store.vaults}
    for (vault, group_id), members in sorted(papers.items()):
        if len(members) < 2:
            continue
        found = [text for path, text in (vaults[vault].notes if vault in vaults else ())
                 if _frontmatter(text).get("type") == "comparison" and _frontmatter(text).get("measure_group") == group_id]
        if not found:
            yield f"{vault}: measure group {group_id} has {len(members)} papers and no comparison note"
        else:
            for paper_id, name in members.items():
                if not any(paper_id in text or f"[[{name}" in text for text in found):
                    yield f"{vault}: the comparison note of {group_id} does not list {paper_id}"
