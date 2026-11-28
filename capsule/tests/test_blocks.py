# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   LegacyOracle.test_router_matches
import json
import tempfile
import unittest
from pathlib import Path
from capsule import blocks, db, intent, lease
from capsule.tests.legacy_oracle import legacy, needs_legacy
from capsule.tests.test_blocks_epic_wait import EpicMergeWait   # noqa: F401  (incident gate id)


def board(tc):
    p = Path(tempfile.mkdtemp()) / "kanban.db"
    db.create_board(p)
    conn = db.connect(p)
    tc.addCleanup(conn.close)   # R28: close every connection this helper opens (ResourceWarning)
    return conn


def status(conn, tid):
    return conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]


class Router(unittest.TestCase):
    def test_dependency_goes_to_todo(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        self.assertEqual(blocks.block(conn, tid, kind="dependency", reason="needs p"), "todo")
        self.assertEqual(status(conn, tid), "todo")
        self.assertEqual(conn.execute("SELECT outcome FROM task_runs").fetchone()[0], "blocked")

    def test_same_cause_twice_goes_to_triage(self):
        """A *worker's own* retry (no human involved) never resets the loop count: the same cause
        twice in a row is still the genuine, unresolved loop."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "blocked")
        self.assertTrue(blocks.unblock(conn, tid, by="worker"))
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "triage")

    def test_same_cause_after_a_human_verb_is_a_fresh_block_not_a_loop(self):
        """A human `unblock` clears `block_kind`/`block_recurrences`, so a re-raise right after it
        is a fresh first strike, not the loop's second; a worker's own retry after that still
        reaches `triage`."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "blocked")
        self.assertTrue(blocks.unblock(conn, tid, by="human"))
        row = conn.execute("SELECT block_kind, block_recurrences FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["block_kind"], row["block_recurrences"]), (None, 0))
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "blocked")
        self.assertTrue(blocks.unblock(conn, tid, by="worker"))
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "triage")

    def test_different_causes_alternate_without_triage(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        for kind in ("capability", "transient", "capability"):
            self.assertEqual(blocks.block(conn, tid, kind=kind, reason=kind), "blocked")
            blocks.unblock(conn, tid, by="human")

    def test_bad_kind_raises(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        with self.assertRaises(ValueError):
            blocks.block(conn, tid, kind="bogus", reason="x")

    def test_unblock_rejects_a_bad_by_value(self):
        """Fix round 2, R1: a typo like `by="Human"` must raise, never silently behave like
        `worker` (no notice) - a caller error here must be loud."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        with self.assertRaises(ValueError):
            blocks.unblock(conn, tid, by="Human")

    def test_triage_is_not_unblockable(self):
        """Row 487 (t_de1466d6): a triage unblock is refused WITH an event and a notice, never
        silently - the pilot run's unblock recorded an intervention and changed nothing."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")
        conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
        self.assertFalse(blocks.unblock(conn, tid, by="human"))
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'", (tid,)).fetchone()[0])
        self.assertEqual(payload["reason"], "triage: reset|accept only")
        body = conn.execute("SELECT body FROM outbox WHERE thread_key=?", (tid,)).fetchone()[0]
        self.assertIn("reset|accept", body)

    def test_triage_with_no_acceptable_block_kind_offers_reset_only(self):
        """Fix round 1, finding 2: `accept` only lifts a triage card whose block_kind is one of
        `ACCEPTABLE_TRIAGE_KINDS`. Offering `reset|accept` for every triage card (block_kind NULL,
        e.g. a `request` verb's child, or the failure-limit park) sent them in a circle: `accept`
        refused offering `unblock|reset`, `unblock` refused offering `accept` right back."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")   # block_kind NULL
        self.assertFalse(blocks.unblock(conn, tid, by="human"))
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'", (tid,)).fetchone()[0])
        self.assertEqual(payload["reason"], "triage: reset only")
        body = conn.execute("SELECT body FROM outbox WHERE thread_key=?", (tid,)).fetchone()[0]
        self.assertNotIn("accept", body)

    def test_accept_and_unblock_refusals_never_point_at_each_other(self):
        """A triage card whose block_kind is not in ACCEPTABLE_TRIAGE_KINDS: `accept`'s refusal may
        still offer `unblock` (unchanged), but `unblock`'s own refusal must not send them back to
        `accept` - otherwise the two notices form a loop that never actually releases the card."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")   # block_kind NULL, not acceptable
        self.assertFalse(blocks.accept(conn, tid))
        accept_body = conn.execute(
            "SELECT body FROM outbox WHERE thread_key=? ORDER BY id", (tid,)).fetchall()[-1][0]
        self.assertIn("unblock|reset", accept_body)
        self.assertFalse(blocks.unblock(conn, tid, by="human"))
        unblock_body = conn.execute(
            "SELECT body FROM outbox WHERE thread_key=? ORDER BY id", (tid,)).fetchall()[-1][0]
        self.assertNotIn("accept", unblock_body)

    def test_scheduled_is_lifted(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", status="scheduled")
        self.assertTrue(blocks.unblock(conn, tid, by="human"))
        self.assertEqual(status(conn, tid), "ready")

    def test_blocked_is_lifted(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="capability", reason="a")
        self.assertTrue(blocks.unblock(conn, tid, by="human"))
        self.assertEqual(status(conn, tid), "ready")

    def test_ready_running_done_are_refused_with_a_named_event(self):
        """`unblock` on a status it cannot lift returns False WITH an `unblock_refused` event
        naming the status - never silently (row 487)."""
        conn = board(self)
        for st in ("ready", "running", "done"):
            tid = db.new_task(conn, title="t", status=st)
            self.assertFalse(blocks.unblock(conn, tid, by="human"))
            payload = json.loads(conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'", (tid,)).fetchone()[0])
            self.assertIn(st, payload["reason"])

    def test_a_human_unblock_refused_on_a_done_card_still_reaches_the_outbox(self):
        """Fix round 1, finding 1: the generic refusal wrote its event but left the outbox empty -
        a human `unblock` file on a `done` card was archived silently; the owner never learned his
        verb changed nothing."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="done")
        self.assertFalse(blocks.unblock(conn, tid, by="human"))
        rows = conn.execute("SELECT body FROM outbox WHERE thread_key=?", (tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("done", rows[0][0])

    def test_a_worker_unblock_refused_on_a_done_card_gets_no_notice(self):
        """Fix round 1, finding 1's caveat: a worker's bad `unblock` verb (spool.py) must not page
        them - it gets the `unblock_refused` event (so the record exists) but no outbox notice."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="done")
        self.assertFalse(blocks.unblock(conn, tid, by="worker"))
        payload = json.loads(conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'", (tid,)).fetchone()[0])
        self.assertIn("done", payload["reason"])
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox WHERE thread_key=?", (tid,)).fetchone()[0], 0)

    def test_a_worker_unblock_refused_on_triage_also_gets_no_notice(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")
        self.assertFalse(blocks.unblock(conn, tid, by="worker"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox WHERE thread_key=?", (tid,)).fetchone()[0], 0)

    def test_unblock_lands_in_todo_while_a_parent_is_open(self):
        conn = board(self)
        parent = db.new_task(conn, title="p")
        child = db.new_task(conn, title="c", parents=[parent])
        blocks.block(conn, child, kind="needs_input", reason="q")
        blocks.unblock(conn, child, by="human")
        self.assertEqual(status(conn, child), "todo")


class FailureCounter(unittest.TestCase):
    def test_u1_card_22_reset_clears_the_gate_for_good(self):
        """U1 card 22 (rows 359, 376): the derived counter and the triage branch re-parked after a reset."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        for _ in range(3):
            last = blocks.record_failure(conn, tid, reason="boom", limit=3)
        self.assertEqual(last, "triage")
        blocks.reset_failures(conn, tid, by="owner")
        self.assertEqual((status(conn, tid), conn.execute("SELECT failure_count FROM tasks").fetchone()[0]), ("ready", 0))
        self.assertEqual(blocks.record_failure(conn, tid, reason="boom again", limit=3), "ready")
        self.assertEqual(conn.execute("SELECT failure_count FROM tasks").fetchone()[0], 1)

    def test_a_triage_card_failing_again_stays_counted_not_double_parked(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        for _ in range(4):
            last = blocks.record_failure(conn, tid, reason="x", limit=3)
        self.assertEqual(last, "triage")
        parked = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='failure_limit'").fetchone()[0]
        self.assertEqual((parked, conn.execute("SELECT failure_count FROM tasks").fetchone()[0]), (1, 4))

    def test_back_to_review_for_review_runs(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", status="review")
        self.assertEqual(blocks.record_failure(conn, tid, reason="invalid verdict", limit=3, back_to="review"), "review")

    def test_reset_also_clears_block_recurrences(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="capability", reason="a")
        blocks.reset_failures(conn, tid, by="owner")
        row = conn.execute("SELECT status, block_kind, block_recurrences FROM tasks").fetchone()
        self.assertEqual(tuple(row), ("ready", None, 0))

    def test_reset_lands_in_todo_while_a_parent_is_open(self):
        """R16: reset_failures must not release a card past an open parent (unblock's rule)."""
        conn = board(self)
        parent = db.new_task(conn, title="p")
        child = db.new_task(conn, title="c", parents=[parent])
        blocks.block(conn, child, kind="capability", reason="a")
        blocks.reset_failures(conn, child, by="owner")
        self.assertEqual(status(conn, child), "todo")

    def test_record_failure_on_missing_task_returns_none(self):
        conn = board(self)
        self.assertIsNone(blocks.record_failure(conn, "t_nope", reason="x", limit=3))


class RunIdOnEvents(unittest.TestCase):
    """R25 (t_53d35852): failed/parked/blocked events name the run they came from (row 379 join key)."""

    def payloads(self, conn, kind):
        return [(json.loads(p or "{}"), pk) for p, pk in conn.execute(
            "SELECT payload, run_id FROM task_events WHERE kind=? ORDER BY id", (kind,))]

    def test_block_writes_the_run_id_into_the_payload(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        c = lease.claim(conn, tid, ttl=900, role="r")
        blocks.block(conn, tid, kind="needs_input", reason="q", run_pk=c.run_pk, run_id=c.run_id)
        [(payload, pk)] = self.payloads(conn, "blocked")
        self.assertEqual((payload["run_id"], pk), (c.run_id, c.run_pk))

    def test_block_loop_and_dependency_events_carry_it_too(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="capability", reason="a", run_id="r1")
        blocks.unblock(conn, tid, by="worker")   # t_8e00b598: a worker retry, not a human verb, keeps the loop
        blocks.block(conn, tid, kind="capability", reason="a", run_id="r2")
        self.assertEqual(self.payloads(conn, "block_loop_detected")[0][0]["run_id"], "r2")
        other = db.new_task(conn, title="o")
        blocks.block(conn, other, kind="dependency", reason="p", run_id="r3")
        self.assertEqual(self.payloads(conn, "dependency_wait")[0][0]["run_id"], "r3")

    def test_record_failure_writes_the_run_id_on_failed_and_parked(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        c = lease.claim(conn, tid, ttl=900, role="r")
        lease.end_run(conn, c.run_pk, status="failed", outcome="failed")
        blocks.record_failure(conn, tid, reason="x", limit=2, run_id=c.run_id)
        blocks.record_failure(conn, tid, reason="y", limit=2, run_id="other")
        [(failed, pk)] = self.payloads(conn, "failed")
        self.assertEqual((failed["run_id"], pk), (c.run_id, c.run_pk))   # the run's pk joins too
        [(parked, pk)] = self.payloads(conn, "failure_limit")
        self.assertEqual((parked["run_id"], pk), ("other", None))

    def test_without_a_run_id_the_payload_is_unchanged(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.record_failure(conn, tid, reason="x", limit=3)
        self.assertNotIn("run_id", self.payloads(conn, "failed")[0][0])


class Promotion(unittest.TestCase):
    """R26: a `todo` child becomes `ready` once every parent is done or archived."""

    def test_a_chain_moves_one_link_at_a_time(self):
        conn = board(self)
        a = db.new_task(conn, title="a")
        b = db.new_task(conn, title="b", status="todo", parents=[a])
        c = db.new_task(conn, title="c", status="todo", parents=[b])
        self.assertEqual(blocks.promote_ready(conn), [])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.assertEqual(blocks.promote_ready(conn), [b])
        self.assertEqual((status(conn, b), status(conn, c)), ("ready", "todo"))
        kinds = [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=?", (b,))]
        self.assertIn("promoted", kinds)

    def test_the_promoted_event_names_the_parent(self):
        """t_48b71707: the event that lifts a `todo` child to `ready` names the parent that just
        finished, so a card's history says why it became ready."""
        conn = board(self)
        a = db.new_task(conn, title="a")
        b = db.new_task(conn, title="b", status="todo", parents=[a])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.assertEqual(blocks.promote_ready(conn), [b])
        payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted'",
                               (b,)).fetchone()[0]
        self.assertEqual(json.loads(payload)["parents"], [a])

    def test_a_child_with_another_unfinished_parent_stays_todo(self):
        """t_48b71707: every parent must be done or archived - one unfinished parent holds the
        child in `todo` with no `promoted` event, even once its other parent finishes."""
        conn = board(self)
        a = db.new_task(conn, title="a")
        other = db.new_task(conn, title="other")
        c = db.new_task(conn, title="c", status="todo", parents=[a, other])
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.assertEqual(blocks.promote_ready(conn), [])
        self.assertEqual(status(conn, c), "todo")
        self.assertEqual(conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='promoted'",
                                      (c,)).fetchone(), None)
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (other,))
        self.assertEqual(blocks.promote_ready(conn), [c])
        self.assertEqual(status(conn, c), "ready")
        payload = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='promoted'",
                               (c,)).fetchone()[0]
        self.assertEqual(sorted(json.loads(payload)["parents"]), sorted([a, other]))

    def test_archived_parent_counts_and_blocked_is_left_alone(self):
        conn = board(self)
        a = db.new_task(conn, title="a", status="archived")
        b = db.new_task(conn, title="b", status="todo", parents=[a])
        c = db.new_task(conn, title="c", status="blocked")
        self.assertEqual(blocks.promote_ready(conn), [b])
        self.assertEqual(status(conn, c), "blocked")

    def test_only_the_named_epic_branches_are_promoted(self):
        conn = board(self)
        mine = db.new_task(conn, title="mine", status="todo", branch="epic/a")
        other = db.new_task(conn, title="other", status="todo", branch="epic/pet")
        loose = db.new_task(conn, title="loose", status="todo")
        self.assertEqual(blocks.promote_ready(conn, branches=["epic/a"]), [mine])
        self.assertEqual((status(conn, other), status(conn, loose)), ("todo", "todo"))
        self.assertEqual(blocks.promote_ready(conn, branches=[]), [])


class Notify(unittest.TestCase):
    """Pilot run 1 (t_de1466d6): a held card must reach the owner. The needs_input hold wrote no
    outbox row, so the tick's flush had nothing to send and outbox.log was never created."""

    def outbox(self, conn):
        return conn.execute("SELECT channel, thread_key, body FROM outbox ORDER BY id").fetchall()

    def test_a_block_queues_one_outbox_line_naming_the_card_and_the_reply(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        self.assertEqual(blocks.block(conn, tid, kind="needs_input", reason="golden rows stay off?"), "blocked")
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        channel, key, body = rows[0]
        self.assertEqual((channel, key), ("file", tid))
        for part in (tid, "needs_input", "golden rows stay off?", "python3 -m capsule human", "accept"):
            self.assertIn(part, body)

    def test_offer_overrides_the_first_holds_verbs_never_advertising_accept(self):
        """Row 508 R2-4: a preflight hold (`Tick._claim_and_launch`'s sync refusal, `Tick._close`'s
        in-worker `why_not_worktree` refusal) has no run report to `accept` - the default
        `needs_input` verbs (`unblock|reset|accept`) would offer a verb that always refuses.
        `offer` overrides them on the FIRST hold only; a block-loop/triage recurrence keeps its
        ordinary kind-based verbs, untouched."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        self.assertEqual(
            blocks.block(conn, tid, kind="needs_input", reason="dirty", offer="unblock|reset"), "blocked")
        body = self.outbox(conn)[0][2]
        self.assertIn("unblock|reset", body)
        self.assertNotIn("accept", body)
        blocks.unblock(conn, tid, by="human")
        # t_8e00b598: a human `unblock` cleared `block_recurrences`, so this re-raise is a fresh
        # first strike (still `route == "blocked"`, `offer` still applies) - a worker's own retry
        # with no human verb in between is the real loop and reaches `triage`.
        self.assertEqual(
            blocks.block(conn, tid, kind="needs_input", reason="dirty", offer="unblock|reset"), "blocked")
        blocks.unblock(conn, tid, by="worker")
        self.assertEqual(
            blocks.block(conn, tid, kind="needs_input", reason="dirty", offer="unblock|reset"), "triage")
        # the block-loop notice is the ordinary kind-based one - `offer` never reaches triage
        self.assertIn("accept", self.outbox(conn)[-1][2])

    def test_a_block_loop_to_triage_is_announced_too(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="capability", reason="a")
        blocks.unblock(conn, tid, by="worker")
        self.assertEqual(blocks.block(conn, tid, kind="capability", reason="a"), "triage")
        self.assertEqual(len(self.outbox(conn)), 2)
        self.assertIn("triage", self.outbox(conn)[1][2])

    def test_a_dependency_wait_and_a_refused_block_stay_quiet(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="dependency", reason="needs p")
        done = db.new_task(conn, title="d", status="done")
        self.assertIsNone(blocks.block(conn, done, kind="needs_input", reason="late"))
        self.assertEqual(self.outbox(conn), [])

    def test_parking_after_the_failure_limit_is_announced_once(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        for _ in range(4):
            blocks.record_failure(conn, tid, reason="boom", limit=3)
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("triage", rows[0][2])
        self.assertIn("reset", rows[0][2])


class HumanVerbs(unittest.TestCase):
    """t_91c331db: `epic <branch>` and `park '<trigger>'`, the two new human verbs. Two oracles
    per check (Zenkai Z2): the task row (status/branch_name/block_kind) and the event row."""

    def event_payload(self, conn, tid, kind):
        row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC LIMIT 1",
                           (tid, kind)).fetchone()
        return json.loads(row[0]) if row and row[0] else (None if row is None else {})

    # -- epic -----------------------------------------------------------------------------

    def test_epic_reassigns_a_declared_branch(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", branch="epic/old")
        self.assertTrue(blocks.assign_epic(conn, tid, "epic/new", declared=["epic/old", "epic/new"]))
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["branch_name"], "epic/new")
        self.assertEqual(self.event_payload(conn, tid, "epic_assigned"), {"branch": "epic/new"})

    def test_epic_refuses_an_undeclared_branch(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", branch="epic/old")
        self.assertFalse(blocks.assign_epic(conn, tid, "epic/rogue", declared=["epic/old"]))
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["branch_name"], "epic/old")
        payload = self.event_payload(conn, tid, "epic_refused")
        self.assertEqual(payload["branch"], "epic/rogue")
        self.assertIn("not declared", payload["reason"])
        self.assertEqual(len(self.outbox(conn)), 1)

    def test_epic_refuses_a_running_card(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", branch="epic/old", status="running")
        self.assertFalse(blocks.assign_epic(conn, tid, "epic/new", declared=["epic/old", "epic/new"]))
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["branch_name"], "epic/old")
        payload = self.event_payload(conn, tid, "epic_refused")
        self.assertIn("running", payload["reason"])

    def test_epic_refuses_a_done_card(self):
        """M1: `epic` uses the same status allowlist as `park` (B1) - a finished card must not
        have its branch silently rewritten."""
        conn = board(self)
        tid = db.new_task(conn, title="t", branch="epic/old", status="done")
        self.assertFalse(blocks.assign_epic(conn, tid, "epic/new", declared=["epic/old", "epic/new"]))
        row = conn.execute("SELECT branch_name FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual(row["branch_name"], "epic/old")
        payload = self.event_payload(conn, tid, "epic_refused")
        self.assertIn("done", payload["reason"])

    def test_epic_refuses_a_missing_task(self):
        """Same convention as `accept`'s own missing-row refusal (blocks.py): the refusal is
        still recorded, `why` naming the task as missing - never a silent no-op or a raw KeyError.
        I2: the outbox notice is its own oracle too - the tick never reaches this branch (a
        missing task_id is archived before any kind handler runs), so this unit call is the only
        place that notice is ever exercised."""
        conn = board(self)
        self.assertFalse(blocks.assign_epic(conn, "t_nope", "epic/new", declared=["epic/new"]))
        payload = self.event_payload(conn, "t_nope", "epic_refused")
        self.assertEqual(payload, {"branch": "epic/new", "reason": "no such task"})
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("epic refused", rows[0][2])

    def outbox(self, conn):
        return conn.execute("SELECT channel, thread_key, body FROM outbox ORDER BY id").fetchall()

    # -- park -------------------------------------------------------------------------------

    def test_park_holds_the_card_blocked_with_a_trigger(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        self.assertTrue(blocks.park(conn, tid, "waiting on the vendor's API key"))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        self.assertEqual(self.event_payload(conn, tid, "parked_by_human"),
                         {"by": "human", "trigger": "waiting on the vendor's API key"})
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("waiting on the vendor's API key", rows[0][2])

    def test_park_refuses_a_running_card(self):
        conn = board(self)
        tid = db.new_task(conn, title="t", status="running")
        self.assertFalse(blocks.park(conn, tid, "trigger"))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("running", None))
        payload = self.event_payload(conn, tid, "park_refused")
        self.assertIn("running", payload["reason"])
        # I1: mutation found this notice untested - removing it left every other test green.
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("park refused", rows[0][2])

    def test_park_refuses_a_done_card(self):
        """`park` must refuse a `done`/`archived`/`review` card - park-then-reset must never
        revive one (`triage` is a deliberate exception; see
        test_park_from_triage_moves_it_to_parked_with_the_trigger below)."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="done")
        self.assertFalse(blocks.park(conn, tid, "trigger"))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("done", None))
        payload = self.event_payload(conn, tid, "park_refused")
        self.assertEqual(payload, {"trigger": "trigger", "reason": "done: cannot park"})
        # the second oracle proving no revival: reset must not un-park a card that never parked.
        blocks.reset_failures(conn, tid, by="owner")
        self.assertEqual(conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()["status"], "done")

    def test_park_refuses_a_missing_or_blank_trigger(self):
        """B2: a raw human file lacking `text` (or blank) used to raise `TypeError` inside the
        intervention transaction, which rolled back and left the file un-archived, jamming every
        file queued behind it (reproduced by the reviewer: the poison file re-raised 3 ticks
        running). `park` must instead refuse it cleanly, like any other bad input."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        for bad in (None, "", "   ", 42):
            self.assertFalse(blocks.park(conn, tid, bad))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("ready", None))
        payload = self.event_payload(conn, tid, "park_refused")
        self.assertEqual(payload["reason"], "park needs a non-empty trigger")

    def test_park_refuses_a_missing_task(self):
        conn = board(self)
        self.assertFalse(blocks.park(conn, "t_nope", "trigger"))
        payload = self.event_payload(conn, "t_nope", "park_refused")
        self.assertEqual(payload, {"trigger": "trigger", "reason": "no such task"})
        rows = self.outbox(conn)
        self.assertEqual(len(rows), 1)
        self.assertIn("park refused", rows[0][2])

    def test_park_from_triage_moves_it_to_parked_with_the_trigger(self):
        """A `triage` card is idle, not running - `park` (unlike `unblock`, which stays
        reset|accept-only on triage) must not refuse it."""
        conn = board(self)
        tid = db.new_task(conn, title="t", status="triage")
        conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", ("needs_input", tid))
        self.assertTrue(blocks.park(conn, tid, "waiting on a call"))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        self.assertEqual(self.event_payload(conn, tid, "parked_by_human"),
                         {"by": "human", "trigger": "waiting on a call", "was": "needs_input"})

    def test_park_records_the_prior_block_kind_it_overwrites(self):
        """M2: `park` on an already-`blocked` card (a status the allowlist still permits) would
        otherwise silently replace its `block_kind` - `was` keeps an audit trail."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.block(conn, tid, kind="needs_input", reason="q")
        self.assertTrue(blocks.park(conn, tid, "trigger"))
        payload = self.event_payload(conn, tid, "parked_by_human")
        self.assertEqual(payload, {"by": "human", "trigger": "trigger", "was": "needs_input"})

    def test_reset_lifts_a_parked_card(self):
        """Confirms `reset_failures` (unchanged) already lifts a `parked` hold, same as any
        other `blocked` kind - the card's own second oracle: status and block_kind both clear."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.park(conn, tid, "waiting on the vendor's API key")
        blocks.reset_failures(conn, tid, by="owner")
        row = conn.execute("SELECT status, block_kind, failure_count FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"], row["failure_count"]), ("ready", None, 0))
        self.assertEqual(self.event_payload(conn, tid, "failure_reset"), {"by": "owner"})

    # -- unblock/accept refuse a park (B4, R1) ---------------------------------------------

    def test_unblock_refuses_a_parked_card(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.park(conn, tid, "trigger")
        self.assertFalse(blocks.unblock(conn, tid, by="human"))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        payload = self.event_payload(conn, tid, "unblock_refused")
        self.assertEqual(payload, {"reason": "parked: reset only"})
        rows = [r for r in self.outbox(conn) if "unblock refused" in r[2]]
        self.assertEqual(len(rows), 1)

    def test_unblock_refuses_a_parked_card_for_a_worker_without_a_notice(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        blocks.park(conn, tid, "trigger")
        before = len(self.outbox(conn))
        self.assertFalse(blocks.unblock(conn, tid, by="worker"))
        self.assertEqual(len(self.outbox(conn)), before)   # no new notice
        payload = self.event_payload(conn, tid, "unblock_refused")
        self.assertEqual(payload, {"reason": "parked: reset only"})

    def test_accept_refuses_a_parked_card(self):
        """R1: `accept` must not send a human-parked card to review even when its last
        implementer run reported `status: done` - only `reset` lifts a park. Round 2, r2: the
        refusal notice must offer `reset` alone, never `unblock` too - `unblock` is refused on a
        parked card as well (B4), and offering it here would send the owner straight into that
        second refusal."""
        conn = board(self)
        tid = db.new_task(conn, title="t")
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at, ended_at) VALUES"
            " (?, 'implementer', 'done', 'done', ?, 10, 20)", (tid, json.dumps({"status": "done"})))
        conn.commit()
        blocks.park(conn, tid, "trigger")
        self.assertFalse(blocks.accept(conn, tid))
        row = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        payload = self.event_payload(conn, tid, "accept_refused")
        self.assertEqual(payload, {"reason": "parked: reset only"})
        rows = [r for r in self.outbox(conn) if "accept refused" in r[2]]
        self.assertEqual(len(rows), 1)
        self.assertIn("reset", rows[0][2])
        self.assertNotIn("unblock", rows[0][2])


class InterventionsIntent(unittest.TestCase):
    """B5: `contracts/intent/interventions.jsonl` (registered in `intent.STANDALONE`) ships
    examples for the human verbs' own refusal shapes - each one run here through the real
    `blocks.assign_epic`/`blocks.park`, so the file is a live second oracle, not a document
    a schema-free lint call declares well-formed (the two lines it replaced in `unblocked.jsonl`
    never touched `epic`/`park`: `evaluate_blocker` only reads `waits`, so any verb passed).

    Row 492: the file's own `assign` example needs a board db *and* a `config.json` on disk
    (`assign.assign` connects to both by path, unlike `blocks.assign_epic`/`blocks.park`, which
    take an already-open `conn` and an in-memory `declared` set) - it is its own live oracle,
    `test_assign.py`'s `AssignIntent`, rather than shoehorned into this class's `board(self)`
    fixture. The id-set assertion below still counts it (the whole file's ids), so a stray or
    misspelled id here is still caught."""

    def test_every_example_matches_what_the_real_verb_does(self):
        examples = intent.load("interventions")
        self.assertEqual({e["id"] for e in examples},
                         {"epic-refuses-an-undeclared-branch", "park-refuses-a-done-card",
                          "assign-refuses-an-unroutable-assignee",
                          "accept-applies-on-a-needs-input-triage-card",
                          "unblock-is-never-a-silent-no-op-on-triage",
                          "accept-resumes-a-needs-input-triage-card-with-no-done-report",
                          "park-applies-from-triage", "done-to-running-is-refused",
                          "a-guarded-miss-moves-nothing"})
        for ex in examples:
            doc = ex["given"]["document"]
            if doc["verb"] in ("assign", "transition"):
                continue    # test_assign.py AssignIntent / test_db.py are their own live oracle
            conn = board(self)
            tid = db.new_task(conn, title="t", status=doc["status"])
            if "block_kind" in doc:
                conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", (doc["block_kind"], tid))
            if doc["verb"] == "epic":
                applied = blocks.assign_epic(conn, tid, doc["branch"], declared=doc["declared"])
            elif doc["verb"] == "park":
                applied = blocks.park(conn, tid, doc["trigger"])
            elif doc["verb"] == "accept":   # t_5a8cb343/t_8e00b598: accept reads the last run's handoff
                conn.execute(
                    "INSERT INTO task_runs (task_id, profile, status, outcome, handoff, started_at, ended_at)"
                    " VALUES (?, 'implementer', ?, ?, ?, 10, 20)",
                    (tid, doc["last_report_status"], doc["last_report_status"],
                     json.dumps({"schema": "report.v1", "status": doc["last_report_status"]})))
                conn.commit()
                applied = blocks.accept(conn, tid)
            elif doc["verb"] == "unblock":
                applied = blocks.unblock(conn, tid, by="human")
            else:
                self.fail(f"unknown verb in {ex['id']!r}: {doc['verb']!r}")
            self.assertEqual(applied, ex["expect"]["applied"], ex["id"])
            event = conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id DESC LIMIT 1",
                                 (tid,)).fetchone()[0]
            self.assertEqual(event, ex["expect"]["event"], ex["id"])


class LegacyOracle(unittest.TestCase):
    @needs_legacy
    def test_router_matches(self):
        home = Path(tempfile.mkdtemp())
        theirs = legacy(
            "from pathlib import Path\n"
            f"conn = kb.connect(Path({str(home / 'h.db')!r}))\n"
            "out = []\n"
            "for kinds in (['dependency'], ['capability', 'capability'], ['capability', 'transient']):\n"
            "    t = kb.create_task(conn, title='t')\n"
            "    for k in kinds:\n"
            "        kb.block_task(conn, t, reason=k, kind=k)\n"
            "        out.append(conn.execute('SELECT status FROM tasks WHERE id=?', (t,)).fetchone()[0])\n"
            "        kb.unblock_task(conn, t)\n"
            "print(json.dumps(out))", home)
        conn = board(self)
        ours = []
        for kinds in (["dependency"], ["capability", "capability"], ["capability", "transient"]):
            t = db.new_task(conn, title="t")
            for k in kinds:
                blocks.block(conn, t, kind=k, reason=k)
                ours.append(status(conn, t))
                blocks.unblock(conn, t, by="worker")   # old unblock_task: unattributed
        self.assertEqual(ours, theirs)


class CardStateMachine(unittest.TestCase):
    """Row 509: `db.transition` is the one writer of tasks.status."""

    def test_an_illegal_pair_raises_and_a_guarded_miss_moves_nothing(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        with self.assertRaises(ValueError):
            db.transition(conn, tid, "running", frm=("done",))
        self.assertFalse(db.transition(conn, tid, "blocked", frm=("running",)))
        self.assertEqual(status(conn, tid), "ready")

    def test_a_move_sets_the_columns_and_clears_the_claim(self):
        conn = board(self)
        tid = db.new_task(conn, title="t")
        lease.claim(conn, tid, ttl=900, role="r")
        self.assertTrue(db.transition(conn, tid, "review", frm=("running",), claim=True,
                                      block_kind="x"))
        row = conn.execute("SELECT status, claim_lock, block_kind FROM tasks WHERE id=?",
                           (tid,)).fetchone()
        self.assertEqual(tuple(row), ("review", None, "x"))

    def test_a_human_park_and_a_failure_limit_write_different_events(self):
        conn = board(self)
        a, b = db.new_task(conn, title="a"), db.new_task(conn, title="b")
        blocks.park(conn, a, "wait for the vendor")
        blocks.record_failure(conn, b, reason="boom", limit=1)
        kinds = dict(conn.execute("SELECT task_id, kind FROM task_events"
                                  " WHERE kind IN ('parked_by_human', 'failure_limit')"))
        self.assertEqual(kinds, {a: "parked_by_human", b: "failure_limit"})
