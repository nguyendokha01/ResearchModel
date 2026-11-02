"""Project paths and settings, read from the YAML files in config/."""

from datetime import datetime
import json
from pathlib import Path
import re

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
STORE = ROOT / "knowledge_store"
DB_PATH = STORE / "library.db"
PAPERS_DIR = STORE / "papers"
LOGS_DIR = STORE / "logs"
CACHE_DIR = STORE / "cache"
VAULTS_DIR = ROOT / "vaults"
REVIEW_PENDING = ROOT / "review" / "pending"


def analyst_prompt():
    """The AI analyst prompt: the marked part of docs/AI_ANALYSIS_GUIDE.md."""
    text = (ROOT / "docs" / "AI_ANALYSIS_GUIDE.md").read_text(encoding="utf-8")
    return text.split("<!-- begin-analyst-prompt -->", 1)[1].split("<!-- end-analyst-prompt -->", 1)[0].strip() + chr(10)


def is_research_vault_name(name):
    """Exclude mother-vault folders and hidden implementation directories."""
    value = str(name or "")
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value)
                and not value.startswith(("_", "."))
                and value.casefold() != "registry")


def _read_yaml(name):
    path = CONFIG_DIR / name
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load():
    """Merge all config files into one dict."""
    cfg = {}
    for name in ("system.yaml", "retrieval.yaml", "ingest.yaml"):
        cfg.update(_read_yaml(name))
    cfg.setdefault("ingest", {})
    cfg.setdefault("passages", {})
    cfg.setdefault("search", {})
    cfg["synonyms"] = _read_yaml("synonyms.yaml") or []
    cfg["word_variants"] = _read_yaml("word_variants.yaml") or []   # query side only: the concept registry does not read it
    cfg["concept_parents"] = _read_yaml("concept_parents.yaml") or {}
    return cfg


def research_vaults():
    """Sub-vault folders: a valid name plus `.obsidian/` or `Home.md` (a stray folder with only `_Maps` is not a vault)."""
    if not VAULTS_DIR.is_dir():
        return []
    return sorted(p.name for p in VAULTS_DIR.iterdir() if p.is_dir() and is_research_vault_name(p.name)
                  and ((p / ".obsidian").is_dir() or (p / "Home.md").is_file()))


def require_research_vault(vault):
    """`vault` itself when it is one of research_vaults(); else ValueError (no silent empty answer for a typo or '../x')."""
    if not isinstance(vault, str) or vault not in research_vaults():
        raise ValueError(f"Unknown vault: {vault!r}")
    return vault


def default_vault(cfg):
    configured = cfg["ingest"].get("default_vault", "") or ""
    selected = configured
    preferences = STORE / "ui_preferences.json"
    if preferences.is_file():
        try:
            saved = json.loads(preferences.read_text(encoding="utf-8")).get("selected_vault")
            if isinstance(saved, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", saved):
                selected = saved
        except (OSError, ValueError, AttributeError):
            pass
    if is_research_vault_name(selected) and vault_dir(selected).is_dir():
        return selected
    if is_research_vault_name(configured) and vault_dir(configured).is_dir():
        return configured
    available = research_vaults()
    return available[0] if available else ""


def vault_dir(vault):
    return VAULTS_DIR / vault


def now():
    return datetime.now().isoformat(timespec="seconds")


def lf(text):
    """Text from a client (CRLF or old-Mac CR) as LF, once, before it is saved (CHR-03)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def rel(path):
    """Path relative to the project root, with forward slashes."""
    return Path(path).resolve().relative_to(ROOT).as_posix()


def log(message):
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    day = datetime.now().strftime("%Y-%m-%d")
    with open(LOGS_DIR / f"ingest_{day}.log", "a", encoding="utf-8", newline="\n") as f:
        f.write(f"{now()}  {message}\n")
