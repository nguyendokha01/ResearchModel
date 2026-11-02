"""Conformance checks of the stages VN (vault notes) and RQ (review queue and human decisions) of
config/standard.yaml, see conform.py.

VN is also the VAULT LINT: machine-written notes (Papers, node folders, Results, Claims) are checked for headings, machine
IDs, broken text, links, attachments, citations and size, the graph settings and the way the software writes. `Mine/` and
`_Templates/` are human-managed: they are only checked for machine markers (VN-01), never for their content.
RQ reads the queue file `Cần duyệt.md` (`review_digest.NOTE`; found anywhere in the vault, its place is judged by RQ-06 only)
and runs `review_digest.write` / `review_policy.refresh` on synthetic data in a throw-away project.

Queue file model (docs/PLAN.md "Hàng chờ duyệt"): four `## ` groups numbered 1..4; one `### [Nhóm N] title` section per item
with a quote (`>`), a note link, `**Máy phát hiện:**`, `**Đề xuất:**`, three boxes (Đồng ý / Giữ nguyên / Khác) and a hidden
key `<!--k:...-->`. The older format (`- [ ] ... <!--k:card:...-->` lines under five sections) is still read: every such line is
an item that has no group tag and none of the parts above, so it fails RQ-01 and RQ-03 until `review_digest.write` is changed.

Not implemented / only partly checked, and why:
- RQ-04: the machine step that decides whether a claim is invented (and moves it to removed/) does not exist yet and no
  group-4 item is produced, so there is nothing to test; a check would have to invent the meaning of "undecidable".
- RQ-05: nothing applies a decision yet (no code writes review_decisions.jsonl). The check covers the log side only (a ticked
  decision box of the new item format needs a log row) and is vacuous on a queue of the old format. "The PDF is never edited"
  is IN-03 (SHA-256 of every original). "Goes through the validators" cannot be seen in data.
- RQ-02: unclear sentences and "delete / push" decisions leave no marker in the queue file; only cards, conflicts, open
  hypotheses and wrong crops are detected (by the key prefixes, link names and section names `review_digest.write` uses).
- RQ-06: "three instruction lines" is a constant of this module (INSTRUCTION_LINES); the clause has no param for it.
- VN-05: heading anchors (`[[Note#Heading]]`) are not checked, only that the note exists.
- VN-06: a whole-page caption screenshot cannot be recognised by code; duplicates (same SHA-256) are named in the line of an
  unreferenced image.
- VN-08: no language detector exists. Checked: the 127.0.0.1 dashboard links, and lines with Vietnamese-only letters in the
  generated block of node notes, Results and Claims, outside the quoted (`>`) evidence. Paper notes keep their bilingual
  headings (notes.PAPER_*_HEADING; a link anchor `[[Note#Heading|alias]]` is not judged) and list notes (kb:index) are the Vietnamese interface: neither is judged. English text
  inside a Vietnamese sentence, and Vietnamese written without tone marks, are NOT detected.
- VN-09: the Extended Graph link colours are checked only when the plugin's data.json exists in the vault.
"""
import contextlib
import functools
import hashlib
import json
import posixpath
import re
import sqlite3
import unicodedata
import urllib.parse
from pathlib import Path
from unittest import mock

from . import concept_notes, config, db, neural, notes, presentation, result_notes, review_digest, review_policy, selftest, standard
from .conform import check
from .conform_intake import _sandbox, _seed

# ---------------------------------------------------------------- helpers

FOLDERS = (*notes.NODE_FOLDERS, "Papers", "Results", "Claims")  # the folders selftest N3b lints
HUMAN_FOLDERS = ("Mine", "_Templates")
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp"}
INSTRUCTION_LINES = 3  # RQ-06: "three instruction lines at its top"
QUEUE_GROUPS = ("1", "2", "3", "4")  # RQ-01: the four groups
DECISION_BOXES = ("Đồng ý", "Giữ nguyên", "Khác")  # RQ-03: agree, keep as is, other

_COMMENT = re.compile(r"(?s)<!--.*?-->")
_CODE = re.compile(r"(?ms)^[ \t]*(```|~~~).*?^[ \t]*\1[^\n]*$|`[^`\n]*`")
_WIKI = re.compile(r"(!?)\[\[([^\[\]\n]+?)\]\]")
_MDLINK = re.compile(r"(!?)\[[^\]\n]*\]\(<?([^)\n>]+?)>?(?:\s+\"[^\"]*\")?\)")


def _folder(path):
    parts = path.split("/")
    return parts[0] if len(parts) == 2 else None


def _machine_notes(vault, folders=FOLDERS):
    """(path, text) of the notes with a kb:auto block directly inside one of the machine folders."""
    for path, text in vault.notes:
        if _folder(path) in folders and notes.AUTO_START in text:
            yield path, text


def _fm(text):
    fm, _ = notes._split_frontmatter(text)
    return fm if isinstance(fm, dict) else {}


def _auto(text):
    start, end = text.find(notes.AUTO_START), text.find(notes.AUTO_END)
    return text[start:end] if 0 <= start < end else ""


def _visible(text):
    """Text without comments and code: what a reader (and Obsidian's link parser) sees."""
    return _CODE.sub("", _COMMENT.sub("", text))


def _stems_by_fold(vault):
    out = {}
    for path, _ in vault.notes:
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        out.setdefault(stem.casefold(), set()).add(stem)
    return out


def _norm(text):
    return " ".join(unicodedata.normalize("NFC", str(text or "")).split())


# ---------------------------------------------------------------- links (VN-05, VN-06)

class Files:
    """Every file of a vault by relative path, with the Obsidian rule for resolving a link to one of them."""

    def __init__(self, vault):
        self.md = {path for path, _ in vault.notes}
        names = list(vault.attachments)
        if vault.root.is_dir():
            names += [p.relative_to(vault.root).as_posix() for p in vault.root.rglob("*")
                      if p.is_file() and p.suffix.lower() != ".md" and ".obsidian" not in p.relative_to(vault.root).parts]
        self.other = set(names)
        self.suffixes = {}  # casefolded suffix path -> vault-relative paths (md notes without .md)
        for path in self.md:
            self._add(path.removesuffix(".md"), path)
        for path in self.other:
            self._add(path, path)

    def _add(self, key, path):
        parts = key.split("/")
        for i in range(len(parts)):
            self.suffixes.setdefault("/".join(parts[i:]).casefold(), set()).add(path)

    def resolve(self, target, source=None):
        """The files a link target means ([] = broken). `source`: the note holding a markdown-style relative link."""
        t = urllib.parse.unquote(target).strip().replace("\\", "/").lstrip("/")
        found = self.suffixes.get(t.casefold()) or self.suffixes.get(t.casefold().removesuffix(".md"))
        if not found and source:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(source), t))
            found = self.suffixes.get(joined.casefold()) or self.suffixes.get(joined.casefold().removesuffix(".md"))
        return sorted(found or ())


def links(text):
    """(embed, target, kind) of every link of a note body: `[[Note#Heading|alias]]`, `![[image.png|640]]`, `[x](path)`, `![](path)`.
    Links inside code and comments do not count."""
    body = _visible(text)
    for embed, raw in _WIKI.findall(body):
        target = re.split(r"\\?\|", raw, maxsplit=1)[0]  # the alias starts at a pipe, escaped (\|) inside a table cell
        target = re.split(r"[#^]", target, maxsplit=1)[0].strip()
        if target:
            yield bool(embed), target, "wiki"
    for embed, raw in _MDLINK.findall(body):
        raw = raw.strip()
        if raw and not re.match(r"(?i)[a-z][a-z0-9+.-]*:|#", raw):  # URLs, mailto:, obsidian:, file:, same-page anchors
            yield bool(embed), raw.split("#", 1)[0], "md"


def index_missing_edges(vault):
    """(note, link text) of every edge the vault index of the web page (neural._edge_rows) gives the status `missing`. Uses the product
    functions on an in-memory copy of the notes, so it sees exactly what the index would store."""
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    try:
        con.execute("CREATE TABLE notes (path TEXT, text TEXT)")
        con.executemany("INSERT INTO notes VALUES (?, ?)", vault.notes)
        seen = {path for path, _ in vault.notes}
        others = set(vault.attachments) | (set(neural._vault_files(vault.root)) if vault.root.is_dir() else set())
        names = neural.defaultdict_names(seen | others)  # notes and attachments, as neural._refresh_locked feeds them
        for path in sorted(seen):
            for _, _, label, status, *_ in neural._edge_rows(con, path, names, seen):
                if status == "missing":
                    yield path, label
    finally:
        con.close()


@check("VN-05", "vault")
def links_resolve(vault, files=None, index=index_missing_edges):
    """Every link and embed of every note of the vault resolves to a note or a file (Obsidian: by note name, by (partial) path, by file
    name; alias, heading and block parts are ignored). Mine/ and _Templates/ are human-managed: their links are not counted.
    The clause counts the edges with status `missing` in the vault index of the web page: an edge that Obsidian resolves but the index
    calls missing (an escaped pipe in a table cell, an image embed, a link to a heading of the same note) is reported too, as an index
    defect."""
    files = files or Files(vault)
    for path, text in vault.notes:
        if path.split("/")[0] in HUMAN_FOLDERS:
            continue
        for embed, target, kind in links(text):
            if not files.resolve(target, path if kind == "md" else None):
                yield f"{path}: {'embed' if embed else 'link'} [[{target}]] does not resolve" if kind == "wiki"                     else f"{path}: {'embed' if embed else 'link'} ({target}) does not resolve"
    for path, label in index(vault):
        target = re.split(r"[#^]", re.split(r"\\?\|", label)[0], maxsplit=1)[0].strip()  # as links() reads it
        if path.split("/")[0] not in HUMAN_FOLDERS and (not target or files.resolve(target)):  # a broken link is reported above
            yield f"{path}: the vault index calls [[{label[:60]}]] missing, but Obsidian resolves it"


@check("VN-06", "vault")
def no_orphan_image(vault, files=None):
    """Every image in _Attachments is embedded or linked by a note (by full path, partial path or file name). An unreferenced image
    is reported with the files that have the same SHA-256 (a duplicate is removed with the researcher's permission, a real figure
    is attached to its object)."""
    files = files or Files(vault)
    images = [a for a in vault.attachments if Path(a).suffix.lower() in IMAGE_SUFFIXES]
    used = set()
    for path, text in vault.notes:
        for _, target, kind in links(text):
            used.update(files.resolve(target, path if kind == "md" else None))
    orphans = [a for a in images if a not in used]
    for a in orphans:
        mine = _sha(vault.root / a)  # only the files of the same folder are compared (duplicates of one paper)
        same = [b for b in images if b != a and posixpath.dirname(b) == posixpath.dirname(a) and _sha(vault.root / b) == mine]
        yield f"{a}: no note embeds or links this image" + (f" (identical file: {same[0]}{' ...' if len(same) > 1 else ''})" if same else "")


def _sha(path):
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return str(path)  # an unreadable file is never "identical" to another


# ---------------------------------------------------------------- VN-01: the software writes only its own block

def _marked(text):
    return any(m in text for m in (notes.AUTO_START, notes.AUTO_END, notes.INDEX_MARK, notes.STUB_MARK, "kb:concept-fingerprint"))


def _human_parts(text):
    """(front matter keys that are not machine keys, text before the auto block, text after it) of a paper note."""
    fm, rest = notes._split_frontmatter(text)
    start, end = rest.find(notes.AUTO_START), rest.find(notes.AUTO_END) + len(notes.AUTO_END)
    own = {k: v for k, v in (fm or {}).items() if k not in notes.MACHINE_KEYS and not str(k).startswith("kb_")
           and k not in ("tags", "source_papers", "evidence_count")}
    return own, rest[:start], rest[end:]


def probe_rebuild(rebuild=notes.refresh_all):
    """A rebuild in a throw-away project: Mine/ bytes, human text around the auto block and custom properties stay identical, a note
    whose auto markers a person damaged is left alone, and a machine property still changes (so the probe is not vacuous)."""
    with _sandbox() as (root, con):
        for pid in ("p1", "p2"):
            _seed(con, pid, ["one"], title=f"Cells in test medium {pid}", note_name=f"Note {pid}")
        vault = config.vault_dir("one")
        for pid in ("p1", "p2"):
            notes.write_paper_note(db.read_json(db.paper_dir(pid) / "metadata.json"), "one", con=con)
        p1, p2 = vault / "Papers" / "Note p1.md", vault / "Papers" / "Note p2.md"
        text = p1.read_text(encoding="utf-8")
        text = text.replace("tags:\n- paper", "tags:\n- paper\nmy_property: keep this", 1)
        text = text.replace(notes.AUTO_START, "My line before the block.\n\n" + notes.AUTO_START, 1) + "My line after the block.\n"
        p1.write_bytes(text.encode("utf-8"))
        broken = p2.read_text(encoding="utf-8").replace(notes.AUTO_END, "") + "My line in a damaged note.\n"
        p2.write_bytes(broken.encode("utf-8"))
        mine = vault / "Mine" / "Ideas" / "Idea one.md"
        mine.parent.mkdir(parents=True)
        mine.write_bytes("My idea with [[a link]].\n".encode("utf-8"))
        before = {"mine": mine.read_bytes(), "p2": p2.read_bytes(), "p1": _human_parts(p1.read_text(encoding="utf-8"))}
        meta = db.read_json(db.paper_dir("p1") / "metadata.json")
        meta["title"] = "Cells in test medium, changed"
        db.write_json(db.paper_dir("p1") / "metadata.json", meta)
        rebuild(con, "one")
        after = p1.read_text(encoding="utf-8")
        if mine.read_bytes() != before["mine"]:
            yield "a rebuild changed a Mine/ file"
        if p2.read_bytes() != before["p2"]:
            yield "a rebuild changed a note whose auto block was damaged by hand"
        if _human_parts(after) != before["p1"]:
            yield "a rebuild changed a custom property or human text outside the auto block"
        if _fm(after).get("title") != "Cells in test medium, changed":
            yield "the probe is vacuous: the rebuild did not update the machine property title"


@check("VN-01", "store")
def software_writes_only_its_block(store, rebuild=notes.refresh_all):
    """Data: kb:auto markers come as one start and one end in this order; no Mine/ or _Templates/ file carries a machine marker.
    Probe: `rebuild` (notes.refresh_all) keeps Mine/ bytes, human text and custom properties byte-identical."""
    for vault in store.vaults:
        loaded = "notes" in vault._data
        for path, text in vault.notes:
            if path.split("/")[0] in HUMAN_FOLDERS:
                if _marked(text):
                    yield f"{vault.name}/{path}: a human-managed file carries a machine marker"
            elif notes.AUTO_START in text or notes.AUTO_END in text:
                if text.count(notes.AUTO_START) != 1 or text.count(notes.AUTO_END) != 1 or text.find(notes.AUTO_START) > text.find(notes.AUTO_END):
                    yield f"{vault.name}/{path}: kb:auto markers are not one start followed by one end"
        if not loaded:
            vault._data.pop("notes", None)
    yield from probe_rebuild(rebuild)


# ---------------------------------------------------------------- VN-02: headings

PLACEHOLDER_BULLET = re.compile(r"(?m)^\s*[-*+]\s*(?:[-–—.…]*|n/?a|none|null|todo|tbd|\?)\s*$", re.I)


def _heading_problems(path, text):
    folder = _folder(path)
    block = _auto(text)
    if folder in notes.NODE_FOLDERS:
        if "<!-- kb:concept-fingerprint:" not in text:
            return []
        return concept_notes.heading_problems(text, "moc" if folder == notes.MAPS or "## Overview" in block else None)
    if folder in ("Results", "Claims"):
        return result_notes.heading_problems(text)
    return []


@check("VN-02", "vault")
def approved_headings(vault, stems=None):
    """concept_notes.heading_problems (node notes, MOC) and result_notes.heading_problems (Results, Claims): approved English sections
    in order; plus no empty heading, no empty section (selftest.note_text_problems) and no placeholder bullet in a generated block."""
    for path, text in _machine_notes(vault, (*notes.NODE_FOLDERS, "Results", "Claims")):
        for problem in _heading_problems(path, text):
            yield f"{path}: {problem}"
        block = _visible(_auto(text))
        for problem in selftest.note_text_problems(text, check_placeholders=False):
            if problem.startswith("empty section"):
                yield f"{path}: {problem}"
        if re.search(r"(?m)^#{2,6}\s*$", block):
            yield f"{path}: heading without text"
        for m in PLACEHOLDER_BULLET.finditer(block):
            yield f"{path}: placeholder bullet {m.group(0).strip()!r}"


# ---------------------------------------------------------------- VN-03 / VN-04: IDs and broken text

@check("VN-03", "vault")
def no_machine_ids_in_reading_text(vault):
    """presentation.machine_ids on every generated note (headings and body, outside the Source IDs callout), and no machine ID in a
    title: the file name or the front matter `title`."""
    for path, text in vault.notes:
        if _folder(path) not in FOLDERS:
            continue
        stem = path.rsplit("/", 1)[-1].removesuffix(".md")
        title = str(_fm(text).get("title") or "")
        for where, value in (("file name", stem), ("title", title)):
            found = presentation.MACHINE_ID.search(value)
            if found:
                yield f"{path}: machine ID {found.group(0)!r} in the {where}"
        if notes.AUTO_START in text:
            # machine_ids skips a bare ID that follows "#" (a reagent catalogue number), which also skips "### E0077 ...": headings are read here
            ids = presentation.machine_ids(text) or [m.group(0) for line in _visible(_auto(text)).splitlines()
                                                     if line.startswith("#") for m in presentation.MACHINE_ID.finditer(line.lstrip("# "))]
            if ids:
                yield f"{path}: machine IDs visible outside the Source IDs callout: {ids[:3]}"


RAW_DATA = (  # raw Python/JSON data in reading text: a dict or list of dicts, or a bare None as a value
    (re.compile(r"\{\s*['\"][A-Za-z_][\w ]*['\"]\s*:"), "raw dict or JSON"),
    (re.compile(r"\[\s*\{\s*['\"]"), "raw list of dicts"),
    (re.compile(r"(?m)(?:[:=(]\s*|\|\s*|^\s*[-*]\s+)None\s*(?:[,)|]|$)"), "bare None"),
)


def raw_data_problems(text):
    """Raw JSON / None / Python dict text in the visible auto block (comments, code and the front matter are not looked at)."""
    block = _visible(_auto(text))
    return [f"{label} {m.group(0).strip()[:30]!r}" for pattern, label in RAW_DATA for m in list(pattern.finditer(block))[:2]]


@check("VN-04", "vault")
def no_broken_text(vault):
    """selftest.note_text_problems (broken characters, placeholders, empty sections, a link that differs from a note title only by case;
    placeholders are not looked for in Claims) plus raw JSON / None / dict text."""
    stems = _stems_by_fold(vault)
    for path, text in _machine_notes(vault):
        for problem in selftest.note_text_problems(text, stems, check_placeholders=_folder(path) != "Claims") + raw_data_problems(text):
            yield f"{path}: {problem}"


# ---------------------------------------------------------------- VN-07: citations, quotes, size

CITE = re.compile(r"^[^|\[\]]+ (?:\d{4}|n\.d\.)(?:, pp?\. [^|\[\]]+)?$")  # presentation.cite_label: "Author Year, p. N"
_BLOCK = re.compile(r"(?m)(?:^>.*(?:\n|\Z))+")
_PAPER_PAGE_QUOTE = re.compile(r"(?m)^- pp?\. [^\n]*\n((?:>[^\n]*\n?)+)")  # "- p. 3" then the quoted sentences of a paper note
_PAPER_LINK = re.compile(r"\[\[([^\[\]|]+)\|([^\[\]]+)\]\]")


def _paper_stems(vault):
    return {path.rsplit("/", 1)[-1].removesuffix(".md").casefold() for path, _ in vault.notes if _folder(path) == "Papers"}


def source_ids(text):
    """label -> sentence IDs of the note's Source IDs callout."""
    out, on = {}, False
    for line in text.splitlines():
        if line.startswith(f"> [!info]- {presentation.SOURCE_IDS_TITLE}"):
            on = True
        elif on and line.startswith("> - "):
            label, _, ids = line[4:].rpartition(": ")
            out.setdefault(label.strip(), []).extend(i.strip() for i in ids.split(",") if i.strip())
        elif on and not line.startswith(">"):
            on = False
    return out


_QUOTE_HEAD = (re.compile(r"\[!quote\][+-]?\s*\[\[[^|\]]+\|([^\]]+)\]\]"), re.compile(r"\*\*\[\[[^|\]]+\|([^\]]+)\]\]\*\*"))
_NOTICE = re.compile(r"(?i)^(?:preceding-page source fragment|⚠)")  # machine remarks inside a quote row, not quoted text


def quote_rows(text):
    """(citation alias, quoted text) of the quote rows of the generated block: `> [!quote]- [[Paper|Author Year, p. N]] · section` (node notes)
    and `> **[[Paper|Author Year, p. N]]**` (Results, Claims); the quote is the `>` lines up to the next row, minus machine remarks."""
    for block in _BLOCK.findall(_auto(text)):
        lines = [line[1:].strip() for line in block.splitlines()]
        heads = [(i, m.group(1)) for i, line in enumerate(lines) for m in [next(filter(None, (h.match(line) for h in _QUOTE_HEAD)), None)] if m]
        for (i, alias), nxt in zip(heads, [h[0] for h in heads[1:]] + [len(lines)]):
            quote = [x for x in lines[i + 1:nxt] if x and not x.startswith(("[!", ">")) and not _NOTICE.match(x)]
            if quote:
                yield alias, " ".join(quote)


def _is_copy(quote, sentences):
    """True when `quote` (a leading "[…] " marker removed) is made only of whole sentences of the paper, in order, joined by a space (the
    writer may skip a sentence between two quoted ones, never cut or edit one). A fragment such as "The" is also a sentence, so every
    candidate is tried."""
    q = re.sub(r"^\[…\]\s*", "", _norm(quote))

    @functools.lru_cache(maxsize=None)
    def fits(start, pos):  # q[start:] from sentence number `pos` on
        if start >= len(q):
            return True
        return any(sentences[i] and q.startswith(sentences[i], start) and q[start + len(sentences[i]):start + len(sentences[i]) + 1] in ("", " ")
                   and fits(start + len(sentences[i]) + 1, i + 1) for i in range(pos, len(sentences)))
    return fits(0, 0)


def _default_sentences(con):
    @functools.lru_cache(maxsize=4)
    def load(paper_id):
        return tuple(_norm(r[0]) for r in con.execute("SELECT text FROM sentences WHERE paper_id = ? ORDER BY seq", (paper_id,)))
    return load


@check("VN-07", "vault")
def citations_quotes_and_size(vault, sentences=None):
    """Citations: the alias of a quote's paper link reads `Author Year, p. N` and the link resolves to a paper note. Quotes: the quoted
    text of every quote row equals one sentence (or consecutive sentences) of the paper named in the note's Source IDs callout,
    copied from the database (`sentences(paper_id)` = normalised texts in order). Size: paper notes and node notes stay under
    the caps of the clause (standard.param) and agree with the caps the writer uses."""
    papers = _paper_stems(vault)
    sentences = sentences or (_default_sentences(vault.con) if vault.con is not None else None)
    caps = {"Papers": standard.param("VN-07", "paper_note_kib") * 1024}
    node_cap = standard.param("VN-07", "node_note_kib") * 1024
    if caps["Papers"] != notes.PAPER_NOTE_MAX_BYTES or node_cap != notes.NODE_NOTE_MAX_BYTES:
        yield "the size caps of VN-07 differ from notes.PAPER_NOTE_MAX_BYTES / NODE_NOTE_MAX_BYTES"
    for path, text in _machine_notes(vault):
        size, cap = len(text.encode("utf-8")), caps.get(_folder(path), node_cap)
        if size > cap:
            yield f"{path}: {size} bytes is over the cap of {cap}"
        ids = source_ids(text)
        for alias, quote in quote_rows(text):
            if not CITE.match(alias):
                yield f"{path}: citation alias {alias!r} is not 'Author Year, p. N'"
            if sentences is None:
                continue
            who = alias.split(", p")[0]  # the callout groups pages ("Author Year, pp. 9, 10"): match on Author Year
            paper_ids = {i.rsplit(":", 1)[0] for label, group in ids.items() if label.split(", p")[0] == who for i in group if ":" in i}
            if not paper_ids:
                yield f"{path}: citation {alias!r} has no ID in the Source IDs callout"
            elif not any(_is_copy(quote, list(sentences(p))) for p in paper_ids):
                yield f"{path}: the quote under {alias!r} is not the text of the cited sentences: {quote[:50]!r}"
        pid = _fm(text).get("paper_id")
        if sentences is not None and _folder(path) == "Papers" and isinstance(pid, str):  # "- p. 3" followed by the quoted sentences
            for m in _PAPER_PAGE_QUOTE.finditer(_auto(text)):
                quote = " ".join(x[1:].strip() for x in m.group(1).splitlines() if x[1:].strip() and not x[1:].strip().startswith("[!"))
                if quote and not _is_copy(quote, list(sentences(pid))):
                    yield f"{path}: a quoted sentence is not the text of the paper's sentences: {quote[:50]!r}"
        for target, alias in _PAPER_LINK.findall(_visible(_auto(text))):
            if target.strip().casefold() in papers and re.search(r", pp?\.", alias) and not CITE.match(alias):
                yield f"{path}: citation alias {alias!r} is not 'Author Year, p. N'"


# ---------------------------------------------------------------- VN-08: language and local links

# Letters that only Vietnamese uses (not é, à, ô, ê ... of French or Spanish names): a, o, u with a hook or breve, d with a stroke, and the
# Latin Extended Additional block of tone marks (U+1EA0..U+1EF9).
VIETNAMESE = re.compile("[ăơưđĂƠƯĐĩũĨŨẠ-ỹ]")
LOCAL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?(/[^\s)\]>]*)?", re.I)
_WIKI_ANCHOR = re.compile(r"(\[\[[^\[\]|#]*)#(?:" + "|".join(map(re.escape, (notes.PAPER_RESULTS_HEADING, notes.PAPER_FIGURES_HEADING, notes.PAPER_STRUCTURE_HEADING))) + r")(?=\|)")
MACHINE_ENGLISH = tuple(f for f in FOLDERS if f != "Papers")  # node notes, Results, Claims: English headings by CONTRACTS 8.11


@check("VN-08", "vault")
def one_language_and_no_local_links(vault):
    """No link to the local dashboard (127.0.0.1 / localhost, except a /pdf/ page link) in note text; no line with Vietnamese-only letters
    in the generated block of a node note, Result or Claim outside the quoted (`>`) evidence. No language detector exists: see the
    module notes for what is not checked."""
    for path, text in vault.notes:
        if path.split("/")[0] in HUMAN_FOLDERS:
            continue
        body = _COMMENT.sub("", notes._split_frontmatter(text)[1] if isinstance(notes._split_frontmatter(text)[0], dict) else text)
        urls = [m for m in LOCAL_URL.finditer(body) if not (m.group(1) or "").startswith("/pdf/")]
        if urls:
            yield f"{path}: link to the local dashboard {urls[0].group(0)[:60]!r}" + (f" (+{len(urls) - 1})" if len(urls) > 1 else "")
    for path, text in _machine_notes(vault, MACHINE_ENGLISH):
        if notes.INDEX_MARK in text:  # list and navigation notes are the Vietnamese interface, not canonical text
            continue
        # only the paper note's own bilingual heading as an aliased anchor ([[Note#Heading|alias]]) is exempt; other text after # is judged
        mixed = [line for line in _visible(_auto(text)).splitlines()
                 if not line.startswith(">") and VIETNAMESE.search(_WIKI_ANCHOR.sub(lambda m: m.group(1), line))]
        if mixed:
            yield f"{path}: {len(mixed)} machine-written lines mix Vietnamese into the English text, e.g. {mixed[0].strip()[:60]!r}"


# ---------------------------------------------------------------- VN-09: graph

def _tokens(search):
    return re.findall(r'-?[\w:#\[\]]*"[^"]*"|\S+', search or "")


@check("VN-09", "vault")
def graph_settings(vault):
    """`.obsidian/graph.json`: the machine colour group queries (notes.GRAPH_GROUPS) appear in order (researcher groups may come
    before or between them; colours are the researcher's choice and not checked), the default filter holds every
    token of notes.GRAPH_FILTER (navigation, stubs, Review Queue, _Bases, _Templates, _Attachments, Home), hideUnresolved is on and
    showAttachments off. With the Extended Graph plugin: syncDefaultState is on (notes.sync_extended_graph), conflicts_with and
    opposes use the conflict colour, supports another one."""
    path = vault.root / ".obsidian" / "graph.json"
    if not path.is_file():
        yield ".obsidian/graph.json is missing"
        return
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as err:
        yield f".obsidian/graph.json is not valid JSON ({err})"
        return
    if not isinstance(settings, dict):
        yield ".obsidian/graph.json is not a JSON object"
        return
    missing = notes.missing_graph_groups(settings.get("colorGroups"))
    if missing:
        yield f"graph.json colour groups lack notes.GRAPH_GROUPS queries in order: {missing}"
    present = set(_tokens(settings.get("search")))
    for token in _tokens(notes.GRAPH_FILTER):
        if token not in present:
            yield f"graph.json filter lacks {token}"
    if settings.get("hideUnresolved") is not True:
        yield "graph.json: hideUnresolved is not true"
    if settings.get("showAttachments") is not False:
        yield "graph.json: showAttachments is not false"
    plugin = vault.root / ".obsidian" / "plugins" / "extended-graph" / "data.json"
    if plugin.is_file():
        try:
            data = json.loads(plugin.read_text(encoding="utf-8"))
        except ValueError:
            yield "extended-graph/data.json is not valid JSON"
            return
        if not isinstance(data, dict):
            yield "extended-graph/data.json is not a JSON object"
            return
        if data.get("syncDefaultState") is not True:
            yield "extended-graph: syncDefaultState is off (its saved Default state replaces graph.json colours and filter)"
        colours = {c.get("type"): str(c.get("color")).casefold() for c in (((data.get("interactiveSettings") or {}).get("link") or {}).get("colors") or [])}
        red = next(c for q, c, _ in notes.GRAPH_GROUPS if "CONFLICT" in q).casefold()
        for kind in ("conflicts_with", "opposes"):
            if colours.get(kind) != red:
                yield f"extended-graph: link type {kind} is not the conflict colour {red}"
        if not colours.get("supports") or colours["supports"] in (red, colours.get("none")):
            yield "extended-graph: link type supports has no colour of its own"


# ---------------------------------------------------------------- VN-10: retired notes

_FP = f"<!-- kb:concept-fingerprint:{'0' * 40} -->"


def probe_retire(retire=concept_notes._retire_orphans):
    """In a throw-away project: a retired machine note moves to knowledge_store/removed/ with its bytes, one with human text stays with
    the warning block and its text, and a note that is still live is not touched."""
    machine = f"---\ntype: organism\n---\n# {{}}\n\n{notes.AUTO_START}\n{_FP}\n\n## Description\n\n- Cells were counted.\n{notes.AUTO_END}\n\n## My notes\n"
    with _sandbox() as (root, con):
        folder = config.vault_dir("one") / "Organisms"
        folder.mkdir()
        files = {"Old machine": machine.format("Old machine"), "Old human": machine.format("Old human") + "My own line.\n",
                 "Live": machine.format("Live")}
        for name, text in files.items():
            (folder / f"{name}.md").write_bytes(text.encode("utf-8"))
        retire("one", {("Organisms", "live")})
        removed = [p for p in (config.STORE / "removed").rglob("*.md")] if (config.STORE / "removed").is_dir() else []
        if (folder / "Old machine.md").exists():
            yield "a retired machine note stays in the vault"
        if not [p for p in removed if p.name == "Old machine.md" and p.read_bytes() == files["Old machine"].encode("utf-8")]:
            yield "a retired machine note is not in knowledge_store/removed/ with its original bytes (deleted?)"
        kept = folder / "Old human.md"
        if not kept.is_file() or "My own line." not in kept.read_text(encoding="utf-8"):
            yield "a retired note with human text lost the text or was moved"
        elif "[!warning]" not in kept.read_text(encoding="utf-8"):
            yield "a retired note with human text has no warning block"
        live = folder / "Live.md"
        if not live.is_file() or live.read_bytes() != files["Live"].encode("utf-8"):
            yield "a live note was deleted or changed by the retirement"


@check("VN-10", "probe")
def probe_retired_notes(retire=concept_notes._retire_orphans):
    yield from probe_retire(retire)


# ---------------------------------------------------------------- RQ: the review queue

def queue_files(vault):
    """[(path, text)] of the queue note `Cần duyệt` anywhere in the vault."""
    return [(p, t) for p, t in vault.notes if p.rsplit("/", 1)[-1] == f"{review_digest.NOTE}.md"]


_KEY = re.compile(r"<!--k:(.+?)-->")


def parse_queue(text):
    """-> (groups, items). groups: [(number or None, title)] of the `## ` headings; items: dicts with `tag` (the N of `[Nhóm N]` or None),
    `group` (the N of the enclosing `## ` heading), `title`, `body`, `key`, `legacy` (a `- [ ] ... <!--k:key-->` line)."""
    groups, items, group, current = [], [], None, None
    for line in _COMMENT.sub(lambda m: m.group(0) if "<!--k:" in m.group(0) else "", text).splitlines():
        h2, h3 = re.match(r"## (.+?)\s*$", line), re.match(r"### (.+?)\s*$", line)
        legacy = re.match(r"- \[[ xX]\] .*<!--k:(.+?)-->\s*$", line)
        if h2:
            number = re.match(r"(?:Nhóm\s+)?(\d+)\b", h2.group(1))
            group = number.group(1) if number else None
            groups.append((group, h2.group(1)))
            current = None
        elif h3:
            tag = re.match(r"\[Nhóm\s+(\d+)\]\s*(.*)", h3.group(1))
            current = {"tag": tag.group(1) if tag else None, "group": group, "title": tag.group(2) if tag else h3.group(1),
                       "body": "", "key": None, "legacy": False}
            items.append(current)
        elif legacy:
            items.append({"tag": None, "group": group, "title": line[6:80], "body": line, "key": legacy.group(1), "legacy": True})
            current = None
        elif current is not None:
            current["body"] += line + "\n"
            key = _KEY.search(line)
            current["key"] = current["key"] or (key.group(1) if key else None)
    return groups, items


def queue_group_problems(text):
    """RQ-01 on one queue file."""
    groups, items = parse_queue(text)
    numbers = [g for g, _ in groups]
    if sorted(numbers, key=str) != list(QUEUE_GROUPS) or None in numbers:
        yield f"the queue has {len(groups)} groups ({', '.join(t[:30] for _, t in groups)}), not exactly the four numbered {', '.join(QUEUE_GROUPS)}"
    for item in items:
        if item["tag"] not in QUEUE_GROUPS:
            yield f"item {item['title'][:50]!r} has no group tag [Nhóm 1..4]"
        elif item["tag"] != item["group"]:
            yield f"item {item['title'][:50]!r} is tagged group {item['tag']} but sits under group {item['group']}"


FORBIDDEN_KEYS = {"card": "an AI node card", "conflict": "a conflict", "crop": "a wrong crop"}  # review_digest.write key prefixes
FORBIDDEN_LINKS = ((re.compile(r"\[\[(?:CONFLICT - |Open conflicts)"), "a conflict"), (re.compile(r"\[\[Open hypotheses"), "an open hypothesis"))
FORBIDDEN_HEADINGS = ((re.compile(r"(?i)xung đột|conflict"), "a conflict"), (re.compile(r"(?i)giả thuyết|hypothes"), "an open hypothesis"),
                      (re.compile(r"(?i)thẻ node|node card|thẻ AI"), "an AI node card"), (re.compile(r"(?i)ảnh cắt sai|wrong crop"), "a wrong crop"))


def queue_forbidden(text):
    """RQ-02 on one queue file: kinds that never enter the queue."""
    groups, items = parse_queue(text)
    for _, title in groups:
        for pattern, kind in FORBIDDEN_HEADINGS:
            if pattern.search(title):
                yield f"section {title[:40]!r} lists {kind}"
    for line in text.splitlines():
        if re.match(r"#{3} (?!\[Nhóm)", line):  # a tagged item is judged by its key and links: its title is the paper's words
            for pattern, kind in FORBIDDEN_HEADINGS:
                if pattern.search(line):
                    yield f"section {line[4:44]!r} lists {kind}"
    for item in items:
        kind = FORBIDDEN_KEYS.get((item["key"] or "").split(":")[0])
        if kind:
            yield f"item {item['key']} is {kind}"
        else:
            for pattern, what in FORBIDDEN_LINKS:
                if pattern.search(item["body"]) or pattern.search(item["title"]):
                    yield f"item {item['title'][:50]!r} is {what}"


def _queue_data(store, problems):
    for vault in store.vaults:
        loaded = "notes" in vault._data
        found = queue_files(vault)
        if not found:
            yield f"{vault.name}: no {review_digest.NOTE}.md in the vault"
        for path, text in found:
            yield from (f"{vault.name}/{path}: {p}" for p in problems(text))
        if not loaded:
            vault._data.pop("notes", None)


def _synthetic_queue(write):
    """The queue file `write` (review_digest.write) produces for synthetic data holding one of each kind of item the old queue listed."""
    with _sandbox(vaults=("one",)) as (root, con):
        _seed(con, "p1", ["one"], note_name="Note p1")
        vdir = config.vault_dir("one")
        db.write_json(config.STORE / "node_cards" / "one" / "local_cells.json",
                      {"status": "accepted", "card": {"name": "Cells"}, "unit_ids": ["p1:E0001"], "paper_ids": ["p1"], "analyst": "AI"})
        (vdir / "Claims").mkdir()
        (vdir / "Claims" / "CONFLICT - A vs B.md").write_text("---\ntype: conflict\nstatus: open\n---\n# CONFLICT - A vs B\n", encoding="utf-8", newline="\n")
        readings = db.paper_dir("p1") / "figure_readings"
        db.write_json(readings / "F1.json", {"label": "Figure 1", "crop_wrong": True, "note": "title strip only"})
        db.write_json(readings / "F2.json", {"label": "Figure 2", "caption_consistency": [{"status": "inconsistent", "note": "caption differs"}]})
        db.write_jsonl(config.STORE / "gap_candidates.jsonl", [{"vault": "one", "status": "needs_review", "gap_type": "unexplained_hypothesis",
                                                                "description": "An open hypothesis", "gap_id": "g1"}])
        with mock.patch.object(config, "require_research_vault", lambda v: v):
            write(con, "one")
        return [p.read_text(encoding="utf-8") for p in vdir.rglob(f"{review_digest.NOTE}.md")]


@check("RQ-01", "store")
def queue_has_four_groups(store, write=review_digest.write):
    """Data: the queue file of every vault has the four numbered groups and every item is tagged with one. Probe: the file `write`
    (review_digest.write) produces for synthetic cards, a conflict, a wrong crop, a caption mismatch and an open hypothesis."""
    yield from _queue_data(store, queue_group_problems)
    for text in _synthetic_queue(write):
        yield from (f"probe: {p}" for p in queue_group_problems(text))


@check("RQ-02", "store")
def queue_excludes_other_kinds(store, write=review_digest.write):
    """Data and probe as RQ-01: no AI card, conflict, open hypothesis or wrong crop in the queue."""
    yield from _queue_data(store, queue_forbidden)
    for text in _synthetic_queue(write):
        yield from (f"probe: {p}" for p in queue_forbidden(text))


def _sentence_text(vault):
    return (lambda sid: _norm(next((r[0] for r in vault.con.execute("SELECT text FROM sentences WHERE sentence_id = ?", (sid,))), ""))) \
        if vault.con is not None else None


@check("RQ-03", "vault")
def queue_items_are_self_contained(vault, sentence_text=None):
    """Every item (new format or old) has a quote, a note link, 'Máy phát hiện', 'Đề xuất' and the three boxes; when the item lists
    sentence IDs (<small>mã: ...</small>) the quote holds the text of one of them (copied by code)."""
    sentence_text = sentence_text or _sentence_text(vault)
    for path, text in queue_files(vault):
        for item in parse_queue(text)[1]:
            body = item["body"]
            parts = {"quote": re.search(r"(?m)^\s*>", body), "link": "[[" in body or "[[" in item["title"],
                     "finding": "**Máy phát hiện:**" in body, "proposal": "**Đề xuất:**" in body}
            missing = [k for k, v in parts.items() if not v] + [f"box {b!r}" for b in DECISION_BOXES
                                                                 if not re.search(rf"\[[ xX]\]\s*{b}", body)]
            if missing:
                yield f"{path}: item {(item['key'] or item['title'])[:45]!r} lacks {', '.join(missing)}"
            elif sentence_text:
                ids = re.findall(r"[\w-]+:S\d{4,}", body)
                quote = _norm(" ".join(re.findall(r"(?m)^\s*>\s?(.*)$", body)))
                if ids and not any(t and t in quote for t in map(sentence_text, ids)):
                    yield f"{path}: item {(item['key'] or item['title'])[:45]!r}: the quote is not the text of {ids[0]}"


@check("RQ-05", "store")
def decisions_are_logged(store):
    """A ticked decision box (new item format) has a row with its key in knowledge_store/review_decisions.jsonl; every row names a key
    and a decision. Applying through the validators and the unchanged PDF (IN-03) cannot be seen in data."""
    logged = list(db.read_jsonl(store.root / "review_decisions.jsonl"))
    keys = {r.get("key") for r in logged}
    for i, row in enumerate(logged, 1):
        if not row.get("key") or not row.get("decision"):
            yield f"review_decisions.jsonl row {i} has no key or no decision"
    for vault in store.vaults:
        loaded = "notes" in vault._data
        for path, text in queue_files(vault):
            for item in parse_queue(text)[1]:
                ticked = re.search(rf"\[[xX]\]\s*(?:{'|'.join(DECISION_BOXES)})", item["body"])
                if ticked and not item["legacy"] and item["key"] not in keys:
                    yield f"{vault.name}/{path}: item {item['key']!r} has a ticked decision and no row in review_decisions.jsonl"
        if not loaded:
            vault._data.pop("notes", None)


def _json_text(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _nodes(data):
    """Every dict inside a parsed JSON value."""
    if isinstance(data, dict):
        yield data
        data = list(data.values())
    if isinstance(data, list):
        for item in data:
            yield from _nodes(item)


def _pinned(workspace, name):
    """A pinned tab (Obsidian: a leaf with "pinned": true) that shows the note `name`."""
    return any(leaf.get("pinned") is True and any(n.get("file") == name for n in _nodes(leaf)) for leaf in _nodes(workspace))


def _bookmarked(bookmarks, name):
    return any(n.get("type") == "file" and n.get("path") == name for n in _nodes(bookmarks))


@check("RQ-06", "vault")
def queue_is_easy_to_find(vault):
    """`Cần duyệt.md` at the vault root; the first link of Home.md is [[Cần duyệt]]; pinned (a pinned tab in .obsidian/workspace.json or a
    bookmark); three instruction lines between the title and the first group."""
    name = f"{review_digest.NOTE}.md"
    found = queue_files(vault)
    if not found:
        yield f"no {name} in the vault"
        return
    if name not in [p for p, _ in found]:
        yield f"{name} is in {found[0][0].rsplit('/', 1)[0]}/, not at the vault root"
    home = dict(vault.notes).get("Home.md")
    first = next((m for _, m, _ in links(home)), None) if home else None
    if first is None or first.casefold() != review_digest.NOTE.casefold():
        yield f"the first link of Home.md is [[{first}]], not [[{review_digest.NOTE}]]"
    if not (_pinned(_json_text(vault.root / ".obsidian/workspace.json"), found[0][0]) or _bookmarked(_json_text(vault.root / ".obsidian/bookmarks.json"), found[0][0])):
        yield f"{name} is not pinned (no pinned tab in workspace.json, no bookmark)"
    text = found[0][1]
    head = re.split(r"(?m)^## ", _COMMENT.sub("", notes._split_frontmatter(text)[1]), maxsplit=1)[0]
    lines = [x for x in head.splitlines()[1:] if x.strip() and not x.startswith("#")]  # after the H1
    if len(lines) != INSTRUCTION_LINES:
        yield f"{name} has {len(lines)} instruction lines before the first group, expected {INSTRUCTION_LINES}"


def probe_refresh(refresh=review_policy.refresh):
    """review_policy.refresh(apply) in a throw-away project: a paper with no source-backed connection gets one topic_relevance item, a
    connected one none, a paper with a stored human decision is not flagged again, its stored classification is not changed and no
    paper is purged."""
    topic = "Oocyte vitrification"
    with _sandbox(vaults=("one",)) as (root, con):
        (config.vault_dir("one") / "Home.md").write_text(f"# {topic}\n\nVault riêng cho chủ đề nghiên cứu này.\n", encoding="utf-8", newline="\n")
        _seed(con, "pa", ["one"], title=f"{topic} in mice", note_name="Note pa")
        _seed(con, "pb", ["one"], title="Kidney stones in rats", note_name="Note pb")
        _seed(con, "pc", ["one"], title="Bone density in sheep", note_name="Note pc", classification="CORE")
        item = db.add_review_item(con, review_policy.KIND, "[one] decided earlier", "pc")
        con.execute("UPDATE review_items SET status = 'resolved' WHERE item_id = ?", (item,))
        con.commit()
        before = {p: (db.paper_dir(p) / "metadata.json").read_bytes() for p in ("pa", "pb", "pc")}
        refresh(con, "one", apply=True)
        flagged = {r["paper_id"] for r in con.execute("SELECT paper_id FROM review_items WHERE status = 'pending'")}
        kinds = {r["kind"] for r in con.execute("SELECT kind FROM review_items WHERE status = 'pending'")}
        n = con.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
        after = {p: (db.paper_dir(p) / "metadata.json").read_bytes() for p in ("pa", "pb", "pc") if (db.paper_dir(p) / "metadata.json").is_file()}
    if flagged != {"pb"}:
        yield f"probe: flagged papers {sorted(flagged)}, expected only the paper without a connection (pb)"
    if kinds - {review_policy.KIND}:
        yield f"probe: pending flags of kind {sorted(kinds - {review_policy.KIND})}, expected {review_policy.KIND} only"
    if before != after:
        yield "probe: a stored paper record (human classification) changed"
    if n != 3 or len(after) != 3:
        yield f"probe: {n} papers and {len(after)} paper records after the refresh, expected 3 (a paper was purged)"


@check("RQ-07", "store")
def flags_follow_the_connection_rule(store, refresh=review_policy.refresh):
    """Data: every pending review item is a topic_relevance flag, and its paper's review_policy.json says 'not connected' for that vault.
    Probe: `refresh` flags only the unconnected paper, keeps a stored human decision and classification, purges nothing."""
    for row in store.con.execute("SELECT item_id, paper_id, kind, detail FROM review_items WHERE status = 'pending'"):
        if row["kind"] != review_policy.KIND:
            yield f"pending review item {row['item_id']} is of kind {row['kind']!r}, not {review_policy.KIND}"
            continue
        vault = re.match(r"\[([^\]]+)\] ", row["detail"] or "")
        policy = (_json_text(store.root / "papers" / str(row["paper_id"]) / "review_policy.json").get("vaults") or {}).get(vault.group(1) if vault else "")
        if policy and policy.get("connected"):
            yield f"pending item {row['item_id']} flags {row['paper_id']}, which review_policy.json says is connected to {vault.group(1)}"
    yield from probe_refresh(refresh)
