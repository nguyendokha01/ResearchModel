"""Small, store-independent tests for the dictionary-style hedge detector."""
import re
import tempfile
import unittest
from pathlib import Path

from kbsys import answer, evidence, hedges, structure


class HedgeLexiconTests(unittest.TestCase):
    def test_each_category_has_a_realistic_hit(self):
        examples = {
            'modal': 'The treatment may improve survival.',
            'appearance': 'The results seem to indicate a possible benefit.',
            'hypothesis': 'We thought the pathway was involved; this is putative.',
            'association': 'The marker was associated with outcome.',
            'qualification': 'The increase was slightly larger (p = 0.07).',
            'non_significance': 'There was no significant difference between groups.',
            'trend': 'Values trended toward recovery.',
            'scope': 'In our hands, these preliminary observations need confirmation.',
            'negated_hedge': 'The evidence is not conclusive and not necessarily causal.',
            'vietnamese': 'Kết quả dường như có vẻ như chỉ hơi khác biệt.',
        }
        categories = {hit['category'] for text in examples.values() for hit in structure.uncertainty(text)}
        self.assertTrue(set(examples), categories)
        self.assertTrue(set(examples) <= categories, categories)

    def test_negative_and_numeric_controls_are_not_flagged(self):
        self.assertEqual([], structure.uncertainty('The treatment could not improve survival.'))
        self.assertEqual([], structure.uncertainty('The groups significantly increased (p < 0.01).'))
        self.assertEqual([], structure.uncertainty('The pore measured approximately 20 µm.'))
        # Pilot run: "p < 0.05" was flagged as a hedge and blocked an overview finding.
        self.assertEqual([], structure.uncertainty('ATP was significantly higher (p < 0.05).'))
        self.assertEqual(['non_significance'], [m['category'] for m in structure.uncertainty('No difference (P > 0.05).')])
        self.assertEqual(['qualification'], [m['category'] for m in structure.uncertainty('A trend (p = 0.07).')])

    def test_association_inflections_are_not_causal_language(self):
        for term in ('correlate', 'correlates', 'correlated', 'correlation', 'correlations'):
            hits = structure.uncertainty(f'Expression may {term} with dose.' if term.startswith('correlat') else
                                         f'The {term} was reported.')
            self.assertTrue(any(hit['category'] == 'association' for hit in hits), term)

        items = [{'text': 'Expression was correlated with dose.', 'paper_id': 'p',
                  'kind': 'sentence', 'unit_type': None}]
        for verb in ('caused', 'induced'):
            checked = answer.check_sentence({'text': f'Expression {verb} the response [#1].'}, items, {})
            self.assertEqual(checked['check'], 'struck:A3', verb)

    def test_common_biomedical_hedges_are_detected(self):
        expected = {
            'tend to': 'trend',
            'appear to': 'appearance',
            'putative': 'hypothesis',
            'presumably': 'hypothesis',
            'likely': 'modal',
            'possibly': 'modal',
            'trend toward': 'trend',
            'marginally significant': 'qualification',
            'did not reach significance': 'non_significance',
            'might': 'modal',
            'could': 'modal',
            'suggestive of': 'appearance',
            'consistent with': 'qualification',
            'in line with': 'qualification',
            'implicated in': 'association',
            'associated with': 'association',
        }
        for term, category in expected.items():
            hits = structure.uncertainty(f'The finding was {term} the prior result.')
            self.assertTrue(any(hit['category'] == category for hit in hits), term)

    def test_vietnamese_and_output_compatibility(self):
        hits = structure.uncertainty('Kết quả có thể chưa đủ chắc chắn.')
        self.assertTrue(hits)
        self.assertTrue({'category', 'text', 'start', 'end', 'strength', 'label_vi'} <= set(hits[0]))
        self.assertEqual(hits[0]['text'], 'có thể')

    def test_malformed_or_missing_yaml_fails_loudly(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'bad.yaml'
            path.write_text('entries: [this is not a valid entry', encoding='utf-8')
            with self.assertRaises(Exception):
                hedges.load(path)
            with self.assertRaises(OSError):
                hedges.load(Path(directory) / 'missing.yaml')

    def test_longest_overlapping_span_wins(self):
        entries = [
            ('short', re.compile(r'(?<![\w])not significant(?![\w])', re.I), 'weak', 'ngắn'),
            ('long', re.compile(r'(?<![\w])not statistically significant(?![\w])', re.I), 'strong', 'dài'),
        ]
        hits = hedges.uncertainty('The result was not statistically significant.', entries)
        self.assertEqual(1, len(hits))
        self.assertEqual('not statistically significant', hits[0]['text'])
        self.assertEqual('long', hits[0]['category'])


class HedgeNounVerbTests(unittest.TestCase):
    def test_names_compounds_and_noun_potential_are_not_hedges(self):
        for text in (
            "Regional improvement Association of Examples",
            "RNA-associated silencing",
            "The treatment had potential toxicity.",
        ):
            self.assertEqual([], structure.uncertainty(text), text)

    def test_potential_verb_and_rewritten_relation_words_are_hedges(self):
        self.assertTrue(structure.uncertainty("The treatment could potentially reduce damage."))
        self.assertTrue(structure.uncertainty("The method has the potential to improve survival."))
        for text in ("The result was suggesting a possible interaction.",
                     "The authors proposed a mechanism.",
                     "The findings associate expression with survival.",
                     "The analysis was linking the marker to outcome."):
            self.assertTrue(structure.uncertainty(text), text)

        items = [{"text": "The marker was associated with survival.", "paper_id": "p",
                  "kind": "sentence", "unit_type": None}]
        checked = answer.check_sentence(
            {"text": "The marker was linking the marker to survival [1].", "evidence": [1]}, items, {})
        self.assertNotIn("A7", checked["problems"])


class SingleVocabularyTests(unittest.TestCase):
    def test_organisation_names_and_x_associated_are_not_hedges(self):
        for text in ("Presented by the American Medical Association in 2019.",
                     "Association for Research in Vision and Ophthalmology.",
                     "Regional improvement Association of Examples",
                     "Mid-Atlantic Cryobiology Association members",
                     "RNA-associated silencing", "Histone-associated factors were measured."):
            self.assertEqual([], hedges.uncertainty(text), text)
            self.assertIsNone(evidence.HEDGE.search(text), text)
        for text in ("An association between age and yield was found.",
                     "Association between age and yield was found.",
                     "Association of age with yield was found.",
                     "Associations were seen in older mice."):
            self.assertTrue(hedges.uncertainty(text), text)

    def test_evidence_validator_uses_the_lexicon(self):
        for word in ("suggesting", "proposed", "associate", "linking", "correlation", "possible", "apparently",
                     "hypothesized", "speculated", "probable", "barely", "appearing", "potentially"):
            self.assertTrue(evidence.HEDGE.search(f"The data were {word} a role."), word)
        self.assertIsNone(evidence.HEDGE.search("It is possible to measure it."))
        self.assertIsNone(evidence.HEDGE.search("It could not improve survival."))
        self.assertTrue(evidence.MODAL_HEDGE.search("The value was slightly higher."))
        self.assertIsNone(evidence.MODAL_HEDGE.search("Expression was correlated with dose."))
        self.assertTrue(evidence.ASSOCIATION_WORD.search("Expression was correlated with dose."))


class AssociationAdjectiveTests(unittest.TestCase):
    def test_adjective_associated_is_not_a_hedge_but_relation_is(self):
        from kbsys import structure
        self.assertFalse(structure.uncertainty("Obesity and associated disease states impair fertility."))
        for text in ("The marker was associated with survival.", "An association between age and yield was found.",
                     "Expression was significantly associated in older mice."):
            self.assertTrue(structure.uncertainty(text), text)


class VietnameseHedgeWordsTests(unittest.TestCase):
    def test_the_old_answer_hedge_words_are_in_the_lexicon_only(self):
        # G3: answer.VI_HEDGE was a second list; every one of its words must still count as a hedge, from the lexicon.
        from kbsys import answer
        self.assertFalse(hasattr(answer, "VI_HEDGE"))
        for word in ("có thể", "có lẽ", "gợi ý", "cho thấy khả năng", "giả thuyết", "đề xuất", "cho rằng", "dường như",
                     "liên quan", "có liên hệ", "tương quan", "chưa chắc", "chưa rõ", "nghi ngờ", "khả năng"):
            self.assertTrue(answer._is_hedged(f"Kết quả {word} khác biệt."), word)
        self.assertFalse(answer._is_hedged("Kết quả khác biệt giữa hai nhóm."))
        self.assertFalse(answer._is_hedged("Kết quả liên quanx khác biệt."))  # inside a longer word: no hit


class ClaimHedgingTests(unittest.TestCase):
    """EV-06: the stored `hedging` is judged on the claim sentence(s) and without non-significance; V5 keeps every hedge."""
    TEXTS = ["Survival increased to 82% in treated embryos.", "These data may reflect the medium used.",
             "Survival did not differ between groups (P > 0.05)."]

    def check(self, **unit):
        from kbsys import concepts
        sents = {f"P:S{n:04d}": {"sentence_id": f"P:S{n:04d}", "seq": n, "passage_id": "a", "page": 1, "section": "results",
                                 "kind": "text", "text": t, "citations": [], "mentions": []}
                 for n, t in enumerate(self.TEXTS, start=1)}
        ctx = evidence.PaperContext.__new__(evidence.PaperContext)
        ctx.paper_id, ctx.meta, ctx.table_caption_ids, ctx.refs, ctx.sentences = "P", {}, set(), {}, sents
        ctx._index()
        ctx.registry, ctx.canonical = concepts.Registry([]), {"_groups": {}}
        errors, flags, normalized = [], [], []
        got = evidence._check_unit(ctx, 1, {"unit_type": "result", "terms": {"concepts": ["Survival"]}, **unit}, False,
                                   lambda i, c, m: errors.append(c), lambda i, c, m: flags.append(c), normalized)
        return got, errors, flags, [n for n in normalized if n.get("field") == "hedging"]  # not the EV-17 rule fills

    def test_hedging_follows_the_claim_sentence_not_every_cited_sentence(self):
        unit = {"statement": "Survival increased to 82%.", "sentence_ids": ["S0001", "S0002"], "hedging": "none"}
        got, errors, _, normalized = self.check(**unit)
        self.assertEqual((got["hedging"], len(normalized)), ("hedged", 1))   # no claim sentence named: the whole unit is read
        got, errors, _, normalized = self.check(**unit, hedged_sentence_ids=["S0001"])
        self.assertEqual((errors, got["hedging"], normalized, got["hedged_sentence_ids"]), ([], "none", [], ["P:S0001"]))
        got, _, _, _ = self.check(**unit, hedged_sentence_ids=["S0002"])
        self.assertEqual(got["hedging"], "hedged")

    def test_a_null_result_is_no_hedge_but_v5_still_sees_it(self):
        unit = {"sentence_ids": ["S0003"], "hedging": "none"}
        got, errors, flags, normalized = self.check(statement="Survival did not differ.", **unit)
        self.assertEqual((errors, flags, normalized, got["hedging"]), ([], [], [], "none"))
        _, _, flags, _ = self.check(statement="Treatment causes the survival difference.", **unit)
        self.assertEqual(flags, ["V5"])

    def test_unknown_hedged_sentence_id_is_a_v1_error_and_the_key_is_omitted_when_absent(self):
        _, errors, _, _ = self.check(statement="Survival increased to 82%.", sentence_ids=["S0001"],
                                     hedged_sentence_ids=["S0002"])
        self.assertEqual(errors, ["V1"])
        got, errors, _, _ = self.check(statement="Survival increased to 82%.", sentence_ids=["S0001"])
        self.assertEqual(errors, [])
        self.assertNotIn("hedged_sentence_ids", got)

    def test_hedged_sentence_ids_empty_is_v1_and_stored_once_in_sentence_ids_order(self):
        unit = {"statement": "Survival increased to 82%.", "sentence_ids": ["S0001", "S0002"]}
        _, errors, _, _ = self.check(**unit, hedged_sentence_ids=[])
        self.assertEqual(errors, ["V1"])
        got, errors, _, _ = self.check(**unit, hedged_sentence_ids=["S0002", "P:S0001", "S0002"])
        self.assertEqual((errors, got["hedged_sentence_ids"]), ([], ["P:S0001", "P:S0002"]))


if __name__ == '__main__':
    unittest.main()
