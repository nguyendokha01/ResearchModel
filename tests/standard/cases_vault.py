"""PASS / FAIL cases of the stages VN (vault notes) and RQ (review queue), see scripts/kbsys/conform_vault.py.

Data checks get small synthetic vaults (notes passed in memory, files and `.obsidian` settings in the temporary folder). The probes
(VN-01 rebuild, VN-10 retirement, RQ-01/02 `review_digest.write`, RQ-07 `review_policy.refresh`) run the product in a throw-away
project in every PASS and FAIL case; a FAIL case of a probe hands the check a deliberately broken judge or damaged data.
test_vault.py runs the data parts apart from the probes and checks that every FAIL case fails for its own reason.
"""
import json

from kbsys import concepts, db, notes, review_digest, review_policy
from kbsys import conform_vault as cv
from kbsys.conform import Store, Vault

from .cases_graph import mem_db, put

CASES = {}
RED = {}
FP = f"<!-- kb:concept-fingerprint:{'0' * 40} -->"
QUEUE = f"{review_digest.NOTE}.md"


def node(name="Apoptosis", body="## Definition\n\n- Cells were counted in group A.\n", front="type: concept\n", folder_note=None):
    return (f"---\n{front}---\n# {name}\n\n{notes.AUTO_START}\n{FP}\n\n{body}\n{notes.AUTO_END}\n\n## My notes\n")


def paper(name="Chen 2020", body="## Tóm tắt (Abstract)\n\nCells were counted in group A.\n", extra=""):
    return (f"---\ntype: paper\npaper_id: p1\ntitle: Cells in medium\n{extra}---\n# Cells in medium\n\n{notes.AUTO_START}\n\n{body}\n"
            f"{notes.AUTO_END}\n\n## My notes\n")


def vault(tmp, files=(), attachments=(), name="v"):
    """A Vault over tmp/<name> holding these (path, text) notes in memory; attachments are created as files."""
    for rel in attachments:
        put(tmp / name / rel, "image " + rel)
    return Vault(name, tmp / name, notes=list(files), attachments=list(attachments))


# ---------------------------------------------------------------- VN-01: the software writes only its block

def vn01(tmp, marker):
    files = [("Mine/Ideas/Idea one.md", (notes.AUTO_START + "\n" if marker else "") + "My idea.\n"), ("Papers/Chen 2020.md", paper())]
    return (Store(vaults=[vault(tmp, files)]),)


CASES["VN-01"] = (lambda tmp: vn01(tmp, False), lambda tmp: vn01(tmp, True))


# ---------------------------------------------------------------- VN-02 .. VN-04

def one_note(path, text):
    return lambda tmp: (vault(tmp, [(path, text)]),)


CASES["VN-02"] = (one_note("Concepts/Apoptosis.md", node()), one_note("Concepts/Apoptosis.md", node(body="## Random thoughts\n\n- Cells were counted.\n")))
CASES["VN-03"] = (one_note("Concepts/Apoptosis.md", node()),
                  one_note("Concepts/Apoptosis.md", node(body="## Definition\n\n- Cells (S0123) were counted in group A.\n")))
CASES["VN-04"] = (one_note("Concepts/Apoptosis.md", node()),
                  one_note("Concepts/Apoptosis.md", node(body="## Definition\n\n- Cells were counted \u00e2\u20ac in group A.\n")))


# ---------------------------------------------------------------- VN-05 / VN-06: links and images

LINKS_OK = ("Concepts/Apoptosis.md", node(body="## Definition\n\n- [[Caspase]] and [[Concepts/Caspase|the caspase]] and [[Caspase#Role|role]] and [[caspase.md]].\n"
                                                    "\n| Paper | Note |\n|---|---|\n| [[Caspase\\|Caspase table]] | ![[_Attachments/p1/F1.png|640]] |\n\n[[#Description]]\n"
                                                    "\n```\n[[Not a link]]\n```\n"))
CASPASE = ("Concepts/Caspase.md", node("Caspase"))
IMAGE = "_Attachments/p1/F1.png"


def vn05(tmp, bad):
    files = [LINKS_OK, CASPASE, ("Mine/Ideas/Idea.md", "My idea about [[Nothing yet]].\n")]
    if bad:
        files.append(("Methods/PCR.md", node("PCR", body="## Definition\n\n- See [[Nowhere]] and ![[_Attachments/p1/missing.png]].\n")))
    return (vault(tmp, files, [IMAGE]),)


CASES["VN-05"] = (lambda tmp: vn05(tmp, False), lambda tmp: vn05(tmp, True))


def vn06(tmp, orphan):
    files = [("Concepts/Apoptosis.md", node(body=f"## Definition\n\n![[{IMAGE}|640]] ![[F2.png]]\n")), ("Concepts/Caspase.md", node("Caspase"))]
    return (vault(tmp, files, [IMAGE, "_Attachments/p1/F2.png"] + (["_Attachments/p1/F3.png"] if orphan else [])),)


CASES["VN-06"] = (lambda tmp: vn06(tmp, False), lambda tmp: vn06(tmp, True))


# ---------------------------------------------------------------- VN-07: citations, quotes, size

SENTENCES = ["Cells were counted in group A.", "Dishes were washed twice.", "The The"]


def sentences(paper_id):
    return SENTENCES if paper_id == "p1" else []


def quoted(quote, alias="Chen 2020, p. 3", body_extra=""):
    return node(body=f"## Definition\n\n> [!quote]- [[Chen 2020|{alias}]] \u00b7 results\n> {quote}\n{body_extra}\n"
                     f"> [!info]- Source IDs\n> Exact sentence / evidence IDs behind the citations in this note.\n> - Chen 2020, p. 3: p1:S0001, p1:S0002\n")


def vn07(tmp, quote, alias="Chen 2020, p. 3", filler=0):
    body = "x" * filler
    files = [("Papers/Chen 2020.md", paper(body=f"## Tóm tắt (Abstract)\n\n{body}\n")), ("Concepts/Apoptosis.md", quoted(quote, alias))]
    return vault(tmp, files), sentences


CASES["VN-07"] = (lambda tmp: vn07(tmp, "Cells were counted in group A. Dishes were washed twice."),
                  lambda tmp: vn07(tmp, "Cells were counted in group B."))


# ---------------------------------------------------------------- VN-08: language and local links

CASES["VN-08"] = (one_note("Concepts/Apoptosis.md", node(body="## Definition\n\n- Cells were counted in group A. ([PDF](http://127.0.0.1:8765/pdf/p1#page=3))\n")),
                  one_note("Concepts/Apoptosis.md", node(body="## Definition\n\n- Cells were counted. [dashboard](http://127.0.0.1:8765/neural?tab=note&q=a)\n")))


# ---------------------------------------------------------------- VN-09: graph settings

def graph_files(tmp, name="v", hide=True, drop_filter=None, extended=True):
    base = tmp / name / ".obsidian"
    search = notes.GRAPH_FILTER.replace(drop_filter, "") if drop_filter else notes.GRAPH_FILTER
    # a researcher group first and a recoloured machine group pass (U6g: colours are the researcher's choice)
    put(base / "graph.json", json.dumps({"search": search, "hideUnresolved": hide, "showAttachments": False, "colorGroups": [
        {"query": "tag:#mine", "color": {"a": 1, "rgb": 1}}] + [
        {"query": q, "color": {"a": 1, "rgb": 7 if i == 1 else int(c[1:], 16)}} for i, (q, c, _) in enumerate(notes.GRAPH_GROUPS)]}))
    if extended:
        put(base / "plugins" / "extended-graph" / "data.json", json.dumps({"syncDefaultState": True, "interactiveSettings": {"link": {"colors": [
            {"type": "conflicts_with", "color": "#dc2626"}, {"type": "opposes", "color": "#DC2626"},
            {"type": "supports", "color": "#15803d"}, {"type": "none", "color": "#94a3b8"}]}}}))
    return (Vault(name, tmp / name, notes=[]),)


CASES["VN-09"] = (lambda tmp: graph_files(tmp), lambda tmp: graph_files(tmp, drop_filter='-path:"Review Queue"'))


# ---------------------------------------------------------------- VN-10: retired notes

def delete_all(vault_name, live):
    """A broken retirement: deletes the notes instead of moving them."""
    from kbsys import config
    for path in (config.vault_dir(vault_name) / "Organisms").glob("*.md"):
        path.unlink()
    return []


CASES["VN-10"] = (lambda tmp: (), lambda tmp: (delete_all,))


# ---------------------------------------------------------------- RQ: the review queue

def item(number=1, key="fig:p1:F3", boxes="- [ ] Đồng ý đề xuất   - [ ] Giữ nguyên, không phải lỗi   - [ ] Khác (ghi bên dưới)", extra=""):
    return (f"### [Nhóm {number}] Chen 2020 \u2014 the caption differs\n"
            "> *nguyên văn (code chép)* Cells were counted in group A. \u2014 (Chen 2020, p. 3) \u00b7 [[Chen 2020#Figure 3]]\n"
            "**Máy phát hiện:** the caption names a group the figure lacks. **Đề xuất:** check the figure.\n"
            f"{boxes}\nGhi chú của bạn:\n<small>mã: p1:F3, p1:S0001</small> <!--k:{key}-->\n{extra}")


GROUPS = ("Sai kiến thức trong bài gốc", "Sai do AI, sửa chưa chắc", "Node hoặc bài mồ côi", "Nhận định không có nguồn")


def queue(items=None, instructions=3):
    """A queue file of the new format: `items` = {group number: item text}."""
    items = {1: item()} if items is None else items
    out = ["<!-- kb:index -->", f"# {review_digest.NOTE}", ""] + [f"Dòng hướng dẫn {n}." for n in range(1, instructions + 1)] + [""]
    for number, title in enumerate(GROUPS, 1):
        out += [f"## {number}. {title}", "", items.get(number, "*Không có.*\n")]
    return "\n".join(out) + "\n"


def good_write(con, vault_name):
    """A fixed review_digest.write: writes a queue of the new format at the vault root."""
    from kbsys import config
    (config.vault_dir(vault_name) / QUEUE).write_text(queue(), encoding="utf-8", newline="\n")


LEGACY = (f"# {review_digest.NOTE}\n\n## 1. Thẻ node do AI viết (1 thẻ)\n\n- [ ] [[Cells]] \u2014 5 bằng chứng, 2 bài (AI) <!--k:card:local_cells-->\n"
          "\n## 2. Hình/bảng (0)\n\n*Không có.*\n\n## 3. Xung đột giữa các bài (1 đang mở)\n\n- [ ] [[CONFLICT - A vs B]] <!--k:conflict:CONFLICT - A vs B-->\n")


def queue_store(tmp, text):
    return Store(vaults=[vault(tmp, [(QUEUE, text)])])


CASES["RQ-01"] = (lambda tmp: (queue_store(tmp, queue()),), lambda tmp: (queue_store(tmp, LEGACY),))
CASES["RQ-02"] = (lambda tmp: (queue_store(tmp, queue()),), lambda tmp: (queue_store(tmp, LEGACY),))


def queue_vault(tmp, text):
    return vault(tmp, [(QUEUE, text)])


SENTENCE_TEXT = {"p1:S0001": "Cells were counted in group A."}.get
CASES["RQ-03"] = (lambda tmp: (queue_vault(tmp, queue()), SENTENCE_TEXT),
                  lambda tmp: (queue_vault(tmp, queue({1: item(boxes="- [ ] Đồng ý đề xuất   - [ ] Giữ nguyên")})), SENTENCE_TEXT))


def rq05(tmp, logged):
    ticked = item(boxes="- [x] Đồng ý đề xuất   - [ ] Giữ nguyên, không phải lỗi   - [ ] Khác")
    root = tmp / "s"
    put(root / "review_decisions.jsonl", json.dumps({"key": "fig:p1:F3", "decision": "agree"}) + "\n" if logged else "")
    return (Store(root=root, vaults=[queue_vault(tmp, queue({1: ticked}))]),)


CASES["RQ-05"] = (lambda tmp: rq05(tmp, True), lambda tmp: rq05(tmp, False))


def rq06(tmp, root_file=True, first=review_digest.NOTE, pinned=True):
    home = f"---\ntype: home\n---\n# Home\n\n- [[{first}]] \u2014 everything waiting for you\n- [[Index]]\n"
    files = [("Home.md", home), (QUEUE if root_file else f"Review Queue/{QUEUE}", queue())]
    if pinned:
        put(tmp / "v" / ".obsidian" / "workspace.json", json.dumps({"main": {"children": [
            {"type": "leaf", "pinned": True, "state": {"type": "markdown", "state": {"file": QUEUE}}}]}}))
    return (Vault("v", tmp / "v", notes=files),)


CASES["RQ-06"] = (lambda tmp: rq06(tmp), lambda tmp: rq06(tmp, root_file=False))


def rq07(tmp, kind="topic_relevance", connected=False):
    con = mem_db()
    root = tmp / "s"
    put(root / "papers" / "pb" / "review_policy.json", json.dumps({"version": 1, "vaults": {"one": {"connected": connected}}}))
    con.execute("INSERT INTO review_items (paper_id, kind, detail, status) VALUES ('pb', ?, '[one] No supported connection', 'pending')", (kind,))
    return (Store(con=con, root=root),)


CASES["RQ-07"] = (lambda tmp: rq07(tmp), lambda tmp: rq07(tmp, kind="failed_pdf"))
