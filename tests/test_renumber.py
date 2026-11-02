"""Tier 1 rebuild keeps saved analysis attached (renumber.py); all data in temporary stores."""
import copy
import unittest
from unittest.mock import patch

import pymupdf

from kbsys import db, evidence, extract, figure_reading, node_cards, renumber, structure, tier1
from . import test_core, test_extraction, test_figure_reading


def unit(pid, number, rows, ids):
    cited = [r for r in rows if r['sentence_id'] in ids]
    return {'unit_id': f'{pid}:E{number:04d}', 'seq': number, 'sentence_ids': list(ids),
            'quote': evidence._quote(cited), 'pages': [1], 'sections': sorted({r['section'] for r in cited}),
            'unit_type': 'background', 'statement': 's', 'hedging': 'none', 'analyst': 'test', 'status': 'ok', 'flags': []}


class CarryOverTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def ids(self, *numbers):
        return [self.rows[n]['sentence_id'] for n in numbers]

    def save_artifacts(self, overview_numbers=(0,)):
        """Real overview, WH and protocol profiles plus evidence units citing rows 0, 4 and 5."""
        task = structure.prepare_overview(self.pid)
        item = {'text': 'The study may affect the example model.', 'sentence_ids': self.ids(*overview_numbers),
                'certainty': 'may'}
        reply = {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        reply.update({field: [copy.deepcopy(item)] for field in structure.OVERVIEW_FIELDS})
        structure.submit_overview(self.con, self.pid, reply, 'test')
        wh = {k: structure.prepare(self.pid)[k] for k in ('source_sha256', 'revision_of')}
        wh['answers'] = {k: {'text': None, 'reason': 'Not supplied by this Introduction.'} for k in structure.WH}
        wh['answers']['what'] = {'text': self.rows[0]['text'], 'sentence_ids': self.ids(0)}
        self.assertTrue(structure.submit_profile(self.con, self.pid, wh, 'test')['ok'])
        proto = structure.prepare_protocol(self.pid)
        reply = {k: proto[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
        reply.update({'protocols': [{'name': 'example protocol', 'sentence_ids': self.ids(2), 'details': [],
                                     'steps': [{'sentence_ids': self.ids(2, 3)}]}], 'skipped': []})
        self.assertTrue(structure.submit_protocol(self.con, self.pid, reply, 'test')['ok'])
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit(self.pid, 1, self.rows, self.ids(0)),
                                                        unit(self.pid, 2, self.rows, self.ids(4)),
                                                        unit(self.pid, 3, self.rows, self.ids(5))])
        db.write_json(self.folder / 'analysis.json', {'version': 2, 'n_flagged': 0})

    def rebuild(self, edit):
        """Replace sentences.jsonl by edit(copy of rows) renumbered in order, then carry over."""
        old = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        before = renumber.snapshot(self.pid)
        rows = edit([dict(r) for r in old])
        for seq, row in enumerate(rows, start=1):
            row['seq'], row['sentence_id'] = seq, f'{self.pid}:S{seq:04d}'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        return renumber.carry_over(self.pid, old, rows, before), rows

    def new_sentence(self):
        return {**self.rows[0], 'text': 'A new opening sentence appeared.', 'section': 'introduction'}

    def profiles_ready(self):
        done = structure.completion(self.pid)
        return (structure.overview_status(self.pid)['ready'], done['wh']['ready'], done['protocol']['ready'])

    def test_pure_renumbering_keeps_every_artifact_attached_and_current(self):
        self.save_artifacts()
        self.assertEqual(self.profiles_ready(), (True, True, True))
        result, rows = self.rebuild(lambda r: [self.new_sentence(), *r])
        self.assertEqual((result['renumbered'], result['unmapped']), (6, 0))
        self.assertEqual(self.profiles_ready(), (True, True, True))
        units = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual(units[0]['sentence_ids'], [f'{self.pid}:S0002'])
        self.assertEqual([u['status'] for u in units], ['ok', 'ok', 'ok'])
        self.assertEqual(evidence.source_drift(units, {r['sentence_id']: r for r in rows}, self.pid), [])
        wh = db.read_json(self.folder / 'section_profile.json')
        self.assertEqual(wh['answers']['what']['sentence_ids'], [f'{self.pid}:S0002'])
        self.assertEqual(wh['source_sha256'], renumber.workflow.fingerprint([self.folder / 'sentences.jsonl']))

    def test_changed_sentence_stales_only_its_citers(self):
        self.save_artifacts(overview_numbers=(0, 4))  # the overview also cites the discussion sentence
        def edit(rows):
            rows[4]['text'] += ' It was revised.'          # text changed
            rows[5]['section'] = 'discussion'               # section changed, text identical
            return [self.new_sentence(), *rows]
        result, rows = self.rebuild(edit)
        self.assertEqual(result['unmapped'], 2)
        overview_ready, wh_ready, protocol_ready = self.profiles_ready()
        self.assertFalse(overview_ready)                    # cites a changed sentence: stays stale
        self.assertTrue(wh_ready and protocol_ready)        # cite only unchanged sentences: carried over
        ok, text_changed, section_changed = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual(ok['status'], 'ok')
        for flagged in (text_changed, section_changed):
            self.assertEqual(flagged['status'], 'flagged')
            self.assertEqual([f['code'] for f in flagged['flags']], ['V14'])
        self.assertEqual(db.read_json(self.folder / 'analysis.json')['n_flagged'], 2)

    def test_unmapped_id_now_naming_another_sentence_is_flagged_not_silently_followed(self):
        self.save_artifacts()
        self.rebuild(lambda r: [self.new_sentence(), *r[:4], *r[5:]])  # old S0005 vanishes; S0005 is now old S0004
        moved = list(db.read_jsonl(self.folder / 'evidence.jsonl'))[1]
        self.assertEqual(moved['sentence_ids'], [f'{self.pid}:S0005'])  # untouched: no guessing
        self.assertEqual(moved['status'], 'flagged')
        self.assertEqual(moved['flags'][0]['code'], 'V14')

    def test_cross_paper_files_rewrite_only_this_papers_full_ids(self):
        other = 'other2026_example_ffff'
        card = {'card': {'quotes': {
            f'{self.pid}:S0003': {'sentence_id': f'{self.pid}:S0003', 'paper_id': self.pid,
                                   'section': 'methods', 'page': 1, 'text': self.rows[2]['text']}},
            'fields': {'definition': {'sentence_ids': [f'{self.pid}:S0003', f'{other}:S0003', 'S0003']}}},
            'status': 'accepted'}
        path = renumber.config.STORE / 'node_cards' / 'one' / 'local_x.json'
        db.write_json(path, card)
        relations = renumber.config.STORE / 'relations.jsonl'
        db.write_jsonl(relations, [{'relation_id': 'r1', 'from_unit': f'{other}:E0001',
                                    'evidence': [f'{self.pid}:S0003', f'{other}:S0003']}])
        self.rebuild(lambda r: [self.new_sentence(), *r])
        stored = db.read_json(path)
        self.assertEqual(list(stored['card']['quotes']), [f'{self.pid}:S0004'])
        self.assertEqual(stored['card']['quotes'][f'{self.pid}:S0004']['sentence_id'], f'{self.pid}:S0004')
        self.assertEqual(stored['card']['fields']['definition']['sentence_ids'],
                         [f'{self.pid}:S0004', f'{other}:S0003', 'S0003'])
        self.assertEqual(list(db.read_jsonl(relations))[0]['evidence'], [f'{self.pid}:S0004', f'{other}:S0003'])
        stored['card']['fields']['definition']['sentence_ids'] = [f'{self.pid}:S0004']  # this paper only
        self.assertEqual(node_cards._card_currency(stored, {'concept_id': 'local:x'}, 'one', [self.pid], [])[0],
                         'current')

    def test_recheck_keeps_the_drift_flag_after_the_quote_is_refreshed(self):
        self.save_artifacts()
        rows = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        rows[4]['text'] += ' It was revised.'               # same ID, new text: the stored quote is now old
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        stored = list(db.read_jsonl(self.folder / 'evidence.jsonl'))

        def accept(con, paper_id, submission, legacy_terms_warning=False):
            accepted = [{**u, 'quote': evidence._quote([r for r in rows if r['sentence_id'] in u['sentence_ids']]),
                         '_validation_unit_index': i} for i, u in enumerate(stored, start=1)]
            return {'flags': [], 'errors': [], 'coverage_missing': [], 'warnings': []}, accepted, []

        with patch.object(evidence, 'validate', accept), patch.object(evidence, '_review_items'),                 patch.object(evidence.notes, 'write_paper_note'):
            for _ in range(2):  # the second pass has a refreshed quote and must still carry V14
                evidence.recheck(self.con, self.pid)
                after = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
                self.assertEqual([u['status'] for u in after], ['ok', 'flagged', 'ok'])
                self.assertEqual([f['code'] for f in after[1]['flags']], ['V14'])
                self.assertEqual(after[1]['quote'], evidence._quote([rows[4]]))
                stored = after

    def test_export_submission_is_the_complete_short_id_file_and_report_flags_drift(self):
        self.save_artifacts()
        stored = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        submission = evidence.export_submission(self.pid)
        self.assertEqual(submission['paper_id'], self.pid)
        self.assertEqual(len(submission['units']), len(stored))
        self.assertTrue(all(i.startswith('S') and ':' not in i for u in submission['units'] for i in u['sentence_ids']))
        self.assertFalse({'unit_id', 'quote', 'status', 'flags'} & set(submission['units'][0]))
        self.assertEqual(evidence.repair_report(self.pid)[-2:][0], 'UNCOVERED:')   # nothing drifted yet
        rows = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        rows[4]['text'] = 'Something else entirely happened here.'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        self.assertTrue(any('drift=True' in line for line in evidence.repair_report(self.pid)))

    def test_card_citing_a_section_changed_sentence_is_stale(self):
        quote = {'sentence_id': f'{self.pid}:S0006', 'paper_id': self.pid, 'section': 'conclusion', 'page': 1,
                 'text': self.rows[5]['text']}
        stored = {'card': {'quotes': {f'{self.pid}:S0006': quote},
                           'fields': {'x': {'text': 't', 'sentence_ids': [f'{self.pid}:S0006']}}}, 'status': 'accepted'}
        record = {'concept_id': 'local:x'}
        self.assertEqual(node_cards._card_currency(stored, record, 'one', [self.pid], [])[0], 'current')
        rows = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        rows[5]['section'] = 'discussion'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        self.assertEqual(node_cards._card_currency(stored, record, 'one', [self.pid], [])[0], 'stale')

    def test_renamed_key_never_overwrites_an_unmapped_entry(self):
        rewrite = renumber._rewriter({f'{self.pid}:S0001': f'{self.pid}:S0002'}, short=False)
        quotes = {f'{self.pid}:S0001': {'text': 'moved'}, f'{self.pid}:S0002': {'text': 'vanished'}}
        self.assertEqual(rewrite(quotes), {f'{self.pid}:S0002': {'text': 'vanished'}})  # stays stale, not hidden
        self.assertEqual(rewrite({f'{self.pid}:S0001': 1, 'other': 2}), {f'{self.pid}:S0002': 1, 'other': 2})

    def test_overview_with_a_stale_digest_is_not_made_current_by_the_rebuild(self):
        self.save_artifacts()
        path = self.folder / 'paper_overview.json'
        db.write_json(path, dict(db.read_json(path), overview_sha256='0' * 64))   # stale BEFORE: content digest differs
        self.assertFalse(structure.overview_status(self.pid)['ready'])
        self.rebuild(lambda r: [self.new_sentence(), *r])
        self.assertFalse(structure.overview_status(self.pid)['ready'])

    def test_impact_counts_a_protocol_whose_cited_sentences_changed_order(self):
        self.save_artifacts()
        old = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        new = [dict(r) for r in old]
        new[2], new[3] = new[3], new[2]                      # same texts, Methods order reversed
        for seq, row in enumerate(new, start=1):
            row['seq'], row['sentence_id'] = seq, f'{self.pid}:S{seq:04d}'
        report = renumber.impact(self.pid, old, renumber.id_map(old, new), new)
        self.assertEqual(report['profiles'], ['protocol_profile.json'])

    def test_impact_counts_only_artifacts_citing_unmapped_sentences(self):
        self.save_artifacts(overview_numbers=(0, 4))
        old = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        new = [self.new_sentence(), *[dict(r) for r in old]]
        new[5]['text'] += ' It was revised.'                # old row 4
        for seq, row in enumerate(new, start=1):
            row['seq'], row['sentence_id'] = seq, f'{self.pid}:S{seq:04d}'
        report = renumber.impact(self.pid, old, renumber.id_map(old, new), new)
        self.assertEqual(report['evidence_units'], [f'{self.pid}:E0002'])  # not E0001/E0003 (renumbered only)
        self.assertEqual(report['profiles'], ['paper_overview.json'])
        self.assertEqual((report['figure_readings'], report['node_cards'], report['relations']), ([], [], 0))
        new.append({'sentence_id': f'{self.pid}:S0099', 'section': 'methods', 'page': 1, 'source': 'main',
                    'kind': 'text', 'text': 'An unassigned Methods sentence.'})
        self.assertIn('protocol_profile.json', renumber.impact(self.pid, old, renumber.id_map(old, new), new)['profiles'])


class ReadingCarryOverTests(unittest.TestCase):
    setUp = test_figure_reading.FigureReadingTests.setUp
    tearDown = test_figure_reading.FigureReadingTests.tearDown
    add_paper = test_figure_reading.FigureReadingTests.add_paper
    reply = staticmethod(test_figure_reading.FigureReadingTests.reply)

    def legacy_reading(self):
        folder, object_id, crop = self.add_paper('paper')
        figure_reading.submit('paper', object_id, self.reply(), 'sonnet')
        path = folder / 'figure_readings' / 'F1.json'
        saved = db.read_json(path)
        saved.pop('cited_text_sha256')                      # a reading made before the content digest existed
        db.write_json(path, saved)
        return folder, object_id, crop, path

    def renumber_rows(self, folder, edit=lambda rows: rows):
        old = list(db.read_jsonl(folder / 'sentences.jsonl'))
        before = renumber.snapshot('paper')
        rows = edit([dict(r) for r in old])
        rows.insert(0, {'sentence_id': 'x', 'seq': 0, 'text': 'New first sentence.', 'section': 'results',
                        'kind': 'text', 'source': 'main'})
        for seq, row in enumerate(rows, start=1):
            row['seq'], row['sentence_id'] = seq, f'paper:S{seq:04d}'
        db.write_jsonl(folder / 'sentences.jsonl', rows)
        figures = list(db.read_jsonl(folder / 'figures.jsonl'))
        figures[0].update(caption_sentence_ids=['paper:S0002'], mentioned_in=['paper:S0003'])
        db.write_jsonl(folder / 'figures.jsonl', figures)
        return renumber.carry_over('paper', old, rows, before), rows

    def test_renumbering_keeps_a_current_reading_current(self):
        folder, object_id, _, path = self.legacy_reading()
        result, _ = self.renumber_rows(folder)
        self.assertEqual(result['renumbered'], 2)
        saved = db.read_json(path)
        self.assertEqual(saved['provenance']['caption_sentence_ids'], ['paper:S0002'])
        self.assertEqual(saved['caption_consistency'][0]['claim_sentence_id'], 'paper:S0002')
        self.assertTrue(saved['cited_text_sha256'])
        self.assertEqual(figure_reading.load('paper', object_id)['status'], 'current')
        rows = list(db.read_jsonl(folder / 'sentences.jsonl'))
        rows[1]['text'] += ' Revised source wording.'      # the caption text changes afterwards
        db.write_jsonl(folder / 'sentences.jsonl', rows)
        self.assertEqual(figure_reading.load('paper', object_id)['status'], 'stale')

    def test_reading_whose_caption_changed_is_not_made_current(self):
        folder, object_id, _, path = self.legacy_reading()
        self.renumber_rows(folder, lambda rows: [{**rows[0], 'text': rows[0]['text'] + ' Revised.'}, *rows[1:]])
        self.assertEqual(figure_reading.load('paper', object_id)['status'], 'stale')

    def test_reading_stale_before_the_rebuild_is_not_re_stamped(self):
        folder, object_id, crop, path = self.legacy_reading()
        crop.write_bytes(crop.read_bytes() + b'changed')    # stale BEFORE the rebuild
        self.assertEqual(renumber.snapshot('paper')['readings']['F1.json'], None)


class RebuildTests(unittest.TestCase):
    """tier1.reextract_dry_run predicts exactly what tier1.build carries over."""
    setUp = test_extraction.ExtractionAcceptance.setUp
    tearDown = test_extraction.ExtractionAcceptance.tearDown
    seed = test_extraction.ExtractionAcceptance.seed

    def make_paper(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 60), 'Results', fontsize=16)
            page.insert_text((70, 80), 'Table 1. Ragged values.')
            for y, row in zip((115, 145, 175), [('Group', 'n', 'Value'), ('Control', '8', '12.5'), ('Treated', '', '18.75')]):
                for x, text in zip((70, 230, 390), row):
                    if text:
                        page.insert_text((x, y), text)
            for x in (65, 225, 385, 540):
                page.draw_line((x, 95), (x, 190))
            for y in (95, 125, 155, 190):
                page.draw_line((65, y), (540, y))
            page.insert_text((70, 260), 'The treated group may differ from control.', fontsize=11)
            page.insert_text((70, 290), 'Values were higher after washing.', fontsize=11)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.seed(ex)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        # Emulate an earlier extraction without table-row sentences: later IDs were 2 lower.
        rows = [r for r in db.read_jsonl(self.folder / 'sentences.jsonl') if r['kind'] != 'table_row']
        for seq, row in enumerate(rows, start=1):
            row['seq'], row['sentence_id'] = seq, f'fixture:S{seq:04d}'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        self.old = rows
        db.write_json(self.folder / 'analysis.json', {'version': 2, 'n_flagged': 0})
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit('fixture', 1, rows, ['fixture:S0001']),
                                                        unit('fixture', 2, rows, ['fixture:S0004'])])
        return ex

    def test_dry_run_matches_build_and_counts_only_unmapped_citers(self):
        ex = self.make_paper()
        report = tier1.reextract_dry_run(self.con, None, 'fixture')
        self.assertEqual((report['renumbered_sentences'], len(report['unmapped_old_sentence_ids'])), (3, 0))
        self.assertEqual(report['stale_analysis_count'], 0)     # E0002 only renumbered
        self.assertEqual(report['needs_reanalysis']['evidence_units'], 0)
        info = tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        self.assertEqual(info['sentence_ids'], {'renumbered': 3, 'unmapped': 0})
        self.assertEqual(info['reference_ids'], {'renumbered': 0, 'unmapped': 0})   # D-PENDING-15: counted next to it
        self.assertEqual({k: report['reference_ids'][k] for k in ('renumbered', 'unmapped')}, info['reference_ids'])
        units = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual([u['sentence_ids'] for u in units], [['fixture:S0001'], ['fixture:S0006']])
        self.assertEqual([u['status'] for u in units], ['ok', 'ok'])

    def test_vanished_sentence_is_counted_and_flagged_without_guessing(self):
        ex = self.make_paper()
        rows = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        rows[3]['text'] = 'Values were higher after rinsing.'   # the old sentence is not in the new extraction
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit('fixture', 1, rows, ['fixture:S0001']),
                                                        unit('fixture', 2, rows, ['fixture:S0004'])])
        report = tier1.reextract_dry_run(self.con, None, 'fixture')
        self.assertEqual(report['stale_evidence_units'], ['fixture:E0002'])
        self.assertEqual(report['needs_reanalysis']['evidence_units'], 1)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        units = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual([u['status'] for u in units], ['ok', 'flagged'])
        self.assertEqual(units[1]['sentence_ids'], ['fixture:S0004'])   # left as written


    def test_reextract_rereads_passages_of_an_analysed_paper(self):
        self.make_paper()
        wrong = 'Values were higher after washing.'
        passages = list(db.read_jsonl(self.folder / 'passages.jsonl'))
        for row in passages:
            if wrong in row['text']:
                row['section'] = 'references'            # a labelling error stored by an older extractor
        db.write_jsonl(self.folder / 'passages.jsonl', passages)
        rows = list(db.read_jsonl(self.folder / 'sentences.jsonl'))
        for row in rows:
            if row['text'] == wrong:
                row['section'] = 'references'
        db.write_jsonl(self.folder / 'sentences.jsonl', rows)
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit('fixture', 1, rows, ['fixture:S0001']),
                                                        unit('fixture', 2, rows, ['fixture:S0004'])])
        report = tier1.reextract_dry_run(self.con, None, 'fixture')
        self.assertEqual(report['change_summary']['section_changes'], {'references -> results': 1})
        self.assertEqual(report['stale_evidence_units'], ['fixture:E0002'])  # the section label is part of the key
        info, how = tier1.reextract(self.con, None, 'fixture')
        self.assertIn('passages included', how)
        self.assertEqual({r['section'] for r in db.read_jsonl(self.folder / 'passages.jsonl') if wrong in r['text']}, {'results'})
        self.assertEqual(info['sentence_ids']['unmapped'], 1)
        units = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual([u['status'] for u in units], ['ok', 'flagged'])
        self.assertEqual(units[1]['sentence_ids'], ['fixture:S0004'])      # not re-pointed
        archived = list((self.folder / 'tier1_history').glob('*/passages.jsonl'))
        self.assertTrue(any('"section": "references"' in a.read_text(encoding='utf-8') for a in archived))  # old kept

    def test_failed_reextract_names_the_folder_with_the_previous_files(self):
        self.make_paper()
        with patch('kbsys.tier1.build', side_effect=ValueError('boom')):
            with self.assertRaises(RuntimeError) as caught:
                tier1.reextract(self.con, None, 'fixture')
        message = str(caught.exception)
        self.assertIn('boom', message)
        self.assertIn('tier1_history', message)
        archived = list((self.folder / 'tier1_history').glob('*/passages.jsonl'))
        self.assertTrue(any(str(a.parent) in message for a in archived))


class TextPreservationTests(unittest.TestCase):
    def test_glued_superscript_counts_as_present_in_either_spelling(self):
        def lost(old_text, *new_texts):
            old = [{'sentence_id': 'p:S0001', 'text': old_text}]
            new = [{'sentence_id': f'p:S{n + 2:04d}', 'text': t} for n, t in enumerate(new_texts)]
            return renumber.text_preservation(old, new, [], [])['lost_total']
        self.assertEqual(lost('Muse cells^51,54 were found. Others stayed.', 'Muse cells51,54 were found.', 'Others stayed.'), 0)
        self.assertEqual(lost('Muse cells51,54 were found. Others stayed.', 'Muse cells^51,54 were found.', 'Others stayed.'), 0)
        self.assertEqual(lost('Muse cells^51,54 were found. Others stayed.', 'Muse cells were found.', 'Others stayed.'), 1)


if __name__ == '__main__':
    unittest.main()
