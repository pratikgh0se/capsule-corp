"""A fake `vault_ops` for tests: just the `search` and index name that bin/vault-search calls.
Word-count ranking over the markdown under $OBSIDIAN_VAULT_PATH, limited to `folders`."""
import os
import re
from pathlib import Path

_SEMANTIC_INDEX_FILE = ".obsidian-semantic-index.json"


def search(query, *, limit=6, semantic=None, folders=None):
    vault = Path(os.environ["OBSIDIAN_VAULT_PATH"])
    terms = set(re.findall(r"[a-z0-9]{4,}", query.lower()))
    scored = []
    for md in sorted(vault.rglob("*.md")):
        rel = md.relative_to(vault).as_posix()
        if folders and not rel.startswith(tuple(folders)):
            continue
        low = md.read_text(encoding="utf-8").lower()
        score = sum(low.count(t) for t in terms)
        if score:
            scored.append((-score, rel))
    return [{"path": rel} for _, rel in sorted(scored)[:limit]]
