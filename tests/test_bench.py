"""kb.py ai-bench with a FAKE provider (answer._chat is replaced): scores, the repair path, and the real store never opened."""
import contextlib
import io
import json
from pathlib import Path
import re
import tempfile
import unittest
from unittest import mock

from kbsys import answer, bench, config, evidence, mesh, relations

GOLD = json.loads((bench.BENCH / "gold.json").read_text(encoding="utf-8"))
REAL_STORE = config.STORE


def sentences(pid):
    rows = [json.loads(line) for line in (bench.BENCH / pid / "sentences.jsonl").read_text(encoding="utf-8").splitlines()]
    return {r["sentence_id"].split(":")[1]: r["text"] for r in rows}


def unit(pid, short, hedge=True):
    text = sentences(pid)[short]
    if not hedge:   # drop the authors' hedge words, keep the finding
        text = re.sub(r" tended to be", " was", text)
        text = re.sub(r", which may indicate reduced apoptosis", "", text)
        text = text.replace("appear to be", "were").replace("may have had", "had")
    return {"unit_type": "result", "statement": text, "sentence_ids": [short],
            "outcome": {"direction": GOLD["papers"][pid]["results"][short]},
            "terms": {"molecules": ["Kelmorin" if "kelmorin" in pid else "Aldoran"]}}


def submission(pid, hedge=True):
    rows = sentences(pid)
    results = GOLD["papers"][pid]["results"]
    return {"classification": "CORE", "classification_reason": "Synthetic bench paper.", "evidence_type": "primary_research",
            "units": [unit(pid, s, hedge) for s in results],
            "skipped": [{"sentence_id": s, "reason": "not a result"} for s in rows if s not in results]}


class Fake:
    """A provider: mode 'perfect', 'drop_hedge', 'bad_first' (an invalid first submission, then a repair) or 'agree' (trap)."""

    def __init__(self, mode="perfect"):
        self.mode, self.seen, self.stores = mode, {}, []

    def __call__(self, s, system, user):
        self.stores.append(config.STORE)
        if system.startswith("You answer"):
            return json.dumps(self.answer(user)), {}
        pid = re.search(r"Paper id: (\S+)", user).group(1)
        self.seen[pid] = self.seen.get(pid, 0) + 1
        if self.mode == "bad_first":
            if self.seen[pid] == 1:
                data = submission(pid)
                data["units"][-1]["sentence_ids"] = ["S9999"]   # the last unit cites a sentence that does not exist
                return json.dumps(data), {}
            return json.dumps({"replace_units": [{"index": len(GOLD["papers"][pid]["results"]),
                                                  "unit": unit(pid, list(GOLD["papers"][pid]["results"])[-1])}]}), {}
        return json.dumps(submission(pid, hedge=self.mode != "drop_hedge")), {}

    def answer(self, user):
        def item(text):   # the [#n] of the evidence item that holds this text
            return next(int(m.group(1)) for m in re.finditer(r"\[#(\d+)\][^\n]*\nVerbatim: ([^\n]*)", user) if text in m.group(2))
        if "water" in user.split("Evidence items")[0]:
            if self.mode == "agree":
                n = item("water content")
                return {"answer": [{"text": f"Kelmorin làm giảm hàm lượng nước của lát thận [#{n}].", "evidence": [n]}],
                        "reasoning": [], "missing": []}
            return {"answer": [], "reasoning": [], "missing": ["Không tìm thấy bằng chứng rằng Kelmorin làm giảm hàm lượng nước."]}
        n = item("Viability after storage")
        return {"answer": [{"text": f"Sau khi bảo quản lạnh, khả năng sống cao hơn khi dùng Kelmorin [#{n}].", "evidence": [n]}],
                "reasoning": [], "missing": []}


class BenchTests(unittest.TestCase):
    def run_bench(self, mode):
        fake = Fake(mode)
        with mock.patch.object(answer, "_chat", side_effect=fake):
            result = bench.run(provider="ollama", model="fake", say=lambda _: None)
        self.assertEqual(config.STORE, REAL_STORE)   # patched paths are restored
        self.assertTrue(fake.stores and all(Path(tempfile.gettempdir()).resolve() in s.resolve().parents for s in fake.stores))
        return result

    def test_gold_hedged_sentences_are_hedged_for_the_validator(self):
        for pid, spec in GOLD["papers"].items():
            for short in spec["hedged"]:
                self.assertTrue(evidence.HEDGE.search(sentences(pid)[short]), short)

    def test_perfect_provider_scores_one_and_passes(self):
        result = self.run_bench("perfect")
        self.assertEqual({k: r["value"] for k, r in result["scores"].items()}, {k: 1.0 for k in result["scores"]})
        self.assertTrue(result["pass"], result)
        self.assertEqual(set(result["scores"]) - set(bench.targets()), {"sycophancy_ok"})
        self.assertIn("ĐẠT", bench.summary_vi(result))

    def test_dropping_a_hedge_fails_hedge_kept_only(self):
        result = self.run_bench("drop_hedge")
        self.assertEqual(result["scores"]["hedge_kept"]["value"], 0.0)
        self.assertFalse(result["scores"]["hedge_kept"]["pass"] or result["pass"])
        self.assertEqual(result["scores"]["sentence_match"]["value"], 1.0)

    def test_invalid_first_submission_then_valid(self):
        result = self.run_bench("bad_first")
        self.assertEqual(result["scores"]["first_try_pass"]["value"], 0.0)
        self.assertEqual(result["scores"]["within_3_attempts"]["value"], 1.0)
        self.assertFalse(result["pass"])

    def test_agreeing_with_the_trap_fails_sycophancy(self):
        result = self.run_bench("agree")
        self.assertEqual(result["scores"]["sycophancy_ok"]["value"], 0.0)
        self.assertFalse(result["details"]["trap_says_not_supported"])
        self.assertFalse(result["pass"])

    def test_stdout_is_only_json_and_a_failing_provider_fails_with_the_key_masked(self):
        key = "sk-TESTKEY0123456789abcdef"

        def failing(*args):
            raise answer.AIError("401 Incorrect API key provided: sk-TES***cdef " + key[:12])
        out = io.StringIO()
        with mock.patch.object(answer, "_secret", return_value=key), mock.patch.object(answer, "_chat", side_effect=failing), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            result = bench.run(provider="ollama", model="fake")   # default say
        self.assertEqual(out.getvalue(), "")
        self.assertFalse(result["pass"])
        text = json.dumps(result)
        self.assertNotIn(key[:12], text)
        self.assertFalse(any(key[i:i + 4] in text for i in range(len(key) - 3)), text)

    def test_unusable_replies_count_against_within_3(self):
        fake, junk = Fake("perfect"), {}

        def chat(s, system, user):   # three replies that are not JSON, then a perfect one: 4 calls, 1 submit
            pid = None if system.startswith("You answer") else re.search(r"Paper id: (\S+)", user).group(1)
            if pid and junk.get(pid, 0) < 3:
                junk[pid] = junk.get(pid, 0) + 1
                return "not json", {}
            return fake(s, system, user)
        with mock.patch.object(answer, "_chat", side_effect=chat):
            result = bench.run(provider="ollama", model="fake", say=lambda _: None)
        self.assertEqual(result["scores"]["within_3_attempts"]["value"], 0.0, result["details"]["papers"])

    def test_import_bound_store_paths_point_into_the_temp_store(self):
        seen = []

        def chat(s, system, user):
            seen.append((relations.RELATIONS, relations.MEASURE_GROUPS, mesh.MESH_DB))
            return Fake("perfect")(s, system, user)
        with mock.patch.object(answer, "_chat", side_effect=chat):
            bench.run(provider="ollama", model="fake", say=lambda _: None)
        self.assertTrue(seen and all(Path(tempfile.gettempdir()).resolve() in p.resolve().parents and "demo3-bench-" in str(p)
                                     for row in seen for p in row))

    def test_not_ready_provider_stops_before_any_call(self):
        with mock.patch.object(answer, "_chat") as chat, self.assertRaises(answer.AIError):
            bench.run(provider="mcp_agent", say=lambda _: None)
        chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
