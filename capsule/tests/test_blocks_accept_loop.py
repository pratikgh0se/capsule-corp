# mutation-check: targets-predate-range ff625e94 --
#   moved verbatim from test_blocks (size limit); fixture rename only, no behaviour pinned ::
#   test_accept_moves_a_needs_input_triage_card_to_review,
#   test_accept_still_refuses_a_looped_triage_card_with_no_implementer_run,
#   test_needs_input_reraise_after_accept_is_a_fresh_block_not_a_loop,
#   test_spec_stale_reraise_after_accept_still_reaches_triage
import json
import unittest

from capsule import blocks, db
from capsule.tests.test_blocks import board


class AcceptResumesABlockLoopTriageCard(unittest.TestCase):
    """A card parked in `triage` by a `needs_input` block loop never has a `status: done`
    implementer run to point at - it blocked, it didn't finish - so `accept` only requires a run
    to exist, not one that reported done."""

    def board_with_looped_triage(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="needs_input", reason="q1")
        # a worker retry, not a human verb: the loop still counts
        blocks.unblock(conn, tid, by="worker")
        # block() itself inserts no task_runs row (worker.run_one does, before it calls block()
        # with the report already in hand); this run failed, so it never reported `status: done`.
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at,"
            " ended_at) VALUES"
            " (?, 'implementer', 'failed', 'failed', ?, 10, 20)",
            (tid, json.dumps({"schema": "report.v1", "status": "failed"})))
        conn.commit()
        self.assertEqual(blocks.block(conn, tid, kind="needs_input", reason="q2"), "triage")
        return conn, tid

    def test_accept_moves_a_needs_input_triage_card_to_review(self):
        """`accept` is the owner's answer to a `needs_input` hold, so it clears `block_recurrences`
        too (unlike `spec_stale`/`review_infra`, where the same run is re-reviewed and a second
        identical hold really is still the same loop - see the two tests below)."""
        conn, tid = self.board_with_looped_triage()
        self.assertTrue(blocks.accept(conn, tid))
        row = conn.execute(
            "SELECT status, block_recurrences FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_recurrences"]), ("review", 0))
        kinds = [r[0] for r in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]
        self.assertIn("request_review", kinds)

    def test_accept_still_refuses_a_looped_triage_card_with_no_implementer_run(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")
        conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
        self.assertFalse(blocks.accept(conn, tid))
        row = conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='accept_refused'",
            (tid,)).fetchone()
        self.assertIsNotNone(row)   # the refusal must still be an event, not a silent no-op
        self.assertEqual(json.loads(row[0])["reason"], "no implementer run to review")

    def test_needs_input_reraise_after_accept_is_a_fresh_block_not_a_loop(self):
        """Accept, then an honest re-raise of the same kind, must not count as the loop's second
        strike. `tick.py`'s `_verdict` moves `review` back to `ready` before re-raising a hold;
        mirrored here."""
        conn, tid = self.board_with_looped_triage()
        self.assertTrue(blocks.accept(conn, tid))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        self.assertEqual(blocks.block(conn, tid, kind="needs_input", reason="q3"), "blocked")

    def test_spec_stale_reraise_after_accept_still_reaches_triage(self):
        """B1: unlike `needs_input`, `accept` on `spec_stale`/`review_infra` re-reviews the same
        run without editing the spec/guide, so a second identical hold is still the same loop
        (round-3 fix R3/L2, `test_tick.py`'s `SpecConflictRows`/`ReviewInfra`). `tick.py`'s own
        `_verdict` moves `review` back to `ready` right before
        re-raising the hold; mirrored here."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at,"
            " ended_at) VALUES"
            " (?, 'implementer', 'done', 'done', ?, 10, 20)", (tid, json.dumps({"status": "done"})))
        conn.commit()
        self.assertEqual(
            blocks.block(conn, tid, kind="spec_stale", reason="row 417 vs the spec"), "blocked")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        self.assertEqual(
            blocks.block(conn, tid, kind="spec_stale", reason="row 417 vs the spec"), "triage")
        self.assertTrue(blocks.accept(conn, tid))
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        self.assertEqual(
            blocks.block(conn, tid, kind="spec_stale", reason="row 417 vs the spec"), "triage")
