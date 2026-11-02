"""D3.29 neural ranking: lexical/semantic floor plus bounded graph context."""
import unittest
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import config, neural, notes, workflow
import time
from argparse import Namespace
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path
import urllib.request


AUTO, AUTO_END = notes.AUTO_START + '\n', notes.AUTO_END + '\n'


class NeuralRankingTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def test_specific_note_beats_high_degree_hub(self):
        vault = config.vault_dir('one')
        (vault / 'Specific.md').write_text('specific biomarker ' + self.rows[0]['sentence_id'], encoding='utf-8')
        links = ' '.join('[[Other%d]]' % i for i in range(8))
        (vault / 'Hub.md').write_text('hub context ' + links, encoding='utf-8')
        for i in range(8):
            (vault / ('Other%d.md' % i)).write_text('linked context', encoding='utf-8')
        result = neural.search('one', 'specific biomarker', hops=1)
        self.assertEqual(result['nodes'][0]['path'], 'Specific.md')
        self.assertNotEqual(result['nodes'][0]['path'], 'Hub.md')

    def test_vietnamese_query_finds_english_sentence_and_keeps_google_hit(self):
        vault = config.vault_dir('one')
        english_id = self.rows[3]['sentence_id']
        google_id = self.rows[0]['sentence_id']
        (vault / 'Cells.md').write_text('tế bào ' + english_id, encoding='utf-8')
        google = {'hits': [{'paper_id': self.pid, 'sentence_ids': [google_id]}]}
        with patch('kbsys.find.search', return_value=google):
            prepared = neural.prepare(self.con, config.load(), 'one', 'tế bào')
        ids = [item['id'] for item in prepared['items']]
        self.assertIn(english_id, ids)
        self.assertIn(google_id, ids)
        english = next(item for item in prepared['items'] if item['id'] == english_id)
        self.assertIn('cells', english['text'].lower())
        self.assertIn('Cells.md', prepared['papers'][english['paper_id']]['graph_context']['notes'])


class NeuralIndexTests(unittest.TestCase):
    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        self._reset_refresh_throttle()

    def tearDown(self):
        fixtures.Demo3Tests.tearDown(self)

    def _reset_refresh_throttle(self):
        with neural._REFRESH_GUARD:
            neural._REFRESH_STATE.clear()
            neural._REFRESHING.clear()

    def _edge_rows(self):
        con = neural._cache('one', readonly=True)
        try:
            return [tuple(row) for row in con.execute(
                'SELECT source,target,label,status FROM edges ORDER BY source,target,label,status')]
        finally:
            con.close()

    def test_search_serves_snapshot_when_index_writer_is_busy(self):
        vault = config.vault_dir('one')
        (vault / 'A.md').write_text('ROS source [[B]]', encoding='utf-8')
        (vault / 'B.md').write_text('B source', encoding='utf-8')
        first = neural.search('one', 'ROS')
        self.assertEqual(first['index_status'], 'ready')
        with workflow.writer_lock('vault-index:one'):
            result = neural.search('one', 'ROS')
        self.assertEqual(result['index_status'], 'refreshing')
        self.assertEqual([n['path'] for n in result['nodes']], [n['path'] for n in first['nodes']])

        import web
        server = web.Server(('127.0.0.1', 0), web.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        request = urllib.request.Request(
            f"http://127.0.0.1:{server.server_address[1]}/api/neural/search?vault=one&q=ROS",
            headers={'X-KB-Token': web.TOKEN})
        with workflow.writer_lock('vault-index:one'):
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 200)
                payload = json.load(response)
        self.assertEqual(payload['index_status'], 'refreshing')

    def test_incremental_edges_match_full_rebuild(self):
        vault = config.vault_dir('one')
        for i in range(300):
            target = f'Note{(i + 1) % 300:03d}'
            (vault / f'Note{i:03d}.md').write_text(
                f'---\nkb_kind: concept\n---\nNode {i} [[{target}]]', encoding='utf-8')
        self._reset_refresh_throttle()
        neural.refresh('one')
        before = self._edge_rows()
        changed = vault / 'Note000.md'
        changed.write_text('---\nkb_kind: concept\n---\nNode 0 [[Note002]]', encoding='utf-8')
        self._reset_refresh_throttle()
        started = time.perf_counter()
        neural.refresh('one')
        elapsed = time.perf_counter() - started
        after = self._edge_rows()
        changed_sources = {row[0] for row in before if row not in after} | {row[0] for row in after if row not in before}
        self.assertEqual(changed_sources, {'Note000.md'})
        self.assertLess(elapsed, 1.0)

        # Invalidate the edge-format marker to force the reference rebuild.
        con = neural._cache('one')
        try:
            con.execute("DELETE FROM meta WHERE key='edge_format'")
            con.commit()
        finally:
            con.close()
        self._reset_refresh_throttle()
        neural.refresh('one')
        self.assertEqual(after, self._edge_rows())

    def _refresh_edges(self):
        self._reset_refresh_throttle()
        neural.refresh('one')
        return self._edge_rows()

    def test_edges_resolve_the_way_obsidian_does(self):
        vault = config.vault_dir('one')
        (vault / '_Attachments' / 'p1').mkdir(parents=True)
        (vault / '_Attachments' / 'p1' / 'F1.png').write_bytes(b'png')
        (vault / 'Caspase.md').write_text('Caspase', encoding='utf-8')
        (vault / 'A.md').write_text(
            '| [[Caspase\\|alias]] | ![[_Attachments/p1/F1.png|640]] | ![[F1.png]] |\n[[#Description]] [[^abc]]\n'
            '```\n[[In a fence]]\n```\nand `[[inline]]` but [[Nowhere]]', encoding='utf-8')
        got = {(row[2], row[1], row[3]) for row in self._refresh_edges() if row[0] == 'A.md'}
        self.assertEqual(got, {
            ('Caspase\\|alias', 'Caspase.md', 'resolved'),
            ('_Attachments/p1/F1.png|640', '_Attachments/p1/F1.png', 'resolved'),
            ('F1.png', '_Attachments/p1/F1.png', 'resolved'),
            ('Nowhere', 'Nowhere', 'missing')})

    def test_attachment_added_or_removed_re_resolves_its_edges(self):
        vault = config.vault_dir('one')
        (vault / 'A.md').write_text('![[F1.png]] [[B]]', encoding='utf-8')
        (vault / 'B.md').write_text('B', encoding='utf-8')
        self.assertEqual([r[3] for r in self._refresh_edges() if r[2] == 'F1.png'], ['missing'])
        (vault / '_Attachments').mkdir()
        (vault / '_Attachments' / 'F1.png').write_bytes(b'png')
        added = self._refresh_edges()
        self.assertEqual([r[3] for r in added if r[2] == 'F1.png'], ['resolved'])
        (vault / '_Attachments' / 'F1.png').unlink()
        self.assertEqual([r[3] for r in self._refresh_edges() if r[2] == 'F1.png'], ['missing'])
        (vault / '_Attachments' / 'F1.png').write_bytes(b'png')
        incremental = self._refresh_edges()
        con = neural._cache('one')
        try:
            con.execute("DELETE FROM meta WHERE key='edge_format'")
            con.commit()
        finally:
            con.close()
        self.assertEqual(incremental, self._refresh_edges())  # equal to a full rebuild

    def test_attachment_behind_a_link_out_of_the_vault_is_not_the_vaults(self):
        vault = config.vault_dir('one')
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, outside, True)
        (outside / 'secret.png').write_bytes(b'png')
        link = vault / 'link'
        try:
            os.symlink(outside, link, target_is_directory=True)
        except OSError:
            if os.name != 'nt':
                self.skipTest('no directory links here')
            import _winapi
            _winapi.CreateJunction(str(outside), str(link))  # a junction is no symlink: rglob walks into it
        try:
            (vault / 'A.md').write_text('![[secret.png]]', encoding='utf-8')
            self.assertEqual(neural._vault_files(vault), [])
            self.assertEqual([r[3] for r in self._refresh_edges() if r[2] == 'secret.png'], ['missing'])
        finally:
            os.rmdir(link) if os.name == 'nt' else link.unlink()

    def _typed_edges(self, source):
        self._reset_refresh_throttle()
        neural.refresh('one')
        con = neural._cache('one', readonly=True)
        try:
            return sorted((tuple(r) for r in con.execute(
                'SELECT target,etype,unit_id,sentence_ids FROM edges WHERE source=? AND status=?', (source, 'resolved'))), key=str)
        finally:
            con.close()

    def _write_typed_vault(self, evd_id='p1:E0001'):
        vault = config.vault_dir('one')
        for folder in ('Papers', 'Organisms', 'Concepts', 'Methods', 'Results', 'Claims'):
            (vault / folder).mkdir(exist_ok=True)
        for name in ('Organisms/Mouse', 'Concepts/Alpha', 'Methods/Imm', 'Papers/P'):
            (vault / (name + '.md')).write_text('x', encoding='utf-8')
        evd = ("---\ntype: result\nid: {id}\nsource: '[[P]]'\nsubject: '[[Alpha]]'\nknowledge_level: {level}\nin_taxon:\n- '[[Mouse]]'\n"
               "uses_technique:\n- '[[Imm]]'\nsentence_ids:\n- p1:S0001\n---\n# E\n\n" + AUTO + "[[Mouse]] [[Alpha]] [[P]]\n" + AUTO_END)
        (vault / 'Results' / 'EVD A.md').write_text(evd.format(id=evd_id, level='knowledge_assertion'), encoding='utf-8')
        (vault / 'Results' / 'EVD B.md').write_text(evd.format(id='p1:E0002', level='statistical_association'), encoding='utf-8')
        for name, claim_type in (('conc', 'author_conclusion'), ('rev', 'review_statement'), ('odd', 'surprise')):
            (vault / 'Claims' / f'CLM {name}.md').write_text(
                f"---\ntype: claim\nid: p1:E1{len(name)}\nclaim_type: {claim_type}\nsentence_ids:\n- p1:S0009\nabout:\n- '[[Alpha]]'\n---\n# C\n",
                encoding='utf-8')
        (vault / 'Claims' / 'CONFLICT x.md').write_text(
            "---\ntype: conflict\nconflicts_with:\n- '[[EVD A]]'\n---\n# X\n\n" + AUTO + "Linked [[EVD A]] and [[Alpha]]\n" + AUTO_END, encoding='utf-8')

    def test_edges_are_typed_from_the_note_type_and_the_property(self):
        self._write_typed_vault()
        a = self._typed_edges('Results/EVD A.md')
        unit = ('p1:E0001', '["p1:S0001"]')
        self.assertEqual(a, sorted([
            ('Organisms/Mouse.md', 'in_taxon', *unit), ('Organisms/Mouse.md', 'observed_effect', *unit),  # property, then body
            ('Concepts/Alpha.md', 'observed_effect', *unit), ('Concepts/Alpha.md', 'observed_effect', *unit),
            ('Methods/Imm.md', 'uses_method', *unit), ('Papers/P.md', 'nav', None, None), ('Papers/P.md', 'nav', None, None)], key=str))
        self.assertEqual({e[1] for e in self._typed_edges('Results/EVD B.md') if e[0] == 'Concepts/Alpha.md'}, {'observed_association'})
        # evidence rule 4: a claim never becomes observed_effect; an unknown claim_type is the weaker one
        self.assertEqual(self._typed_edges('Claims/CLM conc.md')[0][1:], ('hypothesis_only', 'p1:E14', '["p1:S0009"]'))
        self.assertEqual(self._typed_edges('Claims/CLM rev.md')[0][1], 'cited_background')
        self.assertEqual(self._typed_edges('Claims/CLM odd.md')[0][1], 'hypothesis_only')
        # a conflict note cites the target result note's unit; links to other notes are nav
        self.assertEqual(self._typed_edges('Claims/CONFLICT x.md'), sorted([
            ('Results/EVD A.md', 'conflicts_with', *unit), ('Results/EVD A.md', 'conflicts_with', *unit),
            ('Concepts/Alpha.md', 'nav', None, None)], key=str))

    def test_a_link_to_a_topic_map_only_navigates(self):
        self._write_typed_vault()
        vault = config.vault_dir('one')
        (vault / '_Maps').mkdir(exist_ok=True)
        (vault / '_Maps' / 'Topic.md').write_text('x', encoding='utf-8')
        (vault / 'Claims' / 'CLM map.md').write_text(
            "---\ntype: claim\nid: p1:E1\nclaim_type: author_conclusion\nsentence_ids:\n- p1:S0009\nabout:\n- '[[Topic]]'\n---\n# C\n",
            encoding='utf-8')
        self.assertEqual([e[1] for e in self._typed_edges('Claims/CLM map.md')], ['nav'])

    def test_a_body_link_outside_the_auto_block_only_navigates(self):  # D-PENDING-21 B1
        self._write_typed_vault()
        vault = config.vault_dir('one')
        head = "---\ntype: result\nid: p1:E0005\nknowledge_level: knowledge_assertion\nsentence_ids:\n- p1:S0001\n---\n# E\n\n"
        (vault / 'Results' / 'EVD C.md').write_text(head + "[[Alpha]]\n" + AUTO + "[[Mouse]]\n" + AUTO_END + "[[Imm]]\n", encoding='utf-8')
        self.assertEqual([(e[0], e[1]) for e in self._typed_edges('Results/EVD C.md')],
                         [('Concepts/Alpha.md', 'nav'), ('Methods/Imm.md', 'nav'), ('Organisms/Mouse.md', 'observed_effect')])
        for name, body in (('none', "[[Mouse]]\n"), ('two', AUTO + "[[Mouse]]\n" + AUTO_END + AUTO + AUTO_END),
                           ('unclosed', AUTO + "[[Mouse]]\n"), ('reversed', AUTO_END + "[[Mouse]]\n" + AUTO)):
            (vault / 'Results' / 'EVD C.md').write_text(head + body, encoding='utf-8')
            self.assertEqual([e[1] for e in self._typed_edges('Results/EVD C.md')], ['nav'], name)
        (vault / 'Claims' / 'CLM body.md').write_text(  # a claim: only its property link is typed, its body link is not
            "---\ntype: claim\nid: p1:E15\nclaim_type: author_conclusion\nsentence_ids:\n- p1:S0009\nabout:\n- '[[Alpha]]'\n---\n# C\n\n[[Alpha]]\n",
            encoding='utf-8')
        self.assertEqual([e[1] for e in self._typed_edges('Claims/CLM body.md')], ['hypothesis_only', 'nav'])

    def test_a_not_significant_result_gives_no_observed_effect(self):  # D-PENDING-21 B3, B2
        self._write_typed_vault()
        vault = config.vault_dir('one')
        evd = (vault / 'Results' / 'EVD A.md').read_text(encoding='utf-8')
        self.assertIn('knowledge_level: knowledge_assertion\n', evd)
        for value, expected in (('ns', 'nav'), (' Not Significant ', 'nav'), ('significant', 'observed_effect')):
            (vault / 'Results' / 'EVD C.md').write_text(  # B2: conflicts_with property -> result-to-result conflicts_with
                evd.replace('knowledge_level: knowledge_assertion\n', f"knowledge_level: knowledge_assertion\nsignificance: '{value}'\n"
                            "conflicts_with:\n- '[[EVD B]]'\n").replace('p1:E0001', 'p1:E0003'), encoding='utf-8')
            edges = self._typed_edges('Results/EVD C.md')
            self.assertEqual({e[1] for e in edges if e[0] in ('Concepts/Alpha.md', 'Organisms/Mouse.md')} - {'in_taxon'}, {expected}, value)
            self.assertIn(('Organisms/Mouse.md', 'in_taxon', 'p1:E0003', '["p1:S0001"]'), edges)
            self.assertIn(('Results/EVD B.md', 'conflicts_with', 'p1:E0003', '["p1:S0001"]'), edges)

    def test_every_item_of_a_subject_or_object_list_is_an_observed_effect_edge(self):  # D-PENDING-23 A4, A5
        from kbsys import result_notes
        self._write_typed_vault()
        vault = config.vault_dir('one')
        (vault / 'Concepts' / 'Beta.md').write_text('x', encoding='utf-8')
        values = {'type': 'result', 'id': 'p1:E0007', 'subject': ['[[Alpha]]', '[[Beta]]'], 'predicate': 'reports',
                  'object': ['[[Beta]]', '[[Alpha]]'], 'measure': 'Alpha and Beta levels', 'agent': 'Alpha and Beta',
                  'knowledge_level': 'observation', 'sentence_ids': ['p1:S0001']}
        (vault / 'Results' / 'EVD L.md').write_text(result_notes._frontmatter_text(values) + '# E\n', encoding='utf-8')
        edges = self._typed_edges('Results/EVD L.md')
        self.assertEqual(sorted((e[0], e[1]) for e in edges), [('Concepts/Alpha.md', 'observed_effect')] * 2 + [('Concepts/Beta.md', 'observed_effect')] * 2)

    def test_a_dash_run_inside_a_value_does_not_close_the_frontmatter(self):
        self._write_typed_vault()
        (config.vault_dir('one') / 'Results' / 'EVD C.md').write_text(  # only a line `---` closes it (evidence rule 4)
            "---\ntype: result\nid: p1:E0003\npredicate: a --- b\nknowledge_level: statistical_association\nobject: '[[Alpha]]'\n"
            "sentence_ids:\n- p1:S0001\n---\n# E\n", encoding='utf-8')
        self.assertEqual([e[1] for e in self._typed_edges('Results/EVD C.md')], ['observed_association'])
        self.assertEqual(neural._front("---\ntype: result\n[[Alpha]]\n"), ({}, 0))  # unclosed: no frontmatter, as in Obsidian

    def test_conflict_edge_follows_a_changed_target_id_like_a_full_rebuild(self):
        self._write_typed_vault()
        self._typed_edges('Claims/CONFLICT x.md')
        evd = config.vault_dir('one') / 'Results' / 'EVD A.md'  # only the target changes: the conflict note is not rewritten
        evd.write_text(evd.read_text(encoding='utf-8').replace('p1:E0001', 'p1:E0099'), encoding='utf-8')
        incremental = self._typed_edges('Claims/CONFLICT x.md')
        self.assertEqual({e[2] for e in incremental if e[1] == 'conflicts_with'}, {'p1:E0099'})
        con = neural._cache('one')
        try:
            con.execute("DELETE FROM meta WHERE key='edge_format'")
            con.commit()
        finally:
            con.close()
        self.assertEqual(incremental, self._typed_edges('Claims/CONFLICT x.md'))

    def test_search_edges_do_not_show_the_type_columns_or_duplicate_a_property_link(self):
        vault = config.vault_dir('one')
        (vault / 'Organisms').mkdir()
        (vault / 'Organisms' / 'Mouse.md').write_text('Mouse zebrafish', encoding='utf-8')
        (vault / 'Results').mkdir()
        (vault / 'Results' / 'EVD A.md').write_text(
            "---\ntype: result\nid: p1:E0001\nin_taxon:\n- '[[Mouse]]'\nsentence_ids:\n- p1:S0001\n---\nzebrafish [[Mouse]]\n", encoding='utf-8')
        edges = neural.search('one', 'zebrafish', hops=1)['edges']
        self.assertEqual(len(edges), 1)  # the property link and the body link are one row for the reader
        self.assertEqual(set(edges[0]), {'source', 'target', 'label', 'status', 'is_conflict'})

    def test_hops_follow_knowledge_edges_and_skip_hubs(self):  # RL-09, NW-04
        vault = config.vault_dir('one')
        for folder in ('Concepts', 'Papers', 'Results', 'Claims'):
            (vault / folder).mkdir(exist_ok=True)
        for name in ('Hub', 'Bridge', 'Plain'):
            (vault / 'Concepts' / f'{name}.md').write_text(f'---\nkb_kind: concept\n---\n{name} node hubterm' if name == 'Hub' else f'{name} node', encoding='utf-8')
        for i in range(4):  # Hub is linked from 3 of the 4 papers: more than half, so a hub
            (vault / 'Papers' / f'P{i}.md').write_text('---\nkb_kind: paper\n---\nPaper' + (' [[Hub]]' if i < 3 else ''), encoding='utf-8')
        head = '---\ntype: {}\nid: p1:E000{}\nsentence_ids:\n- p1:S0001\n---\n# N\n\n'
        (vault / 'Results' / 'Seed.md').write_text(head.format('result', 1) + 'seedterm [[Plain]]\n' + AUTO + '[[Bridge]] [[Hub]]\n' + AUTO_END, encoding='utf-8')
        (vault / 'Claims' / 'Far.md').write_text(head.format('claim', 2) + AUTO + '[[Bridge]]\n' + AUTO_END, encoding='utf-8')
        paths = lambda query, hops: {n['path'] for n in neural.search('one', query, hops=hops)['nodes']}
        self.assertEqual(paths('seedterm', 1), {'Results/Seed.md', 'Concepts/Bridge.md'})  # not Plain (navigation), not Hub
        self.assertEqual(paths('seedterm', 2), {'Results/Seed.md', 'Concepts/Bridge.md', 'Claims/Far.md'})
        result = neural.search('one', 'hubterm', hops=2)  # a hub that is a seed stays, but is not expanded
        self.assertEqual([n['path'] for n in result['nodes']], ['Concepts/Hub.md'])
        self.assertEqual(result['edges'], [])

    def _graph(self, links, nodes=(), prop=None):
        """Nodes in Concepts/ and result notes Results/<name>.md; links = [(name, [nodes], sentence number or None, in_taxon target)]."""
        vault = config.vault_dir('one')
        for folder in ('Concepts', 'Results', 'Papers'):
            (vault / folder).mkdir(exist_ok=True)
        names = set(nodes) | {n for _, linked, _, _ in links for n in linked}
        for name in sorted(names):
            if not (vault / 'Concepts' / f'{name}.md').exists():
                (vault / 'Concepts' / f'{name}.md').write_text(f'---\nkb_kind: concept\n---\n{name} node', encoding='utf-8')
        for i, (name, linked, number, taxon) in enumerate(links, 1):
            ids = f'{self.pid}:S{number:04d}' if number else 'nope:S0001'
            head = f'---\ntype: result\nid: {self.pid}:E{i:04d}\nsentence_ids:\n- {ids}\n' + (f'in_taxon: "[[{taxon}]]"\n' if taxon else '') + '---\n# N\n\n'
            (vault / 'Results' / f'{name}.md').write_text(head + AUTO + ' '.join(f'[[{n}]]' for n in linked) + '\n' + AUTO_END, encoding='utf-8')

    def test_walk_lists_typed_neighbours_with_quotes_and_hub_flag(self):
        vault = config.vault_dir('one')
        self._graph([('R1', ['Start', 'Alpha'], 1, None), ('R2', ['Start', 'Hub'], 2, None), ('R3', ['Start', 'Ghost'], None, None)])
        (vault / 'Concepts' / 'Start.md').write_text('---\nkb_kind: concept\n---\nStart node, plain link [[NavOnly]]', encoding='utf-8')
        (vault / 'Concepts' / 'NavOnly.md').write_text('---\nkb_kind: concept\n---\nNav node', encoding='utf-8')
        for i in range(4):  # Hub is linked from 3 of the 4 paper notes: a hub
            (vault / 'Papers' / f'P{i}.md').write_text('---\nkb_kind: paper\n---\nPaper' + (' [[Hub]]' if i < 3 else ''), encoding='utf-8')
        result = neural.walk(self.con, 'one', 'start')
        self.assertEqual([(s['target'], s['hub']) for s in result['steps']], [('Alpha', False), ('Hub', True)])  # not NavOnly, not Ghost (no stored sentence)
        first = result['steps'][0]
        self.assertEqual((first['source_path'], first['via'], first['etypes'], first['unit_id']),
                         ('Concepts/Start.md', 'Results/R1.md', ['observed_effect', 'observed_effect'], f'{self.pid}:E0001'))
        self.assertEqual(first['quotes'], [{'sentence_id': self.rows[0]['sentence_id'], 'paper_id': self.pid, 'page': 1,
                                            'section': 'introduction', 'text': self.rows[0]['text']}])  # copied from the stored sentence
        self.assertEqual(neural.walk(self.con, 'one', 'Concepts/Start.md')['total'], 2)
        limited = neural.walk(self.con, 'one', 'Start', limit=1)
        self.assertEqual((len(limited['steps']), limited['total'], limited['truncated']), (1, 2, True))

    def test_path_through_a_hub_and_not_beyond_max_steps(self):
        vault = config.vault_dir('one')
        longest = neural.standard.param('NW-05', 'max_steps') + 1
        chain = [(f'C{i}', [f'N{i}', f'N{i + 1}'], 1 + i % 6, None) for i in range(longest)]
        self._graph(chain + [('H1', ['X', 'Hub'], 5, None), ('H2', ['Hub', 'Y'], 6, None)])
        for i in range(4):
            (vault / 'Papers' / f'P{i}.md').write_text('---\nkb_kind: paper\n---\nPaper' + (' [[Hub]]' if i < 3 else ''), encoding='utf-8')
        through = neural.find_path(self.con, 'one', 'X', 'Y')
        self.assertTrue(through['found'])  # a named pair is an explicit request: the hub is marked, not skipped
        self.assertEqual([(n['title'], n['hub']) for n in through['nodes']], [('X', False), ('Hub', True), ('Y', False)])
        self.assertTrue(all(s['etypes'] and s['unit_id'] and s['quotes'] for s in through['steps']))
        self.assertEqual(len(neural.find_path(self.con, 'one', 'N0', f'N{longest - 1}')['steps']), longest - 1)
        beyond = neural.find_path(self.con, 'one', 'N0', f'N{longest}')
        self.assertEqual((beyond['found'], beyond['steps'], beyond['message']), (False, [], neural.NOT_FOUND))

    def test_names_unknown_ambiguous_and_empty(self):
        vault = config.vault_dir('one')
        self._graph([('R1', ['Start', 'Alpha'], 1, None)])
        (vault / 'Concepts' / 'Alpha.md').write_text('---\nkb_kind: concept\naliases: [First letter]\n---\nAlpha node', encoding='utf-8')
        (vault / 'Concepts' / 'Dup.md').write_text('---\nkb_kind: concept\n---\nDup', encoding='utf-8')
        (vault / 'Methods').mkdir(exist_ok=True)
        (vault / 'Methods' / 'Dup.md').write_text('---\nkb_kind: method\n---\nDup', encoding='utf-8')
        self.assertTrue(neural.find_path(self.con, 'one', 'FIRST LETTER', 'start')['found'])  # alias and title, case-insensitive
        self.assertEqual(neural.walk(self.con, 'one', 'Nowhere'), {'vault': 'one', 'found': False, 'steps': [], 'message': neural.NOT_FOUND})
        self.assertFalse(neural.find_path(self.con, 'one', 'Start', 'Nowhere')['found'])
        with self.assertRaisesRegex(ValueError, 'Concepts/Dup.md'):
            neural.walk(self.con, 'one', 'Dup')
        from kbsys import mcp_server
        for name, args in (('neural_walk', {'node': ' '}), ('neural_path', {'source': 'a'}), ('neural_path', {'source': 'a', 'target': 3})):
            with self.assertRaises(ValueError):
                mcp_server._check_args(self.con, name, args)

    def test_endpoints_and_step_targets_are_node_notes_and_source_differs_from_target(self):
        # NW-05: a Results note is only a step; naming one is refused (it exists: never 'not found'); source == target refused
        self._graph([('R1', ['Start', 'Alpha'], 1, None)])
        with self.assertRaisesRegex(ValueError, 'not a node note'):
            neural.walk(self.con, 'one', 'R1')
        with self.assertRaisesRegex(ValueError, 'not a node note'):
            neural.find_path(self.con, 'one', 'Start', 'Results/R1.md')
        with self.assertRaisesRegex(ValueError, 'same node'):
            neural.find_path(self.con, 'one', 'Start', 'Concepts/Start.md')  # compared after resolving
        self.assertTrue(all(s['target_path'].startswith('Concepts/') for s in neural.walk(self.con, 'one', 'Start')['steps']))
        for pair in (('Nowhere', 'R1'), ('R1', 'Nowhere')):  # refused in either argument order, never 'not found'
            with self.assertRaisesRegex(ValueError, 'not a node note'):
                neural.find_path(self.con, 'one', *pair)
        with self.assertRaisesRegex(ValueError, 'not a node note'):
            neural.walk(self.con, 'one', 'Results\\R1.md')  # a Windows-typed path names the existing note
        self.assertTrue(neural.walk(self.con, 'one', 'Concepts\\Start.md')['found'])
        (config.vault_dir('one') / 'Papers' / 'Alpha.md').write_text('---\nkb_kind: paper\n---\nPaper', encoding='utf-8')
        self.assertEqual(neural._explicit('one', ['Alpha'], lambda idx, paths: paths), ['Concepts/Alpha.md'])  # one node + one non-node match: the node

    def test_small_vault_where_every_node_is_a_hub_still_walks(self):
        # U3 doubt: in a 1-2 paper vault every node can be a hub; walk and path mark hubs, never skip them
        vault = config.vault_dir('one')
        self._graph([('R1', ['Start', 'Alpha'], 1, None)])
        for i in range(2):
            (vault / 'Papers' / f'P{i}.md').write_text('---\nkb_kind: paper\n---\nPaper [[Start]] [[Alpha]]', encoding='utf-8')
        walked = neural.walk(self.con, 'one', 'Start')
        self.assertTrue(walked['found'] and walked['node']['hub'] and all(s['hub'] for s in walked['steps']))
        self.assertEqual(walked['steps'][0]['unit_id'], f'{self.pid}:E0001')  # one unit_id per step
        self.assertTrue(neural.find_path(self.con, 'one', 'Start', 'Alpha')['found'])

    def test_path_tie_prefers_fewer_taxon_and_method_steps(self):
        # both routes Start -> Goal have 2 steps; the taxon route sorts first, so only the ordering rule picks the other
        self._graph([('A1', ['Start'], 1, 'Alpha'), ('A2', ['Alpha', 'Goal'], 2, None),  # A1 links Alpha by its in_taxon property
                     ('B1', ['Start', 'Beta'], 3, None), ('B2', ['Beta', 'Goal'], 4, None)], nodes=['Alpha'])
        path = neural.find_path(self.con, 'one', 'Start', 'Goal')
        self.assertEqual([n['title'] for n in path['nodes']], ['Start', 'Beta', 'Goal'])
        self.assertEqual(neural.find_path(self.con, 'one', 'Start', 'Alpha')['steps'][0]['etypes'], ['observed_effect', 'in_taxon'])  # still found alone

    def test_step_keeps_the_stronger_edge_type_of_one_note(self):
        # R1 links Alpha in its kb:auto block and by in_taxon: the step is observed, not only a shared species (review 8c8dc80)
        self._graph([('R1', ['Start', 'Alpha'], 1, 'Alpha')])
        self.assertEqual(neural.walk(self.con, 'one', 'Start')['steps'][0]['etypes'], ['observed_effect', 'observed_effect'])

    def test_nfd_name_resolves_and_no_index_is_no_absence(self):
        import unicodedata
        self._graph([('R1', [unicodedata.normalize('NFC', 'Tế bào'), 'Alpha'], 1, None)])
        self.assertTrue(neural.walk(self.con, 'one', unicodedata.normalize('NFD', 'tế bào'))['found'])  # CHR-01 (review 8c8dc80)
        for suffix in ('', '-wal', '-shm'):
            Path(str(neural._index_path('one')) + suffix).unlink(missing_ok=True)
        self._reset_refresh_throttle()
        with workflow.writer_lock('vault-index:one'), self.assertRaisesRegex(RuntimeError, 'refreshing'):
            neural.walk(self.con, 'one', 'Alpha')  # an index being built is no answer, never NOT_FOUND

    def test_exact_title_and_alias_rank_before_body_matches(self):
        vault = config.vault_dir('one')
        (vault / 'Methods').mkdir()
        (vault / 'Concepts').mkdir()
        (vault / 'Methods' / 'Vitrification.md').write_text(
            '---\nkb_kind: method\naliases: [cryopreservation method]\n---\nProtocol for vitrification.', encoding='utf-8')
        (vault / 'Concepts' / 'Embryo.md').write_text(
            '---\nkb_kind: concept\n---\nEmbryo notes mention vitrification.', encoding='utf-8')
        (vault / 'Background.md').write_text('Background body mentions vitrification.', encoding='utf-8')
        neural.refresh('one')
        result = neural.search('one', 'Vitrification')
        self.assertEqual(result['nodes'][0]['path'], 'Methods/Vitrification.md')

    def test_search_excerpt_removes_note_scaffolding(self):
        vault = config.vault_dir('one')
        (vault / 'Raw.md').write_text(
            '---\nkb_kind: concept\nsource_papers: [p]\n---\n<!-- kb:auto:start -->\n'
            '> [!quote]- source\n> Vitrification is discussed here.\n<!-- kb:auto:end -->', encoding='utf-8')
        result = neural.search('one', 'Vitrification')
        excerpt = result['nodes'][0]['excerpt']
        self.assertIn('Vitrification is discussed here.', excerpt)
        self.assertNotIn('source_papers:', excerpt)
        self.assertNotIn('kb:auto', excerpt)
        self.assertNotIn('[!quote]', excerpt)
        self.assertNotIn('>', excerpt)

    def test_notes_command_prewarms_neural_index(self):
        import kb

        tidy = {'moved': [], 'human_protected': []}
        with patch.object(kb.notes, 'refresh_all', return_value=0), \
             patch.object(kb.concept_notes, 'refresh', return_value={}), \
             patch.object(kb.source_notes, 'write', return_value=0), \
             patch.object(kb.notes, 'write_indexes'), \
             patch.object(kb.notes, 'last_tidy_report', return_value=tidy), \
             patch.object(kb.vault_layout, 'append_log'), \
             patch.object(kb.mother_vault, 'refresh', return_value={}), \
             patch.object(kb.neural, 'refresh', return_value={'index_status': 'ready'}) as prewarm:
            kb.cmd_notes(Namespace(vault='one'), config.load(), self.con)
        prewarm.assert_called_once_with('one')


if __name__ == '__main__':
    unittest.main(verbosity=2)
