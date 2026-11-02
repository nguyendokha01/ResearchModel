"""EV-17: direction and species of a result unit, filled by code from the cited source before an AI is asked.

`fill_fields` is pure (no files). The validator calls it for new submissions (`evidence._check_unit`); the command
`kb.py fill-evidence-fields` calls `fill_stored` for units already stored. A value the unit already has is never touched.
"""
import re

from . import db, evidence

# "goat anti-mouse IgG", "rabbit polyclonal": the host of a reagent is not the species of the study
_SPECIES = {key: re.compile(r"(?<!anti-)\b(?:" + "|".join(map(re.escape, words)) + r")\b(?!\s*(?:anti|monoclonal|polyclonal|igg|antibod|serum))", re.I)
            for key, words in evidence.SPECIES_SYNONYMS.items()}
# words that name no direction by themselves ("high concentration", "low density", "nothing was lost", "peak at 450 nm") and a
# threshold ("more than 50 cells", "no more than 5%"): hidden from the filler, V7 still reads them
_NOT_A_DIRECTION = re.compile(r"\b(?:high|low|loss|lost|peaks?)\b|\b(?:more|less|fewer|greater|higher|lower|larger|smaller|longer|shorter)"
                              r"\s+than\s+(?:or equal to\s+)?[~<>=≤≥]?\s*(?:\d|(?:one|two|three|four|five|six|seven|eight|nine|ten)\b)", re.I)
NEGATION = re.compile(r"\b(?:no|not|never|nor|neither|none|nothing|without|cannot)\b|n't", re.I)  # "better survival was not observed"


def _species(text):
    return {key for key, pattern in _SPECIES.items() if pattern.search(text)}


def fill_fields(unit, sentences, paper_facts):
    """{"direction": {"value", "source"}, "species": {...}} for the EMPTY fields of a result unit that the source decides.

    direction: the cited data sentences (captions skipped, as V7) state exactly one of increase / decrease / no_change;
    none, several (mixed, contrast) or no_change next to another -> not filled. species: the cited sentences name exactly
    one species of SPECIES_SYNONYMS; when they name none, the paper title + abstract do. source = a short sentence id or
    `paper:title` / `paper:abstract`."""
    out = {}
    if unit.get("unit_type") != "result":
        return out
    if not (unit.get("outcome") or {}).get("direction"):
        found = [(s, evidence.directions(_NOT_A_DIRECTION.sub(" ", evidence._masked(s)))) for s in sentences if s.get("kind") != "caption"]
        union = set().union(*(d for _, d in found)) if found else set()
        value = next(iter(union)) if len(union) == 1 else None
        # unsure -> empty: a negated increase/decrease, or a statement that states another direction than the source
        if value and not (value != "no_change" and any(NEGATION.search(evidence._masked(s)) for s, _ in found)) \
                and evidence.directions(str(unit.get("statement") or "")) <= union:
            out["direction"] = {"value": value, "source": next(evidence._short(s["sentence_id"]) for s, d in found if d)}
    if not (unit.get("model") or {}).get("species"):
        facts = paper_facts or {}
        title, abstract = str(facts.get("title") or ""), str(facts.get("abstract") or "")
        in_paper = _species(title + " " + abstract)
        named = _species(" ".join(s["text"] for s in sentences))
        if named:  # the cited sentences speak: one species or none (several is ambiguous, the paper does not decide)
            # a sentence of a rat study that says "human Muse cells" names the graft: when the title/abstract name another species too, unsure
            if len(named) == 1 and in_paper <= named:
                out["species"] = {"value": next(iter(named)), "source": next(
                    evidence._short(s["sentence_id"]) for s in sentences if _species(s["text"]))}
        elif len(in_paper) == 1:
            out["species"] = {"value": next(iter(in_paper)), "source": "paper:title" if _species(title) else "paper:abstract"}
    return out


def fill_stored(unit, sentences_by_id, paper_facts):
    """The stored unit with the fields the rule fills (`filled_by` set), or None when nothing changes. Other keys untouched."""
    rows = [sentences_by_id.get(s) for s in unit.get("sentence_ids") or []]
    if not rows or None in rows:
        return None
    got = fill_fields(unit, rows, paper_facts)
    if not got:
        return None
    new = {**unit, "filled_by": {**(unit.get("filled_by") or {})}}
    for field, part in (("direction", "outcome"), ("species", "model")):
        if field in got:
            new[part] = {**(unit.get(part) or {}), field: got[field]["value"]}
            new["filled_by"][field] = {"by": "rule", "source": got[field]["source"]}
    return new


FIELDS = (("direction", "outcome", "max_empty_direction_share"), ("species", "model", "max_empty_species_share"))


def papers_report(con, paper_ids=None, apply=False):
    """One dict per paper with stored units: result units, empty fields, how many the rule fills. `apply` rewrites
    evidence.jsonl of the papers where something is filled (nothing else of the paper is written). Dry run writes nothing."""
    if paper_ids is None:
        paper_ids = [r["paper_id"] for r in con.execute(
            "SELECT paper_id FROM papers WHERE analysis_status IN ('done', 'needs_fix') ORDER BY paper_id")]
    report = []
    for pid in paper_ids:
        folder = db.paper_dir(pid)
        units = list(db.read_jsonl(folder / "evidence.jsonl"))
        if not units:
            continue
        sentences = {s["sentence_id"]: s for s in db.read_jsonl(folder / "sentences.jsonl")}
        facts = db.read_json(folder / "metadata.json", {}) or {}
        rows = {"paper_id": pid, "results": 0, "empty": {"direction": 0, "species": 0}, "filled": {"direction": 0, "species": 0},
                "samples": []}
        out = []
        for unit in units:
            new = fill_stored(unit, sentences, facts)
            if unit.get("unit_type") == "result":
                rows["results"] += 1
                for field, part, _ in FIELDS:
                    if not (unit.get(part) or {}).get(field):
                        rows["empty"][field] += 1
                        if new and (new.get(part) or {}).get(field):
                            rows["filled"][field] += 1
                            src = new["filled_by"][field]["source"]
                            text = sentences[unit["sentence_ids"][0]]["text"] if src.startswith("paper:") else \
                                sentences[f"{pid}:{src}"]["text"]
                            rows["samples"].append({"unit_id": unit["unit_id"], "field": field, "value": new[part][field],
                                                    "source": src, "text": text[:160]})
            out.append(new or unit)
        if apply and rows["samples"]:
            db.write_jsonl(folder / "evidence.jsonl", out)
            # only the evidence rows (species, direction) change: no relink, so no concept registry or note is touched
            con.execute("DELETE FROM evidence WHERE paper_id = ?", (pid,))
            db.load_evidence(con, pid, folder)
        report.append(rows)
    return report


def summary(report, standard_param):
    """Totals and the predicted empty shares against the EV-17 limits (`standard_param(name)`)."""
    total = {"papers": len(report), "results": sum(r["results"] for r in report)}
    for field, _, limit in FIELDS:
        empty = sum(r["empty"][field] for r in report)
        left = empty - sum(r["filled"][field] for r in report)
        share = lambda n: n / total["results"] if total["results"] else 0
        total[field] = {"empty": empty, "filled": empty - left, "empty_share": share(empty), "predicted_share": share(left),
                        "limit": standard_param(limit)}
    return total
