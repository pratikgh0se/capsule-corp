"""The blind run (R28, blind spec 4.1-4.3): after the contract commands, a SECOND strong container
runs the card's blind tests against the card's work. Mode `redesign.blind_tests`: `off` (default)
does nothing; `report` shows the results to the validator as `blind:<id>` rows marked `mode:
report only` that never fail or block a card; `gate` (R32) makes them count (`gate.py`).

Container shape (the same strong profile as every run, `runners/docker.py`): the card worktree at
`/workspace` READ-ONLY, `/blind:ro` = a `git archive` of the blind repo's `cards/<id>` plus a copy
of the protected harness, `/out` = `<spool>/blind-out` the only writable path (besides tmpfs), git
dir `:ro`, no network. The snapshot dir is removed in a `finally`. The product can write `/out`,
so the tick reads `results.jsonl` without following links, only as a regular file and only up to
1 MiB, and renders `<spool>/blind_result.json` outside the mount.

Two oracles per run (spec 4.2): the harness's `results.jsonl` (written by its result class) and the
runner's own summary line plus exit code (stdout). Counts, the fail/error verdict and the exit code
must agree. A second pair guards the worktree: `git status --porcelain` before and after must be
equal. Any disagreement, a missing result, or a changed worktree is `inconclusive` and opens a
Korin incident (`review_infra`); in report mode the run never blocks the card. A board without a
usable blind repo gets one `blind:unavailable` row, never a silent pass (spec 2); a reason that
holds for the whole board also opens one incident and one alert (`unavailable.py`).

Gate mode (R32, blind spec 4.3, 4.4): test rows are marked `gate` and carry `result`; a failing
one also carries `test` (id, given, expect, observed), the one finding the implementer gets. Every
other row (`blind:pending` no tests landed or they are stale, `blind:unavailable`,
`blind:inconclusive`, `blind:skipped` every test skipped) is marked `hold` with a plain `reason`:
the validator never sees it and `gate.hold` holds the card. Before the snapshot is deleted its
lines are kept for the host-side leak lint: a failing test whose finding holds a whole line of
the snapshot loses its given, expect and observed, and is counted in the `blind_gated` event
(`incidents._classify` opens the incident from a count above 0)."""
import collections
import contextlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import tomllib
import uuid
from collections import namedtuple
from pathlib import Path

from capsule import (config, incident_blind, incidents, jsonschema, paths, review_findings,
                     safe_write, spool, workspace)
from capsule.blind import checkout, preflight, unavailable
from capsule.db import event, txn
from capsule.runners import docker, host

SCHEMA = "blind_result.v1"
RESULTS_FILE = "results.jsonl"          # written by the harness result class (oracle 1)
RESULT_FILE = "blind_result.json"       # rendered by the tick into the spool, outside the mount
MAX_READ = 1 << 20                      # results.jsonl above 1 MiB is not read (inconclusive)
SNAP_DIR, OUT_DIR = "blind", "blind-out"    # under the run's spool
BLIND_TIMEOUT = 1200                    # = tick.CONTRACT_TIMEOUT
SNAP_MTIME = 946684800                  # 2000-01-01: the mtime of the card's files `unpack` writes
OUTPUT_TAIL = 8000
HARNESS_SRC = paths.REPO / "capsule" / "blind" / "harness"
CARD_ID = re.compile(r"^t_[0-9a-f]{8}$")
COMMIT_ID = re.compile(r"^[0-9a-f]{40}$")
RAN_LINE = re.compile(r"^Ran (\d+) tests? in ", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")
COUNTS = re.compile(r"(?<!expected )(failures|errors|unexpected successes)=(\d+)")
STATUS = ("OK", "FAILED", "NO TESTS RAN")        # the first words of unittest's final line
_COUNT = r"(?:failures|errors|skipped|expected failures|unexpected successes)=\d+"
SUMMARY = re.compile(           # the whole of unittest's final line, nothing before or after
    rf"(?:OK|FAILED|NO TESTS RAN)(?: \({_COUNT}(?:, {_COUNT})*\))?")
SUMMARY_MAX = 300            # bytes: unittest's longest line is under 150
MODE = "report"                         # the default row mode; `gate` rows are marked by `_mode`
MIN_LINE = 8                            # a snapshot line shorter than this (stripped) is not a leak
_NAME = r"\w+(?:\s+as\s+\w+)?"
IMPORT = re.compile(                    # a real import statement, never a prose line
    rf"(?:import\s+[\w.]+(?:\s+as\s+\w+)?(?:\s*,\s*[\w.]+(?:\s+as\s+\w+)?)*"
    rf"|from\s+[\w.]+\s+import\s+(?:\*|{_NAME}(?:\s*,\s*{_NAME})*|\(\s*(?:{_NAME}"
    rf"(?:\s*,\s*{_NAME})*\s*,?\s*\)?)?))\s*(?:#.*)?")
WITHHELD = "(withheld: it matched a line of the blind tests)"
PENDING = "no blind tests yet"          # the plain hold reason of a card with no landed tests
SKIPPED_REASON = "a blind test was skipped"     # likewise: a skipped test is never a pass
EXIT_OF = {"pass": 0, "skipped": 0, "fail": 1, "error": 2}     # spec 4.3
NO_EXAMPLE = "(no example row)"
OBSERVED = {"cmd": str, "exit": int, "stdout_tail": str, "stderr_tail": str}


def _row(cmd, exit_code, output, mode=MODE, **extra):
    return {"cmd": cmd, "exit": exit_code, "output": output[-OUTPUT_TAIL:], "blind": mode, **extra}


def _note(mode, cmd, text, reason):
    """A row that is not one test's result: shown as is in report mode; in gate mode marked `hold`
    (the validator never sees it) with the plain `reason` the card is held on."""
    if mode != "gate":
        return _row(cmd, 2, text, mode)
    return _row(cmd, 2, text, "hold", reason=reason)


def _held_mode(conn, cfg, task, mode):
    """The mode of a row about missing or unusable tests: gate mode holds a card that has landed
    tests or would get a blind tester (dispatch's test: acceptance statements, the strong or mac
    tier, a branch among the board's epics); any other card goes on as in report mode."""
    return "report" if mode == "gate" and not (gets_tester(cfg, task) or _recorded_commit(
        conn, task["id"])) else mode


def gets_tester(cfg, task):
    """True when the card would get a blind tester (dispatch's test: acceptance statements, the
    strong or mac tier, a branch among the board's epics): the one test the validator's hold and
    the lander's `no blind tests yet` hold both use."""
    from capsule.blind import dispatch      # dispatch imports this module
    return bool(dispatch.acceptance(task["body"])
                and (task["tier"] or "strong") in dispatch.TESTED_TIERS
                and task["branch_name"] in config.epic_branches_from_list(cfg.get("epics")))


def _stale(conn, task):
    """True when the acceptance text changed since the card's tests landed: the red check's own
    stale rule (`dispatch._rerun_inputs`; `blind_landed` carries the statements). Not stale: a
    card with no `blind_landed` event, or with no acceptance statements left (no tester reruns)."""
    from capsule.blind import dispatch      # dispatch imports this module
    last, now = dispatch._landed(conn, task["id"]), dispatch.acceptance(task["body"])
    return last is not None and bool(now) and last.get("acceptance", []) != now


def _failed_row(conn, cfg, task, mode, why):
    """The row of a blind run that raised (`why`: what went wrong, in words). Gate mode with no way
    to tell whether the card is held (`_held_mode` itself raised) holds it: a card is never passed
    on a broken check."""
    try:
        held = _held_mode(conn, cfg, task, mode)
    except Exception:
        held = mode
    return _note(held, "blind:unavailable", f"the blind run failed: {why}",
                 "blind tests unavailable")


def _recorded(conn, mode, task, run_id, rows):
    """`rows`, after one `blind_gated` event in gate mode (counts, failing count, leak count: every
    run, held or not, leaves one). The record is a note: it never stops the tick."""
    if mode == "gate":
        with contextlib.suppress(Exception):
            _record(conn, task, run_id, rows)
    return rows


def _record(conn, task, run_id, rows):
    """The `blind_gated` event of a gate-mode run, from its rows: result counts, failing count,
    leak count and, for a hold, the plain reason and detail."""
    gate = [r for r in rows if r["blind"] == "gate"]
    held = next((r for r in rows if r["blind"] == "hold"), {})
    counts = collections.Counter(r["result"] for r in gate)
    with txn(conn):
        event(conn, task["id"], "blind_gated", {
            "counts": {k: counts[k] for k in EXIT_OF} if gate else {},
            "failing": sum(bool(r["exit"]) for r in gate),      # a count: no test id (D17)
            "leaks": sum(bool(r.get("leaked")) for r in rows),
            "held": held.get("reason", ""), "detail": held.get("output", "")[:500]},
            run_id=run_id)


class _Cancelled(Exception):
    """The run a job thread was doing ended under it; its container is stopped."""


def _stop_quietly(strong, name):
    """Best effort stop: the reaper removes what this could not; the error goes to stderr."""
    try:
        strong.stop(name)
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        print(f"capsule: could not stop container {name}: {e}", file=sys.stderr)


class Cancellable:
    """The cancel half of work on a job thread (a blind run, a tester's setup): the thread tells
    `watch` the name of the container it started and calls `check` between steps; the tick thread
    calls `cancel`, which stops that container (on a thread of its own: docker may be slow, the
    tick never waits, and the reaper catches what is left) and makes the next `check` raise
    `_Cancelled`."""

    def __init__(self, strong):
        self.strong, self.name, self.stopped = strong, None, threading.Event()
        self.wt = None      # a host session's scratch copy (a mac job): `cancel` kills its commands

    def check(self):
        if self.stopped.is_set():
            raise _Cancelled("the run ended during its job")

    def watch(self, name):
        """Note the container just started; refuse one already cancelled."""
        self.name = name
        self.check()

    def cancel(self):
        self.stopped.set()
        if self.wt:
            threading.Thread(target=host._kill_under, args=(self.wt,), daemon=True).start()
        if self.name:
            threading.Thread(target=_stop_quietly, args=(self.strong, self.name),
                             daemon=True).start()


class Pending(Cancellable):
    """One card's blind run split between threads: `start` (tick thread) fills `plan`, or `rows`
    when there is nothing left to run; `run` is the job thread's part and leaves its outcome in
    `ran`; `finish` (tick thread) turns that into rows. `cancel` stops the container and keeps
    `run` from starting one."""

    def __init__(self, plan, ws, strong, rows=()):
        super().__init__(strong)
        self.plan, self.ws, self.rows, self.ran = plan, ws, list(rows), None
        self.mounted = ()       # the neutral checkout and its git dir, set by `_on_checkout`
        self.lane = None        # a mac card's held `mac-lane.lock`, set by its job (`_on_host`)

    def run(self):
        """The job thread's part. A failure of the run itself is kept for `finish` to say in a
        row; only `_Cancelled` before the container starts is raised."""
        if self.plan is not None and not self.plan.stale:
            self.check()
            self.ran = _execute(self.strong, self.plan, self.ws, self)

    def finish(self, tick, conn):
        """The rows of the run, as the inline run gave them; never raises."""
        if self.plan is None:
            return self.rows
        plan = self.plan
        mode = config.redesign(plan.cfg)["blind_tests"]
        try:
            rows = self._rows(tick, conn, mode)
        except Exception as exc:      # the row says what went wrong (report: shown; gate: held)
            unavailable.report(conn, plan.cfg, plan.card, plan.run_id, exc=exc,
                               card_paths=(self.ws, *self.mounted),
                               board_paths=(plan.root / plan.run_id / SNAP_DIR,))
            why = incident_blind.why(exc, plan.card, incident_blind.roots(plan.root, plan.cfg))
            rows = [_failed_row(conn, plan.cfg, plan.task, mode, why)]
        return _recorded(conn, mode, plan.task, plan.run_id, rows)

    def _rows(self, tick, conn, mode):
        plan = self.plan
        if plan.stale:        # opened here, once the job is done: never for a dropped job
            _incident(conn, plan.board, plan.card, plan.run_id, plan.stale)
            return [_note(mode, "blind:inconclusive", plan.stale, "blind tests inconclusive")]
        if self.ran.error is not None:
            raise self.ran.error
        return _finish(tick, conn, plan, self.ran, self.ws, mode)


Plan = namedtuple("Plan", "run_id card board commit repo root base stale work cfg task home",
                  defaults=(None, None))
Ran = namedtuple("Ran", "examples code text dirty error lines",
                 defaults=(None, None, None, "", None, ()))     # lines: the leak lint's (gate)


def start(tick, cfg, run_id, ws, conn, task, strong):
    """Tick thread: None with the mode off; else the card's `Pending` blind run, whose `rows` are
    the answer already when there is nothing to run (a refusal, or no tests)."""
    mode = config.redesign(cfg)["blind_tests"]
    if mode == "off":
        return None
    try:
        planned = _plan(tick, cfg, run_id, conn, task, mode)
    except Exception as exc:      # the row says what went wrong (report: shown; gate: held)
        unavailable.report(conn, cfg, task["id"], run_id, exc=exc, card_paths=(ws,),
                           board_paths=(tick._spool_root(cfg) / run_id / SNAP_DIR,))
        why = incident_blind.why(exc, task["id"],
                                 incident_blind.roots(tick._spool_root(cfg), cfg))
        planned = [_failed_row(conn, cfg, task, mode, why)]
    if isinstance(planned, Plan):
        return Pending(planned, ws, strong)
    return Pending(None, ws, strong, _recorded(conn, mode, task, run_id, planned))


def _plan(tick, cfg, run_id, conn, task, mode):
    """A `Plan` for the card's blind run (`stale` says why it is not to run), or the final rows when
    there is nothing to run. Opens nothing: the job may be dropped before it is used."""
    board, card = cfg["board"], task["id"]
    held = _held_mode(conn, cfg, task, mode)    # the mode of a row about missing tests
    problems = preflight.problems(cfg, board, preflight.worker_mounts(cfg, board, tick.home))
    if problems:
        unavailable.report(conn, cfg, card, run_id, problems)
        return [_note(held, "blind:unavailable", "; ".join(problems), "blind tests unavailable")]
    repo = preflight.expand(config.blind_tests(cfg)["repo"])
    commit = _recorded_commit(conn, card)       # the commit R31 landed, never the blind HEAD
    has_tests = commit and not (mode == "gate" and _stale(conn, task)) and preflight.git(
        repo, "ls-tree", "-d", commit, f"cards/{card}").stdout.strip()
    if not CARD_ID.match(card) or not has_tests:
        return [_note(held, "blind:pending", PENDING, PENDING)] if held == "gate" else []
    landed, base = _landed_base(conn, card), tick._start_sha(conn, card)
    why = ""
    if landed and base and landed != base:      # spec A2.3: the tests were red-checked elsewhere
        why = f"the blind tests were red-checked on {landed[:12]}, the card's base is {base[:12]}"
    return Plan(run_id, card, board, commit, repo, tick._spool_root(cfg), base, why,
                paths.board_dir(board, tick.home) / "work", cfg, task, tick.home)


def _execute(strong, plan, ws, pending):
    """Job thread: the spool, the snapshot and the container's run; a `Ran` that holds the
    failure when any step raises (`_Cancelled` too)."""
    try:
        sp = spool.make(plan.root, plan.run_id)
        snap, out = sp / SNAP_DIR, sp / OUT_DIR
        shutil.rmtree(out, ignore_errors=True)
        out.mkdir(mode=0o700)
        return _on_checkout(strong, plan, ws, snap, out, pending)
    except Exception as exc:
        return Ran(error=exc)


def _finish(tick, conn, plan, ran, ws, mode):
    """Tick thread: the rows of a finished run; the rendered result goes in the spool."""
    sp, card = plan.root / plan.run_id, plan.card
    rows, problem = _parse(_read(sp / OUT_DIR / RESULTS_FILE), ran.text, ran.code)
    problem = ran.dirty or problem
    if problem is None and mode == "gate":
        problem = _missing(conn, card, plan.commit, rows, ran.examples)
    envelope = {"schema": SCHEMA, "run_id": plan.run_id, "task_id": plan.card,
                "claim_lock": f"{tick.host}:{plan.run_id}", "blind_commit": plan.commit[:40],
                "base_sha": plan.base, "head_sha": workspace.head(ws)}
    doc, problem = _render(envelope, tick.schemas[SCHEMA], rows, problem, ran.examples)
    if mode == "gate":
        _anonymise(doc["tests"], ran.examples, card)
    leaked = _scrub(doc["tests"], ran.lines)
    # the rendered result lives in the spool, never in /out: the product can write there
    safe_write.atomic_write(sp, sp / RESULT_FILE, json.dumps(doc))
    if problem:
        # fingerprint = check name + card (spec 4.4 item 8): the role carries both, the problem
        # text goes in the evidence (an id-free key would merge every card's incident)
        _incident(conn, plan.board, card, plan.run_id, problem, str(sp / RESULT_FILE))
        return [_note(mode, "blind:inconclusive", problem, "blind tests inconclusive")]
    skipped = any(t["result"] == "skipped" for t in doc["tests"])
    if mode == "gate" and skipped and not any(EXIT_OF[t["result"]] for t in doc["tests"]):
        return [_note(mode, "blind:skipped", "a blind test was skipped", SKIPPED_REASON)]
    return [_test_row(t, mode, t["id"] in leaked) for t in doc["tests"]]


def _on_checkout(strong, plan, ws, snap, out, pending):
    """The `Ran` of the blind tests run in the second container. The container gets not the
    card's worktree but a neutral checkout of the files it holds now (`checkout.worktree_tree`:
    the card's work is not committed at this point; `checkout.build`, the same the red check
    gets), in a `work/<8 hex>/<name>` folder as the red check's: the card's real history, commit
    names and file times never reach the test. `dirty` says the run changed its read-only
    checkout, or the card's worktree changed while it ran (the result would be of work that is
    gone). The snapshot and the checkout are removed."""
    box = plan.work / uuid.uuid4().hex[:8]
    box.parent.mkdir(parents=True, exist_ok=True)
    box.mkdir()
    try:
        try:
            tree = checkout.worktree_tree(ws)
        except checkout.TooManyFiles as exc:          # too much to read on the contract job's time
            return Ran({}, 0, "", str(exc))
        product = checkout.build(workspace.gitdir(ws), tree, box / Path(ws).name)
        checkout.layout(product)
        pending.mounted = (product, workspace.gitdir(product))
        before = _status(product)
        examples = _snapshot(plan.repo, plan.commit, plan.card, snap)
        line = _run_line(plan.repo, plan.commit, plan.card)
        code, text = (_on_host(plan, product, snap, out, line, pending)
                      if is_mac(plan.task) else
                      _in_container(strong, plan.run_id, product, snap, out, line, pending.watch))
        gate = config.redesign(plan.cfg)["blind_tests"] == "gate"
        lines = _lines(snap, plan.card) if gate else ()     # the leak lint's, before it goes
        problem = ""
        if _status(product) != before:
            problem = "the blind run changed its read-only checkout of the card's work"
        elif checkout.worktree_tree(ws) != tree:
            problem = "the card's worktree changed while the blind run ran"
        return Ran(examples, code, text, problem, None, lines)
    finally:
        shutil.rmtree(snap, ignore_errors=True)
        shutil.rmtree(box, ignore_errors=True)


def _landed_base(conn, card):
    """The `start_sha` the card's tests were red-checked on (its latest `blind_landed`), or None."""
    row = conn.execute("SELECT json_extract(payload,'$.start_sha') FROM task_events WHERE task_id=?"
                       " AND kind='blind_landed' ORDER BY id DESC LIMIT 1", (card,)).fetchone()
    return row[0] if row else None


def _incident(conn, board, card, run_id, problem, *evidence):
    """The Korin incident of an inconclusive blind run. Fingerprint = check name + card (spec 4.4
    item 8): the role carries both, the problem text goes in the evidence (an id-free key would
    merge every card's incident)."""
    incidents.open_incident(conn, board=board, kind="review_infra", task_id=card, run_id=run_id,
                            key="blind_runner inconclusive", role=f"blind_runner:{card}",
                            evidence=[problem, *evidence])


def _missing(conn, card, commit, rows, examples):
    """The problem when the latest `blind_landed` event, for `commit`, lists a test (one with an
    example row) the run has no row for: a landed test with no row is not a pass. Else None. Not
    checked when a failing runner holder row (a non-pass row with no example row) is there: that
    run is a failure with a finding of its own, and a setup that failed is why rows are missing.
    A passing row with no example row does not switch the check off."""
    from capsule.blind import dispatch      # dispatch imports this module
    last = dispatch._landed(conn, card) or {}
    if last.get("blind_commit") != commit or any(
            r["id"] not in examples and r["result"] != "pass" for r in rows):
        return None
    gone = (set(last.get("tests") or ()) & set(examples)) - {r["id"] for r in rows}
    return f"the run has no row for {len(gone)} landed blind test(s)" if gone else None


def _anonymise(tests, examples, card):
    """A row with no example row is no test of the card's but a runner holder (`setUpClass
    (module.Class)`, an import failure's module name): rename it, so a finding names no file or
    class of the blind tests."""
    unknown = [t for t in tests if t["id"] not in examples]
    for number, test in enumerate(unknown, 1):
        test["id"] = f"{card}.setup-{number}"


def _recorded_commit(conn, card):
    """The blind repo commit recorded on the card when its tests landed (`tasks.blind_commit`), or
    "" when nothing landed for it (or the column holds anything but a full commit id)."""
    row = conn.execute("SELECT blind_commit FROM tasks WHERE id=?", (card,)).fetchone()
    value = row[0] if row else None
    return value if isinstance(value, str) and COMMIT_ID.match(value) else ""


def _read(path):
    """The text of a file the product could have written, or None when it is missing, not a
    regular file (a symlink or a FIFO the product planted) or over MAX_READ bytes. Never follows
    a link and never blocks on a pipe."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        return None
    with os.fdopen(fd, "rb") as handle:
        data = handle.read(MAX_READ + 1)
    return None if len(data) > MAX_READ else data.decode("utf-8", errors="replace")


def _status(ws):
    return workspace._git("status", "--porcelain", "--end-of-options", ws=ws).stdout


def _run_line(repo, commit, card):
    """The run line for this card: the safe one for the adapter `blind.toml` names as committed at
    `commit`. Nothing else from the file reaches the container, whatever its `run` line says."""
    shown = preflight.git(repo, "show", f"{commit}:{preflight.TOML_NAME}")
    adapter = tomllib.loads(shown.stdout).get("adapter")
    if not isinstance(adapter, str) or adapter not in preflight.RUN_LINES:
        raise ValueError(f"{preflight.TOML_NAME} at {commit[:12]}: adapter {adapter!r} has no"
                         " blind runner yet")
    return preflight.RUN_LINES[adapter][0].format(card=card)


def _fixed_time(member, path):
    """The `data` filter's member with the one fixed mtime: git archive stamps files with the
    commit's time, which would tell a red check's snapshot from a blind run's."""
    return tarfile.data_filter(member, path).replace(mtime=SNAP_MTIME)


def unpack(repo, commit, card, dest, mode=0o700):
    """`git archive <commit> cards/<card>` into the new directory `dest`, plus the harness copy.
    The tar is read with the `data` filter: no links out, no absolute paths, no device files; every
    file and folder of the card's `cards/<card>` gets the mtime `SNAP_MTIME`. The harness copy
    keeps its source mtimes (the same for every snapshot made on this host)."""
    dest.mkdir(mode=mode)
    tar = dest.parent / f"{dest.name}.tar"
    done = preflight.git(repo, "archive", "--format=tar", "-o", str(tar), commit, f"cards/{card}")
    if done.returncode:
        raise RuntimeError(f"git archive failed: {done.stderr.strip()}")
    try:
        with tarfile.open(tar) as archive:
            archive.extractall(dest, filter=_fixed_time)
    finally:
        tar.unlink(missing_ok=True)
    copy_harness(dest)


def copy_harness(dest):
    """The shared harness package as `dest/harness` (a fresh copy of the protected source)."""
    shutil.copytree(HARNESS_SRC, dest / "harness", ignore=shutil.ignore_patterns("__pycache__"))


def _snapshot(repo, commit, card, snap):
    """The card's tests at `commit` and the harness in `snap`; returns the card's example rows by
    test id."""
    unpack(repo, commit, card, snap)
    return _examples(snap / "cards" / card / "examples.jsonl")


def _examples(path):
    """{test id: example row} from the card's examples.jsonl; unreadable lines are skipped."""
    found = {}
    for line in (_read(path) or "").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and isinstance(row.get("id"), str):
            found[row["id"]] = row
    return found


@contextlib.contextmanager
def container(strong, run_id, ws, snap, out, watch=None, git=True, **more):
    """The blind run's strong container (the card worktree read-only, `/blind:ro`, `/out`),
    stopped on exit; yields its name. The red check (`red.py`) and the lander run this same
    shape. `watch` is told the name once the container is up (and may raise to have it stopped at
    once). `git` false: the clone at `ws` holds its own `.git` (the lander's), so no separate git
    dir is mounted; `more` goes to `strong.start` (the lander's `lifetime`)."""
    gitdir = str(workspace.gitdir(ws)) if git else None
    name = strong.start(run_id, str(ws), git_dir=gitdir, tmpdir=docker.CONTRACT_TMPDIR, ro=True,
                        allow_blind=(snap, out, str(ws), *([gitdir] if git else [])),
                        mounts=(f"{snap}:/blind:ro", f"{out}:/out"), **more)
    try:
        if watch:
            watch(name)
        yield name
    finally:
        _stop_quietly(strong, name)


def execute(strong, name, run_line, timeout=BLIND_TIMEOUT):
    """(exit code, stdout+stderr) of the run line in the container `name`; a timeout is exit 124."""
    try:
        return strong.exec(name, run_line, timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"


def is_mac(task):
    """True for a card on the Mac lane (D305): its blind suite runs on the host, never in docker."""
    return dict(task).get("tier") == "mac"


def _on_host(plan, ws, snap, out, run_line, pending):
    """(exit code, stdout+stderr) of the run line on the host under the Mac profile (B6), inside
    the Mac lane's one-job lock the job already holds (`pending.lane`): a blind run never waits
    for the lane on a pool thread."""
    from capsule.runners import mac
    pending.check()
    if pending.lane is None:
        raise RuntimeError("a mac blind run needs the Mac lane lock its job holds")
    return mac.run_blind(pending.lane, ws, snap, out, run_line, BLIND_TIMEOUT, RESULTS_FILE,
                         pending.stopped)


def _in_container(strong, run_id, ws, snap, out, run_line, watch=None):
    """(exit code, stdout+stderr) of the run line in the second strong container."""
    with container(strong, run_id, ws, snap, out, watch) as name:
        return execute(strong, name, run_line)


def _result_rows(text):
    """The rows of results.jsonl, or (None, why)."""
    rows = []
    for number, line in enumerate((text or "").splitlines(), 1):
        try:
            row = json.loads(line)
        except ValueError:
            return None, f"results.jsonl line {number} is not JSON"
        ok = (isinstance(row, dict) and isinstance(row.get("id"), str) and row["id"]
              and row.get("result") in EXIT_OF)
        observed = row.get("observed") if ok else None
        if not ok or (observed is not None and not (isinstance(observed, dict) and all(
                type(observed.get(k)) is v for k, v in OBSERVED.items()))):
            return None, f"results.jsonl line {number} is not a result row"
        rows.append(row)
    ids = [r["id"] for r in rows]
    return (rows, None) if len(set(ids)) == len(ids) else (None, "results.jsonl repeats a test id")


def _forged(rows, status):
    """True when results.jsonl has more fail and error rows than the summary line counts (its
    `failures=`, `errors=` and `unexpected successes=`): an exit hook in the code under test can
    append rows, but cannot change what the runner printed."""
    said = sum(int(n) for _, n in COUNTS.findall(status))
    return sum(r["result"] in ("fail", "error") for r in rows) > said


def _summary_line(text):
    """unittest's own final line of the runner's `text` (the last line that starts like one, whole
    and short: `SUMMARY`), or "" when there is none."""
    raw = next((x for x in reversed(text.splitlines()) if x.startswith(STATUS)), "")
    line = raw.strip()      # trailing blanks are fine; the RAW length is what is capped
    return line if len(raw) <= SUMMARY_MAX and SUMMARY.fullmatch(line) else ""


def _parse(results, text, code):
    """(rows, problem): the two oracles of spec 4.2. `results` is results.jsonl (oracle 1);
    `text` and `code` are the runner's stdout and exit code (oracle 2). `problem` is None only when
    they agree on: the test count (`Ran N` plus holder rows, which are never passes), the verdict
    (a fail/error row iff `FAILED` iff a non-zero exit) and the skip count."""
    if results is None:
        return [], (f"no usable {RESULTS_FILE} (missing, not a regular file or over 1 MiB;"
                    f" exit code {code})")
    rows, why = _result_rows(results)
    if rows is None:
        return [], why
    ran, status = RAN_LINE.search(text), _summary_line(text)
    if ran is None or not status.startswith(("OK", "FAILED")):
        return rows, f"the runner printed no usable summary (exit code {code})"
    if not rows:                    # `Ran 0 tests` / OK for a card with tests: nothing was judged
        return rows, "the run reported no tests for a card that has blind tests"
    bad = [r for r in rows if r["result"] in ("fail", "error")]
    verdicts = (bool(bad), status.startswith("FAILED"), code != 0)
    holders = len(rows) - int(ran[1])
    skips = SKIPPED.search(status)
    if len(set(verdicts)) > 1:
        return rows, ("results.jsonl, the summary line and the exit code disagree: rows say"
                      f" {'fail' if verdicts[0] else 'pass'}, summary says"
                      f" {'FAILED' if verdicts[1] else 'OK'}, exit code {code}")
    if not 0 <= holders <= sum(r["result"] != "pass" for r in rows):
        return rows, f"results.jsonl has {len(rows)} rows but the summary says: Ran {ran[1]}"
    if _forged(rows, status):
        return rows, f"results.jsonl has more fail and error rows ({len(bad)}) than the summary"
    if sum(r["result"] == "skipped" for r in rows) > (int(skips[1]) if skips else 0):
        return rows, "results.jsonl has more skipped rows than the summary says"
    return rows, None


def _tests(rows, examples):
    """The `tests` array of blind_result.v1: each row joined to its example (behaviour words)."""
    tests = []
    for row in rows:
        ex = examples.get(row["id"], {})
        test = {"id": row["id"], "result": row["result"]}
        for key in ("source", "given", "expect"):
            value = ex.get(key)
            test[key] = NO_EXAMPLE if not value else value if isinstance(value, str) \
                else json.dumps(value)
        tests.append({**test, **({"observed": row["observed"]} if row.get("observed") else {})})
    return tests


def _render(envelope, schema, rows, problem, examples):
    """(blind_result.v1 document, problem). `red` is the base-commit red check, which this run does
    not perform: `not-run` when the oracles agree, `inconclusive` (no tests listed) when they do
    not. The head verdict lives in `counts` and each `tests[].result`. A document that would break
    the schema (an older schema without `not-run`, say) becomes an inconclusive one."""
    counts = {k: sum(r["result"] == k for r in rows) for k in EXIT_OF}
    red = "inconclusive" if problem else "not-run"
    tests = [] if problem else _tests(rows, examples)
    doc = {**envelope, "red": red, "counts": counts, "tests": tests}
    errors = jsonschema.validate(doc, schema)
    if errors and not problem:
        return _render(envelope, schema, rows, f"the rendered result broke {SCHEMA}", examples)
    return doc, problem


def _facts(test, leaked=False):
    """The id, given, expect and observed (as text) of one blind_result test: a finding's all."""
    obs = test.get("observed")
    seen = (f"{obs['cmd']} -> exit {obs['exit']}\n{obs['stdout_tail']}\n{obs['stderr_tail']}"
            if obs else "(the test ran no command)")
    gone = WITHHELD if leaked else None
    return {"id": test["id"], "given": gone or test["given"], "expect": gone or test["expect"],
            "observed": gone or seen}


def _test_row(test, mode=MODE, leaked=False):
    facts = _facts(test, leaked)
    text = f"given: {facts['given']}\nexpect: {facts['expect']}\nobserved: {facts['observed']}"
    row = _row(f"blind:{test['id']}", EXIT_OF[test["result"]], text, mode)
    if mode != "gate":
        return row
    row["result"] = test["result"]
    if row["exit"]:             # the one finding the implementer gets (`gate.failures`)
        row.update(test=facts, **({"leaked": True} if leaked else {}))
    return row


def _lines(snap, card):
    """The stripped lines of every regular file of the card's own tests and examples
    (`cards/<card>`; the harness copy is shared code a traceback may show) that are MIN_LINE
    characters or longer and are no import line: what the leak lint looks for."""
    found = set()
    for root, _, names in os.walk(snap / "cards" / card):
        for name in names:
            text = _read(os.path.join(root, name)) or ""
            found.update(s for s in map(str.strip, text.splitlines())
                         if len(s) >= MIN_LINE and not IMPORT.fullmatch(s))
    return found


def _scrub(tests, lines):
    """The leak lint (blind spec 4.4): the ids of failing tests whose finding holds a whole line
    of the snapshot (`lines`; none outside gate mode). Each loses its given, expect and observed
    in `tests` (the rendered result); `_test_row` withholds them from validator and implementer."""
    leaked = set()
    for test in filter(lambda t: lines and EXIT_OF[t["result"]], tests):
        finding = review_findings.blind_finding(_facts(test))
        if any(line in finding for line in lines):
            leaked.add(test["id"])
            test.update(given=WITHHELD, expect=WITHHELD)
            test.pop("observed", None)
    return leaked
