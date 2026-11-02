"""PASS / FAIL cases of the stages NW (the neural-network route) and SF (safety and privacy), see scripts/kbsys/conform_network.py.

Probes run the product function in a throw-away project in every PASS case, so a product that is wrong today makes its PASS
case report violations (listed in RED). The FAIL cases hand the check a judge that is wrong for the reason the clause names.
`good_prepare` below is a thin wrapper over neural.prepare that does what NW-01/02/03/08/09 ask (relevance order, unit labels,
a small payload, needs_translation, not_found); test_network.py hands it to the checks to prove they can be satisfied.
"""
import contextlib
import importlib.util
import json
import re
import shutil
import unicodedata

from kbsys import config, mcp_server, neural, package
from kbsys.conform import Store

from .cases_intake import put


# scripts/benchmark_research.py is a development file that a release does not ship; the checks that need it yield nothing without it
NEEDS_BENCH = ("NW-01", "NW-02", "NW-09")
HAS_BENCH = importlib.util.find_spec("benchmark_research") is not None


def no_args(tmp):
    return ()


def store(tmp):
    """A Store whose project folder (the parent of its root) has no runtime/ folder: no gold file, so only the probe half runs."""
    return Store(root=tmp / "knowledge_store", paper_ids=[], vaults=[])


def tamper(real, edit):
    """A judge that calls the real function and edits its result."""
    return lambda *args, **kwargs: edit(real(*args, **kwargs))


# ---------------------------------------------------------------- NW-01 .. NW-03, NW-08, NW-09

def good_prepare(con, cfg, vault, question, hops=0, speed="fast"):
    """neural.prepare, then: items in relevance order (most question groups matched first), at most five, each with the
    unit_type of its stored unit, no repeated field, a short prompt; not found (no items) when no item matches every group."""
    question = unicodedata.normalize("NFC", question)
    full = neural.prepare(con, cfg, vault, question, hops=hops, speed=speed)
    groups = full["vault_context"].get("groups") or []
    units = [(r["unit_type"], set(json.loads(r["sentence_ids"]))) for r in con.execute("SELECT unit_type, sentence_ids FROM evidence")]

    def matched(item):
        low = item["text"].lower()
        return sum(1 for g in groups if any(str(t).lower() in low for t in g))
    ranked = sorted(full["items"], key=lambda item: -matched(item))
    not_found = bool(groups) and len(groups) > 1 and (not ranked or matched(ranked[0]) < len(groups))
    items = [] if not_found else [{**item, "unit_type": next((t for t, ids in units if item["id"] in ids), None)} for item in ranked[:5]]
    return {"question": question, "vault": vault, "speed": speed, "ticket": full["ticket"], "items": items, "not_found": not_found,
            "needs_translation": not question.isascii(), "vault_context": {"expanded_query": full["vault_context"].get("expanded_query"), "nodes": []},
            "prompt": full["prompt"][:2000]}


# ---------------------------------------------------------------- NW-05 / NW-04 judges

def path_tool(con, cfg, name, args):
    """A neural_path that answers the probe library as the clause asks (the answers are fixed, the contract is what is tested)."""
    pair = (args["source"], args["target"])
    if pair[0] == pair[1] or any(p.startswith("Results/") for p in pair):
        raise ValueError("source and target are two node notes")
    step = lambda a, b, n: {"source": a, "target": b, "etypes": ["observed_effect", "observed_effect"], "unit_id": f"pa:E{n:04d}", "sentence_ids": [f"pa:S{n:04d}"]}
    if pair == ("Aterm", "Cterm"):
        return {"found": True, "steps": [step("Aterm", "Bterm", 1), step("Bterm", "Dterm", 2), step("Dterm", "Cterm", 3)]}
    if pair == ("Xterm", "Yterm"):
        return {"found": True, "steps": [step("Xterm", "Hubterm", 8), step("Hubterm", "Yterm", 9)]}
    return {"found": False, "steps": []}


def paper_note_path(con, cfg, name, args):
    return {"found": True, "steps": [{"source": args["source"], "target": "Paper pa", "sentence_ids": []},
                                     {"source": "Paper pa", "target": args["target"], "sentence_ids": []}]}


def hop_search(vault, query, hops=0, **kwargs):
    """A neural.search whose second hop reaches Far through Bridge and skips the hub, the navigation neighbour and the index note."""
    notes = {0: ["Seed.md"], 1: ["Seed.md", "Concepts/Bridge.md"], 2: ["Seed.md", "Concepts/Bridge.md", "Far.md"]}[hops]
    return {"nodes": [{"path": p} for p in notes], "edges": []}


def flat_search(vault, query, hops=0, **kwargs):
    return {"nodes": [{"path": "Seed.md"}], "edges": []}


# ---------------------------------------------------------------- NW-06 / NW-07 judges

def accept_all(con, ticket, reply):
    return {"evidence": [{"id": "pa:S0001"}], "answer": [], "reasoning": []}


def replacing_check(con, ticket, reply):
    """neural.check, except that an item list supplied by the caller is used as the evidence."""
    checked = neural.check(con, ticket, {k: v for k, v in reply.items() if k != "items"})
    if reply.get("items"):
        return {**checked, "answer": [{"text": r["text"], "check": "ok"} for r in reply["answer"]], "evidence": reply["items"]}
    return checked


def deaf_dependency(paper_id):
    return "always the same"


# ---------------------------------------------------------------- SF judges

def leaky_release(dest):
    """Ships the private files and reports no leak."""
    for rel in ("config/secrets.yaml", "docs/PLAN.md", "CLAUDE.md", "scripts/kbsys/tool.py"):
        put(dest / rel, (config.ROOT / rel).read_bytes())
    put(dest / ".release_files", "config/secrets.yaml\ndocs/PLAN.md\nCLAUDE.md\nscripts/kbsys/tool.py\n")
    return {"dest": str(dest), "files": 4, "leaks": []}


def overwriting_save(vault, title, markdown, model=None, kind="discussion"):
    path = config.vault_dir(vault) / "Discussions" / f"{title}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(markdown, encoding="utf-8")
    return {"saved": path.relative_to(config.vault_dir(vault)).as_posix()}


@contextlib.contextmanager
def no_lock(name="library", timeout=0):
    yield


def project(tmp, server="Server(('127.0.0.1', 0), Handler)", extra=""):
    """A project folder for the SF-07 scan: one script that opens `server`, requirements.txt, .gitignore, a .bat file."""
    root = tmp / "project"
    put(root / "scripts" / "web.py", f"import yaml\nfrom http.server import ThreadingHTTPServer as Server\nHandler = None\n{server}\n{extra}")
    put(root / "scripts" / "ui.py", "button.bind('<Return>', lambda event: None)\n")  # a Tk bind is not a socket
    put(root / "requirements.txt", "pyyaml==6.0.3\n")
    put(root / ".gitignore", "config/secrets.yaml\n")
    put(root / "1 - Open.bat", "@echo off\npython scripts\\web.py\n")
    put(root / "config" / "ai.yaml", "ai:\n  provider: mcp_agent\n")
    return root


CASES = {
    "NW-01": (lambda tmp: (store(tmp),), lambda tmp: (store(tmp), tamper(good_prepare, lambda r: {**r, "padding": "x" * 300_000}))),
    "NW-02": (lambda tmp: (store(tmp),),
              lambda tmp: (store(tmp), tamper(good_prepare, lambda r: {**r, "items": r["items"][::-1], "media": [{"p": 1}], "figures": [{"p": 1}]}))),
    "NW-03": (no_args, lambda tmp: (tamper(good_prepare, lambda r: {**r, "items": [{**i, "unit_type": None} for i in r["items"]]}),)),
    "NW-04": (no_args, lambda tmp: (flat_search,)),
    "NW-05": (no_args, lambda tmp: (paper_note_path, ("neural_path",))),
    "NW-06": (no_args, lambda tmp: (accept_all,)),
    "NW-07": (no_args, lambda tmp: (neural._profile_fingerprint, deaf_dependency)),
    "NW-08": (no_args,
              lambda tmp: (lambda con, cfg, vault, question, hops=0, **kw: {"items": [], "needs_translation": False, "vault_context": {"expanded_query": question}},)),
    "NW-09": (lambda tmp: (store(tmp),), lambda tmp: (store(tmp), tamper(good_prepare, lambda r: {**r, "items": r["items"][::-1]}))),
    "SF-01": (no_args, lambda tmp: (lambda con, name, args: None,)),
    "SF-02": (no_args, lambda tmp: (overwriting_save,)),
    "SF-05": (lambda tmp: (store(tmp),), lambda tmp: (store(tmp), leaky_release)),
    "SF-06": (no_args, lambda tmp: (no_lock,)),
    "SF-07": (lambda tmp: (project(tmp),), lambda tmp: (project(tmp, "Server(('0.0.0.0', 80), Handler)", "KEY = 'sk-abcdefghijklmnopqrstuvwxyz0123'"),)),
}

RED = {
    "NW-09": "the mini gold run: an absence question returns its partial matches (6 items) instead of not found, because neural.prepare returns no not_found",
}
