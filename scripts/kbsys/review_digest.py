"""`Cần duyệt.md` (vault root): the review queue, standard clauses RQ-01..RQ-03 and RQ-06.

Four groups, one self-contained item each: the source sentence (copied from `sentences` by code), the note link and PDF page, what
the machine found, a proposal and three tick boxes. Reads files only. Ticked boxes survive regeneration (each item carries a hidden
key). Nothing is accepted, moved or deleted from here. Node cards, conflicts, open hypotheses, wrong crops and orphan images are
not queue items (RQ-02): they have their own notes (`Open conflicts`, `Open hypotheses`, node notes) or belong to the secretary.
Group 4 stays empty until the machine claim check exists (RQ-04).
"""

import json
import re
import time

from . import config, db, notes, presentation, review_policy, vault_layout

NOTE = "Cần duyệt"
GROUPS = ("Sai kiến thức trong bài gốc", "Sai do AI, sửa chưa chắc cách sửa", "Bài mồ côi, chưa nối lại được",
          "Nhận định không có nguồn mà máy không quyết được")
INSTRUCTIONS = (
    "Trang này tự tạo: chỉ có 4 nhóm việc mà máy không tự quyết được; mỗi mục có câu gốc, link tới note và trang PDF, điều máy phát hiện và đề xuất.",
    "Với mỗi mục chọn một ô: đồng ý đề xuất, giữ nguyên (không phải lỗi) hoặc khác; ô đã chọn được giữ lại khi phần mềm cập nhật.",
    "Đánh dấu ô không tự sửa gì: muốn áp dụng, nói với AI agent. Ghi chú của bạn viết ngoài khối tự động.",
)
BOXES = ("Đồng ý đề xuất", "Giữ nguyên, không phải lỗi", "Khác (ghi chú của bạn, ngoài khối tự động)")
_BOX = re.compile(r"^- \[([xX ])\] (?:Đồng ý|Giữ nguyên|Khác)")
_KEY = re.compile(r"<!--k:(.+?)-->")


def _ticked(path):
    """{item key: [ticked?, ...] for its three boxes}; the file may still be the old one in `Review Queue/`."""
    done, boxes = {}, []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return done
    for line in lines:
        if m := _BOX.match(line):
            boxes.append(m.group(1) != " ")
        elif m := _KEY.search(line):
            done[m.group(1)], boxes = boxes, []
    return done


def _json(path):
    """A malformed or odd-shaped file must not stop `kb.py notes`."""
    try:
        data = db.read_json(path, {})
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _figure_flags(vault_papers):
    """(meta, paper_id, label, [(claim sentence id, machine note)], red warning?) of each reading the machine flags; a wrong crop is not one."""
    for pid in vault_papers:
        meta = _json(db.paper_dir(pid) / "metadata.json")
        for path in sorted((db.paper_dir(pid) / "figure_readings").glob("*.json")):
            reading = _json(path)
            bad = [c for c in reading.get("caption_consistency") or [] if isinstance(c, dict) and c.get("status") == "inconsistent"]
            red = bool(reading.get("red_warning_candidate"))
            if reading.get("crop_wrong") is not True and (bad or red):
                claims = [(c.get("claim_sentence_id"), str(c.get("note") or "")) for c in bad]
                caption = (reading.get("provenance") or {}).get("caption_sentence_ids") or []
                yield meta, pid, reading.get("label") or path.stem, claims or [(sid, "") for sid in caption[:1]], red


def _item(con, number, key, title, meta, pid, claims, finding, proposal, done):
    """The lines of one queue item. `claims`: [(sentence id, note)]; the quoted text is the stored sentence."""
    rows = [(sid, row) for sid, _ in claims if sid and (row := con.execute("SELECT text, page FROM sentences WHERE sentence_id = ?", (sid,)).fetchone())]
    quotes = [f"> {' '.join(row[0].split())} — ({presentation.cite_label(meta, [row[1]] if row[1] else [])})" for _, row in rows]
    pages = ", ".join(str(p) for p in sorted({row[1] for _, row in rows if row[1]}))
    ticks = (done.get(key, []) + [False] * len(BOXES))[:len(BOXES)]  # a box line the researcher deleted comes back unticked
    return [f"### [Nhóm {number}] {title}", *(quotes or ["> *Không tìm thấy câu gốc trong kho dữ liệu.*"]),
            f"Bài: [[{meta.get('note_name') or pid or 'Review Queue'}]]" + (f" · PDF trang {pages}" if pages else ""),
            f"**Máy phát hiện:** {finding} **Đề xuất:** {proposal}",
            *(f"- [{'x' if tick else ' '}] {label}" for tick, label in zip(ticks, BOXES)),
            f"<small>mã: {', '.join(sid for sid, _ in rows)}</small> <!--k:{key}-->" if rows else f"<!--k:{key}-->", ""]


def _pin(vdir):
    """RQ-06: bookmark the queue in `.obsidian/bookmarks.json`; the entry is added only if absent and the researcher's own entries stay."""
    def has(items):
        return isinstance(items, list) and any(isinstance(i, dict) and ((i.get("type") == "file" and i.get("path") == f"{NOTE}.md") or has(i.get("items"))) for i in items)

    def add(data):
        if isinstance(data.get("items", []), list) and not has(data.get("items")):
            data.setdefault("items", []).append({"type": "file", "ctime": int(time.time() * 1000), "path": f"{NOTE}.md", "title": NOTE})
    vault_layout._merge_json(vdir / ".obsidian" / "bookmarks.json", add)


def write(con, vault):
    vdir = config.vault_dir(vault)
    path, old = vdir / f"{NOTE}.md", vdir / "Review Queue" / f"{NOTE}.md"
    if old.is_file() and not path.exists():
        old.replace(path)  # the queue lived in Review Queue/ before RQ-06; its ticks and the text outside the block come along
    done = _ticked(path)
    groups = [[], [], [], []]
    papers = [r[0] for r in con.execute("SELECT paper_id FROM paper_vaults WHERE vault = ? ORDER BY paper_id", (vault,)).fetchall()]
    for meta, pid, label, claims, red in _figure_flags(papers):
        why = " ".join("; ".join(n for _, n in claims if n).split())[:300]  # one line: a note's newline must not open an item or a box
        title = f"{label} — {meta.get('note_name') or pid}"
        if red:
            groups[0] += _item(con, 1, f"fig:{pid}:{label}", title, meta, pid, claims, f"{label}: {why or 'AI đề nghị cảnh báo đỏ.'}",
                               "mở hình trong PDF; nếu bài gốc sai thì gắn cờ source_issue và cảnh báo đỏ (PDF không bị sửa).", done)
        else:
            groups[1] += _item(con, 2, f"fig:{pid}:{label}", title, meta, pid, claims, f"{label}: {why}",
                               "mở hình trong PDF và so với chú thích; nếu máy đọc sai, nói AI agent đọc lại hình.", done)
    for it in review_policy.pending(con, vault):
        pid = it["paper_id"]
        meta = _json(db.paper_dir(pid) / "metadata.json") if pid else {}
        first = con.execute("SELECT sentence_id FROM sentences WHERE paper_id = ? AND kind = 'text' ORDER BY seq LIMIT 1", (pid,)).fetchone()
        groups[2] += _item(con, 3, f"orphan:{pid or it['item_id']}", meta.get("note_name") or it["note_name"] or pid or f"mục {it['item_id']}", meta, pid,
                           [(first[0], "")] if first else [], it["detail"].split("] ", 1)[-1],
                           "giữ bài như ứng viên cầu nối hoặc nhờ AI agent nối vào chủ đề; không xóa bài.", done)
    lines = [notes.INDEX_MARK, f"# {NOTE}", "", *INSTRUCTIONS, ""]
    for number, (title, group) in enumerate(zip(GROUPS, groups), 1):
        lines += [f"## {number}. {title} ({sum(l.startswith('### ') for l in group)})", "", *(group or ["*Không có.*", ""])]
    notes._write_index(path, "\n".join(lines) + "\n")
    _pin(vdir)
    return {"items": [sum(l.startswith("### ") for l in g) for g in groups]}


if __name__ == "__main__":  # self-check of the tick-preserving key
    assert _KEY.search("<small>mã: p:S0001</small> <!--k:fig:p:Figure 1-->").group(1) == "fig:p:Figure 1"
    assert _BOX.match("- [x] Đồng ý đề xuất").group(1) == "x" and not _BOX.match("- [ ] [[A]] <!--k:card:a-->")
    print(json.dumps("ok"))
