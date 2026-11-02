"""The Analysis Standard: `config/standard.yaml` is the only source; the two docs are generated from it.

    python scripts/kb.py standard                 check the yaml and that the docs are in sync
    python scripts/kb.py standard --write-docs    rewrite docs/STANDARD.md and docs/STANDARD_vi.md

Nothing here changes behaviour: code reads the standard from G3 on. `enforced_by` names the function that
checks a clause (`module.func` under kbsys, or a script module such as benchmark_research) or `G3` = not yet.
"""
import functools
import importlib
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
YAML = ROOT / "config" / "standard.yaml"
DOC_EN, DOC_VI = ROOT / "docs" / "STANDARD.md", ROOT / "docs" / "STANDARD_vi.md"
FIELDS = ("id", "stage", "rule", "vi", "pass", "fail", "level", "enforced_by", "audit")
NOT_YET = "G3"
STATUS_VI = {"draft": "bản nháp, chờ duyệt", "frozen": "đã đóng băng"}
GENERATED = "<!-- GENERATED from config/standard.yaml by `python scripts/kb.py standard --write-docs`. Do not edit by hand. -->"


def load(path=YAML):
    """The parsed standard; ValueError lists every schema problem at once."""
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as err:
        raise ValueError(f"cannot read {path}: {err}")
    if not isinstance(data, dict):
        raise ValueError("top level must be a mapping")
    bad = [f"top level: missing {k}" for k in ("version", "status", "stages", "clauses") if not data.get(k)]
    stages, seen = data.get("stages") or {}, set()
    for c in data.get("clauses") or []:
        if not isinstance(c, dict):
            bad.append(f"clause is not a mapping: {c!r}")
            continue
        cid, stage = c.get("id", "?"), str(c.get("stage"))
        bad += [f"{cid}: missing {k}" for k in FIELDS if c.get(k) in (None, "", [])]
        if not isinstance(c.get("audit", []), list):
            bad.append(f"{cid}: audit must be a list")
        if stage not in stages or not re.fullmatch(rf"{re.escape(stage)}-\d\d", str(cid)):
            bad.append(f"{cid}: id must be <stage>-<nn> with a listed stage")
        if c.get("level") not in ("fail", "warn"):
            bad.append(f"{cid}: level must be fail or warn")
        if cid in seen:
            bad.append(f"{cid}: duplicate id")
        seen.add(cid)
    for od in data.get("open_decisions") or []:
        bad += [f"{od.get('id', '?')}: unknown clause {c}" for c in od.get("clauses") or [] if c not in seen]
        bad += [f"{od.get('id', '?')}: missing {k}" for k in ("question_vi", "recommended_vi", "recommended") if od.get(k) in (None, "")]
    if bad:
        raise ValueError("\n".join(bad))
    return data


@functools.lru_cache(maxsize=1)
def _params():
    return {c["id"]: c.get("params") or {} for c in load()["clauses"]}


def param(clause_id, key):
    """A threshold or list from the `params` of a clause (KeyError names what the yaml lacks)."""
    try:
        return _params()[clause_id][key]
    except KeyError:
        raise KeyError(f"config/standard.yaml: clause {clause_id} has no param {key!r}") from None


def unresolved(data):
    """Clauses whose `enforced_by` is neither G3 nor an importable function."""
    out = []
    for c in data["clauses"]:
        ref = c["enforced_by"]
        if ref == NOT_YET:
            continue
        mod, _, func = str(ref).rpartition(".")
        for name in (f"kbsys.{mod}", mod):
            try:
                if mod and hasattr(importlib.import_module(name), func):
                    break
            except Exception:  # a module that cannot be imported is "unresolved", not a crash
                continue
        else:
            out.append(f"{c['id']}: {ref}")
    return out


def counts(data):
    """{stage: (clauses, fail-level, enforced by code)} in stage order."""
    out = {}
    for s in data["stages"]:
        rows = [c for c in data["clauses"] if c["stage"] == s]
        out[s] = (len(rows), sum(c["level"] == "fail" for c in rows), sum(c["enforced_by"] != NOT_YET for c in rows))
    return out


def _stage_rows(data):
    n = counts(data)
    return n, [(s, data["stages"][s], *n[s]) for s in data["stages"]]


def render_en(data):
    n, rows = _stage_rows(data)
    total = [sum(v[i] for v in n.values()) for i in range(3)]
    out = [GENERATED, "", f"# Analysis Standard v{data['version']} ({data['status']})", "",
           "One source of rules for every AI and every program of this project. Each clause has a measurable PASS and FAIL "
           "condition. `fail` clauses block, `warn` clauses are reported. `enforced_by` names the function that implements the clause (it may check only part of the PASS/FAIL condition until G3); `G3` means no code checks it yet.",
           "", "| Stage | Name | Clauses | Blocking (fail) | Enforced by code |", "|---|---|---|---|---|"]
    out += [f"| {s} | {name['en']} | {a} | {b} | {c} |" for s, name, a, b, c in rows]
    out += [f"| **Total** | | **{total[0]}** | **{total[1]}** | **{total[2]}** |", ""]
    for s, name in data["stages"].items():
        out += [f"## {s}: {name['en']}", ""]
        for c in (c for c in data["clauses"] if c["stage"] == s):
            out += [f"### {c['id']} ({c['level']}) enforced by `{c['enforced_by']}`", "", c["rule"], "",
                    f"- PASS: {c['pass']}", f"- FAIL: {c['fail']}", f"- Audit: {', '.join(map(str, c['audit']))}"]
            if c.get("params"):
                out.append(f"- Params: `{yaml.safe_dump(c['params'], default_flow_style=True, allow_unicode=True, width=10**6).strip()}`")
            out.append("")
    return "\n".join(out).rstrip("\n") + "\n"


def render_vi(data):
    n, rows = _stage_rows(data)
    total = [sum(v[i] for v in n.values()) for i in range(3)]
    out = [GENERATED, "", f"# Quy chuẩn phân tích v{data['version']} ({STATUS_VI.get(data['status'], data['status'])}) - bản tiếng Việt", "",
           "Đây là bản tóm tắt cho nhà nghiên cứu duyệt. Mỗi điều khoản là một luật mà mọi AI và mọi chương trình trong dự án "
           "phải theo. **Chặn** = vi phạm thì bị từ chối; **Cảnh báo** = chỉ báo cáo. **Đã có code kiểm** = đã có hàm thực hiện luật này, có thể mới kiểm một phần; "
           "**chưa có code kiểm** = luật đã ghi nhưng chưa có hàm kiểm tự động. Giai đoạn G3 sẽ đo đầy đủ từng điều khoản. Điều kiện đạt/không đạt đo được nằm trong bản tiếng Anh "
           "(`docs/STANDARD.md`).", "", "| Bước | Tên | Số điều khoản | Chặn | Đã có code kiểm |", "|---|---|---|---|---|"]
    out += [f"| {s} | {name['vi']} | {a} | {b} | {c} |" for s, name, a, b, c in rows]
    out += [f"| **Tổng** | | **{total[0]}** | **{total[1]}** | **{total[2]}** |", ""]
    for s, name in data["stages"].items():
        out += [f"## {s}: {name['vi']}", ""]
        for c in (c for c in data["clauses"] if c["stage"] == s):
            level = "Chặn" if c["level"] == "fail" else "Cảnh báo"
            code = "chưa có code kiểm" if c["enforced_by"] == NOT_YET else "đã có code kiểm"
            out.append(f"- **{c['id']}** ({level}, {code}): {c['vi']}")
        out.append("")
    out += (["## Các quyết định đã chốt (Open decisions)", "", "Bạn đã duyệt các giá trị đề xuất dưới đây.", ""]
            if data["status"] == "frozen" else
            ["## Các quyết định cần bạn xác nhận (Open decisions)", "",
             "Mỗi mục có giá trị đề xuất. Nếu đồng ý tất cả, bạn chỉ cần trả lời 'đồng ý các giá trị đề xuất'.", ""])
    for od in data.get("open_decisions") or []:
        refs = ", ".join(od["clauses"]) or "không gắn điều khoản"
        out += [f"{od['id']}. {od['question_vi']}", f"   - Đề xuất: {od['recommended_vi']}", f"   - Liên quan: {refs}", ""]
    return "\n".join(out).rstrip("\n") + "\n"


def out_of_sync(data):
    """Docs that differ from what the yaml renders (empty list = in sync)."""
    return [p.name for p, text in ((DOC_EN, render_en(data)), (DOC_VI, render_vi(data)))
            if not p.is_file() or p.read_bytes().decode("utf-8") != text]


def write_docs(data):
    for path, text in ((DOC_EN, render_en(data)), (DOC_VI, render_vi(data))):
        path.write_bytes(text.encode("utf-8"))  # LF, never CRLF
