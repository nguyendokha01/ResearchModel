"""The one hedge vocabulary: config/hedge_lexicon.yaml, loaded once and cached.

structure.uncertainty, the answer checker and the evidence validator all read it
through this module.  A missing or malformed yaml raises: there is no second list.
"""
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import yaml

# The shipped file next to the code (not config.CONFIG_DIR, which tests and tools may redirect).
DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "hedge_lexicon.yaml"

# Categories that were the evidence validator's old MODAL list (word-level hedges).
MODAL_CATEGORIES = frozenset({"modal", "appearance", "hypothesis", "qualification", "negated_hedge"})
# EV-06 / OD-12: categories that are not a hedge of the claim (p > 0.05, "no significant difference" is its own category).
NON_HEDGE = frozenset({"non_significance"})


@lru_cache(maxsize=None)
def _load(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("categories"), dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"invalid hedge lexicon shape: {path}")
    labels = {str(k): str(v["label_vi"]) for k, v in data["categories"].items() if isinstance(v, dict) and v.get("label_vi")}
    entries = []
    for item in data["entries"]:
        if not isinstance(item, dict) or not item.get("category") or not item.get("strength"):
            raise ValueError(f"invalid hedge lexicon entry: {item!r}")
        raw = item.get("regex") or item.get("phrase")
        if not isinstance(raw, str) or not raw:
            raise ValueError(f"missing hedge pattern: {item!r}")
        if item.get("phrase") and not item.get("regex"):
            raw = r"(?<![\w])" + re.escape(raw) + r"(?![\w])"
        category = str(item["category"])
        entries.append((category, re.compile(raw, re.I | re.UNICODE), str(item["strength"]), labels.get(category, "")))
    if not entries:
        raise ValueError(f"empty hedge lexicon: {path}")
    return tuple(entries)


def load(path=None):
    """Return [(category, compiled_regex, strength, label_vi)]; raises if the yaml is absent or invalid."""
    return list(_load(str(path or DEFAULT_PATH)))


_ORG_FOLLOWER = re.compile(r"\s+(?:for|of)\b", re.I)
_PREV_WORD = re.compile(r"(\S+)\s+$")


def _accept(text, category, match):
    """Association hits that are not hedges: X-associated compounds and organisation names."""
    if category != "association":
        return True
    if match.start() > 0 and text[match.start() - 1] == "-":
        return False
    value = match.group(0)
    if value[:1].isupper() and value.casefold().startswith("association"):
        prev = _PREV_WORD.search(text[:match.start()])  # "American Medical Association"
        initial = not prev or prev.group(1)[-1] in ".!?:;"  # sentence-initial "Association of X with Y" is a real claim
        follower = _ORG_FOLLOWER.match(text, match.end())
        if follower and (not initial or follower.group(0).strip().lower() == "for"):  # "Association for/of ..."
            return False
        if prev and prev.group(1)[:1].isupper() and prev.group(1)[-1] not in ".!?:;":
            return False
    return True


def uncertainty(text, entries=None):
    """Return the non-overlapping hedge hits in text (longest span wins)."""
    text = unicodedata.normalize("NFC", str(text or ""))   # CHR-01: an NFD spelling is read like its NFC form
    found = []
    for category, pattern, strength, label_vi in (entries if entries is not None else load()):
        for match in pattern.finditer(text):
            if _accept(text, category, match):
                found.append((match.start(), match.end(), category, match.group(), strength, label_vi))
    selected = []
    for hit in sorted(found, key=lambda row: (row[0], -(row[1] - row[0]), row[2])):
        if selected and hit[0] < selected[-1][1]:
            continue
        selected.append(hit)
    return [{"category": c, "text": t, "start": s, "end": e, "strength": st, "label_vi": lb}
            for s, e, c, t, st, lb in selected]


class _Hit:
    def __init__(self, text):
        self._text = text

    def group(self, _n=0):
        return self._text


class Finder:
    """``re.Pattern``-like ``.search(text)`` over the lexicon, optionally limited to some categories or without some."""

    def __init__(self, categories=None, exclude=()):
        self.categories = categories
        self.exclude = exclude

    def search(self, text):
        for hit in uncertainty(text):
            if (self.categories is None or hit["category"] in self.categories) and hit["category"] not in self.exclude:
                return _Hit(hit["text"])
        return None
