"""Keeps an epic clone from drifting off `origin/main` between cards :
with no sync, anything merged to main while an epic is in flight only ever meets it
at the finalize PR, as one five-file conflict (the old-system dry run this card names)."""
import subprocess
import sys
import time
from pathlib import Path

from capsule import (blocks, config, cost, db, epic_clean, lander, lane_behind, outbox,
                     review_scope, workspace)
from capsule.db import event, transition, txn
from capsule.landing_reverts import completed_payload

CONFLICT_KIND = "epic_sync_conflict"
ERROR_KIND = "epic_sync_error"
MAIN = "main"
LANE_MAIN = "refs/capsule/lane-main"
_GIT_ERRORS = (RuntimeError, subprocess.TimeoutExpired)


def after_gate_pass(conn, cfg, task, ws, *, run_id=None, now=None):
    """True: `_verdict` may go on to mark the card done (not a declared epic, nothing
    new to merge, the merge landed clean or settled with main's lines)."""
    branch = task["branch_name"]
    if branch not in config.epic_branches(cfg):
        return True
    dirty = workspace._git("status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws).stdout.strip()
    if dirty:
        return _hold(conn, task["id"], "needs_input", run_id, now,
                     f"epic sync skipped: clone at {ws} is dirty ahead of the merge:\n{dirty}",
                     extra_event=("worktree_mismatch", {"role": "epic_sync", "error_class": "worktree_dirty",
                                                        "error": dirty[:500]}))
    try:
        workspace._git("fetch", "--end-of-options", "origin", MAIN, ws=ws, timeout=workspace.GIT_SLOW_TIMEOUT)
    except _GIT_ERRORS as exc:
        if isinstance(exc, RuntimeError) and "couldn't find remote ref" in str(exc):
            return True   # the origin has no `main` (never the product repo, e.g. a test fixture)
        return _hold(conn, task["id"], "transient", run_id, now,
                     f"epic sync: fetch of origin/{MAIN} failed: {exc}",
                     extra_event=("epic_sync_skipped", {"error": str(exc)[:500]}))
    advance_main(ws)
    before = workspace.head(ws)
    try:
        workspace._git(*workspace.IDENTITY, "merge", "--no-ff", "--no-edit", "--end-of-options",
                       f"origin/{MAIN}", ws=ws)
    except _GIT_ERRORS as exc:
        # nothing to abort: unrelated histories,
        if not (workspace.gitdir(ws) / "MERGE_HEAD").exists():
            return _hold(conn, task["id"], ERROR_KIND, run_id, now,   # a refused merge, a timeout
                         f"epic sync: merge of origin/{MAIN} into {branch} failed: {exc}")
        kept, conflicts = _settle_replayed(ws)
        if kept is None:
            reason = f"epic sync conflict merging origin/{MAIN} into {branch}: "
            reason += ", ".join(conflicts)
            return _hold(conn, task["id"], CONFLICT_KIND, run_id, now, reason)
    else:
        kept = None
    after = workspace.head(ws)
    if after != before:   # a no-op merge (nothing new on main) is not a sync (row 515)
        with txn(conn):
            event(conn, task["id"], "epic_synced", _synced(after, kept), run_id=run_id, now=now)
    return True


def _synced(sha, kept):
    return dict(sha=sha, main_kept=kept) if kept else {"sha": sha}


def _settle_replayed(ws):
    """(kept, conflicts) for an open conflicted merge. Inc_3af85063: the lander replays lane
    commits onto main as new commits, so a later main edit of those lines conflicts here though
    main holds the lane's change. Settle with main's files only when every conflicted file passes
    `_replayed`: `kept` lists them and the merge is committed. Else abort: `kept` is None (a real
    conflict). Never settles in part, and any git failure aborts."""
    listed = _git_or_none("diff", "--name-only", "--diff-filter=U", ws=ws)
    conflicts = listed.stdout.split() if listed else []
    repo = workspace.gitdir(ws)   # never `ws`: a run may plant its own `.git` there
    if conflicts and _no_revert(ws):
        main_ids = lander._patch_ids(repo, f"HEAD..origin/{MAIN}")
        if all(_replayed(ws, repo, f, main_ids) for f in conflicts) \
                and _git_or_none("checkout", f"origin/{MAIN}", "--", *conflicts, ws=ws) \
                and _git_or_none(*workspace.IDENTITY, "commit", "--no-edit", ws=ws):
            return conflicts, conflicts
    if (repo / "MERGE_HEAD").exists():
        workspace._git("merge", "--abort", ws=ws)
    return None, conflicts


def _no_revert(ws):
    """False when HEAD..origin/main holds a revert, or git cannot say."""
    out = _git_or_none("log", "-1", "--fixed-strings", "--grep", "This reverts commit",
                       "--format=%H", f"HEAD..origin/{MAIN}", ws=ws)
    return out is not None and not out.stdout.strip()


def _replayed(ws, repo, name, main_ids):
    """True: main keeps `name` and every lane commit touching it has a verbatim patch id on main.
    A merge commit has no patch id (`_patch_ids` skips merges), so it fails here."""
    if _git_or_none("cat-file", "-e", f"origin/{MAIN}:{name}", ws=ws) is None:
        return False
    listed = _git_or_none("rev-list", "HEAD", f"^origin/{MAIN}", "--", name, ws=ws)
    shas = listed.stdout.split() if listed else []
    for sha in shas:
        ids = lander._patch_ids(repo, "--no-walk", sha)
        if not ids or not ids <= main_ids:
            return False
    return bool(shas)


def sync_idle(conn, cfg, work_root, now=None):
    """The tick's idle-epic sync: `after_gate_pass` runs only after a gate pass, so main moving
    under an idle epic never reached it. Merge and publish `origin/main` into each declared epic
    clone no card holds (`epic_clean.held`: a live card with a pinned `start_sha` always holds).
    No card owns an idle sync, so a hold is an event on the `epic:<branch>` ledger id, once per
    (clone HEAD, main) pair. A clone ahead of its host epic branch (merged but unpublished)
    retries its push every tick; a host branch that moved is fast-forwarded to, or, if diverged,
    skipped with an event. Never raises."""
    now = int(now or time.time())
    epic_clean.clean_idle(conn, cfg, work_root, now)   # row 624: clean a dirty idle clone first
    for branch in config.epic_branches(cfg):
        ws = Path(work_root) / workspace._slug(branch)
        try:
            if not workspace.gitdir(ws).is_dir():
                continue
            if epic_clean.held(conn, branch):
                _note_held(conn, branch, ws, now)
            else:
                lane_behind.note(conn, branch, 0, 0, now)   # synced below: no lag to report
                _sync_one(conn, branch, ws, now)
        except _GIT_ERRORS + (OSError,) as exc:
            print(f"capsule epic_sync: {branch}: {exc}", file=sys.stderr)


def _sync_one(conn, branch, ws, now):
    git, slow = workspace._git, workspace.GIT_SLOW_TIMEOUT
    try:
        git("fetch", "--end-of-options", "origin", MAIN, ws=ws, timeout=slow)
        git("fetch", "--end-of-options", "origin",
            f"+refs/heads/{branch}:refs/remotes/origin/{branch}", ws=ws, timeout=slow)
    except _GIT_ERRORS:
        return   # no origin/main or epic branch, or the network is down: the next tick tries again
    advance_main(ws)
    host = f"origin/{branch}"
    if not _catch_up(conn, branch, ws, now, host) or not _merge_main(conn, branch, ws, now):
        return
    head = workspace.head(ws)
    if head != git("rev-parse", host, ws=ws).stdout.strip():
        # ahead of the host branch (by this tick's merge, an earlier tick whose push failed, or
        # the clean step's merge): publish. Deriving this from git, not a flag, retries forever.
        _push_idle(conn, branch, ws, _state(ws, head), now, f"epic:{branch}", head)


def advance_main(ws):
    """L136: after a fetch, move the clone's local `refs/heads/main` to `origin/main` so anything
    reading local `main` sees current code. Only a fast-forward, by compare-and-swap
    (`update-ref <new> <old>`: it fails if main moved since we read it); never a reset or force.
    A `main` checked out in this clone or any worktree of it is skipped: its index and files would
    no longer match the ref, and the clone may hold local changes. An epic clone sits on its epic
    branch, so main is normally free. True when main moved; skips are silent and never raise."""
    new = _git_or_none("rev-parse", "--verify", "-q", f"refs/remotes/origin/{MAIN}", ws=ws)
    old = _git_or_none("rev-parse", "--verify", "-q", f"refs/heads/{MAIN}", ws=ws)
    # a rebase of main in progress shows as detached; epic clones never rebase main
    listing = _git_or_none("worktree", "list", "--porcelain", ws=ws)
    checked_out = listing is None or f"branch refs/heads/{MAIN}" in listing.stdout.splitlines()
    if not new or not old or checked_out:
        return False
    new, old = new.stdout.strip(), old.stdout.strip()
    if new == old or not _is_ancestor(ws, old, new):
        return False
    return _git_or_none("update-ref", f"refs/heads/{MAIN}", new, old, ws=ws) is not None


def _note_held(conn, branch, ws, now):
    """L155: measure a held clone's base against main and record the lag (`lane_behind`). A card
    is running in this clone, so it writes only the private ref `LANE_MAIN` (never `origin/main`,
    local main or FETCH_HEAD, which the card's mounted git dir shows) and fetches at most once per
    `lane_behind.CHECK_S` per branch, failed or not."""
    key = f"{lane_behind.TRY_PREFIX}{branch}"
    last = (db.guard_get(conn, key)[0] or {}).get("at")
    if last is not None and now - last < lane_behind.CHECK_S:
        return
    db.guard_set(conn, key, {"at": now}, now=now)
    try:   # by URL: fetching the remote by name also moves refs/remotes/origin/main
        url = workspace._git("remote", "get-url", "origin", ws=ws).stdout.strip()
        workspace._git("fetch", "--no-write-fetch-head", "--no-auto-gc", "--no-tags",
                       "--end-of-options", url, f"+refs/heads/{MAIN}:{LANE_MAIN}", ws=ws,
                       timeout=workspace.GIT_FAST_TIMEOUT)
    except _GIT_ERRORS:
        return   # network down or no origin/main: keep the last record until it goes stale
    rng = f"HEAD..{LANE_MAIN}"
    count = _git_or_none("rev-list", "--no-merges", "--count", rng, ws=ws)
    times = _git_or_none("log", "--no-merges", "--format=%ct", rng, ws=ws)
    if count is None or times is None or not count.stdout.strip().isdigit():
        return
    stamps = times.stdout.split()
    hours = max(0, now - int(stamps[-1])) // 3600 if stamps else 0
    lane_behind.note(conn, branch, int(count.stdout), hours, now)


def _state(ws, head=None):
    main = workspace._git("rev-parse", f"origin/{MAIN}", ws=ws).stdout.strip()
    return {"head": head or workspace.head(ws), "main": main}


def _is_ancestor(ws, a, b):
    return _git_or_none("merge-base", "--is-ancestor", a, b, ws=ws) is not None


def _dirty(ws):
    return workspace._git("status", "--porcelain", *workspace.SCRATCH_EXCLUDE, ws=ws).stdout.strip()


def _dirty_hold(conn, branch, ws, now, dirt):
    payload = {"role": "epic_sync", "error_class": "worktree_dirty", "error": dirt[:500]}
    return _idle_hold(conn, branch, _state(ws), now, f"epic:{branch}", "needs_input", payload)


def _catch_up(conn, branch, ws, now, host):
    """False: stop this tick. A host epic branch someone else moved on is fast-forwarded to;
    one that diverged from the clone is an event, since a merge here could never be pushed."""
    ledger = f"epic:{branch}"
    if workspace.head(ws) == workspace._git("rev-parse", host, ws=ws).stdout.strip():
        return True
    if not _is_ancestor(ws, "HEAD", host):
        if _is_ancestor(ws, host, "HEAD"):
            return True   # merely ahead: the publish step handles it
        _idle_hold(conn, branch, _state(ws), now, ledger, "epic_sync_skipped",
                   {"error": f"clone and {host} have diverged; idle sync skipped"})
        return False
    if _dirty(ws):
        _dirty_hold(conn, branch, ws, now, _dirty(ws))
        return False
    try:
        workspace._git("merge", "--ff-only", "--end-of-options", host, ws=ws)
    except _GIT_ERRORS as exc:
        _idle_hold(conn, branch, _state(ws), now, ledger, "epic_sync_skipped",
                   {"error": str(exc)[:500]})
        return False
    with txn(conn):
        event(conn, ledger, "epic_synced", {"sha": workspace.head(ws)}, now=now)
    return True


def _merge_main(conn, branch, ws, now):
    """False: stop this tick (dirty, real conflict or a failed merge, each an event)."""
    if _is_ancestor(ws, f"origin/{MAIN}", "HEAD"):
        return True
    ledger = f"epic:{branch}"
    if _dirty(ws):
        _dirty_hold(conn, branch, ws, now, _dirty(ws))
        return False
    try:
        workspace._git(*workspace.IDENTITY, "merge", "--no-ff", "--no-edit", "--end-of-options",
                       f"origin/{MAIN}", ws=ws)
    except _GIT_ERRORS as exc:
        if not (workspace.gitdir(ws) / "MERGE_HEAD").exists():
            _idle_hold(conn, branch, _state(ws), now, ledger, "epic_sync_skipped",
                       {"error": str(exc)[:500]})
            return False
        kept, conflicts = _settle_replayed(ws)
        if kept is None:
            _idle_hold(conn, branch, _state(ws), now, ledger, "epic_sync_conflict",
                       {"branch": branch, "files": ", ".join(conflicts)[:500]})
            return False
    else:
        kept = None
    with txn(conn):
        event(conn, ledger, "epic_synced", _synced(workspace.head(ws), kept), now=now)
    return True


def _push_idle(conn, branch, ws, state, now, ledger, sha):
    """Publish the idle clone's HEAD. A host worktree that is dirty or a refused push is a hold
    event; the clone stays ahead of the host branch, so the next tick retries."""
    held = _free_host_branch(ws, branch)
    try:
        if held:
            raise RuntimeError(f"host worktree {held} has {branch} checked out and is dirty")
        workspace._git("push", "--end-of-options", "origin", f"HEAD:refs/heads/{branch}", ws=ws,
                       timeout=workspace.GIT_SLOW_TIMEOUT)
    except _GIT_ERRORS as exc:
        return _idle_hold(conn, branch, state, now, ledger, "epic_push_refused",
                          {"error": str(exc)[:500]})
    with txn(conn):
        event(conn, ledger, "epic_published", {"sha": sha}, now=now)
    db.guard_set(conn, f"epic_sync_idle:{branch}", {}, now=now)   # a later refusal is news again


def _idle_hold(conn, branch, state, now, ledger, kind, payload):
    key = f"epic_sync_idle:{branch}"
    if db.guard_get(conn, key)[0] == dict(state, kind=kind):
        return
    with txn(conn):
        event(conn, ledger, kind, payload, now=now)
    db.guard_set(conn, key, dict(state, kind=kind), now=now)


def _publish(conn, cfg, task, ws, *, run_id=None, now=None):
    """True: the host epic branch holds the clone's HEAD (or the card is not on a declared epic).
    False: the fast-forward push was refused or failed; this call already held the card. Never
    `--force`: the lander checks the host ref, so a refusal is an event and a hold, not a crash."""
    branch = task["branch_name"]
    if branch not in config.epic_branches(cfg):
        return True
    held = _free_host_branch(ws, branch)
    if held:
        return _hold(conn, task["id"], ERROR_KIND, run_id, now,
                     f"epic publish: host worktree {held} has {branch} checked out and is dirty; "
                     "commit or discard its changes, then unblock")
    try:
        workspace._git("push", "--end-of-options", "origin", f"HEAD:refs/heads/{branch}", ws=ws,
                       timeout=workspace.GIT_SLOW_TIMEOUT)
    except _GIT_ERRORS as exc:
        return _hold(conn, task["id"], ERROR_KIND, run_id, now,
                     f"epic publish: push of {branch} to origin refused: {exc}",
                     extra_event=("epic_push_refused", {"error": str(exc)[:500]}))
    with txn(conn):
        event(conn, task["id"], "epic_published", {"sha": workspace.head(ws)},
              run_id=run_id, now=now)
    return True


def _free_host_branch(ws, branch):
    """Git refuses a push to a branch checked out in a host worktree."""
    url = _git_or_none("remote", "get-url", "origin", ws=ws)
    if url is None or not Path(url.stdout.strip()).is_dir():
        return None
    listing = _git_or_none("-C", url.stdout.strip(), "worktree", "list", "--porcelain")
    for block in (listing.stdout if listing else "").split("\n\n"):
        lines = block.strip().splitlines()
        if f"branch refs/heads/{branch}" not in lines or not lines[0].startswith("worktree "):
            continue
        wt = lines[0][len("worktree "):]
        status = _git_or_none("-C", wt, "status", "--porcelain", "--untracked-files=no")
        if status is None:
            continue
        if status.stdout.strip():
            return wt
        if _git_or_none("merge-base", "--is-ancestor", lines[1].split()[-1], "HEAD", ws=ws):
            _git_or_none("-C", wt, "checkout", "--detach")
    return None


def _git_or_none(*args, ws=None):
    try:
        return workspace._git(*args, ws=ws)
    except _GIT_ERRORS + (OSError,):   # a missing clone dir is unknown, like any git failure
        return None


def finish_gate_pass(tick, conn, cfg, task, ws, touched, impl, docs, run_id, now,
                     guide_aligned_uncited, overridden, review_roles):
    """`Tick._verdict`'s post-gate-pass tail: commit the card's work, sync the clone, then mark it
    done unless the sync held it instead."""
    if hold_half_merge(conn, task, ws, run_id, now, tick._start_sha):   # W6-2: never reviewed
        return
    screens = tick._first_screen(conn, cfg, task, ws, touched)
    # <= 500 chars: cut the title, never the suffix that lander._card_commits matches
    suffix = f" ({task['id']}, capsule run {run_id})"
    sha = workspace.commit(ws, task["title"][:max(0, 500 - len(suffix))] + suffix)
    span = (ws, tick._start_sha(conn, task["id"]), sha or workspace.head(ws))
    with txn(conn):    # the round passed every gate: runaway does not count it as a round
        event(conn, task["id"], "gate_passed", run_pk=impl["id"], run_id=run_id, now=now)
    if not after_gate_pass(conn, cfg, task, ws, run_id=run_id, now=now) \
            or not _publish(conn, cfg, task, ws, run_id=run_id, now=now):
        return
    if sha is None and task["branch_name"] in config.epic_branches(cfg):
        sha = _card_tip(ws, task["id"])   # a hand merge leaves nothing new to commit
    with txn(conn):
        transition(conn, task["id"], "done", frm=("review",), completed_at=now)
        gates = {role: tick._gate_word(docs.get(role)) for role in review_roles}
        # guide's own doc still says "drifted"; the round passed
        if guide_aligned_uncited:
            gates["guide"] = "pass"
        gates["rounds"] = review_scope.reviewer_round(conn, task["id"])
        gates["overrides"] = list(overridden)
        event(conn, task["id"], "completed", completed_payload(conn, task["id"], sha, gates),
              run_pk=impl["id"], run_id=run_id, now=now)
        outbox.notify(conn, "done", f"{task['id']} done: {task['title']}",
                      fields=[("branch", task["branch_name"]), ("commit", (sha or "n/a")[:12]),
                              ("cost", cost.record(conn, task["id"], impl, span, now))],
                      thread_key=task["id"], file=False, now=now)
        if screens is not None:            # row 369: the epic waits on the owner's answer
            blocks.hold_first_screen(conn, task["id"], task["branch_name"], screens=screens, commit=sha, now=now)


MARKER_READ = 2_000_000       # bytes read per file: a huge file never stalls the tick


def _has_markers(path):
    """True when the regular file holds a `<<<<<<< `, `=======`, `>>>>>>> ` triple in that order
    within its first MARKER_READ bytes. Symlinks and non-files (a link to /dev/zero) are skipped."""
    p = Path(path)
    try:
        if p.is_symlink() or not p.is_file():
            return False
        with open(p, "rb") as fh:
            lines = fh.read(MARKER_READ).decode("utf-8", errors="replace").splitlines()
    except OSError:
        return False
    return _triple(lines)


def _triple(lines):
    seen = 0
    for line in lines:
        if seen == 0 and line.startswith("<<<<<<< "):
            seen = 1
        elif seen == 1 and line == "=======":
            seen = 2
        elif seen == 2 and line.startswith(">>>>>>> "):
            return True
    return False


def _blob_markers(ws, spec):
    """True/False: the blob `spec` (rev:path) holds a marker triple; None when it is missing. A blob
    over MARKER_READ bytes is never loaded and counts as False."""
    size = _git_or_none("cat-file", "-s", spec, ws=ws)
    if not size or not size.stdout.strip().isdigit():
        return None
    if int(size.stdout) > MARKER_READ:
        return False
    out = _git_or_none("show", spec, ws=ws)
    return None if out is None else _triple(out.stdout.splitlines())


def _renames(ws, start):
    """(new path, old path) for each file start..HEAD changed; a delete is skipped."""
    out = _git_or_none("diff", "--name-status", "-M", "-z", f"{start}..HEAD", ws=ws)
    tok = out.stdout.split("\0") if out else []
    pairs, i = [], 0
    while i < len(tok) and tok[i]:
        width = 3 if tok[i][0] in "RC" else 2
        if tok[i][0] != "D" and i + width <= len(tok):
            pairs.append((tok[i + width - 1], tok[i + 1]))
        i += width
    return pairs


def _committed_markers(ws, start):
    """Files the card's own commits (its start sha..HEAD) changed that now hold a marker triple in
    HEAD that neither the start sha nor origin/main had (the old path, for a rename)."""
    if not start or _git_or_none("cat-file", "-e", f"{start}^{{commit}}", ws=ws) is None:
        return []
    return [new for new, old in _renames(ws, start)
            if _blob_markers(ws, f"HEAD:{new}")
            and not _blob_markers(ws, f"{start}:{old}")
            and not _blob_markers(ws, f"origin/{MAIN}:{old}")]


def _half_merge(ws, start=None):
    """Why the clone holds a half-done hand merge (MERGE_HEAD, or the files with conflict
    markers among its changed tracked files), else ''."""
    if (workspace.gitdir(ws) / "MERGE_HEAD").exists():
        return "MERGE_HEAD present"
    changed = _git_or_none("diff", "--name-only", "-z", "HEAD", ws=ws)
    names = changed.stdout.split("\0") if changed else []
    bad = [n for n in names if n and _has_markers(Path(ws) / n)]
    bad += [n for n in _committed_markers(ws, start) if n not in bad]
    return "conflict markers in " + ", ".join(bad) if bad else ""


def hold_half_merge(conn, task, ws, run_id, now, start_sha=None):
    """W6-2: hold the card as `epic_sync_conflict` (a human `unblock` after finishing the merge
    goes straight back to review) when its clone is a half-done hand merge; the reason or None.
    `start_sha` is `Tick._start_sha`; markers the card committed itself count only with it."""
    try:
        start = start_sha(conn, task["id"]) if start_sha else None
    except (KeyError, TypeError, ValueError, RuntimeError):   # no pinned sha: no start
        start = None
    half = _half_merge(ws, start)
    if not half:
        return None
    reason = f"half-done hand merge in {ws}: {half}. Finish the merge there first, then unblock."
    _hold(conn, task["id"], CONFLICT_KIND, run_id, now, reason)
    return reason


def _card_tip(ws, task_id):
    """The newest non-merge commit on HEAD and off origin/main whose message names the card the way
    `finish_gate_pass` writes it (`... (<task_id>, capsule run N)`), else None (inc_e6170a47).
    Never a merge: the lander refuses one."""
    out = _git_or_none("rev-list", "-1", "--no-merges", "--fixed-strings", "--grep",
                       f"({task_id}, capsule run", "HEAD", f"^origin/{MAIN}", ws=ws)
    return (out.stdout.strip() or None) if out else None


def _hold(conn, task_id, kind, run_id, now, reason, *, extra_event=None):
    with txn(conn):
        if extra_event is not None:
            event(conn, task_id, extra_event[0], extra_event[1], run_id=run_id, now=now)
        blocks.block(conn, task_id, kind=kind, reason=reason[:2000], run_id=run_id, now=now,
                     offer="unblock|reset")
    return False
