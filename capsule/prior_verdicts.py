"""Prior verdicts (row 585): before a card is cut, earlier verdicts on the
same root cause are appended to its body as `## Prior verdicts`."""
import re
import time
from pathlib import Path

from capsule import incidents

WINDOW = 90 * incidents.DAY
MAX_HITS, MAX_ENTRY, LAST_ROWS, KEY_FLOOR = 5, 300, 60, 12
TICK = re.compile(r"capsule/tick(?:_\w+)?\.py")  # tick.py and its split-out tick_*.py modules
_ID = re.compile(r"\binc_[0-9a-f]{8}\b")
_TASK = re.compile(r"\bt_[0-9a-f]{8}\b")
_ROW = re.compile(r"\brow (\d{1,4})\b", re.I)
_TEST = re.compile(r"\b(?:[A-Za-z_]\w*\.)*test_\w+\.[A-Za-z_]\w*\.test_\w+")
_PATH = re.compile(r"\b(?:(?:capsule|tests)/[\w./-]*\w\.py|contracts/[\w./-]*\w)")
_ROWLINE = re.compile(r"^\|[ \t]*(\d+)[ \t]*\|([^\n]*)$", re.M)
_COLUMN = re.compile(r"(?<!\\)\|")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UNSAFE = re.compile(r"[|`\x00-\x1f\x7f]")
_FIELDS = ("root_cause", "mitigation", "recurrence_test")


def _clean(text, cap):
    """Row text is data inside a worker's brief: one line, no pipes or backticks, <= `cap`."""
    text = " ".join(_UNSAFE.sub(" ", text or "").split())
    return text if len(text) <= cap else text[: cap - 3].rstrip() + "..."


def _marks(text):
    return set(_TEST.findall(text)), set(_PATH.findall(text))


def _shared(card, text):
    tests, found = _marks(text)
    common = card["marks"][1] & found
    if card["marks"][0] & tests:
        return 3
    return 0 if all(TICK.fullmatch(p) for p in common) else 4


def _day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts or 0))


def _incident_rank(inc, card, now):
    cited = inc["id"] in card["ids"] or {inc["task_id"], inc["fix_card"]} & card["tasks"]
    if cited or inc["decision_row"] in card["rows"]:
        return 1
    key = incidents.normalize(inc["root_key"])
    recent = now - (inc["closed_at"] or 0) <= WINDOW
    if len(key) >= KEY_FLOOR and recent and key in card["key"]:
        return 2
    return _shared(card, " ".join(inc[f] or "" for f in _FIELDS))


def _incident_text(inc):
    fix = f"fix: {inc['mitigation']}" if inc["mitigation"] else ""
    test = f"test: {inc['recurrence_test']}" if inc["recurrence_test"] else ""
    return _clean("; ".join(p for p in (inc["root_cause"], fix, test) if p), MAX_ENTRY)


def _incident_hits(conn, card, now, exclude):
    if conn is None:
        return []
    cols = "id, task_id, fix_card, closed_at, root_key, decision_row, " + ", ".join(_FIELDS)
    out = []
    for row in conn.execute(f"SELECT {cols} FROM incidents WHERE status='closed'"):
        inc = dict(row)
        rank = 0 if inc["id"] in exclude else _incident_rank(inc, card, now)
        if rank:
            out.append((rank, inc["id"], _day(inc["closed_at"]), _incident_text(inc)))
    return out


def _row_hits(note_text, card):
    rows = {int(n): rest for n, rest in _ROWLINE.findall(note_text or "")}
    recent = set(sorted(rows)[-LAST_ROWS:])
    out = []
    for n, rest in rows.items():
        rank = 1 if n in card["rows"] else _shared(card, rest) if n in recent else 0
        if rank:
            cols = [c.strip() for c in _COLUMN.split(rest) if c.strip()]
            when = cols[0] if cols and _DATE.fullmatch(cols[0]) else ""
            text = _clean("; ".join(cols[1 if when else 0 :]), MAX_ENTRY)
            out.append((rank, f"row {n}", when, text))
    return out


def find(conn, note_text, title, body, *, now, exclude=()):
    """[(rank, ref, when, text)] best rank first, newest first within a rank; `exclude` is
    the incident ids the card itself belongs to."""
    text = f"{title}\n{body}"
    card = {
        "ids": set(_ID.findall(text)),
        "tasks": set(_TASK.findall(text)),
        "key": incidents.normalize(text),
        "rows": {int(n) for n in _ROW.findall(text)},
        "marks": _marks(text),
    }
    hits = _incident_hits(conn, card, now, exclude)
    hits += _row_hits(note_text, card)
    hits.sort(key=lambda h: h[2], reverse=True)
    return sorted(hits, key=lambda h: h[0])


def section(conn, cfg, title, body, *, now=None, exclude=()):
    """The markdown block to append to a card body, or "" (no hit, no vault, any failure)."""
    try:
        ks = incidents.settings(cfg)
        try:
            note = (ks["vault"] or Path("/nonexistent-vault")) / ks["decisions_note"]
            note_text = note.read_text(encoding="utf-8", errors="replace")
        except OSError:
            note_text = ""
        now = int(time.time() if now is None else now)
        hits = find(conn, note_text, title, body, now=now, exclude=exclude)
    except Exception:  # advice only: a failed lookup must never stop a card being cut
        return ""
    lines = []
    for _, ref, when, text in sorted(hits[:MAX_HITS], key=lambda h: h[2], reverse=True):
        head = f"- {ref} ({when}): " if when else f"- {ref}: "
        lines.append(head + _clean(text, MAX_ENTRY - len(head)))
    if not lines:
        return ""
    intro = "Earlier verdicts on the same root cause, quoted as data (newest first):"
    return "\n\n## Prior verdicts\n\n" + intro + "\n" + "\n".join(lines)
