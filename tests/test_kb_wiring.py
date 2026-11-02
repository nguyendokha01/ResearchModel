"""Offline parser and notes-wiring checks for the demo3 CLI."""

import contextlib
import io
from pathlib import Path
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


try:
    import yaml  # noqa: F401
except ModuleNotFoundError:  # Plain sandbox Python has no project dependencies.
    yaml_stub = types.ModuleType("yaml")
    yaml_stub.safe_load = lambda text: {}
    yaml_stub.safe_dump = lambda value, **kwargs: ""
    sys.modules["yaml"] = yaml_stub

try:
    import pymupdf  # noqa: F401
except ModuleNotFoundError:  # Imported by extraction modules; no PDF work is run here.
    pymupdf_stub = types.ModuleType("pymupdf")
    pymupdf_stub.TEXTFLAGS_TEXT = 0
    pymupdf_stub.VersionBind = "stub"
    sys.modules["pymupdf"] = pymupdf_stub

import kb
from kbsys import pipeline
import tempfile


class _Connection:
    def close(self):
        pass


class KbWiringTests(unittest.TestCase):
    def test_neural_prepare_cli_pages_with_offset_and_hops_default_zero(self):
        with patch.object(kb.config, "load", return_value={}), patch.object(kb.db, "connect", return_value=_Connection()), \
                patch.object(kb.neural, "prepare", return_value={}) as prepare, patch.object(kb, "_out_json"):
            with patch.object(sys, "argv", ["kb.py", "neural-prepare", "ROS", "--vault", "one", "--offset", "5"]):
                kb.main()
        self.assertEqual(prepare.call_args.args[2:], ("one", "ROS", 0))
        self.assertEqual(prepare.call_args.kwargs, {"offset": 5})

    def test_parser_has_vault_v2_passthrough_commands(self):
        modules = {
            "node-card": "kbsys.node_cards",
            "figure-reading": "kbsys.figure_reading",
            "obsidian-plugins": "kbsys.vault_plugins",
        }
        con = _Connection()
        with patch.object(kb.config, "load", return_value={}), \
                patch.object(kb.db, "connect", return_value=con), \
                patch.object(kb.workflow, "writer_lock", return_value=contextlib.nullcontext()), \
                patch.object(kb.mother_vault, "refresh", return_value={}):
            for command, module in modules.items():
                with self.subTest(command=command), patch(f"{module}.main", return_value=0) as delegated:
                    with patch.object(sys, "argv", ["kb.py", command]):
                        kb.main()
                    delegated.assert_called_once_with([])
            with patch("kbsys.sources_misc._cli") as source_cli:
                with patch.object(sys, "argv", ["kb.py", "source"]):
                    kb.main()
                source_cli.assert_called_once_with()
            with patch.object(sys, "argv", ["kb.py", "mother-vault"]):
                kb.main()

    def test_cmd_notes_writes_source_notes_before_mother_vault(self):
        events = []
        con = _Connection()
        tidy = {"moved": [], "human_protected": []}

        def mark(name, result=None):
            def call(*args, **kwargs):
                events.append(name)
                return result
            return call

        temp = tempfile.TemporaryDirectory()  # gaps, the catalogue and the neural prewarm must not touch the real store
        self.addCleanup(temp.cleanup)
        (Path(temp.name) / "vaults" / "research").mkdir(parents=True)
        (Path(temp.name) / "vaults" / "research" / "Home.md").write_text("# Home\n", encoding="utf-8")  # a research vault
        with patch.object(kb.config, "STORE", Path(temp.name) / "store"), \
                patch.object(kb.config, "VAULTS_DIR", Path(temp.name) / "vaults"), \
                patch.object(kb.config, "CACHE_DIR", Path(temp.name) / "store/cache"), \
                patch.object(kb.config, "PAPERS_DIR", Path(temp.name) / "store/papers"), \
                patch.object(kb.notes, "refresh_all", side_effect=mark("papers", 2)), \
                patch.object(kb.concept_notes, "refresh", side_effect=mark("concepts", {"created": 1})), \
                patch.object(kb.source_notes, "write", side_effect=mark("sources", 3)) as source_write, \
                patch.object(kb.notes, "write_indexes", side_effect=mark("indexes")), \
                patch.object(kb.notes, "last_tidy_report", return_value=tidy), \
                patch.object(kb.vault_layout, "append_log", side_effect=mark("log")), \
                patch.object(kb.mother_vault, "refresh", side_effect=mark("mother", {})):
            output = io.StringIO()
            with patch("sys.stdout", output):
                kb.cmd_notes(SimpleNamespace(vault="research"), {}, con)

        source_write.assert_called_once_with("research")
        self.assertLess(events.index("sources"), events.index("mother"))
        self.assertIn("Source notes: 3 notes written or refreshed.", output.getvalue())

    def test_main_locks_plain_writers_but_not_ai_commands(self):
        con = _Connection()
        for command, extra, handler, expect_lock in (
                ("notes", [], "cmd_notes", True), ("link-concepts", [], "cmd_link_concepts", True),
                ("analyze", ["--vault", "v"], "cmd_analyze", False), ("steps", ["notes"], "cmd_demo3", False),
                ("structured-run", ["P"], "cmd_demo3", False)):
            with self.subTest(command=command):
                lock = MagicMock(return_value=contextlib.nullcontext())
                with patch.object(kb.config, "load", return_value={}), patch.object(kb.db, "connect", return_value=con),                         patch.object(kb.workflow, "writer_lock", lock), patch.object(kb, handler) as handled,                         patch.object(sys, "argv", ["kb.py", command, *extra]):
                    kb.main()
                handled.assert_called_once()
                self.assertEqual(lock.called, expect_lock)

    def test_steps_notes_runs_the_same_routine_as_kb_notes(self):
        con = MagicMock()
        con.execute.return_value = []
        with tempfile.TemporaryDirectory() as folder,                 patch.object(pipeline.config, "vault_dir", return_value=Path(folder)),                 patch.object(pipeline.workflow, "writer_lock", return_value=contextlib.nullcontext()),                 patch.object(pipeline.workflow, "checkpoint", side_effect=lambda s, sc, i, o, operation, opts=None: operation()),                 patch.object(pipeline, "rebuild_vault_notes", return_value={"ok": 1}) as shared:
            result = pipeline.run(con, {}, "v", "notes")
        shared.assert_called_once()
        self.assertEqual(shared.call_args.args[:2], (con, "v"))
        self.assertEqual(result, [{"ok": 1}])


if __name__ == "__main__":
    unittest.main()


class ReextractRefreshTests(unittest.TestCase):
    """cmd_reextract: targeted path refreshes only affected concepts in every vault of the paper; failures are not silent."""

    def _run(self, ids, all_=False, fail=(), media_fail=False):
        from kbsys import tier1, media, ghosts, concepts, concept_notes
        tier1.LAST_RELINK.clear()  # other tests' builds leave entries behind
        con = MagicMock()
        con.execute.side_effect = lambda sql, *a: (
            [("v1",), ("v2",)] if "paper_vaults" in sql and "DISTINCT" not in sql else
            [{"paper_id": i} for i in ids] if "FROM papers" in sql else [("v1",), ("v2",), ("v3",)])

        def fake_reextract(con_, cfg, pid):
            tier1.LAST_RELINK[pid] = {"affected_concepts": [f"c_{pid}"], "affected_vaults": ["v9"]}
            if pid in fail:
                raise RuntimeError("boom after relink")
            return {"sentences": 1, "references": 0, "figures": 0, "tables": 0}, "ok"
        refresh = MagicMock(return_value="r")
        args = SimpleNamespace(all=all_, paper_id=ids[0], dry_run=False)
        with patch.object(tier1, "reextract", fake_reextract), \
             patch.object(media, "build", MagicMock(side_effect=OSError("no pdf") if media_fail else None)) as mb, \
             patch.object(ghosts, "rebuild", return_value=0), patch.object(concepts, "rebuild_links", return_value=0), \
             patch.object(concept_notes, "refresh", refresh), contextlib.redirect_stdout(io.StringIO()):
            kb.cmd_reextract(args, {}, con)
        self.assertEqual(tier1.LAST_RELINK, {})  # nothing stale left behind
        return refresh, mb

    def test_targeted_refreshes_affected_concepts_in_every_paper_vault(self):
        refresh, mb = self._run(["p1"])
        mb.assert_called_once_with("p1")
        self.assertEqual({c.args[1] for c in refresh.call_args_list}, {"v1", "v2", "v9"})
        for c in refresh.call_args_list:
            self.assertEqual(c.kwargs, {"concept_ids": ["c_p1"]})

    def test_media_failure_does_not_stop_the_refresh(self):
        refresh, _ = self._run(["p1"], media_fail=True)
        self.assertEqual(refresh.call_count, 3)

    def test_failure_after_relink_still_refreshes_its_concepts(self):
        refresh, _ = self._run(["p1"], fail=("p1",))
        self.assertEqual(refresh.call_args.kwargs, {"concept_ids": ["c_p1"]})

    def test_all_refreshes_every_vault_fully(self):
        refresh, _ = self._run(["p1", "p2"], all_=True)
        self.assertEqual({c.args[1] for c in refresh.call_args_list}, {"v1", "v2", "v3"})
        for c in refresh.call_args_list:
            self.assertEqual(c.kwargs, {})
