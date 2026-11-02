"""W7: `kb.py release DEST` copies only shareable software and reports private traces."""
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from . import test_core as fixtures
from kbsys import config, db, package


class ReleaseTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def put(self, rel, text):
        path = config.ROOT / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def test_release_filters_private_files_and_reports_leaks(self):
        self.put("AGENTS.md", "# Rules\n## Working on the code\nread docs/STATUS.md\n## Core rule\nNo invention.\n")
        self.put("README.md", "Start here.\nProgress: docs/STATUS.md\n")
        self.put("CLAUDE.md", "@AGENTS.md\n")
        self.put("config/vaults.yaml", "# kinds\nvaults:\n  secret_topic:\n    vault_kind: research\n")
        self.put("docs/STATUS.md", "private progress\n")
        self.put(".claude/hooks/review_gate.py", "x = 1\n")
        self.put("scripts/kbsys/x.py", "PATH = r'" + "C:" + "\\Users\\someone\\kb'\n")  # split: no leak in this file
        self.put("tests/test_x.py", "ID = 'smith2020_real_paper_abcd'\n")
        db.write_json(config.PAPERS_DIR / "smith2020_real_paper_abcd" / "metadata.json",
                      {"doi": "10.1/REAL", "title": "A real paper title that is long enough", "year": 2020,
                       "authors": [{"family": "Smith"}]})
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "release"
            result = package.release(dest)
            shipped = set((dest / ".release_files").read_text(encoding="utf-8").split())
            self.assertNotIn("CLAUDE.md", shipped)
            self.assertNotIn("docs/STATUS.md", shipped)
            self.assertNotIn(".claude/hooks/review_gate.py", shipped)
            agents = (dest / "AGENTS.md").read_text(encoding="utf-8")
            self.assertNotIn("Working on the code", agents)
            self.assertIn("No invention.", agents)
            self.assertNotIn("STATUS", (dest / "README.md").read_text(encoding="utf-8"))
            self.assertNotIn("secret_topic", (dest / "config/vaults.yaml").read_text(encoding="utf-8"))
            kinds = " ".join(result["leaks"])
            self.assertIn("machine path", kinds)
            self.assertIn("library paper id", kinds)
            (config.ROOT / "tests/test_x.py").unlink()  # gone from the project -> removed from the next release
            self.assertEqual(package.release(dest)["removed_from_earlier_release"], 1)
            self.assertFalse((dest / "tests/test_x.py").exists())
            with self.assertRaises(ValueError):
                package.release(config.ROOT / "inside")


    def test_plan_and_audit_quote_papers_so_neither_package_nor_release_ships_them(self):
        self.put("docs/PLAN.md", "plan: a paper reported something\n")
        self.put("docs/AUDIT_2026-10-16.md", "audit quotes a sentence of a paper\n")
        self.put("docs/AGENT_TOOLS.md", "public tool guide\n")
        self.put("README.md", "Start here.\nPlan: docs/PLAN.md\nAudit: docs/AUDIT_2026-10-16.md\n")
        for name in ("docs/PLAN.md", "docs/AUDIT_2026-10-16.md", "docs/AUDIT_next.md"):
            self.assertTrue(package.NEVER.search(name), name)
        self.assertFalse(package.NEVER.search("docs/AGENT_TOOLS.md"))
        with tempfile.TemporaryDirectory() as temp:
            built = package.build(Path(temp) / "kb.zip", with_models=False)
            with zipfile.ZipFile(built["zip"]) as z:
                names = set(z.namelist())
            self.assertIn("docs/AGENT_TOOLS.md", names)
            self.assertNotIn("docs/PLAN.md", names)
            self.assertNotIn("docs/AUDIT_2026-10-16.md", names)
            dest = Path(temp) / "release"
            package.release(dest)
            shipped = set((dest / ".release_files").read_text(encoding="utf-8").split())
            self.assertIn("docs/AGENT_TOOLS.md", shipped)
            self.assertFalse({"docs/PLAN.md", "docs/AUDIT_2026-10-16.md"} & shipped)
            self.assertFalse((dest / "docs" / "PLAN.md").exists())
            readme = (dest / "README.md").read_text(encoding="utf-8")
            self.assertNotIn("PLAN", readme)
            self.assertNotIn("AUDIT", readme)

    def test_status_history_plans_decisions_and_dev_skill_never_ship(self):
        for name in ("docs/status/2026-10.md", "docs/plan/full-2026-10-17.md", "docs/plan/G4-AN.md", "docs/DECISIONS.md"):
            self.assertTrue(package.NEVER.search(name), name)
        self.assertTrue(package.RELEASE_NEVER.search(".claude/skills/dev-workflow/SKILL.md"))
        self.assertTrue(package.RELEASE_NEVER.search("tests/test_review_gate.py"))  # it loads the hooks, which are not shipped
        self.assertFalse(package.NEVER.search("docs/ARCHITECTURE.md"))
        text = "# Rules\n## Evidence rules\n1. No invention.\n## Working on the code\nsee docs/status/2026-10.md\nprivate dev rules\n"
        out = package.DEV_SECTION.sub("", text)
        self.assertIn("No invention.", out)
        self.assertNotIn("private dev rules", out)  # the dev section is the last one in AGENTS.md
        self.assertNotIn("docs/DECISIONS.md", package.PRIVATE_DOC_LINE.sub("", "keep\nsee docs/DECISIONS.md\n"))

    def test_release_root_has_exactly_two_bat_files_and_no_dev_only_file(self):
        bats = ["1 - Mo trang chinh.bat", "2 - Ket noi AI (MCP).bat", "3 - Dong goi chia se.bat", "5 - Kiem tra demo3.bat"]
        never = ["tools/run.py", "tools/verify.py", "tools/accept_revision.py", "tests/test_accept_revision.py", "scripts/ui.py",
                 "scripts/accept_requirements.py", "scripts/benchmark_research.py", "CLAUDE.md", ".claude/settings.json",
                 ".claude/skills/ai-analysis-submit/SKILL.md", "projects/vault_registry.json"]
        keep = ["tools/research_workspace.py", "scripts/kb.py", "config/system.yaml", "docs/ARCHITECTURE.md", "docs/CONTRACTS.md",
                "README.md", "HUONG_DAN.md", "tests/test_core.py"]
        for name in bats + never + keep:
            self.put(name, "x\n")
        self.put("AGENTS.md", "# Rules\nread the table at the end.\n## Working on the code\nprivate\n| Need | Read |\n| s | `docs/STATUS.md` |\n")
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "release"
            package.release(dest)
            shipped = set((dest / ".release_files").read_text(encoding="utf-8").split("\n"))
            self.assertEqual(sorted(p.name for p in dest.glob("*.bat")), bats[:2])
            for name in never:
                self.assertNotIn(name, shipped)
                self.assertFalse((dest / name).exists(), name)
            for name in keep:
                self.assertIn(name, shipped)
            agents = (dest / "AGENTS.md").read_text(encoding="utf-8")  # the promised table at the end is the shipped one
            self.assertNotIn("private", agents)
            self.assertTrue(agents.rstrip().endswith("| Design and full research policy | `docs/ARCHITECTURE.md` |"))

    def test_scan_names_vaults_id_stems_owner_and_drive_folders_read_at_run_time(self):
        (config.VAULTS_DIR / "secret_topic").mkdir(parents=True)
        (config.VAULTS_DIR / "secret_topic" / "Home.md").write_text("x\n", encoding="utf-8")
        (config.PAPERS_DIR / "smith2020_real_paper_abcd").mkdir(parents=True)
        self.put("scripts/kbsys/x.py", "# smith2020 S0001 / secret_topic / Zedowner Doe / " + "Q:" + "\\zedfolder\\a\n")
        owner, listdir, anchor = Path.home().name.lower(), package.os.listdir, config.ROOT.resolve().anchor
        drive = lambda path: ["zedfolder", "Program Files"] if path == anchor else listdir(path)
        with tempfile.TemporaryDirectory() as temp, \
                mock.patch.object(package.Path, "home", return_value=Path("/h/zedowner")), \
                mock.patch.object(package.os, "listdir", side_effect=drive):
            kinds = " ".join(package.release(Path(temp) / "release")["leaks"])
        for why in ("library paper id stem: smith2020", "library vault name: secret_topic", "personal name: Zedowner",
                    "machine path: Q:\\zedfolder"):
            self.assertIn(why, kinds)
        self.assertNotIn("Program Files", kinds)
        if len(owner) >= 4:  # the scan reads the owner's name at run time; the shipped code never types it
            self.assertNotIn(owner, Path(package.__file__).read_text(encoding="utf-8").lower())

    def test_scan_flags_text_shared_with_a_library_sentence_of_45_characters_or_more(self):
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))  # never the real store
        sentence = "The  quartz lantern flickered over   the Harbour Wall at midnight."
        db.write_jsonl(config.PAPERS_DIR / "smith2020_real_paper_abcd" / "sentences.jsonl",
                       [{"sentence_id": "smith2020_real_paper_abcd:S0001", "text": sentence}])
        copied = "the quartz LANTERN flickered over the harbour wall at midnight"[:45]  # 45 characters, other case and spacing
        self.put("tests/test_copied.py", f"S = 'x {copied}!'\n")
        self.put("tests/test_short.py", f"S = '{copied[:44]}'\n")  # 44 characters: below the threshold
        self.put("tests/test_joined.py", f"S = ('{copied[:29]}'\n     '{copied[29:]}')\n")  # two literals, each under 45
        self.put("tests/test_fixture.py", "ID = 'smith_2020'\n")  # an id stem written the way fixtures do
        with tempfile.TemporaryDirectory() as temp:
            leaks = package.release(Path(temp) / "release")["leaks"]
            self.assertEqual(sorted(l for l in leaks if "library sentence text" in l),
                             ["tests/test_copied.py: library sentence text: line 1, 45 characters",
                              "tests/test_joined.py: library sentence text: line 1, 45 characters"])
            self.assertNotIn(copied.lower()[:20], " ".join(leaks).lower())  # the message never quotes the library
            self.assertIn("tests/test_fixture.py: library paper id stem: smith_2020", leaks)
            shutil.rmtree(config.PAPERS_DIR)  # a release copy has no store: the check does nothing
            self.assertEqual([l for l in package.release(Path(temp) / "release")["leaks"] if "library sentence" in l], [])


if __name__ == "__main__":
    unittest.main()
