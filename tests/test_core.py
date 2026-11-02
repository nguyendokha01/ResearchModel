"""Behavioral tests in temporary stores, never in the researcher's library."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from kbsys import answer, config, db, evidence, neural, notes, pipeline, structure, workflow, vectors

class Demo3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='demo3-test-')
        self.root = Path(self.temp.name)
        self.saved = {k: getattr(config, k) for k in ('ROOT', 'CONFIG_DIR', 'STORE', 'DB_PATH', 'PAPERS_DIR', 'LOGS_DIR', 'CACHE_DIR', 'VAULTS_DIR', 'REVIEW_PENDING')}
        mapping = {'ROOT': self.root, 'CONFIG_DIR': self.root / 'config', 'STORE': self.root / 'knowledge_store',
                   'DB_PATH': self.root / 'knowledge_store/library.db', 'PAPERS_DIR': self.root / 'knowledge_store/papers',
                   'LOGS_DIR': self.root / 'knowledge_store/logs', 'CACHE_DIR': self.root / 'knowledge_store/cache',
                   'VAULTS_DIR': self.root / 'vaults', 'REVIEW_PENDING': self.root / 'review/pending'}
        for k, v in mapping.items():
            setattr(config, k, v)
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / 'ai.yaml').write_text('ai:\n  provider: mcp_agent\n', encoding='utf-8')
        for v in ('one', 'two'):
            config.vault_dir(v).mkdir(parents=True)
            (config.vault_dir(v) / 'Home.md').write_text(f'# {v}\n', encoding='utf-8')  # what create_vault writes
        self.con = db.connect()
        self.pid = 'test2026_example_abcd'
        self.folder = db.paper_dir(self.pid)
        self.rows = []
        samples = [('introduction', 'Reactive oxygen species (ROS) may affect the test model.'),
                   ('introduction', 'ROS seems to increase survival in the example model.'),
                   ('methods', 'The example protocol used 5 mg for 2 hours.'),
                   ('methods', 'The cells were then washed and incubated for 3 hours.'),
                   ('discussion', 'Compared with earlier observations, the effect was slightly stronger.'),
                   ('conclusion', 'This finding might support further experiments.')]
        for i, (section, text) in enumerate(samples, 1):
            self.rows.append({'sentence_id': f'{self.pid}:S{i:04d}', 'seq': i, 'passage_id': f'{self.pid}:{i:04d}',
                              'page': 1, 'section': section, 'subsection': 'Example protocol' if section == 'methods' else None,
                              'kind': 'text', 'source': 'main', 'text': text, 'start': 0, 'end': len(text), 'citations': [], 'mentions': []})
        meta = {'paper_id': self.pid, 'title': 'Artificial test example', 'year': 2026, 'vaults': ['one'],
                'authors': [], 'source_files': [], 'evidence_type': 'primary_research', 'extraction_version': 2}
        db.write_json(self.folder / 'metadata.json', meta)
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        db.write_jsonl(self.folder / 'passages.jsonl', [{'passage_id': s['passage_id'], 'seq': s['seq'], 'page': 1,
                       'section': s['section'], 'kind': 'text', 'text': s['text']} for s in self.rows])
        db.load_paper(self.con, self.pid)
        self.con.commit()
        self.semantic = patch('kbsys.vectors.index_ready', return_value=False)
        self.semantic.start()
        self.html_dir = patch.object(answer, 'OUTPUT_DIR', self.root / 'output' / 'answers')   # U10b: never the real output/
        self.html_dir.start()

    def tearDown(self):
        self.html_dir.stop()
        self.semantic.stop()
        self.con.close()
        for k, v in self.saved.items():
            setattr(config, k, v)
        self.temp.cleanup()

    def test_full_inventory_keeps_methods_order_qualifiers_and_missing_wh(self):
        r = structure.build(self.pid)
        ros = next(a for a in r['abbreviations'] if a['short'] == 'ROS')
        self.assertEqual(ros['definitions'][0]['text'], 'Reactive oxygen species')
        self.assertEqual(len(ros['sentence_ids']), 2)
        self.assertEqual(r['methods'][0]['ordered_sentence_ids'], [s['sentence_id'] for s in self.rows[2:4]])
        self.assertEqual(r['coverage']['required'], 6)
        self.assertEqual(len(r['coverage']['uncovered']), 6)
        self.assertTrue(all(v is None for v in r['introduction_wh'].values()))
        self.assertEqual(len(r['discussion_comparisons']), 1)
        self.assertEqual(len(r['uncertainty']), 4)

    def test_validator_accepts_newer_extraction_version_rejects_older(self):
        meta = db.read_json(self.folder / 'metadata.json')
        for version, ok in ((3, True), (1, False)):
            db.write_json(self.folder / 'metadata.json', {**meta, 'extraction_version': version})
            if ok:
                self.assertIn(f'{self.pid}:S0001', evidence.PaperContext(self.con, self.pid).sentences)
            else:
                with self.assertRaises(ValueError):
                    evidence.PaperContext(self.con, self.pid)

    def test_wh_profile_rejects_forged_id_and_number(self):
        def payload(text, ids):
            return {'answers': {k: {'text': text, 'sentence_ids': ids} for k in structure.WH}}
        r = structure.submit_profile(self.con, self.pid, payload('There were 900 subjects.', [self.rows[0]['sentence_id']]), 'test')
        self.assertFalse(r['ok'])
        self.assertFalse((self.folder / 'section_profile.json').exists())
        r = structure.submit_profile(self.con, self.pid, payload('Example answer', ['foreign:S0001']), 'test')
        self.assertFalse(r['ok'])
        good = {'answers': {k: {'text': None, 'reason': 'Not supplied by this Introduction.'} for k in structure.WH}}
        good.update({k: structure.prepare(self.pid)[k] for k in ('source_sha256', 'revision_of')})
        good['answers']['what'] = {'text': self.rows[0]['text'], 'sentence_ids': [self.rows[0]['sentence_id']]}
        r = structure.submit_profile(self.con, self.pid, good, 'test')
        self.assertTrue(r['ok'])
        self.assertEqual(r['answers']['what']['quotes'][0]['text'], self.rows[0]['text'])
        self.rows[0]['text'] += ' Updated.'
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        self.assertEqual(structure.build(self.pid)['profile_status'], 'missing_or_stale')

    def test_cache_restarts_detects_output_tampering_and_input_change(self):
        source, dest = self.root / 'in.txt', self.root / 'out.txt'
        source.write_text('one')
        calls = []
        def op():
            calls.append(True)
            dest.write_text(source.read_text())
            return {'ok': True}
        a = workflow.checkpoint('test', 'x', [source], [dest], op)
        b = workflow.checkpoint('test', 'x', [source], [dest], op)
        self.assertEqual(a['status'], 'done')
        self.assertTrue(b['cache_hit'])
        dest.write_text('tampered')
        self.assertFalse(workflow.checkpoint('test', 'x', [source], [dest], op)['cache_hit'])
        source.write_text('two')
        workflow.checkpoint('test', 'x', [source], [dest], op)
        self.assertEqual(len(calls), 3)

    def test_waiting_error_and_interrupted_steps_are_not_cached(self):
        p = self.root / 'out'
        p.write_text('placeholder')
        for _ in range(2):
            r = workflow.checkpoint('wait', 'x', [], [p], lambda: {'waiting': True})
            self.assertFalse(r['cache_hit'])
            self.assertEqual(r['status'], 'waiting')
        with self.assertRaisesRegex(ValueError, 'broken'):
            workflow.checkpoint('fail', 'x', [], [p], lambda: {'error': 'broken'})
        self.assertTrue(any(r['status'] == 'failed' for r in workflow.status()))
        r = workflow.checkpoint('empty', 'x', [], [self.root / 'missing'], lambda: {})
        self.assertEqual(r['status'], 'waiting')

    def test_writer_lock_excludes_other_process_identity(self):
        with workflow.writer_lock('test'):
            with workflow.writer_lock('test'):
                self.assertTrue(True)  # nested same thread is safe
            # Simulate a different execution identity; an existing O_EXCL lock blocks it.
            with patch.object(workflow._LOCAL, 'held', set()):
                with self.assertRaisesRegex(ValueError, 'Another writer'):
                    with workflow.writer_lock('test'):
                        self.fail('must not enter')
        with workflow.writer_lock('test'):
            pass

    def test_markdown_search_real_links_backlinks_isolation_and_update(self):
        v = config.vault_dir('one')
        # hops follow knowledge edges only (kb:auto block of a result / claim note -> node note): A -> B <- C
        auto = notes.AUTO_START + '\n[[B]]\n' + notes.AUTO_END + '\n'
        (v / 'Concepts').mkdir()
        (v / 'A.md').write_text('---\ntype: result\nid: p1:E0001\n---\nROS context\n' + auto, encoding='utf-8')
        (v / 'Concepts' / 'B.md').write_text('Protocol [[C]]', encoding='utf-8')
        (v / 'C.md').write_text('---\ntype: claim\nid: p1:E0002\n---\nLast note\n' + auto, encoding='utf-8')
        (config.vault_dir('two') / 'Private.md').write_text('ROS secret', encoding='utf-8')
        result = neural.search('one', 'ROS', hops=2)
        self.assertEqual({n['path'] for n in result['nodes']}, {'A.md', 'Concepts/B.md', 'C.md'})
        self.assertEqual(neural.refresh('one')['updated'], 0)
        (v / 'A.md').write_text('New topic [[B]]', encoding='utf-8')
        self.assertEqual(neural.search('one', 'ROS')['nodes'], [])
        (v / 'C.md').unlink()
        result = neural.search('one', 'Protocol', hops=1)
        self.assertEqual(result['edges'], [])  # the missing link is a navigation edge: not expanded, not listed
        con = neural._cache('one')  # the removal still re-resolves B's link to missing and drops C's own edges
        try:
            self.assertEqual([r[0] for r in con.execute("SELECT status FROM edges WHERE source='Concepts/B.md'")], ['missing'])
            self.assertEqual(con.execute("SELECT COUNT(*) FROM edges WHERE source='C.md'").fetchone()[0], 0)
        finally:
            con.close()
        with self.assertRaises(ValueError):
            neural.read_note('one', '../two/Private.md')
        with self.assertRaises(ValueError):
            neural.search('../two', 'ROS')

    def test_ambiguous_wikilinks_do_not_guess(self):
        v = config.vault_dir('one')
        for folder in ('X', 'Y'):
            (v / folder).mkdir()
            (v / folder / 'Same.md').write_text('A concept')
        (v / 'Seed.md').write_text('Unique [[Same]]')
        r = neural.search('one', 'Unique',hops=1)
        self.assertEqual(len(r['nodes']), 1)
        self.assertEqual(r['edges'], [])  # an ambiguous link is a navigation edge: never followed, never listed
        con = neural._cache('one')
        try:
            self.assertEqual([x[0] for x in con.execute("SELECT status FROM edges WHERE source='Seed.md'")], ['ambiguous'])
        finally:
            con.close()

    def test_saved_snapshot_rejects_stale_note_and_fake_evidence(self):
        p = config.vault_dir('one') / 'Topic.md'
        p.write_text('ROS example')
        prepared = neural.prepare(self.con, config.load(), 'one', 'ROS')
        r = neural.check(self.con, prepared['ticket'], {'answer': [{'text': 'The source may affect the test model.', 'evidence': [1]}]})
        self.assertEqual(r['answer'][0]['check'], 'ok')
        prepared['items'][0]['text'] = '900 subjects'
        r = neural.check(self.con, prepared['ticket'], {'answer': [{'text': '900 subjects.', 'evidence': [1]}]})
        self.assertNotEqual(r['answer'][0]['check'], 'ok')
        p.write_text('Changed source')
        with self.assertRaisesRegex(ValueError, 'Note changed'):
            neural.check(self.con, prepared['ticket'], {'answer': []})

    def test_protocol_comparison_keeps_exact_source_and_sequence(self):
        r = structure.compare_methods(self.con, 'one', 'protocol')
        self.assertEqual(len(r['records']), 1)
        self.assertEqual(r['records'][0]['source'][0]['text'], self.rows[2]['text'])
        self.assertEqual(structure.compare_methods(self.con, 'two')['records'], [])

    def test_saved_snapshot_rejects_changed_provenance_without_text_change(self):
        prepared = neural.prepare(self.con, config.load(), 'one', 'ROS')
        self.con.execute('UPDATE sentences SET section=? WHERE sentence_id=?', ('discussion', self.rows[0]['sentence_id']))
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            neural.check(self.con, prepared['ticket'], {'answer': []})

    def test_done_evidence_still_waits_for_structured_profiles(self):
        db.write_json(self.folder / 'analysis_state.json', {'status': 'done'})
        db.write_jsonl(self.folder / 'evidence.jsonl', [])
        cfg = config.load()
        cfg['analysis'] = {'require_structured_profiles': True}
        with patch('kbsys.analysis_runner.analyze_paper') as runner:
            result = pipeline.run(self.con, cfg, 'one', 'analyze', say=lambda *a: None)
            runner.assert_not_called()
        self.assertEqual(result[0]['status'], 'waiting')
        task = structure.prepare(self.pid)
        structure.submit_profile(self.con, self.pid, {'source_sha256': task['source_sha256'], 'revision_of': task['revision_of'],
            'answers': {k: {'text': None, 'reason': 'Not specified in this artificial example.'} for k in structure.WH}}, 'test')
        task = structure.prepare_protocol(self.pid)
        ids = [s['sentence_id'] for s in self.rows[2:4]]
        structure.submit_protocol(self.con, self.pid, {'source_sha256': task['source_sha256'], 'revision_of': task['revision_of'],
            'protocols': [{'name': 'example protocol', 'sentence_ids': ids, 'details': [], 'steps': [{'sentence_ids': [sid]} for sid in ids]}]}, 'test')
        self.assertTrue(structure.completion(self.pid)['complete'])
        result = pipeline.run(self.con, cfg, 'one', 'analyze', say=lambda *a: None)
        self.assertEqual(result[0]['status'], 'done')

    def test_vector_resume_repairs_missing_and_tampered_arrays_and_model_change(self):
        import numpy as np
        from contextlib import ExitStack
        from unittest.mock import Mock
        folder = config.STORE / 'embeddings'
        model = folder / 'model'
        model.mkdir(parents=True)
        (model / vectors.MODEL_FILE).write_bytes(b'isolated model fixture')
        (model / 'tokenizer.json').write_text('{}')
        fake = Mock()
        fake.encode.side_effect = lambda texts: np.ones((len(texts), vectors.DIM), dtype=np.float32) * .1
        with ExitStack() as stack:
            for key, value in {'EMB_DIR': folder, 'MODEL_DIR': model, 'PAPER_VECS': folder / 'papers', 'INDEX_JSON': folder / 'index.json'}.items():
                stack.enter_context(patch.object(vectors, key, value))
            stack.enter_context(patch.object(vectors, 'encoder', return_value=fake))
            run = lambda: pipeline.run(self.con, config.load(), 'one', 'vectors', say=lambda *a: None)[0]
            first = run()
            self.assertEqual(first['status'], 'done')
            self.assertTrue(run()['cache_hit'])
            matrix = folder / db.read_json(vectors.INDEX_JSON)['file']
            matrix.unlink()
            self.assertFalse(run()['cache_hit'])
            matrix = folder / db.read_json(vectors.INDEX_JSON)['file']
            np.save(matrix, np.zeros((6, vectors.DIM), dtype=np.int8))
            self.assertFalse(run()['cache_hit'])
            matrix = folder / db.read_json(vectors.INDEX_JSON)['file']
            self.assertTrue(np.load(matrix).any())
            cache = vectors._paper_file(self.pid)
            with np.load(cache, allow_pickle=False) as data:
                altered = {k: data[k] for k in data.files}
            altered['vecs'] = np.zeros_like(altered['vecs'])
            np.savez(cache, **altered)
            calls = fake.encode.call_count
            self.assertFalse(run()['cache_hit'])
            self.assertGreater(fake.encode.call_count, calls)
            calls = fake.encode.call_count
            (model / vectors.MODEL_FILE).write_bytes(b'a changed isolated model fixture')
            self.assertFalse(run()['cache_hit'])
            self.assertGreater(fake.encode.call_count, calls)
            self.assertTrue(run()['cache_hit'])

    def test_protocol_profile_checks_values_order_and_coverage(self):
        a, b = self.rows[2:4]
        good = {'protocols': [{'name': 'example protocol', 'sentence_ids': [a['sentence_id']],
                'details': [{'field': 'dose', 'value': '5 mg', 'sentence_ids': [a['sentence_id']]}],
                'steps': [{'sentence_ids': [a['sentence_id']]}, {'sentence_ids': [b['sentence_id']]}]}]}
        good.update({k: structure.prepare_protocol(self.pid)[k] for k in ('source_sha256', 'revision_of')})
        r = structure.submit_protocol(self.con, self.pid, good, 'test')
        self.assertTrue(r['ok'])
        self.assertEqual(r['protocols'][0]['steps'][1]['quotes'][0]['text'], b['text'])
        partial = json.loads(json.dumps(good))
        partial['protocols'][0]['details'][0]['value'] = '5 m'
        with self.assertRaisesRegex(ValueError, 'not present'):
            structure.submit_protocol(self.con, self.pid, partial, 'test')
        bad = json.loads(json.dumps(good))
        bad['protocols'][0]['details'][0]['value'] = '900 mg'
        with self.assertRaisesRegex(ValueError, 'not present'):
            structure.submit_protocol(self.con, self.pid, bad, 'test')
        bad = json.loads(json.dumps(good))
        bad['protocols'][0]['steps'].reverse()
        with self.assertRaisesRegex(ValueError, 'preserve'):
            structure.submit_protocol(self.con, self.pid, bad, 'test')
        good['protocols'][0]['steps'].pop()
        good.update({k: structure.prepare_protocol(self.pid)[k] for k in ('source_sha256', 'revision_of')})
        r = structure.submit_protocol(self.con, self.pid, good, 'test')
        self.assertFalse(r['ok'])
        self.assertEqual(r['coverage_missing'], [b['sentence_id']])

    def test_full_section_policy_requires_plain_intro_and_methods(self):
        (config.CONFIG_DIR / 'ingest.yaml').write_text('analysis:\n  coverage_policy: full_sections\n')
        ctx = evidence.PaperContext(self.con, self.pid)
        required = evidence.required_sentences(ctx)
        self.assertEqual(required, [s['sentence_id'] for s in self.rows])

    def test_profiles_reject_stale_revision_and_source(self):
        task = structure.prepare(self.pid)
        reply = {'source_sha256': task['source_sha256'], 'revision_of': task['revision_of'],
                 'answers': {k: {'text': None, 'reason': 'Not supplied.'} for k in structure.WH}}
        self.assertTrue(structure.submit_profile(self.con, self.pid, reply, 'test')['ok'])
        with self.assertRaisesRegex(ValueError, 'Profile changed'):
            structure.submit_profile(self.con, self.pid, reply, 'stale-agent')
        reply['source_sha256'] = 'stale'
        with self.assertRaisesRegex(ValueError, 'Source changed'):
            structure.submit_profile(self.con, self.pid, reply, 'stale-agent')

    def test_dynamic_outputs_detect_deleted_artifacts(self):
        source = self.root / 'dynamic-input'
        output_dir = self.root / 'artifacts'
        output_dir.mkdir()
        source.write_text('content')
        calls = []
        def op():
            calls.append(True)
            (output_dir / 'a.txt').write_text(source.read_text())
            return {'ok': True}
        outputs = lambda: list(output_dir.glob('*.txt'))
        workflow.checkpoint('dynamic', 'x', [source], outputs, op)
        (output_dir / 'a.txt').unlink()
        workflow.checkpoint('dynamic', 'x', [source], outputs, op)
        self.assertEqual(len(calls), 2)

    def test_quoted_markdown_search_is_exact_phrase(self):
        v = config.vault_dir('one')
        (v / 'Exact.md').write_text('unique_acceptance_marker')
        (v / 'Other.md').write_text('The marker is unique but different.')
        r = neural.search('one', '"unique_acceptance_marker"', hops=0)
        self.assertEqual([n['path'] for n in r['nodes']], ['Exact.md'])

    def test_weak_qualifiers_cannot_become_declarations(self):
        for word in ('seems to', 'looks like it might', 'may', 'slightly', 'not really', 'we thought it could'):
            text = f'The treatment {word} improves survival.'
            h = {'kind': 'sentence', 'paper_id': self.pid, 'unit_type': None, 'text': text, 'review_paper': False}
            r = answer.check_sentence({'text': 'The treatment improves survival.', 'evidence': [1]}, [h], {})
            self.assertIn('A7', r['problems'], word)
            self.assertTrue(evidence.HEDGE.search(text), word)
        h = {'kind': 'sentence', 'paper_id': self.pid, 'unit_type': None, 'text': 'The treatment may improve survival.', 'review_paper': False}
        self.assertEqual(answer.check_sentence({'text': 'The treatment may improve survival.', 'evidence': [1]}, [h], {})['check'], 'ok')

    def test_pipeline_mcp_waits_instead_of_false_completion(self):
        r = pipeline.run(self.con, config.load(), 'one', 'analyze', say=lambda *x: None)
        self.assertEqual(r[0]['status'], 'waiting')
        self.assertFalse((self.folder / 'evidence.jsonl').exists())

if __name__ == '__main__':
    unittest.main(verbosity=2)
