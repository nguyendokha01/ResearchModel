"""G6 unit 5: kb.py canvas writes the walk of one node as an Obsidian Canvas (temp vault only)."""
import json
import subprocess
import unittest
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import canvas, config, neural, notes

AUTO, AUTO_END = notes.AUTO_START + '\n', notes.AUTO_END + '\n'


def step(target, via, etypes=('observed_effect', 'observed_effect'), hub=False):
    return {'source': 'Start', 'source_path': 'Concepts/Start.md', 'target': target, 'target_path': f'Concepts/{target}.md',
            'via': via, 'etypes': list(etypes), 'unit_id': 'u', 'sentence_ids': [], 'quotes': [], 'hub': hub}


STEPS = [step('Alpha', 'Results/R1.md'), step('Hub', 'Results/R2.md', ('hypothesis_only', 'observed_effect'), True),
         step('Hub', 'Results/R3.md', ('in_taxon', 'in_taxon'), True)]
WALK = {'vault': 'one', 'found': True, 'node': {'path': 'Concepts/Start.md', 'title': 'Start', 'hub': False},
        'total': 3, 'truncated': False, 'steps': STEPS}
MISS = {'vault': 'one', 'found': False, 'steps': [], 'message': neural.NOT_FOUND}


class CanvasTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def run_canvas(self, walk=WALK, node='Start', vault='one'):
        with patch.object(neural, 'walk', return_value=walk):
            return canvas.write(self.con, vault, node)

    def test_counts_labels_hub_colour_and_valid_json(self):
        out, message = self.run_canvas()
        self.assertEqual((out, message), (config.vault_dir('one') / 'Canvas' / 'Start.canvas', None))
        raw = out.read_bytes()
        self.assertNotIn(b'\r', raw)
        data = json.loads(raw)
        self.assertEqual(data['kb_machine'], canvas._digest(data))
        by_file = {n['file']: n for n in data['nodes']}
        self.assertEqual(len(data['nodes']), 3)  # centre + Alpha + Hub (two steps to Hub share one file node)
        self.assertEqual(set(by_file), {'Concepts/Start.md', 'Concepts/Alpha.md', 'Concepts/Hub.md'})
        self.assertTrue(all(n['type'] == 'file' and {'id', 'x', 'y', 'width', 'height'} <= set(n) for n in data['nodes']))
        self.assertEqual(by_file['Concepts/Hub.md']['color'], '1')
        self.assertNotIn('color', by_file['Concepts/Alpha.md'])
        self.assertNotIn('color', by_file['Concepts/Start.md'])
        self.assertEqual([e['label'] for e in data['edges']], ['observed_effect', 'hypothesis_only/observed_effect', 'in_taxon'])
        self.assertEqual([e['kb_via'] for e in data['edges']], ['Results/R1.md', 'Results/R2.md', 'Results/R3.md'])  # rule 3 trace
        ids = {n['id'] for n in data['nodes']}
        self.assertEqual(len(data['edges']), len(STEPS))
        self.assertTrue(all(e['fromNode'] == by_file['Concepts/Start.md']['id'] and e['toNode'] in ids for e in data['edges']))
        self.assertEqual(len({e['id'] for e in data['edges']}), 3)

    def test_layout_is_deterministic_and_rewrite_of_our_own_canvas_is_idempotent(self):
        out, _ = self.run_canvas()
        first = out.read_bytes()
        again, _ = self.run_canvas()
        self.assertEqual((again, again.read_bytes()), (out, first))
        self.assertEqual(sorted(p.name for p in out.parent.iterdir()), ['Start.canvas'])

    def test_canvas_without_marker_is_never_overwritten(self):
        folder = config.vault_dir('one') / 'Canvas'
        folder.mkdir()
        mine = '{"nodes": [], "edges": []}'
        (folder / 'Start.canvas').write_text(mine, encoding='utf-8')
        out, _ = self.run_canvas()
        self.assertEqual(out.name, 'Start_1.canvas')
        self.assertEqual((folder / 'Start.canvas').read_text(encoding='utf-8'), mine)
        (folder / 'Start_1.canvas').write_text('not json', encoding='utf-8')  # the next one is free, never the researcher's
        out, _ = self.run_canvas()
        self.assertEqual(out.name, 'Start_2.canvas')
        self.assertEqual((folder / 'Start_1.canvas').read_text(encoding='utf-8'), 'not json')

    def test_edited_canvas_other_node_and_cut_walk(self):
        """Review 2026-11-03: a researcher edit inside our canvas (marker kept), another node with the same name, a cut walk."""
        out, _ = self.run_canvas()
        data = json.loads(out.read_text(encoding='utf-8'))
        data['nodes'].append({'id': 'mine', 'type': 'text', 'text': 'my idea', 'x': 0, 'y': 0, 'width': 10, 'height': 10})
        edited = json.dumps(data)
        out.write_text(edited, encoding='utf-8')
        again, message = self.run_canvas()
        self.assertEqual((again.name, out.read_text(encoding='utf-8')), ('Start_1.canvas', edited))
        self.assertIn('kept', message)
        other = {**WALK, 'node': {'path': 'Methods/Start.md', 'title': 'Start', 'hub': False}, 'truncated': True, 'total': 9}
        third, message = self.run_canvas(other)
        self.assertEqual(third.name, 'Start_2.canvas')  # Start_1 is our canvas of Concepts/Start.md, never replaced
        self.assertIn('3 of 9 steps shown', message)

    def test_not_found_writes_nothing(self):
        out, message = self.run_canvas(MISS, node='Nowhere')
        self.assertEqual((out, message), (None, neural.NOT_FOUND))
        self.assertFalse((config.vault_dir('one') / 'Canvas').exists())

    def test_unknown_vault_refused(self):
        for bad in ('nope', '..', ''):
            with self.assertRaises(ValueError):
                self.run_canvas(vault=bad)
        self.assertFalse((self.root / 'Canvas').exists())
        outside = self.root / 'outside'  # review: a Canvas junction out of the vault is refused, nothing written there
        outside.mkdir()
        if subprocess.run(['cmd', '/c', 'mklink', '/J', str(config.vault_dir('one') / 'Canvas'), str(outside)], capture_output=True).returncode:
            self.skipTest('no junction')
        with self.assertRaises(ValueError):
            self.run_canvas()
        self.assertEqual(list(outside.iterdir()), [])

    def test_real_walk_on_a_temp_vault(self):
        vault = config.vault_dir('one')
        for folder in ('Concepts', 'Results'):
            (vault / folder).mkdir()
        for name in ('Start', 'Alpha'):
            (vault / 'Concepts' / f'{name}.md').write_text(f'---\nkb_kind: concept\n---\n{name} node', encoding='utf-8')
        head = f'---\ntype: result\nid: {self.pid}:E0001\nsentence_ids:\n- {self.rows[0]["sentence_id"]}\n---\n# N\n\n'
        (vault / 'Results' / 'R1.md').write_text(head + AUTO + '[[Start]] [[Alpha]]\n' + AUTO_END, encoding='utf-8')
        out, message = canvas.write(self.con, 'one', 'Start')
        self.assertIsNone(message)
        data = json.loads(out.read_text(encoding='utf-8'))
        self.assertEqual([n['file'] for n in data['nodes']], ['Concepts/Start.md', 'Concepts/Alpha.md'])
        self.assertEqual([e['label'] for e in data['edges']], ['observed_effect'])
        self.assertEqual([e['kb_via'] for e in data['edges']], ['Results/R1.md'])  # the walk's vault-relative note path
        self.assertEqual(canvas.write(self.con, 'one', 'Nowhere'), (None, neural.NOT_FOUND))


if __name__ == '__main__':
    unittest.main()
