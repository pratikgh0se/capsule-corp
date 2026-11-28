"""Host guards (stall supervisor unit 4b, docs/architecture/components/stall-supervisor.md): the
disk rows and the failure storm (class `failure_storm`, G9).

Disk: `step` reads free GB once per tick pass. Under `stall.disk_floor_gb` (15) it kicks
the D204 `disk-relief` job (once per `KICK_S`) and sets guard `host:disk`, which stops new
strong-tier (container) cards (`gate`). Under `stall.disk_pause_gb` (8) it holds
`pause:board` with reason `disk` and keeps it until free space is back over the floor. Nothing is
deleted here.

Storm: 5 ended runs in a row, on 3 or more cards, all `spawn_failed` or crashed, inside 30 min.
`step` pauses the board for 1 h, then 2, 4 and 6 h (guard `host:storm` keeps the level). After
each pause one card probes (`gate`): a failure pauses at the next level, a success resets.
`probes` (optional, injected by a test): `disk_free` (callable, bytes), `kickstart(label)`."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

from capsule import config, outbox, tick_watch
from capsule.db import event, guard_get, guard_set, txn

DISK, STORM, PAUSE = "host:disk", "host:storm", "pause:board"
GB, KICK_S, PAUSE_TTL_S = 10**9, 1800, 900
RUNS, CARDS, WINDOW_S, QUIET_S = 5, 3, 1800, 86400
STORM_PAUSE_S = (3600, 7200, 14400, 21600)
GUARD_REASONS = ("disk", "failure_storm")
FAILED = ("(outcome='spawn_failed' OR (outcome='failed' AND EXISTS (SELECT 1 FROM task_events e"
          " WHERE e.run_id=task_runs.id AND e.kind='crashed')))")


def _act(conn, cls, board, action, now, **extra):
    with txn(conn):
        event(conn, "stall", "stall_action", {"class": cls, "subject": board, "action": action,
                                              **extra}, now=now)


def _pause(conn, until, reason, now):
    """Hold `pause:board` for `reason`; False when a pause for another reason is live."""
    held = guard_get(conn, PAUSE)[0]
    live = isinstance(held, dict) and held.get("until", 0) > now
    if live and held.get("reason") != reason:
        return False
    guard_set(conn, PAUSE, {"until": until, "since": held["since"] if live else now,
                            "reason": reason}, now=now)
    return True


def _lift(conn, reason, now):
    held = guard_get(conn, PAUSE)[0]
    if isinstance(held, dict) and held.get("reason") == reason and held.get("until", 0) > now:
        guard_set(conn, PAUSE, {**held, "until": now}, now=now)


def _disk_low(conn):
    """True while free space is under the floor: no new container card starts."""
    rec = guard_get(conn, DISK)[0]
    return bool(isinstance(rec, dict) and rec.get("low"))


def _kickstart(label):
    if os.environ.get("AGENT_OPS_NO_LAUNCHCTL") == "1":
        return False
    try:
        r = subprocess.run([os.environ.get("CAPSULE_LAUNCHCTL", "launchctl"), "kickstart",
                            f"gui/{os.getuid()}/{label}"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _relief_label():
    found = sorted(tick_watch._agents().glob("*.capsule-corp-disk-relief.plist"))
    return found[0].name[:-len(".plist")] if found else None


def _disk_step(conn, cfg, now, probes=None):
    p, st = probes or {}, config.stall(cfg)
    free = (p.get("disk_free") or (lambda: shutil.disk_usage(Path.home()).free))() / GB
    rec = guard_get(conn, DISK)[0] or {}
    board = cfg.get("board") or "board"
    if free >= st["disk_floor_gb"]:
        if rec:
            _lift(conn, "disk", now)
            guard_set(conn, DISK, None, now=now)
            _act(conn, "host_disk", board, "resume", now, result=f"{free:.1f} GB free")
            outbox.add(conn, "local", f"{board}: disk is back to {free:.0f} GB. Work resumes.",
                       now=now)
        return
    rec = {"low": True, "since": rec.get("since", now), "kicked_at": rec.get("kicked_at", 0),
           "paused": rec.get("paused", False), "free_gb": round(free, 1)}
    if now - rec["kicked_at"] >= KICK_S:
        label = _relief_label()
        done = (p.get("kickstart") or _kickstart)(label) if label else False
        rec["kicked_at"] = now
        _act(conn, "host_disk", board, "kickstart", now, result="started" if done else "skipped")
    if free < st["disk_pause_gb"] or rec["paused"]:
        mine = _pause(conn, now + PAUSE_TTL_S, "disk", now)
        if mine and not rec["paused"]:
            _act(conn, "host_disk", board, "pause", now, result=f"{free:.1f} GB free")
            outbox.add(conn, "local", f"{board}: disk is almost full ({free:.0f} GB). Work paused.",
                       now=now)
        rec["paused"] = mine
    guard_set(conn, DISK, rec, now=now)


# -- failure storm ----------------------------------------------------------------------------

FAILS_Q = ("SELECT task_id, ended_at FROM task_runs"  # noqa: S608
           " WHERE ended_at>? AND started_at>=? AND "
           + FAILED + " ORDER BY ended_at DESC, id DESC LIMIT ?")
LAST_Q = ("SELECT task_id, ended_at, " + FAILED + " AS bad FROM task_runs"  # noqa: S608
          " WHERE ended_at>? AND started_at>=? ORDER BY ended_at DESC, id DESC LIMIT ?")
WORKED_Q = ("SELECT 1 FROM task_runs WHERE ended_at>? AND outcome IN"
            " ('completed','checkpoint','ruling')")


def _boot_time():
    """Epoch seconds of the last Mac boot, or 0 when unknown (then every run counts)."""
    try:
        out = subprocess.run(["sysctl", "-n", "kern.boottime"], capture_output=True, text=True,
                             check=False, timeout=10).stdout
        return int(out.split("sec =")[1].split(",")[0])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return 0


def _boot(probes):
    """`probes["boot_time"]` (a test) or the real boot time. A run that began before the boot
    died with the Mac (a dead pid, no run.json), so it is no storm evidence."""
    p = probes or {}
    return p["boot_time"] if "boot_time" in p else _boot_time()


def storm(conn, now, boot=0):
    """The storm now, or None: `{"since", "evidence", "probe"}`. A failed probe counts alone.
    Runs that started before `boot` (epoch s, 0 = unknown) do not count."""
    rec = guard_get(conn, STORM)[0] or {}
    probe = bool(rec.get("level")) and now >= rec.get("until", 0)
    if probe:
        got = conn.execute(FAILS_Q, (rec["until"], boot, 1)).fetchall()
        return {"since": got[0][1], "evidence": ["the probe card failed"], "probe": True} \
            if got else None
    last = conn.execute(LAST_Q, (rec.get("mark", 0), boot, RUNS)).fetchall()
    cards = {r["task_id"] for r in last}
    if (len(last) < RUNS or not all(r["bad"] for r in last) or len(cards) < CARDS
            or last[0]["ended_at"] - last[-1]["ended_at"] > WINDOW_S
            or now - last[0]["ended_at"] > WINDOW_S):
        return None
    return {"since": last[-1]["ended_at"], "probe": False,
            "evidence": [f"{RUNS} runs in a row failed on {len(cards)} cards"]}


def gate(conn, rows, now):
    """`dispatch.ready`'s last step: the ready cards that may start. Disk under the floor: no
    strong-tier (container) card. After a storm pause: one card, none while a run is open."""
    if _disk_low(conn):
        rows = [r for r in rows if (r["tier"] or "strong") != "strong"]
    rec = guard_get(conn, STORM)[0]
    rec = rec if isinstance(rec, dict) else {}      # bad data is no storm
    if rows and rec.get("level") and now >= rec.get("until", 0):
        busy = conn.execute("SELECT 1 FROM task_runs WHERE ended_at IS NULL").fetchone()
        rows = [] if busy else rows[:1]
    return rows


def _detect(conn, cfg, now, probes):
    got = storm(conn, now, _boot(probes))
    if got is None:
        return []
    return [{"class": "failure_storm", "subject": cfg.get("board") or "board",
             "since": got["since"], "evidence": got["evidence"], "grace": 0, "l2_after": 0}]


DETECTORS = (_detect,)


def _storm_step(conn, cfg, now, probes=None):
    rec = guard_get(conn, STORM)[0] or {}
    level, board = rec.get("level", 0), cfg.get("board") or "board"
    if level and now >= rec["until"]:
        done = conn.execute(WORKED_Q, (rec["until"],)).fetchone()
        if done or now - rec["until"] >= QUIET_S:
            guard_set(conn, STORM, None, now=now)
            _act(conn, "failure_storm", board, "reset", now, result="probe worked")
            return
    from capsule import dispatch    # dispatch reads this module: a late import breaks the cycle
    got = None if dispatch.any_pause(conn, now) else storm(conn, now, _boot(probes))
    if got is None:
        return
    until = now + STORM_PAUSE_S[min(level, len(STORM_PAUSE_S) - 1)]
    if _pause(conn, until, "failure_storm", now):
        guard_set(conn, STORM, {"level": level + 1, "until": until, "mark": now}, now=now)
        _act(conn, "failure_storm", board, "pause", now, n=level + 1, result=f"until {until}")


def step(conn, cfg, now, probes=None):
    """The tick's one call (after the stalls): disk, then the storm. Each is its own guard."""
    for fn, args in ((_disk_step, (probes,)), (_storm_step, (probes,))):
        try:
            fn(conn, cfg, now, *args)
        except Exception as exc:
            print(f"capsule host guard: {fn.__name__}: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
