"""G6 U1: kb.py run-all (pipeline.run_all) in a temp store, never the researcher's library."""
from contextlib import redirect_stdout
import io
import sys
import unittest
from unittest.mock import MagicMock, patch

import kb
from kbsys import config, pipeline, vault_layout
from tests import test_core


class RunAllTests(unittest.TestCase):
    def setUp(self):
        test_core.Demo3Tests.setUp(self)  # temp STORE/VAULTS, vault 'one' with one sample paper
        self.mine = config.vault_dir('one') / 'Mine' / 'Ideas' / 'idea.md'
        self.mine.parent.mkdir(parents=True)
        self.mine.write_text('mine\n', encoding='utf-8')
        self.snap = MagicMock(return_value={'snapshot': 'SNAP'})
        patcher = patch.object(vault_layout, '_research_workspace', return_value=MagicMock(snapshot=self.snap))
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        test_core.Demo3Tests.tearDown(self)

    def _run(self, effect=None, **kw):
        calls = []
        def fake(con, cfg, vault, step, say=print, **_):
            calls.append(step)
            if effect:
                effect(step)
            return [{'status': 'done', 'cache_hit': step == 'vectors'}]
        with patch.object(pipeline, 'run', side_effect=fake):
            return pipeline.run_all(self.con, {}, 'one', say=lambda *a: None, **kw), calls

    def test_order_analyze_skipped_without_ai_and_one_snapshot(self):
        report, calls = self._run()
        self.assertEqual(calls, [s for s in pipeline.STEPS if s != 'analyze'])
        self.assertEqual([s['step'] for s in report['steps']], list(pipeline.STEPS))
        self.assertEqual(report['steps'][3]['status'], 'skipped (needs --ai)')
        self.assertEqual(report['steps'][5]['result'], {'done (cache)': 1})
        self.snap.assert_called_once()
        self.assertEqual((report['snapshot'], report['mine_unchanged'], report['mine_changed']), ('SNAP', True, []))

    def test_ai_runs_analyze(self):
        self.assertEqual(self._run(ai=True)[1], list(pipeline.STEPS))

    def test_until_stops_after_stage(self):
        report, calls = self._run(until='structure')
        self.assertEqual(calls, ['ingest', 'quality', 'structure'])
        self.assertEqual(len(report['steps']), 3)

    def test_first_failing_step_stops_the_run(self):
        def effect(step):
            if step == 'quality':
                raise RuntimeError('boom')
        report, calls = self._run(effect)
        self.assertEqual(calls, ['ingest', 'quality'])
        self.assertEqual(report['failed'], 'quality')
        self.assertEqual(report['steps'][-1]['status'], 'failed')
        self.assertIn('boom', report['steps'][-1]['result'])

    def test_mine_change_is_detected_and_not_fixed(self):
        report, _ = self._run(lambda step: self.mine.write_text('changed\n', encoding='utf-8') if step == 'quality' else None)
        self.assertFalse(report['mine_unchanged'])
        self.assertEqual(report['mine_changed'], [str(self.mine.relative_to(config.vault_dir('one') / 'Mine'))])
        self.assertEqual(self.mine.read_text(encoding='utf-8'), 'changed\n')

    def test_unknown_vault_or_stage_fails_before_snapshot(self):
        for kw in ({'vault': 'nope'}, {'until': 'nope'}, {'vault': '..'}, {'vault': ''}, {'vault': '.'}):  # '..' = project root
            with self.subTest(kw=kw), self.assertRaises(ValueError):
                pipeline.run_all(self.con, {}, kw.get('vault', 'one'), until=kw.get('until'))
        self.snap.assert_not_called()

    def test_snapshot_failure_or_busy_lock_runs_no_step(self):
        from kbsys import workflow
        self.snap.side_effect = OSError('disk full')
        with self.assertRaises(OSError):
            self._run()
        self.snap.side_effect = None
        with patch.object(workflow, 'writer_lock', side_effect=workflow.WriterBusy('busy')), self.assertRaises(workflow.WriterBusy):
            self._run()
        with patch.object(pipeline, 'run') as run:
            self.snap.side_effect = OSError('x')
            with self.assertRaises(OSError):
                pipeline.run_all(self.con, {}, 'one', say=lambda *a: None)
            run.assert_not_called()

    def test_a_waiting_state_is_not_reported_done(self):
        with patch.object(pipeline, 'run', return_value=[{'status': 'done'}, {'status': 'waiting'}]):
            report = pipeline.run_all(self.con, {}, 'one', ai=True, until='analyze', say=lambda *a: None)
        self.assertEqual({s['status'] for s in report['steps']}, {'waiting'})
        self.assertNotIn('failed', report)

    def test_real_quality_step_runs_on_the_temp_vault(self):
        real = pipeline.run
        with patch.object(pipeline, 'run', side_effect=lambda con, cfg, v, step, **k: real(con, cfg, v, step, **k)
                          if step == 'quality' else [{'status': 'done'}]):
            report = pipeline.run_all(self.con, config.load(), 'one', until='quality', say=lambda *a: None)
        self.assertEqual(report['steps'][1], {**report['steps'][1], 'step': 'quality', 'status': 'done'})
        self.assertTrue((self.folder / 'analysis_quality.json').is_file())

    def test_cli_exits_1_on_failed_step(self):
        for failed, code in (('quality', 1), (None, None)):
            report = {'vault': 'one', 'steps': [], 'mine_unchanged': True} | ({'failed': failed} if failed else {})
            argv = ['kb.py', 'run-all', '--vault', 'one', '--until', 'quality']
            with patch.object(kb.pipeline, 'run_all', return_value=report) as run_all, patch.object(sys, 'argv', argv), \
                    patch.object(kb.config, 'load', return_value={}), patch.object(kb.db, 'connect', return_value=MagicMock()), \
                    patch.object(kb.workflow, 'writer_lock') as lock, redirect_stdout(io.StringIO()):
                if code:
                    with self.assertRaises(SystemExit) as cm:
                        kb.main()
                    self.assertEqual(cm.exception.code, code)
                else:
                    kb.main()
            run_all.assert_called_once_with(unittest.mock.ANY, {}, 'one', ai=False, until='quality')
            lock.assert_not_called()  # like `steps`: per-stage locks live inside pipeline.run


if __name__ == '__main__':
    unittest.main()
