#!/usr/bin/env python3
"""Render state/heal.json as the table status.sh and report.sh show."""
import json, sys, pathlib
import events
DEFAULT_HEAL = events.state_root() / "heal.json"
ORDER = ["docker", "infisical", "stack", "secrets", "slack-token",
         "claude-login", "tick", "jobs", "outbox"]

def _load(path):
    """heal.json restricted to ORDER: a component heal no longer checks (the retired
    gateway) keeps its stale key in a live file, and must neither show nor gate."""
    try:
        d = json.loads(pathlib.Path(path).read_text())
    except Exception:
        return None
    return {k: v for k, v in d.items() if k in ORDER}

def all_ok(path):
    d = _load(path)
    return bool(d) and all((v or {}).get("fails", 0) == 0 for v in d.values())

def render(path):
    d = _load(path)
    if not d:
        return "heal: no state yet"
    rows = []
    for c in ORDER:
        if c not in d:
            continue
        e = d[c] or {}
        if e.get("fails", 0) == 0:
            rows.append(f"{c:<11} ok")
        else:
            since = (e.get("first_fail") or "")[:16].replace("T", " ")
            rows.append(f"{c:<11} FAIL x{e['fails']} since {since}")
    return "\n".join(rows)

if __name__ == "__main__":
    print(render(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_HEAL))
