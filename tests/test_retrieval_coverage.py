"""Regression tests for exact-term Results coverage in answer retrieval."""
import unittest
from unittest import mock

from . import test_core as fixtures
from kbsys import agent_tasks, answer, config, db, find


class RetrievalCoverageTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def _add_paper(self, paper_id, text, evidence=None):
        folder = db.paper_dir(paper_id)
        row = {
            "sentence_id": f"{paper_id}:S0001", "seq": 1, "passage_id": f"{paper_id}:0001",
            "page": 2, "section": "results", "subsection": None, "kind": "text", "source": "main",
            "text": text, "start": 0, "end": len(text), "citations": [], "mentions": [],
        }
        meta = {
            "paper_id": paper_id, "title": paper_id, "year": 2026, "vaults": ["one"],
            "authors": [], "source_files": [], "evidence_type": "primary_research", "extraction_version": 2,
        }
        db.write_json(folder / "metadata.json", meta)
        db.write_jsonl(folder / "sentences.jsonl", [row])
        db.write_jsonl(folder / "passages.jsonl", [{"passage_id": row["passage_id"], "seq": 1,
                       "page": 2, "section": "results", "kind": "text", "text": text}])
        db.write_jsonl(folder / "evidence.jsonl", evidence or [])
        db.load_paper(self.con, paper_id)

    def test_conflicting_results_reach_fast_and_deep_answer_items(self):
        # many papers whose cited_statement units repeat the query fill the 12-item page;
        # the only paper with the opposite result has just a plain Results sentence.
        for i in range(13):
            sentence = f"H3K9me3 intensity stayed flat after vitrification in embryos ({i})."
            pid = f"unit_paper{i:02d}"
            self._add_paper(pid, sentence, [{
                "unit_id": f"{pid}:E0001", "paper_id": pid, "seq": 1, "unit_type": "result",
                "status": "ok", "statement": "H3K9me3 stayed flat after vitrification in embryos.",
                "quote": sentence, "sentence_ids": [f"{pid}:S0001"], "pages": [2], "sections": ["results"],
                "model": {}, "intervention": {}, "outcome": {}, "terms": {},
            }])
        for i in range(25):  # a term found in most papers would identify nothing
            self._add_paper(f"other_paper{i:02d}", "Oocytes were cultured in medium for two hours.")
        self._add_paper("plain_paper", "Imaging showed a clear rise in H3K9me3 intensity.")
        self.con.commit()

        question = "Does vitrification alter H3K9me3 in embryos?"
        for speed in ("fast", "deep"):
            with self.subTest(speed=speed):
                result = answer.answer(self.con, config.load(), question, speed=speed, use_ai=False)
                self.assertLessEqual(len(result["items"]), 45 if speed == "deep" else 12)
                self.assertIn("plain_paper:S0001", [item["id"] for item in result["items"]])
                # the best-ranked hits keep their places
                self.assertTrue(result["items"][0]["id"].startswith("unit_paper"))

    def test_generic_query_does_not_promote_unmatched_results(self):
        self._add_paper("unit_paper", "H3K9me3 intensity stayed flat after vitrification in embryos.", [{
            "unit_id": "unit_paper:E0001", "paper_id": "unit_paper", "seq": 1, "unit_type": "result",
            "status": "ok", "statement": "H3K9me3 stayed flat.",
            "quote": "H3K9me3 intensity stayed flat after vitrification in embryos.",
            "sentence_ids": ["unit_paper:S0001"], "pages": [2], "sections": ["results"],
            "model": {}, "intervention": {}, "outcome": {}, "terms": {},
        }])
        for i in range(25):  # a term found in most papers would identify nothing
            self._add_paper(f"other_paper{i:02d}", "Oocytes were cultured in medium for two hours.")
        self._add_paper("plain_paper", "H3K9me3 intensity increased in embryos.")
        self.con.commit()

        result = find.search(self.con, config.load(), "vitrification", vault="one", semantic=False, limit=12)
        self.assertEqual({hit["paper_id"] for hit in result["hits"]}, {"unit_paper"})


class RetrievalRankingTests(unittest.TestCase):
    """RT-02 (OR ranking), RT-06 (unknown vault), RT-07 (fair collect), RT-08 (stable paging)."""
    def setUp(self):
        fixtures.Demo3Tests.setUp(self)
        patcher = mock.patch.object(answer, "ANSWER_CACHE", self.root / "answers")   # the traces of the asks stay in the temp project
        patcher.start()
        self.addCleanup(patcher.stop)
    tearDown = fixtures.Demo3Tests.tearDown

    def _paper(self, pid, sentences, vault="one", units=()):
        folder = db.paper_dir(pid)
        rows = [{"sentence_id": f"{pid}:S{i:04d}", "seq": i, "passage_id": f"{pid}:{i:04d}", "page": 1, "section": section,
                 "subsection": None, "kind": "text", "source": "main", "text": text, "start": 0, "end": len(text),
                 "citations": [], "mentions": []} for i, (section, text) in enumerate(sentences, 1)]
        db.write_json(folder / "metadata.json", {"paper_id": pid, "title": pid, "year": 2026, "vaults": [vault], "authors": [],
                                                 "source_files": [], "evidence_type": "primary_research", "extraction_version": 2})
        db.write_jsonl(folder / "sentences.jsonl", rows)
        db.write_jsonl(folder / "passages.jsonl", [{"passage_id": r["passage_id"], "seq": r["seq"], "page": 1,
                                                    "section": r["section"], "kind": "text", "text": r["text"]} for r in rows])
        db.write_jsonl(folder / "evidence.jsonl", list(units))
        db.load_paper(self.con, pid)
        self.con.commit()

    def search(self, query, **kw):
        return find.search(self.con, config.load(), query, vault=kw.pop("vault", "one"), semantic=False, **kw)

    def test_rt02_or_mode_ranks_by_group_count_even_with_a_small_cap(self):
        self._paper("full", [("results", "Alphaterm and betaterm and gammaterm were examined together in one long sentence "
                                         "about many unrelated things such as medium, washing, storage and plates.")])
        for i in range(3):
            self._paper(f"two{i}", [("results", f"Betaterm gammaterm other words number {i}.")])
            self._paper(f"one{i}", [("results", "Alphaterm.")])
        counts = {"full": 3, "two": 2, "one": 1}
        for kw in ({}, {"cap": 3}):
            result = self.search("alphaterm betaterm gammaterm", **kw)
            ranked = [counts[h["paper_id"].rstrip("0123456789")] for h in result["hits"]]
            self.assertEqual(result["mode"], "or")
            self.assertEqual(result["hits"][0]["paper_id"], "full", kw)
            self.assertEqual(ranked, sorted(ranked, reverse=True), kw)

    def test_rt02_and_mode_ranking_is_unchanged(self):
        self._paper("short", [("results", "Alphaterm betaterm.")])
        for i in range(20):   # AND mode needs 20 hits; otherwise the search falls back to OR
            self._paper(f"long{i:02d}", [("results", f"Alphaterm and betaterm in a long sentence {i} about plates, medium, washing and storage.")])
        result = self.search("alphaterm betaterm")
        self.assertEqual(result["mode"], "and")
        self.assertEqual(result["hits"][0]["paper_id"], "short")
        self.assertEqual({h["matched_terms"] for h in result["hits"]}, {2})

    def test_rt06_unknown_vault_raises_and_other_scopes_work(self):
        self._paper("pa", [("results", "Alphaterm was higher.")], vault="one")
        self._paper("pb", [("results", "Alphaterm was lower.")], vault="two")
        with self.assertRaises(ValueError):
            self.search("alphaterm", vault="no_such_vault")
        with self.assertRaises(ValueError):
            self.search("alphaterm", vault="../one")
        self.assertEqual({h["paper_id"] for h in self.search("alphaterm", vault="two")["hits"]}, {"pb"})
        self.assertEqual({h["paper_id"] for h in self.search("alphaterm", vault=None, all_vaults=True)["hits"]}, {"pa", "pb"})
        self.assertEqual({h["paper_id"] for h in self.search("alphaterm", vault="no_such_vault", all_vaults=True)["hits"]}, {"pa", "pb"})

    def test_rt07_each_query_keeps_its_top_items_and_one_query_is_unchanged(self):
        for i in range(6):
            self._paper(f"a{i}", [("results", f"Alphaterm was higher in group {i}.")])
        for i in range(2):
            self._paper(f"b{i}", [("results", f"Betaterm gammaterm were measured in group {i}.")])
        cfg, queries = config.load(), ["alphaterm", "betaterm gammaterm"]
        items, runs = answer.collect(self.con, cfg, queries, "one", False, {}, 4)
        self.assertEqual(len(items), 4)
        self.assertEqual(len(runs), 2)
        for query in queries:
            for hit in find.search(self.con, cfg, query, vault="one", semantic=False, limit=4)["hits"][:2]:
                self.assertIn(hit["id"], [i["id"] for i in items])
        single, _ = answer.collect(self.con, cfg, ["alphaterm"], "one", False, {}, 4)
        own = sorted(find.search(self.con, cfg, "alphaterm", vault="one", semantic=False, limit=4)["hits"], key=lambda h: -h["score"])
        self.assertEqual([i["id"] for i in single], [h["id"] for h in own])

    def test_rt07_a_flagged_unit_keeps_the_sentences_that_matched(self):
        rows = [("results", f"Sentence number {i} is about plates.") for i in range(1, 5)] + [("results", "Deltaterm was higher in group C.")]
        unit = {"unit_id": "u:E0001", "paper_id": "u", "seq": 1, "unit_type": "result", "status": "flagged", "statement": rows[0][1],
                "quote": " ".join(t for _, t in rows), "sentence_ids": [f"u:S{i:04d}" for i in range(1, 6)], "pages": [1],
                "sections": ["results"], "hedging": "none", "analyst": "test/analyst", "model": {}, "intervention": {}, "outcome": {}}
        self._paper("u", rows, units=[unit])
        items, _ = answer.collect(self.con, config.load(), ["deltaterm"], "one", False, {}, 12)
        self.assertEqual([i["id"] for i in items], ["u:S0005"])
        # nothing matches the groups, or no groups are given: the first three sentences, as before
        hit = {"kind": "unit", "id": "u:E0001", "paper_id": "u", "status": "flagged", "score": 1,
               "sentence_ids": unit["sentence_ids"]}
        self.assertEqual([i["id"] for i in answer.source_items(self.con, [hit])], ["u:S0001", "u:S0002", "u:S0003"])
        self.assertEqual([i["id"] for i in answer.source_items(self.con, [hit], [["zzzterm"]])], ["u:S0001", "u:S0002", "u:S0003"])

    def _pages(self, query, limit):
        everything = self.search(query, limit=50)["hits"]
        pages = [self.search(query, limit=limit, offset=o)["hits"] for o in range(0, len(everything) + limit, limit)]
        return [h["id"] for h in everything], [h["id"] for page in pages for h in page]

    def test_rt08_paging_with_a_typed_identity_term_repeats_and_skips_nothing(self):
        for i in range(20):
            self._paper(f"q{i}", [("results", f"Cells were cultured in medium {i}.")])
        for i in range(10):
            self._paper(f"t{i}", [("results", f"ZK12me3 was higher in sample {i}.")])
        self._paper("f", [("results", "ZK12me3 was measured."), ("funding", "ZK12me3 work was funded by a grant.")])
        everything, paged = self._pages("ZK12me3", 8)
        self.assertEqual(len(paged), len(set(paged)))
        self.assertEqual(set(paged), set(everything))
        self.assertNotIn("f:S0002", everything)

    def test_rt08_a_page_of_one_to_three_with_a_typed_identity_term_does_not_crash(self):
        for i in range(5):
            self._paper(f"q{i}", [("results", f"Cells were cultured in medium {i}.")])
            self._paper(f"t{i}", [("results", f"ZK12me3 was higher in sample {i}.")])
        for limit in (1, 2, 3, 4):
            with self.subTest(limit=limit):
                self.assertEqual(len(self.search("ZK12me3", limit=limit)["hits"]), limit)

    def test_rt08_plain_query_paging_is_the_slice_of_the_ranking(self):
        for i in range(10):
            self._paper(f"p{i}", [("results", f"Alphaterm count {i} in the sample.")])
        everything, paged = self._pages("alphaterm", 3)
        self.assertEqual(paged[:len(everything)], everything)


    def test_rt05_absent_topic_is_flagged_but_hits_stay_and_never_say_not_in_the_literature(self):
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        result = self.search("zetaterm etaterm thetaterm alphaterm")
        self.assertTrue(result["hits"])
        self.assertEqual((result["not_found"], result["best_groups_matched"], len(result["groups"])), (True, 1, 4))
        self.assertIn("không tìm thấy trong vault hiện tại", result["not_found_message"].lower())
        self.assertIn("not found in the current vault", result["not_found_message"])
        self.assertNotIn("literature", result["not_found_message"])
        wide = self.search("zetaterm etaterm thetaterm alphaterm", all_vaults=True)
        self.assertIn("searched vaults", wide["not_found_message"])

    def test_rt05_near_misses_have_no_flag(self):
        self._paper("pa", [("results", "Alphaterm and betaterm and gammaterm were measured.")])
        for query in ("alphaterm", "alphaterm betaterm", "alphaterm betaterm gammaterm deltaterm"):   # the last one: 3 of 4 = 0.75, not below
            with self.subTest(query=query):
                result = self.search(query)
                self.assertFalse(result["not_found"])
                self.assertNotIn("not_found_message", result)
        self.assertFalse(self.search("")["not_found"])   # nothing to look for is not "not found"

    def test_rt05_the_written_answer_says_not_found_in_the_current_vault(self):
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        cfg = config.load()
        bad = answer.answer(self.con, cfg, "zetaterm etaterm thetaterm alphaterm", use_ai=False, vault="one", queries=["zetaterm etaterm thetaterm alphaterm"])
        self.assertTrue(bad["not_found"] and bad["items"])
        self.assertIn(bad["not_found_message"], bad["answer"]["missing"])
        self.assertIn(bad["not_found_message"], bad["markdown"])
        good = answer.answer(self.con, cfg, "alphaterm", use_ai=False, vault="one", queries=["alphaterm"])
        self.assertFalse(good["not_found"])
        self.assertEqual(good["answer"]["missing"], [answer.SCOPE_NOTE])

    def _asked(self, **kw):
        patcher = mock.patch.object(answer, "ANSWER_CACHE", self.root / "answers")
        patcher.start()
        self.addCleanup(patcher.stop)
        reply = {"answer": [{"text": "Alphaterm was 99 percent in group A [#1].", "evidence": [1]}], "reasoning": [], "missing": []}
        return agent_tasks.answer_check(self.con, config.load(), "Is alphaterm higher?", ["alphaterm"], reply, "evidence4", "one",
                                        "fast", "test", **kw)

    def test_an13_check_uses_the_prepared_items_and_gives_the_reasons_of_struck_sentences(self):
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        prepared = agent_tasks.answer_prompt(self.con, config.load(), "Is alphaterm higher?", ["alphaterm"], "evidence4", "one", "fast", "test")
        self._paper("pz", [("results", "Alphaterm alphaterm alphaterm alphaterm.")])   # would now rank first
        checked = self._asked(items_digest=prepared["items_digest"])
        self.assertEqual(checked["items_digest"], prepared["items_digest"])
        struck = [r for r in checked["answer"]["answer"] if r["check"] != "ok"]
        self.assertTrue(struck and struck[0]["reasons"][0] in checked["markdown"])
        self.assertEqual(self._asked()["items_digest"], prepared["items_digest"])   # no digest passed: still works

    def test_nw12_ask_prepare_links_follow_the_prompt_numbers_and_leave_the_digest_alone(self):
        import hashlib, json
        from kbsys import neural
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        self._paper("pb", [("results", "Alphaterm was lower in group B.")])
        (config.vault_dir("one") / "Papers").mkdir()
        (config.vault_dir("one") / "Papers" / "Nguyễn 2026 - Tế bào.md").write_text("---\npaper_id: pa\n---\nnote\n", encoding="utf-8")
        neural.refresh("one", force=True)
        ask = lambda: agent_tasks.answer_prompt(self.con, config.load(), "Is alphaterm higher?", ["alphaterm"], "evidence4", "one", "fast", "test")
        prepared = ask()
        links, user = prepared["links"], prepared["prompts"][0]["user"]
        self.assertEqual([l["n"] for l in links], list(range(1, prepared["items_found"] + 1)))
        self.assertEqual(prepared["items_digest"], hashlib.sha256(json.dumps([l["id"] for l in links]).encode()).hexdigest())
        self.assertTrue(all(f"[#{l['n']}] " in user for l in links))
        by_paper = {l["id"].split(":")[0]: l for l in links}
        self.assertEqual(by_paper["pa"]["note_path"], "Papers/Nguyễn 2026 - Tế bào.md")
        self.assertEqual(by_paper["pa"]["obsidian_url"], "obsidian://open?vault=one&file=Papers%2FNguy%E1%BB%85n%202026%20-%20T%E1%BA%BF%20b%C3%A0o.md")
        self.assertEqual((by_paper["pb"]["note_path"], by_paper["pb"]["obsidian_url"]), (None, None))
        self.assertIn("note_missing", by_paper["pb"])
        self.assertEqual({l["item_type"] for l in links}, {"sentence"})
        self.assertNotIn("obsidian://", user)
        self.assertNotIn("item_type", user)
        self.assertTrue(all("note_path" not in i for i in agent_tasks._PREPARED[agent_tasks._asked(
            "Is alphaterm higher?", ["alphaterm"], "evidence4", "one", "fast")][0]))   # the kept items stay as retrieved

    def test_an13_a_changed_item_list_is_refused_only_when_a_digest_is_passed(self):
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        prepared = agent_tasks.answer_prompt(self.con, config.load(), "Is alphaterm higher?", ["alphaterm"], "evidence4", "one", "fast", "test")
        agent_tasks._PREPARED.clear()   # another process: nothing kept, the items are retrieved again
        self._paper("pz", [("results", "Alphaterm alphaterm alphaterm alphaterm.")])
        with self.assertRaisesRegex(ValueError, "items_digest"):
            self._asked(items_digest=prepared["items_digest"])
        self.assertNotEqual(self._asked()["items_digest"], prepared["items_digest"])
        with self.assertRaisesRegex(ValueError, "items_digest"):   # a wrong digest is refused with the kept items too
            agent_tasks.answer_prompt(self.con, config.load(), "Is alphaterm higher?", ["alphaterm"], "evidence4", "one", "fast", "test")
            self._asked(items_digest="0" * 64)

    def test_an13_a_repeated_prepare_does_not_evict_another_ask(self):
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        agent_tasks._PREPARED.clear()
        ask = lambda i: agent_tasks.answer_prompt(self.con, config.load(), f"Is alphaterm higher {i}?", ["alphaterm"], "evidence4", "one", "fast", "test")
        for i in range(16):
            ask(i)
        ask(15)   # same ask again: 16 asks are kept, the oldest is still there
        self.assertEqual(len(agent_tasks._PREPARED), 16)
        ask(16)   # a 17th ask evicts the oldest only
        self.assertEqual(len(agent_tasks._PREPARED), 16)
        self.assertFalse(any("higher 0?" in key[1] for key in agent_tasks._PREPARED))
        self.assertTrue(any("higher 1?" in key[1] for key in agent_tasks._PREPARED))

    def test_rt05_web_merge_flags_only_when_every_english_query_is_not_found(self):
        import web
        self._paper("pa", [("results", "Alphaterm was higher in group A.")])
        one = lambda key, default="": {"vault": "one"}.get(key, default)
        for queries, flagged in ((["zetaterm etaterm"], True), (["zetaterm etaterm", "alphaterm"], False)):
            first = find.search(self.con, config.load(), "Alphaterm cao hơn ở nhóm A", vault="one", semantic=False)
            self.assertTrue(first["needs_translation"] and not first["not_found"])
            with mock.patch.object(web, "CFG", config.load()):
                merged = web._merge(self.con, first, queries, one, {}, 20, 0)
            self.assertEqual(merged["not_found"], flagged, queries)
            self.assertEqual("not_found_message" in merged, flagged, queries)

if __name__ == "__main__":
    unittest.main()
