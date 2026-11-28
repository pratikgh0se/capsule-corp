"""Red check and landing for blind tests (blind spec 3.4; R31). Blind tests only count if they stay
in their folder and fail on the code from before the work; those that do land on the blind repo's
`main`, which only this host code moves. The full description is
docs/architecture/components/blind-path.md (red check and landing).

When a `blind-tester` run has ended with a valid report, `step` (called once a pass by
`dispatch.step`; `redesign.blind_tests` `off` changes nothing) stops the tester's container, then
takes that round through: the lint (`lint.py`); a red check, 5 runs of the staged commit's tests
against the card's base (the earliest `start_sha` of its runs), each run in its own fresh strong
container shaped like the blind run (`run.py`), each test getting `ok`, `passes-on-base`,
`errors-before-first-call` or `inconclusive`, names and outcomes only going back to the tester;
then landing: host git
fast-forwards `main` to exactly the commit that was checked, sets `tasks.blind_commit` and emits
`blind_landed`. A round that does not land is bounced or triaged (`settle.py`). A runner failure is
no tester round: it is tried again on a later pass and the third triages the card. One card's red
check, clone included, is held to `RED_BUDGET` seconds of the tick pass, give or take the calls of
30 s or less that are not cut (about 210 s over at worst: 30 + 90 + 90); at most one runs a pass.

`red_exempt`: a test whose full id is in the card's own `HEADER.json` `red_exempt` list (written by
the tester, linted) AND whose statement an applied guide ruling names (`_exempt_statements`) may
pass on base and counts as `ok`. Nothing else is exempt: not a test the tester only labels as
covering that statement, not a test a ruling names without the list. A listed test that passes on
base under a statement of the card no ruling names yet files one guide ruling request for the round
(`exempt.py`); the round waits for it and is checked again once the card is back."""
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from capsule import (bundles, config, contradiction, incident_blind, incidents, intent, outbox,
                     paths, spool, workspace)
from capsule.blind import checkout, exempt, gate, lint, preflight, settle
from capsule.blind import run as blind_run
from capsule.db import event, transition, txn
from capsule.jsonschema import load_all
from capsule.runners import docker

ROLE = config.BLIND_TESTER
RUNS = 5                    # red-check runs per test (C1: new tests rerun 5 times)
MAX_RUNNER_FAILURES = 3     # runner failures of one round before the card is triaged
RED_BUDGET = 300            # seconds of the tick pass one card's red check may take
OK, PASSES, EARLY, MIXED = "ok", "passes-on-base", "errors-before-first-call", "inconclusive"
_STATEMENT = re.compile(r"statement[\s_-]*(\d+)", re.I)
_KINDS = {"pass": PASSES, "red": OK, "early": EARLY}
MARKER = "red-check"       # empty file in a red run's spool folder: the sweep's proof it is ours
LOST = (124, 126, 127)     # exit codes of a command that timed out or never ran


def base_sha(conn, task_id):
    """The card's base: the earliest `start_sha` among all its runs, any profile (the tester
    records the epic head at dispatch, the implementer its own pinned start), or None."""
    row = conn.execute("SELECT json_extract(metadata,'$.start_sha') FROM task_runs WHERE task_id=?"
                       " AND json_extract(metadata,'$.start_sha') IS NOT NULL ORDER BY id LIMIT 1",
                       (task_id,)).fetchone()
    return row[0] if row else None


def classify(runs, exempt=()):
    """{test id: outcome} from the red check's `runs`: one {test id: result row} per run of the
    suite on the base (rows as `harness` writes them: `result` and, once the test called the
    product, `observed`). `ok` needs the same red every run: an assertion that failed after the
    first `run()` call, on a command that ran (exit not 124, 126 or 127: those mean it timed out
    or was not there). A fail or error before any `run()` call is `errors-before-first-call`; all
    passes is `passes-on-base` (`ok` for a test whose full id is in `exempt`); anything else
    (an error after a call, mixed, skipped, missing from a run) is `inconclusive`."""
    def kind(row):
        if row is None or row["result"] == "skipped":
            return "mixed"
        if row["result"] == "pass":
            return "pass"
        seen = row.get("observed")
        if not seen:
            return "early"
        return "red" if row["result"] == "fail" and seen.get("exit") not in LOST else "mixed"
    out = {}
    for test in sorted(set().union(*runs) if runs else ()):
        kinds = {kind(r.get(test)) for r in runs}
        out[test] = MIXED if len(kinds) != 1 or kinds == {"mixed"} else _KINDS[kinds.pop()]
        if out[test] == PASSES and test in exempt:
            out[test] = OK
    return out


def _exempt_statements(conn, task_id):
    """The acceptance statements a guide ruling exempts from the red check: an applied
    `guide_ruling` event of role `blind-tester` and choice `red-exempt` names each as `statement
    N` in its `examples`, `reason` or `fingerprint`. A statement named nowhere is never exempt. A
    ruling counts only while the card's text has the `card_hash` it carries (any change to
    the card's text voids it, not only to its acceptance); none: it counts for nothing."""
    found = set()
    body = conn.execute("SELECT body FROM tasks WHERE id=?", (task_id,)).fetchone()
    now_hash = bundles.card_hash(body[0] if body else "")
    for (raw,) in conn.execute("SELECT payload FROM task_events WHERE task_id=? AND"
                               " kind='guide_ruling'", (task_id,)):
        p = json.loads(raw or "{}")
        if (p.get("role") == ROLE and p.get("choice") == "red-exempt" and p.get("applied")
                and p.get("card_hash") == now_hash):
            text = " ".join(str(p.get(k) or "") for k in ("examples", "reason", "fingerprint"))
            found |= {int(n) for n in _STATEMENT.findall(text)}
    return found


def step(tick, conn, cfg, now, counts):
    """Once a pass: triage the cards whose triage waited for a running implementer, then take
    each finished, unprocessed tester round through lint, the red check and landing. Runs nothing
    with `redesign.blind_tests` off; stops after the first round that ran a red check (its
    container time is the pass's)."""
    if config.redesign(cfg)["blind_tests"] == "off":
        return
    _sweep(tick, cfg)
    for held in settle.waiting(conn):
        tick._guarded(conn, cfg, held["task_id"], now, counts,
                      lambda held=held: settle.release(conn, held, now))
    rows = settle.unsettled(conn)
    for run in (r for r in rows if not exempt.waiting(conn, r)):
        ran = []
        tick._guarded(conn, cfg, run["task_id"], now, counts,
                      lambda run=run: ran.append(_round(tick, conn, cfg, run, now)))
        if any(ran):
            return


def _acceptance(body):
    from capsule.blind import dispatch      # dispatch imports this module
    return dispatch.acceptance(body)


def _round(tick, conn, cfg, run, now):
    """One finished tester round; True when a red check container ran. The tester's container is
    stopped first, whatever the round turns out to be: nothing of it may run on."""
    blind_run._stop_quietly(tick.strong, docker.container_name(run["run_id"]))
    task = conn.execute("SELECT * FROM tasks WHERE id=?", (run["task_id"],)).fetchone()
    meta = json.loads(run["metadata"] or "{}")
    now_hash = bundles.card_hash(task["body"])
    clone = Path(meta.get("blind_clone") or "/nonexistent")
    if task["status"] in ("done", "archived") or meta.get("card_hash") != now_hash \
            or not clone.is_dir():
        settle.record(conn, run, "stale", now, card_hash=meta.get("card_hash"))
        settle.cleanup(clone)
        return False
    try:
        return _attempt(tick, conn, cfg, run, task, clone, now_hash, now)
    except Exception as exc:        # a runner failure is not a tester round
        blind = incident_blind.roots(tick._spool_root(cfg), cfg)
        _retry(conn, cfg, run, task, incident_blind.why(exc, task["id"], blind), now)
        return True


def _attempt(tick, conn, cfg, run, task, clone, now_hash, now):
    """Collect and lint the tester's files (its container is stopped), then the red check. True
    when a red check container ran."""
    files, found = lint.collect(clone, task["id"])
    found += lint.problems(files, task["id"], now_hash) if files and not found else []
    if found or not files:
        found = found or ["the tester wrote no tests"]
        return settle.bounce(conn, run, task, now_hash, {}, found, "", now)
    return _check(tick, conn, cfg, run, task, files, now_hash, now)


def _disagree(conn, cfg, run, task, starts, theirs, now):
    """Spec A2.3: the base the red check would use (or the tester's own) and the implementer's
    differ, so the red check is not run: `inconclusive` and a Korin incident (`review_infra`,
    fingerprint check name + card). Nothing lands and no round is counted."""
    why = (f"the tester's base {'/'.join(sorted(x[:12] for x in starts))} and the implementer's"
           f" {theirs[:12]} differ")
    settle.record(conn, run, "inconclusive", now, reason=why)
    incidents.open_incident(conn, board=cfg["board"], kind="review_infra", task_id=task["id"],
                            run_id=run["run_id"], key="blind_red base disagreement",
                            role=f"blind_red:{task['id']}", evidence=[why])
    settle.cleanup(Path(json.loads(run["metadata"])["blind_clone"]))
    return False


def _unrun(files, card, covered, outcomes, landed):
    """Test ids the example rows, the tester's report or the landed tests of an earlier round name
    that did not run (a `load_tests` hook can hide tests; a rerun can delete landed ones): full
    ids, as the harness ran them."""
    named = set(lint.examples(files, card)[0]) | {t for c in covered for t in c["tests"]}
    return sorted((named | set(landed)) - set(outcomes))


def _unrun_reason(card, test):
    """The plain bounce reason for a named test that did not run: a bare method name (no card id
    and dot) says what the full id is, as the contract does; any other name says it did not run."""
    if "." not in test:
        return (f"test {test} is a bare name: write its full id {card}.{test} in the examples and"
                " the report")
    return f"test {test} is named in the examples, the report or the landed tests, but did not run"


def _judge(conn, run, task, files, runs):
    """(outcomes, problems, statements a ruling exempts) of the red check's `runs`."""
    card, covered = task["id"], json.loads(run["handoff"]).get("covered") or []
    ruled = _exempt_statements(conn, card)
    allowed = set(lint.exempt_ids(files, card)[0])
    under = [{t for c in covered if (c["statement"] in ruled) is named for t in c["tests"]}
             for named in (True, False)]        # tests listed under ruled / under other statements
    exempt = (under[0] - under[1]) & allowed
    outcomes = classify(runs, exempt)
    landed = (settle.events(conn, card, "blind_landed") or [(0, 0, {})])[-1][2].get("tests", [])
    bare = [f"no example row for test {t}" for t in _missing_examples(files, card, outcomes)]
    bare += [_unrun_reason(card, t) for t in _unrun(files, card, covered, outcomes, landed)]
    return outcomes, bare, ruled


def _check(tick, conn, cfg, run, task, files, now_hash, now):
    if _deferred(conn, run, task, files, _exempt_statements(conn, task["id"])):
        return False
    card, repo = task["id"], preflight.expand(config.blind_tests(cfg)["repo"])
    found = preflight.problems(cfg, cfg["board"], preflight.worker_mounts(cfg, cfg["board"],
                                                                         tick.home))
    base = base_sha(conn, card)
    if found or not base:
        raise RuntimeError("; ".join(found) or "the card has no start_sha on any run")
    theirs = conn.execute("SELECT json_extract(metadata,'$.start_sha') FROM task_runs WHERE"
                          " task_id=? AND profile='implementer' ORDER BY id LIMIT 1",
                          (card,)).fetchone()
    starts = {base, json.loads(run["metadata"]).get("start_sha") or base}
    if theirs and theirs[0] and starts != {theirs[0]}:
        return _disagree(conn, cfg, run, task, starts, theirs[0], now)
    with _suite_lane(tick, task) as (free, held):
        if not free:        # a mac card's suite waits for the Mac lane: next pass, nothing failed
            return False    # and nothing staged: no new commit, `card/<id>` stays put
        commit = _stage(repo, card, files, f"blind: tests for {card} ({run['run_id'][:8]})")
        runs = _base_runs(tick, cfg, task, repo, commit, base, time.monotonic() + RED_BUDGET, held)
    outcomes, bare, ruled = _judge(conn, run, task, files, runs)
    if _request(tick, conn, cfg, (run, task, files), outcomes, ruled, now):
        return True
    if bare or any(v != OK for v in outcomes.values()):
        return settle.bounce(conn, run, task, now_hash, outcomes, bare, commit, now) or True
    _land(conn, run, task, repo, commit, outcomes, base, now_hash, sorted(ruled), now)
    settle.cleanup(Path(json.loads(run["metadata"])["blind_clone"]))
    return True


@contextlib.contextmanager
def _suite_lane(tick, task):
    """Yields (may run now, the Mac lane's lock handle): a non-mac card always may, with no
    handle; a mac card's takes the lock first and may not while another mac job holds it."""
    if not blind_run.is_mac(task):
        yield True, None
        return
    from capsule.runners import mac
    with mac.lane(tick.home) as held:
        yield held is not None, held


def _request(tick, conn, cfg, round_, outcomes, ruled, now):
    """True when the round's tests that pass on base and are listed in `red_exempt` call for a
    ruling request to the guide that this round has not filed yet: it is filed now, or (the card
    moved during the base runs) the round is deferred, nothing recorded and no bounce; `_deferred`
    then holds it, with no base run, until the card can move. It is checked again once answered."""
    run, task, files = round_
    covered, acceptance = json.loads(run["handoff"]).get("covered") or [], _acceptance(task["body"])
    asked = exempt.asks(covered, lint.exempt_ids(files, task["id"])[0],
                        [t for t, v in outcomes.items() if v == PASSES], acceptance, ruled)
    if not asked or exempt.filed(conn, run["id"]):
        return False
    schemas = getattr(tick, "schemas", None) or load_all(paths.SCHEMAS)   # a stand-in has none
    _file_request(conn, spool.make(tick._spool_root(cfg), run["run_id"]), schemas, (run, task),
                  asked, acceptance, now)
    return True     # filed, or the card moved during the base runs: the round waits, unrecorded


def _deferred(conn, run, task, files, ruled):
    """True while the round may need a ruling request (a listed test under an unruled statement
    of the card) but the card cannot be moved to the guide's triage (`exempt.movable`): the round
    is taken up again, with no red check run, once it can."""
    covered = json.loads(run["handoff"]).get("covered") or []
    listed = lint.exempt_ids(files, task["id"])[0]
    return (not exempt.movable(conn, task) and not exempt.filed(conn, run["id"])
            and bool(exempt.asks(covered, listed, listed, _acceptance(task["body"]), ruled)))


def _file_request(conn, sp, schemas, round_, asked, acceptance, now):
    """The one request of the tester round: the card goes to the guide's triage with block_kind
    `guide_ruling`, the request doc (each `statement N` with its text and the listed test ids,
    never test source) and the `contradiction` event commit in one transaction (the pattern of
    `contradiction.route_contradiction`). True when it did; False when the card cannot be moved
    (`exempt.movable`) or the round already filed one. Idempotent by run id."""
    run, task = round_
    was, now_hash = task["status"], bundles.card_hash(task["body"])
    found = [{"id": f"statement {n}", "source": f"{task['id']} acceptance {n}",
              "kind": exempt.KIND, "was": was, "card_hash": now_hash,
              "differs": [f"statement text: {acceptance[n - 1]}", ", ".join(tests)]}
             for n, tests in sorted(asked.items())]
    what = "; ".join(f"{f['id']} ({f['differs'][1]})" for f in found)
    text = (f"guide_ruling: {task['id']} {ROLE} run {run['run_id']} listed tests in red_exempt"
            " that pass on the code from before the card; each may do so only when its statement"
            " describes behaviour the product already has. The guide rules red-exempt or"
            " not-exempt for all of them.\n"
            + "\n".join(f"{f['id']}: {f['differs'][0]}; tests: {f['differs'][1]}" for f in found))
    with txn(conn):
        moved = exempt.movable(conn, task) and not exempt.filed(conn, run["id"]) and transition(
            conn, task["id"], "triage", frm=(was,), claim=True, block_kind="guide_ruling")
        if not moved:
            return False
        ruling = contradiction._file_ruling_request(conn, sp, schemas, now, task["id"], ROLE,
                                                    run, what, text)
        event(conn, task["id"], "contradiction",
              {"role": ROLE, "examples": found, "precedence": intent.PRECEDENCE,
               "ruling_card": ruling, "routed": "guide"},
              run_pk=run["id"], run_id=run["run_id"], now=now)
        outbox.notify(conn, "running", f"{task['id']} red_exempt: routed to the guide for a ruling",
                      fields=[("request card", ruling)], thread_key=task["id"],
                      file_body=(f"capsule: {task['id']} red_exempt ({ROLE} run"
                                 f" {run['run_id'][:8]}): {what} | routed to the guide for a"
                                 f" ruling, request card {ruling}")[:1500], now=now)
    return True


def _base_runs(tick, cfg, task, repo, commit, base, deadline, lock=None):
    """RUNS result maps, one per run of `commit`'s tests on the card's `base`, each run in its own
    fresh strong container (a process or file one run left, in the temp folder or the home, must
    not reach the next) with its own spool; raises when the runner fails (no usable results, the
    two oracles disagreeing, over `deadline`, docker or git failing). Each container is stopped
    after its run, on a raise too, unless docker cannot do it: a `docker rm -f` that times out, or a
    `docker run` refused at start, can leave a container (one left `created` stays until the next
    pass's reap). The checkout, snapshots and spools are removed. A test can read the host paths the
    containers mount in /proc/self/mountinfo, so they are made to look like a normal blind run's:
    the clone is `work/<8 hex>/<branch slug>` (git dir `work/<8 hex>/.gitdirs/<slug>`, so the `.git`
    pointer and `/.gitdirs/<slug>` read as a normal clone's) and each run's snapshot and `/out` are
    `<spool>/<run id>/blind` and `blind-out`. The checkout has no real history (`checkout.build`,
    the same one a blind run gets), so the git history tells the check from a run no better than
    the paths do. A name equal to a live clone's raises (`mkdir` without `exist_ok`) and is never
    removed."""
    branch = workspace._validate_ref(task["branch_name"], what="branch")
    try:
        workspace._git("check-ref-format", "--branch", branch,    # as `checkout -B` did
                       timeout=min(workspace.GIT_FAST_TIMEOUT, max(deadline - time.monotonic(), 1)))
    except RuntimeError:
        raise ValueError(f"unsafe branch (git refuses the name): {branch!r}") from None
    box = paths.board_dir(cfg["board"], tick.home) / "work" / uuid.uuid4().hex[:8]
    box.parent.mkdir(parents=True, exist_ok=True)
    box.mkdir()
    try:
        ws = _checkout(cfg["repo_url"], base, box / workspace._slug(branch), deadline)
        line = blind_run._run_line(repo, commit, task["id"])
        with _bounded(tick.strong, deadline):
            return [_one_run(tick.strong, tick._spool_root(cfg), (ws, repo, commit, task["id"]),
                             line, deadline, lock) for _ in range(RUNS)]
    finally:
        shutil.rmtree(box, ignore_errors=True)


def _sweep(tick, cfg):
    """Remove what a killed red check left, once a pass. In the board's `work` folder: a folder
    named by 8 hex digits that holds `.gitdirs` (and at most one clone), has no `work/.gitdirs/
    <name>` beside it (a live clone always has one; a red box never does) and is untouched for
    twice `RED_BUDGET`. In the spool: a `<run id>` folder holding the `red-check` marker
    `_one_run` writes (a blind run's spool never has one; no `task_runs` row is used, a run id
    exists before its row) whose marker is that old. Nothing else is touched; a link is never
    followed."""
    work, root = paths.board_dir(cfg["board"], tick.home) / "work", tick._spool_root(cfg)
    old = time.time() - 2 * RED_BUDGET
    with contextlib.suppress(OSError):
        for box in work.glob("[0-9a-f]" * 8):
            if (box.is_dir() and not box.is_symlink() and (box / ".gitdirs").is_dir()
                    and not (work / ".gitdirs" / box.name).exists()
                    and len(os.listdir(box)) <= 2 and box.stat().st_mtime < old):
                shutil.rmtree(box, ignore_errors=True)
        for mark in root.glob(f"*/{MARKER}"):
            if (mark.is_file() and not mark.is_symlink() and not mark.parent.is_symlink()
                    and mark.stat().st_mtime < old):
                shutil.rmtree(mark.parent, ignore_errors=True)


@contextlib.contextmanager
def _bounded(strong, deadline):
    """Within `deadline`, the strong runner's long docker calls (over 30 s: its `start` allows 300 s
    a try, three tries and 45 s of sleeps) get only the time left and its retry sleeps are cut to
    it; one that runs out raises `TimeoutExpired`, so the card gets a retry. A runner without those
    hooks (a test's stand-in) is left alone."""
    run, sleep = getattr(strong, "_run", None), getattr(strong, "_sleep", None)
    if run is None or sleep is None:
        yield
        return

    def limited(cmd, **kw):
        left, wanted = deadline - time.monotonic(), kw.get("timeout")
        if wanted is not None and wanted > 30:
            if left <= 0:
                raise subprocess.TimeoutExpired(cmd, wanted)
            kw["timeout"] = min(wanted, left)
        return run(cmd, **kw)
    strong._run, strong._sleep = limited, lambda secs: sleep(max(0, min(secs, deadline
                                                                         - time.monotonic())))
    try:
        yield
    finally:
        strong._run, strong._sleep = run, sleep


def _checkout(url, base, ws, deadline):
    """The neutral checkout of `base` (`checkout.build`: no real history) at the new path `ws`, in
    the tick's clone layout (`checkout.layout`). The product is cloned bare into a scratch folder
    that is removed here, so nothing of the clone or its URL reaches `ws`. The git calls get the
    time left of the red check's budget, never `workspace.clone`'s own 600 s; a failure of the
    clone is raised without the repo URL (it may carry a credential)."""
    workspace._validate_repo_url(url)

    def left():
        return max(1, deadline - time.monotonic())
    with tempfile.TemporaryDirectory(prefix="red-src-") as src:
        try:
            workspace._git("clone", "--bare", "--", url, f"{src}/repo", timeout=left())
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            raise workspace._redact_git_failure("clone", exc, url) from None
        checkout.build(f"{src}/repo", base, ws, timeout=left())
    return checkout.layout(ws)


def _one_run(strong, spool_root, source, line, deadline, lock=None):
    """{test id: result row} of one run of the suite in a container of its own, in a spool of its
    own (named by its run id, as a blind run's is, and removed after); `source` is (card
    worktree, blind repo, commit, card). Raises when the budget is spent or the two oracles give
    no usable result. A mac card's suite (`lock`, the held Mac lane lock) runs on the host."""
    if deadline <= time.monotonic():
        raise TimeoutError(f"the red check is over its {RED_BUDGET}s budget")
    ws, repo, commit, card = source
    sp = spool.make(spool_root, uuid.uuid4().hex)
    (sp / MARKER).touch()               # not mounted: only `blind` and `blind-out` are
    try:
        snap, out = sp / blind_run.SNAP_DIR, sp / blind_run.OUT_DIR
        blind_run._snapshot(repo, commit, card, snap)
        out.mkdir(mode=0o700)
        if lock is not None:    # a mac card (`lock`: the lane's): on the host, Mac profile
            from capsule.runners import mac
            left = max(1, deadline - time.monotonic())
            code, text = mac.run_blind(lock, ws, snap, out, line,
                                       min(blind_run.BLIND_TIMEOUT, left), blind_run.RESULTS_FILE)
        else:
            with blind_run.container(strong, sp.name, ws, snap, out) as name:
                left = max(1, deadline - time.monotonic())      # the start took time too
                code, text = blind_run.execute(strong, name, line,
                                               min(blind_run.BLIND_TIMEOUT, left))
        rows, problem = blind_run._parse(blind_run._read(out / blind_run.RESULTS_FILE), text, code)
    finally:
        shutil.rmtree(sp, ignore_errors=True)
    if problem:
        raise RuntimeError(f"the runner gave no usable result: {problem}")
    return {r["id"]: r for r in rows}


def _retry(conn, cfg, run, task, reason, now):
    """A runner failure: try again on a later pass; the `MAX_RUNNER_FAILURES`th triages the card
    (and opens a `review_infra` incident, whatever the cause). The count is written first, in its
    own transaction, so a failure of the triage cannot roll it back and rerun the check forever."""
    earlier = [1 for _, rid, _ in settle.events(conn, task["id"], "blind_red_retry")
               if rid == run["id"]]
    n = 1 + len(earlier)
    with txn(conn):
        event(conn, task["id"], "blind_red_retry", {"reason": reason[:500], "attempt": n},
              run_pk=run["id"], run_id=run["run_id"], now=now)
    if n < MAX_RUNNER_FAILURES:
        return
    incidents.open_incident(conn, board=cfg["board"], kind="review_infra", task_id=task["id"],
                            run_id=run["run_id"], key="blind_red runner failed",
                            role=f"blind_red:{task['id']}", evidence=[reason[:500]])
    settle.finish(conn, run, task, now,         # a fixed phrase: `reason` can hold blind test text
                  f"the blind red check could not run {n} times (see its blind_red_retry events)")
    settle.cleanup(Path(json.loads(run["metadata"])["blind_clone"]))


def _missing_examples(files, card, test_ids):
    """The test ids (of `test_ids`, as the harness ran them) with no example row in the card's
    examples.jsonl."""
    rows, _ = lint.examples(files, card)
    return sorted(t for t in test_ids if t not in rows)


def _blob(repo, data, scratch):
    """The id of `data` written to the blind repo's object store, byte for byte."""
    path = Path(scratch) / "blob"
    path.write_bytes(data)
    done = preflight.git(repo, "hash-object", "-w", "--no-filters", "--", str(path))
    if done.returncode:
        raise RuntimeError(f"git hash-object failed: {done.stderr.strip()}")
    return done.stdout.strip()


def _stage(repo, card, files, message):
    """The sha of a new commit on branch `card/<card>` of the blind repo `repo`: its parent is the
    tip of the branch the repo has checked out (it must be `main`), its tree is that tip's tree
    with `cards/<card>/` replaced by `files`. Nothing is checked out and `main` does not move."""
    head = preflight.git(repo, "symbolic-ref", "--short", "-q", "HEAD").stdout.strip()
    if head != "main":
        raise RuntimeError(f"the blind repo has {head or 'a detached head'} checked out, not main")
    parent = preflight.git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
    with tempfile.TemporaryDirectory(prefix="blind-index-") as tmp:
        env = {"GIT_INDEX_FILE": os.path.join(tmp, "index")}
        steps = [("read-tree", parent), ("rm", "-r", "--cached", "-q", "--ignore-unmatch", "--",
                                         f"cards/{card}")]
        steps += [("update-index", "--add", "--cacheinfo", f"100644,{_blob(repo, data, tmp)},{rel}")
                  for rel, data in sorted(files.items())]
        for args in steps:
            done = preflight.git(repo, *args, env_extra=env)
            if done.returncode:
                raise RuntimeError(f"git {args[0]} failed: {done.stderr.strip()}")
        tree = preflight.git(repo, "write-tree", env_extra=env).stdout.strip()
    commit = preflight.git(repo, "commit-tree", tree, "-p", parent, "-m", message).stdout.strip()
    done = preflight.git(repo, "update-ref", f"refs/heads/card/{card}", commit)
    if not commit or done.returncode:
        raise RuntimeError(f"git could not stage the tests: {done.stderr.strip()}")
    return commit


def _land(conn, run, task, repo, commit, outcomes, base, card_hash, exempt, now):
    """Fast-forward the blind repo's `main` to `commit` (the commit that was checked, whatever
    `card/<id>` points at by now), record `blind_commit` on the card and emit `blind_landed`."""
    head = preflight.git(repo, "rev-parse", "--verify", "HEAD").stdout.strip()
    if preflight.git(repo, "rev-parse", "--verify", f"{commit}^").stdout.strip() != head:
        raise RuntimeError("the blind repo's main moved while the tests were checked")
    done = preflight.git(repo, "merge", "--ff-only", "--no-edit", commit)
    if done.returncode or preflight.git(repo, "rev-parse", "HEAD").stdout.strip() != commit:
        raise RuntimeError(f"git could not fast-forward main: {done.stderr.strip()}")
    with txn(conn):
        conn.execute("UPDATE tasks SET blind_commit=? WHERE id=?", (commit, task["id"]))
        event(conn, task["id"], "blind_landed", {
            "blind_commit": commit, "card_hash": card_hash, "start_sha": base,
            "tests": sorted(outcomes), "acceptance": _acceptance(task["body"]), "exempt": exempt},
            run_pk=run["id"], run_id=run["run_id"], now=now)
        settle.record(conn, run, "landed", now, card_hash=card_hash, outcomes=outcomes,
                      tested=commit)
        gate.release(conn, task["id"], now)     # a card held for these tests goes back to review
