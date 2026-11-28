"""t_b1cca171 (row 553 predicate): the lookout `review_rounds` column and the `completed` event's
`gates.rounds` both read `review_scope.reviewer_round`, so a held implementer run a human `accept`
sends to review (a `request_review` event, never `outcome='completed'`) counts as round 1.
Tests live in their own module so test_tick.py (frozen) and test_lookout.py (at its 800-line
FILE_LIMIT, no headroom) do not grow, row 514."""
import json
import subprocess
import time

from capsule import blocks, db, human, lookout, review_scope, tick
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS, VAL_PASS, TickCase


def accepted_card(conn):
    """A real board card whose only implementer run failed (never outcome=completed) and that
    `blocks.accept` then sent to review: the `request_review` event is its only round marker."""
    tid = db.new_task(conn, title="t", branch="epic/screens")
    blocks.block(conn, tid, kind="needs_input", reason="q1")
    blocks.unblock(conn, tid, by="worker")
    conn.execute("INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at, ended_at,"
                 " run_id, runner) VALUES (?, 'implementer', 'failed', 'failed', ?, 10, 20, 'r_acc', 'host')",
                 (tid, json.dumps({"schema": "report.v1", "status": "failed"})))
    conn.commit()
    assert blocks.block(conn, tid, kind="needs_input", reason="q2") == "triage"
    assert blocks.accept(conn, tid)
    return tid


def spool_lines(home):
    p = lookout.spool_path(home)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class RoundFlow(TickCase):
    """A card whose implementer run ends needs_input and is accepted by a human: `prior_rounds`
    completed implementer runs are seeded before it, so the accepted run is round prior_rounds+1."""
    prior_rounds = 0

    def setUp(self):
        super().setUp()
        head = ["git", "-C", str(self.home / "origin"), "rev-parse", "HEAD"]
        start_sha = subprocess.run(head, check=True, capture_output=True, text=True).stdout.strip()
        for n in range(self.prior_rounds):
            self.conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at,"
                " run_id, runner, handoff, metadata)"
                " VALUES (?, 'implementer', 'done', 'completed', 1, 2, ?, 'host', ?, ?)",
                (self.tid, f"r_prior_{n}", json.dumps(IMPL_DONE),
                 json.dumps({"start_sha": start_sha})))
        self.conn.commit()
        self.script["implementer"] = [[dict(IMPL_DONE, issues=["needs a design call"])]]
        self.script["reviewer"] = [[REV_PASS]]
        self.script["validator"] = [[VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK]]
        for _ in range(5):
            self.tick.once()
            if self.task()["block_kind"] == "needs_input":
                break
        self.assertEqual(self.task()["block_kind"], "needs_input")
        human.write(self.home, "test", self.tid, "accept", "fine as is", now=1)
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")


class AcceptedRoundIsCounted(RoundFlow):
    def test_the_predicate_itself_reads_round_1(self):
        # oracle 1: the run was never marked completed, so only the accept event can make it a round
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_runs WHERE task_id=? AND profile='implementer' AND outcome='completed'",
            (self.tid,)).fetchone()[0], 0)
        self.assertEqual(review_scope.reviewer_round(self.conn, self.tid), 1)

    def test_the_lookout_card_row_counts_the_accepted_round(self):
        col = lookout.Collector(self.home, "test", host="h", pid=1)
        col.collect(self.conn, self.cfg, time.time(), dict.fromkeys(tick.COUNTS, 0), pass_ms=5)
        row = [l["row"] for l in spool_lines(self.home)
               if l["t"] == "snap" and l["table"] == "cap_cards" and l["row"]["task_id"] == self.tid][-1]
        self.assertEqual(row["review_rounds"], 1)

    def test_the_completed_event_gates_count_the_accepted_round(self):
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["gates"]["rounds"], 1)


class CompletedPlusAcceptedRoundsAreSummed(RoundFlow):
    """F1 (t_b1cca171 review): one round per call-site test lets a capped or boolean count pass
    (`min(1, reviewer_round(...))`); a completed round 1 plus an accepted round 2 must read 2."""
    prior_rounds = 1

    def test_the_predicate_reads_round_2(self):
        self.assertEqual(review_scope.reviewer_round(self.conn, self.tid), 2)

    def test_the_lookout_card_row_sums_both_rounds(self):
        col = lookout.Collector(self.home, "test", host="h", pid=1)
        col.collect(self.conn, self.cfg, time.time(), dict.fromkeys(tick.COUNTS, 0), pass_ms=5)
        row = [x["row"] for x in spool_lines(self.home) if x["t"] == "snap"
               and x["table"] == "cap_cards" and x["row"]["task_id"] == self.tid][-1]
        self.assertEqual(row["review_rounds"], 2)

    def test_the_completed_event_gates_sum_both_rounds(self):
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["gates"]["rounds"], 2)
