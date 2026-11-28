"""The tick loop: the only writer of a board's SQLite file; it never calls a model.

One pass runs in this order: read config and record human intervention files; reap the
strong runner's orphan containers; time out, heartbeat and release stale runs; close and
route each finished run from its spool; run the review stage, guide rulings and incidents
(Korin); dispatch ready cards; export the unblocked/unowned metrics and check the idle alert;
land and boundary-check; then flush the outbox, heartbeat and write the lookout timeline.
`once()` and `_step_pass` carry the exact order.

Layout under the board's directory: `spool/<run_id>/` per run, `work/<branch-slug>/` per epic.
"""
import json
import os
import shlex
import socket
import sys
import time
import uuid
from pathlib import Path

from capsule import (blocks, bundles, check_gate, checkpoint, config, contracts, db, dispatch,
                     epic_sync, guide_notes, incident_inputs, incidents, intake, intent, restart,
                     interventions, lease, lookout, metrics, outbox, override, paths, prior_runs,
                     report_notes, review_findings, review_scope, ruling_sweep, safe_write, slack,
                     spool, style_lint, tick_guard, validator_report, workspace, run_record,
                     follow_ups, stall, security_review, vault_context, fix_card_quotes,
                     contract_audit, weekly_pause, stop_queue, stall_continue, korin_checkout)
from capsule import boundary, contract_jobs, main_waits, invalid_doc as idoc, incident_blind
from capsule import missing_module
from capsule import invariants, journal, landing, probe_detail, protected_scope, reviewer_rerun
from capsule import runaway, wall_clock, exec_outage, image_requests, stall_ruling
from capsule import stall_decided, stall_owner, docker_down, preview
from capsule.blind import close as blind_close, dispatch as blind_dispatch, exempt as blind_exempt
from capsule.blind import gate as blind_gate
from capsule.citations import card_spec_section, cited_rows, cited_sections
from capsule.outcomes import tick_pass
# re-exported: these helpers moved to capsule.tick_rows; callers and tests still use capsule.tick.X
from capsule.tick_rows import (EPIC_SECTION_MAX_LINES, ROW_TEXT_BUDGET, ROW_TEXT_CAP,  # noqa: F401
                               ROW_TEXT_OVER_BUDGET, _cap_row_texts, _row_columns,
                               _truncate_row_text, decision_rows, epic_spec_section,
                               spec_sections)
from capsule.db import event, transition, txn
from capsule.jsonschema import load_all
from capsule.runners import docker, reap, runner_for, scoped
from capsule.tick_review import REVIEW_ROLES, REVIEW_TRIES, TickReview
from capsule.tick_rulings import TickRulings
from capsule.tick_runs import TickRuns

KORIN = incidents.KORIN
READONLY_ROLES = REVIEW_ROLES + (blind_dispatch.ROLE, KORIN)   # host runner, no lease, no failure
KORIN_TRIES = 2               # post-mortem runs per incident before the tick stops and says so
RETEST_SECONDS = 3600         # the closure gate re-runs a failing recurrence test at most hourly
LOG_TAIL = 3000               # characters of each worker.log Korin gets inline
RULING_SCHEMA = bundles.RULING_SCHEMA   # a guide ruling on an intent contradiction
RULINGS = tuple(i for i, _ in bundles.RULING_OPTIONS)
# document-wrong rulings per task per example id; the next one blocks (loop cap)
RULING_RESETS = 2
WORKER_CAP_MARGIN = 120       # seconds the worker's model call ends before the tick's cap
KILL_GRACE = 5.0              # seconds between SIGTERM and SIGKILL
CONTRACT_TIMEOUT = 1200       # seconds per contract command (the suite: ~500 s idle, 600+ loaded)
OUTPUT_TAIL = 8000            # characters of each contract command's output kept for the validator
RATE_LIMIT_DEFAULT = 3600     # pause length when a rate limit names no window
COUNTS = ("config_error", "interventions", "intake", "reaped", "timed_out", "heartbeats", "released", "applied", "closed", "restarted",
          "review_runs", "reviewed", "ruling_runs", "incidents", "korin_runs", "incidents_closed", "metrics_exported",
          "dispatched", "flushed", "tick_errors", "lookout_errors", "lookout_ms")


def _write_json(path, doc):
    # path is <spool root>/<run id>/job.json: the walk refuses a symlinked run dir
    safe_write.atomic_write(path.parent.parent, path, json.dumps(doc, ensure_ascii=False))


def _read_json(path):
    try:
        doc = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) else None


def _run_cap(cfg, task, role):
    """The wall-clock cap of a run: `review_seconds` for a read-only role, else the task's own."""
    if role in READONLY_ROLES:
        return config.cap(cfg, "review_seconds")
    own = task["max_runtime_seconds"] if task is not None else None
    return own or config.cap(cfg, "max_runtime_seconds")


def _last_line(path):
    """The last non-empty line of a log (a hang's root-cause key), or '': also '' for the log of a
    run that carries blind tests (`first_tail` skips it), whose last line may quote them."""
    got = incident_inputs.first_tail((path,), 4096)
    lines = [ln.strip() for ln in (got["tail"] if got else "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _is_bytecode_cache(ws, path):
    """`<dir>/__pycache__/<name>.pyc` that the restore removed: a worker's test-run cache, not a
    row-395 failure. A `.pyc` elsewhere, a `.py` inside, or a survivor of restore still fails."""
    *dirs, name = path.split("/")
    return (dirs[-1:] == ["__pycache__"] and name.endswith(".pyc")
            and not os.path.lexists(f"{ws}/{path}"))


class Tick(TickRuns, TickReview, TickRulings):
    def __init__(self, home, *, board=None, pin_board=None, spawn=None, alive=None, runners=None,
                slack_factory=None, events=None, clock=time.time):
        self.home = Path(home)
        self.clock = clock
        self.board = board            # None = the legacy, single-board tick
        # the board this process locked
        self.pin_board = pin_board if pin_board is not None else board
        self.outbox = outbox.Router(home=self.home, log_path=paths.outbox_log(self.home, board) if board else None,
                                    slack_factory=slack_factory)
        self.loader = config.Loader(paths.config_path(self.home, board))
        self.spawn = spawn or self._spawn
        self.alive = alive or self._alive
        self.runners = scoped(runners or runner_for, lambda: self.cfg, self.home)
        self.host = socket.gethostname()
        self.schemas = load_all(paths.SCHEMAS)
        self.intent_dir = paths.INTENT
        self.kill_grace = KILL_GRACE
        self.children = {}
        self.contracts = contract_jobs.ContractJobs(CONTRACT_TIMEOUT, OUTPUT_TAIL)
        self.cfg = None
        self.migrated_boards = set()   # str(board_db) paths already migrated this process
        # tests swap it; the closure gate's test runner
        self.run_recurrence = incidents.run_recurrence_test
        # the gate's "test is new on main" check
        self.check_new = incidents.recurrence_test_is_new
        # incremented once per once() call; the tick_health heartbeat's `pass`
        self.pass_num, self.code_note = 0, None
        # None => lazily import the real maintenance/events.py (tests inject a fake)
        self._events_mod = events
        # a lookout.Collector, built on the first pass (needs the board name)
        self.lookout = None
        self.lookout_kick = lookout.kick
        # the previous pass's Lookout step, shipped in this pass's tickmark (ruling 7)
        self.last_lookout_ms = 0
        self.last_lookout_errors = 0  # ditto for lookout_errors, so it actually reaches ops-pg

    # -- one pass -----------------------------------------------------------------

    def _engine_probe(self):    # really up (maybe by hand)? any probe error is "not up"
        try:
            return bool(getattr(self.strong, "_engine_up", lambda: False)())
        except Exception:
            return False

    def _wake_failed(self, conn, cfg, now, exc, waiting):    # say so; 3 downs open an incident
        if not isinstance(exc, docker.EngineDown):
            print(f"capsule tick: engine wake failed: {exc}", file=sys.stderr)
        elif docker.WOKE["count"] >= 3:     # same escalation as a worker start's
            incidents.open_incident(
                conn, board=cfg["board"], kind="review_infra", key="Docker engine down",
                evidence=[f"task:{waiting[0]}", str(exc)[:300]], task_id=waiting[0], now=now)

    def once(self, now=None):
        now = int(now if now is not None else time.time())
        docker.TICK_NOW[:] = [now, time.monotonic()]      # the wake memo runs on the tick clock
        self._pass_t0 = time.monotonic()
        start_wall = time.time()
        self.pass_num += 1
        counts = dict.fromkeys(COUNTS, 0)
        cfg, err = self.loader.get()
        if cfg is None:
            counts["config_error"] = int(err is not None)
            if err:
                print(f"capsule tick: no usable config: {err}", file=sys.stderr)
            return counts
        if self.pin_board is None:      # pin on the first valid load when nothing pinned it yet
            self.pin_board = cfg["board"]
        if self.pin_board is not None and cfg.get("board") != self.pin_board:
            # never serve a board this process did not lock, even a hot-reloaded legacy config
            counts["config_error"] = 1
            print(f"capsule tick: {paths.config_path(self.home, self.board)} names board"
                  f" {cfg.get('board')!r}, not {self.pin_board!r}; no pass", file=sys.stderr)
            return counts
        self.cfg = cfg
        board_db = paths.board_db(cfg["board"], self.home)
        if not board_db.exists():
            raise FileNotFoundError(f"capsule tick: no board file at {board_db}")
        self.board_db = board_db
        self.strong = self.runners("strong", "implementer"); strong_up = self.strong.available()
        conn = db.connect(board_db)
        try:
            if str(self.board_db) not in self.migrated_boards:
                # A board older than Korin has no `incidents` table: migrate it once per process.
                db.migrate(conn)
                self.migrated_boards.add(str(self.board_db))
            if strong_up and self._engine_probe():
                docker.WOKE.update(failed_at=None, delay=0, count=0)
            elif (not os.environ.get("AGENT_OPS_NO_DOCKER") and not dispatch.any_pause(conn, now)
                    and docker.worker_image(self.home, self.board)
                    and not docker.recently_down(now) and (waiting := docker_down.waiting(conn))):
                try:    # the engine is down and cards need it: wake it before the tiers are set
                    self._wake_engine(conn, waiting[0], now)
                    strong_up = self.strong.available()
                except Exception as exc:     # a wake error must not stop the pass
                    self._wake_failed(conn, cfg, now, exc, waiting)
            self.host_tiers = {"strong"} if strong_up else set()
            self.host_tiers |= self._up_tiers()      # light and mac (TickRuns); sets `mac_hold`
            self._step_pass(conn, cfg, err, strong_up, now, counts)
        finally:  # flush even after a failed step
            try:
                counts["flushed"] = outbox.flush(conn, self.outbox.configure(cfg), now=now,
                                                 quiet=cfg)
            finally:
                try:
                    # dead-man signal (Grafana observability audit, 2026-09-27)
                    self._heartbeat(conn, cfg, start_wall)
                finally:
                    try:
                        self._lookout(conn, cfg, counts)
                    finally:
                        conn.close()
        return counts

    def _step_pass(self, conn, cfg, err, strong_up, now, counts):
        """Interventions through dispatch, metrics, landing and boundary checks."""
        outbox.set_slack(conn, slack.block(cfg), now=now)
        outbox.digest(conn, cfg, now, self.home, self._guarded, counts)
        if err:
            counts["config_error"] = 1
            outbox.notify(conn, "incident", "capsule config error: keeping the last good config",
                          fields=[("error", err)], page=True,
                          file_body=f"capsule: config error, keeping the last good config: {err}", now=now)
        intake.step(conn, self.home, cfg, now, counts, self._interventions)
        preview.step(conn, cfg, now)    # F1: ask the owner once at a preview card
        if self.host_tiers:
            counts["reaped"] = len(reap(self.runners, self.host_tiers, self._live_run_ids(conn)))
        self._timeouts(conn, cfg, now, counts)
        if self.host_tiers and counts["timed_out"]:    # a killed run's leftovers go in this pass
            counts["reaped"] += len(reap(self.runners, self.host_tiers, self._live_run_ids(conn)))
        counts["heartbeats"], counts["released"] = self._leases(conn, cfg, now)
        self._close_runs(conn, cfg, now, counts)
        stall_continue.step(self, conn, cfg, now)    # before the review and dispatch passes (9.10)
        stop_queue.sweep(self, conn, cfg, now, counts)    # queued `stop`
        blind_dispatch.late(self, conn, cfg, now, counts)
        self._review(conn, cfg, now, counts)
        self._rulings(conn, cfg, now, counts)
        ruling_sweep.close_ruled(conn, now)
        self._incidents(conn, cfg, now, counts)
        epic_sync.sync_idle(conn, cfg, self._work_root(cfg), now)    # row 624 clean, then sync
        counts["dispatched"] = self._dispatch(conn, cfg, now, counts)
        if counts["dispatched"]: self._record_dispatch(conn, now)
        blind_dispatch.step(self, conn, cfg, now, counts)    # after the dispatch: no delay
        counts["metrics_exported"] = self._export_metrics(conn, now)
        stall.step(self, conn, cfg, strong_up, now,    # Z5: stalls, docker down, dead-man alert
                   {"kickstart": stall_owner.kickstart})
        image_requests.step(conn, cfg, strong_up, now)    # M137: a missing tool's card
        exec_outage.release(conn, strong_up, now)      # a lost exec daemon is back: rerun
        landing.step(conn, cfg, self.home, now)
        boundary.step(conn, cfg, self.home, now)

    def _events(self):
        """The real maintenance/events.py, lazily imported (it is a sibling script directory,
        not a package); a test passes `events=` to the constructor and never imports it."""
        if self._events_mod is None:
            sys.path.insert(0, str(paths.REPO / "maintenance"))
            import events as events_mod
            self._events_mod = events_mod
        return self._events_mod

    def _heartbeat(self, conn, cfg, start_wall):
        """One `tick_health` event per pass: the only signal to Grafana that THIS tick is alive
        (`capsule-tick-deadman`, docs/RUNBOOK.md "Alerting"), shipped via `events.log()` then
        `events.ship()`. Never raises: a failed read, write or ship is a stderr line at most."""
        try:
            row = conn.execute("SELECT SUM(status='running'), SUM(status='blocked') FROM tasks").fetchone()
            running, blocked = row[0] or 0, row[1] or 0
        except Exception:
            running = blocked = None
        try:
            paused = dispatch.any_pause(conn, checks=True)     # a wall pause counts 15 min
        except Exception:
            paused = None
        payload = {"pass": self.pass_num, "duration_s": round(time.time() - start_wall, 3),
                   "running": running, "blocked": blocked, "pid": os.getpid(),
                   "main_sha": boundary.BOOT, "disk_sha": incidents.main_head(paths.REPO),
                   "config": str(paths.config_path(self.home, self.board)),
                   "lock": tick_guard.held(self.home, self.board), "code": self.code_note,
                   "watch": bool(cfg.get("deadman", True)), "paused": paused}
        try:
            events_mod = self._events()
            events_mod.log("tick_health", "capsule", "tick heartbeat", board=cfg["board"], payload=payload)
            if not lookout.ship_on(cfg):   # no Lookout shipper yet: the heartbeat ships itself
                events_mod.ship()
        except Exception as exc:
            print(f"capsule tick: heartbeat not shipped: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _guarded(self, conn, cfg, task_id, now, counts, work, run_id=None):
        """Run one card's work; an error is one `tick_error` per message (`MissingImage`: none)."""
        try:
            work()
            return True
        except Exception as exc:
            if isinstance(exc, docker.MissingImage):
                return False
            counts["tick_errors"] += 1
            text = incident_inputs.tick_text(conn, exc, self._spool_root(cfg), task_id, run_id, cfg)
            line = f"capsule: tick error on {task_id}: {type(exc).__name__}: {text}"[:1000]
            with txn(conn):
                seen = conn.execute("SELECT 1 FROM outbox WHERE thread_key=? AND body=? LIMIT 1",
                                    (task_id, line)).fetchone()
                if seen is None:
                    event(conn, task_id, "tick_error", {"error": line, **probe_detail.fields(exc)},
                          run_id=run_id, now=now)
                    outbox.notify(conn, "incident", f"{task_id} tick error: {type(exc).__name__}",
                                  fields=[("error", text[:300])], thread_key=task_id, page=True,
                                  file_body=line, now=now)
            return False

    def _lookout(self, conn, cfg, counts):
        """Collect -> spool, then kick the shipper. Never raises out of the pass."""
        t0 = time.monotonic()
        try:
            if self.lookout is None:
                self.lookout = lookout.Collector(self.home, cfg["board"], host=self.host, pid=os.getpid(),
                                                per_board=self.board is not None)
            pass_ms = int((time.monotonic() - self._pass_t0) * 1000)
            self.lookout.collect(conn, cfg, time.time(), counts, pass_ms, lookout_ms=self.last_lookout_ms,
                                 lookout_errors=self.last_lookout_errors)
        except Exception as exc:
            counts["lookout_errors"] += 1
            print(f"capsule tick: lookout: {type(exc).__name__}: {exc}"[:500], file=sys.stderr)
        counts["lookout_ms"] = self.last_lookout_ms = int((time.monotonic() - t0) * 1000)
        try:
            if lookout.ship_on(cfg):
                self.lookout_kick(self.home, ["lookout", "ship"], "ship.pid")
            self.lookout_kick(self.home, ["lookout", "check"], "check.pid", 3600)   # hourly
        except Exception as exc:
            counts["lookout_errors"] += 1
            print(f"capsule tick: lookout kick: {type(exc).__name__}: {exc}"[:500], file=sys.stderr)
        self.last_lookout_errors = counts["lookout_errors"]   # this pass's total, shipped next pass

    # -- human interventions ----------------------------------------------------

    def _interventions(self, conn, cfg, now):
        """Each file directly under `human/` becomes one `intervention` event on its task, then
        moves to `human/done/`. An unreadable file, or one naming no task on this board, is
        archived without an event (one stderr line), never retried.

        The event commits before the file moves, so a crash between the two leaves the file in
        place for the next tick. The source file name is the idempotency key: a file whose name
        already carries a recorded `intervention` event is archived again without a second
        event or a repeated side effect."""
        hdir = spool.human_dir(self.home, cfg["board"])
        if not hdir.is_dir():
            return 0
        done = hdir / "done"
        recorded = 0
        for path in sorted(hdir.iterdir(), key=spool._sort_key):
            if path.name.startswith(".") or (path.is_dir() and not path.is_symlink()):
                continue
            doc = _read_json(path) if path.is_file() and not path.is_symlink() else None
            task_id = doc.get("task_id") if doc else None
            known = isinstance(task_id, str) and conn.execute(
                "SELECT 1 FROM tasks WHERE id=?", (task_id,)).fetchone() is not None
            done.mkdir(exist_ok=True)
            target = done / path.name
            n = 1
            while target.exists() or target.is_symlink():
                target = done / f"{path.stem}.{n}{path.suffix}"
                n += 1
            if known:
                # json_extract on the stored payload, not LIKE: LIKE disagrees with db.event's
                # ensure_ascii=False storage for non-ASCII names and treats `_`/`%` as wildcards.
                already = conn.execute(
                    "SELECT 1 FROM task_events WHERE task_id=? AND kind='intervention'"
                    " AND json_extract(payload, '$.src') = ?", (task_id, path.name)).fetchone()
                if already is None:
                    interventions.apply(conn, cfg, task_id, doc, path.name, target.name, now)
                    recorded += 1
                else:
                    print(f"capsule tick: human file {path.name} already recorded; archived without a duplicate event",
                          file=sys.stderr)
            else:
                print(f"capsule tick: human file {path.name} names no task on this board; archived",
                      file=sys.stderr)
            os.replace(path, target)
        return recorded

    def _timeouts(self, conn, cfg, now, counts):
        """The worker's model call times out WORKER_CAP_MARGIN seconds before the cap and step 4
        closes that run; this pass is the backstop for a worker that outlives the cap.
        A launched, unfinished run past its wall-clock cap is killed and
        ends `timed_out`, with one outbox line; an implementer run counts one failure, a review
        run is retried. The cap is the task's `max_runtime_seconds`, else the config's; a review
        run's is `caps.review_seconds`. The clock starts at launch or journal `start`, the later."""
        root = self._spool_root(cfg)
        runs = conn.execute("SELECT * FROM task_runs WHERE ended_at IS NULL AND run_id IS NOT NULL ORDER BY id").fetchall()
        for run in runs:
            self._guarded(conn, cfg, run["task_id"], now, counts,
                          lambda run=run: self._timeout_one(conn, cfg, root, run, now, counts), run_id=run["run_id"])

    def _apply_final_spool(self, conn, sp, run, now, counts):
        """Apply a killed worker's last spool before its run ends; returns the fresh run."""
        run = self._run(conn, run["id"])
        statuses = spool.apply(conn, sp, run, role_verbs=contracts.verbs_for(run["profile"]),
                               schemas=self.schemas, now=now, settle=None)
        counts["applied"] += statuses.count("applied")
        return self._run(conn, run["id"])

    def _timeout_one(self, conn, cfg, root, run, now, counts):
        sp = root / run["run_id"]
        if not sp.is_dir() or (sp / "run.json").exists():
            return                                 # not launched yet, or finished: step 4 closes it
        task = conn.execute("SELECT status, current_run_id, max_runtime_seconds FROM tasks WHERE id=?",
                            (run["task_id"],)).fetchone()
        cap = _run_cap(cfg, task, run["profile"])
        launched = (_read_json(sp / "job.json") or {}).get("launched_at") or run["started_at"]
        elapsed = now - wall_clock.counted_from(sp, launched)
        if elapsed <= cap:
            return
        if not self._is_local(run):
            return                                 # another host's run: never ours to end
        pid = run["worker_pid"]
        if pid:
            if not self.alive(pid):
                return                             # already dead: step 4 closes it
            self._kill(pid)
            if self.alive(pid):
                return                             # survived SIGKILL: the next pass retries
        task_id = run["task_id"]
        # Worker dead: apply its final spool first (ledger rule); a request before the kill lands.
        run = self._apply_final_spool(conn, sp, run, now, counts)
        task = conn.execute("SELECT status, current_run_id FROM tasks WHERE id=?", (task_id,)).fetchone()
        current = task is not None and task["status"] == "running" and task["current_run_id"] == run["id"]
        reason = (f"run {run['run_id']} ({run['profile']}) timed out: {elapsed}s against its {cap}s"
                  f" wall-clock cap; killed")
        steps = journal.read(sp)
        with txn(conn):
            event(conn, task_id, "timed_out", {"role": run["profile"], "cap": cap, "elapsed": elapsed,
                                               "last_log": _last_line(sp / "worker.log"),
                                               "last_step": (steps or [{}])[-1].get("step"),
                                               "worker_pid": pid}, run_pk=run["id"], run_id=run["run_id"], now=now)
            outbox.notify(conn, "incident", f"{task_id} timed out: {run['profile']} killed at its {cap}s cap",
                          fields=[("run", run["run_id"]), ("elapsed", f"{elapsed}s")], thread_key=task_id,
                          file_body=f"capsule: {task_id} {reason}", now=now)
            if run["ended_at"] is not None:        # a spool verb (block) already ended it
                counts["timed_out"] += 1
                return
            lease.end_run(conn, run["id"], status="timed_out", outcome="timed_out", error=reason, now=now)
            if run["profile"] not in READONLY_ROLES and current:
                blocks.record_failure(conn, task_id, reason=reason, limit=restart.failure_cap(cfg),
                                      run_id=run["run_id"], now=now)
        counts["timed_out"] += 1

    # -- close finished runs ------------------------------------------------------

    def _close_runs(self, conn, cfg, now, counts):
        root = self._spool_root(cfg)
        pks = [r[0] for r in conn.execute(
            "SELECT id FROM task_runs WHERE ended_at IS NULL AND run_id IS NOT NULL ORDER BY id")]
        for pk in pks:
            run = self._run(conn, pk)
            self._guarded(conn, cfg, run["task_id"], now, counts,
                          lambda: self._close_one(conn, cfg, root, pk, now, counts), run_id=run["run_id"])

    def _close_one(self, conn, cfg, root, pk, now, counts):
        run = self._run(conn, pk)                  # fresh: claim_lock as of now, before apply
        sp = root / run["run_id"]
        if run["ended_at"] is not None or not sp.is_dir():
            return
        gone = run["worker_pid"] is not None and not self.alive(run["worker_pid"])
        # checked after `gone`: a worker writes run.json, then exits
        finished = (sp / "run.json").exists()
        last = finished or gone
        statuses = spool.apply(conn, sp, run, role_verbs=contracts.verbs_for(run["profile"]),
                               schemas=self.schemas, now=now, settle=None if last else 2)
        counts["applied"] += statuses.count("applied")
        if not last:
            return
        run = self._run(conn, pk)
        if run["ended_at"] is not None:            # a spool verb (block) already ended it
            stall_continue.on_close(self, conn, cfg, pk, sp, now, True)    # the run's own block
            return
        run_json = _read_json(sp / "run.json") if finished else None
        run_json = incident_inputs.run_text(run_json, sp, run["profile"])
        close = blind_close.close if run["profile"] == blind_dispatch.ROLE else type(self)._close
        close(self, conn, cfg, run, sp, run_json, now, counts)
        stall_continue.on_close(self, conn, cfg, pk, sp, now)    # quiet stops (9.10)
        counts["closed"] += 1

    def _close(self, conn, cfg, run, sp, run_json, now, counts):
        role, task_id = run["profile"], run["task_id"]
        if run_json is None:
            error_class = "fatal"
            error = "worker exited without a readable run.json"
        else:
            error_class, error = run_json.get("error_class"), None
        tokens = (run_json or {}).get("tokens") or {}
        with txn(conn):
            conn.execute(
                "UPDATE task_runs SET tokens_in=?, tokens_out=?, tokens_cache_read=?,"
                " tokens_cache_write=?, attempts=?, error_class=COALESCE(error_class,?) WHERE id=?",
                (*(tokens.get(k) for k in ("in", "out", "cache_read", "cache_write")),
                 (run_json or {}).get("attempts"), error_class, run["id"]))
        run = self._run(conn, run["id"])
        if role == KORIN:
            return self._close_korin(conn, cfg, run, sp, run_json, error_class, now)
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        review = role in REVIEW_ROLES
        current = task is not None and task["status"] == "running" and task["current_run_id"] == run["id"]
        if not review and not current:
            with txn(conn):
                lease.end_run(conn, run["id"], status="failed", outcome="stale",
                              error="the task no longer holds this run", now=now)
                event(conn, task_id, "stale_run", {"role": role}, run_pk=run["id"], run_id=run["run_id"], now=now)
            return
        if error_class in blocks.PAUSE_CLASSES:
            until = self._pause(conn, run, (run_json or {}).get("retry_after"), now, run_json)
            # an invalid document still fails the run (§4)
            if run["error_class"] != "invalid_output":
                self._release_rate_limited(conn, run, review, until, now)
                return
        report, why = (None, error or f"error_class {error_class}") if error_class else self._report(sp, run, conn, now)
        # An invalid requests/ document (`spool._mark_invalid` set error_class) is not WORK failure.
        invalid_doc = report is not None and run["error_class"] == "invalid_output"
        if invalid_doc:
            report, why = None, idoc.reason(conn, run["id"]) or "an invalid spool document"
        if report is not None and report.get("status") == "failed":
            report, why = None, idoc.failed_report_reason(report)
        if report is None:
            if error_class == "worktree_mismatch":
                # A `why_not_worktree` refusal (implementer only) takes `_claim_and_launch`'s path:
                # one `needs_input` hold with the full text, no failure_count change, one Korin
                # incident, off the typed event. Never a retry loop and never an immediate
                # triage-park.
                msg = ((run_json or {}).get("error") or why)[:500]
                with txn(conn):
                    event(conn, task_id, "worktree_mismatch", {"role": role, "error": msg},
                          run_pk=run["id"], run_id=run["run_id"], now=now)
                    blocks.block(conn, task_id, kind="needs_input", reason=((run_json or {}).get("error") or why)[:2000],
                                 run_pk=run["id"], run_id=run["run_id"], now=now, offer="unblock|reset")
                return
            restarted = idoc.close_failed_run(conn, cfg, run, run_json, error_class, role, task_id,
                                              review, why, invalid_doc, now)
            if restarted:
                self._launch_unspawned(conn, cfg, task_id, now)
            return
        ruling_of = self._ruling_of(run)
        found = None
        if ruling_of is None:
            report = guide_notes.drop_no_op_hunks(role, conn, task_id, run, report, now)
            if not review:
                self._note_git_lock(conn, task_id, report, run, now)
                blocks.note_optional_items(conn, task_id, report, run, now, task["body"])
                report_notes.note(conn, task_id, run, report, now)   # report.v1 `notes`
            judged = (review_scope.judged_document(conn, cfg, task, role, report,
                                                   self._work_root(cfg)) if review else
                      blocks.drop_optional_item_notes(
                          self._drop_git_lock_notes(report), task["body"]))
            judged = follow_ups.hold_own_files(conn, cfg, task, judged, self._work_root(cfg))
            if role == "reviewer" and review_findings.refuse(conn, run, judged, sp, now):
                return
            # Second oracle after the schema; `outcome` is the one router `$outcome` judges.
            outcome = None if review else self._route(judged)
            job_inputs = blind_gate.job_inputs(sp / "job.json")
            found = intent.check(role, judged, inputs=job_inputs, outcome=outcome, directory=self.intent_dir)
            if role == "guide":
                found = found + self._guide_structural_checks(report, job_inputs) \
                    + guide_notes.amendment_scope_violations(report) \
                    + guide_notes.rows_not_handed(report, job_inputs)
                if found:
                    return self._guide_contradiction(conn, run, found, now)
                # alignment_verdict.v1 `notes`, once the round is not refused
                report_notes.note(conn, task_id, run, report, now)
        with txn(conn):
            conn.execute("UPDATE task_runs SET handoff=? WHERE id=?", (json.dumps(report, ensure_ascii=False), run["id"]))
        if ruling_of is not None:
            lease.end_run(conn, run["id"], status="done", outcome="ruling", summary=report["recommended"], now=now)
            apply = blind_exempt.applier(conn, ruling_of, self._apply_ruling)
            apply(conn, cfg, run, report, ruling_of, now)
            return
        if found:
            self._contradiction(conn, cfg, run, sp, found, now)
            return
        if review:
            contract_audit.note(conn, task_id, run, role, judged, task["body"], now)  # D220
            lease.end_run(conn, run["id"], status="done", outcome="completed", summary=report.get("verdict"), now=now)
        elif outcome == "checkpoint":
            if checkpoint.on_report(conn, run, report, cfg, now=now) == "restarted":
                counts["restarted"] += 1
                self._launch_unspawned(conn, cfg, task_id, now)
        elif outcome == "hold":
            exec_outage.hold(conn, task_id, run, sp, judged, now)
        else:
            with txn(conn):
                lease.end_run(conn, run["id"], status="done", outcome="completed", now=now)
                follow_ups.file_cards(conn, self.home, cfg, run, report, now)
                transition(conn, task_id, "review", claim=True)
                event(conn, task_id, "request_review", None, run_pk=run["id"], run_id=run["run_id"], now=now)

    # -- guide rulings on contradictions ------------------------------------------------

    def _rulings(self, conn, cfg, now, counts):
        held = conn.execute("SELECT * FROM tasks WHERE status='triage'"
                            " AND block_kind IN ('guide_ruling', 'runaway', 'claims')").fetchall()
        for task in sorted({t["id"]: t for t in held + stall_ruling.wanted(conn)}.values(),
                           key=lambda t: t["created_at"]):    # L1: a held card's stall ruling
            self._guarded(conn, cfg, task["id"], now, counts,
                          lambda task=task: self._ruling_one(conn, cfg, task, now, counts))

    def _ruling_one(self, conn, cfg, task, now, counts):
        """One guide run per contradiction, retried once without a valid decision; a second miss
        goes to the owner as one outbox line. A ruling already applied or escalated is left alone.
        A card only reaches here once every review run of the round has ended; this checks again
        so a ruling never starts, or restarts, while a validator/reviewer/guide run of this task
        is still open."""
        ev = stall_ruling.pending(conn, task["id"]) or (
            runaway.pending_event(conn, task["id"]) if runaway.held(task) else next(
            (e for e in conn.execute("SELECT * FROM task_events WHERE task_id=?"
                                     " AND kind='contradiction' ORDER BY id DESC", (task["id"],))
             if json.loads(e["payload"] or "{}").get("routed") == "guide"), None))
        if ev is None or conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND id>? AND kind IN"
                                      " ('guide_ruling', 'ruling_failed') LIMIT 1", (task["id"], ev["id"])).fetchone():
            return
        if conn.execute("SELECT 1 FROM task_runs WHERE task_id=? AND profile IN (?,?,?,?)"
                        " AND ended_at IS NULL AND json_extract(COALESCE(metadata, '{}'),"
                        " '$.ruling_of') IS NULL LIMIT 1",
                        (task["id"],) + REVIEW_ROLES).fetchone():
            return
        runs = [r for r in conn.execute("SELECT * FROM task_runs WHERE task_id=? AND profile='guide' ORDER BY id",
                                        (task["id"],)) if self._ruling_of(r) == ev["id"]]
        if any(r["ended_at"] is None for r in runs):
            return
        if len(runs) >= REVIEW_TRIES:
            why = "; ".join(f"{r['run_id'][:8]}: {r['error'] or 'no valid decision'}" for r in runs)
            if ev["kind"] == stall_ruling.KIND:       # L2 at once, one incident (no page here)
                return stall_ruling.record_miss(conn, cfg, task, now, why)
            with txn(conn):
                event(conn, task["id"], "ruling_failed", {"contradiction": ev["id"], "error": why[:500]}, now=now)
                outbox.notify(conn, "blocked", f"{task['id']} the guide gave no valid ruling on its contradiction twice",
                              fields=[("error", why[:300])],
                              reply=f"python3 -m capsule human {cfg['board']} {task['id']} accept|reset '<answer>'",
                              thread_key=task["id"],
                              file_body=(f"capsule: {task['id']} the guide gave no valid ruling on its contradiction"
                                         f" twice ({why}) | reply: python3 -m capsule human {cfg['board']} {task['id']}"
                                         " accept|reset '<answer>'")[:1500], now=now)
            return
        route = config.route(cfg, "guide")
        if dispatch.hold(self, conn, cfg, route, now):
            return
        ws = workspace.clone(cfg["repo_url"], task["branch_name"], self._work_root(cfg))
        inputs = dict(self._guide_inputs(cfg, task, ws), ruling=self._ruling_packet(conn, ev),
                      interventions=self._human_entries(cfg, task["id"], conn))
        run_id = uuid.uuid4().hex
        with txn(conn):
            run_pk = lease.start_run(
                conn, task["id"], role="guide", claim_lock=f"{self.host}:{run_id}",
                expires=now + cfg["caps"]["claim_ttl"], run_id=run_id, now=now, runner="host",
                provider=route["provider"], account=route["account"], model=route["model"],
                metadata=config.run_meta(cfg, task["branch_name"], ruling_of=ev["id"]))
            event(conn, task["id"], "ruling_started", {"contradiction": ev["id"]}, run_pk=run_pk, run_id=run_id, now=now)
        try:
            self._launch(conn, cfg, self._run(conn, run_pk), task, ws=ws, now=now, inputs=inputs)
        except Exception as exc:
            lease.end_run(conn, run_pk, status="failed", outcome="spawn_failed",
                          error=f"{type(exc).__name__}: {exc}"[:2000], now=now)
            raise
        counts["ruling_runs"] += 1

    def _apply_ruling(self, conn, cfg, run, doc, ev_id, now):
        """The guide's decision.v1 on a contradiction: `document-wrong` reruns only the reviewer for
        its verdict (reason in its bundle), else resets the card; `example-wrong` accepts it and
        files an intent-change request (row 393 gate), `ask-owner` is one outbox line and the card
        stays in triage. The first two are reversible, so the tick applies them."""
        task_id, choice = run["task_id"], doc["recommended"]
        reason = doc.get("context") or doc["question"]
        ev = conn.execute("SELECT payload, run_id FROM task_events WHERE id=?", (ev_id,)).fetchone()
        payload = json.loads(ev["payload"]) if ev is not None and ev["payload"] else {}
        role = payload.get("role")
        ids = ", ".join(f["id"] for f in payload.get("examples", []))
        fingerprint = sorted(f["id"] for f in payload.get("examples", []))
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        waiting = task is not None and task["status"] == "triage" and task["block_kind"] == "guide_ruling"
        # Loop cap: a document-wrong ruling can recur, as a reviewer rerun or as a reset;
        # task_events keeps the count across a tick restart.
        looped = [i for i in fingerprint if self._document_wrong_resets(conn, task_id, i) >= RULING_RESETS]
        loop = waiting and choice == "document-wrong" and bool(looped)
        with txn(conn):
            applied = waiting and choice not in bundles.ASKS and not loop
            event(conn, task_id, "guide_ruling", {"choice": choice, "reason": reason, "contradiction": ev_id,
                                                  "role": role, "examples": ids, "fingerprint": fingerprint,
                                                  "applied": applied},
                  run_pk=run["id"], run_id=run["run_id"], now=now)
            if not waiting:
                return
            if choice not in bundles.ASKS and payload.get("ruling_card"):  # stands past the cap
                ruling_sweep.close_card(conn, payload["ruling_card"], now)
            if loop:
                transition(conn, task_id, "blocked", claim=True, block_kind="contradiction_loop")
                event(conn, task_id, "contradiction_loop", {"examples": looped, "resets": RULING_RESETS,
                                                            "reason": reason, "role": role}, now=now)
                outbox.notify(conn, "blocked", f"{task_id} blocked (contradiction_loop): {role} intent example ruled"
                              f" document-wrong {RULING_RESETS} times came back", fields=[("examples", ", ".join(looped))],
                              reply=f"python3 -m capsule human {cfg['board']} {task_id} accept|reset '<answer>'",
                              thread_key=task_id,
                              file_body=(f"capsule: {task_id} blocked (contradiction_loop): intent example"
                                         f" {', '.join(looped)} ({role}) was ruled document-wrong {RULING_RESETS} times"
                                         f" and came back again; the guide's last reason: {reason} | reply: python3 -m"
                                         f" capsule human {cfg['board']} {task_id} accept|reset '<answer>'")[:1500],
                              now=now)
                return
            if choice in bundles.ASKS:
                outbox.notify(conn, "blocked", f"{task_id} the guide asks you to rule on a contradiction ({role})",
                              fields=[("examples", ids), ("question", doc["question"])],
                              reply=f"python3 -m capsule human {cfg['board']} {task_id} accept|reset '<answer>'",
                              thread_key=task_id,
                              file_body=(f"capsule: {task_id} the guide asks you to rule on a contradiction ({role}:"
                                         f" {ids}): {doc['question']} {reason} | reply: python3 -m capsule human"
                                         f" {cfg['board']} {task_id} accept|reset '<answer>'")[:1500], now=now)
                return
            if choice == "document-wrong":
                reviewer_rerun.apply(conn, task_id, role, ev["run_id"], reason, now)
            else:
                blocks.accept(conn, task_id, now=now)
                db.new_task(conn, title=f"intent change (row 393 gate): contracts/intent/{role}.jsonl {ids}"[:120],
                            body=(f"The guide ruled example-wrong on {task_id}'s contradiction ({ids}): {reason}\n"
                                  "Change contracts/intent/" f"{role}.jsonl and re-pin its SHA256SUMS line in the"
                                  " alignment session with the owner (row 393 gate; rows 395, 419:"
                                  " no worker or guide edits it)."),
                            status="triage", request_needs="decision",
                            branch=task["branch_name"], parents=(task_id,), now=now)

    def _report(self, sp, run, conn=None, now=None):
        """The run's validated document, or (None, why). Its schema and envelope must be this run's."""
        doc, errs = spool.read_report(sp, self.schemas)
        if doc is None:
            return None, errs[0] if errs else "no report"
        ruling_of = self._ruling_of(run)
        ruling = ruling_of is not None
        expected = RULING_SCHEMA if ruling else contracts.schema_for(run["profile"])
        if doc.get("schema") != expected:
            return None, f"report schema {doc.get('schema')!r} is not the {run['profile']}'s ({expected})"
        offered = blind_exempt.offered(conn, ruling_of, RULINGS) if ruling else RULINGS
        if ruling and (doc["recommended"] not in bundles.accepted(offered)
                       or doc["recommended"] not in {o["id"] for o in doc["options"]}):
            return None, f"ruling {doc['recommended']!r} is not one of the offered {offered}"
        if ruling and runaway.is_runaway(conn, ruling_of) and runaway.lacks_fix_list(doc):
            return None, "a runaway `continue` needs the complete fix list in `context`"
        if ruling and (err := stall_ruling.check(conn, ruling_of, doc)):
            return None, err
        if (doc.get("run_id"), doc.get("task_id"), doc.get("claim_lock")) != (run["run_id"], run["task_id"], run["claim_lock"]):
            return None, "report envelope does not match the run (run_id/task_id/claim_lock)"
        if doc.get("status") == "checkpoint" and "checkpoint" not in doc:
            return None, "status checkpoint without a checkpoint object"
        if run["profile"] == "validator":
            outputs = blind_gate.outputs(sp.parent, run)
            err = (validator_report.not_run_violation(doc, outputs, validator_report.is_repeat(sp))
                   or validator_report.bare_fail_violation(doc))
            if err:
                return None, validator_report.reject(conn, run, err, now, outputs)
            doc = validator_report.withhold(doc, outputs)       # a blind job: no free text
        # warns; blocks rules 1 and 12 from BLOCKING_FROM
        err = style_lint.gate(doc, run, conn=conn, now=now, sp=sp)
        if err is None and run["profile"] == "validator":
            validator_report.note_error(conn, run, doc, sp, now)
        return (None, err) if err else (doc, None)

    def _pause(self, conn, run, retry_after, now, run_json=None):
        """Pause the board, and the account that hit the limit (`idoc.pause_scope`)."""
        until = int(retry_after or now + RATE_LIMIT_DEFAULT)
        was_paused = self._paused(conn, run["account"], now)     # before `pause_scope` writes it
        keys, why = idoc.pause_scope(conn, self.cfg["board"], run, run_json, now)
        for key in keys:
            held, _ = db.guard_get(conn, key)   # a live human pause keeps its reason and since
            held = held if isinstance(held, dict) and held.get("until", 0) > now else {}
            db.guard_set(conn, key, {**held, "until": max(until, held.get("until", 0))}, now=now)
        if keys and not was_paused:         # one line per pause, not one per rate-limited run
            outbox.notify(conn, "paused", f"{self.cfg['board']} paused: {why}",
                          fields=[("until", time.strftime("%Y-%m-%d %H:%M", time.localtime(until))),
                                  *[("result", t) for t in [idoc.refusal(run_json)] if t]],
                          thread_key=run["task_id"], now=now)
        return until

    def _release_rate_limited(self, conn, run, review, until, now):
        """The run ends without a failure; the card goes back to ready."""
        with txn(conn):
            lease.end_run(conn, run["id"], status="released", outcome="rate_limited", error=f"paused until {until}",
                          now=now)
            if not review:
                transition(conn, run["task_id"], "ready", frm=("running",), claim=True)
            event(conn, run["task_id"], "rate_limited", {"until": until}, run_pk=run["id"], run_id=run["run_id"],
                  now=now)

    # -- review stage ---------------------------------------------------------------

    def _start_reviews(self, conn, cfg, task, impl, missing, now, retry_errors=None):
        """Up to four read-only, mutually blind runs; contracts run in a fresh strong container."""
        if self.contracts.running(task, impl, now):
            return 0               # the suite owns the clone, and holds no seat, until it ends
        todo = dispatch.admit_reviews(conn, cfg, self.home, task, missing, now)
        ws, started = (self._clone(conn, cfg, task["id"], task["branch_name"]) if todo else None), 0
        if todo and epic_sync.hold_half_merge(conn, task, ws, None, now, self._start_sha): return 0
        for role in todo:                      # no seat free: no clone, no fetch
            route = config.route(cfg, role)
            if self._paused(conn, route["account"], now) or (role == "validator" and (
                    dispatch.tier_down(conn, task, self.host_tiers, self.mac_hold, now, True))):
                continue                     # a strong (or mac) need waits, never falls back
            run_id = uuid.uuid4().hex
            if role == "validator":
                got = self.contracts.step(self, conn, cfg, task, impl, ws, now, run_id)
                if got is None:
                    return started                 # the contract job is still running
                run_id, inputs = got[0], {"outputs": got[1]}
            elif role == "reviewer":
                done = json.loads(impl["handoff"] or "{}") if impl is not None else {}
                inputs = {"diff": workspace.diff(ws, self._start_sha(conn, task["id"])),
                          "round": restart.round_for(conn, cfg, task["id"]),
                          "guide_ruling": reviewer_rerun.reason(
                              conn, task["id"], self._round_floor(conn, task["id"], impl)),
                          **self._reviewer_carry(conn, task["id"], done)}
            elif role == security_review.ROLE:
                inputs = security_review.inputs(task, ws, self._start_sha(conn, task["id"]))
            else:
                inputs = self._guide_inputs(cfg, task, ws, impl)
                if self._first_screen(conn, cfg, task, ws) is not None:
                    inputs["first_screen"] = True      # the guide knows this card goes to the owner
                if out_of_set := review_scope.previous_out_of_set(conn, task, review_scope.open_round_touched(conn, task["id"], ws)):
                    inputs["out_of_set"] = out_of_set   # context on a rerun, never judged
            if role != "validator": inputs["interventions"] = self._human_entries(cfg, task["id"])
            inputs.update(card_hash=bundles.card_hash(task["body"]),    # the card as it is now
                          **blind_gate.inputs(conn, self._spool_root(cfg), task["id"], role, cfg))
            if err := (retry_errors or {}).get(role): inputs["invalid_document_error"] = err
            metadata = config.lane_meta(cfg, task["branch_name"], review_of=impl["run_id"])
            if role == "guide": metadata["guide_uncited"] = inputs["uncited"]
            with txn(conn):
                run_pk = lease.start_run(
                    conn, task["id"], role=role, claim_lock=f"{self.host}:{run_id}",
                    expires=now + cfg["caps"]["claim_ttl"], run_id=run_id, now=now, runner="host",
                    provider=route["provider"], account=route["account"], model=route["model"],
                    metadata=json.dumps(metadata))
                event(conn, task["id"], "review_started", {"role": role}, run_pk=run_pk, run_id=run_id, now=now)
            try:
                self._launch(conn, cfg, self._run(conn, run_pk), task, ws=ws, now=now, inputs=inputs)
            except Exception as exc:
                # a review run holds no lease, so nothing else would ever close it
                err = incident_inputs.spawn_error(
                    conn, exc, self._spool_root(cfg), task["id"], run_id, cfg)
                with txn(conn):
                    lease.end_run(conn, run_pk, status="failed", outcome="spawn_failed",
                                  error=err[:2000], now=now)
                    event(conn, task["id"], "spawn_failed", {"role": role, "error": err[:500]},
                          run_pk=run_pk, run_id=run_id, now=now)
                raise
            started += 1
        return started

    def _merge_gate(self, conn, task_id, ws):
        """The landing gate (size, Z8, mutation) as one more contract command, so a card fails in
        review, not at the lander: the host parks a snapshot ref, the command fetches it from the
        read-only git dir (at its container path) into a scratch repo. No gate script, no gate."""
        if not (Path(ws) / "tests" / "mutation_check.py").is_file():
            return []
        (ps, base), git = review_scope.bases(ws, self._start_sha(conn, task_id)), workspace._git
        git("add", "-A", *workspace.SCRATCH_EXCLUDE, ws=ws)
        snap = git(*workspace.IDENTITY, "commit-tree", git("write-tree", ws=ws).stdout.strip(),
                   *(x for p in ps for x in ("-p", p)), "-m", "review", ws=ws).stdout.strip()
        git("update-ref", "refs/capsule/review", snap, ws=ws)
        gd = docker.container_git_dir(ws, workspace.gitdir(ws))   # where the container sees it
        return ["g=$(mktemp -d) && git init -q $g && cd $g && git -c safe.directory='*' fetch -q "
                f"{shlex.quote(gd)} refs/capsule/review && git checkout -q "
                f"--detach FETCH_HEAD && python3 tests/mutation_check.py --base {base} "
                f"--head {snap}"]

    def _guide_inputs(self, cfg, task, ws, impl=None, sha=None):
        """The spec section(s) the card names (§N.N, from the optional `spec` path in the epic clone)
        and its rows. The epic fallback (config `epics[]` object form) applies per field: the
        epic's `spec`/`spec_section` fills in only when the card cites no §, and the epic's `rows`
        are unioned into the card's own cited rows (deduplicated, sorted). `uncited` is set only
        when the card and its epic leave both the section and the rows empty, so the bundle
        (`bundles.guide`) says so instead of rendering two empty headings.
        The guide reads four labelled inputs: the spec section, the cited rows (with text), the
        card as it is now (by hash, `bundles._card_section`, never a `task["body"]` snapshot) and
        the implementer's report under review (`impl`; None when no implementer run is available,
        e.g. a guide ruling packet).
        The spec is read at `sha` if given, else the epic branch's tip (`workspace.tip`, the fetched
        `origin/<branch>`), never from the clone's working tree: `clone` fetches but never
        fast-forwards the local branch, and a run could edit the tree. `spec_sha`/`spec_path`
        name what was read."""
        body = task["body"] or ""
        numbers = cited_sections(body)
        rows = cited_rows(body)
        card_rows = list(rows)                     # the card's OWN citations, before any union
        section, read = "", {}
        sha = sha or workspace.tip(ws, task["branch_name"])
        def at_tip(rel):
            try:
                text = workspace.read_at(ws, sha, rel)
            except ValueError:           # an absolute or `..` spec path in config reads as no spec
                return ""
            if text is not None:
                read.update(spec_sha=sha, spec_path=rel)
            return text or ""
        if numbers:
            section = card_spec_section(body, cfg.get("spec"), at_tip, numbers, read,
                                        cfg.get("repo_url"))
        epic = config.epic_for_branch(cfg, task["branch_name"])
        if epic:
            if not numbers:
                epic_spec, heading = epic.get("spec"), epic.get("spec_section")
                if epic_spec and heading:
                    section = epic_spec_section(at_tip(epic_spec), heading)
            rows = sorted(set(rows) | set(epic.get("rows") or []))
        impl_report = None
        if impl is not None and impl["handoff"]:
            try:
                impl_report = json.loads(impl["handoff"])
            except ValueError:
                impl_report = None
        # Row-text BUDGET priority, never the displayed `rows` order (numerically sorted above):
        # the card's own citations first, an epic-fallback row after; newest (highest number)
        # first within each group, so a low-numbered epic-only row never starves a card row.
        priority = [str(n) for n in sorted(card_rows, reverse=True)]
        priority += [str(n) for n in sorted(set(rows) - set(card_rows), reverse=True)]
        return {"spec_section": section, "rows": rows, "uncited": not section and not rows,
                "row_texts": self._row_texts(cfg, rows, priority), "spec_sha": read.get("spec_sha", ""),
                "spec_path": read.get("spec_path", ""), "impl_report": impl_report}

    @staticmethod
    def _row_texts(cfg, rows, priority=None):
        """The cited rows' texts from the vault decisions note (the note Korin's closure gate reads,
        `incidents.settings`); {} when there are no rows or the note is absent (another machine).
        Each text is capped and their combined total budgeted, in `priority` order
        (`_cap_row_texts`)."""
        if not rows:
            return {}
        ks = incidents.settings(cfg)
        try:
            text = (Path(ks["vault"]) / ks["decisions_note"]).read_text("utf-8", "replace")
        except (OSError, TypeError):  # missing, unreadable, no vault: the guide gets numbers only
            return {}
        return _cap_row_texts(decision_rows(text, rows), priority)

    def _verdict(self, conn, cfg, task, impl, latest, now):
        docs = {role: json.loads(r["handoff"]) if r["handoff"] else None for role, r in latest.items()}
        self._mark_overrides(conn, task["id"], docs, now)
        overridden = self._overridden_lines(docs)
        with txn(conn):   # review_correct: validator overrides only; null: no validator doc
            conn.execute("UPDATE task_runs SET metadata=json_set(metadata, '$.review_correct',"
                         " json(?)) WHERE id=?", (json.dumps(docs["validator"] and not overridden),
                                                  impl["id"]))
            if overridden:   # durable evidence, whether or not the round still fails
                event(conn, task["id"], "checks_overridden", {"checks": overridden}, now=now)
        ws = self._clone(conn, cfg, task["id"], task["branch_name"])
        base = self._start_sha(conn, task["id"])
        review_scope.note_out_of_set(conn, task, docs, (touched := workspace.changed(ws, base)), now)
        # a planted file must not reach the next run; main's merged-in copies are not planted
        protected = [p for p in protected_scope.split(ws, base) if not _is_bytecode_cache(ws, p)]
        failures = protected_scope.refuse(conn, task["id"], protected, now)
        failures += self._content_findings(docs, blind_gate.on(cfg))
        failures += check_gate.failures(cfg, conn, self._spool_root(cfg), latest["validator"])
        # a role without a valid document after its retry is review-infra, never a card failure.
        rr = security_review.roles(cfg)
        infra = [f"{role} run {latest[role]['run_id']}: {latest[role]['error'] or 'no valid document'}"
                 for role in rr if docs[role] is None]
        infra_cause = idoc.infra_cause([r for r in rr if docs[r] is None], latest)
        guide, run_id = docs["guide"], impl["run_id"]
        conflict_rows = (guide.get("spec_conflict_rows") or []) if (not protected and guide is not None) else []
        guide_aligned_uncited = False   # this path reaches commit, so its guide gate reads pass
        if not protected and guide is not None and guide["verdict"] == "drifted":
            if not guide["rows"] and self._guide_uncited(latest["guide"]):
                # no spec citation reached the guide: its "drift" judges the packet, not the work
                guide_aligned_uncited = True
                with txn(conn):
                    event(conn, task["id"], "aligned_uncited", {"reason": guide["reason"]},
                          run_pk=latest["guide"]["id"], run_id=latest["guide"]["run_id"], now=now)
                    outbox.notify(conn, "running", f"{task['id']} guide aligned_uncited; review continues",
                                  file_body=(f"capsule: {task['id']} guide had no spec citation to judge against"
                                             " (aligned_uncited); review continues"),
                                  thread_key=task["id"], now=now)
            else:
                # `spec_conflict_rows` is additive to `drifted`, never a routing branch of its own:
                # the drift's reason comes first, then the round's content findings and the
                # spec-conflict remedy, all in the SAME needs_input hold, never a second block.
                rows = ", ".join(str(r) for r in guide["rows"])
                reason = f"alignment drifted (rows {rows}): {guide['reason']}"
                remedy = self._spec_conflict_remedy(conflict_rows) if conflict_rows else ""
                reason = validator_report.hold_reason(reason, failures, remedy)
                with txn(conn):
                    if conflict_rows:
                        self._record_spec_stale_event(conn, cfg, task, latest["guide"], conflict_rows, now)
                    blocks.block(conn, task["id"], kind="needs_input", reason=reason,
                                 run_id=run_id, now=now)
                return
        elif not protected and guide is not None and conflict_rows:
            # Aligned, but a row the guide cited contradicts the spec section itself: a dedicated
            # hold, never an implementer reset (neither the work nor the row is at fault; the
            # implementer may not touch the spec section). `accept|reset` only (`_notify`): owner
            # amends the spec on the branch or resets; nothing for `unblock` to release into.
            # a spec_stale hold still mentions the round's reviewer/validator failures
            remedy = self._spec_conflict_remedy(conflict_rows)
            reason = validator_report.hold_reason(remedy, failures)
            with txn(conn):
                self._record_spec_stale_event(conn, cfg, task, latest["guide"], conflict_rows, now)
                blocks.block(conn, task["id"], kind="spec_stale", reason=reason, run_id=run_id,
                             now=now)
            return
        if missing_module.gate_hold(self, (conn, cfg, task, latest, ws, base), infra, infra_cause,
                                    failures, run_id, now):
            return          # blind gate, or a module the runner lacks: held, no finding counted
        if failures:
            text = "Review failed:\n" + "\n".join(f"- {f}" for f in failures)
            if infra:
                text += "\nNo valid verdict (review infra, not counted):\n" + "\n".join(f"- {f}" for f in infra)
                with txn(conn):     # the review-infra incident is not lost beside the failure
                    event(conn, task["id"], "blocked", {
                        "reason": ("review run failed twice, no valid verdict: "
                                   + "; ".join(infra))[:2000],
                        "kind": "review_infra", "recurrences": 0,
                        **({"cause": infra_cause} if infra_cause else {})}, run_id=run_id, now=now)
            if restart.stop(conn, cfg, task, impl, now, text):   # the ladder's stop, then runaway's
                return                         # stop-the-line
            with txn(conn):
                conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?,?,?,?)",
                             (task["id"], "capsule", text, now))
                blocks.record_failure(conn, task["id"], reason=text, limit=restart.failure_cap(cfg),
                                      back_to="ready", run_id=run_id, now=now)
            return
        if infra:
            with txn(conn):
                blocks.block(conn, task["id"], kind="review_infra",
                             reason=("review run failed twice, no valid verdict: " + "; ".join(infra))[:2000],
                             run_id=run_id, now=now, cause=infra_cause)
            return
        epic_sync.finish_gate_pass(self, conn, cfg, task, ws, touched, impl, docs, run_id, now,
                                   guide_aligned_uncited, overridden, rr)

    @staticmethod
    def _gate_word(doc):
        """pass|fail|infra for one review role's document; `skipped` when the role never ran.
        An `overridden` verdict reached commit, so it reads `pass`; the override itself is listed
        in `gates.overrides`."""
        if doc is None:
            return "infra"
        verdict = doc.get("verdict")
        return {"pass": "pass", "aligned": "pass", "aligned_uncited": "pass", "overridden": "pass",
                "fail": "fail", "drifted": "fail"}.get(verdict, "skipped")

    @staticmethod
    def _mark_overrides(conn, task_id, docs, now):
        """A validator assertion whose `cmd` (the check id) carries an unexpired override for this
        task is relabelled `overridden` in place, its `excerpt` set to the override's reason.
        `_findings`/`_content_findings` pick only `fail`/`not_run`, so an `overridden` assertion
        drops out of the verdict's failures and the rerun bundle. Its `result` is never rewritten
        to `pass`, so the raw document still shows what was skipped and why. A `fail`/`not_run`
        assertion whose override just lapsed is announced once via `override.note_expiry` and
        judged normally."""
        validator = docs.get("validator")
        assertions = validator.get("assertions") if validator else None
        if not assertions:
            return docs
        flipped = False
        for a in assertions:
            if a.get("result") in ("fail", "not_run"):
                cmd = a.get("cmd")
                ov = override.active(conn, task_id, cmd, now)
                if ov is not None:
                    a["result"] = "overridden"
                    a["excerpt"] = f"override (fix card {ov.get('fix_card')}): {ov.get('reason')}"
                    flipped = True
                else:
                    lapsed = override.last(conn, task_id, cmd)
                    if lapsed is not None:
                        override.note_expiry(conn, task_id, cmd, lapsed, now)
        # The document's `verdict` is the validator's overall judgment. It moves from `fail` to
        # `overridden` (never `pass`) only when NOTHING still blocks the round (no surviving
        # `fail` or `not_run`): an override skips exactly the check it names.
        if flipped and validator.get("verdict") != "pass" and not any(
                a.get("result") in ("fail", "not_run") for a in assertions):
            validator["verdict"] = "overridden"
        return docs

    # -- incidents: Korin ----------------------------------------------------------

    def _incidents(self, conn, cfg, now, counts):
        """Open incidents from this pass's typed events, dispatch Korin (only with `korin.enabled` and
        `routing.korin`), then try the closure gate. A failure here is counted and printed; it never
        stops dispatch."""
        try:
            ks = incidents.settings(cfg)
            self._publish_incidents_guarded(conn, cfg, now, counts)
            tick_pass(conn, cfg, ks, counts, now=now, spool_root=self._spool_root(cfg),
                      human_dir=spool.human_dir(self.home, cfg["board"]),
                      count=lambda ids: invariants.count(conn, cfg, now, ids))
            if ks["enabled"] and KORIN in cfg["routing"]:
                counts["korin_runs"] = self._korin_dispatch(conn, cfg, ks, now)
            counts["incidents_closed"] = self._incident_gate(conn, ks, now)
        except Exception as exc:
            counts["tick_errors"] += 1
            print(f"capsule tick: incidents step failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    def _korin_dispatch(self, conn, cfg, ks, now):
        """One Korin run per open incident (at most `korin.max_running` live), on the incident's own
        Korin card (assignee `korin`, no branch: never dispatched as work). A post-mortem whose run
        ended without applying goes back to `open`; after KORIN_TRIES failed runs the incident stays
        open and the outbox says so once."""
        if korin_checkout.refuse(conn, cfg, ks, now):
            return 0
        for inc in incidents.listing(conn, "postmortem"):
            if not inc["korin_task"] or conn.execute(
                    "SELECT 1 FROM task_runs WHERE task_id=? AND profile=? AND ended_at IS NULL",
                    (inc["korin_task"], KORIN)).fetchone() is None:
                with txn(conn):
                    conn.execute("UPDATE incidents SET status='open' WHERE id=? AND status='postmortem'", (inc["id"],))
        live = conn.execute("SELECT COUNT(*) FROM task_runs WHERE profile=? AND ended_at IS NULL", (KORIN,)).fetchone()[0]
        route, started = config.route(cfg, KORIN), 0
        for inc in incidents.listing(conn, "open"):
            if inc["opened_at"] < (ks["since"] or 0):
                continue                      # pre-Korin backlog: never dispatched
            if live + started >= ks["max_running"] or dispatch.hold(self, conn, cfg, route, now):
                break
            card = inc["korin_task"] or self._korin_card(conn, inc, now)
            failed = conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=? AND profile=? AND ended_at IS NOT NULL"
                                  " AND COALESCE(outcome, '') NOT IN ('completed', 'rate_limited')",
                                  (card, KORIN)).fetchone()[0]
            if failed >= KORIN_TRIES:
                if conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='korin_gave_up'", (card,)).fetchone() is None:
                    with txn(conn):
                        event(conn, card, "korin_gave_up", {"incident": inc["id"], "runs": failed}, now=now)
                        outbox.notify(conn, "incident", f"Korin failed twice on incident {inc['id']}",
                                      fields=[("kind", inc["kind"]), ("card", card)], page=True,
                                      thread_key=inc["task_id"], now=now,
                                      file_body=f"capsule: Korin failed {failed} post-mortem runs on incident {inc['id']}"
                                                f" ({inc['kind']}); it stays open"
                                                f" for the owner (card {card})")
                continue
            run_id = uuid.uuid4().hex
            with txn(conn):
                run_pk = lease.start_run(conn, card, role=KORIN, claim_lock=f"{self.host}:{run_id}",
                                         expires=now + cfg["caps"]["claim_ttl"], run_id=run_id, now=now, runner="host",
                                         provider=route["provider"], account=route["account"], model=route["model"],
                                         metadata=config.run_meta(cfg, None, incident=inc["id"]))
                conn.execute("UPDATE incidents SET status='postmortem' WHERE id=?", (inc["id"],))
                event(conn, card, "korin_started", {"incident": inc["id"]}, run_pk=run_pk, run_id=run_id, now=now)
            task = conn.execute("SELECT * FROM tasks WHERE id=?", (card,)).fetchone()
            try:
                self._launch(conn, cfg, self._run(conn, run_pk), task, ws=None, now=now,
                             inputs=self._korin_inputs(conn, cfg, incidents.get(conn, inc["id"])))
            except Exception as exc:
                with txn(conn):
                    lease.end_run(conn, run_pk, status="failed", outcome="spawn_failed",
                                  error=f"{type(exc).__name__}: {exc}"[:2000], now=now)
                    conn.execute("UPDATE incidents SET status='open' WHERE id=? AND status='postmortem'", (inc["id"],))
                raise
            started += 1
        return started

    @staticmethod
    def _korin_card(conn, inc, now):
        card = db.new_task(conn, title=f"Korin post-mortem {inc['id']} ({inc['kind']})",
                           body=(f"Post-mortem of incident {inc['id']} (row 456). Root-cause key:"
                                 f" {incident_inputs.record(inc)['root_key']}."
                                 f" Task: {inc['task_id'] or 'none'}; run: {inc['run_id'] or 'none'}."),
                           assignee=KORIN, status="todo", now=now)
        with txn(conn):
            conn.execute("UPDATE incidents SET korin_task=? WHERE id=?", (card, inc["id"]))
        return card

    def _korin_inputs(self, conn, cfg, inc):
        """The contract's `gets`: the record, its evidence paths, and the logs' tails (each run's
        worker.log, the board events the evidence names, the outbox log's tail)."""
        evidence, logs = json.loads(inc["evidence"] or "[]"), []
        ids = [int(e.split(":", 1)[1]) for e in evidence if e.startswith("event:") and e.split(":", 1)[1].isdigit()]
        blind = incident_inputs.blind_incident(evidence, inc["role"])    # ids and paths only (D161)
        if ids and not blind:
            rows = conn.execute(f"SELECT id, task_id, kind, payload, created_at FROM task_events WHERE id IN"
                                f" ({','.join('?' * len(ids))}) ORDER BY id", ids).fetchall()
            logs.append({"path": f"board events {ids}", "tail": "\n".join(
                incident_inputs.event_line(conn, r) for r in rows)[-LOG_TAIL:]})
        tails = [incident_inputs.first_tail((Path(e) / "worker.log", Path(e)), LOG_TAIL)
                 for e in evidence if not e.startswith("event:")]
        logs += [t for t in tails if t]
        if log := incident_inputs.first_tail((paths.outbox_log(self.home, self.board),), LOG_TAIL):
            logs.append({**log, "tail": incident_inputs.outbox_tail(log["tail"])})
        return {"incident": incident_inputs.record(inc), "logs": [] if blind else logs,
                "evidence": incident_inputs.readable(evidence)}

    def _close_korin(self, conn, cfg, run, sp, run_json, error_class, now):
        """Apply Korin's `postmortem.v1`: the schema, the envelope, `incidents.postmortem_problems`
        (the intent examples are its second oracle). A good document, in one transaction: the fix
        card (blocked; post-mortem plus prior verdicts), the vault note, the record filled, the
        incident `fix_pending`, the Korin card done, the decision-row text in the outbox."""
        inc_id = json.loads(run["metadata"] or "{}").get("incident")
        inc = incidents.get(conn, inc_id) if inc_id else None
        card = run["task_id"]
        if error_class in blocks.PAUSE_CLASSES:
            until = self._pause(conn, run, (run_json or {}).get("retry_after"), now, run_json)
            with txn(conn):
                lease.end_run(conn, run["id"], status="released", outcome="rate_limited", error=f"paused until {until}",
                              now=now)
                conn.execute("UPDATE incidents SET status='open' WHERE id=? AND status='postmortem'", (inc_id,))
            return
        if run_json is None or error_class:
            report, why = None, ("worker exited without a readable run.json" if run_json is None
                                 else f"error_class {error_class}")
        else:
            report, why = self._report(sp, run, conn, now)
        if report is not None and inc is None:
            report, why = None, f"no incident {inc_id!r} for this Korin run"
        if report is not None:
            problems = incidents.postmortem_problems(report, inc, conn=conn)
            if problems:
                report, why = None, "; ".join(problems)
        if report is None:
            with txn(conn):
                lease.end_run(conn, run["id"], status="failed", outcome="failed", error=why[:2000], now=now)
                conn.execute("UPDATE incidents SET status='open' WHERE id=? AND status='postmortem'", (inc_id,))
                event(conn, card, "korin_failed", {"incident": inc_id, "error": why[:500]}, run_pk=run["id"],
                      run_id=run["run_id"], now=now)
            return
        ks = incidents.settings(cfg)
        rel = incidents.note_rel(inc)
        branch, assignee = incidents.fix_route(conn, cfg, inc)
        with txn(conn):
            card_doc = fix_card_quotes.quoted(report, spool.human_dir(self.home, cfg["board"]))
            fix = incidents.file_fix_card(conn, cfg, inc, card_doc, branch, assignee, now)
            incidents.apply_postmortem(conn, inc_id, report, fix_card=fix, note_path=rel, now=now)
            conn.execute("UPDATE task_runs SET handoff=? WHERE id=?", (json.dumps(report, ensure_ascii=False), run["id"]))
            lease.end_run(conn, run["id"], status="done", outcome="completed", summary=f"incident {inc_id}: fix card {fix}",
                          now=now)
            transition(conn, card, "done", completed_at=now)
            event(conn, card, "korin_applied", {"incident": inc_id, "fix_card": fix, "note": rel}, run_pk=run["id"],
                  run_id=run["run_id"], now=now)
            outbox.notify(conn, "incident", f"incident {inc_id} post-mortem: record its decision row",
                          fields=[("fix card", fix), ("note", rel)], thread_key=inc["task_id"],
                          file_body=(f"capsule: incident {inc_id} ({inc['kind']}) post-mortem applied; fix card {fix}"
                                     f" (blocked); note {rel}; owner records the decision row, then"
                                     f" `python3 -m capsule incidents row {inc_id} <n>`: {report['decision_row_text']}")[:3500],
                          now=now)
        # Written after the commit (no orphan note on rollback); a failed write leaves the
        # incident fix_pending with its note missing, which the gate refuses.
        try:
            if not ks["vault"]:
                raise OSError("no notes vault configured (host.json vault.path)")
            note = incidents.render_note(inc, report, fix, now=now, watch_days=ks["watch_days"])
            safe_write.atomic_write(ks["vault"], ks["vault"] / rel, note, create_dirs=True)
        except OSError as exc:
            outbox.notify(conn, "incident", f"incident {inc_id}: post-mortem note not written",
                          fields=[("error", str(exc)[:300])], thread_key=inc["task_id"],
                          file_body=f"capsule: incident {inc_id} note {rel} not written: {exc}"[:1000], now=now)
            return
        if ks["vault_push"] and not self._vault_push(ks["vault"], rel, inc_id):
            outbox.notify(conn, "incident", f"incident {inc_id}: vault push of the note failed",
                          fields=[("note", rel)], thread_key=inc["task_id"],
                          file_body=f"capsule: incident {inc_id} note {rel} written but not pushed", now=now)

    @staticmethod
    def _vault_push(vault, rel, inc_id):
        """Best effort, through maintenance/vault_git.py (the vault's one push path); never raises.
        Returns whether the push landed (the caller reports a False)."""
        try:
            sys.path.insert(0, str(paths.REPO / "maintenance"))
            import vault_git
            return bool(vault_git.push(vault, [rel], f"maint(korin): post-mortem {inc_id}"))
        except Exception as exc:
            print(f"capsule tick: vault push of {rel} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return False

    @staticmethod
    def _record_dispatch(conn, now):
        """The tick's own dead-man clock: `guard_state` key `last_dispatch_at`, set whenever this
        pass's `_dispatch` claimed and launched at least one card. It is the only source of
        `capsule_last_dispatch_timestamp`; the detector report carries no timestamp. Best effort,
        never raises past this method."""
        try:
            state, version = db.guard_get(conn, metrics.LAST_DISPATCH_GUARD)
            db.guard_set(conn, metrics.LAST_DISPATCH_GUARD, {"ts": now}, expect_version=version, now=now)
        except Exception as exc:
            print(f"capsule tick: recording the dispatch clock failed: {type(exc).__name__}: {exc}", file=sys.stderr)

    @staticmethod
    def _last_dispatch_ts(conn):
        """The epoch second of the last pass that dispatched anything on this board:
        `guard_state` key `last_dispatch_at` when recorded, else the newest
        `task_runs.started_at` (a board dispatched before this guard existed is not "no data").
        None only when `task_runs` is empty."""
        state, _ = db.guard_get(conn, metrics.LAST_DISPATCH_GUARD)
        if isinstance(state, dict) and state.get("ts") is not None:
            return state["ts"]
        row = conn.execute("SELECT MAX(started_at) AS ts FROM task_runs").fetchone()
        return row["ts"] if row is not None else None

    @staticmethod
    def _check_cache_staleness(conn, now):
        """Print one stderr line on each transition BETWEEN cache states
        (`metrics.CACHE_STATES`: "fresh"/"stale"/"missing"/"corrupt"), never on every pass the
        cache stays in one state. No `guard_state` memory defaults to "fresh", so a cache that is
        already stale/missing/corrupt on the first pass still gets its one line. Latched in
        `guard_state` key `unblocked_cache_state`; refreshes first, so a line = refresh failing."""
        try:
            metrics._refresh_cache(now=now)
            state = metrics.cache_state(now=now)
            prev, version = db.guard_get(conn, "unblocked_cache_state")
            prev_state = (prev or {}).get("state", "fresh") if prev is not None else "fresh"
            if state != prev_state:
                if state == "fresh":
                    print(f"capsule tick: unblocked cache fresh again (was {prev_state})", file=sys.stderr)
                elif state == "stale":
                    print(f"capsule tick: unblocked cache stale (age > {metrics.CACHE_MAX_AGE_SECONDS}s)"
                          " - metric absent", file=sys.stderr)
                elif state == "missing":
                    print("capsule tick: unblocked cache missing - metric absent", file=sys.stderr)
                elif state == "corrupt":
                    print("capsule tick: unblocked cache corrupt (invalid JSON) - metric absent", file=sys.stderr)
            db.guard_set(conn, "unblocked_cache_state", {"state": state}, expect_version=version, now=now)
        except Exception as exc:
            print(f"capsule tick: unblocked cache check failed: {exc!r}", file=sys.stderr)

    def _export_metrics(self, conn, now):
        """The metrics step: capsule/metrics.py through maintenance/events.py's `log` (the door
        vault_push uses), best effort, never raises. A missing, unreadable or stale detector cache
        and no dispatch memory yet both mean "metric absent this pass", never a tick error."""
        self._check_cache_staleness(conn, now)
        try:
            p = str(paths.REPO / "maintenance")
            if p not in sys.path:
                sys.path.insert(0, p)
            import events as maint_events
            return 1 if metrics.export(maint_events.log, last_dispatch_ts=self._last_dispatch_ts(conn),
                                       board=self.cfg.get("board"), now=now, refresh=True) else 0
        except Exception as exc:
            print(f"capsule tick: metrics export failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 0

    def _incident_gate(self, conn, ks, now):
        """The tick's automatic close attempt: the cheap checks first; only when they all pass does it
        run the recurrence test on main, at most once per RETEST_SECONDS per incident and at most one
        test run per pass, so a pass never stalls on several tests."""
        closed = 0
        for inc in incidents.listing(conn, "fix_pending"):
            if incidents.missing(conn, inc, check_new=self.check_new, now=now, home=self.home,
                                 **incidents.gate_args(ks)):
                continue
            key = f"incident_test:{inc['id']}"
            last, _ = db.guard_get(conn, key)
            if isinstance(last, dict) and now - int(last.get("at", 0)) < RETEST_SECONDS:
                continue
            try:
                incidents.close(conn, inc["id"], run_test=self.run_recurrence,
                                check_new=self.check_new, by="tick", now=now, home=self.home,
                                **incidents.gate_args(ks))
                closed += 1
            except incidents.CloseRefused as exc:
                db.guard_set(conn, key, {"at": now, "missing": exc.missing}, now=now)
            break                                  # one recurrence-test run per pass
        return closed

    # -- dispatch -------------------------------------------------------------------

    def _dispatch(self, conn, cfg, now, counts):
        """Claim one card at a time and re-ask `dispatch.ready`, so the caps and the serial rule see each claim.
        A card whose work raised is skipped this pass; so is its branch if a mount probe failed."""
        for (task_id,) in conn.execute("SELECT id FROM tasks WHERE status='running'").fetchall():
            self._guarded(conn, cfg, task_id, now, counts, lambda t=task_id: self._launch_unspawned(conn, cfg, t, now))
        main_waits.release_and_promote(conn, cfg, now)
        if weekly_pause.held(conn, self.home, now, self._events().state_root()): return 0  # D234
        dispatched, skip = 0, set()
        while True:
            claimed = []
            ready = dispatch.ready(conn, cfg, self.host_tiers, now, self.home, self.mac_hold)
            for row in probe_detail.unskipped(ready, skip):
                self._guarded(conn, cfg, row["id"], now, counts, lambda row=row: claimed.append(
                    probe_detail.attempt(skip, row, self._claim_and_launch, conn, cfg, row, now)))
                if any(claimed):
                    break
            if not any(claimed):
                return dispatched
            dispatched += 1

    # -- launching a run --------------------------------------------------------------------

    def _launch(self, conn, cfg, run, task, *, ws, now, inputs=None):
        """Spool, runner limits and job.json for one run, then spawn `python3 -m capsule run-one`."""
        role = run["profile"]
        if role not in READONLY_ROLES:         # the card's human answers reach the worker
            touched = review_scope.open_round_touched(conn, task["id"], ws)
            found = (("review_findings", self._open_findings(conn, cfg, task["id"], touched, now)),
                     ("interventions", self._human_entries(cfg, task["id"], conn)),
                     ("guide_rulings", self._guide_rulings(conn, task["id"])),
                     ("card_amended_at", runaway.rerun_amendment(conn, task["id"])[1]))
            inputs = {**(inputs or {}), **idoc.retry_error_kwargs(run), **dict(filter(all, found))}
            inputs = restart.apply(conn, cfg, run, inputs, self._spool_root(cfg))   # M155
        sp = spool.make(self._spool_root(cfg), run["run_id"])
        inputs = vault_context.attach(conn, cfg, task, run, sp, inputs, role)   # row 626
        runner = self.runners(task["tier"] or "strong", role)
        if role == KORIN:                      # the main checkout read-only, plus its evidence
            limits = runner.limits(role, sp)
            ks = incidents.settings(cfg)
            workdir = str(ks["main_checkout"])
            extra = [Path(ks["vault"] or workdir) / incidents.NOTE_DIR]
            extra += [Path(e) for e in (inputs or {}).get("evidence", []) if str(e)[:6] != "event:"]
            dirs = list(dict.fromkeys(str(d if d.is_dir() else d.parent) for d in extra if d.exists()))
            limits = dict(limits, add_dirs=list(limits.get("add_dirs") or []) + dirs)
        elif role in REVIEW_ROLES + (blind_dispatch.ROLE,):
            limits = blind_dispatch.limits(runner, role, inputs, sp)
            workdir = ws if role in ("reviewer", blind_dispatch.ROLE) else sp   # rest: no code
        else:
            # Like the contract container: git dir READ-ONLY at its container path, TMPDIR on the
            # /tmp tmpfs (nothing can write what the host's git reads). Commits happen on the host.
            name = incident_blind.start(
                conn, runner, task, now, run["run_id"], ws, requests_dir=str(sp / "requests"),
                git_dir=str(workspace.gitdir(ws)), tmpdir=docker.CONTRACT_TMPDIR)
            inputs, limits = prior_runs.mount(inputs or {}, sp, runner.limits(name, sp))
            workdir = ws
        if workdir == ws:
            limits = dict(limits, git_dir=str(workspace.gitdir(ws)))   # the CLI's own git
        # Preflight needs work root, url sha256: a url may embed credentials; Korin reads job.json.
        _write_json(sp / "job.json", {"route": restart.route(conn, cfg, task["id"], role, run),
                                      "limits": dict(limits, timeout=max(
                                          60, _run_cap(cfg, task, role) - WORKER_CAP_MARGIN)),
                                      "workspace": str(workdir), "inputs": inputs or {},
                                      "work_root": str(self._work_root(cfg)),
                                      "repo_url_sha256": workspace.repo_url_sha256(cfg["repo_url"]),
                                      "launched_at": wall_clock.launch_time(now, self.clock),
                                      **run_record.job_image(runner, role not in READONLY_ROLES)})
        pid = getattr(proc := self.spawn(self.board_db, sp, run["run_id"], role), "pid", None)
        if pid:
            if hasattr(proc, "poll"):
                self.children[pid] = proc
            try:
                with txn(conn):
                    conn.execute("UPDATE task_runs SET worker_pid=? WHERE id=?", (pid, run["id"]))
                    conn.execute("UPDATE tasks SET worker_pid=? WHERE id=? AND current_run_id=?",
                                 (pid, run["task_id"], run["id"]))
            except Exception:
                # The worker is already running but nothing recorded its pid, so the next tick
                # could never heartbeat, release or kill it: it would spend the subscription and
                # hold the workspace forever. Kill it before the caller's exception handling runs.
                self._kill(pid, run["run_id"])
                self.children.pop(pid, None)
                raise

    # -- small reads ------------------------------------------------------------------------

    def _human_entries(self, cfg, task_id, conn=None):
        """Every human file for `task_id`, pending (human/) or recorded (human/done/), oldest first:
        kind, text, timestamp and author only."""
        found, hdir = [], spool.human_dir(self.home, cfg["board"])
        for d in (hdir, hdir / "done"):
            if not d.is_dir():
                continue
            for path in d.iterdir():
                if path.name.startswith(".") or path.is_symlink() or not path.is_file():
                    continue
                doc = _read_json(path)
                if doc and doc.get("task_id") == task_id:
                    found.append((doc.get("at") if isinstance(doc.get("at"), int) else 0, path.name,
                                  {k: doc.get(k) for k in ("kind", "text", "at", "author")}))
        return stall_decided.with_decided(
            conn, task_id, [entry for _, _, entry in sorted(found, key=lambda f: (f[0], f[1]))])
