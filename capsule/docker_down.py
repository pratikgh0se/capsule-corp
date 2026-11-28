"""Z5 detector (D159): Docker unavailable while cards wait on a container, for DOWN_S or longer,
opens one incident and sends one alert, so a stopped factory with no fix in hand tells the owner
instead of showing only the 3-hour stall line in `capsule status`.

`step` runs once per board pass with the tick's own availability answer (`up`, the strong
runner's `available()`: no second probe). Guard `docker_down` holds `{since, waiting, fired_at}`
while the state lasts: docker down and at least one card waiting on a container. It is cleared
when docker has been back for UP_PASSES passes in a row (a one-pass flap keeps the clock), when
nothing waits, and under `pause:board` (a deliberate stop). It fires at
most once per episode, after `docker_down_s` (board config, default DOWN_S); a later outage is a
new incident, or a repeat by `incidents.open_incident`'s own rules. `step` never raises. The
record also keeps `checked` (the last pass) and `tick_s`; `state` hides it once that check is older
than twice the tick, or while the board is paused, so a stopped tick never lets the line grow."""
import json
import sys
import time

from capsule import blocks, db, dispatch, exec_outage, incidents, outbox
from capsule.db import guard_get, guard_set
from capsule.landing_reverts import LANDED

GUARD = "docker_down"
DOWN_S = 15 * 60
UP_PASSES = 2
KEY = "docker unavailable: cards are waiting on a container"


def waiting(conn):
    """Ids of the cards a container would move: a ready strong card (implementer start), any card
    in review (the validator needs a strong container whatever the tier), a lander wait on the
    sandbox or held for an exec outage, a landed card whose suites cannot run."""
    ids = [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status='review'")]
    ids += [r[0] for r in conn.execute(      # a parent gate holds only a card that has not started
        "SELECT id FROM tasks WHERE status='ready' AND COALESCE(tier,'strong')='strong'")
        if db.open_parent(conn, r[0]) is None]
    ids += [r[0] for r in conn.execute("SELECT id FROM tasks WHERE status='blocked'")
            if blocks.lander_wait(conn, r[0]) == "lander_wait_docker"
            or exec_outage.held_for_outage(conn, r[0])]
    for key, raw in conn.execute("SELECT key, value FROM guard_state WHERE key LIKE ?",
                                 (LANDED + "%",)):
        tid = key[len(LANDED):]
        if tid not in ids and _held_landing(conn, tid, raw):
            ids.append(tid)
    return ids


def _held_landing(conn, tid, raw):
    """A landed card whose after-push suites are still waiting: a live `pushed` record with
    `held_since` on a card that is still `done` (a stale `held_since` on a later state is not)."""
    try:
        held = json.loads(raw)
    except ValueError:
        return False
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    return (isinstance(held, dict) and "held_since" in held and held.get("state") == "pushed"
            and row is not None and row[0] == "done")


def _record(conn):
    """(record, version); a corrupt or non-dict guard value is no record."""
    try:
        rec, version = guard_get(conn, GUARD)
    except ValueError:
        row = conn.execute("SELECT version FROM guard_state WHERE key=?", (GUARD,)).fetchone()
        return None, row[0]
    return (rec, version) if isinstance(rec, dict) or rec is None else (None, version)


def state(conn, now=None):
    """The record while docker is down with cards waiting (`since`, `waiting`), else None: also
    None when the last check is older than twice the tick or the board is paused."""
    now = int(time.time()) if now is None else now
    rec = _record(conn)[0]
    if not rec or rec.get("up") or dispatch.board_paused(conn, now):
        return None
    return rec if 0 <= now - rec.get("checked", 0) <= 2 * rec.get("tick_s", 30) else None


def step(conn, cfg, up, now):
    try:
        if not _step(conn, cfg, up, now):     # a lost version race: read again, once
            _step(conn, cfg, up, now)
    except Exception as exc:
        print(f"capsule tick: docker-down check failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)


def _step(conn, cfg, up, now):
    """True when the pass's write landed (or there was nothing to write)."""
    rec, version = _record(conn)
    stamp = {"checked": now, "tick_s": (cfg.get("caps") or {}).get("tick_seconds", 30)}
    paused = dispatch.board_paused(conn, now)
    if up and rec is not None and not paused and rec.get("up", 0) + 1 < UP_PASSES:
        return guard_set(conn, GUARD, {**rec, **stamp, "up": rec.get("up", 0) + 1},
                         expect_version=version, now=now)    # one pass back is not the end
    ids = [] if up or paused else waiting(conn)
    if not ids:
        return rec is None or guard_set(conn, GUARD, None, expect_version=version, now=now)
    rec = {"fired_at": None, **(rec or {}), **stamp, "waiting": len(ids), "up": 0}
    if rec.setdefault("since", now) > now:
        rec.update(since=now, fired_at=None)       # the clock went backwards: a new window
    if not rec["fired_at"] and now - rec["since"] >= cfg.get("docker_down_s", DOWN_S):
        _fire(conn, cfg, rec, now)
        rec["fired_at"] = now
    return guard_set(conn, GUARD, rec, expect_version=version, now=now)


def _fire(conn, cfg, rec, now):
    mins = max(0, now - rec["since"]) // 60
    what = f"Docker down for {mins} min, {rec['waiting']} cards waiting"
    _, action = incidents.open_incident(conn, board=cfg["board"], kind="silent_failure", key=KEY,
                                        evidence=[what], now=now)
    if action == "opened":      # a repeat already pages once (row 456); a deduped one has paged
        outbox.notify(conn, "incident", f"capsule: {what}", page=True,
                      file_body=f"capsule: {what}: container work cannot start", now=now)
