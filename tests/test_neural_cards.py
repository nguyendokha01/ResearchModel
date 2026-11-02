"""Offline contracts for neural typed cards, conflicts, and related media."""
import shutil
import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import config, db, neural


class NeuralCardTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def _note(self):
        sid = self.rows[0]['sentence_id']
        db.write_jsonl(config.STORE / 'concepts.jsonl', [{
            'concept_id': 'local:ros', 'name': 'ROS', 'type': 'chemical', 'synonyms': []
        }])
        (config.vault_dir('one') / 'ROS.md').write_text(
            f'---\nkb_kind: chemical\n---\nROS source {sid}\n', encoding='utf-8')
        return sid

    def _bundle(self, sid, crop_status='unavailable'):
        return {
            'object_id': self.pid + ':F1', 'kind': 'figure', 'label': 'Figure 1',
            'caption': [{**self.rows[0], 'text': 'Figure 1. Survival caption copied from source.'}],
            'data': [], 'mentions': [{**self.rows[1]}], 'anchors': [],
            'associated_units': [], 'unresolved_sentence_ids': [],
            'source_locator': {'page': 1, 'status': 'unavailable', 'region_kind': 'object_region',
                               'region_basis': 'stored_locator', 'region_completeness': 'not_assessed'},
            'crop': {'status': crop_status, 'sha256': None, 'path': None}, 'table': None,
        }

    def test_node_reader_returns_typed_card_conflict_sides_and_figure_reading(self):
        sid = self._note()
        card = {
            'status': 'draft', 'summary_card': {'text': 'ROS summary', 'refs': [sid]},
            'sections': {'definition': [{'statement': 'Reactive oxygen species', 'refs': [sid], 'hedged': True}]},
        }
        conflict = {
            'relation_id': 'CONFLICT-1', 'type': 'direction_differs', 'status': 'proposed',
            'source_sides': [
                {'statement': 'ROS increased survival', 'direction': 'increase', 'species': 'bovine',
                 'stage': 'oocyte', 'verbatim_quote': 'Side one quote', 'paper_link': '/pdf/a'},
                {'statement': 'ROS decreased survival', 'direction': 'decrease', 'species': 'bovine',
                 'stage': 'embryo', 'verbatim_quote': 'Side two quote', 'paper_link': '/pdf/b'},
            ],
        }
        fake_media = self._bundle(sid)
        with patch.object(neural, 'node_cards', SimpleNamespace(load=lambda vault, concept_id: card)), \
             patch.object(neural, 'result_notes', SimpleNamespace(load=lambda vault, ident: {'open_conflicts': [conflict]})), \
             patch.object(neural, 'figure_reading', SimpleNamespace(load=lambda paper, object_id: {'summary': 'AI read', 'inconsistent': True})), \
             patch.object(neural.media, 'bundles', return_value=[fake_media]):
            result = neural.node_reader(self.con, 'one', 'ROS.md')

        self.assertEqual(result['card']['status'], 'draft')
        self.assertEqual(result['card']['sections']['definition'][0]['quotes'][0]['text'], self.rows[0]['text'])
        self.assertEqual(result['conflicts'][0]['source_sides'][0]['direction'], 'increase')
        self.assertEqual(result['media'][0]['bundles'][0]['figure_reading']['summary'], 'AI read')
        self.assertTrue(result['media'][0]['bundles'][0]['reading_inconsistent'])
        self.assertEqual(result['media'][0]['bundles'][0]['caption'][0]['text'], 'Figure 1. Survival caption copied from source.')

    def test_missing_card_falls_back_and_unverified_figure_keeps_caption_without_url(self):
        sid = self._note()
        with patch.object(neural, 'node_cards', None), patch.object(neural.media, 'bundles', return_value=[self._bundle(sid)]):
            result = neural.node_reader(self.con, 'one', 'ROS.md')
        self.assertIsNone(result['card'])
        self.assertEqual(result['card_status'], 'missing')
        bundle = result['media'][0]['bundles'][0]
        self.assertIsNone(bundle['crop_url'])
        self.assertEqual(bundle['caption'][0]['text'], 'Figure 1. Survival caption copied from source.')

    def test_neural_javascript_parses(self):
        node = shutil.which('node') or r'C:\Program Files\nodejs\node.exe'
        if not Path(node).is_file():
            self.skipTest('Node runtime absent; no dependency installation')
        path = Path(__file__).parents[1] / 'scripts/kbsys/web/neural.js'
        subprocess.run([node, '--check', str(path)], capture_output=True, text=True,
                       encoding='utf-8', check=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
