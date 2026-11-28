"""The invariant incident kinds and their detectors (off-the-old-system plan C15b)."""
import os
import subprocess
import sys
from pathlib import Path

from capsule import db, incidents, outbox

KINDS = ("two_dispatchers", "main_checkout_write", "lost_card_state")
EXEMPT_PREFIXES = ("scratchpad/", ".worktrees/")
PAUSE_S = 10 * 365 * 86400            # until the owner clears `pause:board`
SNAPSHOT = "invariants:status"
IMPLIES = {"completed": ("done", "archived"), "blocked": ("blocked", "triage", "archived"),
           "archived": ("archived",), "closed": ("done", "archived")}
STATE_EVENTS = ("created", "completed", "blocked", "block_loop_detected", "unblocked", "claimed",
                "promoted", "parked_by_human", "parked", "failure_limit", "failed",
                "request_review", "reclaimed", "failure_reset", "archived", "closed",
                "first_screen_fix")
REQUEUE = ("ready", "todo")


def settings(cfg):
    """`dev_checkout` has no fallback: under C08 the code runs from the release clone, so
    `korin.main_checkout`'s default would watch the wrong (always clean) tree."""
    block = (cfg or {}).get("invariants") or {}
    enabled = bool(block.get("enabled", False))
    if enabled and not block.get("dev_checkout"):
        raise ValueError("invariants.enabled needs invariants.dev_checkout (the working tree)")
    return {"enabled": enabled, "dev_checkout": Path(block["dev_checkout"]) if enabled else None}


def _dirty(checkout):
    cmd = ["git", "-C", str(checkout), "status", "--porcelain"]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    rows = [row for row in res.stdout.splitlines() if len(row) > 3 and not row.startswith("??")]
    names = [row[3:].split(" -> ")[-1].strip('"') for row in rows]
    return [n for n in names if not n.startswith(EXEMPT_PREFIXES)]


def _main_checkout_write(checkout, *, cwd=None):
    """(cwd evidence, dirty-tree evidence): the first pauses, the second only pages."""
    dirty = [f"dirty: {n}" for n in _dirty(checkout)[:10]]
    out = []
    here = Path(cwd or os.getcwd()).resolve()
    root = Path(checkout).resolve()
    exempt = [root / d.rstrip("/") for d in EXEMPT_PREFIXES]
    inside = here == root or root in here.parents
    if inside and not any(e == here or e in here.parents for e in exempt):
        out.append(f"the tick's cwd is the dev checkout: {here}")
    return out, dirty


def _lost_card_state(conn):
    """Snapshot {"baseline": newest event id on the first pass, "cards": {id: [status, top]}}:
    history up to the baseline is never judged by the static rule (it predates the detector)."""
    out = []
    snap, version = db.guard_get(conn, SNAPSHOT)
    if not snap:
        newest = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events").fetchone()[0]
        snap = {"baseline": newest, "cards": {}}
    cards = {}
    marks = ",".join("?" * len(STATE_EVENTS))
    for t in conn.execute("SELECT id, status FROM tasks").fetchall():
        top = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=?",
                           (t["id"],)).fetchone()[0]
        last = conn.execute(f"SELECT id, kind FROM task_events WHERE task_id=?"
                            f" AND kind IN ({marks}) ORDER BY id DESC LIMIT 1",
                            (t["id"], *STATE_EVENTS)).fetchone()
        fresh = last is not None and last["id"] > snap["baseline"]
        want = IMPLIES.get(last["kind"]) if fresh else None
        if want and t["status"] not in want:
            out.append(f"{t['id']}: status {t['status']} but newest event is {last['kind']}")
        before = snap["cards"].get(t["id"])
        if before and before[0] != t["status"] and top <= before[1] and t["status"] not in REQUEUE:
            out.append(f"{t['id']}: status {before[0]} -> {t['status']} with no event")
        cards[t["id"]] = [t["status"], top]
    out += [f"{i}: card gone (was {v[0]})" for i, v in snap["cards"].items() if i not in cards]
    if cards != snap["cards"] or version == 0:
        db.guard_set(conn, SNAPSHOT, {"baseline": snap["baseline"], "cards": cards},
                     expect_version=version)
    return out


def _raise(conn, board, kind, key, evidence, now, *, pause):
    incident_id, action = incidents.open_incident(conn, board=board, kind=kind, key=key,
                                                  evidence=evidence, now=now)
    if action not in ("deduped", "recurred"):    # the RECURRED page is already the alert
        text = "; ".join(evidence)[:300]
        head = f"{board} {'PAUSED by ' if pause else ''}{kind} ({incident_id})"
        with db.txn(conn):
            if pause:
                db.guard_set(conn, "pause:board",
                             {"until": now + PAUSE_S, "reason": kind, "incident": incident_id},
                             now=now)
            outbox.notify(conn, "needs_input", head, fields=[("evidence", text)], now=now,
                          file_body=f"capsule: {head}: {text}")
    return incident_id


def sweep(conn, cfg, *, now, cwd=None):
    """Run every probe once; the incident ids raised or re-seen."""
    ks = settings(cfg)
    if not ks["enabled"]:
        return []
    board = cfg["board"]
    in_cwd, dirty = _main_checkout_write(ks["dev_checkout"], cwd=cwd)
    found = (("main_checkout_write", "tick cwd in the dev checkout", in_cwd, True),
             ("main_checkout_write", "dev checkout dirty", dirty, False),
             ("lost_card_state", "card status vs events", _lost_card_state(conn), False))
    return [_raise(conn, board, kind, key, ev, now, pause=pause)
            for kind, key, ev, pause in found if ev]


def count(conn, cfg, now, touched):
    """The tick's one call site: incidents `from_events` touched plus this pass's sweep. A crashing
    probe is printed and counted out, never allowed to skip Korin dispatch or the closure gate."""
    try:
        return len(set(touched) | set(sweep(conn, cfg, now=now)))
    except Exception as exc:
        print(f"capsule tick: invariants sweep failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return len(set(touched))
