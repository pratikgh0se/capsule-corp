"""A fake `semantic_search` for tests: the real probe and embed call shapes (an HTTP GET of
/api/tags, an HTTP POST to embed) against $OLLAMA_URL, and an index read from the vault."""
import json
import os
import urllib.request

_RETRY_WAITS = (1, 3, 8, 15)


def _url():
    return os.environ.get("OLLAMA_URL", "http://localhost:11434").rstrip("/")


def ollama_available():
    try:
        with urllib.request.urlopen(f"{_url()}/api/tags", timeout=3) as r:
            return r.status == 200
    except Exception:
        return False


def load_index(vault):
    return json.loads((vault / ".obsidian-semantic-index.json").read_text())


def semantic_search(query, index, limit=10):
    req = urllib.request.Request(f"{_url()}/api/embeddings", data=json.dumps(
        {"prompt": query}).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        json.loads(r.read())
    return [{"path": rel, "title": n["title"]} for rel, n in index["notes"].items()][:limit]
