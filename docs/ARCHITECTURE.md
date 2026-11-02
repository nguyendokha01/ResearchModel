# Architecture

Local, evidence-first knowledge system for biomedical papers. Python 3.13 plus SQLite and ordinary files. No server
except the approved `127.0.0.1` web page and the stdio MCP process (see `AGENTS.md`). Target: 1 000–5 000 papers on an
8 GB laptop, one paper at a time. Data formats are in `docs/CONTRACTS.md`; this file explains how the parts fit.

## 1. Pipeline

```
PDF (immutable, sources/active/)
  │ code only
  ▼
Tier 1  pages → sections → passages → SENTENCES with IDs (S0001…) + references + citations + figures/tables (+crops, OCR)
  │ AI by MCP or provider; every reply checked by code
  ▼
Tier 2  evidence units that only POINT at sentence IDs (V1–V13). Code copies the verbatim quote.
        Structured profiles: overview, Introduction WH (5 answers), Methods protocols (G1/A7/WH1 checks)
        Node cards, figure/table readings (source-bound, same idea)
  │ code
  ▼
Machine store  knowledge_store/ (JSONL per paper = truth; library.db = rebuildable index; vectors; MeSH)
  │
  ├─► Search  find.py (keywords + synonyms + MeSH + vectors, fused) ─► web page ─► AI answer checked by A1–A9
  ├─► Neural  real Markdown + wikilinks of one vault ─► ticketed retrieval ─► checked answer
  ├─► Connect concepts, ghost papers, relations (contradicts / direction_differs / effect_vs_no_effect), graph
  └─► Obsidian vaults (human layer): paper notes, node notes, EVD/CLM/CONFLICT notes, maps, mother vault
```

Three layers: the machine store is the truth; the note network (vault Markdown written by code) is the human layer;
the AI app (Claude Desktop, Codex) is where brainstorming runs, through the MCP tools. Obsidian is recommended for
reading and writing notes but not required: the software runs without it. Conclusions of a discussion are saved back
as notes (`save_discussion`, decision notes).

Rule behind everything: AI proposes, code copies text and checks. A fact shown to the researcher is traceable
AI answer → claim → retrieved passage → section → page → paper → original PDF.

## 2. Seven resumable stages (`pipeline.py`, `kb.py steps`, Library page)

`ingest` (inbox → papers, Tier 1) → `quality` (font/encoding/OCR preflight) → `structure` (section/acronym/protocol
inventory + media inventory, no AI) → `analyze` (evidence + WH + protocol; with `mcp_agent` it returns `waiting`) →
`connect` (concept links, ghosts, relation proposals, concept notes without AI, notes refresh) → `vectors` (embeddings)
→ `notes` (`notes.refresh_all` only). Each stage writes a checkpoint (`knowledge_store/workflow/`) with input and
output fingerprints, so a cache hit is real or the stage reruns. A waiting or failed state is never "done".
Known mismatch: `steps notes` does less than `kb.py notes` (see STATUS, gap T7).

## 3. Code map (`scripts/`)

Entry points
- `kb.py` CLI (all commands: `docs/AGENT_TOOLS.md`). `web.py` local page (stdlib `http.server`, JS in `kbsys/web/`).
  Development-only scripts (`accept_requirements.py` requirement matrix, `benchmark_research.py` retrieval benchmark) are not shipped.
- `tests/` unit tests (offline, temp dirs) and `tests/smoke_demo3.py` (HTTP/MCP smoke on a temp store).

`scripts/kbsys/` (one line each)
- Extraction (Tier 1): `extract` PDF text, headings, columns, OCR hooks; `sentences` sentence split, citations,
  continuation links; `references` reference-list parser; `objects` figures, tables, crops; `tier1` builds all Tier 1
  files per paper; `quality` offline preflight; `media` source-region bundles, crops, locators; `ingest` inbox → papers,
  duplicate detection (DOI, PMID, title+year, hash), supplements; `metadata` Crossref/PubMed lookup (DOI/title only).
- Evidence and AI: `evidence` unit validation V1–V12 and storage; `crosscheck` V13 second-model review; `analysis`
  (v1 legacy import); `analysis_runner` automatic analysis and structured runs with repair loops; `analysis_transport`
  compact reading, repair merge, provider calls incl. images; `agent_tasks` MCP agent as provider; `structure`
  inventory, WH/protocol/overview profiles and validators; `node_cards` typed node cards; `figure_reading` vision
  readings; `answer` provider settings, answer views, checks A1–A9; `mcp_server` MCP tools; `mcp_setup` client config snippets.
- Search: `find` ranked hits; `search` v1 passage search; `vectors` multilingual-e5-small embeddings; `mesh` MeSH
  dictionary; `neural` vault Markdown index, traversal, tickets, node reader; `overlap` lexical overlap; `results`
  Results/figure/table browser; `graph` web graph data.
- Knowledge graph: `concepts` registry and links; `relations` enrichment and conflict proposals; `ghosts` cited-but-absent
  papers; `discovery` OpenAlex citing works (opt-in); `refmanager` Zotero/BibTeX/RIS import; `sources_misc` non-PDF sources.
- Vault: `notes` paper notes, indexes, graph config, folders; `concept_notes` node notes and synonym synthesis;
  `node_profile` source-copied node inventories; `result_notes` EVD/CLM/CONFLICT notes; `source_notes`; `vault_layout`
  layout v2 migration, maps, Obsidian settings; `mother_vault` parent vault; `vault_plugins` pinned plugins;
  `presentation` kinds, roles, colours; `review_policy` topic relevance and exact dedup.
- Infrastructure: `config` paths and YAML; `db` SQLite schema and loaders; `workflow` checkpoints and writer lock;
  `pipeline` the seven stages; `library` vaults, uploads, background job for the web page; `manage` archive/restore/
  delete/review actions; `package` key-free share zip; `selftest` data-contract checks.

`tools/` (developer helpers): `run.py` run a script with the project venv; `verify.py` the local check bundle;
`research_workspace.py` snapshot/audit of a vault; `accept_revision.py` preservation audit.

## 4. Data stores

```
sources/inbox/batches/<batch>/   new PDFs/ZIPs (any sub-folder = one batch)
sources/active/<paper_id>/       original PDFs after ingest, never modified (supp/ for supplements)
sources/archive/{papers,packed}/ archived PDFs and unpacked ZIPs
knowledge_store/
  papers/<paper_id>/             per-paper JSON/JSONL (truth) — file list in CONTRACTS §2
  library.db                     SQLite index (rebuild-db); also review_items, concept_links, paper_vaults
  concepts.jsonl relations.jsonl ghosts.jsonl measure_groups.json gap_candidates.jsonl
  node_cards/<vault>/            accepted node cards
  sources/<source_id>/           non-PDF sources
  embeddings/ indexes/           vectors (cache) and MeSH sqlite
  cache/                         vault_index, neural_sessions, answers (disposable)
  workflow/ locks/ logs/ ai_replies/ discovery/ migrations/
  backups/ concept_history/ analysis history/ review_policy_history/ stub_history/ removed/   recoverable history
  ui_preferences.json            selected vault (per device)
vaults/<vault>/                  Obsidian vaults (human layer); vaults/ itself is the mother vault
review/pending/                  quarantined duplicates/unreadable PDFs awaiting a human decision
projects/vault_registry.json     logical catalogue of vaults (does not widen search)
runtime/ migration_archive/      local scratch, backups, acceptance output (git-ignored)
```

Git tracks code, config, docs and tools. It ignores PDFs, ZIPs, `knowledge_store/`, `vaults/`, `sources/`, `runtime/`,
`config/secrets.yaml`. Back up `sources/`, `knowledge_store/`, `vaults/`, `review/` together; they are not in git.

## 5. Vault design (approved 2026-10-05)

- Mother vault = parent folder `vaults/` (option A); each sub-vault opens on its own. Wikilinks do not cross vault
  boundaries in Obsidian, so the parent holds `Home`, `_Maps` (Atlas, Efforts, Calendar, Open conflicts - all vaults) and
  `Registry/` (vault list, one `paper_ref` note per paper with its vaults). One canonical paper record lives in the
  machine store; each vault shows its own paper note (`also_in` lists the other vaults).
- Few folders; the note type is the `type` property (`docs/CONTRACTS.md` §8). Names follow Steph Ango's vault style:
  plural English folder names, short lower-case property names, one `type` drives colour.
- Human text is protected: machine writes only inside `kb:auto` blocks and machine keys; `Mine/` is never written; a
  hand-edited auto block is left alone. Raw sentences are never notes; long evidence goes to the dashboard via a link
  (size caps 120 KiB paper, 60 KiB node).
- Node content varies by type: substance, method/technique, organism/cell type, concept, result (EVD), claim (CLM),
  conflict, paper, topic map (MOC). Each node starts with a short summary card; missing data is stated as "chưa tìm thấy
  trong kho hiện tại", never invented. Source-derived sections use sentence IDs; AI-filled node cards are checked.
- Conflicts are never removed: a red `[!conflict]` callout at the top of each affected note, a red `conflict` node, red
  graph link colours (Extended Graph), listed in `_Maps/Open conflicts`. Red is used only for conflicts.
- Future vault kinds (`ideas`, `data`, `decisions`, `personality`) and Word/video/image inputs are only prepared for
  (`vault_kind`, `_Inbox/`, `sources_misc`). Video/audio/image evidence extraction (OCR/ASR) is not implemented.

## 6. Retrieval design

Hybrid, not GraphRAG: FTS5 keywords with synonym and MeSH expansion, multilingual embeddings, reciprocal-rank fusion,
filters, then re-reading of neighbouring sentences (Deep). Two routes: **Google-style** (`find.search`, evidence
units and sentences) and **Neural** (real Markdown notes and wikilinks of one vault, bounded 0–2 hops, then back to source
sentences). Notes are context, never evidence. Vault scope is the current vault unless the user asks otherwise.
sentence recall@10 0.56–0.58, 0.5–0.7 s; Neural route paper recall@5 0.44–0.51, sentence recall@10 0.06, ~40 s. After D3.29 the lead measured
Neural sentence@10 0.56 in about 2.4 s (note in the old task log; not stored in `results.json`, so re-run `benchmark_research.py`). Vietnamese
questions find the right paper but rarely the right sentence (sentence@10 0.13 against 0.65 for English). Gold answers were written by an AI and
are not yet researcher-approved.

## 7. AI integration

Every AI step is available three ways with the same checks: (1) MCP (`kb.py mcp`) where the connected app is the
provider; (2) providers in `config/ai.yaml` called by the software (`ai.tasks.<task>` per task); (3) command-line
`prepare`/`submit` pairs. Providers: `none, mcp_agent, ollama, anthropic, gemini, deepseek, openrouter,
openai_compatible, codex_cli, command`. Default is `mcp_agent` (no outbound AI call); `answer` is routed to `codex_cli`
(`gpt-5.6-luna`). Keys live only in `config/secrets.yaml`. `kb.py package` builds a zip without keys, `.venv`, `.git`,
caches and (by default) the library. AI calls are opt-in and bounded; never run bulk AI to clear a status.

## 8. Safety invariants (tested)

Original files never modified; every destructive step is recoverable (`removed/`, `*_history/`, snapshots); machine
writes take the writer lock; stale or changed sources invalidate derived work instead of being reused; `Mine/` hash
unchanged on every rebuild; stored quotes equal source slices (E1); associations stay associations and hedges stay
hedges (V5, A3, A7); contradictions are kept and shown side by side.

## 9. Testing

`tests/` (unittest, offline, temp stores; run `python -m unittest discover -s tests -t .` with the project venv),
`kb.py selftest` (data contracts on the live store), `web.py --check` (answer checks), `tests/smoke_demo3.py` (HTTP/MCP
on a temp store), `tools/verify.py` (all of these in one bundle; development checkout only),
`accept_requirements.py` (requirement matrix, read-only; development checkout only). Passing tests does not certify scientific correctness.

## 10. Research policy (moved verbatim from AGENTS.md, 2026-10-17)

The short, always-loaded version is the "Evidence rules" list in `AGENTS.md`; machine-checkable clauses are in
`config/standard.yaml`. This section keeps the full policy text.

### Core rule

Never present model memory or general knowledge as evidence from this project.
When answering a vault query, evidence must come from retrieved project sources.
If adequate evidence cannot be retrieved, say that it was not found in the current knowledge base.
Never invent citations, page numbers, passages, statistics, methods, results or relationships.

### Source hierarchy

AI answer → claim → retrieved passage → section → page → paper → original source.
Important claims must remain traceable through this chain.

### Original sources

Original PDFs and supplementary files are immutable. Never modify an original source file. Never permanently delete
one without explicit human approval. Sources may be locally active, archived, managed by an external reference
manager, or unavailable; if unavailable, say so when original-file verification is requested.

### Incremental ingestion and duplicates

The knowledge base is built incrementally; new batches may arrive at any time. Do not rebuild or re-analyse the
whole library unless explicitly requested or technically required. Before ingesting a paper check whether it exists.
Duplicate detection order: DOI, PMID, normalised title + year, file hash. If a paper exists, update or merge missing
source information; do not create a duplicate. The same paper in two vaults is one canonical record, not a duplicate.

### Batch processing and memory

A batch may contain many papers. Process papers individually or with very low concurrency. The project runs on
machines with limited RAM (8 GB). Do not load a whole batch or the whole knowledge base into memory.

### Full-text preservation

Preserve searchable full text whenever extraction is possible. Do not discard a passage because a model thinks it is
unimportant. Human-facing notes may be selective; machine-searchable evidence stays as complete as reasonably possible.

### Citation requirements

Retrieved evidence keeps: paper identifier, title, DOI and/or PMID, section, page, original passage, surrounding
context reference. Association must not be rewritten as causation. Authors' hypotheses must not become demonstrated
findings. Discussion statements are distinguished from experimental results. Review-paper statements are
distinguishable from primary evidence. Hedges (may, suggest, seems, associated, not significant) are never upgraded.

### Knowledge classification

CORE, CONNECTED, POTENTIAL_BRIDGE, UNCERTAIN, OUT_OF_SCOPE. An item is not rejected solely because it is not connected
to the existing graph; weakly connected or novel evidence may be POTENTIAL_BRIDGE. Present UNCERTAIN and OUT_OF_SCOPE
items for review when appropriate.

### Human approval

Agents may recommend actions. Agents must not independently: permanently delete source material; overwrite human
research notes; approve a knowledge gap as established; purge rejected papers. Technical readiness is not scientific
acceptance: only the researcher accepts science.

### Knowledge gaps

Agents may create candidate gaps. A candidate must distinguish "not found in the current vault" from "does not exist
in the scientific literature". Never claim a global literature gap from the local knowledge base. Candidates need human
review before becoming accepted Knowledge Gap notes.

Researcher definition (2026-10-13): machine-generated rows are "open hypotheses", not gaps. A knowledge gap is formed in a
discussion from cited evidence + the AI's general knowledge (always labelled as such, never presented as evidence) + the
researcher's judgment, and is saved (`kind: knowledge_gap`) only when the researcher agrees.

### Human-managed content

Human ideas, hypotheses, interpretations and personal notes (`Mine/`, text outside `kb:auto` blocks, custom
properties) are human-managed. Agents may reference them and propose edits; never overwrite them unless explicitly told.

### Machine-managed and AI-managed content

Agents may create and update machine content (extracted text, page/section maps, passages, indexes, embeddings,
metadata, logs, caches), kept outside the human-facing vault whenever possible, and AI-managed knowledge content (paper
notes, concepts, methods, entities, claims, relationships, topic links, citation relationships). Changes stay
traceable to source evidence.

### Vault philosophy

Obsidian is the human-facing knowledge layer: readable, navigable, visually useful. Do not create one note per raw text
chunk; raw passages and indexes belong in the machine store. A paper has one canonical record even if it belongs to
several topics, projects or vaults.

### Search behaviour

Default scope is the current vault; search across vaults only when requested. Combine when available: exact terms, full
text, metadata filters, synonym/entity expansion, semantic search, contextual re-reading. Semantic similarity alone is
not evidence. Re-read surrounding context before answering an important scientific query.

### Canonical language

Canonical biomedical knowledge is stored in English; original English evidence is never changed. Explanations may be
in Vietnamese or another requested language without replacing canonical evidence.

### Storage policy

Support local PDFs, archived PDFs, externally managed PDFs and future Zotero/Mendeley/EndNote integration. Do not assume
every PDF stays on the primary disk. Keep enough metadata and provenance to locate or restore archived sources.

### Software philosophy

Prefer simple commands, readable configuration, safe defaults, automatic validation and reversible operations. Avoid
unnecessary infrastructure: no servers, containers, databases or frameworks beyond need. SQLite and ordinary files are
preferred. Approved exceptions (researcher decision, 2026-09-25):
- a local search web page served only on `127.0.0.1`, started by a `.bat` file and stopped when closed;
- an MCP tool process on stdio, started on demand by an agent.

Neither may listen on an external network interface or run as a permanent background service. New dependencies
require the researcher's approval before installation. Every AI step must work through MCP and through several
providers; the shared package never ships API keys or the private library.

### Modification safety

Before a destructive change: identify what will change; preserve or verify recoverability; request human approval when
required. Prefer reversible operations over deletion. When uncertain, quarantine or flag instead of deleting.
