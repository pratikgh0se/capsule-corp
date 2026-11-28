"""The landing step behind `lander.mode` (row 600)."""
import argparse
import contextlib
import json
import os
import re
import signal
import subprocess
import sys
import traceback
import time
from pathlib import Path

from capsule import (blocks, boundary, ci_watch, day_branch, db, dispatch, incidents, lander,
                     lander_blind, landing_journal, landing_rows, paths, restart, seats, shards)
from capsule.blind import settle
from capsule.db import event, guard_get, guard_set, transition, txn
from capsule.landing_reverts import (BUSY, BUSY_TTL, CLOSED_RED, CRASHED, CRASH_LIMIT,
                                     FETCHFAIL, HOLD, LANDED, _hold_alarm, _last_completed,
                                     _replayed_floor, _reverted_on_main, _unrecorded_push, _waiting,
                                     completed_payload, fetch_waiting)
from capsule.landing_seen import WINDOW, _ask, _candidates, _flag_reverted, _out, _seen
from capsule.runners import mac

PUSHFAIL, KEEPER_ASKED = "lander.pushfail", "lander.keeperask:"
STALE_ASKED = "lander.staleask:"
WORKER_TTL = 3 * 3600            # a worker alive this long is stuck (worst honest run: ~2 h)
CHECKING = "lander.checking"     # {task, sha, pid, at}: a detached worker is running (G2b)
KIDS = {}                        # pid -> Popen of the workers this process spawned (poll reaps)
STALE_CLAIM_S = 40 * 60      # unblocked.OWNER_HEARTBEAT_MAX_S: an older claim is asked about once
# origin moved; a hook or branch-protection "[remote rejected]" is a failure, not this
REJECTED = re.compile(r"non-fast-forward|fetch first|! \[rejected\]")
CI_WAIT_S = 1800      # lander.ci_wait_s: how long a pending or unreadable CI is waited on
OK = ("landed", "already_on_main", "verified", "reverted", "dry-run", "nothing", "ci_pending",
      "ci_unverified", "reverted_on_main", "verify_held", "closed_red", "verify_refused",
      "lane_wait")


def acquire(conn, task_id, now):
    """Compare-and-set `lander.busy`; a flag older than BUSY_TTL is a dead lander, taken over."""
    held, version = guard_get(conn, BUSY)
    if held and held.get("busy") and now - held["at"] < BUSY_TTL:
        return False
    return guard_set(conn, BUSY, {"busy": True, "task": task_id, "at": int(time.time())},
                     expect_version=version, now=now)


def _keeper_busy(conn, task_id, cfg, home, now):
    """G3: the `busy` outcome while the keeper is landing, else None. Every block prints a line;
    a claim older than 40 minutes also logs one event and asks once (per claim), so a forgotten
    claim cannot stall land mode silently."""
    why = stale = ""
    queue = paths.queue_dir(cfg.get("repo_name", cfg["board"]), home)
    for reason, age, beat in paths.keeper_claims([queue / "owners.json"], queue, now):
        why = reason
        if beat and age > STALE_CLAIM_S:
            why, stale = f"{reason}, heartbeat {age / 3600:.1f}h old", beat
        break
    if not why:
        return None
    print(f"landing: busy, {why}", file=sys.stderr)
    if stale and not guard_get(conn, KEEPER_ASKED + stale)[0]:
        guard_set(conn, KEEPER_ASKED + stale, {"at": now}, now=now)
        with txn(conn):
            event(conn, task_id, "landing", {"outcome": "busy_keeper", "commit": None,
                                             "detail": why[:600]}, now=now)
        _ask(conn, task_id, f"{task_id}: a stale merge-keeper claim blocks landing", why, now)
    return _out(conn, task_id, "busy", now, detail=why)


def _free(conn, task_id, now):
    guard_set(conn, BUSY, {"busy": False, "task": task_id, "at": now}, now=now)


def _marker(conn, cfg, task_id, home, state, result, deploy):
    branch = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
    path = paths.queue_dir(cfg.get("repo_name", cfg["board"]), home) / f"{branch or task_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    suites = "; ".join(f"{r['name']} {r['status']}" for r in result["checks"])
    path.write_text(json.dumps({"state": "landed", "landed_sha": state["sha"], "suites": suites,
                                "commits": state["commits"], "deploy": deploy,
                                "authority": "capsule lander, row 600"}))


def _complete(conn, task_id, sha, result, now):
    gates = {r["name"]: r["status"] for r in result["checks"]}
    with txn(conn):
        transition(conn, task_id, "done", frm=("review", "done"))
        conn.execute("UPDATE tasks SET completed_at=COALESCE(completed_at, ?)"
                     " WHERE id=? AND status='done'", (now, task_id))
        if _last_completed(conn, task_id) != (p := completed_payload(conn, task_id, sha, gates)):
            event(conn, task_id, "completed", p, now=now)   # not twice


NO_DEPLOY = ("deploy.sh is not in this checkout (the public edition does not ship it): "
             "set lander.deploy_check in the board config to your own check")


def _deploy(cfg, release):
    """(drift, output tail) of the configured `deploy --check`, run in the trusted release checkout,
    never in a clone of a landed sha (that ran the candidate's own deploy.sh, row 632 b)."""
    cmd = (cfg.get("lander") or {}).get("deploy_check", lander.DEPLOY)
    if cmd == lander.DEPLOY and not (Path(release) / "deploy.sh").is_file():   # a public clone
        return True, NO_DEPLOY
    rc, tail = lander._sh(cmd, release, 300, os.environ.copy())
    return rc != 0, tail


def _push(conn, task_id, sha, release, now, board, branch="main"):
    """(outcome, detail): pushed | push_rejected (origin moved: re-check) | push_failed (auth or
    network: back off, ask once, incident on the second failure)."""
    rc, out = lander._git(release, "push", "origin", f"{sha}:refs/heads/{branch}")  # no --force
    if rc == 0:
        guard_set(conn, PUSHFAIL, None, now=now)
        return "pushed", ""
    if REJECTED.search(out):
        return "push_rejected", out
    seen, _ = guard_get(conn, PUSHFAIL)
    count = (seen or {}).get("count", 0) + 1
    guard_set(conn, PUSHFAIL, {"count": count, "until": now + min(3600, 300 * 2 ** (count - 1))},
              now=now)
    if count == 1:
        _ask(conn, task_id, f"{task_id}: push to origin failed (auth or network?)", out, now)
    if count == 2:
        incidents.open_incident(conn, board=board, kind="silent_failure", task_id=task_id,
                                key="lander push to origin keeps failing", evidence=[out[-800:]],
                                now=now)
    return "push_failed", out


def _backing_off(conn, cfg, now):
    """True while a failed push's back-off runs (land mode only): the pass skips the suites."""
    seen, _ = guard_get(conn, PUSHFAIL)
    return bool(seen) and (cfg.get("lander") or {}).get("mode") == "land" and now < seen["until"]


_journal = landing_journal.write    # a module-level name, so a test can stand in for it


def _record_landing(conn, task_id, result, cfg, repo, release, home, now, state, base):
    """Everything after the push, shared by a fresh landing and the recovery of a killed one:
    deploy check, marker, completion, restart flag, `lander.landed` state, `landed` journal line
    and the drift hold. `base` is main as it was before the push."""
    sha = state["sha"]
    drift, tail = _deploy(cfg, release)
    _marker(conn, cfg, task_id, home, state,
            result, f"--check: {'DRIFT' if drift else 'ok'}: {tail[-300:]}")
    if not blocks.was_closed(conn, task_id) and _last_completed(conn, task_id).get(
            "commit") in (None, state.get("card"), sha):    # a closed card stays closed
        _complete(conn, task_id, sha, result, now)    # else a newer completion is still to land
    boundary.mark(conn, repo, base, sha, cfg, now)   # C07c
    guard_set(conn, LANDED + task_id, state, now=now)
    _journal(cfg, task_id, home, "landed", sha=sha)
    if drift:
        guard_set(conn, HOLD, {"reason": tail[-300:], "task": task_id}, now=now)
        _ask(conn, task_id, f"{task_id} landed but deploy --check drifts (no apply)", tail, now)
    return "drift" if drift else "ok"


def _already_on_main(conn, task_id, result, cfg, repo, release, home, now):
    """The sha is already an ancestor of origin/main (a crash after the push, or a human pushed
    it): nothing to push. The card's commits come from an earlier `landing_checked` base for this
    sha; with none the card is not completed and the owner is asked, never an empty revert list."""
    sha = result["commit"]
    commits = []
    if sha == result["base"] != result.get("card_commit"):   # an empty replay: it is all on main
        state = {"sha": sha, "commits": [], "state": "verified"}
        _marker(conn, cfg, task_id, home, state, result, "--check: not run (already on main)")
        _complete(conn, task_id, sha, result, now)
        guard_set(conn, LANDED + task_id, state, now=now)
        return _out(conn, task_id, "already_on_main", now, sha, pushed=False)
    for (payload,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND "
                                   "kind='landing_checked' ORDER BY id DESC", (task_id,)):
        seen = json.loads(payload)
        if seen.get("commit") == sha and not commits:
            commits = lander._git(repo, "rev-list", "--reverse",
                                  f"{seen['base']}..{sha}")[1].split()
    if not commits:
        _ask(conn, task_id, f"{task_id}: {sha[:12]} is already on origin/{lander.target(cfg)}",
             "no recorded base to tell which commits are the card's; no landing recorded", now)
        return _out(conn, task_id, "already_on_main", now, sha, "no recorded base", pushed=False)
    state = {"sha": sha, "commits": commits, "state": "pushed"}
    _marker(conn, cfg, task_id, home, state, result,
            f"--check: not run (already on origin/{lander.target(cfg)})")
    _complete(conn, task_id, sha, result, now)
    guard_set(conn, LANDED + task_id, state, now=now)
    return _out(conn, task_id, "already_on_main", now, sha, pushed=False)


def _land(conn, task_id, result, cfg, repo, release, home, now):
    sha = result["commit"]
    rc, out = lander._git(release, "fetch", "--end-of-options", str(repo), sha)   # a local path
    if rc:
        return _out(conn, task_id, "fetch_failed", now, sha, out, pushed=False)
    if sent := _unrecorded_push(cfg, task_id, home, repo, result["base"]):
        state = {"sha": sent["sha"], "commits": sent["commits"], "state": "pushed",
                 "card": sent.get("card")}      # our own push, killed before it was recorded:
        _record_landing(conn, task_id, result, cfg, repo, release, home, now, state, sent["base"])
        return _out(conn, task_id, "already_on_main", now, sent["sha"], "recorded after a kill",
                    pushed=False)               # recorded first, whatever else the card owes
    # the card's own commits, recorded now: a red revert must never touch anyone else's work
    commits = lander._git(repo, "rev-list", "--reverse", f"{result['base']}..{sha}")[1].split()
    if not commits:
        return _already_on_main(conn, task_id, result, cfg, repo, release, home, now)
    card = result.get("card_commit")   # what a later replay of this card starts after
    _journal(cfg, task_id, home, "push", sha=sha, commits=commits, card=card, base=result["base"])
    outcome, detail = _push(conn, task_id, sha, release, now, cfg["board"], lander.target(cfg))
    if outcome != "pushed":
        _journal(cfg, task_id, home, "push_failed", sha=sha, outcome=outcome)   # closes the push
        return _out(conn, task_id, outcome, now, sha, detail, pushed=False)
    state = {"sha": sha, "commits": commits, "state": "pushed", "card": card}
    drift = _record_landing(conn, task_id, result, cfg, repo, release, home, now, state,
                            result["base"])
    return _out(conn, task_id, "landed", now, sha, pushed=True, deploy=drift)


def _fetch_origin(conn, release, task_id, board, now, branch="main"):
    """None when `release`'s origin/<branch> was just fetched, else the outcome word. A failed fetch
    backs the card off as a failed push does (`fetch_failed` logged, one ask, an incident on the
    second failure, a doubling pause up to an hour), never each pass; a good fetch clears it."""
    seen = guard_get(conn, FETCHFAIL + task_id)[0]
    if fetch_waiting(conn, task_id, now):
        return "fetch_backoff"
    rc, text = day_branch.fetch(release, branch)
    if rc == 0:
        if seen:
            guard_set(conn, FETCHFAIL + task_id, None, now=now)
        return None
    count = (seen or {}).get("count", 0) + 1
    guard_set(conn, FETCHFAIL + task_id,
              {"count": count, "until": now + min(3600, 300 * 2 ** (count - 1))}, now=now)
    if count == 1:
        _ask(conn, task_id, f"{task_id}: fetching origin failed, a closed card's push waits",
             text, now)
    if count == 2:
        incidents.open_incident(conn, board=board, kind="silent_failure", task_id=task_id,
                                key="lander fetch of origin keeps failing for a closed card",
                                evidence=[text[-800:]], now=now)
    return _out(conn, task_id, "fetch_failed", now, detail=text)["outcome"]


def _record_closed(conn, task_id, cfg, release, home):
    """An owed push (killed after the push) of a closed card, or of an open one whose check came
    out red: recorded from its journal line with no suites, so the after-push watch judges the
    pushed sha (a red one asks a person, never reverts). The card is never moved, blocked or
    landed here. A fresh fetch first: the push must be on origin/main as it is now."""
    now = int(time.time())
    if word := (_fetch_origin(conn, release, task_id, cfg["board"], now, lander.target(cfg))
                or _prerun(conn, task_id, cfg, release, home)):
        return word
    if not acquire(conn, task_id, now):
        return "busy"
    try:
        if held := _keeper_busy(conn, task_id, cfg, home, now):     # as `_land_checked` does
            return held["outcome"]
        sent = _unrecorded_push(cfg, task_id, home, release)
        if sent is None:
            return "nothing"
        state = {"sha": sent["sha"], "commits": sent["commits"], "state": "pushed",
                 "card": sent.get("card")}
        result = {"checks": [{"name": "suites", "status": "not run (owed push)"}]}
        if (guard_get(conn, LANDED + task_id)[0] or {}).get("sha") == sent["sha"]:
            _journal(cfg, task_id, home, "landed", sha=sent["sha"])   # recorded, killed before
        else:                                                         # its journal line
            _record_landing(conn, task_id, result, cfg, cfg["repo_url"], release, home, now,
                            state, sent["base"])
        return _out(conn, task_id, "already_on_main", now, sent["sha"], "owed push: recorded",
                    pushed=False)["outcome"]
    finally:
        _free(conn, task_id, now)


def _land_checked(conn, task_id, result, *, cfg, repo, release, home=None, now=None):
    """The outcome dict: outcome (dry-run | not_checked | held_deploy_drift | no_seat | busy |
    fetch_failed | push_rejected | push_failed | landed), detail, and `pushed` on a push."""
    now = int(now if now is not None else time.time())
    if (cfg.get("lander") or {}).get("mode", "dry-run") != "land":
        return _out(conn, task_id, "dry-run", now)
    if result["verdict"] != "checked":
        return _out(conn, task_id, "not_checked", now, detail=result["verdict"])
    hold, _ = guard_get(conn, HOLD)
    if hold:
        drift, tail = _deploy(cfg, release)
        if drift:
            _hold_alarm(conn, cfg["board"], now)
            return _out(conn, task_id, "held_deploy_drift", now, detail=tail[-300:])
        guard_set(conn, HOLD, None, now=now)      # the drift is gone: the hold clears itself
    if not acquire(conn, task_id, now):
        return _out(conn, task_id, "busy", now, detail="lander.busy is held")
    try:
        if settle.red_waiting(conn, cfg, task_id):    # a round finished while the suites ran
            return _out(conn, task_id, "busy", now, detail="red check waiting")
        if held := _keeper_busy(conn, task_id, cfg, home, now):   # G3, checked after the CAS
            return held
        got = seats.counts(conn, home, cfg["board"], now)   # our own flag is one of each count
        if got["landers"] > 1:
            return _out(conn, task_id, "busy", now, detail="another board is landing")
        if got["heavy"] > seats.heavy_cap(cfg):
            return _out(conn, task_id, "no_seat", now)
        return _land(conn, task_id, result, cfg, repo, release, home, now)
    finally:
        _free(conn, task_id, now)


def _suites_red(cfg, release, sha, timeout, home=None, mac_card=False):
    """(status, detail) of the suites on the landed sha, in the sandbox (a mac card's: on the host
    under the Mac lane profile); `wait`: unavailable."""
    cmds = shards.expand(cfg)
    span = timeout * len(cmds)
    with lander._clone(release, sha, True) as work, mac.checks(work and work.path, cfg,
                                                                        span, home=home,
                                                                        mac_card=mac_card) as box:
        rows = []
        status, detail = lander._commands(cmds, work, timeout, box=box, rows=rows)
        if status == "wait" and getattr(box, "lane", None):
            detail = box.lane                  # a `mac.LaneWait`: the lane could not run them
        bad = (cfg.get("contract_shards") and status == "pass"
               and shards.failures(cmds, rows))    # a part's count line
        return ("fail", "; ".join(bad)[:lander.DETAIL]) if bad else (status, detail)


def _verify_held(conn, task_id, cfg, state, now, detail):
    """Sandbox unavailable after the push: stay `pushed`, retry, never revert; ask once."""
    since = state.setdefault("held_since", now)
    lane = dispatch.lane_wait(conn, task_id, detail, now)   # a Mac lane wait: no ask, no incident
    if not lane and now - since >= (cfg.get("lander") or {}).get("ci_wait_s", CI_WAIT_S) \
            and not state.get("held_asked"):
        _ask(conn, task_id, f"{task_id}: after-push suites cannot run for {now - since}s",
             detail, now)
        incidents.open_incident(conn, board=cfg["board"], kind="review_infra", task_id=task_id,
                                key="lander after-push suites cannot run: sandbox unavailable",
                                evidence=[detail[-800:]], now=now)
        state["held_asked"] = True
    guard_set(conn, LANDED + task_id, state, now=now)
    return _out(conn, task_id, "verify_held", now, state["sha"], detail)


def _verify_refused(conn, task_id, cfg, state, now, exc):
    """The mount audit refused the after-push suites: an incident and one ask, no respawn."""
    detail = lander_blind.audit_hold(conn, cfg, task_id, exc)[1]
    guard_set(conn, LANDED + task_id, {**state, "state": "verify_refused"}, now=now)
    _ask(conn, task_id, f"{task_id}: after-push suites refused by the mount audit",
         lander_blind.VERIFY_ASK.format(sha=state["sha"][:12], card=task_id), now)
    return _out(conn, task_id, "verify_refused", now, state["sha"], detail)


def _revert(conn, task_id, release, state, now, branch="main"):
    """Revert the card's recorded commits in a throwaway clone of origin's CURRENT main and push
    from there; the release checkout is read (fetch) but never committed into."""
    sha = state["sha"]
    rc, out = lander._git(release, "fetch", "origin", branch, timeout=180)
    tip = lander._git(release, "rev-parse", "FETCH_HEAD")[1]
    url = lander._git(release, "remote", "get-url", "origin")[1]
    with lander._clone(release, tip, rc == 0) as work:
        if work is None:
            return _out(conn, task_id, "revert_push_rejected", now, sha, out or "no clone")
        rc, out = lander._git(work.path, *lander.IDENT, "revert", "--no-edit",
                              *reversed(state["commits"]))
        if rc:
            guard_set(conn, LANDED + task_id, {**state, "state": "revert_failed"}, now=now)
            _ask(conn, task_id, f"{task_id}: revert of {sha[:12]} failed", out, now)
            return _out(conn, task_id, "revert_failed", now, sha, out)
        rc, out = lander._git(work.path, "push", url, f"HEAD:refs/heads/{branch}")  # no --force
    if rc:
        return _out(conn, task_id, "revert_push_rejected", now, sha, out)
    guard_set(conn, LANDED + task_id, {**state, "state": "reverted"}, now=now)
    return _out(conn, task_id, "reverted", now, sha)


def _ci_gate(conn, task_id, cfg, release, state, now):
    """(red detail, waiting outcome, ci state) from GitHub CI for the pushed sha, only when
    `lander.ci_watch` is on (missing means off: "off", as before). The repo is the release
    checkout's `origin`. Pending or unreadable CI waits (`ci_pending`, retried next pass without
    re-running suites) until `lander.ci_wait_s`, then asks once (`ci_unverified`) and closes the
    watch: never a revert, never an incident. A red sha whose parent (main before the push) is
    red too is pre-existing red: the watch closes, no incident, no revert; a pending or unreadable
    parent waits the same way."""
    lc, sha, tgt = cfg.get("lander") or {}, state["sha"], lander.target(cfg)
    if not lc.get("ci_watch"):
        return "", None, "off"
    rc, url = lander._git(release, "remote", "get-url", "origin")
    url = url if rc == 0 else ""
    got = ci_watch.conclusion(url, sha, branch=tgt)
    if got == "red":      # red on the sha is the card's bug only if main was green before the push
        before = (state.get("commits") or [sha])[0] + "^1"     # the card's base: main's old tip
        rc, parent = lander._git(release, "rev-parse", before)
        then = ci_watch.conclusion(url, parent, branch=tgt) if rc == 0 else "unknown"
        if then == "red":
            return "", None, "pre-existing red"
        got = "red" if then == "green" else f"parent {then}"
    if got in ("green", "red"):
        return ("GitHub CI red on " + sha[:12] if got == "red" else ""), None, got
    since = state.get("ci_since", now)
    if now - since < lc.get("ci_wait_s", CI_WAIT_S):
        guard_set(conn, LANDED + task_id, {**state, "ci_since": since}, now=now)
        return "", _out(conn, task_id, "ci_pending", now, sha, f"GitHub CI {got}"), got
    guard_set(conn, LANDED + task_id, {**state, "state": "ci_unverified"}, now=now)
    _ask(conn, task_id, f"{task_id}: GitHub CI for {sha[:12]} is still {got} after {now - since}s",
         "no revert was made; check the CI run and the gh login", now)
    return "", _out(conn, task_id, "ci_unverified", now, sha, f"GitHub CI {got}"), got


def verify(conn, task_id, *, cfg, release, now=None, red=None, timeout=lander.TIMEOUT, home=None):
    """The red-after-push rule for a card this lander pushed. `red` is an outside signal (CI);
    without it the board's suites run in a throwaway clone of the landed commit. Green closes the
    watch; red opens the incident once and owes a revert (`revert_owed`), retried each pass."""
    now = int(now if now is not None else time.time())
    state, _ = guard_get(conn, LANDED + task_id)
    if state and state["state"] == "verify_refused":   # only a person's verify gets here: re-arm
        state = {**state, "state": "pushed"}
    if not state or state["state"] not in ("pushed", "revert_owed"):
        return _out(conn, task_id, "nothing", now)
    if state["state"] == "pushed":
        why = red or ""
        if not why and not state.get("ci_since"):
            try:
                status, detail = _suites_red(cfg, release, state["sha"], timeout, home,
                                            mac.card_is_mac(conn, task_id))
            except RuntimeError as exc:         # the mount audit refused the container
                return _verify_refused(conn, task_id, cfg, state, now, exc)
            if status == "wait":    # retried next pass, like a pending CI
                return _verify_held(conn, task_id, cfg, state, now, detail)
            state = {k: v for k, v in state.items() if k not in ("held_since", "held_asked")}
            why = "" if status == "pass" else detail
        ci = "skipped"
        if not why:       # local suites are green (or already were): GitHub CI has a say too
            why, waiting, ci = _ci_gate(conn, task_id, cfg, release, state, now)
            if waiting:
                return waiting
        if not why:
            guard_set(conn, LANDED + task_id, {**state, "state": "verified"}, now=now)
            return _out(conn, task_id, "verified", now, state["sha"], f"ci: {ci}")
        incidents.open_incident(conn, board=cfg["board"], kind="escaped_bug", task_id=task_id,
                                key=f"landed {state['sha'][:12]} was red after push",
                                evidence=[why[-800:]], main_sha=state["sha"], now=now)
        if blocks.was_closed(conn, task_id):    # a person's card or push: ask, never revert
            title = conn.execute("SELECT title FROM tasks WHERE id=?", (task_id,)).fetchone()[0]
            _ask(conn, task_id, f'Closed card "{title[:60]}": its commit {state["sha"][:8]} on '
                 "main failed the after-push tests", CLOSED_RED, now)     # before the guard write:
            guard_set(conn, LANDED + task_id, {**state, "state": "closed_red"}, now=now)   # a kill
            return _out(conn, task_id, "closed_red", now, state["sha"], why)
        state = {**state, "state": "revert_owed"}
        guard_set(conn, LANDED + task_id, state, now=now)
    if not acquire(conn, task_id, now):
        return _out(conn, task_id, "busy", now, detail="lander.busy is held")
    try:       # G3: a revert pushes too
        return _keeper_busy(conn, task_id, cfg, home, now) or _revert(
            conn, task_id, release, state, now, lander.target(cfg))
    finally:
        _free(conn, task_id, now)


def _check(conn, cfg, home, now, release, task_id, branch, sha):
    """lander.run, then `_land_checked`; dry-run only logs. A push of ours is recorded first."""
    repo, tgt = cfg["repo_url"], lander.target(cfg)
    if cfg["lander"]["mode"] == "land" and (word := _prerun(conn, task_id, cfg, release, home)):
        return word                   # busy, no seat, hold or keeper: no suites this pass
    result = lander.run(repo, branch, cfg=cfg, commit=sha, task_id=task_id, conn=conn,
                        after=lambda: _replayed_floor(conn, repo, task_id, sha, tgt), home=home)
    now = int(time.time())            # the suites took minutes: stamp what follows with the clock
    if result["verdict"] != "checked" and landing_journal.unrecorded(cfg, task_id, home, release):
        return _record_closed(conn, task_id, cfg, release, home)
    reverted = _reverted_on_main(conn, repo, task_id, sha, tgt)   # run() fetched: may be new
    if reverted is None and result["verdict"] == "checked":   # git could not say: land nothing
        return _out(conn, task_id, "revert_check_failed", now, sha, f"git log origin/{tgt} failed",
                    pushed=False)["outcome"]
    if reverted:
        flagged = _flag_reverted(conn, task_id, sha, now, tgt)
        landing_journal.abandon(cfg, task_id, home, release)   # our pending push is closed too
        return flagged
    if _lane_waited(conn, task_id, result, now):
        return "lane_wait"            # a mac card the lane could not run: nothing recorded
    if cfg["lander"]["mode"] == "land":
        lander.record(conn, task_id, result, limit=restart.failure_cap(cfg), now=now,
                      board=cfg["board"])
    else:                             # dry-run: never moved, only the event
        with txn(conn):
            event(conn, task_id, "landing_checked",
                  {k: result.get(k) for k in ("verdict", "hold", "commit", "card_commit", "base",
                                              "checks")}, now=now)
    return _land_checked(conn, task_id, result, cfg=cfg, repo=repo, release=release, home=home,
                         now=now)["outcome"]


def _lane_waited(conn, task_id, result, now):
    """True when the check waited because the Mac lane could not run a mac card (D305): nothing is
    recorded (no `landing_checked`, no block, no failure), the card stays `done` and is checked
    again on a later pass; a busy lane is silent, any other reason is the one `mac_lane_held`."""
    lane = result.get("lane")
    return result["verdict"] == "wait" and dispatch.lane_wait(conn, task_id, lane, now)


def _prerun(conn, task_id, cfg, release, home):
    """The outcome word when a landing could not proceed (a deploy hold that still drifts, busy,
    no seat, the keeper), checked BEFORE `lander.run` so held cards do not re-run the suites."""
    now = int(time.time())
    if guard_get(conn, HOLD)[0]:
        drift, _ = _deploy(cfg, release)
        if drift:
            _hold_alarm(conn, cfg["board"], now)
            return "held_deploy_drift"
        guard_set(conn, HOLD, None, now=now)
    held = _keeper_busy(conn, task_id, cfg, home, now)
    return _waiting(conn, cfg, home, now) or (held and held["outcome"])


def _alive(pid, task_id):
    """A worker this process spawned is alive until `poll` says it ended; any other pid only when
    it is a `capsule.landing` process for this task (a reused pid is not our lander)."""
    if not pid:
        return False
    if pid in KIDS:
        if KIDS[pid].poll() is None:
            return True
        del KIDS[pid]
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    cmd = subprocess.run(["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True)
    return "capsule.landing" in cmd.stdout and task_id in cmd.stdout


def _stop(held):
    """Stop a stuck worker's session: SIGTERM, then SIGKILL after a wait. True when it is gone."""
    print(f"landing: worker {held['pid']} for {held['task']} passed {WORKER_TTL}s, killed",
          file=sys.stderr)
    for sig in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(OSError):
            os.killpg(held["pid"], sig)
        for _ in range(20):
            if not _alive(held["pid"], held["task"]):
                return True
            time.sleep(0.1)
    return False


def _spawn(conn, cfg, home, now, release, action, task_id, sha=None):
    """`python3 -m capsule.landing <action>` detached, as `boundary._fire` does, under
    the `lander.checking` guard."""
    logs = paths.home(home) / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    board, conf = cfg["board"], logs / f"landing-{cfg['board']}.json"
    conf.write_text(json.dumps(cfg))
    argv = [sys.executable, "-m", "capsule.landing", action, "--db",
            conn.execute("PRAGMA database_list").fetchone()[2], "--task", task_id, "--repo",
            cfg["repo_url"], "--release", str(release), "--config", str(conf), "--home",
            str(paths.home(home))] + (["--commit", sha] if sha else [])
    guard_set(conn, CHECKING, {"task": task_id, "sha": sha, "pid": 0, "at": now}, now=now)
    with open(logs / f"landing-{board}.log", "ab") as out:
        kid = subprocess.Popen(argv, cwd=str(paths.REPO), stdout=out, stderr=out,
                               start_new_session=True)
    KIDS[kid.pid] = kid
    held, version = guard_get(conn, CHECKING)
    if held:                          # else the worker already finished and cleared it
        guard_set(conn, CHECKING, {**held, "pid": kid.pid}, expect_version=version, now=now)
    return "running"


def _crashed(conn, cfg, held, now):
    """A guard dropped with a dead worker behind it is a crash (a worker that ends well
    clears it). Dry-run and a closed card are never moved."""
    task_id, sha = held["task"], held.get("sha")
    seen, _ = guard_get(conn, CRASHED + task_id)
    count = (seen["count"] if seen and seen["sha"] == sha else 0) + 1
    guard_set(conn, CRASHED + task_id, {"sha": sha, "count": count}, now=now)
    if count != CRASH_LIMIT:
        return False
    last = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='landing' "
                        "ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
    seen = json.loads(last[0]) if last else {}
    note = seen.get("detail", "")[-300:] if (seen.get("outcome"), seen.get("commit")) == (
        "worker_error", sha) else "no worker_error event (killed?)"
    why = f"the lander worker crashed {count} times: {note}"
    incidents.open_incident(conn, board=cfg["board"], kind="silent_failure", task_id=task_id,
                            key="lander worker keeps crashing", evidence=[why], now=now)
    if sha is None or cfg["lander"]["mode"] != "land" or blocks.was_closed(conn, task_id):
        _ask(conn, task_id, f"{task_id}: the lander worker keeps crashing", why, now)
        return False
    guard_set(conn, CRASHED + task_id, None, now=now)
    with txn(conn):
        transition(conn, task_id, "ready", frm=("done",))   # `block` takes review, ready
        blocks.block(conn, task_id, kind="needs_input", reason=why[:2000], now=now,
                     offer="unblock|reset")
    return True


def _stale_notice(conn, cfg, home, release, now):
    """"stale_push" after one notice per push of a done, unclosed card past the window."""
    spool = paths.home(home) / "landing" / cfg["board"]
    for entry in sorted(spool.iterdir()) if spool.is_dir() else ():   # one folder per pushed card
        task_id, key = entry.name, STALE_ASKED + entry.name
        pending = conn.execute(landing_rows.STALE_DONE, (task_id, now - WINDOW)).fetchone() \
            and not blocks.was_closed(conn, task_id) \
            and landing_journal.unrecorded(cfg, task_id, home, release)
        if pending and (guard_get(conn, key)[0] or {}).get("sha") != pending["sha"]:
            with txn(conn):           # the notice and its once-only mark stand or fall together
                guard_set(conn, key, {"sha": pending["sha"]}, now=now)
                _ask(conn, task_id, f"{task_id}: a push of this lander reached main, unrecorded",
                     f"{pending['sha'][:12]} is on origin/{lander.target(cfg)} but the card is "
                     "past the landing window: nobody watches it", now)
            return "stale_push"


def _step(conn, cfg, home, now, release):
    mode = (cfg.get("lander") or {}).get("mode")
    if mode not in ("dry-run", "land"):
        return "off"                  # no lander section (or a typo): never lands, never checks
    for pid, kid in list(KIDS.items()):
        if kid.poll() is not None:    # reap workers that finished
            del KIDS[pid]
    held, _ = guard_get(conn, CHECKING)
    if held:
        alive = _alive(held["pid"], held["task"])
        if alive and now - held["at"] < WORKER_TTL:
            return "running"          # one worker at a time
        if alive and not _stop(held):  # stuck: stop it (its own session) BEFORE dropping the guard
            return "running"          # still alive: keep the guard
        guard_set(conn, CHECKING, None, now=now)
        if _crashed(conn, cfg, held, now):
            return "crashed"
    if mode == "land":
        owed = conn.execute("SELECT key FROM guard_state WHERE key LIKE ? ORDER BY updated_at",
                            (LANDED + "%",)).fetchall()
        for (key,) in owed:           # one verify per pass: it runs the board's suites
            state, _ = guard_get(conn, key)
            if state and state["state"] in ("pushed", "revert_owed"):
                return _spawn(conn, cfg, home, now, release, "verify", key[len(LANDED):])
        if _backing_off(conn, cfg, now):
            return "push_backoff"
    # one card per pass
    for task_id, _branch, sha in _candidates(conn, mode, now, cfg, home, release):
        if mode == "land" and (word := _waiting(conn, cfg, home, now)):
            return word
        return _spawn(conn, cfg, home, now, release, "land", task_id, sha)
    return _stale_notice(conn, cfg, home, release, now) or "idle"


def step(conn, cfg, home, now, *, release=paths.REPO):
    """The tick's landing pass (C07d, G2b): spawn a detached worker to verify one pushed
    or revert-owed landing, else to check (and in land mode land) one approved card;
    no suite ever runs in the pass."""
    try:
        word = _step(conn, cfg, home, now, release)
    except Exception as exc:
        print(f"capsule tick: landing step failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        word = "error"
    try:
        _note(conn, word, now)
    except Exception as exc:
        print(f"capsule tick: lander.last failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return word


def _note(conn, word, now):
    """`lander.last` `{word, since, at}`: the pass's word and how long it stayed the same (the stall
    supervisor reads it). A claim held by a card that waited 3 times is released, so the next
    approved card can land; the stuck card is rechecked later, never forced through."""
    prev = guard_get(conn, "lander.last")[0] or {}
    if prev.get("word") != word or now - prev.get("at", 0) >= 60:
        since = prev["since"] if prev.get("word") == word else now
        guard_set(conn, "lander.last", {"word": word, "at": now, "since": since}, now=now)
    busy = guard_get(conn, BUSY)[0]
    if not (busy and busy.get("busy")):
        return
    working = guard_get(conn, CHECKING)[0]       # a live landing worker still holds the flag
    if working and (not working["pid"] or _alive(working["pid"], working["task"])):
        return
    waits = conn.execute(     # since the last landing attempt only
        "SELECT COUNT(*) FROM task_events WHERE task_id=? AND (kind='landing_checked' AND"
        " json_extract(payload, '$.verdict')='wait' OR kind='stall_action' AND"
        " json_extract(payload, '$.action')='recheck') AND id>(SELECT COALESCE(MAX(id), 0) FROM"
        " task_events WHERE task_id=? AND kind='landing')", (busy.get("task"),) * 2).fetchone()[0]
    if waits >= lander.WAITS_FOR_INCIDENT:
        _free(conn, busy.get("task"), now)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Land a checked card (lander.mode=land) or verify it.")
    ap.add_argument("action", choices=("land", "verify"))
    for name in ("db", "task", "repo", "release", "config"):
        ap.add_argument(f"--{name}", required=True)
    ap.add_argument("--home")
    ap.add_argument("--no-fetch", action="store_true")
    ap.add_argument("--commit", help="the tick's worker: check exactly this sha, as the tick did")
    a = ap.parse_args(argv)
    cfg, conn = json.loads(Path(a.config).read_text()), db.connect(a.db)
    home, now = (Path(a.home) if a.home else None), int(time.time())
    held, version = guard_get(conn, CHECKING)
    if held and held["pid"] == 0 and held["task"] == a.task:     # claim the tick's pid-0 guard
        guard_set(conn, CHECKING, {**held, "pid": os.getpid()}, expect_version=version)
    try:
        got = _work(conn, cfg, a, home, now)
    except Exception:      # leave the guard: the tick sees the dead pid and counts the crash
        got = _out(conn, a.task, "worker_error", now, a.commit, traceback.format_exc()[-600:])
    else:
        held = guard_get(conn, CHECKING)[0]
        if held and held["task"] == a.task and held["pid"] in (0, os.getpid()):
            guard_set(conn, CHECKING, None)           # the worker's own guard ends with it
        if guard_get(conn, CRASHED + a.task)[0]:
            guard_set(conn, CRASHED + a.task, None)   # a clean exit ends the crash count
    conn.close()
    print(json.dumps(got, indent=1))
    return 0 if got["outcome"] in OK else 1


def _work(conn, cfg, a, home, now):
    if a.action == "verify":
        return verify(conn, a.task, cfg=cfg, release=a.release, home=home)
    if (cfg.get("lander") or {}).get("mode") == "land" and blocks.was_closed(conn, a.task):
        return {"outcome": _record_closed(conn, a.task, cfg, a.release, home), "detail": ""}
    if _backing_off(conn, cfg, now):
        return _out(conn, a.task, "push_backoff", now)
    branch = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (a.task,)).fetchone()[0]
    if a.commit:
        return {"outcome": _check(conn, cfg, home, now, a.release, a.task, branch, a.commit),
                "detail": ""}
    # no --commit: the check runs on the branch tip, not on a completion's commit
    result = lander.run(a.repo, branch, cfg=cfg, fetch=not a.no_fetch, task_id=a.task, home=home,
                        conn=conn, after=lambda: _replayed_floor(
                            conn, a.repo, a.task, "refs/heads/" + branch, lander.target(cfg)))
    if _lane_waited(conn, a.task, result, now):
        return {"outcome": "lane_wait", "detail": ""}
    if not blocks.was_closed(conn, a.task):       # a closed card is never moved
        lander.record(conn, a.task, result, limit=restart.failure_cap(cfg), board=cfg["board"])
    return _land_checked(conn, a.task, result, cfg=cfg, repo=a.repo, release=a.release, home=home)


if __name__ == "__main__":
    sys.exit(main())
