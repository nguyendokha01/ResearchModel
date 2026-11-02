"""Make a clean zip of the software to share (e.g. with students): `kb.py package`.

Never included: config/secrets.yaml (API keys), .venv, .git, caches, backups, logs.
Not included unless asked: the researcher's library (knowledge_store papers/db/notes, vaults, sources, review).
Included by default: the public MeSH dictionary and the local embedding model, so search works offline.
In the copy, config/ai.yaml uses provider `mcp_agent` (no AI is called until the receiver chooses one).
The zip is written file by file (low memory).
"""

import json
import os
import re
import unicodedata
import zipfile
import yaml
from datetime import datetime
from pathlib import Path

from . import config

CODE = ["scripts", "tests", "docs", "config", "tools", ".claude/skills"]
ROOT_FILES = ("*.bat", "*.md", "requirements.txt", ".gitignore", ".gitattributes", ".editorconfig")
NEVER = re.compile(r"(^|/)(\.git|\.venv|__pycache__|\.scratch_[^/]*|backups|concept_history|removed|cache|logs)(/|$)"
                   r"|(^|/)config/secrets\.ya?ml|\.pyc$|\.bak-\d+"
                   r"|^docs/(demo3/|rag_benchmark/|source_requirements/|status/|plan/|STATUS\.md$|REQUIREMENTS\.md$|PLAN\.md$|DECISIONS\.md$|AUDIT_[^/]*$)|^\.claude/skills/(codex-job|lead-board)/", re.I)
MODELS = ["knowledge_store/indexes/mesh.sqlite", "knowledge_store/embeddings/model"]
LIBRARY = ["knowledge_store", "vaults", "sources", "review", "projects"]
EMPTY_DIRS = ["sources/inbox/", "vaults/", "knowledge_store/", "review/pending/"]


def _files(rel):
    path = config.ROOT / rel
    if path.is_file():
        yield path
    elif path.is_dir():
        yield from (p for p in sorted(path.rglob("*")) if p.is_file())


def _student_config(name, text):
    if name == "config/ai.yaml":
        data = yaml.safe_load(text) or {}
        data.setdefault('ai', {}).update(provider='mcp_agent', tasks={})
        text = '# No automatic AI calls until the recipient explicitly configures a provider.\n' + yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False)
    if name == "config/ingest.yaml":
        text = re.sub(r'(?m)^(\s*default_vault:\s*).*$', r'\1""', text, count=1)
    return text


def build(out=None, with_models=True, with_library=False):
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    out = Path(out) if out else config.ROOT.parent / f"biomedical_kb_{stamp}.zip"
    out.parent.mkdir(parents=True, exist_ok=True)
    rels = list(CODE)
    rels += [p.name for pattern in ROOT_FILES for p in sorted(config.ROOT.glob(pattern))]
    if with_models:
        rels += MODELS
    if with_library:
        rels += LIBRARY
    added, skipped, total = set(), 0, 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rel in rels:
            for path in _files(rel):
                name = path.relative_to(config.ROOT).as_posix()
                if name in added or path.resolve() == out.resolve():  # never zip the zip being written
                    continue
                if NEVER.search(name):
                    skipped += 1
                    continue
                if name in ("config/ai.yaml", "config/ingest.yaml"):
                    z.writestr(name, _student_config(name, path.read_text(encoding="utf-8")))
                else:
                    z.write(path, name)
                added.add(name)
                total += path.stat().st_size
        for d in EMPTY_DIRS:
            if not any(n.startswith(d) for n in added):
                z.writestr(d, "")
    return {"zip": str(out), "files": len(added), "uncompressed_mb": round(total / 1e6, 1),
            "zip_mb": round(out.stat().st_size / 1e6, 1), "skipped_private_or_cache": skipped,
            "library_included": with_library, "models_included": with_models,
            "secrets_included": False}


# ---------------------------------------------------------------- clean GitHub release (W7)
# Never published on top of NEVER: development rules and hooks, progress/requirements, the researcher's library.
# The release root has exactly two .bat files; KEEP = scripts (minus the dev scripts below), config, tools/research_workspace.py,
# README, HUONG_DAN, AGENTS (trimmed), docs ARCHITECTURE/CONTRACTS/AGENT_TOOLS/AI_ANALYSIS_GUIDE/STANDARD*, tests, requirements.
RELEASE_NEVER = re.compile(r"^\.claude/|^CLAUDE\.md$"
                           r"|^(?!1 - |2 - )[^/]*\.bat$"  # root .bat files other than the two the receiver needs
                           r"|^(scripts/(ui|accept_requirements|benchmark_research)"
                           r"|tools/(run|verify|accept_revision)"
                           r"|tests/(test_accept_requirements|test_research_benchmark|test_review_gate|test_accept_revision))\.py$", re.I)
RELEASE_TAIL = """## Where to read more

| Need | Read |
|---|---|
| A rule, by clause id | `config/standard.yaml`, `scripts/kb.py standard` |
| Formats, IDs, commands | `docs/CONTRACTS.md`, `docs/AGENT_TOOLS.md` |
| How an AI analyses a paper | `docs/AI_ANALYSIS_GUIDE.md` |
| Design and full research policy | `docs/ARCHITECTURE.md` |
"""
PRIVATE_DOC_LINE = re.compile(r"(?m)^.*docs/(STATUS\.md|REQUIREMENTS\.md|PLAN\.md|DECISIONS\.md|AUDIT_|status/|plan/|rag_benchmark|source_requirements).*\n?")
DEV_SECTION = re.compile(r"(?ms)^## Working on the code.*?(?=^## |\Z)")  # also when it is the last section
LEAK_PATTERNS = [(re.compile(r"\b[\w.+-]+@(?:gmail|yahoo|outlook|hotmail)\.\w+", re.I), "personal email")]


def _machine_patterns():
    """Read from this machine at run time, so the shipped code itself names no private folder or person."""
    try:
        top = os.listdir(config.ROOT.resolve().anchor)  # the folders of the project's drive (the project, other vaults)
    except OSError:
        top = []
    places = "|".join(map(re.escape, ["Users", *(n for n in top if not n.startswith(("Program", "Windows", "$")))]))
    found = [(re.compile(rf"\b[A-Za-z]:[\\/]+(?:{places})\b", re.I), "machine path")]
    users = {Path.home().name, os.environ.get("USERNAME", ""), os.environ.get("USER", "")}
    return found + [(re.compile(rf"\b{re.escape(n)}\w*", re.I), "personal name") for n in sorted(users)
                    if len(n) >= 4 and n.lower() not in {"user", "users", "admin", "administrator", "public", "default", "root"}]


def _release_text(name, text):
    text = _student_config(name, text)
    if name == "config/vaults.yaml":  # the researcher's vault names stay private
        text = re.sub(r"(?ms)^vaults:.*", "vaults: {}\n", text)
    if name.endswith(".md"):  # pointers to private progress files would dangle
        text = PRIVATE_DOC_LINE.sub("", text)
    return DEV_SECTION.sub(lambda _: RELEASE_TAIL, text) if name == "AGENTS.md" else text  # dev rules + table of private docs -> table of shipped docs


def _library_markers():
    """Paper IDs (and their author+year stem), DOIs, titles and vault names of the researcher's library: none may appear in a release."""
    markers = [(name.lower(), "library vault name") for name in config.research_vaults()]
    for folder in sorted(config.PAPERS_DIR.glob("*")) if config.PAPERS_DIR.is_dir() else []:
        stem = re.match(r"([a-z]+)(\d{4})", folder.name)
        if stem:  # also "smith_2020", the form test fixtures use
            markers += [(stem.group(0), "library paper id stem"), ("_".join(stem.groups()), "library paper id stem")]
        meta = {}
        try:
            meta = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        markers.append((folder.name, "library paper id"))
        if meta.get("doi"):
            markers.append((str(meta["doi"]).lower(), "library DOI"))
        family = ((meta.get("authors") or [{}])[0] or {}).get("family")
        if family and meta.get("year"):
            markers.append((f"{family} {meta['year']}".lower(), "library citation"))
        if len(meta.get("title") or "") > 30:
            markers.append((meta["title"][:40].lower(), "library title"))
    return markers


SENTENCE_WINDOW = 45  # a shipped text sharing this many characters with a library sentence is a leak


_ADJACENT_LITERALS = re.compile(r"""(["'])(\s*)[rRbBfFuU]{0,2}\1""")  # 'a ' 'b' / "a "\n f"b": one text


def _norm(text):
    return " ".join(unicodedata.normalize("NFC", text).casefold().split())


def _shipped(text):
    """A shipped file as one normalised text (adjacent string literals joined) and the line of each character."""
    words, lines = [], []
    for n, line in enumerate(_ADJACENT_LITERALS.sub(lambda m: m.group(2) or " ", text).split("\n"), 1):
        for word in _norm(line).split():
            words.append(word)
            lines += [n] * (len(word) + 1)
    return " ".join(words), lines


def _windows(t):
    return (hash(t[i:i + SENTENCE_WINDOW]) for i in range(len(t) - SENTENCE_WINDOW + 1))


def _library_windows(wanted):
    """The hashes in `wanted` that are a 45-character window of a library sentence (whitespace-normalised, NFC, casefold).

    Reads each paper's sentences.jsonl one line at a time and keeps only the matches, so memory follows the shipped
    text, not the library. Empty when the machine has no store, so a release copy scans nothing."""
    found = set()
    for folder in sorted(config.PAPERS_DIR.glob("*")) if config.PAPERS_DIR.is_dir() else []:
        try:
            f = open(folder / "sentences.jsonl", encoding="utf-8")
        except OSError:
            continue
        with f:
            for line in f:
                try:
                    found.update(h for h in _windows(_norm(json.loads(line).get("text") or "")) if h in wanted)
                except (ValueError, AttributeError):
                    continue
    return found


def _sentence_hits(text, windows):
    """(line, length) per run of overlapping 45-character windows of `text` found in `windows`; never the text itself."""
    (t, lines), w, hits, start = _shipped(text), SENTENCE_WINDOW, [], None
    for i in range(len(t) - w + 2):  # one step past the end closes a run that reaches it
        if i <= len(t) - w and hash(t[i:i + w]) in windows:
            start, end = (i if start is None else start), i + w
        elif start is not None:
            hits, start = hits + [(lines[start], end - start)], None
    return hits


def release(dest):
    """Copy the shareable software into `dest` (a git working folder) and scan it for private traces.

    Files of an earlier release that are no longer shipped are removed from `dest` (only files this function wrote,
    listed in `dest/.release_files`). `.git` is never touched. Returns the file count and every leak hit."""
    dest = Path(dest).resolve()
    if config.ROOT.resolve() in (dest, *dest.parents):
        raise ValueError("the release folder must be outside the project")
    listing = dest / ".release_files"
    previous = set(listing.read_text(encoding="utf-8").splitlines()) if listing.is_file() else set()
    if dest.exists() and not previous and any(p.name != ".git" for p in dest.iterdir()):
        raise ValueError(f"{dest} is not empty and is not an earlier release")
    rels = list(CODE) + [p.name for pattern in ROOT_FILES for p in sorted(config.ROOT.glob(pattern))]
    written = []
    for rel in rels:
        for path in _files(rel):
            name = path.relative_to(config.ROOT).as_posix()
            if name in written or NEVER.search(name) or RELEASE_NEVER.search(name):
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix.lower() in {".py", ".md", ".yaml", ".yml", ".json", ".txt", ".bat", ".sh", ".toml", ".cfg",
                                       ".css", ".js", ".html", ""} or name.startswith("."):
                text = _release_text(name, path.read_bytes().decode("utf-8"))  # keeps CRLF of .bat files
                target.write_bytes(text.encode("utf-8"))
            else:
                target.write_bytes(path.read_bytes())
            written.append(name)
    for name in sorted(previous - set(written)):
        if not (dest / name).resolve().is_relative_to(dest):  # a damaged listing must never reach outside dest
            continue
        (dest / name).unlink(missing_ok=True)  # shipped last time, private or gone now
    listing.write_text("\n".join(written) + "\n", encoding="utf-8", newline="\n")
    markers, patterns = _library_markers(), LEAK_PATTERNS + _machine_patterns()
    texts = {}
    for name in written:
        try:
            texts[name] = (dest / name).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
    windows = set()
    if config.PAPERS_DIR.is_dir():  # ponytail: one set of every shipped window (~4 M hashes, ~300 MB), bounded by the software size
        windows = _library_windows({h for text in texts.values() for h in _windows(_shipped(text)[0])})
    leaks = []
    for name, text in texts.items():
        low = text.lower()
        for pattern, why in patterns:
            leaks += [f"{name}: {why}: {m.group(0)}" for m in pattern.finditer(text)]
        leaks += [f"{name}: {why}: {marker}" for marker, why in markers if marker in low]
        if windows:
            leaks += [f"{name}: library sentence text: line {n}, {k} characters" for n, k in _sentence_hits(text, windows)]
    return {"dest": str(dest), "files": len(written), "removed_from_earlier_release": len(previous - set(written)),
            "leaks": leaks}
