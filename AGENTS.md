# Biomedical Knowledge System — core rules

Every session and every sub-agent loads this file. It holds only what must never be missed. Everything else is read
on demand (table at the end).

## Mission

A curated biomedical research knowledge system. It must preserve scientific evidence, enable detailed retrieval,
build connections between papers and concepts, and support identification of potential knowledge gaps.
Accuracy and traceability are more important than producing an answer.

## Evidence rules (never broken)

1. Never present model memory or general knowledge as evidence from this project. If retrieved project sources do not
   support a statement, say it was not found in the current knowledge base. "Not found in this vault" never becomes
   "does not exist in the literature".
2. Never invent citations, page numbers, passages, statistics, methods, results or relationships. Agents reference
   sentence IDs; verbatim quotes are copied by code, never typed by an agent.
3. Important claims stay traceable: AI answer → claim → passage → section → page → paper → original source.
4. Keep the strength of every statement: association is not causation, an authors' hypothesis is not a finding,
   discussion is not a result, a review is not primary evidence, and hedges (may, suggest, associated, not
   significant) are never upgraded.
5. Original PDFs and supplements are immutable. Human content (`Mine/`, text outside `kb:auto` blocks, custom
   properties) is never overwritten.
6. Authority. The AI decides, marks the change as machine/AI, keeps it reversible and reports it: node labels,
   types, merges and splits; reversible removals; machine-added concepts; section and unit labels; conflict and gap
   candidates; figure links; readings the standard leaves open (the stricter one); parameters backed by a measured
   gate; model choice for allowed AI passes. Only the researcher, asked in chat (batched at a stop point): accepts
   science or a knowledge gap; irreversible deletes or purges; anything touching human content (rule 5); pushes to
   GitHub; sends papers to an AI provider not yet allowed; changes rules 1-5 and 9, what counts as evidence or the
   review-queue groups; settles a major app-logic error (two approved rules cannot both hold, risk of data loss).
7. All analysis rules live only in `config/standard.yaml` (`docs/STANDARD*.md` are generated from it). Read the clause
   you need (`scripts/kb.py standard`); never copy a rule into code, prompts or docs.
8. Canonical knowledge is English and original evidence is never changed. Explanations to the researcher are in
   Vietnamese (unless asked otherwise), plain words, short sentences.
9. Also kept from the full policy (`docs/ARCHITECTURE.md` §10): keep searchable full text and never drop a passage as
   unimportant; retrieved evidence carries paper id, title, DOI/PMID, section, page, original passage and context
   reference; semantic similarity alone is not evidence, re-read the context; before ingesting check DOI, PMID,
   title+year, file hash and never create a duplicate; never rebuild or re-analyse the whole library unless asked;
   a machine gap row is an "open hypothesis"; an unavailable source file is reported as unavailable; when unsure,
   quarantine or flag, never delete, and purging rejected papers is the researcher's step; no server listens outside
   127.0.0.1 or stdio, and the shared package never ships API keys or the private library.

The full research policy (duplicates, classification, gaps, vault, search, storage) is `docs/ARCHITECTURE.md` §10.

## Where to read more

| Need | Read |
|---|---|
| A rule, by clause id | `config/standard.yaml`, `scripts/kb.py standard` |
| Formats, IDs, commands | `docs/CONTRACTS.md`, `docs/AGENT_TOOLS.md` |
| How an AI analyses a paper | `docs/AI_ANALYSIS_GUIDE.md` |
| Design and full research policy | `docs/ARCHITECTURE.md` |
