"""MeSH dictionary, offline (M3.4, docs/CONTRACTS.md §4.5).

Built once from the NLM descriptor file (desc<year>.gz, XML) into
knowledge_store/indexes/mesh.sqlite. The XML is read as a stream, one record at a time
(low memory). The tree numbers are kept for M4 (concept hierarchy).

Search uses only true synonyms: a phrase found in the query expands to the terms of the same
MeSH *concept* (not to the whole descriptor, whose narrower concepts are not synonyms).
"""

import gzip
import json
import re
import sqlite3
import urllib.request
import xml.etree.ElementTree as ET

from . import config

MESH_DIR = config.STORE / "indexes" / "mesh"
MESH_DB = config.STORE / "indexes" / "mesh.sqlite"
SOURCE_URL = "https://nlmpubs.nlm.nih.gov/projects/mesh/MESH_FILES/xmlmesh/desc{year}.gz"
MAX_WORDS = 6          # longest phrase looked up in a query
MAX_VARIANTS = 10      # synonyms added per phrase
MIN_VARIANT_CHARS = 4  # shorter synonyms (e.g. "CVA") are ambiguous and not added

SCHEMA = """
CREATE TABLE IF NOT EXISTS descriptors (ui TEXT PRIMARY KEY, name TEXT, tree_numbers TEXT, scope_note TEXT);
CREATE TABLE IF NOT EXISTS concepts (ui TEXT PRIMARY KEY, descriptor_ui TEXT, name TEXT, preferred INTEGER);
CREATE TABLE IF NOT EXISTS terms (norm TEXT, term TEXT, concept_ui TEXT, descriptor_ui TEXT);
CREATE TABLE IF NOT EXISTS info (key TEXT PRIMARY KEY, value TEXT);
"""


def norm(text):
    """Lookup key: lower case, anything that is not a letter or digit becomes one space."""
    return re.sub(r"[^0-9a-z]+", " ", (text or "").lower()).strip()


def uninvert(term):
    """MeSH writes some terms inverted ("Cells, Cultured"); text says "Cultured Cells"."""
    if term.count(",") == 1:
        head, tail = [x.strip() for x in term.split(",")]
        if head and tail and not re.search(r"\d", tail):
            return f"{tail} {head}"
    return None


def download(year=2026, path=None):
    MESH_DIR.mkdir(parents=True, exist_ok=True)
    path = path or MESH_DIR / f"desc{year}.gz"
    if not path.is_file():
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(SOURCE_URL.format(year=year), timeout=120) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(path)
    return path


def build(source):
    """desc<year>.gz -> mesh.sqlite (replaced atomically). Returns counts."""
    tmp = MESH_DB.with_suffix(".building")
    tmp.unlink(missing_ok=True)
    con = sqlite3.connect(tmp)
    con.executescript(SCHEMA)
    n_desc = n_terms = 0
    batch = []
    with gzip.open(source, "rb") as f:
        for _, el in ET.iterparse(f, events=("end",)):
            if el.tag != "DescriptorRecord":
                continue
            ui = el.findtext("DescriptorUI")
            name = el.findtext("DescriptorName/String")
            trees = [t.text for t in el.findall("TreeNumberList/TreeNumber")]
            scope = None
            for c in el.findall("ConceptList/Concept"):
                cui = c.findtext("ConceptUI")
                pref = c.get("PreferredConceptYN") == "Y"
                if pref:
                    scope = (c.findtext("ScopeNote") or "").strip() or None
                con.execute("INSERT OR REPLACE INTO concepts VALUES (?,?,?,?)",
                            (cui, ui, c.findtext("ConceptName/String"), int(pref)))
                for t in c.findall("TermList/Term"):
                    term = t.findtext("String")
                    for variant in {term, uninvert(term)} - {None}:
                        batch.append((norm(variant), variant, cui, ui))
            con.execute("INSERT OR REPLACE INTO descriptors VALUES (?,?,?,?)", (ui, name, json.dumps(trees), scope))
            n_desc += 1
            if len(batch) > 5000:
                con.executemany("INSERT INTO terms VALUES (?,?,?,?)", batch)
                n_terms += len(batch)
                batch.clear()
            el.clear()
    con.executemany("INSERT INTO terms VALUES (?,?,?,?)", batch)
    n_terms += len(batch)
    con.execute("CREATE INDEX terms_norm ON terms(norm)")
    con.execute("CREATE INDEX terms_concept ON terms(concept_ui)")
    con.execute("CREATE INDEX concepts_descriptor ON concepts(descriptor_ui)")
    con.execute("INSERT OR REPLACE INTO info VALUES ('source', ?)", (source.name,))
    con.execute("INSERT OR REPLACE INTO info VALUES ('built_at', ?)", (config.now(),))
    con.commit()
    con.close()
    tmp.replace(MESH_DB)
    return {"descriptors": n_desc, "terms": n_terms, "source": source.name}


# ---------------------------------------------------------------- lookup

_con = None
_tree_to_descriptor = None


def available():
    return MESH_DB.is_file()


def _db():
    global _con
    if _con is None:
        _con = sqlite3.connect(f"file:{MESH_DB.as_posix()}?mode=ro", uri=True, check_same_thread=False)
    return _con


def lookup(phrase, case_abbrev=False):
    """Concepts whose term equals the phrase: [{concept_ui, descriptor_ui, name, synonyms}].

    `case_abbrev` (query expansion): all-capitals abbreviation terms must match the typed case."""
    key = norm(phrase)
    if not key or not available():
        return []
    con = _db()
    out = []
    pairs = {}
    for cui, dui, term in con.execute("SELECT concept_ui, descriptor_ui, term FROM terms WHERE norm = ?", (key,)):
        # search only: an all-capitals abbreviation ("STEM", "MUSE", "AIDS") matches only when typed in capitals,
        # so the ordinary word "stem" is not expanded to "Scanning Transmission Electron Microscopy"
        if not (case_abbrev and term.isalpha() and term.isupper() and len(term) > 1) or term == phrase.strip():
            pairs.setdefault((cui, dui), None)
    for cui, dui in pairs:
        name = con.execute("SELECT name FROM descriptors WHERE ui = ?", (dui,)).fetchone()
        syns = [t for (t,) in con.execute("SELECT DISTINCT term FROM terms WHERE concept_ui = ?", (cui,))]
        out.append({"concept_ui": cui, "descriptor_ui": dui, "name": name[0] if name else None, "synonyms": syns})
    return out


def descriptor(ui):
    """Return one descriptor's stored MeSH identity and hierarchy metadata."""
    if not available():
        return None
    row = _db().execute("SELECT ui, name, tree_numbers, scope_note FROM descriptors WHERE ui = ?", (ui,)).fetchone()
    if not row:
        return None
    return {"ui": row[0], "name": row[1], "tree_numbers": json.loads(row[2] or "[]"), "scope_note": row[3]}


def parent_descriptors(ui):
    """Immediate MeSH descriptor parents resolved from prefixes of its tree numbers."""
    global _tree_to_descriptor
    child = descriptor(ui)
    if not child:
        return []
    if _tree_to_descriptor is None:
        mapping = {}
        for descriptor_ui, raw in _db().execute("SELECT ui, tree_numbers FROM descriptors"):
            for tree in json.loads(raw or "[]"):
                mapping.setdefault(tree, set()).add(descriptor_ui)
        _tree_to_descriptor = mapping
    parents = set()
    for tree in child["tree_numbers"]:
        if "." in tree:
            parents.update(_tree_to_descriptor.get(tree.rsplit(".", 1)[0], set()))
    parents.discard(ui)
    return sorted(parents)


def plural_key(term):
    """"Strokes" and "Stroke", "Cytometries" and "Cytometry" get the same key."""
    out = []
    for w in norm(term).split():
        if len(w) > 4 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return " ".join(out)


def _usable(term):
    return "," not in term and len(term) >= MIN_VARIANT_CHARS and not re.search(r"[()\[\]\"*]", term)


def expand(words, is_stopword):
    """Greedy longest match of MeSH phrases in a list of query words.

    Returns [(start, end, variants, info)] for the phrases found; `variants` starts with the phrase
    as typed, then usable synonyms of the matched concept(s)."""
    if not available():
        return []
    found, i = [], 0
    while i < len(words):
        hit = None
        for n in range(min(MAX_WORDS, len(words) - i), 0, -1):
            chunk = words[i:i + n]
            if n == 1 and (is_stopword(chunk[0]) or len(chunk[0]) < 3):
                continue
            if is_stopword(chunk[0]) or is_stopword(chunk[-1]):
                continue
            phrase = " ".join(chunk)
            concepts = lookup(phrase, case_abbrev=True)
            if concepts:
                hit = (i, i + n, phrase, concepts)
                break
        if hit:
            start, end, phrase, concepts = hit
            seen, variants = {plural_key(phrase)}, [phrase]
            for c in concepts:
                for t in sorted(c["synonyms"], key=len):
                    if _usable(t) and plural_key(t) not in seen:
                        seen.add(plural_key(t))
                        variants.append(t)
            variants = variants[:MAX_VARIANTS]
            info = [{"descriptor_ui": c["descriptor_ui"], "name": c["name"], "concept_ui": c["concept_ui"]}
                    for c in concepts]
            found.append((start, end, variants, info))
            i = end
        else:
            i += 1
    return found
