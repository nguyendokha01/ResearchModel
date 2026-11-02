"""N3d: each section of a node note contains only its own kind of information (deterministic rules, no AI)."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from kbsys import concept_notes, config, node_profile, presentation


def ref(sid, text, section="introduction", page=1):
    return {"sentence_id": f"p:{sid}", "paper_id": "p", "text": text, "section": section, "page": page}


def field(*refs):
    return {"items": list(refs)}


def paper(fields=None, variants=(), pid="p"):
    return {"paper_id": pid, "cite": "Doe 2026", "note_name": "Doe", "fields": fields or {}, "protocol_variants": list(variants)}


def sections(record, profile, **kw):
    """{H2: body lines} of the typed sections."""
    out, current = {}, None
    for line in concept_notes._typed_sections(record, profile, None, **kw):
        if line.startswith("## "):
            current = out.setdefault(line[3:], [])
        elif current is not None:
            current.append(line)
    return out


class SentenceFormTests(unittest.TestCase):
    def test_definition_needs_the_node_as_subject_of_a_definitional_form(self):
        d = node_profile.definitional
        self.assertTrue(d("Vitrification is a method for cryopreservation.", "Vitrification"))
        self.assertTrue(d("Oocytes are the female gametes.", "Oocyte"))
        self.assertTrue(d("Chromatin, a complex of DNA and protein, packages the genome.", "Chromatin"))
        self.assertTrue(d("Compared with the older method, vitrification is a simple protocol.", "Vitrification"))
        self.assertTrue(d("The authors define the term vitrification as solidification into a glass.", "vitrification"))
        self.assertTrue(d("EG (ethylene glycol) is a permeating cryoprotectant.", "EG"))
        # name present, but not defined: another subject, a prepositional object, a result, a past-tense event
        self.assertFalse(d("Cryopreservation of oocytes is an important procedure.", "Oocyte"))
        self.assertFalse(d("During vitrification, the sample vials are subjected to fast chilling.", "Vitrification"))
        self.assertFalse(d("The probe was not cross-linked with DNA.", "DNA"))
        self.assertFalse(d("Oocytes were obtained from ovaries.", "Oocyte"))
        self.assertFalse(d("Vitrification increased ROS.", "Vitrification"))

    def test_purpose_marker_and_concentration_forms(self):
        self.assertTrue(node_profile.purposive("Vitrification is widely used to preserve embryos.", "Vitrification"))
        self.assertFalse(node_profile.purposive("Vitrification is widely used in animal breeding.", "Vitrification"))
        m = node_profile.states_marker
        self.assertTrue(m("Mature oocytes express Zp3 and Gdf9.", "Oocyte"))
        self.assertTrue(m("Oct4 is a marker of oocytes.", "Oocyte"))
        self.assertFalse(m("Vitrification had a negative impact on embryo quality.", "Embryo"))
        self.assertFalse(m("Hoechst was used as a DNA marker in embryos.", "Embryo"))
        c = node_profile.concentration_near
        self.assertTrue(c("Cells were treated with 10 uM DNA ligase inhibitor.", "10 uM", "DNA"))
        self.assertFalse(c("Cells were treated with the B02 inhibitor at 10 uM and then washed several times; DNA was extracted.",
                           "10 uM", "DNA"))
        self.assertTrue(c("x", "7.5% ethylene glycol", "ethylene glycol"))

    def test_pdf_superscript_artefacts_are_cleaned_but_micro_signs_stay(self):
        clean = presentation.clean_pdf_text
        self.assertEqual(clean("vesicle sorting machinery^51, and"), "vesicle sorting machinery, and")
        self.assertEqual(clean("Alphason^1,5,6 and m^6A marks"), "Alphason and m6A marks")
        self.assertEqual(clean("50 ng ml^−1 and 2 x 10^6 cells"), "50 ng ml−1 and 2 x 10^6 cells")
        self.assertEqual(clean("100 µm vs 100 μm"), "100 µm vs 100 μm")
        self.assertTrue(presentation.has_pdf_artefact("cooling^7 and") and not presentation.has_pdf_artefact("cooling and"))
        self.assertEqual(presentation.object_title("figure", "p:F1", None, "Figure 1. Loss of H3.3 in oocytes^12. A) x", {"cite": "A 1"}),
                         "Figure 1 — Loss of H3.3 in oocytes (A 1)")

    def test_protocol_detail_routing(self):
        r = concept_notes._route_detail
        self.assertEqual(r("chemical", "7.5% ethylene glycol"), "Reagents & concentrations")
        self.assertEqual(r("concentration", "5% O2, 5% CO2, and 90% N2"), "Equipment & parameters")  # gas mix
        self.assertIsNone(r("concentration", "between 50% and 100% of the standard"))
        self.assertIsNone(r("concentration", "0.5 μM"))  # an amount without a reagent
        self.assertEqual(r("duration", "13 min"), "Equipment & parameters")
        self.assertEqual(r("temperature", "38.5°C"), "Equipment & parameters")
        for value in ("one month", "day 5 post-warming", "every other day", "August 2022 and December 2024"):
            self.assertIsNone(r("timing", value), value)
        self.assertEqual(r("equipment", "Cryotop"), "Equipment & parameters")


class SectionContentTests(unittest.TestCase):
    def test_each_section_takes_only_its_own_sentences(self):
        definition = ref("S1", "Vitrification is a method for cryopreservation.")
        profile = {"entity_kind": "method", "name": "Vitrification", "aliases": [], "papers": [paper(
            {"definitions": field(definition), "purpose": field(), "markers": field()},
            [{"name": "Cryotop protocol", "steps": [ref("S3", "Embryos were loaded onto a Cryotop.", "methods")],
              "sources": [], "details": {
                  "chemical": {"items": [{"value": "15% EG", "sources": [ref("S3", "x", "methods")]}]},
                  "concentration": {"items": [{"value": "5% O2, 5% CO2", "sources": [ref("S3", "x", "methods")]}]},
                  "duration": {"items": [{"value": "one month", "sources": []}, {"value": "13 min", "sources": []}]},
                  "equipment": {"items": [{"value": "Cryotop", "sources": []}]}}}])]}
        out = sections({"name": "Vitrification", "type": "method"}, profile)
        self.assertIn("Vitrification is a method", " ".join(out["Purpose"]))
        self.assertIn("loaded onto a Cryotop", " ".join(out["Protocol steps"]))
        self.assertIn("15% EG", " ".join(out["Reagents & concentrations"]))
        self.assertNotIn("O2", " ".join(out["Reagents & concentrations"]))
        params = " ".join(out["Equipment & parameters"])
        self.assertIn("5% O2, 5% CO2", params)
        self.assertIn("13 min", params)
        self.assertNotIn("one month", params)
        self.assertIn("Cryotop protocol", " ".join(out["Variants across papers"]))
        self.assertNotIn("Measured effects", out)

    def test_a_sentence_is_shown_in_the_most_specific_section_only(self):
        both = ref("S1", "Oocytes express Zp3, a marker of oocytes.")
        profile = {"entity_kind": "cell_type", "name": "Oocyte", "aliases": [], "papers": [paper(
            {"definitions": field(both, ref("S2", "Oocytes are female gametes.")), "markers": field(both)})]}
        out = sections({"name": "Oocyte", "type": "cell_type"}, profile)
        self.assertIn("Zp3", " ".join(out["Markers"]))
        self.assertNotIn("Zp3", " ".join(out.get("Description", [])))  # S1 stays under Markers
        self.assertIn("female gametes", " ".join(out["Description"]))

    def test_quote_is_printed_in_one_section_only_and_a_result_sentence_is_not_a_definition(self):
        text = {"S1": "EG is a cryoprotectant.", "S3": "EG increased survival."}
        card = {"node_type": "substance", "status": "accepted", "statements": [
            {"field": "definition", "text": "EG protects cells.", "sentence_ids": ["p:S1", "p:S3"]},
            {"field": "function", "text": "EG raised survival.", "sentence_ids": ["p:S3"]}]}
        refs = [ref("S1", text["S1"]), ref("S3", text["S3"], "results")]
        unit = {"unit_id": "p:E1", "unit_type": "result", "status": "ok", "sentence_ids": ["p:S3"],
                "statement": "EG increased survival.", "terms": {"molecules": ["EG"]}, "paper_id": "p",
                "paper": {"note_name": "Doe"}}
        profile = {"entity_kind": "substance", "name": "EG", "aliases": [], "card": card, "papers": [paper()]}
        with patch.object(concept_notes, "_card_refs", lambda prof, ids: [r for r in refs if r["sentence_id"] in ids]), \
                patch.object(concept_notes, "_ref_cite", lambda r: "cite"), \
                patch.object(concept_notes, "_papers_report", lambda *a, **k: ["| row |"]):
            out = sections({"name": "EG", "type": "molecule"}, profile, units=[unit])
        body = "\n".join(out["Definition"])
        self.assertIn("EG is a cryoprotectant.", body)  # definitional sentence quoted
        self.assertEqual(body.count("EG increased survival."), 1)  # S3 quoted once ...
        self.assertNotIn("EG raised survival.", body)  # ... and the result-only statement is not a definition
        self.assertIn("| row |", "\n".join(out["Measured effects"]))

    def test_findings_come_only_from_accepted_result_units_tagged_with_this_node(self):
        record = {"name": "DNA", "synonyms": []}
        base = {"unit_type": "result", "status": "ok", "sentence_ids": ["p:S1"]}
        units = [{**base, "unit_id": "tagged", "terms": {"molecules": ["DNA"]}},
                 {**base, "unit_id": "measure", "outcome": {"measure": "DNA damage"}},
                 {**base, "unit_id": "word-only", "quote": "RNA-seq was run on DNA samples", "terms": {"methods": ["RNA-seq"]}},
                 {**base, "unit_id": "flagged", "status": "flagged", "terms": {"molecules": ["DNA"]}},
                 {**base, "unit_id": "method", "unit_type": "method_detail", "terms": {"molecules": ["DNA"]}},
                 {**base, "unit_id": "hypothesis", "unit_type": "hypothesis", "terms": {"molecules": ["DNA"]}}]
        self.assertEqual([u["unit_id"] for u in concept_notes.finding_units(units, record, None)], ["tagged", "measure", "hypothesis"])

    def test_review_and_hypothesis_units_stay_in_the_table_labelled_primary_first(self):
        record = {"name": "DNA", "synonyms": []}
        base = {"status": "ok", "sentence_ids": [], "terms": {"molecules": ["DNA"]}, "statement": "S", "paper": {}, "paper_id": "p"}
        with patch.object(concept_notes, "_paper_model_labels", return_value=("", "")):
            rows = concept_notes._papers_report(record, {"papers": []}, [
            {**base, "unit_id": "p:U3", "unit_type": "hypothesis"}, {**base, "unit_id": "p:U2", "unit_type": "cited_statement"},
            {**base, "unit_id": "p:U1", "unit_type": "result"}], None)
        text = " ".join(rows)
        self.assertLess(text.index("result"), text.index("cited study"))
        self.assertLess(text.index("cited study"), text.index("hypothesis — not a finding"))

    def test_every_protocol_step_is_shown(self):
        steps = [ref(f"S{i}", f"Step number {i} was done.", "methods") for i in range(1, 9)]
        profile = {"entity_kind": "method", "name": "Vitrification", "aliases": [], "papers": [paper(
            variants=[{"name": "v", "steps": steps, "sources": [], "details": {}}])]}
        body = " ".join(sections({"name": "Vitrification", "type": "method"}, profile)["Protocol steps"])
        self.assertIn("Step number 8 was done", body)

    def test_substance_concentration_must_sit_next_to_its_own_name(self):
        near = ref("S1", "Embryos were exposed to 10 uM DNA ligase inhibitor for 1 h.", "methods")
        far = ref("S2", "The B02 inhibitor was applied at 0, 10, 50 uM for 24 h before the cells were harvested and DNA was extracted.", "methods")
        variant = {"name": "x", "steps": [], "sources": [], "details": {"concentration": {"items": [
            {"value": "10 uM", "sources": [near]}, {"value": "0, 10, 50 uM", "sources": [far]}]}}}
        profile = {"entity_kind": "substance", "name": "DNA", "aliases": [], "papers": [paper(variants=[variant])]}
        body = " ".join(sections({"name": "DNA", "type": "molecule"}, profile)["Concentrations used"])
        self.assertIn("10 uM", body)
        self.assertNotIn("0, 10, 50", body)

    def test_methods_only_protocol_steps_and_no_lexical_key_findings(self):
        profile = {"entity_kind": "concept", "name": "Chromatin", "aliases": [], "papers": [paper(
            {"purpose": field(ref("S1", "Chromatin is used to ...")), "significance": field(ref("S2", "Chromatin plays an important role.", "results"))})]}
        self.assertEqual(sections({"name": "Chromatin", "type": "concept"}, profile).get("Key findings"), None)


class SummaryTests(unittest.TestCase):
    def write(self, record, units, profile=None):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with patch.multiple(config, ROOT=root, STORE=root / "store", PAPERS_DIR=root / "papers",
                                VAULTS_DIR=root / "vaults", LOGS_DIR=root / "logs"):
                concept_notes._write_note("v", record, "<!-- kb:auto:start -->\nx\n<!-- kb:auto:end -->\n", "f" * 40,
                                          units, profile or {})
                text = (root / "vaults" / "v" / "Substances" / f"{record['name']}.md").read_text(encoding="utf-8")
        return yaml.safe_load(text.split("---\n", 2)[1])

    def test_kb_summary_is_a_definition_or_the_key_is_omitted(self):
        record = {"concept_id": "dna", "name": "DNA", "type": "molecule", "synonyms": []}
        garbage = [{"status": "ok", "quote": "DNA at SHL 1.5 moves 4.5 � towards SHL 2."}]
        self.assertNotIn("kb_summary", self.write(record, garbage))
        good = [{"status": "ok", "quote": "DNA is the molecule that carries genetic information^12."}]
        self.assertEqual(self.write(record, good)["kb_summary"], "DNA is the molecule that carries genetic information.")


if __name__ == "__main__":
    unittest.main()
