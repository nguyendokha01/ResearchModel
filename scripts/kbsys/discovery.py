"""Explicit, metadata-only citation discovery using OpenAlex.

Only the selected seed's DOI/identifier is sent to OpenAlex. Results are cached as
unverified bibliographic candidates; no PDF is downloaded or ingested automatically.
"""

import hashlib
import json
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import config, db, extract

API = "https://api.openalex.org"
MAX_LIMIT = 200


def _candidate(raw):
    """Reduce an OpenAlex record to bibliographic metadata, excluding abstracts/full text."""
    work_id = str(raw.get("id") or "").strip()
    doi = _doi(raw.get("doi"))
    title = " ".join(str(raw.get("display_name") or "").split()) or None
    if not work_id.startswith("https://openalex.org/W") or not title:
        return None
    key = doi or work_id
    location = raw.get("primary_location") or {}
    landing = location.get("landing_page_url") if isinstance(location, dict) else None
    if not isinstance(landing, str) or not landing.startswith("https://"):
        landing = None
    authors = []
    for row in raw.get("authorships") or []:
        name = ((row.get("author") or {}).get("display_name") or "").strip()
        if name and name not in authors:
            authors.append(name)
        if len(authors) == 5:
            break
    return {
        "candidate_id": hashlib.sha1(key.encode("utf-8")).hexdigest()[:16],
        "openalex_id": work_id, "doi": doi, "title": title,
        "year": raw.get("publication_year"), "cited_by_count": raw.get("cited_by_count"),
        "authors": authors, "landing_page_url": landing,
        "source": "OpenAlex", "source_record_url": work_id,
    }


def _doi(value):
    doi = str(value or "").strip().lower()
    doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi)
    return doi or None


def _local_match(con, item):
    if item.get("doi"):
        row = con.execute("SELECT paper_id FROM papers WHERE lower(doi)=?", (item["doi"].lower(),)).fetchone()
        if row:
            return row["paper_id"], "DOI"
    normalized = extract.normalize_title(item.get("title"))
    year = item.get("year")
    if len(normalized) > 20 and year:
        row = con.execute("SELECT paper_id FROM papers WHERE norm_title=? AND year=?",
                          (normalized, year)).fetchone()
        if row:
            return row["paper_id"], "title + year"
    return None, None


def _get_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": "2nd-brain-local/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        if response.status != 200:
            raise RuntimeError(f"OpenAlex returned HTTP {response.status}")
        return json.loads(response.read(4_000_001).decode("utf-8"))


def discover(con, paper_id, vault, limit=100, fetch_json=None):
    """Fetch works citing one in-vault paper, save reduced metadata, and return the cache."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", vault or ""):
        raise ValueError("invalid vault name")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,200}", paper_id or ""):
        raise ValueError("invalid seed paper ID")
    limit = max(1, min(int(limit), MAX_LIMIT))
    paper = con.execute("SELECT p.paper_id,p.doi,p.title FROM papers p JOIN paper_vaults v "
                        "ON v.paper_id=p.paper_id WHERE p.paper_id=? AND v.vault=?", (paper_id, vault)).fetchone()
    if not paper:
        raise ValueError("seed paper is not in the selected vault")
    doi = _doi(paper["doi"])
    if not doi:
        raise ValueError("seed paper has no DOI; OpenAlex discovery needs a DOI")
    getter = fetch_json or _get_json
    escaped = urllib.parse.quote("https://doi.org/" + doi, safe="")
    try:
        seed = getter(f"{API}/works/{escaped}?select=id,doi")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            raise RuntimeError(f"OpenAlex has no record for DOI {doi}") from error
        raise RuntimeError(f"OpenAlex returned HTTP {error.code}") from error
    except urllib.error.URLError as error:
        raise RuntimeError(f"Cannot reach OpenAlex (check the internet connection): {error.reason}") from error
    seed_id = str(seed.get("id") or "")
    if not re.fullmatch(r"https://openalex\.org/W\d+", seed_id):
        raise RuntimeError("OpenAlex could not resolve the seed DOI to a work ID")
    params = {"filter": "cites:" + seed_id.rsplit("/", 1)[-1],
              "select": "id,doi,display_name,publication_year,cited_by_count,primary_location,authorships",
              "per-page": str(min(limit, 100)), "cursor": "*", "sort": "publication_date:desc"}
    rows, cursor = [], "*"
    while len(rows) < limit and cursor:
        params["cursor"] = cursor
        payload = getter(f"{API}/works?{urllib.parse.urlencode(params)}")
        for raw in payload.get("results") or []:
            item = _candidate(raw)
            if item:
                rows.append(item)
                if len(rows) >= limit:
                    break
        cursor = (payload.get("meta") or {}).get("next_cursor")
    seen = set()
    candidates = []
    for item in rows:
        if item["openalex_id"] == seed_id or item["candidate_id"] in seen:
            continue
        seen.add(item["candidate_id"])
        local_id, local_match = _local_match(con, item)
        item["already_in_library"] = bool(local_id)
        item["local_paper_id"] = local_id
        item["local_match"] = local_match
        candidates.append(item)
    result = {"schema_version": 1, "vault": vault, "seed_paper_id": paper_id,
              "seed_title": paper["title"], "seed_doi": doi, "seed_openalex_id": seed_id,
              "source": "OpenAlex", "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "sent_fields": ["seed DOI", "OpenAlex work ID derived from DOI"],
              "candidate_count": len(candidates), "candidates": candidates}
    path = config.STORE / "discovery" / vault / f"{paper_id}.json"
    db.write_json(path, result)
    return result


def iter_cached(vault):
    root = config.STORE / "discovery" / vault
    if not root.is_dir():
        return
    for path in sorted(root.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if (data.get("schema_version") == 1 and data.get("vault") == vault and
                    data.get("source") == "OpenAlex" and isinstance(data.get("candidates"), list)):
                yield data
        except (OSError, ValueError):
            continue


def stage(vault, candidate_id, con=None):
    """Copy selected metadata to a non-ingested inbox manifest; never fetch a PDF."""
    if not re.fullmatch(r"[0-9a-f]{16}", candidate_id or ""):
        raise ValueError("invalid candidate ID")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", vault or ""):
        raise ValueError("invalid vault name")
    for data in iter_cached(vault):
        for item in data.get("candidates") or []:
            if item.get("candidate_id") != candidate_id:
                continue
            inbox = config.ROOT / "sources" / "inbox" / "discovered"
            inbox.mkdir(parents=True, exist_ok=True)
            path = inbox / f"{candidate_id}.json"
            if item.get("source") != "OpenAlex" or not re.fullmatch(
                    r"https://openalex\.org/W\d+", str(item.get("openalex_id") or "")):
                raise ValueError("candidate provenance is invalid")
            if con is not None:
                local_id, local_match = _local_match(con, item)
                if local_id:
                    return {"staged": False, "already_in_library": True, "local_paper_id": local_id,
                            "match": local_match, "candidate_id": candidate_id}
            staged = {"schema_version": 1, "status": "metadata_only_waiting_for_researcher",
                      "discovered_from": {"paper_id": data["seed_paper_id"], "doi": data["seed_doi"]},
                      "candidate": item, "source": "OpenAlex", "staged_at": datetime.now().astimezone().isoformat(
                          timespec="seconds")}
            encoded = json.dumps(staged, ensure_ascii=False, indent=2) + "\n"
            if path.exists():
                if path.read_text(encoding="utf-8") == encoded:
                    return {"staged": True, "already_staged": True, "path": config.rel(path),
                            "candidate_id": candidate_id, "status": staged["status"]}
                raise FileExistsError(f"refusing to overwrite existing inbox manifest: {config.rel(path)}")
            try:
                with path.open("x", encoding="utf-8", newline="\n") as handle:
                    handle.write(encoded)
            except FileExistsError:
                raise FileExistsError(f"refusing to overwrite existing inbox manifest: {config.rel(path)}")
            return {"staged": True, "path": config.rel(path), "candidate_id": candidate_id,
                    "status": staged["status"]}
    raise ValueError("candidate not found in this vault's local discovery cache")


def _selfcheck():
    candidate = _candidate({"id": "https://openalex.org/W123", "doi": "https://doi.org/10.X/Y",
                            "display_name": "  Example   title ", "abstract_inverted_index": {"secret": [0]},
                            "authorships": [{"author": {"display_name": "A"}}]})
    assert candidate and candidate["title"] == "Example title" and "abstract" not in candidate
    assert candidate["source"] == "OpenAlex" and candidate["authors"] == ["A"]
    assert _doi("https://doi.org/10.X/Y") == "10.x/y"
    assert _candidate({"id": "bad", "display_name": "no"}) is None
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.execute("CREATE TABLE papers(paper_id TEXT, doi TEXT, norm_title TEXT, year INTEGER)")
    realistic_title = "A Study of Muse Cell Therapeutic Potential"
    normalized_title = extract.normalize_title(realistic_title)
    con.execute("INSERT INTO papers VALUES('local-1', NULL, ?, 2020)", (normalized_title,))
    assert len(normalized_title) > 20
    assert _local_match(con, {"title": realistic_title.lower(), "year": 2020}) == ("local-1", "title + year")
    con.execute("INSERT INTO papers VALUES('local-2', '10.1234/abc', ?, 2020)",
                (normalized_title,))
    assert _local_match(con, {"doi": "10.1234/abc", "title": realistic_title.lower(), "year": 2020}) == (
        "local-2", "DOI")
    assert 1 <= MAX_LIMIT <= 500
    return 5
