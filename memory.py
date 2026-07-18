"""Long-term memory of the student — what a real tutor would remember.

Two sources feed it:
  1. quiz_scores in progress.json  -> weak/strong topic list (computed live)
  2. session observations          -> memory.json, written when a session ends:
     the fast model reads the transcript and notes what the student
     struggled with or understood well.

Every new conversation gets a "WHAT YOU REMEMBER ABOUT YOUR STUDENT" system
block, so the mentor greets you like someone who was there last time.
"""

import datetime
import json
import os

from ollama_api import Ollama, OllamaError

BASE = os.path.dirname(os.path.abspath(__file__))
MEMORY_FILE = os.path.join(BASE, "memory.json")
MAX_OBSERVATIONS = 60   # keep the file (and the prompt) small
RECALL = 12             # how many recent observations go into the prompt

SUMMARY_PROMPT = """\
You are the note-taking half of a programming tutor's brain. Below is a
transcript of one tutoring session with the student.

Write 1-3 SHORT observations a tutor would remember about THIS student,
one per line, no numbering. Focus on:
- concepts the student got wrong or found hard (be specific)
- concepts the student clearly understands now
- anything to follow up on next time

Rules: max 20 words per line. No praise fluff. No markdown. If the session
was too short to learn anything, output exactly: NOTHING

Transcript:
"""


def load():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"observations": []}


def save(mem):
    with open(MEMORY_FILE, "w", encoding="utf-8") as f:
        json.dump(mem, f, indent=2)


def _weak_strong(progress):
    """Split quizzed topics by average score."""
    weak, strong = [], []
    for topic, scores in progress.get("quiz_scores", {}).items():
        avg = sum(scores) / len(scores)
        label = f"{topic} (avg {avg:.1f}/5)"
        (weak if avg < 4 else strong).append(label)
    return weak, strong


def system_block(progress):
    """The memory text prepended to every conversation ('' when empty)."""
    mem = load()
    weak, strong = _weak_strong(progress)
    lines = []
    if weak:
        lines.append("Weak topics (quiz scores): " + ", ".join(weak))
    if strong:
        lines.append("Solid topics (quiz scores): " + ", ".join(strong))
    for obs in mem["observations"][-RECALL:]:
        lines.append(f"[{obs['date']}] {obs['note']}")
    if not lines:
        return ""
    return (
        "WHAT YOU REMEMBER ABOUT YOUR STUDENT (from earlier sessions — use it:"
        " revisit weak spots, connect new ideas to what they already know,"
        " and mention these memories naturally when relevant):\n- "
        + "\n- ".join(lines)
    )


def observe_session(cfg, transcript, mode, topic_label):
    """Distill a finished session into observations (uses the fast model).
    Returns the list of new notes ([] when nothing was worth remembering)."""
    if len(transcript) < 4:  # fewer than 2 exchanges — nothing to learn
        return []
    text = "\n\n".join(f"{role}: {msg[:800]}" for role, msg in transcript[-24:])
    client = Ollama(cfg["ollama_url"])
    model = cfg["models"]["fast"]
    try:
        if not client.has_model(model):
            model = cfg["models"]["big"]
        reply = ""
        for token, final in client.chat_stream(
            model,
            [{"role": "user", "content": SUMMARY_PROMPT + text}],
            num_ctx=8192, keep_alive="5m",
        ):
            if final is not None:
                break
            reply += token
    except OllamaError:
        return []

    notes = [
        ln.strip("-• \t") for ln in reply.splitlines()
        if ln.strip() and "NOTHING" not in ln.upper()
    ][:3]
    if not notes:
        return []

    mem = load()
    today = datetime.date.today().isoformat()
    for note in notes:
        mem["observations"].append(
            {"date": today, "mode": mode, "topic": topic_label, "note": note}
        )
    mem["observations"] = mem["observations"][-MAX_OBSERVATIONS:]
    save(mem)
    return notes
