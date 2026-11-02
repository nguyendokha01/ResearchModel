# Biomedical Knowledge System

A local, evidence-first knowledge base for biomedical papers. PDF → sentence-level full text with IDs → evidence units
proposed by any AI and **checked by code** (verbatim quotes are copied by code, never typed by the AI) → Obsidian vaults
per research topic, a local search page with checked AI answers, a knowledge graph, concept/node notes that keep
contradictions side by side, and an MCP server so any AI app can do the analysis work.

- Windows, Python 3.11+ (3.13 tested). Built for a biomedical researcher who is not a programmer; 8 GB RAM, 1 000-5 000 papers.
- Runs only on the local machine: the web page binds `127.0.0.1`; the MCP process runs on stdio, started by an AI app on demand.
- Code checks do not certify scientific interpretation. Only the researcher accepts science.

## Quick start

Double-click (details in `HUONG_DAN.md`, in Vietnamese):

| File | Purpose |
|---|---|
| `1 - Mo trang chinh.bat` | first run installs itself (needs **Python 3.11+**; **Obsidian** is recommended for reading and writing notes, the software runs without it; creates `.venv`, installs `requirements.txt`); then the local web page: Library (import, analyse), Search, Neural (vault notes), Graph |
| `2 - Ket noi AI (MCP).bat` | connect Claude Desktop / Codex / Gemini CLI / Cursor to the knowledge base |

Command line, from the root:
`.venv\Scripts\python.exe scripts\kb.py <command>` (`-h` lists commands). Unit tests:
`.venv\Scripts\python.exe -m unittest discover -s tests -t .`.

## Layout

```
1 - Mo trang chinh.bat, 2 - Ket noi AI (MCP).bat   the two files to double-click
HUONG_DAN.md                     guide for the researcher (Vietnamese)
AGENTS.md                        the rules every AI session follows
scripts/kb.py, web.py            CLI and local web page
scripts/kbsys/                   the library (module map: docs/ARCHITECTURE.md)
tests/                           unit tests (offline, synthetic data, temp stores)
config/                          ai.yaml, ingest.yaml, system.yaml, standard.yaml, synonyms.yaml, ... (secrets.yaml is git-ignored)
tools/research_workspace.py      backup before and check after live changes
docs/                            ARCHITECTURE, CONTRACTS, AGENT_TOOLS, AI_ANALYSIS_GUIDE, STANDARD
sources/                         inbox/ (new files), active/ (immutable originals), archive/        - not in git
knowledge_store/                 machine data: papers/<id>/ (truth), library.db, vectors, caches   - not in git
vaults/                          mother vault; vaults/<topic>/ Obsidian vaults (human layer)         - not in git
review/                          items awaiting a human decision                                     - not in git
```

Back up `sources/`, `knowledge_store/`, `vaults/`, `review/` separately: git excludes them.

## Where to read next

| You are | Read |
|---|---|
| the researcher | `HUONG_DAN.md` |
| an AI querying or analysing papers | `AGENTS.md` (rules), `docs/AGENT_TOOLS.md`, `docs/AI_ANALYSIS_GUIDE.md` |
| changing code or data formats | `docs/ARCHITECTURE.md` -> `docs/CONTRACTS.md` (single source of truth for formats, IDs, validator codes, MCP tools) |
