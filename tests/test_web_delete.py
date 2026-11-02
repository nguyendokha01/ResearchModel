"""POST /api/paper/delete (Library "Xóa bài"): guards, writer lock, reversible move, Mine/ untouched."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from . import test_core as fixtures
from kbsys import config, db, workflow


class WebDeleteTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def post(self, body, headers=None):
        """(status, json) of POST /api/paper/delete against a throw-away server."""
        import web
        server = web.Server(('127.0.0.1', 0), web.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        hdr = {'Content-Type': 'application/json', **({'X-KB-Token': web.TOKEN} if headers is None else headers)}
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/api/paper/delete',
                                     data=json.dumps(body).encode(), headers=hdr, method='POST')
        try:
            try:
                with urllib.request.urlopen(req) as r:
                    return r.status, json.load(r)
            except urllib.error.HTTPError as e:
                return e.code, json.load(e)
        finally:
            server.shutdown(); server.server_close(); thread.join()

    def add_notes(self):
        vault = config.vault_dir('one')
        (vault / 'Papers').mkdir()
        note = vault / 'Papers' / 'Test note.md'
        note.write_text('---\ntitle: x\n---\nMy own words stay.\n', encoding='utf-8')
        mine = vault / 'Mine' / 'Ideas' / 'Idea.md'
        mine.parent.mkdir(parents=True)
        mine.write_bytes(b'my idea\n')
        meta = db.read_json(self.folder / 'metadata.json')
        meta['note_name'] = 'Test note'
        db.write_json(self.folder / 'metadata.json', meta)
        db.reload_paper(self.con, self.pid)
        self.con.commit()
        return note, mine

    def test_refused_without_token_or_with_bad_host_and_nothing_moves(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        self.assertEqual(self.post({'paper_id': self.pid}, headers={})[0], 403)
        import web
        self.assertEqual(self.post({'paper_id': self.pid}, headers={'X-KB-Token': web.TOKEN, 'Host': 'evil.test'})[0], 403)
        self.assertEqual(self.post({'paper_id': self.pid}, headers={'X-KB-Token': 'wrong'})[0], 403)
        self.assertTrue(self.folder.is_dir())
        self.assertFalse((config.STORE / 'removed').exists())

    def test_bad_or_unknown_id_is_a_clean_error(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        for body in ({}, {'paper_id': 5}, {'paper_id': '../x'}, {'paper_id': 'a/b'}, {'paper_id': ''}):
            code, data = self.post(body)
            self.assertEqual(code, 400, body)
            self.assertIn('Mã bài', data['error'])
        code, data = self.post({'paper_id': 'nobody2026_none_abcd'})
        self.assertEqual((code, data['error']), (400, 'Không có bài này trong thư viện'))
        self.assertTrue(self.folder.is_dir())

    def test_writer_busy_is_refused_in_vietnamese_and_nothing_moves(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        with workflow.writer_lock():
            code, data = self.post({'paper_id': self.pid})
        self.assertEqual(code, 409)
        self.assertIn('đang có việc khác', data['error'].lower())
        self.assertTrue(self.folder.is_dir())
        self.assertIsNotNone(self.con.execute('SELECT 1 FROM papers WHERE paper_id=?', (self.pid,)).fetchone())

    def test_running_library_job_is_refused_and_nothing_moves(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        from kbsys import library
        saved = dict(library._JOB)
        library._JOB.update(running=True, action='analyze')
        try:
            code, data = self.post({'paper_id': self.pid})
        finally:
            library._JOB.update(saved)
        self.assertEqual(code, 409)
        self.assertIn('analyze', data['error'])
        self.assertTrue(self.folder.is_dir())
        self.assertFalse((config.STORE / 'removed').exists())

    def test_good_request_moves_paper_reversibly_with_manifest_and_leaves_mine(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        note, mine = self.add_notes()
        code, data = self.post({'paper_id': self.pid})
        self.assertEqual(code, 200, data)
        self.assertEqual(data['paper_id'], self.pid)
        staged = config.ROOT / data['moved_to']
        self.assertTrue(staged.is_dir() and staged.parent == config.STORE / 'removed')
        manifest = json.loads((staged / 'manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['paper_id'], self.pid)
        moved_from = {Path(m['from']).resolve() for m in manifest['moved']}
        self.assertIn(self.folder.resolve(), moved_from)
        self.assertIn(note.resolve(), moved_from)
        self.assertFalse(self.folder.exists())
        self.assertTrue(any(p.read_text(encoding='utf-8').endswith('My own words stay.\n')
                            for p in staged.rglob('Test note.md')))     # human text kept whole in removed/
        self.assertEqual(mine.read_bytes(), b'my idea\n')                # Mine/ untouched
        con = db.connect()
        try:
            self.assertIsNone(con.execute('SELECT 1 FROM papers WHERE paper_id=?', (self.pid,)).fetchone())
        finally:
            con.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
