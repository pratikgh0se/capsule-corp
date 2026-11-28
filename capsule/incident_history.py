"""Close old incidents as history (owner row 638 b): the CoS lists incidents whose issue is no
longer open, with a verdict and one line of evidence each; this closes them without the Korin gate
(they pre-date Korin, so no fix card or decision row can exist). Dry run unless `apply`; one
transaction; no outbox notice per incident. The tool itself enforces the bound "only old incidents
whose issue is not open": nothing opened after CUTOFF, nothing that recurred since, nothing Korin
worked on, nothing whose card is still active. `python3 -m capsule incidents close-history`."""
import hashlib
import json
import re
import time
from pathlib import Path

from capsule import incidents, slack_msgs
from capsule.db import event, txn

ROW = 638
# Row 638 b: the analysis snapshot, 2026-10-30 10:00 Asia/Bangkok (03:00 UTC). An incident opened
# at or after it, or whose fingerprint recurred at or after it, was never analysed: it stays open.
CUTOFF = 1793329200
HISTORY = "history"                      # the `closure` value; the column is free text
BY = "cos-history"
VERDICTS = ("resolved", "false_alarm", "card_done")
BARRED_KINDS = ("repeat", "escaped_bug")
MAX_EVIDENCE = 300
CARD_FINISHED = ("done", "archived")     # any other card status is the issue still being worked


class HistoryRefused(Exception):
    """The whole run is refused (no decision row), as opposed to one id."""


def _parse_list(text):
    """One dict per non-blank line; a line that is not a JSON object becomes {"_bad": why}."""
    entries = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            doc = json.loads(line)
        except ValueError:
            doc = None
        entries.append(doc if isinstance(doc, dict) else {"_bad": f"line {n} is not a JSON object"})
    return entries


def _require_row(vault, decisions_note):
    note = Path(vault).expanduser() / decisions_note
    if not note.is_file():
        raise HistoryRefused(f"decisions note not found: {decisions_note}")
    text = note.read_text(encoding="utf-8", errors="replace")
    if not re.search(rf"^\|\s*{ROW}\s*\|", text, re.M):
        raise HistoryRefused(f"no '| {ROW} |' row in {decisions_note}: record the decision first")


def _record_problem(conn, inc):
    """Old, untouched by Korin, and not recurred since the analysis; else why not."""
    if inc["opened_at"] >= CUTOFF:
        return "opened after the row-638 analysis"
    late = conn.execute("SELECT id FROM incidents WHERE fingerprint=? AND id!=? AND opened_at>=?"
                        " LIMIT 1", (inc["fingerprint"], inc["id"], CUTOFF)).fetchone()
    if late is not None:
        return f"recurred since the analysis: {late['id']} has the same fingerprint"
    if inc.get("closure") or inc.get("fix_card"):
        return "Korin filled the record: use incidents close"
    return None


def _card_problem(conn, inc):
    """The issue is open while its card is: a card on this board must be done or archived."""
    tid = inc.get("task_id")
    if not tid and inc["kind"] == "human_intervention":
        return "no card: a human_intervention needs its finished card"
    card =conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone() if tid else None
    if card is not None and card["status"] not in CARD_FINISHED:
        return f"card still active: the issue is open (card {tid}, status {card['status']})"
    if card is None and inc["kind"] == "human_intervention":
        return f"card {tid} is not done or archived (status missing)"
    return None


def _verdict_problem(inc, verdict, evidence):
    if verdict not in VERDICTS:
        return f"verdict {verdict!r} is not one of {', '.join(VERDICTS)}"
    if not isinstance(evidence, str) or not evidence.strip():
        return "evidence is empty"
    if "\n" in evidence or "\r" in evidence:
        return "evidence is not one line"
    if len(evidence) > MAX_EVIDENCE:
        return f"evidence is {len(evidence)} chars, over {MAX_EVIDENCE}"
    if verdict == "card_done" and inc["kind"] != "human_intervention":
        return f"card_done is only for human_intervention, not {inc['kind']}"
    return None


def _refusal(conn, inc, entry):
    """Why this entry may not close (the live-repeat check aside), or None. Reads the board only."""
    if inc["status"] not in ("open", "fix_pending"):
        return f"status is {inc['status']}, not open or fix_pending"
    if inc["kind"] in BARRED_KINDS:
        return f"kind {inc['kind']} is never closed as history"
    return (_record_problem(conn, inc)
            or _verdict_problem(inc, entry.get("verdict"), entry.get("evidence"))
            or _card_problem(conn, inc))


def _live_repeat(conn, inc, now, closing):
    """The refusal text for a same-fingerprint twin that is open, opened within 30 days and not
    itself closing in this run (a listed twin that closes is no live repeat), else None."""
    twins = conn.execute(
        "SELECT id FROM incidents WHERE fingerprint=? AND id!=?"
        " AND status NOT IN ('closed','superseded') AND opened_at>=?",
        (inc["fingerprint"], inc["id"], now - incidents.REPEAT_WINDOW)).fetchall()
    live = next((t["id"] for t in twins if t["id"] not in closing), None)
    if live is None:
        return None
    return f"live repeat: {live} has the same fingerprint, open, opened within 30 days"


def _close_one(conn, inc, entry, now, sha):
    """True when closed; False when the incident changed since the plan (nothing is written)."""
    done = conn.execute("UPDATE incidents SET status='closed', closed_at=?, closure=? WHERE id=?"
                        " AND status IN ('open','fix_pending')", (now, HISTORY, inc["id"]))
    if done.rowcount != 1:
        return False
    reason = (f"history by {BY} at {now}: row {ROW}: {entry['verdict']}: {entry['evidence']}"
              f" (list {sha[:12]})")
    incidents.attach(conn, inc["id"], [reason])
    if inc.get("task_id") and conn.execute(
            "SELECT 1 FROM tasks WHERE id=?", (inc["task_id"],)).fetchone():
        event(conn, inc["task_id"], "incident_closed",
              {"incident": inc["id"], "by": BY, "reason": reason, "list_sha": sha}, now=now)
    return True


def _plan(conn, entries, now):
    """[(incident or None, entry, outcome, text)], outcome: close | skipped | refused."""
    plan, seen = [], set()
    for entry in entries:
        inc_id = entry.get("id")
        inc = incidents.get(conn, inc_id) if isinstance(inc_id, str) else None
        if "_bad" in entry:
            plan.append((None, entry, "refused", entry["_bad"]))
        elif not isinstance(inc_id, str):
            plan.append((None, entry, "refused", "id is not a string"))
        elif inc is None:
            plan.append((None, entry, "refused", f"{inc_id}: not found"))
        elif inc_id in seen:
            plan.append((inc, entry, "refused", f"{inc_id}: listed twice"))
        elif inc["status"] == "closed":
            plan.append((inc, entry, "skipped", f"{inc_id}: already closed"))
        else:
            why = _refusal(conn, inc, entry)
            outcome = "refused" if why else "close"
            plan.append((inc, entry, outcome, f"{inc_id}: {why or entry['verdict']}"))
        if isinstance(inc_id, str):
            seen.add(inc_id)
    _settle_twins(conn, plan, now)
    return plan


def _settle_twins(conn, plan, now):
    """Live-repeat pass over the candidates (every entry that passed the other checks): twins that
    are both candidates do not block each other, any other open twin does, so a listed twin
    refused for another reason still blocks its siblings. One pass is the fixpoint: a candidate
    refused here has a non-candidate live twin, which blocks every sibling in the same pass.
    That holds because every incident shares the one now-based REPEAT_WINDOW; a per-incident
    window would need a fixpoint loop."""
    closing = {inc["id"] for inc, _, outcome, _ in plan if outcome == "close"}
    for i, (inc, entry, outcome, _) in enumerate(plan):
        live = _live_repeat(conn, inc, now, closing) if outcome == "close" else None
        if live:
            plan[i] = (inc, entry, "refused", f"{inc['id']}: {live}")


def _pending_notices(conn, plan):
    """A queued 'broken' notice for a listed fingerprint is the fresh start's to settle first
    (only a row still `pending` counts: a delivered or dead one does not)."""
    held = []
    for fp in sorted({inc["fingerprint"] for inc, _, _, _ in plan if inc is not None}):
        queued = conn.execute(
            "SELECT 1 FROM outbox_key k JOIN outbox o ON o.id=k.outbox_id"
            " WHERE k.dedup_key=? AND o.status='pending' LIMIT 1",
            (slack_msgs.broken_key(fp),)).fetchone()
        if queued is not None:
            held.append(f"pending broken notice for {fp}: run the fresh start first")
    return held


def _execute(conn, entries, now, sha, apply):
    plan = _plan(conn, entries, now)
    held = _pending_notices(conn, plan)
    if held and apply:
        raise HistoryRefused(held[0])
    results = []
    for inc, entry, outcome, text in plan:
        if outcome == "close":
            if not apply:
                outcome = "would close"
            elif _close_one(conn, inc, entry, now, sha):
                outcome = "closed"
            else:       # apply runs in one txn: raising rolls every earlier close back
                raise HistoryRefused(f"{inc['id']}: changed during run")
        results.append((outcome, text))
    return results + [("warning", text) for text in held]     # a dry run says what apply refuses


def run(conn, list_path, *, vault, decisions_note, apply=False, list_sha=None, now=None):
    """([(outcome, text)] per line of `list_path`, sha256 of the list file); outcome is closed |
    would close | skipped | refused. Raises HistoryRefused without the row-638 decision (dry run
    included), and on --apply without the dry run's `list_sha` of this very file. Apply plans and
    closes inside one transaction."""
    _require_row(vault, decisions_note)
    data = Path(list_path).read_bytes()
    sha = hashlib.sha256(data).hexdigest()
    if apply and (list_sha or "").strip().lower() != sha:
        raise HistoryRefused(f"--apply needs --list-sha {sha[:12]}... from this list's dry run"
                             f" (got {list_sha or 'none'}): the list changed or was never dry-run")
    entries = _parse_list(data.decode("utf-8", errors="replace"))
    now = int(now if now is not None else time.time())
    if not apply:
        return _execute(conn, entries, now, sha, False), sha
    with txn(conn):
        return _execute(conn, entries, now, sha, True), sha
