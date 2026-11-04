"""G6 U2: `kb.py ai-check` and `kb.py init` for a new user; the key example ships, the real key file never does."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import kb
from kbsys import answer, config, package

from . import test_core as fixtures

SECRET = "sk-test-SECRET-1234567890"


class FirstRunTests(unittest.TestCase):
    tearDown = fixtures.Demo3Tests.tearDown

    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        self.assertTrue(config.STORE.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()))
        patcher = patch.object(answer, "SECRETS", config.CONFIG_DIR / "secrets.yaml")   # SECRETS is fixed at import: point it at the temp root
        patcher.start()
        self.addCleanup(patcher.stop)

    def ai(self, provider, secret=SECRET):
        (config.CONFIG_DIR / "ai.yaml").write_text(f"ai:\n  provider: {provider}\n", encoding="utf-8")
        if secret:
            (config.CONFIG_DIR / "secrets.yaml").write_text(f"{provider}_api_key: {secret}\n", encoding="utf-8")

    def run_kb(self, *argv):
        out = io.StringIO()
        with patch.object(sys, "argv", ["kb.py", *argv]), patch.object(kb.config, "load", return_value={"ingest": {}}), \
                contextlib.redirect_stdout(out):
            kb.main()
        return json.loads(out.getvalue())

    def test_ai_check_sends_nothing_without_call_and_never_prints_the_key(self):
        self.ai("anthropic")
        with patch.object(answer, "_chat", return_value=("OK", {})) as chat:
            rows = self.run_kb("ai-check")
            self.assertEqual(chat.call_count, 0)
            self.assertEqual({r["task"] for r in rows}, set(answer.AI_TASKS))
            self.assertTrue(all(r["ready"] and r["key"] == "present" and "call" not in r for r in rows))
            self.assertNotIn(SECRET, json.dumps(rows))
            rows = self.run_kb("ai-check", "--call")
            self.assertEqual(chat.call_count, 1)   # all tasks share one provider+model: one call
            self.assertTrue(all(r["call"]["ok"] for r in rows))

    def test_ai_check_missing_key_is_not_called_and_an_error_hides_the_key(self):
        self.ai("anthropic", secret="")
        with patch.object(answer, "_chat") as chat:
            rows = self.run_kb("ai-check", "--call")
        self.assertEqual(chat.call_count, 0)
        self.assertTrue(all(not r["ready"] and r["key"] == "missing" for r in rows))
        self.ai("anthropic")
        with patch.object(answer, "_chat", side_effect=answer.AIError(f"anthropic trả lỗi 401 {SECRET}")):
            text = json.dumps(self.run_kb("ai-check", "--call"))
        self.assertNotIn(SECRET, text)
        self.assertIn('"ok": false', text)
        self.ai("mcp_agent", secret="")
        with patch.object(answer, "_chat") as chat:
            rows = self.run_kb("ai-check", "--call")
        self.assertEqual((chat.call_count, rows[0]["key"]), (0, "not needed"))

    def test_ai_check_masks_a_cut_key_and_asks_json_mode_providers_for_json(self):
        import urllib.error
        self.ai("deepseek")   # json_mode on by default: a prompt without "JSON" is refused by the provider
        sent = []

        def refuse(req, timeout):
            sent.append(req.data.decode("utf-8"))
            raise urllib.error.HTTPError(req.full_url, 401, "no", {}, io.BytesIO(("x" * 280 + "Bearer " + SECRET).encode()))
        with patch("urllib.request.urlopen", side_effect=refuse):
            text = json.dumps(self.run_kb("ai-check", "--call"), ensure_ascii=False)
        self.assertEqual(len(sent), 1)
        self.assertIn("json", json.loads(sent[0])["messages"][1]["content"].lower())
        self.assertFalse([SECRET[i:i + 6] for i in range(len(SECRET) - 5) if SECRET[i:i + 6] in text])
        self.ai("ollama", secret="")
        self.assertEqual({r["key"] for r in self.run_kb("ai-check")}, {"not needed"})   # optional key, not "missing"

    def test_vault_name_of_the_mother_vault_or_a_dot_path_is_refused_before_any_write(self):
        for name in ("Registry", "registry", ".."):
            with self.assertRaises(SystemExit):
                self.run_kb("init", "--vault", name)
        self.assertFalse((config.VAULTS_DIR / "Registry").exists())
        self.assertEqual(sorted(p.name for p in config.VAULTS_DIR.iterdir()), ["one", "two"])

    def test_init_is_idempotent_and_never_overwrites(self):
        first = self.run_kb("init", "--vault", "Tế bào", "--topic", "chủ đề thử")
        self.assertEqual(first["vault"]["status"], "created")
        home = config.vault_dir(first["vault"]["vault"]) / "Home.md"
        self.assertIn("chủ đề thử", home.read_text(encoding="utf-8"))
        home.write_text("MY OWN TEXT\n", encoding="utf-8")
        again = self.run_kb("init", "--vault", "Tế bào", "--topic", "khác")
        self.assertEqual(again["created"], [])
        self.assertIn("knowledge_store/library.db", again["existing"])
        self.assertEqual(again["vault"]["status"], "existing")
        self.assertEqual(home.read_text(encoding="utf-8"), "MY OWN TEXT\n")
        self.assertEqual(len(again["ai"]), len(answer.AI_TASKS))
        self.assertEqual(len(again["next"]), 2)
        self.assertEqual(self.run_kb("init")["vault"], None)

    def test_init_creates_the_store_folders_on_a_fresh_root(self):
        import shutil
        self.con.close()
        shutil.rmtree(config.STORE)
        shutil.rmtree(config.VAULTS_DIR)
        out = self.run_kb("init")
        self.assertIn("knowledge_store/library.db", out["created"])
        self.assertIn("knowledge_store", out["created"])   # main's db.connect() makes it before init looks
        self.assertIn("sources/inbox", out["created"])
        self.assertTrue((config.ROOT / "sources" / "inbox").is_dir() and config.PAPERS_DIR.is_dir())

    def test_release_ships_the_key_example_never_the_key_file(self):
        for name, text in (("secrets.example.yaml", 'anthropic_api_key: ""\n'), ("secrets.yaml", f"anthropic_api_key: {SECRET}\n")):
            (config.CONFIG_DIR / name).write_text(text, encoding="utf-8")
        self.assertFalse(package.NEVER.search("config/secrets.example.yaml"))
        self.assertTrue(package.NEVER.search("config/secrets.yaml"))
        with tempfile.TemporaryDirectory() as temp:
            dest = Path(temp) / "release"
            result = package.release(dest)
            self.assertTrue((dest / "config/secrets.example.yaml").is_file())
            self.assertFalse((dest / "config/secrets.yaml").exists())
            self.assertNotIn(SECRET, " ".join(p.read_text(encoding="utf-8", errors="ignore") for p in dest.rglob("*") if p.is_file()))
            self.assertEqual(result["leaks"], [])
        names = (Path(__file__).resolve().parents[1] / "config/secrets.example.yaml").read_text(encoding="utf-8")
        for p in answer.KEY_PROVIDERS:
            self.assertIn(f"{p}_api_key:", names)


if __name__ == "__main__":
    unittest.main()
