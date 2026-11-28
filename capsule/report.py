"""The interventions-per-card report (spec §9's input, row 391, 420)."""
import datetime
import json
import os
import sqlite3
import statistics
import time
from pathlib import Path

from capsule import (db, host, human, incident_inputs, incidents, legacy_names, lookout,
                     outcomes, override, paths, spool)

# The tick's own vocabulary: `failed`/`stale` outcomes; it also
# writes `timed_out` (a run killed at its wall-clock cap), counted through WASTED_OUTCOMES.
WASTED_STATUSES = ("failed", "stale")
# Kept for legacy rows migrated from retired legacy board, which still carry the legacy names.
WASTED_OUTCOMES = ("crashed", "timed_out", "invalid_output")


# The events that mark a run's failure or a hold; their payload names the run.
FAILURE_EVENTS = ("failed", "parked", "failure_limit", "blocked", "block_loop_detected",
                  "timed_out", "reclaimed")


def _is_wasted(outcome):
    return outcome in WASTED_STATUSES or outcome in WASTED_OUTCOMES

# Verbatim from `Knowledge/Capsule Corp - U1 report 2026-09-15`.
U1 = {"interventions_per_card": 2.4, "wasted_run_rate": 0.111, "incidents": 24, "cards": 20}

# Row 440: interventions the handover forced (tag `migration-reset`) are shown per
# card and in the totals but never counted in interventions_per_card, so vs_u1 compares
# like with like.
EXCLUDED_TAGS = ("migration-reset", "handover-route")


def owner_get(doc, key, default=None):
    """`owner_<key>` from a report, else the key stored before the rename (`<old name>_<key>`)."""
    return doc.get("owner_" + key, doc.get(human.LEGACY_OWNER + "_" + key, default))


def ledger_name():
    """The vault note the owner's weekly acts ledger lives in (the oracle B of owner_acts)."""
    return f"Capsule Corp - {host.owner_name()} acts ledger.md"

# Row 369: a UI epic's first-screen hold (`first_screen_hold` event) is one
# intervention of kind `first_screen` on its card; the owner's `screen-ok`/`screen-fix`
# reply answers it and is not counted a second time.
ANSWER_KINDS = ("screen-ok", "screen-fix")


def _since_ts(since):
    if since is None:
        return 0
    if isinstance(since, (int, float)) or str(since).isdigit():   # --help: epoch seconds too
        return int(since)
    return int(datetime.datetime.fromisoformat(str(since)).timestamp())


def _default_since(capsule_home, board):
    """Without `--since`, isolate Capsule's own work on a board that came from a
    handover: the timestamp of the legacy take record in `handovers.jsonl`
    (the retired `capsule handover take` wrote it; the file is still read)."""
    path = Path(capsule_home) / "handovers.jsonl"
    if not path.is_file():
        return None
    ts = None
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("board") == board and rec.get("from") == legacy_names.ERA_VALUE:
            ts = rec.get("at")
    return ts


def _card_failures(conn, task_id, since_ts):
    """A card's `FAILURE_EVENTS` split into (`failures`, `spec_stale_holds`) - pulled
    out of `interventions` to keep it inside its size budget (Zenkai Z7, row 509)."""
    failures, spec_stale_holds = [], []
    for e in conn.execute(
            f"SELECT kind, payload, created_at FROM task_events WHERE task_id=? AND created_at >= ?"
            f" AND kind IN ({','.join('?' * len(FAILURE_EVENTS))}) ORDER BY id",
            (task_id, since_ts, *FAILURE_EVENTS)):
        payload = _payload(e["payload"])
        if payload is None:      # a malformed payload (not a JSON object) is skipped
            continue
        entry = {"kind": e["kind"], "run_id": payload.get("run_id"), "by": payload.get("by"),
                 "at": e["created_at"]}
        if payload.get("kind") == "spec_stale":
            spec_stale_holds.append(entry)
        else:
            failures.append(entry)
    charged = {f["run_id"] for f in failures
               if f["kind"] in ("failed", "parked", "failure_limit") and f["run_id"]}
    failures = [f for f in failures if not (f["kind"] == "timed_out" and f["run_id"] in charged)]
    # a pre-rename human `parked` event is an operator decision, not a failure
    failures = [f for f in failures if not (f["kind"] == "parked" and f["by"] == "human")]
    return failures, spec_stale_holds


YT_DAYS = 7
NO_DATA = "not enough data"
YT_REQUESTS = ("blocked", "decision_requested", "first_screen_hold")   # what an approval answers


BATCH_FILE = "brief-batch.json"      # maintenance/budgets.py BATCH_NAME


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _payload(text):
    """The event payload as a dict; None for anything else (bad JSON, a list, a string)."""
    try:
        doc = json.loads(text or "{}")
    except (TypeError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _span(seconds, days=False):
    m = int(seconds // 60)
    return f"{m // 1440}d {m % 1440 // 60}h" if days and m >= 1440 else f"{m // 60}h {m % 60:02d}m"


def _overflow(start, now):
    """Items the daily message budget deferred to the morning brief: the `waiting` rows of the
    brief batch under the maintenance state root (maintenance/budgets.py `defer`, one `ts` per
    item, pruned after 7 days). The batch keeps a row until it is pruned, so every row counts
    as unsent. A missing or unreadable file is no data."""
    root = os.environ.get("AGENT_OPS_STATE_DIR") or (   # maintenance/events.py state_root()
        Path.home() / ".local" / "state" / "capsule-corp" / "maint")
    try:
        rows = json.loads((Path(root) / BATCH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return NO_DATA
    if not isinstance(rows, list):
        return NO_DATA
    ts = [r["ts"] for r in rows if isinstance(r, dict) and r.get("kind") == "waiting"
          and _num(r.get("ts")) and start <= r["ts"] <= now]
    if not ts:
        return "0 messages, none pending"
    return f"{len(ts)} message{'s' * (len(ts) > 1)}, oldest {_span(now - min(ts), True)}"


def _slowest(conn, start, now):
    """M201: (task_id, seconds, is_open) of the longest wait on the owner, or None. A request's
    `created_at` (YT_REQUESTS) is its waited-since time. Open: the card is not done/archived, no
    approve/accept and no Lookout hold-close event (`lookout.BLOCK_CLOSE`, `request_review`)
    came after it; its age runs to `now`. Answered: only his approvals (not
    `cos`, not an excluded tag) inside the window count."""
    answers = {}   # task_id -> [(at, is his)]
    for tid, created, payload in conn.execute(
            "SELECT task_id, created_at, payload FROM task_events WHERE kind='intervention'"):
        doc = _payload(payload) or {}
        at = doc.get("at", created)
        if doc.get("kind") in ("approve", "accept") and _num(at):
            mine = not human.is_cos(doc.get("author")) and doc.get("tag") not in EXCLUDED_TAGS
            answers.setdefault(tid, []).append((at, mine))
    closes = (*lookout.BLOCK_CLOSE, lookout.ACCEPT_CLOSE_KIND)   # the Lookout's hold-closed events
    closed = dict(conn.execute(
        f"SELECT task_id, MAX(created_at) FROM task_events"
        f" WHERE kind IN ({','.join('?' * len(closes))}) GROUP BY task_id", closes))
    best = None
    marks = ",".join("?" * len(YT_REQUESTS))
    for tid, asked, status in conn.execute(
            f"SELECT e.task_id, MAX(e.created_at), t.status FROM task_events e"
            f" JOIN tasks t ON t.id = e.task_id WHERE e.kind IN ({marks}) AND e.created_at<=?"
            f" GROUP BY e.task_id", (*YT_REQUESTS, now)):
        later = sorted(a for a in answers.get(tid, []) if a[0] > asked)
        if later:
            if not later[0][1] or not start <= later[0][0] <= now:
                continue
            cand = (tid, later[0][0] - asked, False)
        elif status in ("done", "archived") or closed.get(tid, -1) > asked:
            continue   # finished, or the hold was closed some other way (unblock, reset, accept)
        else:
            cand = (tid, now - asked, True)
        if best is None or cand[1] > best[1]:
            best = cand
    return best


def _classes(conn, start, now):
    """`class n.n` per decision class (`scope`, else `unscoped`), per day, alphabetical."""
    seen = {}
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE kind="
                                   "'decision_requested' AND created_at BETWEEN ? AND ?",
                                   (start, now)):
        c = str((_payload(payload) or {}).get("scope") or "unscoped")
        seen[c] = seen.get(c, 0) + 1
    return ", ".join(f"{c} {n / YT_DAYS:.1f}" for c, n in sorted(seen.items()))


def _yt(home, board, now):
    """M007/M201: the `## YOUR TIME` lines, the 7 days to `now`, from the board alone. Decision
    classes are `scope` of the card's `decision_requested` events; overflow is read from the
    maintenance state (`_overflow`), the one line that is not the board's."""
    from capsule import human_minutes
    conn = db.connect_ro(paths.board_db(board, Path(home)))
    start = now - YT_DAYS * 86400
    appr, waits = [], []
    for r in conn.execute("SELECT task_id, created_at, payload FROM task_events"
                          " WHERE kind='intervention' ORDER BY id").fetchall():
        doc = _payload(r["payload"]) or {}
        at = doc.get("at", r["created_at"])
        if (doc.get("kind") not in ("approve", "accept") or human.is_cos(doc.get("author"))
                or doc.get("tag") in EXCLUDED_TAGS or not _num(at) or not start <= at <= now):
            continue
        appr.append(at)
        asked = conn.execute(
            f"SELECT MAX(created_at) FROM task_events WHERE task_id=? AND created_at<?"
            f" AND kind IN ({','.join('?' * len(YT_REQUESTS))})",
            (r["task_id"], at, *YT_REQUESTS)).fetchone()[0]
        if asked is not None:
            waits.append(at - asked)
    by_class = _classes(conn, start, now)
    slow = _slowest(conn, start, now)
    conn.close()
    wait = _span(statistics.median(waits)) if waits else NO_DATA
    docs = [d for v in spool.human_by_task(home, board, start).values() for d in v
            if int(d.get("at") or 0) <= now]
    hours = human_minutes.measure(home, board, docs, start, now)["human_minutes"] / 60
    return [
        f"- Decisions per day by class: {by_class or NO_DATA}",
        "- Approvals per day: " + (f"{len(appr) / YT_DAYS:.1f}, median wait {wait}"
                                  if appr else NO_DATA),
        "- Terminal hours: " + (f"{hours:.1f}" if docs else NO_DATA),
        f"- Overflow: {_overflow(start, now)}",
        "- Slowest: " + (NO_DATA if slow is None else
                         f"{slow[0]}, {'open' if slow[2] else 'answered after'}"
                         f" {_span(slow[1], True)}"),
    ]


def interventions(capsule_home, board, *, since=None, now=None):
    now = int(now if now is not None else time.time())
    if since is not None:
        since_ts = _since_ts(since)
    else:
        auto = _default_since(capsule_home, board)
        since_ts = int(auto) if auto is not None else 0
    conn = db.connect_ro(paths.board_db(board, Path(capsule_home)))
    try:
        # Cards themselves are never filtered by `created_at`: a
        # migrated board's cards mostly predate the handover.
        tasks = conn.execute(
            "SELECT id, title, status, created_at, completed_at FROM tasks ORDER BY id").fetchall()
        human = spool.human_by_task(capsule_home, board, since_ts)
        # row 454 item 4: active/expired/refused overrides per card and in totals.
        override_doc = override.report(conn, board, now=now)

        cards, total_runs, wasted_runs, merged, merge_times = [], 0, 0, 0, []
        wasted_by_error_class = {}
        for t in tasks:
            runs = conn.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND started_at >= ? ORDER BY id",
                (t["id"], since_ts)).fetchall()
            card_all = human.get(t["id"], [])
            card_human = [h for h in card_all if h.get("tag") not in EXCLUDED_TAGS]
            card_migration = len(card_all) - len(card_human)
            answered = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind IN ('first_screen_ok','first_screen_fix')"
                " AND created_at >= ?", (t["id"], since_ts)).fetchone()[0]
            replies = [h for h in card_human if h.get("kind") in ANSWER_KINDS]
            card_human = [h for h in card_human if h.get("kind") not in ANSWER_KINDS] + replies[answered:]
            card_human += [{"kind": "first_screen"} for _ in conn.execute(
                "SELECT 1 FROM task_events WHERE task_id=? AND kind='first_screen_hold' AND created_at >= ?",
                (t["id"], since_ts))]
            decisions = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='decision_requested'"
                " AND created_at >= ?", (t["id"], since_ts)).fetchone()[0]
            failures, spec_stale_holds = _card_failures(conn, t["id"], since_ts)
            # row 452's `amend` verb: a second oracle for contract_amended
            # events, same shape as first_screen above - visible per card and in totals,
            # but never folded into interventions_per_card/vs_u1 (an operator-approved
            # contract edit is not the kind of rework U1 measured).
            amend_rows = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='contract_amended'"
                " AND created_at >= ? ORDER BY id", (t["id"], since_ts)).fetchall()
            contract_amendments = len(amend_rows)
            last_amend_reason = None
            if amend_rows:
                last_amend_reason = (_payload(amend_rows[-1]["payload"]) or {}).get("reason")
            card_overrides = [o for o in override_doc["by_task"].get(t["id"], []) if o["at"] >= since_ts]
            if (not runs and not card_all and not card_human and not decisions
                    and not failures and not spec_stale_holds and not contract_amendments
                    and not card_overrides):
                continue
            impl_runs = [r for r in runs if r["profile"] == "implementer"]
            wasted = sum(1 for r in runs if _is_wasted(r["outcome"]))
            for r in runs:
                if _is_wasted(r["outcome"]):
                    wasted_by_error_class[r["error_class"] or "unknown"] = (
                        wasted_by_error_class.get(r["error_class"] or "unknown", 0) + 1)
            total_runs += len(runs)
            wasted_runs += wasted
            if t["status"] == "done":
                merged += 1
                if t["completed_at"] is not None and t["created_at"] is not None:
                    merge_times.append(t["completed_at"] - t["created_at"])
            cards.append({
                "task_id": t["id"], "title": t["title"], "status": t["status"],
                "runs": len(runs),
                "outcomes": sorted(r["outcome"] for r in runs if r["outcome"]),
                "wasted_runs": wasted,
                "human": len(card_human),
                # EVAL-07: merged with zero counted human events; null when not merged.
                # A card created before the since cutoff is not judged: its early human
                # events fall outside the window.
                "unattended": ((not card_human) if t["status"] == "done"
                               and (t["created_at"] or 0) >= since_ts else None),
                "migration_resets": card_migration,
                "human_kinds": sorted({h.get("kind") for h in card_human if h.get("kind")}),
                "decisions_answered": decisions,
                "rework_rounds": max(0, len(impl_runs) - 1),
                "failure_events": failures,
                "spec_stale_holds": spec_stale_holds,
                "contract_amendments": contract_amendments,
                "last_amend_reason": last_amend_reason,
                "overrides": card_overrides,
            })
        incident_counts = _incidents(conn, _korin_since(capsule_home, board))
        escaped_14d = outcomes.rate(conn, 14)    # Evals E4: "pending" until a +14 d outcome
    finally:
        conn.close()

    total_cards = len(cards)
    total_human = sum(c["human"] for c in cards)
    migration_resets = sum(c["migration_resets"] for c in cards)
    contract_amendments = sum(c["contract_amendments"] for c in cards)
    override_totals = {"granted": 0, "active": 0, "expired": 0, "refused": 0,
                       "applied": override_doc["totals"]["applied"]}
    for c in cards:
        for o in c["overrides"]:
            override_totals["refused" if o["state"] == "refused" else "granted"] += 1
            if o["state"] in ("active", "expired"):
                override_totals[o["state"]] += 1
    interventions_per_card = (total_human / total_cards) if total_cards else 0.0
    wasted_run_rate = (wasted_runs / total_runs) if total_runs else 0.0
    unattended = sum(1 for c in cards if c["unattended"])
    judged = sum(1 for c in cards if c["unattended"] is not None)
    time_to_merge_median_s = statistics.median(merge_times) if merge_times else None
    totals = {
        "cards": total_cards, "runs": total_runs,
        "interventions_per_card": interventions_per_card,
        "migration_resets": migration_resets,
        "contract_amendments": contract_amendments,
        "wasted_run_rate": wasted_run_rate,
        "wasted_by_error_class": wasted_by_error_class,
        "merged": merged,
        "unattended": unattended,
        "unattended_judged": judged,
        "unattended_rate": (unattended / judged) if judged else 0.0,
        "time_to_merge_median_s": time_to_merge_median_s,
        "failure_events": sum(len(c["failure_events"]) for c in cards),
        "failure_events_without_run_id": sum(1 for c in cards for f in c["failure_events"] if not f["run_id"]),
        # row 472: spec_stale holds, counted apart from failure_events - a hold/needs_input, not
        # an implementer failure (see the loop above).
        "spec_stale_holds": sum(len(c["spec_stale_holds"]) for c in cards),
        "overrides": override_totals,
    }
    return {
        "board": board, "since": since_ts, "cards": cards, "totals": totals,
        # Also at the top level: the validation contract reads these directly.
        "interventions_per_card": interventions_per_card,
        "wasted_run_rate": wasted_run_rate,
        "merged": merged,
        "time_to_merge_median_s": time_to_merge_median_s,
        # Row 456: the incident loop's counts; `repeat_incidents` is the status board's first
        # number.
        "incidents": incident_counts,
        "repeat_incidents": incident_counts["repeat"], "your_time": _yt(capsule_home, board, now),
        "escaped_defect_rate_14d": escaped_14d,
        "u1": U1,
        "vs_u1": {
            "interventions_per_card": interventions_per_card - U1["interventions_per_card"],
            "wasted_run_rate": wasted_run_rate - U1["wasted_run_rate"],
        },
    }


def _korin_since(capsule_home, board):
    """`korin.since` from the board's config as epoch seconds; None when unset or unreadable."""
    try:
        cfg = json.loads(paths.config_path(Path(capsule_home), board).read_text())
        return incident_inputs.since_ts((cfg.get("korin") or {}).get("since"))
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def _incidents(conn, since=None):
    """incidents.summary plus the repeat list; a board migrated before row 456 has no table: zeros."""
    try:
        counts = incidents.summary(conn, since)
        counts["repeats"] = [{"id": r["id"], "repeat_of": r["repeat_of"], "status": r["status"]}
                             for r in conn.execute("SELECT id, repeat_of, status FROM incidents WHERE kind='repeat'"
                                                   " ORDER BY opened_at")]
    except sqlite3.OperationalError:
        counts = {"repeat": 0, "open": 0, "closed": 0, "total": 0, "by_status": {}, "by_kind": {}, "repeats": [],
                  "backlog": 0, "absent": True}
    return counts


def _incident_lines(report):
    inc = report.get("incidents") or {"repeat": 0, "open": 0, "closed": 0, "total": 0, "repeats": []}
    lines = ["## Incidents", "",
             f"**Repeat incidents: {inc['repeat']}** (the headline, row 456: the same root cause again within"
             " 30 days of a close)",
             f"open: {inc['open']} · closed: {inc['closed']} · closed as history:"
             f" {inc.get('closed_history', 0)} · total: {inc['total']}"]
    if inc.get("backlog"):
        lines.append(f"backlog (before korin.since, reported, never dispatched): {inc['backlog']}")
    if inc.get("by_kind"):
        lines.append("by kind: " + ", ".join(f"{k} {v}" for k, v in sorted(inc["by_kind"].items())))
    lines += [f"- repeat {r['id']} of {r['repeat_of']} ({r['status']})" for r in inc.get("repeats", [])]
    return lines + [""]


def _migration_note(card):
    n = card.get("migration_resets", 0)
    return f" (+{n} migration-reset)" if n else ""


def _amend_note(card):
    n = card.get("contract_amendments", 0)
    if not n:
        return "0"
    reason = card.get("last_amend_reason")
    return f"{n} (last: {reason})" if reason else str(n)


def _eval_lines(report):
    """E2 (row 617): the `EVAL INCIDENT` lines that open the document, then `## Eval checks`."""
    head = [f"EVAL INCIDENT {i['id']}: {i['root_key']} (open since "
            f"{datetime.date.fromtimestamp(i['opened_at'])}) - an eval oracle disagrees; "
            'RUNBOOK "Korin (incidents)"' for i in report.get("eval_incidents") or []]
    head += [f"LEDGER ROW DUE {datetime.date.fromtimestamp(s)} to {datetime.date.fromtimestamp(u)}:"
             f" write it in {ledger_name()}"
             for s, u in report.get("eval_ledger_due") or []]
    checks = report.get("eval_checks") or {}
    body = [f"{m} a={c['a']} b={c['b']} agree={'yes' if c['agree'] else 'no'}"
            f" window={c['window'][0]}..{c['window'][1]}" for m, c in sorted(checks.items())]
    return head, ["## Eval checks", "", *(body or ["no eval checks yet"]), ""]


HEADLINE_MIN_DAYS = 14   # EVAL-10 trigger: two weeks of series before the number is shown


def _headline(report):
    """EVAL-10: the owner's minutes and the agents' share, in one plain line (estimates)."""
    if "human_minutes_per_day" not in report:
        return "HEADLINE: not enough data yet (no human-time series on this report)"
    days = int(report["human_window_days"])
    if days < HEADLINE_MIN_DAYS:
        return (f"HEADLINE: not enough data yet ({days} days; needs {HEADLINE_MIN_DAYS} for a"
                " trustworthy number)")
    d = report["delegation_ratio"]
    share = ("agents' share not measured yet" if isinstance(d, str)
             else f"agents did {d:.0%} of the work")
    return (f"HEADLINE: you spent about {report['human_minutes_per_day'] * 7:.0f} min a week on"
            f" Capsule (estimate, last {days} days); {share}")


def render_md(report):
    head, evals = _eval_lines(report)
    lines = [
        *head, f"# Interventions report: {report['board']}", _headline(report), "",
        *_incident_lines(report), *evals,
        *(["## YOUR TIME", *report["your_time"], ""] if "your_time" in report else []),
        "| card | status | runs | wasted | human | decisions | rework | failure events | amended |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for c in report["cards"]:
        lines.append(f"| {c['task_id']} {c['title']} | {c['status']} | {c['runs']} | {c['wasted_runs']} |"
                     f" {c['human']}{_migration_note(c)} |"
                     f" {c['decisions_answered']} | {c['rework_rounds']} |"
                     f" {len(c.get('failure_events', []))} | {_amend_note(c)} |")
    t = report["totals"]
    ttm = "n/a" if t["time_to_merge_median_s"] is None else f"{t['time_to_merge_median_s']:.0f}s"
    lines += [
        "",
        f"totals: cards={t['cards']} runs={t['runs']} interventions_per_card={t['interventions_per_card']:.2f}"
        f" wasted_run_rate={t['wasted_run_rate']:.3f} merged={t['merged']} time_to_merge_median={ttm}"
        f" unattended={t['unattended']}/{t['unattended_judged']}"
        f" migration_resets={t['migration_resets']} (not in the vs-U1 rate)"
        f" failure_events={t.get('failure_events', 0)} ({t.get('failure_events_without_run_id', 0)} without run_id)",
        f" spec_stale_holds={t.get('spec_stale_holds', 0)} (holds/needs_input, not counted as failures - row 472)",
        f" contract_amendments={t['contract_amendments']} (also not in the vs-U1 rate)",
        f"vs U1 (2.4/card, 0.111 wasted): interventions_per_card {report['vs_u1']['interventions_per_card']:+.2f},"
        f" wasted_run_rate {report['vs_u1']['wasted_run_rate']:+.3f}",
        f"overrides: granted={t['overrides']['granted']} active={t['overrides']['active']}"
        f" expired={t['overrides']['expired']} refused={t['overrides']['refused']}"
        f" applied={t['overrides']['applied']}",
    ]
    if "human_minutes_per_day" in report:
        d = report["delegation_ratio"]
        lines.append(f"human time (estimates, EVAL-10): {report['human_minutes_per_day']:.1f}"
                     f" min/day, {report['decisions_per_day']:.1f} decisions/day, delegation"
                     f" (cos share) {d if isinstance(d, str) else format(d, '.2f')}")
    if owner_get(report, "touches_per_card") is not None:
        q = owner_get(report, "touches_per_card")
        q = q if isinstance(q, str) else f"{q:.2f}"
        bar = owner_get(report.get("bar", {}), "touches_per_card")
        why = ("no bar (unmeasured)" if q == "unmeasured" or bar is None
               else "n/a: no bar comparison" if q in ("insufficient", "pending")
               else f"vs bar {bar}: {float(q) - bar:+.2f}")
        lines.append(f"ramp: owner_touches_per_card {q} over {report['landed_cards']} landed"
                     f" ({why})")
    return "\n".join(lines)
