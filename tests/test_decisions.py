"""U8 / AN-17, AN-19: the researcher's "chốt" is a create-only `decision` note; later answers (neural_prepare and ask_prepare)
surface it as "ý chốt của bạn (date)" next to the evidence, and evidence newer than the decision is flagged once."""
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import agent_tasks, answer, config, db, discussions, mcp_server, neural

WORDS = 'Tôi chốt: ROS tăng sống sót.'


class DecisionTests(unittest.TestCase):
    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))  # never the real store
        self.sid = self.rows[0]['sentence_id']
        patcher = patch.object(answer, 'ANSWER_CACHE', self.root / 'answers')   # the traces of the asks stay in the temp project
        patcher.start()
        self.addCleanup(patcher.stop)

    tearDown = fixtures.Demo3Tests.tearDown

    def decide(self, title='ROS decision', words=WORDS, citations=(), **kw):
        return discussions.save('one', title, 'ROS is settled.', 'test', 'decision', con=self.con,
                                researcher_words=words, citations=list(citations), **kw)

    def read(self, saved):
        return (config.vault_dir('one') / saved['saved']).read_text(encoding='utf-8')

    def add_paper(self, pid, ingested):
        """A second paper of vault one with a ROS sentence; `ingested` = papers.ingested_at."""
        folder = db.paper_dir(pid)
        text = 'ROS was measured in the second paper.'
        row = {'sentence_id': pid + ':S0001', 'seq': 1, 'passage_id': pid + ':0001', 'page': 2, 'section': 'results',
               'subsection': None, 'kind': 'text', 'source': 'main', 'text': text, 'start': 0, 'end': len(text),
               'citations': [], 'mentions': []}
        db.write_json(folder / 'metadata.json', {'paper_id': pid, 'title': 'Second', 'year': 2026, 'vaults': ['one'], 'authors': [],
                                                 'source_files': [], 'evidence_type': 'primary_research', 'extraction_version': 2})
        db.write_jsonl(folder / 'sentences.jsonl', [row])
        db.write_jsonl(folder / 'passages.jsonl', [{'passage_id': row['passage_id'], 'seq': 1, 'page': 2, 'section': 'results',
                                                    'kind': 'text', 'text': text}])
        db.load_paper(self.con, pid)
        self.con.execute('UPDATE papers SET ingested_at=? WHERE paper_id=?', (ingested, pid))
        self.con.commit()
        return row['sentence_id']

    def prepare(self, pids=None, question='ROS', count=2, direct=False, **kw):
        """The MCP neural_prepare call (it alone writes the flag cache); direct = neural.prepare itself."""
        pids = pids or [self.pid]
        hits = [{'paper_id': pid, 'sentence_ids': [r['sentence_id'] for r in self.all_rows(pid)][:count], 'matched_sentences': None} for pid in pids]
        with patch('kbsys.find.search', return_value={'hits': hits}):
            if direct:
                return neural.prepare(self.con, config.load(), 'one', question, **kw)
            return mcp_server.call_tool(self.con, config.load(), 'neural_prepare', {'vault': 'one', 'question': question, **kw})

    def all_rows(self, pid):
        return list(db.read_jsonl(db.paper_dir(pid) / 'sentences.jsonl'))

    def ask(self, question='ROS'):
        return mcp_server.call_tool(self.con, config.load(), 'ask_prepare', {'question': question, 'queries': ['ROS'],
                                                                          'view': 'evidence4', 'vault': 'one', 'speed': 'fast'})

    def flags(self):
        return db.read_json(config.CACHE_DIR / 'decision_flags.json')

    # --- saving -------------------------------------------------------------------------------------------------

    def test_create_only_second_decision_links_the_first_and_never_edits_it(self):
        first = self.decide()
        before = (config.vault_dir('one') / first['saved']).read_bytes()
        second = self.decide()
        third = self.decide()
        self.assertEqual([first['saved'], second['saved'], third['saved']],
                         ['Discussions/ROS decision.md', 'Discussions/ROS decision (2).md', 'Discussions/ROS decision (3).md'])
        self.assertEqual((config.vault_dir('one') / first['saved']).read_bytes(), before)
        self.assertNotIn('earlier', self.read(first))
        self.assertIn('earlier: Discussions/ROS decision.md', self.read(second))
        self.assertIn('[[Discussions/ROS decision]]', self.read(second))
        self.assertIn('earlier: Discussions/ROS decision (2).md', self.read(third))   # the newest earlier one

    def test_named_earlier_must_be_a_decision_note_of_discussions(self):
        talk = discussions.save('one', 'ROS talk', 'plain talk')
        self.decide('Other point')
        with self.assertRaisesRegex(ValueError, 'not a decision'):
            self.decide('ROS decision', earlier=talk['saved'])
        for bad in ('Discussions/missing.md', '../Home.md', 'Home'):
            with self.assertRaisesRegex(ValueError, 'not a decision'):
                self.decide('ROS decision', earlier=bad)
        named = self.decide('ROS decision', earlier='Discussions/Other point.md')
        self.assertIn('earlier: Discussions/Other point.md', self.read(named))

    def test_a_same_title_discussion_is_not_taken_as_the_earlier_decision(self):
        discussions.save('one', 'ROS decision', 'plain talk')
        saved = self.decide('ROS decision')
        self.assertEqual(saved['saved'], 'Discussions/ROS decision (2).md')
        self.assertNotIn('earlier', self.read(saved))

    def test_front_matter_evidence_false_and_index_kind(self):
        saved = self.decide(citations=[self.sid])
        text = self.read(saved)
        front = neural._front(text)[0]
        self.assertEqual((front['type'], front['kb_kind'], front['evidence'], front['tags']), ('decision', 'decision', False, ['decision']))
        self.assertEqual((front['cited_items'], front['cited_papers']), ([self.sid], [self.pid]))
        self.assertRegex(front['created'], r'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$')
        neural.refresh('one')
        self.assertEqual(neural._note_metadata(text, saved['saved'])['entity_kind'], 'decision')
        self.assertEqual([n['path'] for n in neural.search('one', 'ROS', kind='decision')['nodes']], [saved['saved']])

    def test_citation_is_validated_and_the_quote_is_copied_by_code(self):
        saved = self.decide(citations=[self.sid])
        text = self.read(saved)
        self.assertIn(f"`{self.sid}`", text)
        self.assertIn('> ' + self.rows[0]['text'], text)
        self.assertIn(f'bài {self.pid}', text)
        self.assertIn('trang 1', text)
        self.assertIn('> Tôi chốt: ROS tăng sống sót.', text)   # the researcher's words, verbatim, as a blockquote
        self.assertNotIn(discussions.NO_EVIDENCE_LINE, text)
        before = sorted(p.name for p in (config.vault_dir('one') / 'Discussions').glob('*'))
        for bad in (self.pid + ':S9999', 'nonsense', ''.join(['..', '/x'])):
            with self.assertRaisesRegex(ValueError, 'not a sentence'):
                self.decide('Bad', citations=[bad])
        with self.assertRaisesRegex(ValueError, 'citations must be'):
            self.decide('Bad', citations=[1])
        with self.assertRaisesRegex(ValueError, 'store connection'):
            discussions.save('one', 'Bad', 'x', kind='decision', researcher_words=WORDS, citations=[self.sid])
        # a sentence of a paper that is not in this vault (or was removed from it) is refused
        with self.assertRaisesRegex(ValueError, 'not a sentence'):
            discussions.save('two', 'Bad', 'x', kind='decision', con=self.con, researcher_words=WORDS, citations=[self.sid])
        self.con.execute('DELETE FROM paper_vaults WHERE paper_id=?', (self.pid,))
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'not a sentence'):
            self.decide('Bad', citations=[self.sid])
        self.assertEqual(sorted(p.name for p in (config.vault_dir('one') / 'Discussions').glob('*')), before)   # nothing written

    def test_no_citation_writes_the_line_itself(self):
        text = self.read(self.decide())
        self.assertIn(discussions.NO_EVIDENCE_LINE, text)
        self.assertEqual(neural._front(text)[0]['cited_items'], [])

    def test_only_a_decision_takes_decision_arguments(self):
        for kw in ({'researcher_words': WORDS}, {'citations': [self.sid]}, {'earlier': 'x'}):
            with self.assertRaisesRegex(ValueError, 'decision only'):
                discussions.save('one', 'T', 'x', con=self.con, **kw)
        self.assertFalse((config.vault_dir('one') / 'Discussions').exists())

    def test_the_word_chot_is_required(self):
        for words in ('Chốt', 'CHỐT lại điểm này', 'chot', 'ok, chot nhé', unicodedata.normalize('NFD', 'chốt'),
                      unicodedata.normalize('NFD', 'CHỐT'), 'Tôi "chốt".'):
            self.assertTrue(self.decide('T', words)['saved'], words)
        for words in ('chốtx', 'xchốt', 'chotx', 'unchốt', 'chốt_', 'tôi đồng ý', '', None, 5):
            with self.assertRaisesRegex(ValueError, 'chốt'):
                self.decide('Refused', words)
        self.assertFalse(list((config.vault_dir('one') / 'Discussions').glob('Refused*')))
        with self.assertRaisesRegex(ValueError, 'chốt'):
            discussions.save('one', 'Refused', 'x', kind='decision', con=self.con)

    def test_mcp_save_decision_and_argument_types(self):
        call = lambda **a: mcp_server.call_tool(self.con, config.load(), 'save_discussion', {'vault': 'one', 'title': 'MCP', 'markdown': 'x', **a})
        schema = next(t for t in mcp_server.TOOLS if t['name'] == 'save_discussion')['inputSchema']['properties']
        self.assertIn('decision', schema['kind']['enum'])
        self.assertEqual(call(kind='decision', researcher_words=WORDS, citations=[self.sid])['kind'], 'decision')
        for bad in ({'researcher_words': 3}, {'citations': 'abc'}, {'citations': [1]}, {'earlier': 4}):
            with self.assertRaises(ValueError):
                mcp_server._check_args(self.con, 'save_discussion', {'vault': 'one', **bad})

    # --- surfacing ----------------------------------------------------------------------------------------------

    def test_prepare_surfaces_a_decision_saved_a_moment_ago_without_a_manual_refresh(self):
        self.prepare()   # builds the index first
        saved = self.decide(citations=[self.sid])
        out = self.prepare()
        [d] = out['decisions']
        today = neural._front(self.read(saved))[0]['created'][:10]
        self.assertEqual((d['label'], d['date'], d['note_path'], d['item_type'], d['evidence']),
                         (f'ý chốt của bạn ({today})', today, saved['saved'], 'decision', False))
        self.assertEqual((d['cited_items'], d['earlier'], d['newer_evidence']), ([self.sid], None, []))
        self.assertTrue(d['obsidian_url'].startswith('obsidian://open?vault=one&file='))
        self.assertIn('Tôi chốt', d['text'])
        self.assertIn('Researcher decisions', out['prompt'])
        self.assertTrue(all(i['item_type'] != 'decision' and i['id'] != d['note_path'] for i in out['items']))   # never an item
        self.assertEqual([i['n'] for i in out['items'] if 'n' in i], [])

    def test_no_decision_no_prompt_line(self):
        out = self.prepare()
        self.assertNotIn('decisions', out)
        self.assertNotIn('decisions', self.ask())
        self.assertNotIn('Researcher decisions', out['prompt'])

    def test_decisions_are_counted_by_the_byte_cap(self):
        self.decide('ROS decision a', citations=[self.sid])
        full = self.prepare(count=6)
        self.assertTrue(full['decisions'] and len(full['items']) > 1)
        size = len(neural.json.dumps(full, ensure_ascii=False, default=str).encode())
        real = neural.standard.param
        cap = lambda clause, name: (size - 1) / 1024 if (clause, name) == ('NW-01', 'max_payload_kb') else real(clause, name)
        with patch.object(neural.standard, 'param', side_effect=cap):
            out = self.prepare(count=6)
        self.assertEqual(len(out['items']), len(full['items']) - 1)   # one byte short: the decision's bytes were part of the count
        self.assertEqual(out['decisions'], full['decisions'])

    def test_newer_evidence_is_flagged_once_across_two_prepares_and_ask_prepare(self):
        saved = self.decide(citations=[self.sid])
        created = neural._front(self.read(saved))[0]['created']
        newer = self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        first = self.prepare(pids=[self.pid, 'later2026_second_abcd'])
        self.assertEqual(first['decisions'][0]['newer_evidence'], ['later2026_second_abcd'])
        self.assertEqual(self.flags(), {'one/' + saved['saved']: ['later2026_second_abcd']})
        second = self.prepare(pids=[self.pid, 'later2026_second_abcd'])
        self.assertEqual(second['decisions'][0]['newer_evidence'], [])
        self.assertEqual(self.ask()['decisions'][0]['newer_evidence'], [])   # AN-19 covers ask_prepare too
        self.assertEqual(self.flags(), {'one/' + saved['saved']: ['later2026_second_abcd']})
        self.assertLess(created, '2999')
        self.assertTrue(newer)

    def test_ask_prepare_flags_first_and_prepare_stays_quiet_after(self):
        saved = self.decide(citations=[self.sid])
        self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        ask = self.ask()
        self.assertEqual(ask['decisions'][0]['newer_evidence'], ['later2026_second_abcd'])
        self.assertEqual(ask['decisions'][0]['note_path'], saved['saved'])
        self.assertTrue(all(link['item_type'] != 'decision' for link in ask['links']))
        self.assertEqual(self.prepare(pids=[self.pid, 'later2026_second_abcd'])['decisions'][0]['newer_evidence'], [])

    def test_a_cited_paper_and_an_older_paper_are_never_flagged(self):
        second = self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        self.add_paper('older2026_third_abcd', '2000-01-01T00:00:00')
        self.con.execute('UPDATE papers SET ingested_at=? WHERE paper_id=?', ('2999-01-01T00:00:00', self.pid))
        self.con.commit()
        self.decide(citations=[self.sid, second])   # cites both newer papers
        out = self.prepare(pids=[self.pid, 'later2026_second_abcd', 'older2026_third_abcd'])
        self.assertEqual(out['decisions'][0]['newer_evidence'], [])
        self.assertFalse((config.CACHE_DIR / 'decision_flags.json').exists())
        self.decide('ROS decision b')   # cites nothing: the cited-paper exemption is gone, the older paper stays out
        out = self.prepare(pids=[self.pid, 'later2026_second_abcd', 'older2026_third_abcd'], question='ROS decision b')
        flagged = {d['note_path']: d['newer_evidence'] for d in out['decisions']}
        self.assertEqual(flagged['Discussions/ROS decision b.md'], sorted(['later2026_second_abcd', self.pid]))

    def test_a_paper_without_ingest_date_is_not_flagged(self):
        self.decide(citations=[self.sid])
        self.add_paper('later2026_second_abcd', None)
        self.assertEqual(self.prepare(pids=[self.pid, 'later2026_second_abcd'])['decisions'][0]['newer_evidence'], [])

    def test_a_refused_session_round_writes_no_cache(self):
        self.decide(citations=[self.sid])
        self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        pids = [self.pid, 'later2026_second_abcd']
        sid = self.prepare(pids, session='new')['session']
        cache = config.CACHE_DIR / 'decision_flags.json'
        cache.unlink()
        before = neural.load_session(sid)
        self.prepare(pids, 'ROS survival', session=sid)
        cache.unlink()
        with patch.object(neural, 'load_session', return_value=before), self.assertRaisesRegex(ValueError, 'meanwhile'):
            self.prepare(pids, 'third question ROS', session=sid)
        self.assertFalse(cache.exists())

    def test_a_damaged_cache_is_forgotten_not_fatal(self):
        saved = self.decide(citations=[self.sid])
        self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        (config.CACHE_DIR).mkdir(parents=True, exist_ok=True)
        (config.CACHE_DIR / 'decision_flags.json').write_text('{not json', encoding='utf-8')
        out = self.prepare(pids=[self.pid, 'later2026_second_abcd'])
        self.assertEqual(out['decisions'][0]['newer_evidence'], ['later2026_second_abcd'])
        self.assertEqual(self.flags(), {'one/' + saved['saved']: ['later2026_second_abcd']})

    def test_a_note_that_is_not_a_discussions_decision_is_not_surfaced(self):
        (config.vault_dir('one') / 'Fake.md').write_text('---\ntype: decision\nkb_kind: decision\ncreated: 2000-01-01T00:00:00\n---\nROS fake\n', encoding='utf-8')
        self.assertNotIn('decisions', self.prepare())

    # --- review regressions (2026-10-25) -------------------------------------------------------------------------

    def test_only_the_mcp_answer_tools_consume_the_flag(self):
        # web /prepare, the web AI answer, kb.py discussion pack, the benchmark and conform call these directly and never
        # show `decisions`: they must not mark the newer evidence as reported
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.decide(citations=[self.sid])
        self.add_paper('later2026_second_abcd', '2999-01-01T00:00:00')
        pids = [self.pid, 'later2026_second_abcd']
        cache = config.CACHE_DIR / 'decision_flags.json'
        self.assertEqual(self.prepare(pids, direct=True)['decisions'][0]['newer_evidence'], ['later2026_second_abcd'])
        agent_tasks.answer_prompt(self.con, config.load(), 'ROS', ['ROS'], 'evidence4', 'one', 'fast', 'test')
        hits = [{'paper_id': pid, 'sentence_ids': [r['sentence_id'] for r in self.all_rows(pid)][:2], 'matched_sentences': None} for pid in pids]
        with patch('kbsys.find.search', return_value={'hits': hits}), \
                patch.object(answer, 'provider_status', return_value={'ready': False, 'message': 'no provider'}):
            neural.answer_question(self.con, config.load(), 'one', 'ROS')
        self.assertFalse(cache.exists())
        self.assertEqual(self.prepare(pids)['decisions'][0]['newer_evidence'], ['later2026_second_abcd'])
        self.assertTrue(cache.exists())

    def test_an_unquoted_created_written_by_obsidian_still_compares_as_a_time(self):
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        path = config.vault_dir('one') / self.decide(citations=[self.sid])['saved']
        text = path.read_text(encoding='utf-8')
        created = neural._front(text)[0]['created']
        path.write_text(text.replace(f"created: '{created}'", 'created: 2026-10-25T12:00:00'), encoding='utf-8')
        self.add_paper('early2026_second_abcd', '2026-10-25T05:00:00')   # same day, before the decision
        self.add_paper('late2026_third_abcd', '2026-10-25T13:00:00')
        pids = [self.pid, 'early2026_second_abcd', 'late2026_third_abcd']
        [d] = self.prepare(pids, direct=True)['decisions']
        self.assertEqual((d['date'], d['newer_evidence']), ('2026-10-25', ['late2026_third_abcd']))
        path.write_text(text.replace(f"created: '{created}'", 'created: 2026-10-25'), encoding='utf-8')   # a date only = end of day
        self.assertEqual(self.prepare(pids, direct=True)['decisions'][0]['newer_evidence'], [])

    def test_a_bom_or_non_utf8_decision_note_does_not_break_the_answer(self):
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        saved = self.decide(citations=[self.sid])
        path = config.vault_dir('one') / saved['saved']
        path.write_bytes(b'\xef\xbb\xbf' + path.read_bytes() + b'\nedited in a cp1252 editor: caf\xe9\n')
        self.assertEqual([d['note_path'] for d in self.prepare()['decisions']], [saved['saved']])
        self.assertEqual([d['note_path'] for d in self.ask()['decisions']], [saved['saved']])
        self.assertTrue(self.decide()['saved'])   # the auto-link reads it too

    def test_earlier_in_another_folder_is_refused_and_case_resolves_to_the_real_name(self):
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.decide('Other point')
        for bad in ('Mine/Other point.md', 'Discussions/../Other point.md', 'Discussions/sub/Other point.md'):
            with self.assertRaisesRegex(ValueError, 'not a decision'):
                self.decide('ROS decision', earlier=bad)
        text = self.read(self.decide('ROS decision', earlier='discussions\\OTHER point'))
        self.assertIn('earlier: Discussions/Other point.md', text)
        self.assertIn('[[Discussions/Other point]]', text)

    def test_an_nfd_title_links_the_same_nfc_title(self):
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        first = self.decide(unicodedata.normalize('NFD', 'Chốt ROS'))
        second = self.decide('Chốt ROS')
        self.assertEqual((first['saved'], second['saved']), ('Discussions/Chốt ROS.md', 'Discussions/Chốt ROS (2).md'))
        self.assertIn('earlier: Discussions/Chốt ROS.md', self.read(second))


if __name__ == '__main__':
    unittest.main()
