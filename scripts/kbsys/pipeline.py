"""Restartable, independently runnable stages. One paper and one writer at a time."""
from collections import Counter
from contextlib import nullcontext

from . import config, db, workflow

STEPS = ('ingest', 'quality', 'structure', 'analyze', 'connect', 'vectors', 'notes')

def _papers(con, vault):
    return [r[0] for r in con.execute('SELECT paper_id FROM paper_vaults WHERE vault=? ORDER BY paper_id', (vault,))]

def prewarm_neural(vault, say=print):
    import sqlite3
    import time
    from . import neural
    started = time.perf_counter()
    try:
        result = neural.refresh(vault)
    except (ValueError, OSError, sqlite3.Error) as exc:  # a warm index is a convenience, never a reason to fail notes
        say(f"Neural index prewarm skipped ({vault}): {exc}")
        return None
    say(f"Neural index prewarm: {result.get('index_status', 'ready')} in {time.perf_counter() - started:.2f}s ({vault}).")
    return result

def rebuild_vault_notes(con, vault, say=print):
    """The one 'rebuild notes for a vault' routine: `kb.py notes`, `kb.py steps notes` and the paper delete all call it."""
    import json
    from . import concept_notes, gaps, mother_vault, neural, notes, source_notes, vault_layout
    count = notes.refresh_all(con, vault)
    concept_totals = concept_notes.refresh(con, vault)
    source_total = source_notes.write(vault)
    gaps.generate(vault, cfg=config.load())  # deterministic candidates (<1 s)
    gaps.write_catalogue(vault)  # _Maps/Open hypotheses.md (linked from Home and the maps)
    notes.write_indexes(con, vault)  # Catalog reflects freshly generated typed node properties.
    tidy = notes.last_tidy_report(vault)
    say(f"Refreshed {count} paper notes and the index notes in vault {vault}. "
        f"Tidy moved {len(tidy['moved'])} stubs; protected {len(tidy['human_protected'])} human notes.")
    say("Concept notes:", concept_totals)
    say("Source notes:", source_total, "notes written or refreshed.")
    vault_layout.append_log(vault, (
        f"kb.py notes · {count} paper notes refreshed · concept notes: {concept_totals.get('created', 0)} created, "
        f"{concept_totals.get('updated', 0)} updated, {concept_totals.get('unchanged', 0)} unchanged, "
        f"{concept_totals.get('retired', 0)} retired · {len(tidy['moved'])} stubs moved"))
    if tidy["moved"] or tidy["human_protected"]:
        say(json.dumps(tidy, ensure_ascii=False, indent=2))
    mother = mother_vault.refresh(con)
    say("Mother vault:", mother)
    warm = prewarm_neural(vault, say)
    return {'paper_notes': count, 'concept_notes': concept_totals, 'source_notes': source_total,
            'tidy_moved': len(tidy['moved']), 'mother_vault': mother,
            'neural_index': (warm or {}).get('index_status', 'skipped')}

def run(con, cfg, vault, step, say=print, limit=None, structured_ai=False, structured_max_chunks=1):
    from . import answer, concepts, concept_notes, ghosts, ingest, notes, quality, relations, structure, vectors
    if step not in STEPS:
        raise ValueError('Unknown stage')
    if not config.vault_dir(vault).is_dir():
        raise ValueError('Choose an existing vault')
    code = list(config.ROOT.joinpath('scripts/kbsys').glob('*.py')) + list(config.CONFIG_DIR.glob('*.yaml'))
    code = [p for p in code if p.name != 'secrets.yaml']
    options = {'vault': vault, 'step': step, 'limit': limit, 'structured_ai': structured_ai, 'structured_max_chunks': structured_max_chunks}
    results = []
    # The analyze stage calls an AI provider (minutes): analysis_runner takes the library lock only around its writes.
    with nullcontext() if step == 'analyze' else workflow.writer_lock():
        pids = _papers(con, vault)
        if step == 'ingest':
            def inputs():
                return code + [p for p in config.ROOT.joinpath('sources/inbox').rglob('*') if p.is_file()]
            def outputs():
                names = ('metadata.json', 'pages.jsonl', 'sections.json', 'passages.jsonl', 'sentences.jsonl', 'references.jsonl', 'tables.jsonl', 'figures.jsonl')
                return [config.DB_PATH] + [db.paper_dir(pid) / name for pid in _papers(con, vault) for name in names]
            state = workflow.checkpoint(step, vault, inputs, outputs, lambda: ingest.run(cfg, con, vault, say=say), options)
            con.commit()
            return [state]
        if step in ('quality', 'structure', 'analyze'):
            for pid in pids[:limit] if limit else pids:
                folder = db.paper_dir(pid)
                base = code + [folder / name for name in ('metadata.json', 'sentences.jsonl', 'tables.jsonl', 'figures.jsonl')]
                meta = db.read_json(folder / 'metadata.json', {}) or {}
                for source in meta.get('source_files', []):
                    if source.get('path'):
                        from pathlib import Path
                        path = Path(source['path'])
                        base.append(path if path.is_absolute() else config.ROOT / path)
                if step == 'quality':
                    operation = lambda pid=pid, folder=folder: _quality(quality, pid, folder)
                    outputs = [folder / 'analysis_quality.json']
                elif step == 'structure':
                    base += [folder / name for name in ('evidence.jsonl', 'analysis.json', 'section_profile.json', 'protocol_profile.json', 'media_regions.json', 'media_manifest.json')]
                    operation = lambda pid=pid: _structure_inventory(pid)
                    outputs = lambda pid=pid, folder=folder: [folder / 'structure.json'] + media_outputs(pid)
                else:
                    from . import analysis_runner
                    ready = answer.provider_status(answer.settings('analysis'))
                    already_done = (db.read_json(folder / 'analysis_state.json', {}) or {}).get('status') == 'done'
                    if already_done or structured_ai:
                        operation = lambda pid=pid: _analyze_one(con, vault, pid, analysis_runner, say, cfg, structured_ai, structured_max_chunks)
                    elif not ready.get('ready'):
                        operation = lambda ready=ready: {'waiting': True, 'message': ready['message'], 'next': 'MCP read_paper -> add_units -> finish_analysis'}
                    else:
                        operation = lambda pid=pid: _analyze_one(con, vault, pid, analysis_runner, say, cfg)
                    base += [folder / 'analysis_quality.json']
                    outputs = [folder / 'analysis_state.json', folder / 'evidence.jsonl']
                    if (cfg.get('analysis') or {}).get('require_structured_profiles'):
                        profiles = [folder / 'section_profile.json', folder / 'protocol_profile.json']
                        base += profiles
                        outputs += profiles
                inputs = (lambda pid=pid, base=base: base + media_outputs(pid)[1:]) if step == 'structure' else base
                result = workflow.checkpoint(step, pid, inputs, outputs, operation, options)
                results.append(result)
                say(f'{step}: {pid}: {result["status"]}' + (' (cache)' if result['cache_hit'] else ''))
            return results
        inputs = code + [db.paper_dir(pid) / name for pid in pids
                         for name in ('metadata.json', 'sentences.jsonl', 'evidence.jsonl', 'analysis.json')]
        inputs += [config.STORE / 'relations.jsonl', config.STORE / 'measure_groups.json']
        if step == 'connect':
            def outputs():
                return [config.STORE / 'concepts.jsonl', config.STORE / 'ghosts.jsonl', config.STORE / 'relations.jsonl'] + list(config.vault_dir(vault).rglob('*.md'))
            def operation():
                linked = concepts.rebuild_links(con, vault)
                gs = ghosts.rebuild(con)
                rs = relations.propose(con, vault)
                if not (config.STORE / 'relations.jsonl').is_file():
                    db.write_jsonl(config.STORE / 'relations.jsonl', [])
                ns = concept_notes.refresh(con, vault, allow_ai=False)
                notes.refresh_all(con, vault)
                con.commit()
                return {'links': linked, 'ghosts': gs, 'relations': rs, 'notes': ns,
                        'ai_tasks': 'Synthesis/enrichment remain explicit MCP tasks; not silently marked complete.'}
        elif step == 'vectors':
            # The semantic index is global; track every contributing paper and model.
            all_pids = [r[0] for r in con.execute('SELECT paper_id FROM papers ORDER BY paper_id')]
            inputs += [db.paper_dir(pid) / name for pid in all_pids for name in ('metadata.json', 'sentences.jsonl', 'passages.jsonl', 'evidence.jsonl')]
            inputs += [vectors.MODEL_DIR / vectors.MODEL_FILE, vectors.MODEL_DIR / 'tokenizer.json']
            previous = db.read_json(workflow.checkpoint_path(step, vault), {}) or {}
            checksums = previous.get('output_fingerprints', {})
            forced = [pid for pid in all_pids if str(vectors._paper_file(pid)) in checksums
                      and checksums[str(vectors._paper_file(pid))] != workflow.fingerprint([vectors._paper_file(pid)])]
            outputs = lambda: _vector_outputs(vectors, all_pids)
            operation = lambda: vectors.update(con, progress=say, force_papers=forced, force_rebuild=True)
        else:
            inputs += list(config.vault_dir(vault).rglob('*.md'))
            def outputs():
                return sorted(set(config.vault_dir(vault).rglob('*.md')) | {config.vault_dir(vault) / 'Home.md'})
            operation = lambda: rebuild_vault_notes(con, vault, say)
        return [workflow.checkpoint(step, vault, inputs, outputs, operation, options)]

def _mine_hashes(vault):
    import hashlib
    mine = config.vault_dir(vault) / 'Mine'
    return {str(p.relative_to(mine)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(mine.rglob('*')) if p.is_file()}

def run_all(con, cfg, vault, ai=False, until=None, say=print):
    """G6 U1: every stage in order through `run` (its checkpoints skip unchanged work), after one vault snapshot.
    `analyze` needs `ai` (it calls the configured provider). The first stage that raises stops the run (report['failed'])."""
    import time
    from . import vault_layout
    if until is not None and until not in STEPS:
        raise ValueError('Unknown stage')
    if not config.is_research_vault_name(vault) or not config.vault_dir(vault).is_dir():  # '' / '..' would snapshot vaults/ or the root
        raise ValueError('Choose an existing vault')
    before = _mine_hashes(vault)
    with workflow.writer_lock():  # a consistent backup: no other writer mid-copy
        report = {'vault': vault, 'steps': [], 'snapshot': vault_layout._research_workspace().snapshot(con, vault)['snapshot']}
    for step in STEPS[:STEPS.index(until) + 1 if until else None]:
        if step == 'analyze' and not ai:
            report['steps'].append({'step': step, 'status': 'skipped (needs --ai)', 'seconds': 0, 'result': None})
            continue
        started = time.perf_counter()
        try:
            states = run(con, cfg, vault, step, say=say)
        except Exception as exc:  # stop here and report; the caller exits 1
            report['steps'].append({'step': step, 'status': 'failed', 'seconds': round(time.perf_counter() - started, 2),
                                    'result': f'{type(exc).__name__}: {exc}'})
            report['failed'] = step
            break
        counts = dict(Counter((s.get('status') or '?') + (' (cache)' if s.get('cache_hit') else '') for s in states))
        done = all(state.get('status') == 'done' for state in states)  # a waiting state (no AI, failed PDF) is not done
        report['steps'].append({'step': step, 'status': 'done' if done else 'waiting', 'seconds': round(time.perf_counter() - started, 2), 'result': counts})
    after = _mine_hashes(vault)
    report['mine_changed'] = sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))
    report['mine_unchanged'] = not report['mine_changed']
    return report

def _vector_outputs(vectors, pids):
    outputs = [vectors.INDEX_JSON] + [vectors._paper_file(pid) for pid in pids]
    try:
        index = db.read_json(vectors.INDEX_JSON, {}) or {}
        name = index.get('file')
    except (ValueError, TypeError):
        name = None
    from pathlib import Path
    if isinstance(name, str) and Path(name).name == name and name.endswith('.npy'):
        outputs.append(vectors.EMB_DIR / name)
    else:
        outputs.append(vectors.EMB_DIR / '.missing-matrix')
    return outputs

def _quality(module, pid, folder):
    report = module.inspect(pid)
    workflow.atomic_json(folder / 'analysis_quality.json', report)
    if not report.get('ready_for_analysis'):
        return {'waiting': True, **report}
    return report

def media_outputs(pid):
    from . import media
    folder = db.paper_dir(pid)
    manifest = structure_profile(folder / 'media_manifest.json')
    outputs = [folder / 'media_manifest.json']
    for bundle in manifest.get('bundles', []):
        relative = (bundle.get('crop') or {}).get('path')
        if relative:
            outputs.append(media.crop_path(folder, relative))
    return outputs

def structure_profile(path):
    try:
        value = db.read_json(path, {}) or {}
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError, OSError):
        return {}

def build_media(pid, regions_path=None, persist=True):
    from . import media
    import json
    from pathlib import Path
    regions = json.loads(Path(regions_path).read_text(encoding='utf-8-sig')) if regions_path else None
    # Explicit caller path is passed as data. media validates registered source/page/bbox.
    return media.build(pid, regions=regions, persist=persist)

def _structure_inventory(pid):
    from . import structure
    inventory = structure.build(pid)
    manifest = build_media(pid)
    return {'inventory': inventory, 'media_inventory_status': manifest['status'],
            'media_cache_hit': manifest['cache_hit'], 'status': 'source_inventory',
            'scientific_acceptance': 'not_assessed', 'region_completeness': 'not_assessed'}

def _analyze_one(con, vault, pid, runner, say, cfg, structured_ai=False, structured_max_chunks=1):
    before = db.read_json(db.paper_dir(pid) / 'analysis_state.json', {}) or {}
    if structured_ai:
        structured = runner.run_structured(con, pid, allow_ai=True, max_chunks=structured_max_chunks)
        if structured['status'] != 'complete':
            return {'waiting': True, 'structured': structured, 'preserved_existing_analysis': before.get('status') == 'done'}
    if before.get('status') == 'done':
        result = {'preserved_existing_analysis': True}
    else:
        result = runner.analyze_paper(con, pid, vault, say)
    state = db.read_json(db.paper_dir(pid) / 'analysis_state.json', {}) or {}
    from . import structure
    profiles = structure.completion(pid) if (cfg.get('analysis') or {}).get('require_structured_profiles') else {'complete': True, 'pending': []}
    return {'runner': result, 'waiting': state.get('status') != 'done' or not profiles['complete'],
            'state': state.get('status', 'pending'), 'profiles': profiles,
            'message': 'Cần get/submit_section_profile và get/submit_protocol_profile qua MCP.' if not profiles['complete'] else ''}
