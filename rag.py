"""Build and query a vector index of the study notes and reference library.
Standard library only.

Sources: cfg["study_dir"] plus every dir in cfg["extra_dirs"] (the downloaded
books / docs / interview questions). Indexes .md, .txt (reST headings, e.g.
Python docs), and .html (e.g. Beej's guide).

Usage:
    python rag.py build              # (re)index everything
    python rag.py query "text" [k]   # test a search
"""

import array
import json
import math
import os
import re
import sqlite3
import sys
from html.parser import HTMLParser

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


class _HTMLText(HTMLParser):
    """HTML -> text with h1-h3 turned into #/##/### lines."""

    SKIP = {"script", "style", "nav", "head"}
    BLOCK = {"p", "div", "li", "pre", "tr", "br", "ul", "ol", "table", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in ("h1", "h2", "h3"):
            self.out.append("\n\n" + "#" * int(tag[1]) + " ")
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in ("h1", "h2", "h3"):
            self.out.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.out.append(data)


def html_to_md(html):
    parser = _HTMLText()
    parser.feed(html)
    text = "".join(parser.out)
    # headings must stay on one line for chunk_markdown
    text = re.sub(r"(?m)^(#{1,3} )[ \t]*\n[ \t]*", r"\1", text)
    return re.sub(r"\n{3,}", "\n\n", text)


def rst_to_md(text):
    """Turn reST underlined headings (Python text docs) into # headings."""
    lines = text.splitlines()
    out = []
    i = 0
    while i < len(lines):
        line, nxt = lines[i], lines[i + 1] if i + 1 < len(lines) else ""
        under = nxt.strip()
        if (
            line.strip()
            and not line.startswith(" ")
            and len(under) >= 3
            and len(set(under)) == 1
            and under[0] in "*=-~^\""
            and len(under) >= len(line.rstrip())
        ):
            level = {"*": 1, "=": 1, "-": 2}.get(under[0], 3)
            out.append("#" * level + " " + line.strip())
            i += 2
            continue
        out.append(line)
        i += 1
    return "\n".join(out)


def _source_files(cfg):
    """Yield (label, path) for every indexable file across all source dirs.
    label = '<root-name>/<relative-path>' so chunks say where they came from."""
    skip = set(cfg["skip_dirs"])
    roots = [cfg["study_dir"]] + cfg.get("extra_dirs", [])
    for root_dir in roots:
        prefix = os.path.basename(os.path.normpath(root_dir))
        for root, dirs, files in os.walk(root_dir):
            dirs[:] = [d for d in dirs if d not in skip and not d.startswith(".")]
            for fname in sorted(files):
                if fname.endswith((".md", ".txt", ".html")):
                    path = os.path.join(root, fname)
                    rel = os.path.relpath(path, root_dir).replace("\\", "/")
                    yield f"{prefix}/{rel}", path


def _read_as_markdown(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        text = f.read()
    if path.endswith(".html"):
        return html_to_md(text)
    if path.endswith(".txt"):
        return rst_to_md(text)
    return text


def build():
    cfg = _config()
    client = Ollama(cfg["ollama_url"])
    embed_model = cfg["models"]["embed"]

    rows = []  # (file, heading, content)
    for label, path in _source_files(cfg):
        for heading, body in chunk_markdown(_read_as_markdown(path)):
            rows.append((label, heading, body))
    if not rows:
        sys.exit("no indexable files found in study_dir / extra_dirs")

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
