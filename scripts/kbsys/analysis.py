"""Import an AI agent's analysis of a paper, after strict validation.

The agent writes knowledge_store/papers/<paper_id>/analysis.draft.json.
`kb.py import-analysis <paper_id>` checks it and, only if every check
passes, stores it as analysis.json and updates the database and the note.
Every claim must quote its passage word-for-word, so claims stay traceable.
"""

import re
import unicodedata
from datetime import datetime

from . import config, db, notes

CLASSIFICATIONS = {"CORE", "CONNECTED", "POTENTIAL_BRIDGE", "UNCERTAIN", "OUT_OF_SCOPE"}
CLAIM_TYPES = {"result", "discussion", "hypothesis", "background", "method"}
EVIDENCE_TYPES = {"primary_research", "review", "systematic_review", "meta_analysis",
                  "protocol", "case_report", "commentary", "other"}
_CHARMAP = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"',
                          "–": "-", "—": "-", "−": "-", " ": " "})


def _norm(text):
    text = unicodedata.normalize("NFKC", text or "").translate(_CHARMAP)
    return re.sub(r"\s+", " ", text).strip().lower()


def validate(paper_id, draft, passages):
    errors = []
    if draft.get("classification") not in CLASSIFICATIONS:
        errors.append(f"classification must be one of {sorted(CLASSIFICATIONS)}")
    if draft.get("evidence_type") not in EVIDENCE_TYPES:
        errors.append(f"evidence_type must be one of {sorted(EVIDENCE_TYPES)}")
    for key in db.TERM_TYPES:
        value = draft.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            errors.append(f"{key} must be a list of non-empty strings")
    for i, c in enumerate(draft.get("claims", []), start=1):
        where = f"claim {i}"
        if c.get("claim_type") not in CLAIM_TYPES:
            errors.append(f"{where}: claim_type must be one of {sorted(CLAIM_TYPES)}")
        if not (c.get("statement") or "").strip():
            errors.append(f"{where}: empty statement")
        passage = passages.get(c.get("passage_id"))
        if passage is None:
            errors.append(f"{where}: passage_id {c.get('passage_id')!r} does not exist in {paper_id}")
            continue
        quote = c.get("quote") or ""
        if len(_norm(quote)) < 15:
            errors.append(f"{where}: quote is missing or too short")
        elif _norm(quote) not in _norm(passage["text"]):
            errors.append(f"{where}: quote is not found word-for-word in passage {c['passage_id']}")
    return errors


def import_analysis(con, paper_id):
    """Returns (ok, list of messages)."""
    folder = db.paper_dir(paper_id)
    draft_path = folder / "analysis.draft.json"
    draft = db.read_json(draft_path)
    if draft is None:
        return False, [f"No draft found at {config.rel(draft_path)}"]
    passages = {p["passage_id"]: p for p in db.read_jsonl(folder / "passages.jsonl")}
    errors = validate(paper_id, draft, passages)
    if errors:
        return False, errors

    for c in draft.get("claims", []):  # page and section always come from the store
        p = passages[c["passage_id"]]
        c["page"], c["section"] = p["page"], p["section"]
    draft.setdefault("analysed_at", datetime.now().strftime("%Y-%m-%d"))

    current = folder / "analysis.json"
    if current.exists():  # keep the previous version
        history = folder / "analysis_history"
        history.mkdir(exist_ok=True)
        current.replace(history / f"analysis_{datetime.now():%Y%m%d_%H%M%S}.json")
    db.write_json(current, draft)
    draft_path.unlink()

    meta = db.read_json(folder / "metadata.json")
    meta.update({"classification": draft["classification"], "analysis_status": "done",
                 "evidence_type": draft["evidence_type"], "summary": draft.get("summary"),
                 "updated_at": config.now()})
    db.write_json(folder / "metadata.json", meta)
    db.reload_paper(con, paper_id)
    from . import concepts, concept_notes
    con.commit()

    messages = [f"Imported analysis for {paper_id}: {draft['classification']}, "
                f"{len(draft.get('claims', []))} claims"]
    for vault in meta["vaults"]:
        messages.append(f"Note {notes.write_paper_note(meta, vault)} in vault {vault}")
        concept_notes.refresh(con, vault)
        if draft["classification"] in ("UNCERTAIN", "OUT_OF_SCOPE"):
            item_id = db.add_review_item(con, "classification",
                                         f"{draft['classification']}: {draft.get('classification_reason', '')}",
                                         paper_id)
            notes.write_review_note(con, item_id, vault)
        notes.write_indexes(con, vault)
    con.commit()
    return True, messages
