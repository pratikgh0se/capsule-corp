"""How a blind tester round ends without landing (blind spec 3.4; R31): `bounce` sends the round
back to the tester (the next run gets the outcomes; the second bounce triages the card with
`could not write a failing test for statement N`) and `finish` triages a card for good. Both
leave one `blind_red_checked` event on the round's run, which is what marks the round settled.

A card is triaged only from `ready` or `review`: triage would end a running implementer's live run
and would overwrite the block of a blocked card. Otherwise the round is recorded as
`triage_pending` and `release` triages the card on a later pass (`waiting` lists those). Split
from `red` for its size."""
import json
import shutil
from pathlib import Path

from capsule import blocks, bundles, config, workspace
from capsule.db import event, txn

MAX_ROUNDS = 2              # tester rounds without a landing before the card is triaged
TRIAGEABLE = ("ready", "review")


def events(conn, task_id, kind):
    """[(event id, run id (task_runs.id), payload)] of the card's events of `kind`, oldest first."""
    return [(r["id"], r["run_id"], json.loads(r["payload"] or "{}")) for r in conn.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind))]


def record(conn, run, verdict, now, **payload):
    """The round's `blind_red_checked` event: `verdict` and whatever else says why."""
    with txn(conn):
        event(conn, run["task_id"], "blind_red_checked", {"verdict": verdict, **payload},
              run_pk=run["id"], run_id=run["run_id"], now=now)


def cleanup(clone):
    """The tester's clone and its git dir, once its round is settled."""
    for path in (clone, workspace.gitdir(clone)):
        shutil.rmtree(path, ignore_errors=True)


def rounds(conn, task_id, card_hash):
    """Tester rounds of this card hash that reached the red check or the lint without landing."""
    return sum(p.get("card_hash") == card_hash and "round" in p
               for _, _, p in events(conn, task_id, "blind_red_checked"))


def _reason(run, outcomes, problems):
    """The plain triage reason for a round that bounced a second time."""
    covered = json.loads(run["handoff"]).get("covered") or []
    bad = {t.rsplit(".", 1)[-1] for t, v in outcomes.items() if v != "ok"}
    nums = sorted({c["statement"] for c in covered if bad & {t.rsplit(".", 1)[-1]
                                                             for t in c["tests"]}})
    if nums:
        more = f" (also statements {', '.join(map(str, nums[1:]))})" if nums[1:] else ""
        return f"could not write a failing test for statement {nums[0]}{more}"
    return (f"could not write tests that pass the lint ({len(problems)} problems)"
            if problems else "could not write a failing test for any statement")


def bounce(conn, run, task, card_hash, outcomes, problems, commit, now):
    """The round did not land: record it (`bounced`, with the outcomes, the lint problems and the
    checked commit the next tester starts from) and send the outcomes back, names and outcomes
    only. The second bounce triages the card. Always False: no red check container ran."""
    n = rounds(conn, task["id"], card_hash) + 1
    shown = {"round": n, "card_hash": card_hash, "outcomes": outcomes, "problems": problems[:20],
             "tested": commit}
    if n >= MAX_ROUNDS:
        finish(conn, run, task, now, _reason(run, outcomes, problems), **shown)
    else:
        record(conn, run, "bounced", now, **shown)
    cleanup(Path(json.loads(run["metadata"])["blind_clone"]))
    return False


def _triage(conn, run, task_id, reason, now):
    """Triage the card with a plain reason (`needs_input`: `reset` or `accept` it); True when it
    moved. Only a `ready` or `review` card moves: a running one would lose its live run, any other
    is blocked or finished already and keeps the block it has."""
    with txn(conn):
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
        if status not in TRIAGEABLE:
            return False
        conn.execute("UPDATE tasks SET block_kind='needs_input', block_recurrences="
                     "MAX(block_recurrences, 1) WHERE id=?", (task_id,))
        return blocks.block(conn, task_id, kind="needs_input", reason=reason,
                            run_id=run["run_id"], now=now) == "triage"


def finish(conn, run, task, now, reason, **payload):
    """Settle the round by triaging the card with `reason`, or by recording `triage_pending` when
    the card cannot be triaged now (`release` triages it later)."""
    with txn(conn):
        moved = _triage(conn, run, task["id"], reason, now)
        record(conn, run, "triaged" if moved else "triage_pending", now, reason=reason[:500],
               **payload)


def waiting(conn, task_id=None):
    """The `triage_pending` events (of one card, or all) whose card has not been triaged (or
    dropped) yet."""
    return conn.execute(
        "SELECT e.* FROM task_events e WHERE e.kind='blind_red_checked'"
        " AND (? IS NULL OR e.task_id=?)"
        " AND json_extract(e.payload,'$.verdict')='triage_pending' AND NOT EXISTS ("
        "SELECT 1 FROM task_events f WHERE f.task_id=e.task_id AND f.run_id=e.run_id"
        " AND f.kind='blind_red_checked'"
        " AND json_extract(f.payload,'$.verdict') IN ('triaged','stale'))"
        " ORDER BY e.id", (task_id, task_id)).fetchall()


def _outdated(conn, task, run, pending):
    """Why the pending round no longer matters, or None: the card is finished, its text has
    changed since the round (a new hash gets its own rounds), or a person reset it since (a
    guide's reset does not count)."""
    if task is None or task["status"] in ("done", "archived"):
        return "the card is finished"
    if json.loads(run["metadata"] or "{}").get("card_hash") != bundles.card_hash(task["body"]):
        return "the card changed since the round"
    reset = conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='failure_reset'"
                         " AND json_extract(payload,'$.by')='human' AND id>?",
                         (task["id"], pending["id"])).fetchone()      # as runaway.py: a person's
    return "the card was reset by a person since the round" if reset else None


def release(conn, pending, now):
    """Triage the card of a `waiting` event once it is `ready` or `review`. A round that no
    longer matters (`_outdated`) is recorded `stale` and triages nothing; any other state keeps
    waiting."""
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (pending["task_id"],)).fetchone()
    run = conn.execute("SELECT * FROM task_runs WHERE id=?", (pending["run_id"],)).fetchone()
    if run is None:
        return
    why = _outdated(conn, task, run, pending)
    if why:
        record(conn, run, "stale", now, reason=why)
        return
    reason = json.loads(pending["payload"])["reason"]
    with txn(conn):
        if _triage(conn, run, task["id"], reason, now):
            record(conn, run, "triaged", now, reason=reason)


def enabled(cfg):
    """Red checks can run: blind tests are on and an epic is configured (`red.step` is reached)."""
    return config.redesign(cfg)["blind_tests"] != "off" \
        and bool(config.epic_branches_from_list(cfg.get("epics")))


def red_waiting(conn, cfg, task_id):
    """True while the card must not land or be published: a tester round for its current text is
    running (a hung or crashed one ends through the tick's timeout or close, a failed one gets at
    most `dispatch.MAX_TRIES` runs), a finished round's red check has not run (no
    `blind_red_checked` event on its run), or a triage waits for the card to be ready or in review
    (`waiting`; `release` applies it), where `red.step` can run."""
    body = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    running = body and conn.execute(
        "SELECT 1 FROM task_runs WHERE task_id=? AND profile=? AND ended_at IS NULL"
        " AND json_extract(metadata,'$.card_hash')=?",
        (task_id, config.BLIND_TESTER, bundles.card_hash(body[0]))).fetchone()
    pending = bool(waiting(conn, task_id))      # a triage `release` owes
    return enabled(cfg) and bool(running or unsettled(conn, task_id) or pending)


def unsettled(conn, task_id=None):
    """The finished tester rounds (of one card, or all) whose red check has not run: no
    `blind_red_checked` event on the run. While a card has one, its red check is waiting and the
    card must not land; `red.step` takes the round to a verdict, passed or failed, later in the
    same pass."""
    return conn.execute(
        "SELECT * FROM task_runs r WHERE profile=? AND status='done' AND outcome='completed'"
        " AND handoff IS NOT NULL AND NOT EXISTS (SELECT 1 FROM task_events e WHERE e.run_id=r.id"
        " AND e.kind='blind_red_checked') AND (? IS NULL OR task_id=?) ORDER BY id",
        (config.BLIND_TESTER, task_id, task_id)).fetchall()
