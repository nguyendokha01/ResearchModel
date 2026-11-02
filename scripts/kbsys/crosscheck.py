"""Independent V13 statement-to-quote review by a separately configured verifier model.

The verifier is `ai.tasks.crosscheck` in config/ai.yaml (any provider, e.g. another vendor than the analyst).
Unset, it stays the historical Codex `gpt-5.6-terra`, whose reports keep the bare model name as `model`."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

from . import config, db

MODEL = "gpt-5.6-terra"  # historical default verifier (Codex CLI)
ELIGIBLE = {"result", "cited_statement", "conclusion"}
REPORT = "analysis_v13.json"
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["reviews"],
    "properties": {"reviews": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["ordinal", "verdict", "reason"],
        "properties": {"ordinal": {"type": "integer", "minimum": 1},
                       "verdict": {"type": "string", "enum": ["supported", "partly", "not_supported"]},
                       "reason": {"type": "string", "maxLength": 500}}
    }}}
}


def _fingerprint(unit):
    payload = json.dumps({"statement": unit.get("statement"), "quote": unit.get("quote"),
                          "sentence_ids": unit.get("sentence_ids")}, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _report_path(paper_id):
    return db.paper_dir(paper_id) / REPORT


def _load_report(paper_id):
    return db.read_json(_report_path(paper_id), {}) or {}


def verifier():
    """(settings, verifier_id) for V13. Unconfigured -> Codex CLI with MODEL (legacy id = MODEL)."""
    from . import answer
    s = answer.settings("crosscheck")
    if not (s.get("tasks") or {}).get("crosscheck"):
        s["provider"] = "codex_cli"
        s["codex_cli"] = {**s["codex_cli"], "model": MODEL}
    model = answer.model_of(s)
    if s["provider"] == "codex_cli" and model == MODEL:
        return s, MODEL
    return s, f"{s['provider']}:{model or 'default'}"


SYSTEM = ("Independently assess whether each biomedical statement is supported by its quoted source passage. "
          "A statement is supported only if the quote supports its subject, direction, certainty, attribution, "
          "and material details. Use partly when some but not all is supported; not_supported when the quote "
          "does not support the central claim. Do not use outside knowledge. Return a concise reason grounded "
          "only in the supplied quote. Preserve each ordinal exactly.")


def _check_reviews(batch, reviews):
    expected = {item["ordinal"] for item in batch}
    got = [item.get("ordinal") for item in reviews or []]
    if len(got) != len(expected) or set(got) != expected:
        raise ValueError("Verifier response has missing, duplicate, or unexpected ordinals")
    for item in reviews:
        if item.get("verdict") not in {"supported", "partly", "not_supported"}:
            raise ValueError("Verifier response has an invalid verdict")
    return reviews


def provider_prompt(batch):
    """User message for a V13 batch (any provider, including an MCP agent)."""
    return ("Review these statement/quote pairs. Reply with JSON only: {\"reviews\": [{\"ordinal\": n, "
            "\"verdict\": \"supported|partly|not_supported\", \"reason\": \"...\"}]}\n"
            + json.dumps(batch, ensure_ascii=False))


def pending_batch(paper_id):
    """V13 request for units of a paper without a current result: [{ordinal, statement, quote}], unit ids."""
    units = list(db.read_jsonl(db.paper_dir(paper_id) / "evidence.jsonl"))
    done = results_for(paper_id, units)
    todo = [u for u in units if u.get("unit_type") in ELIGIBLE and u["unit_id"] not in done]
    return [{"ordinal": n, "statement": u.get("statement"), "quote": u.get("quote")}
            for n, u in enumerate(todo, 1)], [u["unit_id"] for u in todo]


def _provider_call(s, batch):
    """Any configured provider (answer._chat); the reply must be the SCHEMA JSON."""
    from . import answer
    user = provider_prompt(batch)
    raw, _ = answer._chat(s, SYSTEM, user)
    data = answer._json(raw)
    return _check_reviews(batch, (data or {}).get("reviews") if isinstance(data, dict) else None)


def _codex_call(exe, batch, timeout, model=MODEL):
    prompt = SYSTEM + "\n\nReview these statement/quote pairs:\n" + json.dumps(batch, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="kb_v13_") as tmp:
        out = Path(tmp) / "reply.json"
        schema = Path(tmp) / "schema.json"
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8", newline="\n")
        cmd = [exe, "exec", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config", "-s", "read-only",
               "-C", tmp, "-m", model, "-c", 'model_reasoning_effort="low"', "--output-schema", str(schema),
               "-o", str(out), "-"]
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout)
        if proc.returncode or not out.is_file():
            detail = ((proc.stderr or "") + "\n" + (proc.stdout or ""))[-1200:]
            raise RuntimeError(f"Codex {model} call failed ({proc.returncode}): {detail.strip()}")
        data = json.loads(out.read_text(encoding="utf-8"))
        return _check_reviews(batch, data.get("reviews"))


def run(con, paper_ids, batch_size=10, timeout=180, override=None):
    """`override` = (settings, verifier_id), e.g. an MCP-connected agent acting as verifier."""
    s, verifier_id = override or verifier()
    exe = None
    if s["provider"] == "codex_cli" and not override:
        exe = shutil.which(s["codex_cli"].get("command") or "codex")
        if not exe:
            raise RuntimeError("Codex CLI not found (or set ai.tasks.crosscheck to another provider)")
    elif s["provider"] in ("none", "mcp_agent") and not override:
        raise RuntimeError("V13 needs a verifier: set ai.tasks.crosscheck in config/ai.yaml to a provider "
                           "(use a different model/vendor than the one that wrote the analysis)")
    total = {"papers": 0, "eligible": 0, "supported": 0, "partly": 0, "not_supported": 0, "errors": 0}
    for paper_id in paper_ids:
        folder = db.paper_dir(paper_id)
        analysis = db.read_json(folder / "analysis.json") or {}
        if analysis.get("version") != 2:
            continue
        units = list(db.read_jsonl(folder / "evidence.jsonl"))
        candidates = [(i, u) for i, u in enumerate(units, 1) if u.get("unit_type") in ELIGIBLE]
        report = _load_report(paper_id)
        if report and report.get("model") != verifier_id:
            raise RuntimeError(f"{paper_id}: existing {REPORT} was made by {report.get('model')}, verifier is now "
                               f"{verifier_id}; rename that file to start a new V13 report (refusing to mix verifiers)")
        if not report:
            report = {"version": 1, "model": verifier_id, "reasoning": "low", "created_at": config.now(),
                      "eligible_types": sorted(ELIGIBLE), "results": {}, "errors": []}
        results = report.setdefault("results", {})
        pending = []
        for seq, unit in candidates:
            key = unit["unit_id"]
            fingerprint = _fingerprint(unit)
            old = results.get(key)
            if old and old.get("fingerprint") == fingerprint:
                total[old["verdict"]] += 1
                continue
            pending.append({"seq": seq, "unit": unit, "fingerprint": fingerprint})
        total["papers"] += 1
        total["eligible"] += len(candidates)
        report["eligible_count"] = len(candidates)
        report["updated_at"] = config.now()
        _save_report(folder, report)
        for start in range(0, len(pending), batch_size):
            chunk = pending[start:start + batch_size]
            request = [{"ordinal": n + 1, "statement": x["unit"].get("statement"),
                        "quote": x["unit"].get("quote")} for n, x in enumerate(chunk)]
            try:
                reviews = (_codex_call(exe, request, timeout, s["codex_cli"]["model"]) if exe
                           else _provider_call(s, request))
            except Exception as exc:
                report.setdefault("errors", []).append({"at": config.now(), "first_seq": chunk[0]["seq"],
                                                        "last_seq": chunk[-1]["seq"], "error": str(exc)})
                report["updated_at"] = config.now()
                _save_report(folder, report)
                total["errors"] += 1
                print(f"{paper_id}: V13 halted at unit {chunk[0]['seq']}: {exc}", flush=True)
                return total
            by_ordinal = {item["ordinal"]: item for item in reviews}
            for n, item in enumerate(chunk, 1):
                review = by_ordinal[n]
                results[item["unit"]["unit_id"]] = {
                    "fingerprint": item["fingerprint"], "unit_type": item["unit"].get("unit_type"),
                    "verdict": review["verdict"], "reason": review["reason"],
                    "checked_at": config.now(), "model": verifier_id
                }
                total[review["verdict"]] += 1
            report["updated_at"] = config.now()
            _save_report(folder, report)
            print(f"{paper_id}: V13 reviewed through unit {chunk[-1]['seq']} / {len(units)}", flush=True)
        if candidates:
            from . import evidence
            refreshed = evidence.recheck(con, paper_id)
            con.commit()
            print(f"{paper_id}: V13 flags applied; {refreshed['flagged_units']} units flagged", flush=True)
    return total


def _save_report(folder, report):
    path = folder / REPORT
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    tmp.replace(path)


def results_for(paper_id, units):
    report = _load_report(paper_id)
    if not report.get("model"):
        return {}
    current = {unit.get("unit_id"): _fingerprint(unit) for unit in units}
    return {key: row for key, row in (report.get("results") or {}).items()
            if key in current and row.get("fingerprint") == current[key]}


def unit_checks(report):
    r = report.expect
    r(_fingerprint({"statement": "claim", "quote": "quote", "sentence_ids": ["S0001"]}) ==
      _fingerprint({"sentence_ids": ["S0001"], "quote": "quote", "statement": "claim"}),
      "v13/fingerprint", "fingerprint is independent of JSON key order")
    r(_fingerprint({"statement": "claim", "quote": "quote", "sentence_ids": ["S0001"]}) !=
      _fingerprint({"statement": "claim", "quote": "changed", "sentence_ids": ["S0001"]}),
      "v13/fingerprint", "quote change invalidates an earlier review")
    r(ELIGIBLE == {"result", "cited_statement", "conclusion"}, "v13/eligibility",
      "only the approved V13 evidence types are reviewed")
    r(SCHEMA["properties"]["reviews"]["items"]["properties"]["verdict"]["enum"] ==
      ["supported", "partly", "not_supported"], "v13/verdict", "the model response is restricted to contract verdicts")
    sample_unit = {"unit_id": "p:E0001", "unit_type": "result", "statement": "claim",
                   "quote": "quote", "sentence_ids": ["S0001"], "status": "ok", "flags": []}
    fp = _fingerprint(sample_unit)
    base = {"version": 1, "model": MODEL, "eligible_types": sorted(ELIGIBLE), "results": {
        "p:E0001": {"fingerprint": fp, "unit_type": "result", "verdict": "supported", "model": MODEL}}}
    r(not validate_report_units([sample_unit], base)["errors"], "v13/selftest_fixture",
      "valid supported review passes integrated checks")
    adverse = json.loads(json.dumps(base))
    adverse["results"]["p:E0001"].update(verdict="partly")
    r(any("must have exactly one matching V13 flag" in error
          for error in validate_report_units([sample_unit], adverse)["errors"]),
      "v13/selftest_fixture", "partly verdict without a stored flag is detected")
    stale_flag = dict(sample_unit, status="flagged", flags=[{"code": "V13", "message": "partly"}])
    r(any("stale or unsupported V13 flag" in error
          for error in validate_report_units([stale_flag], base)["errors"]),
      "v13/selftest_fixture", "supported verdict cannot retain a stale V13 flag")
    changed = dict(sample_unit, quote="changed")
    r(not validate_report_units([changed], base)["errors"] and
      validate_report_units([changed], base)["warnings"], "v13/selftest_fixture",
      "a changed fingerprint is treated as stale/resumable, not applied")
    pending = {**base, "results": {}}
    pending_check = validate_report_units([stale_flag], pending)
    r(not pending_check["errors"] and any("retained pending" in warning for warning in pending_check["warnings"]),
      "v13/selftest_fixture", "a prior verification flag survives while its refreshed result is missing")


def validate_report_units(units, report):
    """Check durable report shape and consistency of all currently applicable V13 flags."""
    errors, warnings = [], []
    if not isinstance(report, dict):
        return {"errors": ["report must be an object"], "warnings": [], "current_results": 0,
                "eligible": 0}
    if report.get("version") != 1:
        errors.append("report version must be 1")
    model = report.get("model")
    if not isinstance(model, str) or not model:
        errors.append("report model must name the verifier")
    if report.get("eligible_types") != sorted(ELIGIBLE):
        errors.append("report eligible_types do not match the V13 contract")
    results = report.get("results")
    if not isinstance(results, dict):
        return {"errors": errors + ["results must be an object"], "warnings": warnings,
                "current_results": 0, "eligible": sum(u.get("unit_type") in ELIGIBLE for u in units)}
    current = {u.get("unit_id"): u for u in units}
    eligible = sum(u.get("unit_type") in ELIGIBLE for u in units)
    current_results = 0
    for unit_id, row in results.items():
        if not isinstance(row, dict):
            errors.append(f"{unit_id}: result must be an object")
            continue
        if row.get("verdict") not in {"supported", "partly", "not_supported"}:
            errors.append(f"{unit_id}: invalid verdict")
        if row.get("model") != model:
            errors.append(f"{unit_id}: result model must equal the report verifier {model}")
        if not isinstance(row.get("fingerprint"), str) or len(row["fingerprint"]) != 64:
            errors.append(f"{unit_id}: fingerprint must be a SHA-256 hex digest")
            continue
        if any(ch not in "0123456789abcdef" for ch in row["fingerprint"].lower()):
            errors.append(f"{unit_id}: fingerprint must be a SHA-256 hex digest")
            continue
        unit = current.get(unit_id)
        if unit is None or unit.get("unit_type") not in ELIGIBLE or row["fingerprint"] != _fingerprint(unit):
            continue  # retained stale rows do not apply; the resumable runner may replace them
        current_results += 1
        unit_flags = [f for f in unit.get("flags", []) if f.get("code") == "V13"]
        verdict = row["verdict"]
        if verdict == "supported":
            if unit_flags:
                errors.append(f"{unit_id}: stale or unsupported V13 flag for supported verdict")
        elif (len(unit_flags) != 1 or unit.get("status") != "flagged" or
              verdict not in unit_flags[0].get("message", "")):
            errors.append(f"{unit_id}: {verdict} verdict must have exactly one matching V13 flag")
    for unit in units:
        unit_id = unit.get("unit_id")
        unit_flags = [f for f in unit.get("flags", []) if f.get("code") == "V13"]
        if not unit_flags:
            continue
        row = results.get(unit_id)
        if (not isinstance(row, dict) or row.get("verdict") not in {"partly", "not_supported"} or
                row.get("fingerprint") != _fingerprint(unit) or row.get("model") != report.get("model")):
            warnings.append(f"{unit_id}: prior V13 flag retained pending a current matching result")
    if current_results < eligible:
        warnings.append(f"{eligible - current_results} eligible units lack a current V13 result (report may be resumable)")
    return {"errors": errors, "warnings": warnings, "current_results": current_results, "eligible": eligible}
