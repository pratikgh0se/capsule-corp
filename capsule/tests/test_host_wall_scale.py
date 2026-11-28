"""The host wall's start-up check at real size and under load (D161, D162): one process proves the
whole wall; a check that runs out of time is no wall failure (that start is dropped, the card spends
nothing, the board is not paused, the next pass tries again) and three in a row page once; a real
refusal still pauses. The tick side of a refusal is `test_host_wall_close` and `_outage`."""
import subprocess
import time
import unittest
from unittest import mock

from capsule import db
from capsule import invalid_doc as idoc
from capsule.models import Result
from capsule.runners import host_wall
from capsule.tests.test_host_wall import DARWIN, Board
from capsule.tests.test_host_wall_close import TOKENS, WallCase
from capsule.tests.test_tick import IMPL_DONE

SPOOLS, PROJECTS, KIDS = 1479, 1107, 6      # the counts of a real board and a real claude config


def timeout_result(now):
    """What `ClaudeAdapter.run` returns for a start-up check that ran out of time (no wait)."""
    return Result(None, 1, TOKENS, 1, host_wall.TIMEOUT_CLASS, now, None,
                  "host wall unavailable: the start-up check did not finish in 15 s")


class Scale(Board):
    def fill(self, folder, count):
        for i in range(count):
            run = folder / f"s{i:05d}"
            run.mkdir()
            for k in range(KIDS):
                (run / f"f{k}.json").write_text("x")

    @DARWIN
    def test_a_board_of_real_size_is_proved_by_one_process_in_time(self):
        self.fill(self.spools, SPOOLS)
        self.fill(self.cfg / "projects", PROJECTS)
        with mock.patch.object(host_wall.subprocess, "run", wraps=subprocess.run) as run:
            began = time.monotonic()
            argv = host_wall.apply(["claude"], self.plan())
            took = time.monotonic() - began
        self.assertEqual(argv[-1], "claude")
        self.assertEqual(len(run.call_args_list), 1)            # not a process per path
        paths = run.call_args.kwargs["input"].split(b"\0")
        self.assertGreater(len(paths), 500)                     # about 600 paths at this size
        self.assertLess(took, host_wall.PROBE_TIMEOUT)
        self.assertEqual(list(self.spool.glob("hostwall.probe*")), [])

    @DARWIN
    def test_at_real_size_a_wall_that_leaks_is_still_a_refusal_not_a_timeout(self):
        self.fill(self.spools, SPOOLS)
        wall = self.plan()
        wall["allow"].append(str(self.board / "blind-work" / "r9"))
        with self.assertRaises(host_wall.WallUnavailable) as caught:
            host_wall.apply(["claude"], wall)
        self.assertNotIsInstance(caught.exception, host_wall._ProbeTimeout)
        self.assertEqual(caught.exception.error_class, host_wall.ERROR_CLASS)
        self.assertIn("did not refuse a walled path", str(caught.exception))

    def test_a_start_up_check_that_runs_out_of_time_is_a_timeout_and_leaves_no_file(self):
        with mock.patch.object(host_wall.subprocess, "run",
                               side_effect=subprocess.TimeoutExpired("sandbox-exec", 15)), \
                self.assertRaises(host_wall._ProbeTimeout) as caught:
            host_wall.apply(["claude"], self.plan("korin"))
        exc = caught.exception
        self.assertEqual((exc.error_class, exc.retry, exc.wait), (host_wall.TIMEOUT_CLASS, True, 0))
        self.assertIsInstance(exc, host_wall.WallUnavailable)       # fail closed: no argv returned
        self.assertIn("did not finish", str(exc))
        self.assertEqual(list(self.spool.glob("hostwall.probe*")), [])
        self.assertEqual(list(self.spool.glob(f"{host_wall.BLIND_PROBE}*")), [])
        self.assertNotEqual(host_wall.TIMEOUT_CLASS, host_wall.ERROR_CLASS)

    def test_a_path_the_profile_cannot_quote_is_one_plain_message(self):
        home = self.root / 'a"b'
        home.mkdir()
        wall = self.plan()
        wall["blind"] = [*wall["blind"], str(home)]       # a regex path: no quote can be escaped
        with self.assertRaises(host_wall.WallUnavailable) as caught:
            host_wall.apply(["claude"], wall)
        self.assertIn("cannot be walled", str(caught.exception))
        self.assertNotIn("Traceback", str(caught.exception))
        self.assertNotIn("\n", str(caught.exception))


class Timeouts(WallCase):
    def timed_out(self):
        run = {"account": None, "task_id": self.tid, "run_id": "r1", "profile": "implementer"}
        done = {"error_class": host_wall.TIMEOUT_CLASS, "error": "slow"}
        return idoc.pause_scope(self.conn, self.cfg["board"], run, done, int(self.at))

    def pages(self):
        return [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel != 'local'"
            " AND body LIKE '%start-up check timed out%'")]

    def setUp(self):
        super().setUp()
        self.at = time.time()

    def step(self, seconds=60):
        self.at += seconds

    def test_a_timeout_pauses_nothing_and_the_third_in_a_row_pages_once(self):
        for n in range(1, 7):
            keys, why = self.timed_out()
            self.assertEqual(keys, [], n)                       # no pause key: the board runs on
            self.assertIn("tried again", why)
            self.assertEqual(len(self.pages()), 0 if n < 3 else 1, (n, self.pages()))
            self.step()
        self.assertIsNone(self.guard("pause:board"))
        found = self.conn.execute("SELECT kind, role, root_key FROM incidents").fetchall()
        key = "review_infra: host_wall: wall_probe_timeout"
        self.assertEqual([tuple(r) for r in found], [("review_infra", "host_wall", key)])
        self.assertIn("overloaded", self.pages()[0])

    def test_a_start_that_worked_or_a_long_quiet_spell_counts_the_timeouts_again(self):
        for _ in range(2):
            self.timed_out()
            self.step()
        self.conn.execute(      # an implementer session ended well after the last timeout
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
            " run_id) VALUES (?, 'implementer', 'completed', 'completed', ?, ?, ?)",
            (self.tid, self.at, self.at + 5, "w".ljust(32, "0")))
        self.step(30)
        self.timed_out()                                        # the first again, not the third
        self.step()
        self.timed_out()
        self.assertEqual(self.pages(), [])
        self.step(idoc.OUTAGE_GAP + 5)                          # a long quiet spell
        for _ in range(2):
            self.timed_out()
            self.step()
        self.assertEqual(self.pages(), [])
        self.timed_out()
        self.assertEqual(len(self.pages()), 1)

    def test_the_tick_drops_a_timed_out_start_and_tries_the_card_again_next_pass(self):
        now = int(time.time())
        self.script["implementer"] = [[timeout_result(now + k)] for k in range(3)] + [[IMPL_DONE]]
        counts = [self.tick.once(now + 5 * k) for k in range(8)]
        task = self.task()
        self.assertEqual(task["failure_count"], 0)              # the card spent no try
        self.assertIsNone(self.guard("pause:board"))            # and the board never paused
        self.assertIsNone(self.guard("pause:account:default"))
        outcomes = [r["outcome"] for r in self.runs("implementer")]
        self.assertEqual(outcomes[:3], ["rate_limited"] * 3)
        self.assertEqual([r["error_class"] for r in self.runs("implementer")][:3],
                         [host_wall.TIMEOUT_CLASS] * 3)
        self.assertGreaterEqual(sum(c["dispatched"] for c in counts), 4)    # tried again at once
        self.assertEqual(len(self.pages()), 1)                  # three in a row: one page
        self.assertEqual(self.messages(), [])                   # not the "wall cannot start" page
        self.assertNotEqual(task["status"], "blocked")

    def test_a_real_refusal_pauses_the_board_where_a_timeout_does_not(self):
        run = {"account": None, "task_id": self.tid, "run_id": "r1", "profile": "implementer"}
        refusal = {"error_class": host_wall.ERROR_CLASS, "error": "gone",
                   "retry_after": int(self.at) + 300}
        keys, _ = idoc.pause_scope(self.conn, self.cfg["board"], run, refusal, int(self.at))
        self.assertEqual(keys, ["pause:board"])
        self.assertIn("wall", db.guard_get(self.conn, "pause:board")[0])
        self.assertEqual(self.timed_out()[0], [])


if __name__ == "__main__":
    unittest.main()
