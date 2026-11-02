"""Focused tests for lexical overlap search using an isolated SQLite store."""

import tempfile
import unittest
from pathlib import Path

from kbsys import config, db
from kbsys.overlap import find_overlaps


class OverlapTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="overlap-test-")
        root = Path(self.temp.name)
        self.saved = {name: getattr(config, name) for name in
                      ("ROOT", "CONFIG_DIR", "STORE", "DB_PATH", "PAPERS_DIR", "LOGS_DIR",
                       "CACHE_DIR", "VAULTS_DIR", "REVIEW_PENDING")}
        values = {"ROOT": root, "CONFIG_DIR": root / "config", "STORE": root / "store",
                  "DB_PATH": root / "store" / "library.db", "PAPERS_DIR": root / "store" / "papers",
                  "LOGS_DIR": root / "store" / "logs", "CACHE_DIR": root / "store" / "cache",
                  "VAULTS_DIR": root / "vaults", "REVIEW_PENDING": root / "review" / "pending"}
        for name, value in values.items():
            setattr(config, name, value)
        config.CONFIG_DIR.mkdir()
        (config.CONFIG_DIR / "ingest.yaml").write_text("ingest:\n  default_vault: alpha\n", encoding="utf-8")
        for vault in ("alpha", "beta"):
            config.vault_dir(vault).mkdir(parents=True)
        self.con = db.connect()
        self._add_paper("p-alpha", "alpha", [
            "The cells were washed and incubated for 3 hours before analysis.",
            "A completely unrelated sentence contains only alpha.",
        ])
        self._add_paper("p-beta", "beta", [
            "The cells were washed and incubated for 3 hours before analysis.",
        ])
        self.con.commit()

    def tearDown(self):
        self.con.close()
        for name, value in self.saved.items():
            setattr(config, name, value)
        self.temp.cleanup()

    def _add_paper(self, paper_id, vault, texts):
        folder = db.paper_dir(paper_id)
        db.write_json(folder / "metadata.json", {"paper_id": paper_id, "title": paper_id,
                     "year": 2026, "authors": [], "source_files": [], "vaults": [vault]})
        db.write_json(folder / "sections.json", [])
        rows = [{"sentence_id": f"{paper_id}:S{i:04d}", "seq": i, "page": i,
                 "section": "methods", "kind": "text", "source": "main",
                 "text": text, "passage_id": f"{paper_id}:P{i:04d}"}
                for i, text in enumerate(texts, 1)]
        db.write_jsonl(folder / "sentences.jsonl", rows)
        db.write_jsonl(folder / "passages.jsonl", [{"passage_id": row["passage_id"], "seq": row["seq"],
                       "page": row["page"], "section": row["section"], "kind": "text", "text": row["text"]}
                      for row in rows])
        for name in ("references.jsonl", "figures.jsonl", "tables.jsonl", "evidence.jsonl"):
            db.write_jsonl(folder / name, [])
        db.load_paper(self.con, paper_id)

    def test_exact_copy_partial_overlap_and_unrelated_score(self):
        query = "The cells were washed and incubated for 3 hours before analysis."
        result = find_overlaps(self.con, query, vault="alpha")
        exact = next(hit for hit in result["hits"] if hit["paper_id"] == "p-alpha")
        self.assertEqual(exact["score"], 1.0)
        self.assertEqual(exact["text"][exact["matched_spans"][0]["start"]:
                                        exact["matched_spans"][0]["end"]], query.rstrip("."))

        partial = find_overlaps(self.con, "The cells were washed and incubated for 3 hours in a dish.",
                                vault="alpha")
        part = next(hit for hit in partial["hits"] if hit["paper_id"] == "p-alpha")
        self.assertTrue(0 < part["score"] < 1)
        self.assertEqual(partial["papers"][0]["title"], "p-alpha")
        self.assertEqual(find_overlaps(self.con, "alpha beta gamma delta epsilon", vault="alpha")["hits"], [])
        self.assertEqual(find_overlaps(self.con, "   ")["hits"], [])

    def test_spans_slice_original_text_despite_punctuation_and_case(self):
        self._add_paper("p-dash", "alpha", ["Rewarming of the CELL–based “model” was fast, then ice formed."])
        self.con.commit()
        result = find_overlaps(self.con, "of the cell-based model was fast then", vault="alpha")
        hit = next(h for h in result["hits"] if h["paper_id"] == "p-dash")
        span = hit["matched_spans"][0]
        self.assertEqual(hit["text"][span["start"]:span["end"]], "of the CELL–based “model” was fast, then")

    def test_default_and_explicit_vault_scoping_and_paper_coverage(self):
        query = "The cells were washed and incubated for 3 hours before analysis."
        default = find_overlaps(self.con, query)
        self.assertEqual({hit["paper_id"] for hit in default["hits"]}, {"p-alpha"})
        self.assertEqual(default["papers"][0]["coverage"], 1.0)
        beta = find_overlaps(self.con, query, vault="beta")
        self.assertEqual({hit["paper_id"] for hit in beta["hits"]}, {"p-beta"})


if __name__ == "__main__":
    unittest.main()
