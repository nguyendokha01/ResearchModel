"""Conformance checks of the stages RT (retrieval) and AN (answers and the answer checker) of config/standard.yaml, see conform.py.

Almost every rule is about code behaviour, so almost every check is a PROBE: a small synthetic library (invented
sentences) is built in a throw-away project (`conform_intake._sandbox`) and the product function runs on it, or items
and answer sentences are handed straight to `answer.check_sentence`. A probe takes the judge as an optional argument so a
test can pass a deliberately broken one. A judge that crashes is reported as a violation, never as a PASS.

RT-10, AN-17 and AN-18 also read stored artifacts when they exist (the answer cache `cache/answers/*.json`, one file at a
time, and the notes under `<vault>/Discussions/`).

Reply keys the probes expect (approved 2026-10-18):
- RT-05: `find.search` returns `not_found: True` and `best_groups_matched: <int>` when the best hit matches fewer than
  `min_group_coverage` of the keyword groups.
- AN-13: `ask_prepare` and `ask_check` both return `items_digest` (same value although the library changed in between:
  ask_check uses the items ask_prepare kept) and `ask_check` returns `markdown` with the reason of every struck sentence.

Only partly checked, and why:
- RT-09: "the answer protocol step 1 is followed" is a process of the AI session; the tools that serve it are checked
  (context widening, deep search, deep answer items).
- AN-04: whether a re-citation sits "in the sentence actually used" cannot be probed, an item carries the citations of
  the whole unit without saying which sentence they belong to.
- AN-15 / AN-17: that general knowledge is labelled and that a gap is saved only when the researcher agrees are
  behaviours of the AI session; the wording of the protocol, the rendering of the parts and the saved notes are checked.
- AN-16: only the retrieval and the comparison table of two opposed findings are probed; that an AI sentence does not
  pick a winner silently needs a reading of the text.
"""
import contextlib
import json
import re
import unicodedata
from unittest import mock

import yaml

from . import agent_tasks, answer, config, db, discussions, find, overlap, sources_misc, standard, vectors
from .conform import check
from .conform_intake import _sandbox

SKIPPED = ("references", "front_matter", "acknowledgements", "funding", "author_contributions", "conflicts",
           "data_availability", "notes")  # RT-08, as the clause lists them (find.SKIP_SECTIONS is the product's copy)
NOT_STUDIED = re.compile(r"chưa (?:từng |được |có )?(?:ai )?nghiên cứu|chưa có nghiên cứu|không có nghiên cứu nào|"
                         r"(?:not|never) (?:been )?studied|no stud(?:y|ies)", re.I)
CHAIN_ROWS = ("answer", "sentences", "reasoning", "can_conclude", "cannot_conclude")  # lists of checked sentences in a cached answer


# ---------------------------------------------------------------- the throw-away library

@contextlib.contextmanager
def _lab():
    """A throw-away project with vaults `one` and `two`, no meaning index and the answer cache inside it."""
    with _sandbox() as (root, con), mock.patch.object(vectors, "index_ready", return_value=False), \
            mock.patch.object(answer, "ANSWER_CACHE", root / "answers"):
        cfg = config.load()
        cfg["ingest"]["default_vault"] = "one"
        yield root, con, cfg


def _add_paper(con, pid, sentences, vault="one", units=(), **meta):
    """One stored paper: `sentences` = [(section, page, text)], `units` = rows of evidence.jsonl (see `_unit`)."""
    folder = db.paper_dir(pid)
    rows = [{"sentence_id": f"{pid}:S{i:04d}", "seq": i, "passage_id": f"{pid}:{i:04d}", "page": page, "section": section,
             "subsection": None, "kind": "text", "source": "main", "text": text, "start": 0, "end": len(text),
             "citations": [], "mentions": []} for i, (section, page, text) in enumerate(sentences, 1)]
    db.write_json(folder / "metadata.json", {
        "paper_id": pid, "title": f"Title of {pid}", "year": 2020, "vaults": [vault], "authors": [], "source_files": [],
        "evidence_type": "primary_research", "extraction_version": 2, **meta})
    db.write_jsonl(folder / "sentences.jsonl", rows)
    db.write_jsonl(folder / "passages.jsonl", [{"passage_id": r["passage_id"], "seq": r["seq"], "page": r["page"],
                                                "section": r["section"], "kind": "text", "text": r["text"]} for r in rows])
    db.write_jsonl(folder / "evidence.jsonl", list(units))
    db.load_paper(con, pid)
    con.commit()


def _unit(pid, n, sentence_nos, rows, unit_type="result", status="ok", direction=None, measure=None, hedging="none"):
    """A stored evidence unit over the sentences `sentence_nos` (1-based) of `rows` ([(section, page, text)])."""
    chosen = [rows[i - 1] for i in sentence_nos]
    return {"unit_id": f"{pid}:E{n:04d}", "paper_id": pid, "seq": n, "unit_type": unit_type, "status": status,
            "statement": chosen[0][2], "quote": " ".join(c[2] for c in chosen),
            "sentence_ids": [f"{pid}:S{i:04d}" for i in sentence_nos], "pages": [c[1] for c in chosen],
            "sections": sorted({c[0] for c in chosen}), "hedging": hedging, "analyst": "test/analyst",
            "model": {}, "intervention": {}, "outcome": {"measure": measure, "direction": direction}}


def _ids(hits):
    return [h["id"] for h in hits]


def _stored_problem(con, hit):
    """Why a hit is not a stored sentence or unit (None when it is): unknown id, other paper, other text."""
    if hit.get("kind") == "unit":
        row = con.execute("SELECT paper_id, quote AS stored FROM evidence WHERE unit_id = ?", (hit.get("id"),)).fetchone()
    else:
        row = con.execute("SELECT paper_id, text AS stored FROM sentences WHERE sentence_id = ?", (hit.get("id"),)).fetchone()
    if row is None:
        return "is not a stored sentence or unit"
    if row["paper_id"] != hit.get("paper_id"):
        return f"names paper {hit.get('paper_id')} but is stored in {row['paper_id']}"
    if hit.get("text") != row["stored"]:
        return "text is not the stored text"
    return None


def _raises(call, *args, **kwargs):
    """(result, None) or (None, 'ErrorName: message'): a judge that crashes is reported by the caller, never read as a PASS."""
    try:
        return call(*args, **kwargs), None
    except Exception as err:  # noqa: BLE001 - the crash becomes a violation text
        return None, f"{type(err).__name__}: {str(err)[:80]}"


def _search(judge, con, cfg, query, **kw):
    return _raises(judge, con, cfg, query, vault=kw.pop("vault", "one"), semantic=False, **kw)


def _cache_answers(store):
    """(file name, parsed answer) of every cached answer of the store, one file in memory at a time."""
    folder = store.root / "cache" / "answers"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as err:
            yield path.name, {"_unreadable": f"{type(err).__name__}"}
            continue
        yield path.name, data if isinstance(data, dict) else {"_unreadable": "not an object"}


# ---------------------------------------------------------------- RT: retrieval

@check("RT-01", "probe")
def probe_hits_are_stored(judge=find.search):
    bad = []
    with _lab() as (root, con, cfg):
        rows = [("results", 3, "Alphaterm was higher in group A."), ("methods", 2, "Cells were washed twice.")]
        _add_paper(con, "pa", rows, units=[_unit("pa", 1, [1], rows)])
        _add_paper(con, "pb", [("results", 1, "Alphaterm was similar in group B.")])
        result, error = _search(judge, con, cfg, "alphaterm")
        if error:
            return [f"search crashed: {error}"]
        if len(result["hits"]) < 2:
            bad.append(f"the probe library gave {len(result['hits'])} hits, expected the unit and the other paper's sentence")
        for hit in result["hits"]:
            problem = _stored_problem(con, hit)
            if problem:
                bad.append(f"hit {hit.get('id')} {problem}")
            elif not hit.get("sections") or not hit.get("pages") or not hit.get("sentence_ids"):
                bad.append(f"hit {hit.get('id')} has no section, page or sentence_ids")
    if result.get("mode") not in ("and", "or") or not isinstance(result.get("semantic"), bool):
        bad.append(f"the retrieval mode is not reported (mode {result.get('mode')!r}, semantic {result.get('semantic')!r})")
    return bad


@check("RT-02", "probe")
def probe_or_ranking(judge=find.search):
    query = "alphaterm betaterm gammaterm"
    bad, groups = [], {}
    with _lab() as (root, con, cfg):
        _add_paper(con, "full", [("results", 1, "Alphaterm and betaterm and gammaterm were examined together in one long "
                                                "sentence about many unrelated things such as medium, washing, storage and plates.")])
        groups["full:S0001"] = 3
        for i in range(3):
            _add_paper(con, f"two{i}", [("results", 1, f"Betaterm gammaterm other words number {i}.")])
            _add_paper(con, f"one{i}", [("results", 1, "Alphaterm.")])
            groups[f"two{i}:S0001"], groups[f"one{i}:S0001"] = 2, 1
        result, error = _search(judge, con, cfg, query, limit=10)
        if error:
            return [f"search crashed: {error}"]
        if result.get("mode") != "or":
            bad.append(f"the probe did not reach OR mode (mode {result.get('mode')!r})")
        ranked = [groups.get(i, 0) for i in _ids(result["hits"])]
        if 3 not in ranked:
            bad.append("the item matching all 3 groups is missing from the results")
        inversions = sum(1 for i, a in enumerate(ranked) for b in ranked[i + 1:] if a < b)
        if inversions:
            first = next(i for i, a in enumerate(ranked) if any(a < b for b in ranked[i + 1:]))
            bad.append(f"{inversions} pairs rank an item with fewer groups above one with more "
                       f"(rank {first + 1} matches {ranked[first]} of 3 groups; ranks by group count: {ranked})")
        capped, error = _search(judge, con, cfg, query, limit=10, cap=3)
        if error:
            bad.append(f"search with a small candidate cap crashed: {error}")
        elif "full:S0001" not in _ids(capped["hits"]):
            bad.append("the item matching all 3 groups is lost when the candidate cap is 3 (the cap is applied before the group count)")
    return bad


@check("RT-03", "probe")
def probe_word_variants(judge=find.search):
    target = standard.param("RT-03", "variant_recall_target")
    forms = ("Oocytes were vitrified in straws.", "Vitrification of oocytes was done in straws.", "We vitrify oocytes in straws.")
    bad = []
    shipped = config.load().get("word_variants")   # the variant group lives in config/word_variants.yaml; the lab has an empty config dir
    with _lab() as (root, con, cfg):
        cfg["word_variants"] = shipped
        for i, text in enumerate(forms):
            _add_paper(con, f"v{i}", [("results", 1, text)])
        for i in range(3):
            _add_paper(con, f"d{i}", [("results", 1, f"Oocytes were cultured in medium {i}.")])
        found = 0
        for word in ("vitrification", "vitrified", "vitrify"):
            result, error = _search(judge, con, cfg, word, limit=50)
            if error:
                return [f"search crashed: {error}"]
            found += len({h["paper_id"] for h in result["hits"]} & {f"v{i}" for i in range(len(forms))})
        recall = found / (3 * len(forms))
        if recall < target:
            bad.append(f"recall of word variants is {recall:.2f}, target {target}")
        base, error = _search(judge, con, cfg, "vitrification")
        if error:
            return bad + [f"search crashed: {error}"]
        for word in ("affect", "impact", "influence", "change"):
            result, error = _search(judge, con, cfg, f"{word} vitrification")
            if error:
                bad.append(f"search crashed: {error}")
            elif len(result["groups"]) != len(base["groups"]):
                bad.append(f"'{word}' is searched as a keyword, not dropped as a stop word (groups {result['groups']})")
    return bad


@check("RT-04", "probe")
def probe_vietnamese(judge=find.search):
    vietnamese = {"tones and hooks": "ảnh hưởng của vitrification lên phôi", "same text, NFD": unicodedata.normalize("NFD", "ảnh hưởng của vitrification lên phôi"),
                  "acute and grave only": "các nhóm là gì"}
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")])
        for label, query in vietnamese.items():
            result, error = _search(judge, con, cfg, query)
            if error:
                bad.append(f"Vietnamese query ({label}) crashed the search: {error}")
            elif not result.get("needs_translation"):
                bad.append(f"Vietnamese query ({label}) is searched as English: no needs_translation flag (hits {len(result['hits'])})")
        result, error = _search(judge, con, cfg, "histone acetylation embryos")
        if not error and result.get("needs_translation"):
            bad.append("an English query is flagged needs_translation")
    return bad


@check("RT-05", "probe")
def probe_not_found(judge=find.search):
    minimum = standard.param("RT-05", "min_group_coverage")
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")])
        _add_paper(con, "pb", [("results", 1, "Alphaterm and betaterm were measured in group B.")])
        for label, query, best, groups in (("absent topic", "zetaterm etaterm thetaterm alphaterm", 1, 4),
                                           ("present topic", "alphaterm betaterm", 2, 2)):
            result, error = _search(judge, con, cfg, query)
            if error:
                bad.append(f"{label}: search crashed: {error}")
                continue
            if not result["hits"]:
                bad.append(f"{label}: the probe query found nothing, so the flag cannot be tested")
            below = best / groups < minimum
            if below and not result.get("not_found"):
                bad.append(f"{label}: best item matches {best} of {groups} groups (below {minimum}) but there is no not_found flag")
            elif below and result.get("best_groups_matched") != best:
                bad.append(f"{label}: best_groups_matched is {result.get('best_groups_matched')!r}, expected {best}")
            elif not below and result.get("not_found"):
                bad.append(f"{label}: best item matches {best} of {groups} groups but the result says not found")
    return bad


@check("RT-06", "probe")
def probe_scope(judge=find.search):
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")], vault="one")
        _add_paper(con, "pb", [("results", 1, "Alphaterm was higher in group B.")], vault="two")
        for label, kw, want in (("vault one", {"vault": "one"}, {"pa"}), ("vault two", {"vault": "two"}, {"pb"}),
                                ("default vault", {"vault": None}, {"pa"}), ("all vaults", {"all_vaults": True}, {"pa", "pb"})):
            result, error = _search(judge, con, cfg, "alphaterm", **kw)
            if error:
                bad.append(f"{label}: search crashed: {error}")
            elif {h["paper_id"] for h in result["hits"]} != want:
                bad.append(f"{label}: hits come from {sorted({h['paper_id'] for h in result['hits']})}, expected {sorted(want)}")
        result, error = _search(judge, con, cfg, "alphaterm", vault="no_such_vault")
        if not error:
            bad.append(f"an unknown vault does not raise (it returned {len(result['hits'])} hits)")
    return bad


@check("RT-07", "probe")
def probe_collect_fairness(judge=answer.collect):
    bad = []
    with _lab() as (root, con, cfg):
        for i in range(6):  # q1 has many good hits (AND mode); q2 has two papers (OR mode, its scores are weighted lower)
            _add_paper(con, f"a{i}", [("results", 1, f"Alphaterm was higher in group {i}.")])
        for i in range(2):
            _add_paper(con, f"b{i}", [("results", 1, f"Betaterm gammaterm were measured in group {i}.")])
        queries, slots = ["alphaterm", "betaterm gammaterm"], 4
        items, runs = judge(con, cfg, queries, "one", False, {}, slots)
        got = set(_ids(items))
        for query in queries:
            own = find.search(con, cfg, query, vault="one", semantic=False, limit=slots)["hits"][:2]
            for rank, hit in enumerate(own, 1):
                if hit["id"] not in got:
                    bad.append(f"rank {rank} hit {hit['id']} of query {query!r} is dropped ({len(got)} of {slots} slots go to the other query)")
        if len(runs) != len(queries):
            bad.append(f"{len(runs)} query runs reported for {len(queries)} queries")
        # a draft unit stands in for its first three sentences, not for the sentence that matched the query
        rows = [("results", 1, f"Sentence number {i} is about plates.") for i in range(1, 5)] + [("results", 1, "Deltaterm was higher in group C.")]
        _add_paper(con, "u", rows, units=[_unit("u", 1, [1, 2, 3, 4, 5], rows, status="flagged")])
        items, _ = judge(con, cfg, ["deltaterm"], "one", False, {}, 12)
        if "u:S0005" not in _ids(items):
            bad.append(f"the sentence that matches the query (u:S0005) is not among the items {_ids(items)}; the first three sentences of its unit are")
    return bad


@check("RT-08", "probe")
def probe_skipped_sections_and_paging(judge=find.search):
    bad = []

    def paging(con, cfg, query, limit):
        everything, error = _search(judge, con, cfg, query, limit=50)
        if error:
            return [f"search crashed: {error}"]
        pages, offset = [], 0
        while offset < len(everything["hits"]) + limit and len(pages) < 6:
            result, error = _search(judge, con, cfg, query, limit=limit, offset=offset)
            if error:
                return [f"paged search crashed: {error}"]
            pages.append(_ids(result["hits"]))
            offset += limit
        flat = [i for page in pages for i in page]
        out = []
        if len(flat) != len(set(flat)):
            out.append(f"query {query!r}: paging repeats rows ({len(flat) - len(set(flat))})")
        if set(flat) != set(_ids(everything["hits"])):
            out.append(f"query {query!r}: paging skips rows {sorted(set(_ids(everything['hits'])) - set(flat))[:3]}")
        return out

    with _lab() as (root, con, cfg):
        for i in range(7):
            sentences = [("results", 1, f"Alphaterm count {i} in the sample.")]
            if i == 0:
                sentences += [(section, 1, f"Alphaterm is named in the {section} text.") for section in SKIPPED]
            _add_paper(con, f"p{i}", sentences)
        result, error = _search(judge, con, cfg, "alphaterm", limit=50)
        if error:
            return [f"search crashed: {error}"]
        if not result["hits"]:
            bad.append("the probe query found nothing")
        for hit in result["hits"]:
            if set(hit.get("sections") or []) & set(SKIPPED):
                bad.append(f"hit {hit['id']} is from section {hit['sections']}")
        bad += paging(con, cfg, "alphaterm", 3)
        for i in range(20):  # a typed identity term is also searched in the Results of every paper: paging must stay stable
            _add_paper(con, f"q{i}", [("results", 1, f"Cells were cultured in medium {i}.")])
        for i in range(10):
            _add_paper(con, f"t{i}", [("results", 1, f"ZK12me3 was higher in sample {i}.")])
        bad += paging(con, cfg, "ZK12me3", 8)
    return bad


@check("RT-09", "probe")
def probe_context(judge=find.context):
    bad = []
    with _lab() as (root, con, cfg):
        rows = [("results", 1, f"Sentence number {i} is about plates.") for i in range(1, 8)] + [("references", 9, "Smith 2000. A reference line.")]
        rows[3] = ("results", 1, "Deltaterm is the subject of sentence number 4.")
        _add_paper(con, "pa", rows, units=[_unit("pa", 1, [4], rows)])
        _add_paper(con, "pb", [("results", 1, "Another paper sentence.")])
        windows = {w: [r["sentence_id"] for r in judge(con, "pa:S0004", w)] for w in (1, 2, 3)}
        if windows[1] != [f"pa:S{i:04d}" for i in (3, 4, 5)]:
            bad.append(f"window 1 around S0004 is {windows[1]}")
        if not (set(windows[1]) < set(windows[2]) < set(windows[3])):
            bad.append("a wider window does not contain the narrower one and more")
        if any(not i.startswith("pa:") for w in windows.values() for i in w):
            bad.append("the context reaches into another paper")
        if any(r["section"] == "references" for r in judge(con, "pa:S0007", 3)):
            bad.append("the context returns reference lines")
        if judge(con, "pa:S9999", 2):
            bad.append("an unknown sentence has a context")
        result, error = _search(find.search, con, cfg, "deltaterm", depth="deep")
        if error:
            bad.append(f"deep search crashed: {error}")
        else:
            for hit in result["hits"]:
                if hit["sentence_ids"][0] not in [c["sentence_id"] for c in hit.get("context") or []]:
                    bad.append(f"deep search: hit {hit['id']} has no context around its own sentence")
        hits, _ = answer.collect(con, cfg, ["deltaterm"], "one", False, {}, 12)
        items = answer.add_context(con, hits, 8, 2, 45)
        extra = [i for i in items if i.get("context_of")]
        if not extra or any(con.execute("SELECT 1 FROM sentences WHERE sentence_id = ?", (i["id"],)).fetchone() is None for i in extra):
            bad.append("deep answer items do not add stored neighbouring sentences")
    return bad


def _hit_fields(hit, card):
    """Fields RT-10 asks for that this hit (with its paper card) lacks."""
    lacking = [name for name, ok in (
        ("paper_id", hit.get("paper_id")), ("title", (card or {}).get("title")),
        ("doi and pmid fields", card is not None and "doi" in card and "pmid" in card),
        ("section", [s for s in hit.get("sections") or [] if s]),
        ("page", [p for p in hit.get("pages") or [] if p is not None]),
        ("verbatim text", (hit.get("text") or "").strip()), ("sentence_ids", hit.get("sentence_ids"))) if not ok]
    return lacking


@check("RT-10", "store")
def hit_fields(store, judge=find.search):
    """Data: every item of a cached answer. Probe: every hit and answer item of a small library with its paper card."""
    for name, data in _cache_answers(store):
        if data.get("_unreadable"):
            yield f"cached answer {name} cannot be read ({data['_unreadable']})"
            continue
        for item in data.get("items") or []:
            lacking = _hit_fields(item, (data.get("papers") or {}).get(item.get("paper_id")))
            if lacking:
                yield f"cached answer {name}: item {item.get('id')} lacks {', '.join(lacking)}"
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 3, "Alphaterm was higher in group A.")], doi="10.1/probe", pmid="42")
        result, error = _search(judge, con, cfg, "alphaterm")
        if error:
            yield f"search crashed: {error}"
            return
        items, _ = answer.collect(con, cfg, ["alphaterm"], "one", False, {}, 12)
        cards = find.paper_cards(con, {"pa"})
        if not result["hits"] or not items:
            yield "the probe library gave no hit or no answer item"
        for source, hits, cards_of in (("hit", result["hits"], result["papers"]), ("answer item", items, cards)):
            for hit in hits:
                lacking = _hit_fields(hit, cards_of.get(hit.get("paper_id")))
                card = cards_of.get(hit.get("paper_id")) or {}
                if card and (card.get("doi"), card.get("pmid")) != ("10.1/probe", "42"):
                    lacking.append("the stored doi / pmid")
                if lacking:
                    yield f"{source} {hit.get('id')} lacks {', '.join(lacking)}"


@check("RT-11", "probe")
def probe_overlap_label(judge=overlap.find_overlaps):
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "The quick marker analysis showed that cells were cultured in warm medium for two hours.")])
        pasted = "We state that cells were cultured in warm medium for two hours before the next test."
        for label, text in (("a pasted passage", pasted), ("no overlap", "Completely different invented wording about nothing."), ("empty text", "")):
            result, error = _raises(judge, con, text, "one")
            if error:
                bad.append(f"{label}: crashed: {error}")
                continue
            if label == "a pasted passage" and not result.get("hits"):
                bad.append("the probe passage found no overlap, so the label of a hit cannot be tested")
            if result.get("label") != "lexical overlap":
                bad.append(f"{label}: the result is labelled {result.get('label')!r}")
            for hit in result.get("hits") or []:
                if hit.get("label") != "lexical overlap":
                    bad.append(f"{label}: hit {hit.get('sentence_id')} is labelled {hit.get('label')!r}")
            words = json.dumps({k: v for k, v in result.items() if k != "hits"}, ensure_ascii=False) + " ".join(
                str(v) for h in result.get("hits") or [] for k, v in h.items() if k != "text")
            if re.search(r"plagiar|đạo văn", words, re.I):
                bad.append(f"{label}: the result is worded as a plagiarism verdict")
    return bad


@check("RT-12", "probe")
def probe_source_hits(judge=find.search):
    bad = []
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Gammaterm was higher in group A.")])
        note = root / "inbox" / "notebook.md"
        note.parent.mkdir()
        note.write_text("Gammaterm looked odd in my own notebook sample.\n", encoding="utf-8", newline="\n")
        imported = sources_misc.import_source(note, "one", "Notebook", con=con)
        con.commit()
        result, error = _search(judge, con, cfg, "gammaterm")
        if error:
            return [f"search crashed: {error}"]
        if not isinstance(result.get("source_hits"), list) or not result["source_hits"]:
            bad.append("source_hits is not a separate list holding the notebook row")
        for row in result.get("source_hits") or []:
            if row.get("evidence_class") not in ("own_data", "human_note") or row.get("published_evidence") is not False:
                bad.append(f"source row {row.get('sentence_id')} is labelled {row.get('evidence_class')!r}, published_evidence {row.get('published_evidence')!r}")
        source_texts = {row.get("text") for row in result.get("source_hits") or []}
        for hit in result["hits"]:
            if hit.get("paper_id") == imported["source_id"] or hit.get("text") in source_texts or _stored_problem(con, hit):
                bad.append(f"a non-PDF source row appears among the paper hits ({hit.get('id')})")
        items, _ = answer.collect(con, cfg, ["gammaterm"], "one", False, {}, 12)
        if any(i.get("text") in source_texts or i.get("paper_id") == imported["source_id"] for i in items):
            bad.append("a non-PDF source row appears among the answer items")
        if not items:
            bad.append("the probe gave no answer item")
    return bad


# ---------------------------------------------------------------- AN: the answer checker

def _item(text, **extra):
    return {"id": "p1:S0001", "paper_id": "p1", "kind": "sentence", "unit_type": None, "text": text, "review_paper": False, **extra}


def _verdict_problem(got, want):
    """The first token of `want` that the verdict `got` breaks. ok / struck: the strike state; A3: that problem or warning is
    present; -A3: it is absent."""
    flags = set(got.get("problems") or []) | set(got.get("warnings") or [])
    for token in want.split():
        if token == "ok":
            fine = got.get("check") == "ok"
        elif token == "struck":
            fine = str(got.get("check")).startswith("struck")
        elif token.startswith("-"):
            fine = token[1:] not in flags
        else:
            fine = token in flags
        if not fine:
            return f"expected {token}, got check {got.get('check')!r}, flags {sorted(flags)}"
    return None


def _judge_cases(judge, cases):
    """Run `(label, sentence, items, want[, kwargs])` through the judge; one violation per verdict that differs from `want`."""
    bad = []
    for label, sentence, items, want, *rest in cases:
        kw = dict(rest[0]) if rest else {}
        cards = kw.pop("cards", {})
        sentence = sentence if isinstance(sentence, dict) else {"text": sentence}
        got, error = _raises(judge, sentence, items, cards, **kw)
        problem = f"the checker crashed ({error})" if error else _verdict_problem(got, want)
        if problem:
            bad.append(f"{label}: {problem}")
    return bad


HEDGED = _item("Treatment may improve survival of cells.", hedging="hedged", uncertainty=["may"])
PLAIN = _item("Treatment improved survival of cells in group A.")
CAUSAL = _item("Treatment caused improved survival of cells.")
SECONDHAND = _item("Vitrification damages the spindle.", kind="unit", unit_type="cited_statement",
                   cited_refs=[{"num": 7, "year": 2016, "first_author": "Smith"}])
REVIEW = _item("Treatment improved survival of cells in group A.", review_paper=True)


@check("AN-01", "probe")
def probe_a1_citation(judge=answer.check_sentence):
    return _judge_cases(judge, [
        ("marker to an existing item", "Cells were counted [#1].", [PLAIN], "ok -A1"),
        ("evidence list to an existing item", {"text": "Cells were counted.", "evidence": [1]}, [PLAIN], "ok -A1"),
        ("no citation", "Cells were counted.", [PLAIN], "struck A1"),
        ("item number that does not exist", "Cells were counted [#2].", [PLAIN], "struck A1"),
        ("item number zero", "Cells were counted [#0].", [PLAIN], "struck A1"),
        ("one existing and one missing item", {"text": "Cells were counted.", "evidence": [1, 3]}, [PLAIN], "struck A1")])


@check("AN-02", "probe")
def probe_a2_numbers(judge=answer.check_sentence):
    other = _item("H3K9me3 reached 70 percent in group B.", id="p2:S0001", paper_id="p2")
    mark = _item("H4K12ac rose in group A.")
    return _judge_cases(judge, [
        ("number from the item", "Survival was 85 percent in group A [#1].", [_item("Survival was 85 percent in group A.")], "ok -A2"),
        ("invented number", "Survival was 90 percent in group A [#1].", [_item("Survival was 85 percent in group A.")], "struck A2"),
        ("cited year", "In 2020 survival was 85 percent [#1].", [_item("Survival was 85 percent.")], "ok -A2", {"cards": {"p1": {"year": 2020}}}),
        ("reference number", "Theo tài liệu trích dẫn, survival was 85 percent as in reference 12 [#1].",
         [_item("Survival was 85 percent.", cited_refs=[{"num": 12, "year": None}])], "ok -A2"),
        ("number borrowed from an item about another entity", "H4K12ac reached 70 percent [#1][#2].", [mark, other], "struck")])


@check("AN-03", "probe")
def probe_a3_causal(judge=answer.check_sentence):
    verbs = ("gây ra", "khiến", "gây nên", "làm cho", "chứng tỏ", "chứng minh", "dẫn đến")
    cases = [("hedged wording over a hedged item", "Điều trị có thể cải thiện sự sống sót của tế bào [#1].", [HEDGED], "ok -A3 -A7"),
             ("causal wording over a source that states causation", "Điều trị gây ra cải thiện sự sống sót của tế bào [#1].", [CAUSAL], "ok -A3"),
             ("causal wording over an item without causation", "Điều trị gây ra cải thiện sự sống sót của tế bào [#1].", [PLAIN], "struck A3")]
    for verb in verbs:
        text = f"Điều trị {verb} tế bào sống sót tốt hơn [#1]."
        for form in ("NFC", "NFD"):
            cases.append((f"'{verb}' over a hedged item ({form})", unicodedata.normalize(form, text), [HEDGED], "struck A3"))
    return _judge_cases(judge, cases)


@check("AN-04", "probe")
def probe_a4_attribution(judge=answer.check_sentence):
    return _judge_cases(judge, [
        ("re-cited finding without attribution", "Vitrification làm hỏng thoi vô sắc [#1].", [SECONDHAND], "struck A4"),
        ("attributed with 'Theo'", "Theo Smith 2016, vitrification làm hỏng thoi vô sắc [#1].", [SECONDHAND], "ok -A4"),
        ("author year proposed (Vietnamese)", "Smith 2016 đề xuất rằng vitrification làm hỏng thoi vô sắc [#1].", [SECONDHAND], "ok -A4"),
        ("author year suggested (Vietnamese)", "Smith 2016 gợi ý rằng vitrification làm hỏng thoi vô sắc [#1].", [SECONDHAND], "ok -A4"),
        ("author year proposed (English)", "Smith 2016 proposed that vitrification damages the spindle [#1].", [SECONDHAND], "ok -A4"),
        ("author year hypothesised (English)", "Smith 2016 hypothesised that vitrification damages the spindle [#1].", [SECONDHAND], "ok -A4"),
        ("a bare 'theo' that attributes nothing", "Số phôi sống giảm theo thời gian [#1].", [SECONDHAND], "struck A4"),
        ("own finding needs no attribution", "Điều trị cải thiện sự sống sót của tế bào [#1].", [PLAIN], "ok -A4")])


@check("AN-05", "probe")
def probe_a5_review(judge=answer.check_sentence):
    return _judge_cases(judge, [
        ("review-only support not named", "Điều trị cải thiện sự sống sót của tế bào [#1].", [REVIEW], "ok A5"),
        ("review named", "Theo bài tổng quan, điều trị cải thiện sự sống sót của tế bào [#1].", [REVIEW], "ok -A5"),
        ("review and primary paper", {"text": "Điều trị cải thiện sự sống sót của tế bào.", "evidence": [1, 2]},
         [REVIEW, _item("Treatment improved survival of cells.", id="p2:S0001", paper_id="p2")], "ok -A5"),
        ("primary paper only", "Điều trị cải thiện sự sống sót của tế bào [#1].", [PLAIN], "ok -A5")])


@check("AN-06", "probe")
def probe_a6_direction(judge=answer.check_sentence):
    inc = _item("Marker level increased in group A.", direction="increase")
    dec = _item("Marker level decreased in group A.", direction="decrease", id="p2:S0001", paper_id="p2")
    same = _item("Marker level did not change in group A.", direction="no_change")
    return _judge_cases(judge, [
        ("up over an increase", "Mức marker tăng ở nhóm A [#1].", [inc], "ok -A6"),
        ("down over a decrease", "Mức marker giảm ở nhóm A [#1].", [dec], "ok -A6"),
        ("no change over no change", "Mức marker không thay đổi ở nhóm A [#1].", [same], "ok -A6"),
        ("down over an increase", "Mức marker giảm ở nhóm A [#1].", [inc], "struck A6"),
        ("up over a decrease", "Mức marker tăng ở nhóm A [#1].", [dec], "struck A6"),
        ("up over no change", "Mức marker tăng ở nhóm A [#1].", [same], "struck A6"),
        ("up and down both said, one item", "Mức marker tăng ở nhóm A nhưng giảm ở nhóm B [#1].", [dec], "ok -A6"),
        ("up over two items, one decreasing", "Mức marker tăng ở nhóm A [#1][#2].", [inc, dec], "struck A6")])


@check("AN-07", "probe")
def probe_a7_uncertainty(judge=answer.check_sentence):
    mixed = _item("Marker X was not significantly altered. Marker Y increased (p < 0.05).", kind="unit", unit_type="result",
                  hedging="hedged")
    suggest = _item("Results suggest that marker X is higher in group A.", hedging="hedged")
    nonsig = _item("Marker X was not significantly different between groups.", hedging="hedged")
    return _judge_cases(judge, [
        ("a different measure of a unit that has a non-significant sentence", "Marker Y tăng (p < 0.05) [#1].", [mixed], "ok -A7"),
        ("non-significance written in Vietnamese", "Marker X không khác biệt có ý nghĩa thống kê giữa các nhóm [#1].", [nonsig], "ok -A7"),
        ("hedge kept", "Kết quả gợi ý marker X cao hơn ở nhóm A [#1].", [suggest], "ok -A7"),
        ("hedge dropped", "Marker X cao hơn ở nhóm A [#1].", [suggest], "struck A7")])


@check("AN-08", "probe")
def probe_a8_reasoning_notes(judge=answer.check_reasoning):
    note = {"text": "Tổng hợp từ ghi chú của thư viện.", "basis": ["Notes/a.md"]}
    return _judge_cases(judge, [
        ("note on the allowlist", note, [PLAIN], "ok -A8", {"note_refs": ["Notes/a.md"]}),
        ("note not on the allowlist", {**note, "basis": ["Notes/b.md"]}, [PLAIN], "struck A8", {"note_refs": ["Notes/a.md"]}),
        ("no allowlist at all", note, [PLAIN], "struck A8"),
        ("item and unknown note", {"text": "Điều trị cải thiện sự sống sót.", "basis": [1, "Notes/b.md"]}, [PLAIN], "struck A8",
         {"note_refs": ["Notes/a.md"]}),
        ("item only", {"text": "Điều trị cải thiện sự sống sót.", "basis": [1]}, [PLAIN], "ok -A8")])


@check("AN-09", "probe")
def probe_a9_bare_verdict(judge=answer.check_sentence):
    item = _item("Embryo survival was 80 percent.")
    return _judge_cases(judge, [
        ("Có.", "Có. Tỷ lệ sống của phôi là 80 percent [#1].", [item], "ok A9", {"part2": True}),
        ("Không.", "Không. Tỷ lệ sống của phôi là 80 percent [#1].", [item], "ok A9", {"part2": True}),
        ("Yes.", "Yes. Embryo survival was 80 percent [#1].", [item], "ok A9", {"part2": True}),
        ("No.", "No. Embryo survival was 80 percent [#1].", [item], "ok A9", {"part2": True}),
        ("what the items report", "Tỷ lệ sống của phôi là 80 percent [#1].", [item], "ok -A9", {"part2": True}),
        ("hedged opener is not a verdict", "Có thể tỷ lệ sống của phôi là 80 percent [#1].", [item], "-A9", {"part2": True}),
        ("outside Part 2", "Có. Tỷ lệ sống của phôi là 80 percent [#1].", [item], "-A9")])


@check("AN-10", "probe")
def probe_a10_subject(judge=answer.check_sentence):
    sip = _item("Treatment with sip150 reduced H4K12ac level in embryos.")
    slash = _item("H3K4me2/3 levels were higher in group A.")
    return _judge_cases(judge, [
        ("a vitrification sentence on an item about sip150", "Vitrification làm giảm H4K12ac ở phôi [#1].", [sip], "struck"),
        ("a sentence about the item's own treatment", "Sip150 làm giảm H4K12ac ở phôi [#1].", [sip], "ok"),
        ("a histone mark the item does not hold", "Sip150 làm giảm H3K9me3 ở phôi [#1].", [sip], "struck"),
        ("H3K4me2/3 read as me2 and me3", "H3K4me2 và H3K4me3 cao hơn ở nhóm A [#1].", [slash], "ok -A2"),
        ("H3K4me2/3 copied", "H3K4me2/3 cao hơn ở nhóm A [#1].", [slash], "ok -A2"),
        ("only me2 of a me2/3 item", "H3K4me2 cao hơn ở nhóm A [#1].", [slash], "ok -A2")])


@check("AN-11", "probe")
def probe_limits_sentence(judge=answer.check_sentence):
    mouse = _item("A single mouse study may show an effect.", hedging="hedged", direction="decrease")
    return _judge_cases(judge, [
        ("limit without hedge words over a hedged item", "Only one mouse study, not generalisable [#1].", [mouse], "ok -A7", {"limits": True}),
        ("firm 'cannot conclude' wording", "Không thể kết luận chắc chắn vì chỉ có một nghiên cứu trên chuột [#1].", [mouse], "ok -A3", {"limits": True}),
        ("direction word naming what is not shown", "Chưa rõ vì nghiên cứu duy nhất ghi nhận tăng ở một nhóm [#1].", [mouse], "ok -A6", {"limits": True}),
        ("affirming verb (English)", "Only one mouse study proves the effect [#1].", [mouse], "struck", {"limits": True}),
        ("affirming verb (Vietnamese)", "Nghiên cứu duy nhất trên chuột chứng minh hiệu quả [#1].", [mouse], "struck", {"limits": True}),
        ("the same limit sentence outside limits mode", "Only one mouse study, not generalisable [#1].", [mouse], "struck A7")])


@check("AN-12", "probe")
def probe_item_numbers(judge=answer.check_sentence, reasoning=answer.check_reasoning):
    items = [PLAIN, _item("Another sentence.", id="p1:S0002"), _item("A third sentence.", id="p1:S0003")]
    bad = _judge_cases(judge, [
        ("[#3] is item 3", "Cells were counted [#3].", items, "ok -A1"),
        ("[40-42] is a literature reference", "Cells recovered after warming [40-42] [#1].", items[:1], "ok -A1"),
        ("[2] inside a reference list is no item", "Cells were counted [#1] as in earlier work [1, 2, 3].", items[:1], "ok -A1"),
        ("bool is not an item number", {"text": "Cells were counted.", "evidence": [True]}, items[:1], "struck A1"),
        ("float is not an item number", {"text": "Cells were counted.", "evidence": [1.5]}, items[:1], "struck A1"),
        ("Infinity is rejected, not a crash", {"text": "Cells were counted.", "evidence": [float("inf")]}, items[:1], "struck A1"),
        ("NaN is rejected, not a crash", {"text": "Cells were counted.", "evidence": [float("nan")]}, items[:1], "struck A1"),
        ("a bad value beside a good item is still struck", {"text": "Cells were counted.", "evidence": [1, True]}, items[:1], "struck A1")])
    bad += _judge_cases(reasoning, [
        ("Infinity as a reasoning basis", {"text": "Synthesis of the cited sentence.", "basis": [float("inf")]}, items[:1], "struck"),
        ("bool as a reasoning basis", {"text": "Synthesis of the cited sentence.", "basis": [True]}, items[:1], "struck")])
    return bad


@check("AN-13", "probe")
def probe_items_digest(prepare=agent_tasks.answer_prompt, checker=agent_tasks.answer_check):
    bad = []
    question, queries = "Is alphaterm higher?", ["alphaterm"]
    reply = {"answer": [{"text": "Alphaterm was higher in group A [#1].", "evidence": [1]},
                        {"text": "Alphaterm was 99 percent in group A [#1].", "evidence": [1]}], "reasoning": [], "missing": []}
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")])
        first, error = _raises(prepare, con, cfg, question, queries, "evidence4", "one", "fast", "conform")
        if error:
            return [f"ask_prepare crashed: {error}"]
        _add_paper(con, "pz", [("results", 1, "Alphaterm alphaterm alphaterm alphaterm.")])  # would now rank first
        second, error = _raises(checker, con, cfg, question, queries, reply, "evidence4", "one", "fast", "conform")
        if error:
            return bad + [f"ask_check crashed: {error}"]
    for name, out in (("ask_prepare", first), ("ask_check", second)):
        if not out.get("items_digest"):
            bad.append(f"{name} returns no items_digest")
    if first.get("items_digest") and second.get("items_digest") and first["items_digest"] != second["items_digest"]:
        bad.append("the items_digest of ask_check differs from ask_prepare: the items were retrieved again and the [n] numbers can change")
    rows = (second.get("answer") or {}).get("answer") or []
    struck = [r for r in rows if r.get("check") != "ok"]
    if not struck:
        bad.append("the sentence with an invented number was not struck in the probe")
    elif not all(r.get("reasons") for r in struck):
        bad.append("a struck sentence carries no reason")
    elif not any(r["reasons"][0] in (second.get("markdown") or "") for r in struck):
        bad.append("ask_check returns no markdown that gives the reason of the struck sentence")
    return bad


@check("AN-14", "probe")
def probe_code_quotes(judge=answer.code_evidence):
    bad = []
    unit_items = [_item("Alphaterm was higher in group A.", id="pa:E0001", paper_id="pa", kind="unit", unit_type="result", pages=[3]),
                  _item("Betaterm was lower in group A.", id="pa:E0002", paper_id="pa", kind="unit", unit_type="result", pages=[])]
    with _lab() as (root, con, cfg):
        _add_paper(con, "pb", [("results", 5, "Gammaterm was higher in group B.")], first_author="Smith", year=2021)
        stored, _ = answer.collect(con, cfg, ["gammaterm"], "one", False, {}, 12)
        cards = find.paper_cards(con, {"pb"})
    cases = [("unit items, missing metadata", unit_items, {"pa": {"first_author": None, "year": None}}),
             ("stored sentences", stored, cards)]
    rows, error = _raises(judge, stored, cards)
    if not error and not (rows and rows[0]["citation"].startswith("[Smith, 2021, trang 5]")):
        bad.append(f"the citation of a stored sentence is not made from its paper card and page: {rows and rows[0]['citation']}")
    for label, items, cards_of in cases:
        rows, error = _raises(judge, items, cards_of)
        if error:
            bad.append(f"{label}: crashed: {error}")
            continue
        if len(rows) != len(items):
            bad.append(f"{label}: {len(rows)} quotes for {len(items)} items")
        for row, item in zip(rows, items):
            if row.get("quote") != item["text"]:
                bad.append(f"{label}: the quote of {item['id']} is not the stored text")
    rows, error = _raises(judge, unit_items, {"pa": {"first_author": None, "year": None}})
    if not error and rows:
        if "không rõ" not in rows[0]["citation"].split(",")[0] or rows[0]["citation"].count("không rõ") < 2:
            bad.append(f"missing author and year are not 'không rõ': {rows[0]['citation']}")
        if "trang không rõ" not in rows[1]["citation"]:
            bad.append(f"a missing page is not 'trang không rõ': {rows[1]['citation']}")
    typed = {"answer": [{"text": "Alphaterm cao hơn [#1].", "evidence": [1], "quote": "typed by the AI"}],
             "evidence": [{"quote": "typed by the AI", "item": 1}]}
    checked, error = _raises(answer.check_parts, typed, unit_items, {})
    if error:
        bad.append(f"check_parts crashed: {error}")
    elif [row["quote"] for row in checked["evidence"]] != [unit_items[0]["text"]]:
        bad.append("a quote typed by the AI reaches Part 1")
    return bad


@check("AN-15", "probe")
def probe_parts_apart(judge=answer.check_parts, render=answer.render_markdown):
    bad = []
    items = [_item("Alphaterm was higher in group A.")]
    data = {"answer": [{"text": "Alphaterm cao hơn ở nhóm A [#1].", "evidence": [1]},
                       {"text": "Một điều tôi biết chung chung mà không có nguồn.", "evidence": []}],
            "reasoning": [{"text": "Có thể do một cơ chế nào đó của alphaterm.", "basis": [1]}], "missing": []}
    checked, error = _raises(judge, data, items, {})
    if error:
        return [f"check_parts crashed: {error}"]
    if checked["answer"][1].get("check") == "ok":
        bad.append("a Part 2 sentence without a cited item (model memory) is not struck")
    if checked.get("scope_note") != answer.SCOPE_NOTE or not re.search(r"không có nghĩa.*không tồn tại", checked.get("scope_note") or ""):
        bad.append("the scope note 'not found in the current vault is not absent from the literature' is missing")
    markdown, error = _raises(render, checked, items)
    if error:
        return bad + [f"rendering crashed: {error}"]
    parts, name = {}, ""
    for line in markdown.splitlines():
        if line.startswith("## "):
            name = line
        parts.setdefault(name, []).append(line)
    reasoning = next((v for k, v in parts.items() if "REASONING" in k), None)
    evidence = next((v for k, v in parts.items() if "Bằng chứng" in k), [])
    part2 = next((v for k, v in parts.items() if "Trả lời" in k), [])
    if reasoning is None:
        bad.append("the markdown has no [REASONING] part")
    else:
        if not any("cơ chế nào đó" in line for line in reasoning):
            bad.append("the reasoning sentence is not in the [REASONING] part")
        if not any("AI" in line and "không phải" in line for line in reasoning):
            bad.append("the [REASONING] part carries no label that it is AI reasoning and not document content")
    if any("cơ chế nào đó" in line for line in evidence + part2):
        bad.append("AI reasoning appears in Part 1 or Part 2")
    from . import mcp_server
    nfc = lambda text: unicodedata.normalize("NFC", text)
    label = answer.AI_KNOWLEDGE_LABEL   # AN-15: one label in the instructions, the protocol and the rendered answer
    for name, text in (("the MCP instructions", mcp_server.INSTRUCTIONS), ("the answer protocol", discussions.protocol())):
        if nfc(label) not in nfc(text):
            bad.append(f"{name} lack the one label for the AI's general knowledge")
    basis_data = {"answer": [{"text": "Alphaterm cao hơn ở nhóm A [#1].", "evidence": [1]},
                             {"text": "Một điều chung chung.", "evidence": ["ai_knowledge"]},
                             {"text": "Một điều chung chung khác.", "evidence": [1, "ai_knowledge"]}],
                  "reasoning": [{"text": "Một suy luận không nêu cơ sở.", "basis": []},
                                {"text": "Một suy luận từ kiến thức chung.", "basis": ["ai_knowledge"]},
                                {"text": "Một suy luận trộn cơ sở.", "basis": [1, "ai_knowledge"]},
                                {"text": "Một suy luận từ mục.", "basis": [1]}], "missing": []}
    got, error = _raises(judge, basis_data, items, {})
    if error:
        return bad + [f"check_parts crashed on basis rows: {error}"]
    if [r["check"] for r in got["answer"][1:]] != ["struck:A11"] * 2:
        bad.append("'ai_knowledge' outside Part 3 is not struck (A11)")
    if [r["check"] for r in got["reasoning"]] != ["struck:A11", "ok", "struck:A11", "ok"]:
        bad.append("a Part 3 sentence needs exactly one kind of basis: none and mixed strike A11, 'ai_knowledge' alone is ok")
    shown = [line for line in (_raises(render, got, items)[0] or "").splitlines() if "kiến thức chung" in line]
    if not shown or nfc(label) not in nfc(shown[0]):
        bad.append("a Part 3 sentence with basis 'ai_knowledge' is not rendered with the label")
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")])
        for query in ("zetaterm", "alphaterm"):
            out, error = _raises(answer.answer, con, cfg, query, use_ai=False, vault="one")
            if error:
                bad.append(f"answer({query!r}) crashed: {error}")
                continue
            texts = list(out.get("overview") or []) + list((out.get("answer") or {}).get("missing") or []) + [out.get("scope_note") or ""]
            if any(NOT_STUDIED.search(t) for t in texts):
                bad.append(f"the answer to {query!r} words absence as 'not studied in the literature'")
            if query == "zetaterm" and not (out.get("answer") or {}).get("not_found"):
                bad.append("an absent topic is not reported as not found in the current vault")
    return bad


@check("AN-16", "probe")
def probe_both_sides(judge=answer.collect):
    bad = []
    with _lab() as (root, con, cfg):
        for pid, word, direction in (("pa", "raised", "increase"), ("pb", "lowered", "decrease")):
            rows = [("results", 2, f"Alphaterm {word} marker M in cells.")]
            _add_paper(con, pid, rows, units=[_unit(pid, 1, [1], rows, direction=direction, measure="marker M")],
                       first_author=pid.upper())
        items, runs = judge(con, cfg, ["alphaterm marker"], "one", False, {}, 12)
        by_paper = {i["paper_id"]: i for i in items}
        if set(by_paper) != {"pa", "pb"}:
            return [f"the two opposed findings reach the answer items from papers {sorted(by_paper)}"]
        for pid, item in by_paper.items():
            if not item.get("sentence_ids") or not item.get("pages"):
                bad.append(f"the item of {pid} has no citation (sentence_ids / pages)")
        table = answer.comparison_table(items, find.paper_cards(con, {"pa", "pb"}))
        directions = {row["cells"][0]: row["cells"][5] for row in table["rows"]}
        if len(directions) != 2 or len(set(directions.values())) != 2:
            bad.append(f"the comparison table does not show both directions: {directions}")
    return bad


def _front_matter(text):
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    try:
        return yaml.safe_load(m.group(1)) if m else None
    except yaml.YAMLError:
        return None


GAP_PARTS = ("Bằng chứng từ bài", "Kiến thức của AI", "Nhận định của bạn")


@check("AN-17", "store")
def saved_discussions(store, judge=discussions.save):
    """Data: the notes under `<vault>/Discussions/`. Probe: `judge` (discussions.save) in a throw-away project."""
    for vault in store.vaults:
        folder = vault.root / "Discussions"
        for path in sorted(folder.glob("*.md")) if folder.is_dir() else ():
            text = path.read_bytes().decode("utf-8", errors="replace")
            front = _front_matter(text)
            where = f"{vault.name}/Discussions/{path.name}"
            if not isinstance(front, dict):
                yield f"{where}: no front matter"
                continue
            if front.get("type") not in ("discussion", "knowledge_gap", "decision"):
                yield f"{where}: type is {front.get('type')!r}"
            if front.get("evidence") is not False:
                yield f"{where}: evidence is {front.get('evidence')!r}, not false"
            if front.get("type") == "decision" and not front.get("cited_items") and discussions.NO_EVIDENCE_LINE not in text:
                yield f"{where}: a decision with no cited item lacks the line {discussions.NO_EVIDENCE_LINE!r}"
            if front.get("type") == "knowledge_gap":
                missing = [p for p in GAP_PARTS if not re.search(rf"(?m)^[ #*\-]*{p}", text, re.I)]
                if missing:
                    yield f"{where}: knowledge gap lacks the labelled parts {missing}"
    protocol = discussions.protocol()
    for step in ("## 1.", "## 2.", "## 3.", "## 4."):
        if step not in protocol:
            yield f"the answer protocol has no step {step[3]}"
    with _lab() as (root, con, cfg):
        _add_paper(con, "pa", [("results", 1, "Alphaterm was higher in group A.")])
        body = {"discussion": "Zorblaxx idea with [[Note]].",
                "knowledge_gap": "\n\n".join(f"**{part}**\n\nZorblaxx text." for part in GAP_PARTS)}
        body["decision"] = "Zorblaxx settled."
        for kind, markdown in body.items():
            more = {"con": con, "researcher_words": "Chốt: zorblaxx.", "citations": ["pa:S0001"]} if kind == "decision" else {}
            saved, error = _raises(judge, "one", f"A {kind} title", markdown, "conform", kind, **more)
            if error:
                yield f"{kind}: save crashed: {error}"
                continue
            front = _front_matter((config.vault_dir("one") / saved["saved"]).read_bytes().decode("utf-8"))
            if not isinstance(front, dict) or front.get("evidence") is not False or front.get("type") != kind:
                yield f"{kind}: the saved note has front matter {front}"
            elif kind == "decision" and (front.get("cited_items") != ["pa:S0001"]
                                         or "Alphaterm was higher in group A." not in (config.vault_dir("one") / saved["saved"]).read_text(encoding="utf-8")):
                yield "decision: the citation is not stored or its quote is not copied from the store"
        _, refused = _raises(judge, "one", "Unsaid", "text", "conform", "decision", con=con, researcher_words="Maybe later.")
        if not refused:
            yield "a decision is saved without the researcher saying 'chốt'"
        found, error = _search(find.search, con, cfg, "zorblaxx")
        items, _ = answer.collect(con, cfg, ["zorblaxx"], "one", False, {}, 12)
        if error or found["hits"] or found["source_hits"] or items:
            yield "a saved discussion is found by library search or answer collection (it is used as evidence)"
        _, refused = _raises(judge, "one", "A title", "text", "conform", "evidence")
        if not refused:
            yield "a note of kind 'evidence' can be saved"


@check("AN-18", "store")
def traceable_chain(store, judge=answer.collect):
    """Data: every cited sentence of a cached answer resolves to its item, section, page, paper and source status.
    Probe: the same chain for the items of a small library with a recorded original file."""
    for name, data in _cache_answers(store):
        if data.get("_unreadable"):
            continue  # RT-10 reports the unreadable file
        items, papers, answers = data.get("items") or [], data.get("papers") or {}, data.get("answer") or {}
        rows = [r for key in CHAIN_ROWS for r in (answers.get(key) or []) if isinstance(r, dict) and "check" in r] if isinstance(answers, dict) else []
        for row in rows:
            if row.get("check") == "struck:A1":
                continue
            for number in row.get("evidence") or row.get("basis") or []:
                if not isinstance(number, int):
                    continue
                item = items[number - 1] if 1 <= number <= len(items) else None
                if item is None:
                    yield f"cached answer {name}: [{number}] is not an item"
                    continue
                paper = papers.get(item.get("paper_id")) or {}
                lacking = [k for k, ok in (("sentence_ids", item.get("sentence_ids")), ("section", item.get("sections")),
                                           ("page", item.get("pages")), ("paper card", paper),
                                           ("original-file status", "has_pdf" in paper)) if not ok]
                if lacking:
                    yield f"cached answer {name}: [{number}] ({item.get('id')}) cannot be resolved: lacks {', '.join(lacking)}"
    with _lab() as (root, con, cfg):
        gone = {"sha256": "0" * 64, "path": "sources/archive/pa.pdf", "status": "archived", "role": "main", "original_filename": "pa.pdf"}
        _add_paper(con, "pa", [("results", 4, "Alphaterm was higher in group A.")], source_files=[gone])
        collected, error = _raises(judge, con, cfg, ["alphaterm"], "one", False, {}, 12)
        if error:
            yield f"collect crashed: {error}"
            return
        items = collected[0]
        if not items:
            yield "the probe library gave no item"
        cards = find.paper_cards(con, {i.get("paper_id") for i in items})
        for number, item in enumerate(items, 1):
            sentences = [con.execute("SELECT section, page FROM sentences WHERE sentence_id = ?", (sid,)).fetchone()
                         for sid in item.get("sentence_ids") or []]
            source = con.execute("SELECT status FROM source_files WHERE paper_id = ?", (item.get("paper_id"),)).fetchone()
            card = cards.get(item.get("paper_id"))
            if not sentences or any(s is None or s["page"] is None or not s["section"] for s in sentences):
                yield f"item {number} ({item.get('id')}): a sentence_id does not resolve to a section and page"
            if card is None or "has_pdf" not in card:
                yield f"item {number} ({item.get('id')}): the paper card does not state whether the original is available"
            if source is None or source["status"] != "archived":
                yield f"item {number} ({item.get('id')}): the source file status of the paper does not resolve"
        checked = answer.check_parts({"answer": [{"text": "Alphaterm cao hơn ở nhóm A [#1].", "evidence": [1]}]}, items, cards)
        row = checked["answer"][0]
        if row.get("chunk_ids") != [items[0]["id"]] or "trang 4" not in checked["evidence"][0]["citation"]:
            yield f"the answer sentence does not resolve to its item and page (chunk_ids {row.get('chunk_ids')}, {checked['evidence'][0]['citation']})"
