"""The guide's ruling on a tester's `red_exempt` list (blind spec 3.4; row 454 flow, the pattern of
`capsule/contradiction.py`). A listed test that passes on the base needs a ruling that its statement
describes behaviour the product already has: `red._file_request` moves the card to the guide's
triage and files one ruling request for the round (a `contradiction` event whose examples have
`KIND`); the guide's `red-exempt` or `not-exempt` is offered by `offered` and applied by `apply`
(an applied `guide_ruling` event, which `red._exempt_statements` reads), and the card goes back
where it was so `red.step` takes the round up again. Split from `red` for its size."""
import json

from capsule import bundles, config, ruling_sweep, runaway, stall_ruling
from capsule.blind import gate, settle
from capsule.db import event, transition, txn

ROLE = config.BLIND_TESTER
KIND = "red_exempt"        # the `kind` of each example of a request's `contradiction` event
IDS = tuple(i for i, _ in bundles.RED_EXEMPT_OPTIONS)
UNPROCESSED = (         # a finished tester round (of `task_runs r`) the red check has not settled
    "profile=? AND status='done' AND outcome='completed' AND handoff IS NOT NULL AND NOT EXISTS"
    " (SELECT 1 FROM task_events e WHERE e.run_id=r.id AND e.kind='blind_red_checked')")


def reviewable(conn, cfg):
    """The cards in `review` the tick may judge now. `red.step` runs after the review step in a
    pass, so with blind tests on a card whose tester round is not processed yet (or is deferred,
    or waits for a ruling) gets no verdict: it could land first and the round go stale."""
    held = "" if config.redesign(cfg)["blind_tests"] == "off" else (
        f" AND NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=t.id AND {UNPROCESSED})")
    rows = conn.execute("SELECT * FROM tasks t WHERE t.status='review'" + held
                        + " ORDER BY t.created_at", (ROLE,) if held else ()).fetchall()
    return [t for t in rows if not settle.red_waiting(conn, cfg, t["id"])]   # no review waits


def _requests(conn, **where):
    """The red-exempt requests' `contradiction` events (an intent contradiction of the same
    role has examples of no `kind`), optionally of one `run_id` or `id`."""
    cond = "".join(f" AND {k}=?" for k in where)
    return [e for e in conn.execute("SELECT * FROM task_events WHERE kind='contradiction'" + cond,
                                    tuple(where.values()))
            if any(x.get("kind") == KIND for x in json.loads(e["payload"] or "{}")["examples"])]


def is_request(conn, ev_id):
    """Whether contradiction event `ev_id` is a tester's red-exempt request."""
    return bool(_requests(conn, id=ev_id))


def offered(conn, ev_id, default):
    """The choice ids the guide may give on event `ev_id`: a request's, a runaway or claims
    hold's, else `default` (an intent contradiction's)."""
    if is_request(conn, ev_id):
        return IDS
    if stall_ruling.is_stall(conn, ev_id):
        return stall_ruling.IDS
    return runaway.IDS if runaway.is_runaway(conn, ev_id) else default


def applier(conn, ev_id, default):
    """The function that applies the guide's ruling on event `ev_id`: `apply` for a request,
    `stall_ruling.apply` for a stall ruling, `runaway.apply` for a runaway or claims hold, else
    `default`."""
    if is_request(conn, ev_id):
        return apply
    if stall_ruling.is_stall(conn, ev_id):
        return stall_ruling.apply
    return runaway.apply if runaway.is_runaway(conn, ev_id) else default


def filed(conn, run_pk):
    """Whether the tester round of run `run_pk` has filed its request (it files at most one)."""
    return bool(_requests(conn, run_id=run_pk))


def waiting(conn, run):
    """Whether the tester round `run` has a request the guide has not answered: it was filed and
    its card still sits in the guide's triage. The round is not processed again meanwhile."""
    if not filed(conn, run["id"]):
        return False
    row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                       (run["task_id"],)).fetchone()
    return row is not None and (row["status"], row["block_kind"]) == ("triage", "guide_ruling")


def movable(conn, task):
    """Whether the card can go to the guide's triage now: `ready`, or `review` with no live
    validator, reviewer, guide or security run (that run would lose its round)."""
    live = conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND ended_at IS NULL AND profile"
                        " IN ('validator', 'reviewer', 'guide', 'security')",
                        (task["id"],)).fetchone()
    return task["status"] == "ready" or (task["status"] == "review" and live is None)


def report(conn, ev_id, run):
    """The document the guide is shown for contradiction event `ev_id`, raised by `run`: its
    report (`gate.scrubbed`: no blind row's excerpt or expected text), but none for a red-exempt
    request (statements and test ids only, no tester report)."""
    if is_request(conn, ev_id) or run is None:
        return None
    return gate.scrubbed(json.loads(run["handoff"])) if run["handoff"] else None


def asks(covered, listed, passing, acceptance, ruled):
    """{statement: [test ids]} to put to the guide: the `listed` (red_exempt) tests that are in
    `passing` (pass on the base) and sit only under statements of the card's `acceptance`, under
    each such statement a ruling (`ruled`) does not exempt yet. A test under no statement, or
    under one the card does not have, is never asked about."""
    under = {}
    for entry in covered:
        for test in entry["tests"]:
            under.setdefault(test, set()).add(entry["statement"])
    out = {}
    for test in sorted(set(listed) & set(passing)):
        found = under.get(test, set())
        if found and all(1 <= n <= len(acceptance) for n in found):
            for n in sorted(found - set(ruled)):
                out.setdefault(n, []).append(test)
    return out


def apply(conn, cfg, run, doc, ev_id, now):
    """The guide's ruling on a request: an applied `guide_ruling` event naming the statements
    (`statement N` in `examples`), the request card closed, and the card back in `ready` or
    `review` where it was, so its round is checked again. A card no longer waiting only gets the
    event, unapplied."""
    task_id, choice = run["task_id"], doc["recommended"]
    ev = json.loads(conn.execute("SELECT payload FROM task_events WHERE id=?",
                                 (ev_id,)).fetchone()[0])
    ids = [f["id"] for f in ev["examples"]]
    card = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (task_id,)).fetchone()
    latest = conn.execute("SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='contradiction'"
                          " AND json_extract(payload,'$.routed')='guide'", (task_id,)).fetchone()[0]
    held = latest == ev_id and (card["status"], card["block_kind"]) == ("triage", "guide_ruling")
    with txn(conn):
        # `reason` and `examples` are what `red._exempt_statements` scans: only the asked ids;
        # the guide's own words go in `text`, which it never reads
        event(conn, task_id, "guide_ruling",
              {"choice": choice, "reason": f"{choice}: {', '.join(ids)}",
               "text": doc.get("context") or doc["question"],
               "card_hash": ev["examples"][0]["card_hash"],
               "contradiction": ev_id, "role": ROLE, "examples": ", ".join(ids),
               "fingerprint": sorted(ids), "applied": held},
              run_pk=run["id"], run_id=run["run_id"], now=now)
        if held:
            ruling_sweep.close_card(conn, ev["ruling_card"], now)
            transition(conn, task_id, ev["examples"][0]["was"], frm=("triage",), claim=True,
                       block_kind=None)
