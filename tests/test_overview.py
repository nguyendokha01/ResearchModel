"""D3.20 whole-paper outline/overview tests; all data stays in a temporary store."""
import copy
import json
import unittest
from unittest.mock import patch

from kbsys import db, mcp_server, structure
from . import test_core


class OverviewTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def reply(self, task, ids=None):
        ids = ids or [self.rows[0]['sentence_id']]
        item = {'text': 'The study may affect the example model.', 'sentence_ids': ids, 'certainty': 'may'}
        return {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')} | {
            field: [copy.deepcopy(item)] for field in structure.OVERVIEW_FIELDS}

    def test_outline_is_deterministic_and_keeps_objects(self):
        db.write_jsonl(self.folder / 'figures.jsonl', [{'figure_id': self.pid + ':F1', 'label': 'Figure 1',
            'page': 1, 'caption_sentence_ids': [self.rows[0]['sentence_id']]}])
        first = structure.build_outline(self.pid)
        second = structure.build_outline(self.pid)
        self.assertEqual(first, second)
        self.assertEqual(first['sections'][0]['first_sentence_id'], self.rows[0]['sentence_id'])
        self.assertEqual(first['sections'][0]['sentence_count'], 2)
        self.assertEqual(first['objects'][0]['caption_sentence_ids'], [self.rows[0]['sentence_id']])

    def test_overview_rejects_bad_ids_missing_ids_and_missing_certainty(self):
        task = structure.prepare_overview(self.pid)
        bad = self.reply(task, ['foreign:S0001'])
        with self.assertRaisesRegex(ValueError, 'existing sentence IDs'):
            structure.submit_overview(self.con, self.pid, bad, 'test')
        bad = self.reply(task)
        del bad['main_findings'][0]['sentence_ids']
        with self.assertRaisesRegex(ValueError, 'text, sentence_ids and certainty'):
            structure.submit_overview(self.con, self.pid, bad, 'test')
        bad = self.reply(task)
        del bad['limitations'][0]['certainty']
        with self.assertRaisesRegex(ValueError, 'text, sentence_ids and certainty'):
            structure.submit_overview(self.con, self.pid, bad, 'test')

    def test_overview_stales_when_source_changes(self):
        task = structure.prepare_overview(self.pid)
        self.assertTrue(structure.submit_overview(self.con, self.pid, self.reply(task), 'test')['ok'])
        self.assertTrue(structure.overview_status(self.pid)['ready'])
        rows = copy.deepcopy(self.rows)
        rows[0]['text'] += ' Changed.'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        self.assertFalse(structure.overview_status(self.pid)['ready'])

    def test_chunk_has_reference_context_and_accepted_chunks_survive_overview_change(self):
        task = structure.prepare_structured(self.pid, 'wh')
        self.assertIn('paper_outline', task)
        self.assertIsNone(task.get('paper_overview'))
        self.assertIn('reference-only', task['rules'])
        self.assertEqual(task['context_sentences'], [])  # both Introduction sentences fit in one chunk
        with patch.object(structure, 'CHUNK_SENTENCES', 1):
            split = structure.prepare_structured(self.pid, 'wh')
            self.assertEqual(split['sentences'][0]['sentence_id'], self.rows[0]['sentence_id'])
            self.assertEqual(split['context_sentences'][0]['sentence_id'], self.rows[1]['sentence_id'])
            second = structure.prepare_structured(self.pid, 'wh', 'wh-0002')
            self.assertEqual({row['sentence_id'] for row in second['context_sentences']},
                             {self.rows[0]['sentence_id']})  # WH task holds Introduction sentences only
        with patch.object(structure, 'CHUNK_SENTENCES', 1):
            staged = structure.prepare_structured(self.pid, 'protocol')
            reply = {k: staged[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
            reply.update({'protocols': [{'name': 'example protocol',
                         'sentence_ids': [self.rows[2]['sentence_id']], 'details': [],
                         'steps': [{'sentence_ids': [self.rows[2]['sentence_id']]}]}], 'skipped': []})
            structure.submit_structured(self.con, self.pid, staged['task_id'], staged['chunk']['chunk_id'], reply)
            before = structure.prepare_structured(self.pid, 'protocol')
            self.assertEqual(before['accepted_chunks'], 1)
            overview = structure.prepare_overview(self.pid)
            structure.submit_overview(self.con, self.pid, self.reply(overview), 'test')
            after = structure.prepare_structured(self.pid, 'protocol')
            self.assertEqual(after['accepted_chunks'], 1)
            self.assertNotEqual(after['chunk']['chunk_id'], staged['chunk']['chunk_id'])  # next pending chunk; accepted one kept

    def test_overview_rejects_upgraded_hedge(self):
        task = structure.prepare_overview(self.pid)
        bad = self.reply(task)
        bad['main_findings'][0]['certainty'] = 'demonstrated'
        with self.assertRaisesRegex(ValueError, 'never upgrade'):
            structure.submit_overview(self.con, self.pid, bad, 'test')

    def test_overview_accepts_equal_payload_echoes_and_rejects_mismatches(self):
        task = structure.prepare_structured(self.pid, 'overview')
        bad = self.reply(task)
        bad['paper_id'] = 'different_paper'
        with self.assertRaisesRegex(ValueError, 'echo paper_id'):
            structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], bad, 'test')

        reply = self.reply(task)
        reply.update({key: task[key] for key in ('paper_id', 'kind', 'task_id', 'source_evidence_type')})
        reply['chunk_id'] = task['chunk']['chunk_id']
        reply['reference_only'] = True
        result = structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], reply, 'test')
        self.assertTrue(result['ok'])

    def test_overview_grounding_accepts_defined_acronym_in_both_directions(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['text'] = 'Mesenchymal stem cells (MSC) may affect the test model.'
        rows[1]['text'] = 'MSC seems to increase survival in the example model.'
        rows[2]['text'] = 'Mesenchymal stem cells may affect the test model.'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)

        task = structure.prepare_overview(self.pid)
        reply = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        reply['research_question'] = [{'text': 'MSC may affect the test model.',
                                       'sentence_ids': [rows[2]['sentence_id']], 'certainty': 'may'}]
        self.assertTrue(structure.submit_overview(self.con, self.pid, reply, 'test')['ok'])
        self.assertNotIn('warnings', db.read_json(self.folder / 'paper_overview.json')['research_question'][0])
        self.assertTrue(structure.overview_status(self.pid)['ready'])

        task = structure.prepare_overview(self.pid)
        reply = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        reply['research_question'] = [{'text': 'Mesenchymal stem cells seems to increase survival in the example model.',
                                       'sentence_ids': [rows[1]['sentence_id']], 'certainty': 'seems to'}]
        self.assertTrue(structure.submit_overview(self.con, self.pid, reply, 'test')['ok'])
        self.assertNotIn('warnings', db.read_json(self.folder / 'paper_overview.json')['research_question'][0])
        self.assertTrue(structure.overview_status(self.pid)['ready'])

    def test_overview_grounding_rejects_undefined_acronym_and_accepts_plural(self):
        rows = copy.deepcopy(self.rows)
        rows[0]['text'] = 'Mesenchymal stem cells may affect the test model.'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        task = structure.prepare_overview(self.pid)
        bad = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        bad['research_question'] = [{'text': 'MSC may affect the test model.',
                                     'sentence_ids': [rows[0]['sentence_id']], 'certainty': 'may'}]
        with self.assertRaisesRegex(ValueError, 'MSC'):
            structure.submit_overview(self.con, self.pid, bad, 'test')

        rows[0]['text'] = 'The study included rat.'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        task = structure.prepare_overview(self.pid)
        good = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        good['groups_models'] = [{'text': 'The study included rats.',
                                  'sentence_ids': [rows[0]['sentence_id']], 'certainty': 'reported'}]
        self.assertTrue(structure.submit_overview(self.con, self.pid, good, 'test')['ok'])

    def test_long_overview_splits_at_section_and_restricts_ids_per_chunk(self):
        with patch.object(structure, 'OVERVIEW_CHARS', 130):
            task = structure.prepare_structured(self.pid, 'overview')
            self.assertGreater(task['total_chunks'], 1)
            state = structure._read_profile(structure._task_path(self.pid, task['task_id']))
            flat = [sid for c in state['chunks'] for sid in c['sentence_ids']]
            self.assertEqual(flat, [r['sentence_id'] for r in self.rows])  # nothing lost, nothing split
            first = state['chunks'][0]
            outside = next(r['sentence_id'] for r in self.rows if r['sentence_id'] not in first['sentence_ids'])
            echo = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
            bad = echo | {f: [{'text': 'x', 'sentence_ids': [outside], 'certainty': 'may'}] for f in structure.OVERVIEW_FIELDS}
            with self.assertRaisesRegex(ValueError, 'existing sentence IDs'):
                structure.submit_structured(self.con, self.pid, task['task_id'], first['chunk_id'], bad)
            empty = echo | {f: [] for f in structure.OVERVIEW_FIELDS}
            for i, chunk in enumerate(state['chunks']):
                # chunks may be empty on their own, but the merged overview may not be (D3.28)
                reply = empty if i < len(state['chunks']) - 1 else echo | {
                    'main_findings': [{'text': 'x', 'sentence_ids': chunk['sentence_ids'][:1], 'certainty': 'may, slightly'}]}
                result = structure.submit_structured(self.con, self.pid, task['task_id'], chunk['chunk_id'], reply)
            self.assertEqual(result['status'], 'submitted')
            self.assertTrue(structure.overview_status(self.pid)['ready'])

    def test_reference_context_gains_overview_only_when_fresh(self):
        before = structure.reference_context(self.pid)
        structure.submit_overview(self.con, self.pid, self.reply(structure.prepare_overview(self.pid)), 'test')
        self.assertIsNone(before['paper_overview'])
        self.assertIsNotNone(structure.reference_context(self.pid)['paper_overview'])

    def test_cli_and_mcp_accept_overview_kind(self):
        tool = next(t for t in mcp_server.TOOLS if t['name'] == 'structured_prepare')
        self.assertIn('overview', tool['inputSchema']['properties']['kind']['enum'])
        run = next(t for t in mcp_server.TOOLS if t['name'] == 'structured_run')
        self.assertIn('overview', run['inputSchema']['properties']['kind']['enum'])
        self.assertIn('all', run['inputSchema']['properties']['kind']['enum'])


if __name__ == '__main__':
    unittest.main()
