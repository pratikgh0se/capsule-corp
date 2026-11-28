"""M137: a worker's missing tool becomes an image-request card that a tick step handles (D5-D8).

Filing. A `block` request with kind `capability` may carry `tool` (a Debian package) and `bin`
(the command it should provide). `file` makes one card per board and tool (`image:<board>:<tool>`,
assignee `image-builder`, which no config may map to a model, so dispatch never routes it), links
the waiting card under it, and holds the waiter as a dependency. A second ask comments on the
open card; a card a human closed within 30 days becomes a plain capability block instead.

Handling. `step`, once per board pass after the docker-down check, advances every ready request
card: D7 edit of the project's own capsule.toml only (`image_toml`), a clone at origin/main, one
build per machine (an `fcntl` lock on `<home>/image/build.lock` that the detached `capsule image
request-run` inherits and holds for its whole life, so a dead builder frees it), the build writing
`<home>/image/<task>.json` (no record, no push, no card change), polled by later passes. `home` is
derived from the board db's path, for filing and for the step alike (one ledger).
Docker is never started (D224): docker down, or free disk under the build floor,
waits with no try counted. D8 limits (`image_ledger`) and any D7 refusal block the card
`needs_input`. Success needs both oracles (`image_build.build`: the render judge plus every smoke
line under --network=none) and the disk and size limits; then the digest is recorded in
`images.lock`, `image/<task>` is pushed, the card is `done` with its gates (the lander lands it,
`_protected` holds capsule.toml for the stamp) and each waiter is promoted. A try that dies or
outlasts its timeout counts as failed. `step` never raises."""
import contextlib
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from capsule import blocks, db, image_ledger as ledger, image_render, image_toml, paths
from capsule.db import event, guard_get, guard_set, txn
from capsule.runners import docker

ASSIGNEE = "image-builder"
BIN = re.compile(r"[a-z0-9][a-z0-9._+-]{0,40}")
GB = 10**9
MAX_RUN_S, FREE_AFTER, ALL_CAP, GROWTH = 1200, 25 * GB, 20 * GB, GB   # D8, D10, open question 2
REFUSED_DAYS = 30
GUARD, ERRORS, GIT_FAILS = "image_req:", "image_err:", 3
D8 = "D8 limit"


def home_of(conn):
    """The home dir, from the board db path (boards/<board>/kanban.db under it): the one source
    of home for this module."""
    for row in conn.execute("PRAGMA database_list"):
        if row[1] == "main" and row[2]:
            return Path(row[2]).parents[2]
    raise RuntimeError("the board db has no file: no home for the image ledger")


def guard_target(conn, target):
    """ValueError when a worker verb aims at an image-request card: only a human or the tick's own
    step moves those (an unblock is a Z4 override, a reassign would route the card to a model)."""
    row = conn.execute("SELECT assignee FROM tasks WHERE id=?", (target,)).fetchone()
    if row is not None and row[0] == ASSIGNEE:
        raise ValueError(f"{target} is an image request: not for workers to unblock or reassign")


def check(doc):
    """ValueError unless tool/bin sit on a capability block, a valid package and command."""
    if "tool" not in doc and "bin" not in doc:
        return
    tool, bin_ = doc.get("tool"), doc.get("bin", doc.get("tool"))
    if (doc.get("verb"), doc.get("kind")) != ("block", "capability") or not (
            isinstance(tool, str) and image_render.APT.fullmatch(tool)
            and isinstance(bin_, str) and BIN.fullmatch(bin_)):
        raise ValueError("tool/bin: only on a capability block; tool a Debian package, "
                         "bin a command name")


def _find(conn, key, *, open_only):
    marks = "NOT IN ('done','archived')" if open_only else "='archived'"
    return conn.execute(
        f"SELECT id, created_at FROM tasks WHERE assignee=? AND instr(body, ?)=1 AND status {marks}"
        " ORDER BY created_at DESC LIMIT 1", (ASSIGNEE, f"key: {key}\n")).fetchone()


def _wait_on(conn, card, run, tool, now):
    waiter = run["task_id"]
    if not conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?",
                        (card, waiter)).fetchone():
        conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (card, waiter))
    blocks.block(conn, waiter, kind="dependency", run_pk=run["id"], run_id=run["run_id"], now=now,
                 reason=f"waiting on image request {card}: {tool}")


def file(conn, run, doc, now):
    """Apply a `block` capability request that names a tool (spool `_apply_verb`)."""
    board, tool, home = blocks._board_name(conn), doc["tool"], home_of(conn)
    waiter, key = run["task_id"], f"image:{board}:{tool}"
    card = _find(conn, key, open_only=True)
    if card:
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
            (card[0], run["profile"], f"also needed by {waiter}: {doc['text']}", int(now)))
        return _wait_on(conn, card[0], run, tool, now)
    refused = _find(conn, key, open_only=False)
    if refused and now - refused[1] < REFUSED_DAYS * 86400:
        return blocks.block(
            conn, waiter, kind="capability", run_pk=run["id"], run_id=run["run_id"], now=now,
            reason=f"{doc['text']} (image request {refused[0]} for {tool} was refused within "
                   f"{REFUSED_DAYS} days)")
    over = len(ledger.rows(home, "filed", board=board, today=now)) >= ledger.DAILY_FILED
    bin_ = doc.get("bin", tool)
    body = f"key: {key}\ntool: {tool}\nbin: {bin_}\nwaiting: {waiter}\n\n{doc['text']}\n"
    card = db.new_task(conn, title=f"Image request: {tool} for {board}", body=body,
                       assignee=ASSIGNEE, status="ready", file_set=["capsule.toml"],
                       workload="upkeep", now=now)
    conn.execute("UPDATE tasks SET branch_name=? WHERE id=?", (f"image/{card}", card))
    event(conn, card, "image_requested", {"tool": tool, "bin": bin_}, now=now)
    ledger.append(home, "filed", now, board=board, task=card, tool=tool, minutes=0)
    if over:
        blocks.block(conn, card, kind="needs_input", now=now, reason=(
            f"{D8}: {ledger.DAILY_FILED} image requests already filed today for {board}"))
    _wait_on(conn, card, run, tool, now)


# ---- the tick step


def _free(home):
    return shutil.disk_usage(home).free


def _floor():
    try:
        return int((paths.REPO / "docker" / "BUILD_MIN_FREE_BYTES").read_text().strip())
    except (OSError, ValueError):
        return 40 * GB


def _lock(home):
    """The machine's build lock (an open file holding `flock`), or None when a build holds it."""
    path = Path(home) / "image" / "build.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None
    return fh


def _spawn(board, task, home, timeout, lock):
    """Start the detached builder; it inherits `lock`, which this process then closes."""
    argv = [sys.executable, "-m", "capsule", "image", "request-run", "--board", board, "--task",
            task, "--capsule-home", str(home), "--timeout", str(timeout)]
    with open(Path(home) / "image" / task / "run.log", "ab") as log:
        subprocess.Popen(argv, cwd=str(paths.REPO), stdout=log, stderr=log,
                         pass_fds=(lock.fileno(),), start_new_session=True)


def _git(cwd, *args):
    done = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          timeout=300, env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
                                            "LC_ALL": "C"})
    return done.returncode, (done.stdout + done.stderr).strip()


def _hold(conn, tid, reason, now):
    blocks.block(conn, tid, kind="needs_input", reason=reason[:600], now=now)


def step(conn, cfg, strong_up, now):
    try:
        home = home_of(conn)
        cards = conn.execute("SELECT id, body FROM tasks WHERE assignee=? AND status='ready'"
                             " ORDER BY created_at", (ASSIGNEE,)).fetchall()
    except Exception as exc:
        return print(f"capsule tick: image-request step failed: {type(exc).__name__}: {exc}",
                     file=sys.stderr)
    for card in cards:                  # a try per card: one card's error never starves the rest
        head = (card["body"] or "").split("\n\n")[0]
        f = dict(ln.split(": ", 1) for ln in head.splitlines() if ": " in ln)
        try:
            _one(conn, cfg, home, strong_up, now, card["id"], f)
        except Exception as exc:
            print(f"capsule tick: image request {card['id']} failed: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            with contextlib.suppress(Exception):     # the board db itself is failing: stderr only
                _errored(conn, card["id"], exc, now)


def _errored(conn, tid, exc, now):
    """Every failing pass leaves a card event; the 3rd in a row blocks the card (a permanent git
    or toml error would otherwise repeat for ever)."""
    rec, _ = guard_get(conn, ERRORS + tid)
    count = (rec or 0) + 1
    reason = f"{type(exc).__name__}: {exc}"[:300]
    with txn(conn):
        event(conn, tid, "image_request_error", {"reason": reason, "count": count}, now=now)
    guard_set(conn, ERRORS + tid, None if count >= GIT_FAILS else count, now=now)
    if count >= GIT_FAILS:
        _hold(conn, tid, f"{GIT_FAILS} passes in a row failed: {reason}", now)


def _one(conn, cfg, home, strong_up, now, tid, f):
    rec, _ = guard_get(conn, GUARD + tid)
    if rec:
        return _poll(conn, home, now, tid, rec, f)
    done = len(ledger.rows(home, "try", task=tid))
    lifted = ledger.rows(home, "override", task=tid)
    held = conn.execute("SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND "
                        "kind IN ('blocked','block_loop_detected')", (tid,)).fetchone()[0]
    used = {r.get("event") for r in lifted}
    # Z4: an override is one human `unblocked` event after the latest hold, consumed by its id
    fresh = [i for i, p in conn.execute(
        "SELECT id, payload FROM task_events WHERE task_id=? AND kind='unblocked' AND id>? "
        "ORDER BY id", (tid, held)) if p and json.loads(p).get("by") == "human" and i not in used]
    for event_id in fresh:
        lifted.append({"after_tries": done, "event": event_id})
        ledger.append(home, "override", now, board=cfg["board"], task=tid, tool=f.get("tool"),
                      minutes=0, after_tries=done, event=event_id,
                      outcome=f"{D8} lifted by a human unblock")
    pending = any(r["after_tries"] == done for r in lifted)    # one extra try, no try since
    left = ledger.DAY_MINUTES - sum(r["minutes"] for r in ledger.rows(home, "try", today=now))
    if done >= ledger.TRIES and not pending:
        return _hold(conn, tid, f"{D8}: {done} build tries failed for {f['tool']}", now)
    if left <= 0 and not pending:
        return _hold(conn, tid, f"{D8}: {ledger.DAY_MINUTES} image build minutes used today", now)
    if os.environ.get("OPS_WORKER_IMAGE"):
        return _hold(conn, tid, "OPS_WORKER_IMAGE is set: the worker image is pinned by hand", now)
    if not strong_up or _free(home) < _floor() or (lock := _lock(home)) is None:
        return                          # D224: nothing starts docker; no try counted
    with lock:
        if why := _prepare(cfg, home, tid, f):
            return _hold(conn, tid, why, now)
        timeout = MAX_RUN_S if pending else min(MAX_RUN_S, left * 60)
        _spawn(cfg["board"], tid, home, timeout, lock)
    guard_set(conn, GUARD + tid, {"started": now, "timeout": timeout, "board": cfg["board"],
                                  "waive": pending}, now=now)
    guard_set(conn, ERRORS + tid, None, now=now)


def _prepare(cfg, home, tid, f):
    """None when the clone holds the committed edit, else why a human must decide. A git failure
    raises (no try counted, the next pass clones again)."""
    work = home / "image" / tid / "work"
    shutil.rmtree(work.parent, ignore_errors=True)
    work.parent.mkdir(parents=True)
    branch = (cfg.get("lander") or {}).get("branch") or "main"
    ident = ("-c", "user.name=capsule-image-builder", "-c",
             "user.email=image-builder@capsule.invalid")
    msg = f"Image request: add {f['tool']} to the image ({tid}, capsule run 1)"
    steps = [(work.parent, "clone", "--quiet", cfg["repo_url"], "work"),
             (work, "checkout", "-q", "-B", f"image/{tid}", f"origin/{branch}")]
    for cwd, *args in steps:
        rc, out = _git(cwd, *args)
        if rc:
            raise RuntimeError(f"git {args[0]}: {out[-200:]}")
    text = (work / "capsule.toml").read_text(encoding="utf-8")
    new, why = image_toml.add_item(text, "apt", f["tool"])
    if not why:
        new, why = image_toml.add_item(new, "smoke", f"command -v {f.get('bin') or f['tool']}")
    if why:
        return why
    if new == text:     # nothing to add: the list already names it (the recorded image is stale)
        return (f"{f['tool']} is already in [image].apt and its smoke line is there: the recorded "
                f"image may be stale; rebuild it by hand (capsule image build)")
    (work / "capsule.toml").write_text(new, encoding="utf-8")
    for args in (("add", "capsule.toml"), (*ident, "commit", "-q", "-m", msg)):
        rc, out = _git(work, *args)
        if rc:
            raise RuntimeError(f"git {args[0]}: {out[-200:]}")
    return None


def _poll(conn, home, now, tid, rec, f):
    result = home / "image" / f"{tid}.json"
    free = _lock(home)          # first: a builder that writes its result and exits after this
    if free is not None:        # point has the file in place for the check below
        free.close()
    if result.is_file():
        try:
            res = json.loads(result.read_text(encoding="utf-8"))
        except ValueError:
            res = {"reason": "unreadable result file"}
        return _finish(conn, home, now, tid, rec, f, res if isinstance(res, dict) else {})
    if free is not None:        # the builder holds the lock all its life: gone, no result
        _fail(conn, home, now, tid, rec, f, "the build run died without a result")


def _end(conn, home, now, tid, rec, f, outcome):
    """Ledger the try, drop the in-flight record, the result file and the machine lock."""
    minutes = max(1, -(-(now - rec["started"]) // 60))
    ledger.append(home, "try", now, board=rec["board"], task=tid, tool=f["tool"],
                  minutes=minutes, outcome=outcome)
    guard_set(conn, GUARD + tid, None, now=now)
    (home / "image" / f"{tid}.json").unlink(missing_ok=True)


def _fail(conn, home, now, tid, rec, f, reason, *, stop=False):
    """A failed try. `stop`: a decision for a human (disk, size, push), never retried."""
    _end(conn, home, now, tid, rec, f, reason[:200])
    shutil.rmtree(home / "image" / tid, ignore_errors=True)
    if stop:
        _hold(conn, tid, reason, now)
    elif len(ledger.rows(home, "try", task=tid)) >= ledger.TRIES:
        _hold(conn, tid, f"{D8}: {ledger.TRIES} build tries failed for {f['tool']}: {reason}", now)


def _verdict(home, f, res, waive):
    """Why the built image may not be recorded, or None. Sizes are bytes from docker. A human
    override (`waive`) waives only the growth check, never the D10 disk and total limits; with no
    previous recorded image (`old_size` None) there is nothing to measure growth against."""
    size, old, total = (res.get(k) for k in ("size", "old_size", "total"))
    if not (isinstance(size, int) and isinstance(total, int)
            and (old is None or isinstance(old, int))):
        return "image sizes unreadable"
    free = _free(home)
    if free < FREE_AFTER:
        return f"free disk is {free // GB} GB after the build, under {FREE_AFTER // GB} GB (D10)"
    if total > ALL_CAP:
        return f"capsule images would total {total // GB} GB, over {ALL_CAP // GB} GB (D10)"
    if old is not None and size - old > GROWTH and not waive:
        return (f"adding {f['tool']} grows the image by {(size - old) // 10**6} MB, over "
                f"{GROWTH // 10**6} MB: your call")
    return None


def _finish(conn, home, now, tid, rec, f, res):
    if not res.get("digest"):
        return _fail(conn, home, now, tid, rec, f, res.get("reason") or "build failed")
    if why := _verdict(home, f, res, rec.get("waive")):
        return _fail(conn, home, now, tid, rec, f, f"not recorded: {why}", stop=True)
    from capsule import image_build
    name, work = docker.project_image_name(rec["board"]), home / "image" / tid / "work"
    image_build._record(paths.image_record(home), name, f"{name}@{res['digest']}")
    rc, out = _git(work, "push", "-q", "origin", f"image/{tid}")
    sha = _git(work, "rev-parse", "HEAD")[1]
    if rc:
        return _fail(conn, home, now, tid, rec, f, f"recorded, but the push failed: {out[-200:]}",
                     stop=True)
    _end(conn, home, now, tid, rec, f, "ok")
    gates = {"image_render": "pass", "image_smoke": "pass"}
    with txn(conn):
        db.transition(conn, tid, "done", frm=("ready",), completed_at=now)
        event(conn, tid, "completed", {"commit": sha, "gates": gates}, now=now)
        for (child,) in conn.execute("SELECT child_id FROM task_links WHERE parent_id=?",
                                     (tid,)).fetchall():
            blocks.promote_one(conn, child, now=now)
    shutil.rmtree(home / "image" / tid, ignore_errors=True)
