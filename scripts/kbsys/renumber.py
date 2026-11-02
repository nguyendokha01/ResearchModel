"""Keep saved analysis attached to the same verbatim sentence when Tier 1 is rebuilt.

Sentence IDs are sequential, so one inserted or removed sentence renumbers every later one. An old
sentence is the "same" sentence when a new one has the same source, page, section, kind and text
(`id_map`). `carry_over` (the one function `tier1.build` calls) then

  1. rewrites the IDs in every stored artifact that cites this paper's sentences and is read back by code;
  2. re-stamps the profile hashes of profiles that were current before and cite only mapped sentences
     (a profile citing a vanished sentence keeps its old hash, so it reads as stale);
  3. flags evidence units whose cited sentence vanished or now reads differently (V14);
  4. gives pre-existing figure readings the content digest of the sentences they were shown;
  5. carries reference ids ("R012") by exact entry text (`ref_id_map`) into evidence `cited_refs` and the
     "[R012]" markers of a statement; a unit citing an entry that is gone is flagged (V6), never re-pointed.

Unmapped IDs are never guessed: they stay as written and the readers report them. `impact` is the
read-only counterpart used by `reextract --dry-run`. Docs: CONTRACTS.md section 2.7.
"""

import glob
import re
import unicodedata
import shutil
from collections import defaultdict, deque
from pathlib import Path

from . import config, db, workflow

SHORT_ID = re.compile(r"S\d{4}")
# Per-paper files: short ("S0012") and full IDs are rewritten.
PAPER_FILES = ("analysis.json", "analysis.draft.json", "analysis_state.json", "analysis_rejected.json",
               "analysis_v2.draft.json", "analysis_v2.parts.json", "review_policy.json")
PROFILES = ("paper_overview.json", "section_profile.json", "protocol_profile.json")
# Left alone on purpose: structured_tasks/ (immutable task checkpoints, superseded once the new revision is
# prepared), ai_replies/ (raw provider replies, matched by revision), analysis_runner*.json, _units.json,
# _must_cover.json, analysis_v2.submit.json (never read back), and structure.json / media_manifest.json
# (derived: `steps structure` rebuilds them from the new sentences).


def sentence_key(sentence):
    return (sentence.get("source", "main"), sentence.get("page"), sentence.get("section"),
            sentence.get("kind"), sentence.get("text"))


def id_map(old_sentences, new_sentences, field="sentence_id"):
    """{old full id: new full id} for every old sentence that still exists verbatim (identity included).
    `field="passage_id"` maps passages the same way (same key: source, page, section, kind, text)."""
    pool = defaultdict(deque)
    for sentence in new_sentences:
        pool[sentence_key(sentence)].append(sentence[field])
    mapping = {}
    for sentence in old_sentences:
        candidates = pool.get(sentence_key(sentence))
        if candidates:
            mapping[sentence[field]] = candidates.popleft()
    return mapping


def ref_id_map(old_refs, new_refs):
    """{old ref_id: new ref_id} for every old reference entry that still exists with the same printed text
    (`raw`), identity included; same text twice is paired in order. A numbered entry whose text was read
    differently keeps its printed number. An entry that was split or merged is not in the map."""
    pool = defaultdict(deque)
    for ref in new_refs:
        pool[ref["raw"]].append(ref["ref_id"])
    mapping = {}
    for ref in old_refs:
        if pool.get(ref["raw"]):
            mapping[ref["ref_id"]] = pool[ref["raw"]].popleft()
    taken = set(mapping.values())
    new_ids = {ref["ref_id"] for ref in new_refs}
    for ref in old_refs:
        if ref.get("num") and ref["ref_id"] not in mapping and ref["ref_id"] in new_ids - taken:
            mapping[ref["ref_id"]] = ref["ref_id"]
    return mapping


_REF_MARKER = re.compile(r"\[[^\[\]]*\bR\d{3}\b[^\[\]]*\]")  # "[R004]", "[R004, R007]", "[R003–R005]"; "Biol. 14, R115" is free text


def carry_refs(units, old_refs, mapping, paper_id):
    """Evidence units with `cited_refs` and the "[R###]" markers of `statement` renumbered by `mapping` in ONE
    substitution (R040 -> R045 and R045 -> R050 do not cascade). A unit citing an old entry that has no new
    counterpart keeps what it wrote and gets a V6 flag instead of silently naming whatever entry has that
    number now. Returns (units, number of units flagged)."""
    from . import evidence
    raw = {ref["ref_id"]: ref["raw"] for ref in old_refs}
    out, flagged = [], 0
    for index, unit in enumerate(units, start=1):
        gone = []

        def token(match):
            old = f"{paper_id}:{match.group(0)}"
            if old in mapping:
                return mapping[old].rsplit(":", 1)[-1]
            if old in raw and match.group(0) not in gone:
                gone.append(match.group(0))
            return match.group(0)

        def marker(match):
            return evidence.REF_ID.sub(token, match.group(0))

        refs = []
        for ref in unit.get("cited_refs") or []:
            short = ref.rsplit(":", 1)[-1] if isinstance(ref, str) else None
            if short and evidence.REF_ID.fullmatch(short) and ref in (short, f"{paper_id}:{short}"):
                new = evidence.REF_ID.sub(token, short)
                ref = new if ref == short else f"{paper_id}:{new}"
            refs.append(ref)
        statement = unit.get("statement")
        statement = _REF_MARKER.sub(marker, statement) if isinstance(statement, str) else statement
        changed = {k: v for k, v in (("cited_refs", refs), ("statement", statement)) if k in unit and unit[k] != v}
        if gone:
            names = ", ".join(f"{g} ({raw[f'{paper_id}:{g}'][:60]!r})" for g in gone)
            flag = evidence.ref_gone_flag(index, names)
            changed["flags"] = [f for f in unit.get("flags") or [] if f != flag] + [flag]
            changed["status"] = "flagged"
            flagged += 1
        out.append({**unit, **changed} if changed else unit)
    return out, flagged


def ref_counts(old_refs, mapping):
    """{"renumbered": old entries now under another id, "unmapped": old entries with no counterpart}."""
    return {"renumbered": sum(old != new for old, new in mapping.items()),
            "unmapped": sum(ref["ref_id"] not in mapping for ref in old_refs or [])}


def _full(text, paper_id):
    """Full sentence ID of this paper named by a whole string (short or full form), else None."""
    if SHORT_ID.fullmatch(text):
        return f"{paper_id}:{text}"
    head, _, tail = text.rpartition(":")
    return text if head == paper_id and SHORT_ID.fullmatch(tail) else None


def cited(value, paper_id):
    """Full IDs of this paper's sentences named by whole-string values or dict keys anywhere in `value`."""
    found = set()

    def walk(item):
        if isinstance(item, str):
            sid = _full(item, paper_id)
            if sid:
                found.add(sid)
        elif isinstance(item, list):
            for entry in item:
                walk(entry)
        elif isinstance(item, dict):
            for key, entry in item.items():
                walk(key)
                walk(entry)

    walk(value)
    return found


def _rewriter(mapping, short):
    changed = {old: new for old, new in mapping.items() if old != new}
    short_map = {old.rsplit(":", 1)[-1]: new.rsplit(":", 1)[-1] for old, new in changed.items()} if short else {}

    def rewrite(value):
        if isinstance(value, str):
            return changed.get(value, short_map.get(value, value))
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            # A renamed key that lands on a key which stays as written (an unmapped old ID) must not overwrite
            # it: that would hide the stale entry behind a matching one. The renamed entry is dropped (the old
            # file is in the history copy) and the reader sees the stale one.
            stays = {key for key in value if rewrite(key) == key}
            return {rewrite(key): rewrite(item) for key, item in value.items()
                    if rewrite(key) == key or rewrite(key) not in stays}
        return value

    return rewrite


def _cards():
    return map(Path, sorted(glob.glob(str(config.STORE / "node_cards" / "*" / "*.json"))))


def snapshot(paper_id):
    """Facts only the OLD files can tell, taken before they are replaced: which profiles and figure
    readings are current right now (a stale one must never be made current by re-stamping)."""
    from . import figure_reading, structure
    folder = db.paper_dir(paper_id)
    sha = workflow.fingerprint([folder / "sentences.jsonl"])
    revisions = structure._revision_candidates(paper_id)
    profiles = {}
    for name in PROFILES:
        data = db.read_json(folder / name, None)
        profiles[name] = (isinstance(data, dict) and data.get("source_sha256") == sha
                          and (not data.get("source_revision") or data["source_revision"] in revisions))
        if profiles[name] and name == PROFILES[0]:  # an overview also carries a content digest: stale means stale
            profiles[name] = structure._fresh_overview(paper_id) is not None
    readings = {}
    for path in sorted((folder / "figure_readings").glob("*.json")):
        saved = db.read_json(path, None)
        try:
            readings[path.name] = figure_reading.legacy_digest(paper_id, saved) if isinstance(saved, dict) else None
        except (OSError, TypeError, ValueError, KeyError):
            readings[path.name] = None
    return {"profiles": profiles, "readings": readings}


def carry_over(paper_id, old_sentences, new_sentences, before, history=None, old_refs=None, new_refs=None):
    """Apply the carry-over after the new sentences/metadata are written. `before` = snapshot(paper_id).
    Files that change are first copied into `history` (a directory) so the step is reversible.
    `old_refs` = references.jsonl read BEFORE it was replaced, `new_refs` = the new entries: evidence reference
    ids follow the entry text (`ref_id_map`); without `old_refs` they are left alone.
    Returns {"renumbered", "unmapped", "reference_ids": {"renumbered", "unmapped"}, "files"}."""
    from . import evidence, structure
    folder = db.paper_dir(paper_id)
    mapping = id_map(old_sentences, new_sentences)
    unmapped = [s["sentence_id"] for s in old_sentences if s["sentence_id"] not in mapping]
    renumbered = sum(old != new for old, new in mapping.items())
    written = []

    def put(path, value, rows=False):
        """Write only when the content changed; keep the previous file in `history`."""
        old = list(db.read_jsonl(path)) if rows else db.read_json(path, None)
        if old == value:
            return
        if history is not None and path.is_file():
            target = Path(history) / "carry_over" / "__".join(path.relative_to(config.STORE).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
        (db.write_jsonl if rows else db.write_json)(path, value)
        written.append(str(path.relative_to(config.STORE)))

    ref_map = ref_id_map(old_refs, new_refs) if old_refs is not None else {}
    own = _rewriter(mapping, short=True)
    for name in PAPER_FILES:
        if (folder / name).is_file():
            put(folder / name, own(db.read_json(folder / name)))

    # Evidence: remap, then flag units whose cited sentence is gone or reads differently (V14).
    path = folder / "evidence.jsonl"
    if path.is_file():
        units = [own(row) for row in db.read_jsonl(path)]
        units = evidence.flag_source_drift(units, {s["sentence_id"]: s for s in new_sentences}, paper_id)
        if old_refs is not None:
            units, _ = carry_refs(units, old_refs, ref_map, paper_id)
        put(path, units, rows=True)

    # Profiles: rewrite IDs; re-stamp only if current before and every cited sentence survived verbatim.
    sha = workflow.fingerprint([folder / "sentences.jsonl"])
    for name in PROFILES:
        path = folder / name
        data = db.read_json(path, None) if path.is_file() else None
        if not isinstance(data, dict):
            continue
        survives = cited(data, paper_id) <= set(mapping)  # judged on the OLD IDs, before rewriting
        data = own(data)
        if before["profiles"].get(name) and survives:
            stamped = dict(data, source_sha256=sha)
            if data.get("source_revision"):
                stamped["source_revision"] = structure.source_revision(paper_id)
            try:
                if name == "paper_overview.json":
                    rows = {r["sentence_id"]: r for r in structure._overview_rows(paper_id)}
                    payload = structure._validate_overview(
                        stamped, rows, structure._abbreviation_inventory(new_sentences))
                    stamped["overview_sha256"] = structure._digest(payload)
                data = stamped
            except (ValueError, TypeError, KeyError):
                pass  # the renumbered profile no longer validates: leave the old hash, it reads as stale
        put(path, data)

    # Figure readings: rewrite IDs; a reading that was current and cites only surviving sentences gets the
    # digest of the texts it was shown, so later freshness depends on those texts and the crop only.
    for path in sorted((folder / "figure_readings").glob("*.json")):
        saved = db.read_json(path, None)
        if not isinstance(saved, dict):
            continue
        survives = cited(saved, paper_id) <= set(mapping)
        saved = own(saved)
        digest = before["readings"].get(path.name)
        if digest and survives:
            saved["cited_text_sha256"] = digest
        put(path, saved)

    # Cross-paper files: only this paper's FULL IDs are rewritten, and only files that name this paper.
    full_only = _rewriter(mapping, short=False)
    needle = f"{paper_id}:S"
    for path in _cards():
        if needle in path.read_text(encoding="utf-8"):
            put(path, full_only(db.read_json(path)))
    relations = config.STORE / "relations.jsonl"
    if relations.is_file() and needle in relations.read_text(encoding="utf-8"):
        put(relations, [full_only(row) for row in db.read_jsonl(relations)], rows=True)

    analysis = db.read_json(folder / "analysis.json", None) if (folder / "analysis.json").is_file() else None
    if isinstance(analysis, dict) and "n_flagged" in analysis and (folder / "evidence.jsonl").is_file():
        flagged = sum(unit.get("status") == "flagged" for unit in db.read_jsonl(folder / "evidence.jsonl"))
        if flagged != analysis["n_flagged"]:
            put(folder / "analysis.json", dict(analysis, n_flagged=flagged))
    return {"renumbered": renumbered, "unmapped": len(unmapped), "files": written,
            "reference_ids": ref_counts(old_refs, ref_map)}


def _reordered(value, paper_id, old_sentences, mapping, new_sentences):
    """True when a list of this paper's sentence IDs that was in source order is no longer in source order
    after the rebuild (a protocol profile then fails "must preserve Methods source order")."""
    old_seq = {s["sentence_id"]: s["seq"] for s in old_sentences}
    new_seq = {s["sentence_id"]: s["seq"] for s in new_sentences if "seq" in s}

    def walk(item):
        if isinstance(item, list):
            ids = [_full(x, paper_id) if isinstance(x, str) else None for x in item]
            if len(ids) > 1 and all(i in old_seq and mapping.get(i) in new_seq for i in ids):
                before, after = [old_seq[i] for i in ids], [new_seq[mapping[i]] for i in ids]
                if before == sorted(before) and after != sorted(after):
                    return True
            return any(walk(entry) for entry in item)
        return isinstance(item, dict) and any(walk(entry) for entry in item.values())

    return walk(value)


def impact(paper_id, old_sentences, mapping, new_sentences=()):
    """Read-only: what `carry_over` could NOT keep for this paper -- artifacts citing an unmapped old
    sentence. Renumbered-only citations are carried automatically and are not counted.
    Readings are judged by their recorded caption/mention IDs (a lower bound: a changed caption set that
    keeps its old IDs is only seen after the rebuild). `methods_added` = new Methods sentences with no old
    counterpart; each is an unassigned sentence, which makes a protocol profile read as needing a fix."""
    folder = db.paper_dir(paper_id)
    unmapped = {s["sentence_id"] for s in old_sentences} - set(mapping)

    def hit(value):
        return bool(cited(value, paper_id) & unmapped)

    units = [u.get("unit_id") for u in db.read_jsonl(folder / "evidence.jsonl")
             if hit(u.get("sentence_ids"))] if (folder / "evidence.jsonl").is_file() else []
    profiles = []
    for name in PROFILES:
        data = db.read_json(folder / name, None) if (folder / name).is_file() else None
        if isinstance(data, dict) and hit(data):
            profiles.append(name)
    matched = set(mapping.values())
    methods_added = sum(1 for s in new_sentences if s["sentence_id"] not in matched and s.get("section") == "methods")
    if (folder / PROFILES[2]).is_file() and PROFILES[2] not in profiles and (methods_added or _reordered(
            db.read_json(folder / PROFILES[2], None), paper_id, old_sentences, mapping, new_sentences)):
        profiles.append(PROFILES[2])
    readings = []
    for path in sorted((folder / "figure_readings").glob("*.json")):
        saved = db.read_json(path, None)
        if isinstance(saved, dict) and hit(saved):
            readings.append(saved.get("object_id") or path.stem)
    cards = []
    needle = f"{paper_id}:S"
    for path in _cards():
        if needle in path.read_text(encoding="utf-8") and hit(db.read_json(path)):
            cards.append(f"{path.parent.name}/{path.stem}")
    relations = 0
    path = config.STORE / "relations.jsonl"
    if path.is_file() and needle in path.read_text(encoding="utf-8"):
        relations = sum(hit(row) for row in db.read_jsonl(path))
    return {"evidence_units": units, "profiles": profiles, "figure_readings": readings,
            "node_cards": sorted(cards), "relations": relations, "methods_added": methods_added}


def change_summary(old_sentences, new_sentences):
    """Read-only description of how a rebuild changes sentences (used by `reextract --dry-run`).
    section_changes: sentences whose text/page/kind survive but whose section label changes, as
    "old -> new": count. text_removed: old sentences with no counterpart even ignoring the section.
    text_added: new sentences with no old counterpart. in_table / table_row / subsection: counts old -> new."""
    pool = defaultdict(deque)
    for sentence in new_sentences:
        pool[(sentence.get("source", "main"), sentence.get("page"), sentence.get("kind"), sentence.get("text"))].append(sentence)
    strict = set(id_map(old_sentences, new_sentences).values())
    changes = defaultdict(int)
    removed = 0
    for sentence in old_sentences:
        candidates = pool.get((sentence.get("source", "main"), sentence.get("page"), sentence.get("kind"), sentence.get("text")))
        if not candidates:
            removed += 1
            continue
        match = candidates.popleft()
        if match.get("section") != sentence.get("section"):
            changes[f"{sentence.get('section')} -> {match.get('section')}"] += 1
    added = sum(len(q) for q in pool.values())
    count = lambda rows, test: sum(1 for r in rows if test(r))
    return {"section_changes": dict(sorted(changes.items(), key=lambda kv: -kv[1])),
            "section_changed_total": sum(changes.values()), "text_removed": removed, "text_added": added,
            "table_row": [count(old_sentences, lambda r: r.get("kind") == "table_row"),
                          count(new_sentences, lambda r: r.get("kind") == "table_row")],
            "in_table": [count(old_sentences, lambda r: r.get("in_table")), count(new_sentences, lambda r: r.get("in_table"))],
            "strict_unchanged": len(strict)}


_DROP = re.compile(r"[\W_]+|\xad")


def _compact(text):
    """Lower-case letters and digits only: ignores whitespace, hyphenation, punctuation and glyph spacing."""
    return _DROP.sub("", unicodedata.normalize("NFKC", text or "").casefold())


def text_preservation(old_sentences, new_sentences, passages, tables, sections=()):
    """Does any source text disappear? Every old sentence without an exact counterpart is looked up in
    the new paper text (new passages, sentences with their subsection, section headings, table header/rows/
    footnotes), ignoring case, whitespace,
    hyphenation and punctuation. `contiguous`: the whole sentence occurs as one run. Otherwise `lost` lists
    sentences whose words are partly absent (tokens counted per word, so table cells re-joined differently
    still count as present)."""
    strict = set(id_map(old_sentences, new_sentences))
    removed = [s for s in old_sentences if s["sentence_id"] not in strict]
    cells = [c for t in tables or [] for row in [t.get("header") or [], *(t.get("rows") or [])] for c in row]
    pieces = [p.get("text") for p in passages or []] + [s.get("text") for s in new_sentences] + cells
    pieces += [t.get("footnotes") for t in tables or [] if isinstance(t.get("footnotes"), str)]
    pieces += [s.get("subsection") for s in new_sentences] + [sec.get("heading") for sec in sections or []]
    glued = {_compact(f"{s['subsection']} {s['text']}") for s in new_sentences if s.get("subsection")}
    corpus = " ".join(_compact(piece) for piece in pieces if piece)
    # a glued superscript is stored "cells^51,54" and printed "cells51,54": count both spellings as present
    words = {w for piece in pieces if piece for p in (piece, piece.replace("^", ""))
             for w in re.findall(r"\w+", unicodedata.normalize("NFKC", p).casefold())}
    contiguous, lost, split = 0, [], 0
    for sentence in removed:
        text = sentence.get("text") or ""
        split += _compact(text) in glued          # a subheading that used to be glued to its first sentence
        if _compact(text) in corpus:
            contiguous += 1
            continue
        missing = min(([w for w in re.findall(r"\w+", unicodedata.normalize("NFKC", t).casefold()) if w not in words]
                       for t in (text, text.replace("^", ""))), key=len)
        if missing:
            lost.append({"sentence_id": sentence["sentence_id"], "missing_words": missing[:12], "text": text[:120]})
    return {"removed": len(removed), "contiguous": contiguous, "words_present_not_contiguous": len(removed) - contiguous - len(lost),
            "lost": lost[:25], "lost_total": len(lost), "glued_subheadings_split": split}
