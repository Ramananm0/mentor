"""Web interface for the Local AI Mentor — same brain, browser chat UI.

Run:   python web.py          then open  http://localhost:8090
Stdlib only (like the rest of the project): ThreadingHTTPServer + chunk
streaming over a close-delimited response. Single student, single session.

Reuses: prompts.py (modes), progress.py (curriculum/progress), rag.py
(study-note retrieval), ollama_api.py (streaming chat).
"""

import datetime
import json
import os
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import memory
import progress as prog
import prompts
import rag
from ollama_api import Ollama, OllamaError

BASE = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(BASE, "progress.json")
SESSIONS_DIR = os.path.join(BASE, "sessions")
INDEX_HTML = os.path.join(BASE, "web", "index.html")
PORT = 8090


class WebMentor:
    """Mentor state + chat, decoupled from the terminal (no printing)."""

    def __init__(self):
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            self.cfg = json.load(f)
        self.client = Ollama(self.cfg["ollama_url"])
        self.curriculum = prog.load_curriculum(
            self.cfg["study_dir"], self.cfg["skip_dirs"]
        )
        self.progress = prog.load_progress(PROGRESS_FILE)
        self.mode = "ask"
        self.subject = "c"
        self.model_key = self.cfg["default_model"]
        self.topic = None
        self.extra_context = ""
        self.messages = []
        self.transcript = []
        self._absorbed = 0  # transcript index already distilled into memory
        self.lock = threading.Lock()
        self._pick_available_model()

    @property
    def model(self):
        return self.cfg["models"][self.model_key]

    def _pick_available_model(self):
        try:
            tags = self.client.tags()
        except OllamaError:
            return  # surfaced later, on first chat
        if self.model in tags:
            return
        other = "fast" if self.model_key == "big" else "big"
        if self.cfg["models"][other] in tags:
            self.model_key = other

    # ----- context (same logic as mentor.py) --------------------------------

    def topic_id(self):
        return self.topic["id"] if isinstance(self.topic, dict) else None

    def topic_label(self):
        if isinstance(self.topic, dict):
            return f"{self.topic['id']} {self.topic['title']}"
        return self.topic or "(none)"

    def _rag_notes(self, query_text):
        try:
            hits = rag.query(query_text, self.cfg["rag_top_k"])
        except Exception:
            return ""
        blocks = [f"[{f} / {h}]\n{c}" for score, f, h, c in hits if score > 0.4]
        if not blocks:
            return ""
        return (
            "STUDENT'S OWN STUDY NOTES (use these as your source and refer to "
            "them):\n\n" + "\n\n".join(blocks)
        )

    def _system_messages(self, user_text):
        head = prompts.MODES[self.mode] + (
            f"\nSubject focus: {self.subject}. Current topic: {self.topic_label()}."
        )
        msgs = [{"role": "system", "content": head}]
        remembered = memory.system_block(self.progress)
        if remembered:
            msgs.append({"role": "system", "content": remembered})
        if self.extra_context:
            msgs.append({"role": "system", "content": self.extra_context})
        elif self.mode in ("ask", "teach", "quiz"):
            notes = self._rag_notes(user_text)
            if notes:
                msgs.append({"role": "system", "content": notes})
        return msgs

    # ----- chat --------------------------------------------------------------

    def stream_reply(self, user_text):
        """Yield reply tokens; record history/transcript/quiz score at the end."""
        msgs = (
            self._system_messages(user_text)
            + self.messages[-self.cfg["history_keep"]:]
            + [{"role": "user", "content": user_text}]
        )
        reply = ""
        for token, final in self.client.chat_stream(
            self.model, msgs, self.cfg["num_ctx"], self.cfg["keep_alive"]
        ):
            if final is not None:
                break
            reply += token
            yield token
        self.messages += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        self.transcript += [("student", user_text), ("mentor", reply)]
        self._maybe_record_quiz_score(reply)

    def _maybe_record_quiz_score(self, reply):
        if self.mode != "quiz" or len(self.messages) < 4:
            return
        m = re.search(r"SCORE:\s*(\d)\s*/\s*5", reply)
        if m and self.topic_id():
            self.progress["quiz_scores"].setdefault(self.topic_id(), []).append(
                int(m.group(1))
            )
            prog.save_progress(PROGRESS_FILE, self.progress)

    def absorb(self):
        """Distill the conversation since the last absorb into long-term
        memory. Runs on the fast model in a background thread so the UI
        never waits."""
        chunk = self.transcript[self._absorbed:]
        self._absorbed = len(self.transcript)
        if len(chunk) < 4:
            return
        mode, topic = self.mode, self.topic_label()
        threading.Thread(
            target=memory.observe_session,
            args=(self.cfg, chunk, mode, topic),
            daemon=True,
        ).start()

    # ----- commands -----------------------------------------------------------

    def _resolve_topic(self, arg):
        if arg:
            return prog.find_topic(self.curriculum, arg) or arg
        return prog.next_topic(self.curriculum, self.progress, self.subject)

    def _load_topic_file(self):
        self.extra_context = ""
        if isinstance(self.topic, dict) and self.topic["path"]:
            with open(self.topic["path"], encoding="utf-8", errors="replace") as f:
                text = f.read()[:15000]
            self.extra_context = (
                "STUDENT'S STUDY FILE FOR THIS TOPIC (teach from this, follow "
                "its order and examples):\n\n" + text
            )

    def start_teach(self, arg):
        self.absorb()
        topic = self._resolve_topic(arg)
        if topic is None:
            return {"error": "everything in your plan is marked done — congrats!"}
        self.mode, self.topic, self.messages = "teach", topic, []
        self._load_topic_file()
        self.progress["last_topic"] = self.topic_id() or str(self.topic)
        prog.save_progress(PROGRESS_FILE, self.progress)
        return {
            "note": f"teach mode · {self.topic_label()}",
            "kickoff": f"Teach me this topic: {self.topic_label()}",
        }

    def start_ask(self):
        self.absorb()
        self.mode, self.topic, self.messages, self.extra_context = "ask", None, [], ""
        return {"note": "ask mode — show code or ask anything; hints first"}

    def start_quiz(self, arg):
        self.absorb()
        topic = self._resolve_topic(arg) if arg else (
            prog.find_topic(self.curriculum, self.progress.get("last_topic") or "")
            or self._resolve_topic("")
        )
        if topic is None:
            return {"error": "no topic found to quiz on — try C-05"}
        self.mode, self.topic, self.messages = "quiz", topic, []
        self._load_topic_file()
        return {
            "note": f"quiz mode · {self.topic_label()}",
            "kickoff": f"Quiz me on: {self.topic_label()}",
        }

    def start_review(self, name, code):
        self.absorb()
        if not code.strip():
            return {"error": "paste some code to review"}
        name = name.strip() or "pasted_code"
        self.mode, self.topic, self.messages, self.extra_context = (
            "review", name, [], ""
        )
        code = code[:12000]
        return {
            "note": f"review mode · {name}",
            "kickoff": f"Please review my code file `{name}`:\n\n```\n{code}\n```",
        }

    def set_model(self, key):
        if key not in ("big", "fast"):
            return {"error": "model must be big or fast"}
        name = self.cfg["models"][key]
        try:
            if not self.client.has_model(name):
                return {"error": f"{name} is not downloaded yet"}
        except OllamaError as e:
            return {"error": str(e)}
        self.model_key = key
        return {"note": f"model -> {name}"}

    def set_subject(self, subj):
        if subj not in prog.SUBJECT_PREFIX:
            return {"error": "subject must be c, cpp or python"}
        self.subject = subj
        return {"note": f"subject -> {subj}"}

    def mark_done(self, tid, done=True):
        item = prog.find_topic(self.curriculum, tid or "")
        if not item:
            return {"error": f"unknown topic id: {tid}"}
        if done and item["id"] not in self.progress["done"]:
            self.progress["done"].append(item["id"])
        if not done and item["id"] in self.progress["done"]:
            self.progress["done"].remove(item["id"])
        prog.save_progress(PROGRESS_FILE, self.progress)
        return {"note": f"{'done' if done else 'unmarked'}: {item['id']}"}

    def save_session(self):
        self.absorb()
        if not self.transcript:
            return {"error": "nothing to save yet"}
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(SESSIONS_DIR, f"web_session_{stamp}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Mentor session (web) — {stamp}\n\n")
            for role, text in self.transcript:
                f.write(f"**{role}:**\n\n{text}\n\n---\n\n")
        return {"note": f"saved: {os.path.basename(path)}"}

    def state(self):
        nxt = prog.next_topic(self.curriculum, self.progress, self.subject)
        return {
            "mode": self.mode,
            "subject": self.subject,
            "model_key": self.model_key,
            "model": self.model,
            "topic": self.topic_label(),
            "next_topic": f"{nxt['id']} {nxt['title']}" if nxt else None,
            "summary": prog.summary(self.curriculum, self.progress),
            "done_count": len(self.progress["done"]),
            "total_count": len(self.curriculum),
        }


MENTOR = WebMentor()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quiet console
        pass

    # ----- helpers -----------------------------------------------------------

    def _json(self, obj, status=200):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}

    # ----- routes ------------------------------------------------------------

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            with open(INDEX_HTML, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/state":
            self._json(MENTOR.state())
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        data = self._read_body()
        if self.path == "/api/chat":
            self._chat(data.get("text", ""))
        elif self.path == "/api/command":
            self._command(data)
        else:
            self._json({"error": "not found"}, 404)

    def _command(self, data):
        cmd, arg = data.get("cmd", ""), data.get("arg", "")
        with MENTOR.lock:
            if cmd == "teach":
                out = MENTOR.start_teach(arg)
            elif cmd == "ask":
                out = MENTOR.start_ask()
            elif cmd == "quiz":
                out = MENTOR.start_quiz(arg)
            elif cmd == "review":
                out = MENTOR.start_review(data.get("name", ""), data.get("code", ""))
            elif cmd == "model":
                out = MENTOR.set_model(arg)
            elif cmd == "subject":
                out = MENTOR.set_subject(arg)
            elif cmd == "done":
                out = MENTOR.mark_done(arg, True)
            elif cmd == "undone":
                out = MENTOR.mark_done(arg, False)
            elif cmd == "save":
                out = MENTOR.save_session()
            else:
                out = {"error": f"unknown command: {cmd}"}
            out["state"] = MENTOR.state()
        self._json(out)

    def _chat(self, text):
        if not text.strip():
            self._json({"error": "empty message"}, 400)
            return
        # Close-delimited streaming body: no Content-Length, close when done.
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            with MENTOR.lock:
                for token in MENTOR.stream_reply(text):
                    self.wfile.write(token.encode("utf-8"))
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass  # browser tab closed mid-stream
        except OllamaError as e:
            try:
                self.wfile.write(f"\n[error: {e}]".encode("utf-8"))
            except OSError:
                pass
        self.close_connection = True


if __name__ == "__main__":
    print(f"Local AI Mentor (web) -> http://localhost:{PORT}")
    print(f"model: {MENTOR.model} ({MENTOR.model_key}) · Ctrl+C to stop")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
