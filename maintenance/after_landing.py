#!/usr/bin/env python3
"""The post-landing chain the chief of staff ran by hand after every keeper landing (row 643).

Apply, in order, stopping at the first failure (non-zero exit, one-line reason on stderr that
names what already moved and the next step):
 1. Fetch both repos; refuse if the live checkout has changes to tracked files or the release
    clone (harness/main) has any change, untracked files included (capsule-tick.sh restart's own
    rule). Then `git pull --ff-only` in the live checkout. The release clone is not pulled
    before the restart decision (that would hide the trigger): capsule-tick.sh moves it, after
    its quiet wait, during `restart`; with no tick-code change (step 4) it is fast-forwarded here.
 2. `./deploy.sh` in the live checkout (deploy refuses from the harness), then `--check` there.
 3. A running tick is stale when a TICK_CODE path differs between the sha it loaded at start
    (`tick-<board>.sha`, written by `capsule tick`, shown as `loaded_sha` by capsule-tick.sh
    status) and origin/main, or when no sha is recorded for its pid (an old process). The
    release clone's own HEAD is not the test: the clone may already be pulled. Run
    `bin/capsule-tick.sh restart <board> --quiet` from the harness for every stale board that has
    a tick plist and whose tick is running (the script's own `status`: alive and holding its
    lock); a board without a plist is skipped. Afterwards `status` must show a new pid,
    alive, with the lock.
 4. With no restart, `git merge --ff-only origin/main` in the release clone (it ends at
    origin/main after every --apply; the stale-by-design early return above is the one exception),
    then `./deploy.sh --check` in the release clone, then the
    image sweep (maintenance/image_sweep.py: untagged images and older
    tags of Capsule's own images the pins no longer name; never forced; a failed removal is
    logged once and never fails the landing; the newest rollback
    image per repo is kept), then a one-screen summary.
deploy.sh exit 4 means only the observability or secrets stack failed (or, for --check, drifted):
install and stamp are done. From the apply it is a WARNING, never a stop; a --check rc 4 is
accepted only when the apply also returned 4. The ticks are restarted, the reason repeats in
the summary, and after_landing exits 4 (0 = all healthy, 1 = refused). Any other non-zero stops.
The tick-code list is capsule/, bin/capsule-tick.sh, contracts/schemas/ and capsule/boundary.py
MAINT as of
origin/main (the in-process maintenance imports), so it cannot drift from the tick's own list.
The decision compares each tick's loaded sha with origin/main, so a re-run after a failed deploy,
check or partial restart still restarts the boards still stale (a restarted tick records its new
sha). The refusal after a partial restart names the boards already restarted
and each board still to restart with its exact `bin/capsule-tick.sh restart <board> --quiet S`
command (the same wait the script uses), and logs one `restart-partial` line. No flag = preview:
fetch, `git log old..origin/main`, `./deploy.sh --dry-run` (a failure there exits non-zero), the
boards that would restart, and the stale-board NOTE; nothing is pulled, deployed or restarted.
Stack-row sync (after the deploy checks, BEFORE any tick restart, so restarted ticks dispatch
against it): each capsule-base/python/web row of origin/main's docker/IMAGES.lock (git show) that
differs from the live record $CAPSULE_HOME/images.lock is written into the record when
`docker image inspect` finds that digest locally (backup `images.lock.bak` beside it, mode kept,
atomic replace, under the same lockdir + flock as image_build and build-base.sh; a busy
record prints a line and is skipped); an absent image prints the
`docker/build-base.sh <stack>` line; docker down = one skip line.
capsule-proj-* rows are never touched.
A landing that changes docker/base/, docker/stacks/python/ or docker/stacks/web/ prints one
`STALE record row capsule-<name>` line each (apply also logs `record-stale`; preview logs nothing).
A landing that changes a stack and also relocks can print a STALE line and then "record updated";
this is expected.
Every --apply step is logged as a JSON line (what, id, before, result) in
<maintenance state root>/after_landing.jsonl.
"""
import argparse
import ast
import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import events   # noqa: E402  state_root(): the maintenance state dir
import image_sweep   # noqa: E402

CHECKOUT = Path.home() / "CodeBase" / "capsule-corp"
HOME = Path.home() / ".local" / "share" / "capsule"
LA_DIR = Path(os.environ.get("CAPSULE_LA_DIR") or Path.home() / "Library" / "LaunchAgents")
# A change to any of these (plus capsule/boundary.py MAINT, read from origin/main by
# _tick_code) means the running tick has stale code: capsule/ is the tick itself and the script
# drives it.
TICK_ROOTS = ("capsule/", "bin/capsule-tick.sh", "contracts/schemas/")   # schemas: loaded once
STACK_SRC = (("docker/base/", "base"), ("docker/stacks/python/", "python"),
             ("docker/stacks/web/", "web"))   # layer source -> docker/build-base.sh argument
CLEAN = "deploy --check: clean"      # the line deploy.sh --check prints when nothing drifted
OBS_RC = 4                   # deploy.sh: only observability (compose, grafana, fluent-bit) failed
QUIET = "2700"               # --quiet wait (s): runs and contract jobs; 2 * 1200 + margin


class Refusal(Exception):
    pass


def _run(argv, cwd, timeout=300, **kw):
    try:
        return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout, **kw)
    except (OSError, subprocess.SubprocessError) as e:
        raise Refusal(f"{' '.join(argv[:3])} could not run: {e}")


def _git(repo, *args):
    r = _run(["git", "-C", str(repo), *args], None, 120)
    if r.returncode != 0:
        raise Refusal(f"git {' '.join(args[:2])} failed in {repo}: {r.stderr.strip()[:120]}")
    return r.stdout.strip()


def _dirty(repo, tracked_only):
    """Tracked changes only for the live checkout; the release clone counts untracked files too
    (the same rule as capsule-tick.sh restart's update_release)."""
    flag = ["--untracked-files=no"] if tracked_only else []
    return _git(repo, "status", "--porcelain", *flag)


def _tick_code(a):
    """TICK_ROOTS plus the MAINT tuple of capsule/boundary.py as of origin/main (read, not
    imported: the script's own copy may predate the landing)."""
    text = _git(a.checkout, "show", "origin/main:capsule/boundary.py")
    for node in ast.parse(text).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "MAINT" for t in node.targets):
            return (*TICK_ROOTS, *ast.literal_eval(node.value))
    raise Refusal("capsule/boundary.py has no MAINT tuple; cannot tell what the tick imports")


def _checkout_diff(a):
    """Tick-code paths that differ between the release clone's HEAD and origin/main (the
    stopped-tick note only: the restart decision uses the loaded code, `_stale`)."""
    return _git(a.harness, "diff", "--name-only", "HEAD", "origin/main", "--",
                *_tick_code(a)).splitlines()


def _stale(a, loaded):
    """Tick-code paths that differ between `loaded` (the sha a running tick recorded at start)
    and origin/main. No sha, or one the clone does not know (an old process), is stale: [""]."""
    if loaded:
        r = _run(["git", "-C", str(a.harness), "diff", "--name-only", loaded, "origin/main", "--",
                  *_tick_code(a)], None, 120)
        if r.returncode == 0:
            return r.stdout.split()
    return [""]


def _stale_boards(a, running):
    """{board: differing paths} for each running tick whose loaded code is stale, whatever
    state the release clone's checkout is in."""
    out = {}
    for board in running:
        diff = _stale(a, a.loaded.get(board))
        if diff:
            out[board] = diff
    return out


def _says(a, stale):
    return ", ".join(f"{b} stale (loaded {(a.loaded.get(b) or 'none recorded')[:10]})"
                     for b in stale) or "none"


def _status(a, board):
    """capsule-tick.sh's own status for a board: its JSON (label, pid, alive, lock)."""
    r = _run(["bin/capsule-tick.sh", "status", board], a.harness, 60)
    try:
        return json.loads(r.stdout)
    except ValueError:
        raise Refusal(f"capsule-tick.sh status {board} gave no JSON (rc={r.returncode})")


def _ticks(a):
    """({board: pid} of running ticks, [boards without a tick plist], [plist, not running]);
    also `a.loaded[board]`, the sha that tick loaded (None = unrecorded)."""
    running, skipped, stopped = {}, [], []
    a.loaded = {}
    for d in sorted(p for p in (a.home / "boards").glob("*") if p.is_dir()):
        st = _status(a, d.name)
        if not (a.la_dir / f"{st['label']}.plist").exists():
            skipped.append(d.name)
        elif st["alive"] and st["lock"]:
            running[d.name] = st["pid"]
            a.loaded[d.name] = st.get("loaded_sha")
        else:
            stopped.append(d.name)
    return running, skipped, stopped


STACKS = ("capsule-base", "capsule-python", "capsule-web")


def _rows(text):
    """{name: row} of the stack rows in images.lock-shaped text."""
    rows = (ln.strip() for ln in text.splitlines())
    return {r.split("@")[0]: r for r in rows if r.split("@")[0] in STACKS and "@sha256:" in r}


LOCK_TRIES, LOCK_WAIT = 600, 0.2     # the build-base.sh lock: 600 tries 0.2 s apart = 2 minutes


@contextlib.contextmanager
def _record_lock(path):
    """The lock capsule/image_build.py `_record` and docker/build-base.sh take around a rewrite of
    the record (copied, not imported: maintenance stays free of the capsule package): the
    `<path>.lockdir` mkdir lock retried for 2 minutes and never taken over, plus flock on
    `<path>.lock`. TimeoutError when it stays held."""
    lockdir = Path(f"{path}.lockdir")
    for _ in range(LOCK_TRIES):
        try:
            lockdir.mkdir()
            break
        except FileExistsError:
            time.sleep(LOCK_WAIT)
    else:
        raise TimeoutError(f"{lockdir} still held after 2 minutes")
    try:
        with open(f"{path}.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield
    finally:
        lockdir.rmdir()


def _write_row(live, stack, row):
    """Replace (or append) one stack row of the live record, the old text read under the lock;
    a backup beside it, the mode kept, an atomic replace. Returns the row it replaced."""
    tmp = live.with_name(f"{live.name}.tmp.{os.getpid()}")
    with _record_lock(live):
        try:
            old = live.read_text(encoding="utf-8").splitlines()
        except OSError:
            old = []
        before = _rows("\n".join(old)).get(stack)
        out = [row if ln.strip().split("@")[0] == stack else ln for ln in old]
        if before is None:
            out.append(row)
        try:
            if live.exists():
                shutil.copy2(live, live.with_name(f"{live.name}.bak"))
            tmp.write_text("".join(f"{ln}\n" for ln in out), encoding="utf-8")
            if live.exists():
                tmp.chmod(live.stat().st_mode & 0o7777)
            os.replace(tmp, live)
        finally:
            tmp.unlink(missing_ok=True)
    return before


def _sync_stacks(a):
    """Bring the live record's stack rows to origin/main's docker/IMAGES.lock (read from git,
    so it works before the release clone moves) when that image is local."""
    shown = _run(["git", "-C", str(a.harness), "show", "origin/main:docker/IMAGES.lock"], None, 60)
    want = _rows(shown.stdout) if shown.returncode == 0 else {}     # no such file: nothing pinned
    live = a.home / "images.lock"
    try:
        have = _rows(live.read_text(encoding="utf-8"))
    except OSError:
        have = {}
    for stack, row in want.items():
        if have.get(stack) == row:
            continue
        digest = row.split("@", 1)[1]
        try:
            r = events.docker_run(["image", "inspect", "--format", "{{.Id}}", digest],
                                  run=lambda argv, **kw: _run(argv, None, 60, **kw))
        except Refusal as e:
            print(f"stack rows: docker unavailable, {stack} not synced ({e})")
            return
        if r.returncode != 0 and "No such image" not in r.stderr:
            print(f"stack rows: docker not answering, {stack} not synced")
            return
        if r.returncode != 0 or r.stdout.strip() != digest:
            print(f"stack rows: {stack} image {digest[:19]} is not local; run "
                  f"docker/build-base.sh {stack.removeprefix('capsule-')} (rebuilds, records)")
            continue
        try:
            before = _write_row(live, stack, row)
        except (OSError, TimeoutError) as e:
            print(f"stack rows: {stack} not synced, record busy or unwritable ({e}); re-run "
                  "after_landing.py --apply or run docker/build-base.sh")
            continue
        _log({"what": "stack-row", "id": stack, "before": before, "result": row})
        print(f"stack rows: {stack} record updated to {digest[:19]}")


def _stale_rows(a, old, new, log=None):
    """Print one STALE line per record row whose layer source changed between old and new."""
    names = _git(a.checkout, "diff", "--name-only", old, new).splitlines()
    for prefix, name in STACK_SRC:
        if any(n.startswith(prefix) for n in names):
            print(f"STALE record row capsule-{name}: the landing changed {prefix}; "
                  f"rebuild with docker/build-base.sh {name}")
            if log:
                log({"what": "record-stale", "id": f"capsule-{name}", "before": old,
                     "result": new})


def _log(row):
    path = events.state_root() / "after_landing.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), **row}) + "\n")


def _preview(a):
    for name, repo in (("live checkout", a.checkout), ("release clone", a.harness)):
        _git(repo, "fetch", "-q", "origin")
        head, up = _git(repo, "rev-parse", "HEAD"), _git(repo, "rev-parse", "origin/main")
        dirty = _dirty(repo, repo == a.checkout)
        flag = " DIRTY (apply would refuse)" if dirty else ""
        log = _git(repo, "log", "--oneline", f"{head}..{up}") if head != up else ""
        verb = "would pull" if repo == a.checkout else "moved only by the tick restart"
        print(f"{name} {repo}: {head[:10]} -> {up[:10]}, {len(log.splitlines())} ({verb}){flag}")
        print("".join(f"  {ln}\n" for ln in log.splitlines()), end="")
    d = _run(["./deploy.sh", "--dry-run"], a.checkout, 600)
    if d.returncode != 0:
        raise Refusal(f"./deploy.sh --dry-run failed rc={d.returncode}: {d.stdout.strip()[-100:]}")
    would = [ln for ln in d.stdout.splitlines() if ln.startswith("WOULD")]
    print(f"deploy --dry-run (current checkout, before the pull): rc=0, "
          f"{len(would)} would-do lines")
    print("".join(f"  {ln}\n" for ln in would[:10]), end="")
    _stale_rows(a, "HEAD", "origin/main")
    running, skipped, stopped = _ticks(a)
    stale = _stale_boards(a, running)
    print(f"running ticks: {', '.join(f'{b} (pid {p})' for b, p in running.items()) or 'none'}"
          f"; no tick plist, skipped: {', '.join(skipped) or 'none'}")
    print(f"{len(_checkout_diff(a))} tick-code file(s) differ from origin/main in the release "
          "clone checkout (not the restart test)")
    print(f"stale ticks (loaded code vs origin/main): {_says(a, stale)}; "
          f"would restart: {', '.join(stale) or 'no board'}")
    if not running and _checkout_diff(a):
        _stale_note(a, stopped)
    image_sweep.sweep(False)
    print("preview only: nothing pulled, deployed or restarted; --apply does it")
    return 0


def _deploy(repo, *flags, soft=False):
    d = _run(["./deploy.sh", *flags], repo, 900)
    what = "deploy-check" if flags else "deploy"
    _log({"what": what, "id": str(repo), "before": None, "result": d.returncode})
    clean = CLEAN in [ln.strip() for ln in d.stdout.splitlines()]
    # rc 4 on apply: install and stamp done, ticks still restart. On --check it is accepted only
    # when the apply already returned 4 (soft): observability drift is the same failure.
    if d.returncode == OBS_RC and (not flags or soft):
        lines = (d.stdout + "\n" + d.stderr).splitlines()    # compose's reason is on stderr
        why = [x.strip() for x in lines if x.strip().startswith("FAILED") or "compose:" in x]
        note = "; ".join(why)[-300:] or "see deploy output"
        what = "observability or secrets drift" if flags else "observability step FAILED"
        print(f"WARNING deploy: {what} (rc={OBS_RC}), continuing: {note}", file=sys.stderr)
        return note
    if d.returncode != 0 or (flags and not clean):
        raise Refusal(f"./deploy.sh {' '.join(flags)} not ok in {repo} (rc={d.returncode}): "
                      f"{d.stdout.strip()[-100:]}")


def _restart(a, running):
    done = {}
    for n, (board, before) in enumerate(running.items()):
        # 3700 s: the quiet wait (2700) plus wait_live (up to ~915)
        r = _run(["bin/capsule-tick.sh", "restart", board, "--quiet", QUIET], a.harness, 3700)
        st = _status(a, board)
        _log({"what": "tick-restart", "id": board, "before": before,
              "result": {"rc": r.returncode, "pid": st["pid"], "alive": st["alive"]}})
        if r.returncode != 0 or not (st["alive"] and st["lock"]) or st["pid"] == before:
            left = list(running)[n:]       # the failed board first: it may have half-restarted
            _log({"what": "restart-partial", "id": "after_landing", "before": None,
                  "result": {"restarted": list(done), "remaining": left}})
            cmds = ", ".join(f"`bin/capsule-tick.sh restart {b} --quiet {QUIET}`" for b in left)
            raise Refusal(
                f"tick restart for {board} not confirmed (rc={r.returncode}); restarted so far: "
                f"{', '.join(done) or 'none'}; the release clone may already have moved, so a "
                f"re-run may see no trigger: check `bin/capsule-tick.sh status {board}` first, "
                f"then run from the release clone: {cmds}")
        done[board] = st["pid"]
    return done


def _stale_note(a, stopped):
    """A restart is needed but no tick runs: only a restart moves the release clone, so it stays
    stale by design. A loud note per installed-but-stopped board."""
    behind = _git(a.harness, "rev-list", "--count", "HEAD..origin/main")
    for board in stopped or ["<board>"]:
        print(f"NOTE {board}: tick not running; release clone {behind} commits behind "
              f"origin/main; resume with `bin/capsule-tick.sh restart {board}`, not start "
              "(start never moves it)")


KORIN_CARDS = "SELECT fix_card FROM incidents WHERE status='fix_pending' AND fix_card IS NOT NULL"


def _card_done(boards, here, fix_card):
    """True when the fix card (`t_x` here, or `<board>:t_x`) has status done; both read-only."""
    board, _, task = fix_card.rpartition(":")
    path = boards / board / "kanban.db" if board else here
    with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
        row = c.execute("SELECT status FROM tasks WHERE id=?", (task,)).fetchone()
    return row is not None and row[0] == "done"


def _korin_ready(a):
    """Z5 detector: fix_pending incidents whose fix card is done (any board), read-only. They wait
    for "incidents landed", then "close" (RUNBOOK, Korin outside-build close path). It never fails
    the landing."""
    total, unread = 0, 0
    for path in sorted((a.home / "boards").glob("*/kanban.db")):
        try:
            with contextlib.closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True)) as c:
                cards = [r[0] for r in c.execute(KORIN_CARDS)]
            total += sum(_card_done(a.home / "boards", path, card) for card in cards)
        except Exception:
            unread += 1       # a board with no incidents table, a locked one or a bad card
    print(f"korin: {total} fix_pending incident(s) with a done fix card"
          " (run incidents landed, then close)"
          + (f"; {unread} board(s) not read" if unread else ""))


def _apply(a, moved):
    if _dirty(a.checkout, True) or _dirty(a.harness, False):
        raise Refusal("the live checkout has tracked changes or the release clone has any "
                      "change; clean it first")
    for repo in (a.checkout, a.harness):
        _git(repo, "fetch", "-q", "origin")
    old = _git(a.checkout, "rev-parse", "HEAD")
    _git(a.checkout, "pull", "--ff-only", "-q")
    new = _git(a.checkout, "rev-parse", "HEAD")
    _log({"what": "pull", "id": str(a.checkout), "before": old, "result": new})
    moved.append(f"repo {old[:10]} -> {new[:10]}")
    _stale_rows(a, old, new, _log)       # before the deploys: a refused deploy still names them
    obs = _deploy(a.checkout)
    moved.append("deployed" + (" (observability FAILED)" if obs else ""))
    chk = _deploy(a.checkout, "--check", soft=bool(obs))
    obs = obs or chk
    _sync_stacks(a)       # before any restart: the new ticks dispatch against it
    running, skipped, stopped = _ticks(a)
    stale = _stale_boards(a, running)
    code = _checkout_diff(a)
    print(f"{a.checkout}: {old[:10]} -> {new[:10]}" + ("" if old != new else " (unchanged)"))
    if code and not running:
        _stale_note(a, stopped)
        print("release clone is stale by design: its deploy --check was skipped")
        _korin_ready(a)
        return OBS_RC if obs else 0
    if stopped and not code:
        print(f"info: tick not running for {', '.join(stopped)} (no tick-code change, so "
              "nothing to restart; check why it is down)")
    restarted = _restart(a, {b: running[b] for b in stale}) if stale else {}
    moved.append("restarted " + (", ".join(restarted) or "none"))
    if not restarted and _git(a.harness, "rev-parse", "HEAD") != _git(a.harness, "rev-parse",
                                                                    "origin/main"):
        _git(a.harness, "merge", "--ff-only", "-q", "origin/main")   # no trigger, so no restart
        moved.append("release clone fast-forwarded")
    obs = _deploy(a.harness, "--check", soft=bool(obs)) or obs
    ok = "accepted observability-only drift" if obs else "clean"
    print(f"deploy: applied; --check {ok}"
          " in the live checkout and in the release clone "
          + ("after the restart" if restarted else "at origin/main"))
    image_sweep.sweep(True, log=_log)
    print(f"stale ticks: {_says(a, stale)}; restarted: "
          f"{', '.join(f'{b} (pid {p})' for b, p in restarted.items()) or 'none'}"
          f"; skipped (no tick plist): {', '.join(skipped) or 'none'}")
    _korin_ready(a)
    if obs:
        print(f"WARNING observability stack NOT healthy: {obs}; heal retries compose every 5 min, "
              "or fix the cause and run ./deploy.sh")
        return OBS_RC
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--apply", action="store_true", help="do it; the default is a preview")
    p.add_argument("--checkout", type=Path, default=CHECKOUT)
    p.add_argument("--home", type=Path, default=HOME, help="the Capsule home (boards, harness)")
    p.add_argument("--la-dir", type=Path, default=LA_DIR, help="where the tick plists live")
    a = p.parse_args(argv)
    a.harness = a.home / "harness" / "main"
    moved = []
    try:
        return _apply(a, moved) if a.apply else _preview(a)
    except Refusal as e:
        msg = str(e)
        if a.apply:
            msg += (f"; already done: {', '.join(moved) or 'nothing'}; "
                    "next: fix that and re-run after_landing.py --apply")
            _log({"what": "refused", "id": "after_landing", "before": None, "result": msg})
        print(f"after-landing: {msg}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
