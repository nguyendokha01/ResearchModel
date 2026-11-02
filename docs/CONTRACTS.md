# Data contracts (single source of truth)

Status: consolidated 2026-10-11 from `docs/v2/CONTRACTS.md` (v2.0.0) and `docs/demo3/CONTRACTS.md` (3.4.0).
It describes what the code does now. Superseded versions and history are in git (tag `pre-cleanup-2026-10-11`).

## 0. Rules

1. Contract first. Change a field, file, table, ID or tool in this file in the same commit as the code.
2. Additive changes (a new optional field) are allowed. Renaming or removing a field needs the researcher's
   approval, because stored data would need a migration.
3. Canonical content is English. Verbatim text is never paraphrased, translated or "cleaned" beyond section 2.0.
4. Every invariant listed here is enforced by `kb.py selftest` or a test. Code that breaks one must not be committed.
5. Technical readiness is never scientific acceptance. Derived files carry `scientific_acceptance: "not_assessed"`.
   Only the researcher accepts science.
6. All text files are written with LF (`newline="\n"` on every `write_text` / `open(..., "w"|"a"|"x")`, enforced by `tests/test_line_endings.py` and conform `CHR-03`). `.bat` files keep CRLF. Text received from a client (MCP `save_discussion`) goes through `config.lf()` once before it is saved (CRLF and CR become LF). Files that already hold CRLF (store, vault notes) are converted at G5, not by the writers: a note writer compares old and new text after a universal-newline read, so an unchanged CRLF note is not rewritten. `CHR-03` skips `Mine/` and `_Templates/` (human content). See `.gitattributes`.

## 1. Identifiers

| Object | Format | Example |
|---|---|---|
| Paper | `make_paper_id` (surname+year+title words+4 hex) | `smith2020_example_paper_title_1a2b` |
| Passage | `<paper_id>:<seq 4 digits>` | `…_73a6:0023` |
| Sentence | `<paper_id>:S<seq 4+ digits>`; short form `S0142` only in prompts/UI | `…_73a6:S0142` |
| Reference entry | `<paper_id>:R<num 3 digits>`; author-year lists use reading-order index | `…_73a6:R012` |
| Figure / Table | `<paper_id>:F<n>` / `:T<n>`; supplementary `:FS<n>` / `:TS<n>` | `…_73a6:F3` |
| Supplementary file | `<paper_id>:X<n>` | `…_73a6:X1` |
| Evidence unit | `<paper_id>:E<seq 4 digits>` (aliases: section 9) | `…_73a6:E0007` |
| Media locator | `M…` ids are locators only, never evidence IDs | |
| Ghost paper | `ghost:doi:<lowercase doi>` or `ghost:t:<sha1(normalised title + " " + year)[:12]>` | |
| Concept | `mesh:<descriptor UI>` or `local:<slug>` | |
| Non-PDF source | `src_<first 16 hex of SHA-256>`; sentences `<source_id>:S0001` | |
| Relation | `relation_id` (16 hex) | |

Sequence numbers follow reading order and are never reused inside a paper. A change to `extract.py` or the
sentence/passage/table building in `tier1.py` can shift sentence IDs and make evidence, profiles and readings of
that paper stale. Re-extraction therefore needs `reextract --dry-run` first (section 2.7).

## 2. Tier 1: extraction (code only, no AI)

Per-paper folder `knowledge_store/papers/<paper_id>/` is the durable source of truth. `library.db` is an index
rebuilt from it by `kb.py rebuild-db`.

| File | Written by | Content |
|---|---|---|
| `metadata.json` | ingest, tier1, evidence | bibliographic data, `extraction_version` (`tier1.EXTRACTION_VERSION` = 3; the 16 live papers are 2; `evidence.py:223` and `selftest.py:372` still demand exactly 2: bug B1 in STATUS), `citation_style`, `tier1{}`, `supplements[]`, `ocr_pages[]`, `source_files[]`, `reference_managers[]`, `duplicate_sources[]`, `classification`, `evidence_type`, `analysis_status` |
| `pages.jsonl`, `sections.json`, `passages.jsonl` | ingest | page text, section map, passages (target 1000, max 2000 chars) |
| `sentences.jsonl`, `references.jsonl`, `figures.jsonl`, `tables.jsonl` | tier1 | section 2.2–2.4 |
| `crops/` | tier1 | JPEG crops of figures and tables |
| `evidence.jsonl`, `analysis.json`, `analysis_state.json`, `analysis_rejected.json`, `analysis_quality.json` | evidence | section 3 |
| `analysis_runner.json`, `analysis_runner_reply.json`, `analysis_usage.jsonl`, `analysis_history/` | analysis_runner | checkpoint, last raw reply, usage log, archived previous files |
| `analysis_v13.json` | crosscheck | independent verifier report |
| `structure.json`, `section_profile.json`, `protocol_profile.json`, `paper_overview.json`, `structured_tasks/` | structure | section 5 |
| `media_manifest.json`, `media_regions.json`, `media_history/`, `figure_readings/` | media, figure_reading | section 8.6–8.7 |
| `review_policy.json`, `evidence_aliases.json` | review_policy | section 9 |

### 2.0 Text normalisation (passages, therefore sentences)

Only mechanical changes; text is never paraphrased.
- Whitespace collapsed, line-break hyphens resolved, ligatures replaced. A superscript run is written `^run`
  (`stroke.^1,7–10` is a citation; `10^5`, `cm^2`, `SSEA-3^+` are not).
- Verified font glyph repairs only (checked against the immutable PDF): `þ` to `+` when a standalone marker, BEL
  (`U+0007`) to `±`, and exact per-font maps for Advent-style subset fonts listed in `extract.py`. Unverified symbol-font
  codes stay unchanged and raise selftest warning `FS2`. PDF bytes are never changed.
- Page furniture (repeated headers/footers, printed page numbers, sideways margin text, link-only blocks) leaves passages
  but stays in page text. Missing word spaces are restored (gap above 0.12 em in a 20+ character run).
- Sidebar/footnote blocks (Citation, Received/Accepted, Copyright, Correspondence, Funding, Competing interests, Data
  availability…) become passages labelled `front_matter`, `funding`, `conflicts` or `data_availability` with
  `"sidebar": true`; they never change the running section. Run-in labels of a structured abstract start a new passage.
- A note printed after the last reference ends the list (D-PENDING-20): while the section is `references`, a block starting
  "Publisher's note" is labelled `notes`, one starting "See www… for supporting information" `supplementary`; its text no longer
  enters the last reference's `raw`. A reference block after a `notes` block resumes the list.
- Blocks are read in column order (XY-cut, 1–3 columns); rotated landscape tables in the rotated frame. A short, fully bold
  line at body size or larger is a subsection heading (stored in `subsection` of the following passages). A heading-only block directly followed by another subsection heading, once body text has begun (not on page-1 front matter), is joined to it (`HDAC Isoforms Class I HDACs`); a run-in `Label: text` line (`Key words: …`) is a paragraph, not a heading.
- Sections without a printed heading are inferred (`inferred: true`): text repeating the online abstract is `abstract`;
  text before the first IMRaD heading is `introduction`; headingless review body is `main_text`.
- OCR: pages with fewer than `ocr.min_page_chars` characters and an image are read with RapidOCR (offline). Provenance:
  `pages.jsonl` `ocr{engine,dpi,n_lines,mean_confidence,low_confidence_lines}`, `passages.jsonl` `ocr{engine,mean_confidence}`,
  `metadata.ocr_pages[]`, review item `ocr_used`. OCR numbers need PDF verification.

Passages: optional `subsection`, `source` (`"main"` if absent, else a supplement id), `sidebar`, `lay` (CH-08, main PDF
passages read after G5; absent on older ones and on supplements): one entry per paragraph block in text order,
`[n_chars, size, y0, y1]` = the length of the block's text, the character-weighted mean span size in pt (0.01), and the
block's top and bottom as a share of the page height (2 dp). A passage that holds several blocks lists them all; a passage
cut by `_split_long` keeps the blocks its text overlaps (lengths clipped). `n_chars` is counted before line-break hyphens
are resolved, so readers scale the cumulative lengths to `len(text)` (`sentences.layout_slice`).

### 2.1 `metadata.json` additions

`extraction_version`, `citation_style` (`numeric|superscript|author_year|mixed|none|unknown`),
`tier1{built_at, from: "pdf"|"passages_only", sentences, references, figures, tables, cited_by_links, citation_counts}`,
`supplements[{supp_id, sha256, original_filename, kind: pdf|xlsx|csv|other, status: extracted|failed|unsupported}]`.
`from: "passages_only"` means the PDF was unreachable: sentences/references were rebuilt from passages and figures
and tables are empty.

### 2.2 `sentences.jsonl` (frozen)

One object per line, reading order.

```json
{"sentence_id": "<pid>:S0142", "seq": 142, "passage_id": "<pid>:0023", "source": "main", "page": 7,
 "section": "results", "subsection": "Mitochondrial DNA integrity", "kind": "text",
 "text": "…", "start": 0, "end": 99,
 "citations": [{"marker": "[12,15]", "ref_nums": [12, 15], "style": "numeric", "confidence": "high", "start": 90, "end": 97}],
 "mentions": ["<pid>:T1"]}
```

- `section` is one of `front_matter, abstract, introduction, methods, results, results_discussion, discussion, conclusion,
  main_text, figure_legends, supplementary, acknowledgements, funding, author_contributions, conflicts, data_availability,
  references, notes`. `page` is the 1-based PDF page. `kind`: `text | caption | table_row` (a `table_row` has null `passage_id/start/end`
  and an `object_id`).
- Optional `continues: "<next sentence_id>"`: a body sentence cut by a page break, column or figure; both parts are one sentence
  for V1/V8. `context_basis: "source_continuation"` marks links added by `sentences.link_page_breaks`; a capitalised row continues a head only when the head has 6+ words or ends in a function word, a `/` start or `-` end always continues, a one-word tail ("SHL.", not a capitalised word: "Antibodies." is a run-in heading) in another passage links, a lower-case row of 6+ words that ends a sentence is not table text for the linker (the read-time flag is unchanged), and a verb-bearing sentence is still body when it is number-dense (mostly lower-case words) or only mentions University/Institute/Department with at least half of its words lower-case (as a head, or as a target that reads as the rest of the head). Optional `in_table: true`:
  running text that is really a table: (a) at least 5 significant numbers, 70 % in rows of a table on the same page, no
  citations; or (b) a text table (species/methods/reference lists): 8+ words, 85 % of them in the cells of a table on the same
  page (citations allowed, they sit inside the cells); or (c) the sentence holds 80 % of the cell words of a table with 12+
  cell words on the same page; or (d) it is the text right after the table's caption or `table_row` rows on that page (a
  0-row table too; a caption on a page without a table record counts only for a table of an earlier page) - when the
  cells are known at least half of its words must be cells - or, after such a flagged row, text that reads like table
  text (`table_text_suspect`); (c) and (d) with known cells skip a sentence whose words after its last cell word hold a
  verb (a body sentence glued after the table); evidence must cite `table_row` sentences instead.
- `citations[]`: `marker` is exactly `text[start:end]`; `confidence: "low"` = ambiguous, `ref_nums` then lists all candidates
  (flagged, never guessed). None in `front_matter`, `references`, supplements. Forms: `[12]`, `[12,15]`, `[12–15]`, `(12)` (only in
  papers using that style), `^12`, author-year `(Marlow et al. 2010)`, `;` lists, `2010a`, narrative `Marlow et al. (2010)`.
- `mentions`: figure/table IDs named in the sentence ("Fig. 3B" to `F3`, "Supplementary Fig. S1" to `FS1`).
- Optional `lay_size` (pt, 2 dp), `lay_ref` (pt, 2 dp) and `lay_y` (`[top, bottom]`, share of page height) (CH-08; only on rows
  built from a passage that has `lay`, so never on `table_row`s or on rows stored before the G5 re-extraction). `lay_size` = the
  largest block size among the blocks that cover at least 20 % of the sentence's characters (`sentences.LAYOUT_SHARE`), the
  character-weighted mean of its blocks when none does (a table row of many cells). `lay_ref` = the character-weighted mean size
  of the blocks inside the modal 0.5 pt bin over the `text` passages of the sentence's own section; `front_matter` uses the same
  over the body sections together (`abstract|introduction|methods|results|results_discussion|discussion|conclusion|main_text`); a section
  with no text block falls back to that body reference (no body: its own). A row with both keys is a layout interruption
  (`sentences._layout_interruption`) when its kind/flags say so, it is `in_table`, or `lay_size / lay_ref <
  sentences.LAYOUT_SIZE_RATIO` (0.92, unrounded), or it is a page-1 footer line: `page == 1`, `lay_y` top > `LAYOUT_FOOTER_TOP`
  (0.85) and `lay_size / lay_ref > LAYOUT_FOOTER_RATIO` (1.03; DECISIONS 2026-10-20); no word, length, digit or `table_text_suspect` rule applies to it. A row without
  the keys keeps the old text rule (licence/affiliation words, over 600 characters, digit-dense, `table_text_suspect`). The verdict
  only decides which rows `link_page_breaks` skips, ignores or accepts as the two ends of a `continues` link.
- Splitting never breaks at `et al.`, `Fig.`, `e.g.`, `i.e.`, `vs.`, `cf.`, initials, decimals, or before a lower-case word.

Invariants: I1 `passage.text[start:end] == sentence.text` and sentences cover the passage; I2 `seq` is 1..n without gaps; I3 every
`ref_nums` of a `high` citation exists in `references.jsonl`; I4 sentences in `references` have no citations.

Read-time layer (`sentences.cached_linked_rows`, memory only, never written): adds legacy page-break `continues` links, ignores
stored links whose source/target is a table/layout row, licence/footer/affiliation blob or table-like text, may link a same-passage
word cut, and adds UI-only `source_glyph_suspect`, `table_text_suspect`, `display_text`. Cache key = path + `(mtime_ns, size)`.
Search treats `^◦C`/`◦C` as `°C`. A structured chunk never ends between a row and its `continues` target.

### 2.3 `references.jsonl` (frozen)

`ref_id, num` (null in author-year lists)`, page, raw` (always present)`, first_author, year, year_suffix, title, journal, doi, pmid,
parse_confidence, resolved_paper_id, ghost_id`. A field is null rather than guessed; numbers must continue 1, 2, 3… (gap up to 2
marks `low`). `resolved_paper_id` = the cited paper is in the library (same DOI/PMID, or same first-author surname, year ±1 and at
least 85 % of title words in `raw`); set backwards when the cited paper is added later (`references.link_back`) and cleared on
deletion (`unlink`). `ghost_id` is set for unresolved references with DOI or title+year. Online lookup is off (not implemented).
Unnumbered lists (`references._unnumbered`): an entry starts at an author line (accented surnames included, matched on NFC text) after a line that ended an
entry; a line ending in a DOI (also one wrapped over two lines) ends an entry, and a lone `Surname,` line after a DOI starts one
(initials on the next line); an entry with no year whose surname has no comma (lowercase particles and `Smith J,` allowed; a journal tail such as `Indian J. Exp. Biol. 43, 740–755.`) is joined to the entry before it. `ref_id` is
`R` + the printed number, else the 1-based position, so one split shifts every later id: `renumber.carry_over` follows the ids (section 2.7).

### 2.4 `figures.jsonl`, `tables.jsonl`

Figure: `figure_id, label ("Figure 3"), source, page, bbox [x0,y0,x1,y1] or null, crop ("crops/F3.jpg" or null),
caption_sentence_ids[], mentioned_in[]`. Table adds `header[], rows[][], footnotes, extraction (pymupdf_tables|word_positions|
text_fallback|csv|xlsx), quality (ok|uncertain)`. An object comes from a caption; a table is kept only when its caption is on the
same page. Every table row is also a `table_row` sentence after the caption sentences, cells joined by ` | `; `ok` tables write
`<column header>: <value>`, `uncertain` tables write cells without column names. A ruled table whose row 0 has a digit and no words sit above it takes row 0 as `header`; a table printed sideways
(most of its text lines not horizontal: `objects._sideways`) is `uncertain` and row 0 is not promoted. CH-06 counts an empty-first-cell row as a split only when
fewer than 4 cells (or at most half) are filled; a row with more is a new record under a merged first column. If rebuilt numbering changes, saved analysis
references are carried over by `renumber.carry_over` (section 2.7) only when old and new sentences match by exact text, page, section, source and kind. The crop (JPEG
about 110 dpi, bbox plus caption) survives PDF archival.

### 2.5 Supplements

`kb.py attach-supplement <paper_id> <file>` copies (never moves) the file to `sources/active/<paper_id>/supp/`, verifies SHA-256,
records it in `source_files` (`role: supplement`) and `metadata.supplements`, rebuilds Tier 1. PDF supplements add passages with
`source: "<pid>:X1"`, section `supplementary`, objects `FS/TS`; CSV gives one table, XLSX/XLSM one table per sheet. In a batch, files
named `supp`, `SI`, `S1`…, `mmc<n>`, `SDC/SDT<n>` are matched by DOI, then unique parent filename; only a unique match is attached.

### 2.6 SQLite (`library.db`, rebuildable)

Tables: `papers, source_files, paper_vaults, sections, passages, passages_fts, claims (v1 legacy), paper_terms,
concept_links, review_items, batches, sentences, sentences_fts, references_list, citations, figures_tables, evidence,
evidence_fts`; `source_sentences_fts` for non-PDF sources. `db.PAPER_TABLES` lists every per-paper table.
`db.reload_paper` refreshes one paper (load plus relink); `rebuild-db` loads all then links once.

### 2.7 Quality preflight and re-extraction

`quality-check` / MCP `quality_check` returns `{paper_id, sentences, ready_for_analysis, blockers, warnings, scope}`. Blockers:
replacement/private-use/control characters, unresolved Advent font codes, failed font scans, no readable text, low-confidence OCR.
Warnings: unavailable original, uncertain tables, OCR use. The CLI saves `analysis_quality.json`; the MCP call is read-only.
`reextract <paper_id>|--all` rebuilds Tier 1 from the PDF (no AI). With the PDF reachable, pages, sections and PASSAGES are read again
for every paper, analysed or not (passages of a supplement whose file is gone are kept, renumbered); previously analysed papers kept
their stored passages, which froze extractor fixes. No stored analysis cites passage IDs (v2 cites sentence IDs); passage IDs of
retained objects follow by exact text (`renumber.id_map(..., "passage_id")`, unmapped dropped). The old pages/passages/sections go to
`tier1_history/`. Without the PDF only sentences are rebuilt from the stored passages.
Derived data after a re-extraction: the database (passages, sentences, FTS) is reloaded by `build`; run
`kb.py steps structure` (structure.json + media manifest), `kb.py steps vectors` (re-embeds only papers whose passage IDs/texts
changed; fingerprint over passage ids + texts + units) and `kb.py notes`. Dry-run prints `change_summary` per paper:
`section_changes {"old -> new": n}`, `text_removed`, `text_added`, `table_row [old, new]`, `in_table [old, new]`, and
`text_preservation {removed, contiguous, words_present_not_contiguous, lost, lost_total, glued_subheadings_split}`: each old
sentence without an exact counterpart is searched in the new passages, sentences (with `subsection`), section headings and table
cells, ignoring case, whitespace, hyphenation and punctuation; `lost_total` must be 0 before a real run. Extractor rules added with
it: a caption block (or the lower-case continuation of an unfinished caption on the same page) met inside the reference list is
labelled `supplementary` (label `S<n>`) or `figure_legends`, never `references`; "Supplementary Figure 1" labels a caption, it
is not a section heading; a copyright/licence line (`LICENCE_NOTE`) on the first pages is `front_matter`.
If a real re-extraction fails after files were replaced, the error names the `tier1_history/<uuid>/` folder holding exact copies of the previous files (the database is rolled back); copy them back and run `kb.py rebuild-db`. The preview (`--dry-run`) is read-only and prints the old-to-new sentence-ID
map (identical text) and what would need AI re-analysis. Do papers one at a time, with a backup and the researcher's
approval. Invariant `E1` fails for a paper whose IDs changed under stored evidence: re-analyse it.

Carry-over (`scripts/kbsys/renumber.py`; `tier1.build` calls `renumber.snapshot` before and `renumber.carry_over` after writing the
new sentences; the previous files go to `tier1_history/<uuid>/`, `carry_over/` inside it for readings and node cards).
`id_map` = old full ID -> new full ID for each old sentence with an identical (source, page, section, kind, text) key in the new
list. Then, for IDs that changed:

- rewritten (short `S0012` and full IDs): `analysis.json`, `analysis.draft.json`, `analysis_state.json`, `analysis_rejected.json`,
  `analysis_v2.draft.json`, `analysis_v2.parts.json`, `review_policy.json`, `evidence.jsonl` (sentence IDs; reference ids below), `paper_overview.json`,
  `section_profile.json`, `protocol_profile.json`, `figure_readings/*.json`;
- reference ids (`R012`), `evidence.jsonl` only: `renumber.ref_id_map(old_refs, new_refs)` pairs each old entry with the new entry of
  identical `raw` text (same text twice: in order; a numbered entry whose text was read differently keeps its number). `cited_refs` (short or
  full) and the `[R012]` / `[R012, R013]` / `[R012–R015]` markers of `statement` are rewritten in ONE substitution (no cascade); free text
  such as `Biol. 14, R115` is not touched. A unit citing an old entry with no new counterpart (split or merged) keeps what it wrote and gets
  a V6 flag ("a cited reference no longer exists"; sticky like V14: `recheck` keeps it) and status `flagged`: it never follows the number to another entry. The old
  `references.jsonl` is read before it is replaced (`tier1.build`). Other holders need no rewrite: `sentences.jsonl` citations, `references.jsonl`
  and `library.db` are regenerated by the rebuild, `ghosts.jsonl` `cited_by` by `reextract`'s `ghosts.rebuild` (or `rebuild-ghosts`); vault
  notes keep `[R###]` only inside `kb:auto` blocks (regenerated); `analysis_runner*.json`, `analysis_rejected.json` and `*_history/` are
  history and are left;
- cross-paper, only this paper's FULL IDs (whole-string values and dict keys): `knowledge_store/node_cards/*/*.json`, `relations.jsonl`
  (which holds none today);
- not rewritten: `structured_tasks/` (immutable checkpoints, superseded by the next `prepare`), `ai_replies/`, `analysis_runner*.json`,
  `_units.json`, `_must_cover.json`, `analysis_v2.submit.json` (none read back), `structure.json`, `media_manifest.json` (derived; run
  `steps structure`), IDs written inside free-text `reason` strings, and DB `review_items`.

Freshness after a rebuild (an unmapped ID is never rewritten or guessed):

| Artifact | Stale when | After a rebuild |
|---|---|---|
| Evidence unit | no stored hash; `source_drift`: a cited ID is missing, or the stored `quote` (gap markers ignored) or `sections` differ from the current cited sentences | renumbered units stay `ok`; units citing an unmapped ID get V14 and `flagged` |
| Overview / WH / protocol profile | `source_sha256` (whole `sentences.jsonl`) or `source_revision` differs, plus the content checks (`copied=True` quotes, `overview_sha256`, protocol coverage) | if current BEFORE the rebuild (an overview also needs its `overview_sha256` to match) and every cited ID is mapped: `source_sha256`, `source_revision` (and `overview_sha256`) are re-stamped; otherwise the old hash stays and it reads stale |
| Figure reading | `crop_sha256` differs, or `cited_text_sha256` (SHA-256 of the texts of the object's caption and mention sentences, IDs excluded) differs; a reading without the field falls back to `source_revision` (whole files) | a reading current before the rebuild whose cited IDs all survive gets `cited_text_sha256`; later renumbering or metadata/manifest churn no longer stales it |
| Node card | any cited quote's `text`, `section` or `page` differs from the live sentence (`node_cards._card_currency`; the broad `source_revision` is for tasks only) | renumbered full IDs are rewritten (keys and `sentence_id` of `quotes`, `sentence_ids` lists); a card citing an unmapped ID reads stale |

`reextract --dry-run` simulates the real run (stored passages for an analysed paper, glyph repairs, table-row insertion) and prints per paper:
`renumbered_sentences`, `unmapped_old_sentence_ids`, `mapping` (changed IDs only), `stale_evidence_units` / `stale_analysis_count`
(units citing an UNMAPPED ID only), `needs_reanalysis {evidence_units, profiles, figure_readings, node_cards, relations}` and
`needs_reanalysis_detail` (names, plus `methods_added`: new Methods sentences, which make a protocol profile need a fix; a protocol whose cited sentences change source order also counts). Readings are
judged by their recorded caption/mention IDs (a lower bound). `metadata.tier1.sentence_ids = {renumbered, unmapped}` after a build; `metadata.tier1.reference_ids = {renumbered, unmapped}` (old
reference entries under another id / with no counterpart) next to it, and the dry run shows `reference_ids` (+ `old_entries`, `new_entries`,
`evidence_units_flagged`).

## 3. Tier 2: evidence units and validation

Code: `evidence.py`. Entry points: MCP `submit_analysis`, `add_units/finish_analysis`, CLI `submit-analysis`, runner `analyze`.
Rules for the analyst: `docs/AI_ANALYSIS_GUIDE.md`.

### 3.1 Submission

```json
{"paper_id": "…", "analyst": "model/name", "classification": "CORE|CONNECTED|POTENTIAL_BRIDGE|UNCERTAIN|OUT_OF_SCOPE",
 "classification_reason": "…", "evidence_type": "primary_research|review|systematic_review|meta_analysis|protocol|case_report|commentary|other",
 "research_question": {"text": "…", "sentence_ids": ["S0021"]}, "topics": ["…"],
 "units": [ … ], "skipped": [{"sentence_id": "S0144", "reason": "…"}], "summary": [{"text": "…", "units": [1, 4]}]}
```

Sentence IDs may be short or full; references `R004`, `4`, `R4` or full. A submission is complete and replaces the
previous one. `research_question` and each `summary` sentence are grounded like units (summary `units` are 1-based
positions of accepted units). A `research_question` sentence is flagged (V1) outside abstract/introduction/title; `main_text`
also counts for a paper whose body has no introduction/methods/results/results_discussion heading (a review). For weak models the MCP tools `add_units` (at most ~10 per call), `skip_sentences`,
`remove_units`, `draft_status`, `finish_analysis` build it in `analysis_v2.parts.json`; only `finish_analysis` runs the
full check and counts as an attempt.

### 3.2 Evidence unit (as submitted)

```json
{"unit_type": "result|method_detail|interpretation|hypothesis|conclusion|limitation|cited_statement|background",
 "statement": "English sentence.", "sentence_ids": ["S0142"],
 "model": {"species": "", "cell_type": "", "stage": "", "origin": "", "setting": "in vitro|in vivo|…"},
 "intervention": {"agent": "", "dose": "", "timing": "", "duration": "", "protocol": "", "comparator": ""},
 "outcome": {"measure": "", "method": "", "direction": "increase|decrease|no_change|mixed|not_applicable",
             "values": [""], "value_source": "text|table|figure_only", "n": "", "p": "", "source_objects": ["T1"]},
 "quality": {"design": "", "replicates": "", "is_primary": true},
 "hedging": "none|hedged", "hedged_sentence_ids": ["S0143"], "cited_refs": ["R004"],
 "terms": {"topics": [], "concepts": [], "methods": [], "species": [], "molecules": [], "cell_types": []}}
```

`hedging` says whether the sentence(s) that carry the claim are hedged; "no significant difference" / `p > 0.05` is
`non_significance`, not a hedge. `hedged_sentence_ids` (optional, a subset of `sentence_ids`, default: all cited
sentences) names those sentence(s); an id outside `sentence_ids` is a V1 error. The key is stored only when the unit gave
it (the code never stores its own guess). V5 stronger-wording checks still read every cited sentence and every hedge category.

`filled_by` (EV-17, optional, set by code only, stored only when code filled a field): `{"direction": {"by": "rule", "source": "S0012"}, "species": {"by": "rule", "source": "paper:abstract"}}`. For a `result` unit whose `outcome.direction` / `model.species` is empty,
`evidence_fill.fill_fields` fills it from the cited sentences (direction: exactly one of `evidence.directions`, captions skipped; not filled when the text
is negated, only gives a threshold ("more than 50") or `high`/`low`/`loss`/`peak`, or the unit's statement states another direction; species: exactly
one species of `SPECIES_SYNONYMS` (not a reagent host such as "anti-mouse", and not when the title/abstract name another species: a graft), else the paper title/abstract). `source` is the sentence id or `paper:title` / `paper:abstract`; a value the
analyst gave is never replaced; `validate` lists each fill in `normalized_terms` (`field` `outcome.direction` / `model.species`, `because` `rule: S0012`).
Stored units get the same fill with `kb.py fill-evidence-fields --apply` (G5). The key is not hashed by `crosscheck._fingerprint`.
Unknown is `null`. 1–6 sentence IDs. Optional unknown fields and empty optional objects may be omitted; validator
defaults are authoritative. The runner moves well-formed top-level lists named `concepts, methods, molecules,
cell_types, species, topics` into `terms` before validation (it never invents terms). A `cited_statement` is written as
attribution. For `result` and `method_detail`, at least one of `concepts, methods, molecules, cell_types, species` must be
non-empty (V9); during `recheck-analysis` of stored units this is a warning only.

### 3.3 Validator codes (checked by code, no AI)

| Code | Rule | Effect |
|---|---|---|
| V1 | valid `unit_type`; English statement (at least 10 chars, no Vietnamese letters); 1–6 existing sentence IDs of this paper forming one place: same passage, or within 5 body sentences (captions, table rows and `continues` parts do not count), or all rows of one table; never the reference list | reject |
| V2 | every number in `statement/values/n/p` occurs in the cited sentences with citation markers blanked (a cited reference's year is attribution). Signs are kept (a dropped minus is a warning). Numbers in `dose/timing/duration/model` occur anywhere in the paper body. Ranges and names like CD105/SSEA-3/H2O2 are excluded | reject / warning |
| V3 | species, cell type, agent, measure, method and terms appear in the paper (stem match, species synonyms, synonym groups, MeSH entry terms, an acronym of 3-6 letters and its expansion (3+ words, no stop word, each 3+ letters) in the paper, chi-square/chi^2, an -ed/-ing form of the same root (filtration ~ filtered), or word-by-word) | flag |
| V4 | `result` in a review paper, or whose cited text sentences are none in results/results_discussion/supplementary | flag |
| V5 | source hedged (may, might, could, suggest, possibly, likely, potential, propose; association words) but the statement is definitive/causal (causes, induces, leads to, demonstrates, due to…) or drops a modal hedge (interpretation/hypothesis/conclusion/background/cited_statement; `seem*` and `potential` in the statement keep it). Not causal wording: a noun in -ion, a participle after to/with/of (or after and/comma in a list that a to/with opened), a hyphenated -ed/-ing compound (not up-/down-), a negated verb (not "not only"); `potential` keeps the hedge only as the adjective ("a potential target", not "membrane potential"); `hedging: none` is auto-fixed when the claim sentence(s) (`hedged_sentence_ids`, default all cited) hold a hedge other than non-significance | flag / auto-fix |
| V6 | `result` citing a sentence that has citations; `cited_statement` without `cited_refs`, or refs not cited there (reject); no attribution wording (flag); a cited reference that no longer exists after a re-extraction (set by `renumber.carry_over`, kept by `recheck`; flag) | reject / flag |
| V7 | `direction` contradicts the wording (location words before anatomical nouns ignored; `loss`/`lost` ignored in a text that says retained/maintained/preserved/no loss, unless a % / p-value / "significant" / fold is within 40 characters; `equally X` is no change (X not an -ed/-ing word); mixed wording warns); EV-18 "bundled finding": a `result` with one direction whose statement, split at but / while / whereas (`evidence.CONTRAST`), turns from a direction set to a disjoint one (`evidence.directions`): split it into one unit per finding | flag |
| V8 | coverage: every must-cover sentence is in an accepted unit or in `skipped` with a reason. With `coverage_policy: full_sections` (demo3 default) every body sentence of Introduction, Methods, Results, Results and Discussion, Discussion, Conclusion, main text and supplementary needs a unit or reason. Abstract is reference; captions/table rows stay with objects; boilerplate, references, front/back matter never count. A `continues` chain is one sentence | sent back (`coverage_missing`) |
| V9 | terms normalised to one canonical English name and one type (`cell_type > molecule > species > method > concept > topic`); new `result/method_detail` need at least one scientific term list | auto-fix / reject |
| V10 | `classification`, `classification_reason`, `evidence_type`, `analyst`, non-empty `units`, matching `paper_id` | nothing stored |
| V11 | `result/method_detail` cite data (table row, or non-`in_table` text in results/results_discussion/supplementary/methods/abstract/main_text), not a caption or title | reject |
| V12 | `value_source: figure_only` with `values/n/p`; `table` without a table row | reject |
| V13 | second-model check (`crosscheck-analysis`): statement vs verbatim quote, `supported|partly|not_supported`; the last two flag the unit and create an `analysis_flags` item. `analysis_v13.json` is resumable and applies only while its fingerprint matches | flag |
| V14 | source drift: a cited sentence is missing, or the stored `quote`/`sections` no longer equal what the cited sentences say now (set by `renumber.carry_over` after a re-extraction and by `recheck`; sticky like V13 until the unit is re-analysed). The unit stays, status `flagged`; the cited ID is never rewritten to a guess | flag |
| V15 | added entity (EV-19): a name in the statement (acronym, mixed-case or letter+digit name, capitalised word not opening a sentence) that the cited sentences do not contain. `evidence.added_entities` is the one rule (the conform check EV-19 calls it): a word is held when it is in the cited text (also inside a longer name, a plural, a Greek letter spelled out), a registered synonym of it (`config/synonyms.yaml`), an author of a cited reference, a PDF split (2-3 consecutive cited tokens joined: `HDAC`+`4`), one number of a shared-prefix list (`HDAC 4, 5, 6`), a Latin binomial the text abbreviates (`A. thaliana`), a word of an abbreviation the paper itself defines (`structure.json` `abbreviations`, read into `PaperContext.abbreviations`; paper-local) whose short form or definition is cited, or an acronym and its initials (`MEM` of 3-6 letters for `minimum essential medium` in the cited text, a cited `MPI` for the capitalised words `Max Planck Institute` of one clause; `CTRL` for `Control(s)`: same first and last letter). Sentence ids and `Rnnn` markers are no entity. One flag per unit listing up to 5 words: cite the sentence that names it (up to 6 ids within 5 sentences) or remove it; a referent taken from a neighbouring sentence ("these cells" -> "Muse cells") must be cited too (D-PENDING-10) | flag |
| FG | figure reading warning (EV-20): the unit cites a sentence flagged by `figure_reading.unit_warnings(readings, sentences)` (the claim sentence of an `inconsistent` caption check, or a sentence of that reading's object; a `crop_wrong` reading never warns). The message is `<label>: <WARNING_LABEL>…`; recomputed at every `validate`/`recheck` (also for a unit that `recheck` retains although it is rejected now), so it ends when the reading's warning ends | flag |

Hedge words: one vocabulary, `config/hedge_lexicon.yaml` (format unchanged: `categories` + `entries` of `phrase`|`regex`, `category`, `strength`), loaded and cached by `scripts/kbsys/hedges.py`; a missing or malformed file raises (no fallback list). `structure.uncertainty`, the answer checker and V5 (`evidence.HEDGE` = any category, `MODAL_HEDGE` = modal/appearance/hypothesis/qualification/negated_hedge, `ASSOCIATION_WORD` = association) all read it. Association hits are dropped for `X-associated` compounds and organisation names (`Association for/of ...`, `<Capitalised> Association`).
`p < 0.05` is not a hedge; `p > 0.05` is `non_significance` (`hedges.NON_HEDGE`: not a hedge for the stored `hedging` field, EV-06; `evidence.CLAIM_HEDGE` = every category except it, `HEDGE` stays the full set for V5).

### 3.4 Result, retries, storage

`{ok, status: needs_fix|done|review, attempt, max_attempts, accepted_units, flagged_units, rejected_units, errors[{unit_index,
code, severity, message}], flags[], coverage_missing[], normalized_terms[], next_step}`. `unit_index` is 1-based (0 = whole
submission). `done`: no errors and nothing missing. `needs_fix`: errors or missing coverage, attempts left. `review`: attempts
used up (`analysis.max_attempts`, default 3).
- Only V10 errors block storing; otherwise accepted units replace the stored ones (old `analysis.json`/`evidence.jsonl` go to
  `analysis_history/`).
- `evidence.jsonl` line = submitted fields (terms normalised) plus `unit_id`, `seq`, `quote` (copied by code: sentence texts in
  order, `[…]` between non-adjacent ones, but not between a sentence and its `continues` part), `pages`, `sections`, `status` (`ok|flagged|human_confirmed`), `flags`, `analyst`,
  `attempt`, `created_at`, optional `aliases`, `field_provenance`.
- `analysis.json` (v2): `analyst, analysed_at, status, attempt, classification, classification_reason, evidence_type,
  research_question, summary, n_units, n_flagged, coverage_missing, skipped`, the six term lists, `claims: []`.
  `analysis_state.json`: `attempt, max_attempts, status, updated_at, analyst`, last errors/flags/coverage. SQLite `evidence`
  (+ `evidence_fts`) and `paper_terms` mirror them.
- Review items: `analysis_flags`, `analysis_incomplete`, `analysis_duplicates`, `classification` (UNCERTAIN/OUT_OF_SCOPE). In
  demo3 only `topic_relevance` items are shown as flags (section 9).
- Invariant E1: stored `quote` equals the quote rebuilt from current sentences; unit IDs are `E0001…` in order.
- `evidence.export_submission(id)` rebuilds the stored analysis as a complete submission (what `recheck` validates);
  `kb.py export-analysis <id> [--out f]` writes it. `evidence.submit` refreshes concept notes only for
  `concepts.relink_paper(...)["affected_concepts"]` (the paper's old and new links plus new/changed/dropped concepts) and their
  parents (`concept_notes.refresh(..., concept_ids=…)`); orphan retirement is left to `kb.py notes`.
- `recheck-analysis` reruns validation (legacy-terms warnings), never removes a stored unit, reports newly uncovered sentences
  as `analysis_incomplete`. `reset-analysis` (researcher only) moves analysis files to `analysis_history/removed_<time>/`.
- `analysis_origin` on hits: `heuristic_draft` when the analyst starts with `find.HEURISTIC_ANALYSTS` (`hermes/kr-auto`, rule
  scripts, shown as "nháp tự động"), else `analysis`. The label never rewrites data.

### 3.5 Automatic runner (`analysis_runner`, `analysis_transport`)

`kb.py analyze` and Library "analyze" send the guide plus numbered sentences to `ai.tasks.analysis`. The runner keeps
accepted units, asks only for repairs (`{replace_units, add_units, fields, skipped}` merged locally; indices never
shift), may answer `{need_context: "all"|[ids]}`, and is bounded at `max_attempts + 2` calls. Checkpoint
`analysis_runner.json` (fingerprint of prompt/source/provider/model) is written atomically; a mismatch is archived.
`analysis_usage.jsonl` is append-only (provider usage, never estimated). The writer lock is taken only around store
writes. Compact reading (`read_paper format=compact`) keeps every sentence, ID and flag.

## 4. Search and answers

### 4.1 `find.search`

Query to term groups (stop words and AND/OR/NOT dropped, `"phrase"` kept, `config/synonyms.yaml` and `config/word_variants.yaml` (query side only: the concept registry reads only synonyms.yaml) groups, MeSH concept synonyms,
at most 10 variants). FTS5 over `evidence_fts` and `sentences_fts` (references, front matter, acknowledgements, funding,
contributions, conflicts, data availability, publisher notes excluded). All groups required (`and`); fewer than 20 rows then any group (`or`). A
sentence inside an evidence unit is returned as that unit. In `or` mode the rows that match every group (the `and` run) are kept
whatever `cap` is (rows that match only some groups still pass the `cap` in bm25 order), and the keyword rank is the number of groups matched first, then `-bm25 × weights` (RT-02). In `and` mode keyword rank = `-bm25 × type/section/kind weights × review and flag
penalties × group share`; meaning rank = cosine; fused by reciprocal rank `1000 × [w/(60+rank_kw) + 1/(60+rank_vec)]`. Filters:
`unit_types, sections, species, evidence (primary|review), year_from, year_to, paper_id`; scope one vault (default: the current one; a
vault that is not in `config.research_vaults()` raises `ValueError`, RT-06) or `all_vaults=True`. The coverage insertion of exact
identity-term sentences changes the whole ordering once, then `offset`/`limit` slice it (stable paging for one `limit`, RT-08). Hit:
`kind (unit|sentence), id, paper_id, unit_type, label, statement, text (verbatim), sentence_ids, pages, sections, status, hedging,
species, cell_type, agent, measure, direction, value_source, review_paper, analysis_origin, matched_terms, score, via, cos,
cited_refs[]`. `find.effective_evidence_type(row)` lets source publication type Review override generated primary labels.
Vietnamese questions need AI translation to English queries or the meaning index.
Look-alike symbols (CHR-04) and NFC (CHR-01): the query is NFC-normalised (`find.parse_detail`, and `sentences.glyph_variants` for every path) and each term is expanded by `sentences.glyph_variants` into every spelling (`°C ˚C ◦C ^◦C ºC ℃`, `Δ ∆`) for the FTS expression (`find.fts_expression`, `search.parse_query` and so the MCP `search_sentences`, `sources_misc.search_sources`, neural note search), the highlight terms and the regex patterns; the sentence text is folded with `sentences.fold_search_glyphs` (to `°C`, `Δ`) only for the comparison. Spelling (U6d, DECISIONS 2026-10-24): `sentences.spelling_variants` (inside `glyph_variants`) adds the whole-term all-American and all-British spelling (`-is-/-iz-` and `-ys-/-yz-` before e, es, ed, ing, er, ers, ation(s) at a word end after 3 / 2 letters; `haem/hem` (`haem` only before a letter), `aemi/emi`, `oedem/edem`, `oestr/estr`, `anaesth/anesth`, `paediatr/pediatr`, `foet/fet`) and, for a Greek letter name or symbol (alpha beta gamma delta kappa mu; `µ` U+00B5 is `μ`) attached to a token by a hyphen or glued (`gamma-H2A.X`, `γH2A.X`, `TNF-α`, `IL-1β`), the name and the lower-case symbol each with a hyphen and glued; a lone Greek word, a glued mu (units `µM`) and a trailing mu (`4-MU`) are not folded. In the stemmed regex patterns (`find._group_patterns`, `find._stem_pattern`) a word cut to `-is`/`-ys` must go on with a, e or i (a or e after `-ys`) when the typed word itself goes on so (`Swiss`, `abyss` stay plain prefixes), so `organised` does not match organisms and `hypothesised` not hypothesis; the highlight prefixes (`find.highlight_terms`) keep that next letter (`organise`). Stored text and the vectors are unchanged. FTS tokenizer (D-PENDING-14): `db.FTS_TOKENIZE` adds U+2206 `∆` and U+2103 `℃` as token characters (`passages_fts`, `sentences_fts`, `evidence_fts`, `source_sentences_fts`), so `∆G` is the token `∆g` like `ΔG` is `δg` (a plain `EF` no longer finds `∆EF`, as it never found `ΔEF`) and `37℃` is one token, `37 ℃` two. A table keeps the tokenizer it was created with: its CREATE statement in `sqlite_master` is the version marker, read by `db.fts_glyph_tokens(con, tables)`; only `db.rebuild` (`kb.py rebuild-db`, which drops and recreates the three tables) and a full `sources_misc.index_sources(con)` recreate them (G5). On an older index `∆`/`℃` are separators (`∆G` is `g`, `37 ℃` is `37`), so `sentences.fts_variants(value, glyph_tokens)` keeps the typed form and only the generated forms without `∆`/`℃`; on a current index it keeps every form plus the glued and spaced spelling of `℃` after a digit. Known limit: the separate terms of an unquoted `-196 °C` (find) do not match a glued `-196℃`.
Reply keys `best_groups_matched` (the most keyword groups any item of the whole result matches) and `not_found` (RT-05): `True` when
`best_groups_matched / len(groups)` is below `min_group_coverage` of clause RT-05 (0.75; exactly 0.75 is not below), then also
`not_found_message` ("Không tìm thấy trong vault hiện tại (not found in the current vault)", "…các vault đã tìm (searched vaults)" for
`all_vaults`, with `best/total` groups). The hits stay in the result. No flag for a query without groups or a Vietnamese query
(`needs_translation` says what to do). It says "not found in this vault", never "does not exist in the literature". The web `/api/search` merge of a Vietnamese query with its English queries carries the same keys: flagged only when every English query is flagged.

Meaning index (`vectors.py`): `multilingual-e5-small` ONNX int8 in `knowledge_store/embeddings/model/`, CPU, at most 4 threads, 256
tokens; per-paper `embeddings/papers/<pid>.npz` (`ids, kinds, vecs int8, fingerprint`) plus global `index_<time_ns>.npy` +
`index.json`; all cache (deleting `embeddings/` except `model/` only forces a rebuild). MeSH (`mesh.py`): NLM `desc<year>.gz`
streamed into `indexes/mesh.sqlite`.

### 4.2 Answers (`answer.answer`)

`answer.collect` runs every query, keeps the top `max_items // queries` items of each query, fills the rest of the slots by score
(RT-07), and a draft/flagged unit is replaced by the (at most 3) sentences that match the query groups, the first 3 if none does.

Views: `evidence4` (default), `google`, `report5`, `table` (`table` never calls AI). Speed `fast` (12 items) or `deep`
(30 items plus ±2 sentences around the best 8, at most 45). The AI sees only numbered items and returns JSON; code
validates and prints. Each item is shown to the AI as `[#n]` and cited in answer text as `[#n]` (or in `evidence`/`basis`
as the int `n`); a plain `[n]`, `[n-m]` or `[40-42]` is a literature reference and is never an item. An evidence/basis value
that is not an int (bool, float, NaN, Infinity) is rejected: A1 for `evidence`, A8 for `basis`; it never raises.

`evidence4` envelope: `answer{evidence[{item,id,quote,citation}], answer[{text,evidence,chunk_ids,check}],
sentences[], reasoning[{text,basis,chunk_ids,check}], missing[], scope_note}`, `evidence[]`, `markdown`. Quote and
citation `[Surname, year, trang X] (chunk_id)` are copied by code (missing author/year/page is `không rõ`). The four
headings are: `## Bằng chứng từ tài liệu`, `## Trả lời dựa trên bằng chứng`, `## [REASONING] Phần suy luận của AI`,
`## Không tìm thấy / không chắc chắn`. Reasoning `basis` (AN-15) is exactly one kind: item numbers and/or exact note paths from
the ticket, or the string `"ai_knowledge"` alone (match after strip + casefold; a duplicate counts as alone; it is never an item
number or a note, `answer._numeric_refs` drops it and `answer.has_ai_knowledge` tells). No basis, or `ai_knowledge` next to
anything, strikes A11; `ai_knowledge` in `evidence` of any other sentence (Part 2, report5/google, node cards) strikes A11 in
`check_sentence`. Each reasoning row also carries `basis_kind` (`items | notes | items_notes | ai_knowledge | none`) and
`basis_label` (`answer.AI_KNOWLEDGE_LABEL` = "kiến thức của AI (không phải bằng chứng trong thư viện)" for `ai_knowledge`, else
`suy luận từ <chunk ids / notes>`, empty for `none`); the markdown ends the row with `(suy luận từ ...)` or ` — <label>`, and the
MCP instructions and `config/answer_protocol.md` use the same label. A basis is never evidence. Scope note: absence from the vault is not absence from the literature.

`answer.answer` also returns `not_found` (every query's `find.search` was flagged, RT-05) and `not_found_message`; the message is added
to `missing[]` (and so to the markdown), items are kept. `queries_run[]` rows carry `groups, best_groups_matched, not_found,
not_found_message`. `answer.answer(..., snapshot=(items, runs))` skips retrieval and uses those items. `markdown` appends
`[bị gạch: <reasons>]` to every struck sentence.

MCP `ask_prepare` / `ask_check` (AN-13): `ask_prepare` returns `items_digest` = sha256 of `json.dumps([item ids in [#n] order])`, plus
`not_found` and `not_found_message`, `links` (NW-12, U5: `[{n, id, item_type, note_path, obsidian_url[, note_missing]}]`, `n` = the `[#n]` of the prompt; `item_type` `unit` for a unit hit, else as in §4.3; items, digest, kept snapshot and prompt unchanged), and keeps its items (this process, last 16 asks). `ask_check` has the OPTIONAL argument
`items_digest`; it checks against the kept items (no new retrieval, so `[#n]` cannot move), or retrieves again when nothing is kept,
and returns `items_digest`, `markdown` (reasons of struck sentences), `not_found`, `not_found_message`. A passed `items_digest` that differs
from the digest of the items checked is a tool error; without it nothing is refused.

Per-sentence checks (struck sentences stay visible, crossed out, with reason):

| Code | Rule | Effect |
|---|---|---|
| A1 | cites at least one existing item (answers: `[#n]` markers or int `evidence`; bool/float/Infinity values strike; node cards: the supplied `sentence_ids`, a bracketed `[95,96]` in a statement is a literature reference and is ignored) | struck |
| A2 | every number occurs in the cited verbatim text (source text only, never stored AI statements) or is a cited year/ref number, and it comes from a cited item that holds the digit-bearing name (`H4K12ac`, `sip150`) the sentence uses (no cited item holds one: any cited item); `Figure 6` / `Fig. 2B` / `Table S1` labels and `[#n]` / literature `[n]` markers are not numbers | struck |
| A3 | all cited items hedged but the sentence is definitive or causal; or definitive words no source states (hyphenated compounds such as `laser-induced` are not definitive words) | struck |
| A4 | all cited items are second-hand and the sentence has no attribution | struck |
| A5 | all cited items are review papers and the sentence does not say so | warning |
| A6 | tăng/giảm/không đổi against the `direction` of every cited unit that has one (an increase/decrease word over a no-change unit, a no-change phrase over an increasing/decreasing unit, both over a mixed set) | struck |
| A7 | any cited item has uncertainty (hedge lexicon) that the sentence dropped; in a multi-sentence unit it is read on the source sentence(s) with most words in common with the claim (ties, a following `However`/`This`... sentence, and a hedge flag the lexicon cannot place keep the whole-unit reading); a claim hedged only by a null result (`không đáng kể`, `non_significance`) keeps only a null-result source hedge | struck (`A7_setting` is a warning for who/where/when in WH answers) |
| A8 | reasoning `basis` cites a note not in the ticket allowlist | struck |
| A9 | Part 2 sentence starts with a bare verdict | warning |
| A11 | AN-15: a Part 3 sentence without exactly one kind of basis (none, or `ai_knowledge` mixed with items/notes), or `ai_knowledge` outside Part 3. With `ai_knowledge` alone the numbers must still be in the items' text (A2) | struck |
| A10 | an identifier of the sentence that no cited item holds: a digit-bearing name (`H4K12ac`, `sip150`; case, NFC, Greek letters as names, PDF `m^6A`, a superscript reference `H4K16ac^44`, `H4K16` holding `H4K16ac` are the same; a digit never extends a name, `CD3` is not `CD34`) or a condition term (vitrification / `thủy tinh hóa`, slow freezing); "held" = the item text, its `agent` / `species`, the paper title, its identifier terms. Slash forms are split first (`H3K4me2/3`, `H3K4me2/me3`, `H3K4me1/2/3`, `H3K27ac/me3` = separate marks, also for A2; `Table S1/2` is a label). Not applied to a limits sentence | struck |

`python scripts/web.py --check` runs the checks on planted sentences and must print "đạt".

`answer.check_sentence(..., scope="answer")`: the A2 narrowing (a number from a cited item that holds the digit-bearing name) and A10 run for the ask-answer path only (D-PENDING-8). `scope="card"` (`node_cards._check_statement`, A1-A7 reported) and `scope="note"` (`concept_notes` synthesis rows) pool the numbers of every cited item and skip A10: one statement merges several papers (NC-01). `node_cards.load` returns `card_check: "passed"` and no `card_status`; the node note carries the small line `CARD_CHECK_LINE` (N = distinct cited sentence ids), NC-04. `node_cards._check_statement` refuses a statement whose text holds a machine id (problem `machine_id`, VN-03; NC-01 reports it on stored cards: B, re-submit at G5); the note shows a sentence id of the statement's own sources as that sentence's citation label (`concept_notes._cite_bare_ids`), the card on disk is unchanged.

### 4.3 Neural (vault) retrieval

`cache/vault_index/<vault>.sqlite`: `notes(path, sha256, title, paper_id, text)`, `note_fts`, `edges(source,target,label,
status resolved|ambiguous|missing, etype, unit_id, sentence_ids)`, meta fingerprint, edge format `5`, `files` (the attachments).
RL-01 typing (DECISIONS 2026-10-20, D-PENDING-21), from the source note's frontmatter `type` and the property the link sits in. Only a
frontmatter property link and a link inside the note's single `kb:auto` block (`notes.AUTO_START`..`AUTO_END`) can get a knowledge type; a
body link outside that block always navigates (nav), for every note type; no block, several or an unclosed one = the whole body is outside.
A `result` note: `in_taxon` -> in_taxon, `uses_protocol`/`uses_technique` -> uses_method, `conflicts_with` -> conflicts_with (result ->
result), `source` -> nav, any other link to a node note (`notes.NODE_FOLDERS` but the `_Maps` topic maps, which only navigate) ->
observed_effect (observed_association when `knowledge_level: statistical_association`), but nav when `significance` is not significant
(`result_notes.NOT_SIGNIFICANT`, stripped, case-insensitive; evidence rule 4); a `claim` note: a link to a
node -> cited_background (`claim_type: review_statement`) or hypothesis_only (author_conclusion or unknown); a `conflict` note: a link to a
Results/ note -> conflicts_with and a `comparison` note -> same_measure_comparison, citing the target's `id`/`sentence_ids`; everything else
(missing/ambiguous links, papers, images, other notes) is nav. The RL-01 check reports a resolved result -> node edge typed nav unless
the result is not significant or its link text stands in the body outside the kb:auto block (`conform_graph._may_navigate`, its own reading of
the note text in the index). A knowledge edge carries `unit_id` and `sentence_ids` (JSON text) of the citing
note; search, graph and MCP output show only source, target, label, status. Links resolve as Obsidian does
(VN-05): the alias starts at `|` or `\|`, the heading/block part at `#`/`^`; `[[#H]]` (the note itself) gives no edge; a note is found by
name or partial path, an attachment by path or file name with its extension; code fences and inline code hold no links; an added or
removed note or attachment re-resolves the edges that name it. Real Markdown and wikilinks of one vault; files outside the vault
and out-of-vault symlinks are ignored; duplicate names are never guessed. Traversal default 0 hops (Deep: 2), maximum 2
hops, 50 nodes, 80 edges, reports truncation. Hop expansion follows knowledge edges only (etype not nav; navigation, unresolved and ambiguous links are neither followed nor
listed, NW-04); a hub (`neural._hubs`: a non-paper note with a resolved edge to more than RL-09 `hub_share_of_papers` of the paper
notes) is never added or expanded, and a hub that is itself a ranked match is returned unexpanded; `edges` lists only the knowledge
edges the expansion touched (RL-09).
Prepare (NW-01/02/03/08, U4): `neural.prepare(con, cfg, vault, question, hops=0, speed='fast', offset=None, session=None)` returns one page: `question, vault, speed, ticket, items, papers, media, vault_context, retrieval_basis, source_resolution, note_refs, rules, prompt` (U5: `source_fingerprints`, `profile_fingerprints`, `dependency_fingerprints` stay in the ticket file only, `neural._TICKET_ONLY`: `neural_check` re-reads them there), plus `needs_translation` and `english_query` (the English expansion the graph route searched with; RT-04: False when the vector index is ready) and paging `offset, limit` (the returned count), `total, has_more, next_offset` (null at the end). Items are ranked (U6c, `neural._ranked_sentence_ids`): reciprocal-rank fusion (k=60) of find's order (hits' matched sentences first, then the other sentences of their units) and the graph-FTS order weighted 0.5, so a sentence both routes find rises and graph-only sentences sit below find's best; a page adds items in that order until NW-01 `token_budget_<speed>` (prompt bytes / 4, `neural.est_tokens`, also used by `conform_network._size`) or `max_payload_kb` is reached (the most items whose payload fits: binary search, U5), at least one item per page; the ticket holds exactly the returned page. No `figures` key (use `media`; `answer_question` also returns `media` only). `source_resolution` = `{item_ids, truncated}`. An item has `display_text` only when it differs from `text`. `graph_context` = `{notes (at most 8 vault paths of the page's notes that cite the paper), note_count}` sits once per paper in `papers[pid]` (U5; NW-02 no repeated field), not per item. NW-12 (U5): every item has `item_type` (`caption` or `table_row` = the sentence kind, else `unit` when an accepted unit owns the sentence, else `sentence`), `note_path` (its paper note from the vault index, vault-relative, forward slashes) and `obsidian_url` = `obsidian://open?vault=<quote(vault, safe='')>&file=<quote(note_path, safe='')>` (`neural.paper_note_links`, `neural.obsidian_url`); no indexed note: both null plus `note_missing` (reason), never a guessed path. An item of a sentence cited by an accepted unit carries that unit's `unit_type, label, unit_id, hedging, analysis_origin` (owner chosen as in find, `find.owner_unit`); otherwise `unit_type` is null. `vault_context` is compact (`query, expanded_query, groups, hops, truncated, nodes[path, title, paper_id, depth, sha256, entity_kind, roles, summary, source_count, evidence_count, source_papers, note_numbers, item_type (`result/claim note` for entity_kind result or claim, else `node`), obsidian_url], edges`), only the best-ranked notes within NW-01 `context_share` of the token budget. `media` (`neural.slim_media`) = list of `{paper_id, note_path, obsidian_url, bundles, truncated}` (links as for items, once per group) for papers with figure, table, formula or special-symbol bundles linked to the page's items; a bundle carries `item_type` (= `kind`), `object_id, kind, label, table, crop_url, source_url, unresolved_sentence_ids, reading_inconsistent`; `caption/data/mentions/anchors` as sentence rows (`{sentence_id, source}` only when the sentence is already an item, else `sentence_id, text, page, section, subsection, source, uncertainty, cited_refs`); `source_locator` (source, status, page, bbox, object_bbox, region_kind, region_basis, region_completeness, locator_basis, sha256); `crop {status, sha256}`; `figure_reading {summary, status, stale, stale_reasons, analyst}` or null. The full bundle is `/api/research/media` (`media_reader`).
Decision notes (U8, AN-17, AN-19): `save_discussion(kind="decision")` writes `Discussions/<title>.md` create-only (same naming as other kinds). It needs `researcher_words`
(the researcher's verbatim words; must hold the whole word "chốt" or "chot" after NFC + casefold, else ValueError; a soft guard, the tool description and `answer_protocol.md` carry the rule) and,
optionally, `citations` (item ids = sentence ids of papers in `paper_vaults` for that vault, checked against `sentences`; an unknown or removed id = ValueError, nothing written; `kb.py discussion save --kind decision
--words W --cite ID... --earlier NOTE`; needs the store connection). Front matter: `type: decision`, `kb_kind: decision` (index kind `decision`), `tags: [decision]`, `created` (local ISO seconds), `saved_by`,
`evidence: false`, `cited_items`, `cited_papers`, `earlier` (vault-relative path, only when linked). Body: the words as a `[!quote]` blockquote, the AI text, `[[earlier]]`, then per citation a `[!cite]` callout
(item id, paper id, section, page, paper note link, quote copied from the store by code); no citation = the line `không có bằng chứng trong kho lúc chốt`, written by code. `earlier` must be a decision note directly in
`Discussions/` (a bare name or `Discussions/<name>`, matched case-insensitively after NFC, the real name is written; another folder = ValueError); the title is NFC; default = the newest earlier decision of the same file-name base (the first is `<title>.md`, then `<title> (n).md`); the earlier note is never modified.
`neural.decisions_for(con, vault, question, paper_ids)` (shared by `neural_prepare` and `ask_prepare`) returns the decision notes found by `search(vault, question, kind='decision')` (max 5; only type `decision` notes in
`Discussions/`) as `decisions[{label "ý chốt của bạn (<YYYY-MM-DD>)", title, date, note_path, obsidian_url, item_type "decision", evidence false, cited_items, earlier, text (note body, <= 1200 chars), newer_evidence}]`.
The payload key `decisions` exists only when there is a match (counted by the NW-01 byte cap; plain tickets do not store it); decisions are never `items` and never numbered. `newer_evidence` = papers among the answer's
papers (all ranked sentences for `neural_prepare`, the items for `ask_prepare`) with `papers.ingested_at` later than the decision's `created` (an unquoted YAML datetime is read as ISO; a date only = that day 23:59:59), not in `cited_papers`, not reported before. The reported ones are written to
`knowledge_store/cache/decision_flags.json` `{"<vault>/<note_path>": [paper_id, ...]}` (derived, deletable, not evidence) only by the MCP `neural_prepare` / `ask_prepare` dispatch after the answer is final (a refused session round (NW-10) writes nothing); direct `neural.prepare` / `answer_prompt` callers that do not show `decisions` (web, `answer_question`, `kb.py discussion pack`, benchmark, conform) never write it. The note is read as the index reads it (`utf-8-sig`, errors replaced).
Research session (NW-11, U6a): `session="new"` opens one, a session id adds a round; unknown, closed or other-vault id = tool error. `cache/neural_sessions/session-<32hex>.json` = `{session, vault, status: "open", created, last_used, rounds[{round, question, speed, hops, offset, next_offset (offset + items shown), new_items, item_numbers, fingerprint}], union[items in first-seen order, each with n; item n = union[n-1]], papers, note_refs, vault_context{nodes (by path, latest round wins)}, profile_fingerprints, dependency_fingerprints, source_fingerprints}`; `session-<id>.checked.json` = the last checked answer. The union is append-only and deduped by item id (an item keeps its first number). A round payload adds `session`, `round`, `new_items` and `n` on each item, `ticket` = the session id; the prompt numbers items by `n` (`answer.build_prompt(..., numbers)`). Without `offset` the same question (NFC, casefold, strip) continues at its stored `next_offset`, a new question starts at 0. Round `fingerprint` = sha256 of JSON (sort_keys) `[[union item (every stored field), source_fingerprint] for each item_number]`; `neural.check` and every later round refuse a round hash mismatch, a number out of range or a union item no round covers ("session round N changed; open a new session"), then recheck every union item like a plain ticket item (in a session a changed item, paper dependency or vault scope says 'open a new session'); a context note whose bytes or profile fingerprint changed refuses only a reply whose reasoning cites it ('Vault note <path> changed after the round that retrieved it…') and no longer backs note-only numbers; a later round that retrieves it refreshes it; a round is not written when the session file gained a round or was closed meanwhile; a later round whose shared item or paper dependency fingerprint changed is refused. `list_sessions(vault)`, `open_session(session, numbers?, offset?)`, `close_session(session)`: §10.
NW-10 (U6b): a session round payload adds `slots` (union over the session's round questions of `neural.question_slots`, in `brainstorm_slots` order), `coverage`, `stop_reason` and `prompt_tokens` (est_tokens of the round prompt); each round row stores them; `list_sessions`/`open_session` add the last round's `stop_reason`; session=None is unchanged. `config/wh_words.yaml` (param `question_words`): `brainstorm` (asks every slot), `lowercase_only`, `slots.<slot>.en|vi`; NFC+casefold whole tokens, longest phrase first, a matched span consumed; a `lowercase_only` phrase matches only when not written in capitals ('ai', 'Ai'; never 'AI') in a question with Vietnamese diacritics (NFD or đ); no WH word = `what`. `coverage` = `{slot: [item n ascending] | "missing", "heuristic": true}` over the session union; an item fills a slot via `slot_sources` from stored fields only (`neural._slot_fills`): its `unit_type`; `protocol.<field>` = a detail of a current `protocol_profile.json` cites its sentence; `profile.<key>` = a ready `section_profile.json` answer cites it (stage->when, origin/setting->where); `node.species`/`node.cell_type` = its unit's `species`/`cell_type`, or its sentence is in the `source_ids` of an organism/species/cell_type note of the session's vault_context; `groups` has no stored field and fills nothing. `stop_reason`, the first that holds: `token_budget` (sum of the rounds' `prompt_tokens` >= `session_token_budget`), `max_rounds`, `all_slots_filled`, `no_gain` (the last `no_gain_rounds` rounds added no item filling an asked slot), else null. A round after `max_rounds`/`token_budget` is a tool error: "session <id> reached <reason>; answer from it or close it and open a new session". `neural.check` (and so `ask_check`) of a session appends to `missing` one line per slot the last round's `coverage` marks "missing": `WH '<slot>': chưa có mục nào của phiên được code gắn cho câu hỏi này (ước lượng bằng code, NW-10).` NW-07 cleaning runs after the hard-stop refusal (a refused round writes nothing).
NW-07: prepare removes plain `<32hex>.json` / `<32hex>.checked.json` older than `ticket_retention_days`; `session-*` files go only via `close_session`. `_profile_fingerprint` = sha256 of {vault, path, note sha256, concepts sha256, `workflow.fingerprint` of the node papers' `node_profile.FILES` + relations.jsonl, `vault_papers_sha` (the vault's `paper_vaults` rows)}: content, never mtimes.
Walk and path (NW-05): a step is node A <- evidence note R -> node B, both edges knowledge edges, B eligible (a node note, `neural._is_node_note`: a node folder, not `_Maps`; not an index or stub
note), and at least one sentence id of the two edges resolving to a stored sentence of the vault; `sentence_ids` = the ordered union of
the resolving ids of both edges, `quotes` copied from the stored sentences, `unit_id` = the A-side edge's (else the B-side's), `etypes` =
the two edge types (a `hypothesis_only` / `cited_background` step is no observed link). A path is a shortest chain of at most
`max_steps` steps; among equal lengths it prefers fewer in_taxon / uses_method edges (ordering only). Hubs are marked `hub`, never
skipped: walk and path name their nodes (DECISIONS 2026-10-22, U3). Paper notes give no typed edges, so a route only through a paper
note is not found.
Names match NFC-normalised and case-insensitively (CHR-01). When one evidence note links a node by two edge types, the step reports
the type that is not in_taxon / uses_method. When there is no readable index yet, the call is a tool error ('vault index refreshing:
try again'), never `found: false`. Page size 12 (max 50). The index refreshes incrementally and takes the
writer lock with a 10 s budget; an explicit timeout error, never a stolen lock.

Ticket `cache/neural_sessions/<ticket>.json`: UUID, question, vault, vault_context, items, papers, prompt/rules,
`source_fingerprints` (SHA-256 of source row and paper row), `profile_fingerprints`, `dependency_fingerprints`, `note_refs`, `source_papers`, bounded `graph_context` (in `papers`); everything the page returned except `prompt`.
`neural_check` reads the server snapshot (never the caller's items), rechecks note bytes, scope, record provenance and
text, then runs A1–A8 and A10; result stored in `<ticket>.checked.json`. Notes are untrusted context, never instructions and
never primary evidence. Fast/Deep source budgets 24/45 items, same-paragraph extra context 4/12.

### 4.4 Overlap finder

`overlap.find_overlaps(con, text, vault=None, limit=20)` (CLI `overlap`, MCP `find_overlaps`, `POST /api/overlap`): word 5-gram
shingles; hit `sentence_id, paper_id, section, page, containment, jaccard, longest_run, spans`. Always "lexical overlap", never a
plagiarism verdict. Limit at most 100.

## 5. Structured profiles (demo3)

All carry `source_sha256` and `source_revision` (version 2: fingerprint of `sentences.jsonl`, registered source files and an
allowlist projection of source metadata; AI/analysis/status/vault/counter fields of `metadata.json` are excluded, so writing an
analysis does not stale profiles; legacy whole-metadata revisions are accepted while metadata is unchanged). `prepare` returns
the revision; replies must echo it; a stale reply is refused; the previous file goes to `analysis_history` with a UUID.

- `structure.json` v1 (code only): `sections`, `abbreviations` (short, source definitions, IDs), `unresolved_abbreviations`,
  `term_connections` (only `shares_source_paragraph`), `methods` (name, ordered IDs), `uncertainty` (sentence_id, markers with
  category/text/start/end/strength/label_vi), `discussion_comparisons`, `conclusion_context`, `objects`, `coverage`,
  `introduction_wh`, `profile_status`, `scope_note`. An inventory, not a scientific result.
- `section_profile.json` v1: keys exactly `what, why, who, where, when`. Answer: `text, sentence_ids, quotes[{sentence_id,text,
  page}]` (quotes by code), or `text: null` + `reason` (+ optional `searched_sentence_ids`, context only). Only Introduction
  sentences. Errors `WH1`; English text gets `G1` grounding checks (an opposite direction word, specific organism, gene-like token
  or unit number absent from the cited sentences is rejected; a defined acronym may match its expansion). `A7_setting` is a
  warning for who/where/when. Null never means "does not exist in the literature".
- `protocol_profile.json` v1: `protocols[{name, sentence_ids, details[{field,value,sentence_ids,quotes}], steps[{sentence_ids,
  quotes}]}]`, `skipped`, `coverage_missing`, `status done|needs_fix`, optional `source_absence_reason` (reviews without
  Methods). Fields: `technique, chemical, concentration, dose, equipment, model, temperature, duration, timing, comparator,
  route, medium, supplement, material, reagent`; others as `field: other` + `label`. Name and values are exact Methods
  fragments on whole-token boundaries (5 mg not inside 15 mg). Source order kept; unknown stays unknown.
- `paper_overview.json` v1: lists of `{text, sentence_ids, certainty}` for `research_question, study_design, groups_models,
  key_methods, main_findings, discussion_comparisons, limitations`; `certainty` is copied source wording (a hedge in a cited
  sentence must stay); `overview_sha256`, `reference_only: true`, `analyst`, `scientific_acceptance: "not_assessed"`. Replies echo
  `reference_only, source_evidence_type, paper_id, kind, task_id, chunk_id, revision_of, source_sha256, source_revision`; empty
  overviews and unknown top-level keys are rejected. Budget 150 000 characters per call.
- `paper_outline` (code, in every payload, reference only): `sections[{section, subsection, first_sentence_id,
  last_sentence_id, sentence_count}]`, `objects[{kind,id,object_id,label,caption,caption_sentence_ids}]`.
- Tasks `structured_tasks/<task_id>.json` (`pending|accepted|submitted|invalid_output`): chunks of at most 32 sentences / 12 000
  characters, split only on sentence IDs and never inside a `continues` chain, with previous/next sentence as reference-only
  context; Methods subsections merge to at least 15 sentences / 3000 characters. Retries `analysis.max_attempts` (structured
  default 2), `max_chunks` 1–20; repair prompts carry the previous validator error verbatim. A source or code change invalidates
  only affected chunks. AI is opt-in (`--ai`, `allow_ai: true`); no fallback to the answer provider.
- Structured runner artifacts: per-chunk raw reply `structured_tasks/<task_id>_<chunk_id>_reply.json`; chunk state `status
  running|returned|failed` with `provider_signature` (provider, model, reasoning, transport, timeout, rules, runner code) and
  `reply_sha256`; accepted chunks stay accepted across interruption and provider changes; a same-signature intact reply is reused
  without another call; stale source/revision replies are rejected, never rebased. `task_id` = 32 hex of SHA-256 over kind, paper,
  source hashes, revision_of, chunk inventory, rules, code. Completion report: `complete, pending, protocol_coverage_missing,
  scientific_acceptance, wh/protocol {ready, technical_coverage, status missing_or_invalid|current|missing_context|source_absent|
  pending_review|needs_fix, source_evidence_type, errors}`; `status done` of a protocol means technical coverage only, an all-skips
  protocol has `interpretation_status: pending_review`. Usage rows (`structured_wh`/`structured_protocol`) go to
  `analysis_usage.jsonl`; replays make no call and no fake usage row.
- Completion: with `analysis.require_structured_profiles: true` (default) `analyze` is `done` only when evidence is `done`, WH has
  all five fields and protocol is `done`, all on the current source fingerprint. `finish_analysis` returns `structured_profiles`
  and `demo3_complete`.

## 6. Concepts, relations, graph, discovery, imports

- `concepts.jsonl`: `concept_id, name, type (concept|method|molecule|cell_type|species|topic), mesh_ui,
  mesh_tree_numbers, parents, synonyms, status (mesh|ai_proposed|human_confirmed), classified_by (terms|ND-09|<clause id>|ai:<model>|human)`,
  and `source_sentence_ids [..]` only on a node that has an `add` decision row (copied from the row).
  `classified_by` says who set `type`: `terms` = the AI term lists (TYPE_PRIORITY), `ND-09` = the MeSH tree (`concepts.mesh_tree_type`, params in
  `config/standard.yaml` ND-09; only a node whose type in THIS build is `concept`, never `topic` or a human_confirmed node), `human` = a human_confirmed node,
  a clause id or `ai:<model>` = a decision row. Precedence: human_confirmed > decision row > ND-09 > TYPE_PRIORITY.
- `node_decisions.jsonl` (store root; `scripts/kbsys/node_decisions.py`): one JSON object per line, every row live (undo = delete the row and
  rebuild): `key` (`concepts._key` of the node name), `name`, `kind` (type|no_mesh|not_a_node|add), `value` (a node type for type/add, else null),
  `rule` (clause id), `classified_by` (clause id or `ai:<model>`), `sentence_ids [..]`, `at`, `reason`. Read by `concepts.build_registry`: a row
  names the node by the key of any of its terms (name, synonyms, members; several rows -> the smallest key); `type`/`add` set `type` + `classified_by`
  (never on a human_confirmed or `topic` node), `add` joins the build as a term of its type (on a key a term already has, it acts as a `type` row: name and topic kept; a
  type/add row without a valid node type stops the build with ValueError), `not_a_node` keys join `concepts.generic_keys()` (so
  notes, result notes and the ND-04 check drop them too; only terms with exactly that key leave), `no_mesh` makes `concepts.mesh_hits` return [].
  Writer: `kb.py node-decision submit FILE | remove KEY KIND | check` is the only code that writes the file (`node_decisions.validate`; writer
  lock; each submit all-or-nothing). A row is refused on: a bad field, kind, value, rule or classified_by (`terms`/`human` never); a key or name
  that is not `concepts._key`/`_clean`; a type or not_a_node row on a human_confirmed node, a type row on a topic node; a not_a_node key in a
  `synonyms.yaml` group or that is not the only paper-term/add-row key of its node (the build would split the node under a new id or, on a
  spelling-variant key, ignore the row); a second type/add row
  on one node under another key; an add on an existing node, a generic key, or a key a paper abbreviation merges into a node
  (`concepts.abbreviation_pairs`); two new add rows a paper abbreviation pairs (the build keeps one row's type and sentence ids); an ai/add row
  without a sentence id; a sentence id that is not a full id, does not exist, is not body text (`presentation.body_sentence`), sits in a paper in
  no vault, or does not match the node (`presentation.matches_term` with the node's synonyms; add: the name); a duplicate (key, kind).
  `reextract` renumbers sentences but does not rewrite this file: `kb.py node-decision check` re-validates every stored row (exit 1 on a failure).
  `kb.py node-decision preview [FILE]` (`node_preview.run`, read-only, no writer lock, never writes the store; refuses an --out under the store,
  vaults, sources or review): FILE rows go through the same checks as submit, then `node_decisions.using(rows)` (an in-memory override of `rows()`) builds B (no rows)
  and F (stored + FILE rows); the report lists S -> B (stored concepts.jsonl vs the G5 rebuild) and B -> F (decision effects) with cause and undo, and
  flags an added node that passes ND-05 in no vault.
  A rule type is never written back into stored term lists: `evidence.py` stores terms with `normalize_terms(..., keep_rule_types=True)` (a node
  with `concepts.rule_typed` keeps the list the AI chose; the build re-applies the rule, removing the rule undoes the type); display callers (`notes`)
  still group by the registry type. A registry type not from `terms` beats an accepted card's `node_type` (`node_profile.graph_node_type(..., record)`).
  A stored record without the field (before the first G5 `link-concepts`) is compared as if it had this build's value (`concepts._changed_records`: no whole-library relink). Built by code from `paper_terms`,
  `config/synonyms.yaml`, safe plural/hyphen variants and unambiguous exact MeSH matches; `human_confirmed` name/type are kept.
  Short or ambiguous aliases are ignored; all-caps acronyms are case sensitive (EG does not match `eg`; ITS beats "Its" when it
  occurs verbatim). Read paths never rebuild the registry.
  An acronym and its expansion that one paper defines together (`structure.json` `abbreviations`, expansion letters contain the acronym's
  letters in order from its first letter) and that are both registry terms are ONE concept (`concepts.abbreviation_pairs`, applied in
  `build_registry`). `config/homonyms.yaml` (`<node name>: [regex, ...]`, case-insensitive): a sentence matching a pattern is NOT linked to
  that node by the linker, concept notes or node-card tasks (`presentation.homonym_blocked`, used by `matches_term`, `concepts._pattern_matches`,
  `concept_notes._text_concept_ids`); effective at the next `link-concepts`.
  `concept_id` is unique: `build_registry` keeps the id of the previous `concepts.jsonl` once, and a concept whose slug is already taken by a kept or earlier id gets the slug plus an 8-hex hash of its name.
  MeSH enrichment (`concepts._enrich_mesh`) looks the node name up first; when the name has no MeSH hit, the node's non-acronym synonyms (a spelling merged under an acronym name: CK7 / Cytokeratin 7) are looked up, and an all-capitals synonym never is (ND-01, ND-08). The match must still name exactly one descriptor. `concepts.mesh_hits` is that one rule; `selftest.check_concepts` re-derives the match with it.
  A species or cell-type word inside a reagent phrase (`presentation.SPECIES_REAGENT_PHRASES`: fetal / foetal / fetus bovine serum, ...) is not linked to the species / cell-type node (`concepts._pattern_matches`).
  A term of `config/generic_terms.yaml` that is not a registry node (researcher-confirmed ones are) gets no wikilink in the paper note and no stub (`notes._canonical_analysis_terms`); `tidy_vault` then moves its old unlinked pure stub.
  ND-05 in the paper note: a registry node is linked (studied term, "Mentioned in text") and gets a stub (`notes.ensure_linked_stubs`, `ensure_analysis_stubs`) only when it passes the vault threshold `concepts.meets(units, mentions)` (params of ND-05 in `config/standard.yaml`; `notes.vault_gate` = one SQL query of mentions per concept, exact evidence units via `concept_notes._collect` only for a node below the mentions test); a studied term that fails stays plain text, a failing mentioned node is omitted. It flips on at the next `kb.py notes` run once the node gains evidence. `concept_links.mentioned_note_min_sentences` is gone.
  Merge key ignores case, hyphens, spaces between letters, apostrophes (straight, curly U+2019, modifier U+02BC), a closing number after a hyphen (Lyve-1 = Lyve1; not a number in the middle: IL-1 receptor 2), Greek symbol vs name (RT-qPCR = RTqPCR, m 6A = m6A, Β-tubulin = beta-tubulin); a spelling pair the key cannot see (Blastocoel / Blastocoele) is a `config/synonyms.yaml` group;
  `config/concept_parents.yaml` (`child: parent`) adds a non-synonym parent to `parents` (beta-tubulin under Tubulin).
  selftest `note_text_problems` (N3e): no broken characters, empty sections, placeholders, case-only wikilinks.
- `concept_links` (SQLite): `concept_id, paper_id, level (studied|mentioned), n_sentences, sections, first_sentence_id`, built
  from full text (not references or `in_table`; includes `table_row`), never from AI tags alone; every paper with a matching
  sentence gets a row; a parent aggregates descendants.
- `relations.jsonl`: `relation_id (16 hex), from_unit, to_unit, type (supports|contradicts|direction_differs|
  effect_vs_no_effect|extends|replicates|same_method|same_model|mechanistic_link|miscitation), rationale, differences,
  status (ai_proposed|human_confirmed|human_rejected|withdrawn_by_rule), match_basis (measure_group|exact_measure|
  shared_concept:<id>|shared_concept_clause:<id>), measure_group, directions, proposed_by, created_at, history`.
  `propose-relations` is deterministic: same outcome, same intervention agent (RL-02), distinct papers, opposite directions.
  Agent rule (`relations._same_agent`, unit and clause level): `intervention.agent` equal after normalising; a named agent against an
  empty one is different; two empty agents are the same only when both texts name a shared intervention (vitrification, cryopreservation,
  freezing, thawing). `contradicts` only when both units name that same agent, `direction_differs` when both agents are empty (a matching
  `model.cell_type` is no longer a route); `effect_vs_no_effect` for change vs `no_change` (non-significance is not proof of no effect).
  Same outcome: `exact_measure` / `measure_group` pairs by definition; a shared-concept pair (`shared_concept:` / `shared_concept_clause:`)
  only when both compared texts are equal once the shared concept's name and synonyms and the `outcome_quantity_words` (RL-02 param,
  naive word list) are removed; a measure that is just an organism name or synonym is no `exact_measure` token. `compared_measures`
  `[from side, to side]` (aligned with `directions`) are those compared texts: the unit's `outcome.measure`, or for the clause side
  the concept name. Bundled measures (RL-03: `[,;/&]` or whitespace-delimited and / or / versus / vs) give no `exact_measure` or
  `shared_concept` token; their outcomes pair only through `shared_concept_clause`, run whatever the unit direction. A clause gives an
  entity a direction only when its clause holds direction words of a single direction (up-/down-regulated count), and the unit's own
  intervention agent is never a clause entity. A clause gives no direction to: any entity of a text (or of a non-bundled unit whose
  measure) naming an RL-03 `non_outcome_words` word (GO/KEGG enrichment, baseline characteristics); an entity span inside parentheses
  or followed by `-` + a lower-case word of 2+ letters (modifier; `NGF-β`, `PDGF-BB` are isoforms); a pair with an RL-03 `predicate_words` word plus `, ; :` between entity and direction word.
  The entity must occur in `outcome.measure`; method concepts and
  `relations.MEASURE_ENTITY_STOPLIST` never serve as basis; at most `MAX_PAIRS_PER_CONCEPT = 30` per concept. A rerun withdraws every
  `ai_proposed` conflict row of the vault being run whose pair is no longer a candidate (any `match_basis`): status `withdrawn_by_rule`,
  `withdrawn_reason` names the first failed rule (`RL-02: different intervention agents ('X' vs None)`, `RL-02: no named agent and no
  shared intervention in the texts`, `RL-02: a species name is not a measure`,
  `RL-02: different measured outcomes`, `RL-03: bundled measure paired as a whole`, `unit is no longer an eligible result`, else
  `no longer a candidate under RL-02/RL-03`); revived if valid again, no row is ever deleted. Only relations whose two units both belong
  to the vault being run are touched (a rerun for one vault never withdraws another vault's relations); researcher statuses are never touched. Rows carry `vault` (a run without `--vault` writes the one vault both papers share);
  `kb.py backfill-relation-vault [--dry-run]` fills legacy rows from the one vault their two papers share (back up first). Active = `ai_proposed|proposed|
  human_confirmed`. Evidence units are never changed.
- `measure_groups.json`: `{schema_version:1, groups:[{group_id "mg:<slug>", name, members[exact measure strings], rationale,
  status, proposed_by, created_at}]}`; `human_rejected` groups are ignored.
- Conflict display: 🔴 contradicts, 🟠 direction differs, 🟡 effect vs no effect, ✅ same direction; side-by-side table with a
  column per direction (↑ ↓ =); pairs written `A ⟷ B`. Conflicts are never deleted or hidden.
- `enrich-interventions`: AI may fill missing intervention/model fields of increase/decrease `result` units only with a value that
  is a whole-token match (NFKC, case-folded, at least 2 chars) in a cited sentence; provenance in `field_provenance`;
  `human_confirmed` units and existing values are immutable; old `evidence.jsonl` is archived first.
- `ghosts.jsonl`: `ghost_id, doi, pmid, title, first_author, year, journal, cited_by[ref_id], count` (rebuilt from local
  references; no DOI and no title+year means no ghost).
- `gap_candidates.jsonl`: `gap_id, gap_type (untested_combination|unexplained_conflict|unproven_hypothesis|broken_chain),
  description, supporting_units, queries_run, papers_scanned, scope_note ("not found in the current vault", never "does not exist
  in the literature"), status needs_review`. Only the researcher promotes a gap. Full rules below.
  Row fields as written by code: `gap_id` = "gap:" + 12 hex of SHA-1 over type|vault|key; plus `vault, provenance
  (generator|analyst), created_at, entities?`; `scope_note` is always "not found in the current vault — this does not mean it is
  absent from the literature"; `papers_scanned` is an integer (papers of the vault checked; null when an agent gave none).
  `needs_review` and `withdrawn_by_rule` belong to the code, any other status is a human decision and is never rewritten,
  withdrawn or duplicated. Written by `kb.py gaps --vault V [--dry-run]` (`kbsys.gaps`; deterministic, accepted units only, no
  AI/network) and by MCP `propose_gap`. A rerun refreshes `needs_review` generator rows, withdraws (`withdrawn_by_rule`,
  `withdrawn_reason`) generator rows no longer derivable and revives them if they return; analyst rows are never touched.
  Keys: `unexplained_conflict` = relation_id of an active conflict relation (`relations.conflict_relations`) without
  `explanation`/`human_explanation`; `unproven_hypothesis` = unit_id of an accepted `hypothesis` (or hedged `interpretation`)
  unit whose `terms.concepts/molecules` have no accepted `result` unit mentioning them (whole tokens, plural/singular) in
  measure, terms or statement under the entity's own name, the name or a synonym of its registry concept, or of a child
  concept (never a parent; an entity outside the registry is matched by string only; RL-08; `queries_run` line 2 says so)
  (a unit without such terms is skipped); `untested_combination` = measure|intervention.agent|
  species where both have accepted results for the same measure (reviewed measure group when any) but never together in one
  paper's results, only the top `gaps.max_untested_combinations` (default 15) by unit count; `broken_chain` = unit A (agent a,
  increase/decrease, measure naming agent b) + unit B (agent b, other measure C) with no result of agent a on C. At most
  `gaps.max_per_type` (default 100, `config/system.yaml`) per type. Node notes never show gaps (approved section set only). `vault_layout.write_maps` writes the machine catalogue `_Maps/Open hypotheses.md` (renamed 13/10 from `Candidate gaps.md`, which is moved to
`knowledge_store/removed/renamed_maps_<date>/` when machine-only; unexplained conflicts only counted, kb:auto, banner, grouped by type, citations via `presentation.cite`, IDs only in the Source IDs callout), linked from Index and Home; `pipeline.rebuild_vault_notes` runs `gaps.generate` first.
- Graph `GET /api/graph`: nodes/links of the selected vault only (paper-concept, resolved citations, MeSH parents, relation links,
  cached discovery candidates). Caps 10 000 nodes / 30 000 edges. `contradicts` solid red, `direction_differs` dashed orange.
- Discovery (`discover-citations`, opt-in): OpenAlex, sends only the seed DOI; cache `knowledge_store/discovery/<vault>/<pid>.json`;
  candidates are unverified leads. `stage-discovery` rechecks the library (DOI, then title+year) and writes only a metadata
  manifest into `sources/inbox/discovered/` (ignored by ingest). No PDF is downloaded.
- `import-refs`: CSL JSON, BibTeX, RIS, Zotero SQLite (copied to temp, opened read-only). Match order DOI, PMID, title+year;
  matched papers get `reference_managers[{manager,item_key,attachment_path}]` and only empty metadata is filled; unmatched
  records become `sources/inbox/refmanager/<id>.json` (ignored by ingest); attachments are listed, never touched.
- Non-PDF sources (`kbsys.sources_misc`): `knowledge_store/sources/<source_id>/{metadata.json, sentences.jsonl, original/,
  attachments/}`: docx (paragraphs, headings, tables, images), md/txt, images (text only from a sidecar `.txt/.md`, else
  `needs_ocr`), video/audio (sidecar `.vtt/.srt/.txt` with `time_start/time_end`, else `needs_transcript`). Same SHA-256 only gains a
  vault. Searchable in `source_sentences_fts` as `source_hits` labelled non-published evidence (`evidence_class: own_data|
  human_note`), never mixed with paper hits or answers. `Sources/<source_id>.md` notes (`source_notes.write`).

## 7. Workflow, cache and locks

- Seven stages (`pipeline.STEPS`): `ingest, quality, structure, analyze, connect, vectors, notes`. `kb.py steps <stage>
  --vault V [--limit N] [--structured-ai] [--max-chunks N]`, `steps --status`, Library UI. One paper and one writer at a
  time. `analyze` in `mcp_agent` mode returns `waiting` (the connected agent must call the analysis tools); it is never
  recorded as done by waiting.
- Checkpoint `knowledge_store/workflow/<sha256(stage:scope)[:24]>.json` v1: `stage, scope, status
  (running|done|waiting|failed), input_sha256, output_paths, output_sha256, output_fingerprints, missing_outputs,
  started, ended, cache_hit, result/error`. Written temp + flush + fsync + replace. A cache hit needs matching inputs,
  outputs list and output bytes; running-after-crash is never a hit; old checkpoints without checksums rerun once.
  `job_last.json` is a UI snapshot only.
- Vector stage tracks `index.json`, the referenced `.npy`, every per-paper `.npz` and model/tokenizer; a changed model or
  code invalidates vectors only (no PDF re-import, no AI).
- Locks: `workflow.writer_lock(name='library', timeout=0)` uses `O_EXCL` files in `knowledge_store/locks/` with
  `{pid, token, name, created}`. Locks never expire or get stolen. After a crash check the PID before removing one
  (never remove a live lock). Read-only CLI commands skip the lock. A busy lock yields `WriterBusy`; AI runners mark the
  item `blocked_by_writer` without spending a call.
- One lock name, `library`, guards every store/vault write (node cards and figure readings included). It is reentrant
  per thread and name, so a command that already holds it cannot release it around an AI call. Therefore `kb.py`
  does not wrap the AI commands `analyze`, `structured-run`, `steps`, `node-card`, `figure-reading` in the lock: the
  runners take it only around store writes (submit/import/write), never around the provider call. `steps` locks every
  stage except `analyze` for its whole run. Non-AI writers (`notes`, `link-concepts`, `ingest`, ...) hold it for the
  whole command. MCP `structured_run` is likewise not wrapped.
- `pipeline.rebuild_vault_notes(con, vault)` is the single 'rebuild notes of a vault' routine (paper notes, concept
  notes, source notes, indexes, mother vault, neural prewarm). `kb.py notes`, `kb.py steps notes` and
  the post-delete refresh all call it, so they cannot drift.

## 8. Vault notes (Obsidian)

### 8.1 Ownership

Only the text between `<!-- kb:auto:start … -->` and `<!-- kb:auto:end -->`, and machine frontmatter keys, are written
by software. Everything else is human text and is never touched (`## My notes`, custom properties, `Mine/`). A note whose
auto block was edited by hand is left alone. Retired machine notes move to `knowledge_store/removed/…` (never deleted);
a retired note with human text stays with a warning block and `retired: true`. Index notes carry `tags: [kb-index]`.

### 8.2 Layout v2 (`notes.FOLDERS`, `vault_layout`)

`Papers/ Concepts/ Methods/ Substances/ Organisms/ Results/ Claims/ Review Queue/ _Maps/ _Bases/ _Templates/ _Attachments/<paper_id>/`
and `Mine/{Ideas,Questions,Knowledge Gaps,Decisions,…}` (human only, never written; the machine writes its catalogues
under `_Maps/` instead). Term type to folder: molecules to `Substances`, species and cell_types to `Organisms`, topics to
`_Maps` (type `moc`), concepts and methods unchanged. File names of nodes never change. Two records with one file name:
the first keeps it, the other is counted as `name_clash`. `Home.md` gets one `kb:auto` navigation block after the
researcher's text; its first link is `[[Cần duyệt]]`. The review queue is `<vault>/Cần duyệt.md` (vault root, `review_digest.write`,
RQ-01..03, RQ-06): three instruction lines, `## 1..4` groups (source-paper error, AI error with an uncertain fix, orphan
node/paper, undecidable claim), one `### [Nhóm N] title` per item = `> ` source sentence copied from `sentences` by code, `Bài: [[note]]
· PDF trang N`, `**Máy phát hiện:**`/`**Đề xuất:**`, three `- [ ]` boxes (agree / keep as is / other) and `<small>mã: sentence ids</small>
<!--k:key-->`; ticks are kept by key; an empty group shows `*Không có.*`. Node cards, conflicts, open hypotheses, wrong crops and orphan
images are not queue items. The older file `Review Queue/Cần duyệt.md` is moved to the root on the next `kb.py notes`. `kb.py notes` also adds `Cần duyệt.md` to `<vault>/.obsidian/bookmarks.json` (pin, RQ-06) only when no file bookmark with that path exists (groups included); the researcher's entries are never removed and an unreadable file is left alone. Maps: `_Maps/Index.md`, `Open conflicts.md`, `Results and media.md`, `Chú giải graph.md`, `Log.md`
(append-only), and Ideas/Questions/Knowledge Gaps index notes. Written once, never overwritten: `_Bases/*.base`
(Papers, Substances, Methods, Organisms, Open conflicts, Review queue), `_Templates/Template - {Idea,Question,Hypothesis,
Decision,Lab note}.md`, `.obsidian/snippets/kb-conflict.css`. Obsidian settings are merged, researcher keys kept.
`kb.py vault-migrate --vault V [--dry-run] [--undo MAP]` moves a legacy vault with a snapshot, a map
`knowledge_store/migrations/<stamp>_<vault>.json` and a before/after link check; it is refused if unresolved links would
increase. `notes`, `concept-notes` raise a plain error while legacy moves are pending.
Every other retired machine note (`vault_layout.retire`: concept-note orphans `concept_notes_<stamp>`, tidy stubs
`vault_stubs_<stamp>`, result notes `result_notes_<stamp>`, the old catalogue note `renamed_maps_<date>`) is moved to
`knowledge_store/removed/<kind>_<stamp>/<vault>/<vault-relative path>` (`_1`, `_2` on a clash, never overwritten) and
recorded in `knowledge_store/removed/<kind>_<stamp>/<vault>.undo.json`, the same map shape (`entries` with `action: "retire"`,
`from`, `to`, `sha256`); `kb.py vault-migrate --undo MAP` restores them (a file that reappeared is a conflict, not overwritten).

### 8.3 Frontmatter and types

`type`: `paper, method, technique, substance, organism, cell_type, concept, result, claim, conflict, moc, home,
lab_note, question, idea, decision, hypothesis, meeting, vault, vault_registry, paper_ref, source`. Machine keys include `title, authors,
year, journal, doi, pmid, evidence_type, classification, analysis_status, text_quality, source_status, also_in`
(other vaults holding the paper; never a review reason), `kb_kind, kb_roles, kb_category_basis, kb_summary,
evidence_count, source_papers, kb_stub`. `presentation.metadata_for_term` separates kind (chemical, chemical_class,
biological_entity, molecule, method, concept, topic, species, cell_type, paper) from contextual role (intervention,
measured_marker, model, source_mention). Roles need a whole-token copied-quote match plus a matching structured field.
The category mapping is a presentation choice, not a scientific definition. Untouched machine stubs get `kb_stub: true`
after a byte copy to `knowledge_store/stub_history` and are hidden from graph and indexes.

### 8.4 Size caps and rendering

Paper notes at most 120 KiB, node notes at most 60 KiB, `_Maps/Index.md` nodes part 60 KiB. Over the cap, whole lowest-
priority quote rows are dropped (never partial quotes) and one English line `> N more items not shown; the complete list is in the dashboard.` is added (no link: VN-08 allows no 127.0.0.1 link in a note; `notes.cap_auto_block(block, cap, row_kind)` has no dashboard parameter; the old Vietnamese notice with a link is still removed when found); human text is never cut. Callouts:
`> [!conflict]` red (also accepts `[!danger]`), nested `> >` folds; no `<details>`. Quotes are copied from
`sentences.jsonl`; an unlinked sentence starting lower-case is prefixed `[…] `. Copied hedges stay visible. No raw JSON,
`None` or Python dicts in notes (regression class). Concept Note format version is `concept_notes.FORMAT_VERSION`; synthesis
payload version `SYNTHESIS_PAYLOAD_VERSION`.

### 8.5 Graph palette (`notes.GRAPH_GROUPS`, first match wins)

Folder queries, in this order: `path:"Mine/"` `#FF00A8`, `path:"Discussions/"` `#FACC15`, `file:"CONFLICT -"` `#DC2626`
(red only for conflicts), `path:"Papers/"` `#2563EB`, `path:"Methods/"` `#0D9488`, `path:"Substances/"` `#7C3AED`,
`path:"Organisms/"` `#16A34A`, `path:"Results/" OR path:"Claims/"` `#D97706`, `path:"Concepts/"` `#0EA5E9`,
`path:"_Maps/"` `#84CC16`; anything else default grey. The web graph uses the same colours by note type
(`notes.GRAPH_TYPE_COLORS`). Merge (`notes.merge_graph_groups`, used by `notes` and `mother-vault`): researcher groups
(any other query) first in their order, then the machine groups; a machine query already in the file keeps its colour
(the researcher's recolour); legacy machine queries (`LEGACY_GRAPH_QUERIES`) and duplicates are dropped; other
`graph.json` keys are preserved. `kb.py graph-colours [vault...]` (default: every vault plus the mother vault; every name must be a research vault, checked before any write) backs up
each `graph.json` and the `_Maps/Chú giải graph` legend to `<name>.bak-<YYYYmmdd-HHMMSS>` beside it, re-merges without a
notes rebuild and reports VN-09 per vault (the mother vault: group order only, its filter shows maps). Default filter
hides `kb-index`, stubs, Review Queue, `_Bases`, `_Templates`, `_Attachments`, Home. `hideUnresolved: true`,
`showAttachments: false`. VN-09: the machine queries appear in this order as a subsequence of the file's groups
(colours not checked). Extended Graph: `syncDefaultState: true` (`notes.sync_extended_graph`, run by `notes`,
`mother-vault`, `graph-colours`, and in the install defaults), so its Default state opens with graph.json's colours and
filter instead of its saved copy (empty groups and filter replaced the colours, 2026-10-24); `graph-colours` also backs
up its `data.json`; VN-09 fails when the flag is off. Link colours: `conflicts_with`/`opposes` red, `supports` green, others `gray`.

### 8.6 Generated notes

- Concept Notes (sections: 8.9): synthesis (only A1–A6-passing sentences, at most 15; `concept_notes.language` `vi|en`; at most 8
  units per paper and 60 per note sent; stale text keeps `needs update — evidence changed`), evidence table,
  agreement/conflict/mechanism, open questions (stored candidates only). The previous version goes to
  `knowledge_store/concept_history/`. Only `kb.py ingest` and `concept-notes` call AI; `notes` and `link-concepts` are offline;
  `concept-notes --revalidate` re-checks stale syntheses without AI.
- Result/Claim/Conflict notes (`result_notes.refresh`): primary-paper `result` units that have an `outcome.measure` become `type: result`
  EVD notes in `Results/` (at most 15 per paper; a result unit without a measure gets no EVD note, also when a summary, relation or figure
  refers to it, and its old machine note retires through `_retire`); `conclusion` units become `type: claim` CLM in `Claims/` (at most 5; review papers `claim_type:
  review_statement`, `evidence_type: secondary`); relations become `type: conflict` notes. ID = `unit_id`/`relation_id`
  (frontmatter `id`). Frontmatter keeps `sentence_ids`, paper/source links, `conflicts_with`, `has_conflict`, `derived_from`, `about`
  (at most 8 node links). Significance is source-derived (`significant|ns|not_reported|not_tested`); association wording forces
  `predicate: associated_with`, `knowledge_level: statistical_association`. EVD `subject` and `object` are always YAML lists (at
  least one item; stored scalars become lists at the next refresh). `subject` (RL-10, D-PENDING-23 A1/A5): the unit's stated
  subject/agent, else its `model.cell_type` (a node link when that note exists), else the RL-10 `subject_not_stated` text (then the
  only item); never read from the statement; a text that splits on ` and `, `,`, `/`, ` + `, `&` into 2+ parts that are all node
  notes becomes one link per part, otherwise it stays one item. `object` (A4): the whole measure phrase when it is a node note; else
  a link to every registry concept named in the phrase that has a node note (most specific only; no species, method or ND-04
  generic term; phrase order, deduped); else the phrase as text. The phrases as written are the text properties `measure` and
  `agent` (the stated agent when it is no link; a combined agent such as "fatty acids and melatonin" stays one agent; dropped when
  the unit no longer states one). Claim notes: `subject`/`object` are `[x]` or `[]`, rules unchanged. EVD `predicate` (A2, one of RL-10 `predicates`): otherwise the stated direction; `mixed` only when the
  unit states `mixed`; no stated direction (also `not_applicable` or an unknown value) gets `reports`. A measure that is blank or a
  missing-annotation marker counts as no measure. Claim notes and conflict side tables
  keep the old subject/predicate rules. Image-only values are never transcribed.
  EVD statistics (`n`, `p_value`/`p`, `statistical_test`/`test`): the unit's own field wins; otherwise the literal string found in
  its cited source sentences (`P < 0.05`, `n = 12`, `two-way ANOVA`; up to 3 distinct strings joined by `; `; nothing is computed).
  Empty statistics are omitted from frontmatter (old `null` keys are dropped on rewrite) and from the body.
  CONFLICT side tables (and concept `species/stage` cells) fall back to the paper-level model (unit model, protocol model, WH `who`,
  paper title) as a short term marked ` (from paper)` (`presentation.TEXT_FROM_PAPER`); a missing value renders `—` (never placeholder text). Units of review-type
  papers (`find.REVIEW_TYPES`) and `cited_statement` units use ONLY their own fields/sentences/statement, never the paper-level model.
  Species words (incl. adjectives: porcine -> Pig) map to names via `config/model_terms.yaml`.
  Conflict relations are read ONLY through `relations.conflict_relations(vault, history=False)` (strict `vault` match; a row without `vault` belongs to no vault): live = status
  `ai_proposed|proposed|human_confirmed`; `history=True` adds human outcomes (`explained|dismissed|rejected|human_rejected|stale`).
  `withdrawn_by_rule` (and unknown) rows stay in `relations.jsonl` for audit but are never shown: no open/red note, no
  `has_conflict`, not in Open conflicts. One CONFLICT note per (paper pair, measure): `id` = first relation, `relation_ids`
  lists all merged ones (extra pairs shown as "Comparison N"); the H1 equals the file name (file name = title, unique,
  `- duplicate` only on a real name collision). The title names each side's own `Species/stage` from the same chain as the
  A/B table (`result_notes._side_model`, paper-level mark stripped): `X vs Y`, once when equal, and NO species/stage for the
  pair when either side states none (never the other side's). A note with an `effect_vs_no_effect` relation carries the line
  "A non-significant result is not evidence of no effect." (RL-06). A conflict note whose relation is no longer live is retired reversibly (moved
  to `knowledge_store/removed/result_notes_<stamp>/`; if the researcher wrote text it stays with `retired: true,
  type: conflict_history, status: withdrawn, has_conflict: false`).
- Node cards (`knowledge_store/node_cards/<vault>/<safe-concept-id>.json`, schema `config/node_card_schema.yaml`): `schema_version,
  status (accepted|needs_fix), analyst, created_at, card, problems, quote map`. Types: substance, technique, protocol, organism,
  cell_type, concept, topic. Statement `{text, sentence_ids, attribution (primary|review|author_hypothesis|cited_study), hedge
  (none|hedged)}`; unsupported fields `{status: not_found_in_vault}`; related nodes `resolved|unresolved`. Served while every cited quote still
  equals the live sentence (text, section, page); the task `source_revision` only guards new submissions. `submit` also stores `paper_ids` and `unit_ids` = the node's FULL evidence inventory at submit (not only the capped task's units); `current_with_new_evidence` = a paper or unit outside that inventory (a card without `unit_ids` only checks papers). A node task also includes sentences of the paper's accepted units whose statement names the node (abbreviation-only sentences, e.g. `E2` for Estradiol) and, for a paper reached only through a descendant node, that paper's sentences naming the descendants. `pending` runs inside `structure.stamp_scope()` (one hash/parse per paper file). Task cap `node_cards.MAX_SENTENCES = 500` (`--include-sentence-id` requests a trimmed one; `submit` rebuilds the task with the same includes: its own `--include-sentence-id`/MCP `include_sentence_ids` plus the reply's `include_sentence_ids`; ineligible IDs stay `unknown_sentence_id`).
- Mother vault (`mother_vault.refresh`): `vaults/` is the parent vault. Generated: `Home.md`, `_Maps/{Atlas,Efforts,Calendar,
  Open conflicts - all vaults}.md`, `Registry/Vaults.md`, `Registry/Vault - <name>.md`, `Registry/Papers/<paper_id>.md`
  (`type: paper_ref`, path-qualified links). `_Inbox/` is the researcher's drop zone (never touched). Optional `config/vaults.yaml`
  gives `vault_kind`/`inputs`. `projects/vault_registry.json` is a logical catalogue; it does not widen retrieval.
- Plugins (`config/obsidian_plugins.yaml`): Dataview 0.5.70 and Extended Graph 2.7.7, pinned with SHA-256; `kb.py obsidian-plugins
  status|download|install`; `data.json` written only when absent (DataviewJS off).

### 8.7 Media and figure readings

- `media_manifest.json` v1: per object `source/page/bbox/rotation/hash`, `object_bbox`, `region_basis (manual|automatic_locator)`,
  `region_completeness: "not_assessed"`, immutable PNG revisions in `media_history/`, complete ordered caption rows
  (`caption_basis pdf_caption_region|stored_caption_ids`), data, mentions, anchors, exact table rows. `locator_basis`:
  `stored_object_region|manual_locator|same_page_caption|manuscript_figure_page_label|manuscript_figure_page_order|unlocated`; an
  object with no located graphic is `unlocated_object` with crop `unavailable` (a page preview never poses as a figure).
  `build-media PAPER [--regions JSON]` takes the writer lock and never re-extracts; `media-status` is read-only; `media.bundles()`
  reads the manifest on the fast path (input signature + `input_stats`) and falls back to a non-writing rebuild. Uncertain tables
  render as a table only when quality is `ok`, the header is non-empty and rows match; otherwise raw cells in a closed fold.
- Figure readings (`papers/<pid>/figure_readings/<local-object-id>.json`): a task exists only for a crop whose SHA-256 matches. Reply:
  `panels` with values `read_quality exact|approximate|uncertain`, `significance_marks`, `caption_consistency {status consistent|
  inconsistent|not_checkable}`, `summary` with `panel_refs`; `{"crop_wrong": true, "note"}` records a crop that does not show the object (no panels; not a queue item, RQ-02: the secretary re-cuts it; a rebuilt crop makes it stale). Stored with analyst, crop hash, source
  revision, `cited_text_sha256`, `red_warning_candidate`; `load` is `current|stale` (crop hash or cited-text digest changed). A reading never replaces captions, text evidence or crops; numbers
  are never inferred from pixels: a number of a consistency note or of the `summary` text must occur in a shown sentence or a panel (`Invented number in
  caption_consistency note` / `in summary`); labels (Figure 1, SSEA-3, Tuj-1) and digits inside names (`figure_reading._NAME_NUMBER`) are not numbers (except after Day, Week, Group, Dose, Sample, Well and similar words: "Day-3" is a quantity). Queue = figures and tables (formulas with `--include-formulas`), main Results first. `figure_reading.readings(paper_id)` lists the stored readings; `unit_warnings` is the one definition of the sentences an evidence unit must not cite without an `FG` flag (used by `evidence.PaperContext`/`validate` and conform EV-20).
- Image audit (`kb.py image-audit [--vault V] [--out FILE]`, `image_audit.py`; read-only, exit 0; decision 2026-10-18, VN-06 helpers `Files`/`links`/`_sha`):
  one row per image file of a vault (not under `.obsidian`), `--out` = `{"version": 1, "rows": [row]}` with row `{vault, path, sha256, bytes, class,
  paper_id, part, part_crop_status, referenced_by (max 3 note paths), cause, twin, identical_unaddressed (max 3), node_to_recheck, action}`.
  `class`: `figure|table|supplement|source_media` (a note embeds it AND a `media_manifest.json` object matches by crop/previous-crop SHA-256 or
  file name; FS*/TS* = supplement; other kinds = source_media), `wrong_crop` (`figure_readings` says `crop_wrong` for the matched object, and its `crop_sha256`, when recorded, is this file's),
  `referenced_unknown`, `orphan`, `unreadable` (cannot be hashed). The last three (without an address) get `cause`: `clone` (an `orphan` that no note
  embeds, with a same-vault image of the same SHA-256 that has an address = `twin`; action "delete candidate at G5 after backup, researcher approval"), `trace` (`part` = manifest object or
  `unmatched`; `node_to_recheck` = the paper note or `unmatched`; action "re-link at next notes run or report to researcher") or `unreadable`
  (file or `media_manifest.json` missing/not JSON) or `human` (file under `Mine/` or `_Templates/`: never a candidate). `--out` is refused inside
  `vaults/`, `knowledge_store/`, `sources/`, `review/` and over any existing file that is not an earlier audit (LF line ends). Nothing is decided or changed by the command.
- Media API: `media.build(paper_id, regions=None, detect=True, persist=True)`, `media.bundles(paper_id, object_ids=None,
  sentence_ids=None, unit_ids=None)` (read-only, OR filters), `media.crop_path(folder, relative)` (refuses traversal, drive/ADS
  paths, symlink escape). Manual `regions` (or `media_regions.json`): `{kind figure|table|formula|special_symbol_region, source,
  page, bbox [x0,y0,x1,y1] in unrotated CropBox points, optional sentence_ids/caption_sentence_ids/mentioned_in/label/
  source_sha256}`; figure/table also need an existing full `object_id` and only refine the locator. Manifest: `version, paper_id,
  revision, built_at, input_sha256, input_hashes, sources{source: {path, sha256, recorded_sha256, status available|unavailable|
  source_changed|non_pdf|unreadable}}, manual_regions, detect, status done|partial, scope_note, bundles, output_sha256`. Bundle: `object_id`
  (F/T IDs; formula/symbol `<pid>:M<16 hex>` of SHA-256(kind, source, page, bbox), a locator only), `kind, label, caption[], data[],
  mentions[], anchors[]` (exact `sentences.jsonl` rows), `unresolved_sentence_ids, associated_units[{unit_id,unit_type,status}],
  source_locator{page, bbox, object_bbox, region_kind object_region|manual_object_region|source_page_preview|unlocated_object,
  object_region_status annotated|not_located, region_basis, region_completeness, rotation, cropbox, coordinate_system, status
  available|…|missing_region|missing_object}, crop{path, sha256, source_sha256, status available|retained|retained_unverified|
  unavailable|not_built|missing|corrupt}, table{header,rows,footnotes,quality,extraction}`; formulas add `detection`, `optional_text`
  (copied PDF text, `text_status unverified_extracted|unavailable`). Crops are published as immutable `crops/media-<revision>/` and the
  previous manifest copied to `media_history/<revision>/`; nothing is deleted. A changed original gives `source_changed` and keeps the
  old derivative. A missing referenced object becomes a `missing_object` placeholder; its location is never borrowed from a mention.
  `tier1.build` writes crops to `crops/tier1-<revision>/<source-hash>/`, copies prior records to `tier1_history/<UUID>` and lists
  `metadata.tier1.retained_object_sources` when a source PDF is unavailable (objects are kept, not blanked).
- AI runner artifacts: raw replies in `knowledge_store/ai_replies/{node_cards/<vault>,figure_reading/<paper>}/…`; logs
  `knowledge_store/logs/{node_cards_runs,figure_reading_runs}.jsonl`. Statuses `accepted|needs_fix|failed|blocked_by_writer|waiting`;
  a valid unprocessed reply is reused after an interruption.

### 8.8 Node profiles and neural readers

- `node_profile.build(record, vault, paper_ids, units=(), persist=True)` returns a profile (v1): `node_id` (= concept_id), `vault,
  name, aliases, entity_kind, category_basis, status: "source_inventory", scientific_acceptance: "not_assessed", source_sha256,
  papers[]`. Each paper: `paper_id, source_availability, fields{definitions, full_names, source_classes, purpose, roles_context,
  significance, limitations: {status source_candidates|not_annotated, items[], reason}}, context, mentions, protocol_variants[]
  (variant_id is a local locator; name; basis; status; sources; ordered steps; details by the protocol field list),
  protocol_profile_status current|missing_or_stale, results[]` (unit_id, status, `source_status resolved|unresolved`, evidence_type,
  stored model/intervention/outcome/quality, hedging, sources, source_objects; figure-only numbers are null), `conflicts[]` (existing
  scoped relations; rejected/stale kept as history). A source ref carries `paper_id, paper_title, note_name, doi, pmid,
  evidence_type, sentence_id, passage_id, page, section, subsection, source, text, citations, mentions, continues, qualifiers`.
  Selectors are lexical candidate inventories, not accepted definitions. Only a structured protocol whose source hash is current
  supplies assignments; otherwise the Methods subsection is listed as unassigned source inventory. `not_annotated` never means
  "not reported" or a literature gap. Cache `knowledge_store/cache/node_profiles/<sha256([vault,concept_id])[:32]>.json`
  (`input_sha256, output_sha256, profile`); the input fingerprint covers record, vault, selected papers and units, per-paper source
  files, global relations/measure groups and the code files; mixed revisions are rejected; written atomically.
- Neural readers: `neural.resolve_sources(con, vault, ids, limit=24, query='')` (max 80 results, 512 input refs, per paper; items are
  sentence hits with `context_basis source_anchor_or_continues|same_source_paragraph`, `section_parent`, `source_url`);
  `neural.require_paper(con, vault, paper_id)` (membership); `neural.media_reader(...)` (bundles with `source_url`, `crop_url`; HTTP
  limit 24); `neural.node_reader(con, vault, path, expected_sha256, query, include_media, speed)` returns note fields plus `profile`,
  `profile_markdown`, `sources`, `media` (at most 8 papers), `outside_scope_ids`, `related_other_vaults` (labels only),
  `card`/`card_status accepted|missing`, `figure_reading`, `conflicts` (with `source_sides`, `display_status`; `is_conflict` red),
  `scope_policy: current_vault_by_default`. The vault index adds `catalog(path, kind, roles, eligible, metadata)`. Tickets add
  `retrieval_basis`, `source_resolution`, `profile_fingerprints`, `dependency_fingerprints`. `/api/research/crop` serves exact PNG bytes
  only after current source/crop status, membership, safe path and checksum checks; there is no all-vault flag.
- Read-path caches: `media.bundles()` and `concepts.load_registry()` use persisted files, so reads never rebuild; `resolve_sources`
  batches vault membership and sentence lookups per paper.

### 8.9 Node note sections (N3a, `concept_notes.NODE_SECTIONS`)

One schema serves the AI-card note and the no-card note. Headings are English, in this order. A section with no real data is
NOT written (no empty heading, no "chưa tìm thấy"/"Synthesis pending" bullet; an untraceable card statement is hidden too).
`concept_notes.node_headings(kind)` returns the list; `heading_problems(text)` (used by `kb.py selftest`) flags a heading that is
not approved or out of order. Types: `technique` uses the `method` set; any other unknown type uses `concept`. MOC/EVD/CLM/CONFLICT
notes: see 8.11. The auto block ends with `kb:auto:end`; `My notes` follows it (human zone).

| Type | Sections in order |
|---|---|
| concept | Definition · Key findings · Conflicting findings · Figures · Results · Related notes · Sources · My notes |
| method / technique | Purpose · Protocol steps · Reagents & concentrations · Equipment & parameters · Variants across papers · Outcomes · Conflicting findings · Figures · Results · Sources · My notes |
| substance | Definition · Concentrations used · Measured effects · Conflicting findings · Figures · Results · Sources · My notes |
| organism | Description · Key findings · Conflicting findings · Figures · Results · Sources · My notes |
| cell_type | Description · Markers · Key findings · Conflicting findings · Figures · Results · Sources · My notes |

Where the old sections went (each lands in exactly one section; "findings" = Key findings, Outcomes for method, Measured effects
for substance):

- Card fields per section are the tuples in `NODE_SECTIONS`. Definition/Purpose/Description take `definition, classification,
  substance_class, function_or_purpose, purpose_scope, scope, full_name, abbreviation` (+ `principle, readout, measures` for
  method; + `taxon_rank, common_name, strain_or_breed, model_use, usage` for organism; + `developmental_stage, in_taxon,
  derived_from` for cell_type). Findings take `key_points, importance, how_cited, cross_paper, cautions, limitations` (+
  `strengths`; organism `interspecies_cautions`). Protocol steps = `steps`; Reagents & concentrations = `materials,
  concentrations`; Equipment & parameters = `equipment, key_parameters`; Variants across papers = `variants, cross_paper,
  parent_technique`; Concentrations used = `concentrations_seen`; Markers = `markers`. Card fields not listed (`chem_roles,
  study_role, category, subclass_fields, related`) are not written to the body (`related` feeds Related notes and frontmatter).
- Without a card row in a section, text comes from the profile (N3d typed rules, below): Definition/Description = source
  `definitions` + `source_classes` sentences; Purpose = `definitions` + `purpose`; Markers = `markers`; concept Key findings has
  no lexical fallback; Protocol steps / Reagents / Equipment / Variants / Concentrations = the papers' `protocol_variants`.
- "Các bài nói gì" -> the per-paper table (paper / species-stage / finding / direction / type / source = `[[Paper|Author Year, p. N]]`) under the findings heading;
  hidden when there are no units. "Synthesis" sentences and "Candidate open questions" -> the top of the findings section (still
  stored as `<!-- kb-synthesis:… -->` rows; the status/pending line is no longer written). "Giống / khác giữa các bài": legend +
  the red `[!conflict]` callout + link -> Conflicting findings (only when an open conflict exists); its `cross_paper` card rows
  -> findings (Variants across papers for method). "Hình liên quan" -> Figures. "Bằng chứng đầy đủ" -> Sources (paper note
  links with sentence counts). `[!abstract] Tóm tắt` / `[!todo] Chưa có hồ sơ AI` summary callout -> dropped
  (Definition carries the card text; the stub was a lexical fragment). Frontmatter `kb_summary` is unchanged.
- Dropped as duplicates or unreliable: "Vai trò / source roles" and "Vai trò / dùng để làm gì" (same list twice, internal IDs);
  "Tên đầy đủ / viết tắt" fallback (expansions of other abbreviations; the card `full_name` is kept in Definition);
  "Phương pháp / source methods" and "Tác dụng đo được" for substances (paragraph dump / copy of the table);
  organism/cell_type fallback "Giai đoạn / Loài / Nguồn gốc / Bài sử dụng" (copy of the table's species/stage columns and unit IDs).
- Human zone: new notes end with `## My notes`. When the software splices an existing note, an empty `## Notes` is renamed to
  `## My notes`; a `## Notes` with text is left exactly as the researcher wrote it. `FORMAT_VERSION = 19` (19 = `## Results` (RL-10 A6) in node notes and, since 2026-10-21 (researcher "Có, liệt kê"), as the last section of topic (MOC) notes; no note was written at 19 before that: one line `- [[<EVD file stem>]] — <predicate>` (plus ` (negated)` when `negated: true` and ` (not significant)` when the significance is not significant and the predicate is not `no_significant_change`; evidence rule 4) per current result note (`type: result`, not retired) that links the node as a `subject`/`object` item, sorted by stem, read from `Results/` once per `concept-notes` run (a snapshot: it trails result notes refreshed later; the list is part of the note fingerprint), hidden when empty, written only inside the single kb:auto block, kept like the Source IDs callout by the 60 KiB capper (only its last-resort fallback drops it); pure stubs get none; 18 = machine text of node notes in English and no dashboard link in the note text, VN-08, wording in `presentation.TEXT_*`; 17 = N3d typed sections, 16 = MOC rewrite, 8.11) makes every node note
  re-render on the next `concept-notes` run.

**N3d - a section holds only its own kind of sentence** (deterministic; `node_profile.definitional / purposive / states_marker /
concentration_near`, `concept_notes._route_detail / _claim / finding_units`). An accepted card row is preferred; without one:

- Definition / Description / Purpose: a sentence outside Methods and Results where THIS node (name or alias) is the subject of
  `X is/are a|the|…`, `X refers to`, `X means`, `X, a …` (sentence-initial) or `… defines X as`; Purpose also `X is used to/for`, `the
  purpose of X`. A card row that rests only on result sentences of units about this node is a finding, not a definition.
- Markers (cell_type): `marker(s) of/for X`, `X marker(s)`, `X (cells) express…`, or `X … CD34+` - not "negative impact".
- Protocol steps: Methods-section sentences of the paper's protocol (profile `steps`), at most 5 per protocol, one bullet per sentence.
  Reagents & concentrations: protocol chemical/concentration/dose values; a gas mix (`CO2`/`O2`/`N2`) goes to Equipment & parameters,
  a bare amount ("0.5 μM") or "50% and 100% of…" is dropped. Equipment & parameters: equipment + duration/temperature/timing values
  that carry a number and unit (not "one month", dates, "day 5"). Variants across papers: the protocol's name (no machine text).
- Concentrations used (substance): a concentration/dose with THIS substance's name inside it or within 30 characters.
- Key findings / Outcomes / Measured effects: the per-paper table lists only accepted (`ok`/`human_confirmed`) units of type
  `result`/`conclusion`/`interpretation` whose `terms` (or intervention agent / outcome measure / model) name this node or a child.
- One sentence appears (quoted, or as a bullet) in one section: sections claim sentence IDs most specific first (Markers, Protocol
  steps, Reagents, Equipment, Concentrations, Variants, Purpose/Definition/Description, then findings); later sections drop rows whose
  sentences are all claimed and quote only unclaimed sentences. Value fragments (Reagents/Equipment/Concentrations) do not claim.
- Frontmatter `kb_summary` = the card definition, else the first definitional sentence about this node (<=160 characters, cut at a
  word boundary with `…`), else the key is omitted. Titles, figure headings and `kb_summary` pass through
  `presentation.clean_pdf_text` (`ml^-1` -> `ml-1`, `m^6A` -> `m6A`, citation carets `machinery^51,` removed; `10^6` and µ/μ are
  kept); quoted evidence is never changed.

### 8.10 Citations and machine information in notes (N3b, `presentation.cite*`)

A reader never sees an internal ID in a heading or in running text of a generated note (node, paper, EVD, CLM, CONFLICT).

- In-body citation = `[[Paper note|Author Year, p. N]]` (`presentation.cite(meta, pages, ids)`; `pp. 4, 5` for several pages; plain
  `Author Year, p. N` via `cite_label` / `link=False` inside the paper's own note; `Author Year` = first author's family name +
  year, `n.d.` when missing). `presentation.author_year` is the only formatter (`result_notes._citation` delegates to it).
  Node quote callouts read `> [!quote]- [[Paper|Author Year, p. N]] · section`; EVD/CLM/CONFLICT quote lines read
  `> **[[Paper|Author Year, p. N]]**` once per page run; paper-note unit rows read `> **[Result]** statement` with `— Results, p. N`.
- Figure / table headings: `### Figure 3 — <caption title sentence, <=70 chars> (Smith 2026)` (`presentation.object_title`);
  supplementary `Figure S3`, `Table S2`; objects without a label use `Formula (p. N)` / `Symbol region (p. N)`. In the paper's own
  note the `(Author Year)` suffix is omitted. Figure mention sentences are bullets ending in the citation (no "Câu nhắc tới hình:").
- Traceability = ONE collapsed callout per note, last in the auto block (before `kb:auto:end`):
  `> [!info]- Source IDs`, then one line per citation `> - Author Year, p. N: <paper_id>:S0123, <paper_id>:E0077, …`.
  IDs are always full (`paper_id:S0123` sentence, `:E0077` evidence unit, `:F3`/`:T2` object), so `neural.SOURCE_ID` still reads
  `source_ids` / `source_count` / `source_papers` of a note from its body, and `selftest` still checks that every `paper:E…` cited in a
  node exists. Built by `presentation.collect_source_ids()` (a context manager that every body builder runs inside) +
  `presentation.source_ids_callout(ids)`. The paper note lists unit IDs (a unit recovers its sentence IDs from `evidence.jsonl`) to stay
  inside the 120 KiB cap; node / EVD / CLM / CONFLICT notes list the sentence IDs too. Neural search excerpts skip this callout.
- Front matter keeps the machine-readable IDs (`id`, `sentence_ids`, `figure_ids`, `source_papers`, `paper_id`) and the `kb:auto`
  / `kb:concept-fingerprint` / `kb:synthesis-*` HTML comments (invisible in reading view; software state).
- Node Properties: researcher-facing keys stay top-level (`type, tags, aliases, kb_kind, kb_roles, kb_summary, evidence_count,
  source_papers, has_conflict, status` and the typed links); the machine-only keys `kb_category_basis, agent_type, papers_n,
  card_status, conflict_open` live under ONE nested key `kb_machine` (`presentation.fold_machine_keys`, idempotent, applied in
  `concept_notes._write_note`; no reader of the old flat keys existed). `tools/accept_revision.py` treats `kb_machine` as managed.
- `kb.py selftest` (`machine_ids`): any `paper:S/E/F/T…` ID or bare `S0123`/`E0077` in a heading or body of a note under node
  folders, `Papers/`, `Results/`, `Claims/` (outside front matter and the Source IDs callout) is an error;
  `presentation.machine_ids(text)` ignores a bare ID right after `Cat. No.` / `#` (reagent catalogue numbers in copied source text).
- Paper results table: `Evidence / page` column = `p. N`; the first cell's link alias pipe is escaped (`\|`) and
  `notes._paper_results_table` splits on unescaped pipes only; `result_notes._markdown_cell` escapes `|` as `\|` (was `/`).

## 9. Review policy and deduplication

- Flags exist only for missing connection to the vault topic: `topic_relevance` items (`detail` starts `[vault] `). Validator flags,
  rejected analyses and coverage gaps stay as evidence details (`evidence_quality_flags`, `processing_details`).
  `review_policy.json` v1 per paper: `vaults{vault: {connected, classification, basis, reason, connection|scope_terms}}`. Connection
  = a title or body source sentence matching the researcher's Home scope/aliases, a source-backed concept link, or a high-confidence
  citation to an in-vault anchor. No connection gives a computed `POTENTIAL_BRIDGE`; stored human classification is never
  overwritten and no paper is purged.
- Exact duplicate evidence (same paper, ordered source IDs, statement/type, structures, qualifiers, quality) is merged reversibly:
  dropped IDs go to canonical `aliases` and `evidence_aliases.json`; `evidence.resolve_unit(con, id)` resolves chains (rejects
  cycles, cross-paper aliases); direct E-ID consumers must use it. Identical incoming PDFs are quarantined recoverably and listed in
  `metadata.duplicate_sources`; a paper in two vaults is not a duplicate. Ingest duplicate order: DOI, PMID, normalised title+year,
  file hash; a match merges source info.
- `kb.py refresh-review-policy --vault V [--dry-run|--apply]` (default dry-run). Apply holds the writer lock, saves SQLite, review
  rows and per-paper machine records to `knowledge_store/review_policy_history/<UUID>/`, retires legacy pending machine review rows
  and moves only byte-exact generated review notes. Recoverable, not atomic across all files.

## 10. MCP server (`kb.py mcp`, stdio JSON-RPC 2.0, one message per line, no network listener)

Server `biomedical-kb`; protocols 2025-06-18, 2025-03-26, 2024-11-05; standard library only. Unknown tool or missing arguments give
error -32602; non-object messages -32600. Arguments are checked in `mcp_server._check_args` before any tool runs; a failure is a tool error (`isError`): `paper_id` must match `find.PAPER_ID`, contain no `..` and be a row of the `papers` table (no path outside `knowledge_store/papers`); `vault`, when given, must be in `config.research_vaults()` (`Unknown vault`); `limit` must be an integer and is clamped to 1..100 (`read_paper`: 1..500). `clientInfo.name` is recorded: when an agent does a `get_ai_task`/`submit_ai_task`/`ask_*`
task it is the provider (`provider: mcp_agent`, model `"<client> / <declared model>"`, V13 verifier `mcp:<client> / <model>`).

| Group | Tools |
|---|---|
| Analysis | `get_analysis_guide`, `list_pending_papers(vault?)`, `quality_check`, `read_paper(paper_id, section?, start?, limit?, format json|compact)`, `lookup_reference`, `get_context(paper_id, sentence_id, window=3)`, `submit_analysis`, `add_units`, `skip_sentences`, `remove_units`, `draft_status`, `finish_analysis`, `analysis_status`, `get_evidence`, `search_sentences` |
| Structured | `get_section_profile`, `submit_section_profile`, `get_protocol_profile`, `submit_protocol_profile`, `structured_prepare(kind overview|wh|protocol)`, `structured_submit`, `structured_status`, `structured_run(allow_ai)`, `compare_protocols(vault, term?)` |
| Media / cards | `build_media`, `media_status`, `get_node_card_task`, `submit_node_card`, `list_pending_figures(vault, include_formulas?)`, `get_figure_task(paper_id, object_id)`, `submit_figure_reading(paper_id, object_id, reply, analyst?)` (under the library lock), `get_figure(vault, paper_id, object_id)`, `get_pdf_page(vault, paper_id, page, dpi?)` (read-only, NW-12, below) |
| Retrieval / answers | `neural_search(vault, query, hops?=0)`, `neural_walk(vault, node, limit?)` (read-only: the knowledge steps of one named node, exact title, alias or vault path, several matches refused; each step with `hub`, `etypes`, `unit_id` and the stored sentences copied by code; `limit` default 20, cap 100; returns `{found, node, total, truncated, steps}`), `neural_path(vault, source, target)` (read-only: one shortest path of at most NW-05 `max_steps` knowledge steps between two named nodes, `{found, steps, nodes[{path, title, hub}]}`; no path or an unknown name = `found: false` with `message` 'không tìm thấy trong kho hiện tại'; a name that resolves only to a non-node note (Results, claim, conflict, paper, topic map) or source == target after resolving = tool error, never not found; `neural_walk` refuses a non-node name the same way; §4.3), `read_vault_note(vault, path, sha256?)`, `neural_prepare(vault, question, hops?=0, offset?, session?)` (one NW-01 page; items carry `item_type`, `note_path`, `obsidian_url`, §4.3; `session` `"new"` or a session id adds a round, §4.3 research session), `neural_check(ticket | session, reply)` (only `reply` required; a session id checks against the union of all its rounds by session number), `ask_prepare` (returns `items_digest`, `not_found`, `links` NW-12 and, when a decision note matches, `decisions`; adds no session round), `ask_check(..., items_digest?, session?)` (§4.2; with `session`: `neural.check` against the union read from the session file, same return keys, `items_digest` = digest of the union, other vault = tool error), `list_sessions(vault)` (open sessions newest first `[{session, topic (round-1 question), questions, rounds, item_count, created, last_used}]`), `open_session(session, numbers?, offset?)` (`{session, vault, topic, rounds, item_count}` plus, without numbers, `offset, index[{n, paper_id, page, line (<= 100 chars)}], has_more, next_offset` within NW-01 `token_budget_fast`; with numbers `items` (full, with `n`), `papers`, `not_returned` (left out by `max_payload_kb`)), `close_session(session)` (`{session, closed: true}`; removes the session files, every later use = tool error); args: `session` 32 hex (`"new"` only for neural_prepare), `numbers` list of int, `offset` int >= 0, anything else = tool error, `find_overlaps`, `list_conflicts(vault)` (unknown vault = tool error), `get_answer_rules` (text of `config/answer_protocol.md`; `ask_prepare` also returns it as `answer_protocol`), `save_discussion(vault, title, markdown, model?, kind discussion|knowledge_gap|decision, researcher_words?, citations?, earlier?)` (`researcher_words` string, `citations` list of strings, `earlier` string: kind decision only, any other kind with them = tool error, see Decision notes; research vaults only; title NFC and folded to one line; under the library lock (also through `kb.py tool`); writes `Discussions/<title>.md` create-only (`open(path, "x")`, a taken name gets ` (n)`), frontmatter `type: discussion|knowledge_gap`, tag `discussion|knowledge-gap`, `evidence: false`; never overwrites; neural kind `discussion`) |
| Gap candidates | `list_gap_candidates(vault)` (open rows of exactly that vault; unknown vault = tool error), `propose_gap(vault, description, supporting_units, gap_type, queries_run?, papers_scanned?)` (library lock; every unit must be an accepted unit of the vault; same text = same gap; `needs_review`, provenance `analyst`) |
| Other AI work | `list_ai_tasks(vault, kind synthesis|enrichment|crosscheck)`, `get_ai_task`, `submit_ai_task` |
| State | `workflow_status` |

Brainstorm (U10, AN-20, NW-11): `neural_prepare` `index` (bool, needs a session; a plain ticket = tool error): the round is computed, saved and numbered exactly as without it; the reply keeps session/round/new_items/slots/coverage/stop_reason/decisions/needs_translation/english_query/total/offset/next_offset/note_refs, drops items/papers/media/prompt/vault_context and adds `index[{n, paper_id, page, unit_type, item_type, line (<= 100 chars)}]`, `context_notes[{path, title, item_type, obsidian_url, line}]`, `edge_count`, `links{paper_id: {note_path, obsidian_url}}`, `not_indexed[n]`, index + head within NW-01 `token_budget_fast` * 4 bytes (`neural.index_page`, also used by `open_session`, whose entries now carry `unit_type`, `item_type`). Session file field `opened` (item numbers received in full: every item of a full-mode round and every number `open_session(numbers)` returned; absent = all opened; outside the round fingerprints). Checker warning `A12` (never a strike, Part 2 only, `answer.check_sentence(part2, unopened)`): a sentence cites an item the session never returned in full. `ask_prepare` + `index` (head: items_found, items_digest, not_found, not_found_message, overview, scope_note, decisions, answer_protocol, plus `index`, `has_more`, `next_offset`; no prompts or links), + `numbers` (head + `items[{..., n}]`, `papers`, `not_returned`, `links` of those numbers; no prompt), + `offset`; `items_digest` is the same in every mode. `get_answer_rules(vault?)` -> `{protocol, memory?}`; `memory{vault, label_researcher, label_ai, note, profile[], topics[]}` only when the server runs with `--memory` and a vault is given. `remember(vault, text, by researcher|ai, scope profile|topic)` -> `{file, line, lines_now, cap}`, appends `- YYYY-MM-DD · bạn nói|AI ghi: <text>` to `runtime/brainstorm/profile.md` (shared) or `runtime/brainstorm/<vault>/topics.md`; text NFC (CHR-01); refused without `--memory`, for a non-research vault, non-string or empty text, text over AN-20 `max_line_chars`, `by ai` with `scope profile` (the profile holds only the researcher's words), a file at its line cap; no writer lock. Toolset: `kb.py mcp --tools all|brainstorm [--memory]`; with brainstorm, tools/list shows only `mcp_server.BRAINSTORM_TOOLS` and any other tool is "Unknown tool". `kb.py mcp-config [--install X] [--tools brainstorm]` (printed snippets and --install carry the toolset) writes `--memory` only for claude-desktop and codex (never gemini, cursor, the generic snippet, Hermes or the claude-code command). CLI bridge `kb.py tool NAME [--args FILE|-] [--out FILE] [--all]`: one tools/call through `mcp_server.handle` (client `claude-code`, memory on, brainstorm toolset unless `--all`); full text to `--out` (default `runtime/brainstorm/last_<NAME>.json`); prints it when <= 4000 chars, else `{full, chars, keys, preview (2500)}`; an error prints the message and exits 1; image bytes are not printed. Answer page (U10b): the MCP `ask_check` / `neural_check` results (also through `kb.py tool`) add `html_path` (absolute path) and `html_url` (`Path.as_uri()`) of a local HTML page of the checked four-part answer, only for the evidence4 shape; a failed page (write or render) gives `html_error` and leaves the check result unchanged. The page is written only by the MCP dispatch (`mcp_server._with_html` -> `answer.write_html`), never by `answer.check_parts`, `neural.check` or `agent_tasks.answer_check` (web, gold gate, conform, tests write none). Files `output/answers/<YYYYmmdd-HHMMSS>-<slug>.html` (slug <= 40 chars; gitignored, deletable, create-only, a taken name gets a numeric suffix). Quotes come verbatim from `checked["evidence"]`; a long quote is folded after whole sentences only when the hidden rest has no hedge (`hedges.uncertainty`) or negation; every text is HTML-escaped; only `obsidian://` hrefs are written. Markdown Part 1 lines are `> [#n] "quote" — citation`.

`read_paper` rows: `{id, section, page, kind, text, cites?, in_table?, continues?, must_cover?, source, subsection?, object_id?,
table_quality?}` plus `n_must_cover`, `next_start`; `compact` replaces `sentences` by `reading`.
`get_figure_task` returns the `figure_reading.task` JSON (with `crop_path`) as text plus an MCP `image` content block (base64, `mimeType`)
when the crop is <= NW-12 `max_image_mb`. `get_figure(vault, paper_id, object_id)` (read-only; `object_id` local `F1` or `<paper_id>:F1`, another paper's prefix, `/`, `\`, `..` or empty = tool error; paper outside the vault or unknown object = tool error) returns a JSON text block `{paper_id, object_id, label, kind, page, caption[{sentence_id, text}] (copied by code), table_rows[{sentence_id, text}], note_path, obsidian_url (+ `note_missing`), image_note, figure_reading ({summary, status, stale, stale_reasons, analyst} as stored, never re-read) | null, reading_inconsistent, pixel_numbers}` plus an MCP `image` block (the crop; `mimeType` image/jpeg for JPEG bytes, else image/png) only when `figure_reading._verified_crop` accepts the crop and it is <= NW-12 `max_image_mb` (4); otherwise text only and `image_note` says why (hash mismatch, no crop, file vanished, over the cap). `get_pdf_page(vault, paper_id, page, dpi?)` (read-only; `page` int >= 1, `dpi` int 50..200, default 110, bool/string/float = tool error) renders ONE page of the main PDF (`find.main_pdf_path`, opened read-only with PyMuPDF, nothing written) as an MCP `image` block plus text `{paper_id, page, page_count, dpi, note_path, obsidian_url, image_note, pixel_numbers}`; a PNG over the cap is re-rendered at dpi x 0.7 down to 50, then text only with `image_note` 'too large'; no/missing/unopenable/password-protected source = text `image_note` 'source file unavailable ...' (not an error); page outside 1..page_count = tool error naming the page count. One shared path (`mcp_server.IMAGE_KEYS`, `_image_content(path|bytes)`) turns the image into the block; PNG bytes never enter the JSON text. Provider routes: `ai.tasks.figure_reading` / `ai.tasks.node_card` (string or `{provider, model}`); when absent
they fall back to `ai.tasks.analysis`, then `ai.provider` (`answer.settings(task)`). `analysis_transport.call` sends images (OpenAI-style
`image_url` data-URL part) to gemini, openai_compatible, openrouter and ollama (its `/v1` endpoint); anthropic and codex_cli have their own formats.

## 11. Local web page (`scripts/web.py [--port 8765] [--no-browser] [--check] [--prepare] [--no-prepare]`)

Binds `127.0.0.1` only (next free port up to +19), refuses a `Host` other than `127.0.0.1:<port>`/`localhost:<port>`, every `/api/*`
needs the `X-KB-Token` written into the page; CSP-safe pages; database opened per request. Pages: `/` (search), `/library`,
`/neural`, `/graph`, `/pdf/<paper_id>#page=N`, `/crop/<paper_id>/crops/<file>`. GET `/api/`: `status, library, job, graph, search,
paper, ref, context, neural/search, neural/note, research/{overview,results,sources,node-profile,media,crop}, structure, protocols,
workflow`. POST: `translate, answer, neural/{answer,prepare,check}, overlap, upload` (raw bytes; .pdf/.zip/.xlsx/.xlsm/.csv; at most
300 MB; into `sources/inbox/batches/<vault>_<date>/`), `vault/{create,select}`, `job/start` (ingest|analyze|connect or one of
the seven stages; one job at a time), `discover-citations`, `stage-discovery`. Release step 5: `POST /api/paper/delete`
(Library button "Xóa bài"): body `{"paper_id": "<id>"}` (string matching `find.PAPER_ID`, else 400 "Mã bài không hợp lệ"); same Host
and `X-KB-Token` guard as every `/api` POST (403); takes `workflow.writer_lock()` with no wait (held by another writer: 409, Vietnamese
text, nothing moved); a Library job is running (`library.job_status()`, checked under the lock): 409, nothing moved; unknown id: 400 "Không có bài này trong thư viện"; otherwise calls `manage.delete_paper` (reversible move of the
paper folder, notes, managed PDFs, review files and embeddings to `knowledge_store/removed/deleted_<id>_<time>/` with `manifest.json`;
`Mine/` is not a target) and returns `{paper_id, title, files_moved, moved_to}`. At start the page runs `prepare` in the background
(downloads MeSH and the model once, then embeds new papers). Primary result browsing excludes reviews; `source` browsing shows
review Results with secondary labels; counts are stored object counts, not completeness.

## 12. Configuration keys

- `config/ai.yaml` (`ai:`): `provider` (`none|mcp_agent|ollama|anthropic|gemini|deepseek|openrouter|openai_compatible|codex_cli|
  command`), per-provider `model/base_url/reasoning`, `command.run` (argv, prompt on stdin), `tasks.{answer,analysis,synthesis,
  enrichment,crosscheck}` (provider name or `{provider, model}`; V13 unset uses Codex `gpt-5.6-terra`), `provider_timeouts`,
  `fast.max_items`, `deep.{max_items,queries,…}`, `concept_notes.language`. Keys only in `config/secrets.yaml` (git-ignored,
  `<provider>_api_key`). `codex_cli` runs `codex exec --ephemeral --ignore-user-config -s read-only` in an empty temp folder with the
  prompt on stdin, uses the account logged in at that moment and retries once with its default model if refused. Provider calls
  time out and kill the whole process tree. Images go only to `codex_cli`, `anthropic`, `gemini`, `openai_compatible`.
- `config/ingest.yaml`: `ingest.{default_vault, inbox_folder, active_folder, online_metadata}`, `passages`, `search`,
  `references.online_lookup`, `ocr.{enabled, min_page_chars, dpi, low_confidence}`, `analysis.{coverage_policy: full_sections,
  require_structured_profiles, max_attempts}`. `config/system.yaml` (`processing.low_memory_mode`, `max_concurrent_papers: 2`,
  review/vault policy), `retrieval.yaml`, `vaults.yaml`, `synonyms.yaml`, `word_variants.yaml`,
  `hedge_lexicon.yaml`, `homonyms.yaml`, `node_card_schema.yaml`, `obsidian_plugins.yaml`.

## 13. Labels shown to users

`result` "Kết quả (bài này)"; `cited_statement` "Bài này trích lại [n]"; `interpretation`/`hypothesis` "Tác giả diễn giải / giả thuyết";
`method_detail` "Phương pháp"; `conclusion` "Kết luận tác giả"; `limitation` "Hạn chế"; `background` "Kiến thức nền"; heuristic "nháp tự động".

### 8.11 EVD / CLM / CONFLICT / MOC sections (N3c, `result_notes.SECTIONS`, `concept_notes.MOC_SECTIONS`)

English H2 sets, in this order; a section without data is NOT written (`result_notes.heading_problems`, `concept_notes.
heading_problems(text, "moc")`; `kb.py selftest` flags unapproved / misordered headings). `My notes` follows `kb:auto:end`.

| Type | Sections in order |
|---|---|
| result (EVD) | Finding · Context (Species, Stage, Method, Protocol, Groups - only the filled ones) · Data (group table, n / p / Test / Significance, related figure embeds) · Source sentences · Related claims / conflicts |
| claim (CLM) | Claim (statement, "Entities:" node links) · Supporting evidence · Opposing evidence · Source sentences |
| conflict | Summary (measure, A / B citation + direction, linked evidence) · Side-by-side comparison (rows nobody reports are left out, a one-sided gap reads `—`; several relations of one paper pair + measure = `### Comparison N`) · What differs (the relation's `differences`, else the A/B rows that differ) · Sources (`### A - Author Year` / `### B - ...` quotes) |
| moc (topic hub) | Overview (topic counts) · Member notes · Key papers · Open conflicts · Results (RL-10 A6, same lines and cap rule as node notes) |

- Titles: `result_notes._short(text, limit)` ends at a clause (`, ; : .` in the second half) or word boundary, drops trailing function
  words and open brackets and appends `…` only when the text was cut. A changed title renames the existing EVD / CLM / CONFLICT note
  (`result_notes._relocate`, undo map in `knowledge_store/migrations/<stamp>_<vault>_result_notes.json`, H1 follows the file name;
  a move that cannot happen keeps the old file, retried at the next refresh; a filled `reviewed_by`/`reviewed_on` is kept);
  uniqueness still comes from the ` (2)` suffix of `_path_for`.
- CLM `supports` / `derived_from`: the EVD notes that share the claim's own (or cited) sentences. `opposes`: the other end of a LIVE
  (`ai_proposed|proposed|human_confirmed`) conflict relation on the SAME measure, reached through a supporting EVD; dismissed,
  rejected, stale and withdrawn relations never create it.
- CONFLICT frontmatter adds `papers` (`Author Year vs Author Year`) and `measure`; `_Maps/Open conflicts.md` groups CONFLICT notes by
  `papers` (heading `## A vs B (n)`, line `[[stem|title]] severity`); other notes with `has_conflict` follow under "Notes that list
  conflicting findings" by kind. The internal `axis` is frontmatter only. The Base filter `status == "open"` is unchanged.
- MOC members (`concept_notes._moc_members`): registry children / descendants of the topic plus registry concepts named in the topic's
  OWN evidence sentences (`units`), never in every sentence of its linked papers; only concepts with an existing note count. Groups:
  Concepts, Methods, Substances, Organisms, Cell types, Topics (`MOC_GROUPS`). `kb_summary` and Overview use the same counts. Key
  papers = `[[Paper|Author Year]] - N sentences` (most mentions first); Open conflicts = `[[CONFLICT - ...|title]]`.
- Paper-note anchors: `notes.PAPER_RESULTS_HEADING`, `PAPER_FIGURES_HEADING`, `PAPER_STRUCTURE_HEADING` are the H2 names; links
  `[[Paper#...]]` use these constants. `notes.separate_callouts` also ends a quote before a plain text line (lazy continuation).

## 14. Analysis Standard (`config/standard.yaml`, G2)

One source of every analysis rule: `config/standard.yaml` (`version`, `status draft|frozen`, `stages{id: {en, vi}}`,
`clauses[]`, `open_decisions[]`). Clause: `id` (`<stage>-<nn>`, stage in IN CHR CH EV SP FG ND RL NC VN RT AN NW RQ SF), `rule` (English),
`vi` (plain Vietnamese), `pass` / `fail` (measurable), `level` (`fail` blocks, `warn` reports), `enforced_by` (`module.func` under
`kbsys` or a script module (since G3: the `conform_*` check of the clause when no product function enforces it), or `G3` = no check yet, reported `not_implemented`), `audit` (refs), optional `params`. Word lists stay in `config/*.yaml` and are
referenced by path. `docs/STANDARD.md` and `docs/STANDARD_vi.md` are GENERATED by `kb.py standard --write-docs`
(`kbsys/standard.py`); `kb.py standard` exits 1 on a schema problem, an unresolvable `enforced_by` or docs out of sync, and
`tests/test_standard.py` fails in the same cases. Code reads the standard from G3 on; G2 changed no behaviour.
Releases: clause text must not name the private progress files (`package.release` deletes whole lines that do, which would put the shipped
docs out of sync) and `enforced_by` must name a module that a release ships (not `benchmark_research`); `test_standard` checks both.

### 14.1 Conformance checker (`kb.py conform`, `kbsys/conform.py`, G3)

`kb.py conform [--stage S] [--paper P] [--vault V] [--json] [--out file.json]` runs every clause of the standard against the data
and the code, read-only, one paper at a time. Statuses per clause: `PASS`; `FAIL` (clause level `fail`) or `WARN` (level `warn`) when a
check returns violations; `ERROR` when the check crashed (never read as PASS); `not_implemented` when no check is registered for the
clause (never silently skipped, never PASS); `no_data` when the filters left nothing for its scope. Exit 1 when any clause is FAIL or ERROR.
JSON: `{version, status, summary{status: n}, clauses[{id, stage, level, status, violations, examples[<=5], where{paper|vault: n}, error?}]}`.
A check is `@conform.check("<clause id>", "paper|vault|store|probe")` on a function that returns/yields one string per violation
(scope `paper` receives `conform.Paper`, `vault` a `Vault`, `store` a `Store`, `probe` nothing and judges built-in synthetic cases
through the product function, which it takes as an optional argument so a test can hand it a broken one). Check modules are listed
in `conform.MODULES`. Thresholds are read with `standard.param(clause, key)` from the clause `params` and never written in code.
`kb.py selftest` ends with a conform section (clauses whose check only re-uses a selftest function are skipped there): failing clauses
are warnings, errors with `--strict-conform`. Tests: `tests/standard/` (`cases_*.py`: a PASS and a FAIL case per check; `RED` lists clauses
whose product code is known to be wrong until G4).
`scripts/accept_requirements.py --vault V --conform` appends one row `STD-<stage>` per stage (PASS: all clauses PASS; PARTIAL: no failure
but a WARN, not_implemented or no_data clause; FAIL: a FAIL/ERROR clause) from `kb.py conform`; without the flag the matrix is unchanged (the run is slow).

## 15. Release list (`package.RELEASE_NEVER`, `package.release`, `kb.py release DEST`)

The release root has exactly two double-click files: `1 - Mo trang chinh.bat` and `2 - Ket noi AI (MCP).bat` (any other root `.bat` is dropped).

| List | Content |
|---|---|
| KEEP | `scripts/**`, `config/**`, `tools/research_workspace.py`, `tests/**` (synthetic data only), the two `.bat`, `README.md`, `HUONG_DAN.md`, `AGENTS.md` (dev section replaced by `package.RELEASE_TAIL`, a table of shipped docs), `docs/` ARCHITECTURE, CONTRACTS, AGENT_TOOLS, AI_ANALYSIS_GUIDE, STANDARD, STANDARD_vi, `requirements.txt`, `.gitignore`, `.gitattributes`, `.editorconfig` |
| never | `.claude/` (whole), `CLAUDE.md`, `scripts/ui.py`, `scripts/accept_requirements.py`, `scripts/benchmark_research.py`, `tools/run.py`, `tools/verify.py`, `tools/accept_revision.py`, their tests (`test_accept_requirements`, `test_research_benchmark`, `test_review_gate`, `test_accept_revision`), `docs/` STATUS, DECISIONS, PLAN, REQUIREMENTS, AUDIT_*, `status/`, `plan/`, `rag_benchmark/`, `source_requirements/`, `demo3/`, `projects/`, the library (`knowledge_store`, `vaults`, `sources`, `review`), keys, caches |

`release` writes `DEST/.release_files` (the shipped names; a later release removes what is no longer listed) and returns every private trace
it finds (`leaks`: machine path, personal email/name, library paper id and its author+year stem (`smith2020`, `smith_2020`), DOI, title,
"Author Year", vault name, and library sentence text: 45 or more characters of a shipped text file (adjacent string literals joined)
equal, after whitespace normalisation, NFC and casefold, a part of a sentence in `knowledge_store/papers/*/sentences.jsonl`; the
hashes of the shipped windows are held in memory and each paper's file is read line by line against them, so memory follows the
software size, not the library; the hit names file, line and length, never the text; a copy without a store skips this check); the goal is 0. The machine-path folders (drive `Users` and the top folders of the project's drive) and the personal name (home
folder / `USERNAME`, 4+ letters) are read at run time, so the shipped code names neither. Tests that need a dropped file skip when it
is absent (`tests/standard/test_network.py`, `test_clauses.py` NW-01/02/09, `test_evidence.py` EV-22). Test: `tests/test_release.py`.
