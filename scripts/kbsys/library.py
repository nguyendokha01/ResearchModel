"""Library management for the local web page: vaults, uploads, and one background job at a time.

Jobs: ingest (inbox → papers, Tier 1), analyze (Tier 2 with the configured AI), connect (concept links,
ghosts, intervention enrichment, relations, Concept Notes). Everything runs in one worker thread with its
own database connection, so the page stays responsive and memory stays low.
"""

import json
import re
import threading
import time
import traceback
import unicodedata

from . import answer, concepts, config, db, evidence, notes

NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}")
UPLOAD_TYPES = {".pdf", ".zip", ".xlsx", ".xlsm", ".csv"}
_JOB = {"running": False, "action": None, "vault": None, "log": [], "result": None, "started": None, "ended": None}
_LOCK = threading.Lock()


# ---------------------------------------------------------------- vaults

def list_vaults():
    return config.research_vaults()


def folder_name(text):
    """A vault folder name from what the researcher typed: "Tế bào Muse" -> "Te_bao_Muse"."""
    text = unicodedata.normalize("NFKD", (text or "").strip().replace("đ", "d").replace("Đ", "D"))
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"_+", "_", re.sub(r"[^A-Za-z0-9_-]+", "_", text)).strip("_-")[:80]


def create_vault(con, name, topic=""):
    typed = (name or "").strip()
    name = folder_name(typed)
    if not NAME.fullmatch(name):
        raise ValueError("Hãy gõ tên vault (chữ, số, _ hoặc -; dấu tiếng Việt sẽ được bỏ trong tên thư mục)")
    folder = config.vault_dir(name)
    if folder.exists():
        raise ValueError(f"Vault {name} đã có")
    (folder / ".obsidian").mkdir(parents=True)
    title = typed if typed != name else name.replace("_", " ").replace("-", " ")
    (folder / "Home.md").write_text(
        f"# {title}\n\n{(topic or '').strip()}\n\nVault riêng cho chủ đề nghiên cứu này. Bài báo và PDF được lưu "
        "trong kho dùng chung; bài liên kết vào vault sẽ xuất hiện trong mục Papers.\n", encoding="utf-8", newline="\n")
    notes.write_indexes(con, name)
    select_vault(name)
    return {"vault": name, "folder": str(folder)}


def select_vault(name):
    if name not in list_vaults():
        raise ValueError(f"Không có vault {name}")
    path = config.STORE / "ui_preferences.json"
    prefs = db.read_json(path, {}) if path.is_file() else {}
    prefs["selected_vault"] = name
    config.STORE.mkdir(parents=True, exist_ok=True)
    db.write_json(path, prefs)
    return {"selected_vault": name}


# ---------------------------------------------------------------- uploads

def inbox_dir(vault, batch=None):
    batch = batch if batch and NAME.fullmatch(batch) else f"{vault}_{time.strftime('%Y%m%d')}"
    return config.ROOT / "sources" / "inbox" / "batches" / batch


def save_upload(vault, filename, data, batch=None):
    if vault not in list_vaults():      # the vault name becomes part of a folder path
        raise ValueError("Hãy chọn hoặc tạo vault trước")
    # keep letters of any language (Vietnamese file names stay readable); drop path and Windows-reserved characters
    name = re.sub(r"[^\w.() -]+", "_", unicodedata.normalize("NFC", (filename or "").strip()))[-150:].strip(" .")
    suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if not name or suffix not in UPLOAD_TYPES:
        raise ValueError("Chỉ nhận PDF, ZIP, XLSX/XLSM hoặc CSV")
    if not data:
        raise ValueError(f"Tệp {name} rỗng (0 byte)")
    folder = inbox_dir(vault, batch)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    i = 1
    while target.exists():
        target = folder / f"{target.stem}_{i}{target.suffix}"
        i += 1
    target.write_bytes(data)
    return {"saved": config.rel(target), "bytes": len(data)}


def inbox_files():
    root = config.ROOT / "sources" / "inbox"
    if not root.is_dir():
        return []
    return [config.rel(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.suffix.lower() in UPLOAD_TYPES and "discovered" not in p.parts
            and "refmanager" not in p.parts]


# ---------------------------------------------------------------- overview

def overview(con, vault):
    from . import structure, review_policy
    papers = []
    relevance_context = review_policy.context(con, vault)
    for r in con.execute("SELECT p.paper_id, p.title, p.year, p.first_author, p.analysis_status FROM papers p "
                         "JOIN paper_vaults v ON v.paper_id = p.paper_id WHERE v.vault = ? ORDER BY p.year, "
                         "p.first_author", (vault,)):
        units = con.execute("SELECT COUNT(*), SUM(status = 'flagged') FROM evidence WHERE paper_id = ?",
                            (r["paper_id"],)).fetchone()
        meta = db.read_json(db.paper_dir(r["paper_id"]) / "metadata.json", {}) or {}
        state = db.read_json(db.paper_dir(r["paper_id"]) / "analysis_state.json", {}) or {}
        check = db.read_json(db.paper_dir(r["paper_id"]) / "analysis_quality.json", {}) or {}
        other_vaults = [v[0] for v in con.execute('SELECT vault FROM paper_vaults WHERE paper_id=? AND vault<>? ORDER BY vault', (r['paper_id'], vault))]
        papers.append({**dict(r), "other_vaults": other_vaults, "structured_profiles": structure.completion(r['paper_id']), "analysis_status": state.get('status') or r['analysis_status'],
                       "quality_blockers": len(check.get('blockers', [])),
                       "uncertain_tables": sum(w.get('code') == 'table_layout_uncertain' for w in check.get('warnings', [])),
                       "quality_checked": bool(check), "units": units[0] or 0, "flagged": int(not review_policy.relevance(con, r["paper_id"], vault, relevance_context)["connected"]),
                       "evidence_quality_flags": units[1] or 0,
                       "relevance": review_policy.relevance(con, r["paper_id"], vault, relevance_context),
                       "duplicate_claims": evidence.duplicate_claim_count(con, r["paper_id"]),
                       "text_quality": meta.get("text_quality"), "ocr_pages": len(meta.get("ocr_pages") or []),
                       "note_name": meta.get("note_name"),
                       "processing_details": [dict(item) for item in con.execute(
                           "SELECT * FROM review_items WHERE paper_id=? AND kind<>? ORDER BY item_id",
                           (r['paper_id'], review_policy.KIND))]})
    pending_review = len(review_policy.pending(con, vault))
    ai = {t: answer.provider_status(answer.settings(t)) for t in ("analysis", "synthesis", "answer")}
    return {"vault": vault, "vaults": list_vaults(), "selected": config.default_vault(config.load()),
            "papers": papers, "inbox": inbox_files(), "review_pending": pending_review, "ai": ai,
            "processing_files": [dict(item) for item in con.execute(
                "SELECT * FROM review_items WHERE paper_id IS NULL AND kind<>? ORDER BY item_id", (review_policy.KIND,))],
            "job": job_status(), "mcp_hint": mcp_hint(vault)}


def mcp_hint(vault):
    return (f"Ứng dụng AI nối MCP (bấm '2 - Ket noi AI (MCP).bat' một lần), rồi nhắn: “Dùng biomedical-kb: phân tích "
            f"các bài đang chờ trong vault {vault} (get_analysis_guide → list_pending_papers → read_paper → add_units → "
            f"finish_analysis; get/submit_section_profile; get/submit_protocol_profile). Bài đã done evidence chỉ làm "
            f"hồ sơ còn thiếu, không phân tích lại. Sau đó làm các việc trong list_ai_tasks của vault {vault}.”")


# ---------------------------------------------------------------- jobs

def job_status():
    with _LOCK:
        return {k: (v[-300:] if k == "log" else v) for k, v in _JOB.items()}


def start_job(action, vault, options=None):
    from .pipeline import STEPS
    if action not in STEPS:
        raise ValueError("Việc không hợp lệ")
    if vault not in list_vaults():
        raise ValueError("Hãy chọn hoặc tạo vault trước")
    with _LOCK:
        if _JOB["running"]:
            raise ValueError(f"Đang chạy việc '{_JOB['action']}' — đợi xong đã")
        _JOB.update(running=True, action=action, vault=vault, log=[], result=None,
                    started=config.now(), ended=None)
    threading.Thread(target=_worker, args=(action, vault, options or {}), daemon=True).start()
    return job_status()


def _say(*parts, **_):
    line = " ".join(str(p) for p in parts)
    with _LOCK:
        _JOB["log"].append(line)
        del _JOB["log"][:-2000]


def _worker(action, vault, options):
    from . import pipeline, workflow
    con = None
    result = None
    try:
        con = db.connect()
        result = pipeline.run(con, config.load(), vault, action, say=_say, limit=options.get("limit"))
        states = [r.get('status') for r in result]
        _say("Đã lưu checkpoint: " + ', '.join(states))
        if 'waiting' in states:
            _say("Còn việc chờ AI/nguồn/duyệt. Xem checkpoint và MCP; chưa hoàn tất.")
    except Exception as e:
        _say(f"Lỗi: {type(e).__name__}: {e}")
        traceback.print_exc()
        result = {"error": str(e)}
    finally:
        if con is not None:
            con.close()
        with _LOCK:
            _JOB.update(running=False, result=result, ended=config.now())
            workflow.atomic_json(config.STORE / 'workflow/job_last.json', dict(_JOB))
