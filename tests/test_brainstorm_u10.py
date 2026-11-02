"""U10a / NW-11, AN-20: index view of a round and of ask_prepare, fetch by number, opened tracking and the A12 warning,
the brainstorm toolset, the memory (remember / get_answer_rules), the `kb.py tool` bridge and the MCP setup flags."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from . import test_core as fixtures
from . import test_neural_sessions as sessions
import kb
from kbsys import agent_tasks, answer, config, db, discussions, mcp_server, mcp_setup, neural


class BrainstormBase(unittest.TestCase):
    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        self.assertTrue(Path(config.STORE).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))   # never the real store
        for target, value in ((answer, ('ANSWER_CACHE', self.root / 'answers')), (discussions, ('MEMORY_DIR', self.root / 'brainstorm'))):
            patcher = patch.object(target, *value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for name in ('CLIENT', 'TOOLSET', 'MEMORY'):   # `kb.py tool` and the tests set these module variables
            patcher = patch.object(mcp_server, name, dict(mcp_server.CLIENT) if name == 'CLIENT' else getattr(mcp_server, name))
            patcher.start()
            self.addCleanup(patcher.stop)

    tearDown = fixtures.Demo3Tests.tearDown
    ids, ask, session_file, reply = (sessions.SessionTests.ids, sessions.SessionTests.ask,
                                     sessions.SessionTests.session_file, sessions.SessionTests.reply)

    def call(self, name, **args):
        return mcp_server.call_tool(self.con, config.load(), name, args)


class IndexTests(BrainstormBase):
    def test_index_round_is_the_same_round_in_one_line_entries(self):
        full = self.ask(range(6), session='new')
        out = self.ask(range(6), session='new', index=True)
        self.assertEqual({k: out[k] for k in ('coverage', 'stop_reason', 'new_items', 'slots', 'total', 'offset', 'round', 'question', 'vault')},
                         {k: full[k] for k in ('coverage', 'stop_reason', 'new_items', 'slots', 'total', 'offset', 'round', 'question', 'vault')})
        self.assertEqual([e['n'] for e in out['index']], [i['n'] for i in full['items']])
        self.assertEqual(out['not_indexed'], [])
        for key in ('items', 'papers', 'media', 'prompt', 'vault_context', 'source_resolution'):
            self.assertNotIn(key, out)
        self.assertEqual({e['paper_id'] for e in out['index']}, set(out['links']))
        self.assertTrue(all(len(e['line']) <= 100 and set(e) == {'n', 'paper_id', 'page', 'unit_type', 'item_type', 'line'} for e in out['index']))
        self.assertEqual(out['context_notes'][0]['path'], 'ROS.md')
        budget = neural.standard.param('NW-01', 'token_budget_fast') * 4
        self.assertLessEqual(len(json.dumps(out['index'], ensure_ascii=False).encode()), budget)
        self.assertLess(len(json.dumps(out, ensure_ascii=False).encode()), len(json.dumps(full, ensure_ascii=False, default=str).encode()))
        # saved exactly as a full round: same items, same fingerprints; only the opened list differs
        a, b = (db.read_json(self.session_file(r['session'])) for r in (full, out))
        self.assertEqual([i['id'] for i in a['union']], [i['id'] for i in b['union']])
        self.assertEqual(a['rounds'][0]['fingerprint'], b['rounds'][0]['fingerprint'])
        self.assertEqual((a['opened'], b['opened']), ([1, 2, 3, 4, 5, 6], []))
        neural._verify_rounds(b)

    def test_plain_ticket_refuses_index(self):
        with self.assertRaisesRegex(ValueError, "index needs a research session"):
            self.ask([0], index=True)
        self.assertFalse(list((config.CACHE_DIR / 'neural_sessions').glob('*.json')) if (config.CACHE_DIR / 'neural_sessions').is_dir() else [])

    def test_the_index_builder_stops_at_the_budget_and_a_page_always_moves_on(self):
        items = [{'paper_id': 'p', 'pages': [n], 'text': 'x ' * 80, 'kind': 'sentence'} for n in range(1, 40)]
        real = neural.standard.param
        with patch.object(neural.standard, 'param', side_effect=lambda c, n: 700 if (c, n) == ('NW-01', 'token_budget_fast') else real(c, n)):
            page = neural.index_page({'session': 's'}, enumerate(items, 1))
            self.assertTrue(0 < len(page) < len(items))
            self.assertLessEqual(len(json.dumps({'session': 's', 'index': page}).encode()), 700 * 4)
            self.assertEqual(len(neural.index_page({'x': 'y' * 9000}, enumerate(items, 1))), 1)

    def test_the_whole_index_reply_stays_within_the_budget(self):   # not only out['index']: not_indexed counts too
        full = self.ask(range(6), session='new')
        links = {i['paper_id']: {'note_path': i.get('note_path'), 'obsidian_url': i.get('obsidian_url')} for i in full['items']}
        real, partial = neural.standard.param, 0
        for tokens in range(100, 3000, 10):
            with patch.object(neural.standard, 'param', side_effect=lambda c, n: tokens if (c, n) == ('NW-01', 'token_budget_fast') else real(c, n)):
                out = neural._index_view(dict(full), links)
            if len(out['index']) > 1:
                self.assertLessEqual(len(json.dumps(out, ensure_ascii=False).encode()), tokens * 4, tokens)
                partial += bool(out['not_indexed'])
        self.assertTrue(partial)

    def test_open_session_numbers_return_full_text_and_count_as_opened(self):
        sid = self.ask(range(6), session='new', index=True)['session']
        self.assertEqual(db.read_json(self.session_file(sid))['opened'], [])
        full = neural.open_session(sid, numbers=[2, 5])
        self.assertEqual([i['text'] for i in full['items']], [self.rows[1]['text'], self.rows[4]['text']])
        self.assertEqual(db.read_json(self.session_file(sid))['opened'], [2, 5])
        self.assertEqual(neural.open_session(sid)['index'][1]['item_type'], 'sentence')   # the same index builder
        neural.open_session(sid, numbers=[2, 3])
        self.assertEqual(db.read_json(self.session_file(sid))['opened'], [2, 3, 5])
        neural._verify_rounds(neural.load_session(sid))

    def test_a12_warns_on_a_part2_sentence_citing_an_item_seen_only_in_the_index(self):
        sid = self.ask(range(6), session='new', index=True)['session']
        cited = lambda: neural.check(self.con, sid, self.reply((0, 1)))['answer'][0]
        row = cited()
        self.assertEqual((row['check'], row['warnings']), ('ok', ['A12']))
        self.assertIn(answer.CHECK_TEXT['A12'], row['reasons'])
        marker = neural.check(self.con, sid, {'answer': [{'text': self.rows[0]['text'] + ' [#1]', 'evidence': []}]})['answer'][0]
        self.assertEqual((marker['check'], marker['warnings']), ('ok', ['A12']))   # "[#n]" in the text cites too
        notes = neural.check(self.con, sid, {'answer': [], 'reasoning': [{'text': 'The note frames ROS.', 'basis': ['ROS.md']}]})
        self.assertEqual(notes['reasoning'][0]['warnings'], [])   # Part 3 is not Part 2
        self.assertNotIn('A12', answer.STRIKE)
        neural.open_session(sid, numbers=[1])
        self.assertEqual(cited()['warnings'], [])
        other = neural.check(self.con, sid, self.reply((1, 2)))['answer'][0]
        self.assertEqual(other['warnings'], ['A12'])   # [#2] was never fetched

    def test_no_a12_for_a_full_round_a_plain_ticket_or_an_old_session_file(self):
        sid = self.ask(range(6), session='new')['session']
        self.assertEqual(neural.check(self.con, sid, self.reply((0, 1)))['answer'][0]['warnings'], [])
        ticket = self.ask([0])['ticket']
        self.assertEqual(neural.check(self.con, ticket, self.reply((0, 1)))['answer'][0]['warnings'], [])
        old = self.ask(range(6), session='new', index=True)['session']
        state = db.read_json(self.session_file(old))
        del state['opened']
        db.write_json(self.session_file(old), state)
        self.assertEqual(neural.check(self.con, old, self.reply((0, 1)))['answer'][0]['warnings'], [])
        self.ask(range(6), 'second question', session=old, index=True)   # an old file keeps its items opened, a new index round adds none
        self.assertEqual(db.read_json(self.session_file(old))['opened'], [1, 2, 3, 4, 5, 6])

    def test_ask_check_with_a_session_gets_the_warning_from_the_same_place(self):
        sid = self.ask(range(6), session='new', index=True)['session']
        out = agent_tasks.answer_check(self.con, config.load(), 'q', ['q'], self.reply((0, 1)), vault='one', session=sid)
        self.assertEqual(out['answer']['answer'][0]['warnings'], ['A12'])

    def test_mcp_neural_prepare_index_flag(self):
        mcp_server._check_args(self.con, 'neural_prepare', {'index': True})
        with self.assertRaisesRegex(ValueError, 'index must be a boolean'):
            mcp_server._check_args(self.con, 'ask_prepare', {'index': 'yes'})
        google = {'hits': [{'paper_id': self.pid, 'sentence_ids': self.ids(0, 1), 'matched_sentences': None}]}
        with patch('kbsys.find.search', return_value=google):
            out = self.call('neural_prepare', vault='one', question='ROS', session='new', index=True)
        self.assertEqual([e['n'] for e in out['index']], [1, 2])


class AskIndexTests(BrainstormBase):
    def ask_prepare(self, **extra):
        return self.call('ask_prepare', question='ROS', queries=['ROS'], view='evidence4', vault='one', speed='fast', **extra)

    def test_index_and_numbers_keep_the_numbering_and_the_digest(self):
        full, index = self.ask_prepare(), self.ask_prepare(index=True)
        self.assertEqual(index['items_digest'], full['items_digest'])
        self.assertTrue(full['items_found'] > 1)
        self.assertEqual([e['n'] for e in index['index']], list(range(1, full['items_found'] + 1)))
        for key in ('prompts', 'links', 'items'):
            self.assertNotIn(key, index)
        for key in ('items_found', 'not_found', 'not_found_message', 'overview', 'scope_note', 'answer_protocol'):
            self.assertEqual(index[key], full[key])
        self.assertEqual(self.ask_prepare(index=True, offset=1)['index'][0]['n'], 2)
        two = self.ask_prepare(numbers=[2])
        self.assertEqual((two['items_digest'], [i['n'] for i in two['items']], [l['n'] for l in two['links']]), (full['items_digest'], [2], [2]))
        self.assertIn(two['items'][0]['text'], full['prompts'][0]['user'])
        self.assertNotIn('prompts', two)
        self.assertEqual(two['links'][0]['n'], 2)
        with self.assertRaisesRegex(ValueError, 'numbers must be'):
            self.ask_prepare(numbers=[99])


class ToolsetTests(BrainstormBase):
    def message(self, method, **params):
        return mcp_server.handle(self.con, config.load(), {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params})

    def test_toolset_limits_list_and_call(self):
        everything = {t['name'] for t in self.message('tools/list')['result']['tools']}
        self.assertTrue({'add_units', 'remember'} <= everything)
        mcp_server.TOOLSET = mcp_server.BRAINSTORM_TOOLS
        self.assertEqual({t['name'] for t in self.message('tools/list')['result']['tools']}, mcp_server.BRAINSTORM_TOOLS)
        reply = self.message('tools/call', name='add_units', arguments={'paper_id': self.pid, 'units': []})
        self.assertIn('Unknown tool', reply['error']['message'])
        self.assertFalse(self.message('tools/call', name='list_sessions', arguments={'vault': 'one'})['result']['isError'])

    def test_save_discussion_waits_for_the_library_lock(self):   # `kb.py tool` is readonly: handle() must take it
        import hashlib
        lock = Path(config.STORE) / 'locks' / (hashlib.sha256(b'library').hexdigest()[:20] + '.lock')
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text('{"token": "another writer"}', encoding='utf-8', newline='\n')
        reply = self.message('tools/call', name='save_discussion', arguments={'vault': 'one', 'title': 't', 'markdown': 'm'})
        self.assertIn('WriterBusy', reply['result']['content'][0]['text'])
        self.assertFalse((config.vault_dir('one') / 'Discussions' / 't.md').exists())
        lock.unlink()
        self.assertFalse(self.message('tools/call', name='save_discussion', arguments={'vault': 'one', 'title': 't', 'markdown': 'm'})['result']['isError'])


class MemoryTests(BrainstormBase):
    def memory_file(self, *parts):
        return discussions.MEMORY_DIR.joinpath(*parts)

    def test_remember_appends_a_dated_line_and_never_changes_earlier_bytes(self):
        first = discussions.remember('one', ' I study\nvitrification  ', 'researcher', 'profile')
        self.assertRegex(first['line'], r'^- \d{4}-\d\d-\d\d · bạn nói: I study vitrification$')
        self.assertEqual((first['file'], first['lines_now'], first['cap']), ('runtime/brainstorm/profile.md', 1, 60))
        before = self.memory_file('profile.md').read_bytes()
        second = discussions.remember('one', 'I prefer "cryo" over vitrification', 'researcher', 'profile')
        self.assertRegex(second['line'], r' · bạn nói: I prefer')
        self.assertRegex(discussions.remember('one', 'searched "cryo" twice: not found', 'ai', 'topic')['line'], r' · AI ghi: ')
        self.assertTrue(self.memory_file('profile.md').read_bytes().startswith(before))
        self.assertEqual(discussions.memory('one')['profile'], [first['line'], second['line']])
        path = self.memory_file('one', 'topics.md')   # a hand-edited file without a final newline keeps its bytes
        path.write_bytes(b'\xef\xbb\xbf- hand written, no newline')
        discussions.remember('one', 'next', 'ai', 'topic')
        self.assertTrue(path.read_bytes().startswith(b'\xef\xbb\xbf- hand written, no newline\n- '))
        self.assertEqual(discussions.memory('one')['topics'][0], '- hand written, no newline')

    def test_refusals(self):
        for vault in ('../x', 'nope', '..', 'one/..', ''):
            with self.subTest(vault=vault), self.assertRaises(ValueError):
                discussions.remember(vault, 'x', 'ai', 'topic')
            with self.assertRaises(ValueError):
                discussions.memory(vault)
        self.assertFalse(discussions.MEMORY_DIR.exists())
        for text in ('', '  \n ', 'x' * 301):
            with self.subTest(text=text[:5]), self.assertRaises(ValueError):
                discussions.remember('one', text, 'ai', 'topic')
        discussions.remember('one', 'x' * 300, 'ai', 'topic')
        for by, scope in (('model', 'topic'), ('ai', 'global'), ('ai', 'profile')):   # AN-20: the profile is the researcher's words only
            with self.assertRaises(ValueError):
                discussions.remember('one', 'x', by, scope)
        for text in (['x'], {'a': 1}, 7):
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, 'string'):
                discussions.remember('one', text, 'researcher', 'profile')
        self.assertFalse(self.memory_file('profile.md').exists())
        real = discussions.standard.param
        with patch.object(discussions.standard, 'param', side_effect=lambda c, n: 2 if n == 'topic_max_lines' else real(c, n)):
            discussions.remember('one', 'second', 'ai', 'topic')
            before = self.memory_file('one', 'topics.md').read_bytes()
            with self.assertRaisesRegex(ValueError, 'đầy'):
                discussions.remember('one', 'third', 'ai', 'topic')
            self.assertEqual(self.memory_file('one', 'topics.md').read_bytes(), before)

    def test_memory_text_is_stored_and_read_as_nfc(self):   # CHR-01
        import unicodedata
        line = discussions.remember('one', unicodedata.normalize('NFD', 'tế bào'), 'researcher', 'profile')['line']
        self.assertTrue(line.endswith('tế bào') and unicodedata.is_normalized('NFC', line))
        path = self.memory_file('one', 'topics.md')
        path.parent.mkdir(parents=True)
        path.write_text(unicodedata.normalize('NFD', '- sửa tay'), encoding='utf-8', newline='\n')
        self.assertEqual(discussions.memory('one')['topics'], ['- sửa tay'])

    def test_a_vault_never_reads_another_vaults_topics(self):
        discussions.remember('two', 'only for two', 'ai', 'topic')
        discussions.remember('one', 'shared profile', 'researcher', 'profile')
        mine = discussions.memory('one')
        self.assertEqual(mine['topics'], [])
        self.assertNotIn('only for two', json.dumps(mine, ensure_ascii=False))
        self.assertEqual(len(mine['profile']), 1)
        self.assertEqual((mine['label_researcher'], mine['label_ai']),
                         ('ý của bạn (đã nhớ, <ngày>)', 'ghi chú tìm kiếm của AI (<ngày>)'))
        self.assertEqual(discussions.memory('two')['topics'][0][-12:], 'only for two')

    def test_the_server_serves_memory_only_when_switched_on(self):
        mcp_server.MEMORY = False   # Hermes / a retrieval-only client
        with self.assertRaisesRegex(ValueError, 'memory is off'):
            self.call('remember', vault='one', text='x', by='ai', scope='topic')
        self.assertFalse(discussions.MEMORY_DIR.exists())
        self.assertEqual(set(self.call('get_answer_rules', vault='one')), {'protocol'})
        mcp_server.MEMORY = True
        self.call('remember', vault='one', text='from the server', by='ai', scope='topic')
        self.assertEqual(self.call('get_answer_rules', vault='one')['memory']['topics'][0][-15:], 'from the server')
        self.assertEqual(set(self.call('get_answer_rules')), {'protocol'})   # no vault, no memory
        with self.assertRaises(ValueError):
            mcp_server._check_args(self.con, 'get_answer_rules', {'vault': '../x'})


class BridgeTests(BrainstormBase):
    def run_tool(self, name, arguments=None, all_tools=False):
        folder = Path(tempfile.mkdtemp(dir=self.root))
        args_file = folder / 'args.json'
        args_file.write_text(json.dumps(arguments or {}), encoding='utf-8')
        args = SimpleNamespace(name=name, args=str(args_file), out=str(folder / 'deep' / 'out.json'), all=all_tools)
        shown = io.StringIO()
        with contextlib.redirect_stdout(shown):
            try:
                kb.cmd_tool(args, config.load(), self.con)
                code = 0
            except SystemExit as stop:
                code = stop.code
        return code, shown.getvalue(), Path(args.out)

    def test_a_short_result_is_printed_and_saved(self):
        code, shown, out = self.run_tool('list_sessions', {'vault': 'one'})
        self.assertEqual((code, json.loads(shown)), (0, []))
        self.assertEqual(out.read_text(encoding='utf-8'), '[]')
        self.assertEqual((mcp_server.CLIENT['name'], mcp_server.MEMORY), ('claude-code', True))

    def test_stdin_arguments_are_read_as_utf8_on_any_console(self):
        stdin = io.TextIOWrapper(io.BytesIO(json.dumps({'vault': 'one', 'text': 'tế bào', 'by': 'researcher', 'scope': 'profile'},
                                                       ensure_ascii=False).encode()), encoding='cp1252')
        out = Path(tempfile.mkdtemp(dir=self.root)) / 'out.json'
        with patch('sys.stdin', stdin), contextlib.redirect_stdout(io.StringIO()):
            kb.cmd_tool(SimpleNamespace(name='remember', args='-', out=str(out), all=False), config.load(), self.con)
        self.assertEqual(discussions.memory('one')['profile'][0][-6:], 'tế bào')

    def test_a_long_result_prints_a_preview(self):
        code, shown, out = self.run_tool('get_answer_rules', {'vault': 'one'})
        full = out.read_text(encoding='utf-8')
        self.assertGreater(len(full), 4000)
        preview = json.loads(shown)
        self.assertEqual((code, preview['full'], preview['chars'], preview['keys'], preview['preview']),
                         (0, str(out), len(full), ['protocol', 'memory'], full[:2500]))

    def test_only_the_brainstorm_toolset_without_all(self):
        code, shown, out = self.run_tool('add_units', {'paper_id': self.pid, 'units': []})
        self.assertEqual(code, 1)
        self.assertIn('Unknown tool: add_units', shown)
        self.assertFalse(out.exists())
        code, shown, _ = self.run_tool('add_units', {'paper_id': 'nope', 'units': []}, all_tools=True)
        self.assertEqual(code, 1)
        self.assertIn('Unknown paper_id', shown)   # an isError result: the error text, exit 1

    def test_the_main_parser_knows_the_new_flags(self):
        with patch('sys.argv', ['kb.py', 'tool', 'x']), patch.object(kb, 'cmd_tool') as handler, \
                patch.object(kb.config, 'load', return_value={}), patch.object(kb.db, 'connect') as connect:
            kb.main()
        self.assertEqual(handler.call_args[0][0].all, False)
        with patch('sys.argv', ['kb.py', 'mcp', '--tools', 'brainstorm', '--memory']), patch.object(kb, 'cmd_mcp') as handler, \
                patch.object(kb.config, 'load', return_value={}), patch.object(kb.db, 'connect'):
            kb.main()
        self.assertEqual((handler.call_args[0][0].tools, handler.call_args[0][0].memory), ('brainstorm', True))


class SetupTests(BrainstormBase):
    def test_memory_flag_only_for_claude_desktop_and_codex(self):
        paths = {'claude-desktop': self.root / 'desktop.json', 'codex': self.root / 'codex.toml',
                 'gemini-cli': self.root / 'gemini.json', 'cursor': self.root / 'cursor.json'}
        with patch.object(mcp_setup, 'config_paths', return_value=paths):
            for client in paths:
                mcp_setup.install(client, 'brainstorm')
            entry = lambda client: json.loads(paths[client].read_text(encoding='utf-8'))['mcpServers'][mcp_setup.NAME]['args']
            self.assertEqual(entry('claude-desktop')[-3:], ['--tools', 'brainstorm', '--memory'])
            self.assertEqual(entry('gemini-cli')[-2:], ['--tools', 'brainstorm'])
            self.assertNotIn('--memory', entry('cursor'))
            self.assertIn('"--memory"', paths['codex'].read_text(encoding='utf-8'))
            self.assertIn('"brainstorm"', paths['codex'].read_text(encoding='utf-8'))
            snippets = mcp_setup.snippets()
            self.assertNotIn('--memory', snippets['generic']['args'])
            self.assertEqual(snippets['generic']['args'][-1], 'mcp')
            self.assertNotIn('--memory', snippets['claude-code']['run'])
            self.assertIn('--memory', snippets['claude-desktop']['add']['mcpServers'][mcp_setup.NAME]['args'])
            self.assertNotIn('--memory', mcp_setup.report_text().split('== generic')[1])
            self.assertIn('9router', mcp_setup.report_text().split('== generic')[1])   # O1/Q6: Hermes sends quotes to a cloud provider
            printed = mcp_setup.report_text('brainstorm')   # `kb.py mcp-config --tools brainstorm` without --install
            self.assertIn('"--tools",', printed.split('== claude-code')[0])
            self.assertIn('"brainstorm"', printed.split('== codex')[1].split('== gemini')[0])
            mcp_setup.install('claude-desktop')
            self.assertEqual(entry('claude-desktop')[-2:], ['mcp', '--memory'])


if __name__ == '__main__':
    unittest.main()
