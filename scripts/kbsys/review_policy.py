"""Source-grounded scope review and reversible, exact within-paper deduplication.

This is a relevance rule, not a scientific relevance classifier or evidence approval.
"""
import copy
import json
import re
import shutil
import uuid
from . import config, db, presentation

LEGACY_KINDS = {'analysis_flags', 'analysis_duplicates', 'analysis_incomplete', 'classification', 'duplicate_file'}
KIND = 'topic_relevance'


def _norm(value):
    if isinstance(value, dict):
        return {k: _norm(v) for k, v in sorted(value.items())}
    if isinstance(value, list):
        return [_norm(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'\s+', ' ', value).strip().casefold()
    return value


def deduplicate(units):
    """Only same source, statement, scientific structures AND quality status merge.

    Different models, outcomes, qualifiers, source objects or human status never merge.
    Identity/audit timestamps alone do not make an otherwise exact claim distinct.
    """
    identity = {'unit_id', 'seq', 'analyst', 'attempt', 'created_at', '_validation_unit_index', 'aliases', 'terms'}
    seen, kept, aliases = {}, [], {}
    for unit in units:
        payload = {k: v for k, v in unit.items() if k not in identity}
        if 'flags' in payload:
            payload['flags'] = [{k: v for k, v in f.items() if k != 'unit_index'} for f in payload['flags']]
        # Source ordering may affect copied quote/context: deliberately keep it significant.
        key = json.dumps(_norm(payload), sort_keys=True, ensure_ascii=False)
        if not unit.get('sentence_ids') or not unit.get('statement') or key not in seen:
            record = copy.deepcopy(unit)
            kept.append(record)
            seen[key] = record
        else:
            target = seen[key]
            for kind, names in (unit.get('terms') or {}).items():
                target.setdefault('terms', {}).setdefault(kind, [])
                target['terms'][kind] = list(dict.fromkeys(target['terms'][kind] + names))
            if unit.get('unit_id') and target.get('unit_id'):
                aliases[unit['unit_id']] = target['unit_id']
                target.setdefault('aliases', []).append(unit['unit_id'])
    for seq, unit in enumerate(kept, 1):
        unit["seq"] = seq
    return kept, aliases


def _scope(vault):
    from . import concepts
    home = config.vault_dir(vault) / 'Home.md'
    if not home.is_file():
        return []
    text = home.read_text(encoding='utf-8').split('Vault riêng cho chủ đề nghiên cứu này.')[0]
    # The machine navigation block (D3.44) is not researcher scope.
    text = re.sub(r'<!-- kb:auto:start.*?<!-- kb:auto:end -->', '', text, flags=re.S)
    # Home is researcher scope. Wikilinks and generated Topics are not scope declarations.
    text = re.sub(r'\[\[.*?\]\]|<!--.*?-->', '', text, flags=re.S)
    names = re.findall(r'^#\s+(.+)$', text, re.M)
    names += re.findall(r'^topic_terms:[ \t]*\[(.+)\][ \t]*$', text, re.M)
    for record in db.read_jsonl(config.STORE / 'concepts.jsonl'):
        variants = [v for name in [record['name'], *record.get('synonyms', [])]
                    for v in concepts.spelling_variants(name)]
        if any(presentation.matches_term(text, v) for v in variants):
            names.extend(variants)
    return sorted({n.strip(' \"\'') for row in names for n in row.split(',') if len(n.strip()) >= 3})


def _grounded(paper_id, terms):
    meta = db.read_json(db.paper_dir(paper_id) / 'metadata.json', {}) or {}
    rows = [s for s in db.read_jsonl(db.paper_dir(paper_id) / 'sentences.jsonl')
            if s.get('section') not in ('references', 'abstract') and s.get('kind') == 'text' and not s.get('in_table')]
    for term in terms:
        matches = [s['sentence_id'] for s in rows if presentation.matches_term(s.get('text'), term)]
        title_match = presentation.matches_term(meta.get('title'), term)
        # Only a real source match to researcher scope anchors a topic; AI tags never do.
        if title_match or matches:
            return {'term': term, 'sentence_ids': matches[:8], 'title_match': title_match}
    return None


def _entities(paper_id, con=None):
    result = {}
    sources = {s['sentence_id']: s for s in db.read_jsonl(db.paper_dir(paper_id) / 'sentences.jsonl')}
    for unit in db.read_jsonl(db.paper_dir(paper_id) / 'evidence.jsonl'):
        if unit.get('status') not in ('ok', 'human_confirmed') or unit.get('unit_type') not in ('result', 'method_detail'):
            continue
        source_ids = unit.get('sentence_ids') or []
        source_rows = [sources.get(s) for s in source_ids]
        if not source_ids or any(s is None for s in source_rows):
            continue
        from .evidence import _quote
        if unit.get('quote') != _quote(source_rows):
            continue
        values = []
        for field, keys in (('intervention', ('agent',)), ('outcome', ('measure',)),
                            ('model', ('cell_type',)), ('method', ('name',))):
            data = unit.get(field) or {}
            if isinstance(data, dict):
                values.extend(data.get(key) for key in keys)
        if unit.get('unit_type') == 'method_detail':
            values.extend((unit.get('terms') or {}).get('methods') or [])
        for value in values:
            if isinstance(value, str) and _norm(value) not in {'cell', 'cells', 'survival', 'viability', 'control', 'temperature', 'duration', 'time', 'concentration', 'dose'} and len(value.strip()) >= 3 and presentation.matches_term(unit.get('quote'), value):
                result.setdefault(_norm(value), []).append(unit.get('unit_id'))
    if con is not None:
        links = {r['concept_id']: dict(r) for r in con.execute(
            'SELECT * FROM concept_links WHERE paper_id=? AND n_sentences>0', (paper_id,))}
        for record in db.read_jsonl(config.STORE / 'concepts.jsonl'):
            link = links.get(record['concept_id'])
            if not link:
                continue
            sentence = sources.get(link.get('first_sentence_id')) or {}
            if sentence.get('section') in ('references',) or sentence.get('in_table'):
                continue
            from . import concepts
            variants = [v for name in [record['name'], *record.get('synonyms', [])]
                        for v in concepts.spelling_variants(name)]
            if any(presentation.matches_term(sentence.get('text'), v) for v in variants):
                result.setdefault(_norm(record['name']), []).append(sentence.get('sentence_id'))
    return result


def context(con, vault):
    """Build source anchor list once; stream each paper and retain only compact connections."""
    scope = _scope(vault)
    anchors = {}
    for row in con.execute('SELECT paper_id FROM paper_vaults WHERE vault=? ORDER BY paper_id', (vault,)):
        anchor = _grounded(row[0], scope)
        if anchor:
            anchors[row[0]] = {'topic': anchor, 'entities': _entities(row[0], con)}
    return {'scope': scope, 'anchors': anchors}


def relevance(con, paper_id, vault, prepared=None):
    if not con.execute('SELECT 1 FROM paper_vaults WHERE paper_id=? AND vault=?', (paper_id, vault)).fetchone():
        raise ValueError('Paper is outside this vault')
    prepared = prepared or context(con, vault)
    scope = prepared['scope']
    direct = (prepared['anchors'].get(paper_id) or {}).get('topic')
    if direct:
        return {'connected': True, 'classification': 'CONNECTED', 'basis': 'source_topic', 'connection': direct,
                'reason': 'Vault topic occurs in the paper title or a body source sentence.'}
    entities = _entities(paper_id, con)
    if entities:
        for other, entry in prepared['anchors'].items():
            if other == paper_id:
                continue
            anchor = entry['topic']
            other_entities = entry['entities']
            shared = sorted(entities.keys() & other_entities.keys())
            if shared:
                term = shared[0]
                return {'connected': True, 'classification': 'CONNECTED', 'basis': 'structured_source_connection',
                        'connection': {'term': term, 'paper_id': other, 'source_refs': entities[term],
                                       'anchor_source_refs': other_entities[term], 'anchor_topic': anchor},
                        'reason': 'A source-matched concept/entity/protocol connects to a paper grounded in the vault topic; this is not a mechanism claim.'}
    for row in con.execute("SELECT c.sentence_id, r.resolved_paper_id FROM citations c "
                           "JOIN references_list r ON r.ref_id=c.ref_id JOIN sentences s ON s.sentence_id=c.sentence_id "
                           "WHERE c.paper_id=? AND c.confidence='high' AND s.section<>'references' ORDER BY c.sentence_id", (paper_id,)):
        if row['resolved_paper_id'] in prepared['anchors']:
            return {'connected': True, 'classification': 'CONNECTED', 'basis': 'source_citation_connection',
                    'connection': {'sentence_id': row['sentence_id'], 'paper_id': row['resolved_paper_id']},
                    'reason': 'A resolved high-confidence body citation links to a paper grounded in the vault topic; citation is not support for a mechanism.'}
    return {'connected': False, 'classification': 'POTENTIAL_BRIDGE', 'basis': 'no_supported_connection',
            'reason': 'No supported topic, concept, entity, protocol, or citation connection was found in this vault. Retained as a candidate bridge for researcher review.', 'scope_terms': scope}


def pending(con, vault=None):
    rows = [dict(r) for r in con.execute("SELECT * FROM review_items WHERE status='pending' AND kind=? ORDER BY item_id", (KIND,)).fetchall()]
    if vault:
        rows = [r for r in rows if r['detail'].startswith(f'[{vault}] ')]
    return rows


def review_note_actions(con, item, vaults):
    """Recognize exact generator bytes; any edit is protected, including frontmatter."""
    from . import notes
    result = []
    if not item.get('note_name'):
        return result
    generated = notes.render_review_note(con, item).encode('utf-8')
    for vault in vaults:
        path = config.vault_dir(vault) / 'Review Queue' / f'{item["note_name"]}.md'
        if path.is_file():
            expected = (generated, generated.replace(b'\n', b'\r\n'))
            result.append({'item_id': item['item_id'], 'vault': vault, 'path': str(path),
                           'action': 'archive_machine_note' if path.read_bytes() in expected else 'protected_human_note'})
    return result


def _retire_review_notes(con, item, vaults, archive=None):
    actions = review_note_actions(con, item, vaults)
    for action in actions:
        if action['action'] != 'archive_machine_note':
            continue
        archive = archive or config.STORE / 'review_policy_history' / uuid.uuid4().hex
        path = __import__('pathlib').Path(action['path'])
        target = archive / 'vault_notes' / action['vault'] / path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(target))
        action['archive_path'] = str(target)
    return actions


def refresh_paper(con, paper_id, archive=None, note_actions=None):
    """Caller holds writer lock and commits; decisions/human text are never rewritten."""
    from . import notes
    meta = db.read_json(db.paper_dir(paper_id) / 'metadata.json', {}) or {}
    vaults = [r[0] for r in con.execute('SELECT vault FROM paper_vaults WHERE paper_id=?', (paper_id,))]
    results = {v: relevance(con, paper_id, v) for v in vaults}
    wanted = {f'[{v}] {r["reason"]}' for v, r in results.items() if not r['connected']}
    old = [dict(r) for r in con.execute("SELECT * FROM review_items WHERE paper_id=? AND status='pending'", (paper_id,))]
    kept = set()
    for item in old:
        if item['kind'] == KIND and item['detail'] in wanted and item['detail'] not in kept:
            kept.add(item['detail'])
            continue
        if item['kind'] not in LEGACY_KINDS | {KIND}:
            continue
        # Human inline decisions remain pending for explicit resolution, never lost to migration.
        human = False
        for v in vaults:
            path = config.vault_dir(v) / 'Review Queue' / f'{item.get("note_name")}.md'
            if path.is_file():
                import yaml
                text = path.read_text(encoding='utf-8')
                if text.startswith('---\n') and '\n---\n' in text[4:]:
                    front = yaml.safe_load(text[4:].split('\n---\n', 1)[0]) or {}
                    human = human or front.get('decision') not in (None, '', 'null', '~')
        if human:
            if note_actions is not None:
                note_actions.extend(review_note_actions(con, item, vaults))
            continue
        actions = _retire_review_notes(con, item, vaults, archive)
        if note_actions is not None:
            note_actions.extend(actions)
        con.execute("UPDATE review_items SET status='superseded', resolved_at=? WHERE item_id=?", (config.now(), item['item_id']))
        # Exact machine templates move reversibly; all human edits stay byte-for-byte.
    for v, r in results.items():
        detail = f'[{v}] {r["reason"]}'
        decided = con.execute("SELECT 1 FROM review_items WHERE paper_id=? AND status='resolved' AND ((kind=? AND detail LIKE ?) OR (kind='classification' AND ?=1))", (paper_id, KIND, f'[{v}] %', len(vaults))).fetchone()
        if not r['connected'] and detail not in kept and not decided:
            item = db.add_review_item(con, KIND, detail, paper_id)
            notes.write_review_note(con, item, v)
    db.write_json(db.paper_dir(paper_id) / 'review_policy.json', {'version': 1, 'vaults': results})
    return results


def refresh(con, vault, apply=False):
    """Supported migration; paper-sized processing, reversible machine archive, no providers."""
    ids = [r[0] for r in con.execute('SELECT paper_id FROM paper_vaults WHERE vault=? ORDER BY paper_id', (vault,))]
    report = {'vault': vault, 'applied': apply, 'papers': [], 'archive': None, 'note_actions': []}
    if apply:
        archive = config.STORE / 'review_policy_history' / uuid.uuid4().hex
        archive.mkdir(parents=True)
        backup_con = __import__('sqlite3').connect(str(archive / 'library.db'))
        try:
            con.backup(backup_con)
        finally:
            backup_con.close()
        db.write_json(archive / 'reviews.json', [dict(r) for r in con.execute('SELECT * FROM review_items')])
        report['archive'] = str(archive)
    prepared = context(con, vault)
    for pid in ids:
        folder = db.paper_dir(pid)
        original = list(db.read_jsonl(folder / 'evidence.jsonl'))
        units, aliases = deduplicate(original)
        info = {'paper_id': pid, 'relevance': relevance(con, pid, vault, prepared), 'duplicates': len(original) - len(units)}
        if not apply:
            vaults = [r[0] for r in con.execute('SELECT vault FROM paper_vaults WHERE paper_id=?', (pid,))]
            for item in con.execute("SELECT * FROM review_items WHERE paper_id=? AND status='pending'", (pid,)):
                item = dict(item)
                if item['kind'] in LEGACY_KINDS or (item['kind'] == KIND and info['relevance']['connected']):
                    report['note_actions'].extend(review_note_actions(con, item, vaults))
        if apply:
            dest = archive / pid
            dest.mkdir()
            for name in ('evidence.jsonl', 'analysis.json', 'review_policy.json', 'evidence_aliases.json'):
                if (folder / name).is_file():
                    shutil.copy2(folder / name, dest / name)
            if aliases:
                db.write_jsonl(folder / 'evidence.jsonl', units)
                previous = db.read_json(folder / 'evidence_aliases.json', {}) or {}
                db.write_json(folder / 'evidence_aliases.json', {**previous, **aliases})
                analysis = db.read_json(folder / 'analysis.json', {}) or {}
                seqs = {u['unit_id']: u['seq'] for u in units}
                mapping = {u['seq']: seqs[aliases.get(u['unit_id'], u['unit_id'])] for u in original}
                for sentence in analysis.get('summary') or []:
                    sentence['units'] = list(dict.fromkeys(mapping.get(s, s) for s in sentence.get('units', [])))
                analysis.update(n_units=len(units), n_flagged=sum(u.get('status') == 'flagged' for u in units))
                db.write_json(folder / 'analysis.json', analysis)
                db.reload_paper(con, pid)
            info['vaults'] = refresh_paper(con, pid, archive=archive, note_actions=report['note_actions'])
        report['papers'].append(info)
    if apply:
        con.commit()
        from . import notes
        for pid in ids:
            meta = db.read_json(db.paper_dir(pid) / 'metadata.json', {}) or {}
            for v in meta.get('vaults') or []:
                notes.write_paper_note(meta, v, con=con)
        notes.write_indexes(con, vault)
    return report
