"""Online metadata lookup (Crossref, PubMed). Sends only a DOI, PMID or title.

Every function returns None on any network problem, so ingestion still works
offline.
"""

import difflib
import html
import json
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from . import __version__
from .extract import normalize_title

USER_AGENT = f"BiomedicalKnowledgeSystem/{__version__} (personal research library)"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"


def _get(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception:
        return None


def _get_json(url, timeout):
    data = _get(url, timeout)
    if data is None:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _strip_tags(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _parse_crossref(msg):
    authors = []
    for a in msg.get("author", []) or []:
        name = " ".join(x for x in (a.get("given"), a.get("family")) if x) or a.get("name")
        if name:
            authors.append({"name": name, "family": a.get("family") or name.split()[-1]})
    year = None
    for key in ("published-print", "published-online", "issued", "created"):
        parts = (msg.get(key) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            year = int(parts[0][0])
            break
    abstract = _strip_tags(msg.get("abstract"))
    abstract = re.sub(r"^abstract\s*", "", abstract, flags=re.I)
    return {
        "title": _strip_tags(" ".join(msg.get("title") or [])) or None,
        "authors": authors,
        "year": year,
        "journal": _strip_tags(" ".join(msg.get("container-title") or [])) or None,
        "doi": (msg.get("DOI") or "").lower() or None,
        "abstract": abstract or None,
        "crossref_type": msg.get("type"),
    }


def crossref_by_doi(doi, timeout=15):
    data = _get_json(f"https://api.crossref.org/works/{urllib.parse.quote(doi, safe='/')}", timeout)
    if not data or data.get("status") != "ok":
        return None
    return _parse_crossref(data["message"])


def crossref_by_title(title, timeout=15):
    query = urllib.parse.urlencode({"query.bibliographic": title, "rows": 3})
    data = _get_json(f"https://api.crossref.org/works?{query}", timeout)
    if not data or data.get("status") != "ok":
        return None
    wanted = normalize_title(title)
    best, best_score = None, 0.0
    for item in data["message"].get("items", []):
        parsed = _parse_crossref(item)
        score = difflib.SequenceMatcher(None, wanted, normalize_title(parsed["title"])).ratio()
        if score > best_score:
            best, best_score = parsed, score
    return best if best_score >= 0.9 else None


def pubmed_info(doi, timeout=15):
    """PubMed data for a DOI: Europe PMC first (mirrors PubMed), then NCBI."""
    return europepmc_by_doi(doi, timeout) or pubmed_by_doi(doi, timeout)


def europepmc_by_doi(doi, timeout=15):
    """Return {"pmid", "pmcid", "abstract", "publication_types"} or None."""
    query = urllib.parse.urlencode({"query": f'DOI:"{doi}"', "resultType": "core",
                                    "format": "json", "pageSize": 3})
    data = _get_json(f"https://www.ebi.ac.uk/europepmc/webservices/rest/search?{query}", timeout)
    results = ((data or {}).get("resultList") or {}).get("result") or []
    for r in results:
        if (r.get("doi") or "").lower() == doi.lower() and r.get("pmid"):
            abstract = r.get("abstractText") or ""
            abstract = re.sub(r"<h4>(.*?)</h4>", r"\n\1: ", abstract)  # structured abstract labels
            abstract = "\n".join(_strip_tags(line) for line in abstract.split("\n") if _strip_tags(line))
            return {
                "pmid": str(r["pmid"]),
                "pmcid": r.get("pmcid"),
                "abstract": abstract or None,
                "publication_types": ((r.get("pubTypeList") or {}).get("pubType")) or [],
            }
    return None


def pubmed_by_doi(doi, timeout=15):
    """Return {"pmid", "pmcid", "abstract", "publication_types"} or None."""
    query = urllib.parse.urlencode({"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json",
                                    "tool": "biomed_kb"})
    data = _get_json(f"{EUTILS}/esearch.fcgi?{query}", timeout)
    time.sleep(0.4)  # NCBI allows 3 requests per second without an API key
    ids = ((data or {}).get("esearchresult") or {}).get("idlist") or []
    if len(ids) != 1:
        return None
    return pubmed_record(ids[0], timeout)


def pubmed_record(pmid, timeout=15):
    query = urllib.parse.urlencode({"db": "pubmed", "id": pmid, "retmode": "xml", "tool": "biomed_kb"})
    raw = _get(f"{EUTILS}/efetch.fcgi?{query}", timeout)
    time.sleep(0.4)
    if not raw:
        return None
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None
    parts = []
    for node in root.findall(".//MedlineCitation/Article/Abstract/AbstractText"):
        text = re.sub(r"\s+", " ", "".join(node.itertext())).strip()
        if text:
            label = node.get("Label")
            parts.append(f"{label}: {text}" if label else text)
    pmcid = None
    # Only the article's own IDs, not those in its reference list.
    for aid in root.findall(".//PubmedData/ArticleIdList/ArticleId"):
        if aid.get("IdType") == "pmc":
            pmcid = aid.text
    return {
        "pmid": str(pmid),
        "pmcid": pmcid,
        "abstract": "\n".join(parts) or None,
        "publication_types": [pt.text for pt in root.iter("PublicationType") if pt.text],
    }
