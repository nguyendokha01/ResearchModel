"""Conformance checks of the stages SP (structured paper profiles), FG (figure / table readings) and NC (node cards)
of config/standard.yaml, see conform.py.

Data checks re-run the product validators on what is stored, one paper (or one card) at a time, nothing written:
SP-01..SP-06 call structure._wh_answers / _protocol_rows / _validate_overview / source_grounding on the stored
section_profile.json, protocol_profile.json and paper_overview.json; FG-01, FG-02, FG-05 call
figure_reading._validate_reply on the stored readings; NC-01 and NC-02 call node_cards._check_statement / _check_enums /
_check_structured_rows on the stored cards. A profile or reading made from an older source (stale) is not judged here:
`structure.completion` and `figure_reading.load` already report it as pending. The synthetic paper that the probes use is
the same one the test cases store (make_paper + submit_good), so the product code decides both verdicts.

Not implemented / only partly checked, and why:
- NC-04: only `card_check: passed` on the shown card is checked. The small line is `concept_notes.CARD_CHECK_LINE` (English since
  FORMAT_VERSION 18, VN-08); no vault check of it is written here yet.
- FG-04: the split of n/N (%) cells into value, count and n has no code yet; only the staleness half is checked.
- SP-03: the clause wants "not found in this paper" wording in every null; the data says "Not stated in the Introduction"
  and similar, so only the forbidden direction is checked (a null worded as absence from the literature).
- SP-09: kind `overview` is not checked: its chunks are whole-paper by design (structure.OVERVIEW_CHARS).
"""
import contextlib
import re
from pathlib import Path
from unittest import mock

from . import config, db, figure_reading, media, node_cards, standard, structure, workflow
from .conform import check
from .conform_evidence import figure_warnings_reach_units
from .conform_intake import _sandbox

WH, OVERVIEW = structure.WH, structure.OVERVIEW_FIELDS
# A denial ("not evidence that no protocols exist") is removed before the absence wording is searched.
DENIAL = re.compile(r"\b(?:not|no)\s+(?:an?\s+)?(?:evidence|statement|assertion|claim|proof|indication)\b[^.;]*", re.I)
# Wording that turns "not found in this paper" into "does not exist in the literature".
ABSENCE_CLAIM = re.compile(
    r"\bin\s+the\s+(?:scientific\s+|published\s+)?literature\b|\b(?:does|do|did)\s+not\s+exist\b|\bnever\s+(?:been\s+)?(?:studied|reported|investigated|described|performed)\b"
    r"|\bno\s+(?:\w+\s+)?(?:protocols?|stud(?:y|ies)|research|publications?)\s+(?:exists?|existed|(?:has|have)\s+been\s+(?:reported|published|performed))\b", re.I)
STORED = (("profile", "section_profile.json"), ("protocol", "protocol_profile.json"), ("overview", "paper_overview.json"))  # Paper attribute, file
NAME_NUMBER = figure_reading._NAME_NUMBER  # digits inside a name (SIRT2/5/6, Tuj-1, 5,6-dihydro) are not a quantity
READING_TEXT_KEYS = ("caption", "caption_sentences", "mention_sentences", "sentences", "text_sentences")  # a reading never carries these


# ---------------------------------------------------------------- helpers

@contextlib.contextmanager
def _at(root):
    """The product reads a paper at db.paper_dir(id) = config.PAPERS_DIR/id: point it at the papers folder of `root` (a store root)."""
    with mock.patch.object(config, "PAPERS_DIR", Path(root) / "papers"):
        yield


def _folder_root(paper):
    return paper.folder.parent.parent


def _stored(paper, name):
    value = getattr(paper, name)
    return value if isinstance(value, dict) else {}


def _absence(text):
    return ABSENCE_CLAIM.search(DENIAL.sub(" ", str(text or "")))


def _fresh(paper, profile):
    """The profile was made from the sentences and the source registration on disk (the test of structure._profile_report)."""
    with _at(_folder_root(paper)):
        return (profile.get("source_sha256") == workflow.fingerprint([paper.folder / "sentences.jsonl"])
                and not (profile.get("source_revision") and profile["source_revision"] not in structure._revision_candidates(paper.id)))


def _sources(paper, section):
    return {s["sentence_id"]: s for s in paper.sentences if s.get("section") == section}


def _wh_errors(paper):
    """structure._wh_answers over the stored answers (once per paper): (accepted, errors), or None when there is nothing to judge.
    Stored only after the validator returned, so a crash reaches every clause as an ERROR and never as a PASS."""
    if "_wh" not in paper._data:
        profile, result = _stored(paper, "profile"), None
        if profile and _fresh(paper, profile):
            with _at(_folder_root(paper)):
                result = structure._wh_answers(paper.id, profile.get("answers"), _sources(paper, "introduction"), copied=True)
        paper._data["_wh"] = result
    return paper._data["_wh"]


def _protocol_errors(paper):
    """One message per stored protocol that structure._protocol_rows refuses (checked alone: it stops at the first error);
    None when there is nothing to judge."""
    if "_protocol" not in paper._data:
        protocol, result = _stored(paper, "protocol"), None
        if protocol and _fresh(paper, protocol):
            sources, result = _sources(paper, "methods"), []
            if not isinstance(protocol.get("protocols"), list):
                result.append("protocols is not a list")
            for number, item in enumerate(protocol["protocols"] if not result else (), 1):
                try:
                    structure._protocol_rows({"protocols": [item]}, sources, copied=True)
                except (ValueError, TypeError, KeyError, AttributeError) as err:  # the verdicts of the product's own report
                    name = item.get("name") if isinstance(item, dict) else item
                    result.append(f"protocol {number} ({str(name)[:30]}): {err}")
        paper._data["_protocol"] = result
    return paper._data["_protocol"]


# ---------------------------------------------------------------- SP-01 .. SP-03: introduction profile (WH1, G1, A7)

@check("SP-01", "paper")
def wh_keys_and_sources(paper):
    """WH1 on the stored section_profile.json: exactly the five keys, every non-null answer cites Introduction sentences
    (quotes as code copied them), every null has a reason; an absent Introduction needs source_absence_reason."""
    profile = _stored(paper, "profile")
    if not profile or not _fresh(paper, profile):
        return
    if not _sources(paper, "introduction") and not str(profile.get("source_absence_reason") or "").strip():
        yield "no Introduction in the source and no source_absence_reason"
    accepted_errors = _wh_errors(paper)
    for error in accepted_errors[1]:
        if error["code"] == "WH1":
            yield f"{error.get('field', 'answers')}: {error['reason']}"


@check("SP-02", "paper")
def profile_text_is_grounded(paper):
    """G1 (structure.source_grounding) on every stored English profile text: the five answers and the overview items. An
    answer the answer checker (answer.check_sentence) rejects for another reason is listed here too."""
    profile = _stored(paper, "profile")
    if profile and _fresh(paper, profile):
        sources, abbreviations = _sources(paper, "introduction"), structure._abbreviation_inventory(paper.sentences)
        for key, answer in (profile.get("answers") or {}).items() if isinstance(profile.get("answers"), dict) else ():
            ids = answer.get("sentence_ids") if isinstance(answer, dict) and answer.get("text") else None
            if ids and isinstance(ids, list) and all(isinstance(sid, str) and sid in sources for sid in ids):
                bad = structure.source_grounding(answer["text"], [sources[sid]["text"] for sid in ids], abbreviations)["reject"]
                if bad:
                    yield f"{key}: not in the cited sentences: {', '.join(bad)}"
        for error in (_wh_errors(paper) or ({}, []))[1]:
            if error["code"] not in ("WH1", "G1"):
                yield f"{error.get('field')}: answer check {error['code']}: {error['reason']}"
    overview = _stored(paper, "overview")
    if overview and _fresh(paper, overview):
        with _at(_folder_root(paper)):
            rows = {r["sentence_id"]: r for r in structure._overview_rows(paper.id)}
        abbreviations = structure._abbreviation_inventory(paper.sentences)
        for field in OVERVIEW:
            for number, item in enumerate(overview.get(field) if isinstance(overview.get(field), list) else (), 1):
                ids = item.get("sentence_ids") if isinstance(item, dict) else None
                if isinstance(item, dict) and isinstance(item.get("text"), str) and isinstance(ids, list) and all(isinstance(sid, str) and sid in rows for sid in ids):
                    bad = structure.source_grounding(item["text"], [rows[sid]["text"] for sid in ids], abbreviations)["reject"]
                    if bad:
                        yield f"overview {field}[{number}]: not in the cited sentences: {', '.join(bad)}"


@check("SP-03", "paper")
def settings_are_kept_and_nulls_are_local(paper):
    """A7_setting warnings stored with an answer (the setting wording of who / where / when was shortened to the claim), and a
    null answer or source_absence_reason worded as absence from the literature (see the module note for what is not checked)."""
    profile = _stored(paper, "profile")
    if not profile:
        return
    for key, answer in (profile.get("answers") or {}).items() if isinstance(profile.get("answers"), dict) else ():
        if not isinstance(answer, dict):
            continue
        if "A7_setting" in (answer.get("warnings") or ()):
            yield f"{key}: A7_setting warning (a hedge of the source qualifies the setting)"
        if answer.get("text") is None and _absence(answer.get("reason")):
            yield f"{key}: null worded as absence from the literature: {str(answer.get('reason'))[:60]!r}"
    if _absence(profile.get("source_absence_reason")):
        yield f"source_absence_reason worded as absence from the literature: {str(profile.get('source_absence_reason'))[:60]!r}"


# ---------------------------------------------------------------- SP-04, SP-05: protocol profile

@check("SP-04", "paper")
def protocol_fragments_are_exact(paper):
    """structure._protocol_rows on each stored protocol: whole-token Methods fragments, ordered ids, code-copied quotes;
    plus the protocol list in Methods order."""
    problems = _protocol_errors(paper)
    yield from problems or ()
    if problems == []:
        sources = _sources(paper, "methods")
        firsts = [min(sources[sid]["seq"] for sid in p["sentence_ids"]) for p in _stored(paper, "protocol")["protocols"]]
        if firsts != sorted(firsts):
            yield "protocols are not stored in Methods source order"


@check("SP-05", "paper")
def protocol_covers_methods(paper):
    """Every Methods sentence is in a protocol or skipped with a reason (structure._protocol_rows), the stored coverage and
    status agree, and an absent Methods section has a source_absence_reason that does not claim no protocols exist."""
    protocol = _stored(paper, "protocol")
    if not protocol or not _fresh(paper, protocol):
        return
    sources = _sources(paper, "methods")
    if not sources:
        if not str(protocol.get("source_absence_reason") or "").strip():
            yield "no Methods in the source and no source_absence_reason"
        elif _absence(protocol["source_absence_reason"]):
            yield f"absence of Methods worded as absence of protocols: {protocol['source_absence_reason'][:60]!r}"
        return
    if _protocol_errors(paper):
        return  # SP-04 reports them; the coverage cannot be computed past an invalid protocol
    try:
        _, _, missing = structure._protocol_rows(protocol, sources, copied=True)
    except (ValueError, TypeError, KeyError, AttributeError) as err:
        yield f"skipped list: {err}"
        return
    if missing:
        yield f"{len(missing)} Methods sentences are neither in a protocol nor skipped: {', '.join(m.rsplit(':', 1)[-1] for m in missing[:5])}"
    if protocol.get("coverage_missing") != missing or protocol.get("status") != ("done" if not missing else "needs_fix"):
        yield f"stored coverage_missing / status ({protocol.get('status')}) differ from the source"


# ---------------------------------------------------------------- SP-06: overview

@check("SP-06", "paper")
def overview_is_reference_only(paper):
    """structure._validate_overview on every stored item (text, sentence ids, certainty that keeps the source hedge, G1), the
    overview is not empty, reference_only is true and overview_sha256 matches the items."""
    overview = _stored(paper, "overview")
    if not overview or not _fresh(paper, overview):
        return
    if overview.get("reference_only") is not True:
        yield "reference_only is not true"
    with _at(_folder_root(paper)):
        rows = {r["sentence_id"]: r for r in structure._overview_rows(paper.id)}
    abbreviations, clean = structure._abbreviation_inventory(paper.sentences), True
    for field in OVERVIEW:
        items = overview.get(field, [])
        for number, item in enumerate(items if isinstance(items, list) else [items], 1):
            try:
                structure._validate_overview({field: [item]}, rows, abbreviations)
            except (ValueError, TypeError, KeyError, AttributeError) as err:
                clean = False
                yield f"{field}[{number}]: {err}"
    if structure._overview_empty(overview):
        yield "the overview has no items"
    elif clean and overview.get("overview_sha256") != structure._digest(structure._validate_overview(overview, rows, abbreviations)):
        yield "overview_sha256 does not match the stored items (edited after submit)"


# ---------------------------------------------------------------- the synthetic paper (probes and test cases)

PID = "test2026_profile_a1b2"
ROWS = (("introduction", "Reactive oxygen species (ROS) may affect the test model."),
        ("introduction", "ROS seems to increase survival in the example model."),
        ("methods", "The example protocol used 5 mg for 2 hours."),
        ("methods", "The cells were then washed and incubated for 3 hours."),
        ("discussion", "Compared with earlier observations, the effect was slightly stronger."),
        ("conclusion", "This finding might support further experiments."))


def make_paper(papers_dir, paper_id=PID, rows=ROWS):
    """Write the synthetic paper (metadata + sentences) under `papers_dir/paper_id`; returns its folder."""
    folder = Path(papers_dir) / paper_id
    db.write_json(folder / "metadata.json", {"paper_id": paper_id, "title": "Artificial test example", "year": 2026, "authors": [],
                                             "source_files": [], "evidence_type": "primary_research", "extraction_version": 2})
    db.write_jsonl(folder / "sentences.jsonl", [
        {"sentence_id": f"{paper_id}:S{n:04d}", "seq": n, "passage_id": f"{paper_id}:{n:04d}", "page": 1, "section": section,
         "subsection": "Example protocol" if section == "methods" else None, "kind": "text", "source": "main", "text": text,
         "start": 0, "end": len(text), "citations": [], "mentions": []} for n, (section, text) in enumerate(rows, 1)])
    return folder


def _echo(task):
    return {k: task[k] for k in ("source_sha256", "source_revision", "revision_of")}


def submit_good(paper_id=PID):
    """Store a good WH profile, protocol profile and overview with the real submit functions (needs config.PAPERS_DIR and
    config.STORE of the paper's project)."""
    ids = lambda *n: [f"{paper_id}:S{i:04d}" for i in n]
    task = structure.prepare(paper_id)
    null = {"text": None, "reason": "Not stated in the Introduction"}
    reply = {**_echo(task), "answers": {"what": {"text": task["sentences"][0]["text"], "sentence_ids": ids(1)},
                                        "why": {"text": task["sentences"][1]["text"], "sentence_ids": ids(2)},
                                        "who": null, "where": null, "when": null}}
    assert structure.submit_profile(None, paper_id, reply, "conform/probe")["ok"]
    task = structure.prepare_protocol(paper_id)
    reply = {**_echo(task), "protocols": [{"name": "example protocol", "sentence_ids": ids(3, 4),
                                           "details": [{"field": "dose", "value": "5 mg", "sentence_ids": ids(3)}],
                                           "steps": [{"sentence_ids": ids(3)}, {"sentence_ids": ids(4)}]}], "skipped": []}
    assert structure.submit_protocol(None, paper_id, reply, "conform/probe")["ok"]
    task = structure.prepare_overview(paper_id)
    reply = {**_echo(task), "research_question": [{"text": task["sentences"][1]["text"], "sentence_ids": ids(2), "certainty": "seems"}],
             "main_findings": [{"text": task["sentences"][5]["text"], "sentence_ids": ids(6), "certainty": "might"}]}
    assert structure.submit_overview(None, paper_id, reply, "conform/probe")["ok"]


@contextlib.contextmanager
def _world(rows=ROWS):
    """A throw-away project (conform_intake._sandbox) holding the synthetic paper: (root, con)."""
    with _sandbox(vaults=()) as (root, con):
        make_paper(config.PAPERS_DIR, rows=rows)
        yield root, con


# ---------------------------------------------------------------- SP-07 .. SP-09: echoes, completion, chunks (data + probe)

def _echo_problems(paper):
    for attribute, name in STORED:
        profile = _stored(paper, attribute)
        if not profile:
            continue
        if profile.get("paper_id") != paper.id or profile.get("schema_version") != 1:
            yield f"{name}: paper_id or schema_version differs"
        if not re.fullmatch(r"[0-9a-f]{64}", str(profile.get("source_sha256") or "")):
            yield f"{name}: source_sha256 missing"
        if not str(profile.get("source_revision") or "").strip():
            yield f"{name}: source_revision missing"


@check("SP-07", "store")
def stale_replies_are_refused(store, submit=structure.submit_structured, submit_profile=structure.submit_profile):
    """Data: every stored profile echoes source_sha256 and source_revision of its paper. Probe: after the source sentences
    change, a structured task and a profile reply made for the old source are refused and nothing is stored."""
    for paper_id in store.paper_ids:
        for problem in _echo_problems(store.paper(paper_id)):
            yield f"{paper_id}: {problem}"
    with _world() as (root, con):
        stale_profile = structure.prepare(PID)
        task = structure.prepare_structured(PID, "wh")
        text = (config.PAPERS_DIR / PID / "sentences.jsonl").read_text(encoding="utf-8")
        (config.PAPERS_DIR / PID / "sentences.jsonl").write_text(text.replace("survival", "viability"), encoding="utf-8", newline="")
        null = {"text": None, "reason": "Not stated in the Introduction"}
        answers = {key: null for key in WH}
        for name, call in (("structured task", lambda: submit(con, PID, task["task_id"], task["chunk"]["chunk_id"],
                                                               {**_echo(task), "answers": answers})),
                           ("profile reply", lambda: submit_profile(con, PID, {**_echo(stale_profile), "answers": answers}, "probe"))):
            try:
                call()
                yield f"probe: a {name} made for an old source was accepted"
            except ValueError:
                pass
        if (config.PAPERS_DIR / PID / "section_profile.json").exists():
            yield "probe: a profile was stored for an old source"


@check("SP-08", "store")
def completion_is_technical_only(store, completion=structure.completion):
    """Data: no stored profile or completion report says anything but scientific_acceptance not_assessed, and
    `complete` equals (WH ready and protocol ready). Probe: complete with both parts current, pending after the source
    changes or a part is missing."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        for attribute, name in STORED:
            if _stored(paper, attribute).get("scientific_acceptance") not in (None, "not_assessed"):
                yield f"{paper_id}: {name} says scientific_acceptance {_stored(paper, attribute)['scientific_acceptance']!r}"
        with _at(store.root):
            report = completion(paper_id)
        if report["scientific_acceptance"] != "not_assessed":
            yield f"{paper_id}: completion says scientific_acceptance {report['scientific_acceptance']!r}"
        if report["complete"] != (report["wh"]["ready"] and report["protocol"]["ready"]):
            yield f"{paper_id}: complete={report['complete']} but WH ready={report['wh']['ready']}, protocol ready={report['protocol']['ready']}"
    with _world() as (root, con):
        folder = config.PAPERS_DIR / PID
        first = completion(PID)
        if first["complete"] or first["pending"] != ["introduction_wh", "protocols"]:
            yield f"probe: a paper without profiles reports complete={first['complete']} pending={first['pending']}"
        submit_good()
        good = completion(PID)
        if not good["complete"] or good["scientific_acceptance"] != "not_assessed":
            yield f"probe: a paper with current profiles is not complete: {good['pending']}"
        (folder / "protocol_profile.json").unlink()
        if completion(PID)["complete"]:
            yield "probe: complete without a protocol profile"
        (folder / "sentences.jsonl").write_text((folder / "sentences.jsonl").read_text(encoding="utf-8").replace("5 mg", "50 mg"),
                                                encoding="utf-8", newline="")
        if completion(PID)["complete"]:
            yield "probe: complete after the source changed"


def _chain_cuts(chunks, rows):
    """Ids of rows whose `continues` target is in another chunk."""
    where = {sid: n for n, chunk in enumerate(chunks) for sid in chunk}
    return [r["sentence_id"] for r in rows if r.get("continues") in where and where[r["continues"]] != where.get(r["sentence_id"])]


def _chunk_problems(chunks, texts, rows):
    limits = (standard.param("SP-09", "max_sentences"), standard.param("SP-09", "max_chars"))
    for number, chunk in enumerate(chunks, 1):
        chars = sum(len(texts.get(sid, "")) for sid in chunk)
        if len(chunk) > limits[0] or chars > limits[1]:
            yield f"chunk {number} has {len(chunk)} sentences and {chars} characters (limits {limits[0]} / {limits[1]})"
    for sid in _chain_cuts(chunks, rows):
        yield f"{sid.rsplit(':', 1)[-1]} is cut from the sentence it continues into"


def _chunk_rows():
    """Rows whose chains sit on the sentence-count boundary and on the character boundary of the limits."""
    sentences, chars = standard.param("SP-09", "max_sentences"), standard.param("SP-09", "max_chars")
    count = 3 * sentences + 5
    rows = [{"sentence_id": f"p:S{n:04d}", "text": "x" * (chars // sentences if n < count - 12 else chars // 10), "source": "main", "subsection": None,
             "seq": n} for n in range(1, count + 1)]
    for n in (sentences, 2 * sentences, count - 10):  # row n continues into row n + 1
        rows[n - 1]["continues"] = rows[n]["sentence_id"]
    return rows


@check("SP-09", "store")
def chunks_stay_inside_limits(store, chunkers=(structure._chunks, structure._protocol_chunks)):
    """Data: the chunks of the current stored structured tasks (kinds wh and protocol) hold at most max_sentences sentences and
    max_chars characters and cut no `continues` chain. Probe: the chunkers do the same on rows built to sit on both limits."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        texts, folder = {s["sentence_id"]: s.get("text", "") for s in paper.sentences}, paper.folder / "structured_tasks"
        for path in sorted(folder.glob("*.json")) if folder.is_dir() else ():
            state = db.read_json(path, {}) or {}
            with _at(store.root):
                current = state.get("source_revision") in structure._revision_candidates(paper_id)
            if not current or state.get("kind") not in ("wh", "protocol"):
                continue
            chunks = [c.get("sentence_ids") or [] for c in state.get("chunks") or [] if isinstance(c, dict)]
            for problem in _chunk_problems(chunks, texts, [s for s in paper.sentences if s.get("continues")]):
                yield f"{paper_id} task {path.stem[:8]} ({state['kind']}): {problem}"
    rows = _chunk_rows()
    texts = {r["sentence_id"]: r["text"] for r in rows}
    for chunker in chunkers:
        for problem in _chunk_problems(chunker(rows), texts, rows):
            yield f"probe {getattr(chunker, '__name__', chunker)}: {problem}"


# ---------------------------------------------------------------- FG: figure and table readings

def _readings(paper):
    """(object local id, stored reading) of figure_readings/*.json."""
    folder = paper.folder / "figure_readings" if paper.folder else None
    for path in sorted(folder.glob("*.json")) if folder and folder.is_dir() else ():
        reading = db.read_json(path, {})
        if isinstance(reading, dict):
            yield path.stem, reading


def _provenance_rows(paper, reading):
    """The caption and mention sentences the reading was shown (current text), None for an id that no longer exists."""
    by_id = {s["sentence_id"]: s for s in paper.sentences}
    prov = reading.get("provenance") or {}
    return [by_id.get(sid) for key in ("caption_sentence_ids", "mention_sentence_ids") for sid in prov.get(key) or ()], by_id


def _text_changed(paper, reading):
    """True when the sentences the reading was shown differ from the current ones (then it is stale, not judged)."""
    if not reading.get("cited_text_sha256"):
        return False  # a legacy reading is judged against the broad revision by figure_reading.load
    rows, _ = _provenance_rows(paper, reading)
    prov = reading.get("provenance") or {}
    cut = len(prov.get("caption_sentence_ids") or ())
    return None in rows or figure_reading._cited_digest(rows[:cut], rows[cut:]) != reading["cited_text_sha256"]


def _replay(paper, reading):
    """(kind, claim sentence id, message) for each way the stored reading fails figure_reading._validate_reply. The payload is
    rebuilt from the paper's sentences (every id the reading cites counts as shown to it); `kind` is "note" for a number or
    an unknown sentence id in a consistency note and "structure" for the rest."""
    rows, by_id = _provenance_rows(paper, reading)
    checks = reading.get("caption_consistency")
    if not isinstance(checks, list) or not all(isinstance(c, dict) for c in checks):
        if reading.get("crop_wrong") is not True:
            yield "structure", None, "caption_consistency must be a list of objects"
            return
        checks = []
    ids = {r["sentence_id"] for r in rows if r} | {c.get("claim_sentence_id") for c in checks if c.get("claim_sentence_id") in by_id}
    shown = [{"sentence_id": sid, "text": by_id[sid].get("text", "")} for sid in sorted(ids)]
    payload = {"allowed_sentence_ids": sorted(ids), "caption_sentences": shown, "mention_sentences": [], "table_data_sentences": []}
    changed = _text_changed(paper, reading)
    summary = reading.get("summary")  # the numbers of the summary are FG-05's: the structure replay reads it without its text
    blank = {**summary, "text": ""} if isinstance(summary, dict) and isinstance(summary.get("text"), str) else summary
    for kind, part, claim in [("structure", {**reading, "caption_consistency": [], "summary": blank}, None)] + (
            [("note", {**reading, "caption_consistency": [c], "summary": blank}, c.get("claim_sentence_id")) for c in checks] if not changed else []):
        try:
            figure_reading._validate_reply(part, payload)
        except ValueError as err:
            yield kind, claim, str(err)


# synthetic figure for the probes: a paper with a caption, a mention and a crop file, and a media bundle read from them
FIGURE_ROWS = (("results", "Figure 1. Cells were counted in group A and group B.", "caption"),
               ("results", "Cell number was 12 in Figure 1.", "text"))


def _bundle_for(paper_id):
    """Stands for media.bundles: the bundle of object F1 built from the files of the (sandbox) paper as they are now."""
    folder = config.PAPERS_DIR / paper_id
    rows = list(db.read_jsonl(folder / "sentences.jsonl"))
    crop = folder / "crops" / "F1.png"
    return {"object_id": f"{paper_id}:F1", "label": "Figure 1", "kind": "figure", "caption": [r for r in rows if r["kind"] == "caption"],
            "mentions": [r for r in rows if r["kind"] != "caption"], "data": [], "table": None, "source_locator": {"revision": "r1"},
            "crop": {"path": "crops/F1.png", "sha256": media._hash(crop), "status": "available"}}


def make_figure_paper(papers_dir):
    """The synthetic paper with a caption, a mention and the crop file of object F1; returns its folder."""
    folder = make_paper(papers_dir, rows=tuple((s, t) for s, t, _ in FIGURE_ROWS))
    db.write_jsonl(folder / "sentences.jsonl", [{**r, "kind": kind} for r, (_, _, kind) in zip(db.read_jsonl(folder / "sentences.jsonl"), FIGURE_ROWS)])
    set_crop(folder, b"crop-v1")
    return folder


def set_crop(folder, data):
    (folder / "crops").mkdir(exist_ok=True)
    (folder / "crops" / "F1.png").write_bytes(data)


def fake_media():
    """media.bundles is replaced by _bundle_for for the probes and the test cases (there is no PDF to locate the figure in)."""
    return mock.patch.object(figure_reading, "_bundle", lambda paper_id, object_id: _bundle_for(paper_id))


@contextlib.contextmanager
def _figure_world():
    """A throw-away project with the figure paper: yields (paper id, change_crop)."""
    with _sandbox(vaults=()), fake_media():
        folder = make_figure_paper(config.PAPERS_DIR)
        yield PID, lambda data: set_crop(folder, data)


def _good_reading(**over):
    return {"panels": [{"panel": "A", "chart_kind": "bar", "groups": ["A", "B"], "measure": "Cell number", "unit": None,
                        "values": [{"group": "A", "value": 12, "error": None, "n": None, "read_quality": "exact"}], "significance_marks": []}],
            "caption_consistency": [{"claim_sentence_id": f"{PID}:S0002", "status": "consistent", "note": "Panel A shows 12 cells."}],
            "summary": {"text": "Bars for group A and group B; group A reaches 12.", "panel_refs": ["A"]}, **over}


@check("FG-01", "store")
def readings_match_the_crop_or_say_it_is_wrong(store, submit=figure_reading.submit, load=figure_reading.load):
    """Data: a stored crop_wrong reading has a note and reads nothing (no panels, no consistency items); any other reading passes
    figure_reading._validate_reply (structure). Probe: crop_wrong without a note is refused; with a note it is stored empty, never
    raises a warning, is current while the crop is the same and stale once the crop is rebuilt (so the object returns to the queue).
    Whether a reading describes the cropped image cannot be judged without a vision model."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        for local, reading in _readings(paper):
            if reading.get("crop_wrong") is True:
                if not str(reading.get("note") or "").strip() or reading.get("panels") or reading.get("caption_consistency"):
                    yield f"{paper_id}:{local}: crop_wrong without a note, or with a reading"
            for kind, _, message in _replay(paper, reading):
                if kind == "structure":
                    yield f"{paper_id}:{local}: {message}"
    with _figure_world() as (pid, change_crop):
        try:
            submit(pid, "F1", {"crop_wrong": True}, "probe")
            yield "probe: crop_wrong without a note was accepted"
        except ValueError:
            pass
        stored = submit(pid, "F1", {"crop_wrong": True, "note": "The crop shows a table, not the figure."}, "probe")
        if stored.get("panels") or stored.get("caption_consistency") or figure_reading.warning(stored):
            yield "probe: a crop_wrong reading stored a reading or a warning"
        if (load(pid, "F1") or {}).get("status") != "current":
            yield "probe: a crop_wrong reading is not current while the crop is unchanged"
        change_crop(b"crop-v2")
        if (load(pid, "F1") or {}).get("status") != "stale":
            yield "probe: a crop_wrong reading stays current after the crop was rebuilt"


@check("FG-02", "paper")
def note_numbers_come_from_source(paper):
    """Every number in a stored caption_consistency note occurs in the cited sentence or a panel value (figure_reading._validate_reply
    rebuilt on the paper's sentences; labels such as Figure 1 or IL-6 are not numbers). A reading made from older sentences is not judged."""
    for local, reading in _readings(paper):
        for kind, claim, message in _replay(paper, reading):
            if kind == "note":
                yield f"{local}: {str(claim).rsplit(':', 1)[-1]}: {message}"


@check("FG-03", "store")
def inconsistency_is_shown_everywhere(store, warnings=figure_reading.sentence_warnings):
    """For every stored reading with a warning (figure_reading.warning): its claim sentences are in `sentence_warnings` (the answer
    WARNING line), the evidence units that cite them carry the figure flag (the EV-20 check), and the paper note of each vault that
    has the paper shows the warning line. Not checked: node notes and the dashboard (no stable marker to look for)."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        flagged = [(local, r) for local, r in _readings(paper) if figure_reading.warning(r)]
        if not flagged:
            continue
        with _at(store.root):
            shown = warnings(paper_id)
        for local, reading in flagged:
            for c in reading.get("caption_consistency") or ():
                if c.get("status") == "inconsistent" and c.get("claim_sentence_id") and figure_reading.WARNING_LABEL not in shown.get(c["claim_sentence_id"], ""):
                    yield f"{paper_id}:{local}: sentence_warnings lacks {c['claim_sentence_id'].rsplit(':', 1)[-1]}"
        for message in figure_warnings_reach_units(paper):
            yield message
        for vault in store.vaults:
            notes = [text for path, text in vault.notes if path.startswith("Papers/") and f"paper_id: {paper_id}\n" in text]
            for local, reading in flagged:
                if notes and not any(figure_reading.warning(reading) in text for text in notes):
                    yield f"{vault.name}/{paper_id}:{local}: the paper note does not show the warning"
            vault._data.pop("notes", None)


@check("FG-04", "store")
def stale_readings_are_labelled(store, load=figure_reading.load):
    """Data: a stored reading whose crop file (provenance.crop_path) or cited sentence texts no longer match its hashes is stale, and
    figure_reading.load must say so. Probe: a fresh reading is current; a changed crop and changed caption text each make it stale.
    Not checked: the split of n/N (%) cells (no code yet)."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        for local, reading in _readings(paper):
            why = (["crop"] if media._hash((reading.get("provenance") or {}).get("crop_path") or "") != reading.get("crop_sha256") else []) + (
                ["cited text"] if _text_changed(paper, reading) else [])
            if why:
                with _at(store.root):
                    state = load(paper_id, reading.get("object_id") or local)
                if state and state.get("status") != "stale":
                    yield f"{paper_id}:{local}: the {' and '.join(why)} changed but load() says {state.get('status')}"
    with _figure_world() as (pid, change_crop):
        figure_reading.submit(pid, "F1", _good_reading(), "probe")
        if (load(pid, "F1") or {}).get("status") != "current":
            yield "probe: a fresh reading is not current"
        change_crop(b"crop-v2")
        if (load(pid, "F1") or {}).get("status") != "stale":
            yield "probe: a reading stays current after the crop changed"
        change_crop(b"crop-v1")
        folder = config.PAPERS_DIR / pid
        (folder / "sentences.jsonl").write_text((folder / "sentences.jsonl").read_text(encoding="utf-8").replace("group A", "group C"),
                                                encoding="utf-8", newline="")
        if (load(pid, "F1") or {}).get("status") != "stale":
            yield "probe: a reading stays current after its caption changed"


def _summary_numbers(paper, reading):
    """Numbers of the reading's summary text that occur in no cited sentence and no panel (value, error, n, measure, groups)."""
    summary = reading.get("summary")
    if not isinstance(summary, dict) or not isinstance(summary.get("text"), str):
        return set()
    rows, by_id = _provenance_rows(paper, reading)
    cited = [r["text"] for r in rows if r] + [by_id[c["claim_sentence_id"]]["text"] for c in reading.get("caption_consistency") or ()
                                               if isinstance(c, dict) and c.get("claim_sentence_id") in by_id]
    cited += [s["text"] for s in paper.sentences if reading.get("object_id") and s.get("object_id") == reading["object_id"] and s.get("kind") != "caption"]
    panels = [p for p in reading.get("panels") or () if isinstance(p, dict)]
    shown = cited + [str(p.get(k) or "") for p in panels for k in ("measure", "unit", "groups")] + [
        str(v.get(k)) for p in panels for v in p.get("values") or () if isinstance(v, dict) for k in ("value", "error", "n", "group") if v.get(k) is not None]
    known = {n for text in shown for n, _ in figure_reading._number_tokens(text)}
    claims = NAME_NUMBER.sub(" ", figure_reading._LABEL.sub(" ", summary["text"]))
    return {n for n, _ in figure_reading._number_tokens(claims)} - known


@check("FG-05", "store")
def reading_never_replaces_the_source(store, submit=figure_reading.submit):
    """Data: a stored reading carries no caption or sentence text of its own, and every number of its summary occurs in a cited
    sentence or a panel. Probe: storing a reading leaves sentences.jsonl, figures.jsonl, tables.jsonl and the crop byte-identical."""
    for paper_id in store.paper_ids:
        paper = store.paper(paper_id)
        for local, reading in _readings(paper):
            for key in READING_TEXT_KEYS:
                if key in reading:
                    yield f"{paper_id}:{local}: the reading carries {key!r}"
            if not _text_changed(paper, reading):
                numbers = sorted(_summary_numbers(paper, reading))
                if numbers:
                    yield f"{paper_id}:{local}: summary number(s) {', '.join(numbers)} in no cited sentence or panel"
    with _figure_world() as (pid, change_crop):
        folder = config.PAPERS_DIR / pid
        names = ("sentences.jsonl", "figures.jsonl", "tables.jsonl", "crops/F1.png")
        before = {n: (folder / n).read_bytes() if (folder / n).exists() else None for n in names}
        submit(pid, "F1", _good_reading(), "probe")
        for name in names:
            after = (folder / name).read_bytes() if (folder / name).exists() else None
            if after != before[name]:
                yield f"probe: storing a reading changed {name}"
        stored = db.read_json(folder / "figure_readings" / "F1.json", {})
        if not stored or any(key in stored for key in READING_TEXT_KEYS):
            yield "probe: the stored reading is missing or carries source text"
        try:
            submit(pid, "F1", _good_reading(caption_consistency=[{"claim_sentence_id": f"{PID}:S0002", "status": "consistent", "note": "Panel A shows 91 cells."}]), "probe")
            yield "probe: a note with a number read from nowhere was stored"
        except ValueError:
            pass


# ---------------------------------------------------------------- NC: node cards

NC02_CODES = {"hedge", "hedge_dropped", "attribution", "review_attribution", "A3", "A4", "A7"}


def _cards(store):
    """(vault, file name, stored card) of every accepted card, one file at a time."""
    base = store.root / "node_cards"
    for folder in sorted(p for p in base.iterdir() if p.is_dir()) if base.is_dir() else ():
        for path in sorted(folder.glob("*.json")):
            stored = db.read_json(path)
            if isinstance(stored, dict) and stored.get("status") == "accepted" and isinstance(stored.get("card"), dict):
                yield folder.name, path.name, stored


def _card_problems(store, vault, name, stored):
    """The problems of one card as node_cards.submit would report them, from the card's own copied quotes (cached per card)."""
    key = ("_card_problems", vault, name)
    if key not in store._data:
        card, schema = stored["card"], node_cards._schema()
        fields = card.get("fields") if isinstance(card.get("fields"), dict) else {}
        quotes = card.get("quotes") if isinstance(card.get("quotes"), dict) else {}
        source_map = {sid: {"id": sid, "sentence_id": sid, "paper_id": q.get("paper_id") or sid.split(":")[0], "text": q.get("text", ""),
                            "hedges": structure.uncertainty(q.get("text", ""))} for sid, q in quotes.items() if isinstance(q, dict)}
        with _at(store.root):
            payload = {"node": {"name": card.get("name"), "aliases": card.get("aliases") or [], "candidate_type": card.get("node_type")},
                       "papers": [node_cards._paper_info(p) for p in sorted({r["paper_id"] for r in source_map.values()})], "schema": schema}
            problems = [node_cards._problem("no_fields", "fields", "The card has no fields object")] if not fields else []
            for field in (k for k in schema.get("common_fields") or {} if k != "related"):
                value = fields.get(field)
                if not isinstance(value, dict) or not ("text" in value or node_cards._is_not_found(value)):
                    problems.append(node_cards._problem("missing_field", f"fields.{field}", "Neither a statement nor not_found_in_vault"))
            for path, statement in node_cards._iter_statements(fields):
                problems.extend(node_cards._check_statement(path, statement, source_map, payload))
                if "quote" in statement or "quotes" in statement:
                    problems.append(node_cards._problem("typed_quote", path, "A statement carries a quote: quotes are copied by code"))
            problems.extend(node_cards._check_enums(fields, card.get("node_type"), schema, source_map))
            problems.extend(node_cards._check_structured_rows(fields, card.get("node_type"), source_map, payload))
        store._data[key] = problems
    return store._data[key]


def _card_checker(clause, in_nc02, doc):
    def run(store):
        for vault, name, stored in _cards(store):
            for p in _card_problems(store, vault, name, stored):
                if (p["code"] in NC02_CODES) == in_nc02:
                    yield f"{vault}/{name[:-5]}: {p['code']} {p['path']}: {p['detail'][:80]}"
    run.__doc__ = doc
    return check(clause, "store")(run)


_card_checker("NC-01", False, "Every stored accepted card: all common fields are a statement or not_found_in_vault, every statement and "
              "enum / row cites sentence ids that are among the card's code-copied quotes (node_cards._check_statement / _check_enums / "
              "_check_structured_rows on the card's own quotes), no statement carries a quote of its own, and the answer checker "
              "(A1, A2, A6) accepts the text. Cards that are stale are still judged against the quotes they hold.")
_card_checker("NC-02", True, "The hedge, attribution and A3 / A4 / A7 codes of node_cards._check_statement on every stored accepted card "
              "(source hedge kept, review support attributed as review / cited_study, no causal upgrade).")


@check("NC-03", "store")
def card_currency_follows_quotes_and_inventory(store, submit=node_cards.submit, load_status=node_cards.load_status):
    """Data: every accepted card records the paper and unit inventory of its node and its source_revision. Probe (a synthetic node): a fresh
    card is current, also when the task was capped; a new paper or unit flips it to current_with_new_evidence; a changed cited quote
    makes it stale."""
    for vault, name, stored in _cards(store):
        lacking = [key for key in ("paper_ids", "unit_ids") if not isinstance(stored.get(key), list)]
        if lacking:
            yield f"{vault}/{name[:-5]}: no {' / '.join(lacking)} inventory recorded at submit"
    with _node_world() as (con, change):
        reply = _good_card_reply(con)
        if not submit(con, "v", "EG", reply, "probe")["ok"]:
            yield "probe: the good card was refused"
            return
        for label, want in (("a fresh card", "current"), ("a card with a new unit", "current_with_new_evidence"),
                            ("a card with a new paper", "current_with_new_evidence"), ("a card whose cited quote changed", "stale")):
            if label == "a card with a new unit":
                change("unit")
            elif label == "a card with a new paper":
                change("paper")
            elif label == "a card whose cited quote changed":
                change("quote")
            got = (load_status("v", "local:eg") or {}).get("card_currency")
            if got != want:
                yield f"probe: {label} is {got}, expected {want}"
    with _node_world() as (con, change), mock.patch.object(node_cards, "MAX_SENTENCES", 1):
        change("methods unit")  # the capped task keeps one sentence and drops this unit's sentence; the card still covers the node's units
        if submit(con, "v", "EG", _good_card_reply(con), "probe")["ok"] and (load_status("v", "local:eg") or {}).get("card_currency") != "current":
            yield "probe: a capped card is not current right after submit"


@check("NC-04", "probe")
def probe_shown_card_says_card_check_passed(load=node_cards.load):
    """A card the reader sees (node_cards.load on a synthetic node, one code path for every card) has card_check: passed and no
    card_status for the researcher to tick. Not checked: the small line `concept_notes.CARD_CHECK_LINE` in the note. Walking the real cards would cost about a second each."""
    with _node_world() as (con, change):
        if not node_cards.submit(con, "v", "EG", _good_card_reply(con), "probe")["ok"]:
            yield "probe: the good card was refused"
            return
        card = load("v", "local:eg")
        if card is None:
            yield "probe: a fresh card is not shown"
        elif card.get("card_check") != "passed":
            yield f"probe: the shown card has card_check {card.get('card_check')!r}, not 'passed'"
        if card is not None and "card_status" in card:
            yield f"probe: the shown card still has card_status {card['card_status']!r}"


def _fake_payload(paper_id, total):
    rows = [{"id": f"{paper_id}:S{n:04d}", "sentence_id": f"{paper_id}:S{n:04d}", "paper_id": paper_id, "seq": n, "page": 1,
             "text": f"Direct {n}", "kind": "text", "hedges": [], "direct_mention": True, "evidence_sentence": False, "context": False}
            for n in range(1, total + 1)]
    rows[0]["evidence_sentence"] = True
    ids = [r["id"] for r in rows]
    return ({"paper": {"paper_id": paper_id, "label": paper_id, "year": 2024, "evidence_type": "primary"}, "sentences": rows,
             "evidence_units": [], "captions": [], "mandatory_ids": ids[:1], "context_ids": [],
             "priority_ids": {"evidence_unit": ids[:1], "protocol_variant": [], "definition_abbreviation_caption": [],
                              "direct_mention": ids, "context": []}, "include_allowed_ids": ids}, {})


@check("NC-05", "probe")
def probe_task_cap_and_includes(task=node_cards.task):
    """Two papers with max_sentences direct sentences each (twice the cap): the task keeps at most max_sentences, keeps an eligible
    include_sentence_id that the round robin would drop, and reports an ineligible one."""
    cap = standard.param("NC-05", "max_sentences")
    registry = type("Registry", (), {"by_id": {"local:x": {"concept_id": "local:x", "name": "X", "type": "concept", "synonyms": []}},
                                      "lookup": lambda self, value: self.by_id.get(value)})()
    late, absent = f"p1:S{cap - 1:04d}", f"p1:S{cap + 99:04d}"
    with mock.patch.object(node_cards, "_registry", return_value=registry), \
            mock.patch.object(node_cards, "_node_inputs", return_value=([], ["p1", "p2"], [])), \
            mock.patch.object(node_cards, "_protocol_variants", return_value=[]), \
            mock.patch.object(node_cards, "_paper_payload", side_effect=[_fake_payload("p1", cap), _fake_payload("p2", cap)]), \
            mock.patch.object(node_cards, "_source_revision", return_value="rev"), \
            mock.patch.object(node_cards, "_schema", return_value={"allowed_types": node_cards.ALLOWED_TYPES}):
        result = task(object(), "v", "local:x", [late, absent])
    ids = {row["id"] for row in result["sentences"]}
    if len(ids) > cap:
        yield f"probe: the task holds {len(ids)} sentences, the cap is {cap}"
    if late not in ids:
        yield f"probe: include_sentence_ids {late} (eligible) was dropped"
    if absent not in result["trimmed"]["rejected_include_sentence_ids"]:
        yield f"probe: include_sentence_ids {absent} (not eligible) was not reported as rejected"


# ---------------------------------------------------------------- a synthetic node (NC-03 probe)

_REAL_CONFIG = Path(config.__file__).resolve().parents[2] / "config"  # the node card schema is read from the project, not the sandbox
NODE_ROWS = (("introduction", "Ethylene glycol (EG) may increase survival by 10%."), ("methods", "EG was used in the freezing protocol."),
             ("methods", "EG was used at 15 mg for 5 min."), ("results", "EG was associated with survival."))


class _Registry:
    def __init__(self, records):
        self.records, self.by_id = records, {r["concept_id"]: r for r in records}

    def lookup(self, value):
        return self.by_id.get(value) or next((r for r in self.records if r["name"].casefold() == str(value).casefold()), None)


def _node_paper(paper_id, rows):
    folder = make_paper(config.PAPERS_DIR, paper_id, rows)
    db.write_jsonl(folder / "evidence.jsonl", [{"unit_id": f"{paper_id}:E1", "seq": 1, "unit_type": "result", "status": "ok",
                                                "statement": "EG may increase survival.", "quote": "ignored",
                                                "sentence_ids": [f"{paper_id}:S0001"], "outcome": {"values": ["10%"]}}])
    db.write_jsonl(folder / "figures.jsonl", [])
    db.write_jsonl(folder / "tables.jsonl", [])


@contextlib.contextmanager
def _node_world():
    """A throw-away project with one node EG linked to paper p of vault v; yields (con, change) where change(what) edits the
    project: "unit" (a new evidence unit), "paper" (a new linked paper), "quote" (the cited sentence text), "methods unit"."""
    with _sandbox(vaults=()) as (root, con), mock.patch.object(config, "CONFIG_DIR", _REAL_CONFIG):
        _node_paper("p", NODE_ROWS)
        con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES('p','v')")
        con.execute("INSERT INTO concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id) VALUES(?,?,?,?,?,?)",
                    ("local:eg", "p", "studied", 4, "introduction,methods,results", "p:S0001"))
        con.commit()
        registry = _Registry([{"concept_id": "local:eg", "name": "EG", "type": "concept", "synonyms": ["ethylene glycol"], "parents": []}])

        def change(what):
            folder = config.PAPERS_DIR / "p"
            if what in ("unit", "methods unit"):
                sid = "p:S0003" if what == "methods unit" else "p:S0004"
                units = list(db.read_jsonl(folder / "evidence.jsonl"))
                units.append({"unit_id": "p:E2", "seq": 2, "unit_type": "result", "status": "ok", "statement": "EG was used for 5 min.",
                              "quote": "ignored", "sentence_ids": [sid], "outcome": {"values": []}})
                db.write_jsonl(folder / "evidence.jsonl", units)
            elif what == "paper":
                _node_paper("new", (("results", "EG was also evaluated in a second paper."),))
                con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES('new','v')")
                con.execute("INSERT INTO concept_links VALUES(?,?,?,?,?,?)", ("local:eg", "new", "studied", 1, "results", "new:S0001"))
                con.commit()
            else:
                rows = list(db.read_jsonl(folder / "sentences.jsonl"))
                rows[0]["text"] += " Revised."
                db.write_jsonl(folder / "sentences.jsonl", rows)

        with mock.patch.object(node_cards, "_registry", return_value=registry):
            yield con, change


def _good_card_reply(con):
    task = node_cards.task(con, "v", "EG")
    statement = {"text": "EG may increase survival by 10%.", "sentence_ids": ["p:S0001"], "attribution": "primary", "hedge": "hedged"}
    fields = {k: {"status": "not_found_in_vault"} for k in node_cards._schema()["common_fields"] if k != "related"}
    return {"source_revision": task["source_revision"], "node_type": "concept", "fields": {**fields, "definition": statement}, "related": []}
