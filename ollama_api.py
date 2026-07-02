"""Tiny Ollama client — Python standard library only (urllib).

Endpoints used:
  /api/tags   - list installed models
  /api/chat   - chat with streaming
  /api/embed  - batch embeddings
"""

import json
import urllib.error
import urllib.request


class OllamaError(Exception):
    pass


class Ollama:
    def __init__(self, base_url="http://localhost:11434"):
        self.base_url = base_url.rstrip("/")

    def _open(self, path, payload=None, timeout=600):
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            headers={"Content-Type": "application/json"},
        )
        try:
            return urllib.request.urlopen(req, timeout=timeout)
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", "")
            except Exception:
                detail = ""
            raise OllamaError(f"Ollama HTTP {e.code}: {detail or e.reason}") from e
        except urllib.error.URLError as e:
            raise OllamaError(
                f"cannot reach Ollama at {self.base_url} — is it running? ({e.reason})"
            ) from e

    def tags(self):
        """Names of all installed models."""
        with self._open("/api/tags", timeout=10) as r:
            return [m["name"] for m in json.load(r).get("models", [])]

    def has_model(self, name):
        tags = self.tags()
        return name in tags or (":" not in name and name + ":latest" in tags)

    def chat_stream(self, model, messages, num_ctx=8192, keep_alive="30m"):
        """Yield (token, final_stats). final_stats is None until the last chunk,
        where token is "" and final_stats holds eval_count / eval_duration etc."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": True,
            "keep_alive": keep_alive,
            "options": {"num_ctx": num_ctx},
        }
        with self._open("/api/chat", payload) as r:
            for line in r:
                line = line.strip()
                if not line:
                    continue
                chunk = json.loads(line)
                if chunk.get("error"):
                    raise OllamaError(chunk["error"])
                if chunk.get("done"):
                    yield "", chunk
                    return
                yield chunk.get("message", {}).get("content", ""), None

    def embed(self, texts, model, timeout=300):
        """Batch-embed a list of strings; returns list of vectors."""
        payload = {"model": model, "input": texts}
        with self._open("/api/embed", payload, timeout=timeout) as r:
            return json.load(r)["embeddings"]
