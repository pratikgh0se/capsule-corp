"""Which done cards the lander still has to check, and the notices it sends about them (split out
of landing.py, which was at its size cap; every name here is re-exported from `capsule.landing`)."""
import json
import time

from capsule import blocks, config, dispatch, lander, landing_journal, landing_rows, outbox
from capsule import slack_msgs
from capsule.blind import settle
from capsule.db import event, guard_get, guard_set, txn
from capsule.diff_ceiling import _held
from capsule.landing_reverts import (LANDED, _crash_limited, _last_completed, _reverted_on_main,
                                     _unrecorded_push, fetch_waiting)
from capsule.runners import mac

LOGGED = ("landed", "already_on_main", "push_rejected", "push_failed", "fetch_failed", "verified",
          "reverted", "revert_push_rejected", "revert_failed", "worker_error", "ci_unverified",
          "reverted_on_main", "revert_check_failed", "closed_red")
WINDOW = 24 * 3600    # the tick only checks cards completed this recently (older ones: the keeper)
REVERT_ASKED = "lander.revertask:"   # per card: the sha a human was told was reverted on main


def _out(conn, task_id, outcome, now, commit=None, detail="", **extra):
    if outcome in LOGGED:
        with txn(conn):
            event(conn, task_id, "landing", {"outcome": outcome, "commit": commit,
                                             "detail": detail[:600]}, now=now)
    return {"outcome": outcome, "detail": detail, **extra}


def _ask(conn, task_id, headline, detail, now):
    outbox.notify(conn, "needs_input", headline, fields=[("detail", detail[-400:])],
                  thread_key=task_id, file_body=f"capsule: {headline}: {detail[-300:]}",
                  key=slack_msgs.needs_key(task_id), now=now)


def _approved(gates):
    """True when every review role's gate word is `pass` (`rounds`, `overrides`: not roles)."""
    words = [v for k, v in gates.items() if k not in ("rounds", "overrides")]
    return bool(words) and all(w == "pass" for w in words)


def _revert_flagged(conn, repo, task_id, sha, tgt="main"):
    """True when a human reverted this completion on main (asked once, left alone)."""
    if hit := _reverted_on_main(conn, repo, task_id, sha, tgt):
        _flag_reverted(conn, task_id, sha, tgt=tgt)
    return bool(hit)


def _seen(conn, mode, task_id, sha, repo, branch, tgt="main"):
    """True when this completion needs no further check: it is the one the card's landing answered
    (`sha`, the latest `completed` commit, is the landed sha or the card commit it replayed; a
    rework, a second one or an amended commit is a new completion), or (dry-run) checked once, or
    (land) found already on origin/main. A check red at `sync` counts until the host has it."""
    if landed := guard_get(conn, LANDED + task_id)[0]:
        # Deliberate (R12): the guard answers ONE completion, in any state. The same commit in a
        # red, reverted or refused state is held for a human, not retried; a rework is a new sha
        # and so is not seen (test_landing_seen_guard).
        return sha in (landed.get("sha"), landed.get("card"))
    if _crash_limited(conn, task_id, sha):
        return True
    dry = mode == "dry-run"
    kind, outcome = ("landing_checked", None) if dry else ("landing", "already_on_main")
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                        (task_id, kind))
    if any(sha in ((d := json.loads(p)).get("commit"), d.get("card_commit"))
           and d.get("outcome") == outcome
           and not _sync_fail_stale(d, repo, branch, sha) for (p,) in rows):
        return True
    return _revert_flagged(conn, repo, task_id, sha, tgt)   # git last: the DB checks are cheaper


def _flag_reverted(conn, task_id, sha, now=None, tgt="main"):
    """Tell a human, once per sha, that a completion's change was reverted on main (an ask and an
    event); the card stays `done` and the lander leaves it alone. The guard is written last, so a
    crash re-asks rather than loses the ask."""
    now = int(now if now is not None else time.time())
    seen, _ = guard_get(conn, REVERT_ASKED + task_id)
    if not seen or seen["sha"] != sha:
        why = (f"origin/{tgt} holds a revert of {task_id}'s change, so the lander will not land it "
               "again. Reapply the change by hand, or send the card back for a fix (a new "
               "completion is checked again).")
        _ask(conn, task_id, f"{task_id}: reverted on main", why, now)
        _out(conn, task_id, "reverted_on_main", now, sha, why, pushed=False)
        guard_set(conn, REVERT_ASKED + task_id, {"sha": sha}, now=now)
    return "reverted_on_main"


def _sync_fail_stale(doc, repo, branch, sha):
    """True for a `landing_checked` that failed at `sync` while `sha` is now on the host branch:
    that check is stale. A commit still off the branch stays seen, so it blocks no younger card."""
    return any(c.get("name") == "sync" and c.get("status") == "fail"
               for c in doc.get("checks") or ()) and lander._git(
        repo, "merge-base", "--is-ancestor", sha, f"refs/heads/{branch}")[0] == 0


def _owed_push(conn, cfg, home, release, task_id):
    """The commit to check for a push of this lander that reached origin/main without being
    recorded, whatever the landed guard says: the card's latest `completed` commit (the landing's
    own completion, the pushed sha, stands for the card commit it replayed). None when there is no
    such push, the card keeps crashing, or a human reverted it (closed as `push_abandoned`)."""
    owed = _unrecorded_push(cfg, task_id, home, release)
    sha = _last_completed(conn, task_id).get("commit") if owed else None
    if owed and sha == owed["sha"]:
        sha = owed.get("card") or sha
    if not sha or _crash_limited(conn, task_id, sha):
        return None
    if _revert_flagged(conn, cfg["repo_url"], task_id, sha, lander.target(cfg)):
        landing_journal.abandon(cfg, task_id, home, release)
        return None
    return sha


def _lane_hold(conn, cfg, home, task_id, now):
    """True when a mac card's landing check would find the lane unable to run it now (switch off,
    no Mac or Xcode, or busy): the card is skipped and checked again on a later pass, with no
    event for a busy lane and the one `mac_lane_held` event for any other reason."""
    if not mac.card_is_mac(conn, task_id):
        return False
    why = mac.unavailable(config.redesign(cfg)["mac_runner"])
    if why:
        dispatch.mac_held(conn, task_id, why, now)
    try:
        return bool(why) or mac.busy(home)
    except OSError:      # the lock itself failed: this card waits, with one event; others land
        dispatch.mac_held(conn, task_id, "The Mac lane check failed (its lock file could not be"
                          " used), so this card waits.", now)
        return True


def _candidates(conn, mode, now, cfg, home, release):
    """(task_id, branch, sha) of done cards whose latest `completed` event is recent, oldest
    first, that still need a check: every review role at `pass`, a commit, and not seen; or (land
    mode) a push of this lander that reached origin/main without being recorded, checked at the
    card's latest completed commit (the pushed sha of a replayed card is not on its branch; the
    worker records that push first, then lands what the card still owes). The second needs no
    `pass`: a kill after the landing's own `completed` event leaves its check rows as the gates,
    and an advisory one must not strand the push. Recent: since the last unblock; a closed card
    always (`landing_rows.DONE_CARDS`)."""
    rows = conn.execute(landing_rows.DONE_CARDS, (now - WINDOW,)).fetchall()
    for task_id, branch, payload in rows:
        if fetch_waiting(conn, task_id, now):       # a closed card's origin fetch backs off
            continue                                # alone: the cards behind it go on
        doc = {} if blocks.was_closed(conn, task_id) else json.loads(payload or "{}")
        if owed := mode == "land" and _owed_push(conn, cfg, home, release, task_id):
            yield task_id, branch, owed
        elif doc.get("commit") and _approved(doc.get("gates") or {}) \
                and not settle.red_waiting(conn, cfg, task_id) \
                and not _seen(conn, mode, task_id, doc["commit"], cfg["repo_url"], branch,
                              lander.target(cfg)) \
                and not _held(conn, cfg, task_id, branch, doc["commit"], now) \
                and not _lane_hold(conn, cfg, home, task_id, now):
            yield task_id, branch, doc["commit"]
