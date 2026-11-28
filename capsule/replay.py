"""Replay harness (R17): re-run the dispatcher against a past board's own history and compare
what it would pick with what the board recorded. The oracle for the dispatch switches (R18 lanes,
R09 deps_ready, R24 allocator report mode): with every switch off, the last N decisions must
replay exactly; with a switch on, the diff is the evidence.

Input is a COPY of a board DB (`capsule.db.copy_board`, or any file copy); the live boards under
`$CAPSULE_HOME/boards` (and `~/.local/share/capsule/boards`) are refused, by inode, so a symlink or
a case-variant path does not slip through. The copy is opened read-only and snapshotted into
memory, so the file is never written, and nothing here starts a run, a tick or a model call.

One decision is one `claimed` event. For each of the last N (or since `--since`), the state of the
board just before that event is rebuilt from `task_events` (a state machine over the lifecycle
kinds in `_STATE` / `_SPECIAL`; every other kind is either in `_NO_MOVE` or reported as unmapped)
on a throwaway in-memory copy, and the real `dispatch.ready` is asked what it would claim first.

The rebuild is itself checked (the second oracle): all events are applied, the result is compared
with `tasks.status`, and the verdict reports `fidelity k/N cards` and the kinds that mismatched. A
mismatched card whose last event is before the last scored decision stayed wrong through the
window, so it makes the verdict DIFFERENT, unless it cannot affect dispatch (its branch is not a
configured epic and neither its rebuilt nor its real status is running or review). A forced
decision (its card was not `ready` in the rebuild, so it was made ready by hand) also counts
against the match. A done card whose log ends in a non-completion with `completed_at` after it was
finished by hand: `completed_at` is its completion point.

Limits, stated on the verdict line too:
- positive decisions only: a tick that claimed nothing is not an event, so it is not replayed;
- today's code, config and `task_links` apply to yesterday's board; priority, assignee, tier,
  file_set and branch are read as they are NOW (not event-sourced);
- `guard_state` (pauses) is treated as clear;
- `home=None`: the seats are this board alone (other boards' running cards are not modelled);
- `new_task` does not record a card's first status: a card with no lifecycle event after `created`
  is taken as it is now, an intake card that later moved starts `triage` unless its first event is
  a claim, any other child starts `todo` (so the fidelity check is blind to a card nothing moved).
"""
import argparse
import bisect
import json
import os
import sqlite3
import sys
from collections import namedtuple
from datetime import datetime
from pathlib import Path

from capsule import config, db, dispatch, paths

# Kinds that leave a card in one fixed status, each paired with the `transition` call beside it.
_STATE = {
    "promoted": "ready", "reclaimed": "ready",            # lease.py:109 (legacy payload: review)
    "failed": "ready",                                    # blocks.py:313, every back_to is ready
    "implementer_note": "ready",                          # blocks.py:301, git-lock back_to
    "gave_up": "blocked",                                 # legacy kind, no writer left
    "claimed": "running", "checkpoint_restart": "running",    # lease.py:43; checkpoint keeps it
    "request_review": "review",                           # blocks.py:269, tick.py:742
    "completed": "done", "closed": "done",                # landing.py:120, blocks.py:180
    "korin_applied": "done",                              # tick.py:1731
    "archived": "archived", "scheduled": "scheduled",     # legacy kinds, same names (and `parked`)
    "blocked": "blocked", "parked_by_human": "blocked",   # blocks.py:50, :152
    "contradiction_loop": "blocked",                      # tick.py:981
    "block_loop_detected": "triage", "failure_limit": "triage",    # blocks.py:50, :313
    "runaway": "triage",                                  # runaway.py:97
    "dependency_wait": "todo", "claim_rejected": "todo",  # blocks.py:39, lease.py:32
    "engine_down": "ready", "start_slow": "ready"}        # tick_runs.py: worker start, engine down
# Kinds whose target depends on the payload or the card's neighbours, resolved in `_apply`.
_SPECIAL = ("created", "parked", "unblocked", "failure_reset", "first_screen_fix",
            "rate_limited", "contradiction")
# Kinds known to leave the status alone (`timed_out` and `crashed` are followed by their own
# `failed`/`blocked`; `close_refused` and the other refusals changed nothing). A kind in neither
# list is reported as unmapped in the result, never skipped silently.
_NO_MOVE = frozenset({
    "heartbeat", "commented", "spawned", "review_started", "intervention", "incident_opened",
    "notes", "review_requested", "style_warning", "claim_extended", "review_retry",
    "review_deferred", "design_brief_missing", "needs_missing", "mac_lane_held",
    "changes_requested",
    "invalid_document", "decision_ids_renamed", "epic_assigned", "gate_passed",
    "contract_amended", "ruling_started", "guide_ruling", "assigned", "korin_started",
    "korin_gave_up", "model_override_set", "protocol_violation", "intake_ingested",
    "epic_published", "decision_requested", "cost", "spec_stale", "linked", "unlinked",
    "eval_check", "eval_ledger_due", "epic_synced", "card_fields_set", "tip_scratch_workspace",
    "review_reopened", "contract_infra", "engine_restarted",
    "park_refused", "park_queued", "stop_refused", "stop_cancelled", "close_refused",
    "unblock_refused", "accept_refused", "reset_refused",
    "epic_sync_conflict", "epic_push_refused", "epic_refused", "landing_checked",
    "landing_flaky", "landing", "spawn_failed", "fix_card_attached", "checkpoint",
    "protected_path_refused",
    "suspected_hallucinated_references", "runaway_warn", "out_of_set_findings",
    "aligned_uncited", "follow_up_filed", "timed_out", "crashed", "reclaim_deferred", "triaged",
    "first_screen_hold", "first_screen_ok", "screen_refused", "needs_input", "outcome",
    "tick_error", "stale_run", "worktree_mismatch", "checks_overridden", "artefact", "untrack",
    "validator_error", "epic_sync_skipped", "amendment_no_op_dropped", "incident_retest",
    "incident_reattached", "contract_started", "contract_failed",
    "blind_tester_started", "blind_tester_skipped", "blind_tester_failed",   # R30: a tester run
    "blind_tests_reported",                                                # never moves a card
    "blind_red_checked", "blind_red_retry", "blind_landed", "blind_tests_stale",   # R31
    "blind_gated", "blind_hold_released",
    "main_wait_released", "main_wait_unknown"})   # R32; main_waits.py: status stays todo
_KINDS = tuple(_STATE) + _SPECIAL
_STATUSES = {"todo", "ready", "running", "review", "blocked", "triage", "scheduled", "parked",
             "done", "archived"}
_SETTLED = ("done", "archived")
_RESTART_KIND = "restart_failed"        # restart.py: a `blocked` event whose card went to triage

Decision = namedtuple("Decision", "event_id at recorded replayed candidates forced")
Mismatch = namedtuple("Mismatch", "card rebuilt actual kind counts")
Rebuilt = namedtuple("Rebuilt", "states expiries status last unmapped blind")
Result = namedtuple("Result", "decisions recorded_order replayed_order matched forced fidelity"
                              " mismatched unmapped blind")


def _refuse_live(path, home=None):
    """ValueError when `path` is, or sits under, a live `boards/` directory (the configured home
    and the default one), or is a hard link to a live board DB. Compared by `(st_dev, st_ino)`
    (`os.path.samefile`), so a symlink, a `..` or a case variant on a case-insensitive disk
    (APFS) is still the same directory."""
    homes = (paths.home(), Path.home() / ".local" / "share" / "capsule", *([home] if home else []))
    live = [h / "boards" for h in homes if (h / "boards").is_dir()]
    raw = Path(os.path.abspath(path))
    above = [a for c in {raw, raw.resolve()} for a in c.parents if a.exists()]
    dbs = [d for b in live for d in b.glob("*/kanban.db")] if os.path.exists(path) else []
    if (any(os.path.samefile(a, b) for a in above for b in live)
            or any(os.path.samefile(path, d) for d in dbs)):
        raise ValueError(f"{path} is a live board; copy it first (capsule.db.copy_board)")


def snapshot(path):
    """An in-memory, migrated copy of the board file at `path`, only ever opened read-only."""
    _refuse_live(path)
    src = db.connect_ro_tolerant(path)
    mem = sqlite3.connect(":memory:", isolation_level=None)
    mem.row_factory = sqlite3.Row
    try:
        src.backup(mem)
    finally:
        src.close()
    db.migrate(mem)
    return mem


def _apply(ev, cur, landing, born, unmoved):
    """The status `ev` leaves its card in, or None for no change. `cur` is its status just
    before, `landing` where a card released from a hold goes (`todo` behind an open parent, else
    `ready`), `born` the status a card without a payload status is created in (`_born`);
    `unmoved`: no other lifecycle event touched the card, so `born` (its status now) beats the
    payload's."""
    kind, pay = ev["kind"], json.loads(ev["payload"] or "{}")
    if kind == "created":                  # a legacy event carries its status; new_task's does not
        return born if unmoved or pay.get("status") not in _STATUSES else pay["status"]
    if kind == "parked":                   # legacy: the old dispatcher's; a human one blocks
        return "blocked" if pay.get("by") == "human" else "parked"
    if kind == "unblocked":                # blocks.py:222 carries a non-ready landing
        return pay["status"] if pay.get("status") in _STATUSES else "ready"
    if kind in ("failure_reset", "first_screen_fix"):    # blocks.py:366, :445
        return landing
    if kind == "rate_limited":             # tick.py:1088: a review run stays in review
        return None if cur == "review" else "ready"
    if kind == "contradiction":            # contradiction.py:49: only when routed to the guide
        return "triage" if pay.get("routed") == "guide" else None
    if kind == "blocked" and pay.get("kind") == _RESTART_KIND:    # restart.py:122
        return "triage"
    if kind in ("failed", "reclaimed"):    # a legacy payload names where the card went
        got = pay.get("back_to") or pay.get("retry_status")
        return got if got in _STATUSES else _STATE[kind]
    return _STATE[kind]


def _events(conn):
    """The lifecycle rows (and claim extensions) in id order. A done card whose log ends in a kind
    that is not a completion, with `completed_at` after its last event, was finished by hand: a
    virtual `completed` (id `.5`) goes just after the last row at or before `completed_at`."""
    kinds = _KINDS + ("claim_extended",)       # extensions move the claim expiry, not the status
    rows = [dict(r) for r in conn.execute(
        "SELECT id, task_id, kind, payload, created_at AS at FROM task_events WHERE kind IN"
        f" ({','.join('?' * len(kinds))}) ORDER BY id", kinds)]
    end = {r["task_id"]: r for r in rows if r["kind"] in _KINDS}
    stamps = [r["at"] for r in rows]
    for tid, at in conn.execute("SELECT id, completed_at FROM tasks WHERE status='done'"
                                " AND completed_at IS NOT NULL"):
        last = end.get(tid) or {"kind": "created", "at": 0}
        by_hand = (last["kind"] != "created" and _STATE.get(last["kind"]) != "done"
                   and last["at"] <= at)
        if by_hand:
            i = bisect.bisect_right(stamps, at)
            rows.insert(i, {"id": (int(rows[i - 1]["id"]) if i else 0) + 0.5, "task_id": tid,
                            "kind": "completed", "payload": None, "at": at})
            stamps.insert(i, at)
    return rows


def _born(rows, parents, now):
    """({card: status it is created in}, cards some other lifecycle event moved). `new_task` does
    not record the status: a card nothing moved is as it is now, an intake card that moved was
    `triage` unless its first event is a claim, any other child was `todo`."""
    first, intake = {}, set()
    for ev in rows:
        if ev["kind"] == "created":
            if json.loads(ev["payload"] or "{}").get("by") == "intake":
                intake.add(ev["task_id"])
        elif ev["kind"] in _KINDS:
            first.setdefault(ev["task_id"], ev["kind"])
    born = {}
    for tid, status in now.items():
        if tid not in first:
            born[tid] = status
        elif tid in intake:
            born[tid] = "ready" if first[tid] == "claimed" else "triage"
        else:
            born[tid] = "todo" if tid in parents else "ready"
    return born, set(first)


def _next(ev, status, parents, born, moved):
    """`_apply` for `ev` with the card's neighbours looked up in the rebuilt `status`."""
    tid = ev["task_id"]
    held = any(status.get(p, "archived") not in _SETTLED for p in parents.get(tid, ()))
    return _apply(ev, status.get(tid), "todo" if held else "ready", born.get(tid, "ready"),
                  tid not in moved)


def _rebuild(conn, wanted):
    """One pass over the lifecycle events: `Rebuilt` with, for each claimed event id in `wanted`,
    {card: status} and {card: claim expiry} just before it, the final {card: status}, {card:
    (last lifecycle event id, kind)}, the log's kinds nothing maps, and how many cards no event
    moved (blind: their rebuilt status is only what the board says now)."""
    parents = {}
    for parent, child in conn.execute("SELECT parent_id, child_id FROM task_links"):
        parents.setdefault(child, []).append(parent)
    now = {r["id"]: r["status"] for r in conn.execute("SELECT id, status FROM tasks")}
    rows = _events(conn)
    born, moved = _born(rows, parents, now)
    status, expires, last, states, expiries = {}, {}, {}, {}, {}
    for ev in rows:
        tid = ev["task_id"]
        if ev["id"] in wanted:
            states[ev["id"]], expiries[ev["id"]] = dict(status), dict(expires)
        if ev["kind"] in ("claimed", "claim_extended"):
            pay = json.loads(ev["payload"] or "{}")
            expires[tid] = pay.get("expires") or pay.get("claim_expires_now")
        if ev["kind"] in _KINDS:
            new = _next(ev, status, parents, born, moved)
            if new is not None:
                status[tid] = new
            last[tid] = (ev["id"], ev["kind"])
    known = set(_KINDS) | _NO_MOVE
    unmapped = sorted(r[0] for r in conn.execute("SELECT DISTINCT kind FROM task_events")
                      if r[0] not in known)
    return Rebuilt(states, expiries, status, last, unmapped, len(set(now) - moved))


def _board_at(conn, event_id, run_id, at, states, expiry, card):
    """Turn the in-memory copy into the board as it stood before `event_id` (see module doc).
    Runs are kept by time: a run that started after `at` (or is the decision's own) is gone, one
    that ended after `at` is still open, and no run has a pid, so `seats._run_alive` never calls
    `os.kill` on a historical pid."""
    conn.execute("DELETE FROM task_events WHERE id >= ?", (event_id,))
    conn.execute("DELETE FROM task_runs WHERE started_at > ? OR id IS ?", (at, run_id))
    conn.execute("UPDATE task_runs SET ended_at=NULL WHERE ended_at > ?", (at,))
    conn.execute("UPDATE task_runs SET worker_pid=NULL")
    conn.execute("DELETE FROM guard_state")
    forced = states.get(card) != "ready"
    states = dict(states, **{card: "ready"})
    for (tid,) in conn.execute("SELECT id FROM tasks").fetchall():
        st = states.get(tid, "archived")
        conn.execute("UPDATE tasks SET status=?, claim_lock=NULL, claim_expires=?, worker_pid=NULL,"
                     " current_run_id=NULL WHERE id=?",
                     (st, expiry.get(tid) if st == "running" else None, tid))
    return forced


def _fidelity(conn, rebuilt, end, epics):
    """((cards matching tasks.status, cards), [Mismatch]). A mismatch `counts` against the verdict
    when its card's last event is before `end` (the last scored decision), so the card was wrong
    through the window, unless it cannot affect dispatch: its branch is not a configured epic and
    neither its rebuilt nor its real status is running or review."""
    rows = conn.execute("SELECT id, status, branch_name FROM tasks").fetchall()
    bad = []
    for r in rows:
        got = rebuilt.status.get(r["id"])
        if (got or "archived") != r["status"]:
            at, kind = rebuilt.last.get(r["id"], (0, None))
            inert = r["branch_name"] not in epics and not {got, r["status"]} & {"running", "review"}
            bad.append(Mismatch(r["id"], got, r["status"], kind, at < end and not inert))
    return (len(rows) - len(bad), len(rows)), bad


def _decide(base, c, cfg, host_tiers, rebuilt):
    """The `Decision` for claimed event row `c`, on a throwaway copy of `base`."""
    work = sqlite3.connect(":memory:", isolation_level=None)
    work.row_factory = sqlite3.Row
    try:
        base.backup(work)
        forced = _board_at(work, c["id"], c["run_id"], c["created_at"], rebuilt.states[c["id"]],
                           rebuilt.expiries[c["id"]], c["task_id"])
        got = [r["id"] for r in dispatch.ready(work, cfg, host_tiers, now=c["created_at"])]
    finally:
        work.close()
    return Decision(c["id"], c["created_at"], c["task_id"], got[0] if got else None, got, forced)


def replay(path, cfg, n=20, host_tiers=frozenset({"strong"}), since=None):
    """Replay the last `n` dispatch decisions (at or after epoch `since`, when given) of the board
    copy at `path` under `cfg`. Switch state comes from `cfg["redesign"]` as given, so the caller
    decides off or on. Positive decisions only, today's code and config, seats of this board
    alone (module doc)."""
    base = snapshot(path)
    try:
        claims = base.execute(
            "SELECT id, task_id, created_at, run_id FROM task_events WHERE kind='claimed'"
            " AND created_at >= ? ORDER BY id DESC LIMIT ?", (since or 0, n)).fetchall()[::-1]
        if not claims:
            raise ValueError("no dispatch decisions to replay"
                             + (" since that time" if since else ""))
        rebuilt = _rebuild(base, {c["id"] for c in claims})
        fidelity, bad = _fidelity(base, rebuilt, claims[-1]["id"], config.epic_branches(cfg))
        decisions = [_decide(base, c, cfg, host_tiers, rebuilt) for c in claims]
    finally:
        base.close()
    rec = [d.recorded for d in decisions]
    rep = [d.replayed for d in decisions]
    forced = sum(d.forced for d in decisions)
    matched = rec == rep and not forced and not any(m.counts for m in bad)
    return Result(decisions, rec, rep, matched, forced, fidelity, bad, rebuilt.unmapped,
                  rebuilt.blind)


def report(result):
    """Plain lines: one per decision, then the verdict and the limits."""
    tab = "\t"
    lines = [f"{d.event_id}{tab}{'ok' if d.recorded == d.replayed else 'DIFF'}{tab}"
             f"recorded={d.recorded}{tab}replayed={d.replayed}"
             f"{tab + 'forced-ready' if d.forced else ''}" for d in result.decisions]
    ok = sum(d.recorded == d.replayed and not d.forced for d in result.decisions)
    k, total = result.fidelity
    wrong = [m for m in result.mismatched if m.counts]
    kinds = ", ".join(sorted({str(m.kind) for m in wrong}))
    verdict = (f"replay: {ok}/{len(result.decisions)} decisions match"
               f" ({'identical' if result.matched else 'DIFFERENT'} order); forced {result.forced};"
               f" fidelity {k}/{total} cards ({result.blind} blind: no event moved them)")
    if wrong:
        verdict += f"; {len(wrong)} wrong through the window, last kinds: {kinds}"
    if result.unmapped:
        verdict += f"; unmapped kinds: {', '.join(result.unmapped)}"
    lines += [verdict, "limits: positive decisions only (ticks that claimed nothing are not"
              " replayed); today's code, config and links; pauses clear; home=None (this board's"
              " seats alone, other boards' cards not modelled)"]
    return "\n".join(lines)


def _since(text):
    """Epoch seconds of an ISO time (`2026-10-22`, `2026-10-22T08:00`); no zone means local."""
    try:
        return int(datetime.fromisoformat(text).timestamp())
    except ValueError:
        raise ValueError(f"--since {text!r} is not an ISO time (2026-10-22 or 2026-10-22T08:00)")


def main(argv=None):
    """`python3 -m capsule.replay --board <copy.db> --config <config.json> [-n 20]
    [--since <ISO time>] [--switches config]`. Exit 0 identical order, 1 different (order, a
    forced decision, or a rebuild that does not match the board), 2 unusable input."""
    ap = argparse.ArgumentParser(prog="capsule.replay", description=__doc__.split("\n")[0])
    ap.add_argument("--board", required=True, help="a COPY of a board DB (live boards are refused)")
    ap.add_argument("--config", required=True, help="the board's config.json")
    ap.add_argument("-n", type=int, default=20, help="how many of the latest decisions")
    ap.add_argument("--since", help="only score decisions made at or after this ISO time")
    ap.add_argument("--switches", choices=("off", "config"), default="off",
                    help="off (default): every switch off; config: as the config file has them")
    args = ap.parse_args(argv)
    try:
        cfg = config.load(args.config)
        if args.switches == "off":
            cfg["redesign"] = {}
        result = replay(args.board, cfg, args.n, since=_since(args.since) if args.since else None)
    except (ValueError, OSError, sqlite3.Error) as exc:
        print(f"capsule.replay: {exc}", file=sys.stderr)
        return 2
    print(report(result))
    return 0 if result.matched else 1


if __name__ == "__main__":
    sys.exit(main())
