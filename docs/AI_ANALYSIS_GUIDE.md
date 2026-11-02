# How AI analyses a paper

Part A is the instruction an AI analyst receives: `analysis_runner`, the MCP tool `get_analysis_guide` and `kb.py analysis-guide`
send only the text between the HTML comments `begin-analyst-prompt` and `end-analyst-prompt` (verbatim from the approved v2 guide). Part B is for the person or developer: connecting AI apps, providers, review policy.

---

<!-- begin-analyst-prompt -->
# How to analyse a paper (v2) — instructions for AI agents

You turn one paper into **evidence units**: small, structured statements, each resting on
1–6 numbered sentences of the paper. You never type quotes: you give sentence ids, and the
software copies the verbatim text. Every submission is checked by code (V1–V12); rejected
units come back with precise messages. Formats: `docs/CONTRACTS.md` §3.
Project rules: `AGENTS.md` (never use your own knowledge as evidence; association is not
causation; hypotheses are not findings; review statements are not primary evidence).

## Workflow

**Demo3 extension:** cover all body sections, including plain Introduction and Methods sentences,
or supply an explicit source-specific skip reason. Abstract is reference material. Retain uncertainty,
comparison citations, methods details and source object captions. Before completing a paper, call
`get_section_profile` → `submit_section_profile` for all five Introduction WH answers and
`get_protocol_profile` → `submit_protocol_profile` for complete ordered Methods protocols.
Echo the source fingerprint and revision; cite IDs so code copies the quotes. Unknown WH answers
require null with a reason. Missing protocol fields remain absent. Do not guess doses or instruments.
`list_pending_papers` also includes done evidence with missing/stale profiles: do only those profiles,
preserving accepted units. `finish_analysis.status=done` is the evidence status; `demo3_complete` and
the analyze checkpoint also require both current profiles. This does not certify scientific correctness.

1. `list_pending_papers` → pick one paper (one at a time).
2. `quality_check(paper_id)` first: if `ready_for_analysis` is false, repair extraction before analysis.
   A clear scan is not proof of perfect recognition; inspect uncertain table crops before assigning columns.
   `read_paper(paper_id, format="compact")` → numbered sentences (`S0142`), section, page, resolved citations
   (`cites: ["R004 Marlow 2010"]`) and `must_cover: true` on sentences that every analysis must
   cover. Long papers come in pages: call again with `start = next_start`.
   `continues: "S0166"` marks a sentence cut by a page break or a figure: its next part is S0166.
   Both parts are one sentence — cite them together (they count as one place for V1); citing
   either part covers the must_cover requirement.
   `lookup_reference(paper_id, 4)` shows what reference 4 is. Compact output groups repeated metadata in
   headers and preserves every sentence verbatim; `next_start` still controls pagination. CLI:
   `kb.py read-paper <id> --compact`. Do not reread already read pages without a context need.
3. **Recommended (MCP): build the analysis in small steps.** Never write the whole analysis in
   one message: long JSON gets cut off.
   - `add_units(paper_id, units)` with **at most 10 units per call**. Each unit is checked at once;
     a unit with errors is not kept: read the error, fix the unit, add it again. The answer shows
     `next_uncovered`: must_cover sentences not yet used.
   - `skip_sentences(paper_id, [{sentence_id, reason}])` for must_cover sentences without content
     worth a unit.
   - `remove_units(paper_id, [draft numbers])` to replace a unit; `draft_status(paper_id)` to review.
   - When `must_cover_remaining` is 0: `finish_analysis(paper_id, analyst, classification,
     classification_reason, evidence_type, research_question?, topics?, summary?)`. `summary.units`
     are draft numbers. This is the submission that is checked and stored.
   (One-shot alternative for short papers or the command line: write the whole submission below
   and call `submit_analysis(paper_id, submission)`.)
4. Read the result:
   - `status: done` → finished, go to the next paper.
   - `status: needs_fix` → fix every item in `errors`, cover every id in `coverage_missing`
     (with a unit, or in `skipped` with a reason), and submit the **complete** analysis again.
   - `status: review` → attempts used up; the researcher will check. Go to the next paper.
   Do not argue with a rule by rewording: if a number is not in the cited sentence, cite the
   sentence (or table row) that contains it, or remove the number.

(Command-line equivalents: `kb.py read-paper <id>`, `kb.py ref <id> <n>`,
`kb.py submit-analysis <id> <file.json>`, `kb.py analysis-status <id>`.)

## Submission

```json
{
  "paper_id": "smith2020_example_paper_title_1a2b",
  "analyst": "hermes/deepseek-chat",
  "classification": "CORE | CONNECTED | POTENTIAL_BRIDGE | UNCERTAIN | OUT_OF_SCOPE",
  "classification_reason": "One sentence, relative to the vault's topic.",
  "evidence_type": "primary_research | review | systematic_review | meta_analysis | protocol | case_report | commentary | other",
  "research_question": {"text": "…", "sentence_ids": ["S0021"]},
  "topics": ["Myocardial infarction"],
  "units": [ … ],
  "skipped": [{"sentence_id": "S0061", "reason": "reagent supplier only"}],
  "summary": [{"text": "Marker X increased after the example treatment.", "units": [3, 4]}]
}
```

`summary` sentences point at the (1-based) positions of the submitted units, including only
accepted unit positions; rejected or duplicate positions are rejected. They may only contain
numbers found in those units' sentences. A research question grounded outside the abstract,
introduction, or title is retained with a warning for review.

## Evidence unit

For every `result` and `method_detail` unit, include at least one non-empty list among
`concepts`, `methods`, `molecules`, `cell_types`, and `species`. A missing or empty set is a
V9 error and the unit is sent back. Other unit types may leave these lists empty. Terms are
normalised to the shared local registry (one canonical spelling and one type); do not create
folder-qualified links or duplicate a topic when it is an entity such as `Muse cell`.

`outcome.direction` and `model.species` of a `result` unit may be left empty: the code fills what the cited sentences
name (one direction word group, one species; else the paper title/abstract species) and records `filled_by`. Fill them
yourself only when the sentence leaves them ambiguous (mixed or contrast wording, several species); your value is never replaced.
One unit states one finding: a statement that turns from an increase to a decrease ("rose in A but fell in B") is flagged V7; split it.

```json
{
  "unit_type": "result",
  "statement": "In AMI patients, the number of SSEA3+/CD105+ Muse cells in peripheral blood increased on day 1 compared with controls.",
  "sentence_ids": ["S0142"],
  "model": {"species": "Human", "cell_type": "Muse cell", "stage": null, "origin": "peripheral blood", "setting": "in vivo"},
  "intervention": {"agent": null, "dose": null, "timing": null, "duration": null, "protocol": null, "comparator": "Healthy controls"},
  "outcome": {"measure": "Muse cell number", "method": "Flow cytometry", "direction": "increase",
              "values": ["P < 0.05"], "value_source": "text", "n": null, "p": "< 0.05", "source_objects": ["F2"]},
  "quality": {"design": "prospective observational", "replicates": null, "is_primary": true},
  "hedging": "none",
  "hedged_sentence_ids": ["S0142"],
  "cited_refs": [],
  "terms": {"concepts": ["Stem cell mobilization"], "cell_types": ["Muse cell"], "species": ["Human"],
            "methods": ["Flow cytometry"], "molecules": ["SSEA-3", "CD105"], "topics": []}
}
```

Unknown → omit the optional field or use `null` (never guess). Empty optional objects/lists may be
omitted; code supplies defaults. Keep known scientific information. `unit_type`:

| type | use for |
|---|---|
| `result` | this paper's own finding, from Results (or a table row) — never a sentence that cites others |
| `method_detail` | dose, concentration, timing, protocol, instrument, statistics used |
| `interpretation` | the authors explaining their results (Discussion) |
| `hypothesis` | proposed but not shown ("may", "we propose") |
| `conclusion` / `limitation` | the authors' conclusion / a limitation they state |
| `cited_statement` | what this paper says **another** study found: statement written as attribution ("Marlow et al. [R004] reported that …"), `cited_refs: ["R004"]` from the sentence's citations |
| `background` | general context without a specific citation |

## Rules the checker enforces

| Code | Rule | If broken |
|---|---|---|
| V1 | sentence ids exist, ≤6, one place in the text (same passage, ≤5 body sentences apart — captions/table rows in between and the parts of a `continues` sentence do not count —, or all rows of one table object) | rejected |
| V2 | every number in the statement, `values`, `n`, `p` is in the cited sentences (citation markers do not count); signed values retain a leading minus sign, while a dropped negative sign is accepted with a warning; numbers in `dose`/`timing`/`duration`/`model` must occur in the paper | rejected / warning for dropped sign |
| V3 | species, cell type, agent, measure, method and terms appear in the paper (use its wording) | warning |
| V4 | `result` comes from Results (or a table row) and never from a review paper | warning |
| V5 | if the source is hedged ("may", "suggest", "probably"), keep the hedge in interpretation/hypothesis/conclusion/background/cited statements and do not write "causes/induces/demonstrates"; if it only reports an association ("associated", "correlated"), write "was associated with", never "reduces/improves/causes"; set `hedging: "hedged"` only when the sentence that carries the claim is hedged ("not significant" / P > 0.05 is not a hedge for this field); when a unit cites more than one sentence, give `hedged_sentence_ids` (the sentence(s) that carry the claim, a subset of `sentence_ids`) | warning |
| V6 | a sentence that cites other papers cannot be a `result`; `cited_refs` must be cited in those sentences; a sentence without a resolved citation (no `cites`) cannot be a `cited_statement` — use `background`/`interpretation` | rejected |
| V7 | `direction` agrees with the source wording (increase/decrease/no_change); ignore location words before anatomical nouns, and warn on mixed increase/decrease wording unless the selected direction is near the outcome measure | warning |
| V8 | every `must_cover` sentence (numeric results, dosed methods, citing sentences, Conclusion, interpretive or hedged Discussion) is used by a unit or listed in `skipped` with a reason | sent back |
| V9 | terms are normalised through the shared one-type registry; new `result`/`method_detail` units need at least one non-empty scientific terms list | rejected if missing, otherwise fixed |
| V10 | classification, reason, evidence_type, analyst present and valid; `units` a non-empty list | nothing stored (other errors: the accepted units are still stored) |
| V11 | `result`/`method_detail` cite data (Results/Methods text, main text of a paper without headings, or a table row), not a caption, title, Introduction or Discussion | rejected |
| V12 | `value_source: figure_only` → no values, n or p (never read numbers off a figure); `table` → cite the row | rejected |

## Good practice

- One finding per unit; a sentence with two findings → two units citing the same sentence.
- Tables: cite the `table_row` sentences (they carry column names when the table is reliable).
  Rows of tables marked uncertain have no column names: do not assign columns you cannot see.
- Keep the authors' hedging and comparison ("compared with controls", "P < 0.05").
- Author–year papers: writing "(Marlow et al. 2010)" in a statement is fine; the year of a cited reference is not
  treated as data. Numbers must otherwise be copied exactly as printed ("2.5 × 10^4", not "2.5e4").
- Statements are in English. Explanations for the researcher in Vietnamese belong in your chat, not here.
- Do not skip sentences to save effort: `skipped` is for sentences with no content worth a unit
  (supplier names, instrument models already covered, repeated statements).
<!-- end-analyst-prompt -->

---

# Part B — Connecting AI and review policy (for the researcher and developer)

## 1. Two ways to use AI (use one or both)

**A. An AI app through MCP (recommended for sharing).** The knowledge base ships an MCP server `biomedical-kb` (stdio, started by
the app on demand, no network port, no background service). The app uses its own account and model; everything it submits goes through
the same checks. Fastest setup: double-click **`2 - Ket noi AI (MCP).bat`** or run `kb.py mcp-config`: it prints ready settings with
this installation's paths for Claude Desktop, Claude Code, Codex, Gemini CLI, Cursor and generic stdio apps, and
`kb.py mcp-config --install claude-desktop|codex|gemini-cli|cursor` merges them into that app's config (a `.bak-<timestamp>` backup is
made first). Restart the app afterwards.

Manual settings (replace `<project>` with the project folder, e.g. `C:\kb`):
- Claude Code: `claude mcp add biomedical-kb -- <project>\.venv\Scripts\python.exe <project>\scripts\kb.py mcp`
- Codex `~/.codex/config.toml`: `[mcp_servers.biomedical_kb]` with `command = '<project>\.venv\Scripts\python.exe'`,
  `args = ['<project>\scripts\kb.py', 'mcp']`, `default_tools_approval_mode = "approve"`, `tool_timeout_sec = 300`.
- Hermes `~/.hermes/config.yaml` under `mcp_servers:` add `biomedical_kb` with the same `command`/`args`, `timeout: 300`,
  `connect_timeout: 60` (use single quotes to keep backslashes); in WSL use `/mnt/c/kb/.venv/Scripts/python.exe`
  as command and the Windows path for the script. In Hermes type `/reload-mcp`.

Then tell the AI: "Use the biomedical_kb tools. Read get_analysis_guide, then analyse each paper in list_pending_papers of vault <V>
following the guide. Submit with finish_analysis (or submit_analysis); fix needs_fix errors and resubmit. At the end report which
papers are done and which went to review." Each paper gets at most `analysis.max_attempts` (3) submissions; what still fails goes to
the Review Queue. Other AI work through MCP: `list_ai_tasks → get_ai_task → submit_ai_task`, `ask_prepare → ask_check`,
`get_node_card_task → submit_node_card`, `structured_*`, `neural_*` (list in `docs/CONTRACTS.md` section 10).

Cheap model for one automatic paper with Codex: `codex exec -m gpt-5.6-luna -c model_reasoning_effort=low -s read-only -C <project> "<task>"`.
Measured cost of one real paper (390 sentences, luna): evidence about 70 000 input and 13 000 output tokens; the whole
pilot with retries 576 000 input (181 000 cached) and 53 000 output over 24 calls. Costs depend on model and repair count.

**B. Software calls AI itself.** Set `ai.provider` in `config/ai.yaml` to `ollama` (free, local), `anthropic`, `gemini`, `deepseek`,
`openrouter`, `openai_compatible`, `codex_cli` (the Codex login on this machine, no key) or `command` (any CLI reading the prompt on stdin,
for example `["claude", "-p"]`); `mcp_agent` = never call AI. Keys only in `config/secrets.yaml` (`<provider>_api_key`, git-ignored, never
packaged). Each task can have its own provider under `ai.tasks` (`analysis, answer, synthesis, enrichment, crosscheck`); use a
different model for `crosscheck` (V13) than the analyst. Restart the web page after editing. Never send papers to a provider the
researcher has not approved in chat.

## 2. Reading the result

In Obsidian open the paper note, section **AI analysis**: groups (results, methods, interpretation, citations…) open on click; each item
shows the verbatim quote, section, page and sentence ID. Items marked with a warning list their reasons under "Units with warnings":
that is where the researcher should check. Whole-library check: `kb.py selftest`.

## 3. Review policy (what is flagged and what is not)

- A paper is flagged only when no source-backed connection to the vault topic is found. The researcher's scope comes from the vault
  `Home.md` heading, explicit `topic_terms: [English term, alias]` and registry names occurring in the Home text. Generated topic notes,
  index hubs and AI tags never widen the scope.
- Connection = title or a body source sentence matching the scope; a concept link backed by a source sentence; source-matched
  result/method entities of a topic-grounded paper; or a high-confidence citation to an in-vault anchor. No connection → computed
  `POTENTIAL_BRIDGE` for human review. Stored human classification is never overwritten; no paper is purged.
- Validator flags, rejected analyses and coverage gaps are kept as evidence details, not queue flags. Only `ok`/`human_confirmed`
  units feed concept syntheses and relations.
- Exact duplicate evidence in one paper is merged reversibly (aliases keep old IDs); different models, doses, outcomes or hedging are
  never merged. Identical incoming PDFs join the canonical paper and are quarantined recoverably.
- Run `kb.py refresh-review-policy --vault V` (dry-run first, then `--apply` after a snapshot with `tools/research_workspace.py`).
  Stop other writers and Obsidian first. Edited human notes are kept byte-for-byte.
