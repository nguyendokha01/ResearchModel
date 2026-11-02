"""`kb.py conform`: run the Analysis Standard (config/standard.yaml) against the data and the code, read-only.

Every clause either has a check registered here (`@check("EV-05", "paper")`) or is reported `not_implemented`;
a clause is never silently skipped and never PASS without a check. A check is a plain function that returns
(or yields) one string per violation; no violation = PASS. Four scopes decide what the function receives:

    paper   a `Paper` (one paper folder, read lazily; the runner holds one paper at a time)
    vault   a `Vault` (one vault folder of notes and attachments)
    store   a `Store` (library database + registries, spanning papers)
    probe   nothing: the check feeds built-in synthetic cases to the product function and flags a wrong verdict
            (it takes the judge as an optional argument so tests can hand it a broken one)

Thresholds come from the clause `params` in config/standard.yaml (`standard.param`), never from literals here.
Check modules are listed in MODULES; tests for every check live in tests/standard/.
"""
import importlib
import json
from collections import Counter
from pathlib import Path

from . import config, db, standard

PASS, FAIL, WARN, ERROR = "PASS", "FAIL", "WARN", "ERROR"
NOT_IMPLEMENTED, NO_DATA = "not_implemented", "no_data"
SCOPES = ("paper", "vault", "store", "probe")
MODULES = ("conform_intake", "conform_chunk", "conform_evidence", "conform_profile", "conform_graph", "conform_vault", "conform_answer", "conform_network")  # check modules under kbsys, e.g. "conform_paper"; each registers its checks on import
EXAMPLES = 5  # violations kept per clause in the report

CHECKS = {}  # clause id -> (scope, function, via_selftest)


def check(clause_id, scope, via_selftest=False):
    """Register `fn` as the check of a clause. `via_selftest`: it only re-uses a selftest function, which
    `kb.py selftest` already runs, so the selftest section of conform skips it."""
    assert scope in SCOPES, scope

    def register(fn):
        assert clause_id not in CHECKS, f"{clause_id} has two checks"
        CHECKS[clause_id] = (scope, fn, via_selftest)
        return fn
    return register


def load_checks():
    for name in MODULES:
        importlib.import_module(f"{__package__}.{name}")
    return CHECKS


def violations(clause_id, *args):
    """The violations one check finds in `args` (for tests and one-off use)."""
    return [str(v) for v in CHECKS[clause_id][1](*args) or ()]


# ---------------------------------------------------------------- data

class _Lazy:
    """Attributes load on first use from `_load`; any of them can be given up front (tests do)."""

    def __init__(self, **data):
        self._data = data

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        if name not in self._data:
            self._data[name] = self._load(name)
        return self._data[name]

    def _load(self, name):
        raise AttributeError(name)


class Paper(_Lazy):
    """One paper folder. Lists: sentences, passages, references, figures, tables, units, pages.
    Mappings: meta, analysis, structure, profile, protocol, overview. Missing file = empty."""
    FILES = {"sentences": "sentences.jsonl", "passages": "passages.jsonl", "references": "references.jsonl",
             "figures": "figures.jsonl", "tables": "tables.jsonl", "units": "evidence.jsonl", "pages": "pages.jsonl"}
    JSON = {"meta": "metadata.json", "analysis": "analysis.json", "structure": "structure.json",
            "profile": "section_profile.json", "protocol": "protocol_profile.json", "overview": "paper_overview.json"}

    def __init__(self, paper_id, folder=None, **data):
        super().__init__(**data)
        self.id, self.folder = paper_id, Path(folder) if folder else None

    def _load(self, name):
        if name not in self.FILES and name not in self.JSON:
            raise AttributeError(name)
        if not self.folder:
            return [] if name in self.FILES else {}
        if name in self.FILES:
            return list(db.read_jsonl(self.folder / self.FILES[name]))
        return db.read_json(self.folder / self.JSON[name], {}) or {}


class Vault(_Lazy):
    """One vault folder. `notes`: [(posix path relative to the vault, text)] of every .md file;
    `attachments`: relative paths of the files under _Attachments."""

    def __init__(self, name, root, con=None, **data):
        super().__init__(**data)
        self.name, self.root, self.con = name, Path(root), con

    def _load(self, name):
        if name == "notes":
            return [(p.relative_to(self.root).as_posix(), p.read_bytes().decode("utf-8", errors="replace"))
                    for p in sorted(self.root.rglob("*.md"))]
        if name == "attachments":
            base = self.root / "_Attachments"
            return [p.relative_to(self.root).as_posix() for p in sorted(base.rglob("*")) if p.is_file()] if base.is_dir() else []
        raise AttributeError(name)


class Store(_Lazy):
    """The library: `con` (SQLite), `paper_ids`, `concepts`, `relations`, `ghosts` (the registries as lists), `vaults` (a Vault each)."""
    FILES = {"concepts": "concepts.jsonl", "relations": "relations.jsonl", "ghosts": "ghosts.jsonl"}

    def __init__(self, con=None, root=None, **data):
        super().__init__(**data)
        self.con, self.root = con, Path(root) if root else config.STORE

    def _load(self, name):
        if name == "paper_ids":
            return [r[0] for r in self.con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]
        if name in self.FILES:
            return list(db.read_jsonl(self.root / self.FILES[name]))
        if name == "vaults":
            return [Vault(v, config.vault_dir(v), self.con) for v in vault_names(self.con)]
        raise AttributeError(name)

    def paper(self, paper_id):
        return Paper(paper_id, self.root / "papers" / paper_id)


# ---------------------------------------------------------------- runner

def _record(found, cid, where, vios):
    entry = found.setdefault(cid, {"n": 0, "examples": [], "where": Counter()})
    for v in vios:
        entry["n"] += 1
        entry["where"][where] += 1
        if len(entry["examples"]) < EXAMPLES:
            entry["examples"].append(f"{where}: {v}" if where else str(v))


def _call(found, cid, fn, where, *args):
    try:
        _record(found, cid, where, [str(v) for v in fn(*args) or ()])
    except Exception as err:  # a crashing check must show up, never read as PASS
        found.setdefault(cid, {"n": 0, "examples": [], "where": Counter()})["error"] = f"{type(err).__name__}: {err}"


def vault_names(con, vault=None):
    if vault:
        return [vault]
    return sorted({r[0] for r in con.execute("SELECT DISTINCT vault FROM paper_vaults")})


def run(con, stage=None, paper=None, vault=None, skip_selftest=False, data=None):
    """Run the standard; returns {"version", "status", "clauses": [row...], "summary": {status: n}}.
    Row: id, stage, level, status, violations, examples, where (violations per paper/vault), error."""
    data = data or standard.load()
    load_checks()
    clauses = [c for c in data["clauses"] if not stage or c["stage"] == stage]
    if not clauses:  # a typo must not read as "nothing failed"
        raise ValueError(f"Unknown stage: {stage!r}")
    if paper and not con.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper,)).fetchone():
        raise ValueError(f"Unknown paper: {paper!r}")
    if vault and vault not in vault_names(con):
        raise ValueError(f"Unknown vault (or no papers in it): {vault!r}")
    wanted = {c["id"] for c in clauses}
    todo = {s: [(cid, fn) for cid, (sc, fn, via) in CHECKS.items()
                if sc == s and cid in wanted and not (skip_selftest and via)] for s in SCOPES}
    found, ran = {}, set()
    store = Store(con)
    if todo["paper"]:
        if paper:
            ids = [paper]
        elif vault:
            ids = [r[0] for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ? ORDER BY paper_id", (vault,))]
        else:
            ids = store.paper_ids
        for pid in ids:  # one paper in memory at a time
            p = store.paper(pid)
            for cid, fn in todo["paper"]:
                _call(found, cid, fn, pid, p)
            ran.update(cid for cid, _ in todo["paper"])
    if not paper:
        for cid, fn in todo["probe"]:
            _call(found, cid, fn, "")
            ran.add(cid)
        for cid, fn in todo["store"]:
            _call(found, cid, fn, "", store)
            ran.add(cid)
        for name in vault_names(con, vault) if todo["vault"] else ():
            v = Vault(name, config.vault_dir(name), con)
            for cid, fn in todo["vault"]:
                _call(found, cid, fn, name, v)
                ran.add(cid)
    rows = []
    for c in clauses:
        cid, entry = c["id"], found.get(c["id"], {"n": 0, "examples": [], "where": Counter()})
        if cid not in CHECKS:
            status = NOT_IMPLEMENTED
        elif skip_selftest and CHECKS[cid][2]:
            continue
        elif cid not in ran:
            status = NO_DATA
        elif entry.get("error"):
            status = ERROR
        else:
            status = PASS if not entry["n"] else FAIL if c["level"] == "fail" else WARN
        rows.append({"id": cid, "stage": c["stage"], "level": c["level"], "status": status, "violations": entry["n"],
                     "examples": entry["examples"], "where": dict(entry["where"].most_common()),
                     **({"error": entry["error"]} if entry.get("error") else {})})
    return {"version": data["version"], "status": data["status"], "clauses": rows,
            "summary": dict(Counter(r["status"] for r in rows))}


def render_table(result):
    lines = [f"{'clause':<7} {'level':<5} {'status':<15} {'n':>6}  first violation"]
    for r in result["clauses"]:
        first = r.get("error") or (r["examples"][0] if r["examples"] else "")
        lines.append(f"{r['id']:<7} {r['level']:<5} {r['status']:<15} {r['violations']:>6}  {first[:100]}")
    lines.append("")
    lines.append(f"Standard v{result['version']} ({result['status']}): " +
                 ", ".join(f"{k} {v}" for k, v in sorted(result["summary"].items())))
    return "\n".join(lines)


def selftest_section(report, con, paper_id=None, strict=False):
    """The conformance section of `kb.py selftest`: clauses whose checks selftest already ran are skipped; a
    failing clause is a warning, or an error with `strict` (the default flips when the G4 work list is empty)."""
    result = run(con, paper=paper_id, skip_selftest=True)
    report.checked += sum(r["status"] in (PASS, FAIL, WARN, ERROR) for r in result["clauses"])
    for r in result["clauses"]:
        if r["status"] in (FAIL, WARN, ERROR):
            first = r.get("error") or (r["examples"][0] if r["examples"] else "")
            (report.error if strict and r["status"] != WARN else report.warn)(
                f"conform:{r['id']}", f"{r['status']} ({r['violations']}) {first}")
    n = result["summary"]
    report.warn("conform", "not_implemented: " + str(n.get(NOT_IMPLEMENTED, 0)) + " clauses (kb.py conform lists them)")
    return result


def write_json(result, path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
