"""A failed mount probe keeps its output on the `tick_error` event and stops its epic branch for
the rest of the pass (card t_afb35167, incident inc_3e98be67). Z1 intent example: the real
incident, a worker container whose `mount-probe` exited 128 with a git message the tick dropped,
after which the same pass started three more cards into the same broken clone."""
import json
import subprocess
import unittest
from unittest import mock

from capsule import db, probe_detail
from capsule.runners import docker
from capsule.tests import test_tick
from capsule.tests.test_tick import BRANCH

GIT_MSG = "fatal: not a git repository"
WANT_LINE = ("capsule: tick error on {}: CalledProcessError: Command 'mount-probe' "
             "returned non-zero exit status 128.")


def probe_failure():
    return subprocess.CalledProcessError(
        128, "mount-probe", GIT_MSG, f"bind mounts missing in a fresh container: {GIT_MSG}")


class FailingStart(test_tick.FakeRunner):
    def __init__(self, exc):
        super().__init__()
        self.exc, self.attempts = exc, 0

    def start(self, run_id, ws, **kw):
        self.attempts += 1
        raise self.exc


class MountProbeFailure(test_tick.TickCase):
    def setUp(self):
        super().setUp()
        self.second = db.new_task(self.conn, title="second", assignee="dev-frontend",
                                  branch=BRANCH, now=2)

    def run_pass(self, exc):
        self.runner = FailingStart(exc)
        return self.tick.once()

    def errors(self):
        rows = self.conn.execute("SELECT task_id, payload FROM task_events WHERE kind='tick_error'")
        return [(task_id, json.loads(payload)) for task_id, payload in rows]

    def outbox_lines(self):
        text = (self.home / "outbox.log").read_text()
        return [ln.split("\t", 1)[1] for ln in self.log_lines(text)]

    def test_the_probe_command_is_the_one_the_docker_runner_raises(self):
        self.assertEqual(probe_detail.PROBE_CMD, docker._PROBE_CMD)

    def test_a_failed_mount_probe_records_its_output_and_stops_the_epic_pass(self):
        counts = self.run_pass(probe_failure())
        errors = self.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(self.runner.attempts, 1)
        task_id, payload = errors[0]
        self.assertIn(GIT_MSG, payload["detail"])
        other = self.second if task_id == self.tid else self.tid
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (other,)).fetchone()
        self.assertEqual(row[0], "ready")
        runs = self.conn.execute("SELECT COUNT(*) FROM task_runs WHERE task_id=?", (other,))
        self.assertEqual(runs.fetchone()[0], 0)
        self.assertEqual(counts["dispatched"], 0)
        self.assertEqual(self.outbox_lines(), [WANT_LINE.format(task_id)])

    def test_the_same_failure_twice_adds_no_second_event_or_line(self):
        counts = {"tick_errors": 0}

        def probe_fails():
            raise probe_failure()
        for _ in range(2):
            self.tick._guarded(self.conn, self.cfg, self.tid, 1000, counts, probe_fails)
        self.assertEqual(counts["tick_errors"], 2)
        self.assertEqual([t for t, _ in self.errors()], [self.tid])
        rows = self.conn.execute("SELECT body FROM outbox WHERE thread_key=?", (self.tid,))
        self.assertEqual([r[0] for r in rows], [WANT_LINE.format(self.tid)])

    def test_another_branch_still_dispatches_after_a_failed_probe(self):
        test_tick.git(self.home / "origin", "branch", "epic/other")
        self.cfg["epics"] = [BRANCH, "epic/other"]
        self.write_cfg()
        db.new_task(self.conn, title="elsewhere", assignee="dev-frontend",
                    branch="epic/other", now=3)
        self.runner = FailingStart(probe_failure())
        self.tick.once()
        self.assertEqual(self.runner.attempts, 2)    # one on each branch, never two on the same

    def test_another_start_failure_records_no_detail_and_keeps_every_card_in_play(self):
        self.run_pass(subprocess.CalledProcessError(1, "docker", "", "refused"))
        self.assertEqual(self.runner.attempts, 2)
        self.assertEqual(len(self.errors()), 2)
        for _, payload in self.errors():
            self.assertEqual(sorted(payload), ["error", "where"])

    def test_a_card_failing_before_its_claim_is_tried_once_and_the_cap_holds(self):
        branches = [BRANCH, "epic/b", "epic/c", "epic/d"]
        for branch in branches[1:]:
            test_tick.git(self.home / "origin", "branch", branch)
        self.cfg["epics"] = branches
        self.cfg["caps"]["max_running"] = 2
        self.write_cfg()
        self.conn.execute("UPDATE tasks SET created_at=1 WHERE id=?", (self.tid,))
        self.conn.execute("DELETE FROM tasks WHERE id=?", (self.second,))
        for n, branch in enumerate(branches[1:], 2):
            db.new_task(self.conn, title=branch, assignee="dev-frontend", branch=branch, now=n)
        real, tried = self.tick._claim_and_launch, []

        def claim(conn, cfg, row, now):
            tried.append(row["id"])
            if row["id"] == self.tid:
                raise ValueError("before the claim")
            return real(conn, cfg, row, now)
        with mock.patch.object(self.tick, "_claim_and_launch", side_effect=claim):
            counts = self.tick.once()
        self.assertEqual(tried.count(self.tid), 1)
        self.assertEqual(len(tried), 3)              # the failed card, then one claim per seat
        self.assertEqual(counts["dispatched"], 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM task_runs").fetchone()[0], 2)

    def test_detail_is_a_probe_failures_stderr_then_output_truncated(self):
        exc = subprocess.CalledProcessError(128, "mount-probe", "o" * 3000, "e" * 10)
        detail = probe_detail.of(exc)["detail"]
        self.assertEqual(len(detail), 2000)
        self.assertTrue(detail.startswith("e" * 10 + "\n" + "o"))

    def test_only_a_probe_failure_gets_detail(self):
        self.assertEqual(probe_detail.of(subprocess.CalledProcessError(1, "docker", "o", "e")), {})
        self.assertEqual(probe_detail.of(subprocess.CalledProcessError(1, "git", "o", "e")), {})
        self.assertEqual(probe_detail.of(ValueError("boom")), {})
        self.assertEqual(probe_detail.of(subprocess.CalledProcessError(128, "mount-probe")), {})


if __name__ == "__main__":
    unittest.main()
