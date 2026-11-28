"""The validator's contract commands, run off the tick thread.

The suite runs in a strong container for up to CONTRACT_TIMEOUT per command; run inline it held
the whole tick (no human files applied, nothing dispatched on any lane). `ContractJobs.step` is
the one call `Tick._start_reviews` makes for the validator role: it submits the container work to
a thread pool and returns None until the job is done, then returns `(run_id, outputs)` once.

Thread split: the tick thread builds the command list (`Tick._merge_gate` runs git on the clone,
`check_gate.commands` reads sqlite), plans the card's blind run (`blind.run.start`) and, once the
job is done, finishes the outputs (`check_gate.extra`, the blind rows, both may use the
connection). The thread only starts the container, execs each command and stops it, then runs the
blind run (up to 1200 s, a second container) when the card has one; it never touches sqlite. A
dropped job cancels its blind run: the container is stopped and nothing is recorded. A mac card's
job runs its board commands in the container like any card's; only its host-only commands (the
card's `check_cmd`, Apple toolchain tools, `mac.host_cmds`: `mac.host_only`) run on the host. A mac
job with host commands or a blind run takes `mac-lane.lock` in its worker thread when it starts
(busy: it returns None and a later pass submits it again, no event, no failure), runs them in that
one lock, and a dropped job kills its host commands (`_Live.cancel`).

Jobs live in memory only. A tick restart loses them: the next pass sees no job and no validator
run and submits a fresh one (the old container is an orphan the reaper removes; `run_ids` keeps a
running job's container off the reaper's list meanwhile)."""
import contextlib
import copy
import functools
import subprocess
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor, wait

from capsule import (base_red, check_gate, config, incident_blind, incidents, lease, shards,
                     workspace)
from capsule.blind import run as blind_run
from capsule.db import event, txn
from capsule.runners import docker, mac

GRACE = 0.25      # seconds a fresh submit waits: a fast job finishes in the same pass
INFRA_INCIDENT_AT = 3   # consecutive engine-down failures of one card that open an incident
BACKOFF, BACKOFF_CAP = 60, 900   # seconds to the first retry after an infra failure, doubling


# blind: `run.Pending`; args: `_exec_all`'s, to run the same job again
Job = namedtuple("Job", "run_id future blind live args", defaults=(None, None, ()))


@contextlib.contextmanager
def _docker(strong, run_id, ws, git_dir):
    """Yield `run(cmd, timeout)` in a strong container holding `ws`. The suite gets a working
    read-only git and a tmpfs TMPDIR; see docker.run_args."""
    name = strong.start(run_id, ws, git_dir=git_dir, tmpdir=docker.CONTRACT_TMPDIR)
    try:
        yield functools.partial(strong.exec, name)
    finally:
        # best effort: the outputs must survive; the reaper removes what this could not
        blind_run._stop_quietly(strong, name)


def _run_cmds(run, cmds, timeout, live=None):
    """[(cmd, exit, output, seconds)] of `cmds` through `run`; a timeout is exit 124. `live`,
    when given, is checked before each command (a dropped job stops there)."""
    done = []
    for cmd in cmds:
        if live is not None:
            live.check()
        t0 = time.monotonic()
        try:
            rc, out = run(cmd, timeout)
        except subprocess.TimeoutExpired:
            rc, out = 124, f"timed out after {timeout}s"
        done.append((cmd, rc, out, int(time.monotonic() - t0)))
    return done


class _Live(blind_run.Cancellable):
    """What a job shares with its thread: the cancel half (`stopped`, and `wt`, the host session's
    scratch copy, whose commands `cancel` kills so a dropped job frees the Mac lane at once) and
    where a mac card's lane lives (`host`: this job runs on the host; `home`: the Capsule home)."""

    def __init__(self, strong, home, host):
        super().__init__(strong)
        self.home, self.host = home, host
        self.hosted = frozenset()   # ids of the commands that run on the host (`mac.host_only`)
        self.lane = host            # the job needs `mac-lane.lock` (host commands or a blind run)
        self.base = {}       # cmd -> base_red.check's verdict on the start commit


def _exec_all(strong, run_id, ws, git_dir, cmds, timeout, blind=None, live=None,
              base=(None, ())):
    """Thread body: [(cmd, exit, output, seconds)], then the card's blind run (`blind`, whose
    outcome waits in `blind.ran` for the tick thread), or None for a mac job whose lane is busy
    (nothing started: the job goes back to a later pass, no event, no failure). No sqlite in here.
    Every command runs in the strong container except a mac card's host-only ones (`live.hosted`:
    its `check_cmd` and toolchain commands, `mac.host_only`), which run on the host under the Mac
    profile (`mac.session`). A job with host commands or a blind run (`live.lane`) takes
    `mac-lane.lock` here, when it starts, never queued behind a pool slot, held once and closed
    here (the blind run uses it as `blind.lane`); a job with neither never touches the lane."""
    live = live or _Live(strong, None, False)
    lock = mac.acquire(live.home) if live.lane else None
    if live.lane and lock is None:
        return None
    try:
        with contextlib.ExitStack() as stack:
            box = stack.enter_context(_docker(strong, run_id, ws, git_dir))
            host = (stack.enter_context(mac.session(ws, lock=lock, git_dir=git_dir))
                    if live.hosted else None)
            live.wt = getattr(host, "wt", None)

            def run(cmd, secs):
                return (host if id(cmd) in live.hosted else box)(cmd, secs)
            done = _run_cmds(run, cmds, timeout, live)
            live.base = base_red.check(box, done, base, docker.container_git_dir(
                ws, git_dir) if base[0] else "", timeout, live)
        if blind is not None:
            blind.lane = lock
            blind.run()
    finally:
        if lock is not None:
            lock.close()
    return done


def _exec_probed(*args):
    """`_exec_all`, plus one probe when it raises: if the Docker daemon does not answer, the
    exception carries `daemon_lost` (a probe that fails counts as down, the rule of
    `runners.exec_mcp._daemon_lost`). Runs in the worker thread; `step` reads the flag."""
    try:
        return _exec_all(*args)
    except Exception as exc:
        probe = getattr(args[0], "_engine_up", None)   # the daemon only: not the image check
        try:
            exc.daemon_lost = probe is not None and not probe()
        except Exception:
            exc.daemon_lost = True
        raise


def _may_need_lane(cfg, task):
    """True when a mac card's job may need the Mac lane: the board has blind tests (a blind run
    uses it), or a shard or the card's `check_cmd` is host-only. A job with none runs in docker."""
    cmds = [*shards.expand(cfg), *check_gate._check_commands(cfg, task)]
    return config.redesign(cfg)["blind_tests"] != "off" or any(
        mac.host_only(c, cfg) for c in cmds)


def _status(conn, task_id):
    row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
    return row["status"] if row is not None else None


class ContractJobs:
    def __init__(self, timeout, tail, grace=GRACE):
        self.timeout, self.tail, self.grace = timeout, tail, grace
        self.pool, self.jobs = None, {}      # (task_id, impl_run_pk) -> Job
        self.backoff = {}                    # task_id -> (retry not before, last delay)
        self.infra = {}                      # task_id -> consecutive engine-down failures

    def run_ids(self, conn):
        """Run ids the reaper must spare. A job whose card left review (reset, closed, parked) is
        dropped and cancelled here, so the reaper then removes its container."""
        for key in [k for k in self.jobs if _status(conn, k[0]) != "review"]:
            self._drop(key)
        for tid in [t for t in self.backoff if _status(conn, t) != "review"]:
            del self.backoff[tid]
            self.infra.pop(tid, None)
        return {job.run_id for job in self.jobs.values()}

    def running(self, task, impl, now):
        """True while this card's job has not finished, or waits out the backoff after an infra
        failure (so no validator is admitted and no new job starts)."""
        job = self.jobs.get((task["id"], impl["id"] if impl is not None else None))
        return self.backoff.get(task["id"], (0, 0))[0] > now or (
            job is not None and not job.future.done())

    def _failed(self, task_id, job, exc, now, blind=()):
        """Back off this card's next job, then raise the failure with no run id in its text, so the
        tick's once-per-message error dedupe holds across retries."""
        self._back_off(task_id, now)
        text = incident_blind.why(exc, task_id, blind)
        for name in (job.run_id, job.run_id[:12]):
            text = text.replace(name, "<run>")
        raise RuntimeError(f"contract job failed: {text}") from None

    def _next_delay(self, task_id):
        return min(self.backoff.get(task_id, (0, BACKOFF // 2))[1] * 2, BACKOFF_CAP)

    def _back_off(self, task_id, now):
        delay = self._next_delay(task_id)
        self.backoff[task_id] = (now + delay, delay)
        return delay

    def _daemon_wait(self, conn, cfg, task_id, job, now, why):
        """The Docker engine or daemon did not answer: back off, write one `contract_infra` event,
        and count it; the INFRA_INCIDENT_AT-th in a row opens a `review_infra` incident. Returns
        None (a wait: no tick_error, no crash). G1 `docker_down` pages after `docker_down_s`."""
        delay = self._back_off(task_id, now)
        with txn(conn):
            event(conn, task_id, "contract_infra",
                  {"run_id": job.run_id, "why": why, "retry_s": delay}, now=now)
        self.infra[task_id] = self.infra.get(task_id, 0) + 1
        if self.infra[task_id] == INFRA_INCIDENT_AT:     # keeps backing off, but is not silent
            incidents.open_incident(
                conn, board=cfg["board"], kind="review_infra", key="Docker engine down",
                evidence=[f"event:{task_id}", why[:300]], task_id=task_id,
                run_id=job.run_id, now=now)

    def _drop(self, key):
        job = self.jobs.pop(key)
        job.future.cancel()
        if job.live:                         # kills a host job's commands: the Mac lane is free
            job.live.cancel()
        if job.blind is not None:
            job.blind.cancel()               # stops its container now; the thread records nothing

    def step(self, tick, conn, cfg, task, impl, ws, now, run_id):
        """None while the job is not done; `(run_id, outputs)` once, then the job is gone."""
        key = (task["id"], impl["id"] if impl is not None else None)
        for stale in [k for k in self.jobs if k[0] == key[0] and k != key]:
            self._drop(stale)                # a job of an earlier implementer run
        job = self.jobs.get(key)
        if job is None:
            if task["tier"] == "mac" and mac.busy(tick.home) and _may_need_lane(cfg, task):
                return None          # another mac job holds the lane: wait, no job, no event
            job = self.jobs[key] = self._submit(tick, conn, cfg, task, ws, now, run_id)
            wait([job.future], timeout=self.grace)
        if not job.future.done():
            return None
        self.jobs.pop(key)
        try:
            raw = job.future.result()
        except docker.EngineDown as exc:    # engine down or start timed out: one event, retry later
            return self._daemon_wait(conn, cfg, key[0], job, now, str(exc) or "engine down")
        except Exception as exc:     # infra failure: _guarded files it, the retry waits its backoff
            if incident_blind.hold(conn, key[0], exc, job.run_id, now):
                raise                # a blind-mount refusal: held, never retried
            if getattr(exc, "daemon_lost", False):   # daemon down: a wait, not a card crash
                return self._daemon_wait(conn, cfg, key[0], job, now, "docker daemon lost")
            blind = incident_blind.roots(tick._spool_root(cfg), cfg)
            raised = f"RuntimeError: contract job failed: {incident_blind.why(exc, None, blind)}"
            with txn(conn):          # the job is over: `idle_lane.contract_live` ends on this
                if incident_blind.contract_start_timeout({"error": raised}):   # engine infra
                    event(conn, task["id"], "contract_infra", {
                        "run_id": job.run_id, "why": "docker run timed out",
                        "retry_s": self._next_delay(key[0])}, now=now)
                else:
                    event(conn, task["id"], "contract_failed",
                          {"run_id": job.run_id, "error": str(exc)[:300]}, now=now)
            self._failed(key[0], job, exc, now, blind)
        if raw is None:              # a mac job that found the lane busy at its start: the same
            self.jobs[key] = job._replace(future=self.pool.submit(_exec_probed, *job.args))
            return None              # job runs again, so the next pass writes no second event
        self.backoff.pop(key[0], None)
        self.infra.pop(key[0], None)
        self._timeouts(conn, task, raw, now)
        raw = base_red.settle(raw, job.live.base)    # a flaky command: its re-run, not its 1st run
        outs = [{"cmd": cmd, "exit": rc, "output": (out or "")[-self.tail:], "seconds": secs,
                 **check_gate.extra(cmd, out, self.tail)} for cmd, rc, out, secs in raw]
        outs = base_red.rows(outs, job.live.base, job.args[-1][0])
        base_red.record(conn, cfg, task, outs, now)   # also writes the `contract_flaky` events
        blind = job.blind.finish(tick, conn) if job.blind is not None else []
        return job.run_id, outs + blind

    @staticmethod
    def _timeouts(conn, task, raw, now):
        """A command that hit the limit is a timeout (exit 124), never a test failure."""
        with txn(conn):
            for cmd, rc, out, secs in raw:
                if rc == 124 and (out or "").startswith("timed out"):
                    event(conn, task["id"], "contract_timeout",
                          {"cmd": str(cmd)[:300], "seconds": secs}, now=now)

    def _submit(self, tick, conn, cfg, task, ws, now, run_id):
        gate = [*tick._merge_gate(conn, task["id"], ws), *check_gate.commands(cfg, task, ws, conn)]
        # not the review seats: a hung job must never queue the next card's suite behind it
        workers = cfg["caps"]["max_running"] + config.cap(cfg, "max_review")
        if config.redesign(cfg)["blind_tests"] != "off":
            workers += cfg["caps"]["max_running"]   # a blind run holds a worker for ~20 min
        workers = max(workers, 1)
        if self.pool is None or self.pool._max_workers != workers:   # read each pass: no restart
            if self.pool is not None:    # its running and queued jobs finish on its own threads,
                self.pool.shutdown(wait=False)       # so a shrink overlaps old and new briefly
            self.pool = ThreadPoolExecutor(max_workers=workers)
        with txn(conn):
            event(conn, task["id"], "contract_started", {"run_id": run_id}, now=now)
        cmds, git_dir = [*shards.expand(cfg), *gate], str(workspace.gitdir(ws))
        # its own runner object: `red._bounded` swaps `_run` and `_sleep` on `tick.strong` for a
        # red check in the same pass, which must not shorten this suite's timeouts
        strong = copy.copy(tick.strong)
        blind = blind_run.start(tick, cfg, run_id, ws, conn, task, strong)
        live = _Live(strong, tick.home, task["tier"] == "mac")
        if live.host:       # only its host-only commands run on the Mac lane; the rest in docker
            live.hosted = frozenset(id(c) for c in cmds if mac.host_only(c, cfg))
            live.lane = bool(live.hosted) or blind is not None
        # a host command has no clean checkout: no start commit is re-run for it
        sha = lease.pinned_start_sha(conn, task["id"])
        base = (sha, {*(str(c) for c in cmds[:len(cmds) - len(gate)] if id(c) not in live.hosted),
                      *(str(c) for c in gate if isinstance(c, check_gate.CheckCmd)
                        and id(c) not in live.hosted)})
        args = (strong, run_id, str(ws), git_dir, cmds, self.timeout, blind, live, base)
        return Job(run_id, self.pool.submit(_exec_probed, *args), blind, live, args)
