"""Stop-the-line: a card that keeps failing review is stopped,
not retried."""
import json
import statistics

from capsule import (blocks, bundles, claims, cost, outbox, restart, review_scope,
                     security_review, slack_msgs, validator_report)
from capsule.blind import gate
from capsule.db import event, transition, txn

DEFAULTS = {"max_rounds": 4, "owner_rounds": 5, "runs_factor": 3, "runs_warn_factor": 2,
            "tokens_factor": 3, "same_class_rounds": 3, "min_landed": 5, "floor_runs": 8,
            "floor_tokens": 150000}
IDS = tuple(i for i, _ in bundles.RUNAWAY_OPTIONS)
# Row 642: the round that stops a card and goes straight to the owner, with no guide pass.
OWNER_CHOICES = ("restart it with the reviewer's findings as the fix list, drop it and I'll"
                 " file a smaller card, or narrow it to fewer files")
# A card the guide rules on this way: its triage `block_kind` is its trigger event's kind.
KINDS = {"runaway": "stopped (runaway)", "claims": "held (claims check)"}


def held(task):
    return task["block_kind"] in KINDS


def settings(cfg):
    ks = {**DEFAULTS, **((cfg or {}).get("runaway") or {})}
    if restart.on(cfg):   # M155: room for the whole ladder; a 0 (off) same-class stays off
        ks["max_rounds"] = max(ks["max_rounds"], restart.LADDER)
        ks["owner_rounds"] = max(ks["owner_rounds"], restart.LADDER)   # row 642: 5 only off-ladder
        if ks["same_class_rounds"]:
            ks["same_class_rounds"] = max(ks["same_class_rounds"], restart.LADDER)
    return ks


def _reset(conn, task_id):
    """(event id, time) of the card's latest human `failure_reset`; (0, 0) when none."""
    row = conn.execute("SELECT id, created_at FROM task_events WHERE task_id=? AND kind="
                       "'failure_reset' AND json_extract(payload,'$.by')='human' ORDER BY id DESC"
                       " LIMIT 1", (task_id,)).fetchone()
    return (row[0], row[1]) if row is not None else (0, 0)


def _body_amended(conn, task_id, skip_harness=False):
    """(event id, time) of the card's latest `contract_amended` that changed the body; (0, 0)
    when none. `skip_harness` ignores amendments the harness itself made."""
    row = conn.execute(
        "SELECT id, created_at FROM task_events WHERE task_id=? AND kind='contract_amended'"
        " AND json_extract(payload,'$.body_hash_before') IS NOT"
        " json_extract(payload,'$.body_hash_after')"
        " AND (? = 0 OR json_extract(payload,'$.issuer') IS NOT 'harness')"
        " ORDER BY id DESC LIMIT 1",
        (task_id, int(skip_harness))).fetchone()
    return (row[0], row[1]) if row is not None else (0, 0)


def _body_amended_at(conn, task_id):
    """Time of the card's latest non-harness `contract_amended` that changed the body; 0 when none.
    The harness's main-moved note (fix_card_drift) is advice and refills no budget."""
    return _body_amended(conn, task_id, skip_harness=True)[1]


def rerun_amendment(conn, task_id):
    """(event id, time) of the latest body amendment for the implementer rerun (inc_0ff71df2): the
    id floors the guide rulings the rerun reads (0: none). The time is 0 when the card's latest
    review run started after the amendment, since that round already judged the current body."""
    amended, at = _body_amended(conn, task_id, skip_harness=True)
    reviewed = conn.execute("SELECT MAX(started_at) FROM task_runs WHERE task_id=? AND profile IN"
                            " ('reviewer','validator','guide')", (task_id,)).fetchone()[0]
    return amended, (at if reviewed is None or at > reviewed else 0)


def _stuck_class(conn, task_id, rounds, since=0):
    """A finding class on one file shared by the verdicts of the last `rounds` review rounds since
    `since`, all fails; one verdict per round (its latest reviewer run, as `Tick._review_round`
    counts); None when off."""
    ids = review_scope.round_impl_ids(conn, task_id, since)
    if rounds <= 0 or len(ids) < rounds:
        return None
    docs = []
    window = ids[-rounds:]
    for impl, hi in zip(window, window[1:] + [2 ** 62]):
        lo = impl   # the round starts after the run under review or the last run an `accept` saw
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=?"
                          " AND kind='request_review' ORDER BY id DESC LIMIT 1",
                          (task_id, impl)).fetchone()
        if ev is not None and ev[0]:
            lo = max(lo, json.loads(ev[0]).get("after_run") or 0)
        row = conn.execute(
            "SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer' AND id>? AND id<?"
            " AND handoff IS NOT NULL AND COALESCE(outcome, '') != 'rate_limited'"
            " AND json_extract(COALESCE(metadata, '{}'), '$.ruling_of') IS NULL"
            " ORDER BY id DESC LIMIT 1", (task_id, lo, hi)).fetchone()
        if row is None:
            return None
        docs.append(json.loads(row[0]))
    if any(d.get("verdict") != "fail" for d in docs):
        return None
    common = set.intersection(*[{(f["class"], f.get("file")) for f in d.get("findings", [])}
                                for d in docs])   # a no-file finding matches only a no-file one
    return min((c for c, _ in common), default=None)


def _true_rounds(conn, task_id, since):
    """Review rounds since `since` less the ones that passed every gate (`gate_passed`)."""
    ids = review_scope.round_impl_ids(conn, task_id, since)
    marks = ",".join("?" * len(ids))
    passed = conn.execute(f"SELECT COUNT(DISTINCT run_id) FROM task_events WHERE task_id=? AND"
                          f" kind='gate_passed' AND run_id IN ({marks})",
                          (task_id, *ids)).fetchone()[0]
    return len(ids) - passed


def check(conn, cfg, task_id):
    """(trigger, detail) when the card has run away, else None; records the one-time warn event.
    The limits are multiples of the trailing median of landed cards, never below the floors."""
    ks = settings(cfg)
    since = _reset(conn, task_id)[1]
    floor = max(since, _body_amended_at(conn, task_id))   # a replaced body restarts the budget
    spent, landed = cost.card_cost(conn, task_id, floor), cost.landed_costs(conn, task_id)
    costs = landed if len(landed) >= ks["min_landed"] else []
    m_runs = max(ks["floor_runs"], statistics.median([c["runs"] for c in costs] or [0]))
    m_tokens = max(ks["floor_tokens"], statistics.median([c["tokens"] for c in costs] or [0]))
    rounds = _true_rounds(conn, task_id, since)
    if rounds >= ks["owner_rounds"]:
        return "owner_rounds", f"it has failed review {rounds} times"
    if rounds >= ks["max_rounds"]:
        return "rounds", f"{rounds} review rounds (limit {ks['max_rounds']})"
    if spent["runs"] >= ks["runs_factor"] * m_runs:
        limit = ks["runs_factor"] * m_runs
        return "runs", f"{spent['runs']} runs (limit {limit:g}, median {m_runs:g})"
    if spent["tokens"] >= ks["tokens_factor"] * m_tokens:
        return "tokens", f"{spent['tokens']} tokens (limit {ks['tokens_factor'] * m_tokens:g})"
    if klass := _stuck_class(conn, task_id, ks["same_class_rounds"], floor):
        n = ks["same_class_rounds"]
        return "same_class", f"finding class {klass} failed {n} rounds running"
    warned = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='runaway_warn'",
                          (task_id,)).fetchone()
    if spent["runs"] >= ks["runs_warn_factor"] * m_runs and warned is None:
        with txn(conn):
            event(conn, task_id, "runaway_warn",
                  {"trigger": "runs", "detail": f"{spent['runs']} runs"})
    return None


def _ask(conn, cfg, task_id, why, now, label=KINDS["runaway"]):
    reply = f"python3 -m capsule human {cfg['board']} {task_id} reset '<answer>'"
    outbox.notify(conn, "needs_input", f"{task_id} {label}: {why}"[:200],
                  fields=[("why", why[:300])], reply=reply, thread_key=task_id,
                  key=slack_msgs.needs_key(task_id), now=now,
                  file_body=f"capsule: {task_id} {label}: {why} | reply: {reply}"[:1500])


def _ask_owner(conn, task, detail, safety, now):
    """Row 642: the stop that goes straight to the owner, in plain words (the card's title, no id,
    no command: the chief of staff applies his answer)."""
    flag = " (includes a safety finding)" if safety else ""
    head = f'Card "{" ".join(task["title"].split())[:60]}" is stuck: {detail}{flag}. Your call:'
    outbox.notify(conn, "needs_input", head, fields=[("choices", OWNER_CHOICES)],
                  thread_key=task["id"], key=slack_msgs.needs_key(task["id"]), now=now,
                  file_body=f"capsule: {head} {OWNER_CHOICES}"[:1500])


def _verdict_runs(conn, task_id, impl):
    """run_id of the latest verdict run of each role in the round under review: the runs after
    `impl` (or after the last run a human `accept` saw), as `Tick._review_round` reads them."""
    floor = impl["id"]
    ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND run_id=? AND"
                      " kind='request_review' ORDER BY id DESC LIMIT 1",
                      (task_id, impl["id"])).fetchone()
    if ev is not None and ev[0]:
        floor = max(floor, json.loads(ev[0]).get("after_run") or 0)
    roles = security_review.BASE + (security_review.ROLE,)
    rows = conn.execute(
        f"SELECT profile, run_id FROM task_runs WHERE task_id=? AND id>? AND profile IN"
        f" ({','.join('?' * len(roles))}) AND COALESCE(outcome, '') != 'rate_limited'"
        " AND json_extract(COALESCE(metadata, '{}'), '$.ruling_of') IS NULL ORDER BY id",
        (task_id, floor, *roles)).fetchall()
    return list(dict(rows).values())   # a role retried in the round keeps its latest run


def step(conn, cfg, task, impl, now, text=""):
    """True when the failed round tripped: the card is stopped and nothing else may retry it."""
    hit = check(conn, cfg, task["id"])
    if hit is None:
        return False
    trigger, detail = hit
    spent = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='guide_ruling'"
                         " AND id>? AND json_extract(payload,'$.role')='runaway'",
                         (task["id"], _reset(conn, task["id"])[0])).fetchone()
    last = conn.execute("SELECT handoff FROM task_runs WHERE task_id=? AND profile='reviewer'"
                        " AND handoff IS NOT NULL ORDER BY id DESC LIMIT 1",
                        (task["id"],)).fetchone()
    found = json.loads(last[0]).get("findings", []) if last is not None else []
    safety = any(f.get("safety") for f in found)
    owner = trigger == "owner_rounds"   # row 642: no guide pass, whatever the guide did before
    with txn(conn):
        transition(conn, task["id"], "triage", frm=("review",), claim=True, block_kind="runaway",
                   block_recurrences=0)
        event(conn, task["id"], "runaway",
              {"trigger": trigger, "detail": detail,
               "pass": spent is None and not safety and not owner, "failures": text[:1500],
               "verdict_runs": _verdict_runs(conn, task["id"], impl)},
              run_pk=impl["id"], run_id=impl["run_id"], now=now)
        if owner:
            _ask_owner(conn, task, detail, safety, now)
        elif spent is not None or safety:
            why = "tripped again after the Opus pass" if spent is not None else "a safety finding"
            _ask(conn, cfg, task["id"], f"{why}: {detail}", now)
        else:
            outbox.notify(conn, "blocked",
                          f"{task['id']} stopped (runaway): {detail}; the guide rules once",
                          thread_key=task["id"], now=now,
                          file_body=f"capsule: {task['id']} stopped (runaway): {detail}"
                          + (f" | {text.splitlines()[0]}" if text.strip() else ""))
    return True


def pending_event(conn, task_id):
    """The card's latest `runaway` or `claims` event when it still wants the guide's pass."""
    ev = conn.execute("SELECT * FROM task_events WHERE task_id=? AND kind IN ('runaway', 'claims')"
                      " ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return ev if ev is not None and json.loads(ev["payload"]).get("pass") else None


def is_runaway(conn, ev_id):
    return conn is not None and ev_id is not None and conn.execute(
        "SELECT 1 FROM task_events WHERE id=? AND kind IN ('runaway', 'claims')",
        (ev_id,)).fetchone() is not None


def _lines(run_id, role, doc):
    if role == "reviewer":
        return [f"run {run_id} reviewer [{f['class']}] {f['file']}:{f['line']} {f['text']}"
                for f in doc.get("findings", [])]
    if role == "validator":
        def line(a):    # a blind row is one fixed phrase: the guide never reads its text
            if validator_report.is_blind(a["cmd"]):
                return validator_report.BLIND_FINDING
            return f"validator `{a['cmd']}` exit {a['exit']} {a.get('excerpt') or ''}"
        fails = [a for a in gate.scrubbed(doc).get("assertions", []) if a.get("result") == "fail"]
        return list(dict.fromkeys(f"run {run_id} {line(a)}" for a in fails))
    drifted = doc.get("verdict") == "drifted"
    return [f"run {run_id} guide drifted: {doc.get('reason')}"] if drifted else []


def finding_lines(conn, task_id):
    """One line per finding of every round (the reviewer, the validator rows, the guide)."""
    lines = []
    for run in conn.execute(
            "SELECT id, profile, handoff FROM task_runs WHERE task_id=? AND profile IN"
            " ('reviewer','validator','guide') AND handoff IS NOT NULL ORDER BY id", (task_id,)):
        lines += _lines(run["id"], run["profile"], json.loads(run["handoff"]))
    return lines


def packet(conn, ev):
    """What the guide rules on: the trip, the card's cost, and every finding of every round."""
    if ev["kind"] == "claims":
        return claims.packet(conn, ev)
    payload, lines = json.loads(ev["payload"]), finding_lines(conn, ev["task_id"])
    lines +=[f"tripping round: {x}" for x in (payload.get("failures") or "").splitlines()]
    return {"kind": "runaway", "role": "runaway", "trigger": payload["trigger"],
            "detail": payload["detail"], "cost": cost.card_cost(conn, ev["task_id"]),
            "findings": [line[:500] for line in lines][-200:]}


def lacks_fix_list(doc):
    return doc["recommended"] == "continue" and not (doc.get("context") or "").strip()


def apply(conn, cfg, run, doc, ev_id, now):
    """The guide's ruling: `continue` releases the card for one more run (the fix list reaches the
    implementer as a guide ruling); `split`, `ask-owner` leave it in triage as a needs-you."""
    task_id, choice = run["task_id"], doc["recommended"]
    reason = doc.get("context") or doc["question"]
    task = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    kind = conn.execute("SELECT kind FROM task_events WHERE id=?", (ev_id,)).fetchone()[0]
    waiting = task is not None and (task["status"], task["block_kind"]) == ("triage", kind)
    with txn(conn):
        event(conn, task_id, "guide_ruling",
              {"choice": choice, "reason": reason, "contradiction": ev_id, "role": kind,
               "applied": waiting}, run_pk=run["id"], run_id=run["run_id"], now=now)
        if waiting and choice == "continue":
            blocks.reset_failures(conn, task_id, by="guide", now=now)
        elif waiting:
            _ask(conn, cfg, task_id, f"the guide says {choice}: {reason}", now, KINDS[kind])
