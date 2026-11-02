"""D3.3 acceptance: isolated PDFs, real offline OCR, and optional read-only corpus probes.

Run: python -m tests.test_extraction [-v] [--real-root CANONICAL_ROOT]
No provider calls, downloads, original-source writes, or live-store rebuilds.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf

from kbsys import config, db, extract, media, objects, quality, tier1


def make_scan(path, embedded_text=None, rotation=0):
    with pymupdf.open() as text_pdf:
        page = text_pdf.new_page(width=600, height=400)
        page.insert_text((70, 80), 'Results', fontsize=24)
        page.insert_text((70, 140), 'Control count 8. Treated count 12.', fontsize=20)
        page.insert_text((70, 200), 'The difference may be uncertain.', fontsize=20)
        pixels = page.get_pixmap(dpi=200).tobytes('png')
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=400)
        page.insert_image(page.rect, stream=pixels)
        if embedded_text:
            page.insert_text((70, 300), embedded_text)
        page.set_rotation(rotation)
        doc.save(path)


def make_table(path, rotation=0, ruled=False):
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=840)
        if rotation == 0:
            page.insert_text((70, 80), 'Table 1. Ragged values.')
            for y, row in zip((115, 145, 175), [('Group', 'n', 'Value'), ('Control', '8', '12.5'), ('Treated', '', '18.75')]):
                for x, text in zip((70, 230, 390), row):
                    if text:
                        page.insert_text((x, y), text)
            if ruled:
                for x in (65, 225, 385, 540):
                    page.draw_line((x, 95), (x, 190))
                for y in (95, 125, 155, 190):
                    page.draw_line((65, y), (540, y))
        else:
            up = rotation == 90
            page.insert_text((100 if up else 500, 780 if up else 60), 'Table 1. Rotated values.', rotate=rotation)
            for x, row in zip((140, 175, 210) if up else (460, 425, 390),
                              [('', 'n', 'Value'), ('Control', '8', '12.5'), ('Treated', '9', '18.75')]):
                for y, text in zip((790, 630, 470) if up else (50, 210, 370), row):
                    if text:
                        page.insert_text((x, y), text, rotate=rotation)
        doc.save(path)


def make_header_table(path, rotation=0, rows=None):
    """A ruled table whose header row has a digit ("Control (n = 8)") and nothing printed above it."""
    rows = rows or [('Group', 'Control (n = 8)', 'Treated (n = 9)'), ('Survival', '12.5', '18.75'), ('Rate', '3.1', '4.2')]
    with pymupdf.open() as doc:
        page = doc.new_page(width=600, height=840)
        if rotation == 0:
            page.insert_text((70, 80), 'Table 1. Header with digits.')
            for x in (65, 225, 385, 540):
                page.draw_line((x, 95), (x, 190))
            for y in (95, 125, 155, 190):
                page.draw_line((65, y), (540, y))
            for y, row in zip((115, 145, 175), rows):
                for x, text in zip((70, 230, 390), row):
                    page.insert_text((x, y), text)
        else:  # the same grid printed sideways
            page.insert_text((100, 300), 'Table 1. Header with digits.', rotate=90)
            for y in (790, 630, 470, 310):
                page.draw_line((135, y), (240, y))
            for x in (135, 170, 205, 240):
                page.draw_line((x, 790), (x, 310))
            for x, row in zip((160, 195, 230), rows):
                for y, text in zip((780, 620, 460), row):
                    page.insert_text((x, y), text, rotate=90)
        doc.save(path)


class ExtractionAcceptance(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='d3-extraction-')
        self.root = Path(self.temporary.name)
        self.mapping = {'ROOT': self.root, 'CONFIG_DIR': self.root / 'config', 'STORE': self.root / 'store',
                        'DB_PATH': self.root / 'store/library.db', 'PAPERS_DIR': self.root / 'store/papers',
                        'LOGS_DIR': self.root / 'logs', 'CACHE_DIR': self.root / 'cache',
                        'VAULTS_DIR': self.root / 'vaults', 'REVIEW_PENDING': self.root / 'review'}
        self.paths = patch.multiple(config, **self.mapping)
        self.paths.start()
        self.pdf = self.root / 'fixture.pdf'
        self.folder = db.paper_dir('fixture')
        self.con = db.connect()

    def tearDown(self):
        self.con.close()
        self.paths.stop()
        self.temporary.cleanup()

    def seed(self, ex, path=None):
        path = path or self.pdf
        meta = {'paper_id': 'fixture', 'title': 'Artificial extraction fixture', 'vaults': [],
                'authors': [], 'text_quality': ex.text_quality, 'ocr_pages': ex.ocr_pages,
                'source_files': [{'role': 'main', 'path': str(path), 'status': 'active',
                                  'sha256': extract.sha256_file(path)}], 'supplements': []}
        db.write_json(self.folder / 'metadata.json', meta)
        db.write_jsonl(self.folder / 'pages.jsonl', ex.pages)
        db.write_jsonl(self.folder / 'passages.jsonl', [{**p, 'passage_id': f"fixture:{p['seq']:04d}"} for p in ex.passages])
        db.write_json(self.folder / 'sections.json', ex.sections)
        return meta

    def read_tables(self):
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        before = extract.sha256_file(self.pdf)
        with pymupdf.open(self.pdf) as doc:
            figures, tables = objects.build('fixture', doc, ex.captions, 'main', self.root / 'crops')
        self.assertEqual(before, extract.sha256_file(self.pdf))
        return ex, figures, tables

    def test_actual_offline_scan_keeps_numbers_hedge_and_provenance(self):
        if importlib.util.find_spec('rapidocr_onnxruntime') is None:
            self.skipTest('Installed offline RapidOCR unavailable; no install/download attempted')
        make_scan(self.pdf)
        before = extract.sha256_file(self.pdf)
        ex = extract.extract(self.pdf)
        self.assertEqual(extract.sha256_file(self.pdf), before)
        self.assertEqual(len(ex.ocr_pages), 1)
        self.assertEqual(ex.pages[0]['ocr_attempt']['status'], 'used')
        self.assertEqual(ex.pages[0]['ocr_attempt']['embedded_chars'], 0)
        text = ' '.join(p['text'] for p in ex.passages)
        for expected in ('Control count 8.', 'Treated count 12.', 'may be uncertain.'):
            self.assertIn(expected, text)
        for passage in ex.passages:
            self.assertEqual(passage['ocr']['engine'], ex.ocr_pages[0]['engine'])
            self.assertNotIn('lay', passage)  # OCR sizes are invented: such rows keep the old text rule
        print('REAL_OCR', json.dumps({'source_sha256': before, 'ocr_pages': ex.ocr_pages, 'text': text}))

    def test_ocr_disabled_is_explicit_even_when_paper_has_other_text(self):
        make_scan(self.pdf)
        with pymupdf.open(self.pdf) as doc:
            page = doc.new_page()
            page.insert_textbox((70, 70, 500, 700), 'Readable text. ' * 150)
            doc.save(self.root / 'mixed.pdf')
        self.pdf = self.root / 'mixed.pdf'
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.assertNotEqual(ex.text_quality, 'no_text')
        self.seed(ex)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        report = quality.inspect('fixture', scan_fonts=False)
        self.assertFalse(report['ready_for_analysis'])
        self.assertIn({'code': 'ocr_disabled', 'source': 'main', 'page': 1, 'embedded_chars': 0}, report['blockers'])

    def test_unavailable_empty_and_failed_ocr_remain_explicit(self):
        make_scan(self.pdf, embedded_text='p = 0.047')
        before = extract.sha256_file(self.pdf)
        for result, error, status in ((None, None, 'unavailable'), ([], None, 'no_text'),
                                      (None, RuntimeError('offline model failed'), 'failed')):
            with self.subTest(status=status), patch.object(extract, '_ocr_lines', return_value=result, side_effect=error):
                ex = extract.extract(self.pdf)
                self.assertEqual(ex.pages[0]['ocr_attempt']['status'], status)
                self.assertIn('0.047', ex.pages[0]['text'])
                self.assertEqual(ex.ocr_pages, [])
                self.assertEqual(before, extract.sha256_file(self.pdf))
                self.seed(ex)
                tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
                codes = {b['code'] for b in quality.inspect('fixture', scan_fonts=False)['blockers']}
                self.assertIn('ocr_' + status, codes)

    def test_low_embedded_text_is_preserved_on_successful_ocr(self):
        # A deterministic adapter test, separate from the real engine test above.
        make_scan(self.pdf, embedded_text='p = 0.047')
        with patch.object(extract, '_ocr_lines', return_value=[((70, 90, 300, 110), 'Control count 8.', .95)]):
            ex = extract.extract(self.pdf)
        self.assertIn('p = 0.047', ex.pages[0]['embedded_text'])
        self.assertEqual(ex.pages[0]['ocr_attempt']['status'], 'used')

    def test_low_confidence_requires_page_and_source_verification(self):
        make_scan(self.pdf)
        with patch.object(extract, '_ocr_lines', return_value=[((70, 90, 300, 110), 'Control count 8.', .42)]):
            ex = extract.extract(self.pdf)
        self.seed(ex)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        report = quality.inspect('fixture', scan_fonts=False)
        self.assertIn({'code': 'ocr_low_confidence', 'source': 'main', 'page': 1}, report['blockers'])

    def test_ragged_table_keeps_empty_cell_and_never_assigns_guessed_headers(self):
        make_table(self.pdf)
        ex, _, tables = self.read_tables()
        self.assertEqual(tables[0]['rows'], [['Control', '8', '12.5'], ['Treated', '', '18.75']])
        self.assertEqual(tables[0]['quality'], 'uncertain')
        self.seed(ex)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        rows = [s for s in db.read_jsonl(self.folder / 'sentences.jsonl') if s['kind'] == 'table_row']
        self.assertEqual([s['text'] for s in rows], ['Control | 8 | 12.5', 'Treated | 18.75'])
        report = quality.inspect('fixture', scan_fonts=False)
        self.assertIn('table_layout_uncertain', {w['code'] for w in report['warnings']})

    def test_ruled_ragged_table_preserves_source_column_names(self):
        make_table(self.pdf, ruled=True)
        _, _, tables = self.read_tables()
        self.assertEqual(tables[0]['extraction'], 'pymupdf_tables')
        self.assertEqual(tables[0]['quality'], 'ok')
        self.assertEqual(tables[0]['header'], ['Group', 'n', 'Value'])
        self.assertEqual(tables[0]['rows'], [['Control', '8', '12.5'], ['Treated', '', '18.75']])

    def test_row_zero_is_the_header_when_it_has_digits_and_nothing_sits_above_it(self):
        make_header_table(self.pdf)
        _, _, tables = self.read_tables()
        self.assertEqual(tables[0]['extraction'], 'pymupdf_tables')
        self.assertEqual(tables[0]['header'], ['Group', 'Control (n = 8)', 'Treated (n = 9)'])
        self.assertEqual(tables[0]['rows'], [['Survival', '12.5', '18.75'], ['Rate', '3.1', '4.2']])
        self.assertEqual(tables[0]['quality'], 'ok')

    def test_a_headerless_table_keeps_its_first_record_as_a_row(self):
        # promoting "Patient 1 | 41 | 22" would read every row as "41: 38 | 22: 25", a relation the paper never printed
        make_header_table(self.pdf, rows=[('Patient 1', '41', '22'), ('Patient 2', '38', '25'), ('Patient 3', '52', '27')])
        _, _, tables = self.read_tables()
        self.assertFalse(any(tables[0]['header']))
        self.assertEqual(tables[0]['rows'][0], ['Patient 1', '41', '22'])
        self.assertEqual(tables[0]['quality'], 'uncertain')

    def test_a_table_printed_sideways_is_uncertain_and_row_zero_is_not_promoted(self):
        make_header_table(self.pdf, rotation=90)
        _, _, tables = self.read_tables()
        self.assertEqual(tables[0]['extraction'], 'pymupdf_tables')  # find_tables reads it transposed
        self.assertEqual(tables[0]['quality'], 'uncertain')
        self.assertFalse(any(tables[0]['header']))

    def test_a_table_on_a_rotated_page_is_uncertain_and_row_zero_is_not_promoted(self):
        # find_tables reports displayed coordinates and get_text unrotated ones: before the guard row 0 ("4.2 | 3.1 | Rate")
        # of the transposed table became an "ok" header and every row read "4.2: 18.75 | ..."
        make_header_table(self.pdf, rotation=90)
        with pymupdf.open(self.pdf) as doc:
            doc[0].set_rotation(270)
            doc.save(self.root / 'rotated.pdf')
        self.pdf = self.root / 'rotated.pdf'
        _, _, tables = self.read_tables()
        self.assertEqual(tables[0]['extraction'], 'pymupdf_tables')
        self.assertEqual(tables[0]['quality'], 'uncertain')
        self.assertFalse(any(tables[0]['header']))

    def test_both_sideways_table_directions_preserve_printed_row_order(self):
        for rotation in (90, 270):
            with self.subTest(rotation=rotation):
                if self.pdf.exists():
                    self.pdf.unlink()  # temporary fixture only
                make_table(self.pdf, rotation)
                ex, _, tables = self.read_tables()
                self.assertEqual(len(ex.captions), 1)
                self.assertEqual(tables[0]['rows'], [['Control', '8', '12.5'], ['Treated', '9', '18.75']])
                self.assertEqual(tables[0]['header'], ['', 'n', 'Value'])
                self.assertEqual(tables[0]['quality'], 'uncertain')
                self.assertTrue((self.root / tables[0]['crop']).is_file())

    def test_unicode_and_numeric_signs_survive_embedded_pdf(self):
        expected = 'H2O2; α β μ; −5; ±2; ≤0.05; may not differ.'
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_font(fontname='fixtureunicode', fontbuffer=pymupdf.Font('helv').buffer)
            page.insert_text((70, 90), expected, fontname='fixtureunicode')
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.assertIn(expected, ex.pages[0]['text'])
        self.assertIn(expected, ' '.join(p['text'] for p in ex.passages))

    def test_right_column_inserted_first_is_read_after_left(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((330, 140), 'Right column follows left.', fontsize=11)
            page.insert_text((70, 140), 'Left column starts first.', fontsize=11)
            page.insert_text((70, 90), 'Results', fontsize=16)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        text = ' '.join(p['text'] for p in ex.passages)
        self.assertLess(text.index('Left column'), text.index('Right column'))

    def test_caption_among_references_is_not_a_reference_and_its_continuation_follows_it(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 70), 'Supplementary Information', fontsize=16)
            page.insert_textbox((70, 80, 530, 140), 'Figure S8 Banding contrast is mildly lowered in the sample panels. '
                                'Heat-map analysis shows the signal intensity of', fontsize=10)
            page.insert_text((70, 190), 'References', fontsize=16)
            page.insert_textbox((70, 200, 530, 300),
                                '1. Ames BD, Hale CD (2000) The language of toy models and plans. Nature 403: 41-45. '
                                '2. Ford W, Wu Y, Hale CD (2003) Notes on a made-up topic. Curr Opin Cell Biol 15: 172-183.',
                                fontsize=10)
            page.insert_textbox((70, 310, 530, 350), 'panels compared with controls. The red area shows banding.', fontsize=10)
            page.insert_textbox((70, 360, 530, 420), 'Figure S9 Salt washing of tagged beads. Cells were lysed in an isotonic buffer.',
                                fontsize=10)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        labelled = [(p['kind'], p['section'], p['text'][:12]) for p in ex.passages]
        self.assertIn(('caption', 'supplementary', 'Figure S9 Sa'), labelled)
        self.assertNotIn('references', {p['section'] for p in ex.passages if p['kind'] == 'caption'})
        self.assertFalse(any('Figure S9' in line['text'] for line in ex.reference_lines))
        self.assertTrue(any(p['section'] == 'references' and p['text'].startswith('1. Ames') for p in ex.passages))
        s8 = next(p for p in ex.passages if p['text'].startswith('Figure S8'))
        self.assertIn('panels compared with controls', s8['text'])         # the continuation joined its caption

    def _list_then_note(self, note, size=10):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 70), 'References', fontsize=16)
            page.insert_textbox((70, 80, 530, 150),
                                '1. Ames BD, Hale CD (2000) The language of toy models and plans. Nature 403: 41-45.',
                                fontsize=10)
            page.insert_textbox((70, 160, 530, 220),
                                '2. Ford W, Wu Y, Hale CD (2003) Notes on a made-up topic. Curr Opin Cell Biol 15: 172-183.',
                                fontsize=10)
            page.insert_textbox((70, 260, 530, 320), note, fontsize=size)
            doc.save(self.pdf)
        return extract.extract(self.pdf, ocr={'enabled': False})

    def test_publishers_note_after_the_last_reference_is_notes_not_part_of_the_reference(self):
        ex = self._list_then_note("Publisher's Note Example Press remains neutral about the disputed borders "
                                  "drawn on the printed maps.")
        self.assertEqual({p['section'] for p in ex.passages if p['text'].startswith("Publisher's Note")}, {'notes'})
        self.assertTrue(any(p['section'] == 'references' and p['text'].startswith('1. Ames') for p in ex.passages))
        self.assertNotIn('Publisher', ' '.join(line['text'] for line in ex.reference_lines))
        self.assertIn('notes', {s['name'] for s in ex.sections})

    def test_see_www_supporting_information_note_after_the_list_is_supplementary(self):
        ex = self._list_then_note('See www.example-journal.org for supporting information online.', size=11)
        self.assertEqual({p['section'] for p in ex.passages if p['text'].startswith('See www')}, {'supplementary'})
        self.assertNotIn('supporting information', ' '.join(line['text'] for line in ex.reference_lines))

    def test_a_reference_after_a_publishers_note_resumes_the_list(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 70), 'References', fontsize=16)
            page.insert_textbox((70, 80, 530, 150),
                                '1. Ames BD, Hale CD (2000) The language of toy models and plans. Nature 403: 41-45.',
                                fontsize=10)
            page.insert_textbox((70, 160, 530, 200), "Publisher's note Example Press remains neutral.", fontsize=10)
            page.insert_textbox((70, 210, 530, 270),
                                '2. Ford W, Wu Y, Hale CD (2003) Notes on a made-up topic. Curr Opin Cell Biol 15: 172-183.',
                                fontsize=10)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.assertTrue(any('Ford' in line['text'] for line in ex.reference_lines))
        self.assertFalse(any('Publisher' in line['text'] for line in ex.reference_lines))

    def test_journal_licence_line_on_page_one_is_front_matter_not_body(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 70), 'Introduction', fontsize=16)
            page.insert_textbox((70, 80, 530, 140), 'Oxidative stress may affect the ovary. ' * 4, fontsize=10)
            page.insert_textbox((70, 700, 530, 740), 'Example Journal of Test Science 2031:7 40412 © 2031 Quill et al. '
                                'This text is issued under licence by Example Press.', fontsize=8)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        extract.refine_sections(ex)
        notes = [p for p in ex.passages if p['text'].startswith('Example Journal')]
        self.assertEqual([p['section'] for p in notes], ['front_matter'])

    def heading_pdf(self, items):
        """items: ('b', y, text, size) bold line | ('p', y, text) body paragraph, all on one page."""
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            for item in items:
                if item[0] == 'b':
                    page.insert_text((70, item[1]), item[2], fontname='hebo', fontsize=item[3])
                else:
                    page.insert_textbox((70, item[1], 530, item[1] + 60), item[2], fontsize=10)
            doc.save(self.pdf)
        return extract.extract(self.pdf, ocr={'enabled': False})

    BODY = 'The treated group may differ from the control group. ' * 3

    def test_parent_heading_followed_by_a_child_heading_keeps_both_after_body_text(self):
        ex = self.heading_pdf([('b', 60, 'Results', 16), ('p', 70, self.BODY), ('b', 160, 'HDAC Isoforms', 11),
                               ('b', 200, 'Class I HDACs', 11), ('p', 210, self.BODY)])
        self.assertEqual([p['subsection'] for p in ex.passages if p['text'].startswith('The treated')],
                         [None, 'HDAC Isoforms Class I HDACs'])

    def test_front_matter_heading_lines_are_replaced_not_joined(self):
        ex = self.heading_pdf([('b', 60, 'Journal Kicker', 11), ('b', 100, 'The Article Title', 11), ('p', 110, self.BODY)])
        self.assertEqual([p['subsection'] for p in ex.passages if p['text'].startswith('The treated')], ['The Article Title'])

    def test_section_heading_closes_an_open_subsection_heading(self):
        ex = self.heading_pdf([('b', 60, 'Results', 16), ('p', 70, self.BODY), ('b', 160, 'Parent', 11),
                               ('b', 200, 'Methods', 16), ('b', 218, 'Child', 11), ('p', 230, self.BODY)])
        self.assertEqual([(p['section'], p['subsection']) for p in ex.passages if p['text'].startswith('The treated')],
                         [('results', None), ('methods', 'Child')])

    def test_run_in_label_line_is_a_paragraph_not_a_heading(self):
        ex = self.heading_pdf([('b', 60, 'Results', 16), ('p', 70, self.BODY),
                               ('b', 160, 'Key words: Mesenchymal stem cells; Muse cells; Stroke', 10), ('p', 200, self.BODY)])
        texts = [p['text'] for p in ex.passages]
        self.assertTrue(any(t.startswith('Key words: Mesenchymal') for t in texts), texts)
        self.assertEqual({p['subsection'] for p in ex.passages}, {None})

    def test_supplementary_figure_label_stays_a_caption_not_a_section_heading(self):
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 60), 'Results', fontsize=16)
            page.insert_textbox((70, 70, 530, 300), 'The treated group may differ from the control group. ' * 12, fontsize=10)
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 60), 'Supplementary Figures, Tables and Files', fontsize=16)
            page.insert_text((70, 100), 'Supplementary Figure 1', fontsize=12)
            page.insert_textbox((70, 120, 530, 180), 'Count of tank refills after the first rinse. A bar graph.', fontsize=10)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.assertIn(('caption', 'supplementary', 'Supplementary Figure 1'),
                      [(p['kind'], p['section'], p['text'][:22]) for p in ex.passages])

    def test_references_table_column_header_is_not_a_references_section(self):
        # paper A: "References" printed as a header cell beside two other header cells in the same font
        with pymupdf.open() as doc:
            page = doc.new_page(width=600, height=840)
            page.insert_text((70, 60), 'Results', fontsize=16)
            page.insert_textbox((70, 70, 530, 300), 'The treated group may differ from the control group. ' * 12, fontsize=10)
            for x, label in ((70, 'Species'), (250, 'Outcome'), (450, 'References')):
                page.insert_text((x, 340), label, fontsize=11)
                page.insert_text((x, 360), '(62)' if label == 'References' else 'Goat', fontsize=7)
            page.insert_textbox((70, 400, 530, 460), 'The next paragraph still belongs to the results section. ' * 3, fontsize=10)
            doc.save(self.pdf)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        self.assertNotIn('references', {p['section'] for p in ex.passages})
        self.assertEqual(ex.reference_lines, [])

    def test_text_table_read_as_prose_is_marked_in_table_even_with_citations(self):
        table = {'source': 'main', 'page': 3, 'header': ['Species', 'Cell type', 'Reference'],
                 'rows': [['Bovine', 'Fresh MII oocytes vitrified', 'Chen et al. [112]'],
                          ['Mouse', 'Blastocysts after warming', 'Wang et al. [7]']], 'quality': 'ok'}

        def row(text, citations):
            return {'source': 'main', 'page': 3, 'kind': 'text', 'text': text, 'citations': citations}

        cells = row('Species Cell type Reference Bovine Fresh MII oocytes vitrified Chen et al. [112]', [{'ref_nums': [112]}])
        prose = row('The embryos were warmed in a stepwise manner and cultured for two days before staining.', [])
        short = row('Bovine Fresh MII', [])
        tier1._mark_table_text([cells, prose, short], [table])
        self.assertTrue(cells.get('in_table'))
        self.assertFalse(prose.get('in_table') or short.get('in_table'))

    def test_text_right_after_a_table_caption_or_rows_is_marked_in_table_by_position(self):
        table = {'source': 'main', 'page': 3, 'label': 'Table 1', 'header': ['Group', 'Survival'],
                 'rows': [['Control', 'high']], 'quality': 'ok'}
        empty = {'source': 'main', 'page': 4, 'label': 'Table 2', 'header': [], 'rows': [], 'quality': 'uncertain'}

        def row(kind, text, page=3):
            return {'source': 'main', 'page': page, 'kind': kind, 'text': text, 'citations': []}

        caption, cells = row('caption', 'Table 1 Survival'), row('table_row', 'Control | high')
        dump = row('text', 'Group Survival Control high')
        glued = row('text', 'Group Survival Mitochondria swelled dramatically after thawing in all treated embryos.')
        run = row('text', '12 40 7 8 9')  # a number-only remainder of the same dump
        body = row('text', 'Mitochondria swelled dramatically after thawing in all treated embryos.')
        far = row('text', 'Control high', page=4)
        cap2 = row('caption', 'Table 2 Notes', page=4)
        zero = row('text', 'Reagent Cost Medium Low', page=4)  # a 0-row table: its cells are unknown
        tier1._mark_table_text([caption, cells, dump, run, body, far, cap2, zero], [table, empty])
        self.assertTrue(dump.get('in_table') and run.get('in_table') and zero.get('in_table'))
        self.assertFalse(body.get('in_table') or far.get('in_table'))
        # a header glued to a body sentence: the cells are known and few of its words are cells
        tier1._mark_table_text([caption, glued], [table])
        self.assertFalse(glued.get('in_table'))

    def test_a_sentence_holding_the_cells_of_a_table_is_marked_without_a_caption(self):
        table = {'source': 'main', 'page': 3, 'label': 'Table 1', 'header': ['Species', 'MET1', 'DRM', 'CMT3'],
                 'rows': [[f'Species{n}', '4', '3', '1'] for n in range(1, 5)], 'quality': 'ok'}

        def row(text):
            return {'source': 'main', 'page': 3, 'kind': 'text', 'text': text, 'citations': []}

        dump = row('Species MET1 DRM CMT3 Species1 Species2 Species3 Species4 4 4 4 4 3 3 3 3 1 1 1 1')  # column-wise
        prose = row('Species4 carries 4 copies of MET1 and 3 of DRM, as shown for Species1 in the earlier tests.')
        tier1._mark_table_text([prose, dump], [table])
        self.assertTrue(dump.get('in_table'))
        self.assertFalse(prose.get('in_table'))

    def test_table_text_with_a_body_sentence_glued_after_it_or_a_caption_away_from_its_table_stays_body(self):
        # review of G4.2e: paper A S0330 / paper B S0152 (cells + "...were aligned using Hisat2."), a cited sentence
        # restating a small table, and a caption at a page end whose table is on the next page hid body text
        table = {'source': 'main', 'page': 3, 'label': 'Table 1', 'header': ['Group', 'Survival', 'Blastocyst'],
                 'rows': [['Fresh', '95', '40'], ['Vitrified', '90', '35'], ['Control', '92', '38']], 'quality': 'ok'}
        later = {**table, 'page': 5, 'label': 'Table 2'}

        def row(kind, text, page=3, cites=()):
            return {'source': 'main', 'page': page, 'kind': kind, 'text': text, 'citations': list(cites)}

        tail = row('text', 'Group Survival Blastocyst Fresh 95 40 trimmed reads were mapped to the reference using Tool2.')
        quote = row('text', 'Survival in the fresh, vitrified and control group was 95, 90 and 92%, and blastocyst rates '
                            'were 40, 35 and 38%, as reported [12].', cites=[{'marker': '[12]'}])
        dump = row('text', 'Group Survival Blastocyst Fresh 95 40 Vitrified 90 35 Control 92 38')
        tier1._mark_table_text([row('caption', 'Table 1 Outcomes'), row('table_row', 'Fresh | 95 | 40'), tail, quote], [table])
        tier1._mark_table_text([row('caption', 'Table 1 Outcomes'), dump], [table])
        self.assertFalse(tail.get('in_table') or quote.get('in_table'))
        self.assertTrue(dump.get('in_table'))
        body = row('text', 'Embryos were cultured for five days and the blastocyst rate was recorded.', page=4)
        tier1._mark_table_text([row('caption', 'Table 2 Primers used in this study.', page=4), body], [later])
        self.assertFalse(body.get('in_table'))
        cont = row('text', 'Reagent Cost Medium Low', page=6)  # a continued table: its record is on an earlier page
        tier1._mark_table_text([row('caption', 'Table 2 (continued)', page=6), cont], [later])
        self.assertTrue(cont.get('in_table'))

    def test_caption_without_graphics_stays_missing(self):
        with pymupdf.open() as doc:
            page = doc.new_page()
            page.insert_text((70, 120), 'Figure 1. Source caption without the image.')
            doc.save(self.pdf)
        _, figures, _ = self.read_tables()
        self.assertEqual(len(figures), 1)
        self.assertIsNone(figures[0]['crop'])
        self.assertIsNone(figures[0]['bbox'])

    def test_archive_and_unavailable_source_keep_prior_rows_and_crop(self):
        make_table(self.pdf, ruled=True)
        ex = extract.extract(self.pdf, ocr={'enabled': False})
        meta = self.seed(ex)
        tier1.build(self.con, 'fixture', ex=ex, pdf_path=self.pdf)
        original = list(db.read_jsonl(self.folder / 'tables.jsonl'))[0]
        crop = media.crop_path(self.folder, original['crop'])
        before = crop.read_bytes()
        archived = self.root / 'archive.pdf'
        self.pdf.rename(archived)  # generated fixture, never a real source
        meta = db.read_json(self.folder / 'metadata.json')
        meta['source_files'][0].update(path=str(archived), status='archived')
        db.write_json(self.folder / 'metadata.json', meta)
        self.assertEqual(tier1.main_pdf(meta), archived)
        meta['source_files'][0]['path'] = str(self.root / 'unavailable.pdf')
        db.write_json(self.folder / 'metadata.json', meta)
        tier1.build(self.con, 'fixture')
        retained = list(db.read_jsonl(self.folder / 'tables.jsonl'))[0]
        self.assertEqual(retained['rows'], original['rows'])
        self.assertEqual(retained['crop'], original['crop'])
        self.assertEqual(crop.read_bytes(), before)
        self.assertIn('source_unavailable', {w['code'] for w in quality.inspect('fixture')['warnings']})

    def test_supplement_ocr_provenance_and_page_status_are_source_scoped(self):
        make_table(self.pdf)
        self.seed(extract.extract(self.pdf, ocr={'enabled': False}))
        supplement = self.root / 'supplement.pdf'
        make_scan(supplement)
        lines = [((70, 90, 350, 110), 'Supplement count 12.', .42)]
        with patch.object(extract, '_ocr_lines', return_value=lines):
            tier1._append_supplement_passages(self.folder, 'fixture:X1', supplement, {})
        meta = db.read_json(self.folder / 'metadata.json')
        meta['supplements'] = [{'supp_id': 'fixture:X1', 'kind': 'pdf', 'status': 'extracted'}]
        db.write_json(self.folder / 'metadata.json', meta)
        passages = [p for p in db.read_jsonl(self.folder / 'passages.jsonl') if p.get('source') == 'fixture:X1']
        self.assertTrue(passages)
        self.assertTrue(all(p['ocr']['mean_confidence'] == .42 for p in passages))
        self.assertEqual(meta['ocr_pages'][0]['source'], 'fixture:X1')
        pages = list(db.read_jsonl(self.folder / 'supplements/X1/pages.jsonl'))
        self.assertEqual(pages[0]['ocr_attempt']['status'], 'used')
        db.write_jsonl(self.folder / 'sentences.jsonl', [{'sentence_id': 'fixture:S0001', 'text': 'Fixture source text.'}])
        report = quality.inspect('fixture', scan_fonts=False)
        self.assertIn({'code': 'ocr_low_confidence', 'source': 'fixture:X1', 'page': 1}, report['blockers'])


def probe_real(root, prefixes):
    """One immutable original at a time; outputs only temporary crops and JSON stdout."""
    for prefix in prefixes:  # paper-id prefixes of the originals to probe, e.g. 'smith2020_'
        sources = sorted((root / 'sources/active').glob(prefix + '*/*.pdf'))
        if len(sources) != 1:
            raise ValueError(f'Expected one original for {prefix}; found {len(sources)}')
        path = sources[0]
        before = extract.sha256_file(path)
        ex = extract.extract(path, ocr={'enabled': False})
        with tempfile.TemporaryDirectory(prefix='d3-real-extraction-') as temporary:
            with pymupdf.open(path) as doc:
                figures, tables = objects.build(path.parent.name, doc, ex.captions, 'main', Path(temporary))
            if extract.sha256_file(path) != before:
                raise AssertionError('Original bytes changed')
            record = {'source': str(path), 'sha256': before, 'source_unchanged': True,
                      'n_pages': ex.n_pages, 'chars': ex.total_chars, 'text_quality': ex.text_quality,
                      'passages': len(ex.passages), 'captions': len(ex.captions),
                      'ocr_attempts': [p for p in ex.pages if p.get('ocr_attempt')],
                      'figures': [{'id': f['figure_id'], 'page': f['page'], 'bbox': f['bbox'],
                                   'crop_available': bool(f['crop'])} for f in figures],
                      'tables': [{'id': t['table_id'], 'page': t['page'], 'header': t['header'],
                                  'rows': len(t['rows']), 'first_two_rows': t['rows'][:2],
                                  'quality': t['quality'], 'extraction': t['extraction'],
                                  'crop_available': bool(t['crop'])} for t in tables]}
            print('REAL_SOURCE', json.dumps(record, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument('--real-root', type=Path)
    parser.add_argument('--real-prefix', nargs='*', default=[])
    args, remainder = parser.parse_known_args()
    if args.real_root and not args.real_prefix:
        parser.error('--real-root needs --real-prefix ID_PREFIX [...]')
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ExtractionAcceptance)
    result = unittest.TextTestRunner(verbosity=2 if '-v' in remainder else 1).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    if args.real_root:
        probe_real(args.real_root.resolve(), args.real_prefix)
