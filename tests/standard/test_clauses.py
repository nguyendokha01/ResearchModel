"""One PASS and one FAIL case per implemented clause of config/standard.yaml (synthetic fixtures only).

A `cases_*.py` module in this folder defines
    CASES = {"EV-05": (pass_case, fail_case)}   each case is `f(tmp: Path) -> tuple of arguments for the check`
    RED = {"AN-10": "why the product code is wrong today"}   clauses whose PASS case is expected to report
        violations until G4 fixes the code; when the code is fixed this test fails and the entry must be deleted.
Clauses without a check are reported not_implemented by `conform.run` (tests/standard/test_runner.py).
"""
import importlib
import pkgutil
import tempfile
import unittest
from pathlib import Path

from kbsys import conform, standard

import tests.standard as package
from .cases_network import HAS_BENCH, NEEDS_BENCH


def _modules():
    return [importlib.import_module(f"{package.__name__}.{m.name}") for m in pkgutil.iter_modules(package.__path__)
            if m.name.startswith("cases_")]


def _all(attr):
    out = {}
    for module in _modules():
        for key, value in getattr(module, attr, {}).items():
            assert key not in out, f"{key} is defined in two cases modules"
            out[key] = value
    return out


CASES, RED = _all("CASES"), _all("RED")


class ClauseCases(unittest.TestCase):
    def setUp(self):
        conform.load_checks()
        self.known = {c["id"] for c in standard.load()["clauses"]}

    def run_case(self, cid, case):
        with tempfile.TemporaryDirectory() as tmp:
            return conform.violations(cid, *case(Path(tmp)))

    def test_every_check_has_a_pass_and_a_fail_case_and_every_case_has_a_check(self):
        self.assertEqual(sorted(set(conform.CHECKS) - set(CASES)), [], "checks without cases")
        self.assertEqual(sorted(set(CASES) - set(conform.CHECKS)), [], "cases without a check")
        self.assertEqual(sorted(set(conform.CHECKS) - self.known), [], "checks of unknown clauses")
        self.assertEqual(sorted(set(RED) - set(CASES)), [])

    def test_pass_cases_report_nothing(self):
        for cid, (good, _) in sorted(CASES.items()):
            with self.subTest(cid):
                if cid in NEEDS_BENCH and not HAS_BENCH:
                    self.skipTest("benchmark_research is not shipped")
                found = self.run_case(cid, good)
                if cid in RED:
                    self.assertTrue(found, f"{cid} now passes: delete it from RED ({RED[cid]})")
                else:
                    self.assertEqual(found, [], f"{cid} PASS case reports violations")

    def test_fail_cases_report_a_violation(self):
        for cid, (_, bad) in sorted(CASES.items()):
            with self.subTest(cid):
                if cid in NEEDS_BENCH and not HAS_BENCH:
                    self.skipTest("benchmark_research is not shipped")
                self.assertTrue(self.run_case(cid, bad), f"{cid} FAIL case reports nothing")


if __name__ == "__main__":
    unittest.main()
