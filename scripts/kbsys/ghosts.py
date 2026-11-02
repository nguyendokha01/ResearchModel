"""Deterministic registry for cited papers that are not present in the library."""

import hashlib
import re
import unicodedata
from . import config, db


def normalized_title(value):
    value = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", value))


def ghost_id(ref):
    doi = re.sub(r"\s+", "", str(ref.get("doi") or "")).lower().rstrip(".,;)")
    if doi:
        return f"ghost:doi:{doi}"
    title = normalized_title(ref.get("title"))
    year = ref.get("year")
    if not title or not year:
        return None
    digest = hashlib.sha1(f"{title} {year}".encode("utf-8")).hexdigest()[:12]
    return f"ghost:t:{digest}"


def build_registry(references_by_paper):
    """Return (updated refs, ghost records), retaining no unverified references."""
    ghosts = {}
    updated = {}
    for paper_id, refs in references_by_paper.items():
        current = []
        for ref in refs:
            item = dict(ref)
            gid = None if item.get("resolved_paper_id") else ghost_id(item)
            item["ghost_id"] = gid
            current.append(item)
            if gid:
                row = ghosts.setdefault(gid, {
                    "ghost_id": gid, "doi": item.get("doi"), "pmid": item.get("pmid"),
                    "title": item.get("title"), "first_author": item.get("first_author"),
                    "year": item.get("year"), "journal": item.get("journal"),
                    "cited_by": [], "count": 0,
                })
                # Fill missing descriptive fields from other entries with the same stable ID.
                for key in ("doi", "pmid", "title", "first_author", "year", "journal"):
                    if row.get(key) is None and item.get(key) is not None:
                        row[key] = item[key]
                row["cited_by"].append(item["ref_id"])
        updated[paper_id] = current
    for row in ghosts.values():
        row["cited_by"] = sorted(set(row["cited_by"]))
        row["count"] = len(row["cited_by"])
    return updated, dict(sorted(ghosts.items()))


def rebuild(con):
    refs_by_paper = {}
    for row in con.execute("SELECT paper_id FROM papers ORDER BY paper_id"):
        paper_id = row["paper_id"]
        refs_by_paper[paper_id] = list(db.read_jsonl(db.paper_dir(paper_id) / "references.jsonl"))
    updated, ghosts = build_registry(refs_by_paper)
    for paper_id, refs in updated.items():
        db.write_jsonl(db.paper_dir(paper_id) / "references.jsonl", refs)
        con.executemany("UPDATE references_list SET ghost_id = ? WHERE ref_id = ?",
                        [(ref["ghost_id"], ref["ref_id"]) for ref in refs])
    db.write_jsonl(config.STORE / "ghosts.jsonl", ghosts.values())
    con.commit()
    return {"papers": len(updated), "references": sum(map(len, updated.values())),
            "ghosts": len(ghosts), "linked_refs": sum(g["count"] for g in ghosts.values()),
            "unidentified": sum(1 for refs in updated.values() for r in refs
                                 if not r.get("resolved_paper_id") and not r.get("ghost_id"))}


def validate_registry(report, references_by_paper, records):
    expected_refs, expected = build_registry(references_by_paper)
    actual = {r.get("ghost_id"): r for r in records}
    report.expect(len(actual) == len(records), "ghosts", "duplicate ghost_id")
    report.expect(actual == expected, "ghosts", "ghost registry differs from references")
    for paper_id, refs in expected_refs.items():
        for ref in refs:
            report.expect(ref.get("ghost_id") == ghost_id(ref) if not ref.get("resolved_paper_id") else
                          ref.get("ghost_id") is None,
                          "ghosts", f"ghost ID mismatch for {ref.get('ref_id')}")
