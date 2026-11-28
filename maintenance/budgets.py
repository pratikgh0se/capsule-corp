#!/usr/bin/env python3
"""budgets.py - every threshold cluster C2 introduces, in one place."""
import contextlib
import datetime as _dt
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from datetime import date as _date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # `capsule` (C24a)

import events  # noqa: E402

MAINT = Path(__file__).resolve().parent
STATE = events.state_root()
BATCH_NAME = "brief-batch.json"
TURNS_NAME = "diff-turns.json"
# Lazily resolved (2026-09-11 incident: `unittest discover` wrote 44 real `brief-
# batch.json` rows for a fake card).
BATCH = None
TURNS = None

# --- the thresholds (row 126) ------------------------------------------------
CARD_COST_WARN = 1.80      # p90 x1.6; M015
CARD_COST_PARK = 2.20      # p90 x2
CARD_RUNS_WARN = 4         # measured p90
CARD_RUNS_PARK = 6         # p90 x1.5 - stops both of last week's worst cards
FILES_ROUTE = 5            # M032: over this, route to the reclassify rung
FILES_PARK = 10            # M032: over this, park
DIFF_LINES_PARK = 0        # M032: real diff lines over this are not landed; 0 = off
EMPTY_DIFF_TURNS = 2       # M018: two turns that moved no diff
DECISION_WARN = 8          # M025
DECISION_HOLD = 10
DECISION_R3_MAX = 2
R3_QUIET_HOURS = (4, 7)    # M030: no R3 raised in [04:00, 07:00) local
REFIT_DATE = "2026-09-24"

BATCH_CAP = 100
BRIEF_WINDOW_S = 24 * 3600
TURNS_TTL_S = 24 * 3600    # A card's diff-turn run expires after a day
BATCH_PRUNE_S = 7 * 24 * 3600
PARK_MARK = "budget: "
DECISION_LOG_DEFAULT = "Decisions/Capsule Corp - System design decisions 2026-09-06.md"

FLAGS = {"card": "OPS_CARD_BUDGET",
         "decision": "OPS_DECISION_BUDGET",
         "route": "OPS_ROUTE_BIG_CARDS"}

# key -> (ops.env name, default, cast).
_OVERRIDES = {
    "cost_warn":       ("OPS_CARD_COST_WARN", CARD_COST_WARN, float),
    "cost_park":       ("OPS_CARD_COST_PARK", CARD_COST_PARK, float),
    "runs_warn":       ("OPS_CARD_RUNS_WARN", CARD_RUNS_WARN, int),
    "runs_park":       ("OPS_CARD_RUNS_PARK", CARD_RUNS_PARK, int),
    "files_route":     ("OPS_FILES_ROUTE", FILES_ROUTE, int),
    "files_park":      ("OPS_FILES_PARK", FILES_PARK, int),
    "diff_lines_park": ("OPS_DIFF_LINES_PARK", DIFF_LINES_PARK, int),
    "empty_turns":     ("OPS_EMPTY_DIFF_TURNS", EMPTY_DIFF_TURNS, int),
    "decision_warn":   ("OPS_DECISION_WARN", DECISION_WARN, int),
    "decision_hold":   ("OPS_DECISION_HOLD", DECISION_HOLD, int),
    "decision_r3_max": ("OPS_DECISION_R3_MAX", DECISION_R3_MAX, int),
}


def enabled(flag):
    """True only for an explicit `1`. Default off (row 126)."""
    return os.environ.get(FLAGS[flag]) == "1"


def thresholds():
    """The constants with any ops.env override applied. A malformed value falls
    back to the constant - `usage_guard._floors`' rule."""
    out = {}
    for key, (env, default, cast) in _OVERRIDES.items():
        raw = os.environ.get(env)
        try:
            out[key] = cast(raw) if raw not in (None, "") else default
        except (TypeError, ValueError):
            out[key] = default
    return out


# --- the per-card brake ------------------------------------------------------

def _capsule_spend(board, task_id, home=None):
    """Capsule's own runs: tokens sit on task_runs itself (no session join)."""
    out = {"runs": 0, "cost_usd": 0.0, "unjoined": 0}
    from capsule import paths
    root = paths.home(Path(home) / ".local" / "share" / "capsule" if home else None)
    db = paths.board_db(str(board), root)
    try:
        with contextlib.closing(sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)) as con:
            rows = list(con.execute(
                "select coalesce(model,''), coalesce(tokens_in,0), coalesce(tokens_out,0),"
                " coalesce(tokens_cache_read,0), coalesce(tokens_cache_write,0)"
                " from task_runs where task_id = ?", (str(task_id),)))
    except sqlite3.Error:
        return out
    import ai_usage
    out["runs"] = len(rows)
    out["cost_usd"] = round(sum(float(ai_usage.price_row(
        {"model": m, "input": i, "output": o, "cache_read": cr, "cache_write": cw}
    ).get("usd_estimate") or 0.0) for m, i, o, cr, cw in rows), 6)
    return out


def card_spend(board, task_id, *, boards_dir=None, home=None):
    """{"runs", "cost_usd", "unjoined"} for ONE card. `boards_dir` is accepted for
    board_bridge's call shape and ignored: Capsule boards live under CAPSULE_HOME."""
    return _capsule_spend(board, task_id, home)


def card_verdict(spend, th=None):
    """("ok"|"warn"|"park", reason). Cost is the yardstick (row 126 answer 1);
    runs are the second axis, because both of last week's worst cards were
    run-count shaped."""
    th = th or thresholds()
    runs = int((spend or {}).get("runs") or 0)
    cost = float((spend or {}).get("cost_usd") or 0.0)
    if cost >= th["cost_park"]:
        return "park", f"cost ${cost:.2f} over ${th['cost_park']:.2f} ({runs} runs)"
    if runs >= th["runs_park"]:
        return "park", f"{runs} runs over {th['runs_park']} (cost ${cost:.2f})"
    if cost >= th["cost_warn"] or runs >= th["runs_warn"]:
        return "warn", f"${cost:.2f} / {runs} runs"
    return "ok", ""


def park_reason(reason):
    """The park text a budget stop writes, so `is_budget_reason` can recognise
    it on every later tick and keep the park quiet."""
    return PARK_MARK + str(reason)


def is_budget_reason(reason):
    """True when this block reason was written by a budget stop. The card's own
    reason is the state - there is no second file to fall out of sync."""
    text = str(reason or "")
    for prefix in ("NEEDS REPAIR: ", "repair: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text.startswith(PARK_MARK)


def budget_detail(reason):
    """The reason without either prefix - what the brief line carries."""
    text = str(reason or "").split("\n")[0]
    for prefix in ("NEEDS REPAIR: ", "repair: "):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    return text[len(PARK_MARK):] if text.startswith(PARK_MARK) else text


# --- the batch (M024/M027) ---------------------------------------------------

def state_dir(create=False):
    """Same override-or-refuse rule as verdicts.verdict_dir() / merge_gate."""
    override = os.environ.get("OPS_BUDGET_STATE")
    if not override and os.environ.get("AGENT_OPS_NO_SEND"):
        raise RuntimeError("refusing to touch the default budget state under test: "
                            "set OPS_BUDGET_STATE explicitly")
    p = Path(override) if override else STATE
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def _batch_path(explicit=None):
    """An explicit `path=` kwarg wins, then a module-level `budgets.BATCH`
    override (the `mock.patch.object(budgets, "BATCH", tmp)` idiom already
    used throughout the tests), then `state_dir()`'s guarded default."""
    if explicit is not None:
        return Path(explicit)
    if BATCH is not None:
        return Path(BATCH)
    return state_dir(create=True) / BATCH_NAME


def _turns_path(explicit=None):
    if explicit is not None:
        return Path(explicit)
    if TURNS is not None:
        return Path(TURNS)
    return state_dir(create=True) / TURNS_NAME


def _read_json(path, default):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return data if isinstance(data, type(default)) else default


def _write_json(path, data):
    """Never raises into a caller: this runs inside dispatch, repair and the
    quality gate - the events.py:107 discipline."""
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data), encoding="utf-8")
        return True
    except OSError as exc:
        print(f"budgets: {path} not written ({exc})", file=sys.stderr)
        return False


def defer(kind, text, *, board=None, task_id=None, path=None, now=None):
    """Put one suppressed line into the 08:30 brief instead of dropping it
    (M027) or paging (M024). Returns the queue length, 0 if it could not be
    written. Rows older than BATCH_PRUNE_S (7 days) are dropped, so a queued
    brainstorm drops out of the brief after 7 days."""
    p = _batch_path(path)
    now = float(time.time() if now is None else now)
    rows = [r for r in _read_json(p, [])
            if isinstance(r, dict) and now - float(r.get("ts") or 0) <= BATCH_PRUNE_S]
    rows.append({"ts": now, "kind": str(kind), "board": board,
                 "task_id": task_id, "text": str(text)[:200]})
    rows = rows[-BATCH_CAP:]
    return len(rows) if _write_json(p, rows) else 0


def brief_lines(now=None, path=None):
    """The last 24 h as printable lines, pruning anything past 7 days. An entry
    is NOT deleted when it is printed: a failed send re-prints it tomorrow
    rather than losing it."""
    p = _batch_path(path)
    now = float(time.time() if now is None else now)
    rows = [r for r in _read_json(p, []) if isinstance(r, dict)]
    keep = [r for r in rows if now - float(r.get("ts") or 0) <= BATCH_PRUNE_S]
    if len(keep) != len(rows):
        _write_json(p, keep)
    out = []
    for r in keep:
        if now - float(r.get("ts") or 0) > BRIEF_WINDOW_S:
            continue
        if str(r.get("text") or "").startswith("Brainstorm - ") and re.fullmatch(
                r"[0-9a-f]{16}", str(r.get("task_id") or "")):
            continue       # the brief's waiting block already shows it, with the title
        where = "/".join(str(x) for x in (r.get("board"), r.get("task_id")) if x)
        prefix = f"{r.get('kind')}: " + (f"{where} - " if where else "")
        out.append(prefix + str(r.get("text") or ""))
    return out


# --- the stop condition (M017/M018) ------------------------------------------

def _line_text(lines):
    """`_git_added_lines` / `_git_removed_lines` give `[(lineno, text), ...]`;
    only the TEXT is folded in - a pure reflow that moves a line without
    changing it is still an empty turn."""
    out = []
    for item in lines or []:
        out.append(str(item[1]) if isinstance(item, (tuple, list)) and len(item) > 1 else str(item))
    return out


def diff_fingerprint(changed, added=None, removed=None):
    """A digest of the branch diff: one `path:+n:-n` triple per touched file, sorted,
    PLUS the added/removed line content (counts alone call three
    different rewrites of the same line an empty turn, and let a stuck loop that
    shuffles counts walk past the brake)."""
    added, removed = added or {}, removed or {}
    parts, body = [], []
    for path in sorted({*(changed or []), *added, *removed}):
        a, r = _line_text(added.get(path)), _line_text(removed.get(path))
        parts.append(f"{path}:+{len(a)}:-{len(r)}")
        body.append(path + "\n+" + "\n+".join(a) + "\n-" + "\n-".join(r))
    digest = hashlib.sha1("\n".join(body).encode("utf-8", "replace")).hexdigest()
    return hashlib.sha1(("\n".join(parts) + "\n" + digest).encode("utf-8")).hexdigest()[:16]


def record_turn(board, task_id, fp, *, path=None):
    """Length of the current run of identical fingerprints for this card."""
    p = _turns_path(path)
    state = _read_json(p, {})
    now = time.time()
    # Expire stale cards the way `defer` prunes the batch: a run of
    # identical rounds from a cut of the card a week ago must not still be counting
    # against it.
    state = {k: v for k, v in state.items()
             if isinstance(v, dict) and now - float(v.get("ts") or 0) <= TURNS_TTL_S}
    key = f"{board}/{task_id}"
    row = state.get(key) if isinstance(state.get(key), dict) else {}
    n = int(row.get("n") or 0) + 1 if row.get("fp") == fp else 1
    state[key] = {"fp": fp, "n": n, "ts": now}
    _write_json(p, state)
    return n


def clear_turns(board, task_id, *, path=None):
    """Forget a card's diff history - called on `quality: pass`."""
    p = _turns_path(path)
    state = _read_json(p, {})
    if state.pop(f"{board}/{task_id}", None) is not None:
        _write_json(p, state)


# --- the decision budget (M025/M026/M030) ------------------------------------

def override():
    """The owner's escape hatch: OPS_DECISION_OVERRIDE=1 in ops.env lifts today's
    hold entirely (row 126 - the budget must never be able to refuse HIM)."""
    return os.environ.get("OPS_DECISION_OVERRIDE") == "1"


def in_quiet_hours(now=None, hours=R3_QUIET_HOURS):
    """M030: no R3 is raised in [04:00, 07:00) local - it queues to the brief."""
    lt = time.localtime(time.time() if now is None else now)
    return hours[0] <= lt.tm_hour < hours[1]


def decision_count(vault=None, today=None, log_path=None):
    """{"cards", "log", "r3", "total"} for today, from two sources."""
    out = {"cards": 0, "log": 0, "r3": 0, "total": 0}
    day = (today or _dt.date.today()).isoformat()
    if not vault:
        import vault_conf   # only the decision count needs it; the brief and budget lines must not
        vault = vault_conf.vault()
    try:
        notes = sorted((Path(vault) / "Decisions").glob("*/*.md")) if vault else []
    except OSError:
        notes = []
    for note in notes:
        if not note.name.startswith(day):
            continue
        out["cards"] += 1
        try:
            head = note.read_text(encoding="utf-8", errors="replace")[:2000]
        except OSError:
            continue
        if re.search(r"^class:\s*R3\s*$", head, re.M | re.I):
            out["r3"] += 1
    log = Path(log_path or os.environ.get("OPS_DECISION_LOG")
               or (vault / DECISION_LOG_DEFAULT))
    try:
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("|") and day in line:
                out["log"] += 1
    except OSError:
        pass
    out["total"] = out["cards"] + out["log"]
    return out


def decision_verdict(counts, th=None, cls="R3"):
    """("ok"|"warn"|"hold", reason) for raising one more DECISION of class
    `cls`. A hold never drops the decision: the caller defers it into the
    brief and the next tick after midnight raises it."""
    th = th or thresholds()
    if override():
        return "ok", "override"
    total = int((counts or {}).get("total") or 0)
    r3 = int((counts or {}).get("r3") or 0)
    if total >= th["decision_hold"]:
        return "hold", f"{total}/{th['decision_hold']} decisions today"
    if str(cls).upper() == "R3" and r3 >= th["decision_r3_max"]:
        return "hold", f"{r3} R3 decisions today (max {th['decision_r3_max']})"
    if total >= th["decision_warn"]:
        return "warn", f"{total}/{th['decision_hold']} decisions today"
    return "ok", f"{total}/{th['decision_hold']}"


def decision_line(counts=None, th=None):
    """The one line status.sh and the brief print."""
    counts = counts if counts is not None else decision_count()
    th = th or thresholds()
    return (f"decisions: {counts['total']}/{th['decision_hold']} today "
            f"({counts['cards']} cards, {counts['log']} log rows; {counts['r3']} R3)")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if "--brief" in argv:
        for line in brief_lines():
            print(line)
        return 0
    if "--decisions" in argv:
        counts = decision_count()
        th = thresholds()
        state, _why = decision_verdict(counts, th, cls="R1")
        print(decision_line(counts, th))
        return 1 if state in ("warn", "hold") else 0
    print("usage: budgets.py --brief | --decisions", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
