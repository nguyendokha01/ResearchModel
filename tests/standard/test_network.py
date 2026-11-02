"""Edges of the NW / SF checks that the generic PASS / FAIL cases do not cover.

The clauses listed in RED have a PASS case that reports violations today; the tests here hand them a judge that does what the
clause asks (cases_network.good_prepare and friends) and expect no violation, so the check can be satisfied and G4 knows what to
build. Each detector is also shown a judge that is wrong in exactly one way. A crashing judge must be a violation, never a PASS.
The probes must leave the real project untouched, and the real-store half (gold file) must yield nothing when the file is absent.
"""
import contextlib
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kbsys import config, conform, discussions, mcp_server, neural, workflow
from kbsys.conform import Store

from . import cases_network as cases
from .cases_intake import put

conform.load_checks()
found = conform.violations
needs_bench = unittest.skipUnless(cases.HAS_BENCH, "scripts/benchmark_research.py is not shipped in a release")


def boom(*args, **kwargs):
    raise ZeroDivisionError("boom")


def content_profile(vault, node, concepts_sha):
    """A node-profile fingerprint of the paper files' content (not their modification time)."""
    folder = config.PAPERS_DIR / node["paper_id"]
    return hashlib.sha256(b"".join(p.read_bytes() for p in sorted(folder.iterdir()) if p.is_file())).hexdigest()


def cleaning_prepare(*args, sessions_too=False, **kwargs):
    """neural.prepare that first removes plain tickets older than a day (with `sessions_too` open research sessions too,
    which NW-07 forbids)."""
    for path in (config.CACHE_DIR / "neural_sessions").glob("*.json"):
        if path.stat().st_mtime < 86400 and (sessions_too or not path.name.startswith("session-")):
            path.unlink()
    return neural.prepare(*args, **kwargs)


def keeping_prepare(*args, **kwargs):
    """neural.prepare that never removes an old ticket."""
    with mock.patch.object(neural, "_clean_tickets", lambda: None):
        return neural.prepare(*args, **kwargs)


class Crashes(unittest.TestCase):
    def test_a_judge_that_crashes_is_a_violation_not_a_pass(self):
        store = cases.store(Path(tempfile.gettempdir()) / "conform-none")
        calls = {"NW-01": (store, boom), "NW-02": (store, boom), "NW-03": (boom,), "NW-04": (boom,), "NW-05": (boom, ("neural_path",)),
                 "NW-06": (boom,), "NW-07": (boom, boom), "NW-08": (boom,), "NW-09": (store, boom), "SF-01": (boom,),
                 "SF-02": (boom,), "SF-05": (store, boom), "SF-06": (boom,)}
        for cid, args in calls.items():
            with self.subTest(cid):
                if cid in cases.NEEDS_BENCH and not cases.HAS_BENCH:
                    self.skipTest("benchmark_research is not shipped")
                got = found(cid, *args)
                self.assertTrue(got and all("boom" in v or "crash" in v for v in got), got)


class GoodJudges(unittest.TestCase):
    """Wrappers that satisfy the RED clauses."""

    def test_budget_ranking_labels_translation_and_gold_with_the_good_prepare(self):
        store = cases.store(Path(tempfile.gettempdir()) / "conform-none")
        for cid in ("NW-01", "NW-02", "NW-09"):
            self.assertEqual(found(cid, store, cases.good_prepare), [], cid)
        for cid in ("NW-03", "NW-08"):
            self.assertEqual(found(cid, cases.good_prepare), [], cid)

    def test_hops_and_path_tool_with_the_contract_the_clause_asks(self):
        self.assertEqual(found("NW-04", cases.hop_search), [])
        self.assertEqual(found("NW-05", cases.path_tool, ("neural_walk",)), [])

    def test_ticket_fingerprint_of_content_and_old_tickets_cleaned(self):
        self.assertEqual(found("NW-07", content_profile, neural.dependency_fingerprint, cleaning_prepare), [])


class Detectors(unittest.TestCase):
    """Each broken judge is caught for its own reason."""

    @needs_bench
    def test_nw02_missing_must_sentence_and_duplicated_fields(self):
        got = found("NW-02", cases.store(Path("none")), cases.tamper(cases.good_prepare, lambda r: {**r, "items": [], "media": [1], "figures": [1]}))
        self.assertTrue(any("not among" in v for v in got), got)
        self.assertTrue(any("media is repeated as figures" in v for v in got), got)

    def test_nw04_index_note_reached_and_no_second_hop(self):
        leak = lambda vault, query, hops=0, **kw: {"nodes": [{"path": p} for p in (["Seed.md", "Leak.md"] if hops else ["Seed.md"])]}
        self.assertTrue(any("index note" in v for v in found("NW-04", leak)))
        for leaked, text in (("Concepts/NavOnly.md", "navigation"), ("Concepts/AHub.md", "hub note AHub.md")):
            judge = lambda vault, query, hops=0, leaked=leaked, **kw: cases.hop_search(vault, query, hops) | (
                {"nodes": cases.hop_search(vault, query, hops)["nodes"] + [{"path": leaked}]} if hops else {})
            self.assertTrue(any(text in v for v in found("NW-04", judge)), leaked)
        got = found("NW-04", cases.flat_search)
        self.assertTrue(any("does not reach Far.md" in v for v in got) and any("no more notes" in v for v in got), got)

    def test_nw05_each_part_of_the_path_contract(self):
        def variant(edit):
            return lambda con, cfg, name, args: edit(cases.path_tool(con, cfg, name, args), args)
        long_path = variant(lambda r, a: {**r, "steps": r["steps"] + [{"source": "Cterm", "target": "L0", "sentence_ids": ["pa:S0004"]}]} if r["found"] else r)
        no_sentence = variant(lambda r, a: {**r, "steps": [dict(s, sentence_ids=[]) for s in r["steps"]]} if r["found"] else r)
        invented = variant(lambda r, a: {**r, "steps": [dict(s, sentence_ids=["pa:S9999"]) for s in r["steps"]]} if r["found"] else r)
        always = variant(lambda r, a: {"found": True, "steps": cases.path_tool(None, None, "x", {"source": "Aterm", "target": "Cterm"})["steps"]})
        malformed = variant(lambda r, a: {"path": []})
        no_hub = variant(lambda r, a: {"found": False, "steps": []} if a["source"] == "Xterm" else r)
        bare = variant(lambda r, a: {**r, "steps": [{k: v for k, v in s.items() if k not in ("etypes", "unit_id")} for s in r["steps"]]})
        for judge, expect in ((long_path, "at most"), (no_sentence, "no stored source sentence"), (invented, "no stored source sentence"),
                              (no_hub, "through the hub note"), (bare, "edge types and unit id"),
                              (always, "instead of not found"), (malformed, "did not return found")):
            with self.subTest(expect):
                self.assertTrue(any(expect in v for v in found("NW-05", judge, ("neural_path",))), found("NW-05", judge, ("neural_path",)))
        self.assertTrue(any("through a paper note" in v for v in found("NW-05", cases.paper_note_path, ("neural_path",))))
        self_path = lambda con, cfg, name, args: {"found": False, "steps": []} if args["source"] == args["target"] else cases.path_tool(con, cfg, name, args)
        self.assertTrue(any("to itself" in v for v in found("NW-05", self_path, ("neural_path",))))
        results_end = lambda con, cfg, name, args: {"found": False, "steps": []} if args["target"].startswith("Results/") else cases.path_tool(con, cfg, name, args)
        self.assertTrue(any("Results note as endpoint" in v for v in found("NW-05", results_end, ("neural_path",))))
        self.assertTrue(any("no neural_path" in v for v in found("NW-05", cases.path_tool, ())))

    def test_nw06_a_caller_supplied_item_list_replaces_the_snapshot(self):
        got = found("NW-06", cases.replacing_check)
        self.assertEqual(len(got), 1, got)
        self.assertIn("replaces the snapshot", got[0])

    def test_nw07_touch_content_change_and_old_ticket_are_each_seen(self):
        self.assertEqual(found("NW-07", neural._profile_fingerprint, neural.dependency_fingerprint, neural.prepare), [])
        deaf = found("NW-07", content_profile, cases.deaf_dependency, neural.prepare)
        self.assertEqual(len(deaf), 1, deaf)
        self.assertIn("does not change the source/profile/media", deaf[0])
        mtime = lambda vault, node, concepts_sha: (config.PAPERS_DIR / node["paper_id"] / "sentences.jsonl").stat().st_mtime_ns
        self.assertTrue(any("touching a file" in v and "node profile" in v for v in found("NW-07", mtime, neural.dependency_fingerprint, neural.prepare)))
        self.assertTrue(any("1970 is still" in v for v in found("NW-07", neural._profile_fingerprint, neural.dependency_fingerprint, keeping_prepare)))
        cleaned = found("NW-07", neural._profile_fingerprint, neural.dependency_fingerprint,
                        lambda *a, **k: cleaning_prepare(*a, sessions_too=True, **k))
        self.assertEqual(cleaned, ["an open research session last used in 1970 was cleaned by a new prepare"])

    def test_nw08_an_english_question_flagged_for_translation(self):
        wrong = lambda con, cfg, vault, question, hops=0, **kw: {**cases.good_prepare(con, cfg, vault, question, hops), "needs_translation": True}
        self.assertEqual(sum("English question" in v for v in found("NW-08", wrong)), 1)

    @needs_bench
    def test_nw09_the_benchmarks_own_scoring_is_checked(self):
        import benchmark_research as bench
        real = bench.gold_metrics
        with mock.patch.object(bench, "gold_metrics", lambda *a, **k: {**real(*a, **k), "idea_coverage": 1.0}):
            got = found("NW-09", cases.store(Path("none")), cases.good_prepare)
        self.assertTrue(any("scores a known case wrong" in v for v in got), got)

    def test_sf01_each_argument_rule(self):
        def variant(name):
            real = mcp_server._check_args

            def judge(con, tool, args):
                if name == "no_clamp":
                    return real(con, tool, {k: v for k, v in args.items() if k != "limit"})  # limit left as it came
                if name == "any_vault":
                    return real(con, tool, {k: v for k, v in args.items() if k != "vault"})
                if name == "any_paper":
                    return real(con, tool, {k: v for k, v in args.items() if k != "paper_id"})
                if name == "same_cap":
                    args2 = dict(args)
                    real(con, tool, args2)
                    if "limit" in args2:
                        args["limit"] = 1000 if args2["limit"] > 1 else args2["limit"]  # a clamp above both caps
                    return None
            return judge
        for name, expect in (("no_clamp", "not a positive number"), ("any_vault", "vault"), ("any_paper", "paper_id"), ("same_cap", "above the cap")):
            with self.subTest(name):
                got = found("SF-01", variant(name))
                self.assertTrue(any(expect in v for v in got), got)

    def test_sf02_a_racy_save_makes_fewer_files(self):
        def racy(vault, title, markdown, model=None, kind="discussion"):
            folder = config.vault_dir(vault) / "Discussions"
            folder.mkdir(exist_ok=True)
            n = 1
            while (folder / f"{title}{'' if n == 1 else f' ({n})'}.md").exists():
                n += 1
            time.sleep(0.05)  # the gap in which another save takes the same name
            (folder / f"{title}{'' if n == 1 else f' ({n})'}.md").write_text(markdown, encoding="utf-8")
            return {"saved": f"Discussions/{title}.md"}
        got = found("SF-02", racy)
        self.assertTrue(any("concurrent saves" in v for v in got), got)
        self.assertTrue(any("save into _Maps" in v for v in got), got)

    def test_sf05_a_private_file_and_a_library_id_that_is_not_reported(self):
        got = found("SF-05", cases.store(Path("none")), cases.leaky_release)
        self.assertTrue(any("not reported as a leak" in v for v in got), got)
        self.assertTrue(any("secrets.yaml is shipped" in v for v in got), got)
        self.assertTrue(any("contains the API key" in v for v in got), got)

    def test_sf06_a_lock_that_is_never_released(self):
        @contextlib.contextmanager
        def stuck(name="library", timeout=0):
            target = config.STORE / "locks" / (hashlib.sha256(name.encode()).hexdigest()[:20] + ".lock")
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise workflow.WriterBusy("busy")
            target.write_text("{}")
            yield  # the file is never removed
        got = found("SF-06", stuck)
        self.assertTrue(any("not released" in v for v in got), got)
        self.assertFalse(any("takes the library lock" in v for v in got), got)

    def test_sf07_each_detector(self):
        def scan(tmp, **files):
            root = cases.project(tmp)
            for name, text in files.items():
                put(root / name.replace("__", "/"), text)
            return found("SF-07", root)
        with tempfile.TemporaryDirectory() as t:
            self.assertEqual(found("SF-07", cases.project(Path(t))), [])
        for name, files, expect in (
                ("socket bind", {"scripts__net.py": "import socket\ns = socket.socket()\ns.bind(('', 5))\n"}, "not bound to a literal local address"),
                ("keyword host", {"scripts__net2.py": "from http.server import HTTPServer\nHTTPServer(None, server_address=(HOST, 8))\n"}, "HOST"),
                ("0.0.0.0 in a .bat", {"2 - Open.bat": "python web.py --host 0.0.0.0\n"}, "opens a listener"),
                ("google key", {"config__x.yaml": "key: AIza" + "A" * 35 + "\n"}, "API key"),
                ("quoted key assignment", {"scripts__k.py": "api_key = '" + "a1b2c3d4e5" * 3 + "'\n"}, "API key"),
                ("undeclared import", {"scripts__i.py": "import requests\n"}, "imports requests"),
                ("no .gitignore line", {".gitignore": "cache/\n"}, ".gitignore")):
            with self.subTest(name), tempfile.TemporaryDirectory() as t:
                got = scan(Path(t), **files)
                self.assertTrue(any(expect in v for v in got), got)
        with tempfile.TemporaryDirectory() as t:  # the secrets file itself and an import of a local module are fine
            root = cases.project(Path(t))
            put(root / "config" / "secrets.yaml", "deepseek_api_key: " + "k" * 40 + "\n")
            put(root / "scripts" / "uses_local.py", "import web\nimport yaml\n")
            self.assertEqual(found("SF-07", root), [])


class RealStoreHalf(unittest.TestCase):
    """The gold file is read from the project folder of the Store; absent = nothing is reported by that half."""
    GOLD = {
        "V1": {"vault": "v", "type": "a_single_paper_factual", "question": "q1", "must": ["a:S1", "a:S2"], "queries": []},
        "V3": {"vault": "v", "type": "c_multihop", "question": "q3", "must": ["a:S1"], "queries": [],
               "chain": [{"link": f"l{i}", "status": "x", "sentences": [f"a:S{i}"]} for i in range(1, 6)] + [{"link": "none", "status": "x", "sentences": []}]},
        "V5": {"vault": "v", "type": "e_absence", "question": "q5", "must": [], "distractors": ["d:S1"], "queries": []},
    }

    def make(self, tmp):
        put(tmp / "runtime" / "audit_2026-10-16" / "D_gold.json", json.dumps(self.GOLD))
        return Store(root=tmp / "knowledge_store", paper_ids=[], vaults=[])

    @staticmethod
    def judge(rank_late=False, padding=0, confident_absence=False, chain=5):
        def run(con, cfg, vault, question, hops=0, speed="fast"):
            ids = {"q1": ["a:S1", "a:S2"], "q3": [f"a:S{i}" for i in range(1, chain + 1)], "q5": ["d:S1"] if confident_absence else []}[question]
            if rank_late and question == "q1":
                ids = [f"x:S{i}" for i in range(9)] + ids
            return {"items": [{"id": i, "sentence_ids": [i]} for i in ids], "prompt": "p" * 400, "not_found": not ids, "pad": "z" * padding}
        return run

    def test_no_gold_file_means_no_violation_from_the_real_half(self):
        with tempfile.TemporaryDirectory() as t:
            store = cases.store(Path(t))
            self.assertEqual([v for cid in ("NW-01", "NW-02", "NW-09") for v in found(cid, store, cases.good_prepare) if v.startswith("gold")], [])

    @needs_bench
    def test_a_gold_run_meeting_every_target_reports_nothing_and_each_miss_is_named(self):
        with tempfile.TemporaryDirectory() as t:
            store = self.make(Path(t))
            from kbsys.conform_network import _gold_problems, _gold_rows
            rows = _gold_rows(store, self.judge())
            self.assertEqual(len(rows), 6)  # 3 questions x fast and deep
            self.assertEqual(_gold_problems(rows, ("budget", "rank", "absence", "chain")), [])
            for label, judge, expect in (("late rank", self.judge(rank_late=True), "median rank"), ("payload", self.judge(padding=300_000), "payload"),
                                         ("absence", self.judge(confident_absence=True), "absence question"), ("chain", self.judge(chain=2), "chain links"),
                                         ("crash", boom, "prepare crashed")):
                with self.subTest(label):
                    got = _gold_problems(_gold_rows(store, judge), ("budget", "rank", "absence", "chain"))
                    self.assertTrue(any(expect in v for v in got), got)

    @needs_bench
    def test_the_real_run_writes_no_ticket_and_restores_what_it_patched(self):
        original = (workflow.atomic_json, neural.refresh)
        with tempfile.TemporaryDirectory() as t:
            from kbsys.conform_network import _gold_rows
            seen = []

            def judge(con, cfg, vault, question, hops=0, speed="fast"):
                seen.append((workflow.atomic_json is original[0], neural.refresh is original[1]))
                return self.judge()(con, cfg, vault, question, hops, speed)
            _gold_rows(self.make(Path(t)), judge)
            self.assertEqual(set(seen), {(False, False)})  # switched off while the run goes
            _gold_rows(self.make(Path(t) / "again"), boom)
        self.assertEqual((workflow.atomic_json, neural.refresh), original)


class RealProject(unittest.TestCase):
    def test_probes_leave_the_real_project_untouched(self):
        before = (config.ROOT, config.STORE, config.DB_PATH, config.VAULTS_DIR, config.CACHE_DIR, workflow.atomic_json, neural.refresh)
        sessions = config.CACHE_DIR / "neural_sessions"
        listing = sorted(p.name for p in sessions.glob("*")) if sessions.is_dir() else None
        threads = threading.active_count()
        for cid in ("NW-04", "NW-06", "NW-07", "SF-01", "SF-02", "SF-06"):
            found(cid)
        self.assertEqual(before, (config.ROOT, config.STORE, config.DB_PATH, config.VAULTS_DIR, config.CACHE_DIR, workflow.atomic_json, neural.refresh))
        self.assertEqual(listing, sorted(p.name for p in sessions.glob("*")) if sessions.is_dir() else None)
        self.assertEqual(threads, threading.active_count())

    def test_the_real_project_passes_the_static_scan_it_runs_on_itself(self):
        # the planted-violation tests above show the scan sees a listener, a key and an import; here it must find none in the code
        self.assertEqual(found("SF-07"), [])


if __name__ == "__main__":
    unittest.main()
