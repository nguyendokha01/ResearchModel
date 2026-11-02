"""PASS / FAIL cases of the stages IN (intake) and CHR (characters), see scripts/kbsys/conform_intake.py."""
import atexit
import hashlib
import json
import sqlite3
import unicodedata
from pathlib import Path

from kbsys import answer, db
from kbsys.conform import Paper, Store, Vault

_DB = []
NFD = lambda s: unicodedata.normalize("NFD", s)


def papers_db(*rows):
    """The in-memory library (one connection, closed at exit) holding exactly these papers (paper_id, doi, pmid, norm_title, year)."""
    if not _DB:
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(db.SCHEMA)
        atexit.register(con.close)
        _DB.append(con)
    con = _DB[0]
    con.execute("DELETE FROM papers")
    con.executemany("INSERT INTO papers (paper_id, doi, pmid, norm_title, year) VALUES (?,?,?,?,?)", rows)
    return con


def put(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))
    return path


def store(tmp, sentences=(), passages=(), notes=(), files=None):
    """A Store over `tmp/s` with one paper p1 (sentences/passages as given) and one vault v with `notes`."""
    root = tmp / "s"
    put(root / "papers" / "p1" / "sentences.jsonl", "".join(json.dumps(s) + "\n" for s in sentences))
    put(root / "papers" / "p1" / "passages.jsonl", "".join(json.dumps(s) + "\n" for s in passages))
    for name, data in (files or {}).items():
        put(root / name, data)
    return Store(root=root, paper_ids=["p1"], vaults=[Vault("v", tmp / "v", notes=list(notes))])


def sentence(text):
    return {"sentence_id": "p1:S0001", "text": text}


def file_with_sha(tmp, data=b"%PDF-1.4 x"):
    path = put(tmp / "a.pdf", data)
    return path, hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- IN

def in01_pass(tmp):
    return (Store(con=papers_db(("a", "10.1/a", "1", "x" * 30, 2020), ("b", "10.1/b", "2", "y" * 30, 2020))),)


def in01_fail(tmp):
    return (Store(con=papers_db(("a", "10.1/A", "1", "x" * 30, 2020), ("b", "10.1/a", "2", "y" * 30, 2020))),)


def in01_fail_probe(tmp):
    return in01_pass(tmp) + (lambda con, info: (None, None),)


def in03_pass(tmp):
    path, sha = file_with_sha(tmp)
    return (Paper("p", meta={"source_files": [
        {"path": str(path), "sha256": sha, "status": "active"},
        {"path": str(tmp / "gone.pdf"), "sha256": "0" * 64, "status": "archived"}]}),)


def in03_fail(tmp):
    path, sha = file_with_sha(tmp)
    return (Paper("p", meta={"source_files": [{"path": str(path), "sha256": "0" * 64, "status": "active"}]}),)


def fake_check(*messages):
    def run(report, con, paper_id):
        for where, message in messages:
            report.error(where, message)
    return run


def in05_pass(tmp):
    return Store(paper_ids=["p1"]), fake_check(("p1:S0001", "section 'x'"))


def in05_fail(tmp):
    return Store(paper_ids=["p1"]), fake_check(("p1", "I2: seq must be 1..n"))


def in06_pass(tmp):
    return (Paper("p", meta={"tier1": {"from": "passages_only"},
                             "source_files": [{"path": str(tmp / "gone.pdf"), "status": "unavailable", "role": "main"}]}),)


def in06_fail(tmp):
    return (Paper("p", meta={"tier1": {"from": "pdf"},
                             "source_files": [{"path": str(tmp / "gone.pdf"), "status": "active", "role": "main"}]}),)


def in07_pass(tmp):
    return (Paper("p", meta={"source_files": [{"path": "a.pdf", "sha256": "0" * 64, "status": "active"},
                                              {"sha256": "0" * 64, "status": "archived"}]}),)


def in07_fail(tmp):
    return (Paper("p", meta={"source_files": [{"path": "a.pdf", "status": "active"}]}),)


def in09_fail(tmp):
    def move(con, cfg, paper_id, path):
        Path(path).unlink()  # "attach" by moving the original away
    return (move,)


def in10_fail(tmp):
    return (lambda cfg, con, pdf, batch, vault: ("new", "p9"),)


def in02_fail(tmp):
    return (lambda con, paper_id, vault: None,)


CASES = {
    "IN-01": (in01_pass, lambda tmp: in01_fail(tmp)),
    "IN-02": (lambda tmp: (), in02_fail),
    "IN-03": (in03_pass, in03_fail),
    "IN-05": (in05_pass, in05_fail),
    "IN-06": (in06_pass, in06_fail),
    "IN-07": (in07_pass, in07_fail),
    "IN-08": (lambda tmp: (), lambda tmp: (lambda ref: "ghost:x",)),
    "IN-09": (lambda tmp: (), in09_fail),
    "IN-10": (lambda tmp: (), in10_fail),
}


# ---------------------------------------------------------------- CHR

def good_judge(sentence_, items, cards):
    """The answer checker as it should be: it normalises to NFC first."""
    return answer.check_sentence({**sentence_, "text": unicodedata.normalize("NFC", sentence_["text"])}, items, cards)


def chr01_pass(tmp):
    return (store(tmp, sentences=[sentence("Tế bào")], passages=[{"passage_id": "p1:0001", "text": "Tế bào"}],
                  notes=[("a.md", "Tế bào")]),)


def chr01_fail(tmp):  # data half only: the judge is the fixed one
    return store(tmp, sentences=[sentence(NFD("Tế bào"))], notes=[("a.md", NFD("Tế"))]), good_judge


def clean_code(tmp):
    put(tmp / "scripts" / "ok.py", 'import re\nA = re.compile(r"\\bword\\b")\nB = "\\x01\\n"\n')
    return tmp


def chr02_pass(tmp):
    return store(tmp, sentences=[sentence("Cells were counted.")], notes=[("a.md", "A note.")]), clean_code(tmp)


def chr02_fail(tmp):
    return store(tmp, sentences=[sentence("Cells\x08 were counted.")], notes=[("a.md", "A note.")]), clean_code(tmp)


def chr03_pass(tmp):
    put(tmp / "scripts" / "ok.py", "x = 1\n")
    put(tmp / "run.bat", "echo\r\n")  # .bat keeps CRLF
    return store(tmp, files={"concepts.jsonl": "{}\n"}, notes=[("a.md", "A\nnote\n")]), tmp


def chr03_fail(tmp):
    put(tmp / "scripts" / "ok.py", "x = 1\n")
    return store(tmp, files={"papers/p1/metadata.json": b'{\r\n"a": 1\r\n}'}), tmp


def broken_selftest(where):
    return lambda report: report.error(where, "broken")


CASES.update({
    "CHR-01": (chr01_pass, chr01_fail),
    "CHR-02": (chr02_pass, chr02_fail),
    "CHR-03": (chr03_pass, chr03_fail),
    "CHR-04": (lambda tmp: (), lambda tmp: (lambda value: value,)),
    "CHR-06": (lambda tmp: (), lambda tmp: (broken_selftest("text/font_glyphs"),)),
    "CHR-07": (lambda tmp: (), lambda tmp: (broken_selftest("citations/superscript"),)),
    "CHR-08": (lambda tmp: (Store(paper_ids=["p1"]), fake_check(("p1", "I2: seq must be 1..n"))),
               lambda tmp: (Store(paper_ids=["p1"]), fake_check(("p1:OCR1", "OCR passage p1:0001 is not on a recorded page")))),
})

RED = {}
