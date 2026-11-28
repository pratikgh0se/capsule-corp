"""Helpers behind `capsule.landing`: whether a card's worker keeps crashing, whether a human
reverted its change on origin/main, which journal push is still unrecorded, the replay floor,
the landing wait words, a closed card's fetch pause and the `completed` event's payload (commit,
gates, acceptance line). Moved out of landing.py (800-line cap);
landing re-imports the names."""
import json
import re

from capsule import incidents, journal, lander, outbox, paths, seats, slack_msgs
from capsule.blind import text as blind_text
from capsule.db import guard_get, guard_set

HOLD, HOLD_ASKED, HOLD_ALARM_S = "lander.hold", "lander.holdask:", 2 * 3600   # ask: per hold set
CRASHED, CRASH_LIMIT = "lander.crashed:", 3
BUSY, LANDED = "lander.busy", "lander.landed:"
BUSY_TTL = seats.LANDER_TTL
CLOSED_RED = ("A person closed this card. Its commit is on main and failed the tests that run "
              "after a push. Main was left as it is and nothing was undone. You decide: undo it "
              "by hand, or accept it. Closing the incident changes nothing on main.")
FETCHFAIL = "lander.closedfetch:"    # per closed card: {count, until} of a failing origin fetch
CLOSING = ("landed", "push_failed", "push_abandoned")   # journal lines that close a `push` line
REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")


def _crash_limited(conn, task_id, sha):
    """True when its worker keeps crashing on this sha: asked once, left alone."""
    crashed = guard_get(conn, CRASHED + task_id)[0]
    return bool(crashed and crashed["sha"] == sha and crashed["count"] >= CRASH_LIMIT)


def _reverted_on_main(conn, repo, task_id, sha, branch="main", since=None):
    """True when origin/main holds a live revert of this completion's change, made since the
    card's latest `completed` event: of `sha`, or of a commit whose message names the card (the
    keeper's replay has another sha). Reverts are walked newest first: one whose own commit was
    reverted is dead (a reapply) and kills nothing; a live one kills its targets. None when git
    cannot say (the caller waits); False when nothing is live. `since=0` asks whether it was ever
    reverted, whatever the card did after."""
    if since is None:
        since = conn.execute("SELECT MAX(created_at) FROM task_events WHERE task_id=? AND "
                             "kind='completed'", (task_id,)).fetchone()[0] or 0
    rc, out = lander._git(repo, "log", f"origin/{branch}", "--fixed-strings", "--grep", task_id,
                          "--grep", "This reverts commit", "--format=%H%x1f%ct%x1f%B%x1e")
    if rc:
        return None
    logs = [r.strip().split("\x1f", 2) for r in out.split("\x1e") if r.strip()]
    killed = {}                       # sha prefix -> commit time of the live revert that killed it
    for h, ct, body in logs:
        if (targets := REVERTS.findall(body)) and not any(h.startswith(k) for k in killed):
            killed.update({t: int(ct) for t in targets})
    mine = {sha} | {h for h, _, body in logs if task_id in body and not REVERTS.search(body)}
    return any(m.startswith(t) and when >= since for m in mine for t, when in killed.items())


def _unrecorded_push(cfg, task_id, home, repo, tip=None):
    """The journal's `push` line this lander wrote that no `landed`, `push_failed` or
    `push_abandoned` line followed and whose sha is on `tip` (origin/main): a kill between the
    push and its record. None otherwise. `tip` defaults to the board's `origin/<lander.branch>`."""
    pending, tip = None, tip or f"refs/remotes/origin/{lander.target(cfg)}"
    for line in journal.read(paths.home(home) / "landing" / cfg["board"] / task_id):
        if line.get("step") in ("push", *CLOSING):    # the others close a push
            pending = line if line["step"] == "push" else None
    if pending and lander._git(repo, "merge-base", "--is-ancestor", pending["sha"], tip)[0] == 0:
        return pending
    return None


def _last_completed(conn, task_id):
    """The payload of the card's latest `completed` event ({} when it has none)."""
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='completed' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    return json.loads(last[0] or "{}") if last else {}


def fetch_waiting(conn, task_id, now):
    """True while a closed card's failed fetch of origin is backing off (that card alone: the
    pass skips it, so younger cards keep their turn)."""
    seen = guard_get(conn, FETCHFAIL + task_id)[0]
    return bool(seen) and now < seen["until"]


def _replayed_floor(conn, repo, task_id, commit, branch="main"):
    """The card commit the card's last landing replayed (that replay already put it, and what came
    before it, on main), or None: no landing, one the lander reverted, one reverted on main, one
    git cannot judge, or a card commit that is not under `commit`. A replay then picks only the
    commits after it; with None it picks all the card's commits from the base."""
    state, _ = guard_get(conn, LANDED + task_id)
    card = (state or {}).get("card")
    if not card or state.get("state") not in ("pushed", "verified"):
        return None
    if _reverted_on_main(conn, repo, task_id, state["sha"], branch, since=0) is not False:
        return None
    return card if lander._git(repo, "merge-base", "--is-ancestor", card, commit)[0] == 0 else None


def _waiting(conn, cfg, home, now):
    """`busy` or `no_seat` while a landing could not start, else None."""
    busy, _ = guard_get(conn, BUSY)
    if busy and busy.get("busy") and now - busy["at"] < BUSY_TTL:
        return "busy"
    got = seats.counts(conn, home, cfg["board"], now)    # our own flag is not held yet
    if got["landers"]:
        return "busy"
    return "no_seat" if got["heavy"] + 1 > seats.heavy_cap(cfg) else None


def _hold_alarm(conn, board, now):
    """Z5: a `lander.hold` older than HOLD_ALARM_S asks once and opens one incident, per hold (keyed
    by when it was set, so a cleared-then-new hold alarms again); repeat passes stay silent."""
    row = conn.execute("SELECT updated_at FROM guard_state WHERE key=?", (HOLD,)).fetchone()
    if row is None or now - row[0] < HOLD_ALARM_S or guard_get(conn, HOLD_ASKED + str(row[0]))[0]:
        return
    guard_set(conn, HOLD_ASKED + str(row[0]), {"at": now}, now=now)
    why = (guard_get(conn, HOLD)[0] or {}).get("reason", "")
    headline = f"lander hold stuck for {(now - row[0]) / 3600:.1f}h: deploy --check still drifts"
    outbox.notify(conn, "needs_input", headline, fields=[("detail", why[-400:])],
                  thread_key=HOLD, file_body=f"capsule: {headline}: {why[-300:]}",
                  key=slack_msgs.needs_key(HOLD), now=now)
    incidents.open_incident(conn, board=board, kind="silent_failure", task_id=None,
                            key="lander hold on deploy drift is stuck", evidence=[why[-800:]],
                            now=now)


def _acceptance(conn, task_id):
    """M115's line (`acceptance: green|red (K of N failing)|none`) from the card's latest
    `blind_gated` event; `acceptance: none` with no such event (gate mode off, none landed)."""
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blind_gated' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    seen = json.loads(last[0] or "{}") if last else {}
    return blind_text.acceptance_line(seen.get("counts") or {}, int(seen.get("failing") or 0))


def completed_payload(conn, task_id, sha, gates):
    """The `completed` event's payload: the commit (when there is one), the gates and the card's
    acceptance line, so the record shows whether acceptance was green."""
    return {**({"commit": sha} if sha else {}), "gates": gates,
            "acceptance": _acceptance(conn, task_id)}
