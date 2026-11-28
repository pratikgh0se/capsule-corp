"""Stall supervisor, unit 3: the text a continue sends, and the small readers it needs. Split from
`stall_continue`, which imports every name here."""
import json
import re

UNCHECKED = re.compile(r"^\s*[-*]\s*\[ \]\s*(.+?)\s*$", re.M)
NOBODY = ("Nobody is here to answer. If one command can undo the choice, choose by the card and "
          "the pushed docs, write the choice in your handoff, and continue. If not, end with a "
          "block that gives the options and the one you recommend.")
HARNESS_TEXT = ("The harness performs this step itself (it replays every `untrack` request on "
                "each clone). Do not wait for it and do not block on it: continue with the rest "
                "of the card.")


def _items(handoff, *keys):
    out = []
    for key in keys:
        out += [str(x) for x in (handoff or {}).get(key) or [] if x]
    return out


def _json(text):
    try:
        got = json.loads(text) if text else None
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def _closed(conn, run):
    row = conn.execute("SELECT payload FROM task_events WHERE run_id=? AND kind='run_closed'"
                       " ORDER BY id DESC LIMIT 1", (run["id"],)).fetchone()
    return _json(row[0]) if row else None


def missing(conn, task, run):
    """What is missing, from facts: the card's unchecked done-when lines, the run's undone
    items, then the sentence the quiet-stop kind needs. No model."""
    ev = _closed(conn, run) or {}
    got = ev.get("kind") or run["outcome"]
    lines = UNCHECKED.findall(task["body"] or "")[:8] or ["the done-when list of the card"]
    out = ["Not done yet: " + "; ".join(lines) + "."]
    left = _items(_json(run["handoff"]), "left_undone", "remaining")[:6]
    if left:
        out.append("Your handoff left undone: " + "; ".join(left) + ".")
    if got == "run_asked":
        ask = (_json(run["handoff"]) or {}).get("question") or (ev.get("after") or {}).get(
            "block_reason") or run["summary"] or "you stopped to ask"
        out.append(f"Your question was: {str(ask)[:600]}")
        out.append(NOBODY)
    elif got == "run_harness":
        out.append(HARNESS_TEXT)
    elif got == "run_silent":
        out.append("Your last run went silent: it made no progress and was stopped.")
    return " ".join(out)
