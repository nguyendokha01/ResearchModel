"""PASS / FAIL cases of the stages RT (retrieval) and AN (answers and the answer checker), see scripts/kbsys/conform_answer.py.

Probes run the product function in a throw-away project in every PASS case, so a product that is wrong today makes its PASS
case report violations (listed in RED). The FAIL cases hand the check a deliberately broken judge or a damaged cache / note.
test_answer.py holds the edges the generic cases do not cover, among them judges that satisfy the RED clauses.
"""
import json

from kbsys import answer, find
from kbsys.conform import Store, Vault

from .cases_intake import put


def tamper(real, edit):
    """A judge that calls the real function and edits its result."""
    return lambda *args, **kwargs: edit(real(*args, **kwargs))


def no_args(tmp):
    return ()


def sure(sentence, items, cards, **kwargs):
    """A checker that lets every sentence through."""
    return {"check": "ok", "problems": [], "warnings": [], "evidence": []}


FAKE_HIT = {"kind": "sentence", "id": "nope:S0001", "paper_id": "nope", "text": "x", "sections": ["results"], "pages": [1],
            "sentence_ids": ["nope:S0001"]}


# ---------------------------------------------------------------- RT

def search_in_every_vault(con, cfg, query, vault=None, all_vaults=False, **kwargs):
    return find.search(con, cfg, query, all_vaults=True, **kwargs)  # ignores the requested vault


def good_cache():
    item = {"id": "p1:S0001", "paper_id": "p1", "text": "Alphaterm was higher.", "sentence_ids": ["p1:S0001"],
            "sections": ["results"], "pages": [3]}
    return {"items": [item], "papers": {"p1": {"title": "A title", "doi": None, "pmid": "42", "has_pdf": False}},
            "answer": {"answer": [{"text": "Alphaterm was higher [#1].", "check": "ok", "evidence": [1], "chunk_ids": ["p1:S0001"]}],
                       "reasoning": [{"text": "Synthesis.", "check": "ok", "basis": [1]}]}}


def cache_store(tmp, mutate=None):
    data = good_cache()
    if mutate:
        mutate(data)
    root = tmp / "s"
    put(root / "cache" / "answers" / "20260101_000000_evidence4_fast.json", json.dumps(data))
    return Store(root=root, paper_ids=[], vaults=[])


CASES = {
    "RT-01": (no_args, lambda tmp: (tamper(find.search, lambda r: {**r, "hits": r["hits"] + [FAKE_HIT]}),)),
    "RT-02": (no_args, lambda tmp: (tamper(find.search, lambda r: {**r, "hits": r["hits"][::-1]}),)),
    "RT-03": (no_args, lambda tmp: (lambda con, cfg, query, **kw: {"hits": [], "groups": [[query]]},)),
    "RT-04": (no_args, lambda tmp: (lambda con, cfg, query, **kw: {"hits": [], "groups": [[query]], "needs_translation": False},)),
    "RT-05": (no_args, lambda tmp: (tamper(find.search, lambda r: {**r, "not_found": False}),)),
    "RT-06": (no_args, lambda tmp: (search_in_every_vault,)),
    "RT-07": (no_args, lambda tmp: (lambda con, cfg, queries, vault, all_vaults, filters, max_items:
                                    answer.collect(con, cfg, queries[:1], vault, all_vaults, filters, max_items),)),
    "RT-08": (no_args, lambda tmp: (tamper(find.search, lambda r: {**r, "hits": r["hits"] + [{**FAKE_HIT, "sections": ["funding"]}]}),)),
    "RT-09": (no_args, lambda tmp: (lambda con, sentence_id, window=3: [{"sentence_id": sentence_id, "section": "results"}],)),
    "RT-10": (lambda tmp: (cache_store(tmp),),
              lambda tmp: (cache_store(tmp, lambda d: d["items"][0].pop("pages")),)),
    "RT-11": (no_args, lambda tmp: (lambda con, text, vault=None, **kw: {"label": "possible plagiarism", "hits": []},)),
    "RT-12": (no_args, lambda tmp: (tamper(find.search, lambda r: {**r, "hits": r["hits"] + r["source_hits"], "source_hits": []}),)),
}


# ---------------------------------------------------------------- AN

def an13_prepare(*args, **kwargs):
    return {"items_digest": "aaa"}


def an13_check(*args, **kwargs):
    return {"items_digest": "bbb", "markdown": "no reasons here",
            "answer": {"answer": [{"check": "struck:A2", "reasons": ["a number that is not in the item"]}]}}


def an14_code_evidence(items, cards, cited=None):
    return [{"item": n, "id": i["id"], "quote": i["text"] + " (typed by an AI)", "citation": "[Smith, 2020, trang 1]"}
            for n, i in enumerate(items, 1)]


def an15_render(checked, items=()):
    return "## Trả lời dựa trên bằng chứng\n" + "\n".join(r["text"] for r in checked["answer"] + checked["reasoning"])


def an16_collect(*args):
    items, runs = answer.collect(*args)
    return [i for i in items if i["paper_id"] == "pa"], runs


NOTE = "---\ntype: {type}\nevidence: {evidence}\n---\n# T\n\n> [!note] banner\n\n{body}\n"


def vault_with(tmp, name, text):
    put(tmp / "v" / "Discussions" / name, text)
    return Store(root=tmp / "s", paper_ids=[], vaults=[Vault("v", tmp / "v")])


CASES.update({
    "AN-01": (no_args, lambda tmp: (sure,)),
    "AN-02": (no_args, lambda tmp: (sure,)),
    "AN-03": (no_args, lambda tmp: (sure,)),
    "AN-04": (no_args, lambda tmp: (sure,)),
    "AN-05": (no_args, lambda tmp: (sure,)),
    "AN-06": (no_args, lambda tmp: (sure,)),
    "AN-07": (no_args, lambda tmp: (sure,)),
    "AN-08": (no_args, lambda tmp: (sure,)),
    "AN-09": (no_args, lambda tmp: (sure,)),
    "AN-10": (no_args, lambda tmp: (sure,)),
    "AN-11": (no_args, lambda tmp: (sure,)),
    "AN-12": (no_args, lambda tmp: (sure, sure)),
    "AN-13": (no_args, lambda tmp: (an13_prepare, an13_check)),
    "AN-14": (no_args, lambda tmp: (an14_code_evidence,)),
    "AN-15": (no_args, lambda tmp: (answer.check_parts, an15_render)),
    "AN-16": (no_args, lambda tmp: (an16_collect,)),
    "AN-17": (lambda tmp: (vault_with(tmp, "good.md", NOTE.format(type="discussion", evidence="false", body="Text.")),),
              lambda tmp: (vault_with(tmp, "bad.md", NOTE.format(type="discussion", evidence="true", body="Text.")),)),
    "AN-18": (lambda tmp: (cache_store(tmp),),
              lambda tmp: (cache_store(tmp, lambda d: d["items"][0].pop("sections")),)),
})

RED = {}
