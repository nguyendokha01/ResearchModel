"""Offline regression tests: python -m unittest tests.test_analysis_runner (no provider calls)."""
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock
from contextlib import contextmanager, nullcontext

from kbsys import analysis_transport as t, analysis_runner as r, answer, evidence, quality, package
import yaml


class UnwrapTests(unittest.TestCase):
    def test_single_wrapper_is_unwrapped_other_shapes_untouched(self):
        fmt = {'research_question': [], 'main_findings': []}
        wrapped = {'source_sha256': 'x', 'overview': {'research_question': [{'text': 'q'}]}}
        self.assertEqual(r._unwrap(wrapped, fmt), {'research_question': [{'text': 'q'}], 'source_sha256': 'x'})
        flat = {'research_question': [], 'source_sha256': 'x'}
        self.assertIs(r._unwrap(flat, fmt), flat)
        two = {'a': {'research_question': []}, 'b': {'main_findings': []}}
        self.assertIs(r._unwrap(two, fmt), two)  # ambiguous: left for the validator to reject

    def test_missing_single_wrapper_is_added(self):
        fmt = {'answers': {k: {} for k in ('what', 'why', 'who', 'where', 'when')}}
        flat = {'source_sha256': 'x', **{k: {'text': k} for k in fmt['answers']}}
        self.assertEqual(r._unwrap(flat, fmt), {'answers': {k: {'text': k} for k in fmt['answers']}, 'source_sha256': 'x'})
        partial = {'what': {}, 'why': {}}
        self.assertIs(r._unwrap(partial, fmt), partial)  # incomplete: left for the validator to reject


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.rows = [{"id": f"S{i:04d}", "text": f"Source sentence {i}: μ ± −80°C.",
                      "source": "main", "section": "results", "page": 1, "kind": "text"}
                     for i in range(1, 31)]
        self.previous = {"units": [{"statement": "Accepted finding", "sentence_ids": ["S0001"]},
                                   {"statement": "Incorrect finding", "sentence_ids": ["S0020"]}],
                         "classification": "CORE", "summary": [{"text": "Accepted finding", "units": [1]}]}
        self.result = {"errors": [{"unit_index": 2, "code": "V2", "message": "Wrong value"}],
                       "coverage_missing": ["S0021"]}

    def test_compact_is_lossless(self):
        self.rows[0].update(cites=["R001 Smith 2000 (uncertain)"], must_cover=True, continues="S0002")
        self.rows[1]["in_table"] = True
        self.rows[-1]["source"] = "P:X1"
        text = t.compact_reading(self.rows)
        for s in self.rows:
            self.assertIn(s["id"], text)
            self.assertIn(s["text"], text)
        for marker in ("uncertain", "MUST", "continues=S0002", "in_table", "P:X1"):
            self.assertIn(marker, text)
        self.assertEqual(text.count('[main | results'), 1)

    def test_patch_preserves_accepted_units_and_summary_indices(self):
        before = copy.deepcopy(self.previous)
        new = t.merge_patch(self.previous, {"replace_units": [{"index": 2, "unit": {"statement": "Fixed"}}],
                                            "add_units": [{"statement": "Missing finding"}]}, self.result)
        self.assertEqual(self.previous, before)
        self.assertEqual(new["units"][0], before["units"][0])
        self.assertEqual(new["summary"], before["summary"])
        self.assertEqual(len(new["units"]), 3)

    def test_move_valid_top_level_terms_without_changing_claims(self):
        original = {"units": [{"unit_type": "method_detail", "sentence_ids": ["S0100"],
                               "statement": "Regression was used.", "methods": ["Logistic regression"],
                               "species": ["Human"], "terms": {"methods": ["Regression"]}},
                              {"unit_type": "result", "molecules": "not a list"}]}
        fixed = t.normalize_term_placement(original)
        self.assertEqual(original['units'][0]['methods'], ["Logistic regression"])
        self.assertEqual(fixed['units'][0]['terms'],
                         {"methods": ["Regression", "Logistic regression"], "species": ["Human"]})
        self.assertEqual(fixed['units'][0]['statement'], original['units'][0]['statement'])
        self.assertNotIn('methods', fixed['units'][0])
        self.assertEqual(fixed['units'][1]['molecules'], 'not a list')
        patch_reply = {"replace_units": [{"index": 2, "unit": {"methods": ["Flow cytometry"]}}],
                       "add_units": [{"species": ["Mouse"]}]}
        fixed_patch = t.normalize_term_placement(patch_reply)
        self.assertEqual(fixed_patch['replace_units'][0]['unit']['terms']['methods'], ["Flow cytometry"])
        self.assertEqual(fixed_patch['add_units'][0]['terms']['species'], ["Mouse"])
        self.assertEqual(patch_reply['replace_units'][0]['unit']['methods'], ["Flow cytometry"])

    def test_malformed_patches_cannot_change_accepted_content(self):
        for bad in ({"units": []}, {"replace_units": [{"index": 1, "unit": {}}]},
                    {"replace_units": [{"index": True, "unit": {}}]}, {"add_units": [None]},
                    {"fields": {"analyst": "spoof"}}, {"skipped": "all"},
                    {"replace_units": [{"index": 2, "unit": {}}, {"index": 2, "unit": {}}]}):
            before = copy.deepcopy(self.previous)
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                t.merge_patch(self.previous, bad, self.result)
            self.assertEqual(self.previous, before)

    def test_explicit_skip_list_replacement_can_remove_invalid_ids(self):
        self.previous["skipped"] = [{"sentence_id": "P:S0002", "reason": "old"},
                                    {"sentence_id": "S0003", "reason": "keep"}]
        merged = t.merge_patch(self.previous, {"skipped": [{"sentence_id": "S0002", "reason": "fixed"}]}, self.result)
        self.assertEqual(merged["skipped"], [{"sentence_id": "S0002", "reason": "fixed"}])
        unchanged = t.merge_patch(self.previous, {}, self.result)
        self.assertEqual(unchanged["skipped"], self.previous["skipped"])

    def test_repair_context_keeps_continuations_and_requested_context(self):
        self.rows[19]["continues"] = "S0029"
        self.rows[28]["continues"] = "S0030"
        selected = t.repair_context(self.rows, self.previous, self.result)
        ids = {s["id"] for s in selected}
        self.assertTrue({"S0017", "S0020", "S0024", "S0029", "S0030"} <= ids)
        self.assertNotIn("S0001", ids)
        self.assertEqual(t.repair_context(self.rows, self.previous, self.result, "all"), self.rows)

    def test_paper_level_error_gets_full_source(self):
        self.result["errors"].append({"unit_index": 0})
        self.assertEqual(t.repair_context(self.rows, self.previous, self.result), self.rows)

    def test_provider_usage_and_missing_usage(self):
        self.assertEqual(t.normalized_usage({}), {})
        self.assertEqual(t.normalized_usage({"prompt_tokens": 100, "completion_tokens": 20,
                                            "prompt_tokens_details": {"cached_tokens": 80}}),
                         {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 80})
        self.assertEqual(t.normalized_usage({"input_tokens": 10, "cache_read_input_tokens": 80,
                                            "cache_creation_input_tokens": 10})["input_tokens"], 100)

    def test_quality_detects_unreadable_characters_not_scientific_symbols(self):
        self.assertEqual(quality.suspicious_characters('μ α β ± −80°C 10^5'), [])
        self.assertEqual(len(quality.suspicious_characters('\ufffd\ue000\x07')), 3)

    def test_package_disables_task_specific_automatic_providers(self):
        config = 'ai:\n  provider: codex_cli\n  tasks:\n    analysis: codex_cli\n    synthesis: anthropic\n'
        exported = yaml.safe_load(package._student_config('config/ai.yaml', config))
        self.assertEqual(exported['ai']['provider'], 'mcp_agent')
        self.assertEqual(exported['ai']['tasks'], {})

    def test_package_never_ships_private_docs(self):
        for name in ('docs/demo3/inputs/Ve_phan_tach.docx', 'docs/rag_benchmark/questions.json', 'docs/STATUS.md',
                     'config/secrets.yaml', '.claude/skills/codex-job/SKILL.md'):
            self.assertTrue(package.NEVER.search(name), name)
        self.assertFalse(package.NEVER.search('docs/AI_ANALYSIS_GUIDE.md'))

    def test_duplicate_claim_count_preserves_distinct_claims(self):
        con = sqlite3.connect(':memory:')
        con.execute('CREATE TABLE evidence (paper_id TEXT, unit_type TEXT, statement TEXT, sentence_ids TEXT)')
        con.executemany('INSERT INTO evidence VALUES (?,?,?,?)', [
            ('P', 'result', 'Same claim', '["S0001"]'),
            ('P', 'result', 'Same claim', '["S0001"]'),
            ('P', 'result', 'Different claim', '["S0001"]'),
            ('Q', 'result', 'Same claim', '["S0001"]')])
        self.assertEqual(evidence.duplicate_claim_count(con, 'P'), 1)
        self.assertEqual(evidence.duplicate_claim_count(con, 'Q'), 0)
        con.close()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.state = {"status": "pending", "attempt": 0}
        self.submissions = []
        self.prompts = []
        self.rows = [{"id": f"S{i:04d}", "text": "This is a source sentence with a scientific result.",
                      "section": "results", "page": 1, "kind": "text"} for i in range(1, 80)]
        self.initial = {"classification": "CORE", "classification_reason": "Relevant", "evidence_type": "primary_research",
                        "units": [{"statement": "Keep this accepted result", "sentence_ids": ["S0001"]},
                                  {"statement": "This needs correction", "sentence_ids": ["S0040"]}]}
        self.correct = {"replace_units": [{"index": 2, "unit": {"statement": "Corrected result", "sentence_ids": ["S0040"]}}]}
        self.patches = [patch.object(r.db, 'paper_dir', return_value=self.folder),
                        patch.object(r.quality, 'inspect', return_value={"ready_for_analysis": True}),
                        patch.object(r.evidence, 'status', side_effect=lambda _: copy.deepcopy(self.state)),
                        patch.object(r.evidence, 'submit', side_effect=self.submit),
                        patch.object(r.answer, 'settings', return_value={"provider": "fake"}),
                        patch.object(r.answer, 'provider_status', return_value={"ready": True}),
                        patch.object(r.answer, 'model_of', return_value="offline-test"),
                        patch.object(r.config, 'load', return_value={"analysis": {"max_attempts": 3}}),
                        patch.object(r, '_vault_topic', return_value="Test vault"),
                        patch.object(r.workflow, 'writer_lock', side_effect=lambda **_: nullcontext()),
                        patch.object(r, '_reading_text', return_value=(t.compact_reading(self.rows), {"sentences": self.rows}))]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.temp.cleanup()

    def submit(self, con, pid, data, max_attempts=None):
        self.submissions.append(copy.deepcopy(data))
        self.state = {"status": "needs_fix" if len(self.submissions) == 1 else "done", "attempt": len(self.submissions)}
        return {**self.state, "accepted_units": len(data["units"]), "errors":
                [{"unit_index": 2, "code": "V2", "message": "wrong number"}] if len(self.submissions) == 1 else [],
                "coverage_missing": []}

    def chat(self, replies):
        values = iter(replies)
        def call(s, system, user):
            self.prompts.append(user)
            reply = next(values)
            if isinstance(reply, Exception):
                raise reply
            return (reply if isinstance(reply, str) else json.dumps(reply)), {"prompt_tokens": 100, "completion_tokens": 10}
        return patch.object(r.answer, '_chat', side_effect=call)

    def run_paper(self):
        return r.analyze_paper(Mock(), "P", "test", say=lambda _: None)

    def test_end_to_end_repairs_only_failed_unit(self):
        with self.chat([self.initial, self.correct]):
            self.assertEqual(self.run_paper()["status"], "done")
        self.assertEqual(self.submissions[1]["units"][0], self.initial["units"][0])
        self.assertNotIn("\nS0001 ", self.prompts[1])
        self.assertIn("S0040", self.prompts[1])
        self.assertEqual(r.usage_report("P")["reported_token_totals"]["input_tokens"], 200)

    def test_writer_lock_is_free_during_provider_call_and_held_during_submit(self):
        depth = {"n": 0}
        seen = {"chat": [], "submit": []}

        @contextmanager
        def tracking_lock(**_):
            depth["n"] += 1
            try:
                yield
            finally:
                depth["n"] -= 1

        real_submit = self.submit

        def submit(*args, **kwargs):
            seen["submit"].append(depth["n"])
            return real_submit(*args, **kwargs)

        replies = iter([self.initial, self.correct])

        def chat(s, system, user):
            seen["chat"].append(depth["n"])
            return json.dumps(next(replies)), {}

        with patch.object(r.workflow, 'writer_lock', side_effect=tracking_lock),                 patch.object(r.evidence, 'submit', side_effect=submit),                 patch.object(r.answer, '_chat', side_effect=chat):
            self.assertEqual(self.run_paper()["status"], "done")
        self.assertEqual(seen["chat"], [0, 0])
        self.assertEqual(seen["submit"], [1, 1])

    def test_runner_moves_new_term_lists_before_validation(self):
        initial = copy.deepcopy(self.initial)
        initial['units'][0]['methods'] = ['Accepted method']
        initial['units'][1]['methods'] = ['Old method']
        correction = {'replace_units': [{'index': 2, 'unit':
                       {'statement': 'Corrected', 'sentence_ids': ['S0040'], 'species': ['Human']}}]}
        with self.chat([initial, correction]):
            self.assertEqual(self.run_paper()['status'], 'done')
        self.assertEqual(self.submissions[0]['units'][0]['terms']['methods'], ['Accepted method'])
        self.assertEqual(self.submissions[1]['units'][0], self.submissions[0]['units'][0])
        self.assertEqual(self.submissions[1]['units'][1]['terms']['species'], ['Human'])

    def test_truncated_reply_does_not_submit_or_erase_good_units(self):
        with self.chat([self.initial, '{"replace_units":[{"unit":{}}', self.correct]):
            self.assertEqual(self.run_paper()["status"], "done")
        self.assertEqual(len(self.submissions), 2)

    def test_restart_after_provider_failure_resumes_repair(self):
        with self.chat([self.initial, answer.AIError("offline")]), self.assertRaises(answer.AIError):
            self.run_paper()
        with self.chat([self.correct]):
            self.assertEqual(self.run_paper()["status"], "done")
        self.assertIn("REPAIR MODE", self.prompts[-1])
        self.assertEqual(len(self.submissions), 2)

    def test_returned_reply_is_reused_after_store_failure(self):
        def locked(*a, **k):
            raise RuntimeError("database is locked")
        with self.chat([self.initial]), patch.object(r.evidence, 'submit', side_effect=locked), self.assertRaises(RuntimeError):
            self.run_paper()
        with self.chat([self.correct]):
            self.assertEqual(self.run_paper()["status"], "done")
        self.assertEqual(len(self.prompts), 2)  # the initial reply was submitted from disk, not requested again
        self.assertEqual(self.submissions[0]["units"], self.initial["units"])

    def test_context_request_does_not_consume_validation_attempt(self):
        with self.chat([self.initial, {"need_context": "all"}, self.correct]):
            self.run_paper()
        self.assertIn("\nS0001 ", self.prompts[-1])
        self.assertEqual(len(self.submissions), 2)

    def test_done_paper_never_calls_provider(self):
        self.state = {"status": "done"}
        with self.chat([]):
            self.assertEqual(self.run_paper()["status"], "done")

    def test_bad_extraction_never_calls_provider(self):
        with patch.object(r.quality, 'inspect', return_value={"ready_for_analysis": False}), self.chat([]):
            with self.assertRaises(answer.AIError):
                self.run_paper()
        self.assertTrue((self.folder / 'analysis_quality.json').is_file())

    def test_malformed_replies_are_bounded_across_restarts(self):
        with self.chat(['not JSON'] * 5), self.assertRaises(answer.AIError):
            self.run_paper()
        with self.chat([]), self.assertRaises(answer.AIError):
            self.run_paper()
        self.assertEqual(len(self.prompts), 5)
        self.assertEqual(self.submissions, [])


if __name__ == '__main__':
    unittest.main()
