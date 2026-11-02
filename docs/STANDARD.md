<!-- GENERATED from config/standard.yaml by `python scripts/kb.py standard --write-docs`. Do not edit by hand. -->

# Analysis Standard v1.4 (frozen)

One source of rules for every AI and every program of this project. Each clause has a measurable PASS and FAIL condition. `fail` clauses block, `warn` clauses are reported. `enforced_by` names the function that implements the clause (it may check only part of the PASS/FAIL condition until G3); `G3` means no code checks it yet.

| Stage | Name | Clauses | Blocking (fail) | Enforced by code |
|---|---|---|---|---|
| IN | Intake and duplicates | 10 | 7 | 9 |
| CHR | Characters and encoding | 8 | 7 | 7 |
| CH | Chunking: sentences, passages, tables, sections | 12 | 11 | 12 |
| EV | Evidence units (AI) | 23 | 17 | 23 |
| SP | Structured paper profiles | 9 | 8 | 9 |
| FG | Figure and table reading | 5 | 4 | 5 |
| ND | Concepts and nodes | 10 | 9 | 10 |
| RL | Relations, conflicts, typed network | 11 | 9 | 11 |
| NC | Node cards (AI) | 5 | 3 | 5 |
| VN | Vault notes | 10 | 8 | 10 |
| RT | Retrieval | 12 | 7 | 12 |
| AN | Answers and the answer checker | 20 | 16 | 19 |
| NW | Neural network route | 12 | 9 | 9 |
| RQ | Review queue and human decisions | 7 | 6 | 6 |
| SF | Safety and privacy | 7 | 7 | 5 |
| **Total** | | **161** | **128** | **152** |

## IN: Intake and duplicates

### IN-01 (fail) enforced by `ingest.find_existing`

Before ingesting a paper, look for an existing record in this order: DOI, PMID, normalised title plus year, file hash; a match merges missing source information and never creates a second record.

- PASS: Re-ingesting the same paper (any one of the four keys equal) leaves the paper count unchanged and fills only empty source fields.
- FAIL: A second paper record exists for a paper whose DOI, PMID, normalised title+year or file hash equals an existing one.
- Audit: AGENTS-duplicates

### IN-02 (fail) enforced by `ingest._attach_paper_to_vault`

A paper that belongs to two vaults is one canonical record, never a duplicate.

- PASS: paper_vaults lists the same paper_id under each vault and papers has one row for it.
- FAIL: The same publication exists under two paper_ids because it is in two vaults.
- Audit: AGENTS-duplicates

### IN-03 (fail) enforced by `conform_intake.sources_unchanged`

Original PDFs and supplementary files are immutable: their SHA-256 never changes after ingest, and none is deleted without the researcher's approval.

- PASS: The stored SHA-256 of every source file equals the file on disk (or the file is recorded as archived/unavailable).
- FAIL: A source file hash differs from the recorded one, or a source file was removed without a recorded approval.
- Audit: AGENTS-sources

### IN-04 (warn) enforced by `G3`

A batch is processed one paper at a time; no step loads the whole batch or the whole knowledge base into memory.

- PASS: Ingest, analysis, rebuild and benchmark loops read one paper folder per iteration.
- FAIL: A step reads every sentences.jsonl at once, or its peak memory grows with the library size.
- Audit: AGENTS-batch
- Params: `{machine_ram_gb: 8}`

### IN-05 (fail) enforced by `selftest.check_paper`

Identifiers follow CONTRACTS section 1: paper make_paper_id, sentence <paper_id>:S<seq>, evidence unit <paper_id>:E<seq>, figure :F<n>, table :T<n>; sequence numbers follow reading order and are never reused.

- PASS: Every ID matches its pattern and the sentence seq runs 1..n without gaps.
- FAIL: An ID does not match its pattern, is reused, or a seq gap exists.
- Audit: CONTRACTS-1, I2

### IN-06 (warn) enforced by `conform_intake.passages_only_says_so`

When the PDF is unreachable, sentences are rebuilt from stored passages, the record says so (tier1.from = passages_only) and original-file verification is reported as unavailable.

- PASS: metadata.tier1.from is passages_only and the verification request answers 'original unavailable'.
- FAIL: A verification against the original is claimed for a paper whose PDF is unreachable.
- Audit: AGENTS-sources

### IN-07 (warn) enforced by `conform_intake.sources_can_be_located`

A source file may be local, archived, managed by a reference manager or unavailable; the record keeps enough metadata and provenance (path, SHA-256, status, reference-manager key) to locate or restore it.

- PASS: Every source_files entry has a path or an archived/unavailable status plus a SHA-256.
- FAIL: A source entry has neither a reachable path nor a recorded status, or no SHA-256.
- Audit: AGENTS-storage

### IN-08 (fail) enforced by `ghosts.ghost_id`

A cited reference not in the library becomes a ghost paper only when it has a DOI (ghost:doi:<lowercase doi>) or a title plus year (ghost:t:<sha1 of normalised title and year, 12 hex>); with neither it makes no ghost.

- PASS: ghost_id returns None for a reference with no DOI and no title+year, and the documented ID forms otherwise.
- FAIL: A ghost is created from a reference with no DOI and no title+year, or its ID form differs.
- Audit: CONTRACTS-6

### IN-09 (fail) enforced by `tier1.attach_supplement`

A supplementary file is copied, never moved or modified, its SHA-256 is verified after the copy, and in a batch only a unique match (DOI, then unique parent filename) is attached.

- PASS: The copy's SHA-256 equals the original's, the original is unchanged, and an ambiguous batch match is not attached.
- FAIL: A supplement is moved or altered, a mismatching copy is kept, or an ambiguous match is attached.
- Audit: CONTRACTS-2.5

### IN-10 (fail) enforced by `ingest._quarantine`

An identical incoming PDF (same SHA-256) joins the canonical paper and the extra copy is quarantined recoverably, never deleted.

- PASS: The extra copy is found in the review quarantine folder and the paper count is unchanged.
- FAIL: An identical PDF creates a second paper or is deleted.
- Audit: CONTRACTS-9, AGENTS-duplicates

## CHR: Characters and encoding

### CHR-01 (fail) enforced by `conform_intake.all_text_is_nfc`

All stored and generated text is Unicode NFC; text in other forms is normalised on input and never bypasses a check.

- PASS: unicodedata.normalize('NFC', s) == s for every stored sentence, note and answer; an NFD answer is judged like its NFC form.
- FAIL: A stored text or an answer differs from its NFC form, or an NFD answer passes a check that the NFC form fails.
- Audit: N03

### CHR-02 (fail) enforced by `quality.inspect`

Stored sentences contain no replacement character U+FFFD, private-use character or control character (including backspace 0x08) other than newline, carriage return and tab.

- PASS: A scan of sentences.jsonl finds 0 such characters.
- FAIL: At least one such character is found in a stored sentence.
- Audit: P4-14, A-chars

### CHR-03 (fail) enforced by `conform_intake.only_lf`

Text files are written with LF only (.bat files keep CRLF); a CRLF received from a client is converted before saving.

- PASS: No stored .md/.json/.jsonl/.yaml/.py file contains a carriage return.
- FAIL: A carriage return is found in a generated note or store file.
- Audit: CONTRACTS-0.6

### CHR-04 (warn) enforced by `sentences.fold_search_glyphs`

Look-alike symbols are matched as one for search and display (delta forms, degree/ring forms such as C, degree-C), while the stored source text stays as extracted.

- PASS: A query using one delta or degree form finds sentences that use the other, and the stored text is unchanged.
- FAIL: The search misses a sentence because of a delta/degree variant, or the stored text was rewritten (today the code folds only the degree forms, not delta).
- Audit: A-E, P2-8

### CHR-05 (fail) enforced by `G3`

A character problem that can change a number (lost times sign, lost P, 10?5, decimal comma read as ^0) is checked against the PDF or flagged needs_pdf_check; the number is never guessed.

- PASS: Every sentence whose number depends on a repaired or suspect character is either PDF-verified or carries needs_pdf_check.
- FAIL: A suspect number-bearing sentence has neither a PDF check nor the flag, or the code guessed the value.
- Audit: P4-19, A-chars

### CHR-06 (fail) enforced by `selftest.check_paper`

Only verified font-glyph repairs are applied (checked against the immutable PDF); an unverified symbol-font code stays unchanged and raises selftest warning FS2.

- PASS: Every repair is in the verified map; unmapped symbol-font codes appear as FS2 warnings.
- FAIL: A repair outside the verified map is applied.
- Audit: CONTRACTS-2.0

### CHR-07 (fail) enforced by `sentences.find_citations`

A superscript run is stored as ^run, and the citation reader tells a citation (stroke.^1,7-10) from a number or unit (10^5, cm^2, SSEA-3^+).

- PASS: On the fixture set, citation superscripts are read as citations and 10^5, cm^2, SSEA-3^+ are not.
- FAIL: A numeric or unit superscript is read as a citation, or a citation superscript is lost.
- Audit: CONTRACTS-2.0

### CHR-08 (fail) enforced by `selftest.check_paper`

A page read by OCR (fewer than ocr.min_page_chars characters and an image) records its provenance (engine, dpi, mean confidence) and is listed in metadata.ocr_pages; numbers read by OCR need PDF verification.

- PASS: Every OCR passage lies on a recorded OCR page with an engine, and low-confidence OCR blocks analysis.
- FAIL: An OCR passage has no recorded page or engine, or low-confidence OCR text is analysed.
- Audit: CONTRACTS-2.0, CONTRACTS-2.7

## CH: Chunking: sentences, passages, tables, sections

### CH-01 (fail) enforced by `sentences.split_sentences`

Sentence splitting never breaks at et al., Fig., e.g., i.e., vs., cf., initials or decimals, nor before a lower-case word.

- PASS: The parser examples of kb.py selftest all pass and the fixture error rate is 0.
- FAIL: A split occurs at one of the listed positions.
- Audit: CONTRACTS-2.2

### CH-02 (fail) enforced by `conform_chunk.no_orphan_fragments`

No orphan fragment: a stored sentence that starts lower-case or ends cut off must carry a correct continues link, otherwise it is a defect.

- PASS: Count of unlinked head or tail fragments = 0 on the fixtures and is reported per paper on the real store.
- FAIL: A head fragment (13 known) or tail fragment (22 known; 35 in all) without a correct continues link exists.
- Audit: A-chunking, P4-15

### CH-03 (fail) enforced by `conform_chunk.continues_links_are_real`

A continues link joins the real two halves of one sentence (same flow of text across a page, column or figure break) and never a body sentence to a table cell, heading or an unrelated sentence.

- PASS: For every link, the head ends without terminal punctuation and the target continues it; no link target is a table_row or heading.
- FAIL: A link joins unrelated sentences (15 known, e.g. a link that skips the true next part) or targets a table cell or heading.
- Audit: A-chunking, P4-15

### CH-04 (fail) enforced by `conform_chunk.passages_end_at_a_sentence_boundary`

A passage is never cut in the middle of a sentence; the flush at about 1000 characters waits for a sentence end.

- PASS: Every passage ends at a sentence boundary or a continues link is stored.
- FAIL: A passage ends mid-sentence without a link.
- Audit: P4-16

### CH-05 (fail) enforced by `conform_chunk.tables_apart_from_running_text`

Table text is separated from running text: table rows are table_row sentences, flattened table text is marked in_table, and a caption or table cell is never glued to a body sentence.

- PASS: No body sentence contains a caption or cell fragment, and every flattened table is flagged in_table (also 0-row tables).
- FAIL: A table was dumped as one sentence (63 known) or a caption or cell is glued to a body sentence (32 known).
- Audit: A-chunking, P4-17

### CH-06 (fail) enforced by `conform_chunk.tables_have_header_and_flag`

A table has its header from row 0, no record split over two rows, and a quality value; tables with 0 rows or an empty header are flagged uncertain.

- PASS: Header is non-empty for quality ok tables, and no row is the continuation of another row.
- FAIL: A table marked ok has an empty header (9 known), a record split in two (14 known), or a 0-row table is not flagged (13 known).
- Audit: A-tables, P4-17

### CH-07 (fail) enforced by `conform_chunk.sections_follow_the_text`

Sections follow printed headings; a review article takes its subsection names from its sub-headings; body text is never assigned to references, front_matter or a wrong section.

- PASS: No body sentence is in references/front_matter, no paper with printed sub-headings has its whole body under introduction, and a body that stays main_text is flagged only when none of its sentences carries a subsection (a review whose sentences carry the printed sub-headings as subsection passes).
- FAIL: Body text sits in references or front_matter, or a paper's whole body is introduction while a sentence carries a subsection, or a paper's whole body is main_text and no sentence carries a subsection.
- Audit: A-sections, P4-18

### CH-08 (fail) enforced by `conform_chunk.probe_layout_signals`

Layout interruptions are recognised by structural signals only (position, font, repetition), never by words such as University, sentence length or number density.

- PASS: No body sentence is treated as layout because of its words, length or digits.
- FAIL: A body sentence is skipped as layout for such a reason.
- Audit: P4-15

### CH-09 (fail) enforced by `selftest.check_paper`

Citation markers are exact text slices, author-year citations are captured, affiliation marks are not citations, and every high-confidence ref_num exists in references (invariants I1-I4).

- PASS: I1 passage slice equals sentence text; I2 seq 1..n; I3 ref_nums exist; I4 no citations in references; author-year count per paper has 0 lost.
- FAIL: An invariant breaks, 50 author-year citations are lost (regex backspace), or an affiliation mark is read as a citation (10 known).
- Audit: P4-14, A-citations

### CH-10 (warn) enforced by `conform_chunk.probe_hyphenated_names`

The reference parser keeps hyphenated author names whole and writes a field as null rather than guessing it.

- PASS: On the fixtures a hyphenated surname is parsed whole.
- FAIL: A hyphenated name is truncated (58 references known).
- Audit: P4-14

### CH-11 (fail) enforced by `renumber.text_preservation`

Re-extraction is done paper by paper with a dry-run first; it proceeds only when lost_total is 0, and carries over IDs by exact text (renumber) with the old files kept in tier1_history.

- PASS: Dry-run shows text_preservation.lost_total = 0 and a recoverable tier1_history/<uuid> exists after the real run (the code measures lost_total; refusing a run with lost_total > 0 is G3).
- FAIL: A real run starts with lost_total > 0, or the old files cannot be restored.
- Audit: CONTRACTS-2.7

### CH-12 (fail) enforced by `conform_chunk.page_text_is_in_sentences`

Searchable full text is preserved whenever extraction is possible: no passage is discarded because a model finds it unimportant, and page furniture leaves passages but stays in page text.

- PASS: Every extracted page has its text in pages.jsonl and every body sentence is in sentences.jsonl; text_preservation.lost_total is 0 after any re-extraction.
- FAIL: Body text present in pages.jsonl is missing from sentences.jsonl, or text was dropped by a relevance judgement.
- Audit: AGENTS-fulltext, CONTRACTS-2.0

## EV: Evidence units (AI)

### EV-01 (fail) enforced by `evidence.validate`

V1: an evidence unit has a valid unit_type, an English statement and 1-6 existing sentence IDs of its own paper forming one place, never the reference list.

- PASS: unit_type is allowed, statement is English (>= 10 chars), IDs exist, and the IDs are in one passage, within 5 body sentences, or all rows of one table.
- FAIL: Any of those conditions is broken; the unit is rejected.
- Audit: V1
- Params: `{max_gap_body_sentences: 5, max_sentence_ids: 6}`

### EV-02 (fail) enforced by `evidence.validate`

V2: every number in statement, values, n and p occurs in the cited sentences (citation markers blanked), with its sign.

- PASS: Each number of statement/values/n/p is found in the cited sentences.
- FAIL: A number is absent from the cited sentences (the unit is rejected) or a minus sign was dropped (warning).
- Audit: V2

### EV-03 (warn) enforced by `evidence.validate`

V3: species, cell type, agent, measure, method and terms appear in the paper, in its own wording or a registered synonym.

- PASS: Each named entity matches the paper text by stem, species synonym, synonym group, MeSH entry term or word by word.
- FAIL: An entity is not found in the paper (the unit is flagged).
- Audit: V3

### EV-04 (warn) enforced by `evidence.validate`

V4: a result unit comes from a primary paper's Results (or a table row), never from a review paper.

- PASS: Every result unit cites a results/results_discussion/supplementary sentence of a non-review paper.
- FAIL: A result unit is attached to a review paper or to a non-result section.
- Audit: V4

### EV-05 (warn) enforced by `evidence.validate`

V5: a hedge in the source is kept; hedged source wording never becomes definitive or causal wording, and association never becomes causation.

- PASS: A unit citing a hedged sentence has hedging = hedged and no causal or definitive verb that the source lacks.
- FAIL: A causal or definitive verb appears over hedged source wording.
- Audit: V5, AGENTS-citation
- Params: `{lexicon: config/hedge_lexicon.yaml}`

### EV-06 (fail) enforced by `conform_evidence.hedged_only_by_the_main_claim`

Hedging is judged on the sentence that carries the unit's main claim (hedged_sentence_ids), not on every sentence of the unit; non-significance is its own category and is not a hedge.

- PASS: A unit is hedged only when the main-claim sentence is hedged; p > 0.05 is category non_significance and associated with used as a location is not a hedge.
- FAIL: A unit is hedged because of another sentence (61 known, 33 results), e.g. 'could be seen' elsewhere in the unit, or 'associated with' used as a location x19.
- Audit: P1-5, A-hedge
- Params: `{lexicon: config/hedge_lexicon.yaml}`

### EV-07 (fail) enforced by `evidence.validate`

V6: a sentence that cites other papers is not a result; a cited_statement names its source, lists cited_refs that the sentence really cites, and is written as attribution.

- PASS: No result cites a sentence with citations; every cited_statement has valid cited_refs and attribution wording.
- FAIL: A result cites a citing sentence, or a cited_statement has missing or unrelated refs (rejected) or no attribution wording (flagged).
- Audit: V6

### EV-08 (warn) enforced by `evidence.validate`

V7: outcome.direction agrees with the source wording (increase, decrease, no_change, mixed, not_applicable).

- PASS: Direction matches the wording after location words are ignored.
- FAIL: Direction contradicts the wording (flagged).
- Audit: V7

### EV-09 (fail) enforced by `evidence.validate`

V8: every must-cover sentence of a body section is used by a unit or skipped with a specific reason; a continues chain counts as one sentence.

- PASS: coverage_missing is empty.
- FAIL: A must-cover sentence is neither cited nor skipped with a reason (sent back).
- Audit: V8

### EV-10 (fail) enforced by `evidence.validate`

V9: terms are normalised to one canonical English name and one type; a new result or method_detail unit has at least one scientific term list.

- PASS: All terms resolve in the one-type registry and result/method_detail units have a non-empty list.
- FAIL: A term has two types or a result unit has no scientific term (rejected).
- Audit: V9

### EV-11 (fail) enforced by `evidence.validate`

V10: a submission carries classification (CORE, CONNECTED, POTENTIAL_BRIDGE, UNCERTAIN, OUT_OF_SCOPE), reason, evidence_type, analyst, matching paper_id and a non-empty units list; an unconnected paper is not rejected for that alone.

- PASS: All required submission fields are present and valid.
- FAIL: A required field is missing (nothing is stored).
- Audit: V10, AGENTS-classification

### EV-12 (fail) enforced by `evidence.validate`

V11: result and method_detail units cite data (a table row, or non-in_table text of results, results_discussion, supplementary, methods, abstract or main text), not a caption or title.

- PASS: Every result/method_detail cites data sentences.
- FAIL: A result cites a caption or title (rejected).
- Audit: V11, B-caption-numbers

### EV-13 (fail) enforced by `evidence.validate`

V12: value_source figure_only carries no values, n or p (numbers are never read off a figure); value_source table cites a table row.

- PASS: figure_only units have empty values/n/p; table units cite table rows.
- FAIL: A figure_only unit has numbers, or a table unit has no row (rejected).
- Audit: V12

### EV-14 (warn) enforced by `crosscheck.run`

V13: a second model checks statement against the verbatim quote (supported, partly, not_supported); the last two flag the unit.

- PASS: Units with a current analysis_v13 report have supported or are flagged.
- FAIL: A not_supported unit is shown without a flag.
- Audit: V13

### EV-15 (fail) enforced by `evidence.recheck`

V14: a unit whose cited sentence is missing or whose stored quote or sections differ from the current sentences is flagged source drift; the cited ID is never rewritten to a guess.

- PASS: recheck leaves no unflagged drifted unit.
- FAIL: A drifted unit stays ok, or an ID was rewritten without an exact text match.
- Audit: V14, E1

### EV-16 (fail) enforced by `evidence._quote`

Agents give sentence IDs only; the stored quote is copied by code from the cited sentences, in order, with the gap marker only between non-adjacent sentences, never inside two halves joined by continues.

- PASS: quote equals the code-rebuilt text and contains no gap marker inside a continues chain.
- FAIL: A quote differs from the rebuild (E1) or has a gap marker inside one sentence (65 known).
- Audit: P1-6, A-quote, AGENTS-rule9

### EV-17 (fail) enforced by `conform_evidence.results_state_direction_and_species`

A result unit states its intervention or agent, outcome measure, direction, species and condition; where the sentence or paper names them, code fills them (filled_by: rule) before AI, and an AI fills only what stays ambiguous.

- PASS: Result units with an empty direction or species are <= the agreed limit and each filled field records filled_by.
- FAIL: Direction or species are missing on most results (94% / 100% for one analyst, 84% / 73% for another).
- Audit: A-§B-fields
- Params: `{max_empty_direction_share: 0.05, max_empty_species_share: 0.05}`

### EV-18 (fail) enforced by `conform_evidence.one_finding_per_unit`

One unit holds one finding: a sentence that reports two outcomes is split into two units citing the same sentence, and a bundled direction never compresses mixed results.

- PASS: No unit has one direction for statements that report opposite outcomes.
- FAIL: A bundled result carries a single direction (one bundled conclusion, one result without a control group).
- Audit: B-bundled-result, B-no-control-group

### EV-19 (fail) enforced by `conform_evidence.statement_adds_no_content`

A statement adds no content beyond its cited sentences: every concept, entity and number in it occurs in them.

- PASS: Every content term of the statement occurs in the cited sentences or their registered synonyms.
- FAIL: The statement names something the sentences do not (an added mechanism phrase, an added P value).
- Audit: B-added-content

### EV-20 (fail) enforced by `conform_evidence.figure_warnings_reach_units`

A unit that rests on a figure whose reading is flagged inconsistent carries that flag.

- PASS: Every unit citing an object with a red figure-reading warning shows the warning.
- FAIL: A flagged figure's warning is not passed on to a unit using it (13 known cases).
- Audit: A-figure-flags

### EV-21 (fail) enforced by `evidence.resolve_unit`

Exact duplicate evidence inside one paper is merged reversibly (aliases keep old IDs); units differing in model, dose, outcome or hedging are never merged.

- PASS: Merged IDs resolve through evidence_aliases and no merge crosses a differing field.
- FAIL: Two non-identical units are merged or an alias chain cannot be resolved.
- Audit: CONTRACTS-9

### EV-22 (warn) enforced by `hedges.load`

Hedge words come from one vocabulary, config/hedge_lexicon.yaml; no module keeps its own list.

- PASS: evidence, structure, answer all read hedges.py (true today for English); no second hedge list is found in code.
- FAIL: A module defines its own hedge list (answer.VI_HEDGE was a second, Vietnamese list before G3).
- Audit: PLAN-G3
- Params: `{exceptions: [{file: scripts/kbsys/result_notes.py, item: associated_with, reason: 'outcome.direction labels (associated, correlated ...) normalised to a relation name, not hedge detection'}, {file: scripts/kbsys/vault_layout.py, item: 'HYP - ', reason: 'the Hypothesis note template (field values and Vietnamese headings), not a hedge vocabulary'}, {file: scripts/accept_requirements.py, item: look like, reason: 'P15 sample words that the lexicon must match; a test input, not a classifier'}], lexicon: config/hedge_lexicon.yaml, min_hedge_share: 0.3, min_hedge_words: 3}`

### EV-23 (fail) enforced by `evidence.validate`

Canonical knowledge (statements, terms, stored quotes) is English and original English evidence is never changed; an explanation in another language never replaces it.

- PASS: Every statement is English (no Vietnamese letters) and every stored quote equals the source sentence text.
- FAIL: A statement contains Vietnamese letters, or a quote is translated or edited.
- Audit: AGENTS-language, V1, E1

## SP: Structured paper profiles

### SP-01 (fail) enforced by `structure.submit_profile`

WH1: the introduction profile has exactly the keys what, why, who, where, when; each is a text with sentence IDs (quotes copied by code) or null with a reason.

- PASS: Five keys present; every non-null answer cites Introduction sentences.
- FAIL: A key is missing, an answer has no sentence ID, or null has no reason.
- Audit: WH1

### SP-02 (fail) enforced by `structure.submit_profile`

G1 grounding: an English profile text may not contain an opposite direction word, a specific organism, a gene-like token or a unit number that is absent from its cited sentences.

- PASS: Every such token of the text occurs in the cited sentences (or is a defined acronym matching its expansion).
- FAIL: A token is absent from the cited sentences (rejected).
- Audit: G1

### SP-03 (warn) enforced by `structure.submit_profile`

A7_setting: who, where and when answers keep the setting wording of the source; a null answer never means 'does not exist in the literature'.

- PASS: A7_setting warnings are 0 and every null carries 'not found in this paper' wording.
- FAIL: A setting is dropped or a null is worded as absence from the literature.
- Audit: A7_setting, AGENTS-gaps

### SP-04 (fail) enforced by `structure.submit_protocol`

A protocol profile lists ordered Methods protocols; names and values are exact Methods fragments on whole-token boundaries (5 mg not inside 15 mg), source order is kept and unknown stays unknown.

- PASS: Each name/value is a whole-token fragment of its cited Methods sentences; missing fields are absent.
- FAIL: A value is not a whole-token fragment, order changed, or a dose/instrument was guessed.
- Audit: CONTRACTS-5

### SP-05 (fail) enforced by `structure.submit_protocol`

Protocol coverage is complete or each uncovered Methods sentence is skipped with a reason; a review without Methods uses source_absence_reason that says the cited studies may still have protocols.

- PASS: coverage_missing is empty and an absent Methods section has source_absence_reason.
- FAIL: A Methods sentence is uncovered without reason, or absence is stated as 'no protocols exist'.
- Audit: CONTRACTS-5

### SP-06 (fail) enforced by `structure.submit_overview`

The paper overview is reference-only: each item has text, sentence IDs and a certainty copied from the source wording, a hedge in a cited sentence stays, and empty overviews or unknown keys are rejected.

- PASS: reference_only is true, every item has sentence IDs and the source hedge word in certainty.
- FAIL: An item lacks IDs, drops a hedge, or the overview is empty.
- Audit: CONTRACTS-5

### SP-07 (fail) enforced by `structure.submit_structured`

Every profile echoes source_sha256 and source_revision of the paper; a reply for a stale source is refused, never rebased.

- PASS: A reply with an old source_revision is refused.
- FAIL: A stale reply is stored.
- Audit: CONTRACTS-5

### SP-08 (fail) enforced by `structure.completion`

A paper is complete only when evidence is done, all five WH answers exist and the protocol is done, all on the current source; this is technical completeness, never scientific acceptance.

- PASS: demo3_complete is true only with the three parts current, and scientific_acceptance stays not_assessed.
- FAIL: A paper is marked complete with a missing or stale part, or marked scientifically accepted by software.
- Audit: AGENTS-approval

### SP-09 (fail) enforced by `structure.prepare_structured`

Structured tasks use chunks of at most 32 sentences and 12000 characters, split only on sentence IDs and never inside a continues chain.

- PASS: No chunk exceeds the limits or ends inside a continues chain.
- FAIL: A chunk exceeds a limit or cuts a chain.
- Audit: CONTRACTS-5
- Params: `{max_chars: 12000, max_sentences: 32}`

## FG: Figure and table reading

### FG-01 (fail) enforced by `figure_reading.submit`

A figure or table reading matches the caption to the image; a wrong crop is reported as crop_wrong and the object leaves the queue until the crop is rebuilt.

- PASS: Every reading either describes the cropped image or is crop_wrong with a note.
- FAIL: A reading describes a different image than the crop.
- Audit: CONTRACTS-8.7

### FG-02 (fail) enforced by `figure_reading.submit`

Numbers in caption_consistency notes occur in the cited sentence or in a panel value; numbers are never read off the image alone.

- PASS: Each number of a note occurs in the cited sentence or a panel value.
- FAIL: A number appears in no source text.
- Audit: skill-ai-analysis-submit

### FG-03 (fail) enforced by `figure_reading.sentence_warnings`

A caption-versus-figure inconsistency is shown wherever the figure is used (paper note, node note, dashboard, answer WARNING line) and is passed on to units that rely on it.

- PASS: figure_reading.warning appears in each place and in the unit's flags.
- FAIL: A flagged figure is shown without its warning (unit flags missing today).
- Audit: A-figure-flags, STATUS-14

### FG-04 (warn) enforced by `figure_reading.load`

A figure reading is current only while the crop hash and the cited caption/mention text hash match; n/N (%) cells are split into value, count and n.

- PASS: A changed crop or cited text marks the reading stale.
- FAIL: A stale reading is served as current.
- Audit: CONTRACTS-2.7

### FG-05 (fail) enforced by `conform_profile.reading_never_replaces_the_source`

A figure reading never replaces the caption, the text evidence or the crop, and numbers are never inferred from pixels.

- PASS: Caption, sentences and crop are unchanged after a reading is stored, and every number in a reading occurs in source text or a panel value.
- FAIL: A reading overwrites a caption, sentence or crop, or carries a number found in no source text.
- Audit: CONTRACTS-8.7

## ND: Concepts and nodes

### ND-01 (fail) enforced by `presentation.homonym_blocked`

A term maps to the MeSH or registry sense that fits the paper's context; a sense from another field is never used (Vitrification is not Glass Transition, PCA is not Passive Cutaneous Anaphylaxis in a statistics paper, SIRT imaging is not Sirtuins).

- PASS: No linked concept has a sense contradicted by the paper; the homonym list blocks the known cases.
- FAIL: A node carries a sense that the paper does not use.
- Audit: A-concepts, B-concepts
- Params: `{homonyms: config/homonyms.yaml}`

### ND-02 (fail) enforced by `concepts.abbreviation_pairs`

An acronym that a paper defines together with its expansion is one concept, and sentences using the acronym link to it within that paper (AMI defined as acute myocardial infarction: 78 sentences of that paper).

- PASS: abbreviation_pairs merges the pair and the acronym sentences of the defining paper appear in the node.
- FAIL: A node lacks the acronym sentences of the paper that defines it.
- Audit: STATUS-13

### ND-03 (fail) enforced by `concepts.build_registry`

Variants of one concept merge into one node: case, hyphen, spaces between letters and Greek symbol versus name (m6A, m 6A, RT-qPCR, beta-tubulin, Blastocoel and Blastocoele, Lyve-1).

- PASS: The merge key maps all listed variants to one concept_id.
- FAIL: Two nodes exist for one concept.
- Audit: B-concepts
- Params: `{parents: config/concept_parents.yaml}`

### ND-04 (fail) enforced by `conform_graph.no_generic_nodes`

Study words, reagents and phrases never become a species or concept node (Mater from dura mater, Bovine and Fetus from bovine serum, Clinical data).

- PASS: No node is created from a generic term or from a phrase fragment.
- FAIL: A node exists for such a term.
- Audit: STATUS-14-item3
- Params: `{generic_terms: config/generic_terms.yaml}`

### ND-05 (warn) enforced by `concept_notes.shown_in_vault`

A node shows in a vault only with at least one evidence unit or at least three mentions in that vault's papers.

- PASS: Every shown node meets the threshold.
- FAIL: A node below the threshold is shown.
- Audit: STATUS-14
- Params: `{min_evidence: 1, min_mentions: 3}`

### ND-06 (fail) enforced by `conform_graph.no_orphan_or_empty_nodes`

No orphan node: every shown node has at least one source sentence and a note with content; an empty note (253 bytes, Sphingosine-1-phosphate) is a defect.

- PASS: Every node note has a non-empty section built from source sentences.
- FAIL: A node exists without source sentences or has an empty note.
- Audit: D-notes, PLAN-RQ3

### ND-07 (fail) enforced by `selftest.check_concepts`

Concept links are built from full text (not references or in_table text) and concept_id values are unique.

- PASS: selftest finds no duplicate concept_id and relink_paper keeps sentence counts and sections.
- FAIL: Duplicate concept_id, a link built from references or in_table text, or a relink_paper mismatch (only the duplicate-id check is in selftest today).
- Audit: CONTRACTS-6

### ND-08 (fail) enforced by `concepts.build_registry`

All-caps acronyms are case sensitive, and short or ambiguous aliases are ignored.

- PASS: EG does not match eg; ITS beats Its only when verbatim.
- FAIL: A short or ambiguous alias links sentences wrongly.
- Audit: CONTRACTS-6

### ND-09 (fail) enforced by `concepts.mesh_tree_type`

A node whose term lists give only the general type concept takes its type from its MeSH tree numbers when every tree number (cell structures A11.284 ignored) maps to one type: A11 cell_type, B01/B03/B04 species, D molecule, E05 method; a mixed or unmapped tree, a specific type, topic and a human-confirmed node never change.

- PASS: A fresh build changes the type of exactly the nodes whose build type is concept and whose trees map to one type, each marked classified_by ND-09; a second build changes nothing.
- FAIL: A node with a mixed tree, a specific type, topic or human_confirmed status changes type, or a qualifying concept node keeps the type concept.
- Audit: DECISIONS-2026-10-21, PLAN-d-U2
- Params: `{ignore: [A11.284], only_from: concept, tree_types: {A11: cell_type, B01: species, B03: species, B04: species, D: molecule, E05: method}}`

### ND-10 (fail) enforced by `node_decisions.validate`

A node type, removal, MeSH unlink or added concept decided by a rule or an AI model is one row of knowledge_store/node_decisions.jsonl that names the clause or model that set it and is never marked human-confirmed; an AI row and an added concept cite vault body sentences that the node's name or synonyms match; undo = remove the row and rebuild.

- PASS: Every stored row passes node_decisions.validate (kb.py node-decision check); a build after removing a row equals the build before it was added.
- FAIL: A machine or AI node decision has no classified_by, is marked human_confirmed, or an AI or add row cites no matching vault body sentence.
- Audit: DECISIONS-2026-10-20, PLAN-d-U2b

## RL: Relations, conflicts, typed network

### RL-01 (fail) enforced by `conform_graph.edges_are_typed_and_sourced`

Every knowledge edge between notes has a type from the agreed list and cites the evidence unit and sentence IDs behind it; edges that only navigate carry the type nav. In a result note only properties and the kb:auto block give typed edges (a conflicts_with property gives a result-to-result conflicts_with edge); links written outside the kb:auto block are nav.

- PASS: 100% of knowledge edges have etype, unit_id and sentence_ids.
- FAIL: A knowledge edge has no type or no source.
- Audit: D-graph, P3-12
- Params: `{etypes: [observed_effect, observed_association, tested_mechanism, hypothesis_only, cited_background, same_measure_comparison, conflicts_with, in_taxon, uses_method, nav]}`

### RL-02 (fail) enforced by `relations.propose`

A contradiction is proposed only for the same outcome, the same intervention agent and opposite directions; a species name alone is never the measured outcome.

- PASS: On the fixtures every proposed pair has equal measure and agent and opposite directions.
- FAIL: A pair mixes different measures or agents (10 of 11 live relations, e.g. two different antioxidants compared on ROS).
- Audit: S16-10, B-relations
- Params: `{outcome_quantity_words: [level, levels, content, amount, concentration, intensity, signal, fluorescence, immunofluorescence, rate, frequency]}`

### RL-03 (fail) enforced by `conform_graph.bundled_conclusions_are_split`

Compound conclusions are split by outcome before pairing (a text naming non_outcome_words, such as an enrichment or baseline table, gives no per-entity outcome; a direction word belongs to an entity only when no other predicate from predicate_words, followed by a comma, semicolon or colon, stands between them), and review-table rows and cited statements are considered as 'literature disagreement'.

- PASS: A pair is formed per outcome.
- FAIL: A bundled unit produces pairs for unrelated outcomes (one bundled conclusion produced 6 wrong relations).
- Audit: B-bundled-result, S16-11
- Params: `{non_outcome_words: [enrichment analysis, enrichment analyses, Gene Ontology, GO analysis, GO analyses, GO term, GO terms, GO function, GO enrichment, enrichment-analysis, KEGG, GSEA, baseline characteristic, baseline characteristics], predicate_words: [was, were, is, are, remain, remains, remained]}`

### RL-04 (fail) enforced by `conform_graph.conflict_titles_name_both_species`

Conflict note titles carry the species of both sides; an empty species field is filled or reported, never taken from the other side.

- PASS: Title species equal each side's own species.
- FAIL: A title shows one species for both sides (H3K9me3 - Mouse for a bovine side).
- Audit: S16-10

### RL-05 (fail) enforced by `relations.conflict_relations`

Relations are written, withdrawn and read inside their own vault only; an unknown vault is an error.

- PASS: A rerun for vault A leaves vault B's relations unchanged; list_conflicts of an unknown vault errors.
- FAIL: A cross-vault withdrawal or leak occurs.
- Audit: S16-11, N19

### RL-06 (fail) enforced by `result_notes.refresh`

Conflicts are never deleted or hidden: they show in the Conflicting findings section with the tag #conflict, and non-significance is not proof of no effect.

- PASS: Every active conflict appears in its note and in Open conflicts; effect_vs_no_effect rows carry the non-significance caution.
- FAIL: An active conflict is missing from the note or worded as proof of no effect.
- Audit: STATUS-14, AGENTS-citation

### RL-07 (fail) enforced by `relations.propose`

Human decisions on a relation (human_confirmed, rejected) and researcher text are never changed by a rerun.

- PASS: Human-status relations are byte-identical after propose.
- FAIL: A human-status relation changed.
- Audit: AGENTS-approval

### RL-08 (fail) enforced by `gaps.generate`

A gap candidate says 'not found in the current vault', never 'absent from the literature', is judged by concept match (not string equality) and is promoted only by the researcher in a discussion.

- PASS: All gap rows carry the scope note and false 'absent' rows from string-only matching are 0.
- FAIL: About 9 of 23 vitrification rows claim absence wrongly, or a gap is accepted without the researcher.
- Audit: B-gaps, AGENTS-gaps

### RL-09 (warn) enforced by `conform_graph.probe_hubs_do_not_expand`

The network separates knowledge edges from navigation edges and marks hubs (a node present in more than half of the papers) and index maps so they are not used to expand reasoning.

- PASS: Hub nodes and MOC notes are marked and absent from hop expansion.
- FAIL: A generic hub (DNA in 15 of 16 papers) takes part in expansion.
- Audit: D-graph, P3-12
- Params: `{hub_share_of_papers: 0.5}`

### RL-10 (fail) enforced by `conform_graph.result_notes_name_subject_predicate_object`

A Result (EVD) note names a subject, a predicate from the agreed list and an object that are real node links, and the node note links back.

- PASS: Each subject item resolves to an existing node note (the subject_not_stated text only as the sole subject item, when the unit states no subject); each object item resolves to an existing node note; the predicate is one of predicates; each linked node lists the result; a unit without an outcome has no EVD note.
- FAIL: A result note has an empty subject or object (also an empty list), a subject or object item that is no node link, a predicate outside the list or mixed for a result without direction, or a linked node that does not link back.
- Audit: D-notes, P3-12
- Params: `{predicates: [increases, decreases, no_significant_change, associated_with, mixed, reports], subject_not_stated: not stated}`

### RL-11 (warn) enforced by `conform_graph.shared_measures_have_comparison_notes`

Cross-paper comparison notes are created when two or more papers measure the same thing, reusing the reviewed measure groups.

- PASS: Every measure group with 2+ papers has a comparison note listing each paper's result.
- FAIL: A shared measure has no comparison note.
- Audit: D-graph, P3-12

## NC: Node cards (AI)

### NC-01 (fail) enforced by `node_cards.submit`

Every node-card statement cites sentence IDs of the node's papers (quotes copied by code); a field with no source is {status: not_found_in_vault}; every common field is present.

- PASS: All statements have sentence IDs and all common fields exist as a statement or not_found_in_vault.
- FAIL: A statement has no source, a field is missing, or a quote was typed by the agent.
- Audit: skill-ai-analysis-submit, CONTRACTS-8

### NC-02 (fail) enforced by `node_cards.submit`

Card statements keep the source hedge and attribution (primary, review, author_hypothesis, cited_study), and A3 and A7 apply to them.

- PASS: No card statement is definitive over a hedged source or loses its attribution.
- FAIL: A hedge is dropped or attribution is wrong.
- Audit: A3, A7

### NC-03 (fail) enforced by `node_cards._card_currency`

A card is current until a cited quote changes; the card records the node's full evidence inventory at submit, and a paper or unit outside it makes it current_with_new_evidence.

- PASS: A fresh card is current; new evidence flips it to current_with_new_evidence; a changed quote makes it stale.
- FAIL: A capped card stays current_with_new_evidence forever (fixed in 4a818d7) or a changed quote stays current.
- Audit: STATUS-14

### NC-04 (warn) enforced by `conform_profile.probe_shown_card_says_card_check_passed`

A card note carries the small English line 'AI-written summary from N source sentences, checked automatically.' (researcher 2026-10-19) and card_check: passed; there is no accepted/needs-review status for the researcher to tick.

- PASS: Every shown card has the line and card_check: passed.
- FAIL: A card is shown without it or enters the review queue.
- Audit: PLAN-RQ

### NC-05 (warn) enforced by `node_cards.task`

A node task is capped at 500 sentences; a capped node can request specific sentences with include_sentence_ids.

- PASS: Task sentences <= cap and include_sentence_ids are honoured when eligible.
- FAIL: A task exceeds the cap or ignores an eligible include.
- Audit: D3.74
- Params: `{max_sentences: 500}`

## VN: Vault notes

### VN-01 (fail) enforced by `notes.write_paper_note`

Software writes only inside kb:auto blocks and machine frontmatter keys; human text, Mine/ and custom properties are never changed, and a note whose auto block was edited by hand is left alone.

- PASS: Mine/ and text outside kb:auto have identical hashes before and after a rebuild.
- FAIL: Any Mine/ file or human text hash differs.
- Audit: AGENTS-human, CONTRACTS-8.1

### VN-02 (fail) enforced by `concept_notes.heading_problems`

Each note type uses its approved English section set in order (concept, method, substance, organism, cell_type, EVD, CLM, CONFLICT, MOC); a section without data is not written.

- PASS: heading_problems returns nothing and no empty heading or placeholder bullet exists.
- FAIL: An unapproved or misordered heading, or an empty section, is found.
- Audit: STATUS-10, CONTRACTS-8.9, CONTRACTS-8.11

### VN-03 (fail) enforced by `presentation.machine_ids`

No machine ID (paper:S0123, E0077, F3) appears in a heading or in the running text of a note; IDs live in front matter and the single Source IDs callout.

- PASS: machine_ids finds 0 in headings and bodies.
- FAIL: An ID appears outside the allowed places.
- Audit: N3b

### VN-04 (fail) enforced by `selftest.note_text_problems`

Notes contain no broken characters, placeholders, raw JSON, None or Python dicts, and no link that differs only by case.

- PASS: note_text_problems finds 0.
- FAIL: A problem is reported.
- Audit: N3e

### VN-05 (fail) enforced by `conform_vault.links_resolve`

Every wikilink in a vault resolves to an existing note or attachment.

- PASS: Link edges with status missing = 0.
- FAIL: A missing link exists (2399 reported on 17/10).
- Audit: PLAN-VN

### VN-06 (fail) enforced by `conform_vault.no_orphan_image`

No orphan image: every file in _Attachments is embedded by a note; a duplicate or whole-page caption screenshot found by kb.py image-audit is removed only on the researcher's yes in chat and reversibly, a real figure is attached to its object.

- PASS: Unreferenced attachments = 0.
- FAIL: An attachment is not used (24 known).
- Audit: STATUS-15

### VN-07 (fail) enforced by `presentation.cite`

Citations in notes read [[Paper note|Author Year, p. N]]; a quote is copied from sentences.jsonl, and the size caps hold (paper note 120 KiB, node note 60 KiB) by dropping whole lowest-priority rows, never partial quotes.

- PASS: All citations use the format, quotes equal sentence text, sizes are under the caps.
- FAIL: A citation is malformed, a quote was edited or cut, or a cap is exceeded.
- Audit: N3b, CONTRACTS-8.4
- Params: `{node_note_kib: 60, paper_note_kib: 120}`

### VN-08 (warn) enforced by `conform_vault.one_language_and_no_local_links`

Machine-written text in notes uses one language per kind of text (English headings and canonical text), with no mixed Vietnamese-English machine phrases and no dashboard link to 127.0.0.1 inside note text.

- PASS: Mixed-language machine phrases = 0 and local dashboard links in body = 0.
- FAIL: 74% of notes mix languages and 79% carry a 127.0.0.1 link today.
- Audit: D-notes

### VN-09 (warn) enforced by `conform_vault.graph_settings`

The graph colours knowledge edges and navigation edges differently and the default filter hides navigation, stubs and review items.

- PASS: graph.json groups follow CONTRACTS 8.5 and navigation edges are filtered.
- FAIL: Navigation edges drown knowledge edges.
- Audit: D-graph, P3-12

### VN-10 (fail) enforced by `concept_notes._retire_orphans`

A retired machine note moves to knowledge_store/removed/ and is never deleted; one with human text stays with a warning block.

- PASS: Retired notes are found in removed/ and human text survives.
- FAIL: A retired note is deleted or human text is lost.
- Audit: CONTRACTS-8.1

## RT: Retrieval

### RT-01 (fail) enforced by `find.search`

Search combines exact terms, full text, metadata filters, synonym and entity expansion and optional semantic search; semantic similarity alone is never evidence.

- PASS: Every hit is a stored sentence or unit with a source; the retrieval mode is reported.
- FAIL: A hit without a stored source is presented as evidence.
- Audit: AGENTS-search

### RT-02 (fail) enforced by `conform_answer.probe_or_ranking`

In OR mode results rank first by the number of keyword groups matched, then by bm25; the match is filtered before the candidate cap, and no multiplier is applied twice.

- PASS: An item matching all groups ranks above an item matching fewer groups.
- FAIL: A sentence matching one weak group outranks one matching all (a paper matching only one weak group above a paper that states the topic directly).
- Audit: S16-05

### RT-03 (warn) enforced by `conform_answer.probe_word_variants`

Word variants (vitrification, vitrified, vitrify) match each other, and affect, impact, influence, change are stop words.

- PASS: Recall on the variant fixture reaches the target.
- FAIL: Recall stays at 0.68.
- Audit: N06
- Params: `{variant_recall_target: 0.9}`

### RT-04 (warn) enforced by `find.looks_vietnamese`

A Vietnamese query is expanded to English keywords or flagged needs_translation; it is never searched as if it were English.

- PASS: Vietnamese queries return an English expansion or the flag.
- FAIL: A Vietnamese query returns 0 rows silently.
- Audit: S16-06, P2-9

### RT-05 (fail) enforced by `conform_answer.probe_not_found`

When keyword-group coverage of the best items is below the threshold, the result carries a 'not found in the current vault' flag and states how many groups the best item matched.

- PASS: A query about something absent (V5, M3 gold questions) returns the flag.
- FAIL: An absent topic returns 12 loose items with no warning.
- Audit: S16-06, P2-9, N16
- Params: `{min_group_coverage: 0.75}`

### RT-06 (fail) enforced by `find.search`

Default scope is the current vault; other vaults are searched only when requested; an unknown vault is an error.

- PASS: Hits belong to the requested vault; an unknown vault raises.
- FAIL: A hit from another vault appears unrequested.
- Audit: AGENTS-search, N14

### RT-07 (fail) enforced by `answer.collect`

Answer collection gives each query a fair share of the item slots and keeps the sentences that match the query, not the first three of a unit.

- PASS: Top items of every query appear in the item list.
- FAIL: A rank 1-2 hit of a query is dropped for rank 1-5 hits of another.
- Audit: S16-05, N05

### RT-08 (warn) enforced by `find.search`

References, front matter, acknowledgements, funding, contributions, conflicts, data availability and notes printed after the references (section notes) are excluded from search, and pagination is stable.

- PASS: No hit from those sections; repeated paging returns no duplicate or skipped rows.
- FAIL: A funding sentence is returned, or paging shifts.
- Audit: N07, N14g

### RT-09 (warn) enforced by `find.context`

For an important scientific query the surrounding sentences, paragraph and section are re-read before answering (get_context), widening step by step.

- PASS: The answer protocol step 1 is followed and context items exist for the cited sentences.
- FAIL: An answer rests on an isolated sentence without a context check.
- Audit: AGENTS-search, protocol-1

### RT-10 (fail) enforced by `conform_answer.hit_fields`

Retrieved evidence keeps its paper identifier, title, DOI and/or PMID, section, page, original passage and a reference to the surrounding context.

- PASS: Every hit and every answer item carries paper_id, title, doi or pmid (when the paper has one), section, page, verbatim text and sentence_ids.
- FAIL: A hit lacks one of those fields.
- Audit: AGENTS-citation

### RT-11 (warn) enforced by `overlap.find_overlaps`

Overlap search is always labelled lexical overlap (word 5-gram containment) and is never presented as a plagiarism verdict.

- PASS: Every overlap hit and the result carry the label 'lexical overlap'.
- FAIL: An overlap result is worded as plagiarism or has no label.
- Audit: CONTRACTS-4.4

### RT-12 (fail) enforced by `find.search`

Non-PDF sources (own data, human notes) are returned as source_hits labelled non-published evidence and are never mixed with paper hits or answers.

- PASS: source_hits is a separate list and each row carries evidence_class own_data or human_note.
- FAIL: A non-PDF source row appears among paper hits or in an answer as published evidence.
- Audit: CONTRACTS-6

## AN: Answers and the answer checker

### AN-01 (fail) enforced by `answer.check_sentence`

A1: every answer sentence cites at least one existing item.

- PASS: Each sentence has a valid [#n] or sentence_ids.
- FAIL: A sentence has no existing citation (struck).
- Audit: A1

### AN-02 (fail) enforced by `answer.check_sentence`

A2: every number in an answer occurs in the cited verbatim text or is a cited year or reference number, and the number comes from an item that holds the identifier it describes.

- PASS: Each number occurs in a cited item that names the described entity; adding more citations cannot rescue a number.
- FAIL: A number is absent, or is borrowed from an item about another entity.
- Audit: A2, N01, S16-09

### AN-03 (fail) enforced by `answer.check_sentence`

A3: a definitive or causal sentence over hedged items is struck; Vietnamese causal verbs (gây ra, khiến, gây nên, làm cho, chứng tỏ, chứng minh, dẫn đến) are included and NFD spelling does not evade the check.

- PASS: Causal wording over hedged items is struck in NFC and NFD.
- FAIL: A causal sentence passes because of spelling form or a missing verb.
- Audit: A3, N03

### AN-04 (fail) enforced by `answer.check_sentence`

A4: a sentence whose cited items are all second-hand (re-cited) needs attribution; an item is second-hand only when the citation sits in the sentence actually used, and 'Author Year proposed/suggested/hypothesised' counts as attribution.

- PASS: Attributed sentences pass; own-hypothesis sentences of the cited authors pass.
- FAIL: A correct 'Author Year proposed a hypothesis' sentence is struck (S16-03) or a bare 'theo' passes.
- Audit: A4, S16-03

### AN-05 (warn) enforced by `answer.check_sentence`

A5: a sentence supported only by review papers must say so (warning).

- PASS: Review-only support is named.
- FAIL: Review-only support is presented as primary evidence.
- Audit: A5

### AN-06 (fail) enforced by `answer.check_sentence`

A6: tăng/giảm/không đổi in an answer agrees with the direction of every cited unit that has one.

- PASS: Direction words match.
- FAIL: A direction contradicts a cited unit (struck).
- Audit: A6

### AN-07 (fail) enforced by `answer.check_sentence`

A7: uncertainty in the cited source sentence that matches the claim must stay; hedge is read on that sentence, not on a whole unit, and non-significance wording is recognised in Vietnamese.

- PASS: A correct statement about a different measure of the same unit is not struck.
- FAIL: H4K12ac p<0.05 is struck because H3K9me3 in the same unit was not significant (S16-02, S16-08).
- Audit: A7, S16-02, S16-08

### AN-08 (fail) enforced by `answer.check_reasoning`

A8: reasoning basis may cite only notes of the ticket allowlist.

- PASS: All basis notes are in note_refs.
- FAIL: An unknown note path is accepted (test locks this today).
- Audit: A8, N10

### AN-09 (warn) enforced by `answer.check_sentence`

A9: a Part 2 sentence does not open with a bare verdict (Có, Không, Yes, No).

- PASS: No bare verdict opener.
- FAIL: A bare verdict opens a sentence (warning).
- Audit: A9

### AN-10 (fail) enforced by `conform_answer.probe_a10_subject`

A10 subject and condition: every identifier in an answer sentence (histone mark, species, agent, condition) must occur in the item it cites, split slash forms like H3K4me2/3 before checking.

- PASS: An item about another treatment (sip150) cannot support a vitrification sentence; H3K4me2/3 is read as me2 and me3.
- FAIL: A sentence about vitrification passes on an item about sip150 (S16-01), or H3K4me2/3 trips A2 (S16-09).
- Audit: S16-01, S16-09

### AN-11 (fail) enforced by `conform_answer.probe_limits_sentence`

A sentence that states the limit of the evidence runs with limits=True (A3, A6, A7 relaxed) but may not contain an affirming result verb.

- PASS: 'Only one mouse study, not generalisable' passes; 'proves' fails.
- FAIL: A limits sentence is struck by A7, or an affirming verb slips in.
- Audit: S16-04

### AN-12 (fail) enforced by `conform_answer.probe_item_numbers`

Item numbers are written [#n] so they never clash with literature references like [40-42]; the checker rejects bool, float and Infinity values.

- PASS: [#3] is an item, [40-42] is a literature reference, and bad values are rejected, not crashed.
- FAIL: [2] is read as item 2 inside a reference list, or Infinity kills ask_check.
- Audit: N02, N14d

### AN-13 (fail) enforced by `conform_answer.probe_items_digest`

ask_check checks the same items that ask_prepare gave (items_digest), and returns the markdown with the reason for each struck sentence; item order cannot change between the two calls.

- PASS: The digest of prepare equals the digest of check, and struck sentences carry reasons.
- FAIL: ask_check retrieves again and the [n] numbers change.
- Audit: N12, N13

### AN-14 (fail) enforced by `answer.code_evidence`

Part 1 quotes and citations are copied by code; an AI never types a quote, and missing author, year or page reads 'không rõ'.

- PASS: Every quote equals the stored sentence text.
- FAIL: A typed or altered quote appears.
- Audit: AGENTS-rule9, CONTRACTS-4.2

### AN-15 (fail) enforced by `answer.check_parts`

Model memory is never presented as evidence: every Part 2 and Part 3 sentence carries exactly one kind of basis, item citations [#n] (quotes copied by code), an inference 'suy luận từ ...' over items or server-ticket note references (AN-08) that keeps the strength of the cited sentences, or the label 'kiến thức của AI (không phải bằng chứng trong thư viện)'; general knowledge appears only in Part 3 ([REASONING]); Part 1 (code-printed quotes) and Part 4 (missing coverage, limits under AN-11, scope note) keep their own rules; the server instructions and the answer protocol use this one label, and 'not found in the current vault' is never worded as 'not studied in the literature'.

- PASS: Every Part 2 sentence cites an item; every Part 3 sentence has a basis of items or allowed ticket notes (rendered 'suy luận từ') or basis 'ai_knowledge' (rendered with the AI label); Part 1 and Part 4 are unchanged; the server instructions and config/answer_protocol.md show the same label; the scope note is present.
- FAIL: A Part 3 sentence has no basis (today it passes when it holds no number), AI knowledge appears in Part 1-2, the two texts use different labels, or absence is claimed globally.
- Audit: AGENTS-core, protocol-3, client-app-2026-10-22-Q3

### AN-16 (warn) enforced by `conform_answer.probe_both_sides`

Contradicting findings are shown with both sides and their own citations; the answer never picks a winner silently.

- PASS: Both sides appear with citations.
- FAIL: Only one side is reported.
- Audit: protocol-3

### AN-17 (fail) enforced by `discussions.save`

Saved discussions follow the answer protocol (lookup, related notes, brainstorm, continue), are saved only on 'lưu', are never evidence, and a knowledge gap is saved only when the researcher agrees, with three labelled parts (evidence, AI knowledge, researcher's judgement); a settled point is saved as kind decision only when the researcher says 'chốt', with the researcher's own words, the date and the item citations shown at that time, and a later decision on the same point is a new note that links the earlier one.

- PASS: save_discussion notes have evidence: false and kind discussion, knowledge_gap or decision; a decision note holds the researcher's words, the date and at least one item citation or the line 'không có bằng chứng trong kho lúc chốt'.
- FAIL: A discussion or decision is used as evidence in search, a gap is saved without agreement, a decision is saved without 'chốt', or a decision note overwrites an earlier one.
- Audit: protocol, AGENTS-gaps, client-app-2026-10-22-Q7

### AN-18 (fail) enforced by `conform_answer.traceable_chain`

Every important claim in an answer stays traceable along AI answer, claim, retrieved passage, section, page, paper, original source.

- PASS: For each cited item the chain item, sentence_ids, section, page, paper_id and source file status can be resolved.
- FAIL: A cited item cannot be resolved to its page and paper, or the original-source status is hidden.
- Audit: AGENTS-hierarchy

### AN-19 (warn) enforced by `G3`

An answer does not follow the asker's view without evidence: what the vault supports is stated as supported, what it does not support is stated as not found or contradicted; a disagreement with the researcher cites items (quotes copied by code) and shows both sides when a conflict exists; a point the researcher settled (a decision note) is not argued again, a later answer on it shows 'bằng chứng – ý chốt' side by side, and evidence that entered the vault after the decision date is reported once, without repeating the earlier evidence.

- PASS: Trap questions (the asker asserts what the vault does not support) get 'not found' or the contradicting items with quotes; an answer on a settled point shows the decision note labelled 'ý chốt của bạn (ngày)' next to its evidence; newer evidence is flagged in one answer only.
- FAIL: The answer agrees with an unsupported assertion, disagrees without a quote, argues a settled point again, or flags the same newer evidence twice.
- Audit: AGENTS-core, protocol-3, client-app-2026-10-22-important, client-app-2026-10-22-Q7

### AN-20 (fail) enforced by `discussions.remember`

Brainstorm memory is context, never evidence: a researcher profile shared by the project and topic notes kept per vault (files under the project's runtime/brainstorm folder only, never an AI app's own memory, user-level files or another vault) are read once per chat for the asked vault, shown only as 'ý của bạn (đã nhớ, <date>)' or 'ghi chú tìm kiếm của AI (<date>)' and never cited in Parts 1-2; remember appends one dated line marked with who wrote it (facts about the researcher only from the researcher's own words), never edits an earlier line and refuses a file at its line cap; a remembered 'not found' is searched again before it is used; memory is never sent to a client allowed only retrieval output (Q6, Hermes).

- PASS: remember appends a dated line with its writer, refuses a full file, a vault outside the research vaults or a path outside runtime/brainstorm; get_answer_rules returns the profile and only the asked vault's topics, and nothing when the server runs without memory.
- FAIL: Memory is cited as evidence, an earlier line is changed, one vault reads another vault's topics, memory is written outside runtime/brainstorm or into an app's memory, or a retrieval-only client receives it.
- Audit: AGENTS-1, AGENTS-5, brainstorm-2026-11-02
- Params: `{max_line_chars: 300, profile_max_lines: 60, topic_max_lines: 120}`

## NW: Neural network route

### NW-01 (fail) enforced by `conform_network.payload_budget`

A neural_prepare payload stays within the size budget and the estimated prompt within the token budget.

- PASS: payload bytes <= max and prompt tokens <= budget on the gold questions.
- FAIL: Payload is 0.7-1.1 MB with 10-15k prompt tokens today (scripts/benchmark_research.py measures it but is not shipped in releases).
- Audit: D-payload, P3-11
- Params: `{context_share: 0.25, max_payload_kb: 150, token_budget_deep: 8000, token_budget_fast: 4000}`

### NW-02 (fail) enforced by `conform_network.ranking_and_duplicates`

Items are ranked by relevance to the question (matched sentences first), not by unit order, and a field is not repeated (items versus source_resolution, media versus figures).

- PASS: First must-sentence rank is within the target and no duplicated field exists.
- FAIL: The first must-sentence is at rank 9.5 (median) and fields are duplicated.
- Audit: S16-07, N08, N09, N11
- Params: `{max_first_must_rank: 3, min_idea_coverage: 0.77}`

### NW-03 (fail) enforced by `conform_network.probe_unit_labels`

Unit labels (result, hypothesis, cited_statement, interpretation) are kept in neural items so a tested result is told from a proposal.

- PASS: Each item shows its unit_type.
- FAIL: All items are labelled text.
- Audit: D-neural

### NW-04 (warn) enforced by `conform_network.probe_hops`

Expansion by hops follows knowledge edges only and skips hubs and index/MOC notes, so a second hop adds something.

- PASS: hops=2 returns more than hops=1 when knowledge edges exist.
- FAIL: hops=2 equals hops=1 because the caps are hit at hop 0.
- Audit: D-neural, P3-11

### NW-05 (fail) enforced by `conform_network.probe_path`

A reasoning path between two entities goes through knowledge edges, takes at most 3 steps, carries source sentences at every link, and reports 'not found' when no path exists. Every note a walk or path names (start, each step's target, end) is a node note, not a topic map; Results, claim and conflict notes appear only as the evidence note a step passes through, and naming one is refused (it exists, so never 'not found'); source == target is refused.

- PASS: Vitrification to ROS to gamma-H2A.X path returns >= 3 of 5 links with sentences; an impossible pair returns not found; a path from a node to itself is refused.
- FAIL: A path runs through paper notes alone or a link has no sentence, or a Results note or the start itself is an endpoint.
- Audit: D-graph, P3-11
- Params: `{max_steps: 3, min_chain_links_of_5: 3}`

### NW-06 (fail) enforced by `neural.check`

Vault notes are untrusted context: neural_check reads the server snapshot, rechecks note bytes, scope and provenance, and runs A1-A8; notes are never instructions or primary evidence.

- PASS: A reply citing an unknown note or altered snapshot is struck.
- FAIL: A caller-supplied item list replaces the snapshot.
- Audit: CONTRACTS-4.3, N10

### NW-07 (warn) enforced by `conform_network.probe_ticket_fingerprint`

The ticket fingerprint is a hash of content, not of file modification time, and plain tickets older than ticket_retention_days are cleaned; an open research session (NW-11) is never cleaned by age.

- PASS: Touching a file without changing it leaves the fingerprint; an old plain ticket is gone after a new prepare while an equally old open session stays.
- FAIL: A touched file invalidates the ticket, an old plain ticket stays, or an open session is cleaned.
- Audit: S16-07
- Params: `{ticket_retention_days: 7}`

### NW-08 (warn) enforced by `conform_network.probe_translation`

When the question needs translation, neural_prepare says so and searches with the English expansion.

- PASS: needs_translation is returned for a Vietnamese question.
- FAIL: A Vietnamese question is searched literally.
- Audit: P3-11

### NW-09 (fail) enforced by `conform_network.gold_benchmark`

The 8 gold questions V1-V5 and M1-M3 are the regression benchmark: idea coverage, first must rank, tokens and bytes are measured, absence questions V5 and M3 answer 'not found', and the V3 chain has at least 3 of 5 links.

- PASS: benchmark_research --gold meets the thresholds of NW-01, NW-02, NW-05.
- FAIL: A threshold is missed or an absence question returns confident evidence.
- Audit: D-gold

### NW-10 (fail) enforced by `G3`

A research session expands in bounded rounds: each round returns, for every WH slot the question asks (all brainstorm_slots when the researcher says 'brainstorm'), the item numbers that fill it and the missing slots, marked as a code heuristic from unit type, protocol field and node type, plus a stop reason: every asked slot has an item, no new matching item in no_gain_rounds rounds, or max_rounds or the token budget is reached; a missing slot is reported as missing, never filled from model memory, and the absence questions V5 and M3 still answer not found.

- PASS: A test session stops with each stop reason; coverage carries heuristic: true; a missing slot appears in Part 4; V5 and M3 return not found.
- FAIL: Expansion runs without a bound, a slot is reported filled without an item, or a missing slot is answered from model memory.
- Audit: client-app-2026-10-22-Y2, client-app-2026-10-22-Q2, RT-09
- Params: `{brainstorm_slots: [what, why, which, when, who, where, how], max_rounds: 4, no_gain_rounds: 2, question_words: config/wh_words.yaml, session_token_budget: 16000, slot_sources: {how: [method_detail, protocol.technique, protocol.concentration, protocol.dose, protocol.equipment, protocol.duration], what: [result, conclusion], when: [protocol.timing, protocol.duration, profile.stage], where: [profile.origin, profile.setting], which: [protocol.comparator, groups], who: [node.species, node.cell_type, groups], why: [interpretation, hypothesis, background]}}`

### NW-11 (fail) enforced by `G3`

A research session keeps the items of all its rounds in the server ticket until the researcher closes it or starts a new topic (said in chat; the client then closes the session or opens a new one); it never expires by time and open sessions are listed so a later chat can resume one; ask_check and neural_check of the session check every sentence against the union of those items, each round is fingerprinted by content, a round whose items changed is refused, and only closed sessions are cleaned (NW-07 cleaning skips open sessions); resuming or listing a session returns a compact index (item number, paper, page, one line) within token_budget_fast of NW-01, and full item text is fetched by number on demand, so a long session never has to be reloaded into the client's context; a round (and ask_prepare) may return the same compact index instead of full items, and a Part 2 sentence then cites only items whose full text was fetched by number (the session records them; the check warns on a cited item never fetched).

- PASS: A sentence citing a round-1 item checks in round 3; an altered round is refused; an open session survives any time span and a server restart and is listed for resuming; a closed session is removed; a new topic opens a new session; resuming a 500-item session returns at most token_budget_fast tokens.
- FAIL: The check retrieves again, numbers [#n] change between rounds, a caller-supplied list replaces the session, an open session is cleaned or expires by time, or a closed session stays.
- Audit: client-app-2026-10-22-Y4, client-app-2026-10-22-Q4, client-app-2026-10-22-NW11-change, AN-13, NW-06, NW-07

### NW-12 (fail) enforced by `G3`

Every retrieved item and every answer citation carries its vault-relative note path and an obsidian:// link of the current vault; a figure or PDF page is returned as an MCP image (at most max_image_mb) with caption, page and links; a number a client reads from an image is labelled as read from the image and is never evidence unless it occurs in the caption, a table row or a stored figure reading.

- PASS: neural_prepare and ask_prepare items carry note_path and obsidian_url; get_figure and get_pdf_page return an image block with caption, page and links; the answer protocol labels pixel-read numbers.
- FAIL: An item has no link, an image is returned without its caption or page, or a pixel-read number is cited as evidence.
- Audit: client-app-2026-10-22-Y5, client-app-2026-10-22-Q5, P13-P14
- Params: `{max_image_mb: 4}`

## RQ: Review queue and human decisions

### RQ-01 (fail) enforced by `conform_vault.queue_has_four_groups`

The review queue Cần duyệt.md has only four groups: (1) wrong knowledge in the source paper, (2) AI-made error whose fix is uncertain, (3) orphan papers that cannot be re-linked, (4) claims without a traceable source that machine checking could not decide.

- PASS: Every queue item belongs to one group.
- FAIL: An item of another kind is queued (review_digest.write still queues node cards, conflicts and open hypotheses today).
- Audit: PLAN-RQ

### RQ-02 (fail) enforced by `conform_vault.queue_excludes_other_kinds`

These never enter the queue: conflicts (they live in Conflicting findings with #conflict), AI cards, open hypotheses, wrong crops (re-cut by the secretary), unclear sentences (re-read wider and re-sorted by the secretary), node labels, types, merges, removals and machine-added concepts (decided by the machine under ND-10 and listed), and decisions to delete or push (asked in chat).

- PASS: None of those kinds is in the queue.
- FAIL: Such an item is queued (review_digest.write lists node cards, open conflicts, open hypotheses and wrong crops today).
- Audit: PLAN-RQ, STATUS-15

### RQ-03 (fail) enforced by `conform_vault.queue_items_are_self_contained`

Each queue item is self-contained: the source sentence copied by code, a link to the note and the PDF page, what the machine found, a proposal and three tick boxes (agree, keep as is, other).

- PASS: Every item has quote, link, finding, proposal and three boxes.
- FAIL: An item lacks one of them.
- Audit: PLAN-RQ

### RQ-04 (fail) enforced by `G3`

A claim is machine-checked against its sources first; invented text is removed to removed/ automatically and only what the machine cannot decide enters group 4.

- PASS: Queue group 4 holds only undecidable claims.
- FAIL: A fabricated claim waits for a human.
- Audit: PLAN-RQ

### RQ-05 (fail) enforced by `conform_vault.decisions_are_logged`

Applying a decision goes through the validators, is logged in review_decisions.jsonl (reversible), and never edits the PDF; an error in the source paper only gets a source_issue flag and a visible warning.

- PASS: Each applied decision has a log row and the PDF hash is unchanged.
- FAIL: A decision is applied without a log or the source is modified.
- Audit: PLAN-RQ

### RQ-06 (warn) enforced by `conform_vault.queue_is_easy_to_find`

The queue file sits at the vault root, is the first link of Home and is pinned, with three instruction lines at its top.

- PASS: The file exists at the root with the Home link first.
- FAIL: The researcher cannot find the queue (as on 13/10).
- Audit: STATUS-15

### RQ-07 (fail) enforced by `review_policy.refresh`

A paper is flagged only when no source-backed connection to the vault topic exists (computed POTENTIAL_BRIDGE); a stored human classification is never overwritten and no paper is purged.

- PASS: Flags are topic_relevance only and human classes are unchanged.
- FAIL: A human classification is overwritten or a paper is purged.
- Audit: CONTRACTS-9, AGENTS-classification

## SF: Safety and privacy

### SF-01 (fail) enforced by `mcp_server._check_args`

An MCP paper_id must match the paper-id pattern, contain no '..' and be a row of the papers table; a vault must be a research vault; a limit must be an integer clamped to 1..100 (read_paper 1..500).

- PASS: ../x, an absolute path, an unknown vault and limit -1 are refused or clamped.
- FAIL: A file outside knowledge_store/papers is read or an unlimited query runs.
- Audit: N14
- Params: `{max_limit: 100, max_limit_read_paper: 500}`

### SF-02 (fail) enforced by `discussions.save`

Discussions are written create-only (open 'x'), only into research vaults, with a single-line title; two saves never overwrite each other.

- PASS: Eight concurrent saves of one title make eight files.
- FAIL: A save overwrites another or writes into _Maps/Registry.
- Audit: N15

### SF-03 (fail) enforced by `G3`

Every destructive change first identifies what changes and keeps it recoverable (backup, removed/, history, snapshot); when unsure, quarantine or flag instead of deleting.

- PASS: A snapshot or history copy exists for each data write and Mine/ hashes are unchanged.
- FAIL: Data is deleted irrecoverably.
- Audit: AGENTS-safety

### SF-04 (fail) enforced by `G3`

Agents never delete source material, overwrite human notes, approve a knowledge gap or purge rejected papers; technical readiness is never scientific acceptance.

- PASS: Such actions occur only with a recorded researcher decision.
- FAIL: An agent performs one alone.
- Audit: AGENTS-approval

### SF-05 (fail) enforced by `package.release`

A kb.py package or release never ships secrets, the researcher's library, private plans or audit reports (they quote papers), and a release reports leak_count 0.

- PASS: package.release returns no leaks.
- FAIL: A private file or a library trace is shipped (1 old test leak is open).
- Audit: G1.4

### SF-06 (fail) enforced by `workflow.writer_lock`

One writer at a time takes the library lock; long jobs run detached with a log, nothing edits code or config during a rebuild, and Mine/ hashes are verified after it.

- PASS: A second writer waits or errors; Mine/ hashes are unchanged after a rebuild.
- FAIL: Two writers run, or Mine/ changes.
- Audit: AGENTS-7

### SF-07 (fail) enforced by `conform_network.probe_local_only`

The software runs no server beyond the 127.0.0.1 search page and the stdio MCP process, installs no dependency without the researcher's approval, keeps API keys only in config/secrets.yaml, and sends no paper to an external AI without approval.

- PASS: Only localhost listens; requirements are unchanged without approval.
- FAIL: An external listener, a new dependency or an unapproved upload is found.
- Audit: AGENTS-software
