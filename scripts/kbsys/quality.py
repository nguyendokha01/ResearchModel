"""Offline extraction preflight. A clean scan is not proof of perfect PDF recognition."""
import unicodedata

from . import db, extract, tier1


def suspicious_characters(text):
    return sorted({c for c in text if c == '\ufffd' or unicodedata.category(c) in {'Co', 'Cs'}
                   or (unicodedata.category(c) == 'Cc' and c not in '\n\r\t')})


def inspect(paper_id, scan_fonts=True):
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / 'metadata.json')
    if not meta:
        raise ValueError(f'Unknown paper: {paper_id}')
    blockers, warnings = [], []
    count = 0
    for row in db.read_jsonl(folder / 'sentences.jsonl'):
        count += 1
        chars = suspicious_characters(row.get('text', ''))
        if chars:
            blockers.append({'code': 'text_encoding', 'sentence_id': row['sentence_id'],
                             'source': row.get('source', 'main'), 'page': row.get('page'),
                             'characters': [f'U+{ord(c):04X}' for c in chars]})
    if not count:
        blockers.append({'code': 'no_sentences'})
    if meta.get('text_quality') in ('no_text', 'needs_ocr'):
        blockers.append({'code': 'no_readable_text'})
    sources = [('main', tier1.main_pdf(meta))]
    sources.extend((s['supp_id'], tier1._supplement_path(meta, s)) for s in meta.get('supplements', [])
                   if s.get('kind') == 'pdf')
    for source, path in sources:
        if path is None:
            warnings.append({'code': 'source_unavailable', 'source': source})
        elif scan_fonts:
            try:
                unmapped = extract.scan_unmapped_advent_glyphs(path)
            except Exception as error:
                blockers.append({'code': 'font_scan_failed', 'source': source, 'detail': str(error)})
            else:
                if unmapped:
                    blockers.append({'code': 'unverified_font', 'source': source, 'fonts': unmapped})
    # A readable article can still contain an unreadable scanned page. Report each
    # attempt, including supplements; overall mean text volume cannot clear it.
    page_sources = [('main', folder / 'pages.jsonl')]
    page_sources.extend((supp['supp_id'], folder / 'supplements' / supp['supp_id'].rsplit(':', 1)[-1] / 'pages.jsonl')
                        for supp in meta.get('supplements', []) if supp.get('kind') == 'pdf')
    for source, pages_path in page_sources:
        for page in db.read_jsonl(pages_path):
            attempt = page.get('ocr_attempt') or {}
            status = attempt.get('status')
            if status and status != 'used':
                blockers.append({'code': 'ocr_' + status, 'source': source, 'page': page.get('page'),
                                 'embedded_chars': attempt.get('embedded_chars'),
                                 **({'detail': attempt['detail']} if attempt.get('detail') else {})})
    for table in db.read_jsonl(folder / 'tables.jsonl'):
        if table.get('quality') != 'ok':
            warnings.append({'code': 'table_layout_uncertain', 'object_id': table['table_id'],
                             'page': table.get('page'), 'crop': table.get('crop')})
    for page in meta.get('ocr_pages', []):
        warnings.append({'code': 'ocr_used', 'source': page.get('source', 'main'), 'page': page.get('page'),
                         'low_confidence_lines': page.get('low_confidence_lines', 0)})
        if page.get('low_confidence_lines', 0):
            blockers.append({'code': 'ocr_low_confidence', 'source': page.get('source', 'main'), 'page': page.get('page')})
    return {'paper_id': paper_id, 'sentences': count, 'ready_for_analysis': not blockers,
            'blockers': blockers, 'warnings': warnings,
            'scope': 'Known encoding/font/OCR checks only; uncertain tables require source verification.'}
