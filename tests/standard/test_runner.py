"""`conform.run`: statuses, filters, crashes and the CLI table, with throw-away checks."""
import unittest
from unittest.mock import patch

from .. import test_core as fixtures
from kbsys import conform, standard


class RunnerTests(unittest.TestCase):
    setUp = fixtures.Demo3Tests.setUp
    tearDown = fixtures.Demo3Tests.tearDown

    def run_with(self, checks, **kwargs):
        table = {cid: (scope, fn, False) for cid, (scope, fn) in checks.items()}
        with patch.object(conform, "CHECKS", table), patch.object(conform, "MODULES", ()):
            return {r["id"]: r for r in conform.run(self.con, **kwargs)["clauses"]}

    def test_clause_without_check_is_not_implemented_never_pass(self):
        rows = self.run_with({"EV-05": ("probe", lambda: [])})
        self.assertEqual(rows["EV-05"]["status"], conform.PASS)
        self.assertEqual({r["status"] for cid, r in rows.items() if cid != "EV-05"}, {conform.NOT_IMPLEMENTED})
        self.assertEqual(len(rows), len(standard.load()["clauses"]))

    def test_levels_and_violations(self):
        rows = self.run_with({"EV-05": ("probe", lambda: ["x"]),     # level warn
                              "EV-01": ("probe", lambda: iter(["a", "b"]))})  # level fail
        self.assertEqual((rows["EV-05"]["status"], rows["EV-01"]["status"]), (conform.WARN, conform.FAIL))
        self.assertEqual((rows["EV-01"]["violations"], rows["EV-01"]["examples"]), (2, ["a", "b"]))

    def test_a_crashing_check_is_an_error(self):
        def boom():
            raise KeyError("k")
        rows = self.run_with({"EV-01": ("probe", boom)})
        self.assertEqual(rows["EV-01"]["status"], conform.ERROR)
        self.assertIn("KeyError", rows["EV-01"]["error"])

    def test_paper_scope_reads_one_paper_at_a_time_and_filters(self):
        seen = []
        rows = self.run_with({"CH-02": ("paper", lambda p: seen.append(p.id) or [f"bad {len(p.sentences)}"])})
        self.assertEqual(seen, [self.pid])
        self.assertEqual(rows["CH-02"]["status"], conform.FAIL)
        self.assertEqual(list(rows["CH-02"]["where"]), [self.pid])
        self.assertEqual(list(self.run_with({"CH-02": ("paper", lambda p: [])}, stage="IN")), [c["id"] for c in standard.load()["clauses"] if c["stage"] == "IN"])

    def test_a_mistyped_filter_is_an_error_not_a_pass(self):
        for bad in ({"paper": "nope"}, {"stage": "in"}, {"vault": "nope"}):
            with self.subTest(bad), self.assertRaises(ValueError):
                self.run_with({"CH-02": ("paper", lambda p: [])}, **bad)

    def test_paper_filter_skips_other_scopes_and_skip_selftest_drops_wrappers(self):
        rows = self.run_with({"RT-06": ("probe", lambda: ["x"])}, paper=self.pid)
        self.assertEqual(rows["RT-06"]["status"], conform.NO_DATA)
        table = {"RT-06": ("probe", lambda: ["x"], True)}
        with patch.object(conform, "CHECKS", table), patch.object(conform, "MODULES", ()):
            ids = [r["id"] for r in conform.run(self.con, skip_selftest=True)["clauses"]]
        self.assertNotIn("RT-06", ids)

    def test_table_and_param(self):
        text = conform.render_table(conform.run(self.con))
        self.assertIn("not_implemented", text)
        self.assertEqual(standard.param("RL-09", "hub_share_of_papers"), 0.5)
        with self.assertRaises(KeyError):
            standard.param("RL-09", "nope")


if __name__ == "__main__":
    unittest.main()
