"""U4: neural.prepare ranks by relevance, pages within the NW-01 budgets, labels units, flags translation; MCP hops default 0."""
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from . import test_core as fixtures
from kbsys import config, db, find, mcp_server, neural


class PrepareBudgetTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def ids(self, *positions):
        return [self.rows[i]['sentence_id'] for i in positions]

    def hits(self, *ids, matched=None):
        return {'hits': [{'paper_id': self.pid, 'sentence_ids': list(ids), 'matched_sentences': matched}]}

    def prepare(self, google, **kwargs):
        (config.vault_dir('one') / 'ROS.md').write_text('ROS note')
        with patch('kbsys.find.search', return_value=google):
            return neural.prepare(self.con, config.load(), 'one', 'ROS', **kwargs)

    def test_one_copy_of_each_field_and_unit_label_of_the_owning_unit(self):
        unit = {'unit_id': self.pid + ':E0001', 'paper_id': self.pid, 'seq': 1, 'unit_type': 'hypothesis', 'status': 'ok',
                'statement': 'ROS may matter.', 'quote': self.rows[0]['text'], 'sentence_ids': self.ids(0), 'pages': [1],
                'sections': ['introduction'], 'model': {}, 'intervention': {}, 'outcome': {}, 'terms': {}, 'hedging': 'hedged'}
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit])
        db.reload_paper(self.con, self.pid)
        self.con.commit()
        out = self.prepare(self.hits(*self.ids(0, 1)))
        self.assertNotIn('figures', out)
        self.assertEqual(out['source_resolution']['item_ids'], [i['id'] for i in out['items']])
        self.assertNotIn('items', out['source_resolution'])
        self.assertTrue(all('display_text' not in i for i in out['items']))
        by_id = {i['id']: i for i in out['items']}
        owned, free = by_id[self.rows[0]['sentence_id']], by_id[self.rows[1]['sentence_id']]
        self.assertEqual((owned['unit_type'], owned['kind'], owned['unit_id']), ('hypothesis', 'sentence', unit['unit_id']))
        self.assertEqual(owned['label'], find.UNIT_LABELS['hypothesis'])
        self.assertIsNone(free['unit_type'])   # no unit cites it: stays an unlabelled source sentence

    def test_matched_sentences_rank_first(self):
        out = self.prepare(self.hits(*self.ids(0, 1, 2), matched=self.ids(2)))
        order = [i['id'] for i in out['items']]
        self.assertEqual(order[:3], self.ids(2, 0, 1))
        self.assertEqual(out['total'], len(order))
        self.assertFalse(out['has_more'])
        self.assertIsNone(out['next_offset'])

    def test_rank_fusion_lifts_a_sentence_both_routes_find_and_keeps_graph_only_ones_below(self):
        # U6c (NW-02): find order a, b, c; graph-FTS order c, d -> c (both routes) first, d (graph only, weight 0.5) last
        google = {'hits': [{'sentence_ids': [sid]} for sid in ('a', 'b', 'c')]}
        ranked, google_ids = neural._ranked_sentence_ids(google, [{'id': 'c'}, {'id': 'd'}])
        self.assertEqual(ranked, ['c', 'a', 'b', 'd'])
        self.assertEqual(google_ids, {'a', 'b', 'c'})

    def test_pages_by_token_budget_and_offset_without_repeats(self):
        google = self.hits(*self.ids(0, 1, 2, 3, 4, 5))
        real = neural.standard.param
        tight = lambda clause, name: 330 if (clause, name) == ('NW-01', 'token_budget_fast') else real(clause, name)
        with patch.object(neural.standard, 'param', side_effect=tight):
            first = self.prepare(google)
            self.assertTrue(0 < len(first['items']) < first['total'])
            self.assertTrue(first['has_more'])
            self.assertEqual(first['next_offset'], len(first['items']))
            self.assertLessEqual(neural.est_tokens(len(first['prompt'].encode())), 330)
            second = self.prepare(google, offset=first['next_offset'])
        self.assertEqual(second['offset'], first['next_offset'])
        self.assertFalse({i['id'] for i in first['items']} & {i['id'] for i in second['items']})
        # the ticket holds exactly the page that was returned
        saved = db.read_json(config.CACHE_DIR / 'neural_sessions' / (second['ticket'] + '.json'))
        self.assertEqual([i['id'] for i in saved['items']], [i['id'] for i in second['items']])

    def test_payload_budget_shrinks_the_page(self):
        google = self.hits(*self.ids(0, 1, 2, 3, 4, 5))
        real = neural.standard.param
        tiny = lambda clause, name: 1 if (clause, name) == ('NW-01', 'max_payload_kb') else real(clause, name)
        with patch.object(neural.standard, 'param', side_effect=tiny):
            out = self.prepare(google)
        self.assertEqual(len(out['items']), 1)   # never empty while sentences remain: the page moves on
        self.assertTrue(out['has_more'])

    def test_payload_budget_keeps_the_most_items_that_fit_and_fingerprints_stay_in_the_ticket(self):
        google = self.hits(*self.ids(0, 1, 2, 3, 4, 5))
        full = self.prepare(google)
        for key in neural._TICKET_ONLY:   # check re-reads them from the ticket; the client never does
            self.assertNotIn(key, full)
        saved = db.read_json(config.CACHE_DIR / 'neural_sessions' / (full['ticket'] + '.json'))
        self.assertEqual(set(saved['source_fingerprints']), {i['id'] for i in full['items']})
        size = len(neural.json.dumps(full, ensure_ascii=False, default=str).encode())
        real = neural.standard.param
        cap = lambda clause, name: (size - 1) / 1024 if (clause, name) == ('NW-01', 'max_payload_kb') else real(clause, name)
        with patch.object(neural.standard, 'param', side_effect=cap):
            out = self.prepare(google)
        self.assertEqual(len(out['items']), len(full['items']) - 1)   # one byte short drops one item, not a tenth
        self.assertLessEqual(len(neural.json.dumps(out, ensure_ascii=False, default=str).encode()), size - 1)

    def test_translation_flag_and_english_query_come_from_the_search(self):
        (config.vault_dir('one') / 'ROS.md').write_text('ROS note')
        google = {**self.hits(*self.ids(0)), 'needs_translation': True}
        with patch('kbsys.find.search', return_value=google):
            out = neural.prepare(self.con, config.load(), 'one', 'tế bào ROS')
        self.assertTrue(out['needs_translation'])
        self.assertTrue(out['english_query'].isascii())
        self.assertIn('(English: ' + out['english_query'], out['prompt'])
        self.assertFalse(self.prepare(self.hits(*self.ids(0)))['needs_translation'])

    def test_media_keeps_figures_the_reader_shows_and_ids_of_sentences_already_items(self):
        sent = lambda sid, text: {'sentence_id': sid, 'text': text, 'page': 2, 'section': 'results', 'seq': 9, 'passage_id': 'x',
                                  'mentions': ['p:F1'], 'uncertainty': []}
        figure = {'object_id': 'p:F1', 'kind': 'figure', 'label': 'Figure 1', 'table': None, 'crop_url': '/crop', 'source_url': '/pdf/p',
                  'caption': [sent('p:S1', 'Fig 1.')], 'data': [], 'mentions': [sent('p:S2', 'in item'), sent('p:S3', 'not item')],
                  'anchors': [], 'unresolved_sentence_ids': [], 'reading_inconsistent': True, 'stored_caption_sentence_ids': ['p:S1'],
                  'associated_units': [{'unit_id': 'p:E1'}], 'region_input': {'big': 1},
                  'source_locator': {'status': 'available', 'page': 2, 'path': 'sources/x.pdf', 'cropbox': [0, 0, 1, 1]},
                  'crop': {'status': 'available', 'sha256': 'abc', 'path': 'crops/a.png', 'render_inputs': {'big': 1}},
                  'figure_reading': {'summary': 'AI read', 'status': 'ok', 'panels': ['x' * 500], 'provenance': {'big': 1}}}
        formula = {**figure, 'object_id': 'p:Q1', 'kind': 'formula'}
        group = {'vault': 'one', 'paper_id': 'p', 'bundles': [figure, formula], 'truncated': False}
        out = neural.slim_media(group, {'p:S2'})
        self.assertNotIn('vault', out)
        self.assertEqual([b['object_id'] for b in out['bundles']], ['p:F1', 'p:Q1'])   # formula regions stay: the panel shows them
        b = out['bundles'][0]
        self.assertEqual(b['mentions'], [{'sentence_id': 'p:S2'}, {k: v for k, v in sent('p:S3', 'not item').items()
                                                                  if k in ('sentence_id', 'text', 'page', 'section', 'uncertainty')}])
        self.assertEqual(b['caption'][0].keys(), {'sentence_id', 'text', 'page', 'section', 'uncertainty'})
        self.assertEqual(b['figure_reading'], {'summary': 'AI read', 'status': 'ok'})
        self.assertEqual(b['crop'], {'status': 'available', 'sha256': 'abc'})
        self.assertEqual(b['source_locator'], {'status': 'available', 'page': 2})
        self.assertTrue(b['reading_inconsistent'])
        for gone in ('stored_caption_sentence_ids', 'associated_units', 'region_input'):
            self.assertNotIn(gone, b)

    def test_media_row_of_an_item_keeps_its_source_file(self):
        # review U4: the item has no `source`, so an id-only row would read as main and link the main PDF
        row = {'sentence_id': 'p:S2', 'text': 'Supplement caption.', 'page': 1, 'source': 'p:X1'}
        figure = {'object_id': 'p:F1', 'kind': 'figure', 'caption': [row], 'data': [], 'mentions': [], 'anchors': [],
                  'source_locator': {}, 'crop': {}, 'figure_reading': None}
        out = neural.slim_media({'paper_id': 'p', 'bundles': [figure], 'truncated': False}, {'p:S2'})
        self.assertEqual(out['bundles'][0]['caption'], [{'sentence_id': 'p:S2', 'source': 'p:X1'}])

    def test_vault_notes_are_budgeted_in_bytes_not_characters(self):
        node = {'title': 'Tế bào ' * 20, 'path': 'a.md'}
        chars = len(neural.json.dumps({k: node.get(k) for k in ('title', 'path', 'depth', 'entity_kind', 'roles', 'summary',
                                                               'source_count')}, ensure_ascii=False)) + len('a.md') + 4
        self.assertEqual(neural._pick_nodes({'nodes': [node], 'edges': []}, chars)['nodes'], [])
        self.assertEqual(len(neural._pick_nodes({'nodes': [node], 'edges': []}, 10 * chars)['nodes']), 1)

    def test_prepare_media_drops_papers_without_figures(self):
        empty = {'vault': 'one', 'paper_id': self.pid, 'bundles': [], 'truncated': False}
        with patch.object(neural, 'media_reader', return_value=empty):
            out = self.prepare(self.hits(*self.ids(0)))
        self.assertEqual(out['media'], [])

    def test_mcp_hops_default_is_zero(self):
        with patch.object(neural, 'search', return_value={}) as search, patch.object(neural, 'prepare', return_value={}) as prepare:
            mcp_server.call_tool(self.con, config.load(), 'neural_search', {'vault': 'one', 'query': 'ROS'})
            mcp_server.call_tool(self.con, config.load(), 'neural_prepare', {'vault': 'one', 'question': 'ROS', 'offset': 7})
        self.assertEqual(search.call_args.args, ('one', 'ROS', 0))
        self.assertEqual(prepare.call_args.args[-1], 0)
        self.assertEqual(prepare.call_args.kwargs, {'offset': 7, 'session': None, 'index': False})


    def test_nw12_items_carry_item_type_and_the_paper_note_link(self):
        name = 'Papers/Nguyễn 2026 - Tế bào ROS.md'   # spaces and diacritics in the vault-relative path
        (config.vault_dir('one') / 'Papers').mkdir()
        (config.vault_dir('one') / name).write_text(f'---\npaper_id: {self.pid}\n---\nROS paper note\n', encoding='utf-8')
        unit = {'unit_id': self.pid + ':E0001', 'paper_id': self.pid, 'seq': 1, 'unit_type': 'hypothesis', 'status': 'ok',
                'statement': 'ROS may matter.', 'quote': self.rows[0]['text'], 'sentence_ids': self.ids(0), 'pages': [1],
                'sections': ['introduction'], 'model': {}, 'intervention': {}, 'outcome': {}, 'terms': {}, 'hedging': 'hedged'}
        db.write_jsonl(self.folder / 'evidence.jsonl', [unit])
        db.reload_paper(self.con, self.pid)
        for pos, kind in ((1, 'caption'), (2, 'table_row')):
            self.con.execute('UPDATE sentences SET kind=? WHERE sentence_id=?', (kind, self.rows[pos]['sentence_id']))
        self.con.commit()
        out = self.prepare(self.hits(*self.ids(0, 1, 2, 3)))
        by_id = {i['id']: i for i in out['items']}
        self.assertEqual([by_id[s]['item_type'] for s in self.ids(0, 1, 2, 3)], ['unit', 'caption', 'table_row', 'sentence'])
        for item in out['items']:
            self.assertEqual(item['note_path'], name)
            self.assertEqual(item['obsidian_url'], 'obsidian://open?vault=one&file=' + neural.quote(name, safe=''))
            self.assertTrue(item['obsidian_url'].isascii() and ' ' not in item['obsidian_url'])
            self.assertEqual(unquote(item['obsidian_url'].split('&file=')[1]), name)
            self.assertNotIn('note_missing', item)
        self.assertNotIn('item_type', out['prompt'])   # new item keys never reach the prompt text
        self.assertNotIn('obsidian://', out['prompt'])

    def test_nw12_a_paper_without_an_indexed_note_has_no_path_and_a_reason(self):
        out = self.prepare(self.hits(*self.ids(0)))   # the vault holds ROS.md only: no note of this paper
        item = out['items'][0]
        self.assertEqual((item['note_path'], item['obsidian_url'], item['item_type']), (None, None, 'sentence'))
        self.assertIn('no note', item['note_missing'])
        self.assertIn('vault index not built', neural.paper_note_links('two', [self.pid])[self.pid]['note_missing'])

    def test_nw12_media_groups_and_context_nodes_carry_links(self):
        (config.vault_dir('one') / 'Papers').mkdir()
        (config.vault_dir('one') / 'Papers/P.md').write_text(f'---\npaper_id: {self.pid}\n---\nnote\n', encoding='utf-8')
        bundle = {'object_id': 'p:F1', 'kind': 'figure', 'caption': [], 'data': [], 'mentions': [], 'anchors': [],
                  'source_locator': {}, 'crop': {}, 'figure_reading': None}
        group = {'vault': 'one', 'paper_id': self.pid, 'bundles': [bundle, {**bundle, 'object_id': 'p:T1', 'kind': 'table'}], 'truncated': False}
        with patch.object(neural, 'media_reader', return_value=group):
            out = self.prepare(self.hits(*self.ids(0)))
        shown = out['media'][0]
        self.assertEqual((shown['note_path'], shown['obsidian_url']), ('Papers/P.md', 'obsidian://open?vault=one&file=Papers%2FP.md'))
        self.assertEqual([b['item_type'] for b in shown['bundles']], ['figure', 'table'])
        graph = neural._compact_graph({'nodes': [{'path': 'Results/Tế bào 1.md', 'entity_kind': 'result'},
                                                 {'path': 'Claims/C.md', 'entity_kind': 'claim'},
                                                 {'path': 'Concepts/ROS.md', 'entity_kind': 'concept'}]}, 'one')
        self.assertEqual([n['item_type'] for n in graph['nodes']], ['result/claim note', 'result/claim note', 'node'])
        self.assertEqual(graph['nodes'][0]['obsidian_url'], 'obsidian://open?vault=one&file=Results%2FT%E1%BA%BF%20b%C3%A0o%201.md')
        self.assertTrue(all('note_path' not in n for n in graph['nodes']))


    def test_nw12_a_note_gone_since_the_index_was_built_is_reported_missing(self):
        (config.vault_dir('one') / 'Papers').mkdir()
        note = config.vault_dir('one') / 'Papers' / 'P.md'
        note.write_text(f'---\npaper_id: {self.pid}\n---\nnote\n', encoding='utf-8')
        neural.refresh('one', force=True)
        self.assertEqual(neural.paper_note_links('one', [self.pid])[self.pid]['note_path'], 'Papers/P.md')
        note.unlink()   # renamed or deleted after the last index build (ask_prepare never refreshes the index)
        link = neural.paper_note_links('one', [self.pid])[self.pid]
        self.assertEqual((link['note_path'], link['obsidian_url']), (None, None))
        self.assertIn('not on disk', link['note_missing'])


if __name__ == '__main__':
    unittest.main()
