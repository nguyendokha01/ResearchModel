"""Tier-2 analysis with the configured AI provider (`ai.tasks.analysis`, else `ai.provider`).

For each pending paper of a vault: the AI gets docs/AI_ANALYSIS_GUIDE.md and the numbered sentences,
returns one complete submission, and `evidence.submit` runs the V1–V12 checks. While the result is
`needs_fix`, the errors go back to the AI (up to `analysis.max_attempts`); what still fails goes to the
Review Queue. Papers are processed one at a time (low memory). With provider `mcp_agent`, nothing runs
here: an MCP-connected AI app does the same work through the analysis tools.
"""

import json
from contextlib import nullcontext
from pathlib import Path
import time
import uuid

from . import answer, config, db, evidence, quality, workflow, analysis_transport as transport

RULES = ("\n\nYou are the analyst. Use ONLY the numbered sentences below. Reference sentences by their IDs; "
         "never type quotes (code copies them). Cover every sentence marked MUST (as a unit or in `skipped` "
         "with a reason). Reply with the complete submission JSON only. "
         "Section/source/page/kind headers apply until the next header. Omit unknown optional fields and "
         "empty lists/objects instead of writing nulls. Never omit known scientific information. "
         "In REPAIR MODE, follow its patch format instead of repeating the complete submission.")


# Plain-language meaning of checker codes for the AI repair turn (codes alone were not understood in the pilot).
REPAIR_HINTS = ("Hints: A7 = a cited source sentence is hedged (may/might/could/suggest/seem/associated/not significant); "
                "keep that hedge word in your text, never state it as certain. A1/A2 = cite only supplied IDs and copy numbers "
                "exactly. WH1 = for a missing answer give text null and a reason; sentence_ids then only list where you looked. "
                "G1 = a named term (organism, gene, number, increase/decrease) is not in the sentences you cited: add the "
                "sentence ID that states it, or remove the term. Put the fields at the top level of the JSON.")


def _unwrap(reply, reply_format):
    """Models sometimes nest the answer under one wrapper key ({"overview": {...}}). Unwrap only when that single
    object holds the expected top-level fields; everything is still validated afterwards (first pilot paper)."""
    expected = set(reply_format) if isinstance(reply_format, dict) else set()
    if isinstance(reply, dict) and len(expected) == 1 and not expected & set(reply):
        # The opposite slip: the wrapper is missing ({"what": ..} instead of {"answers": {"what": ..}}; 4-model batch).
        key = next(iter(expected))
        inner = set(reply_format[key]) if isinstance(reply_format[key], dict) else set()
        if inner and inner <= set(reply):
            return {key: {k: reply[k] for k in inner}, **{k: v for k, v in reply.items() if k not in inner}}
    if isinstance(reply, dict) and expected and not expected & set(reply):
        nested = [v for v in reply.values() if isinstance(v, dict) and expected & set(v)]
        if len(nested) == 1:
            echo = {k: reply[k] for k in ('paper_id', 'source_sha256', 'source_revision', 'revision_of') if k in reply}
            return {**nested[0], **echo}
    return reply


def pending_papers(con, vault):
    rows = con.execute("SELECT p.paper_id, p.title, p.analysis_status FROM papers p JOIN paper_vaults v "
                       "ON v.paper_id = p.paper_id WHERE v.vault = ? AND COALESCE(p.analysis_status, 'pending') "
                       "NOT IN ('done') ORDER BY p.ingested_at", (vault,)).fetchall()
    return [dict(r) for r in rows if evidence.status(r["paper_id"]).get("status") != "review"]


def _vault_topic(vault):
    home = config.vault_dir(vault) / "Home.md"
    text = home.read_text(encoding="utf-8")[:600] if home.is_file() else ""
    return f"Vault (research topic): {vault}\n{text}".strip()


def _reading_text(con, paper_id):
    view = evidence.reading_view(con, paper_id, start=1, limit=100000)
    return transport.compact_reading(view["sentences"]), view


def usage_report(paper_id):
    rows = list(db.read_jsonl(db.paper_dir(paper_id) / "analysis_usage.jsonl"))
    totals = {}
    for row in rows:
        for key, value in (row.get("usage") or {}).items():
            totals[key] = totals.get(key, 0) + value
    return {"paper_id": paper_id, "calls": len(rows), "reported_token_totals": totals,
            "calls_without_token_counts": sum(not any(k in (r.get("usage") or {})
                                                      for k in ("input_tokens", "output_tokens")) for r in rows),
            "input_chars": sum(r.get("input_chars", 0) for r in rows),
            "output_chars": sum(r.get("output_chars", 0) for r in rows)}


def _call(s, system, user, paper_id, stage):
    started = time.monotonic()
    entry = {"time": config.now(), "provider": s["provider"], "model": answer.model_of(s), "stage": stage,
             "input_chars": len(system) + len(user), "request_sha256": transport.fingerprint([system, user])}
    try:
        raw, usage = answer._chat(s, system, user)
        entry.update(output_chars=len(raw), usage=transport.normalized_usage(usage),
                     provider_usage=usage, status="returned")
        return raw
    except Exception:
        entry.update(status="failed", usage={})
        raise
    finally:
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
        with (db.paper_dir(paper_id) / "analysis_usage.jsonl").open("a", encoding="utf-8", newline="\n") as f:
            f.write(transport.dumps(entry) + "\n")


def analyze_paper(con, paper_id, vault, say=print):
    state = evidence.status(paper_id)
    if state.get("status") in ("done", "review"):
        return state
    preflight = quality.inspect(paper_id)
    transport.write_atomic(db.paper_dir(paper_id) / "analysis_quality.json", preflight)
    if not preflight["ready_for_analysis"]:
        raise answer.AIError("Dừng trước khi gọi AI: văn bản cần sửa font/OCR. Xem analysis_quality.json "
                             "hoặc chạy quality-check cho bài này.")
    s = answer.settings("analysis")
    status = answer.provider_status(s)
    if not status.get("ready"):
        raise answer.AIError(status["message"])
    folder = db.paper_dir(paper_id)
    meta = db.read_json(folder / "metadata.json", {}) or {}
    text, view = _reading_text(con, paper_id)
    try:
        from . import structure
        reference = structure.reference_context(paper_id)
    except (ValueError, TypeError, OSError, KeyError):
        reference = {'paper_outline': None, 'paper_overview': None}
    analyst = f"{s['provider']}/{answer.model_of(s) or 'default'}"
    head = f"{_vault_topic(vault)}\n\nPaper id: {paper_id}\nTitle: {meta.get('title')}\nYear: {meta.get('year')}\n"
    base = (head + "\nReference-only whole-paper context (never cite it as evidence; cite numbered sentences only):\n" +
            transport.dumps(reference))  # head alone is fingerprinted: an overview change must not reset analysis
    system = config.analyst_prompt() + RULES
    fingerprint = transport.fingerprint([1, head, text, system, s["provider"], answer.model_of(s)])
    path = folder / "analysis_runner.json"
    saved = db.read_json(path, {}) or {}
    state_hash = transport.fingerprint(state)
    if saved and (saved.get("fingerprint") != fingerprint or saved.get("state_hash") != state_hash):
        archive = folder / "analysis_history" / ("runner_" + uuid.uuid4().hex + ".json")
        archive.parent.mkdir(parents=True, exist_ok=True)
        path.replace(archive)
        saved = {}
    submission, result = saved.get("submission"), saved.get("result", {})
    if submission is None and state.get("status") == "needs_fix":
        # Legacy partial evidence may contain accepted units but not rejected raw submissions.
        # Preserve it: require an agent to repair it via MCP instead of silently replacing it.
        if (folder / "evidence.jsonl").exists():
            raise answer.AIError("Bài có phân tích dở từ luồng cũ. Sửa qua MCP để giữ bằng chứng đã đạt; "
                                 "không tự phân tích lại toàn bài.")
    max_attempts = int((config.load().get("analysis") or {}).get("max_attempts", 3))
    calls = saved.get("calls", 0)
    extra, protocol_error = saved.get("extra"), saved.get("protocol_error", "")
    while calls < max_attempts + 2:
        user = (transport.repair_prompt(base, view["sentences"], submission, result, extra)
                if submission is not None else base + "\nNumbered sentences:\n" + text)
        if protocol_error:
            user += "\nPrevious reply was not usable: " + protocol_error
        reply_path = folder / "analysis_runner_reply.json"
        if path.is_file() and reply_path.is_file() and reply_path.stat().st_mtime >= path.stat().st_mtime:
            # The last call returned but its reply was never processed (crash, locked store): use it, do not pay again.
            raw = (db.read_json(reply_path, {}) or {}).get("raw", "")
        else:
            calls += 1
            saved = {"version": 1, "fingerprint": fingerprint, "state_hash": state_hash, "calls": calls,
                     "submission": submission, "result": result, "extra": extra, "protocol_error": protocol_error}
            transport.write_atomic(path, saved)
            raw = _call(s, system, user, paper_id, "repair" if submission is not None else "initial")
            transport.write_atomic(reply_path, {"raw": raw, "time": config.now()})
        try:
            # Strict parse prevents accepting a nested object from a truncated outer JSON reply.
            payload = raw.strip()
            if payload.startswith("```") and payload.endswith("```"):
                payload = payload.split("\n", 1)[1].rsplit("```", 1)[0].strip()
            reply = json.loads(payload)
            if isinstance(reply, dict) and set(reply) == {"need_context"} and submission is not None:
                requested = reply["need_context"]
                known = {r["id"] for r in view["sentences"]}
                if requested == "all":
                    extra = "all"
                elif isinstance(requested, list) and requested and all(isinstance(x, str) and x in known for x in requested):
                    extra = "all" if extra == "all" else sorted(set(extra or []) | set(requested))
                else:
                    raise ValueError("need_context must be 'all' or existing short sentence IDs")
                protocol_error = ""
                saved.update(extra=extra, protocol_error="")
                transport.write_atomic(path, saved)
                continue
            reply = transport.normalize_term_placement(reply)
            data = transport.merge_patch(submission, reply, result) if submission is not None else reply
            if not isinstance(data, dict) or not isinstance(data.get("units"), list):
                raise ValueError("Initial submission must be an object with a units list")
        except (ValueError, IndexError) as error:
            protocol_error = str(error)
            saved["protocol_error"] = protocol_error
            transport.write_atomic(path, saved)
            continue
        data["paper_id"] = paper_id
        data["analyst"] = analyst
        # Submit rebuilds shared notes and caches; parallel batch processes must take turns (4-model batch).
        try:
            with workflow.writer_lock(timeout=1800):
                result = evidence.submit(con, paper_id, data, max_attempts=max_attempts)
                con.commit()
        except workflow.WriterBusy as exc:
            # Keep analysis_runner_reply.json newer than its checkpoint so the
            # returned reply is submitted first on the next run, without a new call.
            return {'status': 'blocked_by_writer', 'calls': calls, 'message': str(exc),
                    'next': 'Retry after the writer lock is released.'}
        submission, state_hash = data, transport.fingerprint(evidence.status(paper_id))
        protocol_error = ""
        saved.update(submission=submission, result=result, state_hash=state_hash, protocol_error="", extra=extra)
        transport.write_atomic(path, saved)
        say(f"  {paper_id}: lần {result.get('attempt')} → {result.get('status')} · "
            f"{result.get('accepted_units', 0)} đơn vị đạt")
        if result.get("status") in ("done", "review"):
            return result
    raise answer.AIError("Đã dừng ở giới hạn lượt gọi; phần đã đạt và tiến độ được giữ. "
                         "Xem analysis_runner.json / analysis_usage.jsonl và sửa phần còn lại qua MCP.")


def run(con, vault, say=print, limit=None):
    papers = pending_papers(con, vault)[:limit] if limit else pending_papers(con, vault)
    totals = {"papers": len(papers), "done": 0, "review": 0, "needs_fix": 0,
              "blocked_by_writer": 0, "errors": 0}
    if not papers:
        say("Không có bài nào chờ phân tích trong vault này." if config.vault_dir(vault).is_dir()
            else f"Không tìm thấy vault '{vault}'.")
        return totals
    status = answer.provider_status(answer.settings("analysis"))
    if not status.get("ready"):  # say it once, before any paper is started
        say(f"Dừng: {status['message']}")
        totals["errors"] += 1
        return totals
    for i, p in enumerate(papers, 1):
        say(f"[{i}/{len(papers)}] Phân tích {p['paper_id']}")
        try:
            status = analyze_paper(con, p["paper_id"], vault, say).get("status", "needs_fix")
            totals[status] = totals.get(status, 0) + 1
            if status == "blocked_by_writer":
                say(f"  Dừng: writer lock is busy; {totals['done'] + totals['review']} items done and "
                    f"{len(papers) - i} wait for the lock.")
                break
        except answer.AIError as e:
            say(f"  Dừng: {e}")
            totals["errors"] += 1
            if (db.read_json(db.paper_dir(p['paper_id']) / 'analysis_quality.json', {}) or {}).get('ready_for_analysis') is False:
                continue  # one damaged PDF must not prevent analysis of the other papers
            break
        except Exception as e:  # one paper failing must not stop the batch
            say(f"  Lỗi {type(e).__name__}: {e}")
            totals["errors"] += 1
    return totals

def run_structured(con, paper_id, kind='both', allow_ai=False, max_chunks=1, provider=None, model=None, reasoning=None):
    """Explicit bounded profile work; never calls evidence analysis or an answer fallback."""
    from . import structure, workflow
    if kind not in ('overview', 'wh', 'protocol', 'both', 'all') or not isinstance(max_chunks, int) or not 1 <= max_chunks <= 20:
        raise ValueError('Choose overview|wh|protocol|both|all and max_chunks from 1 to 20')
    profiles = structure.completion(paper_id)
    kinds = ('overview', 'wh', 'protocol') if kind == 'all' else ('wh', 'protocol') if kind == 'both' else (kind,)
    needed = [k for k in kinds if not profiles[k]['ready']]
    if not needed:
        return {'status': 'complete', 'overall_complete': profiles['complete'], 'calls': 0, 'profiles': profiles, 'scientific_acceptance': 'not_assessed'}
    next_action = 'Use structured-prepare/structured-submit, or explicitly configure ai.tasks.analysis and run structured-run <paper> --kind overview --ai --max-chunks 1.'
    if not allow_ai:
        return {'status': 'waiting', 'calls': 0, 'message': 'Structured AI requires explicit --ai opt-in.', 'next': next_action, 'profiles': profiles}
    settings = answer.settings('analysis')
    if provider is not None:
        if provider not in answer.PROVIDERS:
            raise ValueError('Unknown explicit analysis provider')
        settings['provider'] = provider
        settings['config_error'] = None
    if model is not None or reasoning is not None:
        if not isinstance(settings.get(settings['provider']), dict):
            raise ValueError('Provider does not support model/reasoning overrides')
        settings[settings['provider']] = dict(settings[settings['provider']])
        if model is not None:
            settings[settings['provider']]['model'] = model
        if reasoning is not None:
            settings[settings['provider']]['reasoning'] = reasoning
    if provider is None and not (settings.get('tasks') or {}).get('analysis'):
        return {'status': 'waiting', 'calls': 0, 'message': 'Configure ai.tasks.analysis explicitly; the answer route is not used.', 'next': next_action}
    provider = answer.provider_status(settings)
    if not provider.get('ready'):
        return {'status': 'waiting', 'calls': 0, 'message': provider.get('message'), 'next': next_action}
    preflight = quality.inspect(paper_id)
    if not preflight.get('ready_for_analysis'):
        return {'status': 'waiting', 'calls': 0, 'message': 'Repair source font/OCR before structured AI.', 'next': 'quality-check ' + paper_id}
    signature = transport.fingerprint([settings['provider'], settings.get(settings['provider']), settings.get('timeout_seconds'),
                                       structure.STRUCTURED_RULES, workflow.fingerprint([Path(__file__)])])
    analyst = f"{settings['provider']}/{answer.model_of(settings) or 'default'}"
    configured_attempts = (config.load().get('analysis') or {}).get('max_attempts', 2)
    try:
        max_attempts = int(configured_attempts)
    except (TypeError, ValueError):
        raise ValueError('analysis.max_attempts must be a positive integer')
    if max_attempts < 1:
        raise ValueError('analysis.max_attempts must be a positive integer')
    calls, processed = 0, 0
    # The provider call must stay outside the writer lock.  Each structured
    # submit acquires the lock only for its durable publication.
    with nullcontext():
        for profile_kind in needed:
            if profiles[profile_kind].get('status') == 'pending_review' and profiles[profile_kind].get('technical_coverage'):
                continue  # all-skipped source needs explicit review, never repeated automated attempts
            while processed < max_chunks:
                try:
                    task = structure.prepare_structured(paper_id, profile_kind)
                except workflow.WriterBusy as exc:
                    return {'status': 'blocked_by_writer', 'calls': calls, 'processed_chunks': processed,
                            'message': str(exc), 'next': 'Retry after the writer lock is released.',
                            'profiles': structure.completion(paper_id)}
                chunk = task['chunk']
                if chunk is None:
                    break  # all-skipped submission remains pending scientific review
                path = structure._task_path(paper_id, task['task_id'])
                state = structure._read_profile(path)
                current = next(c for c in state['chunks'] if c['chunk_id'] == chunk['chunk_id'])
                runner = current.get('runner') or {}
                raw_path = path.parent / (task['task_id'] + '_' + chunk['chunk_id'] + '_reply.json')
                cached = structure._read_profile(raw_path)
                raw = cached.get('raw') if (runner.get('provider_signature') == signature and runner.get('reply_sha256')
                        and runner['reply_sha256'] == workflow.fingerprint([raw_path])) else None
                if current['status'] == 'accepted':
                    reply = current['reply'] | {k: task[k] for k in ('source_sha256', 'source_revision', 'revision_of')}
                    try:
                        result = structure.submit_structured(con, paper_id, task['task_id'], current['chunk_id'], reply, analyst)
                    except workflow.WriterBusy as exc:
                        return {'status': 'blocked_by_writer', 'calls': calls, 'processed_chunks': processed,
                                'message': str(exc), 'next': 'Retry after the writer lock is released.',
                                'profiles': structure.completion(paper_id)}
                    processed += 1
                    continue
                if raw is None:
                    if current.get('attempts', 0) >= max_attempts:
                        exhausted = 'Two attempts exhausted' if max_attempts == 2 else f'{max_attempts} attempts exhausted'
                        return {'status': 'waiting', 'calls': calls, 'message': f'{exhausted}; valid chunks retained. Repair this chunk via structured-submit.',
                                'task_id': task['task_id'], 'chunk_id': current['chunk_id'], 'next': next_action, 'profiles': structure.completion(paper_id)}
                    current['attempts'] = current.get('attempts', 0) + 1
                    current['runner'] = {'provider_signature': signature, 'status': 'running', 'started_at': config.now()}
                    workflow.atomic_json(path, state)
                    task_context = {k: task[k] for k in ('paper_id', 'kind', 'source_evidence_type', 'source_sha256', 'source_revision', 'revision_of',
                                                         'sentences', 'context_sentences', 'paper_outline',
                                                         'reply_format', 'source_absence_reason_required')}
                    task_context['paper_overview'] = task.get('paper_overview')
                    user = transport.dumps(task_context)
                    if current.get('error'):
                        user += '\nRepair the prior validation error: ' + str(current['error']) + '\n' + REPAIR_HINTS
                    calls += 1
                    try:
                        raw = _call(settings, task['rules'], user, paper_id, 'structured_' + profile_kind)
                    except answer.AIError as exc:
                        current.update(error=str(exc), runner={**current['runner'], 'status': 'failed'})
                        workflow.atomic_json(path, state)
                        return {'status': 'waiting', 'calls': calls, 'message': str(exc), 'next': next_action}
                    workflow.atomic_json(raw_path, {'raw': raw, 'time': config.now(), 'provider_signature': signature})
                    current['runner'].update(status='returned', reply_sha256=workflow.fingerprint([raw_path]))
                    workflow.atomic_json(path, state)
                try:
                    text = raw.strip()
                    if text.startswith('```') and text.endswith('```'):
                        text = text.split('\n', 1)[1].rsplit('```', 1)[0].strip()
                    reply = _unwrap(json.loads(text), task.get('reply_format'))
                    result = structure.submit_structured(con, paper_id, task['task_id'], current['chunk_id'], reply, analyst,
                                                         {'provider': settings['provider'], 'model': answer.model_of(settings), 'provider_signature': signature,
                                                          'reply_sha256': workflow.fingerprint([raw_path]), 'analyst': analyst})
                    if not result.get('ok'):
                        raise ValueError(transport.dumps(result))
                except workflow.WriterBusy as exc:
                    return {'status': 'blocked_by_writer', 'calls': calls, 'processed_chunks': processed,
                            'message': str(exc), 'next': 'Retry after the writer lock is released.',
                            'profiles': structure.completion(paper_id)}
                except (ValueError, TypeError, IndexError) as exc:
                    # Accepted work was already atomically retained; only this rejected chunk is repaired.
                    state = structure._read_profile(path)
                    current = next(c for c in state['chunks'] if c['chunk_id'] == chunk['chunk_id'])
                    current['error'] = str(exc)
                    current['runner']['reply_sha256'] = None
                    workflow.atomic_json(path, state)
                    continue
                processed += 1
                if result.get('status') == 'submitted':
                    break
    profiles = structure.completion(paper_id)
    requested_complete = all(profiles[k]['ready'] for k in kinds)
    return {'status': 'complete' if requested_complete else 'waiting', 'overall_complete': profiles['complete'], 'calls': calls, 'processed_chunks': processed,
            'profiles': profiles, 'next': next_action if not requested_complete else None,
            'scientific_acceptance': 'not_assessed'}
