"""Offline tests for configured-provider node and figure runners."""

import json
import hashlib
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from kbsys import analysis_runner, analysis_transport, config, db, figure_reading, media, node_cards, workflow, answer
from . import test_figure_reading
from . import test_node_cards
import os
import subprocess
import sys
import tempfile
import time
import unittest


@contextmanager
def held_lock(name):
    target = config.STORE / "locks" / (hashlib.sha256(name.encode()).hexdigest()[:20] + ".lock")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"pid": 1, "token": "test", "name": "%s"}' % name, encoding="utf-8")
    try:
        yield target
    finally:
        if target.is_file():
            target.unlink()


class NodeRunnerTests(test_node_cards.NodeCardTests):
    def test_busy_writer_uses_one_provider_call_and_reuses_reply(self):
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))
        with held_lock("library"), patch.object(analysis_transport, "call", return_value=(raw, {})) as call:
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli", max_attempts=3)
        self.assertEqual(result["status"], "blocked_by_writer")
        call.assert_called_once()
        saved = list((Path(self.temp.name) / "store").glob("ai_replies/node_cards/v/local_eg/*.json"))
        self.assertEqual(len(saved), 1)
        self.assertFalse(json.loads(saved[0].read_text(encoding="utf-8"))["processed"])

        with patch.object(analysis_transport, "call", side_effect=AssertionError("would pay twice")):
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli", max_attempts=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)

    def test_stale_saved_node_reply_is_not_submitted(self):
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))
        with held_lock("library"), patch.object(analysis_transport, "call", return_value=(raw, {})):
            self.assertEqual(node_cards.run(self.con, "v", "EG", provider="codex_cli")["status"],
                             "blocked_by_writer")
        folder = db.paper_dir("p")
        rows = list(db.read_jsonl(folder / "sentences.jsonl"))
        rows[0]["text"] += " Revised."
        db.write_jsonl(folder / "sentences.jsonl", rows)
        current = self.task()
        fresh = json.dumps(self.good_reply(current))
        with patch.object(analysis_transport, "call", return_value=(fresh, {})) as call:
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli")
        self.assertTrue(result["ok"])
        call.assert_called_once()

    def test_happy_path_saves_raw_reply_and_accepts(self):
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))
        with patch.object(analysis_transport, "call", return_value=(raw, {"input_tokens": 11, "output_tokens": 7})) as call:
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli", max_attempts=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["attempts"], 1)
        call.assert_called_once()
        saved = list((Path(self.temp.name) / "store").glob("ai_replies/node_cards/v/local_eg/*.json"))
        self.assertEqual(len(saved), 1)
        self.assertTrue(json.loads(saved[0].read_text(encoding="utf-8"))["processed"])

    def test_repair_turn_after_rejected_statement(self):
        payload = self.task()
        bad = self.good_reply(payload)
        bad["fields"]["definition"]["text"] = "EG increases survival by 10%."
        bad["fields"]["definition"]["hedge"] = "none"
        good = self.good_reply(payload)
        replies = iter([(json.dumps(bad), {}), (json.dumps(good), {"output_tokens": 9})])
        with patch.object(analysis_transport, "call", side_effect=lambda *args, **kwargs: next(replies)) as call:
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli", max_attempts=2)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 2)
        self.assertEqual(call.call_count, 2)
        second_user = call.call_args_list[1].args[2]
        self.assertIn("hedge_dropped", second_user)

    def test_unprocessed_saved_reply_is_reused_after_submit_crash(self):
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))
        with patch.object(analysis_transport, "call", return_value=(raw, {})), \
                patch.object(node_cards, "submit", side_effect=RuntimeError("simulated crash")):
            with self.assertRaises(RuntimeError):
                node_cards.run(self.con, "v", "EG", provider="codex_cli")
        with patch.object(analysis_transport, "call", side_effect=AssertionError("would pay twice")):
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli")
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)

    def test_writer_lock_is_not_held_during_provider_call(self):
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))
        state = {"held": False}

        @contextmanager
        def fake_lock(*args, **kwargs):
            state["held"] = True
            try:
                yield
            finally:
                state["held"] = False

        def provider(*args, **kwargs):
            self.assertFalse(state["held"])
            return raw, {}

        with patch.object(workflow, "writer_lock", fake_lock), patch.object(analysis_transport, "call", side_effect=provider):
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli")
        self.assertTrue(result["ok"])

    def test_real_library_lock_is_free_during_provider_call_and_held_during_write(self):
        import threading
        payload = self.task()
        raw = json.dumps(self.good_reply(payload))

        def busy_elsewhere():  # another thread = another writer; the lock is not reentrant across threads
            found = []
            def probe():
                try:
                    with workflow.writer_lock("library", timeout=0):
                        found.append(False)
                except workflow.WriterBusy:
                    found.append(True)
            thread = threading.Thread(target=probe)
            thread.start()
            thread.join()
            return found[0]

        seen = {"provider": [], "writes": []}
        real_atomic = workflow.atomic_json

        def provider(*args, **kwargs):
            seen["provider"].append(busy_elsewhere())
            return raw, {}

        def atomic(path, data, *args, **kwargs):
            seen["writes"].append((Path(path).parent.name, busy_elsewhere()))
            return real_atomic(path, data, *args, **kwargs)

        with patch.object(analysis_transport, "call", side_effect=provider),                 patch.object(workflow, "atomic_json", side_effect=atomic):
            result = node_cards.run(self.con, "v", "EG", provider="codex_cli")
        self.assertTrue(result["ok"])
        self.assertEqual(seen["provider"], [False])  # lock free while the AI works
        self.assertIn(True, [busy for _name, busy in seen["writes"]])  # card write held `library`
        self.assertFalse(busy_elsewhere())  # and released afterwards

    def test_each_call_is_logged_to_ai_usage_log(self):
        raw = json.dumps(self.good_reply(self.task()))
        usage = {"input_tokens": 11, "output_tokens": 7}
        with patch.object(answer, "_chat", return_value=(raw, usage)):
            self.assertTrue(node_cards.run(self.con, "v", "EG", provider="codex_cli")["ok"])
        rows = list(db.read_jsonl(config.LOGS_DIR / "ai_usage.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stage"], rows[0]["provider"], rows[0]["status"]), ("node_card", "codex_cli", "returned"))
        self.assertEqual(rows[0]["usage"], usage)
        self.assertEqual(set(rows[0]), {"time", "provider", "model", "stage", "input_chars", "request_sha256",
                                        "concept_id", "output_chars", "usage", "provider_usage", "status",
                                        "elapsed_seconds"})  # contract shape: no prompt text, no key
        report = analysis_runner.usage_report()
        self.assertEqual((report["calls"], report["calls_by_stage"]), (1, {"node_card": 1}))

    def test_interrupted_call_still_logs_a_failed_row(self):
        log = Path(self.temp.name) / "logs" / "interrupt.jsonl"
        with self.assertRaises(KeyboardInterrupt):
            analysis_transport.logged_call(lambda *_a: (_ for _ in ()).throw(KeyboardInterrupt()), log, "node_card",
                                           {"provider": "codex_cli"}, "s", "u")
        row = list(db.read_jsonl(log))[0]
        self.assertEqual((row["status"], row["error"], row["usage"]), ("failed", "KeyboardInterrupt", {}))

    def test_failed_call_is_logged_and_log_write_error_does_not_fail_run(self):
        with patch.object(answer, "_chat", side_effect=RuntimeError("boom")):
            self.assertEqual(node_cards.run(self.con, "v", "EG", provider="codex_cli")["status"], "failed")
        row = list(db.read_jsonl(config.LOGS_DIR / "ai_usage.jsonl"))[0]
        self.assertEqual((row["status"], row["error"], row["usage"]), ("failed", "RuntimeError", {}))
        blocker = Path(self.temp.name) / "not_a_dir"
        blocker.write_text("x", encoding="utf-8")
        raw = json.dumps(self.good_reply(self.task()))
        with patch.object(answer, "_chat", return_value=(raw, {})), \
                patch.object(config, "LOGS_DIR", blocker / "logs"):  # mkdir fails -> OSError, reported only
            self.assertTrue(node_cards.run(self.con, "v", "EG", provider="codex_cli")["ok"])  # a good card still saves

    def test_timeout_is_not_repaired_or_submitted(self):
        with patch.object(analysis_transport, "call",
                          side_effect=answer.ProviderTimeout("command", 1)) as call, \
                patch.object(node_cards, "submit") as submit:
            result = node_cards.run(self.con, "v", "EG", provider="command", max_attempts=3, timeout=1)
        self.assertEqual(result["status"], "provider_timeout")
        call.assert_called_once()
        submit.assert_not_called()

    def test_run_pending_continues_after_timeout_and_reports_count(self):
        rows = [{"name": "EG", "concept_id": "local:eg"}, {"name": "Other", "concept_id": "local:other"}]
        outcomes = [{"status": "provider_timeout"}, {"status": "accepted"}]
        with patch.object(node_cards, "pending", return_value=rows), \
                patch.object(node_cards, "run", side_effect=outcomes) as run:
            results = node_cards.run_pending(self.con, "v")
        self.assertEqual([row["status"] for row in results], ["provider_timeout", "accepted"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(results[-1]["run_summary"]["timeouts"], 1)


class FigureRunnerTests(test_figure_reading.FigureReadingTests):
    def test_busy_writer_uses_one_provider_call_and_reuses_reply(self):
        _folder, object_id, _crop = self.add_paper("paper")
        raw = json.dumps(self.reply())
        with held_lock("library"), patch.object(analysis_transport, "call", return_value=(raw, {})) as call:
            result = figure_reading.run("paper", object_id, provider="anthropic", max_attempts=3)
        self.assertEqual(result["status"], "blocked_by_writer")
        call.assert_called_once()
        saved = list((Path(self.temp.name) / "store").glob("ai_replies/figure_reading/paper/F1/*.json"))
        self.assertEqual(len(saved), 1)
        self.assertFalse(json.loads(saved[0].read_text(encoding="utf-8"))["processed"])

        with patch.object(analysis_transport, "call", side_effect=AssertionError("would pay twice")):
            result = figure_reading.run("paper", object_id, provider="anthropic", max_attempts=3)
        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 1)

    def test_stale_saved_figure_reply_is_not_submitted(self):
        folder, object_id, crop = self.add_paper("paper")
        raw = json.dumps(self.reply())
        with held_lock("library"), patch.object(analysis_transport, "call", return_value=(raw, {})):
            self.assertEqual(figure_reading.run("paper", object_id, provider="anthropic")["status"],
                             "blocked_by_writer")
        crop.write_bytes(crop.read_bytes() + b"new revision")
        manifest = db.read_json(folder / "media_manifest.json")
        manifest["bundles"][0]["crop"]["sha256"] = media._hash(crop)
        # media._read_manifest excludes output_sha256 from the digest.  Drop
        # the previous checksum before publishing the revised fixture.
        manifest.pop("output_sha256", None)
        manifest["output_sha256"] = media._digest(manifest)
        db.write_json(folder / "media_manifest.json", manifest)
        with patch.object(analysis_transport, "call", return_value=(raw, {})) as call:
            result = figure_reading.run("paper", object_id, provider="anthropic")
        self.assertTrue(result["ok"])
        call.assert_called_once()

    def test_crop_path_is_attached_to_provider_call(self):
        _folder, object_id, crop = self.add_paper("paper")
        with patch.object(analysis_transport, "call", return_value=(json.dumps(self.reply()), {})) as call:
            result = figure_reading.run("paper", object_id, provider="anthropic")
        self.assertTrue(result["ok"])
        self.assertEqual(call.call_args.kwargs["image_paths"], [crop])

    def test_call_is_logged_to_the_paper_usage_log(self):
        _folder, object_id, _crop = self.add_paper("paper")
        usage = {"input_tokens": 5, "output_tokens": 3}
        with patch.object(analysis_transport, "call", return_value=(json.dumps(self.reply()), usage)):
            self.assertTrue(figure_reading.run("paper", object_id, provider="anthropic")["ok"])
        rows = list(db.read_jsonl(db.paper_dir("paper") / "analysis_usage.jsonl"))
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["stage"], rows[0]["status"], rows[0]["usage"]), ("figure_reading", "returned", usage))
        self.assertEqual(analysis_runner.usage_report("paper")["calls_by_stage"], {"figure_reading": 1})

    def test_hash_mismatch_refuses_to_send_image(self):
        _folder, object_id, crop = self.add_paper("paper")
        crop.write_bytes(crop.read_bytes() + b"changed")
        with patch.object(analysis_transport, "call") as call:
            with self.assertRaisesRegex(ValueError, "SHA256 mismatch"):
                figure_reading.run("paper", object_id, provider="anthropic")
        call.assert_not_called()

    def test_writer_busy_is_typed_and_pending_stops_with_summary(self):
        self.add_paper("p1")
        self.add_paper("p2")
        with held_lock("library"):
            with self.assertRaises(workflow.WriterBusy) as caught:
                with workflow.writer_lock("library"):
                    pass
            self.assertIsInstance(caught.exception, ValueError)

            with patch.object(analysis_transport, "call", return_value=(json.dumps(self.reply()), {})) as call:
                results = figure_reading.run_pending(self.con, "vault", provider="anthropic")

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["status"], "blocked_by_writer")
        self.assertEqual(results[0]["result"]["run_summary"]["done"], 0)
        self.assertEqual(results[0]["result"]["run_summary"]["remaining"], 1)
        self.assertIn("wait for the lock", results[0]["result"]["run_summary"]["message"])
        call.assert_called_once()

    def test_timeout_is_not_repaired_or_submitted(self):
        _, object_id, _ = self.add_paper("paper")
        with patch.object(analysis_transport, "call",
                          side_effect=answer.ProviderTimeout("codex_cli", 1)) as call, \
                patch.object(figure_reading, "submit") as submit:
            result = figure_reading.run("paper", object_id, provider="codex_cli", max_attempts=3, timeout=1)
        self.assertEqual(result["status"], "provider_timeout")
        call.assert_called_once()
        submit.assert_not_called()

    def test_run_pending_continues_after_timeout_and_reports_count(self):
        rows = [{"paper_id": "p1", "object_id": "p1:F1"}, {"paper_id": "p2", "object_id": "p2:F1"}]
        outcomes = [{"status": "provider_timeout"}, {"status": "accepted"}]
        with patch.object(figure_reading, "pending", return_value=rows), \
                patch.object(figure_reading, "run", side_effect=outcomes) as run:
            results = figure_reading.run_pending(None, "vault")
        self.assertEqual([row["status"] for row in results], ["provider_timeout", "accepted"])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(results[-1]["run_summary"]["timeouts"], 1)
        self.assertIn("1 mục quá thời gian chờ nhà cung cấp AI", results[-1]["run_summary"]["message"])


def _pid_running(pid):
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}"], capture_output=True, text=True)
        return any(line.strip().startswith(str(pid) + " ") for line in result.stdout.splitlines())
    result = subprocess.run(["ps", "-p", str(pid), "-o", "pid="], capture_output=True, text=True)
    return bool(result.stdout.strip())


def _kill_pid(pid):
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass


class ProviderTimeoutTests(unittest.TestCase):
    def test_defaults_are_per_provider_and_explicit_timeout_wins(self):
        self.assertEqual(analysis_transport.provider_settings(provider="codex_cli")["timeout_seconds"], 900)
        self.assertEqual(analysis_transport.provider_settings(provider="anthropic")["timeout_seconds"], 300)
        self.assertEqual(analysis_transport.provider_settings(provider="command", timeout=1)["timeout_seconds"], 1)

    def test_command_timeout_kills_the_whole_process_tree(self):
        with tempfile.TemporaryDirectory() as temp:
            pid_file = Path(temp) / "child.pid"
            script = (
                "import pathlib, subprocess, sys, time\n"
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))\n"
                "time.sleep(60)\n"
            )
            settings = {"provider": "command", "timeout_seconds": 1,
                        "command": {"run": [sys.executable, "-c", script], "model": ""}}
            try:
                with self.assertRaises(answer.ProviderTimeout):
                    answer._command(settings, "system", "user")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and not pid_file.is_file():
                    time.sleep(0.05)
                self.assertTrue(pid_file.is_file(), "fake provider did not record its child PID")
                child_pid = int(pid_file.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline and _pid_running(child_pid):
                    time.sleep(0.05)
                self.assertFalse(_pid_running(child_pid), "provider timeout left a child process running")
            finally:
                if pid_file.is_file():
                    _kill_pid(int(pid_file.read_text(encoding="utf-8")))


if __name__ == "__main__":
    import unittest
    unittest.main()
