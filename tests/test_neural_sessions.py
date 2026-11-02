"""U6a / NW-11, NW-07: research sessions of neural_prepare (one server file, session-wide numbers, round fingerprints,
union check, list/open/close) and the age cleaning of plain tickets."""
import json
import os
import unicodedata
import unittest
from unittest.mock import patch

from . import test_core as fixtures
from kbsys import agent_tasks, config, db, mcp_server, neural


class SessionTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def ids(self, *positions):
        return [self.rows[i]['sentence_id'] for i in positions]

    def ask(self, positions, question='ROS', **kwargs):
        (config.vault_dir('one') / 'ROS.md').write_text('ROS note')
        google = {'hits': [{'paper_id': self.pid, 'sentence_ids': self.ids(*positions), 'matched_sentences': None}]}
        with patch('kbsys.find.search', return_value=google):
            return neural.prepare(self.con, config.load(), 'one', question, **kwargs)

    def reply(self, *pairs):
        return {'answer': [{'text': self.rows[pos]['text'] + f' [#{n}]', 'evidence': [n]} for pos, n in pairs],
                'reasoning': [], 'missing': []}

    def session_file(self, sid):
        return config.CACHE_DIR / 'neural_sessions' / f'session-{sid}.json'

    def three_rounds(self):
        first = self.ask([0, 1], session='new')
        sid = first['session']
        second = self.ask([1, 2, 3], 'ROS survival', session=sid)
        third = self.ask([4, 0], 'third question', session=sid)
        return sid, first, second, third

    def test_three_rounds_keep_numbers_and_a_round1_item_checks_in_round3(self):
        sid, first, second, third = self.three_rounds()
        self.assertEqual(first['ticket'], sid)
        self.assertEqual([(r['round'], [i['n'] for i in r['items']], r['new_items']) for r in (first, second, third)],
                         [(1, [1, 2], [1, 2]), (2, [2, 3, 4], [3, 4]), (3, [5, 1], [5])])
        self.assertIn('[#5]', third['prompt'])
        self.assertIn('[#1]', third['prompt'])
        self.assertNotIn('[#2]', third['prompt'])
        self.assertIn('numbers of this research session', third['prompt'])
        state = db.read_json(self.session_file(sid))
        self.assertEqual([i['id'] for i in state['union']], self.ids(0, 1, 2, 3, 4))   # deduped, first-seen order
        self.assertEqual([r['item_numbers'] for r in state['rounds']], [[1, 2], [2, 3, 4], [5, 1]])
        checked = neural.check(self.con, sid, self.reply((0, 1), (3, 4)))
        self.assertEqual([row['check'] for row in checked['answer']], ['ok', 'ok'])
        self.assertEqual([e['id'] for e in checked['evidence']], self.ids(0, 3))
        self.assertTrue(self.session_file(sid).with_name(f'session-{sid}.checked.json').is_file())

    def test_same_question_continues_new_question_starts_at_zero_explicit_offset_wins(self):
        real = neural.standard.param
        tight = lambda clause, name: 330 if (clause, name) == ('NW-01', 'token_budget_fast') else real(clause, name)
        with patch.object(neural.standard, 'param', side_effect=tight):
            first = self.ask(range(6), session='new')
            sid = first['session']
            self.assertTrue(first['has_more'])
            again = self.ask(range(6), '  ros ', session=sid)
            self.assertEqual(again['offset'], first['next_offset'])
            self.assertFalse({i['id'] for i in first['items']} & {i['id'] for i in again['items']})
            other = self.ask(range(6), 'another question', session=sid)
            self.assertEqual(other['offset'], 0)
            self.assertEqual(other['new_items'], [])   # the same items keep their first numbers
            self.assertEqual([i['n'] for i in other['items']], list(range(1, len(other['items']) + 1)))
            explicit = self.ask(range(6), 'ROS', session=sid, offset=1)
            vi = self.ask(range(6), 'tế bào ROS', session='new')
            nfd = self.ask(range(6), unicodedata.normalize('NFD', 'TẾ BÀO ROS'), session=vi['session'])   # CHR-01
        self.assertEqual(explicit['offset'], 1)
        self.assertEqual(nfd['offset'], vi['next_offset'])

    def test_an_edited_context_note_refuses_only_a_reply_citing_it(self):
        sid, *_ = self.three_rounds()
        self.assertEqual([n['path'] for n in db.read_json(self.session_file(sid))['vault_context']['nodes']], ['ROS.md'])
        (config.vault_dir('one') / 'ROS.md').write_text('ROS note edited by the researcher')
        self.assertEqual(neural.check(self.con, sid, self.reply((0, 1)))['answer'][0]['check'], 'ok')
        citing = dict(self.reply((0, 1)), reasoning=[{'text': 'The note frames ROS.', 'basis': ['ROS.md']}])
        with self.assertRaisesRegex(ValueError, 'Vault note ROS.md changed'):
            neural.check(self.con, sid, citing)
        google = {'hits': [{'paper_id': self.pid, 'sentence_ids': self.ids(0), 'matched_sentences': None}]}
        with patch('kbsys.find.search', return_value=google):   # a round that retrieves the edited note again
            neural.prepare(self.con, config.load(), 'one', 'ROS', session=sid)
        edited = neural.read_note('one', 'ROS.md')['sha256']
        self.assertEqual([n['sha256'] for n in db.read_json(self.session_file(sid))['vault_context']['nodes']], [edited])
        self.assertNotIn('struck', str(neural.check(self.con, sid, citing)['reasoning'][0]['check']))

    def test_a_round_added_meanwhile_is_refused_not_overwritten(self):
        sid = self.ask([0], session='new')['session']
        before = neural.load_session(sid)
        self.ask([1], 'second question', session=sid)
        with patch.object(neural, 'load_session', return_value=before), self.assertRaisesRegex(ValueError, 'meanwhile'):
            self.ask([2], 'third question', session=sid)
        self.assertEqual(len(db.read_json(self.session_file(sid))['rounds']), 2)
        loaded = neural.load_session(sid)
        neural.close_session(sid)
        with patch.object(neural, 'load_session', return_value=loaded), self.assertRaisesRegex(ValueError, 'closed'):
            self.ask([2], 'third question', session=sid)
        self.assertFalse(self.session_file(sid).exists())   # a closed session is not written back

    def test_an_altered_round_is_refused(self):
        sid, *_ = self.three_rounds()
        path = self.session_file(sid)
        good = path.read_text(encoding='utf-8')
        for label, tamper in (('item text', lambda s: s['union'][0].update(text='Tampered ROS sentence.')),
                              ('round numbers', lambda s: s['rounds'][1]['item_numbers'].reverse()),
                              ('extra item', lambda s: s['union'].append(dict(s['union'][0], id='x:S1'))),
                              ('page', lambda s: s['union'][0].update(pages=[99])),   # review 2026-10-24: not hashed before
                              ('hedge marker', lambda s: s['union'][0].update(uncertainty='tampered'))):
            with self.subTest(label):
                state = json.loads(good)
                tamper(state)
                path.write_text(json.dumps(state), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'open a new session'):
                    neural.check(self.con, sid, self.reply((0, 1)))
                with self.assertRaisesRegex(ValueError, 'open a new session'):
                    self.ask([0], session=sid)

    def test_an_item_changed_in_the_store_is_refused_by_the_next_round_and_the_check(self):
        sid = self.ask([0], session='new')['session']
        self.con.execute("UPDATE sentences SET text='ROS changed.' WHERE sentence_id=?", (self.ids(0)[0],))
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'changed; open a new session'):   # a new round cannot repair it
            neural.check(self.con, sid, self.reply((0, 1)))
        with self.assertRaisesRegex(ValueError, r'session item \[#1\] changed'):
            self.ask([0, 1], 'another question', session=sid)
        self.assertEqual(len(db.read_json(self.session_file(sid))['rounds']), 1)   # the refused round is not added
        self.con.execute("DELETE FROM sentences WHERE sentence_id=?", (self.ids(0)[0],))   # re-extracted or purged
        self.con.commit()
        with self.assertRaisesRegex(ValueError, 'Evidence changed; open a new session'):
            neural.check(self.con, sid, self.reply((0, 1)))

    def test_open_session_survives_old_mtime_and_restart_and_is_listed(self):
        sid, *_ = self.three_rounds()
        path = self.session_file(sid)
        old_ticket = path.with_name('e' * 32 + '.json')
        old_ticket.write_text('{}')
        for p in (path, old_ticket):
            os.utime(p, (0, 0))
        self.ask([0])   # a plain prepare cleans by age
        self.assertFalse(old_ticket.exists())
        self.assertTrue(path.is_file())
        agent_tasks._PREPARED.clear()   # "restart": nothing but the file is left
        con = db.connect()
        try:
            self.assertEqual(neural.check(con, sid, self.reply((0, 1)))['answer'][0]['check'], 'ok')
        finally:
            con.close()
        listed = neural.list_sessions('one')
        self.assertEqual([(r['session'], r['topic'], r['rounds'], r['item_count']) for r in listed], [(sid, 'ROS', 3, 5)])
        self.assertEqual(listed[0]['questions'], ['ROS', 'ROS survival', 'third question'])
        self.assertEqual(neural.list_sessions('two'), [])

    def test_closed_session_is_removed_and_then_refused(self):
        sid, *_ = self.three_rounds()
        neural.check(self.con, sid, self.reply((0, 1)))
        self.assertEqual(neural.close_session(sid), {'session': sid, 'closed': True})
        self.assertEqual(list((config.CACHE_DIR / 'neural_sessions').glob(f'session-{sid}*')), [])
        for call in (lambda: neural.check(self.con, sid, self.reply((0, 1))), lambda: self.ask([0], session=sid),
                     lambda: neural.open_session(sid), lambda: neural.close_session(sid)):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual(neural.list_sessions('one'), [])

    def test_new_topic_new_session_and_bad_sessions_are_errors(self):
        first = self.ask([0], session='new')['session']
        second = self.ask([0], 'new topic', session='new')['session']
        self.assertNotEqual(first, second)
        self.assertEqual({r['session'] for r in neural.list_sessions('one')}, {first, second})
        for session, vault in (('0' * 32, 'one'), ('../x', 'one'), (first, 'two')):
            with self.subTest(session=session, vault=vault), self.assertRaises(ValueError):
                with patch('kbsys.find.search', return_value={'hits': []}):
                    neural.prepare(self.con, config.load(), vault, 'ROS', session=session)

    def test_open_session_index_of_500_items_stays_within_token_budget_fast(self):
        sid = 'b' * 32
        union = [{'id': f'p:S{n:04d}', 'paper_id': 'p', 'pages': [n], 'text': f'Sentence {n} ' + 'é long text ' * 30, 'n': n}
                 for n in range(1, 501)]
        state = {'session': sid, 'vault': 'one', 'status': 'open', 'created': 'c', 'last_used': 'l', 'union': union,
                 'source_fingerprints': {i['id']: 'fp' for i in union}, 'papers': {'p': {'cite': 'P 2026'}}}
        state['rounds'] = [{'round': 1, 'question': 'q', 'item_numbers': list(range(1, 501)),
                            'fingerprint': neural._round_sha(state, list(range(1, 501)))}]
        db.write_json(self.session_file(sid), state)
        budget = neural.standard.param('NW-01', 'token_budget_fast')
        seen, offset = [], 0
        while offset is not None:
            page = neural.open_session(sid, offset=offset)
            self.assertLessEqual(neural.est_tokens(len(json.dumps(page, ensure_ascii=False).encode())), budget)
            self.assertTrue(page['index'] and all(len(e['line']) <= 100 for e in page['index']))
            seen += [e['n'] for e in page['index']]
            offset = page['next_offset']
        self.assertEqual(seen, list(range(1, 501)))
        full = neural.open_session(sid, numbers=[250, 1])
        self.assertEqual([i['n'] for i in full['items']], [250, 1])
        self.assertEqual(full['items'][0]['text'], union[249]['text'])
        with self.assertRaises(ValueError):
            neural.open_session(sid, numbers=[501])

    def test_ask_check_with_a_session_checks_against_the_union(self):
        sid, *_ = self.three_rounds()
        agent_tasks._PREPARED.clear()
        plain_keys = {'answer', 'errors', 'overview', 'scope_note', 'items_digest', 'markdown', 'not_found', 'not_found_message'}
        out = agent_tasks.answer_check(self.con, config.load(), 'q', ['q'], json.dumps(self.reply((0, 1), (4, 5))),
                                       vault='one', session=sid)
        self.assertEqual(set(out), plain_keys)
        self.assertEqual([row['check'] for row in out['answer']['answer']], ['ok', 'ok'])
        self.assertEqual(out['items_digest'], agent_tasks._items_digest(db.read_json(self.session_file(sid))['union']))
        self.assertEqual(agent_tasks.answer_check(self.con, config.load(), 'q', ['q'], self.reply((0, 1)), vault='',
                                                  session=sid)['answer']['answer'][0]['check'], 'ok')   # "" = no vault given
        with self.assertRaisesRegex(ValueError, 'items_digest'):
            agent_tasks.answer_check(self.con, config.load(), 'q', ['q'], self.reply((0, 1)), session=sid, items_digest='x')
        with self.assertRaisesRegex(ValueError, 'another vault'):
            agent_tasks.answer_check(self.con, config.load(), 'q', ['q'], self.reply((0, 1)), vault='two', session=sid)

    def test_session_none_payload_is_unchanged(self):
        plain = self.ask(range(6))
        self.assertEqual(set(plain), {'english_query', 'has_more', 'items', 'limit', 'media', 'needs_translation', 'next_offset',
                                      'note_refs', 'offset', 'papers', 'prompt', 'question', 'retrieval_basis', 'rules',
                                      'source_resolution', 'speed', 'ticket', 'total', 'vault', 'vault_context'})
        self.assertTrue(all('n' not in item for item in plain['items']))
        self.assertIn(f"between 1 and {len(plain['items'])}", plain['prompt'])
        self.assertTrue((config.CACHE_DIR / 'neural_sessions' / (plain['ticket'] + '.json')).is_file())
        opened = self.ask(range(6), session='new')
        self.assertEqual([{k: v for k, v in i.items() if k != 'n'} for i in opened['items']], plain['items'])
        self.assertEqual(set(opened) - set(plain), {'session', 'round', 'new_items', 'slots', 'coverage', 'stop_reason', 'prompt_tokens'})

    def test_mcp_session_tools_and_argument_checks(self):
        cfg = config.load()
        sid = self.ask([0], session='new')['session']
        call = lambda name, args: mcp_server.call_tool(self.con, cfg, name, args)
        self.assertEqual(call('list_sessions', {'vault': 'one'})[0]['session'], sid)
        self.assertEqual(call('open_session', {'session': sid})['index'][0]['n'], 1)
        self.assertEqual(call('neural_check', {'session': sid, 'reply': self.reply((0, 1))})['answer'][0]['check'], 'ok')
        for name, args in (('neural_check', {'session': 'new'}), ('open_session', {'session': 'X' * 32}),
                           ('open_session', {'session': sid, 'numbers': '1'}), ('open_session', {'session': sid, 'offset': -1}),
                           ('neural_prepare', {'session': 7})):
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                mcp_server._check_args(self.con, name, dict(args))
        mcp_server._check_args(self.con, 'neural_prepare', {'session': 'new'})
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
                   'params': {'name': 'close_session', 'arguments': {'session': sid}}}
        self.assertFalse(mcp_server.handle(self.con, cfg, message)['result']['isError'])
        self.assertFalse(self.session_file(sid).exists())


class SessionCoverageTests(unittest.TestCase):
    """U6b / NW-10: asked WH slots, coverage over the session union from stored fields, stop reasons."""
    setUp, tearDown = fixtures.Demo3Tests.setUp, fixtures.Demo3Tests.tearDown
    ids, ask, session_file, reply = SessionTests.ids, SessionTests.ask, SessionTests.session_file, SessionTests.reply

    def params(self, **values):   # NW-10 params patched only here, never in config
        real = neural.standard.param
        return patch.object(neural.standard, 'param',
                            side_effect=lambda c, n: values[n] if c == 'NW-10' and n in values else real(c, n))

    def units(self, **by_position):   # p<position>=(unit_type, species): accepted units, straight into the temp store
        for key, (unit_type, species) in by_position.items():
            pos = int(key[1:])
            row = self.rows[pos]
            self.con.execute('INSERT INTO evidence VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                             (f'{self.pid}:u{pos}', self.pid, pos + 1, unit_type, 'ok', row['text'], row['text'],
                              json.dumps([row['sentence_id']]), '[1]', json.dumps([row['section']]), species, None,
                              None, None, None, None, 'none', 'test'))
        self.con.commit()

    def rounds(self, question, *positions):
        first = self.ask(positions[0], question, session='new')
        return [first] + [self.ask(p, question, session=first['session'], offset=0) for p in positions[1:]]

    def test_question_slot_traps(self):
        cases = {'Khi nào phôi bị tổn thương?': ['when'], 'Phôi thay đổi như thế nào?': ['how'],
                 'Tế bào đến nơi nào?': ['where'], 'Loại nào tốt hơn?': ['which'],
                 'Tại sao kết quả sai?': ['why'], 'Loại tế bào này có hai dạng gì?': ['what'],
                 'Kết quả sai ở hai nhóm?': ['what'], 'Tại sao vậy?': ['why'],
                 'Mô hình AI dự đoán gì?': ['what'], 'AI model of ROS': ['what'], 'ai đã chứng minh điều này?': ['who'],
                 'Who showed it?': ['who'], 'ai model': ['what'],   # no Vietnamese diacritics: not Vietnamese "ai"
                 'Khoảng cách giữa hai nhóm là gì?': ['what'], 'Khoảng cách giữa hai nhóm': ['what'],
                 'Làm bằng cách nào?': ['how'], 'Cách làm ra sao?': ['how'],
                 'Tế bào nằm ở đâu?': ['where'], 'Nguồn gốc từ đâu?': ['where'],
                 'Is there evidence that ROS rises?': ['what'], 'How many pups were born?': ['what'],
                 'Why does ROS rise, and how?': ['why', 'how'],
                 'Ai đã chứng minh điều này?': ['who'], 'AI đã dự đoán gì?': ['what'],   # sentence-initial Vietnamese 'Ai'
                 'Người nào phát hiện?': ['who'], 'Loài gì được dùng?': ['who'], 'What cell types were used?': ['who'],
                 'Ở khoảng cách nào thì tốt?': ['which'],
                 unicodedata.normalize('NFD', 'Khi nào?'): ['when']}
        for question, slots in cases.items():
            with self.subTest(question=question):
                self.assertEqual(neural.question_slots(question), slots)

    def test_brainstorm_asks_all_seven_slots(self):
        every = neural.standard.param('NW-10', 'brainstorm_slots')
        self.assertEqual(len(every), 7)
        for question in ('Brainstorm ROS', 'ROS: hãy động não', 'what about ROS, brainstorm'):
            with self.subTest(question=question):
                self.assertEqual(neural.question_slots(question), every)

    def test_coverage_fills_from_stored_fields_and_marks_missing(self):
        self.units(p4=('interpretation', None), p5=('conclusion', None), p2=('method_detail', 'mouse'))
        a, b = self.rows[2:4]
        protocol = {'protocols': [{'name': 'example protocol', 'sentence_ids': [a['sentence_id']],
                                   'details': [{'field': 'dose', 'value': '5 mg', 'sentence_ids': [a['sentence_id']]},
                                               {'field': 'duration', 'value': '3 hours', 'sentence_ids': [b['sentence_id']]}],
                                   'steps': [{'sentence_ids': [a['sentence_id']]}, {'sentence_ids': [b['sentence_id']]}]}]}
        protocol.update({k: neural.structure.prepare_protocol(self.pid)[k] for k in ('source_sha256', 'revision_of')})
        self.assertTrue(neural.structure.submit_protocol(self.con, self.pid, protocol, 'test')['ok'])
        wh = {'answers': {k: {'text': None, 'reason': 'Not supplied.'} for k in neural.structure.WH}}
        wh['answers']['where'] = {'text': self.rows[0]['text'], 'sentence_ids': [self.rows[0]['sentence_id']]}
        wh.update({k: neural.structure.prepare(self.pid)[k] for k in ('source_sha256', 'revision_of')})
        self.assertTrue(neural.structure.submit_profile(self.con, self.pid, wh, 'test')['ok'])
        first = self.ask([0, 1, 2], 'brainstorm ROS', session='new')
        n = {item['id']: item['n'] for item in first['items']}
        self.assertEqual(first['slots'], neural.standard.param('NW-10', 'brainstorm_slots'))
        self.assertEqual(first['coverage']['where'], [n[self.ids(0)[0]]])   # profile.origin/setting = WH answer 'where'
        self.assertEqual(first['coverage']['how'], [n[self.ids(2)[0]]])     # method_detail unit + protocol dose
        self.assertEqual(first['coverage']['who'], [n[self.ids(2)[0]]])     # the unit's model.species
        for slot in ('what', 'why', 'which', 'when'):   # sentence 1 has no stored field; groups is not a stored field
            self.assertEqual(first['coverage'][slot], 'missing')
        self.assertIs(first['coverage']['heuristic'], True)
        self.assertIsNone(first['stop_reason'])
        second = self.ask([3, 4, 5], 'brainstorm ROS', session=first['session'], offset=0)   # coverage over the session union
        self.assertEqual(second['coverage']['what'], [6])
        self.assertEqual(second['coverage']['why'], [5])
        self.assertEqual(second['coverage']['when'], [4])                          # protocol.duration
        self.assertEqual(second['coverage']['how'], [3, 4])
        self.assertEqual(second['coverage']['which'], 'missing')
        state = db.read_json(self.session_file(first['session']))
        self.assertEqual([(r['coverage'], r['stop_reason']) for r in state['rounds']],
                         [(first['coverage'], None), (second['coverage'], None)])
        self.assertIsNone(neural.list_sessions('one')[0]['stop_reason'])
        self.assertIsNone(neural.open_session(first['session'])['stop_reason'])
        checked = neural.check(self.con, first['session'], self.reply((3, 4)))   # NW-10: code names the missing slot in Part 4
        self.assertEqual([m for m in checked['missing'] if m.startswith('WH ')],
                         ["WH 'which': chưa có mục nào của phiên được code gắn cho câu hỏi này (ước lượng bằng code, NW-10)."])
        self.assertIn("- WH 'which'", checked['markdown'].split('## Không tìm thấy / không chắc chắn')[1])

    def test_session_stops_all_slots_filled_and_may_continue(self):
        self.units(p4=('interpretation', None), p5=('conclusion', None))
        out = self.rounds('What and why?', [5], [0], [4])
        self.assertEqual([r['stop_reason'] for r in out], [None, None, 'all_slots_filled'])
        self.assertEqual(out[2]['coverage'], {'what': [1], 'why': [3], 'heuristic': True})
        self.assertEqual(neural.list_sessions('one')[0]['stop_reason'], 'all_slots_filled')
        self.assertEqual(self.ask([1], 'What and why?', session=out[0]['session'], offset=0)['round'], 4)   # the client decides

    def test_session_stops_no_gain_and_may_continue(self):
        self.units(p5=('conclusion', None))
        out = self.rounds('What and why?', [5], [0], [1])
        self.assertEqual([r['stop_reason'] for r in out], [None, None, 'no_gain'])
        self.assertEqual(out[2]['coverage']['why'], 'missing')
        self.assertEqual(self.ask([2], 'What and why?', session=out[0]['session'], offset=0)['round'], 4)

    def test_session_stops_at_max_rounds_and_refuses_a_further_round(self):
        with self.params(max_rounds=3):
            out = self.rounds('Why?', [0], [1], [2])
            self.assertEqual([r['stop_reason'] for r in out], [None, 'no_gain', 'max_rounds'])
            old_ticket = self.session_file(out[0]['session']).with_name('e' * 32 + '.json')
            old_ticket.write_text('{}')
            os.utime(old_ticket, (0, 0))
            with self.assertRaisesRegex(ValueError, f"session {out[0]['session']} reached max_rounds; answer from it or "
                                                    'close it and open a new session'):
                self.ask([3], 'Why?', session=out[0]['session'], offset=0)
        self.assertEqual(neural.open_session(out[0]['session'])['stop_reason'], 'max_rounds')
        self.assertEqual(len(db.read_json(self.session_file(out[0]['session']))['rounds']), 3)
        self.assertTrue(old_ticket.exists())   # the refused round wrote nothing, not even the NW-07 cleaning

    def test_session_stops_at_token_budget_and_refuses_a_further_round(self):
        self.units(p5=('conclusion', None))
        first = self.ask([5], 'What?', session='new')   # all_slots_filled from round 1, more rounds allowed
        sid = first['session']
        second = self.ask([0], 'What?', session=sid, offset=0)
        rounds = db.read_json(self.session_file(sid))['rounds']
        self.assertEqual([r['prompt_tokens'] for r in rounds], [first['prompt_tokens'], second['prompt_tokens']])
        used = sum(r['prompt_tokens'] for r in rounds)   # the prompts already given, not the item JSON
        self.assertGreater(used, 0)
        with self.params(session_token_budget=used + 1):
            third = self.ask([1], 'What?', session=sid, offset=0)
            with self.assertRaisesRegex(ValueError, 'reached token_budget'):
                self.ask([2], 'What?', session=sid, offset=0)
        self.assertEqual([r['stop_reason'] for r in (first, second, third)], ['all_slots_filled'] * 2 + ['token_budget'])


if __name__ == '__main__':
    unittest.main()
