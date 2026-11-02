"""Saved discussions (researcher said "lưu"): `Discussions/<title>.md` notes of type `discussion`.

They link to the notes they cite, so they show in the graph, but they are never evidence: library search reads the
store, not the vault, and the neural route marks them `discussion` (context, like Mine/). An existing note is never
overwritten; a new name gets a numeric suffix.

A `decision` note (AN-17) is the researcher's "chốt": their own words, the date and the item citations shown at that time
(quotes copied from the store by code). A later decision on the same point is a new note that links the earlier one.
"""

import re
import unicodedata
from datetime import datetime
from pathlib import Path

import yaml

from . import config, notes, standard

FOLDER = "Discussions"


def protocol():
    """The one answer protocol every AI follows (config/answer_protocol.md)."""
    path = config.CONFIG_DIR / "answer_protocol.md"
    if not path.is_file():  # an install whose config folder predates the file: use the shipped copy
        path = Path(__file__).resolve().parents[2] / "config" / "answer_protocol.md"
    return path.read_text(encoding="utf-8")


MEMORY_DIR = config.ROOT / "runtime" / "brainstorm"   # AN-20: the only place brainstorm memory lives (tests patch it)


def _memory_lines(path):
    """The non-empty lines of a memory file (utf-8 with or without BOM, bad bytes replaced); a missing file has none."""
    if not path.is_file():
        return []
    text = unicodedata.normalize("NFC", path.read_bytes().decode("utf-8-sig", errors="replace"))   # CHR-01: a hand edit may be NFD
    return [line for line in text.splitlines() if line.strip()]


def _memory_path(vault, scope):
    config.require_research_vault(vault)   # a vault outside the research vaults ('../x') never reaches a path
    return MEMORY_DIR / "profile.md" if scope == "profile" else MEMORY_DIR / vault / "topics.md"


def memory(vault):
    """AN-20: the researcher's profile (shared) and the topic notes of THIS vault only; context, never evidence."""
    return {"vault": vault, "label_researcher": "ý của bạn (đã nhớ, <ngày>)", "label_ai": "ghi chú tìm kiếm của AI (<ngày>)",
            "note": "context, never evidence (AN-20)",
            "profile": _memory_lines(_memory_path(vault, "profile")), "topics": _memory_lines(_memory_path(vault, "topic"))}


def remember(vault, text, by, scope):
    """AN-20: append ONE dated line marked with its writer to the profile or the vault's topics; an earlier byte is never
    changed and a file at its line cap is refused (the researcher edits it by hand)."""
    path = _memory_path(vault, scope)
    if by not in ("researcher", "ai") or scope not in ("profile", "topic"):
        raise ValueError("by must be 'researcher' or 'ai', scope 'profile' or 'topic'")
    if scope == "profile" and by != "researcher":   # AN-20: facts about the researcher only from the researcher's words
        raise ValueError("the profile holds only what the researcher said (by='researcher'); AI search notes go to scope 'topic'")
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    text = " ".join(unicodedata.normalize("NFC", text).split())   # CHR-01
    param = lambda name: standard.param("AN-20", name)
    if not text or len(text) > param("max_line_chars"):
        raise ValueError(f"text must be one non-empty line of at most {param('max_line_chars')} characters")
    path.resolve().relative_to(MEMORY_DIR.resolve())   # ValueError when the target is outside runtime/brainstorm
    cap = param("profile_max_lines" if scope == "profile" else "topic_max_lines")
    lines = _memory_lines(path)
    if len(lines) >= cap:
        raise ValueError("đầy: gộp hoặc xóa bớt dòng cũ trước (bạn tự sửa file)")
    line = f"- {datetime.now():%Y-%m-%d} · {'bạn nói' if by == 'researcher' else 'AI ghi'}: {text}"
    path.parent.mkdir(parents=True, exist_ok=True)
    glue = "\n" if path.is_file() and path.stat().st_size and not path.read_bytes().endswith(b"\n") else ""
    with path.open("a", encoding="utf-8", newline="\n") as handle:   # append only
        handle.write(glue + line + "\n")
    return {"file": "runtime/brainstorm/" + path.resolve().relative_to(MEMORY_DIR.resolve()).as_posix(), "line": line,
            "lines_now": len(lines) + 1, "cap": cap}


KINDS = {"discussion": ("discussion", "Thảo luận đã lưu — không phải bằng chứng gốc; trích dẫn bên dưới trỏ về bài."),
         "knowledge_gap": ("knowledge_gap", "Knowledge gap do bạn và AI cùng xác định khi thảo luận: bằng chứng từ bài + "
                                            "kiến thức của AI (đã ghi rõ, không phải bằng chứng) + nhận định của bạn. "
                                            "Vắng trong vault không có nghĩa là vắng trong y văn."),
         "decision": ("decision", "Ý chốt của nhà nghiên cứu — không phải bằng chứng gốc; trích dẫn bên dưới là bằng chứng "
                                  "có trong kho lúc chốt.")}
NO_EVIDENCE_LINE = "không có bằng chứng trong kho lúc chốt"
_CHOT = re.compile(r"(?<!\w)ch(?:ốt|ot)(?!\w)")  # "chốt" or "chot", a whole word


def _is_decision(path):
    """A note file whose front matter says type decision (read as the vault index reads it)."""
    from . import neural
    try:
        return neural._front(path.read_bytes().decode("utf-8-sig", errors="replace"))[0].get("type") == "decision"
    except OSError:
        return False


def _citations(con, vault, ids):
    """Cited sentences of this vault's papers, from the store (unknown or removed ids are refused): [(id, row)]."""
    if not isinstance(ids, (list, tuple)) or not all(isinstance(i, str) for i in ids):
        raise ValueError("citations must be a list of item ids")
    ids = list(dict.fromkeys(i.strip() for i in ids if i.strip()))
    if ids and con is None:
        raise ValueError("a decision with citations needs the store connection")
    rows = []
    for sid in ids:
        row = con.execute("SELECT s.sentence_id, s.paper_id, s.page, s.section, s.text FROM sentences s "
                          "JOIN paper_vaults pv ON pv.paper_id = s.paper_id AND pv.vault = ? WHERE s.sentence_id = ?",
                          (vault, sid)).fetchone()
        if not row:
            raise ValueError(f"citation {sid!r} is not a sentence of a paper in vault {vault}")
        rows.append(dict(row))
    return rows


def _quote(text):
    return "\n".join("> " + line for line in config.lf(text).strip().split("\n"))


def _decision_body(words, markdown, rows, links, earlier):
    """The decision note body: the researcher's words, the AI's text, the earlier decision, the citations."""
    parts = ["> [!quote] Lời của bạn (nguyên văn)\n" + _quote(words), config.lf(markdown).strip()]
    if earlier:
        parts.append(f"Ý chốt trước đó về cùng điểm này: [[{earlier.removesuffix('.md')}]]")
    if not rows:
        parts.append(NO_EVIDENCE_LINE)
    for row in rows:
        link = f" — [[{links[row['paper_id']].removesuffix('.md')}]]" if links.get(row["paper_id"]) else ""
        parts.append(f"> [!cite] `{row['sentence_id']}` — bài {row['paper_id']}, mục {row['section']}, trang {row['page']}{link}\n"
                     + _quote(row["text"]))
    return "\n\n".join(parts)


def save(vault, title, markdown, model=None, kind="discussion", *, con=None, researcher_words=None, citations=None,
         earlier=None):
    """Save a note. kind decision (AN-17): `researcher_words` (verbatim, must hold "chốt"), `citations` (sentence ids of
    this vault, checked against the store through `con`; quotes are copied by code) and `earlier` (a decision note in
    Discussions/; default: the newest earlier decision of the same file name) are the decision's own arguments."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}")
    if kind != "decision" and (researcher_words or citations or earlier):
        raise ValueError("researcher_words, citations and earlier belong to kind decision only")
    try:  # research vaults only: not _Maps/Registry (mother vault), not a path
        config.require_research_vault(vault)
    except ValueError:
        raise ValueError(f"No vault {vault}") from None
    title = " ".join(unicodedata.normalize("NFC", title or "").split())  # one line: a newline would start a fake front matter in the heading
    if not title or not (markdown or "").strip():
        raise ValueError("title and markdown are required")
    vdir = config.vault_dir(vault)
    folder = vdir / FOLDER
    note_type, banner = KINDS[kind]
    front = {"type": note_type, "tags": [note_type.replace("_", "-")],
             "created": datetime.now().isoformat(timespec="seconds"), "saved_by": model or "AI", "evidence": False}
    rows = links = None
    if kind == "decision":
        if not isinstance(researcher_words, str) or not _CHOT.search(unicodedata.normalize("NFC", researcher_words).casefold()):
            raise ValueError('a decision is saved only when the researcher says "chốt": researcher_words must hold their words')
        rows = _citations(con, vault, citations or [])
        from . import neural  # lazily: neural does not import this module, but loads heavy code
        links = {pid: info["note_path"] for pid, info in neural.paper_note_links(vault, {r["paper_id"] for r in rows}).items()
                 if info["note_path"]}
        front.update(kb_kind="decision", cited_items=[r["sentence_id"] for r in rows],
                     cited_papers=sorted({r["paper_id"] for r in rows}))
        if earlier:  # a name in Discussions/ only, matched as Windows and Obsidian do (case, NFC); the real name is linked
            given = Path(str(earlier).replace("\\", "/"))
            want = unicodedata.normalize("NFC", given.name.removesuffix(".md") + ".md").casefold()
            named = next((p for p in folder.glob("*.md") if unicodedata.normalize("NFC", p.name).casefold() == want), None)
            if given.parent.as_posix().casefold() not in (".", FOLDER.casefold()) or not (named and _is_decision(named)):
                raise ValueError(f"earlier {earlier!r} is not a decision note in {FOLDER}/")
    folder.mkdir(exist_ok=True)
    name, n = notes.safe_filename(title), 1
    while True:  # "x" = create only: two saves of one title (even at the same moment) never share a file
        path = folder / (f"{name}.md" if n == 1 else f"{name} ({n}).md")
        link = None
        if kind == "decision":  # the newest earlier decision of this file name (never modified), unless one is named
            link = named if earlier else next((q for q in (folder / (f"{name}.md" if k == 1 else f"{name} ({k}).md")
                                                           for k in range(n - 1, 0, -1)) if q.is_file() and _is_decision(q)), None)
            body = _decision_body(researcher_words, markdown, rows, links, link and link.relative_to(vdir).as_posix())
            note_front = {**front, **({"earlier": link.relative_to(vdir).as_posix()} if link else {})}
        else:
            body, note_front = config.lf(markdown).strip(), front
        text = ("---\n" + yaml.safe_dump(note_front, sort_keys=False, allow_unicode=True) + "---\n"
                f"# {title}\n\n> [!note] {banner}\n\n" + body + "\n")
        try:
            with open(path, "x", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            break
        except FileExistsError:
            n += 1
    return {"saved": path.relative_to(vdir).as_posix(), "vault": vault, "kind": kind}
