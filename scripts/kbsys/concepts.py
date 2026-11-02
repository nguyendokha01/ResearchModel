"""Code-derived concept registry and paper↔concept links.

The registry is built from local AI terms, researcher-maintained synonym groups, safe
spelling variants, and unambiguous exact MeSH matches.
"""

import hashlib
import json
import difflib
import re
import time
from collections import Counter, defaultdict
import yaml

from . import config, db, mesh, node_decisions, presentation, standard

TERM_KEYS = ("topics", "concepts", "methods", "species", "molecules", "cell_types")
TYPE_NAMES = {"topics": "topic", "concepts": "concept", "methods": "method",
              "species": "species", "molecules": "molecule", "cell_types": "cell_type"}
TYPE_KEYS = {v: k for k, v in TYPE_NAMES.items()}
TYPE_PRIORITY = {"cell_type": 0, "molecule": 1, "species": 2, "method": 3,
                 "concept": 4, "topic": 5}


def meets(units_count, mentions_count):
    """ND-05, the one place of the threshold: a node shows in a vault with at least `min_evidence` evidence units or
    `min_mentions` mentions in that vault's papers (both read from config/standard.yaml)."""
    return units_count >= standard.param("ND-05", "min_evidence") or mentions_count >= standard.param("ND-05", "min_mentions")


# Registry construction is intentionally cached per SQLite connection.  Callers that
# write paper_terms must invalidate it before the next operation; the linker does this
# around its own derived-index writes. Single-paper reloads use db.reload_paper,
# which rebuilds this derived index after loading updated paper terms.
_REGISTRY_CACHE = {}
_REGISTRY_FILE_CACHE = {}
_LINK_STATS = {}

_PLURAL_EXCEPTIONS = {"analysis", "basis", "crisis", "diagnosis", "hypothesis", "species",
                      "series", "status", "thesis"}
CANONICAL_ACRONYMS = {"its": "ITS"}
_GREEK_NAMES = {"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "μ": "mu", "µ": "mu",
               # capital look-alikes (what str.upper() made of a leading Greek letter before capitalize_first)
               "Α": "alpha", "Β": "beta", "Γ": "gamma"}


def _clean(text):
    text = str(text or "").strip()
    text = text.replace("–", "-").replace("—", "-").replace("‑", "-")
    return re.sub(r"\s+", " ", text)


def _singular_token(token):
    low = token.lower()
    if low in _PLURAL_EXCEPTIONS or len(low) <= 3:
        return token
    if low.endswith("ies") and len(low) > 4:
        return token[:-3] + "y"
    if low.endswith("s") and not low.endswith(("ss", "us")):
        return token[:-1]
    return token


def _plural_token(token):
    low = token.lower()
    if low in _PLURAL_EXCEPTIONS or len(low) <= 3 or re.fullmatch(r"[A-Z0-9-]+", token):
        return token
    if low.endswith("s"):
        return token
    if low.endswith("y") and len(low) > 3 and low[-2] not in "aeiou":
        return token[:-1] + "ies"
    if low.endswith(("s", "x", "z", "ch", "sh")):
        return token + "es"
    return token + "s"


def spelling_variants(term):
    """Return conservative singular/plural and hyphen/space variants."""
    term = _clean(term)
    if not term:
        return []
    raw = {term, term.replace("-", " "), term.replace(" ", "-")}
    words = term.replace("-", " ").split()
    if words and not re.fullmatch(r"[A-Z0-9-]+", words[-1]):
        singular = words[:-1] + [_singular_token(words[-1])]
        plural = words[:-1] + [_plural_token(words[-1])]
        raw.update({" ".join(singular), " ".join(plural), "-".join(singular), "-".join(plural)})
    return sorted(raw, key=lambda x: (len(x), x.lower(), x))


def synonym_groups(cfg=None):
    """Pluggable synonym source: config groups today, MeSH entry terms later."""
    cfg = cfg or config.load()
    groups = []
    for group in cfg.get("synonyms") or []:
        terms = [_clean(t) for t in group if _clean(t)]
        if terms:
            expanded = []
            for term in terms:
                expanded.extend(spelling_variants(term))
            groups.append(sorted(set(expanded), key=lambda x: (x.lower(), x)))
    return groups


def _key(term):
    """Key that merges safe spelling variants but not neural/neuronal-like terms."""
    text = _clean(term)
    for symbol, name in _GREEK_NAMES.items():
        text = text.replace(symbol, name)
    # These two forms are lexical duplicates in the imported registry, not
    # different scientific entities: RT-qPCR/RTqPCR and m 6A/m6A.
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z]|\d+$)", "", text)  # also Lyve-1 = Lyve1 (a closing number only: IL-1 receptor 2 keeps its id)
    text = re.sub("['’ʼ]", "", text)  # Fisher's = Fisher’s = Fisherʼs (the apostrophes the ND-03 audit folds)
    text = re.sub(r"(?<=[A-Za-z])\s+(?=\d)", "", text)
    words = re.sub(r"[-_]+", " ", text).lower().split()
    if words and words[-1] not in _PLURAL_EXCEPTIONS and len(words[-1]) > 3:
        words[-1] = _singular_token(words[-1].lower())
    # Word breaks between letters are typography, not meaning (PDF line/superscript splits, real time PCR
    # vs real-time PCR, beta tubulin vs beta-tubulin); digits keep their break (type 1 vs type 2 differ anyway).
    return re.sub(r"(?<=[^\W\d_])\s+(?=[^\W\d_])", "", " ".join(words))


def capitalize_first(value):
    """Upper-case a leading ASCII lowercase letter only.

    Greek symbols (beta-tubulin, mu-opioid) keep their lowercase form: str.upper() would turn them into
    the different capital letters (Beta-tubulin), a distinct string that splits the concept.
    """
    return value[0].upper() + value[1:] if value and "a" <= value[0] <= "z" else value


def _display_name(term):
    term = _clean(term)
    term = re.sub(r"\s+", " ", term).strip()
    canonical = CANONICAL_ACRONYMS.get(term.casefold())
    if canonical:
        return canonical
    # Merge keys may normalize hyphens/spaces, but canonical names retain the
    # spelling supplied by the paper (including hyphens and slashes).
    if term and term[0].islower() and not any(c.isupper() for c in term[1:]):
        term = capitalize_first(term)
    return term


def _slug(name):
    for symbol, greek in _GREEK_NAMES.items():
        name = name.replace(symbol, greek + "-")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "term"


def _unique_slug(name, used):
    slug = _slug(name)
    if slug in used and used[slug] != name:
        slug += "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    used[slug] = name
    return slug


class Registry:
    def __init__(self, records):
        self.records = records
        self.by_id = {r["concept_id"]: r for r in records}
        self.by_key = {}
        self.by_lower = {}
        self._lookups = {}  # value -> record: lookup() normalises (regex) and is called per term per node
        for record in records:
            self.by_lower[record["name"].lower()] = record
            for value in [record["name"], *record.get("synonyms", [])]:
                self.by_key[_key(value)] = record
                self.by_lower.setdefault(value.lower(), record)

    def lookup(self, value):
        if len(self._lookups) > 20000:  # long-running servers see arbitrary query strings
            self._lookups.clear()
        if value not in self._lookups:
            cleaned = _clean(value)
            self._lookups[value] = self.by_key.get(_key(cleaned)) or self.by_lower.get(cleaned.lower())
        return self._lookups[value]

    def canonical(self, value):
        record = self.lookup(value)
        return record["name"] if record else _display_name(value)


def invalidate_registry(con=None):
    """Drop the cached registry for *con*, or every cached registry when omitted."""
    if con is None:
        _REGISTRY_CACHE.clear()
        _REGISTRY_FILE_CACHE.clear()
    else:
        _REGISTRY_CACHE.pop(id(con), None)


def _raw_terms(con):
    seen = []
    if con is not None:
        for row in con.execute("SELECT term_type, term FROM paper_terms ORDER BY rowid"):
            if row["term_type"] in TERM_KEYS and row["term"]:
                seen.append((row["term_type"], _clean(row["term"])))
    return seen


def _text_spelling_counts(con, variants):
    """Count exact spellings in searchable paper text for one merge group."""
    counts = Counter()
    if con is None:
        return counts
    for variant in variants:
        if not variant:
            continue
        # An acronym (ITS, ROS, EG) is counted case-sensitively: otherwise the pronoun
        # "its" at a sentence start ("Its") wins and becomes the canonical name.
        acronym = re.fullmatch(r"[A-Z][A-Z0-9]{1,7}s?", variant) is not None
        pattern = re.compile(r"(?<![A-Za-z0-9])" + re.escape(variant) +
                             r"(?![A-Za-z0-9])", 0 if acronym else re.IGNORECASE)
        rows = con.execute(
            "SELECT text FROM sentences WHERE section != 'references' AND text LIKE ?",
            (f"%{variant}%",)).fetchall()
        for row in rows:
            for match in pattern.finditer(row[0] or ""):
                counts[match.group(0)] += 1
    return counts


def _previous_concept_ids():
    """Read prior IDs when available so spelling repairs do not relabel concepts."""
    path = config.STORE / "concepts.jsonl"
    if not path.exists():
        return {}
    previous = {}
    try:
        for record in db.read_jsonl(path):
            if record.get("concept_id") and record.get("name"):
                previous.setdefault(_key(record["name"]), record["concept_id"])
    except (OSError, ValueError, TypeError):
        return {}
    return previous


def _previous_concept_records():
    path = config.STORE / "concepts.jsonl"
    if not path.exists():
        return {}
    try:
        return {_key(record["name"]): record for record in db.read_jsonl(path)
                if record.get("concept_id") and record.get("name")}
    except (OSError, ValueError, TypeError):
        return {}


def _initialism(short, expansion):
    """True when the acronym's letters occur in order in the expansion, starting at its first letter."""
    s = re.sub(r"[^a-z]", "", short.lower())
    e = re.sub(r"[^a-z]", "", expansion.lower())
    if len(s) < 2 or not e or s[0] != e[0]:
        return False
    rest = iter(e)
    return all(c in rest for c in s)


def abbreviation_pairs(known_keys):
    """(short key, expansion key) pairs that a paper itself defines (structure.json abbreviations) and that
    are both already registry terms: an acronym and its expansion in the same paper are one concept."""
    candidates = set()
    defined = defaultdict(set)  # short key -> every expansion key any paper gives it (known to the registry or not)
    for path in sorted(config.PAPERS_DIR.glob("*/structure.json")):
        try:
            abbreviations = (db.read_json(path, {}) or {}).get("abbreviations") or []
        except (OSError, ValueError):
            continue
        for row in abbreviations:
            short = _clean(row.get("short"))
            if _key(short) not in known_keys:
                continue
            for definition in row.get("definitions") or []:
                text = _clean(definition.get("text"))
                if not text:
                    continue
                defined[_key(short)].add(_key(text))
                if _key(text) in known_keys and _key(text) != _key(short) and _initialism(short, text):
                    candidates.add((_key(short), _key(text)))
    # CPA = cryoprotective agent in one paper, cyclophosphamide in another: merge only when every paper that
    # defines the acronym gives this expansion (spelling variants such as fertilisation/fertilization count as equal).
    return sorted(pair for pair in candidates
                  if all(difflib.SequenceMatcher(None, other, pair[1]).ratio() >= 0.9 for other in defined[pair[0]]))


def generic_keys():
    """config/generic_terms.yaml: study-process words (Informed consent, Healthy controls ...) that never become nodes,
    plus the live `not_a_node` decisions (node_decisions.jsonl)."""
    path = config.CONFIG_DIR / "generic_terms.yaml"
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, yaml.YAMLError):
        data = {}
    return {_key(term) for term in (data or {}).get("generic_terms") or [] if isinstance(term, str)} | node_decisions.keys("not_a_node")


def build_registry(con=None):
    """Build one canonical record per term, applying the approved type priority."""
    cache_key = id(con) if con is not None else None
    if cache_key is not None and cache_key in _REGISTRY_CACHE:
        cached_con, cached_registry = _REGISTRY_CACHE[cache_key]
        if cached_con is con:
            return cached_registry
    # a node the researcher confirmed stays, even if its name is on the generic list
    generic = generic_keys() - {k for k, r in _previous_concept_records().items() if r.get("status") == "human_confirmed"}
    raw = [(kind, term) for kind, term in _raw_terms(con) if _key(term) not in generic]
    adds, types = node_decisions.by_key("add"), node_decisions.by_key("type")
    if bad := [k for k, r in [*adds.items(), *types.items()] if r.get("value") not in TYPE_KEYS]:
        raise ValueError(f"node_decisions.jsonl: add/type rows without a valid node type: {bad}")  # never into concepts.jsonl
    have = {_key(t) for _, t in raw}  # an add row on an existing term acts as a type row: it never wins the term type or the name
    raw += [(TYPE_KEYS[r["value"]], r["name"]) for k, r in adds.items() if k not in generic and k not in have]  # a decided node joins like a term
    decided = {**adds, **types}
    groups = synonym_groups()
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            parent[b] = a

    for _, term in raw:
        find(_key(term))
    for group in groups:
        keys = [_key(t) for t in group]
        for key in keys:
            find(key)
        for key in keys[1:]:
            union(keys[0], key)
    if con is not None:
        for short_key, long_key in abbreviation_pairs(set(parent)):
            union(short_key, long_key)

    members = defaultdict(list)
    for term_type, term in raw:
        members[find(_key(term))].append((term_type, term))
    # Config-only synonyms are useful after a researcher adds a group, even before an AI tag uses it.
    for group in groups:
        root = find(_key(group[0]))
        if root not in members:
            members[root] = [("concepts", group[0])]
        for term in group:
            if all(_key(term) != _key(existing) for _, existing in members[root]):
                members[root].append(("concepts", term))

    records, used, taken = [], {}, set()  # used: slug -> name; taken: concept ids already given
    previous_ids = _previous_concept_ids()
    previous_records = _previous_concept_records()
    reserved = {previous_ids[root] for root in members if root in previous_ids}  # a kept id is never given to a new concept that sorts earlier
    for root in sorted(members):
        entries = members[root]
        best_type = min((TYPE_NAMES[k] for k, _ in entries), key=lambda t: TYPE_PRIORITY[t])
        previous = previous_records.get(root, {})
        human_confirmed = previous.get("status") == "human_confirmed"
        if human_confirmed:
            best_type = previous.get("type", best_type)
        candidates = [term for key, term in entries if TYPE_NAMES[key] == best_type]
        counts = Counter(candidates)
        text_counts = _text_spelling_counts(
            con, {variant for term in candidates for variant in spelling_variants(term)})
        # Select among spellings actually proposed for the winning type. Generated
        # plural/hyphen variants are merge keys and synonyms, never new canonical
        # names (otherwise ``Muse cell`` would become ``Muse cells`` merely because
        # the plural occurs more often in prose).
        observed = Counter({spelling: n for spelling, n in text_counts.items()
                            if _key(spelling) == root and
                            any(spelling.casefold() == term.casefold() for term in candidates)})
        # An acronym found verbatim (ITS = insulin-transferrin-selenium) beats a same-letters
        # common word ("Its"/"its" at sentence starts) that an AI tag may have produced.
        # Only a pure casing clash is resolved here; a longer full name (cryoprotective agent vs CPA) keeps winning.
        for acronym in [s for s in observed if re.fullmatch(r"[A-Z][A-Z0-9]{1,7}s?", s)]:
            for other in [s for s in observed if s != acronym and s.casefold() == acronym.casefold()]:
                del observed[other]
        # Paper text is authoritative; fall back to the most frequent AI spelling
        # only when no candidate spelling occurs verbatim in text.
        if human_confirmed:
            chosen = previous.get("name") or sorted(counts, key=lambda t: (-counts[t], len(t), t.lower(), t))[0]
        elif observed:
            chosen = sorted(observed, key=lambda t: (-observed[t], t.lower(), t))[0]
        else:
            chosen = sorted(counts, key=lambda t: (-counts[t], len(t), t.lower(), t))[0]
        name = _display_name(chosen)
        all_values = set()
        for _, term in entries:
            all_values.update(spelling_variants(term))
        all_values.update(v for group in groups if any(_key(x) == root for x in group) for v in group)
        # A synonym kept from an older registry must still belong to this concept (beta-tubulin was once
        # a synonym of Tubulin; it is now its own child concept).
        all_values.update(v for v in previous.get("synonyms") or [] if human_confirmed or _key(v) == root)
        all_values.discard(name)
        if _case_sensitive(name):  # ITS: "Its"/"its" are the English pronoun, never a spelling of the acronym
            all_values = {v for v in all_values if v.casefold() != name.casefold()}
        prior_id = previous_ids.get(root)
        if prior_id and prior_id not in taken:
            concept_id = prior_id
        else:
            concept_id = f"local:{_unique_slug(name, used)}"
            if concept_id in taken or concept_id in reserved:  # a kept id of another concept has this slug (the two keyed spaces were mixed before)
                concept_id += "-" + hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
        taken.add(concept_id)
        status = "human_confirmed" if human_confirmed else "ai_proposed"
        record = {"concept_id": concept_id, "name": name, "type": best_type,
                  "synonyms": sorted(all_values, key=lambda x: (x.lower(), x)),
                  "status": status, "mesh_ui": None,
                  "mesh_tree_numbers": [], "parents": [],
                  "classified_by": "human" if human_confirmed else "terms"}
        if decided:  # a decision row names the node by the key of any of its terms; human_confirmed > decision > ND-09 > TYPE_PRIORITY
            node_keys = {root, _key(name), *(_key(v) for v in all_values), *(_key(t) for _, t in entries)}
            pick = lambda rows: rows[min(node_keys & rows.keys())] if node_keys & rows.keys() else None
            if (row := pick(decided)) and not human_confirmed and best_type != "topic":
                record["type"], record["classified_by"] = row["value"], row["classified_by"]
            if row := pick(adds):
                record["source_sentence_ids"] = row.get("sentence_ids") or []
        records.append(record)
    _enrich_mesh(records)
    for child, parent in config_parent_ids(records).items():
        record = next(r for r in records if r["concept_id"] == child)
        record["parents"] = [*record["parents"], *([parent] if parent not in record["parents"] else [])]
    registry = Registry(records)
    if cache_key is not None:
        _REGISTRY_CACHE[cache_key] = (con, registry)
    return registry


def config_parent_ids(records, cfg=None):
    """{child concept_id: parent concept_id} from config/concept_parents.yaml (both ends must exist)."""
    pairs = (cfg or config.load()).get("concept_parents") or {}
    lookup = Registry(records).lookup
    out = {}
    for child, parent in pairs.items():
        c, p = lookup(str(child)), lookup(str(parent))
        if c and p and c["concept_id"] != p["concept_id"]:
            out[c["concept_id"]] = p["concept_id"]
    return out


def mesh_hits(record):
    """MeSH concepts of a node: its name, else its non-acronym synonyms (a spelling merged under an acronym name:
    Cytokeratin 7 under CK7; an acronym is never looked up: ND-01, ND-08). Build and selftest use this one rule."""
    if (none := node_decisions.keys("no_mesh")) and any(_key(v) in none for v in [record["name"], *(record.get("synonyms") or ())]):
        return []  # a live no_mesh decision (node_decisions.jsonl)
    hits = mesh.lookup(record["name"])
    if not hits:
        hits = [hit for synonym in record.get("synonyms") or () if not _case_sensitive(synonym) for hit in mesh.lookup(synonym)]
    return hits


def mesh_tree_type(tree_numbers):
    """ND-09: the one node type that every MeSH tree number (ignored prefixes dropped) maps to, else None."""
    ignore = standard.param("ND-09", "ignore")
    types = standard.param("ND-09", "tree_types")
    under = lambda t, p: t == p or t.startswith(p + ".") or (p.isalpha() and t.startswith(p))  # a bare letter is a whole category (D09.947)
    kept = [t for t in tree_numbers if not any(under(t, p) for p in ignore)]
    found = {next((ty for p, ty in types.items() if under(t, p)), None) for t in kept}
    return found.pop() if len(found) == 1 else None  # no tree left -> None; an unmapped tree is None, so it also blocks


def _enrich_mesh(records):
    """Attach hierarchy and non-colliding synonyms for unique exact MeSH name matches.

    Local concept IDs and names remain canonical. Ambiguous descriptor matches are ignored;
    MeSH aliases that would redirect a different local concept are not added.
    """
    if not mesh.available() or not records:
        return
    baseline = Registry(records)
    matched = {}
    alias_owners = defaultdict(set)
    for record in records:
        hits = mesh_hits(record)
        descriptor_ids = {hit["descriptor_ui"] for hit in hits}
        if len(descriptor_ids) != 1:
            continue
        descriptor_ui = next(iter(descriptor_ids))
        descriptor = mesh.descriptor(descriptor_ui)
        if not descriptor:
            continue
        matched[record["concept_id"]] = (descriptor, hits)
        for hit in hits:
            for synonym in hit.get("synonyms", []):
                synonym = _clean(synonym)
                if not synonym or synonym.casefold() == record["name"].casefold() or not mesh._usable(synonym):
                    continue
                existing = baseline.lookup(synonym)
                if existing and existing["concept_id"] != record["concept_id"]:
                    continue
                alias_owners[_key(synonym)].add(record["concept_id"])

    ids_by_mesh = defaultdict(list)
    for record in records:
        match = matched.get(record["concept_id"])
        if not match:
            continue
        descriptor, _ = match
        ids_by_mesh[descriptor["ui"]].append(record["concept_id"])
        record["mesh_ui"] = descriptor["ui"]
        record["mesh_tree_numbers"] = descriptor["tree_numbers"]
        if record.get("status") != "human_confirmed":
            if record.get("classified_by", "terms") == "terms" and record["type"] == standard.param("ND-09", "only_from") \
                    and (new := mesh_tree_type(record["mesh_tree_numbers"])):
                record["type"], record["classified_by"] = new, "ND-09"
            record["status"] = "mesh"

    for record in records:
        match = matched.get(record["concept_id"])
        if not match:
            continue
        descriptor, hits = match
        record["parents"] = [ids_by_mesh[parent][0] if len(ids_by_mesh.get(parent, [])) == 1
                             else f"mesh:{parent}"
                             for parent in mesh.parent_descriptors(descriptor["ui"])]
        aliases = set(record.get("synonyms", []))
        for hit in hits:
            aliases.update(synonym for synonym in hit.get("synonyms", [])
                           if _key(synonym) in alias_owners and
                           alias_owners[_key(synonym)] == {record["concept_id"]})
        aliases.discard(record["name"])
        record["synonyms"] = sorted(aliases, key=lambda value: (value.lower(), value))


def rule_typed(record):
    """The node's type was set by a rule (ND-09) or a decision, not by the term lists or a person."""
    return (record or {}).get("classified_by", "terms") not in ("terms", "human")


def normalize_terms(terms, registry, keep_rule_types=False):
    """Normalize a six-list terms object and enforce one canonical type.

    keep_rule_types (term lists that are stored): a rule-typed node keeps the list the AI chose, so the next build
    recomputes the term type and the rule re-applies; removing or narrowing the rule undoes the type."""
    out = {key: [] for key in TERM_KEYS}
    for key in TERM_KEYS:
        for value in (terms or {}).get(key) or []:
            if not isinstance(value, str) or not value.strip():
                continue
            record = registry.lookup(value)
            target = TYPE_KEYS[record["type"]] if record and not (keep_rule_types and rule_typed(record)) else key
            value = record["name"] if record else _display_name(value)
            if value not in out[target]:
                out[target].append(value)
    return out


def write_registry(registry):
    path = config.STORE / "concepts.jsonl"
    db.write_jsonl(path, registry.records)
    _REGISTRY_FILE_CACHE.clear()
    return path


def _persisted_registry():
    path = config.STORE / "concepts.jsonl"
    try:
        stat = path.stat()
    except OSError:
        return None
    key = (str(path.resolve()), getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1e9)), stat.st_size)
    cached = _REGISTRY_FILE_CACHE.get(key)
    if cached is not None:
        return cached
    try:
        records = list(db.read_jsonl(path))
    except (OSError, ValueError, TypeError):
        return None
    if not records:
        return None
    # ``Its`` was persisted by an older registry before the acronym spelling
    # rule was added.  Repair only that unambiguous display spelling at read
    # time; the next link-concepts run writes the canonical record.
    for record in records:
        if str(record.get("name") or "").casefold() == "its":
            record["name"] = "ITS"
            record["synonyms"] = sorted(
                {str(value) for value in record.get("synonyms") or []
                 if str(value).casefold() != "its"},
                key=lambda value: (value.casefold(), value))
    registry = Registry(records)
    _REGISTRY_FILE_CACHE.clear()
    _REGISTRY_FILE_CACHE[key] = registry
    return registry


def load_registry(con=None):
    persisted = _persisted_registry()
    if persisted is not None:
        return persisted
    return build_registry(con)


def _fts_candidates(con, variant, vault=None):
    tokens = re.findall(r"[A-Za-z0-9]+", variant)
    if not tokens:
        return set()
    queries = ['"' + variant.replace('"', ' ') + '"', " AND ".join(tokens)]
    gene = re.fullmatch(r"([A-Z]{2,})(\d+)", variant)
    if gene:  # _regex also accepts SIRT-1, which FTS tokenizes as "sirt 1"
        queries.append('"%s %s"' % gene.groups())
    candidates = set()
    for match in queries:
        try:
            sql = ("SELECT DISTINCT s.paper_id FROM sentences_fts "
                   "JOIN sentences s ON s.rowid = sentences_fts.rowid "
                   "WHERE sentences_fts MATCH ? AND s.section != 'references'")
            args = [match]
            if vault:
                sql += " AND s.paper_id IN (SELECT paper_id FROM paper_vaults WHERE vault = ?)"
                args.append(vault)
            candidates.update(row[0] for row in con.execute(sql, args))
        except Exception:
            continue
    return candidates


def _regex(variant, case_sensitive=False):
    # Gene symbols: SIRT1 is written Sirt1 (mouse/rat) or SIRT-1; still case-sensitive so ordinary words never match.
    gene = case_sensitive and re.fullmatch(r"([A-Z]{2,})(\d+)", variant)
    if gene:
        letters, digits = gene.groups()
        return re.compile(r"(?<!\w)(?:" + letters + "|" + letters.capitalize() + r")-?" + digits + r"(?!\w)")
    pattern = re.escape(variant).replace(r"\ ", r"\s+")
    flags = 0 if case_sensitive else re.IGNORECASE
    return re.compile(r"(?<!\w)" + pattern + r"(?!\w)", flags)


def _case_sensitive(name):
    return bool(re.fullmatch(r"(?:[A-Z][A-Z0-9]{1,}|[A-Z]{2,}\d+|[a-z]+[A-Z][A-Za-z0-9]*)", name))


def _read_sentences(paper_id):
    _LINK_STATS["sentence_files"] = _LINK_STATS.get("sentence_files", 0) + 1
    return list(db.read_jsonl(db.paper_dir(paper_id) / "sentences.jsonl"))


def _read_studied_terms(paper_id, registry):
    _LINK_STATS["evidence_files"] = _LINK_STATS.get("evidence_files", 0) + 1
    found = set()
    for unit in db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl"):
        for key in TERM_KEYS:
            for value in (unit.get("terms") or {}).get(key) or []:
                record = registry.lookup(value)
                if record:
                    found.add(record["concept_id"])
    return found


def _compiled_patterns(registry):
    """Compile every concept's regex once for the current link operation."""
    return {
        record["concept_id"]: tuple(
            _regex(variant, _case_sensitive(variant))
            for variant in [record["name"], *record.get("synonyms", [])]
            if variant
        )
        for record in registry.records
    }


def _pattern_matches(text, pattern, record):
    """Match a registry variant, excluding explicit species reagent phrases."""
    text = str(text or "")
    if presentation.homonym_blocked(record.get("name"), text):
        return False
    organism = record.get("type") in ("species", "organism", "cell_type")  # Fetus (MeSH cell type) in "fetus bovine serum"
    reagent_spans = presentation.species_reagent_spans(text) if organism else ()
    for match in pattern.finditer(text):
        if not organism or not any(match.start() < end and start < match.end()
                                   for start, end in reagent_spans):
            return True
    return False


def _paper_ids(con, vault=None):
    if vault:
        return [row[0] for row in con.execute(
            "SELECT paper_id FROM paper_vaults WHERE vault = ? ORDER BY paper_id", (vault,))]
    return [row[0] for row in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]


def _candidate_map(con, registry, records, paper_ids, vault=None):
    allowed = set(paper_ids)
    out = {}
    for record in records:
        candidates = set()
        for variant in [record["name"], *record.get("synonyms", [])]:
            candidates.update(_fts_candidates(con, variant, vault))
        out[record["concept_id"]] = candidates & allowed
    return out


def _link_stream(con, registry, paper_ids, candidate_records=None, vault=None, patterns=None):
    """Link selected papers while holding only one paper's text in memory."""
    records = list(candidate_records or registry.records)
    patterns = patterns or _compiled_patterns(registry)
    candidate_map = _candidate_map(con, registry, records, paper_ids, vault)
    by_id = {record["concept_id"]: record for record in records}
    inserted = 0
    for paper_id in paper_ids:
        # One read of each per-paper source is an explicit scale invariant.
        sentences = _read_sentences(paper_id)
        studied = _read_studied_terms(paper_id, registry)
        eligible = [s for s in sentences if s.get("section") != "references"
                    and (s.get("kind") == "table_row" or not s.get("in_table"))]
        for concept_id in sorted(by_id):
            if paper_id not in candidate_map.get(concept_id, set()):
                continue
            record = by_id[concept_id]
            matched = [s for s in eligible if any(
                _pattern_matches(s.get("text") or "", pattern, record) for pattern in patterns[concept_id])]
            if not matched:
                continue
            matched.sort(key=lambda s: s.get("seq", 0))
            con.execute("INSERT OR REPLACE INTO concept_links VALUES (?,?,?,?,?,?)",
                        (concept_id, paper_id, "studied" if concept_id in studied else "mentioned",
                         len(matched), json.dumps(sorted({s["section"] for s in matched})),
                         matched[0]["sentence_id"]))
            inserted += 1
    return inserted


def _link_changed_exact(con, registry, records, paper_ids, patterns=None):
    """Scan each other paper for changed concepts without FTS candidate pruning."""
    records = list(records)
    patterns = patterns or _compiled_patterns(registry)
    by_id = {record["concept_id"]: record for record in records}
    inserted = 0
    for paper_id in paper_ids:
        sentences = _read_sentences(paper_id)
        studied = _read_studied_terms(paper_id, registry)
        eligible = [s for s in sentences if s.get("section") != "references"
                    and (s.get("kind") == "table_row" or not s.get("in_table"))]
        for concept_id in sorted(by_id):
            record = by_id[concept_id]
            matched = [s for s in eligible if any(
                _pattern_matches(s.get("text") or "", pattern, record) for pattern in patterns[concept_id])]
            if not matched:
                continue
            matched.sort(key=lambda s: s.get("seq", 0))
            con.execute("INSERT OR REPLACE INTO concept_links VALUES (?,?,?,?,?,?)",
                        (concept_id, paper_id, "studied" if concept_id in studied else "mentioned",
                         len(matched), json.dumps(sorted({s["section"] for s in matched})),
                         matched[0]["sentence_id"]))
            inserted += 1
    return inserted


def _registry_snapshot(path):
    if not path.exists():
        return {}
    return {record["concept_id"]: record for record in db.read_jsonl(path)}


def _changed_records(previous, registry):
    changed = []
    for record in registry.records:
        old = previous.get(record["concept_id"])
        old = old if old is None else {"classified_by": record.get("classified_by"), **old}  # a file from before the field
        if old is None or json.dumps(old, sort_keys=True) != json.dumps(record, sort_keys=True):
            changed.append(record)
    return changed


def _stats(start):
    data = dict(_LINK_STATS)
    data["total_files"] = data.get("sentence_files", 0) + data.get("evidence_files", 0)
    data["elapsed_seconds"] = round(time.perf_counter() - start, 6)
    return data


def rebuild_links(con, vault=None):
    """Write concepts.jsonl and rebuild links, optionally only for one vault."""
    start = time.perf_counter()
    _LINK_STATS.clear()
    invalidate_registry(con)
    registry = build_registry(con)
    write_registry(registry)
    paper_ids = _paper_ids(con, vault)
    if vault:
        for paper_id in paper_ids:
            con.execute("DELETE FROM concept_links WHERE paper_id = ?", (paper_id,))
    else:
        con.execute("DELETE FROM concept_links")
    linked = _link_stream(con, registry, paper_ids, vault=vault)
    con.commit()
    return {"concepts": len(registry.records), "links": linked, "papers": len(paper_ids),
            "timing_seconds": _stats(start)["elapsed_seconds"], "file_reads": _stats(start)}


def relink_paper(con, paper_id, before_terms=(), before_concepts=()):
    """Refresh one paper after analysis or Tier 1 extraction. `before_*`: the paper's terms / concept links from
    before `db.load_paper` wiped them (they decide which concept notes must be rewritten)."""
    start = time.perf_counter()
    _LINK_STATS.clear()
    path = config.STORE / "concepts.jsonl"
    previous = _registry_snapshot(path)
    invalidate_registry(con)
    registry = build_registry(con)
    write_registry(registry)
    current_ids = {record["concept_id"] for record in registry.records}
    stale_ids = set(previous) - current_ids
    if current_ids:
        stale_ids.update(row[0] for row in con.execute(
            "SELECT DISTINCT concept_id FROM concept_links WHERE concept_id NOT IN ({})".format(
                ",".join("?" for _ in current_ids)), tuple(current_ids)))
    else:
        stale_ids.update(row[0] for row in con.execute("SELECT DISTINCT concept_id FROM concept_links"))
    changed = _changed_records(previous, registry)
    changed_ids = {record["concept_id"] for record in changed}
    affected_ids = changed_ids | stale_ids
    affected_vaults = set()
    if affected_ids:
        affected_vaults.update(vault for vault, cid in con.execute(
            "SELECT DISTINCT pv.vault, cl.concept_id FROM concept_links cl "
            "JOIN paper_vaults pv ON pv.paper_id = cl.paper_id") if cid in affected_ids)
    if stale_ids:
        con.executemany("DELETE FROM concept_links WHERE concept_id = ?", ((cid,) for cid in stale_ids))
    if changed_ids:
        con.executemany("DELETE FROM concept_links WHERE concept_id = ?", ((cid,) for cid in changed_ids))
    paper_concepts = set(before_concepts) | {row[0] for row in con.execute(
        "SELECT DISTINCT concept_id FROM concept_links WHERE paper_id = ?", (paper_id,))}  # links it had before
    con.execute("DELETE FROM concept_links WHERE paper_id = ?", (paper_id,))
    all_papers = _paper_ids(con)
    target = [paper_id] if paper_id in all_papers else []
    patterns = _compiled_patterns(registry)
    linked = _link_stream(con, registry, target, patterns=patterns)
    # Exact scan for changed concepts. FTS candidate pruning is intentionally
    # avoided here: the full matcher is the completeness authority. Work remains
    # O(papers) only when the registry actually changes, not on every relink.
    other_papers = [pid for pid in all_papers if pid != paper_id]
    if changed and other_papers:
        linked += _link_changed_exact(con, registry, changed, other_papers, patterns=patterns)
    if affected_ids:
        affected_vaults.update(vault for vault, cid in con.execute(
            "SELECT DISTINCT pv.vault, cl.concept_id FROM concept_links cl "
            "JOIN paper_vaults pv ON pv.paper_id = cl.paper_id") if cid in affected_ids)
    paper_concepts.update(row[0] for row in con.execute(
        "SELECT DISTINCT concept_id FROM concept_links WHERE paper_id = ?", (paper_id,)))  # and links it has now
    # a concept note also lists papers that only carry the concept as a term (concept_notes._collect)
    for term in [*before_terms, *(r[0] for r in con.execute("SELECT term FROM paper_terms WHERE paper_id = ?", (paper_id,)))]:
        matched = registry.lookup(term)
        if matched:
            paper_concepts.add(matched["concept_id"])
    con.commit()
    stats = _stats(start)
    return {"paper_id": paper_id, "linked": linked, "new_or_changed_concepts": len(changed),
            "dropped_concepts": len(stale_ids), "affected_vaults": sorted(affected_vaults),
            # concepts whose notes can differ now: the paper's old and new links, new/changed/dropped concepts
            "affected_concepts": sorted(paper_concepts | affected_ids | {  # + the old parents of a re-parented/dropped concept
                parent for cid in affected_ids for parent in (previous.get(cid) or {}).get("parents") or []}),
            "timing_seconds": stats["elapsed_seconds"],
            "file_reads": stats}
