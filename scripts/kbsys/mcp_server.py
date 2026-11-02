"""MCP server on stdio, so any MCP-connected AI app can use the knowledge base as tools.

The agent starts `kb.py mcp` as a child process and exchanges JSON-RPC 2.0 messages, one per
line, over stdin/stdout (Model Context Protocol). There is no network listener and nothing
runs in the background. Every tool calls the same functions as kb.py, so an analysis
submitted here goes through exactly the same checks (V1-V12).
Only the Python standard library is used.
"""

import json
import re
import sys
import traceback

from . import agent_tasks, answer, config, db, discussions, evidence, find, gaps, image_tools, overlap, relations, search, neural, node_cards, structure, workflow

PROTOCOLS = ["2025-06-18", "2025-03-26", "2024-11-05"]
SERVER = {"name": "biomedical-kb", "version": "3.0"}
CLIENT = {"name": "mcp-client"}  # set from initialize.clientInfo
BRAINSTORM_TOOLS = {"get_answer_rules", "neural_prepare", "open_session", "list_sessions", "close_session", "neural_check",
                    "ask_prepare", "ask_check", "get_context", "neural_walk", "neural_path", "list_conflicts", "get_figure",
                    "get_pdf_page", "read_vault_note", "save_discussion", "remember"}
TOOLSET = None   # None = every tool; `kb.py mcp --tools brainstorm` / `kb.py tool` set BRAINSTORM_TOOLS
MEMORY = False   # AN-20: brainstorm memory is served and written only when the server (or `kb.py tool`) was started with it
INSTRUCTIONS = ("Biomedical knowledge base of the researcher's own papers. Evidence must come from these "
                "tools, never from model memory. To analyse papers: call get_analysis_guide once, then "
                "list_pending_papers, read_paper, then add_units in small batches, skip_sentences, "
                "finish_analysis (one paper at a time). You can also do the other AI work: list_ai_tasks -> "
                "get_ai_task -> submit_ai_task (Concept Note synthesis, intervention enrichment, V13 cross-check), "
                "and answer questions with get_answer_rules (once) -> ask_prepare -> ask_check. Every reply is checked by code; report failed "
                "checks honestly. Demo3 also requires get/submit_section_profile and get/submit_protocol_profile. "
                "For evidence already done, complete only pending profiles; never rerun accepted analysis. "
                "Use neural_prepare -> neural_check for actual vault-note context and checked source answers. "
                "For a research topic of several questions use one session: neural_prepare(session='new'), then its id for "
                "every round and for neural_check/ask_check (numbers stay session-wide); list_sessions/open_session resume it, "
                "close_session only when the researcher closes it or starts a new topic; each round's coverage and "
                "stop_reason follow NW-10 (missing WH slots go to Part 4); search a new idea again before you use it; save a settled point "
                "only on 'chốt' (save_discussion kind=decision) and show returned decisions as 'ý chốt của bạn (ngày)', never as evidence. "
                "To save context pass index=true to neural_prepare (with a session) or ask_prepare: one-line entries come back; read the "
                "items you need in full with open_session(numbers) / ask_prepare(numbers) and cite in Part 2 only items read in full (check warns A12). "
                "Conflicting results are valuable: show them side by side (list_conflicts). "
                "To check pasted wording against stored sentences call find_overlaps (lexical overlap only). "
                "For ask_prepare/ask_check the default view is evidence4: return JSON with answer: [{text, evidence}], "
                "reasoning: [{text, basis}] and missing. The final client answer has exactly four Vietnamese headings: "
                "'## Bằng chứng từ tài liệu', '## Trả lời dựa trên bằng chứng', '## [REASONING] Phần suy luận của AI' "
                "and '## Không tìm thấy / không chắc chắn'. Part 1 is code-printed verbatim source text with "
                "[FirstAuthor, year, trang X] (chunk_id); never type or paraphrase quotes. Part 2 only translates or "
                "restates cited evidence and preserves numbers, hedges and causal strength. Part 3 is explicitly AI "
                "reasoning, may cite only supplied item indexes or server-ticket note references, and is never "
                "evidence; a Part 3 sentence of general knowledge has basis [\"" + answer.AI_KNOWLEDGE + "\"] alone and "
                "is shown as '" + answer.AI_KNOWLEDGE_LABEL + "', never in Part 2. Part 4 reports missing coverage and uncertainty plus the scope note that absence from the "
                "current vault does not mean absence from the literature. Submit the JSON to ask_check; show "
                "struck/failed sentences as returned by the checker.")


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required)}


TOOLS = [
    {'name': 'build_media', 'description': 'Writer-guarded one-paper media inventory; optional regions JSON path, no source re-extraction or scientific approval.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}, 'regions_path': {'type': 'string'}}, ['paper_id'])},
    {'name': 'media_status', 'description': 'Read media source inventory/current crop availability without publication.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}}, ['paper_id'])},
    {'name': 'structured_prepare', 'description': 'Prepare one bounded whole-paper overview, Introduction WH, or Methods chunk; complete valid profiles/evidence are preserved.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}, 'kind': {'enum': ['overview', 'wh', 'protocol']}, 'chunk_id': {'type': 'string'}}, ['paper_id', 'kind'])},
    {'name': 'structured_status', 'description': 'Read structured task checkpoints and technical coverage, separate from scientific approval.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}}, ['paper_id'])},
    {'name': 'structured_submit', 'description': 'Validate one source-revision-bound chunk, atomically keep accepted work, publish complete technical profile.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}, 'task_id': {'type': 'string'}, 'chunk_id': {'type': 'string'}, 'reply': {'type': 'object'}}, ['paper_id', 'task_id', 'chunk_id', 'reply'])},
    {'name': 'structured_run', 'description': 'Explicit optional analysis provider call for one paper, bounded chunks and max two attempts; allow_ai must be true. No answer fallback.',
     'inputSchema': _schema({'paper_id': {'type': 'string'}, 'kind': {'enum': ['overview', 'wh', 'protocol', 'both', 'all']}, 'allow_ai': {'type': 'boolean'},
                             'max_chunks': {'type': 'integer', 'minimum': 1, 'maximum': 20}, 'provider': {'type': 'string'},
                             'model': {'type': 'string'}, 'reasoning': {'type': 'string'}}, ['paper_id'])},
    {"name": "neural_search", "description": "Read actual Markdown and follow knowledge edges (typed links of result, claim, conflict and comparison notes; not navigation links or hub notes) within one vault; notes are context, not primary evidence.",
     "inputSchema": _schema({"vault": {"type": "string"}, "query": {"type": "string"}, "hops": {"type": "integer"}}, ["vault", "query"])},
    {"name": "neural_walk", "description": "Read-only: the knowledge-edge neighbours (typed links of result, claim, conflict and comparison notes, not navigation links) of one named node of a vault, each with the stored source sentences copied by code and a hub flag. etypes give each step's strength: hypothesis_only and cited_background steps are authors' hypotheses or cited background, not observed links. Nothing found means not found in the current vault.",
     "inputSchema": _schema({"vault": {"type": "string"}, "node": {"type": "string", "description": "exact title, alias or vault path of a note"}, "limit": {"type": "integer"}}, ["vault", "node"])},
    {"name": "neural_path", "description": f"Read-only: one shortest path of at most {neural.standard.param('NW-05', 'max_steps')} knowledge steps between two named nodes of a vault (never through paper notes), every step with its stored source sentences copied by code and its etypes (hypothesis_only and cited_background steps are not observed links). No path means not found in the current vault.",
     "inputSchema": _schema({"vault": {"type": "string"}, "source": {"type": "string"}, "target": {"type": "string"}}, ["vault", "source", "target"])},
    {"name": "read_vault_note", "description": "Read an untrusted source note with SHA256 and optional stale-snapshot check.",
     "inputSchema": _schema({"vault": {"type": "string"}, "path": {"type": "string"}, "sha256": {"type": "string"}}, ["vault", "path"])},
    {"name": "neural_prepare", "description": "Retrieve the vault network and numbered paper evidence; save a snapshot ticket for checking the answer. "
                                             "session \"new\" opens a research session, a session id adds a round to it (item numbers n are session-wide).",
     "inputSchema": _schema({"vault": {"type": "string"}, "question": {"type": "string"}, "hops": {"type": "integer"},
                             "offset": {"type": "integer", "minimum": 0, "description": "next_offset of the previous page (in a session the same question continues by itself)"},
                             "session": {"type": "string", "description": "\"new\" or the id of an open research session"},
                             "index": {"type": "boolean", "description": "needs a session: the round is made and saved as usual, the reply holds one-line entries "
                                                                         "{n, paper_id, page, unit_type, item_type, line} instead of the items; fetch full text with open_session(numbers)"}},
                            ["vault", "question"])},
    {"name": "neural_check", "description": "Check an answer against the server-owned retrieval ticket, or against every round of a research session; reject changed notes or paper evidence.",
     "inputSchema": _schema({"ticket": {"type": "string"}, "session": {"type": "string", "description": "a research session id (instead of ticket)"},
                             "reply": {"type": "object"}}, ["reply"])},
    {"name": "list_sessions", "description": "Open research sessions of a vault (topic, questions, rounds, item count), so a later chat can resume one.",
     "inputSchema": _schema({"vault": {"type": "string"}}, ["vault"])},
    {"name": "open_session", "description": "Resume a research session: without numbers a compact index page [{n, paper_id, page, line}] (has_more, next_offset); "
                                           "with numbers those items in full.",
     "inputSchema": _schema({"session": {"type": "string"}, "numbers": {"type": "array", "items": {"type": "integer"}},
                             "offset": {"type": "integer", "minimum": 0}}, ["session"])},
    {"name": "close_session", "description": "Only when the researcher closes the session or starts a new topic: remove the research session.",
     "inputSchema": _schema({"session": {"type": "string"}}, ["session"])},
    {"name": "get_section_profile", "description": "Introduction source context and source-linked inventory; answer five WH fields with sentence IDs, null/reason when absent.",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "submit_section_profile", "description": "Validate five Introduction WH answers; code copies source quotes. Does not replace evidence analysis.",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "reply": {"type": "object"}}, ["paper_id", "reply"])},
    {"name": "compare_protocols", "description": "Exact Methods order/source text and validated model/intervention fields for comparison in a vault.",
     "inputSchema": _schema({"vault": {"type": "string"}, "term": {"type": "string"}}, ["vault"])},
    {"name": "get_protocol_profile", "description": "Source context for full technique/chemical/instrument/model/step extraction; values must be exact source fragments.",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "submit_protocol_profile", "description": "Validate protocol details, retain order, copy quotes, and report every unassigned Methods sentence.",
      "inputSchema": _schema({"paper_id": {"type": "string"}, "reply": {"type": "object"}}, ["paper_id", "reply"])},
    {"name": "get_node_card_task", "description": "Prepare a bounded, source-grounded typed node-card task with sentence IDs and a source revision.",
     "inputSchema": _schema({"vault": {"type": "string"}, "name": {"type": "string"},
                             "include_sentence_ids": {"type": "array", "items": {"type": "string"}}}, ["vault", "name"])},
    {"name": "submit_node_card", "description": "Validate and store an AI-filled typed node card; code copies source quotes and preserves hedges.",
     "inputSchema": _schema({"vault": {"type": "string"}, "name": {"type": "string"}, "reply": {"type": "object"},
                             "analyst": {"type": "string"},
                             "include_sentence_ids": {"type": "array", "items": {"type": "string"}}},
                            ["vault", "name", "reply"])},
    {"name": "list_pending_figures", "description": "Figures/tables of a vault that still need a source-bound reading (same list as `kb.py figure-reading pending`).",
     "inputSchema": _schema({"vault": {"type": "string"}, "include_formulas": {"type": "boolean"}}, ["vault"])},
    {"name": "get_figure_task", "description": "Source-bound task for one figure/table crop: caption, mention sentences, allowed sentence IDs, reply format; "
                                              "returns crop_path and the crop image itself when small enough.",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "object_id": {"type": "string"}}, ["paper_id", "object_id"])},
    {"name": "get_figure", "description": "Read-only: one figure/table of a paper in a vault as an MCP image (the verified crop, at most the NW-12 size cap) with "
                                         "label, caption sentences copied by code, page, note_path + obsidian_url, the stored reading (never re-read) and, "
                                         "when there is no image, why. object_id is local (F1) or paper_id:F1. Numbers read from the image are not evidence "
                                         "unless they occur in the caption, a table row or a stored figure reading (NW-12).",
     "inputSchema": _schema({"vault": {"type": "string"}, "paper_id": {"type": "string"}, "object_id": {"type": "string"}}, ["vault", "paper_id", "object_id"])},
    {"name": "get_pdf_page", "description": f"Read-only: ONE page of the paper's main PDF rendered as an MCP image (page counted from 1; dpi {image_tools.DPI_MIN}-{image_tools.DPI_MAX}, "
                                           f"default {image_tools.DPI_DEFAULT}, lowered automatically under the NW-12 size cap) with page, page count and note_path + obsidian_url. "
                                           "A missing source file is reported as unavailable. Numbers read from the image are not evidence (NW-12).",
     "inputSchema": _schema({"vault": {"type": "string"}, "paper_id": {"type": "string"}, "page": {"type": "integer", "minimum": 1},
                             "dpi": {"type": "integer", "minimum": image_tools.DPI_MIN, "maximum": image_tools.DPI_MAX}}, ["vault", "paper_id", "page"])},
    {"name": "submit_figure_reading", "description": "Validate and store an AI figure/table reading; code copies source quotes and checks numbers.",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "object_id": {"type": "string"}, "reply": {"type": "object"},
                             "analyst": {"type": "string"}}, ["paper_id", "object_id", "reply"])},
    {"name": "workflow_status", "description": "Durable checkpoints; waiting/failed/running must not be called completed.", "inputSchema": _schema({})},
    {"name": "quality_check", "description": "Offline font/encoding/OCR preflight; inspect before paying for analysis.",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "get_analysis_guide",
     "description": "The rules and JSON format for analysing a paper into evidence units. Read before the first analysis.",
     "inputSchema": _schema({})},
    {"name": "list_pending_papers",
     "description": "Papers that still need an analysis (pending or needs_fix), with their status and attempt.",
     "inputSchema": _schema({"vault": {"type": "string", "description": "only papers of this vault"}})},
    {"name": "read_paper",
     "description": "A paper as numbered sentences (S0001…) with section, page, resolved citations and "
                    "must_cover flags. Paged: call again with start=next_start.",
     "inputSchema": _schema({"paper_id": {"type": "string"},
                             "section": {"type": "string", "description": "e.g. results, methods, discussion"},
                             "start": {"type": "integer", "description": "first sentence number (default 1)"},
                             "limit": {"type": "integer", "description": "sentences per page (default 200, max 500)"},
                             "format": {"type": "string", "enum": ["json", "compact"],
                                        "description": "compact reduces repeated labels without losing source text"}},
                            ["paper_id"])},
    {"name": "lookup_reference",
     "description": "What a citation number points to in that paper's own reference list.",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "number": {"type": "integer"}},
                            ["paper_id", "number"])},
    {"name": "get_context",
     "description": "The sentences before and after one sentence (to check context before relying on it).",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "sentence_id": {"type": "string"},
                             "window": {"type": "integer", "description": "sentences on each side (default 3)"}},
                            ["paper_id", "sentence_id"])},
    {"name": "submit_analysis",
     "description": "Submit the complete analysis of one paper. It is checked (V1-V12); the result lists "
                    "errors to fix, sentences still to cover, and the next step. For result and method_detail "
                    "units, at least one of concepts/methods/molecules/cell_types/species must be non-empty.",
     "inputSchema": _schema({"paper_id": {"type": "string"},
                             "submission": {"type": "object", "description": "see get_analysis_guide"}},
                            ["paper_id", "submission"])},
    {"name": "add_units",
     "description": "STEP-BY-STEP analysis (recommended): add up to ~10 evidence units at a time to the paper's "
                    "draft. Each unit is checked at once; units with errors are not kept (fix and add again). "
                    "Result and method_detail units require at least one non-empty scientific terms list. "
                    "Returns draft numbers and the must_cover sentences still uncovered.",
     "inputSchema": _schema({"paper_id": {"type": "string"},
                             "units": {"type": "array", "items": {"type": "object"},
                                       "description": "evidence units, format in get_analysis_guide"}},
                            ["paper_id", "units"])},
    {"name": "skip_sentences",
     "description": "Mark must_cover sentences that deserve no unit, each with a short reason.",
     "inputSchema": _schema({"paper_id": {"type": "string"},
                             "skipped": {"type": "array", "items": {"type": "object"},
                                         "description": "[{sentence_id, reason}]"}},
                            ["paper_id", "skipped"])},
    {"name": "remove_units",
     "description": "Remove units from the draft by draft number (to replace a wrong one).",
     "inputSchema": _schema({"paper_id": {"type": "string"},
                             "draft_numbers": {"type": "array", "items": {"type": "integer"}}},
                            ["paper_id", "draft_numbers"])},
    {"name": "draft_status",
     "description": "The draft so far: units (short), skipped count, must_cover sentences still uncovered.",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "finish_analysis",
     "description": "Submit the draft with the paper-level fields. Runs the full check (V1-V12, coverage) and "
                    "stores the result exactly like submit_analysis. summary.units are draft numbers; "
                    "result/method_detail units require at least one non-empty terms list.",
     "inputSchema": _schema({"paper_id": {"type": "string"}, "analyst": {"type": "string"},
                             "classification": {"type": "string"}, "classification_reason": {"type": "string"},
                             "evidence_type": {"type": "string"},
                             "research_question": {"type": "object", "description": "{text, sentence_ids}"},
                             "topics": {"type": "array", "items": {"type": "string"}},
                             "summary": {"type": "array", "items": {"type": "object"},
                                         "description": "[{text, units: [draft numbers]}]"}},
                            ["paper_id", "analyst", "classification", "classification_reason", "evidence_type"])},
    {"name": "analysis_status",
     "description": "Status of a paper's analysis: pending, needs_fix, done or review, with the last errors.",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "get_evidence",
     "description": "Stored evidence units of a paper (statement, verbatim quote, sentence ids, pages, status).",
     "inputSchema": _schema({"paper_id": {"type": "string"}}, ["paper_id"])},
    {"name": "list_ai_tasks",
     "description": "AI work waiting in a vault: synthesis (Concept Notes), enrichment (intervention/model "
                    "fields of results), crosscheck (independent V13 review). You act as the AI provider.",
     "inputSchema": _schema({"vault": {"type": "string"}, "kind": {"type": "string",
                             "enum": ["synthesis", "enrichment", "crosscheck"]}}, ["vault"])},
    {"name": "get_ai_task",
     "description": "The exact prompt(s) for one task (id = concept_id for synthesis, paper_id otherwise). "
                    "Answer only from the supplied records, in the reply_format given.",
     "inputSchema": _schema({"kind": {"type": "string", "enum": ["synthesis", "enrichment", "crosscheck"]},
                             "id": {"type": "string"}, "vault": {"type": "string"}}, ["kind", "id", "vault"])},
    {"name": "submit_ai_task",
     "description": "Submit your reply for a task. It passes the same checks as any built-in AI (A1-A6 for "
                    "synthesis; whole-word source match for enrichment; schema for V13). Say which model you are.",
     "inputSchema": _schema({"kind": {"type": "string", "enum": ["synthesis", "enrichment", "crosscheck"]},
                             "id": {"type": "string"}, "vault": {"type": "string"},
                             "reply": {"type": ["object", "string"]}, "model": {"type": "string"}},
                            ["kind", "id", "vault", "reply"])},
    {"name": "ask_prepare",
     "description": "Answer a research question from the vault: give the question and 1-4 English search "
                    "queries; returns the numbered evidence, its items_digest, a not_found flag and the exact answering prompt.",
     "inputSchema": _schema({"question": {"type": "string"}, "queries": {"type": "array", "items": {"type": "string"}},
                             "vault": {"type": "string"}, "view": {"type": "string", "enum": ["evidence4", "google", "report5"]},
                             "speed": {"type": "string", "enum": ["fast", "deep"]},
                             "index": {"type": "boolean", "description": "one-line entries {n, paper_id, page, unit_type, item_type, line} instead of the prompt (same numbers and items_digest)"},
                             "numbers": {"type": "array", "items": {"type": "integer"}, "description": "those items in full with their links, no prompt"},
                             "offset": {"type": "integer", "minimum": 0, "description": "index: next_offset of the previous page"}}, ["question", "queries"])},
    {"name": "ask_check",
     "description": "Check your answer (same question/queries/view/speed as ask_prepare). Returns each sentence "
                    "with its check result, and markdown with the reason of each struck sentence; struck sentences must not "
                    "be presented as findings. Checks the same items ask_prepare numbered (items_digest).",
     "inputSchema": _schema({"question": {"type": "string"}, "queries": {"type": "array", "items": {"type": "string"}},
                             "reply": {"type": ["object", "string"]}, "vault": {"type": "string"},
                             "view": {"type": "string", "enum": ["evidence4", "google", "report5"]},
                             "speed": {"type": "string", "enum": ["fast", "deep"]}, "model": {"type": "string"},
                             "items_digest": {"type": "string", "description": "the items_digest ask_prepare returned (optional); "
                                              "a different digest refuses the check"},
                             "session": {"type": "string", "description": "a research session id: check against all its rounds"}},
                            ["question", "queries", "reply"])},
    {"name": "get_answer_rules",
     "description": "The answer protocol every AI follows when answering the researcher: 1 lookup (verbatim italic "
                    "quotes + bold analysis), 2 related saved discussions and Mine/ notes, 3 evidence-backed brainstorm, "
                    "4 continue. Read it once before answering. With vault (and memory switched on) it also returns the "
                    "researcher's profile and that vault's topic notes: context, never evidence (AN-20).",
     "inputSchema": _schema({"vault": {"type": "string"}}, [])},
    {"name": "remember",
     "description": "AN-20: append ONE dated line to the brainstorm memory: scope profile (shared by the project) or topic (this vault). "
                    "by=researcher only for what the researcher said about themselves in their own words, by=ai for your own search notes. "
                    "Context for later chats, never evidence. A full file is refused; earlier lines are never changed.",
     "inputSchema": _schema({"vault": {"type": "string"}, "text": {"type": "string"}, "by": {"type": "string", "enum": ["researcher", "ai"]},
                             "scope": {"type": "string", "enum": ["profile", "topic"]}}, ["vault", "text", "by", "scope"])},
    {"name": "save_discussion",
     "description": "Only when the researcher says 'lưu' (save): store the discussion as a `discussion` note in the "
                    "vault's Discussions/ folder (shown in the graph, never used as evidence). Keep every citation and "
                    "[[note]] link in the markdown. kind=decision: save it only when the researcher says 'chốt' "
                    "(settled); pass their own words verbatim as researcher_words and the item ids (sentence ids of "
                    "the prepared items) they relied on as citations; the date and the quotes are added by code.",
     "inputSchema": _schema({"vault": {"type": "string"}, "title": {"type": "string"},
                             "markdown": {"type": "string"}, "model": {"type": "string"},
                             "kind": {"type": "string", "enum": ["discussion", "knowledge_gap", "decision"]},
                             "researcher_words": {"type": "string", "description": "decision: the researcher's own words, verbatim, containing 'chốt'"},
                             "citations": {"type": "array", "items": {"type": "string"}, "description": "decision: item ids"},
                             "earlier": {"type": "string", "description": "decision: an earlier decision note in Discussions/ (default: the newest of the same title)"}},
                            ["vault", "title", "markdown"])},
    {"name": "find_overlaps",
     "description": "Paste a passage; returns stored sentences of the vault that share wording with it (shared word "
                    "5-grams), with matched character spans, per-paper coverage, section and page. This is lexical "
                    "overlap for human review, never a plagiarism verdict; absence means not found in this vault.",
     "inputSchema": _schema({"text": {"type": "string"}, "vault": {"type": "string"},
                             "limit": {"type": "integer"}}, ["text"])},
    {"name": "list_conflicts",
     "description": "Cross-paper disagreements in a vault (contradicts / direction_differs) with both results, "
                    "so they can be shown side by side.",
     "inputSchema": _schema({"vault": {"type": "string"}}, ["vault"])},
    {"name": "list_gap_candidates",
     "description": "Candidate knowledge gaps of a vault (generated by `kb.py gaps` or proposed by agents). Every row is a "
                    "candidate needing the researcher's review: 'not found in the current vault' is never 'absent from "
                    "the literature'.",
     "inputSchema": _schema({"vault": {"type": "string"}}, ["vault"])},
    {"name": "propose_gap",
     "description": "Propose ONE candidate gap (status needs_review, provenance analyst) backed by accepted evidence unit "
                    "IDs of the vault. State what you searched in queries_run. Never claim the literature lacks it.",
     "inputSchema": _schema({"vault": {"type": "string"}, "description": {"type": "string"},
                             "supporting_units": {"type": "array", "items": {"type": "string"}},
                             "gap_type": {"type": "string", "enum": list(gaps.GAP_TYPES)},
                             "queries_run": {"type": "array", "items": {"type": "string"}},
                             "papers_scanned": {"type": "integer"}},
                            ["vault", "description", "supporting_units", "gap_type"])},
    {"name": "search_sentences",
     "description": "Full-text search over sentences of the papers in a vault (words are ANDed; \"quoted "
                    "phrase\" is exact; configured synonyms are expanded). Returns sentences with paper, "
                    "section and page.",
     "inputSchema": _schema({"query": {"type": "string"}, "vault": {"type": "string"},
                             "all_vaults": {"type": "boolean"}, "limit": {"type": "integer"}}, ["query"])},
]


def _sentence_search(con, cfg, query, vault=None, all_vaults=False, limit=30):
    match, _ = search.parse_query(query, cfg, db.fts_glyph_tokens(con))
    if not match:
        return []
    sql = ["""SELECT s.sentence_id, s.paper_id, s.page, s.section, s.kind, s.text,
                     pa.first_author, pa.year, pa.title
              FROM sentences_fts JOIN sentences s ON s.rowid = sentences_fts.rowid
              JOIN papers pa ON pa.paper_id = s.paper_id
              WHERE sentences_fts MATCH ? AND s.section != 'references'"""]
    args = [match]
    if not all_vaults:
        sql.append("AND s.paper_id IN (SELECT paper_id FROM paper_vaults WHERE vault = ?)")
        args.append(vault or config.default_vault(cfg))
    sql.append("ORDER BY bm25(sentences_fts) LIMIT ?")
    args.append(min(int(limit or 30), 100))
    return [dict(r) for r in con.execute("\n".join(sql), args)]


def _conflicts(vault, con=None):
    rows = []
    config.require_research_vault(vault)
    for rel in relations.conflict_relations(vault):
        sides = []
        for uid, direction in zip((rel["from_unit"], rel["to_unit"]), rel.get("directions") or [None, None]):
            pid = uid.rsplit(":", 1)[0]
            unit = evidence.resolve_unit(con,uid) or {}
            sides.append({"unit_id": uid, "paper_id": pid, "direction": direction, "statement": unit.get("statement"),
                          "quote": unit.get("quote"), "pages": unit.get("pages"), "sections": unit.get("sections")})
        rows.append({"type": rel["type"], "status": rel.get("status"), "measure_group": rel.get("measure_group"),
                     "rationale": rel.get("rationale"), "differences": rel.get("differences"), "sides": sides})
    return rows


LIMIT_MAX = {"read_paper": 500}  # sentences per page; every other `limit` is capped at 100 rows


def _check_args(con, name, args):
    """Reject what must never reach the store: a paper_id that is not a stored paper (`../x` would read files
    outside knowledge_store/papers), a vault that is not a research vault, a `limit` that is not an integer
    (clamped to 1..cap; a negative LIMIT in SQLite means 'no limit')."""
    paper_id = args.get("paper_id")
    if paper_id is not None:
        if (not isinstance(paper_id, str) or not find.PAPER_ID.fullmatch(paper_id) or ".." in paper_id
                or not con.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone()):
            raise ValueError(f"Unknown paper_id: {paper_id!r}")
    if args.get("vault") not in (None, ""):
        config.require_research_vault(args["vault"])
    if args.get("items_digest") is not None and not isinstance(args["items_digest"], str):
        raise ValueError("items_digest must be a string")
    if args.get("index") is not None and not isinstance(args["index"], bool):
        raise ValueError("index must be a boolean")
    session = args.get("session")   # NW-11: "new" opens a session in neural_prepare only; elsewhere a 32-hex id
    if session is not None and not (isinstance(session, str) and (re.fullmatch("[0-9a-f]{32}", session)
                                                                   or (name == "neural_prepare" and session == "new"))):
        raise ValueError("session must be \"new\" (neural_prepare) or a research session id")
    for key in ("researcher_words", "earlier"):
        if args.get(key) is not None and not isinstance(args[key], str):
            raise ValueError(f"{key} must be a string")
    if args.get("citations") is not None and (not isinstance(args["citations"], list)
                                              or not all(isinstance(c, str) for c in args["citations"])):
        raise ValueError("citations must be a list of strings")
    if args.get("numbers") is not None and (not isinstance(args["numbers"], list)
                                            or not all(type(n) is int for n in args["numbers"])):
        raise ValueError("numbers must be a list of integers")
    if args.get("offset") is not None and (type(args["offset"]) is not int or args["offset"] < 0):
        raise ValueError("offset must be an integer >= 0")
    for key in {"neural_walk": ("node",), "neural_path": ("source", "target")}.get(name, ()):
        if not isinstance(args.get(key), str) or not args[key].strip():
            raise ValueError(f"{key} must be a non-empty string")
    if "page" in args and (type(args["page"]) is not int or args["page"] < 1):
        raise ValueError("page must be an integer >= 1")
    if args.get("dpi") is not None and (type(args["dpi"]) is not int or not image_tools.DPI_MIN <= args["dpi"] <= image_tools.DPI_MAX):
        raise ValueError(f"dpi must be an integer from {image_tools.DPI_MIN} to {image_tools.DPI_MAX}")
    if "object_id" in args and (not isinstance(args["object_id"], str) or not args["object_id"].strip()
                                or any(bad in args["object_id"] for bad in ("/", "\\", ".."))):
        raise ValueError("object_id must be a non-empty string without a path separator or '..'")
    if args.get("limit") is not None:
        if isinstance(args["limit"], bool) or not isinstance(args["limit"], int):
            raise ValueError("limit must be an integer")
        args["limit"] = max(1, min(args["limit"], LIMIT_MAX.get(name, 100)))


def _with_html(con, out, checked, snapshot):
    """U10b: add html_path / html_url (a local page of the checked four-part answer) to a check reply. Written here only,
    never in the shared check (the web page, gold gate and conform run it too); a failed page never fails the check.
    `snapshot()` -> (vault, items, question) the reply was checked against."""
    if not (isinstance(checked, dict) and "reasoning" in checked):   # the evidence4 shape only
        return out
    try:
        vault, items, question = snapshot()
        cards = find.paper_cards(con, {i["paper_id"] for i in items if i.get("paper_id")})
        path = answer.write_html(checked, items, question, vault, cards)
    except Exception as exc:   # the check result is the evidence; the page is only a view of it
        return {**out, "html_error": str(exc)}
    return {**out, "html_path": str(path), "html_url": path.as_uri()}


def call_tool(con, cfg, name, args):
    if name == 'neural_search':
        return neural.search(args['vault'], args['query'], args.get('hops', 0))
    if name == 'neural_walk':
        return neural.walk(con, args['vault'], args['node'], args.get('limit', 20))
    if name == 'neural_path':
        return neural.find_path(con, args['vault'], args['source'], args['target'])
    if name == 'read_vault_note':
        return neural.read_note(args['vault'], args['path'], args.get('sha256'))
    if name == 'neural_prepare':
        out = neural.prepare(con, cfg, args['vault'], args['question'], args.get('hops', 0), offset=args.get('offset'),
                             session=args.get('session'), index=bool(args.get('index')))
        neural.mark_decisions_flagged(args['vault'], out.get('decisions') or [])   # AN-19: shown to the client, so not again
        return out
    if name == 'neural_check':
        ticket = args.get('session') or args.get('ticket')
        checked = neural.check(con, ticket, args['reply'])
        return _with_html(con, dict(checked), checked, lambda: neural.ticket_snapshot(ticket))
    if name == 'list_sessions':
        return neural.list_sessions(args['vault'])
    if name == 'open_session':
        return neural.open_session(args['session'], args.get('numbers'), args.get('offset'))
    if name == 'close_session':
        return neural.close_session(args['session'])
    if name in ('build_media', 'media_status'):
        from . import pipeline
        return pipeline.build_media(args['paper_id'], args.get('regions_path') if name == 'build_media' else None, persist=name == 'build_media')
    if name == 'structured_prepare':
        return structure.prepare_structured(args['paper_id'], args.get('kind', 'wh'), args.get('chunk_id'))
    if name == 'structured_status':
        return structure.structured_status(args['paper_id'])
    if name == 'structured_submit':
        return structure.submit_structured(con, args['paper_id'], args['task_id'], args['chunk_id'], args['reply'], CLIENT.get('name') or 'mcp-client')
    if name == 'structured_run':
        from . import analysis_runner
        return analysis_runner.run_structured(con, args['paper_id'], args.get('kind', 'both'), args.get('allow_ai', False),
                                              args.get('max_chunks', 1), args.get('provider'), args.get('model'), args.get('reasoning'))
    if name == 'get_section_profile':
        return {'inventory': structure.build(args['paper_id'], persist=False), 'task': structure.prepare(args['paper_id'])}
    if name == 'submit_section_profile':
        return structure.submit_profile(con, args['paper_id'], args['reply'], CLIENT.get('name') or 'mcp-client')
    if name == 'compare_protocols':
        return structure.compare_methods(con, args['vault'], args.get('term', ''))
    if name == 'get_protocol_profile':
        return structure.prepare_protocol(args['paper_id'])
    if name == 'submit_protocol_profile':
        return structure.submit_protocol(con, args['paper_id'], args['reply'], CLIENT.get('name') or 'mcp-client')
    if name == 'get_node_card_task':
        return node_cards.task(con, args['vault'], args['name'], args.get('include_sentence_ids'))
    if name == 'submit_node_card':
        return node_cards.submit(con, args['vault'], args['name'], args['reply'], args.get('analyst') or CLIENT.get('name') or 'mcp-client',
                                  args.get('include_sentence_ids'))
    if name == 'list_pending_figures':
        from . import figure_reading
        return figure_reading.pending(con, args['vault'], bool(args.get('include_formulas')))
    if name == 'get_figure_task':
        from . import figure_reading
        return figure_reading.task(args['paper_id'], args['object_id'])
    if name == 'get_figure':
        return image_tools.get_figure(con, args['vault'], args['paper_id'], args['object_id'])
    if name == 'get_pdf_page':
        return image_tools.get_pdf_page(con, args['vault'], args['paper_id'], args['page'], args.get('dpi'))
    if name == 'submit_figure_reading':
        from . import figure_reading
        return figure_reading.submit(args['paper_id'], args['object_id'], args['reply'],
                                     args.get('analyst') or CLIENT.get('name') or 'mcp-client')
    if name == 'workflow_status':
        return workflow.status()
    if name == "get_analysis_guide":
        return config.analyst_prompt()
    if name == "list_pending_papers":
        q = "SELECT p.paper_id, p.first_author, p.year, p.title, p.analysis_status FROM papers p"
        values = []
        if args.get("vault"):
            q += " JOIN paper_vaults v ON v.paper_id = p.paper_id WHERE v.vault = ? AND"
            values.append(args["vault"])
        else:
            q += " WHERE"
        q += " 1=1 ORDER BY p.ingested_at"
        rows = []
        for r in con.execute(q, values):
            state = evidence.status(r["paper_id"])
            profiles = structure.completion(r['paper_id'])
            needs_profiles = (cfg.get('analysis') or {}).get('require_structured_profiles') and not profiles['complete']
            if (state.get('status') or r['analysis_status']) != 'done' or needs_profiles:
                rows.append({**dict(r), "attempt": state.get("attempt", 0), "state": state.get("status"), "structured_profiles": profiles})
        return rows
    if name == "read_paper":
        view = evidence.reading_view(con, args["paper_id"], args.get("section"), int(args.get("start") or 1),
                                     int(args.get("limit") or 200))
        if args.get("format") == "compact":
            from .analysis_transport import compact_view
            return compact_view(view)
        return view
    if name == "quality_check":
        from . import quality
        return quality.inspect(args["paper_id"])
    if name == "lookup_reference":
        for i, r in enumerate(db.read_jsonl(db.paper_dir(args["paper_id"]) / "references.jsonl"), start=1):
            if (r.get("num") or i) == int(args["number"]):
                return r
        return {"error": f"reference {args['number']} not found in {args['paper_id']}"}
    if name == "get_context":
        ctx = evidence.PaperContext(con, args["paper_id"])
        target = ctx.sentences.get(ctx.sid(args["sentence_id"]))
        if not target:
            return {"error": f"sentence {args['sentence_id']} not found"}
        window = int(args.get("window") or 3)
        return [{"id": s["sentence_id"].split(":")[-1], "section": s["section"], "page": s["page"],
                 "kind": s["kind"], "text": s["text"]}
                for s in sorted(ctx.sentences.values(), key=lambda s: s["seq"])
                if abs(s["seq"] - target["seq"]) <= window]
    if name == "submit_analysis":
        submission = args["submission"]
        if isinstance(submission, str):  # some clients send the object as a JSON string
            submission = json.loads(submission)
        result = evidence.submit(con, args["paper_id"], submission)
        con.commit()
        return result
    if name in ("add_units", "skip_sentences", "remove_units", "finish_analysis"):
        for key in ("units", "skipped", "draft_numbers", "summary", "research_question", "topics"):
            if isinstance(args.get(key), str):  # some clients send objects as JSON strings
                args[key] = json.loads(args[key])
    if name == "add_units":
        return evidence.add_units(con, args["paper_id"], args["units"])
    if name == "skip_sentences":
        return evidence.skip_sentences(con, args["paper_id"], args["skipped"])
    if name == "remove_units":
        return evidence.remove_units(con, args["paper_id"], args["draft_numbers"])
    if name == "draft_status":
        return evidence.draft_status(con, args["paper_id"])
    if name == "finish_analysis":
        result = evidence.finish(con, args["paper_id"], {k: v for k, v in args.items() if k != "paper_id"})
        con.commit()
        profiles = structure.completion(args['paper_id'])
        return {**result, 'structured_profiles': profiles,
                'demo3_complete': result.get('status') == 'done' and profiles['complete']}
    if name == "analysis_status":
        return evidence.status(args["paper_id"])
    if name == "get_evidence":
        return list(db.read_jsonl(db.paper_dir(args["paper_id"]) / "evidence.jsonl"))
    client = CLIENT.get("name") or "mcp-client"
    if name == "list_ai_tasks":
        return agent_tasks.list_tasks(con, args["vault"], args.get("kind"))
    if name == "get_ai_task":
        return agent_tasks.get_task(con, args["kind"], args["id"], args["vault"], client)
    if name == "submit_ai_task":
        return agent_tasks.submit_task(con, args["kind"], args["id"], args["vault"], args["reply"], client,
                                       args.get("model"))
    if name == "ask_prepare":
        out = agent_tasks.answer_prompt(con, cfg, args["question"], args["queries"], args.get("view") or "evidence4",
                                        args.get("vault"), args.get("speed") or "fast", client, bool(args.get("index")),
                                        args.get("numbers"), args.get("offset"))
        neural.mark_decisions_flagged(args.get("vault") or config.default_vault(cfg), out.get("decisions") or [])   # AN-19
        return out
    if name == "ask_check":
        items = []
        out = agent_tasks.answer_check(con, cfg, args["question"], args["queries"], args["reply"],
                                       args.get("view") or "evidence4", args.get("vault"), args.get("speed") or "fast",
                                       client, args.get("model"), args.get("items_digest"), args.get("session"), items)
        vault = args.get("vault") or (out.get("answer") or {}).get("vault") or config.default_vault(cfg)
        return _with_html(con, out, out.get("answer"), lambda: (vault, items, args["question"]))
    if name == "get_answer_rules":
        return {"protocol": discussions.protocol(), **({"memory": discussions.memory(args["vault"])} if MEMORY and args.get("vault") else {})}
    if name == "remember":
        if not MEMORY:
            raise ValueError("brainstorm memory is off on this server (start it with --memory, or use kb.py tool)")
        return discussions.remember(args["vault"], args["text"], args["by"], args["scope"])
    if name == "save_discussion":
        return discussions.save(args["vault"], args["title"], args["markdown"], args.get("model") or client,
                                args.get("kind") or "discussion", con=con, researcher_words=args.get("researcher_words"),
                                citations=args.get("citations"), earlier=args.get("earlier"))
    if name == "find_overlaps":
        return overlap.find_overlaps(con, args["text"], args.get("vault"), args.get("limit") or 20)
    if name == "list_conflicts":
        return _conflicts(args["vault"],con)
    if name == "list_gap_candidates":
        return gaps.list_candidates(args["vault"])
    if name == "propose_gap":
        units = json.loads(args["supporting_units"]) if isinstance(args["supporting_units"], str) else args["supporting_units"]
        return gaps.propose(con, args["vault"], args["description"], units, args["gap_type"], client,
                            args.get("queries_run"), args.get("papers_scanned"))
    if name == "search_sentences":
        return _sentence_search(con, cfg, args["query"], args.get("vault"), bool(args.get("all_vaults")),
                                args.get("limit") or 30)
    raise KeyError(f"unknown tool {name}")


def _visible_tools():
    return [t for t in TOOLS if TOOLSET is None or t["name"] in TOOLSET]


def handle(con, cfg, message):
    """One JSON-RPC message -> response dict, or None for notifications."""
    if not isinstance(message, dict):  # batches and bare values are not used by MCP clients
        return {"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid Request"}}
    method, msg_id = message.get("method"), message.get("id")
    if msg_id is None:  # notification (e.g. notifications/initialized)
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    if method == "initialize":
        asked = params.get("protocolVersion")
        info = params.get("clientInfo") if isinstance(params.get("clientInfo"), dict) else {}
        CLIENT["name"] = str(info.get("name") or "mcp-client")[:60]
        return {"jsonrpc": "2.0", "id": msg_id, "result": {
            "protocolVersion": asked if asked in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": _visible_tools()}}
    if method == "tools/call":
        tool = next((t for t in _visible_tools() if t["name"] == params.get("name")), None)
        args = params.get("arguments") or {}
        missing = [k for k in (tool or {}).get("inputSchema", {}).get("required", []) if args.get(k) in (None, "")] \
            if isinstance(args, dict) else ["arguments"]
        if not tool or missing:
            return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32602, "message": (
                f"Unknown tool: {params.get('name')}" if not tool else f"Missing arguments: {', '.join(missing)}")}}
        try:
            from contextlib import nullcontext
            mutations = {'submit_analysis', 'add_units', 'skip_sentences', 'remove_units', 'finish_analysis', 'submit_ai_task', 'submit_section_profile', 'submit_protocol_profile', 'submit_node_card', 'submit_figure_reading',
                         'build_media', 'structured_prepare', 'structured_submit', 'propose_gap', 'save_discussion'}  # structured_run (AI call) locks only around its own writes
            _check_args(con, tool["name"], args)
            with workflow.writer_lock() if tool['name'] in mutations else nullcontext():
                value = call_tool(con, cfg, tool["name"], args)
            key = IMAGE_KEYS.get(tool["name"])  # image-capable clients also get the picture; one path for every image tool
            image = value.get(key) if key and isinstance(value, dict) else None
            if isinstance(image, bytes):
                del value[key]  # PNG bytes never go into the JSON text (a crop_path stays: it is a path)
            text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            content = [{"type": "text", "text": text}] + _image_content(image)
            return {"jsonrpc": "2.0", "id": msg_id, "result": {"content": content, "isError": False}}
        except Exception as err:  # reported to the agent as a tool error, the server keeps running
            con.rollback()
            config.log(f"MCP tool {params.get('name')} failed: {traceback.format_exc()}")
            return {"jsonrpc": "2.0", "id": msg_id, "result": {
                "content": [{"type": "text", "text": f"{type(err).__name__}: {err}"}], "isError": True}}
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": f"Method not found: {method}"}}


IMAGE_KEYS = {"get_figure_task": "crop_path", "get_figure": "image", "get_pdf_page": "image"}  # tool -> key of its value holding the image


def _image_content(image, limit=None):
    """MCP image block for a file path or PNG bytes; empty when missing or over `limit` (default NW-12 max_image_mb)."""
    import base64
    import mimetypes
    from pathlib import Path
    limit = image_tools.max_image_bytes() if limit is None else limit
    try:
        if isinstance(image, bytes):
            data, mime = image, "image/jpeg" if image[:3] == b"\xff\xd8\xff" else "image/png"  # a retained crop may be a .jpg
        else:
            p = Path(image)
            data, mime = (p.read_bytes() if p.is_file() and p.stat().st_size <= limit else b""), mimetypes.guess_type(p.name)[0] or "image/png"
        if not data or len(data) > limit:
            return []
        return [{"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime}]
    except (OSError, TypeError):
        return []


def serve():
    channel = sys.stdout.buffer
    if hasattr(sys.stderr, "reconfigure"):  # Vietnamese log lines must not fail on a cp1252 console
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout = sys.stderr  # anything a library prints must not corrupt the protocol channel
    cfg = config.load()
    con = db.connect()
    try:
        for raw in sys.stdin.buffer:
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                response = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}}
            else:
                response = handle(con, cfg, message)
            if response is not None:
                channel.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                channel.flush()
    finally:
        con.close()
