"""D3.15 isolated source, checkpoint, provider and media integration regressions."""
import copy
import json
import unittest
from unittest.mock import patch

from kbsys import analysis_runner, config, db, mcp_server, pipeline, structure, workflow
from . import test_core


class StructuredRevisionTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def wh_reply(self, task):
        reply = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        reply['answers'] = {k: {'text': None, 'reason': 'Not annotated in the supplied Introduction.'} for k in structure.WH}
        if task['sentences']:
            source = task['sentences'][0]
            reply['answers']['what'] = {'text': source['text'], 'sentence_ids': [source['sentence_id']]}
        else:
            reply['source_absence_reason'] = 'Current extracted source has no Introduction section; not a literature assertion.'
        return reply

    def protocol_reply(self, task, skips=False):
        reply = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        rows = task['sentences']
        reply.update(protocols=[], skipped=[])
        if skips:
            reply['skipped'] = [{'sentence_id': s['sentence_id'], 'reason': 'Needs manual protocol assignment.'} for s in rows]
        elif rows:
            name = 'example protocol' if 'example protocol' in rows[0]['text'] else rows[0]['text'].split()[0]
            reply['protocols'] = [{'name': name, 'sentence_ids': [s['sentence_id'] for s in rows],
                                   'steps': [{'sentence_ids': [s['sentence_id']]} for s in rows], 'details': []}]
        else:
            reply['source_absence_reason'] = 'No Methods section in current extracted source; not a primary protocol certification.'
        return reply

    def submit_profiles(self):
        task = structure.prepare(self.pid)
        self.assertTrue(structure.submit_profile(self.con, self.pid, self.wh_reply(task), 'test')['ok'])
        task = structure.prepare_protocol(self.pid)
        self.assertTrue(structure.submit_protocol(self.con, self.pid, self.protocol_reply(task), 'test')['ok'])

    def test_malformed_current_fake_done_and_missing_sources_never_complete(self):
        self.submit_profiles()
        self.assertTrue(structure.completion(self.pid)['complete'])
        path = self.folder / 'section_profile.json'
        original = path.read_bytes()
        for value in ([1], {'paper_id': self.pid, 'source_sha256': structure.prepare(self.pid)['source_sha256'],
                            'answers': dict.fromkeys(structure.WH, {})}):
            db.write_json(path, value)
            self.assertFalse(structure.completion(self.pid)['complete'])
        path.write_text('{corrupt')
        self.assertFalse(structure.completion(self.pid)['complete'])
        path.write_bytes(original)
        protocol = db.read_json(self.folder / 'protocol_profile.json')
        protocol['protocols'][0]['steps'][0]['quotes'][0]['text'] = 'forged source'
        db.write_json(self.folder / 'protocol_profile.json', protocol)
        self.assertFalse(structure.completion(self.pid)['complete'])
        self.folder.joinpath('sentences.jsonl').unlink()
        self.assertFalse(structure.completion(self.pid)['complete'])

    def test_source_and_registration_revision_invalidate_profiles(self):
        self.submit_profiles()
        meta = db.read_json(self.folder / 'metadata.json')
        meta['publication_types'] = ['Review']
        db.write_json(self.folder / 'metadata.json', meta)
        self.assertFalse(structure.completion(self.pid)['complete'])
        task = structure.prepare_structured(self.pid)
        self.rows[0]['text'] += ' Changed source.'
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        with self.assertRaisesRegex(ValueError, 'revision stale'):
            structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], self.wh_reply(task))

    def test_ai_metadata_writes_do_not_change_source_revision(self):
        self.submit_profiles()
        before = structure.source_revision(self.pid)
        meta = db.read_json(self.folder / 'metadata.json')
        meta.update({
            'analysis_status': 'done',
            'classification': 'CORE',
            'evidence_type': 'primary_research',
            'analysis_counters': {'accepted': 6, 'rejected': 1},
            'updated_at': '2099-01-01T00:00:00+00:00',
        })
        db.write_json(self.folder / 'metadata.json', meta)
        self.assertEqual(before, structure.source_revision(self.pid))
        self.assertTrue(structure.completion(self.pid)['complete'])

        meta['title'] = 'Changed source title'
        db.write_json(self.folder / 'metadata.json', meta)
        self.assertNotEqual(before, structure.source_revision(self.pid))
        self.assertFalse(structure.completion(self.pid)['complete'])

    def test_legacy_revision_stays_current_for_unchanged_source(self):
        self.submit_profiles()
        overview_task = structure.prepare_overview(self.pid)
        overview_reply = {k: overview_task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        overview_reply['research_question'] = [{'text': self.rows[0]['text'],
                                                'sentence_ids': [self.rows[0]['sentence_id']], 'certainty': 'may'}]
        self.assertTrue(structure.submit_overview(self.con, self.pid, overview_reply, 'test')['ok'])
        legacy = structure._legacy_source_revision(self.pid)
        for name in ('section_profile.json', 'protocol_profile.json', 'paper_overview.json'):
            path = self.folder / name
            profile = db.read_json(path)
            profile['source_revision'] = legacy
            db.write_json(path, profile)
        status = structure.completion(self.pid)
        self.assertTrue(status['complete'])
        self.assertEqual(status['wh']['status'], 'current')
        self.assertEqual(status['protocol']['status'], 'current')
        self.assertTrue(structure.overview_status(self.pid)['ready'])

    def test_all_null_and_all_skips_are_honest_distinct_coverage(self):
        task = structure.prepare(self.pid)
        reply = self.wh_reply(task)
        reply['answers']['what'] = {'text': None, 'reason': 'Unresolved source question; needs review.'}
        structure.submit_profile(self.con, self.pid, reply, 'test')
        report = structure.completion(self.pid)['wh']
        self.assertTrue(report['technical_coverage'])
        self.assertEqual(report['status'], 'missing_context')
        task = structure.prepare_protocol(self.pid)
        structure.submit_protocol(self.con, self.pid, self.protocol_reply(task, skips=True), 'test')
        report = structure.completion(self.pid)['protocol']
        self.assertTrue(report['technical_coverage'])
        self.assertFalse(report['ready'])
        self.assertEqual(report['status'], 'pending_review')
        self.assertEqual(report['scientific_acceptance'], 'not_assessed')

    def test_hedge_check_a7_rejects_claims_but_only_warns_for_setting_fields(self):
        from unittest import mock
        task = structure.prepare(self.pid)
        source = task['sentences'][0]
        reply = self.wh_reply(task)
        for key in ('what', 'where'):
            reply['answers'][key] = {'text': source['text'], 'sentence_ids': [source['sentence_id']]}
        struck = {'check': 'struck:A7', 'problems': ['A7'], 'warnings': []}
        with mock.patch('kbsys.answer.check_sentence', return_value=struck):
            result = structure.submit_profile(self.con, self.pid, reply, 'test')
        self.assertFalse(result['ok'])
        self.assertEqual({e['field'] for e in result['errors']}, {'what'})

    def test_null_answer_keeps_searched_ids_as_context_only(self):
        # Real pilot: AI said "who" is not stated and listed the sentence it read.
        task = structure.prepare(self.pid)
        sid = task['sentences'][0]['sentence_id']
        reply = self.wh_reply(task)
        reply['answers']['who'] = {'text': None, 'reason': 'Organism not stated in the Introduction.', 'sentence_ids': [sid]}
        result = structure.submit_profile(self.con, self.pid, reply, 'test')
        self.assertTrue(result['ok'], result)
        self.assertEqual(result['answers']['who']['sentence_ids'], [])
        self.assertEqual(result['answers']['who']['searched_sentence_ids'], [sid])
        reply['answers']['who']['sentence_ids'] = ['P:S9999']
        self.assertFalse(structure.submit_profile(self.con, self.pid, reply, 'test')['ok'])

    def test_absent_sections_require_explicit_source_absence_reason(self):
        self.rows = [s for s in self.rows if s['section'] not in ('introduction', 'methods')]
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        task = structure.prepare(self.pid)
        reply = self.wh_reply(task)
        del reply['source_absence_reason']
        with self.assertRaisesRegex(ValueError, 'source_absence_reason'):
            structure.submit_profile(self.con, self.pid, reply, 'test')
        structure.submit_profile(self.con, self.pid, self.wh_reply(task), 'test')
        task = structure.prepare_protocol(self.pid)
        structure.submit_protocol(self.con, self.pid, self.protocol_reply(task), 'test')
        status = structure.completion(self.pid)
        self.assertEqual(status['protocol']['status'], 'source_absent')
        self.assertTrue(status['complete'])
        self.assertEqual(status['scientific_acceptance'], 'not_assessed')

    def test_protocol_order_values_copy_exact_casing_and_token_bounds(self):
        task = structure.prepare_protocol(self.pid)
        reply = self.protocol_reply(task)
        reply['protocols'][0]['name'] = 'EXAMPLE PROTOCOL'
        reply['protocols'][0]['details'] = [{'field': 'dose', 'value': '5 MG', 'sentence_ids': [task['sentences'][0]['sentence_id']]}]
        result = structure.submit_protocol(self.con, self.pid, reply, 'test')
        self.assertEqual(result['protocols'][0]['name'], 'example protocol')
        self.assertEqual(result['protocols'][0]['details'][0]['value'], '5 mg')
        task = structure.prepare_protocol(self.pid)
        other = self.protocol_reply(task)  # unlisted kind (pilot: supplier) is kept as 'other', value still sourced
        other['protocols'][0]['details'] = [{'field': 'supplier', 'value': '5 MG', 'sentence_ids': [task['sentences'][0]['sentence_id']]}]
        detail = structure.submit_protocol(self.con, self.pid, other, 'test')['protocols'][0]['details'][0]
        self.assertEqual((detail['field'], detail['label']), ('other', 'supplier'))
        other['protocols'][0]['details'][0]['value'] = 'not in source'
        with self.assertRaisesRegex(ValueError, 'not present'):
            structure.submit_protocol(self.con, self.pid, other, 'test')
        task = structure.prepare_protocol(self.pid)
        bad = self.protocol_reply(task)
        bad['protocols'][0]['sentence_ids'].reverse()
        with self.assertRaisesRegex(ValueError, 'order'):
            structure.submit_protocol(self.con, self.pid, bad, 'test')
        for text in ('15 mg', '-5 mg', '0.5 mg', '5 mg/kg'):
            self.assertFalse(structure._fragment('5 mg', text))

    def test_full_sections_include_main_text_results_and_supplementary_not_abstract_objects(self):
        for i, section in enumerate(('results', 'results_discussion', 'main_text', 'body', 'supplementary', 'abstract'), 7):
            self.rows.append({**self.rows[0], 'sentence_id': f'{self.pid}:S{i:04d}', 'seq': i, 'section': section})
        self.rows.append({**self.rows[0], 'sentence_id': f'{self.pid}:S0013', 'seq': 13, 'section': 'results', 'kind': 'caption'})
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        view = structure.build(self.pid, persist=False)
        self.assertEqual(view['coverage']['required'], 11)
        self.assertNotIn(f'{self.pid}:S0012', view['coverage']['uncovered'])
        self.assertEqual(view['object_sentence_ids'], [f'{self.pid}:S0013'])

    def test_source_review_publication_precedence_and_non_significance(self):
        meta = db.read_json(self.folder / 'metadata.json')
        meta.update(publication_types=['Review'], evidence_type='primary_research')
        db.write_json(self.folder / 'metadata.json', meta)
        task = structure.prepare(self.pid)
        result = structure.submit_profile(self.con, self.pid, self.wh_reply(task), 'test')
        self.assertTrue(result['ok'])
        self.assertEqual(result['answers']['what']['source_evidence_type'], 'review')
        self.assertIn('A5', result['answers']['what']['warnings'])
        for text in ('There was no significant difference.', 'It was not statistically significant.', 'It was not significantly different.'):
            self.assertTrue(any(m['category'] == 'non_significance' for m in structure.uncertainty(text)))
        self.assertFalse(structure.uncertainty('Values were similar to controls.'))

    def test_staged_subsection_resume_preserves_accepted_chunks_and_complete_methods(self):
        with patch.object(structure, 'CHUNK_SENTENCES', 1):
            task = structure.prepare_structured(self.pid, 'protocol')
            first = task['chunk']['chunk_id']
            result = structure.submit_structured(self.con, self.pid, task['task_id'], first, self.protocol_reply(task))
            self.assertEqual(result['accepted_chunks'], 1)
            resumed = structure.prepare_structured(self.pid, 'protocol')
            self.assertEqual(resumed['task_id'], task['task_id'])
            self.assertEqual(resumed['accepted_chunks'], 1)
            self.assertNotEqual(resumed['chunk']['chunk_id'], first)
            structure.submit_structured(self.con, self.pid, task['task_id'], resumed['chunk']['chunk_id'], self.protocol_reply(resumed))
            report = structure.completion(self.pid)['protocol']
            self.assertTrue(report['ready'])
            self.assertEqual(report['coverage_missing'], [])
            replay = structure.submit_structured(self.con, self.pid, task['task_id'], first, self.protocol_reply(task))
            self.assertTrue(replay['replay'])

    def test_accepted_chunk_tamper_and_final_publication_interruption(self):
        task = structure.prepare_structured(self.pid, 'wh')
        path = structure._task_path(self.pid, task['task_id'])
        with patch.object(structure, 'submit_profile', side_effect=RuntimeError('interrupted publication')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], self.wh_reply(task))
        resumed = structure.prepare_structured(self.pid, 'wh')
        self.assertEqual(resumed['accepted_chunks'], 1)
        self.assertEqual(resumed['chunk']['status'], 'accepted')
        structure.submit_structured(self.con, self.pid, task['task_id'], resumed['chunk']['chunk_id'], self.wh_reply(resumed))
        state = db.read_json(path)
        state['chunks'][0]['reply']['answers']['what']['text'] = 'tampered'
        db.write_json(path, state)
        # On a task with unchanged target revision, corruption must invalidate accepted reuse.
        (self.folder / 'section_profile.json').unlink()
        resumed = structure.prepare_structured(self.pid, 'wh')
        self.assertEqual(resumed['chunk']['status'], 'pending')

    def configured(self):
        (config.CONFIG_DIR / 'ai.yaml').write_text('ai:\n  provider: mcp_agent\n  tasks:\n    analysis:\n      provider: codex_cli\n      model: fixture-model\n')

    def test_provider_requires_opt_in_explicit_route_and_no_evidence_recall(self):
        with patch.object(analysis_runner, '_call') as call, patch.object(analysis_runner, 'analyze_paper') as evidence_run:
            self.assertEqual(analysis_runner.run_structured(self.con, self.pid)['calls'], 0)
            result = analysis_runner.run_structured(self.con, self.pid, allow_ai=True)
            self.assertIn('ai.tasks.analysis', result['message'])
            self.configured()
            with patch('kbsys.answer.provider_status', return_value={'ready': False, 'message': 'Provider unavailable'}):
                result = analysis_runner.run_structured(self.con, self.pid, allow_ai=True)
                self.assertEqual(result['status'], 'waiting')
                self.assertIn('structured-submit', result['next'])
            call.assert_not_called()
            evidence_run.assert_not_called()

    def test_explicit_provider_override_is_bounded_logged_and_keeps_default_config(self):
        before = (config.CONFIG_DIR / 'ai.yaml').read_bytes()
        def chat(settings, system, user, pid, stage):
            self.assertEqual(settings['provider'], 'codex_cli')
            self.assertEqual(settings['codex_cli']['model'], 'explicit-model')
            self.assertEqual(settings['codex_cli']['reasoning'], 'low')
            task = structure.prepare_structured(self.pid, 'wh')
            return json.dumps(self.wh_reply(task))
        with patch('kbsys.answer.provider_status', return_value={'ready': True}), patch.object(analysis_runner, '_call', side_effect=chat) as call:
            result = analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True, max_chunks=1,
                                                    provider='codex_cli', model='explicit-model', reasoning='low')
        self.assertEqual(result['calls'], 1)
        self.assertEqual(result['status'], 'complete')
        self.assertFalse(result['overall_complete'])
        self.assertEqual(call.call_count, 1)
        self.assertTrue(structure.completion(self.pid)['wh']['ready'])
        self.assertEqual((config.CONFIG_DIR / 'ai.yaml').read_bytes(), before)

    def test_runner_reuses_returned_reply_after_interruption_and_checks_signature_missing_output(self):
        self.configured()
        task = structure.prepare_structured(self.pid, 'wh')
        with patch('kbsys.answer.provider_status', return_value={'ready': True}), patch.object(analysis_runner, '_call', return_value=json.dumps(self.wh_reply(task))) as call:
            with patch.object(structure, 'submit_structured', side_effect=RuntimeError('crash after reply')):
                with self.assertRaises(RuntimeError):
                    analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True)
            result = analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True)
            self.assertEqual(result['calls'], 0)
            self.assertEqual(call.call_count, 1)
        self.assertTrue(structure.completion(self.pid)['wh']['ready'])
        # A corrupt/missing raw reply or changed provider cannot be used to avoid a new bounded call.
        (self.folder / 'section_profile.json').unlink()
        task = structure.prepare_structured(self.pid, 'wh')
        state = db.read_json(structure._task_path(self.pid, task['task_id']))
        state['chunks'][0].update(status='pending', attempts=1, runner={'provider_signature': 'old', 'reply_sha256': 'missing'})
        db.write_json(structure._task_path(self.pid, task['task_id']), state)
        with patch('kbsys.answer.provider_status', return_value={'ready': True}), patch.object(analysis_runner, '_call', return_value=json.dumps(self.wh_reply(task))) as call:
            analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True)
            self.assertEqual(call.call_count, 1)

    def test_runner_two_attempt_limit_and_accepted_evidence_never_recalled(self):
        self.configured()
        db.write_json(self.folder / 'analysis_state.json', {'status': 'done'})
        db.write_jsonl(self.folder / 'evidence.jsonl', [])
        with patch('kbsys.answer.provider_status', return_value={'ready': True}), patch.object(analysis_runner, '_call', return_value='broken JSON') as call, patch.object(analysis_runner, 'analyze_paper') as evidence_run:
            result = analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True)
            self.assertEqual(call.call_count, 2)
            self.assertEqual(result['status'], 'waiting')
            result = analysis_runner.run_structured(self.con, self.pid, kind='wh', allow_ai=True)
            self.assertEqual(result['calls'], 0)
            evidence_run.assert_not_called()
        self.assertEqual(db.read_json(self.folder / 'analysis_state.json'), {'status': 'done'})

    def test_structured_runner_honours_configured_attempts_and_repeats_validator_message(self):
        self.configured()
        (config.CONFIG_DIR / 'ingest.yaml').write_text('analysis:\n  max_attempts: 3\n', encoding='utf-8')
        task = structure.prepare_structured(self.pid, 'overview')
        # Valid source/revision echoes reach the intended empty-overview check.
        reply = {key: task[key] for key in ('source_sha256', 'source_revision', 'revision_of')}
        with patch('kbsys.answer.provider_status', return_value={'ready': True}), \
                patch.object(analysis_runner, '_call', return_value=json.dumps(reply)) as call:
            result = analysis_runner.run_structured(self.con, self.pid, kind='overview', allow_ai=True)
        self.assertEqual(call.call_count, 3)
        self.assertEqual(result['calls'], 3)
        validator_message = 'Overview has no items in any field; fill the lists directly at the top level'
        for repair in call.call_args_list[1:]:
            self.assertIn('\nRepair the prior validation error: ' + validator_message + '\n', repair.args[2])

    def test_media_supported_manual_path_and_writer_guard(self):
        from kbsys import media
        path = self.root / 'regions.json'
        regions = [{'kind': 'figure', 'object_id': self.pid + ':F0001', 'source': 'main', 'page': 1, 'bbox': [1, 2, 3, 4]}]
        path.write_text(json.dumps(regions))
        with patch.object(media, 'build', return_value={'status': 'partial'}) as build:
            pipeline.build_media(self.pid, str(path))
            build.assert_called_once_with(self.pid, regions=regions, persist=True)
        with self.assertRaisesRegex(ValueError, 'registered|source|Unknown'):
            pipeline.build_media(self.pid, str(path))
        names = {t['name'] for t in mcp_server.TOOLS}
        self.assertTrue({'build_media', 'media_status', 'structured_prepare', 'structured_submit', 'structured_run', 'structured_status'} <= names)
        with workflow.writer_lock():
            with patch.object(workflow._LOCAL, 'held', set()):
                with self.assertRaisesRegex(ValueError, 'Another writer'):
                    pipeline.build_media(self.pid)

    def test_pipeline_media_output_corruption_regions_change_and_interruption(self):
        crop = self.folder / 'crops' / 'fixture.png'
        crop.parent.mkdir()
        crop.write_bytes(b'fixture')
        manifest = {'bundles': [{'crop': {'path': 'crops/fixture.png'}}], 'status': 'partial'}
        db.write_json(self.folder / 'media_manifest.json', manifest)
        (self.folder / 'media_regions.json').write_text('[]')
        def inventory(pid):
            structure.build(pid)
            crop.write_bytes(b'fixture')
            return {'status': 'source_inventory', 'media_inventory_status': 'partial', 'region_completeness': 'not_assessed'}
        run = lambda: pipeline.run(self.con, config.load(), 'one', 'structure', say=lambda *a: None)[0]
        with patch.object(pipeline, '_structure_inventory', side_effect=inventory) as operation:
            self.assertFalse(run()['cache_hit'])
            self.assertTrue(run()['cache_hit'])
            crop.write_bytes(b'corrupt')
            self.assertFalse(run()['cache_hit'])
            self.assertTrue(run()['cache_hit'])
            (self.folder / 'media_regions.json').write_text('[{}]')
            self.assertFalse(run()['cache_hit'])
            crop.unlink()
            self.assertFalse(run()['cache_hit'])
            state = db.read_json(workflow.checkpoint_path('structure', self.pid))
            state['status'] = 'running'
            db.write_json(workflow.checkpoint_path('structure', self.pid), state)
            self.assertFalse(run()['cache_hit'])
            self.assertEqual(operation.call_count, 5)

    def test_corrupt_workflow_checkpoint_rebuilds_instead_of_trusting_done(self):
        source, output = self.root / 'input', self.root / 'output'
        source.write_text('source')
        def operation():
            output.write_text('inventory')
            return {'status': 'source_inventory', 'scientific_acceptance': 'not_assessed'}
        workflow.checkpoint('fixture', self.pid, [source], [output], operation)
        path = workflow.checkpoint_path('fixture', self.pid)
        path.write_text('{broken')
        self.assertEqual(workflow.status()[0]['status'], 'invalid')
        self.assertFalse(workflow.checkpoint('fixture', self.pid, [source], [output], operation)['cache_hit'])
        self.assertTrue(workflow.checkpoint('fixture', self.pid, [source], [output], operation)['cache_hit'])

    def test_first_media_publication_has_immediate_structure_cache_hit(self):
        crop = self.folder / 'crops' / 'new.png'
        def inventory(pid):
            structure.build(pid)
            crop.parent.mkdir(exist_ok=True)
            crop.write_bytes(b'new source crop')
            db.write_json(self.folder / 'media_manifest.json', {'bundles': [{'crop': {'path': 'crops/new.png'}}], 'status': 'partial'})
            return {'status': 'source_inventory', 'media_inventory_status': 'partial', 'region_completeness': 'not_assessed'}
        run = lambda: pipeline.run(self.con, config.load(), 'one', 'structure', say=lambda *a: None)[0]
        with patch.object(pipeline, '_structure_inventory', side_effect=inventory) as operation:
            self.assertFalse(run()['cache_hit'])
            self.assertTrue(run()['cache_hit'])
            self.assertEqual(operation.call_count, 1)

    def test_legacy_valid_profiles_and_status_excludes_reply_artifacts(self):
        self.submit_profiles()
        for name in ('section_profile.json', 'protocol_profile.json'):
            path = self.folder / name
            profile = db.read_json(path)
            del profile['source_revision']
            db.write_json(path, profile)
        self.assertTrue(structure.completion(self.pid)['complete'])
        task = structure.prepare_structured(self.pid)
        path = structure._task_path(self.pid, task['task_id'])
        db.write_json(path.parent / (task['task_id'] + '_wh-0001_reply.json'), {'raw': 'fixture'})
        self.assertEqual(len(structure.structured_status(self.pid)['tasks']), 1)
        state = db.read_json(path)
        state['chunks'][0].update(status='accepted', reply={'answers': {}}, output_sha256='forged')
        db.write_json(path, state)
        self.assertEqual(structure.structured_status(self.pid)['tasks'][0]['status'], 'invalid_output')

    def test_status_marks_older_task_superseded_and_next_says_done(self):
        self.submit_profiles()
        status = structure.structured_status(self.pid)
        self.assertTrue(status['profiles']['complete'])
        self.assertTrue(status['next'].startswith('nothing to do'))
        # an abandoned second task of a finished kind is history, not pending work
        task = structure.prepare_structured(self.pid)
        kinds = {t['task_id']: t for t in structure.structured_status(self.pid)['tasks']}
        self.assertEqual(kinds[task['task_id']]['status'], 'superseded')

    def test_real_raw_citations_wh_direct_and_staged_copy_with_attribution(self):
        self.rows[0]['text'] = 'According to earlier reports, reactive oxygen species may affect the test model [12].'
        self.rows[0]['citations'] = [{'marker': '[12]', 'ref_nums': [12], 'confidence': 'high'}]
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        db.write_jsonl(self.folder / 'references.jsonl', [{'num': 12, 'year': 2018, 'raw': 'Source bibliography fixture, 2018.'}])
        task = structure.prepare(self.pid)
        reply = self.wh_reply(task)
        result = structure.submit_profile(self.con, self.pid, reply, 'test')
        self.assertTrue(result['ok'])
        self.assertEqual(result['answers']['what']['quotes'][0]['text'], self.rows[0]['text'])
        self.assertEqual(structure._citation_items(self.rows[0], {12: {'year': 2018}}), [{'num': 12, 'year': 2018}])
        self.assertEqual(structure._citation_items(self.rows[0], {}), [{'num': 12, 'year': None}])
        task = structure.prepare_structured(self.pid, 'wh')
        result = structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], self.wh_reply(task))
        self.assertTrue(result['ok'])
        self.assertTrue(structure.completion(self.pid)['wh']['ready'])
        self.assertEqual(db.read_json(self.folder / 'section_profile.json')['answers']['what']['quotes'][0]['text'], self.rows[0]['text'])
        task = structure.prepare(self.pid)
        bad = self.wh_reply(task)
        bad['answers']['what']['text'] += ' [900]'
        self.assertFalse(structure.submit_profile(self.con, self.pid, bad, 'test')['ok'])
        task = structure.prepare(self.pid)
        bad = self.wh_reply(task)
        bad['answers']['what']['text'] += ' [1]'  # a bracket number the source does not carry is no item and no real reference
        self.assertEqual(structure.submit_profile(self.con, self.pid, bad, 'test')['errors'][0]['code'], 'struck:A1')
        # Cited secondary context must not become an unattributed primary result.
        task = structure.prepare(self.pid)
        reply = self.wh_reply(task)
        reply['answers']['what']['text'] = 'Reactive oxygen species may affect the test model.'
        result = structure.submit_profile(self.con, self.pid, reply, 'test')
        self.assertFalse(result['ok'])
        self.assertIn('A4', result['errors'][0]['code'])
        # Unresolved author-year markers likewise retain secondary-source attribution.
        self.rows[0]['citations'] = [{'marker': '(Unknown et al., 2018)', 'ref_nums': [], 'confidence': 'low'}]
        db.write_jsonl(self.folder / 'sentences.jsonl', self.rows)
        task = structure.prepare(self.pid)
        reply = self.wh_reply(task)
        reply['answers']['what']['text'] = 'Reactive oxygen species may affect the test model.'
        self.assertFalse(structure.submit_profile(self.con, self.pid, reply, 'test')['ok'])


if __name__ == '__main__':
    unittest.main()
