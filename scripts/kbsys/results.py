"""Browsable Results and original figures/tables; never estimates values from pixels."""
import re
from . import config, db, find


def _papers(con, vault):
    if not isinstance(vault,str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}',vault) or not config.vault_dir(vault).is_dir():
        raise ValueError('Choose an existing vault')
    return [dict(r) for r in con.execute('SELECT p.* FROM papers p JOIN paper_vaults v ON p.paper_id=v.paper_id WHERE v.vault=? ORDER BY p.year DESC,p.first_author', (vault,))]


def overview(con, vault):
    papers = _papers(con, vault)
    summary = {'papers': len(papers), 'primary_papers': 0, 'review_papers': 0, 'other_papers': 0, 'results': 0, 'source_results': 0, 'figures': 0, 'tables': 0}
    cards = []
    for p in papers:
        pid = p['paper_id']
        evidence_type = find.effective_evidence_type(p)
        review = evidence_type in find.REVIEW_TYPES
        summary['review_papers' if review else 'primary_papers' if evidence_type == 'primary_research' else 'other_papers'] += 1
        units = con.execute("SELECT COUNT(*) FROM evidence WHERE paper_id=? AND unit_type='result'", (pid,)).fetchone()[0]
        source_results = con.execute("SELECT COUNT(*) FROM sentences WHERE paper_id=? AND section IN ('results','results_discussion') AND kind='text' AND COALESCE(in_table,0)=0", (pid,)).fetchone()[0] if 'in_table' in {r[1] for r in con.execute('PRAGMA table_info(sentences)')} else con.execute("SELECT COUNT(*) FROM sentences WHERE paper_id=? AND section IN ('results','results_discussion') AND kind='text'", (pid,)).fetchone()[0]
        objects = {r['kind']: r['n'] for r in con.execute('SELECT kind,COUNT(*) n FROM figures_tables WHERE paper_id=? GROUP BY kind', (pid,))}
        summary['results'] += 0 if review else units
        summary['source_results'] += source_results
        summary['figures'] += objects.get('figure',0)
        summary['tables'] += objects.get('table',0)
        cards.append({'paper_id':pid,'title':p['title'],'cite':f"{p['first_author']} {p['year']}",'evidence_type':evidence_type,'results':0 if review else units,'source_results':source_results,'figures':objects.get('figure',0),'tables':objects.get('table',0)})
    return {'vault':vault,'counts':summary,'papers':cards}


def _legacy_media_bundles(pid, folder, kind):
    """Build a small listing view from raw object rows without opening a PDF."""
    source_rows = {row.get('sentence_id'): row for row in db.read_jsonl(folder / 'sentences.jsonl')}
    if kind in ('figure', 'table'):
        rows = db.read_jsonl(folder / f'{kind}s.jsonl')
    else:
        rows = db.read_json(folder / 'media_regions.json', []) or []
        rows = rows.get('regions', []) if isinstance(rows, dict) else rows
    bundles = []
    for row in rows:
        if not isinstance(row, dict) or (row.get('kind') and row.get('kind') != kind):
            continue
        object_id = row.get('object_id') or row.get(f'{kind}_id')
        if not object_id:
            continue
        caption_ids = row.get('caption_sentence_ids') or []
        caption = [source_rows[sid] for sid in caption_ids if sid in source_rows]
        locator = {'page': row.get('page'), 'source': row.get('source', 'main'), 'status': 'unknown'}
        bundles.append({'object_id': object_id, 'kind': kind, 'label': row.get('label') or object_id,
                        'caption': caption, 'data': [], 'mentions': [], 'anchors': [],
                        'source_locator': locator, 'crop': {'status': 'unknown'},
                        'table': row if kind == 'table' else None})
    return bundles


def browse(con, vault, query='', kind='result', paper_id='', offset=0, limit=12, speed='fast'):
    from . import neural
    kind = kind if kind in ('result','source','figure','table','formula','special_symbol_region') else 'result'
    limit, offset = min(max(int(limit),1),50), max(int(offset),0)
    papers = _papers(con,vault)
    records=[]
    total=0
    needle=query.strip().casefold()
    def keep(record, paper):
        nonlocal total
        hay = ' '.join(str(record.get(k) or '') for k in ('text','label','caption')) + ' '+paper['title']
        hay += ' ' + str(record.get('header') or '') + ' ' + str(record.get('rows') or '')
        if needle and not re.search(r'(?<!\w)'+re.escape(needle)+r'(?!\w)',hay.casefold()):
            return
        if offset <= total < offset+limit:
            records.append(record)
        total+=1
    for p in papers:
        pid=p['paper_id']
        if paper_id and pid!=paper_id: continue
        card={'paper_id':pid,'title':p['title'],'cite':f"{p['first_author']} {p['year']}",'review_paper':find.effective_evidence_type(p) in find.REVIEW_TYPES,'evidence_type':find.effective_evidence_type(p)}
        folder=db.paper_dir(pid)
        if kind=='result':
            if card['review_paper']: continue
            source_rows={s['sentence_id']:s for s in db.read_jsonl(folder/'sentences.jsonl')}
            for u in db.read_jsonl(folder/'evidence.jsonl'):
                if u.get('unit_type')!='result': continue
                ids=u.get('sentence_ids',[])
                from . import node_profile
                wanted,basis=node_profile._source_ids(ids,source_rows,paper_id=pid)
                copied=[s for s in source_rows.values() if s["sentence_id"] in wanted and s.get("section") not in ('abstract','references','front_matter')]
                # Context is retained separately; statements may still need editorial review.
                record={**card,'kind':'result','id':u['unit_id'],'statement':u['statement'],'text':' '.join(s['text'] for s in copied),'sentence_ids':ids,'pages':sorted({s['page'] for s in copied if s.get('page') is not None}),'context_sentence_ids':[s['sentence_id'] for s in copied if s['sentence_id'] not in ids],'context_note':'Includes separately copied preceding-page source fragments where present; source IDs and stored annotation are unchanged.','outcome':u.get('outcome',{}),'intervention':u.get('intervention',{}),'model':u.get('model',{}),'quality_details':u.get('flags',[]),'status':u.get('status'),'source_url':f"/pdf/{pid}#page={copied[0]['page']}" if copied and copied[0].get('source','main')=='main' and copied[0].get('page') else None}
                if record['outcome'].get('value_source')=='figure_only':
                    record['outcome']={**record['outcome'],'values':None,'n':None,'p':None}
                keep(record,p)
        elif kind=='source':
            for s in db.read_jsonl(folder/'sentences.jsonl'):
                if s['section'] not in ('results','results_discussion') or s.get('in_table'): continue
                if s.get('kind')!='text': continue
                keep({**card,'kind':'source','id':s['sentence_id'],'text':s['text'],'statement':'','sentence_ids':[s['sentence_id']],'pages':[s['page']],'source_url':f"/pdf/{pid}#page={s['page']}" if s.get('source','main')=='main' else None},p)
        else:
            manifest = db.read_json(folder / 'media_manifest.json', {}) or {}
            bundles = manifest.get('bundles') if isinstance(manifest, dict) else None
            # A published manifest is enough for listing/filtering. Hydrate only
            # the paginated records below instead of rebuilding every paper.
            if not isinstance(bundles, list):
                bundles = _legacy_media_bundles(pid, folder, kind)
            for bundle in bundles:
                if bundle.get('kind')!=kind: continue
                caption=' '.join(s.get('text','') for s in (bundle.get('caption') or []) if isinstance(s, dict))
                table=bundle.get('table') or {}
                loc=bundle.get('source_locator') or {}
                object_id=bundle.get('object_id')
                mentions=bundle.get('mentions') or []
                record={**card,'kind':kind,'id':object_id,'label':bundle.get('label') or object_id,'caption':caption,'text':caption,'pages':[loc['page']] if loc.get('page') else [],'source':loc.get('source'),'source_url':bundle.get('source_url'),'crop_url':bundle.get('crop_url'),'quality':table.get('quality','source_image'),'header':table.get('header',[]),'rows':table.get('rows',[]),'footnotes':table.get('footnotes') or '', 'mentioned_in':[s.get('sentence_id') for s in mentions if isinstance(s, dict) and s.get('sentence_id')],'bundle':bundle,'numerical_interpretation':'Use extracted rows only when columns are verified; do not infer values from charts.'}
                keep(record,p)
    if kind not in ('result', 'source'):
        by_paper = {}
        for record in records:
            by_paper.setdefault(record['paper_id'], []).append(record)
        for pid, rows in by_paper.items():
            ids = [row['id'] for row in rows]
            hydrated = {b['object_id']: b for b in neural.media_reader(con, vault, pid, ids, limit=None)['bundles']}
            for row in rows:
                bundle = hydrated.get(row['id'])
                if not bundle:
                    continue
                row['bundle'] = bundle
                row['source_url'] = bundle.get('source_url')
                row['crop_url'] = bundle.get('crop_url')
                table = bundle.get('table') or {}
                row['quality'] = table.get('quality', row['quality'])
                row['header'] = table.get('header', row['header'])
                row['rows'] = table.get('rows', row['rows'])
    for r in records:
        if r['kind'] in ('result','source'):
            r['sources']=neural.resolve_sources(con,vault,[r['id']],limit=45 if speed=='deep' else 24,query=query,context_limit=12 if speed=='deep' else 4)
            r['media']=neural.media_reader(con,vault,r['paper_id'],[r['id'],*r['sentence_ids']])
    return {'vault':vault,'kind':kind,'query':query,'records':records,'total':total,'offset':offset,'limit':limit,'has_more':offset+limit<total}
