# Local AI Mentor

A private programming mentor that runs 100% on this laptop (Ollama + RTX 4050).
It teaches C, C++, and Python following the `D:\study` curriculum, and answers
questions Socratically — hints first, full solutions only when asked.

## Use

```bash
mentor            # alias for: python D:/mentor/mentor.py
```

Inside the chat: `/help`. Typical flow:

```
/teach            # lesson on your next planned topic (small steps + exercise)
/quiz C-05        # 5 graded questions, score saved
/review blink.c   # mentor reviews your code
/progress         # curriculum status
```

Browser chat (generic, same models): `webui` → http://localhost:8080

## Models

| Key  | Model            | Use |
|------|------------------|-----|
| big  | qwen3-coder:30b  | default mentor brain (MoE, ~3B active) |
| fast | qwen2.5-coder:7b | quick answers (`/model fast`) |
| —    | nomic-embed-text | embeds `D:\study` notes for retrieval |

## Files

- `mentor.py` — the chat program (modes: ask / teach / quiz / review)
- `prompts.py` — mentor personality and mode rules
- `ollama_api.py` — minimal Ollama client (stdlib only, no pip installs)
- `rag.py` — `python rag.py build` re-indexes the study notes into `index.db`
- `progress.py` — curriculum order (from `D:\study\INDEX.md`) + `progress.json`

Re-run `python rag.py build` whenever notes in `D:\study` change.
