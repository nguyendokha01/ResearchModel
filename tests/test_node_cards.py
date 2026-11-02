"""Offline tests for source-grounded typed node cards."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kbsys import config, db, node_cards


class _Registry:
    def __init__(self, records):
        self.records = records
        self.by_id = {row["concept_id"]: row for row in records}
        self.by_name = {row["name"].casefold(): row for row in records}

    def lookup(self, value):
        return self.by_id.get(value) or self.by_name.get(str(value).casefold())


class NodeCardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.patch = patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "store/papers",
                                    VAULTS_DIR=root / "vaults", CONFIG_DIR=Path(__file__).resolve().parents[1] / "config",
                                    LOGS_DIR=root / "logs", DB_PATH=root / "store" / "library.db")
        (root / "store").mkdir(parents=True, exist_ok=True)
        self.patch.start()
        self.records = [
            {"concept_id": "local:eg", "name": "EG", "type": "molecule", "synonyms": ["ethylene glycol"], "parents": []},
            {"concept_id": "local:other", "name": "Other", "type": "molecule", "synonyms": [], "parents": []},
        ]
        self.registry = _Registry(self.records)
        self._write_paper("p", "primary_research", [
            self._sentence("p:S0001", 1, "Ethylene glycol (EG) may increase survival by 10%.", "introduction", "para"),
            self._sentence("p:S0002", 2, "EG was used in the freezing protocol.", "methods", "methods"),
            self._sentence("p:S0003", 3, "EG was used at 15 mg for 5 min.", "methods", "methods"),
            self._sentence("p:S0004", 4, "EG was associated with survival.", "results", "results"),
        ], [{"unit_id": "p:E1", "seq": 1, "unit_type": "result", "status": "ok", "statement": "EG may increase survival.",
             "quote": "ignored", "sentence_ids": ["p:S0001"], "outcome": {"values": ["10%"]}}])
        self._write_paper("r", "review", [
            self._sentence("r:S0001", 1, "EG is discussed as a cryoprotectant in a review.", "introduction", "para"),
        ], [])
        self.con = db.connect()
        self.con.executemany("INSERT INTO paper_vaults(paper_id,vault) VALUES(?,?)", [("p", "v"), ("r", "v")])
        self.con.executemany("INSERT INTO concept_links(concept_id,paper_id,level,n_sentences,sections,first_sentence_id) VALUES(?,?,?,?,?,?)",
                             [("local:eg", "p", "studied", 4, "introduction,methods,results", "p:S0001"),
                              ("local:eg", "r", "mentioned", 1, "introduction", "r:S0001")])
        self.con.commit()
        self.registry_patch = patch.object(node_cards, "_registry", return_value=self.registry)
        self.registry_patch.start()

    def tearDown(self):
        self.registry_patch.stop()
        self.con.close()
        self.patch.stop()
        self.temp.cleanup()

    @staticmethod
    def _sentence(sid, seq, text, section, passage):
        return {"sentence_id": sid, "seq": seq, "text": text, "passage_id": passage, "section": section,
                "page": 1, "kind": "text", "source": "main"}

    def _write_paper(self, paper_id, evidence_type, sentences, units):
        folder = db.paper_dir(paper_id)
        db.write_json(folder / "metadata.json", {"paper_id": paper_id, "title": paper_id, "note_name": paper_id,
                                                  "year": 2024, "evidence_type": evidence_type,
                                                  "publication_types": ["Review"] if evidence_type == "review" else []})
        db.write_jsonl(folder / "sentences.jsonl", sentences)
        db.write_jsonl(folder / "evidence.jsonl", units)
        db.write_jsonl(folder / "figures.jsonl", [])
        db.write_jsonl(folder / "tables.jsonl", [])

    def task(self):
        return node_cards.task(self.con, "v", "EG")

    def good_reply(self, payload=None):
        payload = payload or self.task()
        sid = "p:S0001"
        stmt = {"text": "EG may increase survival by 10%.", "sentence_ids": [sid], "attribution": "primary", "hedge": "hedged"}
        not_found = {"status": "not_found_in_vault"}
        return {"source_revision": payload["source_revision"], "node_type": "substance",
                "fields": {"definition": stmt, "classification": not_found, "function_or_purpose": not_found,
                            "importance": not_found, "cautions": not_found, "how_cited": not_found,
                            "cross_paper": not_found,
                            "substance_class": {"value": "small_molecule", "sentence_ids": [sid]},
                            "chem_roles": [{"value": "cryoprotectant", "sentence_ids": [sid]}],
                            "study_role": [{"value": "mention", "sentence_ids": [sid]}],
                            "abbreviation": not_found, "full_name": not_found},
                "related": [{"name": "Other", "relation": "associated_with"}]}

    def test_task_contains_source_evidence_methods_and_cap(self):
        rows = [self._sentence(f"p:S{i:04d}", i, "Context sentence in the same paragraph.", "introduction", "para")
                for i in range(10, 610)]
        folder = db.paper_dir("p")
        db.write_jsonl(folder / "sentences.jsonl", list(db.read_jsonl(folder / "sentences.jsonl")) + rows)
        payload = self.task()
        self.assertEqual(payload["source_revision"].__class__, str)
        self.assertIn("p:S0001", {row["id"] for row in payload["sentences"]})
        self.assertLessEqual(len(payload["sentences"]), node_cards.MAX_SENTENCES)
        self.assertGreater(payload["trimmed"]["trimmed"], 0)
        self.assertTrue(payload["protocol_variants"] or payload["sentences"])
        self.assertIn("review", {paper["evidence_type"] for paper in payload["papers"]})

    def test_accept_and_idempotent_resubmit_copies_quotes(self):
        reply = self.good_reply()
        first = node_cards.submit(self.con, "v", "EG", reply, "tester")
        second = node_cards.submit(self.con, "v", "EG", reply, "tester")
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        card = node_cards.load("v", "local:eg")
        self.assertEqual(card["quotes"]["p:S0001"]["text"], "Ethylene glycol (EG) may increase survival by 10%.")

    def test_capped_task_card_is_current_right_after_submit(self):
        folder = db.paper_dir("p")  # a second unit on a methods sentence: the 1-sentence task keeps S0001 and drops it
        db.write_jsonl(folder / "evidence.jsonl", list(db.read_jsonl(folder / "evidence.jsonl")) + [
            {"unit_id": "p:E2", "seq": 2, "unit_type": "method", "status": "ok", "statement": "EG was used for 5 min.",
             "quote": "ignored", "sentence_ids": ["p:S0003"], "outcome": {"values": []}}])
        with patch.object(node_cards, "MAX_SENTENCES", 1):  # the task shows a slice; the card covers the node's units
            self.assertNotIn("p:E2", {u["unit_id"] for u in self.task()["evidence_units"]})
            self.assertTrue(node_cards.submit(self.con, "v", "EG", self.good_reply(), "tester")["ok"])
            shown = node_cards.load("v", "local:eg")
            self.assertEqual(shown["card_currency"], "current")
            self.assertEqual(shown["card_check"], "passed")   # NC-04: no accepted/needs-review status to tick
            self.assertNotIn("card_status", shown)

    def test_submit_honours_includes_for_sentence_over_cap(self):
        rows = [self._sentence(f"p:S{i:04d}", i, "EG was used in the protocol.", "methods", "m%d" % i)
                for i in range(10, 710)]
        folder = db.paper_dir("p")
        db.write_jsonl(folder / "sentences.jsonl", list(db.read_jsonl(folder / "sentences.jsonl")) + rows)
        late = "p:S0709"
        self.assertNotIn(late, {row["id"] for row in self.task()["sentences"]})
        reply = self.good_reply()
        reply["fields"]["definition"]["sentence_ids"] = [late]
        reply["fields"]["definition"]["text"] = "EG was used in the protocol."
        self.assert_rejected(copy.deepcopy(reply), "unknown_sentence_id")
        by_arg = node_cards.submit(self.con, "v", "EG", reply, "tester", include_sentence_ids=[late])
        self.assertTrue(by_arg["ok"], by_arg["problems"])
        reply["include_sentence_ids"] = [late]
        by_reply = node_cards.submit(self.con, "v", "EG", reply, "tester")
        self.assertTrue(by_reply["ok"], by_reply["problems"])
        reply["include_sentence_ids"] = ["p:S9999"]  # ineligible ids stay unknown
        reply["fields"]["definition"]["sentence_ids"] = ["p:S9999"]
        self.assertFalse(node_cards.submit(self.con, "v", "EG", reply, "tester")["ok"])

    def assert_rejected(self, reply, code):
        result = node_cards.submit(self.con, "v", "EG", reply, "tester")
        self.assertFalse(result["ok"])
        self.assertIn(code, {problem["code"] for problem in result["problems"]})

    def test_rejects_unknown_id_invented_number_hedge_drop_and_causation(self):
        base = self.good_reply()
        unknown = copy.deepcopy(base)
        unknown["fields"]["definition"]["sentence_ids"] = ["p:S9999"]
        self.assert_rejected(unknown, "unknown_sentence_id")
        number = copy.deepcopy(base)
        number["fields"]["definition"]["text"] = "EG may increase survival by 99%."
        self.assert_rejected(number, "A2")
        hedge = copy.deepcopy(base)
        hedge["fields"]["definition"]["text"] = "EG increases survival by 10%."
        hedge["fields"]["definition"]["hedge"] = "none"
        self.assert_rejected(hedge, "hedge_dropped")
        causes = copy.deepcopy(base)
        causes["fields"]["definition"]["text"] = "EG causes survival by 10%."
        self.assert_rejected(causes, "A3")

    def test_review_requires_review_or_cited_study_attribution(self):
        payload = self.task()
        reply = self.good_reply(payload)
        reply["fields"]["definition"] = {"text": "EG is discussed as a cryoprotectant.", "sentence_ids": ["r:S0001"],
                                           "attribution": "primary", "hedge": "none"}
        self.assert_rejected(reply, "review_attribution")

    def test_stale_card_and_pending_order(self):
        reply = self.good_reply()
        self.assertTrue(node_cards.submit(self.con, "v", "EG", reply)["ok"])
        folder = db.paper_dir("p")
        changed = list(db.read_jsonl(folder / "sentences.jsonl"))
        changed[0]["text"] += " Revised source."
        db.write_jsonl(folder / "sentences.jsonl", changed)
        self.assertIsNone(node_cards.load("v", "local:eg"))
        self.assertTrue(node_cards.load_status("v", "local:eg")["stale"])
        # Source-stale cards are repaired before nodes with no card.
        self.con.execute("INSERT INTO concept_links VALUES(?,?,?,?,?,?)",
                         ("local:other", "p", "studied", 5, "results", "p:S0001"))
        rows = list(db.read_jsonl(folder / "sentences.jsonl"))
        rows.append(self._sentence("p:S0500", 500, "Other was measured in the results.", "results", "other"))
        db.write_jsonl(folder / "sentences.jsonl", rows)
        units = list(db.read_jsonl(folder / "evidence.jsonl"))
        units.extend([{"unit_id": "p:O1", "seq": 2, "unit_type": "result", "status": "ok",
                       "statement": "Other was measured.", "sentence_ids": ["p:S0500"]},
                      {"unit_id": "p:O2", "seq": 3, "unit_type": "result", "status": "ok",
                       "statement": "Other was measured again.", "sentence_ids": ["p:S0500"]}])
        db.write_jsonl(folder / "evidence.jsonl", units)
        self.con.commit()
        pending = node_cards.pending(self.con, "v")
        self.assertEqual(pending[0]["concept_id"], "local:eg")
        self.assertEqual(pending[0]["card_currency"], "stale")

    def test_new_paper_keeps_card_served_and_sentence_change_makes_it_stale(self):
        self.assertTrue(node_cards.submit(self.con, "v", "EG", self.good_reply())["ok"])
        card = node_cards.load("v", "local:eg")
        self.assertEqual(card["card_currency"], "current")

        self._write_paper("new", "primary_research", [
            self._sentence("new:S0001", 1, "EG was also evaluated in a second paper.", "results", "new")
        ], [{"unit_id": "new:E1", "seq": 1, "unit_type": "result", "status": "ok",
             "statement": "EG was evaluated.", "sentence_ids": ["new:S0001"]}])
        self.con.execute("INSERT INTO paper_vaults(paper_id,vault) VALUES(?,?)", ("new", "v"))
        self.con.execute("INSERT INTO concept_links VALUES(?,?,?,?,?,?)",
                         ("local:eg", "new", "studied", 1, "results", "new:S0001"))
        self.con.commit()

        card = node_cards.load("v", "local:eg")
        self.assertIsNotNone(card)
        self.assertEqual(card["card_currency"], "current_with_new_evidence")
        self.assertEqual(len(card["new_paper_ids"]), 1)
        status = node_cards.load_status("v", "local:eg")
        self.assertEqual(status["card_currency"], "current_with_new_evidence")
        self.assertFalse(status["stale"])

        rows = list(db.read_jsonl(db.paper_dir("p") / "sentences.jsonl"))
        rows[0]["text"] += " changed"
        db.write_jsonl(db.paper_dir("p") / "sentences.jsonl", rows)
        self.assertIsNone(node_cards.load("v", "local:eg"))
        self.assertEqual(node_cards.load_status("v", "local:eg")["card_currency"], "stale")


class SentenceSelectionTests(unittest.TestCase):
    @staticmethod
    def _fake_payload(paper_id, total=320):
        rows = []
        for seq in range(1, total + 1):
            rows.append({"id": f"{paper_id}:S{seq:04d}", "sentence_id": f"{paper_id}:S{seq:04d}",
                         "paper_id": paper_id, "seq": seq, "page": 1, "text": f"Direct {seq}",
                         "kind": "text", "hedges": [], "direct_mention": True,
                         "evidence_sentence": False, "context": False})
        rows[0]["evidence_sentence"] = True
        return ({"paper": {"paper_id": paper_id, "label": paper_id, "year": 2024,
                            "evidence_type": "primary"}, "sentences": rows,
                 "evidence_units": [], "captions": [], "mandatory_ids": [rows[0]["id"]],
                 "context_ids": [], "priority_ids": {"evidence_unit": [rows[0]["id"]],
                    "protocol_variant": [], "definition_abbreviation_caption": [],
                    "direct_mention": [row["id"] for row in rows], "context": []},
                 "include_allowed_ids": [row["id"] for row in rows]}, {})

    def test_priority_and_round_robin_selection_under_500(self):
        payloads = [self._fake_payload("p1"), self._fake_payload("p2")]
        registry = type("Registry", (), {"by_id": {"local:x": {"concept_id": "local:x", "name": "X",
                                                                      "type": "concept", "synonyms": []}},
                                        "lookup": lambda self, value: self.by_id.get(value)})()
        with patch.object(node_cards, "_registry", return_value=registry), \
             patch.object(node_cards, "_node_inputs", return_value=([], ["p1", "p2"], [])), \
             patch.object(node_cards, "_protocol_variants", return_value=[]), \
             patch.object(node_cards, "_paper_payload", side_effect=payloads), \
             patch.object(node_cards, "_source_revision", return_value="rev"), \
             patch.object(node_cards, "_schema", return_value={"allowed_types": node_cards.ALLOWED_TYPES}):
            result = node_cards.task(object(), "v", "local:x")
        self.assertEqual(len(result["sentences"]), 500)
        self.assertEqual(result["sentences"][0]["id"], "p1:S0001")
        self.assertEqual(result["trimmed"]["selected_by_priority"]["evidence_unit"], 2)
        self.assertEqual(result["trimmed"]["trimmed"], 140)
        self.assertEqual(len([row for row in result["sentences"] if row["paper_id"] == "p1"]), 250)
        self.assertEqual(len([row for row in result["sentences"] if row["paper_id"] == "p2"]), 250)

    def test_explicit_sentence_is_retained_and_invalid_one_reported(self):
        payloads = [self._fake_payload("p1"), self._fake_payload("p2")]
        registry = type("Registry", (), {"by_id": {"local:x": {"concept_id": "local:x", "name": "X",
                                                                      "type": "concept", "synonyms": []}},
                                        "lookup": lambda self, value: self.by_id.get(value)})()
        with patch.object(node_cards, "_registry", return_value=registry), \
             patch.object(node_cards, "_node_inputs", return_value=([], ["p1", "p2"], [])), \
             patch.object(node_cards, "_protocol_variants", return_value=[]), \
             patch.object(node_cards, "_paper_payload", side_effect=payloads), \
             patch.object(node_cards, "_source_revision", return_value="rev"), \
             patch.object(node_cards, "_schema", return_value={"allowed_types": node_cards.ALLOWED_TYPES}):
            result = node_cards.task(object(), "v", "local:x", ["p1:S0319", "p1:S9999"])
        ids = {row["id"] for row in result["sentences"]}
        self.assertIn("p1:S0319", ids)
        self.assertEqual(result["trimmed"]["include_sentence_ids"], ["p1:S0319"])
        self.assertEqual(result["trimmed"]["rejected_include_sentence_ids"], ["p1:S9999"])


if __name__ == "__main__":
    unittest.main()


class RowFieldNotFoundTests(unittest.TestCase):
    def test_row_field_may_be_not_found(self):
        payload = {"schema": {"types": {"substance": {"fields": {"concentrations_seen": {"kind": "concentration_rows"}}}}}}
        fields = {"concentrations_seen": {"status": "not_found_in_vault"}}
        self.assertEqual(node_cards._check_structured_rows(fields, "substance", {}, payload), [])
        fields = {"concentrations_seen": {"value": "5 mM"}}
        self.assertEqual(node_cards._check_structured_rows(fields, "substance", {}, payload)[0]["code"], "row_shape")
