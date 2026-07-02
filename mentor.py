"""Local AI Mentor — chat in your terminal, powered by Ollama.

Run:   python mentor.py     (or just `mentor` in Git Bash once the alias is set)
Then:  /help inside the chat shows all commands.

Modes: ask (default, Socratic Q&A) · teach (lessons) · quiz · review
"""

import datetime
import json
import os
import re
import sys

import progress as prog
import prompts
import rag
from ollama_api import Ollama, OllamaError

BASE = os.path.dirname(os.path.abspath(__file__))
PROGRESS_FILE = os.path.join(BASE, "progress.json")
SESSIONS_DIR = os.path.join(BASE, "sessions")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

IS_TTY = sys.stdout.isatty()


def _c(code, s):
    return f"\033[{code}m{s}\033[0m" if IS_TTY else s


def cyan(s):
    return _c("96", s)


def dim(s):
    return _c("2", s)


def yellow(s):
    return _c("93", s)


HELP = """\
Commands:
  /teach [topic]        start a lesson (default: next topic in your plan)
                        topic = an ID like C-08, or free text like "malloc"
  /ask                  back to question mode (hints first, not solutions)
  /quiz [topic]         5 questions, graded, score saved
  /review <file>        mentor reviews your code file
  /model big|fast       switch qwen3-coder:30b <-> qwen2.5-coder:7b
  /subject c|cpp|python set your focus language
  /done <ID>            mark a topic finished (e.g. /done C-08)
  /undone <ID>          unmark a topic
  /progress             show curriculum progress + quiz scores
  /save                 save transcript now (also auto-saves on exit)
  /help                 this help
  /exit                 quit
Anything else you type is sent to the mentor."""


class Mentor:
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
        self.topic = None  # curriculum dict, plain string, or None
        self.extra_context = ""  # full study file / other pinned context
        self.messages = []  # chat history (user/assistant only)
        self.transcript = []
        self._pick_available_model()

    # ----- model handling ---------------------------------------------------

    @property
    def model(self):
        return self.cfg["models"][self.model_key]

    def _pick_available_model(self):
        try:
            tags = self.client.tags()
        except OllamaError as e:
            sys.exit(f"error: {e}")
        if self.model in tags:
            return
        other = "fast" if self.model_key == "big" else "big"
        other_name = self.cfg["models"][other]
        if other_name in tags:
            print(yellow(f"[{self.model} not downloaded yet -> using {other_name}]"))
            self.model_key = other
        else:
            sys.exit(f"error: no chat model installed. Run: ollama pull {other_name}")

    # ----- context building ---------------------------------------------------

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
        blocks = [
            f"[{f} / {h}]\n{c}" for score, f, h, c in hits if score > 0.4
        ]
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
        if self.extra_context:
            msgs.append({"role": "system", "content": self.extra_context})
        elif self.mode in ("ask", "teach", "quiz"):
            notes = self._rag_notes(user_text)
            if notes:
                msgs.append({"role": "system", "content": notes})
        return msgs

    # ----- chat ---------------------------------------------------------------

    def send(self, user_text):
        msgs = (
            self._system_messages(user_text)
            + self.messages[-self.cfg["history_keep"]:]
            + [{"role": "user", "content": user_text}]
        )
        print(cyan(f"\nmentor ({self.model}):"))
        reply, stats = "", None
        try:
            for token, final in self.client.chat_stream(
                self.model, msgs, self.cfg["num_ctx"], self.cfg["keep_alive"]
            ):
                if final is not None:
                    stats = final
                    break
                print(token, end="", flush=True)
                reply += token
        except KeyboardInterrupt:
            print(yellow("\n[stopped]"))
        except OllamaError as e:
            print(yellow(f"\n[error: {e}]"))
            return
        print()
        if stats and stats.get("eval_duration"):
            tps = stats["eval_count"] / (stats["eval_duration"] / 1e9)
            print(dim(f"  · {tps:.1f} tokens/s ({stats.get('eval_count', 0)} tokens)"))
        self.messages += [
            {"role": "user", "content": user_text},
            {"role": "assistant", "content": reply},
        ]
        self.transcript += [("student", user_text), ("mentor", reply)]
        self._maybe_record_quiz_score(reply)

    def _maybe_record_quiz_score(self, reply):
        if self.mode != "quiz":
            return
        m = re.search(r"SCORE:\s*(\d)\s*/\s*5", reply)
        if m and self.topic_id():
            self.progress["quiz_scores"].setdefault(self.topic_id(), []).append(
                int(m.group(1))
            )
            prog.save_progress(PROGRESS_FILE, self.progress)
            print(dim(f"  [score saved for {self.topic_id()}]"))

    # ----- topic selection ------------------------------------------------------

    def _resolve_topic(self, arg):
        """arg -> curriculum item, free-text string, or next planned topic."""
        if arg:
            return prog.find_topic(self.curriculum, arg) or arg
        item = prog.next_topic(self.curriculum, self.progress, self.subject)
        return item

    def _load_topic_file(self):
        """Pin the study file for the current topic as context, if it exists."""
        self.extra_context = ""
        if isinstance(self.topic, dict):
            if self.topic["path"]:
                with open(self.topic["path"], encoding="utf-8", errors="replace") as f:
                    text = f.read()[:15000]
                self.extra_context = (
                    "STUDENT'S STUDY FILE FOR THIS TOPIC (teach from this, follow "
                    "its order and examples):\n\n" + text
                )
            else:
                print(dim(
                    "  [no study file written for this topic yet — "
                    "teaching from model knowledge]"
                ))

    # ----- commands ---------------------------------------------------------------

    def cmd_teach(self, arg):
        topic = self._resolve_topic(arg)
        if topic is None:
            print(yellow("[everything in your plan is marked done — congrats!]"))
            return
        self.mode, self.topic, self.messages = "teach", topic, []
        self._load_topic_file()
        self.progress["last_topic"] = self.topic_id() or str(self.topic)
        prog.save_progress(PROGRESS_FILE, self.progress)
        print(yellow(f"[teach mode · {self.topic_label()}]"))
        self.send(f"Teach me this topic: {self.topic_label()}")

    def cmd_ask(self, _arg):
        self.mode, self.topic, self.messages, self.extra_context = "ask", None, [], ""
        print(yellow("[ask mode — show me code or ask anything; I give hints first]"))

    def cmd_quiz(self, arg):
        topic = self._resolve_topic(arg) if arg else (
            prog.find_topic(self.curriculum, self.progress.get("last_topic") or "")
            or self._resolve_topic("")
        )
        if topic is None:
            print(yellow("[no topic found to quiz on — try /quiz C-05]"))
            return
        self.mode, self.topic, self.messages = "quiz", topic, []
        self._load_topic_file()
        print(yellow(f"[quiz mode · {self.topic_label()}]"))
        self.send(f"Quiz me on: {self.topic_label()}")

    def cmd_review(self, arg):
        if not arg:
            print(yellow("usage: /review <path-to-code-file>"))
            return
        path = os.path.expanduser(arg.strip().strip('"'))
        if not os.path.isfile(path):
            print(yellow(f"file not found: {path}"))
            return
        with open(path, encoding="utf-8", errors="replace") as f:
            code = f.read()[:12000]
        name = os.path.basename(path)
        self.mode, self.topic, self.messages, self.extra_context = (
            "review", name, [], ""
        )
        print(yellow(f"[review mode · {name}]"))
        self.send(f"Please review my code file `{name}`:\n\n```\n{code}\n```")

    def cmd_model(self, arg):
        if arg not in ("big", "fast"):
            print(yellow("usage: /model big|fast"))
            return
        name = self.cfg["models"][arg]
        try:
            if not self.client.has_model(name):
                print(yellow(f"[{name} is not downloaded yet — still pulling?]"))
                return
        except OllamaError as e:
            print(yellow(f"[error: {e}]"))
            return
        self.model_key = arg
        print(yellow(f"[model -> {name}]"))

    def cmd_subject(self, arg):
        if arg not in prog.SUBJECT_PREFIX:
            print(yellow("usage: /subject c|cpp|python"))
            return
        self.subject = arg
        nxt = prog.next_topic(self.curriculum, self.progress, self.subject)
        nxt_label = f"{nxt['id']} {nxt['title']}" if nxt else "all done"
        print(yellow(f"[subject -> {arg} · next planned topic: {nxt_label}]"))

    def cmd_done(self, arg):
        item = prog.find_topic(self.curriculum, arg) if arg else None
        if not item:
            print(yellow("usage: /done <ID>   e.g. /done C-08"))
            return
        if item["id"] not in self.progress["done"]:
            self.progress["done"].append(item["id"])
            prog.save_progress(PROGRESS_FILE, self.progress)
        print(yellow(f"[marked done: {item['id']} {item['title']}]"))

    def cmd_undone(self, arg):
        tid = (arg or "").upper()
        if tid in self.progress["done"]:
            self.progress["done"].remove(tid)
            prog.save_progress(PROGRESS_FILE, self.progress)
            print(yellow(f"[unmarked: {tid}]"))
        else:
            print(yellow(f"[{tid} was not marked done]"))

    def cmd_progress(self, _arg):
        print(prog.summary(self.curriculum, self.progress))
        nxt = prog.next_topic(self.curriculum, self.progress, self.subject)
        if nxt:
            print(f"\nnext planned ({self.subject}): {nxt['id']} {nxt['title']}")

    def cmd_save(self, _arg):
        path = self.save_session()
        print(yellow(f"[saved: {path}]" if path else "[nothing to save yet]"))

    def save_session(self):
        if not self.transcript:
            return None
        os.makedirs(SESSIONS_DIR, exist_ok=True)
        stamp = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        path = os.path.join(SESSIONS_DIR, f"session_{stamp}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"# Mentor session — {stamp}\n\n")
            for role, text in self.transcript:
                f.write(f"**{role}:**\n\n{text}\n\n---\n\n")
        return path

    # ----- main loop ------------------------------------------------------------

    def repl(self):
        nxt = prog.next_topic(self.curriculum, self.progress, self.subject)
        print(cyan("=" * 56))
        print(cyan("  Local AI Mentor  ·  C / C++ / Python"))
        print(cyan(f"  model: {self.model} ({self.model_key})   mode: {self.mode}"))
        if nxt:
            print(cyan(f"  next in your plan: {nxt['id']} {nxt['title']}"))
        print(cyan("  /help for commands · /teach to start the next lesson"))
        print(cyan("=" * 56))

        commands = {
            "/teach": self.cmd_teach, "/ask": self.cmd_ask,
            "/quiz": self.cmd_quiz, "/review": self.cmd_review,
            "/model": self.cmd_model, "/subject": self.cmd_subject,
            "/done": self.cmd_done, "/undone": self.cmd_undone,
            "/progress": self.cmd_progress, "/save": self.cmd_save,
        }
        while True:
            try:
                line = input(f"\n[{self.mode}·{self.subject}·{self.model_key}] > ")
            except (EOFError, KeyboardInterrupt):
                print()
                break
            line = line.strip()
            if not line:
                continue
            if line.startswith("/"):
                parts = line.split(None, 1)
                cmd, arg = parts[0].lower(), (parts[1] if len(parts) > 1 else "")
                if cmd in ("/exit", "/quit", "/q"):
                    break
                if cmd == "/help":
                    print(HELP)
                elif cmd in commands:
                    commands[cmd](arg)
                else:
                    print(yellow(f"unknown command {cmd} — try /help"))
            else:
                self.send(line)

        path = self.save_session()
        if path:
            print(dim(f"[transcript saved: {path}]"))


if __name__ == "__main__":
    Mentor().repl()
