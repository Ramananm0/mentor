"""Curriculum order (parsed from D:\\study\\INDEX.md + files on disk) and
student progress (progress.json: finished topics, quiz scores).
"""

import json
import os
import re

# Matches lines like "C-08  Memory Management - stack, heap" in INDEX.md
INDEX_LINE_RE = re.compile(r"^([A-Z]{1,5}-\d{2})\s+(.+?)\s*$")
# Matches filenames like C-08_Dynamic_Memory.md
FILE_RE = re.compile(r"^([A-Z]{1,5}-\d{2})_(.+)\.md$")

SUBJECT_PREFIX = {"c": "C-", "cpp": "CPP-", "python": "PY-"}

DEFAULT_PROGRESS = {
    # Seeded from study history: C-01..C-07 studied, C-08 is next.
    "done": ["C-01", "C-02", "C-03", "C-04", "C-05", "C-06", "C-07"],
    "quiz_scores": {},
    "last_topic": None,
}


def load_curriculum(study_dir, skip_dirs=()):
    """Ordered list of {"id", "title", "path"} — path is None when the study
    file for that topic has not been written yet."""
    order, seen = [], set()

    index_path = os.path.join(study_dir, "INDEX.md")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = INDEX_LINE_RE.match(line.strip())
                if m and m.group(1) not in seen:
                    seen.add(m.group(1))
                    order.append({"id": m.group(1), "title": m.group(2), "path": None})

    # Attach real file paths; append files that INDEX.md doesn't list.
    for root, dirs, files in os.walk(study_dir):
        dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
        for fname in sorted(files):
            m = FILE_RE.match(fname)
            if not m:
                continue
            fid, path = m.group(1), os.path.join(root, fname)
            for item in order:
                if item["id"] == fid:
                    item["path"] = path
                    break
            else:
                seen.add(fid)
                order.append(
                    {"id": fid, "title": m.group(2).replace("_", " "), "path": path}
                )
    return order


def find_topic(curriculum, topic_id):
    topic_id = topic_id.upper()
    for item in curriculum:
        if item["id"] == topic_id:
            return item
    return None


def next_topic(curriculum, progress, subject="c"):
    """First unfinished topic for the subject; falls back to any unfinished."""
    prefix = SUBJECT_PREFIX.get(subject, "")
    done = set(progress["done"])
    for item in curriculum:
        if item["id"].startswith(prefix) and item["id"] not in done:
            return item
    for item in curriculum:
        if item["id"] not in done:
            return item
    return None


def load_progress(path):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        for key, val in DEFAULT_PROGRESS.items():
            data.setdefault(key, val if not isinstance(val, (list, dict)) else type(val)(val))
        return data
    return json.loads(json.dumps(DEFAULT_PROGRESS))  # deep copy


def save_progress(path, progress):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(progress, f, indent=2)


def summary(curriculum, progress):
    """Per-module progress table as a string."""
    modules = {}
    for item in curriculum:
        prefix = item["id"].split("-")[0]
        mod = modules.setdefault(prefix, {"done": 0, "total": 0})
        mod["total"] += 1
        if item["id"] in progress["done"]:
            mod["done"] += 1
    lines = ["Module   Done/Total", "-" * 22]
    for prefix, mod in modules.items():
        lines.append(f"{prefix:<8} {mod['done']}/{mod['total']}")
    total_done = len(progress["done"])
    total = len(curriculum)
    lines.append("-" * 22)
    lines.append(f"{'ALL':<8} {total_done}/{total}")
    if progress["quiz_scores"]:
        lines.append("\nQuiz scores:")
        for topic, scores in progress["quiz_scores"].items():
            lines.append(f"  {topic}: {', '.join(f'{s}/5' for s in scores)}")
    return "\n".join(lines)
