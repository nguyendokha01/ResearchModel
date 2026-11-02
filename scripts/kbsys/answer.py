"""AI answers checked by code (M3.3, docs/CONTRACTS.md §4.2–4.4).

1. The question becomes English search queries (the AI is asked only when the question is in
   Vietnamese, or always in "deep" mode).
2. find.search collects evidence; each item gets a number [#1], [#2], ...
3. The AI receives only these items and must return every sentence with item numbers.
4. Code checks every sentence (A1–A6). A sentence that fails is kept but struck, with the reason.

Views: "evidence4" (the four-part researcher format), "google" (legacy short summary),
"report5" (legacy five parts), and "table" (comparison table built by code from evidence units, no AI).
Speeds: "fast" (few items, one AI call for the answer) and "deep" (more queries, neighbouring
sentences added as extra items).

The API key lives in config/secrets.yaml (git-ignored). Nothing is sent anywhere unless a
provider is configured; only the question and the retrieved passages are sent, never whole PDFs.
"""

import html
import http.client
import json
import math
import os
import re
import signal
import shutil
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from . import config, find, hedges
from .db import paper_dir, read_jsonl
from .evidence import ATTRIBUTION, DEFINITIVE
from .notes import section_label
from . import sentences as sentence_utils
from .sentences import numbers

PROVIDERS = ("none", "deepseek", "gemini", "openai_compatible", "codex_cli",
             "anthropic", "ollama", "openrouter", "command", "mcp_agent")
DEFAULTS = {
    "provider": "none",
    "timeout_seconds": 300,
    "provider_timeouts": {
        "none": 300,
        "mcp_agent": 300,
        "deepseek": 300,
        "gemini": 300,
        "openai_compatible": 300,
        "anthropic": 300,
        "ollama": 300,
        "openrouter": 300,
        "codex_cli": 900,
        "command": 900,
    },
    "deepseek": {"base_url": "https://api.deepseek.com", "model": "deepseek-chat", "json_mode": True},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-flash",
               "json_mode": False},
    "openai_compatible": {"base_url": "http://127.0.0.1:20128/v1", "model": "", "json_mode": False},
    "codex_cli": {"command": "codex", "model": "gpt-5.6-luna", "reasoning": "low"},
    "anthropic": {"base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-5", "max_tokens": 4096},
    "ollama": {"base_url": "http://127.0.0.1:11434/v1", "model": "", "json_mode": True},
    "openrouter": {"base_url": "https://openrouter.ai/api/v1", "model": "", "json_mode": False},
    # Any command-line AI that reads the prompt on stdin and prints the reply, e.g. ["claude", "-p"] or ["gemini", "-p"].
    "command": {"run": [], "model": ""},
    "mcp_agent": {"model": ""},
    "tasks": {},
    "fast": {"max_items": 12, "queries": 1},
    "deep": {"max_items": 30, "queries": 3, "context_for_top": 8, "context_window": 2, "max_total_items": 45},
}
SECRETS = config.CONFIG_DIR / "secrets.yaml"
ANSWER_CACHE = config.CACHE_DIR / "answers"

# Vietnamese wording (the answers are written in Vietnamese); Vietnamese hedge words live in config/hedge_lexicon.yaml
VI_DEFINITIVE = re.compile(r"\b(gây ra|dẫn đến|dẫn tới|chứng minh|khẳng định|chắc chắn|là nguyên nhân|"
                           r"quyết định|xác nhận|do đó|khiến|gây nên|làm cho|chứng tỏ)\b", re.I)
# AN-04: a bare "theo" (theo thời gian) or the "dẫn" of "dẫn đến" attributes nothing; "theo" needs a study / author / year
# after it. "Author Year đề xuất / proposed ..." (the cited authors' own hypothesis) is attribution too.
_CAP = r"[A-Z\u0110\u0102\u01a0\u01af\u00c0-\u00d6\u00d8-\u00de]"   # an uppercase initial; Vietnamese Đ Ă Ơ Ư included
VI_ATTRIBUTION = re.compile(r"\b(theo (?:nghiên cứu|tác giả|báo cáo|(?:19|20)\d\d|(?-i:(?!Hình|Bảng|Figure|Table)" + _CAP + r"\w+))|trích|"
                            r"được báo cáo|báo cáo|cho rằng|nhắc lại|dẫn lại|dẫn(?!\s+(?:đến|tới))|mô tả|"
                            r"tổng quan|nghiên cứu trước|bài được trích|đã được chỉ ra)\b|"
                            r"(?-i:\b" + _CAP + r"\w+)(?: et al\.?| và cs\.?| và cộng sự)?,? \(?\d{4}\)?,? (?:đã )?"
                            r"(?:đề xuất|gợi ý|giả thuyết|đưa ra giả thuyết|proposed|suggested|hypothesi[sz]ed)\b", re.I)
# AN-11: an affirming result verb in a "cannot conclude" sentence. Reuses the definitive lexicons; a negator or modal in the
# 20 words before it (Vietnamese "Không thể kết luận rằng X ... gây ra" is long), inside its clause, is what a limits sentence says ("does not prove", "không chứng minh được").
_AFFIRM_EXTRA = re.compile(r"\bshow(?:s|ed)? that\b", re.I)
_NOT_AFFIRMING = re.compile(r"^(?:due to|because of|do đó)$", re.I)   # connectives that give the reason for a limit
_NEGATED = re.compile(r"\b(?:not|no|cannot|never|neither|nor|without|unable|may|might|could|"
                      r"possibly|whether|wrong|insufficient|lack\w*|fail\w*|nothing|rarely|không|chưa|chẳng|khó|có thể|liệu)\b|n't",
                      re.I)   # "can", "would", "should" hedge nothing ("we can prove")
_CLAUSE_END = re.compile(r"[;:,.()]|\b(?:and|but|however|therefore|though|although|whereas|while|so|và|nhưng|tuy nhiên|vì vậy|"
                         r"mặc dù|trong khi|song)\b", re.I)


def _affirms(text):
    for rx in (VI_DEFINITIVE, DEFINITIVE, _AFFIRM_EXTRA):
        for m in rx.finditer(text):
            before = " ".join(_CLAUSE_END.split(text[:m.start()])[-1].split()[-20:])
            if not _NOT_AFFIRMING.match(m.group(0)) and not _NEGATED.search(before):
                return True
    return False
VI_REVIEW = re.compile(r"\b(tổng quan|review|bài tổng hợp)\b", re.I)
VI_UP = re.compile(r"\b(tăng|cao hơn|nhiều hơn|cải thiện|thúc đẩy|kích thích)\b", re.I)
VI_DOWN = re.compile(r"\b(giảm|thấp hơn|ít hơn|ức chế|suy giảm|kém hơn)\b", re.I)
VI_NOCHANGE = re.compile(r"\b(không (?:có )?(?:khác biệt|thay đổi|ảnh hưởng|liên quan|tương quan)|tương đương)\b", re.I)
NON_SIGNIFICANCE = hedges.Finder({"non_significance"})   # EV-22: "không đáng kể", "không có ý nghĩa" ... live in config/hedge_lexicon.yaml only
# AN-12: an item is written [#n]; a plain [n] / [n-m] / [40-42] is a literature reference, never an item
_MARKERS = re.compile(r"\[#(\d{1,9})\]", re.ASCII)  # ASCII digits only: "٣" / fullwidth "１" are not item numbers
_LITERATURE = re.compile(r"\[\s*#?\d+(?:\s*[,–-]\s*#?\d+)*\s*\]")
_ITEM_REF = re.compile(r"\[#(\d{1,9})\]|(\d{1,9})", re.ASCII)


def _item_number(value):
    """An item number: an int or its digit string / "[#n]" form. bool, float (NaN, Infinity) and the rest -> None."""
    if type(value) is int:
        return value
    match = _ITEM_REF.fullmatch(value.strip()) if isinstance(value, str) else None
    return int(match.group(1) or match.group(2)) if match else None


CHECK_TEXT = {
    "A1": "không dẫn mục bằng chứng nào có trong danh sách",
    "A2": "có con số không có trong các mục được dẫn",
    "A3": "câu trả lời nói chắc chắn hoặc nhân quả hơn mức nguồn nói (nguồn chỉ ở mức có thể/liên quan)",
    "A4": "ý của bài khác (câu trích dẫn) bị viết như kết quả gốc, thiếu “theo …”",
    "A5": "chỉ dựa trên bài tổng quan (review) mà không ghi rõ",
    "A6": "chiều thay đổi (tăng/giảm) ngược với nguồn",
    "A7": "bỏ mất từ chỉ sự không chắc chắn hoặc mức độ yếu trong nguồn",
    "A8": "tham chiếu ghi chú không có trong danh sách được máy chủ cho phép",
}
CHECK_TEXT["A9"] = "câu Phần 2 mở đầu bằng verdict trần; nêu điều các mục được dẫn báo cáo"
CHECK_TEXT["A10"] = "câu nêu mốc/định danh hoặc điều kiện (vd. thủy tinh hóa) mà không mục nào được dẫn có"
CHECK_TEXT["A11"] = ("câu không có đúng một loại cơ sở: Phần 3 cần mục [#n] hoặc note của máy chủ, hoặc chỉ riêng "
                     "'ai_knowledge'; kiến thức của AI không được dùng ngoài Phần 3")
CHECK_TEXT["A12"] = "câu trích mục chưa được đọc toàn văn (chỉ thấy trong mục lục)"
STRIKE = {"A1", "A2", "A3", "A4", "A6", "A7", "A8", "A10", "A11"}   # A5, A9 and A12 are warnings
AI_KNOWLEDGE = "ai_knowledge"   # AN-15: the one basis value of a Part 3 sentence that rests on model knowledge
REASONING_NOTE = "Suy luận của AI — không phải nội dung tài liệu."   # AN-15: the Part 3 line of both renderers
AI_KNOWLEDGE_LABEL = "kiến thức của AI (không phải bằng chứng trong thư viện)"   # one label: renderer, MCP INSTRUCTIONS, protocol


# ---------------------------------------------------------------- settings

def settings(task=None):
    """config/ai.yaml merged over the defaults; the key comes from config/secrets.yaml.

    `task` (answer | synthesis | enrichment | crosscheck) applies `ai.tasks.<task>`, which is either a provider
    name or {provider: ..., model: ...}; unset tasks use the main `ai.provider`."""
    path = config.CONFIG_DIR / "ai.yaml"
    s = json.loads(json.dumps(DEFAULTS))
    user, error = {}, None
    if path.is_file():
        try:  # the researcher edits this file by hand: a typo must give a message, not a crash
            user = (yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}).get("ai") or {}
        except (yaml.YAMLError, AttributeError, UnicodeDecodeError) as e:
            error = f"config/ai.yaml viết sai (sửa lại rồi mở lại trang): {str(e).splitlines()[0][:160]}"
        if not isinstance(user, dict):
            user, error = {}, "config/ai.yaml: mục ai: phải là một nhóm (provider: ..., model: ...)"
    for k, v in user.items():
        if v is None:                     # an emptied block ("ollama:" with nothing under it) keeps the defaults
            continue
        if isinstance(v, dict) and isinstance(s.get(k), dict):
            s[k].update({kk: vv for kk, vv in v.items() if vv is not None})
        elif not isinstance(s.get(k), dict):   # a wrong type for a block (e.g. "deepseek: abc") is ignored
            s[k] = v
    route = s["tasks"].get(task) if task else None
    if not route and task in ("figure_reading", "node_card"):  # own route absent: use the generic analysis route
        route = s["tasks"].get("analysis")
    if isinstance(route, str) and route:
        s["provider"] = route
    elif isinstance(route, dict) and route.get("provider"):
        s["provider"] = route["provider"]
        if route.get("model") and isinstance(s.get(route["provider"]), dict):
            s[route["provider"]] = {**s[route["provider"]], "model": route["model"]}
    if s["provider"] not in PROVIDERS:
        error = error or (f"config/ai.yaml: không có AI tên “{s['provider']}” (ai.provider hoặc ai.tasks.{task}); "
                          f"chọn một trong: {', '.join(PROVIDERS)}")
        s["provider"] = "none"
    try:
        if not math.isfinite(float(s["timeout_seconds"])) or float(s["timeout_seconds"]) <= 0:
            raise ValueError
    except (TypeError, ValueError):
        s["timeout_seconds"] = DEFAULTS["timeout_seconds"]
    s["timeout_seconds"] = timeout_for(s)
    s["task"] = task
    s["config_error"] = error
    return s


def timeout_for(s, provider=None):
    """Return the positive timeout selected for a provider."""
    selected = provider or s.get("provider")
    values = s.get("provider_timeouts")
    value = values.get(selected) if isinstance(values, dict) else None
    if value is None:
        value = s.get("timeout_seconds", DEFAULTS["timeout_seconds"])
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = float(DEFAULTS["timeout_seconds"])
    if not math.isfinite(value) or value <= 0:
        value = float(DEFAULTS["timeout_seconds"])
    return value


def model_of(s):
    """Model name recorded next to AI output."""
    p = s["provider"]
    return str((s.get(p) or {}).get("model") or "") if isinstance(s.get(p), dict) else ""


def _secret(name):
    if not SECRETS.is_file():
        return ""
    try:
        data = yaml.safe_load(SECRETS.read_text(encoding="utf-8-sig"))
    except (yaml.YAMLError, UnicodeDecodeError):   # reported as a missing key, never as a crash
        return ""
    return str(data.get(name) or "").strip() if isinstance(data, dict) else ""


def provider_status(s=None):
    """What the web page shows next to the question box."""
    s = s or settings("answer")
    p = s["provider"]
    if s.get("config_error"):
        return {"ready": False, "provider": p, "message": s["config_error"]}
    if p == "none":
        return {"ready": False, "provider": p, "message": "Chưa chọn AI (config/ai.yaml). Vẫn tìm kiếm và xem bảng so sánh được."}
    if p == "mcp_agent":
        return {"ready": False, "provider": p, "mcp": True,
                "message": "AI chạy qua MCP: mở ứng dụng AI đã nối MCP (Claude Desktop, Claude Code, Codex, "
                           "Gemini CLI, Cursor…) và nhờ nó làm việc; xem HUONG_DAN.md, mục “Chọn AI”."}
    if p in ("deepseek", "gemini", "anthropic", "openrouter") and not _secret(f"{p}_api_key"):
        return {"ready": False, "provider": p,
                "message": f"Thiếu key hoặc đọc không được: thêm dòng {p}_api_key: ... vào config/secrets.yaml"}
    if p in ("openai_compatible", "ollama", "openrouter") and not s[p].get("model"):
        return {"ready": False, "provider": p, "message": f"Thiếu tên model trong config/ai.yaml ({p}.model)"}
    if p == "command":
        run = s[p].get("run") or []
        if not run or not shutil.which(str(run[0])):
            return {"ready": False, "provider": p,
                    "message": "Chưa ghi lệnh AI (command.run) trong config/ai.yaml, hoặc không tìm thấy lệnh đó"}
    if p == "codex_cli":
        if not shutil.which(s[p].get("command") or "codex"):
            return {"ready": False, "provider": p, "message": "Không tìm thấy lệnh codex trên máy này"}
        if not (_codex_home() / "auth.json").is_file():
            return {"ready": False, "provider": p, "message": "Codex chưa đăng nhập: mở Codex và đăng nhập (hoặc chạy codex login)"}
        return {"ready": True, "provider": p, "model": s[p].get("model", ""),
                "message": f"AI: Codex (tài khoản đang đăng nhập) · {s[p].get('model') or 'model mặc định'}"}
    model = s[p].get("model", "")
    return {"ready": True, "provider": p, "model": model, "message": f"AI: {p} · {model}"}


# ---------------------------------------------------------------- calling the model

class AIError(Exception):
    pass


class ProviderTimeout(AIError):
    """A provider reached its wall-clock limit and was stopped."""

    status = "provider_timeout"

    def __init__(self, provider, timeout):
        self.provider = str(provider)
        self.timeout = float(timeout)
        super().__init__(f"{self.provider} exceeded its provider timeout ({self.timeout:g}s)")


HTTP_HINT = {400: "yêu cầu bị từ chối (kiểm tra tên model trong config/ai.yaml)",
             401: "key sai hoặc đã hết hạn (kiểm tra config/secrets.yaml)",
             403: "key không có quyền dùng model này",
             404: "không có model hoặc địa chỉ này (kiểm tra model/base_url trong config/ai.yaml)",
             429: "hết lượt hoặc gọi quá nhanh; chờ một lúc hoặc nạp thêm hạn mức",
             500: "máy chủ AI đang lỗi; thử lại sau", 502: "máy chủ AI đang lỗi; thử lại sau",
             503: "máy chủ AI đang quá tải; thử lại sau"}


def _post_json(p, req, timeout):
    """Send one HTTP request to an AI provider; every failure becomes an AIError in plain Vietnamese."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIError(f"{p} trả lỗi {e.code}: {HTTP_HINT.get(e.code, 'xem chi tiết')}. Chi tiết: {detail}") from None
    except urllib.error.URLError as e:
        if isinstance(e.reason, TimeoutError) or "timed out" in str(e.reason).lower():
            raise ProviderTimeout(p, timeout) from None
        raise AIError(f"Không gọi được {p} (kiểm tra mạng / base_url, hoặc tăng timeout_seconds): {e}") from None
    except TimeoutError:
        raise ProviderTimeout(p, timeout) from None
    except (OSError, http.client.HTTPException) as e:
        raise AIError(f"Không gọi được {p} (kiểm tra mạng / base_url, hoặc tăng timeout_seconds): {e}") from None
    except ValueError:
        raise AIError(f"{p} trả về nội dung không phải JSON (base_url có đúng không?)") from None
    if not isinstance(data, dict):
        raise AIError(f"{p} trả kết quả lạ: {str(data)[:300]}")
    return data


def _kill_process_tree(proc):
    """Stop a CLI and all descendants after a provider timeout."""
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        proc.wait(timeout=5)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _run_cli(cmd, prompt, timeout, cwd=None, provider="command"):
    """Run a CLI provider with a wall-clock timeout and tree cleanup."""
    kwargs = {
        "stdin": subprocess.PIPE, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
        "text": True, "encoding": "utf-8", "errors": "replace", "cwd": cwd,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(cmd, **kwargs)
    try:
        stdout, stderr = proc.communicate(input=prompt, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_process_tree(proc)
        try:
            proc.communicate(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            pass
        raise ProviderTimeout(provider, timeout) from None
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


def _chat(s, system, user):
    """One request; returns (text, usage dict)."""
    p = s["provider"]
    if p == "codex_cli":
        return _codex(s, system, user)
    if p == "anthropic":
        return _anthropic(s, system, user)
    if p == "command":
        return _command(s, system, user)
    if p in ("none", "mcp_agent"):
        raise AIError("Không có AI tự chạy (provider: " + p + "). "
                      + ("Dùng một ứng dụng AI nối qua MCP." if p == "mcp_agent" else "Chọn AI trong config/ai.yaml."))
    conf = s[p]
    body = {"model": conf["model"], "temperature": 0.1,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    if conf.get("json_mode"):
        body["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    key = _secret(f"{p}_api_key")
    if key:
        headers["Authorization"] = f"Bearer {key}"
    url = conf["base_url"].rstrip("/") + "/chat/completions"
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    data = _post_json(p, req, s["timeout_seconds"])
    try:
        text = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError(f"{p} trả kết quả lạ: {str(data)[:300]}") from None
    return text, data.get("usage") or {}


def _anthropic(s, system, user):
    """Claude via the Anthropic Messages API (key: anthropic_api_key in config/secrets.yaml)."""
    conf = s["anthropic"]
    key = _secret("anthropic_api_key")
    body = {"model": conf["model"], "max_tokens": int(conf.get("max_tokens") or 4096), "temperature": 0.1,
            "system": system, "messages": [{"role": "user", "content": user}]}
    headers = {"Content-Type": "application/json", "x-api-key": key, "anthropic-version": "2023-06-01"}
    req = urllib.request.Request(conf["base_url"].rstrip("/") + "/messages", data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    data = _post_json("anthropic", req, s["timeout_seconds"])
    text = "".join(block.get("text", "") for block in data.get("content") or []
                   if isinstance(block, dict) and block.get("type") == "text")
    if not text:
        raise AIError(f"anthropic trả kết quả lạ: {str(data)[:300]}")
    return text, data.get("usage") or {}


def _command(s, system, user):
    """Any command-line AI: the prompt goes to stdin, the reply is read from stdout.

    Examples for `command.run` in config/ai.yaml: ["claude", "-p"], ["gemini", "-p"], ["ollama", "run", "qwen3"]."""
    run = [str(x) for x in s["command"].get("run") or []]
    exe = shutil.which(run[0]) if run else None
    if not exe:
        raise AIError("Không tìm thấy lệnh AI trong command.run (config/ai.yaml)")
    prompt = system + "\n\n" + user + "\n\nDo not run any command or tool. Reply with the JSON only."
    with tempfile.TemporaryDirectory(prefix="kb_ai_") as tmp:  # an empty working folder: no project files exposed
        proc = _run_cli([exe, *run[1:]], prompt, s["timeout_seconds"], cwd=tmp, provider="command")
    if proc.returncode or not (proc.stdout or "").strip():
        detail = ((proc.stderr or "") + (proc.stdout or "")).strip()[-300:] or "không in ra gì"
        raise AIError(f"Lệnh AI không trả lời (mã {proc.returncode}): {detail}")
    return proc.stdout, {"model": s["command"].get("model") or run[0]}


CODEX_NOT_LOGGED_IN = re.compile(r"not logged in|log ?in|401|unauthori[sz]ed|auth(?:entication)? (?:failed|required)|"
                                 r"token (?:expired|invalid)|refresh token", re.I)
CODEX_NO_QUOTA = re.compile(r"usage limit|rate limit|429|quota|too many requests|limit reached|credits", re.I)
CODEX_BAD_MODEL = re.compile(r"model.{0,80}(?:not supported|not found|does not exist|not available|no access|unknown)|"
                             r"(?:unsupported|unknown|invalid) model", re.I)


def _codex_home():
    import os
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def _codex(s, system, user):
    """Codex CLI with whatever ChatGPT account is logged in right now (no API key); no tools, read-only, no repo.

    Codex reads the login from CODEX_HOME at every call. A unavailable configured model
    returns an error instead of silently spending quota on a different model."""
    conf = s["codex_cli"]
    exe = shutil.which(conf.get("command") or "codex")
    if not exe:
        raise AIError("Không tìm thấy lệnh codex. Cài Codex CLI (npm i -g @openai/codex) hoặc đổi provider trong config/ai.yaml.")
    prompt = system + "\n\n" + user + "\n\nDo not run any command or tool. Reply with the JSON only."
    work = config.CACHE_DIR / "codex_answer"
    work.mkdir(parents=True, exist_ok=True)
    attempts = [conf.get("model") or None]
    last = ""
    for model in attempts:
        with tempfile.TemporaryDirectory(dir=work) as tmp:
            out = Path(tmp) / "last.txt"
            cmd = [exe, "exec", "--json", "--skip-git-repo-check", "--ephemeral", "--ignore-user-config",
                   "-s", "read-only", "-C", tmp, "-o", str(out)]
            if model:
                cmd += ["-m", model]
                if conf.get("reasoning"):
                    cmd += ["-c", f'model_reasoning_effort="{conf["reasoning"]}"']
            cmd.append("-")
            try:
                proc = _run_cli(cmd, prompt, s["timeout_seconds"], provider="codex_cli")
            except FileNotFoundError:
                raise AIError("Không tìm thấy lệnh codex") from None
            if out.is_file() and out.read_text(encoding="utf-8").strip() and proc.returncode == 0:
                usage = {"model": model or "mặc định của tài khoản"}
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
            output = (proc.stderr or "") + "\n" + (proc.stdout or "")
            last = "\n".join(line for line in output.splitlines() if "error" in line.lower())[-600:] or output[-600:]
        if model and CODEX_BAD_MODEL.search(last):
            break               # configured model only; no hidden quota-consuming fallback
        break
    if CODEX_NO_QUOTA.search(last):
        raise AIError("Tài khoản Codex đang đăng nhập đã hết lượt dùng. Đổi sang tài khoản khác trong Codex "
                      "(hoặc chờ hồi lượt), rồi bấm tìm lại — không cần tắt trang.")
    if CODEX_NOT_LOGGED_IN.search(last):
        raise AIError("Codex chưa đăng nhập hoặc phiên đăng nhập đã hết hạn. Mở Codex và đăng nhập lại "
                      "(hoặc chạy: codex login), rồi bấm tìm lại.")
    raise AIError(f"Codex không trả lời: {last[-300:]}")


def _json(text):
    """The first JSON object in a model reply (models sometimes wrap it in ``` or prose)."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        data = json.loads(text)
        if isinstance(data, (dict, list)):   # a bare number or string is not an answer
            return data
    except ValueError:
        pass
    start = text.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    raise AIError("AI không trả về JSON đọc được")


# ---------------------------------------------------------------- 1. queries

QUERY_SYSTEM = """You turn a biomedical researcher's question into English keyword queries for a full-text
search engine (SQLite FTS5 with stemming) over the researcher's own library of papers.
Rules: English only; use standard biomedical terms (MeSH-style) and common synonyms/abbreviations;
2-6 words per query; no boolean operators; put an exact multi-word term in double quotes only when
needed. Reply with JSON: {"queries": ["...", "..."], "english_question": "..."}"""


def make_queries(s, question, n):
    text, usage = _chat(s, QUERY_SYSTEM, f"Question: {question}\nGive at most {n} queries.")
    data = _json(text)
    if isinstance(data, list):              # a bare list of queries
        data = {"queries": data}
    queries = [str(q).strip() for q in _pick(data, "queries") or [] if isinstance(q, str) and q.strip()][:n]
    if not queries:
        raise AIError("AI không trả về từ khóa tìm kiếm nào")
    return queries, str(data.get("english_question") or "").strip(), usage


# ---------------------------------------------------------------- 2. evidence items

MAX_SENTENCE_ITEMS_PER_UNIT = 3

def _unit_details(paper_id, unit_ids):
    """model/intervention/outcome of these units, read from the paper's evidence.jsonl."""
    wanted = set(unit_ids)
    return {u["unit_id"]: u for u in read_jsonl(paper_dir(paper_id) / "evidence.jsonl") if u["unit_id"] in wanted}


def collect(con, cfg, queries, vault, all_vaults, filters, max_items):
    """Evidence items of all queries.  Every query keeps its own top items (a fair share of the
    slots, RT-07); the rest of the slots go to the best scores."""
    merged, reserved, runs = {}, [], []
    share = max(1, max_items // max(len(queries), 1))
    for q in queries:
        r = find.search(con, cfg, q, vault=vault, all_vaults=all_vaults, filters=filters, limit=max_items)
        runs.append({"query": q, "mode": r["mode"], "total": r["total"], "groups": len(r["groups"]),
                     "best_groups_matched": r.get("best_groups_matched", 0), "not_found": r.get("not_found", False),
                     "not_found_message": r.get("not_found_message")})   # RT-05: the written answer must say it
        own = sorted(source_items(con, r["hits"], r["groups"]), key=lambda h: -h["score"])
        reserved += [h["id"] for h in own[:share]]
        for h in own:
            if h["id"] not in merged or merged[h["id"]]["score"] < h["score"]:
                merged[h["id"]] = h
    ordered = sorted(merged.values(), key=lambda h: -h["score"])
    keep = set(reserved[:max_items])
    for h in ordered:
        if len(keep) >= max_items:
            break
        keep.add(h["id"])
    return [h for h in ordered if h["id"] in keep][:max_items], runs


def source_items(con, hits, groups=None):
    """Unaccepted generated units contribute at most three source sentences: those that match the
    query `groups` (most groups first, RT-07), the first three when `groups` is not given or none matches.

    The selected sentence is still rendered from the source row.  If it is the
    first part of a stored or read-time continuation chain, ``_whole_quote``
    joins the chain so bounding item count does not turn a source fragment into
    a standalone claim.
    """
    out, seen = [], set()
    for hit in hits:
        if hit.get('kind')=='unit' and hit.get('status') not in ('ok','human_confirmed'):
            sids=list(dict.fromkeys(hit.get('sentence_ids') or []))
            rows=[con.execute('SELECT s.*,pa.evidence_type AS paper_evidence_type,pa.publication_types FROM sentences s JOIN papers pa ON pa.paper_id=s.paper_id WHERE s.sentence_id=?',(sid,)).fetchone() for sid in (sids if groups else sids[:MAX_SENTENCE_ITEMS_PER_UNIT])]
            rows=[dict(r) for r in rows if r]
            if groups:
                patterns=find._group_patterns(groups)
                count=lambda r:sum(1 for p in patterns if p.search(sentence_utils.fold_search_glyphs(r['text'] or '')))
                best=[r['sentence_id'] for r in sorted((r for r in rows if count(r)),key=lambda r:-count(r))[:MAX_SENTENCE_ITEMS_PER_UNIT]]
                rows=[r for r in rows if r['sentence_id'] in best] or rows[:MAX_SENTENCE_ITEMS_PER_UNIT]  # unit order is kept
            replacements=[]
            for row in rows:
                item=find._sentence_hit(row)
                item['score']=hit.get('score',0)
                replacements.append(item)
        else:
            replacements=[hit]
        for item in replacements:
            # Keep provenance when a flagged generated unit is replaced by its original sentence.
            if hit.get("analysis_origin"):
                item["analysis_origin"] = hit["analysis_origin"]
            if item['id'] not in seen:
                out.append(item)
                seen.add(item['id'])
    find._attach_citations(con,out)
    return out


def add_context(con, hits, top, window, max_total):
    """Deep mode: the sentences around the best hits become extra (citable) items."""
    have = {sid for h in hits for sid in h["sentence_ids"]}
    extra = []
    for h in hits[:top]:
        for sid in h["sentence_ids"][:1] + h["sentence_ids"][-1:]:
            for c in find.context(con, sid, window):
                if c["sentence_id"] in have or len(hits) + len(extra) >= max_total:
                    continue
                have.add(c["sentence_id"])
                extra.append({"kind": "sentence", "id": c["sentence_id"], "paper_id": h["paper_id"],
                              "unit_type": None, "label": "Câu lân cận (nguyên văn)", "statement": None,
                              "text": c["text"], "sentence_ids": [c["sentence_id"]], "pages": [c["page"]],
                              "sections": [c["section"]], "sentence_kind": c["kind"],
                              "review_paper": h["review_paper"], "score": 0, "context_of": h["id"]})
    find._attach_citations(con, extra)
    return hits + extra


def _item_block(n, h, card):
    where = f"{section_label((h['sections'] or [''])[0])}, p. {', '.join(str(p) for p in h['pages'])}"
    kind = h["unit_type"] or h.get("sentence_kind") or "sentence"
    lines = [f"[#{n}] {card.get('cite', h['paper_id'])} · {kind} · {where}"
             + (" · REVIEW ARTICLE" if h["review_paper"] else "")
             + (" · hedged" if h.get("hedging") == "hedged" else "")]
    if h.get("analysis_origin") == "heuristic_draft":
        lines.append("Unit type assigned by rule script (heuristic_draft): rely on the quoted sentence, not on the type label.")
    lines.append(f"Verbatim: {h['text']}")
    from . import figure_reading  # late: figure_reading imports answer
    flags = figure_reading.sentence_warnings(h["paper_id"])
    lines += [f"WARNING: {flags[sid]}" for sid in dict.fromkeys(h.get("sentence_ids") or []) if sid in flags]
    if h.get("cited_refs"):
        refs = "; ".join(f"[{r['num']}] {r['first_author'] or '?'} {r['year'] or ''}" for r in h["cited_refs"])
        lines.append(f"This passage cites other papers: {refs}")
    return "\n".join(lines)


# ---------------------------------------------------------------- 3. the answer

ANSWER_RULES = """You answer a biomedical researcher's question using ONLY the numbered evidence items below,
taken from the researcher's own library. You never use memory or outside knowledge, except in a Part 3 """ + (
    f'sentence whose basis is ["{AI_KNOWLEDGE}"] alone.') + """
Write in Vietnamese; keep biomedical terms in English in parentheses the first time, e.g. "tế bào Muse (Muse cells)".
For the default four-part format, return JSON with exactly these content fields:
{"answer": [{"text": "...", "evidence": [n]}],
 "reasoning": [{"text": "...", "basis": [n or a server-provided note path]}],
 "missing": ["..."]}.
Part 1 (Bằng chứng từ tài liệu) is written by code from the verbatim item text; never reproduce or paraphrase
that part yourself. Part 2 (Trả lời dựa trên bằng chứng) may only translate or restate the cited items; every
sentence must cite item numbers like [#2] or [#2][#5] (a plain [12] is a literature reference, not an item).
Part 3 ([REASONING] Phần suy luận của AI) is synthesis,
framing, or inference from source items and, on the neural route, the supplied vault note paths/wikilinks;
it is not document content and must always be labelled as reasoning. Cite its numeric sources in `basis` and
use only note references supplied by the server. Part 4 (Không tìm thấy / không chắc chắn) lists what is
missing and what remains uncertain. Do not put quotes in your response; code copies them from the store.
Numbers (n, %, p, doses, times) must be copied exactly as printed in the cited items (keep "." decimals).
Never add a number that is not in the cited items.
Keep the strength of the source: if an item says may / suggest / associated / hypothesis, write "có thể",
"gợi ý", "có liên quan", "giả thuyết" — never "gây ra", "chứng minh", "dẫn đến".
Association is not causation. An author's hypothesis or interpretation is not a demonstrated finding.
Items of type cited_statement or background report OTHER papers: write them as attribution, e.g.
"Theo tài liệu được Smith 2016 trích dẫn [12], ..." — never as a result of this library.
If an item is from a REVIEW ARTICLE say so ("bài tổng quan ...").
If the items do not answer (part of) the question, say plainly what was not found in the current library.
Never open a Part 2 sentence with a bare verdict such as "Có.", "Không.", "Yes.", or "No.";
state what the cited items report instead. If cited items disagree, Part 2 must present both
findings with their own item citations and must not collapse them into one verdict.
Reply with JSON only."""

VIEW_FORMAT = {
    "evidence4": """{"answer": [{"text": "... [#n]", "evidence": [n]}],
 "reasoning": [{"text": "...", "basis": [n, "server-note.md"]}],   // every basis non-empty
 "missing": ["what the question asks that the items do not cover"]}""",
    "google": """{"answer": [{"text": "one sentence [#n]", "evidence": [n]}],   // 3-6 sentences
 "missing": ["what the question asks that the items do not cover"]}""",
    "report5": """{"evidence": [{"text": "...", "evidence": [n]}],          // Bằng chứng: what the items show, most direct first
 "can_conclude": [{"text": "...", "evidence": [n]}],      // Kết luận được: only what the items support
 "cannot_conclude": [{"text": "...", "evidence": [n]}],   // Không kết luận được: tempting claims the items do NOT support, and why
 "missing": ["..."]}                                      // Còn thiếu: evidence one would need; not found in the current library""",
}


def build_prompt(question, english, items, cards, view, numbers=None):
    """`numbers`: the item numbers to show (a research session's session-wide numbers, NW-11); None numbers from 1."""
    numbers = range(1, len(items) + 1) if numbers is None else numbers
    blocks = "\n\n".join(_item_block(n, h, cards.get(h["paper_id"], {})) for n, h in zip(numbers, items))
    q = question + (f"\n(English: {english})" if english and english != question else "")
    bound = (f"between 1 and {len(items)}" if isinstance(numbers, range) else
             "the [#n] numbers of this research session (shown here or in its earlier rounds)")
    return (f"Question: {q}\n\nEvidence items:\n\n{blocks}\n\n"
            f"Answer format (JSON):\n{VIEW_FORMAT[view]}\n"
            "In \"cannot_conclude\" sentences, cite the items that show the limit. Evidence numbers must be "
            f"{bound}. Reasoning basis note references are accepted only when supplied by "
            "the server; a heuristic_draft type label is not evidence and must not replace the quoted sentence.")


# ---------------------------------------------------------------- 4. checking

_SLASH = re.compile(r"\b([A-Za-z]\w*?)(\d+)/(\d+)(\w*)")   # H3K4me2/3, H4K12/16ac, CD34/45
_MOD = r"(?:me\d?|ac|ph|ub)"
_MARKS = re.compile(rf"\b(H\d[A-Za-z]\d+)({_MOD})((?:/(?:{_MOD}|\d))+)\b", re.I)   # H3K4me2/me3, H3K4me1/2/3, H3K27ac/me3


def _marks(m):
    pre = re.match(r"[A-Za-z]*", m[2])[0]
    return " ".join(m[1] + (pre + p if p.isdigit() else p) for p in [m[2], *m[3].split("/")[1:]])


def _claim_text(text):
    # "[3]" and "Figure 6" / "Table S1" are labels, not claim content; "H3K4me2/3" is two marks (AN-10), not the number 3
    text = sentence_utils.MENTION.sub(" ", _LITERATURE.sub(" ", text))   # first: "Table S1/2" is a label, not S1 + S2
    return _SLASH.sub(lambda m: f"{m[1]}{m[2]}{m[4]} {m[1]}{m[3]}{m[4]}", _MARKS.sub(_marks, text))


def _answer_numbers(text):
    """Numbers in an answer sentence. A Vietnamese decimal comma ("3,5") is read both ways."""
    clean = _claim_text(text)
    found = numbers(clean)
    alt = numbers(re.sub(r"(?<=\d),(?=\d{1,2}\b)", ".", clean))
    return found, alt


def _identifier_numbers(terms):
    """Numbers embedded in known biomedical identifiers, not measurements.

    Terms are supplied by the source/card context (canonical name and aliases),
    so this does not turn arbitrary answer text into an exemption.  Splitting a
    letter-to-digit boundary handles ``SIRT1`` and ``H3K9me3`` while preserving
    the usual numeric normalisation for ``8-cell`` and ``2D/3D``.
    """
    pool = set()
    for term in terms or ():
        value = re.sub(r"(?<=[A-Za-z])(?=\d)", " ", str(term or ""))
        pool |= numbers(value)
    return pool


_ENTITY = re.compile(r"\b[A-Za-z]+\d\w*")   # H4K12ac, sip150, SIRT1: a name that carries a digit


# AN-10 condition terms (closed set): vitrification, slow freezing. An item about one cannot support a sentence about the other.
_CONDITIONS = {"vitrification": re.compile(r"vitrif|th(?:ủy|uỷ) tinh (?:hóa|hoá)"),
               "slow freezing": re.compile(r"slow[- ]freez|làm lạnh chậm|đông lạnh chậm")}


_GREEK = str.maketrans({"α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta", "κ": "kappa"})


def _idents(text):
    """Identifier tokens (digit-bearing names such as H4K12ac, sip150) and condition terms of a text, case-folded.
    PDF "m^6A" is m6A; a superscript reference "H4K16ac^44" is H4K16ac; Greek letters are read as their names (TGF-β1 = TGF-beta1)."""
    text = re.sub(r"\^\d+(?:[,–-]\d+)*(?!\w)|[\^_]", "",unicodedata.normalize("NFC", _claim_text(text)).casefold().translate(_GREEK))
    return set(_ENTITY.findall(text)) | {name for name, rx in _CONDITIONS.items() if rx.search(text)}


def _foreign(sentence, held):
    """Identifiers of the sentence that no cited item holds. "H4K16" held covers "H4K16ac" (a suffix of up to 3 characters, both ways;
    a digit never extends a name: "CD3" is not "CD34")."""
    return {t for t in _idents(sentence) - held
            if not any(len(b := min(h, t, key=len)) < len(a := max(h, t, key=len)) <= len(b) + 3
                       and a.startswith(b) and not a[len(b)].isdigit() for h in held)}   # CD3 does not hold CD34


def _names(haystack, tokens):
    return any(re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", haystack) for t in tokens)


_LEANS_ON_PREVIOUS = re.compile(r"\s*(?:However|But|Yet|Nevertheless|Nonetheless|Although|This|These|Such|It)(?![\w])")


def _claim_sentences(item_text, claim):
    """The sentence(s) of a cited unit that the claim restates: most words in common (a single-letter word counts
    only as a capital: "Y"). A one-sentence unit, or a claim sharing nothing, gives the whole text."""
    words = lambda s: {w.casefold() for w in re.findall(r"\w+", s) if len(w) > 1 or w.isupper()}
    mine = words(_claim_text(claim))
    parts = [item_text[a:b] for a, b in sentence_utils.split_sentences(item_text)]
    scores = [len(mine & words(p)) for p in parts]
    if len(parts) < 2 or not max(scores):
        return item_text
    keep = {i for i, n in enumerate(scores) if n == max(scores)}
    # "... increased. However, this was not significant": a sentence that leans on the one before it qualifies it
    keep |= {i + 1 for i in keep if i + 1 < len(parts) and _LEANS_ON_PREVIOUS.match(parts[i + 1])}
    return item_text if len(keep) == len(parts) else " ".join(parts[i] for i in sorted(keep))


def _hedge_hits(text):
    # Lazy import avoids structure -> answer imports during the source checks.
    from .structure import uncertainty
    return uncertainty(text)


def _is_hedged(text):
    return bool(_hedge_hits(text))


def _item_identifier_terms(item, card):
    terms = []
    for source in (item or {}, card or {}):
        if not isinstance(source, dict):
            continue
        for key in ("identifier_terms", "concept_terms", "node_name", "concept_name", "aliases", "synonyms"):
            value = source.get(key)
            terms.extend(value if isinstance(value, (list, tuple, set)) else [value] if value else [])
    return terms


def check_sentence(sentence, items, cards, limits=False, part2=False, text_markers=True, scope="answer", unopened=()):
    """Returns {"check": "ok" | "struck:A?", "problems": [...], "warnings": [...]}.

    `limits`: a "cannot conclude" sentence (it names claims the sources do NOT support), so the
    overclaiming (A3) and direction (A6) checks do not apply.
    `text_markers=False`: "[#3]" in the text is NOT an evidence marker (node cards cite by sentence_ids).
    `scope`: "answer" (the ask-answer path) runs the A2 narrowing and A10; any other scope ("card", "note": one
    statement merging several papers) pools the numbers of every cited item and skips A10 (D-PENDING-8, NC-01).
    Evidence values that are not item numbers (bool, float, NaN, Infinity, text) strike A1.
    `unopened` (NW-11): item numbers the client never received in full; a Part 2 sentence (`part2`) citing one, by its
    evidence list or a "[#n]" marker, gets the warning A12 (never a strike)."""
    nfc = lambda value: unicodedata.normalize("NFC", value)   # CHR-01: an NFD answer is judged like its NFC form
    text = nfc(str(sentence.get("text") or ""))
    ev = sentence.get("evidence")
    if has_ai_knowledge(ev):   # AN-15: model knowledge only as a Part 3 basis (check_reasoning never passes it here)
        return {"check": "struck:A11", "evidence": [], "problems": ["A11"], "warnings": []}
    values = [_item_number(x) for x in (ev if isinstance(ev, list) else [] if ev is None else [ev])]
    invalid = None in values
    nums = sorted({n for n in values if n is not None}
                  | {int(m.group(1)) for m in (_MARKERS.finditer(text) if text_markers else ())})
    problems, warnings = [], []
    cited = [{**items[n - 1], "text": nfc(str(items[n - 1].get("text") or ""))} for n in nums if 1 <= n <= len(items)]
    if invalid or not nums or len(cited) != len(nums):
        problems.append("A1")
        return {"check": "struck:A1", "evidence": nums, "problems": problems, "warnings": warnings}
    if part2 and re.match(r"^\s*(?:Có|Không|Yes|No)\.(?:\s|$)", text, re.I):
        warnings.append("A9")
    if part2 and unopened and set(unopened).intersection(nums):
        warnings.append("A12")

    # A2 numbers: a number must come from a cited item that names the entity the sentence describes
    # (ponytail: "names" = holds a digit-bearing name such as H4K12ac that the sentence has; when no cited item does, any item)
    tokens = {t.casefold() for t in _ENTITY.findall(_claim_text(text))}
    shared, pools, named = set(), [], []
    for h in cited:
        card = cards.get(h["paper_id"], {})
        terms = _item_identifier_terms(h, card)
        pools.append(numbers(h["text"]) | _identifier_numbers(terms))
        named.append(_names((h["text"] + " " + " ".join(map(str, terms))).casefold(), tokens))
        if card.get("year"):
            shared.add(str(card["year"]))
        for r in h.get("cited_refs") or []:
            shared |= {str(r["num"])} | ({str(r["year"])} if r.get("year") else set())
    pool = shared.union(*(p for p, ok in zip(pools, named) if scope != "answer" or ok or not any(named)))
    # "giảm 2.0" may state a source "−2.0" without its sign; a sign the source does not have is still wrong
    pool |= {n[1:] for n in pool if n.startswith("-")}
    plain, alt = _answer_numbers(text)
    if not (plain <= pool or alt <= pool):
        problems.append("A2")

    # A3 overclaiming: every source hedged or speculative, the sentence says it firmly
    def hedged(h):
        return (h.get("hedging") == "hedged" or h.get("unit_type") in ("hypothesis", "interpretation")
                or _is_hedged(h["text"]))
    firm = (VI_DEFINITIVE.search(text) or DEFINITIVE.search(text)) and not _is_hedged(text)
    # ... or the sentence asserts causation/proof that no cited source states (association ≠ causation)
    def definitive(h):
        return bool(DEFINITIVE.search(h["text"]))
    if not limits and firm and (all(hedged(h) for h in cited) or not any(definitive(h) for h in cited)):
        problems.append("A3")
    # Even an ordinary declarative sentence can drop "seems" or "slightly".
    # AN-07: the hedge is read on the source sentence the claim restates, not on the whole unit
    # (a unit flagged hedged that the lexicon finds nowhere cannot be localised: the flag stays)
    def hedge_read(h):
        part = _claim_sentences(h["text"], text)
        if part == h["text"]:
            return part if hedged(h) else None
        return part if (h.get("unit_type") in ("hypothesis", "interpretation") or _is_hedged(part)
                        or (h.get("hedging") == "hedged" and not _is_hedged(h["text"]))) else None
    reads = [r for r in map(hedge_read, cited) if r is not None] if not limits else []
    kept = {x["category"] for x in _hedge_hits(text)}
    # a claim whose only hedge is a null result ("không đáng kể") keeps a source hedge only if that source sentence is a null result too
    if reads and (not kept or (kept == {"non_significance"} and any(
            {x["category"] for x in _hedge_hits(r)} - {"non_significance"} or not _is_hedged(r) for r in reads))):
        problems.append('A7')

    if limits and _affirms(text):   # AN-11: relaxed A3 does not let a limits sentence assert a result
        problems.append("A3")

    # A4 other papers' statements written as findings
    def secondhand(h):
        return bool(h.get("cited_refs")) or ((h["kind"] == "unit" or h.get("unit_id")) and h.get("unit_type") in ("cited_statement", "background"))  # a sentence item labelled by its owning unit (neural.prepare) counts too
    if all(secondhand(h) for h in cited) and not (VI_ATTRIBUTION.search(text) or ATTRIBUTION.search(text)):
        problems.append("A4")

    # A5 review-only support
    if all(h.get("review_paper") for h in cited) and not VI_REVIEW.search(text):
        warnings.append("A5")

    # A6 direction
    dirs = {h.get("direction") for h in cited if h.get("direction") in ("increase", "decrease", "no_change")}
    if dirs and not limits:
        up, down, same = bool(VI_UP.search(text)), bool(VI_DOWN.search(text)), bool(VI_NOCHANGE.search(text) or NON_SIGNIFICANCE.search(text))
        wrong = set()   # unit directions this sentence contradicts; every cited unit that has one counts
        if up and not same:
            wrong |= {"no_change"} | ({"decrease"} if not down else set())
        if down and not same:
            wrong |= {"no_change"} | ({"increase"} if not up else set())
        if same and not up and not down:
            wrong |= {"increase", "decrease"}
        if dirs & wrong:
            problems.append("A6")

    # A10 subject / condition: an identifier or condition term of the sentence that no cited item holds
    # (a limits sentence names what the sources do NOT show, so it may name a foreign one)
    if not limits and scope == "answer":
        held = set().union(*(_idents(" ".join(map(str, [h["text"], h.get("agent") or "", h.get("species") or "",
                                                     cards.get(h["paper_id"], {}).get("title") or "",
                                                     *_item_identifier_terms(h, cards.get(h["paper_id"], {}))]))) for h in cited))
        if _foreign(text, held):
            problems.append("A10")

    check = "ok" if not [p for p in problems if p in STRIKE] else "struck:" + problems[0]
    return {"check": check, "evidence": nums, "problems": problems, "warnings": warnings}


def _checked(sentences, items, cards, limits=False, part2=False, unopened=()):
    out = []
    for s in sentences or []:
        if isinstance(s, str):
            s = {"text": s, "evidence": []}
        if not isinstance(s, dict) or not str(s.get("text") or "").strip():
            continue
        c = check_sentence(s, items, cards, limits, part2=part2, unopened=unopened)
        out.append({"text": str(s["text"]).strip(), **c,
                    "reasons": [CHECK_TEXT[p] for p in c["problems"] + c["warnings"]]})
    return out


SCOPE_NOTE = "Không tìm thấy trong kho hiện tại không có nghĩa là không tồn tại trong y văn"


def _author_year(card):
    authors = card.get("authors") or []
    author_value = card.get("first_author") or (authors[0] if authors else None)
    return (author_value if author_value not in (None, "", "Unknown") else "không rõ",
            card.get("year") if card.get("year") not in (None, "", "n.d.") else "không rõ")


def _page_of(item):
    page = item.get("page")
    return (item.get("pages") or [None])[0] if page is None else page


def _citation(card, item):
    """A display citation made only from paper metadata and the item's page."""
    author, year = _author_year(card)
    page = _page_of(item)
    return f"[{author}, {year}, trang {page if page is not None else 'không rõ'}] ({item.get('id', 'không rõ')})"


def code_evidence(items, cards, cited=None):
    """Copy evidence text from the retrieved store items; never use AI text here."""
    cards = cards or {}
    wanted = set(range(1, len(items) + 1)) if cited is None else set(cited)
    result, rows_by_paper = [], {}
    for number, item in enumerate(items, 1):
        if number not in wanted:
            continue
        quote = _whole_quote(item, rows_by_paper)
        result.append({"item": number, "id": item.get("id"), "quote": quote,
                       "text": quote, "citation": _citation(cards.get(item.get("paper_id"), {}), item)})
    return result


def _whole_quote(item, rows_by_paper):
    """Join a sentence split by a page break or figure (`continues`), copied from the store; a sentence that
    still ends mid-way is marked so a fragment is never read as the full claim (Lee 2025 S0276)."""
    text = str(item.get("text") or "")
    sid, pid = str(item.get("id") or ""), item.get("paper_id")
    has_predecessor = False
    joined_chain = False
    is_sentence = bool(pid and ":S" in sid)
    if is_sentence:
        if pid not in rows_by_paper:
            try:
                path = paper_dir(pid) / "sentences.jsonl"
                rows_by_paper[pid] = {
                    r["sentence_id"]: r
                    for r in sentence_utils.cached_linked_rows(read_jsonl(path), path)
                }
            except (OSError, ValueError, KeyError):
                rows_by_paper[pid] = {}
        rows, seen = rows_by_paper[pid], {sid}
        prev = {row.get("continues"): key for key, row in rows.items() if row.get("continues")}
        has_predecessor = sid in prev
        cur = sid
        while cur in prev and prev[cur] not in seen and len(seen) < 4:  # start at the sentence's first fragment
            joined_chain = True
            cur = prev[cur]
            seen.add(cur)
            text = sentence_utils.join_fragments(rows[cur]["text"], text, rows[cur], rows.get(sid))
        nxt = item.get("continues") or (rows.get(sid) or {}).get("continues")
        previous_row = rows.get(sid)
        while nxt and nxt in rows and nxt not in seen and len(seen) < 4:
            joined_chain = True
            seen.add(nxt)
            text = sentence_utils.join_fragments(text, rows[nxt]["text"], previous_row, rows[nxt])
            previous_row = rows[nxt]
            nxt = rows[nxt].get("continues")
    first = text.lstrip()[:1]
    if is_sentence and first.isalpha() and first.islower() and not has_predecessor and not joined_chain:
        text = "[…] " + text.lstrip()
    if text and sentence_utils.TERMINAL.search(text.rstrip()):
        return text
    if text and not re.search(r"[.!?:;)\]\"”’]\s*$", text):
        text += " […] (câu bị cắt bởi trang/hình trong bản trích — mở nguồn để đọc đủ)"
    return text


def _is_marker(value):
    return isinstance(value, str) and value.strip().casefold() == AI_KNOWLEDGE


def has_ai_knowledge(values):
    """True when the basis / evidence holds the `ai_knowledge` marker (strip + casefold; a string or a list)."""
    return any(_is_marker(v) for v in (values if isinstance(values, list) else [values]))


def _numeric_refs(values):
    """(item numbers, note refs). The `ai_knowledge` marker is neither: ask `has_ai_knowledge` for it."""
    nums, notes = [], []
    values = values if isinstance(values, list) else [values]
    for value in values:
        number = _item_number(value)
        if number is not None:
            nums.append(number)
        elif value is None or _is_marker(value):
            continue
        elif str(value).strip():   # bool / float / Infinity stay text: not an allowed note, so A8 strikes them
            notes.append(str(value).strip())
    return sorted(set(nums)), notes


def check_reasoning(sentence, items, cards, note_texts=(), note_refs=()):
    """Check a Part 3 sentence. AN-15: exactly one kind of basis: item numbers and/or allowed ticket notes, or
    `ai_knowledge` alone; anything else strikes A11."""
    text = str(sentence.get("text") or "")
    basis = sentence.get("basis", sentence.get("evidence", []))
    nums, notes = _numeric_refs(basis)
    nums = sorted(set(nums) | {int(m.group(1)) for m in _MARKERS.finditer(text)})   # "[#n]" in the text cites too (as in check_sentence)
    marker = has_ai_knowledge(basis)
    allowed_notes = {str(ref).strip().strip("[]") for ref in (note_refs or ())}
    unknown_notes = sorted(note for note in set(notes) if note.strip().strip("[]") not in allowed_notes)
    if nums:
        checked = check_sentence({"text": text, "evidence": nums}, items, cards)
    else:
        # A note-only synthesis has no evidence-item number to satisfy A1, but its numbers
        # must still be present in the text carried by the ticket or source items.
        pool = set()
        for item in items:
            pool |= numbers(str(item.get("text") or ""))
        for note in note_texts or ():
            pool |= numbers(str(note))
        plain, alt = _answer_numbers(text)
        problems = ["A2"] if not (plain <= pool or alt <= pool) else []
        checked = {"check": "struck:A2" if problems else "ok", "evidence": [],
                   "problems": problems, "warnings": []}
    if unknown_notes:
        checked = {**checked, "problems": list(checked.get("problems") or []) + ["A8"]}
        checked["check"] = "struck:" + checked["problems"][0]
    if marker == bool(nums or notes):   # no basis at all, or the marker next to another kind
        checked = {**checked, "problems": ["A11"] + list(checked.get("problems") or []), "check": "struck:A11"}
    kind = ("none" if "A11" in checked["problems"] else "ai_knowledge" if marker else
            "items_notes" if nums and notes else "items" if nums else "notes")
    refs = [items[n - 1].get("id", n) for n in nums if 1 <= n <= len(items)] + notes
    return {"text": text.strip(), "basis": nums + notes + ([AI_KNOWLEDGE] if marker else []), **checked,
            "basis_kind": kind, "basis_label": AI_KNOWLEDGE_LABEL if kind == "ai_knowledge" else
            ("suy luận từ " + ", ".join(map(str, refs)) if refs and kind != "none" else ""),
            "reasons": [CHECK_TEXT[p] for p in checked["problems"] + checked["warnings"]]}


def _checked_reasoning(rows, items, cards, note_refs=(), note_texts=()):
    return [check_reasoning(row, items, cards, note_texts=note_texts, note_refs=note_refs) for row in (rows or [])
            if isinstance(row, dict) and str(row.get("text") or "").strip()]


def uncertainty_items(items, cited):
    """Return cited items whose stored quote carries uncertainty markers."""
    result = []
    for number in sorted(set(cited)):
        if not 1 <= number <= len(items):
            continue
        item = items[number - 1]
        text = str(item.get("text") or "")
        marker = item.get("uncertainty")
        if marker is None:
            marker = _hedge_hits(text)
        if marker or item.get("hedging") == "hedged" or _is_hedged(text):
            result.append(number)
    return result


def check_parts(data, items, cards, note_refs=(), note_texts=(), unopened=()):
    """Normalize/check the four-part reply and add code-owned evidence and uncertainty."""
    data = data if isinstance(data, dict) else {}
    answers = _checked(_pick(data, "answer", "sentences", "answers", "summary"), items, cards, part2=True, unopened=set(unopened))
    reasoning = _checked_reasoning(_pick(data, "reasoning", "suy_luan"), items, cards, note_refs, note_texts)
    cited = []
    for row in answers + reasoning:
        cited.extend(row.get("evidence") or [])
        cited.extend(n for n in row.get("basis") or [] if isinstance(n, int))
    cited = sorted(set(cited))
    for row in answers + reasoning:
        row["chunk_ids"] = [items[n - 1].get("id") for n in (row.get("evidence") or row.get("basis") or [])
                             if isinstance(n, int) and 1 <= n <= len(items)]
    missing = [str(m.get("text") if isinstance(m, dict) else m).strip()
               for m in (_pick(data, "missing", "gaps") or []) if m]
    missing = [m for m in missing if m and m != "None"]
    for number in uncertainty_items(items, cited):
        marker = items[number - 1].get("id", number)
        note = f"Mục bằng chứng [#{number}] ({marker}) có dấu hiệu không chắc chắn trong trích dẫn."
        if note not in missing:
            missing.append(note)
    # Scope is rendered from the dedicated field; do not print it again in missing.
    missing = list(dict.fromkeys(m for m in missing if m != SCOPE_NOTE))
    return {"evidence": code_evidence(items, cards, cited), "answer": answers,
            "sentences": answers, "reasoning": reasoning, "missing": missing,
            "scope_note": SCOPE_NOTE}


def _markdown_sentence(row, items, reasoning=False):
    text = str(row.get("text") or "")
    if row.get("check") != "ok":
        text = f"~~{text}~~" + (f" [bị gạch: {'; '.join(row['reasons'])}]" if row.get("reasons") else "")   # AN-13
    if reasoning and row.get("basis_kind") == "ai_knowledge":   # AN-15: the one shared label
        return f"{text} — {AI_KNOWLEDGE_LABEL}"
    if reasoning and row.get("basis_kind") in ("items", "notes", "items_notes"):
        return f"{text} ({row['basis_label']})" if row.get("basis_label") else text
    refs = [f"({chunk_id})" for chunk_id in row.get("chunk_ids") or [] if chunk_id]
    if refs:
        return text + " " + " ".join(refs)
    refs = []
    for number in row.get("evidence") or row.get("basis") or []:
        if isinstance(number, int) and 1 <= number <= len(items):
            refs.append(f"({items[number - 1].get('id', number)})")
        elif not isinstance(number, int) and not _is_marker(number):
            refs.append(f"({number})")
    return text + (" " + " ".join(refs) if refs else "")


def render_markdown(checked, items=()):
    """Pure renderer for the checked four-part answer, suitable for export/MCP clients."""
    checked = checked or {}
    items = list(items or [])
    evidence = checked.get("evidence") or []
    lines = ["## Bằng chứng từ tài liệu"]
    if evidence:
        lines += [f'> [#{row.get("item")}] "{row.get("quote", row.get("text", ""))}" — {row.get("citation", "[không rõ, không rõ, trang không rõ] (không rõ)")}'
                  for row in evidence]
    else:
        lines.append("> Không có trích dẫn được chọn từ kho.")
    lines.append("")
    lines.append("## Trả lời dựa trên bằng chứng")
    answers = checked.get("answer") or checked.get("sentences") or []
    lines.extend(_markdown_sentence(row, items) for row in answers)
    lines.append("")
    lines.append("## [REASONING] Phần suy luận của AI")
    lines.append(REASONING_NOTE)
    lines.extend(_markdown_sentence(row, items, True) for row in checked.get("reasoning") or [])
    lines.append("")
    lines.append("## Không tìm thấy / không chắc chắn")
    lines.extend(f"- {m}" for m in checked.get("missing") or [])
    return "\n".join(lines)


# Descriptive alias for callers that prefer the complete name.
render_answer_markdown = render_markdown


# ---------------------------------------------------------------- 4b. local HTML page (U10b)
# Written only by the MCP dispatch (write_html), never by the shared check: the web page, gold gate and conform use it too.

OUTPUT_DIR = config.ROOT / "output" / "answers"
SHORT_QUOTE = 300
# ponytail: the hedge lexicon has no plain negation category; this small constant is the stricter reading (a hidden
# "not" keeps the quote whole). Move it to the lexicon if a negation category is added.
_NEGATION = re.compile(r"\b(?:not|no|never|neither|nor|without|cannot|none|unable|unchanged|fail(?:s|ed|ure)?|lack\w*|absen(?:t|ce)"
                       r"|(?:in|non-?)significant\w*)\b|n['’]t\b", re.I)
_e = lambda value: html.escape("" if value is None else str(value), quote=True)


def short_quote(quote):
    """(shown, whole): the leading whole sentences of a long quote (a prefix of it, up to ~SHORT_QUOTE chars) and the full
    quote; (quote, None) when it is short, no sentence end is in reach, or the hidden rest holds a hedge or a negation."""
    if len(quote) <= SHORT_QUOTE:
        return quote, None
    cut = max((end for _, end in sentence_utils.split_sentences(quote) if end <= SHORT_QUOTE), default=0)
    hidden = quote[cut:]
    if not hidden.strip() or not cut or hedges.uncertainty(hidden) or _NEGATION.search(unicodedata.normalize("NFC", hidden)):
        return quote, None
    return quote[:cut] + "…", quote


def _cited_numbers(row, items):
    """Item numbers a checked row cites: its evidence list, its basis and every [#n] in its text (U9a)."""
    nums = {n for n in list(row.get("evidence") or []) + list(row.get("basis") or []) if type(n) is int}
    nums |= {int(m.group(1)) for m in _MARKERS.finditer(str(row.get("text") or ""))}
    return sorted(n for n in nums if 1 <= n <= len(items))


def _row_html(row, extra=""):
    struck = row.get("check") != "ok"
    why = [CHECK_TEXT.get(p, p) for p in row.get("problems") or []] or (list(row.get("reasons") or []) if struck else [])
    text = f"<del>{_e(row.get('text'))}</del>" if struck else _e(row.get("text"))   # AN-13
    out = text + (f' <span class="why">[bị gạch: {_e("; ".join(why))}]</span>' if struck and why else "")
    warn = [CHECK_TEXT.get(w, w) for w in row.get("warnings") or []]
    return out + (f'<div class="warn">{_e("; ".join(warn))}</div>' if warn else "") + extra


def render_html(checked, items, question, vault=None, cards=None):
    """One readable, self-contained page for a checked four-part answer: stdlib only, inline CSS, no script, no URL.
    Quotes come from the code-owned `checked["evidence"]` rows; every text is escaped; only obsidian:// becomes a link."""
    checked, items, cards = checked or {}, list(items or []), cards or {}
    answers = checked.get("answer") or checked.get("sentences") or []
    reasoning = checked.get("reasoning") or []
    missing = checked.get("missing") or []
    rows = answers + reasoning
    quotes = {r.get("item"): str(r.get("quote", r.get("text", ""))) for r in checked.get("evidence") or []}
    cited = sorted({n for n in quotes if type(n) is int and 1 <= n <= len(items)} | {n for r in rows for n in _cited_numbers(r, items)})
    pids = {items[n - 1].get("paper_id") for n in cited}
    links = {}
    if vault and pids:
        from . import neural   # neural imports this module
        links = neural.paper_note_links(vault, [p for p in pids if p])

    def quote(n):
        return quotes.get(n, str(items[n - 1].get("text") or ""))

    def note_link(item):
        url = item.get("obsidian_url") or (links.get(item.get("paper_id")) or {}).get("obsidian_url")
        if not url:
            return ""
        return f'<a href="{_e(url)}">mở note</a>' if str(url).startswith("obsidian://") else f'<span>{_e(url)}</span>'

    def evidence_html(n):
        item = items[n - 1]
        card = cards.get(item.get("paper_id")) or {}
        author, year = _author_year(card)
        kind = item.get("unit_type")
        badges = ([find.UNIT_LABELS.get(kind, kind).upper()] if kind else []) + (
            ["TỔNG QUAN"] if card.get("evidence_type") in find.REVIEW_TYPES or item.get("review_paper") else [])
        shown, whole = short_quote(quote(n))
        more = f"<details><summary>xem đủ câu</summary>{_e(whole)}</details>" if whole else ""
        return (f'<div class="ev"><div class="meta">{"".join(f"<b>{_e(b)}</b>" for b in badges)}'
                f"[#{n}] {_e(author)} {_e(year)} · tr. {_e(_page_of(item) if _page_of(item) is not None else 'không rõ')} {note_link(item)}</div>"
                f'<blockquote title="{_e(item.get("id"))}">{_e(shown)}{more}</blockquote></div>')

    def basis_html(row):
        if row.get("basis_kind") == "ai_knowledge":
            return f'<div class="meta">{_e(AI_KNOWLEDGE_LABEL)}</div>'
        refs = [f"[#{b}]" if type(b) is int else str(b) for b in row.get("basis") or [] if type(b) is int or not _is_marker(b)]
        ok = row.get("basis_kind") in ("items", "notes", "items_notes") and refs
        return f'<div class="meta">suy luận từ {_e(", ".join(refs))}</div>' if ok else ""

    ok_rows = [r for r in answers if r.get("check") == "ok"]
    m_papers = {items[n - 1].get("paper_id") for r in ok_rows for n in _cited_numbers(r, items)}
    struck = sum(1 for r in rows if r.get("check") != "ok")
    summary = (f"{len(ok_rows)} câu trả lời có bằng chứng từ {len(m_papers)} bài · {len(reasoning)} câu suy luận · "
               f"{len(missing)} điều không tìm thấy · {struck} câu bị gạch")
    part2 = "".join("<li>" + _row_html(r, "".join(evidence_html(n) for n in _cited_numbers(r, items))) + "</li>" for r in answers)
    part3 = "".join("<li>" + _row_html(r, basis_html(r)) + "</li>" for r in reasoning)
    by_paper = {}
    for n in cited:
        by_paper.setdefault(items[n - 1].get("paper_id"), []).append(n)
    sources = ""
    for pid, nums in by_paper.items():
        card = cards.get(pid) or {}
        author, year = _author_year(card)
        ids = " · ".join(f"{k.upper()} {_e(card[k])}" for k in ("doi", "pmid") if card.get(k))
        sources += (f'<div class="paper" title="{_e(pid)}"><h3>{_e(author)} {_e(year)} — {_e(card.get("title") or "không rõ tiêu đề")}</h3>'
                    f'<div class="meta">{ids} {note_link(items[nums[0] - 1])}</div>')
        for n in nums:
            item = items[n - 1]
            where = ", ".join(str(s) for s in item.get("sections") or [] if s) or str(item.get("section") or "không rõ")
            sids = ", ".join(map(str, item.get("sentence_ids") or [item.get("id")]))
            sources += (f'<div class="ev"><div class="meta">[#{n}] · {_e(where)} · tr. '
                        f"{_e(_page_of(item) if _page_of(item) is not None else 'không rõ')} · {_e(sids)}</div>"
                        f"<blockquote>{_e(quote(n))}</blockquote></div>")
        sources += "</div>"
    scope = checked.get("scope_note") or SCOPE_NOTE
    return (f'<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f"<title>Trả lời có kiểm chứng</title><style>{_HTML_CSS}</style></head><body><main>"
            f"<h1>{_e(question)}</h1><p class=\"sum\">{_e(summary)}</p>"
            f"<h2>Trả lời dựa trên bằng chứng</h2><ol>{part2}</ol>"
            f"<h2>[REASONING] Phần suy luận của AI</h2><p class=\"meta\">{REASONING_NOTE}</p><ol>{part3}</ol>"
            f"<h2>Không tìm thấy / không chắc chắn</h2><ul>{''.join(f'<li>{_e(m)}</li>' for m in missing)}</ul><p class=\"meta\">{_e(scope)}</p>"
            f"<h2>Bằng chứng từ tài liệu (nguồn)</h2>{sources}</main></body></html>")


_HTML_CSS = """
:root{--bg:#fff;--fg:#1d2430;--mute:#5d6778;--line:#d9dee7;--card:#f5f7fa;--warn:#9a5b00;--bad:#a12626}
@media (prefers-color-scheme:dark){:root{--bg:#14181f;--fg:#e4e8ef;--mute:#9aa4b5;--line:#2c3441;--card:#1c222c;--warn:#e0b25a;--bad:#ef8a8a}}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}
h1{font-size:1.4rem}h2{font-size:1.1rem;margin-top:2rem;border-bottom:1px solid var(--line);padding-bottom:.2rem}h3{font-size:1rem;margin:.8rem 0 .2rem}
.sum,.meta{color:var(--mute);font-size:.88rem}.warn{color:var(--warn);font-size:.85rem}.why{color:var(--bad);font-size:.88rem}
del{color:var(--mute)}li{margin:.9rem 0}.ev{margin:.4rem 0 .4rem 1rem}
blockquote{margin:.2rem 0;padding:.4rem .7rem;background:var(--card);border-left:3px solid var(--line)}
b{font-size:.72rem;border:1px solid var(--line);border-radius:4px;padding:0 .35rem;margin-right:.4rem}
summary{cursor:pointer;color:var(--mute);font-size:.85rem}a{color:inherit}
"""


def write_html(checked, items, question, vault=None, cards=None):
    """Write render_html to OUTPUT_DIR/<YYYYmmdd-HHMMSS>-<slug>.html (never over an older page) and return the path."""
    folded = unicodedata.normalize("NFKD", str(question or "").replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:40].strip("-") or "answer"
    page = render_html(checked, items, question, vault, cards)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    for suffix in [""] + [f"-{i}" for i in range(2, 100)]:
        path = OUTPUT_DIR / f"{stamp}-{slug}{suffix}.html"
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as handle:
                handle.write(page)
            return path
        except FileExistsError:
            continue
    raise OSError("no free file name in " + str(OUTPUT_DIR))


# ---------------------------------------------------------------- 5. code-written parts

def overview(items, cards, papers_scanned, runs):
    """Report part 1 "Kho có gì", written by code from the retrieved items (no AI)."""
    by_type, papers = {}, {}
    for h in items:
        key = h["label"]
        by_type[key] = by_type.get(key, 0) + 1
        papers.setdefault(h["paper_id"], 0)
        papers[h["paper_id"]] += 1
    reviews = [p for p in papers if cards.get(p, {}).get("evidence_type") in find.REVIEW_TYPES]
    lines = [f"Đã quét {papers_scanned} bài trong phạm vi tìm; {len(items)} mục bằng chứng được đưa cho AI, "
             f"từ {len(papers)} bài" + (f" (trong đó {len(reviews)} bài tổng quan)." if reviews else ".")]
    lines.append("Theo loại: " + "; ".join(f"{k}: {v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])) + ".")
    lines.append("Bài có nhiều mục nhất: " + "; ".join(
        f"{cards.get(p, {}).get('cite', p)} ({n})" for p, n in sorted(papers.items(), key=lambda x: -x[1])[:5]) + ".")
    lines.append("Từ khóa đã tìm: " + "; ".join(f"“{r['query']}” ({r['total']} kết quả)" for r in runs) + ".")
    if items and all("từ khóa" not in (h.get("via") or ["từ khóa"]) for h in items):
        lines.append("Không mục nào chứa đúng từ khóa; các mục chỉ gần nghĩa — có thể kho hiện tại chưa có câu trả lời.")
    return lines


TABLE_COLUMNS = ["Bài", "Loài", "Mẫu", "Can thiệp / liều", "Chỉ số", "Chiều", "Số liệu, p", "Trang", "Mục"]
DIRECTION_VI = {"increase": "↑ tăng", "decrease": "↓ giảm", "no_change": "= không đổi", "mixed": "± hỗn hợp",
                "not_applicable": ""}


def comparison_table(items, cards):
    """Bảng so sánh from the structured fields of evidence units (code only, no AI)."""
    by_paper = {}
    for h in items:
        if h["kind"] == "unit" and h["unit_type"] in ("result", "method_detail", "conclusion"):
            by_paper.setdefault(h["paper_id"], []).append(h["id"])
    details = {}
    for pid, ids in by_paper.items():
        details.update(_unit_details(pid, ids))
    rows = []
    for n, h in enumerate(items, start=1):
        u = details.get(h["id"])
        if not u:
            continue
        m, iv, o = u.get("model") or {}, u.get("intervention") or {}, u.get("outcome") or {}
        sample = ", ".join(x for x in (m.get("cell_type"), m.get("origin"), m.get("stage"), m.get("setting")) if x)
        agent = "; ".join(x for x in (iv.get("agent"), iv.get("dose"), iv.get("timing"), iv.get("duration")) if x)
        if iv.get("comparator"):
            agent += f" (so với {iv['comparator']})"
        data = "; ".join(str(v) for v in o.get("values") or [])
        extra = ", ".join(f"{k} {o[k]}" for k in ("n", "p") if o.get(k))
        if o.get("value_source") == "figure_only":
            data = "chỉ có trong hình"
        rows.append({"item": n, "unit_id": h["id"], "paper_id": h["paper_id"],
                     "cells": [cards.get(h["paper_id"], {}).get("cite", h["paper_id"]), m.get("species") or "",
                               sample, agent, o.get("measure") or "", DIRECTION_VI.get(o.get("direction"), ""),
                               "; ".join(x for x in (data, extra) if x), ", ".join(str(p) for p in h["pages"]),
                               h["label"]],
                     "statement": h["statement"], "status": h.get("status")})
    return {"columns": TABLE_COLUMNS, "rows": rows}


# ---------------------------------------------------------------- entry point

def translate(question, speed="fast"):
    """English queries for a question (used by the web page before searching a Vietnamese question)."""
    s = settings("answer")
    if not provider_status(s)["ready"]:
        return {"queries": [], "english_question": "", "error": provider_status(s)["message"]}
    try:
        qs, english, _ = make_queries(s, question, s[speed if speed in ("fast", "deep") else "fast"]["queries"])
    except AIError as e:
        return {"queries": [], "english_question": "", "error": str(e)}
    return {"queries": qs, "english_question": english, "error": None}


def answer(con, cfg, question, view="evidence4", speed="fast", vault=None, all_vaults=False, filters=None,
           use_ai=True, queries=None, snapshot=None):
    """The whole pipeline. Always returns a dict (errors are reported inside, never raised).

    `queries`: English queries already made (e.g. by `translate`); then no query step is run.
    `snapshot`: (items, runs) kept from an earlier call; then nothing is retrieved again and the [#n] numbers stay (AN-13)."""
    t0 = time.time()
    s = settings("answer")
    status = provider_status(s)
    view = view if view in ("evidence4", "google", "report5", "table") else "evidence4"
    speed = speed if speed in ("fast", "deep") else "fast"
    conf = s[speed]
    ai_ok = use_ai and status["ready"]
    out = {"question": question, "view": view, "speed": speed, "provider": status, "queries_run": [],
           "english_question": "", "papers_scanned": find.papers_in_scope(con, cfg, vault, all_vaults),
           "items": [], "papers": {}, "answer": None, "overview": [], "table": None, "errors": [],
           "usage": [], "scope_note": SCOPE_NOTE, "evidence": []}

    # 1. queries
    vietnamese = find.looks_vietnamese(question)
    queries = [queries] if isinstance(queries, str) else queries if isinstance(queries, list) else []
    given = [str(q).strip() for q in queries if isinstance(q, str) and q.strip()]
    queries = given + ([] if vietnamese else [question]) if given else [question]
    if given:
        pass
    elif ai_ok and (vietnamese or speed == "deep"):
        try:
            qs, english, usage = make_queries(s, question, conf["queries"])
            out["english_question"], out["usage"] = english, [usage]
            queries = qs + ([] if vietnamese else [question])
        except AIError as e:
            out["errors"].append(f"Không đổi được câu hỏi sang từ khóa tiếng Anh: {e}")
    elif vietnamese:
        out["errors"].append("Câu hỏi tiếng Việt: máy chỉ tìm được theo nghĩa. Để chính xác hơn hãy gõ từ khóa tiếng Anh, "
                             + ("hoặc hỏi qua ứng dụng AI đã nối MCP (ask_prepare → ask_check)." if status.get("mcp")
                                else "hoặc chọn AI trong config/ai.yaml."))

    # 2. evidence
    if snapshot:
        items, runs = snapshot
    else:
        items, runs = collect(con, cfg, queries[:4], vault, all_vaults, filters or {}, conf["max_items"])
        if speed == "deep" and items:
            items = add_context(con, items, conf["context_for_top"], conf["context_window"], conf["max_total_items"])
    cards = find.paper_cards(con, {h["paper_id"] for h in items})
    # RT-05: every query's best item covers too few keyword groups -> say so in the answer, the items stay
    not_found = bool(runs) and all(r.get("not_found") for r in runs)
    best = max(runs, key=lambda r: r["best_groups_matched"] / max(r["groups"], 1)) if not_found else {}
    note = best.get("not_found_message")
    out.update(items=items, papers=cards, queries_run=runs, not_found=not_found, not_found_message=note)
    out["evidence"] = code_evidence(items, cards)
    out["overview"] = overview(items, cards, out["papers_scanned"], runs)
    if view == "table":
        out["table"] = comparison_table(items, cards)
    if not items:
        out["answer"] = {"not_found": True, "evidence": [], "answer": [], "sentences": [],
                          "reasoning": [], "missing": ["Không tìm thấy bằng chứng trong kho hiện tại cho câu hỏi này.", SCOPE_NOTE]}
        out["markdown"] = render_markdown(out["answer"], items)
        return _finish(out, t0)
    if view == "table" or not ai_ok:
        if view == "evidence4":
            out["answer"] = {"evidence": out["evidence"], "answer": [], "sentences": [],
                              "reasoning": [], "missing": [SCOPE_NOTE] + ([note] if note else [])}
            out["markdown"] = render_markdown(out["answer"], items)
        return _finish(out, t0)

    # 3–4. answer + checks
    english = out["english_question"]
    try:
        text, usage = _chat(s, ANSWER_RULES, build_prompt(question, english, items, cards, view))
        out["usage"].append(usage)
        data = _json(text)
    except AIError as e:
        out["errors"].append(str(e))
        return _finish(out, t0)
    if (isinstance(data, dict) and len(data) == 1 and isinstance(next(iter(data.values())), dict)
            and "text" not in next(iter(data.values()))):
        data = next(iter(data.values()))          # {"report": {...}} wrapper
    missing = [str(m.get("text") if isinstance(m, dict) else m).strip()
               for m in (_pick(data, "missing", "gaps") if isinstance(data, dict) else None) or [] if m]
    missing = [m for m in missing if m and m != "None"]
    if isinstance(data, dict) and data.get("not_found") is True and not missing:
        missing = ["Không tìm thấy bằng chứng trong kho hiện tại cho câu hỏi này."]
    if note:
        missing.append(note)
        if isinstance(data, dict):
            data["missing"] = missing
    if view == "evidence4":
        out["answer"] = check_parts(data, items, cards)
        out["evidence"] = out["answer"]["evidence"]
        out["markdown"] = render_markdown(out["answer"], items)
        n = len(out["answer"]["answer"]) + len(out["answer"]["reasoning"])
    elif view == "google":
        out["answer"] = {"sentences": _checked(_pick(data, "answer", "sentences", "answers", "summary"), items, cards),
                         "missing": missing}
        n = len(out["answer"]["sentences"])
    else:
        out["answer"] = {"evidence": _checked(_pick(data, "evidence", "bang_chung"), items, cards),
                         "can_conclude": _checked(_pick(data, "can_conclude", "conclusions"), items, cards),
                         "cannot_conclude": _checked(_pick(data, "cannot_conclude", "cannot"), items, cards, True),
                         "missing": missing}
        n = sum(len(out["answer"][k]) for k in ("evidence", "can_conclude", "cannot_conclude"))
    meaningful_missing = [m for m in missing if m not in (SCOPE_NOTE, note)]
    if not n and not meaningful_missing:
        out["errors"].append("AI trả lời sai định dạng (không có câu nào đọc được). Bấm tìm lại; nếu lặp lại, "
                             "xem bản thô trong lưu vết.")
        out["raw_reply"] = text[:4000]
    return _finish(out, t0)


def _pick(data, *keys):
    """First present key (models do not always use the exact name asked for), always as a list:
    a single sentence or string given instead of a list is wrapped, never iterated letter by letter."""
    if not isinstance(data, dict):
        return data if isinstance(data, list) else None
    for k in keys:
        v = data.get(k)
        if v and isinstance(v, list):
            return v
        if v and isinstance(v, (str, dict)):
            return [v]
    return None


def _finish(out, t0):
    out["elapsed_seconds"] = round(time.time() - t0, 1)
    sentences = []
    a = out.get("answer") or {}
    keys = ("sentences", "reasoning") if out.get("view") == "evidence4" else \
           ("sentences", "evidence", "can_conclude", "cannot_conclude")
    for key in keys:
        sentences += a.get(key) or []
    out["summary_checks"] = {"sentences": len(sentences),
                             "struck": sum(1 for x in sentences if x["check"] != "ok"),
                             "warnings": sum(1 for x in sentences if x.get("warnings"))}
    if not out.get("markdown") and out.get("view") == "evidence4":
        out["markdown"] = render_markdown(out.get("answer") or {}, out.get("items") or [])
    if sentences or out.get("raw_reply"):   # keep a trace of every AI answer (machine cache; can be deleted)
        _save_trace(out)
    return out


def _save_trace(out):
    try:
        ANSWER_CACHE.mkdir(parents=True, exist_ok=True)
        name = time.strftime("%Y%m%d_%H%M%S") + f"_{out['view']}_{out['speed']}.json"
        (ANSWER_CACHE / name).write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        out["saved_as"] = config.rel(ANSWER_CACHE / name)
    except OSError:
        pass
