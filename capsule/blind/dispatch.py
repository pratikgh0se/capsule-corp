"""Blind tester dispatch (blind spec 3.1, 3.2, A2.3, A2.4; R30): for a card that is `ready`,
`running` or in `review` (its validation not yet done) and the board's `redesign.blind_tests` is
`report` or `gate`, the tick starts one `blind-tester` run for the card's current hash, after the
implementer dispatch so it never holds a start up. `off` (the default) and a board with no
`blind_tests` block change nothing.

The run is review-shaped: no lease, no card ownership, one review seat. Its container mounts only
a host-side fresh clone of the blind repo on branch `card/<id>` at /workspace (read-write), that
clone's git dir (kept beside it, read-only, GIT_DIR pinned), the interface snapshot at
/interface:ro and /deps:ro: no project worktree, no writable git dir, no prior spools.
`start_sha` (the implementer's pinned one, else the epic head) goes into the run's metadata. A card
with no acceptance, of the light tier (a mac card gets one; its suite runs on the host), with a
production `tester_may_read` glob or a leaking brief
gets no run and one `blind_tester_skipped` event (as does one whose tester runs all failed): this
module neither holds nor fails the card, but in gate mode its verdict holds it, `no blind tests
yet`, until its tests land (`gate.release`) or a person accepts it (`gate.hold`).
`capsule.blind.close.close` ends the run. Linting the tests, the red check and landing are
`red.py` (R31), called at the end of `step`. A round the red check bounced gets a new run whose
brief carries the outcomes; a card whose body changed after its tests landed gets one with the
diff of the acceptance text only, and none when that text is unchanged. The tester's clone is a
sparse copy: its own card's folder and the shared harness, no blind git history."""
import difflib
import json
import re

from capsule import bundles, config, dispatch, seats
from capsule.blind import preflight, red, settle
from capsule.db import event, txn

ROLE = "blind-tester"
TESTED_TIERS = ("strong", "mac")    # the light tier gets none: seatbelt cannot nest (host_wall)
COMMAND_TIMEOUT = 120          # seconds per `interface_commands` entry
SETUP_TOTAL = 300              # seconds for all of them, from before the clones (a job thread)
MAX_TRIES = 2                  # tester runs per card hash that may end failed (any step)
MAX_STATEMENTS = 100           # blind_tests.v1 numbers statements 1..100
_LABEL = re.compile(r"^\s*(?:#+\s*|[-*]\s+|\*\*|__)?(?:acceptance(?: criteria)?|done[- ]when"
                    r"|definition of done)\b[\s:*_]*(.*)$", re.I)
_ITEM = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+(?:\[[ xX]\]\s+)?(\S.*?)\s*$")
_HEADING = re.compile(r"^\s*#{1,6}\s")


def acceptance(body):
    """The card's acceptance statements, in order: the text after an `Acceptance`, `Acceptance
    criteria`, `Done when` or `Definition of done` label, then the list items under it, up to the
    next heading or unindented prose line. [] when the card has none."""
    found, inside = [], False
    for line in (body or "").splitlines():
        item, label = _ITEM.match(line), _LABEL.match(line)
        if inside and item:
            found.append(item[1])
        elif label:
            inside = True
            if label[1].strip():
                found.append(label[1].strip())
        elif inside and (_HEADING.match(line) or (line.strip() and not line[0].isspace())):
            inside = False
    return found[:MAX_STATEMENTS]


def limits(runner, role, inputs, sp):
    """`runner.limits` for a review-shaped run. For the blind tester the strong tier's `--tools`
    list stays (Read, Edit, Write, Glob, Grep); only the grant shrinks: `allowed` is the exec tool
    alone and `add_dirs` is empty, so its host-side `claude` is given no directory but its own
    cwd (the blind clone). That is a permission limit, not a tool limit."""
    if role != ROLE:
        return runner.limits(role, sp)
    return {**runner.limits(inputs["container"], sp), "allowed": "mcp__capsule__exec",
            "add_dirs": []}


_VALIDATED = (      # a review card whose validator completed after its latest implementer run
    "(t.status='review' AND EXISTS (SELECT 1 FROM task_runs v WHERE v.task_id=t.id"
    " AND v.profile='validator' AND v.outcome='completed' AND v.id >"
    " COALESCE((SELECT MAX(i.id) FROM task_runs i WHERE i.task_id=t.id"
    " AND i.profile='implementer'), 0)))")
_CANDIDATES = (
    "SELECT * FROM tasks t WHERE t.status IN ('ready','running','review')"
    f" AND t.branch_name IN ({{marks}}) AND NOT {_VALIDATED}"
    " ORDER BY t.priority DESC, t.created_at ASC")
_DROPPED = ("SELECT * FROM tasks t WHERE t.branch_name IN ({marks}) AND " + _VALIDATED)


def step(tick, conn, cfg, now, counts):
    """Once a pass, after the implementer dispatch (a tester never holds a start up): a tester for
    every running or review card, and for the ready card that is first on its branch in
    `dispatch.ready` (the one whose base commit is the head its implementer will start from; a
    card behind it, or behind a busy branch, waits). A card for which a tester already exists, or
    whose validation is done, gets none. Runs while a seat is free."""
    epics = _epics(tick, cfg)
    if epics or "blind_setups" in tick.__dict__:    # tester setups run on threads: `launch.poll`
        from capsule.blind import launch            # (imports this module's ROLE and `_skip`)
        launch.poll(tick, conn, cfg, now, counts)
    if not epics:
        return
    first = {}
    for row in dispatch.ready(conn, cfg, tick.host_tiers, now, tick.home,
                              getattr(tick, "mac_hold", None)):
        first.setdefault(row["branch_name"], row["id"])
    rows = conn.execute(_CANDIDATES.format(marks=",".join("?" * len(epics))), epics).fetchall()
    for row in rows:
        if row["status"] == "ready" and first.get(row["branch_name"]) != row["id"]:
            continue
        tick._guarded(conn, cfg, row["id"], now, counts,
                      lambda row=row: _dispatch_one(tick, conn, cfg, row, now, counts))
    red.step(tick, conn, cfg, now, counts)


def _epics(tick, cfg):
    """The epic branches the step works on; [] when blind tests are off or no strong tier."""
    if config.redesign(cfg)["blind_tests"] == "off" or "strong" not in tick.host_tiers:
        return []
    return config.epic_branches(cfg)


def late(tick, conn, cfg, now, counts):
    """Called by the tick after the review runs close and before the verdicts, while a card whose
    validator just completed is still in `review`: a card with no tester run for its hash that was
    not refused (any skip event for the hash counts) gets one `blind_tester_skipped` event saying
    no seat freed in time, once per hash. It is not final: the card is not failed or held, and a
    verdict that sends it back for rework (same hash) still gets its tester."""
    epics = _epics(tick, cfg)
    if not epics:
        return
    for row in conn.execute(_DROPPED.format(marks=",".join("?" * len(epics))), epics).fetchall():
        tick._guarded(conn, cfg, row["id"], now, counts, lambda row=row: _late_one(conn, row, now))


def _late_one(conn, task, now):
    card_hash = bundles.card_hash(task["body"])
    seen = conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=:t AND profile=:p AND status!='released'"
        " AND json_extract(metadata,'$.card_hash')=:h UNION ALL"
        " SELECT 1 FROM task_events WHERE task_id=:t AND kind='blind_tester_skipped'"
        " AND json_extract(payload,'$.card_hash')=:h",
        {"t": task["id"], "p": ROLE, "h": card_hash}).fetchone()
    if seen is None:
        _skip(conn, task, card_hash, "no tester seat freed before the card's validation was done",
              now, final=False)


def _known(conn, task_id, card_hash):
    """True when this card hash needs no new tester run: one is open, done or skipped, or
    `MAX_TRIES` ended failed or timed out (D210; a release that paused the board counts for
    nothing; one that paused nothing, a host-wall start-up timeout, counts: it is retried at
    once), or a final skip was
    recorded for it. A run whose round the red check bounced counts for nothing: the next
    round gets a new run (`settle.rounds` caps them)."""
    runs = [r[0] for r in conn.execute(
        "SELECT CASE WHEN status='released' THEN 'failed' ELSE status END FROM task_runs r"
        " WHERE task_id=? AND profile=?"
        " AND (status!='released' OR json_extract(metadata,'$.unpaused_release'))"
        " AND json_extract(metadata,'$.card_hash')=?"
        " AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id=r.id"
        " AND e.kind='blind_red_checked' AND json_extract(e.payload,'$.verdict')='bounced')",
        (task_id, ROLE, card_hash))]
    skip = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='blind_tester_skipped'"
                        " AND json_extract(payload,'$.card_hash')=?"
                        " AND json_extract(payload,'$.final')",
                        (task_id, card_hash)).fetchone()
    return (any(s not in ("failed", "timed_out") for s in runs) or len(runs) >= MAX_TRIES
            or skip is not None)


def _landed(conn, task_id):
    """The payload of the card's latest `blind_landed` event, or None when nothing landed."""
    found = settle.events(conn, task_id, "blind_landed")
    return found[-1][2] if found else None


def _rerun_inputs(conn, task, card_hash, statements, now):
    """(seed commit, extra card keys, skip reason) for a tester run of `task` at `card_hash`.
    The seed is the blind commit whose `cards/<id>` the tester's clone starts from (None for a
    first run). `extra` holds `red_check` (the outcomes of the round that bounced) and
    `acceptance_diff` (stale tests: the diff of the acceptance text only). The skip reason is set
    when the tests landed, the body changed and the acceptance text did not: no new run."""
    seed, extra, last = None, {}, _landed(conn, task["id"])
    if last and last["card_hash"] != card_hash:
        old = last.get("acceptance", [])
        diff = "\n".join(difflib.unified_diff(
            [f"{n}. {s}" for n, s in enumerate(old, 1)],
            [f"{n}. {s}" for n, s in enumerate(statements, 1)], "acceptance (landed)",
            "acceptance (now)", lineterm="", n=1))
        if not any(p.get("card_hash") == card_hash for _, _, p in settle.events(
                conn, task["id"], "blind_tests_stale")):
            with txn(conn):
                event(conn, task["id"], "blind_tests_stale", {
                    "card_hash": card_hash, "landed_hash": last["card_hash"],
                    "blind_commit": last["blind_commit"], "acceptance_changed": bool(diff)},
                    now=now)
        if not diff:
            return None, {}, "the acceptance text is unchanged since the tests landed"
        seed, extra = last["blind_commit"], {"acceptance_diff": diff}
    bounced = [p for _, _, p in settle.events(conn, task["id"], "blind_red_checked")
               if p.get("card_hash") == card_hash and p["verdict"] == "bounced"]
    if bounced:
        got = bounced[-1]
        seed = got.get("tested") or seed
        extra["red_check"] = {"round": got["round"], "tests": got.get("outcomes", {}),
                              "problems": got.get("problems", [])}
    return seed, extra, None


def _skip(conn, task, card_hash, reason, now, *, final=True):
    """One `blind_tester_skipped` event, not repeated every pass for the same reason."""
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind="
                        "'blind_tester_skipped' ORDER BY id DESC LIMIT 1", (task["id"],)).fetchone()
    if last is not None and json.loads(last[0]) == {"reason": reason, "card_hash": card_hash,
                                                    "final": final}:
        return False
    with txn(conn):
        event(conn, task["id"], "blind_tester_skipped",
              {"reason": reason, "card_hash": card_hash, "final": final}, now=now)
    return False


def _dispatch_one(tick, conn, cfg, task, now, counts):
    card_hash = bundles.card_hash(task["body"])
    if _known(conn, task["id"], card_hash):
        return
    statements = acceptance(task["body"])
    if not statements:
        return _skip(conn, task, card_hash, "the card has no acceptance statements to test", now)
    if (task["tier"] or "strong") not in TESTED_TIERS:
        return _skip(conn, task, card_hash, "the tester runs in the strong tier only", now)
    seed, extra, unchanged = _rerun_inputs(conn, task, card_hash, statements, now)
    if unchanged:
        return _skip(conn, task, card_hash, unchanged, now)
    problems = preflight.problems(cfg, cfg["board"], preflight.worker_mounts(cfg, cfg["board"],
                                                                             tick.home))
    if ROLE not in cfg["routing"]:
        problems.append(f"routing.{ROLE} is missing")
    if problems:
        return _skip(conn, task, card_hash, "blind tests unavailable: " + "; ".join(problems),
                     now, final=False)
    route = config.route(cfg, ROLE)
    if tick._paused(conn, route["account"], now):
        return
    if _seat_free(conn, cfg, tick.home, now):
        from capsule.blind import launch        # launch reads this module's ROLE and _skip
        counts["review_runs"] += launch._start(tick, conn, cfg, task, statements, card_hash,
                                               route, now, (seed, extra))


def _seat_free(conn, cfg, home, now):
    """A review seat for a tester: one that is free and one the other review roles keep. With
    `caps.max_review` N at most N-1 tester runs are live across every board (none at N=1), and
    one heavy seat stays free for an implementer (none at a limit of 1). No `review_deferred`
    event: the card is not held, the tester starts on a later pass."""
    snap = seats.snapshot(conn, cfg, home, now)
    cap, heavy = seats.limits(cfg)
    return (not snap["unreadable"] and snap["testers"] < cap - 1 and snap["review"] < cap
            and snap["heavy"] < heavy - 1)
