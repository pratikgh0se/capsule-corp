"""Starting a blind tester run (blind spec 3.1, 3.2; R30), split out of `capsule.blind.dispatch`
for its size: `_start` is what `dispatch._dispatch_one` calls once a card is due a tester. It takes
the run row, then hands the setup (the interface snapshot, the tester's sparse blind clone, the
brief) to a thread (`_Setup`) and launches the container and worker once that is done, in the same
call for a fast setup or in `poll` on a later pass; a failure at any step closes the run
(`_abandon`). The tick thread never waits for the clones or the `interface_commands`. It reads
the role, the setup timeouts and `_skip` of `dispatch`, which imports this module only when it
starts a run or has a setup in flight."""
import contextlib
import copy
import functools
import os
import shutil
import subprocess
import time
import uuid
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, wait

from capsule import bundles, config, incident_blind, lease, paths, spool, workspace
from capsule.blind import preflight, scan, snapshot
from capsule.blind import run as blind_run
from capsule.blind.dispatch import COMMAND_TIMEOUT, ROLE, SETUP_TOTAL, _skip
from capsule.db import event, txn
from capsule.runners import docker, mac

OUTPUT_CAP = 64 * 1024         # bytes of one `interface_commands` entry's output that are kept
CUT = docker.CUT_MIDDLE
GRACE = 0.25                   # seconds a fresh setup waits: a fast one finishes in the same pass
Refusal = namedtuple("Refusal", "reason")      # a setup that ended in a skip event, not a run


def _capped(text):
    """`text` cut to `OUTPUT_CAP` bytes, the cut marked: the first and last half are kept, so
    a traceback at the end survives."""
    if len(text.encode("utf-8", "replace")) <= OUTPUT_CAP:
        return text
    half = OUTPUT_CAP // 2      # the runner may have marked its own cuts: join at the outer ones
    head = text.split(CUT, 1)[0].encode("utf-8", "replace")[:half]
    tail = text.rsplit(CUT, 1)[-1].encode("utf-8", "replace")[-half:]
    return head.decode("utf-8", "ignore") + CUT + tail.decode("utf-8", "ignore")


def _snapshot(job, ws, base, block, out):
    """The interface snapshot of `base` in `out`; `interface_commands` run in a throwaway strong
    container on that commit (read-only), each for at most `COMMAND_TIMEOUT` and all together by
    `job.deadline` (`SETUP_TOTAL` seconds from before the clones), each one's output capped at
    `OUTPUT_CAP`. Over the total raises (the run fails, `_abandon`). Returns the indices of
    commands that failed. The container carries the run's id, so the reaper spares it. A mac
    card's (`job.lane`, its held `mac-lane.lock`, closed here) run on the host under the Mac
    profile in a scratch copy (`mac.session`), never in docker."""
    commands, strong = block["interface_commands"], job.strong
    strong.cap = OUTPUT_CAP      # this job's own copy: its commands' output is never kept whole
    git_dir = str(workspace.gitdir(ws))

    def run_command(cmd):
        left = job.deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f"interface_commands are over their {SETUP_TOTAL}s total")
        job.check()
        code, text = execute(cmd, min(COMMAND_TIMEOUT, left))
        text = _capped(text)
        if code:
            raise subprocess.CalledProcessError(code, cmd, text)
        return text
    try:
        with contextlib.ExitStack() as stack:
            if commands and job.lane is not None:
                execute = stack.enter_context(mac.session(ws, lock=job.lane, git_dir=git_dir))
                job.wt = execute.wt         # a cancelled setup kills its commands
                job.check()
            elif commands:
                name = strong.start(job.run_id, str(ws), git_dir=git_dir,
                                    tmpdir=docker.CONTRACT_TMPDIR, ro=True)
                stack.callback(strong.stop, name)
                job.watch(name)
                execute = functools.partial(strong.exec, name)
            failed = snapshot.take(ws, base, block["tester_may_read"], commands, out, run_command)
            if time.monotonic() > job.deadline:
                raise TimeoutError(f"interface_commands took over their {SETUP_TOTAL}s total")
            return failed
    finally:
        if job.lane is not None:
            job.lane.close()


def _blind_clone(repo, dest, card, seed=None):
    """The tester's sparse copy of the blind repo: its own card's folder (the `seed` commit's
    `cards/<id>` on a rerun, nothing on a first run) and the shared harness, as the one commit of
    a new repository on branch `card/<id>`. Nothing else of the blind repo is in it, not another
    card's tests, not its history, so nothing the tester does in the container can reach them.
    Its git dir lives beside the clone (`workspace.gitdir`), outside every mount of /workspace:
    the clone's `.git` is only a pointer file, and the host git calls name the git dir (GIT_DIR)
    instead of reading whatever a run left at /workspace/.git."""
    gd = workspace.gitdir(dest)
    gd.parent.mkdir(parents=True, exist_ok=True)
    if seed:
        blind_run.unpack(repo, seed, card, dest, mode=0o755)
    else:
        (dest / "cards" / card).mkdir(parents=True)
        blind_run.copy_harness(dest)
    pinned = ("--git-dir", str(gd), "--work-tree", str(dest))   # never discovered from `.git`
    for step, args in (("init", ("init", "-q", "-b", f"card/{card}", f"--separate-git-dir={gd}",
                                 "--", str(dest))),
                       ("add", (*pinned, "add", "-A")),
                       ("commit", (*pinned, "commit", "-q", "--allow-empty", "--no-verify",
                                   "-m", "tests so far"))):
        done = preflight.git(dest.parent, *args)
        if done.returncode:
            raise RuntimeError(f"git {step} failed: {done.stderr.strip()}")
    # git wrote an absolute pointer; the container sees the git dir at /.gitdirs/<name>
    (dest / ".git").write_text(f"gitdir: {os.path.relpath(gd, dest)}\n")
    return dest


def _card(tick, cfg, task, ws, statements, card_hash, base):
    """The brief's card: title, numbered acceptance, hash and the spec section and rows the card
    cites, read at `base`."""
    cited = tick._guide_inputs(cfg, task, ws, sha=base)
    return {"id": task["id"], "title": task["title"], "acceptance": statements,
            "card_hash": card_hash, **{k: cited[k] for k in (
                "spec_section", "rows", "row_texts", "spec_path", "spec_sha")}}


def _pinned(conn, task_id):
    """The `start_sha` of the card's first implementer run (the base its review diffs read), or
    None while the card has no implementer run."""
    row = conn.execute("SELECT json_extract(metadata,'$.start_sha') FROM task_runs WHERE task_id=?"
                       " AND profile='implementer' ORDER BY id LIMIT 1", (task_id,)).fetchone()
    return row[0] if row else None


def _at(ws, pinned):
    """(the tester's base commit, the names the epic's commits since it give away). The base is
    `pinned`, checked out in the throwaway clone so the interface commands run on it, or the
    clone's head; the names are empty when there is no pinned start or the head has not moved
    (the card's own work comes from `scan._work_names`)."""
    head = workspace.head(ws)
    if not pinned or pinned == head:
        return head, set()
    names = scan._work_names(ws, pinned)      # the same read-only scan as the epic clone's
    done = preflight.git(ws, "checkout", "-q", "--detach", pinned)
    if done.returncode:
        raise RuntimeError(f"git checkout failed: {done.stderr.strip()}")
    return pinned, names


def _prepare(job, tick, cfg, sp):
    """Job thread: (card, interface dir, base commit, failed command indices), or a `Refusal`.
    Reads a fresh clone of the epic branch, deleted before it returns; the base is the card's
    pinned `start_sha`, else the epic head; the interface goes under the run's spool `sp`. A brief
    naming an identifier from `start_sha..head` or the card's own work is refused: no tester, the
    card neither held nor failed."""
    task, block = job.task, config.blind_tests(cfg)
    root = paths.board_dir(cfg["board"], tick.home) / "blind-snap" / sp.name
    try:
        ws = workspace.clone(cfg["repo_url"], task["branch_name"], root)
        base, names = _at(ws, job.pinned)
        names |= scan._work_names(tick._work_root(cfg) / workspace._slug(task["branch_name"]),
                                  job.pinned)
        bad = snapshot.production_globs(ws, base, block["tester_may_read"])
        if bad:
            return Refusal("tester_may_read names production code: " + ", ".join(bad))
        interface = sp / "interface"
        failed = _snapshot(job, ws, base, block, interface)
        card = _card(tick, cfg, task, ws, job.statements, job.card_hash, base)
        try:    # the brief the worker will render, linted against what the epic changed since
            bundles.render_blind_tester_brief(card, interface, names)
        except snapshot.BriefLeak as leak:
            return Refusal(("the brief names identifiers from the epic's or the card's own"
                            " changes: " + ", ".join(leak.identifiers))[:500])
    finally:
        shutil.rmtree(root, ignore_errors=True)
    return card, interface, base, failed


def _start(tick, conn, cfg, task, statements, card_hash, route, now, rerun=(None, {})):
    """Start the tester run: 1 when it started, 0 after a refusal (a skip event, no run row).
    The run row comes first, so a failure at any later step closes it (it counts toward
    `MAX_TRIES`), removes what the run made and is raised. `rerun` is `_rerun_inputs`' (seed
    commit, extra brief keys)."""
    needs = task["tier"] == "mac" and config.blind_tests(cfg)["interface_commands"]
    lane = mac.acquire(tick.home) if needs else None     # only interface commands run on the host
    if needs and lane is None:
        return 0        # another mac job holds the lane: no run, no event, a later pass retries
    run_id = uuid.uuid4().hex
    try:
        with txn(conn):
            run_pk = lease.start_run(
                conn, task["id"], role=ROLE, claim_lock=f"{tick.host}:{run_id}", run_id=run_id,
                expires=now + cfg["caps"]["claim_ttl"], now=now, runner="host",
                metadata=config.run_meta(cfg, task["branch_name"], card_hash=card_hash, setup=True),
                provider=route["provider"], account=route["account"], model=route["model"])
    except BaseException:       # the run row failed: free the lane now, not when the traceback goes
        if lane is not None:
            lane.close()
        raise
    setups = _setups(tick)
    try:
        job = _Setup(copy.copy(tick.strong), run_pk, task, run_id, statements, card_hash, rerun,
                     _pinned(conn, task["id"]))
        job.lane = lane
        sp = spool.make(tick._spool_root(cfg), run_id)
        workers = max(config.cap(cfg, "max_review"), 1)
        if setups.pool is None or setups.pool._max_workers != workers:   # read each start: no
            if setups.pool is not None:         # restart; the old pool's setups, running and
                setups.pool.shutdown(wait=False)    # queued, still finish on its own threads
            setups.pool = ThreadPoolExecutor(max_workers=workers)
        job.future = setups.pool.submit(_work, job, tick, cfg, sp)
        if lane is not None:     # a setup cancelled before it started never reaches `_snapshot`
            job.future.add_done_callback(lambda _: lane.close())
    except Exception as exc:
        if lane is not None:
            lane.close()
        _abandon(tick, conn, cfg, task, run_pk, run_id, exc, now)
        raise
    setups[run_pk] = job
    wait([job.future], timeout=GRACE)
    return _settle(tick, conn, cfg, setups, job, now) if job.future.done() else 1


class _Setup(blind_run.Cancellable):
    """One tester run's setup (the epic clone, the interface commands, the brief, the sparse blind
    clone) on a job thread; `deadline` is `SETUP_TOTAL` seconds from before the first clone. The
    tick thread polls it (`poll`) and cancels it when the run ends under it."""

    def __init__(self, strong, run_pk, task, run_id, statements, card_hash, rerun, pinned):
        super().__init__(strong)
        self.run_pk, self.task, self.run_id = run_pk, task, run_id
        self.statements, self.card_hash, self.rerun = statements, card_hash, rerun
        self.pinned, self.future, self.deadline = pinned, None, None   # `_work` sets the deadline
        self.lane = None      # a mac card's held `mac-lane.lock`, for its interface commands


class _Setups(dict):
    """The tick process's setups in flight, by run row; a restart loses them (`poll` ends their
    orphaned run rows), and `pool` is theirs alone, so a hung clone never holds a contract job."""

    def __init__(self):
        super().__init__()
        self.pool = None


def _setups(tick):
    return tick.__dict__.setdefault("blind_setups", _Setups())


def _work(job, tick, cfg, sp):
    """Job thread, never sqlite: `_prepare`, the tester's sparse blind clone, then its container.
    A `Refusal`, or (card, interface dir, base commit, failed command indices, blind clone,
    container name)."""
    job.check()                  # it may have ended while it waited for a pool thread
    job.deadline = time.monotonic() + SETUP_TOTAL    # from here: queue time costs no try
    prepared = _prepare(job, tick, cfg, sp)
    if isinstance(prepared, Refusal):
        return prepared
    job.check()
    repo = preflight.expand(config.blind_tests(cfg)["repo"])
    clone = _blind_clone(repo, paths.board_dir(cfg["board"], tick.home) / "blind-work"
                         / job.run_id, job.task["id"], job.rerun[0])
    job.check()
    return (*prepared, clone, _start_container(job, clone, prepared[1]))


def _settle(tick, conn, cfg, setups, job, now):
    """Tick thread, once the setup is done: launch the tester (1), or after a refusal drop the run
    row (0). A failure of the setup or the launch closes the run and is raised."""
    setups.pop(job.run_pk, None)
    task, run_id = job.task, job.run_id
    try:
        done = job.future.result()
        if isinstance(done, Refusal):     # a refusal leaves no run: only the final skip event
            _skip(conn, task, job.card_hash, done.reason, now)
            with txn(conn):
                conn.execute("DELETE FROM task_runs WHERE id=?", (job.run_pk,))
            _remove(tick, cfg, run_id)
            return 0
        card, interface, base, failed, clone, name = done
        meta = config.run_meta(cfg, task["branch_name"], start_sha=base,
                               card_hash=job.card_hash, blind_clone=str(clone))
        with txn(conn):
            conn.execute("UPDATE task_runs SET metadata=? WHERE id=?", (meta, job.run_pk))
            event(conn, task["id"], "blind_tester_started",
                  {"card_hash": job.card_hash, "start_sha": base, "snapshot_failed": failed},
                  run_pk=job.run_pk, run_id=run_id, now=now)
        _launch(tick, conn, cfg, task, job.run_pk, clone,
                {"card": {**card, **job.rerun[1]}, "interface": str(interface),
                 "container": name}, now)
    except Exception as exc:
        if isinstance(exc, docker.MissingImage):    # held once as a capability block
            incident_blind.hold(conn, task["id"], exc, run_id, now)
        _abandon(tick, conn, cfg, task, job.run_pk, run_id, exc, now)
        raise
    return 1


def poll(tick, conn, cfg, now, counts):
    """Tick thread, once a pass before the dispatch: settle each setup that is done, and cancel the
    one whose run ended under it (the tick's timeout, a release); a tester run row left open by an
    earlier tick process in its setup is failed, so the card gets a fresh run."""
    setups = _setups(tick)
    for run_pk, job in list(setups.items()):
        run = tick._run(conn, run_pk)
        if run is None or run["ended_at"] is not None:
            setups.pop(run_pk)
            job.cancel()      # what it made goes once its thread has stopped, not under its feet
            job.future.add_done_callback(lambda _, id=job.run_id: _remove(tick, cfg, id))
        elif job.future.done():
            tick._guarded(conn, cfg, job.task["id"], now, counts, lambda job=job: _settle(
                tick, conn, cfg, setups, job, now), run_id=job.run_id)
    lost = conn.execute(
        "SELECT r.id AS pk, r.run_id AS rid, r.task_id FROM task_runs r"
        " WHERE r.profile=? AND r.ended_at IS NULL AND r.claim_lock LIKE ?"
        " AND json_extract(r.metadata,'$.setup') IS NOT NULL", (ROLE, f"{tick.host}:%")).fetchall()
    for row in [r for r in lost if r["pk"] not in setups]:
        _abandon(tick, conn, cfg, {"id": row["task_id"]}, row["pk"], row["rid"],
                 RuntimeError("the tick restarted during the tester's setup"), now)


def _remove(tick, cfg, run_id):
    """Everything a run that never started its worker left on disk: its spool (with the interface
    snapshot), the blind clone and that clone's git dir."""
    clone = paths.board_dir(cfg["board"], tick.home) / "blind-work" / run_id
    for path in (tick._spool_root(cfg) / run_id, clone, workspace.gitdir(clone)):
        shutil.rmtree(path, ignore_errors=True)


def _why(exc, tick=None, cfg=None):
    """`Type: text` of a failure, a blind-mount refusal as `incident_blind.masked` (its path), the
    board's blind roots (given `tick` and `cfg`) in a docker failure's output masked."""
    blind = incident_blind.roots(tick._spool_root(cfg), cfg) if tick else ()
    return incident_blind.why(exc, blind=blind)[:2000]


def _abandon(tick, conn, cfg, task, run_pk, run_id, exc, now):
    """The run failed before its worker was spawned: close it (a spawn failure has already) with
    a `blind_tester_failed` event, and remove what it made."""
    if tick._run(conn, run_pk)["ended_at"] is None:
        reason = _why(exc, tick, cfg)
        with txn(conn):
            lease.end_run(conn, run_pk, status="failed", outcome="prepare_failed", error=reason,
                          now=now)
            event(conn, task["id"], "blind_tester_failed", {"reason": reason[:500]},
                  run_pk=run_pk, run_id=run_id, now=now)
    _remove(tick, cfg, run_id)


def _start_container(job, clone, interface):
    """Job thread: the tester's container (blind clone rw at /workspace, its git dir `:ro`,
    `/interface:ro`, `/deps:ro`; GIT_DIR pinned inside it and, through `limits`, for the host
    `claude`). Returns its name; a cancel that came meanwhile stops it and raises."""
    gd = workspace.gitdir(clone)
    name = job.strong.start(
        job.run_id, str(clone), git_dir=str(gd), tmpdir=docker.CONTRACT_TMPDIR,
        mounts=(f"{interface}:/interface:ro",), allow_blind=(str(clone), str(gd)),
        env=(f"GIT_DIR={docker.container_git_dir(clone, gd)}", "GIT_WORK_TREE=/workspace"))
    try:
        job.watch(name)
    except Exception:
        blind_run._stop_quietly(job.strong, name)
        raise
    return name


def _launch(tick, conn, cfg, task, run_pk, clone, inputs, now):
    """Tick thread: the spawned worker, in the container `_start_container` made (`inputs`
    names it). A failure ends the run `spawn_failed` and is raised."""
    run = tick._run(conn, run_pk)
    try:
        tick._launch(conn, cfg, run, task, ws=clone, now=now, inputs=inputs)
    except Exception as exc:
        reason = _why(exc, tick, cfg)
        with txn(conn):
            lease.end_run(conn, run_pk, status="failed", outcome="spawn_failed", error=reason,
                          now=now)
            event(conn, task["id"], "spawn_failed", {"role": ROLE, "error": reason[:500]},
                  run_pk=run_pk, run_id=run["run_id"], now=now)
        raise
