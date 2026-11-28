#!/usr/bin/env python3
"""ai_usage.py - hourly sampler: how many tokens did the agents burn in the last 24 h."""
import glob
import json
import os
import sys
import time
from pathlib import Path

import events
from pricing import FALLBACK_MODEL, PRICES_PATH, load_prices, price_row  # noqa: F401  (re-exported)

WINDOW_H = 24
MTIME_WINDOW_S = 25 * 3600  # a transcript may be appended to after its last in-window turn
STATE = events.state_root() / "samplers.json"
CLAUDE_ROOT = Path(os.path.expanduser("~/.claude"))


# --- the hourly stamp, shared by both samplers ------------------------------------

def read_stamp(state_path=None):
    try:
        return json.loads(Path(state_path or STATE).read_text())
    except Exception:
        return {}


def write_stamp(name, when, state_path=None):
    p = Path(state_path or STATE)
    data = read_stamp(p)          # merge, so the other sampler's stamp survives
    data[name] = float(when)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except Exception as exc:
        print(f"samplers: stamp not written ({exc})", file=sys.stderr)


def due(name, now, state_path=None, every_s=3600):
    return float(read_stamp(state_path).get(name) or 0) <= now - every_s


# --- Claude Code -------------------------------------------------------------------

def _iso_epoch(s):
    try:
        import datetime
        return datetime.datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


_USAGE_KEYS = ("input_tokens", "output_tokens", "cache_read_input_tokens",
               "cache_creation_input_tokens")


def _parse_turn(line, mtime, cutoff):
    """(key, model, usage) for one in-window transcript line, else None. Reads no message text."""
    try:
        r = json.loads(line)
    except Exception:
        return None
    if not isinstance(r, dict):
        return None
    msg = r.get("message") if isinstance(r.get("message"), dict) else {}
    model = r.get("model") or msg.get("model")
    usage = r.get("usage") if isinstance(r.get("usage"), dict) else msg.get("usage")
    if not model or model == "<synthetic>" or not isinstance(usage, dict):
        return None
    ts = _iso_epoch(r.get("timestamp")) if r.get("timestamp") else None
    if (ts if ts is not None else mtime) < cutoff:
        return None
    mid = msg.get("id") or r.get("id")
    key = (mid, r.get("requestId")) if mid else (("uuid", r["uuid"]) if r.get("uuid") else None)
    return key, model, {k: int(usage.get(k) or 0) for k in _USAGE_KEYS}


def _scan_file(f, now, cutoff, seen, loose):
    mtime = os.path.getmtime(f)
    if mtime < now - MTIME_WINDOW_S:
        return
    with open(f, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            turn = _parse_turn(line, mtime, cutoff)
            if turn is None:
                continue
            key, model, got = turn
            if key is None:
                loose.append((model, got))
            elif key in seen:
                cur = seen[key][1]
                cur.update({k: max(cur[k], got[k]) for k in _USAGE_KEYS})
            else:
                seen[key] = (model, got)


def scan_claude_code(root=None, now=None):
    """[{model, turns, input, output, cache_read, cache_write}] for the window.

    Walks every transcript under projects/ (subagent files included), buckets each record by its
    own timestamp, and counts one turn per (message.id, requestId) (else uuid): a message is written
    several times while streaming, so the per-field maximum wins (same rule as capsule/eval_tokens).
    Only usage, model, ids and timestamp are read; message text is never kept.
    """
    now = now or time.time()
    cutoff = now - WINDOW_H * 3600
    seen, loose = {}, []   # dedupe key -> (model, max usage); id-less records counted as they come
    try:
        files = sorted(glob.glob(str(Path(root or CLAUDE_ROOT) / "projects" / "**" / "*.jsonl"),
                                 recursive=True))
    except Exception as exc:
        print(f"ai_usage: claude root unreadable ({exc})", file=sys.stderr)
        return []
    for f in files:
        try:
            _scan_file(f, now, cutoff, seen, loose)
        except Exception as exc:
            print(f"ai_usage: transcript {f}: {exc}", file=sys.stderr)
    agg = {}
    for model, u in list(seen.values()) + loose:
        a = agg.setdefault(model, {"model": model, "turns": 0, "input": 0, "output": 0,
                                   "cache_read": 0, "cache_write": 0})
        a["turns"] += 1
        a["input"] += u["input_tokens"]
        a["output"] += u["output_tokens"]
        a["cache_read"] += u["cache_read_input_tokens"]
        a["cache_write"] += u["cache_creation_input_tokens"]
    return sorted(agg.values(), key=lambda r: r["model"])


# --- the sampler --------------------------------------------------------------------

def sample(now=None, spool=None, state_path=None, claude_root=None, force=False):
    """One `ai_usage` event, at most once an hour. Returns the payload, or None if skipped."""
    try:
        now = now or time.time()
        if not force and not due("ai_usage", now, state_path):
            return None
        cc = [price_row(r) for r in scan_claude_code(claude_root, now)]
        payload = {"window_h": WINDOW_H, "claude_code": cc}
        total = sum(r["input"] + r["output"] + r["cache_read"] + r["cache_write"]
                    for r in cc)
        events.log("ai_usage", "ai_usage.py",
                   f"{total} tokens in {WINDOW_H}h "
                   f"({len(cc)} claude code models)",
                   payload=payload, spool=spool)
        write_stamp("ai_usage", now, state_path)
        return payload
    except Exception as exc:
        print(f"ai_usage: sample failed: {exc}", file=sys.stderr)
        return None


if __name__ == "__main__":
    print(json.dumps(sample(force=True), indent=1))
