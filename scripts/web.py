"""Local search page (M3.2): http://127.0.0.1:<port>, only on this computer.

Started by "1 - Mo trang chinh.bat"; closing that window stops it. The server itself uses only the
standard library; search by meaning uses onnxruntime/tokenizers/numpy (approved 2026-09-25).
Read-only except POST /api/paper/delete (reversible move to knowledge_store/removed/, under the writer lock) and the
library jobs: it never changes the knowledge store otherwise (AI answers are copied to knowledge_store/cache/answers/).

Safety: listens on 127.0.0.1 only; requests whose Host is not this address are refused (blocks
DNS-rebinding pages); every /api call needs the random token written into the page at start.
"""

import argparse
import json
import mimetypes
import os
import secrets
import socket
import sqlite3
import sys
import threading
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from kbsys import answer, config, db, discovery, find, graph, library, manage, mesh, overlap, vectors, neural, structure, workflow, results  # noqa: E402
MAX_UPLOAD = 300 * 1024 * 1024  # one PDF/ZIP per request
from kbsys.db import paper_dir  # noqa: E402

STATIC = Path(__file__).resolve().parent / "kbsys" / "web"
TOKEN = secrets.token_urlsafe(24)
CFG = config.load()
MAX_BODY = 200_000


def connect():
    """A read-only connection per request (the agents may be writing at the same time)."""
    con = sqlite3.connect(f"file:{config.DB_PATH.as_posix()}?mode=ro", uri=True, timeout=10,
                          check_same_thread=False)
    con.row_factory = sqlite3.Row
    return con


def vaults(con):
    names = set()
    if config.VAULTS_DIR.is_dir():
        names |= {p.name for p in config.VAULTS_DIR.iterdir()
                  if p.is_dir() and config.is_research_vault_name(p.name)}
    names |= {r[0] for r in con.execute("SELECT DISTINCT vault FROM paper_vaults")
              if config.is_research_vault_name(r[0])}
    counts = dict(con.execute("SELECT vault, COUNT(*) FROM paper_vaults GROUP BY vault").fetchall())
    return [{"name": n, "papers": counts.get(n, 0)} for n in sorted(names)]


def _int(value, default):
    """A number from the query string; anything else gives the default (never an error page)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class Server(ThreadingHTTPServer):
    """On Windows, SO_REUSEADDR lets a second server bind a port that is already serving, so two copies of
    the page would share port 8765 and answer each other's requests. Bind exclusively instead."""
    daemon_threads = True
    allow_reuse_address = os.name != "nt"

    def server_bind(self):
        if os.name == "nt":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def _list(q, key):
    return [x for v in q.get(key, []) for x in v.split(",") if x]


def _filters(q):
    return {"unit_types": _list(q, "types"), "sections": _list(q, "sections"),
            "species": (q.get("species") or [""])[0], "evidence": (q.get("evidence") or [""])[0],
            "year_from": (q.get("year_from") or [""])[0], "year_to": (q.get("year_to") or [""])[0],
            "paper_id": (q.get("paper") or [""])[0]}


class Handler(BaseHTTPRequestHandler):
    server_version = "KBSearch/2"

    def log_message(self, fmt, *args):  # quiet console: only errors are printed
        pass

    # ------------------------------------------------------------ plumbing
    def _host_ok(self):
        host = (self.headers.get("Host") or "").lower()
        port = self.server.server_address[1]
        return host in (f"127.0.0.1:{port}", f"localhost:{port}")

    def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _file(self, path, ctype=None):
        if not path.is_file():
            return self._send(404, {"error": "không tìm thấy file"})
        ctype = ctype or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(200, path.read_bytes(), ctype)

    def _guard(self, need_token):
        if not self._host_ok():
            self._send(403, {"error": "chỉ mở từ máy này"})
            return False
        if need_token and self.headers.get("X-KB-Token") != TOKEN:
            self._send(403, {"error": "thiếu mã phiên; hãy tải lại trang"})
            return False
        return True

    def do_HEAD(self):
        self.do_GET()

    # ------------------------------------------------------------ GET
    def do_GET(self):
        url = urlparse(self.path)
        path, q = url.path, parse_qs(url.query)
        is_api = path.startswith("/api/")
        if not self._guard(is_api):
            return
        try:
            if path in ("/", "/index.html"):
                page = (STATIC / "index.html").read_text(encoding="utf-8").replace("__KB_TOKEN__", TOKEN)
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; img-src 'self' data:; "
                                   "style-src 'self' 'unsafe-inline'; frame-ancestors 'none'"})
            if path == "/library":
                page = (STATIC / "library.html").read_text(encoding="utf-8").replace("__KB_TOKEN__", TOKEN)
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                   "frame-ancestors 'none'"})
            if path == '/neural':
                page = (STATIC / 'neural.html').read_text(encoding='utf-8').replace('__KB_TOKEN__', TOKEN)
                return self._send(200, page, 'text/html; charset=utf-8',
                                  {'Content-Security-Policy': "default-src 'self'; img-src 'self' blob:; style-src 'self'; frame-ancestors 'none'"})
            if path == "/graph":
                page = (STATIC / "graph.html").read_text(encoding="utf-8").replace("__KB_TOKEN__", TOKEN)
                return self._send(200, page, "text/html; charset=utf-8",
                                  {"Content-Security-Policy": "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                                   "frame-ancestors 'none'"})
            if path.startswith("/static/"):
                name = unquote(path[len("/static/"):])
                target = (STATIC / name).resolve()
                if STATIC.resolve() not in target.parents:
                    return self._send(404, {"error": "không tìm thấy"})
                return self._file(target)
            if path.startswith("/pdf/"):
                return self._pdf(unquote(path[len("/pdf/"):]))
            if path.startswith("/crop/"):
                return self._crop(unquote(path[len("/crop/"):]))
            if is_api:
                return self._api_get(path, q)
            self._send(404, {"error": "không tìm thấy"})
        except workflow.WriterBusy:
            self._send(503, {"error": "Chỉ mục đang được cập nhật; hãy thử lại.", "retry_after": 1},
                       extra={"Retry-After": "1"})
        except ValueError as e:  # user-facing validation messages
            self._send(400, {"error": str(e)})
        except Exception as e:  # report, keep serving
            traceback.print_exc()
            self._send(500, {"error": f"lỗi máy chủ: {e}"})

    def _pdf(self, paper_id):
        con = connect()
        try:
            pdf = find.main_pdf_path(con, paper_id)
        finally:
            con.close()
        if not pdf:
            return self._send(404, "<p>PDF gốc không có trên máy này (có thể đã lưu trữ ở nơi khác).</p>",
                              "text/html; charset=utf-8")
        self._file(pdf, "application/pdf")

    def _crop(self, rest):
        paper_id, _, rel = rest.partition("/")
        if not find.PAPER_ID.fullmatch(paper_id):
            return self._send(404, {"error": "không tìm thấy"})
        folder = paper_dir(paper_id).resolve()
        target = (folder / rel).resolve()
        if not rel or (folder / "crops").resolve() not in target.parents:
            return self._send(404, {"error": "không tìm thấy"})
        self._file(target)

    def _api_get(self, path, q):
        one = lambda k, d="": (q.get(k) or [d])[0]  # noqa: E731
        con = connect()
        try:
            if path == "/api/library":
                return self._send(200, library.overview(con, one("vault") or config.default_vault(CFG)))
            if path == "/api/job":
                return self._send(200, library.job_status())
            if path == "/api/status":
                registry_path=config.ROOT/'projects'/'vault_registry.json'
                registry=json.loads(registry_path.read_text(encoding='utf-8')) if registry_path.is_file() else {'schema_version':1,'vaults':[]}
                return self._send(200, {"vaults": vaults(con), "default_vault": config.default_vault(CFG),
                                        'vault_registry':registry,'scope_policy':'current_vault_by_default',
                                        "ai": answer.provider_status(), "meaning": meaning_status(),
                                        "mesh": {"ready": mesh.available()}})
            if path == "/api/graph":
                vault = one("vault") or config.default_vault(CFG)
                def year_arg(key):
                    value = one(key)
                    return int(value) if value.isdigit() else None
                result = graph.build(con, vault, year_arg("year_from"), year_arg("year_to"),
                                     one("species") or None, one("relation_type") or None)
                return self._send(200 if result else 404, result or {"error": "không có vault này"})
            if path == '/api/neural/search':
                return self._send(200, neural.search(one('vault') or config.default_vault(CFG), one('q'), _int(one('hops'), 0), _int(one('limit'),12),one('kind'),_int(one('offset'),0)))
            if path == '/api/neural/note':
                return self._send(200, neural.node_reader(
                    con, one('vault') or config.default_vault(CFG), one('path'), one('sha256') or None,
                    query=one('q'), speed=one('speed','fast'), source_offset=_int(one('source_offset'), 0),
                    source_limit=_int(one('source_limit'), 20), compact=True))
            if path == '/api/research/sources':
                return self._send(200,neural.resolve_sources(con,one('vault') or config.default_vault(CFG),_list(q,'id'),_int(one('limit'),45 if one('speed')=='deep' else 24),query=one('q'),context_limit=12 if one('speed')=='deep' else 4))
            if path == '/api/research/node-profile':
                return self._send(200,neural.node_reader(con,one('vault') or config.default_vault(CFG),one('path'),one('sha256') or None,query=one('q'),speed=one('speed','fast')))
            if path == '/api/research/media':
                return self._send(200,neural.media_reader(con,one('vault') or config.default_vault(CFG),one('paper'),_list(q,'id')))
            if path == '/api/research/crop':
                from kbsys import media
                data=neural.media_reader(con,one('vault') or config.default_vault(CFG),one('paper'),[one('object')])
                bundle=next((b for b in data['bundles'] if b['object_id']==one('object')),None)
                if not bundle or not bundle['crop_url'] or bundle['crop'].get('sha256')!=one('sha256'):
                    raise ValueError('Crop unavailable or changed; retrieve again')
                target=media.crop_path(paper_dir(one('paper')),bundle['crop']['path'])
                raw=target.read_bytes()
                if __import__('hashlib').sha256(raw).hexdigest()!=one('sha256'):
                    raise ValueError('Crop changed; retrieve again')
                return self._send(200,raw,'image/png')
            if path == '/api/structure':
                return self._send(200, structure.build(one('id'), persist=False))
            if path == '/api/protocols':
                return self._send(200, structure.compare_methods(con, one('vault') or config.default_vault(CFG), one('q')))
            if path == '/api/workflow':
                return self._send(200, workflow.status())
            if path == '/api/research/overview':
                return self._send(200,results.overview(con,one('vault') or config.default_vault(CFG)))
            if path == '/api/research/results':
                return self._send(200,results.browse(con,one('vault') or config.default_vault(CFG),one('q'),one('kind','result'),one('paper'),_int(one('offset'),0),_int(one('limit'),12),speed=one('speed','fast')))
            if path == "/api/search":
                limit = min(max(_int(one("limit"), 20), 1), 100)
                offset = max(_int(one("offset"), 0), 0)
                extra = [x.strip() for x in q.get("also", []) if x.strip()][:6]
                res = find.search(con, CFG, one("q"), vault=one("vault") or None, all_vaults=one("all") == "1",
                                  filters=_filters(q), limit=limit + offset if extra else limit,
                                  offset=0 if extra else offset,depth=one('speed','fast'))
                if extra:  # English queries made from a Vietnamese question: merge them
                    res = _merge(con, res, extra, one, q, limit, offset)
                return self._send(200, res)
            if path == "/api/paper":
                view = find.paper_view(con, one("id"))
                return self._send(200 if view else 404, view or {"error": "không có bài này"})
            if path == "/api/ref":
                ref = find.reference(con, one("id"))
                return self._send(200 if ref else 404, ref or {"error": "không có mục tham khảo này"})
            if path == "/api/context":
                return self._send(200, find.context(con, one("id"), min(max(_int(one("w"), 3), 0), 10)))
            self._send(404, {"error": "không có"})
        finally:
            con.close()

    # ------------------------------------------------------------ POST
    def do_POST(self):
        url = urlparse(self.path)
        if not self._guard(True):
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if url.path == "/api/upload":  # raw file bytes; name/vault/batch in the query string
                if n > MAX_UPLOAD:
                    return self._send(413, {"error": "tệp quá lớn (tối đa 300 MB)"})
                q = parse_qs(url.query)
                one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
                return self._send(200, library.save_upload(one("vault"), one("name"), self.rfile.read(n),
                                                           one("batch") or None))
            if n > MAX_BODY:
                return self._send(413, {"error": "yêu cầu quá lớn"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._send(400, {"error": "yêu cầu không hợp lệ; hãy tải lại trang"})
            if url.path == "/api/translate":
                return self._send(200, answer.translate(str(body.get("question") or ""), body.get("speed")))
            if url.path == '/api/neural/answer':
                con=connect()
                try:
                    return self._send(200,neural.answer_question(con,CFG,body.get('vault') or config.default_vault(CFG),str(body.get('question') or ''),body.get('speed','fast'),body.get('hops')))
                finally:
                    con.close()
            if url.path in ('/api/neural/prepare', '/api/neural/check'):
                con = connect()
                try:
                    result = neural.prepare(con, CFG, body.get('vault') or config.default_vault(CFG), str(body.get('question') or ''), body.get('hops', 0),body.get('speed','fast'),body.get('offset', 0)) if url.path.endswith('prepare') else neural.check(con, body.get('ticket'), body.get('reply'))
                    return self._send(200, result)
                finally:
                    con.close()
            if url.path == "/api/overlap":   # pasted passage -> stored sentences sharing wording (lexical overlap)
                con = connect()
                try:
                    return self._send(200, overlap.find_overlaps(con, str(body.get("text") or ""),
                                      vault=body.get("vault") or None, limit=_int(body.get("limit"), 20)))
                finally:
                    con.close()
            if url.path == "/api/answer":
                con = connect()
                try:
                    res = answer.answer(con, CFG, str(body.get("question") or ""), view=body.get("view"),
                                        speed=body.get("speed"), vault=body.get("vault") or None,
                                        all_vaults=bool(body.get("all")), filters=body.get("filters") or {},
                                        queries=body.get("queries") or None,
                                        use_ai=bool(body.get("use_ai", True)))
                finally:
                    con.close()
                return self._send(200, res)
            if url.path == "/api/vault/create":
                con = connect()
                try:
                    return self._send(200, library.create_vault(con, str(body.get("name") or ""),
                                                                str(body.get("topic") or "")))
                finally:
                    con.close()
            if url.path == "/api/vault/select":
                return self._send(200, library.select_vault(str(body.get("name") or "")))
            if url.path == "/api/job/start":
                return self._send(200, library.start_job(str(body.get("action") or ""), str(body.get("vault") or ""),
                                                         {"limit": body.get("limit")}))
            if url.path == "/api/discover-citations":
                con = connect()
                try:
                    result = discovery.discover(con, str(body.get("paper_id") or ""),
                                                str(body.get("vault") or config.default_vault(CFG)),
                                                min(int(body.get("limit") or 100), 100))
                finally:
                    con.close()
                return self._send(200, result)
            if url.path == "/api/stage-discovery":
                con = connect()
                try:
                    result = discovery.stage(str(body.get("vault") or ""),
                                              str(body.get("candidate_id") or ""), con)
                finally:
                    con.close()
                return self._send(200, result)
            if url.path == "/api/paper/delete":  # reversible: files go to knowledge_store/removed/ (manage.delete_paper)
                paper_id = body.get("paper_id")
                if not isinstance(paper_id, str) or not find.PAPER_ID.fullmatch(paper_id):
                    raise ValueError("Mã bài không hợp lệ")
                try:
                    with workflow.writer_lock():
                        # Busy check: an analyze job holds the lock only around its writes and would
                        # recreate the moved folder. ponytail: an analyze job started during the delete, or a
                        # separate kb.py/MCP analysis, is not caught; job-wide lock if needed.
                        job = library.job_status()
                        if job["running"]:
                            return self._send(409, {"error": f"Đang chạy việc '{job['action']}'. Chưa gỡ bài nào; "
                                                             "hãy đợi việc đó xong rồi gỡ lại."})
                        con = db.connect()  # writable, unlike connect()
                        try:
                            if not con.execute("SELECT 1 FROM papers WHERE paper_id = ?", (paper_id,)).fetchone():
                                raise ValueError("Không có bài này trong thư viện")
                            return self._send(200, manage.delete_paper(con, paper_id))
                        finally:
                            con.close()
                except workflow.WriterBusy:
                    return self._send(409, {"error": "Đang có việc khác ghi vào kho (nhập, phân tích hoặc kết nối). "
                                                     "Chưa gỡ bài nào; hãy đợi việc đó xong rồi gỡ lại."})
            self._send(404, {"error": "không có"})
        except workflow.WriterBusy:
            self._send(503, {"error": "Chỉ mục đang được cập nhật; hãy thử lại.", "retry_after": 1},
                       extra={"Retry-After": "1"})
        except ValueError as e:  # user-facing validation messages
            self._send(400, {"error": str(e)})
        except Exception as e:
            traceback.print_exc()
            self._send(500, {"error": f"lỗi máy chủ: {e}"})


def _merge(con, first, queries, one, q, limit, offset):
    """Results of several English queries, best score kept, re-ranked."""
    merged = {h["id"]: h for h in first["hits"]} if not first.get("needs_translation") else {}
    runs = []
    for text in queries:
        r = find.search(con, CFG, text, vault=one("vault") or None, all_vaults=one("all") == "1",
                        filters=_filters(q), limit=limit + offset,depth=one('speed','fast'))
        runs.append({"query": text, "total": r["total"], "mode": r["mode"], "not_found": r["not_found"],
                     "best_groups_matched": r["best_groups_matched"], "not_found_message": r.get("not_found_message")})
        for h in r["hits"]:
            if h["id"] not in merged or merged[h["id"]]["score"] < h["score"]:
                merged[h["id"]] = h
        first["highlight"] = sorted(set(first["highlight"]) | set(r["highlight"]), key=len, reverse=True)
        first["expansions"] = first.get("expansions", []) + r.get("expansions", [])
        for k, counts in r["facets"].items():
            for name, c in counts.items():
                first["facets"].setdefault(k, {})
                first["facets"][k][name] = max(first["facets"][k].get(name, 0), c)
    hits = sorted(merged.values(), key=lambda h: -h["score"])
    first["hits"] = hits[offset: offset + limit]
    # each query fetched only one page, so the merged count is a floor; the largest single total is a better one
    first["total"] = max([len(hits)] + [r["total"] for r in runs])
    first["papers"] = find.paper_cards(con, {h["paper_id"] for h in first["hits"]})
    first["queries_run"] = runs
    first["mode"] = "or" if any(r["mode"] == "or" for r in runs) else "and"
    # RT-05: flagged only when every English query was (a Vietnamese first query has no groups to judge)
    first["not_found"] = all(r["not_found"] for r in runs) and (first["needs_translation"] or first["not_found"])
    best = max(runs, key=lambda r: r["best_groups_matched"])
    first["best_groups_matched"] = best["best_groups_matched"]
    first.pop("not_found_message", None)
    if first["not_found"]:
        first["not_found_message"] = best["not_found_message"]
    first["needs_translation"] = False
    return first


def self_check():
    """Answer checks (A1–A6) on planted sentences, with no AI: each must get the expected verdict."""
    fake = lambda i, **kw: {"kind": "unit", "id": f"x:E{i}", "paper_id": "x", "unit_type": "result",  # noqa: E731
                            "text": "", "statement": "", "hedging": "none", "direction": None,
                            "review_paper": False, "cited_refs": [], **kw}
    items = [
        fake(1, text="Muse cells increased within 24 hours in 22 (75.9%) of 29 subjects.", direction="increase"),
        fake(2, unit_type="hypothesis", hedging="hedged", text="Muse cells may be mobilized from bone marrow."),
        fake(3, unit_type="cited_statement", text="Stem cells are mobilized in stroke.^2",
             cited_refs=[{"num": 2, "year": 2009}]),
        fake(4, text="Age did not differ between the groups.", direction="no_change"),
        fake(5, review_paper=True, text="Muse cells are pluripotent."),
        fake(6, text="The score changed by −2.0 points after treatment.", direction="decrease"),
    ]
    cards = {"x": {"year": 2016}}
    cases = [
        ("Tế bào Muse tăng trong 24 giờ ở 22 (75.9%) trên 29 bệnh nhân [#1].", "ok"),
        ("Tăng ở 22 (75,9%) trên 29 bệnh nhân [#1].", "ok"),
        ("Tế bào Muse tăng ở 25 trên 29 bệnh nhân [#1].", "struck:A2"),
        ("Tế bào Muse được huy động.", "struck:A1"),
        ("Tế bào Muse được huy động [#9].", "struck:A1"),
        ("Nghiên cứu chứng minh tế bào Muse được huy động từ tủy xương [#2].", "struck:A3"),
        ("Tế bào Muse chắc chắn gây ra hồi phục chức năng [#1].", "struck:A3"),
        ("Tác giả đặt giả thuyết rằng tế bào Muse có thể được huy động [#2].", "ok"),
        ("Tế bào gốc được huy động trong đột quỵ [#3].", "struck:A4"),
        ("Theo tài liệu được trích dẫn năm 2009, tế bào gốc được huy động [#3].", "ok"),
        ("Tế bào Muse giảm trong 24 giờ đầu [#1].", "struck:A6"),
        ("Tuổi làm tăng số tế bào [#4].", "struck:A6"),
        ("Tuổi không liên quan tới số tế bào [#4].", "ok"),
        ("Tế bào Muse đa năng [#5].", "ok"),  # A5 is only a warning
        ("Điểm số giảm 2.0 điểm sau điều trị [#6].", "ok"),       # sign of a decrease may be dropped
        ("Điểm số thay đổi -2.0 điểm sau điều trị [#1].", "struck:A2"),  # a sign the source lacks
    ]
    bad = 0
    for text, want in cases:
        got = answer.check_sentence({"text": text, "evidence": []}, items, cards)["check"]
        mark = "ok " if got == want else "SAI"
        bad += got != want
        print(f"{mark} {got:10} (cần {want:10}) {text}")
    limited = answer.check_sentence({"text": "Không thể kết luận rằng tuổi làm tăng số tế bào [#4]."}, items, cards, True)
    bad += limited["check"] != "ok"
    print(f"{'ok ' if limited['check'] == 'ok' else 'SAI'} phần 'không kết luận được' bỏ qua A3/A6")
    bad += robustness_check()
    print("Kết quả:", "đạt" if not bad else f"{bad} lỗi")
    return 1 if bad else 0


def robustness_check():
    """Bad input must give a clear message, never a crash (M6.3 audit). Returns the number of failures."""
    from kbsys import mcp_server

    def raises(fn):
        try:
            fn()
        except (ValueError, answer.AIError):
            return True
        return False

    cases = [
        ("AI trả một chuỗi thay vì danh sách", answer._pick({"answer": "Câu [1]."}, "answer") == ["Câu [1]."]),
        ("AI trả not_found: true", answer._pick({"not_found": True}, "missing", "not_found") is None),
        ("AI trả một con số", raises(lambda: answer._json("42"))),
        ("năm lọc gõ chữ bị bỏ qua", find._paper_filters({"year_from": "abc", "year_to": "2020"}, []) ==
         "AND pa.year <= ?"),
        ("vault lạ khi tải tệp lên", raises(lambda: library.save_upload("../../x", "a.pdf", b"%PDF"))),
        ("tên vault tiếng Việt", library.folder_name("Tế bào Muse (đột quỵ)") == "Te_bao_Muse_dot_quy"),
        ("MCP: tin nhắn không phải object", mcp_server.handle(None, {}, [1])["error"]["code"] == -32600),
        ("MCP: công cụ lạ", mcp_server.handle(None, {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                          "params": {"name": "x"}})["error"]["code"] == -32602),
        ("MCP: thiếu tham số", mcp_server.handle(None, {}, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                                            "params": {"name": "read_paper", "arguments": {}}})
         ["error"]["code"] == -32602),
    ]
    if mesh.available():
        cases.append(("MeSH: chữ thường 'stem' không thành STEM (kính hiển vi)",
                      not any("Microscopy" in " ".join(v) for _, _, v, _ in mesh.expand(["stem"], lambda w: False))))
    a = Server(("127.0.0.1", 0), Handler)
    try:
        cases.append(("không mở được trang thứ hai trên cùng cổng", _bind_fails(a.server_address[1])))
    finally:
        a.server_close()
    bad = 0
    for label, ok in cases:
        print(f"{'ok ' if ok else 'SAI'} {label}")
        bad += not ok
    return bad


def _bind_fails(port):
    try:
        Server(("127.0.0.1", port), Handler).server_close()
    except OSError:
        return True
    return False


PREP = {"running": False, "message": ""}


def meaning_status():
    st = vectors.status() if vectors.index_ready() else {"ready": False, "message": "Chưa có chỉ mục tìm theo nghĩa"}
    if PREP["running"]:
        st["message"] = PREP["message"] or st["message"]
        st["updating"] = True
    return st


def prepare(say=print):
    """MeSH and the meaning model (downloaded once if missing), then the vectors of new/changed papers.

    Safe to run at every start: nothing is redone for papers that did not change."""
    PREP["running"] = True
    try:
        if not mesh.available():
            say("Đang tải từ điển MeSH (NLM, ~17 MB) và dựng chỉ mục… (chỉ lần đầu)")
            info = mesh.build(mesh.download())
            say(f"  MeSH: {info['descriptors']} đề mục, {info['terms']} thuật ngữ")
        if not vectors.available():
            say("Đang tải mô hình tìm theo nghĩa (~135 MB)… (chỉ lần đầu)")
            vectors.download_model()
        con = connect()
        try:
            def progress(msg):
                PREP["message"] = "Đang cập nhật tìm theo nghĩa:" + msg.split(":", 1)[-1]
                say(msg)
            res = vectors.update(con, progress=progress)
        finally:
            con.close()
        if res.get("embedded") or res.get("removed"):
            say(f"  Tìm theo nghĩa: đã cập nhật {res['embedded']} bài ({res['seconds']} s)")
    except Exception as e:  # never stop the page because of this
        say(f"Chưa chuẩn bị được MeSH/tìm theo nghĩa: {e}. Trang vẫn tìm theo từ khóa được.")
    finally:
        PREP["running"] = False
        PREP["message"] = ""


def warm_default_vault(say=print):
    """Warm common node reads after bind; never make startup depend on it."""
    try:
        vault = config.default_vault(CFG)
        if not vault:
            return
        con = connect()
        try:
            result = neural.warm_vault(con, vault, max_nodes=4, delay=0.05)
        finally:
            con.close()
        if result.get("warmed"):
            say(f"  Node cache: warmed {result['warmed']} hub(s)")
    except Exception as e:  # a warm-up failure must not affect the server
        say(f"  Node cache warm-up skipped: {e}")


def start_background_warmup():
    """Start the non-blocking node warm-up and return its daemon thread."""
    thread = threading.Thread(target=warm_default_vault, name="kb-node-warmup", daemon=True)
    thread.start()
    return thread


def main():
    ap = argparse.ArgumentParser(description="Local search page (127.0.0.1 only)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--check", action="store_true", help="test the answer checks, then exit")
    ap.add_argument("--prepare", action="store_true", help="MeSH + meaning index now, then exit")
    ap.add_argument("--no-prepare", action="store_true", help="do not update the meaning index at start")
    args = ap.parse_args()
    if args.check:
        return self_check()
    if args.prepare:
        prepare()
        return 0
    server = None
    for port in range(args.port, args.port + 20):
        try:
            server = Server(("127.0.0.1", port), Handler)
            break
        except OSError:
            continue
    if server is None:
        print("Không mở được cổng nào từ", args.port)
        return 1
    if not args.no_prepare:
        threading.Thread(target=prepare, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Trang tìm kiếm đang chạy tại {url}")
    print("Chỉ máy này mở được. Đóng cửa sổ này để tắt.")
    start_background_warmup()
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
