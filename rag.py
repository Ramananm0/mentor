"""Build and query a vector index of the study notes. Standard library only.

Usage:
    python rag.py build              # (re)index all D:\\study markdown files
    python rag.py query "text" [k]   # test a search
"""

import array
import json
import math
import os
import re
import sqlite3
import sys

from ollama_api import Ollama

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "index.db")
BATCH = 16
MAX_CHUNK = 3500  # characters
MIN_CHUNK = 60


def _config():
    with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
        return json.load(f)


def chunk_markdown(text):
    """Split on #/##/### headings; long sections are split on blank lines.
    Returns list of (heading, body)."""
    parts = re.split(r"(?m)^(#{1,3} .*)$", text)
    chunks = []
    if parts[0].strip():
        chunks.append(("(intro)", parts[0].strip()))
    for i in range(1, len(parts), 2):
        heading = parts[i].lstrip("#").strip()
        body = parts[i + 1].strip() if i + 1 < len(parts) else ""
        while len(body) > MAX_CHUNK:
            cut = body.rfind("\n\n", 500, MAX_CHUNK)
            cut = cut if cut != -1 else MAX_CHUNK
            chunks.append((heading, body[:cut].strip()))
            body = body[cut:].strip()
        if body:
            chunks.append((heading, body))
    return [(h, b) for h, b in chunks if len(b) >= MIN_CHUNK]


def _normalize(vec):
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / norm for x in vec]


def _study_files(cfg):
    skip = set(cfg["skip_dirs"])
    for root, dirs, files in os.walk(cfg["study_dir"]):
        dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
        for fname in sorted(files):
            if fname.endswith(".md"):
                yield os.path.join(root, fname)


def build():
    cfg = _config()
    client = Ollama(cfg["ollama_url"])
    embed_model = cfg["models"]["embed"]

    rows = []  # (file, heading, content)
    for path in _study_files(cfg):
        rel = os.path.relpath(path, cfg["study_dir"]).replace("\\", "/")
        with open(path, encoding="utf-8", errors="replace") as f:
            for heading, body in chunk_markdown(f.read()):
                rows.append((rel, heading, body))
    if not rows:
        sys.exit(f"no markdown files found under {cfg['study_dir']}")

    conn = sqlite3.connect(DB_PATH)
    conn.execute("DROP TABLE IF EXISTS chunks")
    conn.execute(
        "CREATE TABLE chunks("
        "id INTEGER PRIMARY KEY, file TEXT, heading TEXT, content TEXT, vec BLOB)"
    )

    done = 0
    for start in range(0, len(rows), BATCH):
        batch = rows[start : start + BATCH]
        # nomic-embed-text expects task prefixes for best quality
        inputs = [f"search_document: {f} / {h}\n{b}" for f, h, b in batch]
        vecs = client.embed(inputs, embed_model)
        for (f, h, b), vec in zip(batch, vecs):
            blob = array.array("f", _normalize(vec)).tobytes()
            conn.execute(
                "INSERT INTO chunks(file, heading, content, vec) VALUES (?,?,?,?)",
                (f, h, b, blob),
            )
        done += len(batch)
        print(f"\rembedded {done}/{len(rows)} chunks", end="", flush=True)
    conn.commit()
    conn.close()
    print(f"\nindex written to {DB_PATH} ({len(rows)} chunks)")


def query(text, k=4):
    """Top-k chunks as list of (score, file, heading, content)."""
    if not os.path.exists(DB_PATH):
        return []
    cfg = _config()
    client = Ollama(cfg["ollama_url"])
    qvec = _normalize(client.embed([f"search_query: {text}"], cfg["models"]["embed"])[0])

    conn = sqlite3.connect(DB_PATH)
    scored = []
    for file, heading, content, blob in conn.execute(
        "SELECT file, heading, content, vec FROM chunks"
    ):
        dvec = array.array("f")
        dvec.frombytes(blob)
        score = sum(a * b for a, b in zip(qvec, dvec))
        scored.append((score, file, heading, content))
    conn.close()
    scored.sort(key=lambda t: t[0], reverse=True)
    return scored[:k]


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "build":
        build()
    elif len(sys.argv) >= 3 and sys.argv[1] == "query":
        k = int(sys.argv[3]) if len(sys.argv) > 3 else 4
        for score, file, heading, content in query(sys.argv[2], k):
            print(f"\n--- {score:.3f}  {file} / {heading}\n{content[:300]}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
