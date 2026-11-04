"""Lossless AI reading format and locally merged repairs.

The runner entry point in this module deliberately does not acquire the
knowledge-store writer lock.  It only performs the provider call; callers
persist and validate replies separately.
"""
import base64
import copy
import hashlib
import json
import mimetypes
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

from . import answer, config

FIELDS = {"classification", "classification_reason", "evidence_type", "research_question", "topics", "summary"}
REPAIR_RULES = """REPAIR MODE: the previous analysis is saved locally. Do not repeat it.
Return JSON with optional keys:
replace_units: [{index: original 1-based rejected unit index, unit: complete corrected unit}],
add_units: [new units for missing findings], fields: {paper-level fields to update},
skipped: [{sentence_id, reason}] (complete corrected skip list, only when it needs changing).
Only listed rejected units may be replaced. Accepted units and their indices remain unchanged.
If a rejected unit already cites a missing sentence, replace that unit; do not add a duplicate.
fields accepts classification, classification_reason, evidence_type, research_question, topics, summary.
Omitted content stays unchanged; additions append to the end. Never omit findings to save tokens.
If more source context is needed, return ONLY {"need_context": "all"} or
{"need_context": ["S0001", ...]}. Never guess missing context. No quotes: code copies them.
"""


class ImageInputUnsupported(ValueError):
    """The selected provider cannot receive an image through this transport."""


def provider_settings(provider=None, model=None, reasoning=None, timeout=None, task="analysis"):
    """Return a per-run provider override without editing config/ai.yaml.

    `task` may be figure_reading or node_card; answer.settings falls back to the `analysis` route."""
    settings = answer.settings(task)
    if provider is not None:
        if provider not in answer.PROVIDERS:
            raise ValueError("Unknown explicit analysis provider")
        settings["provider"] = provider
        settings["config_error"] = None
    selected = settings["provider"]
    if model is not None or reasoning is not None:
        if not isinstance(settings.get(selected), dict):
            raise ValueError("Provider does not support model/reasoning overrides")
        settings[selected] = dict(settings[selected])
        if model is not None:
            settings[selected]["model"] = model
        if reasoning is not None:
            settings[selected]["reasoning"] = reasoning
    if timeout is not None:
        value = float(timeout)
        if not (0 < value < float("inf")):
            raise ValueError("timeout must be positive")
        settings["timeout_seconds"] = value
    elif provider is not None:
        settings["timeout_seconds"] = answer.timeout_for(settings, selected)
    return settings


def _image_parts(image_paths):
    parts = []
    for value in image_paths or ():
        path = Path(value)
        if not path.is_file():
            raise FileNotFoundError(f"Image input is unavailable: {path}")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append((path, mime, encoded))
    return parts


def _openai_image_call(settings, system, user, images):
    provider = settings["provider"]
    conf = settings[provider]
    content = [{"type": "text", "text": user}]
    content.extend({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}
                  for _path, mime, encoded in images)
    body = {"model": conf["model"], "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}]}
    if conf.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    key = answer._secret(f"{provider}_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    data = answer._post_json(provider, req, settings["timeout_seconds"])
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise answer.AIError(f"{provider} returned an unexpected result: {str(data)[:300]}") from None
    return text, data.get("usage") or {}


def _anthropic_image_call(settings, system, user, images):
    conf = settings["anthropic"]
    key = answer._secret("anthropic_api_key")
    content = [{"type": "text", "text": user}]
    content.extend({"type": "image", "source": {"type": "base64", "media_type": mime, "data": encoded}}
                   for _path, mime, encoded in images)
    body = {"model": conf["model"], "max_tokens": int(conf.get("max_tokens") or 4096),
            "temperature": 0.1, "system": system, "messages": [{"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    req = urllib.request.Request(conf["base_url"].rstrip("/") + "/messages",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    data = answer._post_json("anthropic", req, settings["timeout_seconds"])
    text = "".join(block.get("text", "") for block in data.get("content") or []
                   if isinstance(block, dict) and block.get("type") == "text")
    if not text:
        raise answer.AIError(f"anthropic returned an unexpected result: {str(data)[:300]}")
    return text, data.get("usage") or {}


def _codex_image_call(settings, system, user, images):
    conf = settings["codex_cli"]
    exe = shutil.which(conf.get("command") or "codex")
    if not exe:
        raise answer.AIError("Codex CLI was not found")
    prompt = system + "\n\n" + user + "\n\nDo not run any command or tool. Reply with the JSON only."
    work = config.CACHE_DIR / "codex_answer"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=work) as tmp:
        out = Path(tmp) / "last.txt"
        cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
               "-s", "read-only", "-C", tmp, "-o", str(out)]
        if conf.get("model"):
            cmd += ["-m", conf["model"]]
            if conf.get("reasoning"):
                cmd += ["-c", f'model_reasoning_effort="{conf["reasoning"]}"']
        for path, _mime, _encoded in images:
            # Codex CLI accepts one or more -i/--image arguments before the prompt.
            cmd += ["-i", str(path)]
        cmd.append("-")
        proc = answer._run_cli(cmd, prompt, settings["timeout_seconds"], provider="codex_cli")
        if out.is_file() and out.read_text(encoding="utf-8").strip() and proc.returncode == 0:
            usage = {"model": conf.get("model") or "account default"}
            for line in (proc.stdout or "").splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                    for key, value in event["usage"].items():
                        if type(value) in (int, float):
                            usage[key] = usage.get(key, 0) + value
            return out.read_text(encoding="utf-8"), usage
        detail = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()[-600:] or "no output"
        raise answer.AIError(f"Codex did not return a reply: {detail[-300:]}")


def call(settings, system, user, image_paths=()):
    """Call a configured provider, optionally attaching verified image paths.

    Without images this delegates to the established provider implementation.
    With images, only providers with an explicit image request format are
    accepted; silently dropping an image would make a figure reading unsafe.
    """
    images = _image_parts(image_paths)
    if not images:
        return answer._chat(settings, system, user)
    provider = settings.get("provider")
    if provider == "codex_cli":
        return _codex_image_call(settings, system, user, images)
    if provider == "anthropic":
        return _anthropic_image_call(settings, system, user, images)
    # openrouter and ollama (its /v1 endpoint) take the same OpenAI-style image_url data-URL part
    if provider in {"gemini", "openai_compatible", "openrouter", "ollama"}:
        return _openai_image_call(settings, system, user, images)
    raise ImageInputUnsupported(f"image input unsupported for provider {provider}")


def logged_call(call_fn, log_path, stage, settings, system, user, extra=None, **kw):
    """Run call_fn(settings, system, user, **kw) and append ONE usage row to log_path (the one usage-log format
    of all four AI loops). No key or prompt text is logged; a log write error is reported, never raised."""
    started = time.monotonic()
    entry = {"time": config.now(), "provider": settings["provider"], "model": answer.model_of(settings),
             "stage": stage, "input_chars": len(system) + len(user), "request_sha256": fingerprint([system, user]),
             **(extra or {})}
    try:
        raw, usage = call_fn(settings, system, user, **kw)
        entry.update(output_chars=len(raw), usage=normalized_usage(usage), provider_usage=usage, status="returned")
        return raw, usage
    except BaseException as exc:  # an interrupt still leaves a complete row
        entry.update(status="failed", usage={}, error=type(exc).__name__)
        raise
    finally:
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(dumps(entry) + "\n")
        except OSError as exc:
            print(f"usage log not written ({log_path.name}): {exc}", file=sys.stderr)


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def fingerprint(value):
    return hashlib.sha256(dumps(value).encode("utf-8")).hexdigest()


def write_atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(dumps(value) + "\n", encoding="utf-8", newline="\n")
    temp.replace(path)


def compact_reading(rows):
    """Keep every sentence/ID, citation, source, continuation and table warning."""
    lines, previous = [], None
    for s in rows:
        group = (s.get("source", "main"), s.get("section"), s.get("page"), s.get("kind"), s.get("subsection"))
        if group != previous:
            lines.append(f"[{group[0]} | {group[1]} | p{group[2]} | {group[3]}"
                         + (f" | {group[4]}" if group[4] else "") + "]")
            previous = group
        tags = ["MUST"] if s.get("must_cover") else []
        if s.get("in_table"):
            tags.append("in_table: cite table rows instead")
        if s.get("cites"):
            tags.append("cites=" + ";".join(s["cites"]))
        if s.get("continues"):
            tags.append("continues=" + s["continues"])
        if s.get("object_id"):
            tags.append(s["object_id"] + ":" + s.get("table_quality", "uncertain"))
        lines.append(s["id"] + (" [" + " | ".join(tags) + "]" if tags else "") + " " + s["text"])
    return "\n".join(lines)


def compact_view(view):
    return {**{k: v for k, v in view.items() if k != 'sentences'},
            'format': 'compact', 'reading': compact_reading(view.get('sentences', []))}


def bad_indices(result):
    return {e["unit_index"] for e in result.get("errors", []) if type(e.get("unit_index")) is int and e["unit_index"] > 0}


def merge_patch(previous, patch, result):
    """Reject malformed patches atomically. Accepted unit positions never shift."""
    if not isinstance(patch, dict) or set(patch) - {"replace_units", "add_units", "fields", "skipped"}:
        raise ValueError("Repair must contain only replace_units, add_units, fields, skipped")
    merged = copy.deepcopy(previous)
    units = merged.get("units")
    if not isinstance(units, list):
        units = merged["units"] = []
    replacements, additions = patch.get("replace_units", []), patch.get("add_units", [])
    if not isinstance(replacements, list) or not isinstance(additions, list):
        raise ValueError("replace_units and add_units must be lists")
    seen, allowed = set(), bad_indices(result)
    for item in replacements:
        if not isinstance(item, dict) or set(item) != {"index", "unit"}:
            raise ValueError("Each replacement needs index and unit")
        i = item["index"]
        if type(i) is not int or not 1 <= i <= len(units) or i not in allowed or i in seen:
            raise ValueError("Replace only listed rejected indices, once per index")
        if not isinstance(item["unit"], dict):
            raise ValueError("Replacement unit must be an object")
        units[i - 1] = copy.deepcopy(item["unit"])
        seen.add(i)
    if not all(isinstance(u, dict) for u in additions):
        raise ValueError("New units must be objects")
    units.extend(copy.deepcopy(additions))
    fields = patch.get("fields", {})
    if not isinstance(fields, dict) or set(fields) - FIELDS:
        raise ValueError("Unknown paper-level repair fields")
    merged.update(copy.deepcopy(fields))
    if "skipped" in patch:
        skips = patch["skipped"]
        if not isinstance(skips, list) or not all(isinstance(s, dict) and isinstance(s.get("sentence_id"), str)
                                                and isinstance(s.get("reason"), str) for s in skips):
            raise ValueError("skipped must list sentence_id/reason objects")
        merged["skipped"] = copy.deepcopy(skips)
    return merged


def normalize_term_placement(reply):
    """Move valid term lists in new AI units into `terms`, without changing values.

    This repairs a common JSON placement error; malformed values remain for the validator.
    Only the current reply is copied, so previously accepted units remain unchanged.
    """
    fixed = copy.deepcopy(reply)
    if not isinstance(fixed, dict):
        return fixed
    units = list(fixed.get('units', [])) if isinstance(fixed.get('units'), list) else []
    units += list(fixed.get('add_units', [])) if isinstance(fixed.get('add_units'), list) else []
    units += [item.get('unit') for item in fixed.get('replace_units', [])
              if isinstance(item, dict)] if isinstance(fixed.get('replace_units'), list) else []
    for unit in units:
        if not isinstance(unit, dict):
            continue
        terms = unit.get('terms')
        if terms is not None and not isinstance(terms, dict):
            continue
        for key in ('concepts', 'methods', 'molecules', 'cell_types', 'species', 'topics'):
            values = unit.get(key)
            if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
                continue
            if terms is None:
                terms = unit['terms'] = {}
            if key not in terms:
                terms[key] = values
                del unit[key]
            elif isinstance(terms[key], list) and all(isinstance(value, str) for value in terms[key]):
                terms[key].extend(value for value in values if value not in terms[key])
                del unit[key]
    return fixed


def repair_context(rows, submission, result, extra=None):
    if extra == "all" or any(e.get("unit_index") == 0 for e in result.get("errors", [])):
        return rows
    wanted = set(result.get("coverage_missing", [])) | set(extra or [])
    units = submission.get("units") or []
    for i in bad_indices(result):
        if i <= len(units) and isinstance(units[i - 1], dict):
            ids = units[i - 1].get("sentence_ids")
            if isinstance(ids, list):
                wanted.update(str(s).split(":")[-1] for s in ids)
    selected = set()
    for i, row in enumerate(rows):
        if row["id"] in wanted:
            selected.update(j for j in range(max(0, i - 3), min(len(rows), i + 4))
                            if rows[j].get("source", "main") == row.get("source", "main"))
    # Preserve full continuation chains in both directions, even across figure pages.
    while True:
        ids = {rows[i]["id"] for i in selected}
        targets = {rows[i].get("continues") for i in selected}
        enlarged = selected | {i for i, r in enumerate(rows) if r["id"] in targets or r.get("continues") in ids}
        if enlarged == selected:
            break
        selected = enlarged
    return [r for i, r in enumerate(rows) if i in selected]


def repair_prompt(base, rows, submission, result, extra=None):
    units = submission.get("units") or []
    payload = {"errors": result.get("errors", []), "coverage_missing": result.get("coverage_missing", []),
               "rejected_units": {str(i): units[i - 1] for i in sorted(bad_indices(result)) if i <= len(units)},
               "fields": {k: submission[k] for k in FIELDS if k in submission},
               "skipped": submission.get("skipped", []), "existing_unit_count": len(units)}
    if any(e.get("unit_index") == 0 for e in result.get("errors", [])):
        payload["unit_index"] = [{"index": i, "statement": u.get("statement"), "sentence_ids": u.get("sentence_ids")}
                                 for i, u in enumerate(units, 1) if isinstance(u, dict)]
    return (base + "\nChecked submission:\n" + dumps(payload) + "\nSource context:\n"
            + compact_reading(repair_context(rows, submission, result, extra)) + "\n" + REPAIR_RULES)


def normalized_usage(raw):
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for name, aliases in {"input_tokens": ("input_tokens", "prompt_tokens"),
                          "output_tokens": ("output_tokens", "completion_tokens"),
                          "cached_input_tokens": ("cached_input_tokens", "cache_read_input_tokens"),
                          "reasoning_tokens": ("reasoning_tokens",)}.items():
        for key in aliases:
            if type(raw.get(key)) in (int, float):
                out[name] = raw[key]
                break
    for key, field, target in (("prompt_tokens_details", "cached_tokens", "cached_input_tokens"),
                               ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens")):
        if isinstance(raw.get(key), dict) and type(raw[key].get(field)) in (int, float):
            out[target] = raw[key][field]
    # Anthropic's input_tokens excludes cache reads/writes; normalize to total input.
    if "cache_read_input_tokens" in raw or "cache_creation_input_tokens" in raw:
        if "input_tokens" in out:
            out["input_tokens"] += sum(raw.get(k, 0) for k in ("cache_read_input_tokens", "cache_creation_input_tokens")
                                       if type(raw.get(k, 0)) in (int, float))
    return out
