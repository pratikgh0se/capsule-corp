"""C07d (rq_d83ab4f7): the lander inside the tick pass (`landing.step`). Real git over a temp bare
remote, a real release checkout and real shell suites, as in test_landing; nothing is mocked.
Oracle 1 is `step`'s return value; oracle 2 reads the bare remote's `main`, the `landing_checked`
rows and the `lander.landed:` guard state back with plain git and sql. Since G2b `step` only
spawns a detached `capsule.landing` worker; `step()` here waits for that worker and returns the
outcome it printed to its log, `launch()` is the pass itself (what the tick sees)."""
import contextlib
import io
import json
import os
import re
import subprocess
import sys
import time
import unittest
from unittest import mock

from capsule import blocks, db, intent, lander, landing, paths
from capsule.db import event, guard_get
from capsule.tests import host_box, test_landing
from capsule.tests.test_lander import BRANCH, git

NOW = test_landing.NOW
PASS = {"implementer": "pass", "reviewer": "pass", "validator": "pass", "guide": "pass",
        "rounds": 1, "overrides": []}


class Step(test_landing.Landing):
    def setUp(self):
        super().setUp()
        # the worker is a child process: its sandbox is the fake docker over the host shell
        patch = mock.patch.dict(os.environ, host_box.docker_env(self.dir))
        patch.start()
        self.addCleanup(patch.stop)
        landing.KIDS.clear()
        self.addCleanup(self.settle)
        self.pids = set()
        self.cfg["repo_url"] = str(self.repo)
        self.sha = self.put("a.txt", "work\n", "card work")
        self.finish(PASS)

    def finish(self, gates, at=NOW):
        """The card as `finish_gate_pass` leaves it: done, with a `completed` event."""
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        event(self.conn, self.tid, "completed", {"commit": self.sha, "gates": gates}, now=at)
        self.conn.commit()

    def launch(self, now=NOW + 1):
        """One tick pass: `landing.step` itself, the worker (if any) left running."""
        got = landing.step(self.conn, self.cfg, self.home, now, release=self.release)
        self.pids |= set(landing.KIDS)
        return got

    def settle(self):
        """Wait for every worker; the outcome its CLI printed to the log (None if none ran)."""
        for kid in list(landing.KIDS.values()):
            kid.wait(timeout=300)
        log = self.home / "logs" / "landing-b.log"
        text = log.read_text() if log.exists() else ""
        return json.loads(text[text.rfind("{\n"):])["outcome"] if "{\n" in text else None

    def step(self, now=NOW + 1):
        """A pass, then the worker's outcome (or the pass's own word when it spawned nothing)."""
        before = len(self.pids)
        got = self.launch(now)
        return self.settle() if got == "running" and len(self.pids) > before else got

    def spy(self, marker):
        """Suites that leave a file behind: proof `lander.run` ran (or, absent, did not)."""
        self.cfg["contract_commands"] = [f"touch {marker}"]

    def checked(self):
        return [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing_checked'")]

    def state(self):
        return (guard_get(self.conn, landing.LANDED + self.tid)[0] or {}).get("state")


class DryRun(Step):
    def setUp(self):
        super().setUp()
        self.cfg["lander"]["mode"] = "dry-run"

    def test_an_approved_card_gets_a_landing_checked_event_and_nothing_is_pushed(self):
        before = self.remote_main()
        self.assertEqual(self.step(), "dry-run")
        got = self.checked()
        self.assertEqual([(g["verdict"], g["commit"]) for g in got], [("checked", self.sha)])
        self.assertEqual(self.remote_main(), before)
        self.assertEqual(self.state(), None)

    def test_the_card_is_checked_once_per_commit_not_every_pass(self):
        self.step()
        self.assertEqual(self.step(NOW + 2), "idle")
        self.assertEqual(len(self.checked()), 1)

    def test_a_red_at_sync_check_is_rerun_once_the_commit_is_on_the_branch(self):
        """t_820cbe78: the commit was not on the host branch at the first pass (sync fail), is
        now; the next pass checks it again and the final verdict then counts as seen."""
        event(self.conn, self.tid, "landing_checked",
              {"verdict": "red", "hold": None, "commit": self.sha, "base": self.sha,
               "checks": [{"name": "sync", "status": "fail", "detail": "not on branch"}]}, now=NOW)
        self.conn.commit()
        self.assertEqual(self.step(), "dry-run")
        self.assertEqual([g["verdict"] for g in self.checked()], ["red", "checked"])
        self.assertEqual(self.step(NOW + 2), "idle")

    def test_a_card_still_red_at_sync_does_not_block_a_younger_approved_card(self):
        """The old card's commit never reached the branch: its sync fail stays final."""
        gone = "0" * 40
        event(self.conn, self.tid, "completed", {"commit": gone, "gates": PASS}, now=NOW)
        event(self.conn, self.tid, "landing_checked",
              {"verdict": "red", "hold": None, "commit": gone, "base": gone,
               "checks": [{"name": "sync", "status": "fail", "detail": "not on branch"}]}, now=NOW)
        young = db.new_task(self.conn, title="y", assignee="dev", branch=BRANCH, status="done")
        event(self.conn, young, "completed", {"commit": self.sha, "gates": PASS}, now=NOW + 1)
        self.conn.commit()
        self.assertEqual(self.step(), "dry-run")
        self.assertEqual([(g["commit"], g["verdict"]) for g in self.checked()],
                         [(gone, "red"), (self.sha, "checked")])

    def test_a_red_dry_run_verdict_is_recorded_but_the_card_stays_done(self):
        self.cfg["contract_commands"] = ["exit 1"]
        self.step()
        self.assertEqual(self.checked()[0]["verdict"], "red")
        status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]
        self.assertEqual(status, "done")

    def test_a_card_a_review_role_did_not_approve_is_not_checked(self):
        self.finish({**PASS, "reviewer": "fail"}, at=NOW + 1)
        self.assertEqual(self.step(NOW + 2), "idle")
        self.assertEqual(self.checked(), [])

    def test_a_card_completed_long_ago_is_not_checked(self):
        self.assertEqual(self.step(NOW + landing.WINDOW + 5), "idle")
        self.assertEqual(self.checked(), [])


class Land(Step):
    def test_land_mode_pushes_then_the_next_pass_verifies_once(self):
        self.assertEqual(self.step(), "landed")
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(self.state(), "pushed")
        self.assertEqual(self.step(NOW + 2), "verified")
        self.assertEqual(self.state(), "verified")
        self.assertEqual(self.step(NOW + 3), "idle")

    def test_a_pushed_card_that_turns_red_is_reverted_in_the_pass(self):
        self.step()
        self.cfg["contract_commands"] = ["exit 1"]
        self.assertEqual(self.step(NOW + 2), "reverted")
        self.assertEqual(self.state(), "reverted")
        self.assertNotEqual(self.remote_main(), self.sha)

    def test_a_revert_owed_card_is_retried_in_a_later_pass(self):
        self.step()
        db.guard_set(self.conn, landing.LANDED + self.tid,
                     {"sha": self.sha, "commits": [self.sha], "state": "revert_owed"}, now=NOW)
        self.assertEqual(self.step(NOW + 2), "reverted")

    def waited(self, hold):
        """The card as a landing check that waited leaves it: blocked, transient, `hold` first."""
        result = {"verdict": "wait", "hold": hold, "commit": self.sha, "card_commit": self.sha,
                  "base": self.sha, "checks": [{"name": "gates", "status": "wait", "detail": "x"}]}
        lander.record(self.conn, self.tid, result, limit=3, now=NOW + 1)

    def card(self):
        row = self.conn.execute("SELECT status, failure_count, block_kind FROM tasks WHERE id=?",
                                (self.tid,))
        return tuple(row.fetchone())

    def test_unblocking_a_lander_wait_checks_the_same_commit_again_however_old_it_is(self):
        for hold in ("lander_wait_env", "lander_wait_docker", "lander_wait_fetch"):
            self.setUp()
            self.waited(hold)
            self.assertEqual((self.card()[0], self.step(NOW + 2)), ("blocked", "idle"))
            late = NOW + landing.WINDOW + 100       # the completion is older than the window
            self.assertTrue(blocks.unblock(self.conn, self.tid, by="human", now=late - 5))
            self.assertEqual(self.card(), ("done", 0, None), hold)
            self.assertIn(("unblocked", {"by": "human", "status": "done"}), [
                (k, json.loads(p)) for k, p in self.conn.execute(
                    "SELECT kind, payload FROM task_events WHERE task_id=?", (self.tid,))
                if k == "unblocked"])
            self.assertEqual(self.step(late), "landed", hold)
            self.assertEqual(self.remote_main(), self.sha)

    def test_unblocking_a_rebase_wait_still_sends_the_card_back_to_ready(self):
        self.waited("lander_wait_rebase")
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human", now=NOW + 3))
        self.assertEqual(self.card()[0], "ready")

    def unblocked_to(self, at=NOW + 3):
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human", now=at))
        return self.card()[0]

    def test_a_worker_blocks_free_text_that_reads_like_a_lander_wait_goes_to_ready(self):
        """A worker's `block` verb takes free text and sets no `cause`: words recheck nothing."""
        db.transition(self.conn, self.tid, "ready", frm=("done",))
        blocks.block(self.conn, self.tid, kind="transient", reason="lander_wait_env: look, a wait")
        self.assertEqual(self.unblocked_to(), "ready")

    def test_a_later_hold_outranks_the_wait_even_one_that_writes_no_blocked_event(self):
        for later in ("contradiction_loop", "failure_limit", "parked_by_human"):
            self.setUp()
            self.waited("lander_wait_env")
            event(self.conn, self.tid, later, {}, now=NOW + 2)
            self.assertEqual(self.unblocked_to(), "ready", later)

    def test_a_card_amended_after_its_approval_goes_to_ready_so_the_implementer_reads_it(self):
        self.waited("lander_wait_env")
        event(self.conn, self.tid, "contract_amended", {"issuer": "cos"}, now=NOW + 2)
        self.assertEqual(self.unblocked_to(), "ready")

    def test_an_approval_older_than_seven_days_goes_to_ready_not_to_a_landing_check(self):
        self.waited("lander_wait_env")
        self.assertEqual(self.unblocked_to(NOW + blocks.APPROVAL_MAX_AGE + 10), "ready")

    def test_a_closed_card_never_revives_as_a_landing_check(self):
        self.waited("lander_wait_env")
        event(self.conn, self.tid, "closed", {"by": "human", "reason": "landed elsewhere"},
              now=NOW + 2)
        self.assertEqual(self.unblocked_to(), "ready")


class Closed(Step):
    """A person's `close` (the work landed outside the factory) holds: the card keeps its old
    `completed` event, but the lander must not check, land, block or re-ready it."""

    def close(self):
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='transient' WHERE id=?",
                          (self.tid,))
        self.conn.commit()
        self.assertTrue(blocks.close(self.conn, self.tid, "landed by hand", now=NOW))

    def status(self):
        return self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]

    def test_a_closed_card_is_not_checked_landed_or_blocked(self):
        self.close()
        self.assertEqual(self.step(), "idle")
        self.assertEqual((self.status(), self.remote_main() == self.sha, self.checked()),
                         ("done", False, []))
        self.assertEqual([k for k in self.kinds() if k in ("blocked", "landing_checked")], [])

    def test_a_closed_card_does_not_stop_another_cards_landing(self):
        self.close()
        sha = self.put("b.txt", "young\n", "young work")      # its own commit, on the branch
        young = db.new_task(self.conn, title="y", assignee="dev", branch=BRANCH, status="done")
        event(self.conn, young, "completed", {"commit": sha, "gates": PASS}, now=NOW + 1)
        self.conn.commit()
        self.assertEqual(self.step(), "landed")
        self.assertEqual((self.status(), self.remote_main()), ("done", sha))
        self.assertNotIn("landing_checked", self.kinds())    # the closed card was never checked

    def test_a_card_completed_again_after_the_close_lands_as_before(self):
        # `finish` stands for any route that completes a closed card again: a `reset` has none
        # (it leaves a closed card `done`), so this only pins the event-order rule.
        self.close()
        self.finish(PASS, at=NOW + 1)
        self.assertEqual(self.step(NOW + 2), "landed")

    def test_a_closed_card_whose_worker_crashed_is_not_moved(self):
        self.close()
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": 0, "at": NOW}, now=NOW)
        db.guard_set(self.conn, landing.CRASHED + self.tid,
                     {"sha": self.sha, "count": landing.CRASH_LIMIT - 1}, now=NOW)
        self.assertNotEqual(self.launch(), "crashed")
        self.assertEqual(self.status(), "done")
        self.assertNotIn("blocked", self.kinds())


class Busy(Step):
    def test_the_busy_flag_is_stamped_with_the_wall_clock_not_the_pass_start(self):
        self.assertTrue(landing.acquire(self.conn, self.tid, NOW - 7200))
        self.assertGreaterEqual(guard_get(self.conn, landing.BUSY)[0]["at"], int(time.time()) - 5)


class Detached(Step):
    """G2b: the pass never waits for a suite."""

    def test_a_pass_with_a_slow_suite_returns_at_once_and_never_spawns_twice(self):
        self.cfg["contract_commands"] = ["sleep 4"]
        start = time.time()
        self.assertEqual(self.launch(), "running")
        self.assertEqual(self.launch(NOW + 2), "running")        # the worker lives: no second one
        self.assertLess(time.time() - start, 3)
        self.assertEqual(len(self.pids), 1)
        guard = guard_get(self.conn, landing.CHECKING)[0]
        self.assertEqual((guard["task"], guard["sha"], guard["pid"]),
                         (self.tid, self.sha, next(iter(self.pids))))
        self.assertEqual(self.remote_main() == self.sha, False)
        self.assertEqual(self.settle(), "landed")
        self.assertEqual(self.remote_main(), self.sha)
        self.assertEqual(guard_get(self.conn, landing.CHECKING)[0], None)   # the worker cleared it

    def test_a_guard_whose_pid_is_dead_is_cleared_and_the_card_is_checked(self):
        dead = subprocess.Popen(["true"])
        dead.wait()
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": dead.pid, "at": NOW}, now=NOW)
        self.assertEqual(self.launch(), "running")
        self.assertNotEqual(guard_get(self.conn, landing.CHECKING)[0]["pid"], dead.pid)
        self.assertEqual(self.settle(), "landed")

    def test_a_live_pid_that_is_not_a_lander_is_a_reused_pid_and_is_dropped(self):
        other = subprocess.Popen(["sleep", "30"])
        self.addCleanup(other.kill)
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": other.pid, "at": NOW}, now=NOW)
        self.assertEqual(self.launch(), "running")
        self.assertEqual(len(self.pids), 1)
        self.assertEqual(self.settle(), "landed")

    def test_a_worker_past_the_worker_ttl_is_killed_then_replaced(self):
        old = subprocess.Popen(["sleep", "60"], start_new_session=True)   # a session leader
        self.addCleanup(old.kill)
        landing.KIDS[old.pid] = old
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": old.pid,
                      "at": NOW - landing.WORKER_TTL - 5}, now=NOW)
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(self.launch(), "running")
        self.assertEqual(old.wait(timeout=10), -15)          # SIGTERM: it is gone, not orphaned
        self.assertIn("killed", err.getvalue())
        self.assertEqual(len(self.pids - {old.pid}), 1)      # a new worker took over
        self.assertEqual(self.settle(), "landed")

    def test_a_worker_older_than_the_busy_ttl_but_not_the_worker_ttl_is_left_alone(self):
        old = subprocess.Popen(["sleep", "60"], start_new_session=True)
        self.addCleanup(old.kill)
        landing.KIDS[old.pid] = old
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": old.pid,
                      "at": NOW - landing.BUSY_TTL - 5}, now=NOW)
        self.assertEqual(self.launch(), "running")
        self.assertEqual((old.poll(), len(self.pids - {old.pid})), (None, 0))

    def test_a_pid_running_another_tasks_lander_is_not_ours(self):
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)",
                                  "capsule.landing", "--task", "t_other"])
        self.addCleanup(other.kill)
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": other.pid, "at": NOW}, now=NOW)
        self.assertEqual(self.launch(), "running")
        self.assertEqual(len(self.pids), 1)
        self.assertEqual(self.settle(), "landed")

    def test_a_finished_worker_is_reaped_at_the_next_pass(self):
        self.launch()
        first = next(iter(self.pids))
        self.settle()
        self.assertIn(first, landing.KIDS)
        self.launch(NOW + 2)                                  # the verify worker
        self.assertNotIn(first, landing.KIDS)
        self.settle()


class Crashes(Step):
    """I3: a worker that raises is an event, then a counted crash, then a blocked card."""

    def crash(self, now=NOW + 1):
        del self.cfg["contract_commands"]       # lander.run raises KeyError inside the worker
        return self.step(now)

    def test_a_raising_worker_writes_a_worker_error_event_and_leaves_the_guard(self):
        self.assertEqual(self.crash(), "worker_error")
        rows = [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing'")]
        self.assertEqual([r["outcome"] for r in rows], ["worker_error"])
        self.assertIn("KeyError", rows[0]["detail"])
        self.assertTrue(guard_get(self.conn, landing.CHECKING)[0])

    def test_a_crash_is_counted_when_the_dead_guard_is_dropped_and_the_third_blocks_the_card(self):
        self.assertEqual(self.crash(), "worker_error")
        for n in (2, 3):
            self.assertEqual(self.step(NOW + n), "worker_error")
            self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0]["count"], n - 1)
        self.assertEqual(self.launch(NOW + 4), "crashed")
        self.assertEqual(self.launch(NOW + 5), "idle")       # no longer a candidate
        status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]
        self.assertEqual(status, "blocked")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]
        self.assertEqual(kinds, ["silent_failure"])
        self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0], None)

    def test_dry_run_asks_but_never_moves_the_card_and_stops_respawning(self):
        self.cfg["lander"]["mode"] = "dry-run"
        self.crash()
        for n in (2, 3, 4):
            self.step(NOW + n)
        self.assertEqual(self.launch(NOW + 5), "idle")
        status = self.conn.execute("SELECT status FROM tasks WHERE id=?", (self.tid,)).fetchone()[0]
        self.assertEqual(status, "done")
        self.assertEqual([r[0] for r in self.conn.execute("SELECT kind FROM incidents")],
                         ["silent_failure"])
        self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0]["count"], 3)
        self.assertEqual(len(self.pids), 3)

    def test_a_clean_verify_resets_its_crash_count(self):
        self.step()                                           # landed: verify owed
        cmds = self.cfg.pop("contract_commands")
        self.step(NOW + 2)
        self.step(NOW + 3)
        self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0]["count"], 1)
        self.cfg["contract_commands"] = cmds
        self.assertEqual(self.step(NOW + 4), "verified")
        self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0], None)

    def test_the_worker_claims_the_pid_0_guard_for_its_task_at_start(self):
        peek, seen = self.dir / "peek.py", self.dir / "seen"
        peek.write_text("import sqlite3, sys\nrow = sqlite3.connect(sys.argv[1]).execute(\n"
                        "    \"select value from guard_state \"\n"
                        "    \"where key='lander.checking'\").fetchone()\n"
                        "open(sys.argv[2], 'w').write(row[0])\n")
        dbfile = str(self.dir / "board.db")
        self.cfg["contract_commands"] = [f"python3 {peek} {dbfile} {seen}"]
        conf = self.dir / "worker.json"
        conf.write_text(json.dumps(self.cfg))
        db.guard_set(self.conn, landing.CHECKING,
                     {"task": self.tid, "sha": self.sha, "pid": 0, "at": NOW}, now=NOW)
        done = subprocess.run(
            [sys.executable, "-m", "capsule.landing", "land", "--db", dbfile, "--task", self.tid,
             "--repo", str(self.repo), "--release", str(self.release), "--config", str(conf),
             "--home", str(self.home), "--commit", self.sha], cwd=str(paths.REPO),
            capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertGreater(json.loads(seen.read_text())["pid"], 0)      # claimed during the suites
        self.assertEqual(guard_get(self.conn, landing.CHECKING)[0], None)   # cleared at the end

    def test_a_worker_that_ends_well_is_not_a_crash(self):
        self.assertEqual(self.step(), "landed")
        self.assertEqual(self.launch(NOW + 2), "running")     # the verify worker
        self.settle()
        self.assertEqual(self.launch(NOW + 3), "idle")
        self.assertEqual(guard_get(self.conn, landing.CRASHED + self.tid)[0], None)
        self.assertEqual(guard_get(self.conn, landing.CHECKING)[0], None)

    def test_a_crashing_verify_asks_and_opens_an_incident_but_keeps_retrying(self):
        self.step()                                           # landed: verify owed
        del self.cfg["contract_commands"]
        for n in range(2, 6):
            self.step(NOW + n)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]
        self.assertEqual(kinds, ["silent_failure"])
        self.assertEqual(self.launch(NOW + 7), "running")      # still retried, the card not blocked
        self.settle()
        self.assertEqual(self.state(), "pushed")


    def test_the_worker_stamps_with_the_clock_after_the_suites_not_the_pass_start(self):
        stale, began = NOW - 7200, int(time.time())
        first = self.conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        landing._check(self.conn, self.cfg, self.home, stale, self.release, self.tid, BRANCH,
                       self.sha)
        stamps = [r[0] for r in self.conn.execute(
            "SELECT created_at FROM task_events WHERE id>? AND kind IN ('completed', 'landing')",
            (first,))]
        self.assertTrue(stamps)
        self.assertGreaterEqual(min(stamps), began)
        self.assertGreaterEqual(guard_get(self.conn, landing.BUSY)[0]["at"], began)


class Waiting(Step):
    """F2: land mode does not run the suites for a card that cannot land yet."""

    def setUp(self):
        super().setUp()
        self.marker = self.dir / "suites-ran"
        self.spy(self.marker)

    def test_a_fresh_busy_flag_spawns_nothing_and_runs_no_suite(self):
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "x", "at": NOW}, now=NOW)
        self.assertEqual(self.launch(), "busy")
        self.assertEqual((len(self.pids), self.marker.exists()), (0, False))

    def test_no_free_heavy_seat_spawns_nothing_and_runs_no_suite(self):
        self.cfg["caps"] = {"max_heavy": 0}
        self.assertEqual(self.launch(), "no_seat")
        self.assertEqual((len(self.pids), self.marker.exists()), (0, False))

    def test_a_deploy_hold_that_still_drifts_runs_no_suite(self):
        db.guard_set(self.conn, landing.HOLD, {"reason": "drift", "task": "x"}, now=NOW)
        self.cfg["lander"]["deploy_check"] = "exit 1"
        self.assertEqual(self.step(), "held_deploy_drift")
        self.assertEqual((self.marker.exists(), self.checked()), (False, []))

    def test_a_hold_that_has_cleared_is_dropped_and_the_card_lands(self):
        db.guard_set(self.conn, landing.HOLD, {"reason": "drift", "task": "x"}, now=NOW)
        self.assertEqual(self.step(), "landed")
        self.assertEqual(guard_get(self.conn, landing.HOLD)[0], None)

    def test_a_merge_keeper_claim_runs_no_suite(self):
        owners = paths.queue_dir("b", self.home) / "owners.json"
        owners.parent.mkdir(parents=True)
        owners.write_text(json.dumps({"capsule/other": {"owner": "merge-keeper",
                                                        "heartbeat": time.time()}}))
        self.assertEqual(self.step(), "busy")
        self.assertEqual((self.marker.exists(), self.remote_main() == self.sha), (False, False))

    def test_dry_run_is_not_held_back_by_a_busy_flag(self):
        self.cfg["lander"]["mode"] = "dry-run"
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "x", "at": NOW}, now=NOW)
        self.assertEqual(self.step(), "dry-run")
        self.assertTrue(self.marker.exists())


class IntentExamples(Step):
    """Z1: the row-600 step examples in contracts/intent/lander.jsonl, replayed through `step`."""

    def replay(self, case):
        """What the example observed: step, then, landing_checked count, pushed (read off the bare
        remote), spawned workers, and the pass's own seconds."""
        mode = {"step_dry_run": "dry-run", "step_land": "land"}.get(case, "land")
        self.cfg["lander"]["mode"] = mode
        if case == "step_off":
            del self.cfg["lander"]["mode"]
        if case == "step_busy":
            db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "x", "at": NOW}, now=NOW)
        if case == "step_dead":
            dead = subprocess.Popen(["true"])
            dead.wait()
            db.guard_set(self.conn, landing.CHECKING,
                         {"task": self.tid, "sha": self.sha, "pid": dead.pid, "at": NOW}, now=NOW)
        if case == "step_slow":
            self.cfg["contract_commands"] = ["sleep 3"]
            first, then = self.launch(), self.launch(NOW + 2)
            seen = (first, then, self.remote_main() == self.sha, len(self.pids))
            self.settle()
            return seen[0], seen[1], len(self.checked()), seen[2], seen[3]
        if case == "step_crash":
            del self.cfg["contract_commands"]
            first = self.step()
            self.step(NOW + 2), self.step(NOW + 3)
            return first, self.launch(NOW + 4), len(self.checked()), False, len(self.pids)
        first = self.step()
        then = self.step(NOW + 2) if case in ("step_dry_run", "step_land") else None
        return first, then, len(self.checked()), self.remote_main() == self.sha, len(self.pids)

    def test_the_step_examples(self):
        shipped = [e for e in intent.load("lander") if "step" in e["expect"]]
        self.assertEqual(len(shipped), 6)
        for ex in shipped:
            self.setUp()
            want = ex["expect"]
            first, then, checked, pushed, spawned = self.replay(ex["given"]["document"]["case"])
            got = (first, then, pushed, spawned)
            wanted = (want["step"], want.get("then"), want["pushed"], want.get("spawned", spawned))
            self.assertEqual(got, wanted, ex["id"])
            if "landing_checked" in want:
                self.assertEqual(checked, want["landing_checked"], ex["id"])


class Reverted(Step):
    """D4: a card whose change a human landed and then reverted on main is never landed again."""

    def keeper_landed_then_reverted(self, reverts=1):
        """As the keeper does: the card's change on main under another sha, its message naming
        the card, then `reverts` human `git revert HEAD`s in a row (2: a reapply; 3: reverted
        again after the reapply)."""
        other = self.dir / "other"
        if not other.exists():
            subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True,
                           capture_output=True)
        (other / "a.txt").write_text("work\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", f"card work ({self.tid}, capsule run r1)")
        for _ in range(reverts):
            git(other, "revert", "--no-edit", "HEAD")
        git(other, "push", "-q", "origin", "main")
        return git(other, "rev-parse", "HEAD")

    def reworked_after_a_revert(self):
        """The card's revert is on main; the card is fixed (an amend: a new sha) and re-completed
        after it."""
        self.keeper_landed_then_reverted()
        (self.repo / "a.txt").write_text("fixed\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "--amend", "--no-edit")
        self.sha = git(self.repo, "rev-parse", "HEAD")
        self.finish(PASS, at=int(time.time()) + 60)

    def asks(self):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE body LIKE '%reverted on main%'")]

    def test_a_card_reverted_on_main_is_not_landed_again_and_a_human_is_told_once(self):
        main = self.keeper_landed_then_reverted()
        self.assertEqual(self.step(), "reverted_on_main")     # the worker's own fetch sees it
        self.assertEqual(self.remote_main(), main)             # nothing was pushed
        self.assertEqual(self.state(), None)
        events = [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing'")]
        self.assertEqual([(e["outcome"], e["commit"]) for e in events],
                         [("reverted_on_main", self.sha)])
        self.assertEqual(len(self.asks()), 1)
        self.assertEqual(self.step(NOW + 2), "idle")           # seen now: no worker, no new ask
        self.assertEqual((len(self.asks()), self.remote_main()), (1, main))

    def test_the_tick_pass_alone_skips_a_card_whose_revert_it_already_has(self):
        self.keeper_landed_then_reverted()
        git(self.repo, "fetch", "-q", "origin")
        self.assertEqual(self.launch(), "idle")
        self.assertEqual((len(landing.KIDS), len(self.asks())), (0, 1))

    def test_a_revert_that_was_itself_reverted_is_not_a_reverted_card(self):
        self.keeper_landed_then_reverted(reverts=2)
        self.assertNotEqual(self.step(), "reverted_on_main")
        self.assertEqual(self.asks(), [])

    def test_a_card_reverted_again_after_a_reapply_is_flagged(self):
        main = self.keeper_landed_then_reverted(reverts=3)
        self.assertEqual(self.step(), "reverted_on_main")
        self.assertEqual((len(self.asks()), self.remote_main()), (1, main))

    def test_the_ask_says_what_a_human_can_do(self):
        self.keeper_landed_then_reverted()
        self.step()
        self.assertIn("Reapply the change by hand, or send the card back", self.asks()[0])
        self.assertIn("a new completion is checked again", self.asks()[0])

    def test_a_card_reworked_after_a_revert_is_checked_normally(self):
        """The revert predates the card's newest `completed` event: that completion is a fix."""
        self.reworked_after_a_revert()
        self.assertEqual(self.step(), "landed")
        self.assertEqual((git(self.remote, "show", "main:a.txt"), self.asks()), ("fixed", []))

    def test_a_git_failure_reading_main_lands_nothing_that_pass(self):
        before = self.remote_main()
        with mock.patch.object(landing, "_reverted_on_main", return_value=None):
            got = landing._check(self.conn, self.cfg, self.home, NOW + 1, self.release, self.tid,
                                 BRANCH, self.sha)
        self.assertEqual((got, self.remote_main(), self.state()), ("revert_check_failed", before,
                                                                  None))
        events = [json.loads(p)["outcome"] for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing'")]
        self.assertEqual(events, ["revert_check_failed"])        # a visible wait, not a silent loop

    def test_a_git_failure_with_a_red_verdict_still_goes_through_record(self):
        self.cfg["contract_commands"] = ["exit 1"]
        with mock.patch.object(landing, "_reverted_on_main", return_value=None):
            got = landing._check(self.conn, self.cfg, self.home, NOW + 1, self.release, self.tid,
                                 BRANCH, self.sha)
        self.assertEqual(got, "not_checked")
        self.assertEqual([c["verdict"] for c in self.checked()], ["red"])
        self.assertNotIn("revert_check_failed", [json.loads(p)["outcome"] for (p,) in
                         self.conn.execute("SELECT payload FROM task_events WHERE kind='landing'")])

    def test_another_cards_revert_does_not_stop_this_card(self):
        other = self.dir / "other"
        subprocess.run(["git", "clone", "-q", str(self.remote), str(other)], check=True,
                       capture_output=True)
        (other / "z.txt").write_text("z\n")
        git(other, "add", "-A")
        git(other, "commit", "-qm", "card z (t_00000000, capsule run r9)")
        git(other, "revert", "--no-edit", "HEAD")
        git(other, "push", "-q", "origin", "main")
        self.assertEqual(self.step(), "landed")

    def replay(self, case):
        """Z1, contracts/intent/lander_revert.jsonl. What the example observed: the pass's
        outcome, whether anything was pushed, the asks and the last `landing_checked` verdict."""
        if case == "land_reworked_after_revert":
            self.reworked_after_a_revert()
        else:
            self.keeper_landed_then_reverted(reverts=3 if case == "land_reverted_after_reapply"
                                             else 1)
        before = self.remote_main()
        outcome = self.step()
        verdict = (self.checked() or [{}])[-1].get("verdict")
        return outcome, self.remote_main() != before, len(self.asks()), verdict

    def test_the_revert_examples(self):
        rows = intent.load("lander_revert")
        self.assertEqual(len(rows), 3)
        for ex in rows:
            self.setUp()
            want = ex["expect"]
            outcome, pushed, asks, verdict = self.replay(ex["given"]["document"]["case"])
            self.assertEqual(asks, want["asks"], ex["id"])
            if "outcome" in want:
                self.assertEqual((outcome, pushed), (want["outcome"], want["pushed"]), ex["id"])
            if want.get("verdict_checked"):
                self.assertEqual(verdict, "checked", ex["id"])


class ReplayCases(Step):
    """Z1, the replay rows: a reworked card lands whole (its original plus the fix-up, oldest
    first), never with another card's commit, and never re-picks an original an earlier landing
    already replayed. The epic holds the Step card's id-less first commit under all of these:
    it is never one of the card's commits."""
    WANT = {
        "land_reworked_original_and_fixup": {
            "outcome": "landed", "landed": ["card work", "card fix-up"],
            "original_change_present": True},
        "land_skips_other_card_commit": {
            "outcome": "landed", "landed": ["card work", "card fix-up"],
            "other_commit_in_tree": False},
        "land_rework_after_replay_landing": {
            "outcome": "landed", "landed": ["card fix-up"], "other_edit_kept": True},
    }

    def commit(self, name, text, title, n=1, tid=None):
        return self.put(name, text, f"{title} ({tid or self.tid}, capsule run {n})")

    def subjects_since(self, before):
        out = git(self.remote, "log", "--reverse", "--format=%s", f"{before}..main").splitlines()
        return [re.sub(r" \(t_\w+, capsule run \w+\)$", "", s) for s in out]

    def tree(self, name):
        return git(self.remote, "ls-tree", "-r", "--name-only", "main").split().count(name) == 1

    def replay(self, case):
        """What the example observed: the pass's outcome, the subjects it landed (card id
        stripped) and the one fact the case is about."""
        if case == "land_rework_after_replay_landing":
            self.sha = self.commit("orig.txt", "one\ntwo\n", "card work")
            self.finish(PASS)
            self.rival()
            self.assertEqual(self.step(), "landed")           # by replay: main had moved
            self.assertEqual(self.step(NOW + 5), "verified")  # the watch on it closes first
            other = self.dir / "other"
            git(other, "pull", "-q", "--ff-only")
            (other / "orig.txt").write_text("other\n")
            git(other, "add", "-A")
            git(other, "commit", "-qm", "other card (t_other, capsule run 1)")
            git(other, "push", "-q", "origin", "main")
            self.sha = self.commit("fix.txt", "fix\n", "card fix-up", 2)
            self.finish(PASS, at=NOW + 10)
            before = self.remote_main()   # a landed card is never re-picked by the tick: by hand
            outcome = self.land(fetch=True)["outcome"]
        else:
            self.rival()
            first = self.commit("orig.txt", "one\ntwo\n", "card work")
            if case == "land_skips_other_card_commit":
                self.commit("other.txt", "x\n", "other card", tid="t_other")
            self.sha = self.commit("orig.txt", "one\ntwo\nfix\n", "card fix-up", 2)
            self.finish(PASS)
            before = self.remote_main()
            outcome = self.step(NOW + 20)
        seen = {"outcome": outcome, "landed": self.subjects_since(before)}
        if case == "land_reworked_original_and_fixup":
            seen["original_change_present"] = git(self.remote, "show", "main:orig.txt") \
                == "one\ntwo\nfix"
        elif case == "land_skips_other_card_commit":
            seen["other_commit_in_tree"] = self.tree("other.txt")
        else:
            seen["other_edit_kept"] = git(self.remote, "show", "main:orig.txt") == "other"
        return seen

    def test_the_replay_cases(self):
        for case, want in self.WANT.items():
            self.setUp()
            self.assertEqual(self.replay(case), want, case)

    def test_the_replay_examples(self):
        rows = intent.load("lander_replay")
        if not rows:
            self.skipTest("contracts/intent/lander_replay.jsonl is not shipped yet")
        self.assertEqual(sorted(r["given"]["document"]["case"] for r in rows), sorted(self.WANT))
        for ex in rows:
            self.setUp()
            want = ex["expect"]
            seen = self.replay(ex["given"]["document"]["case"])
            self.assertEqual({k: seen[k] for k in want if k != "note"},
                             {k: v for k, v in want.items() if k != "note"}, ex["id"])


class Contained(Step):
    def test_a_missing_lander_section_means_no_landing(self):
        del self.cfg["lander"]
        self.assertEqual(self.step(), "off")
        self.assertEqual((self.checked(), self.remote_main() == self.sha), ([], False))

    def test_a_mode_that_is_neither_dry_run_nor_land_means_no_landing(self):
        self.cfg["lander"]["mode"] = "lnad"
        self.assertEqual(self.step(), "off")
        self.assertEqual(self.remote_main() == self.sha, False)

    def test_an_error_inside_the_step_is_logged_and_never_raised(self):
        del self.cfg["repo_url"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            got = self.step()
        self.assertEqual(got, "error")
        self.assertIn("landing step failed: KeyError", err.getvalue())
        self.assertEqual(self.remote_main() == self.sha, False)


if __name__ == "__main__":
    unittest.main()
