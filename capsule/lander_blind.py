"""The lander's blind run (R35, blind spec B6): in `gate` mode a card lands only on a green run of
its blind tests against the exact commit about to be pushed. Main can move between the validator's
run and the landing, so the lander runs them again, in its throwaway clone, as one more check
(`blind`) beside the suites: `lander.run` calls `plan` once, then `check`.

`plan` reads the card (it writes only through `gate._waived`'s own override events): `redesign.
blind_tests` other than `gate`, no conn, or a commit already on main plan nothing and the result is
what it was before. A card with a recorded `blind_commit` runs; a card with none that would get a
tester (`run.gets_tester`) is held with `no blind tests yet` unless a person's release stands
(`gate._waived`, same cause as the validator's hold), and then lands with the row saying it landed
without blind tests; any other card lands as before.

The run reuses the blind run's own parts (`blind/run.py`): the snapshot of the card's tests at the
RECORDED `blind_commit` (never the blind repo's head), the same container shape (worktree
read-only, `/blind:ro`, `/out`, no network, no socket: `run.container`, here with a `lifetime` so a
killed lander leaves nothing running; the mount audit still runs and exempts only what `allow_blind`
names: the mount paths (snapshot, output folder) and the read-only clone, `str(ws)`) and the two
oracles (`run._parse`: results.jsonl against the runner's own summary and exit code), plus
`_missing` and the worktree-unchanged check (`git status` and HEAD before and
after). The clone is mounted read-only, so what the tests write cannot change what lands: the
lander pushes the commit it tested. No text of the runner or a test is ever kept: the
rows, the hold reason, the events and the incidents hold ids, counts and fixed phrases.

Outcomes: all pass = pass. A failing test = `hold` (never red: the implementer gets nothing), rows
`blind:<id>` that name ids only, and an incident; the failing rows are kept whatever else went
wrong (a timeout after one is still a failing run, and never released). A run that cannot be
trusted (a timeout, no tests run, a landed test with no result, oracles that disagree, a status
line that is not unittest's own, a changed worktree, a run line that cannot be built) = `hold`
(`blind tests inconclusive`) and the validator's own `blind_runner` incident; a skipped test =
`hold` (`a blind test was skipped`) and the same incident. A hold's `cause` is the validator's own
(`cause`), so a person's release given at either side is honoured at the other (`_causes`): an
inconclusive release lifts an inconclusive or a skipped run; a skip release lifts either only
when the run finished (`_finished`: a `Ran` line, a summary, a passing row, agreeing verdicts, no
gap between the rows and `Ran` the summary's `skipped=N` does not explain, no landed test without
a row, an untouched worktree; never a timeout, a late run or all tests skipped); a release works
for one landing attempt (`_spent`). A mount audit hit on a lander container = `hold` and the
worker's own incident (`audit_hold`). A mac card's run (D305) is never in docker: `_on_host` runs
it on the host under the Mac lane profile (`mac.run_blind`, the clone read-only, inside the lock
the lander's own mac session already holds: it is never taken twice) and a lane that is off, down
or busy is a wait whose text is the lane's own reason. A wait (nothing is judged, no incident,
nothing counts against the card): docker down, no image, a container that did not start (a
RuntimeError that is not a mount audit refusal), a container gone before the first test or mid-run
(not alive, the daemon up or not: nothing is exec'd in it), a git or system timeout, or less than
a second for the run (`lander.run` sizes the deadline; the run gets the least of the timeout, the
blind limit and what is left). An unusable blind repo = `hold`, no incident. The snapshot is
removed in a `finally`. Every hold names the verbs that lift it (`exits`, the whole `amend`
command included), and `unblock` is not
one of them. Nothing here retries by itself.
"""
import collections
import secrets
import shutil
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

from capsule import config, incident_blind, incidents
from capsule.blind import gate, preflight
from capsule.blind import run as blind_run
from capsule.runners import mac, sandbox
from capsule.runners.docker import BLIND_CHECK, EngineDown, MissingImage

PENDING = blind_run.PENDING             # `no blind tests yet`
UNAVAILABLE = "blind tests unavailable"
INCONCLUSIVE = "blind tests inconclusive"
FAILED = "blind tests failed"
SKIPPED = blind_run.SKIPPED_REASON      # `a blind test was skipped`: the validator's own phrase
AUDIT = "mount audit hit: a lander container held blind material"
MARK = "a person's release"             # in every landing row that went ahead under a release
WAIVED = f"landed without blind tests: {MARK} stands"
TEST_ROW = "failed at land time"        # the detail of a `blind:<id>` row: no test text
RELEASED = f"landed under {MARK} for: "     # then the reason that was released
RELEASABLE = (PENDING, INCONCLUSIVE, SKIPPED)    # the reasons a person's release can lift
GONE = "the blind container is gone: the run has no verdict"
MOVED = "the blind run changed the lander's worktree (it is mounted read-only)"
# The ask of an after-push verify the mount audit refused (`{sha}`, `{card}`): what is unverified
# and the whole command that tries it again, with the warning that a red result reverts by itself.
VERIFY_ASK = ("{sha} is pushed and NOT verified (mount audit: see the incident). Verify again: "
              "`python3 -m capsule.landing verify --task {card} --db <db> --repo <repo> "
              "--release <release> --config <config>`. A RED result pushes a revert to main by "
              "itself.")
WAIT_NEXT = ("nothing retries this by itself: fix the cause, then a person sends the card on "
             "again")
# kind: run|hold|waived
Plan = collections.namedtuple("Plan", "kind card commit reason failed task")


def exits(board, card, accept=False):
    """The verbs that lift a hold, as the hold's text names them (a plain `unblock` re-dispatches
    the implementer and no more). `amend` is the whole command, as the validator's hold names it:
    it needs a reason and a body file. `accept` (a release) only for a reason one can lift."""
    amend = (f"`capsule human {board} {card} amend '<reason>' --body-file <path>` (a new text "
             "for the card, then `reset`: a new blind tester runs on it)")
    if accept:
        return ("to go on a person does one of: `accept` it as the owner "
                "with the id of an open fix "
                f"card in the text (it then lands once, without this check), or {amend}, or "
                "`close` the card")
    return ("to go on a person does one of: `reset` (a new implementer round; it helps only when "
            "the code is at fault: when the card's text or its blind tests are at fault `reset` "
            f"alone repeats this, `amend` the text first), or {amend}, or `close` the card")


def plan(conn, cfg, task_id, on_main):
    """None when the landing has no blind step (not `gate`, no conn, the commit is already on main,
    no such card, or a card that would get no tester and has no tests), else a `Plan`."""
    if conn is None or on_main or config.redesign(cfg)["blind_tests"] != "gate":
        return None
    task = conn.execute("SELECT id, body, tier, branch_name FROM tasks WHERE id=?",
                        (task_id,)).fetchone()
    if task is None:
        return None
    commit = blind_run._recorded_commit(conn, task_id)
    if commit and not blind_run._stale(conn, task):
        return Plan("run", task_id, commit, "", [], task)
    if not (commit or blind_run.gets_tester(cfg, task)):
        return None
    if _released(conn, cfg, task, PENDING):
        return Plan("waived", task_id, commit, "", [], task)
    return Plan("hold", task_id, commit, PENDING, [], task)


def cause(task, reason=PENDING):
    """The `cause` of a hold for `reason`, the validator's own (`gate.hold`): a person's `accept`
    after it waives the hold (`gate._waived`); tests landing release a `no blind tests yet`."""
    return f"{gate.CAUSE}:{gate._digest(task)}:{reason}"


def _spent(conn, task_id):
    """True when a landing already went ahead under a person's release since their latest
    `accept`: a release works for one landing attempt, so a second landing never reuses it."""
    return conn.execute(
        "SELECT 1 FROM task_events WHERE task_id=? AND kind='landing_checked' AND payload LIKE ?"
        " AND id>(SELECT COALESCE(MAX(id), 0) FROM task_events WHERE task_id=? AND"
        " kind='intervention' AND json_extract(payload,'$.kind')='accept')",
        (task_id, f"%{MARK}%", task_id)).fetchone() is not None


def _causes(reason, finished):
    """The reasons whose release lifts a hold on `reason`. A hold on an inconclusive or a skipped
    run is lifted by an inconclusive release always, and by a skip release only when the run
    `finished` (`_finished`): a skip release never lands a run that has no verdict. Any other
    reason is lifted by its own release only."""
    if reason not in (INCONCLUSIVE, SKIPPED):
        return (reason,)
    return (INCONCLUSIVE, SKIPPED) if finished else (INCONCLUSIVE,)


def _released(conn, cfg, task, reason, finished=False):
    """True when a person's live release for a hold on `reason` stands (the rule is
    `gate._waived`'s: a person's accept, the same reason, unexpired, unused) and no landing has
    gone ahead under it yet (`_spent`). `reason` is the validator's own phrase for it, so a
    release given at either side is honoured at the other, within `_causes`."""
    return not _spent(conn, task["id"]) and any(
        gate._waived(conn, cfg, task, cause(task, r), int(time.time()))
        for r in _causes(reason, finished))


def _finished(rows, text, code):
    """True when the run ended by itself and can be trusted as a verdict: a `Ran` line, a
    summary, at least one passing row, the three verdicts agree (a fail or error row iff a
    `FAILED` summary iff a non-zero exit) and any gap between the rows and `Ran N` is explained
    by the summary's `skipped=N`. A timeout (124), no rows, only skipped rows or no summary is
    not finished."""
    status, ran = blind_run._summary_line(text), blind_run.RAN_LINE.search(text)
    if not (rows and ran and code != 124 and status.startswith(("OK", "FAILED"))):
        return False
    skips = blind_run.SKIPPED.search(status)
    verdicts = {any(blind_run.EXIT_OF[r["result"]] for r in rows), status.startswith("FAILED"),
                code != 0}
    return (len(verdicts) == 1 and any(r["result"] == "pass" for r in rows)
            and abs(len(rows) - int(ran[1])) <= (int(skips[1]) if skips else 0))


def _state(path):
    """The clone's `git status --porcelain` and HEAD: equal before and after, or it was changed."""
    return (preflight.git(path, "status", "--porcelain").stdout,
            preflight.git(path, "rev-parse", "HEAD").stdout)


def _outcome(rows, problem, examples, card):
    """(status, detail, failed ids) of a parsed run: an oracle problem is a hold (`inconclusive`),
    a skipped test is a hold (`SKIPPED`), a failing test is a hold naming ids only. The failing
    ids are kept whatever else is wrong with the run (a timeout after a failing row is still a
    failing run: it is never released)."""
    bad = [{"id": r["id"], "result": r["result"]} for r in rows if blind_run.EXIT_OF[r["result"]]]
    blind_run._anonymise(bad, examples, card)
    failed = [t["id"] for t in bad]
    if problem:
        return "hold", f"{INCONCLUSIVE}: {problem}", failed
    if bad:
        ids = ", ".join(f"blind:{i}" for i in failed)
        return "hold", f"{FAILED}: {len(bad)} of {len(rows)} failed at land time ({ids})", failed
    if any(r["result"] == "skipped" for r in rows):
        return "hold", SKIPPED, failed
    return "pass", f"{len(rows)} blind test(s) passed on the commit to push", failed


def _on_host(cfg, home, lane, ws, snap, out, run_line, seconds):
    """(exit code, text) of a mac card's run on the host (`mac.run_blind`: the Mac lane profile,
    the clone read-only), or (None, a `mac.LaneWait`) when the lane cannot run it now. `lane`, the
    lander's own mac session, holds the lane lock: the run uses it and never takes the lane again
    (no `lane`: it is taken once here)."""
    args = (ws, snap, out, run_line, seconds, blind_run.RESULTS_FILE)
    if lane is not None and lane.lane:
        return None, lane.lane
    why = None if lane is not None else mac.unavailable(config.redesign(cfg)["mac_runner"])
    try:
        if lane is not None:
            return mac.run_blind(lane.lock, *args)
        return _take_once(home, why, args)
    except RuntimeError as exc:             # no Xcode after all: the lane's reason, not an audit
        reason = mac.LaneWait(str(exc))
        if lane is not None:
            lane.lane = reason              # the lander's result says the lane waited
        return None, reason


def _take_once(home, why, args):
    """`_on_host` with no lander session: the lane is taken here, once (or `why` it cannot be)."""
    with mac.lane(home) as held:
        if why or held is None:
            return None, mac.LaneWait(why or mac.BUSY)
        return mac.run_blind(held, *args)


def _execute(runner, ws, snap, out, run_line, seconds):
    """(exit code, text), or (None, why) when the sandbox is unavailable: docker down, no image, a
    failed start, an engine that is down or did not start the container, or one gone with the
    daemon (a failed stop after the commands is best effort, never a wait). A timeout is
    (124, ...). A mount audit refusal (a RuntimeError) is raised for `audit_hold`."""
    if not runner.available():
        return None, "docker is not available"
    try:
        with blind_run.container(runner, secrets.token_hex(6), ws, snap, out, git=False,
                                 lifetime=seconds + sandbox.MARGIN) as name:
            return _exec(runner, name, run_line, seconds)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, f"the blind container did not start ({type(exc).__name__})"
    except EngineDown as exc:
        return None, f"the blind container did not start: docker engine down ({exc})"
    except MissingImage:
        raise            # `check` holds it: no stack image is no docker wait
    except RuntimeError as exc:
        if incident_blind.mount_path({"error": f"RuntimeError: {exc}"}):
            raise
        return None, "the blind container did not start (RuntimeError)"


def _exec(runner, name, run_line, seconds):
    """(code, text) of the run, or (None, why) when it failed because the container is gone (docker
    restarted or the daemon was killed under it): that is no verdict on the card, so a wait."""
    if not runner.alive(name):       # gone before the first test: nothing is exec'd in it
        return None, GONE
    code, text = blind_run.execute(runner, name, run_line, seconds)
    if code != 0 and not runner.alive(name):
        return None, GONE
    return code, text


def _judge(plan_, conn, cfg, work, runner, seconds, tmp, home=None, lane=None):
    """(status, detail, failed ids, finished) of one run with its snapshot and output folder under
    `tmp`; `finished` is `_finished` of the run, with the worktree left alone and no landed test
    missing a row."""
    card, commit = plan_.card, plan_.commit
    repo = preflight.expand(config.blind_tests(cfg)["repo"])
    snap, out = tmp / blind_run.SNAP_DIR, tmp / blind_run.OUT_DIR
    out.mkdir(mode=0o700)
    try:
        examples = blind_run._snapshot(repo, commit, card, snap)
    except (RuntimeError, tarfile.TarError) as exc:
        return "hold", (f"{UNAVAILABLE}: the card's tests at {commit[:12]} could not be read "
                        f"({type(exc).__name__})"), [], False
    try:
        run_line = blind_run._run_line(repo, commit, card)
    except (KeyError, IndexError, ValueError, TypeError, AttributeError):
        return "hold", (f"{INCONCLUSIVE}: no blind run line could be built from blind.toml at "
                        f"{commit[:12]} (unreadable, no usable `run` entry or a bad adapter)"
                        ), [], False
    before = _state(work.path)
    if blind_run.is_mac(plan_.task):     # a mac card's suite is never run in docker (D305)
        code, text = _on_host(cfg, home, lane, work.path, snap, out, run_line, seconds)
    else:
        code, text = _execute(runner or sandbox.runner_for(cfg), work.path, snap, out, run_line,
                              seconds)
    if code is None:
        return "wait", text, [], False
    rows, problem = blind_run._parse(blind_run._read(out / blind_run.RESULTS_FILE), text, code)
    moved = _state(work.path) != before          # checked whatever else is wrong with the run
    missing = blind_run._missing(conn, card, commit, rows, examples)
    problem = problem or (MOVED if moved else None) or missing
    finished = not (moved or missing) and _finished(rows, text, code)
    return (*_outcome(rows, problem, examples, card), finished)


def _blind(plan_, conn, cfg, work, runner, seconds, home=None, lane=None):
    """(status, detail) of one run, the failing ids left in `plan_.failed`; the incident of a run
    that broke or failed is opened here."""
    board = cfg["board"]
    problems = preflight.problems(cfg, board, preflight.worker_mounts(cfg, board, None))
    if problems:
        return "hold", f"{UNAVAILABLE}: " + "; ".join(problems)
    tmp = Path(tempfile.mkdtemp(prefix="lander-blind-"))
    try:
        status, detail, plan_.failed[:], finished = _judge(plan_, conn, cfg, work, runner,
                                                           seconds, tmp, home, lane)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    card, ids, reason = plan_.card, ",".join(sorted(plan_.failed)), detail.split(": ")[0]
    if plan_.failed:
        _incident(conn, cfg, card, f"blind test failed at land time: {ids}", f"blind_land:{card}",
                  [f"blind tests failed at land time: {ids}", plan_.commit])
    if status == "hold" and reason in (INCONCLUSIVE, SKIPPED):
        _incident(conn, cfg, card, "blind_runner inconclusive", f"blind_runner:{card}",
                  [detail, plan_.commit])
        if not plan_.failed and _released(conn, cfg, plan_.task, reason, finished):   # no fail
            return "pass", RELEASED + reason
    return status, detail


def _incident(conn, cfg, card, key, role, evidence):
    incidents.open_incident(conn, board=cfg["board"], kind="review_infra", task_id=card,
                            key=key, role=role, evidence=evidence)


def audit_hold(conn, cfg, card, exc):
    """(status, detail) of the RuntimeError `exc` the mount audit raised when a lander container
    held blind material: a hold and the worker's own incident (check name and card: one
    fingerprint). Any other error, or no conn, is raised again."""
    if conn is None or not incident_blind.mount_path({"error": f"RuntimeError: {exc}"}):
        raise exc
    _incident(conn, cfg, card, BLIND_CHECK, f"{BLIND_CHECK}:{card}", [AUDIT])
    return "hold", AUDIT


def check(plan_, conn, cfg, work, *, deadline, timeout, runner=None, home=None,
          lane=None):
    """(status, detail) of the `blind` check; the failing ids are left in `plan_.failed`. The run
    gets the least of `timeout`, the blind run's own limit and what is left before `deadline`."""
    if plan_.kind != "run":
        return ("hold", plan_.reason) if plan_.kind == "hold" else ("pass", WAIVED)
    seconds = int(min(timeout, blind_run.BLIND_TIMEOUT, deadline - time.monotonic()))
    if seconds < 1:
        return "wait", "less than a second is left for the blind run: no verdict was tried"
    try:
        return _blind(plan_, conn, cfg, work, runner, seconds, home, lane)
    except MissingImage as exc:        # a hold with the build remedy; no wait, no engine wake
        return "hold", f"{UNAVAILABLE}: {exc}"
    except RuntimeError as exc:
        return audit_hold(conn, cfg, plan_.card, exc)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "wait", f"the blind run could not finish ({type(exc).__name__})"


def step(go, rows, blind, conn, cfg, work, timeout, spent, runner, home, lane):
    """The `blind` row (`check`), then one `blind:<id>` hold per failed test (ids). `go` is the
    lander's row maker, `lane` its first mac session (see `check`)."""
    go("blind", check, blind, conn, cfg, work, timeout=timeout, deadline=spent + timeout,
       runner=runner or sandbox.runner_for(cfg, home), home=home, lane=lane)
    rows.extend({"name": f"blind:{i}", "status": "hold", "detail": TEST_ROW} for i in blind.failed)
