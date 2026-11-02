"""Edges of the RT / AN checks that the generic PASS / FAIL cases do not cover.

The clauses listed in RED have a PASS case that reports violations today; the tests here hand them a judge that does what the
clause asks (a thin wrapper over the product function, or the product function itself once G4 has built it) and expect no
violation, so the check can be satisfied and G4 knows what to build. A crashing judge must be a violation, never a PASS. The probes must leave the real project untouched.
"""
import json
import re
import tempfile
import unicodedata
import unittest
from pathlib import Path

from kbsys import answer, config, conform, find
from kbsys.conform import Store, Vault

from . import cases_answer as cases

conform.load_checks()


def found(cid, *args):
    return conform.violations(cid, *args)


def nfc(text):
    return unicodedata.normalize("NFC", text)


class Crashes(unittest.TestCase):
    def test_a_judge_that_crashes_is_a_violation_not_a_pass(self):
        def boom(*args, **kwargs):
            raise ZeroDivisionError("boom")
        for cid in ("RT-01", "RT-02", "RT-03", "RT-04", "RT-05", "RT-06", "RT-08", "RT-12", "AN-01", "AN-03", "AN-09"):
            with self.subTest(cid):
                got = found(cid, boom)
                self.assertTrue(got and all("boom" in v or "crashed" in v for v in got), got)

    def test_probes_leave_the_real_project_untouched(self):
        before = (config.STORE, config.DB_PATH, config.VAULTS_DIR, answer.ANSWER_CACHE, find.vectors.index_ready)
        saved_files = sorted(p.name for p in answer.ANSWER_CACHE.glob("*")) if answer.ANSWER_CACHE.is_dir() else None
        found("AN-13")
        found("AN-15")
        self.assertEqual(before, (config.STORE, config.DB_PATH, config.VAULTS_DIR, answer.ANSWER_CACHE, find.vectors.index_ready))
        self.assertEqual(saved_files, sorted(p.name for p in answer.ANSWER_CACHE.glob("*")) if answer.ANSWER_CACHE.is_dir() else None)


class GoodJudges(unittest.TestCase):
    """Wrappers that satisfy the RED clauses."""

    def test_rt02_group_count_first_and_no_cap(self):
        self.assertEqual(found("RT-02", find.search), [])

    def test_rt03_variants_and_stop_words(self):
        self.assertEqual(found("RT-03", find.search), [])

    def test_rt03_variant_group_has_no_false_friends(self):
        from kbsys import conform_answer as lab
        shipped = config.load().get("word_variants")
        with lab._lab() as (root, con, cfg):
            cfg["word_variants"] = shipped
            for pid, text in (("a", "Oocytes were vitrified in straws."), ("b", "Vitrification of oocytes was done in straws."),
                              ("c", "Vitamin C was added to the medium."), ("d", "H3K4me3 and ZK12me3 marks were scored in vitrified oocytes."),
                              ("e", "We vitrify oocytes and H3K4me3 was scored.")):
                lab._add_paper(con, pid, [("results", 1, text)])
            ids = lambda q, **kw: {h["paper_id"] for h in find.search(con, cfg, q, vault="one", semantic=False, **kw)["hits"]}
            self.assertEqual(ids("vitrify"), {"a", "b", "d", "e"})            # any form finds the others, not "Vitamin"
            self.assertEqual(ids("vitrifying"), {"a", "b", "d", "e"})
            self.assertEqual(ids("ZK12me3"), {"d"})                             # a typed identity term is not widened
            both = find.search(con, cfg, "vitrification H3K4me3", vault="one", semantic=False)
            self.assertEqual({h["paper_id"] for h in both["hits"][:2]}, {"d", "e"})   # both groups first (RT-02 ranking)
            self.assertEqual(ids("vitamin"), {"c"})
            self.assertEqual(find.search(con, cfg, "affect of vitrification on H3K4me3", vault="one", semantic=False)["groups"],
                             both["groups"])                                   # "affect" adds no keyword group

    def test_rt04_vietnamese_query_is_flagged_in_every_form(self):
        self.assertEqual(found("RT-04", find.search), [])

    def test_rt05_flag_when_the_best_item_covers_too_few_groups(self):
        self.assertEqual(found("RT-05", find.search), [])

    def test_rt06_unknown_vault_raises(self):
        self.assertEqual(found("RT-06", find.search), [])

    def test_rt07_every_query_keeps_its_top_items_and_the_matched_sentence(self):
        self.assertEqual(found("RT-07", answer.collect), [])

    def test_rt08_paging_is_stable_and_skipped_sections_stay_out(self):
        self.assertEqual(found("RT-08", find.search), [])

    def test_rt08_a_hit_from_the_notes_section_is_a_violation(self):  # RT-08 lists `notes` since v1.0 (D-PENDING-20)
        judge = cases.tamper(find.search, lambda r: {**r, "hits": r["hits"] + [{**cases.FAKE_HIT, "sections": ["notes"]}]})
        self.assertTrue(any("notes" in v for v in found("RT-08", judge)))

    def test_an03_vietnamese_causal_verbs_and_nfd(self):
        self.assertEqual(found("AN-03", answer.check_sentence), [])

    def test_an02_an06_an07_are_met_by_the_real_checker(self):
        for cid in ("AN-02", "AN-06", "AN-07"):
            with self.subTest(cid):
                self.assertEqual(found(cid, answer.check_sentence), [])

    def test_an10_identifiers_must_occur_in_the_cited_item(self):
        self.assertEqual(found("AN-10", answer.check_sentence), [])

    def test_an11_affirming_verb_in_a_limits_sentence(self):
        self.assertEqual(found("AN-11", answer.check_sentence), [])

    def test_an12_item_numbers_are_hash_numbers_and_bad_values_are_rejected(self):
        self.assertEqual(found("AN-12", answer.check_sentence, answer.check_reasoning), [])

    def test_an12_infinity_and_huge_json_numbers_never_crash_the_checker(self):
        item = {"text": "Cells were counted.", "paper_id": "p", "kind": "sentence", "unit_type": None}
        for bad in (float("inf"), float("-inf"), float("nan"), json.loads("1e400"), json.loads("[-1e400]")[0], 1.5, True):
            with self.subTest(bad=bad):
                row = {"text": "Cells were counted.", "evidence": [bad], "basis": [bad]}
                self.assertEqual(answer.check_sentence(row, [item], {})["check"], "struck:A1")
                self.assertEqual(answer.check_reasoning(row, [item], {})["check"], "struck:A8")
                reply = json.dumps({"answer": [row], "reasoning": [row], "missing": []})  # Infinity / NaN, as a client sends it
                done = answer.check_parts(json.loads(reply), [item], {})
                self.assertEqual([r["check"] for r in done["answer"] + done["reasoning"]], ["struck:A1", "struck:A8"])
        ok = answer.check_sentence({"text": "Cells were counted [#1] as in [1, 2] and [40-42].", "evidence": []}, [item], {})
        self.assertEqual((ok["check"], ok["evidence"]), ("ok", [1]))

    def test_an12_only_ascii_digits_make_an_item_number(self):
        item = {"text": "Cells were counted.", "paper_id": "p", "kind": "sentence", "unit_type": None}
        for text, evidence in (("Cells were counted [#١].", []), ("Cells were counted.", ["١"]), ("Cells were counted.", ["１"])):
            with self.subTest(text=text, evidence=evidence):
                self.assertEqual(answer.check_sentence({"text": text, "evidence": evidence}, [item], {})["check"], "struck:A1")
        self.assertEqual(answer.check_sentence({"text": "Cells were counted.", "evidence": ["1", " [#1] "]}, [item], {})["check"], "ok")

    def test_an13_digest_is_kept_from_prepare_and_markdown_gives_reasons(self):
        self.assertEqual(found("AN-13"), [])


class BasicCheckerStaysGreen(unittest.TestCase):
    """The parts of the RED clauses that the product does today: they must not regress while G4 works on the rest."""

    def test_invented_number_and_dropped_hedge_are_still_struck(self):
        plain = {"id": "p1:S0001", "paper_id": "p1", "kind": "sentence", "unit_type": None, "text": "Survival was 85 percent.",
                 "review_paper": False}
        hedged = {**plain, "text": "Results suggest higher survival.", "hedging": "hedged"}
        verdict = lambda text, item: answer.check_sentence({"text": text}, [item], {})["check"]
        self.assertEqual(verdict("Survival was 85 percent [#1].", plain), "ok")
        self.assertEqual(verdict("Survival was 90 percent [#1].", plain), "struck:A2")
        self.assertEqual(verdict("Survival was higher [#1].", hedged), "struck:A7")


class DataHalves(unittest.TestCase):
    def tmp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return Path(d.name)

    def cache(self, tmp, text):
        cases.put(tmp / "s" / "cache" / "answers" / "a.json", text)
        return Store(root=tmp / "s", paper_ids=[], vaults=[])

    def test_unreadable_cache_file_is_reported_not_skipped(self):
        got = found("RT-10", self.cache(self.tmp(), "{not json"))
        self.assertEqual([v for v in got if "cannot be read" in v][:1].__len__(), 1, got)

    def test_cached_item_missing_doi_and_pmid_fields_and_text(self):
        def strip(data):
            data["papers"]["p1"].pop("doi")
            data["items"][0]["text"] = " "
        tmp = self.tmp()
        got = found("RT-10", cases.cache_store(tmp, strip))
        self.assertIn("doi and pmid fields", got[0])
        self.assertIn("verbatim text", got[0])

    def test_cached_answer_citing_a_missing_item_or_a_paper_without_card(self):
        def damage(data):
            data["answer"]["answer"].append({"text": "x [2]", "check": "ok", "evidence": [2]})
            data["answer"]["answer"].append({"text": "no item", "check": "struck:A1", "evidence": [9]})  # A1 is reported by the checker itself
            del data["papers"]["p1"]
        got = found("AN-18", cases.cache_store(self.tmp(), damage))
        self.assertTrue(any("[2] is not an item" in v for v in got), got)
        self.assertTrue(any("lacks paper card" in v and "original-file status" in v for v in got), got)
        self.assertFalse(any("[9]" in v for v in got), got)

    def test_knowledge_gap_needs_its_three_labelled_parts_not_the_banner(self):
        tmp = self.tmp()
        banner = "---\ntype: knowledge_gap\nevidence: false\n---\n# T\n\n> [!note] bằng chứng từ bài + kiến thức của AI + nhận định của bạn\n\ntext\n"
        got = found("AN-17", cases.vault_with(tmp, "gap.md", banner))
        self.assertTrue(any("lacks the labelled parts" in v for v in got), got)
        parts = "\n\n".join(f"**{p}**\n\ntext" for p in ("Bằng chứng từ bài", "Kiến thức của AI", "Nhận định của bạn"))
        tmp2 = self.tmp()
        ok = found("AN-17", cases.vault_with(tmp2, "gap.md", banner + parts))
        self.assertFalse(any("lacks the labelled parts" in v for v in ok), ok)

    def test_note_of_another_type_and_a_note_without_front_matter(self):
        tmp = self.tmp()
        cases.put(tmp / "v" / "Discussions" / "a.md", "---\ntype: evidence\nevidence: false\n---\ntext\n")
        cases.put(tmp / "v" / "Discussions" / "b.md", "no front matter\n")
        got = found("AN-17", Store(root=tmp / "s", paper_ids=[], vaults=[Vault("v", tmp / "v")]))
        self.assertTrue(any("type is 'evidence'" in v for v in got), got)
        self.assertTrue(any("no front matter" in v for v in got), got)


if __name__ == "__main__":
    unittest.main()
