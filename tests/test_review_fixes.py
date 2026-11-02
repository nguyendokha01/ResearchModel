import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import answer, config, notes, overlap, structure
from . import test_core


class ReviewFixTests(unittest.TestCase):
    def test_mixed_hedges_still_strike_a7(self):
        items = [
            {'text': 'The treatment may reduce apoptosis.', 'paper_id': 'p', 'kind': 'sentence', 'unit_type': None},
            {'text': 'The treatment reduces apoptosis.', 'paper_id': 'p', 'kind': 'sentence', 'unit_type': None},
        ]
        checked = answer.check_sentence({'text': 'The treatment reduces apoptosis.', 'evidence': [1, 2]}, items, {})
        self.assertEqual(checked['check'], 'struck:A7')

    def test_wh_rejects_biological_word_absent_from_source(self):
        sources = {'s1': {'sentence_id': 's1', 'page': 1, 'text': 'The experiment used zebrafish.', 'citations': []}}
        answers = {key: {'text': None, 'reason': 'Not supplied by this Introduction.'} for key in structure.WH}
        answers['what'] = {'text': 'The experiment used humans.', 'sentence_ids': ['s1']}
        accepted, errors = structure._wh_answers('review-fix-paper', answers, sources)
        self.assertNotIn('what', accepted)
        self.assertEqual(next(error['code'] for error in errors if error['field'] == 'what'), 'G1')
        answers['what'] = {'text': 'The experiment involved zebrafish.', 'sentence_ids': ['s1']}
        accepted, errors = structure._wh_answers('review-fix-paper', answers, sources)
        self.assertIn('what', accepted)
        self.assertTrue(any(w.startswith('G1:') for w in accepted['what']['warnings']))

    def test_overview_rejects_direction_word_absent_from_source(self):
        sources = {'s1': {'text': 'The treatment may reduce apoptosis.'}}
        warning = {field: [] for field in structure.OVERVIEW_FIELDS}
        warning['main_findings'] = [{'text': 'The treatment may influence apoptosis.', 'sentence_ids': ['s1'], 'certainty': 'may'}]
        checked = structure._validate_overview(warning, sources)
        self.assertTrue(checked['main_findings'][0]['warnings'][0].startswith('G1:'))
        item = {'text': 'The treatment may increase apoptosis.', 'sentence_ids': ['s1'], 'certainty': 'may'}
        reply = {field: [] for field in structure.OVERVIEW_FIELDS}
        reply['main_findings'] = [item]
        with self.assertRaisesRegex(ValueError, 'G1'):
            structure._validate_overview(reply, sources)

    def test_reasoning_note_refs_are_allowlisted_but_numeric_indexes_work(self):
        items = [{'text': 'The treatment may reduce 5 units.', 'paper_id': 'p', 'kind': 'sentence', 'unit_type': None}]
        bad = answer.check_parts({'reasoning': [{'text': 'A note-based claim.', 'basis': ['C:/private/notes.md']}]}, items, {})
        self.assertEqual(bad['reasoning'][0]['check'], 'struck:A8')
        good = answer.check_parts({'reasoning': [{'text': 'It may reduce 5 units.', 'basis': [1]}]}, items, {})
        self.assertEqual(good['reasoning'][0]['check'], 'ok')

    def test_two_word_overlap_query_uses_two_token_phrase(self):
        tokens, size, shingles = overlap._query_profile('embryo survival', 3)
        self.assertEqual(tokens, ['embryo', 'survival'])
        self.assertEqual(size, 2)
        self.assertEqual(shingles, {('embryo', 'survival')})

    def test_graph_default_search_is_restored_and_custom_search_survives(self):
        with tempfile.TemporaryDirectory(prefix='review-fix-') as temp:
            with patch.object(config, 'VAULTS_DIR', Path(temp)):
                notes.write_graph_settings('vault')
                path = Path(temp) / 'vault' / '.obsidian' / 'graph.json'
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['search'], notes.GRAPH_FILTER)
                custom = 'path:"Researcher filter" -tag:#kb-index'
                path.write_text(json.dumps({'search': custom}), encoding='utf-8')
                notes.write_graph_settings('vault')
                self.assertEqual(json.loads(path.read_text(encoding='utf-8'))['search'], custom)

    def test_human_ideas_palette_matches_current_and_future_paths(self):
        query, color, _ = notes.GRAPH_GROUPS[0]
        self.assertEqual(query, 'path:"Mine/"')  # not "Methods/Mineral oil": the slash anchors the folder
        self.assertNotIn(color, [c for _, c, _ in notes.GRAPH_GROUPS[1:]])


class GroundingTolerance(unittest.TestCase):
    def test_other_inflections_and_formats_are_not_rejected(self):
        src = ['Embryos treated with 0.5 mg/ml trehalose survived. Mouse oocytes were frozen.']
        for text in ('Embryo survival was measured after treatment with 0.50 mg/ml trehalose.',
                     'Treated embryos survived.', 'Mice oocytes were frozen.'):
            self.assertEqual(structure.source_grounding(text, src)['reject'], [], text)
        self.assertTrue(structure.source_grounding('Embryos survived at 9 mg/ml.', src)['reject'])
        self.assertTrue(structure.source_grounding('The p53 protein was frozen.', src)['reject'])

    def test_generic_biology_words_warn_but_specific_organisms_reject(self):
        src = ['Bovine embryos were vitrified.']
        self.assertEqual(structure.source_grounding('Embryos of this species were vitrified.', src)['reject'], [])
        self.assertEqual(structure.source_grounding('Porcine embryos were vitrified.', src)['reject'], ['Porcine'])

    def test_direction_synonyms_keep_polarity(self):
        # Pilot run: source "enhance", AI "improve" was rejected; opposite direction must still be rejected.
        src = ['Resveratrol may enhance developmental ability.']
        self.assertEqual(structure.source_grounding('Resveratrol may improve development.', src)['reject'], [])
        self.assertEqual(structure.source_grounding('Resveratrol may reduce development.', src)['reject'], ['reduce'])

    def test_vietnamese_answers_are_not_grounded_against_english(self):
        items = [{'text': 'The treatment may reduce apoptosis.', 'paper_id': 'p', 'kind': 'sentence', 'unit_type': None}]
        checked = answer.check_sentence({'text': 'Điều trị có thể làm giảm apoptosis [1].', 'evidence': [1]}, items, {})
        self.assertEqual(checked['check'], 'ok')


class OverviewEmpty(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def echo(self, task):
        return {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}

    def test_unknown_top_level_key_is_rejected(self):
        task = structure.prepare_overview(self.pid)
        item = {'text': 'The study may affect the example model.', 'sentence_ids': [self.rows[0]['sentence_id']], 'certainty': 'may'}
        nested = self.echo(task) | {'reply_format': {field: [item] for field in structure.OVERVIEW_FIELDS}}
        with self.assertRaisesRegex(ValueError, 'Unknown overview reply keys'):
            structure.submit_overview(self.con, self.pid, nested, 'test')

    def test_all_empty_overview_is_rejected_but_empty_chunk_is_allowed(self):
        task = structure.prepare_overview(self.pid)
        with self.assertRaisesRegex(ValueError, 'no items'):
            structure.submit_overview(self.con, self.pid, self.echo(task), 'test')
        task = structure.prepare_structured(self.pid, 'overview')
        with self.assertRaisesRegex(ValueError, 'no items'):
            structure.submit_structured(self.con, self.pid, task['task_id'], task['chunk']['chunk_id'], self.echo(task), 'test')
        # a chunk that is empty by itself is fine while another chunk of the overview has items
        empty = {field: [] for field in structure.OVERVIEW_FIELDS}
        full = dict(empty, main_findings=[{'text': 'x', 'sentence_ids': ['s'], 'certainty': 'c'}])
        self.assertTrue(structure._overview_empty(empty))
        self.assertFalse(structure._overview_empty(full))


if __name__ == '__main__':
    unittest.main(verbosity=2)
