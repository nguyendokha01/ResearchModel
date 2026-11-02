"""Run from the repo root: python -m unittest discover -s tests -t ."""
import atexit
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for sub in ("tools", "scripts"):
    sys.path.insert(0, str(ROOT / sub))

# Every test writes into a throw-away data root unless it sets its own: a test that forgets to redirect
# a path must never touch the researcher's store or vaults (it happened twice: vaults/vit, vaults/research).
from kbsys import config  # noqa: E402

_DATA = Path(tempfile.mkdtemp(prefix="kb-tests-"))
atexit.register(shutil.rmtree, _DATA, True)
config.STORE = _DATA / "knowledge_store"
config.DB_PATH = config.STORE / "library.db"
config.PAPERS_DIR = config.STORE / "papers"
config.LOGS_DIR = config.STORE / "logs"
config.CACHE_DIR = config.STORE / "cache"
config.VAULTS_DIR = _DATA / "vaults"
config.REVIEW_PENDING = _DATA / "review" / "pending"
