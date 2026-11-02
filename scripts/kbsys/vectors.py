"""Search by meaning (M3.5, docs/CONTRACTS.md §4.6).

Model: multilingual-e5-small (ONNX, int8), runs on the CPU, understands Vietnamese and English
questions. Files in knowledge_store/embeddings/model/ (model_quantized.onnx, tokenizer.json).

What is embedded, per paper: every evidence unit (statement + quote) and every passage outside the
reference list / front matter. Vectors are stored per paper (embeddings/papers/<paper_id>.npz,
int8) with a fingerprint of the texts, model/tokenizer bytes and encoder code, so `update` only redoes new or changed papers, one paper at
a time. All papers are then joined into one file (embeddings/index_<time>.npy + index.json) that
searches read memory-mapped. Everything here is a rebuildable cache.

At query time a passage is turned into its best sentences (the sentences of the top passages are
embedded on the fly), so results still point to exact sentences.
"""

import hashlib
import json
import os
import threading
import time

from . import config, workflow

EMB_DIR = config.STORE / "embeddings"
MODEL_DIR = EMB_DIR / "model"
PAPER_VECS = EMB_DIR / "papers"
INDEX_JSON = EMB_DIR / "index.json"
MODEL_FILE = "model_quantized.onnx"
MODEL_URL = "https://huggingface.co/Xenova/multilingual-e5-small/resolve/main/"
DIM = 384
MAX_TOKENS = 256
BATCH = 16
SKIP_SECTIONS = ("references", "front_matter", "acknowledgements", "funding", "author_contributions",
                 "conflicts", "data_availability", "notes")


def available():
    return (MODEL_DIR / MODEL_FILE).is_file() and (MODEL_DIR / "tokenizer.json").is_file()


def index_ready():
    return available() and INDEX_JSON.is_file()


def download_model():
    import urllib.request
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    for name, remote in ((MODEL_FILE, "onnx/model_quantized.onnx"), ("tokenizer.json", "tokenizer.json")):
        path = MODEL_DIR / name
        if path.is_file():
            continue
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(MODEL_URL + remote, timeout=300) as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        tmp.replace(path)


# ---------------------------------------------------------------- encoder

_encoder = None
_encoder_identity = None
_encoder_lock = threading.Lock()


class Encoder:
    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer
        self.np = np
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, min(4, (os.cpu_count() or 2) - 1))
        self.session = ort.InferenceSession(str(MODEL_DIR / MODEL_FILE), opts, providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        self.tok = Tokenizer.from_file(str(MODEL_DIR / "tokenizer.json"))
        self.tok.enable_truncation(MAX_TOKENS)
        self.tok.enable_padding()

    def encode(self, texts, kind="passage"):
        """Normalised float32 vectors; kind is "query" or "passage" (the e5 prefixes)."""
        np = self.np
        out = []
        for i in range(0, len(texts), BATCH):
            enc = self.tok.encode_batch([f"{kind}: {t}" for t in texts[i:i + BATCH]])
            ids = np.array([e.ids for e in enc], dtype=np.int64)
            mask = np.array([e.attention_mask for e in enc], dtype=np.int64)
            feed = {"input_ids": ids, "attention_mask": mask}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.zeros_like(ids)
            hidden = self.session.run(None, feed)[0]
            v = (hidden * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
            out.append(v / np.linalg.norm(v, axis=1, keepdims=True))
        return np.vstack(out).astype(np.float32) if out else np.zeros((0, DIM), np.float32)


def encoder():
    global _encoder, _encoder_identity
    with _encoder_lock:
        identity = tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in (MODEL_DIR / MODEL_FILE, MODEL_DIR / 'tokenizer.json'))
        if _encoder is None or identity != _encoder_identity:
            _encoder = Encoder()
            _encoder_identity = identity
    return _encoder


# ---------------------------------------------------------------- building

def paper_items(con, paper_id):
    """(ids, kinds, texts) to embed for one paper."""
    ids, kinds, texts = [], [], []
    for r in con.execute("SELECT unit_id, statement, quote FROM evidence WHERE paper_id = ? ORDER BY seq", (paper_id,)):
        ids.append(r[0]); kinds.append("unit"); texts.append(f"{r[1] or ''} {r[2] or ''}".strip())
    q = ("SELECT passage_id, text FROM passages WHERE paper_id = ? AND section NOT IN (%s) ORDER BY seq"
         % ",".join("?" * len(SKIP_SECTIONS)))
    for r in con.execute(q, (paper_id, *SKIP_SECTIONS)):
        if r[1] and r[1].strip():
            ids.append(r[0]); kinds.append("passage"); texts.append(r[1])
    return ids, kinds, texts


def _fingerprint(ids, texts):
    h = hashlib.sha1()
    for i, t in zip(ids, texts):
        h.update(i.encode()); h.update(b"\0"); h.update(t.encode()); h.update(b"\1")
    return h.hexdigest()


def _paper_file(paper_id):
    return PAPER_VECS / f"{paper_id}.npz"


def update(con, progress=print, force_papers=(), force_rebuild=False):
    with workflow.writer_lock():
        return _update(con, progress, force_papers, force_rebuild)


def _update(con, progress, force_papers, force_rebuild):
    """Embed new or changed papers, drop removed ones, rejoin the index. Returns counts."""
    import numpy as np
    if not available():
        return {"error": "model missing"}
    PAPER_VECS.mkdir(parents=True, exist_ok=True)
    papers = [r[0] for r in con.execute("SELECT paper_id FROM papers ORDER BY paper_id")]
    model_signature = workflow.fingerprint([MODEL_DIR / MODEL_FILE, MODEL_DIR / 'tokenizer.json', __file__])
    force_papers = set(force_papers)
    done = skipped = 0
    t0 = time.time()
    for n, pid in enumerate(papers, start=1):
        ids, kinds, texts = paper_items(con, pid)
        fp = hashlib.sha1((_fingerprint(ids, texts) + model_signature).encode()).hexdigest()
        path = _paper_file(pid)
        if path.is_file() and pid not in force_papers:
            try:
                with np.load(path, allow_pickle=False) as old:
                    if str(old["fingerprint"]) == fp:
                        skipped += 1
                        continue
            except (OSError, ValueError, KeyError):
                pass
        vecs = encoder().encode(texts) if texts else np.zeros((0, DIM), np.float32)
        tmp = path.with_suffix(".tmp.npz")
        np.savez(tmp, ids=np.array(ids, dtype=str), kinds=np.array(kinds, dtype=str),
                 vecs=np.clip(np.round(vecs * 127), -127, 127).astype(np.int8), fingerprint=np.array(fp))
        os.replace(tmp, path)
        done += 1
        if progress:
            progress(f"  vector {n}/{len(papers)}: {pid} ({len(texts)} đoạn)")
    keep = {f"{p}.npz" for p in papers}
    removed = 0
    for f in PAPER_VECS.glob("*.npz"):
        if f.name not in keep:
            f.unlink(missing_ok=True)
            removed += 1
    if done or removed or force_rebuild or not _index_intact():
        _join(papers)
    return {"papers": len(papers), "embedded": done, "unchanged": skipped, "removed": removed,
            "seconds": round(time.time() - t0, 1)}


def _index_intact():
    try:
        return (EMB_DIR / json.loads(INDEX_JSON.read_text(encoding="utf-8"))["file"]).is_file()
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _join(papers):
    """All per-paper vectors -> one versioned .npy + index.json (old versions deleted when possible)."""
    import numpy as np
    ids, kinds, pidx, parts = [], [], [], []
    for i, pid in enumerate(papers):
        path = _paper_file(pid)
        if not path.is_file():
            continue
        with np.load(path, allow_pickle=False) as d:
            ids += d["ids"].tolist(); kinds += d["kinds"].tolist()
            pidx += [i] * len(d["ids"]); parts.append(d["vecs"])
    matrix = np.vstack(parts) if parts else np.zeros((0, DIM), np.int8)
    name = f"index_{time.strftime('%Y%m%d_%H%M%S')}_{time.time_ns()}.npy"
    np.save(EMB_DIR / name, matrix)
    tmp = INDEX_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps({"file": name, "model": "multilingual-e5-small-int8", "built_at": config.now(),
                               "papers": papers, "ids": ids, "kinds": kinds, "paper_idx": pidx}), encoding="utf-8", newline="\n")
    os.replace(tmp, INDEX_JSON)
    for old in EMB_DIR.glob("index_*.npy"):
        if old.name != name:
            try:
                old.unlink()
            except OSError:    # still open in a running search page; removed next time
                pass


# ---------------------------------------------------------------- searching

_index = {"file": None}


def _load():
    import numpy as np
    meta_mtime = INDEX_JSON.stat().st_mtime
    if _index.get("mtime") != meta_mtime:
        meta = json.loads(INDEX_JSON.read_text(encoding="utf-8"))
        _index.update(meta, mtime=meta_mtime,
                      matrix=np.load(EMB_DIR / meta["file"], mmap_mode="r"),
                      pidx=np.array(meta["paper_idx"], dtype=np.int32),
                      paper_pos={p: i for i, p in enumerate(meta["papers"])})
    return _index


def search(query, paper_ids=None, k=80):
    """Nearest units/passages: [(id, kind, paper_id, cosine)] best first."""
    import numpy as np
    if not index_ready() or not query.strip():
        return [], None
    idx = _load()
    matrix = idx["matrix"]
    if not len(matrix):
        return [], None
    q = encoder().encode([query], "query")[0]
    allowed = None
    if paper_ids is not None:
        pos = [idx["paper_pos"][p] for p in paper_ids if p in idx["paper_pos"]]
        allowed = np.isin(idx["pidx"], np.array(pos, dtype=np.int32))
    scores = np.empty(len(matrix), dtype=np.float32)
    for s in range(0, len(matrix), 65536):
        scores[s:s + 65536] = matrix[s:s + 65536].astype(np.float32) @ q / 127.0
    if allowed is not None:
        scores[~allowed] = -1
    k = min(k, len(scores))
    top = np.argpartition(-scores, k - 1)[:k]
    top = top[np.argsort(-scores[top])]
    return [(idx["ids"][i], idx["kinds"][i], idx["papers"][idx["pidx"][i]], float(scores[i]))
            for i in top if scores[i] > -1], q


def best_sentences(qvec, sentences, per_passage=1):
    """sentences: [{sentence_id, passage_id, text}] -> {passage_id: [(sentence_id, cosine)]} best first."""
    import numpy as np
    if not sentences:
        return {}
    vecs = encoder().encode([s["text"] for s in sentences])
    sims = vecs @ qvec
    out = {}
    for s, sim in sorted(zip(sentences, sims), key=lambda x: -x[1]):
        lst = out.setdefault(s["passage_id"], [])
        if len(lst) < per_passage:
            lst.append((s["sentence_id"], float(sim)))
    return out


def status():
    if not available():
        return {"ready": False, "message": "Chưa có mô hình tìm theo nghĩa"}
    if not INDEX_JSON.is_file():
        return {"ready": False, "message": "Chưa lập chỉ mục tìm theo nghĩa"}
    try:
        idx = _load()
    except (OSError, ValueError, KeyError):   # index.json or its .npy damaged/half-deleted: rebuilt at next start
        return {"ready": False, "message": "Chỉ mục tìm theo nghĩa bị hỏng; mở lại trang để dựng lại"}
    return {"ready": True, "message": f"Tìm theo nghĩa: {len(idx['ids'])} mục, {len(idx['papers'])} bài",
            "built_at": idx.get("built_at")}
