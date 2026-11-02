"""D-PENDING-15: unnumbered reference entries split at the right places, and evidence keeps pointing at the
same entry (by its printed text) when a re-extraction renumbers the reference list."""
import unittest

from kbsys import db, references, renumber
from . import test_core
from .test_renumber import unit

P = 'test2026_example_abcd'


def lines(*texts):
    return [{'page': 1, 'bbox': [0, 0, 1, 1], 'text': t} for t in texts]


def entries(*texts):
    return [" ".join(e['lines']) for e in references._unnumbered(lines(*texts))]


class SplitTests(unittest.TestCase):
    def test_entry_after_a_doi_ending_in_a_letter_starts_a_new_entry(self):
        self.assertEqual(len(entries(
            'Alder, R. (2010). Seasonal timing. Plant J. 61,', '1001–1013. doi: 10.1111/j.9999-999X.2010.01234.x',
            'Brandel, F. C. (2008). Methylation. Plant J. 1, 1-2.')), 2)

    def test_doi_wrapped_over_two_lines_ends_the_entry(self):
        self.assertEqual(len(entries(
            'Smith, J. (2003). A title here. Plant J. 36, 1-5. doi: 10.1046/j.1365-', '313X.2003.01702.x',
            'Jones, K. (2004). Another title. Cell 2, 1-2.')), 2)

    def test_accented_surname_starts_an_entry_after_a_doi(self):
        got = entries('Smith, J. (2003). A title here. Plant J. 36, 1-5. doi: 10.1000/abc.x',
                      'Fehér, A. (2015). Somatic embryogenesis. Plant Cell 1, 1-2.',
                      'Köhler, C. (2016). Another title. Cell 2, 1-2.')
        self.assertEqual([g.split(',')[0] for g in got], ['Smith', 'Fehér', 'Köhler'])

    def test_lone_surname_line_after_a_doi_starts_an_entry(self):
        got = entries('Smith, J. (2003). A title here. Plant J. 36, 1-5. doi: 10.1000/abc.x', 'Hirochika,',
                      'H., Sugimoto, K. (1996). Title of it. Plant Cell 1, 1-2.')
        self.assertEqual(len(got), 2)
        self.assertTrue(got[1].startswith('Hirochika, H.'))

    def test_journal_line_does_not_start_an_entry(self):
        got = entries('Pelham, D., and Khurana, P. (2005). Effect of stress on wheat cultures.',
                      'Indian J. Exp. Biol. 43, 740–755.',
                      'Peräkylä, M. (1998). A model study of methylation. J. Chem. 1, 1-2.')
        self.assertEqual(len(got), 2)
        self.assertTrue(got[0].endswith('Indian J. Exp. Biol. 43, 740–755.'))

    def test_entry_without_a_year_but_with_an_author_comma_is_kept(self):
        self.assertEqual(len(entries('Pelham, D. (2005). A title here. Cell 1, 1-2.',
                                     'Smith, J., and Jones, K. In press. Another title. Cell.')), 2)

    def test_latin_extended_and_decomposed_surnames_start_an_entry_after_a_doi(self):   # review of 0f21028
        doi = 'Smith, J. (2003). A title here. Plant J. 36, 1-5. doi: 10.1000/abc.x'
        for name in ('Šamaj, J.', 'Łukasiewicz, K.', 'Erdős, P.', 'Féher, A.', 'Fehér, Á.'):
            self.assertEqual(len(entries(doi, f'{name} (2005). Title of it. Cell 1, 1-2.')), 2, name)
        self.assertEqual(len(entries(doi, 'Čermák,', 'M. (2005). Title of it. Cell 1, 1-2.')), 2)

    def test_year_less_entry_with_a_particle_or_vancouver_surname_is_kept(self):   # review of 0f21028
        for second in ('van der Berg, A., and Jones, K. In press. Another title. Cell.',
                       'Smith J, Jones K. Another title. Nature. In press.'):
            self.assertEqual(len(entries('Pelham, D. (2005). A title here. Cell 1, 1-2.', second)), 2, second)

    def test_doi_wrapped_after_a_parenthesis_ends_the_entry(self):   # review of 0f21028
        self.assertEqual(len(entries('Nolan, M. (2003). Gate-mediated. Cell 113, 703–716. doi: 10.1016/S0000-0000(03)',
                                     '00001-X', 'Nash, A. T. (1992). Local positioning. Cell 1, 1-2.')), 2)

    def test_accented_name_inside_a_wrapped_author_list_does_not_split(self):
        self.assertEqual(len(entries('Smith, J., Jones, K.,', 'Köhler, A. (2001). A title here. Cell 1, 1-2.')), 1)


def ref(num, raw, numbered=False, pid=P):
    return {'ref_id': f'{pid}:R{num:03d}', 'raw': raw, 'num': num if numbered else None}


def cited(*numbers, statement='x', pid=P):
    return {'unit_id': f'{pid}:E0001', 'cited_refs': [f'{pid}:R{n:03d}' for n in numbers], 'statement': statement,
            'status': 'ok', 'flags': []}


OLD = [ref(1, 'A (2001).'), ref(2, 'B and C (2002).'), ref(3, 'D (2003).')]
NEW = [ref(1, 'A (2001).'), ref(2, 'B (2002).'), ref(3, 'C (2002).'), ref(4, 'D (2003).')]  # entry 2 became two


class CarryTests(unittest.TestCase):
    def carry(self, units, old=OLD, new=NEW):
        return renumber.carry_refs(units, old, renumber.ref_id_map(old, new), P)

    def test_shifted_entry_is_remapped_in_cited_refs_and_marker_but_free_text_is_left(self):
        (u,), flagged = self.carry([cited(3, statement='D et al. [R003] and [R001]; see Biol. 14, R003.')])
        self.assertEqual((u['cited_refs'], flagged), ([f'{P}:R004'], 0))
        self.assertEqual(u['statement'], 'D et al. [R004] and [R001]; see Biol. 14, R003.')
        self.assertEqual(u['status'], 'ok')

    def test_split_away_entry_is_flagged_and_does_not_resolve_to_its_old_number(self):
        self.assertNotIn(f'{P}:R002', renumber.ref_id_map(OLD, NEW))   # new R002 exists, but it is another entry
        (u,), flagged = self.carry([cited(2, statement='B and C [R002] reported it.')])
        self.assertEqual(flagged, 1)
        self.assertEqual((u['cited_refs'], u['statement']), ([f'{P}:R002'], 'B and C [R002] reported it.'))
        self.assertEqual((u['status'], [f['code'] for f in u['flags']]), ('flagged', ['V6']))
        self.assertIn('B and C (2002)', u['flags'][0]['message'])

    def test_chain_shift_does_not_cascade(self):
        old = [ref(40, 'P (2001).'), ref(45, 'Q (2002).')]
        new = [ref(45, 'P (2001).'), ref(50, 'Q (2002).')]
        (u,), _ = self.carry([cited(40, 45, statement='P [R040, R045] and [R040–R045]')], old, new)
        self.assertEqual(u['cited_refs'], [f'{P}:R045', f'{P}:R050'])
        self.assertEqual(u['statement'], 'P [R045, R050] and [R045–R050]')

    def test_short_form_and_duplicate_text(self):
        old = [ref(1, 'Same (2001).'), ref(2, 'Same (2001).')]
        new = [ref(5, 'Same (2001).'), ref(6, 'Same (2001).')]
        (u,), _ = self.carry([{**cited(), 'cited_refs': ['R002', 'R001']}], old, new)
        self.assertEqual(u['cited_refs'], ['R006', 'R005'])

    def test_unchanged_list_changes_nothing(self):
        units = [cited(1, 3, statement='A [R001] D [R003]')]
        out, flagged = self.carry(units, OLD, OLD)
        self.assertEqual(flagged, 0)
        self.assertIs(out[0], units[0])

    def test_numbered_entry_with_a_re_read_text_keeps_its_number(self):
        old = [ref(1, 'Smith J. 2001 Title.', True)]
        new = [ref(1, 'Smith J. 2001 Title. ', True)]
        self.assertEqual(renumber.ref_id_map(old, new), {f'{P}:R001': f'{P}:R001'})


class CarryOverFileTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def test_carry_over_rewrites_evidence_once_and_a_second_run_writes_nothing(self):
        pid = self.pid
        old = [ref(r_num, r['raw'], pid=pid) for r_num, r in enumerate(OLD, start=1)]
        new = [ref(r_num, r['raw'], pid=pid) for r_num, r in enumerate(NEW, start=1)]
        ids = [self.rows[0]['sentence_id']]
        db.write_jsonl(self.folder / 'evidence.jsonl', [
            {**unit(pid, n, self.rows, ids), **cited(r, statement=f'X [R{r:03d}]', pid=pid), 'unit_id': f'{pid}:E{n:04d}'}
            for n, r in ((1, 3), (2, 2))])
        first = renumber.carry_over(pid, self.rows, self.rows, renumber.snapshot(pid), None, old, new)
        self.assertEqual(first['reference_ids'], {'renumbered': 1, 'unmapped': 1})
        units = list(db.read_jsonl(self.folder / 'evidence.jsonl'))
        self.assertEqual([u['cited_refs'] for u in units], [[f'{pid}:R004'], [f'{pid}:R002']])
        self.assertEqual([u['statement'] for u in units], ['X [R004]', 'X [R002]'])
        self.assertEqual([u['status'] for u in units], ['ok', 'flagged'])
        second = renumber.carry_over(pid, self.rows, self.rows, renumber.snapshot(pid), None, new, new)
        self.assertEqual((second['files'], second['reference_ids']), ([], {'renumbered': 0, 'unmapped': 0}))


class RecheckTests(unittest.TestCase):
    setUp = test_core.Demo3Tests.setUp
    tearDown = test_core.Demo3Tests.tearDown

    def test_recheck_keeps_the_cited_reference_flag_even_when_validation_now_passes(self):
        from unittest.mock import patch
        from kbsys import evidence
        ids = [self.rows[0]['sentence_id']]
        flagged = {**unit(self.pid, 1, self.rows, ids), 'cited_refs': [f'{self.pid}:R002'], 'statement': 'X [R002]'}
        flagged = renumber.carry_refs([flagged], [ref(2, 'B and C (2002).', pid=self.pid)], {}, self.pid)[0][0]
        self.assertEqual([f['code'] for f in flagged['flags']], ['V6'])
        db.write_jsonl(self.folder / 'evidence.jsonl', [flagged])
        db.write_json(self.folder / 'analysis.json', {'version': 2, 'n_flagged': 1})

        def accept(con, paper_id, submission, legacy_terms_warning=False):   # R002 is cited there now: V6 passes
            return ({'flags': [], 'errors': [], 'coverage_missing': [], 'warnings': []},
                    [{**flagged, 'flags': [], 'status': 'ok', '_validation_unit_index': 1}], [])

        with patch.object(evidence, 'validate', accept), patch.object(evidence, '_review_items'),                 patch.object(evidence.notes, 'write_paper_note'):
            evidence.recheck(self.con, self.pid)
        (after,) = db.read_jsonl(self.folder / 'evidence.jsonl')
        self.assertEqual((after['status'], [f['code'] for f in after['flags']]), ('flagged', ['V6']))


if __name__ == '__main__':
    unittest.main()
