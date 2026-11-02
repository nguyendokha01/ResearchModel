"""G2: config/standard.yaml is valid, its enforced_by names exist, and the generated docs are in sync."""
import tempfile
import unittest
from pathlib import Path

import yaml

from kbsys import standard


class StandardTests(unittest.TestCase):
    def test_yaml_is_valid_and_every_enforced_by_exists(self):
        data = standard.load()
        self.assertEqual(data["version"], "1.4")
        self.assertEqual(standard.unresolved(data), [])

    def test_docs_are_generated_not_hand_edited(self):
        # Fails when someone edits the yaml or a doc by hand: run `python scripts/kb.py standard --write-docs`.
        self.assertEqual(standard.out_of_sync(standard.load()), [])

    def test_schema_problems_are_all_reported(self):
        data = yaml.safe_load(standard.YAML.read_text(encoding="utf-8"))
        data["clauses"][0].pop("pass")
        data["clauses"][1]["level"] = "maybe"
        data["clauses"][2]["id"] = data["clauses"][3]["id"]  # also breaks the stage prefix when stages differ
        data["open_decisions"][0]["clauses"] = ["NOPE-01"]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "bad.yaml"
            path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
            with self.assertRaises(ValueError) as err:
                standard.load(path)
        text = str(err.exception)
        self.assertIn("missing pass", text)
        self.assertIn("level must be fail or warn", text)
        self.assertIn("duplicate id", text)
        self.assertIn("unknown clause NOPE-01", text)

    def test_unknown_enforced_by_is_reported(self):
        data = standard.load()
        data["clauses"][0]["enforced_by"] = "relations.no_such_function"
        data["clauses"][1]["enforced_by"] = "nodot"
        self.assertEqual(standard.unresolved(data), [f"{data['clauses'][0]['id']}: relations.no_such_function",
                                                     f"{data['clauses'][1]['id']}: nodot"])

    def test_a_hand_edited_doc_is_detected(self):
        data = standard.load()
        with tempfile.TemporaryDirectory() as temp:
            en, vi = Path(temp) / "en.md", Path(temp) / "vi.md"
            en.write_bytes(standard.render_en(data).encode("utf-8"))
            vi.write_bytes((standard.render_vi(data) + "x").encode("utf-8"))
            old = standard.DOC_EN, standard.DOC_VI
            standard.DOC_EN, standard.DOC_VI = en, vi
            try:
                self.assertEqual(standard.out_of_sync(data), ["vi.md"])
            finally:
                standard.DOC_EN, standard.DOC_VI = old

    def test_frozen_standard_does_not_ask_for_open_decisions(self):
        data = standard.load()
        data["status"] = "frozen"
        self.assertIn("Các quyết định đã chốt", standard.render_vi(data))
        self.assertNotIn("cần bạn xác nhận", standard.render_vi(data))
        data["status"] = "draft"
        self.assertIn("cần bạn xác nhận", standard.render_vi(data))

    def test_standard_survives_a_public_release(self):
        # package.release strips whole lines that name private docs and leaves out some scripts: the shipped copy must still be in sync.
        from kbsys import package
        text = standard.DOC_EN.read_text(encoding="utf-8") + standard.DOC_VI.read_text(encoding="utf-8")
        self.assertEqual(package.PRIVATE_DOC_LINE.sub("", text), text)
        for c in standard.load()["clauses"]:
            module = c["enforced_by"].split(".")[0]
            self.assertIsNone(package.RELEASE_NEVER.search(f"scripts/{module}.py"), c["id"])


if __name__ == "__main__":
    unittest.main()
