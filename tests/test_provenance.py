"""D3.18: heuristic evidence units are labelled. No live store access."""
import unittest
import unittest.mock
from kbsys import concepts, find, media, notes


class ProvenanceTests(unittest.TestCase):
    def test_analysis_origin(self):
        self.assertEqual(find.analysis_origin('hermes/kr-auto'), 'heuristic_draft')
        self.assertEqual(find.analysis_origin('hermes/kr-auto-2'), 'heuristic_draft')
        self.assertEqual(find.analysis_origin('claude/opus'), 'analysis')
        self.assertEqual(find.analysis_origin(None), 'unknown')
        self.assertEqual(find.analysis_origin(''), 'unknown')

    def test_unit_hit_carries_origin(self):
        u = {k: None for k in ('unit_id', 'paper_id', 'unit_type', 'statement', 'quote', 'status', 'hedging', 'species',
                               'cell_type', 'agent', 'measure', 'direction', 'value_source', 'analyst',
                               'paper_evidence_type', 'publication_types', 'sentence_ids', 'pages', 'sections')}
        u.update(unit_type='result', quote='A q.', analyst='hermes/kr-auto')
        self.assertEqual(find._unit_hit(u)['analysis_origin'], 'heuristic_draft')

    def test_callout_only_for_heuristic(self):
        h = '\n'.join(notes._heuristic_callout({'analyst': 'hermes/kr-auto'}))
        self.assertIn('[!warning]', h)
        self.assertIn('heuristic_draft', h)
        self.assertEqual(notes._heuristic_callout({'analyst': 'claude/opus'}), [])
        self.assertEqual(notes._heuristic_callout({}), [])

    def test_v2_note_shows_callout(self):
        meta = {'paper_id': 'p', 'title': 'T'}
        class Con:
            def execute(self, *a): return []
        for analyst, expect in (('hermes/kr-auto', True), ('claude/opus', False)):
            with unittest.mock.patch.object(notes, "read_jsonl", return_value=[]), \
                    unittest.mock.patch.object(media, "bundles", return_value=[]):
                out = '\n'.join(notes._render_v2(meta, {'classification': 'CORE', 'version': 2, 'analyst': analyst},
                                                 Con(), concepts.Registry([]), lambda t: True))
            self.assertEqual('Phân tích nháp tự động' in out, expect)


if __name__ == '__main__':
    unittest.main()
