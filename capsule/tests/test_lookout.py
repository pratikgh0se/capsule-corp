# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Collect.test_intent_legacy_run_is_legacy_era, Pure.test_era
"""Task 2 (The Lookout): collect + spool + watermarks + tick step 8 (spec 5.1-5.10).
Oracle 1: these tests on a board built by capsule/db.py. Oracle 2: `capsule lookout check`
(Task 3) comparing SQLite and ops-pg counts nightly (spec 11.2)."""
import hashlib
import json
import os
import statistics
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

from capsule import db, lookout, paths, tick
from capsule.tests import tmpclean, test_lookout_transient as tlt
from capsule.tests import test_round_counters as rc, test_tick as tt   # module import: its TestCases are not re-collected here
from capsule import legacy_names as _LN  # noqa: E402

INTENT = paths.INTENT / "lookout"


def spool_lines(home):
    p = lookout.spool_path(home)
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines()] if p.exists() else []


class Board(unittest.TestCase):
    """A bare board (no tick) for the pure collect tests."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.board_db = paths.board_db("my-app", self.home)
        db.create_board(self.board_db)
        self.conn = db.connect(self.board_db)
        self.cfg = {"board": "my-app", "epics": ["epic/screens"],
                    "repo_url": "https://github.com/alice/my-app.git",
                    "caps": {"tick_seconds": 30}}
        self.col = lookout.Collector(self.home, "my-app", host="h", pid=1)
        self.counts = dict.fromkeys(tick.COUNTS, 0)

    def tearDown(self):
        self.conn.close()
        tmpclean.sweep(self.home)

    def collect(self, now=None):
        return self.col.collect(self.conn, self.cfg, now or time.time(), self.counts, pass_ms=5)

    def snaps(self, table):
        return [l for l in spool_lines(self.home) if l["t"] == "snap" and l["table"] == table]

    def events(self):
        return [l["row"] for l in spool_lines(self.home) if l["t"] == "event"]

    def block(self, tid, kind, reason="need a name"):
        from capsule import blocks
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        blocks.block(self.conn, tid, kind=kind, reason=reason)


class Pure(unittest.TestCase):
    def test_every_5_6_row(self):
        def o(words): return ("owner", words)
        cases = [
            (("needs_input", "needs_input", "blocked", False, None, None), o("needs your answer")),
            ((None, "decision_requested", "blocked", False, None, None), o("needs your answer")),
            ((None, "first_screen_hold", "done", False, None, None), o("screen to approve")),
            (("contradiction_loop", "contradiction_loop", "triage", False, None, None),
             o("rules disagree")),
            (("capability", "capability", "blocked", False, None, None),
             o("missing tool or access")),
            (("review_infra", "review_infra", "blocked", False, None, None),
             o("review could not run twice")),
            (("spec_stale", "spec_stale", "blocked", False, None, None),
             o("spec citation is stale")),
            ((None, "parked", "triage", False, None, None), o("failed too often, reset it")),
            (("dependency", "dependency", "todo", False, "t_parent", None), ("t_parent", "waiting for card t_parent")),
            ((None, None, "review", False, None, None), ("tick", "review queued")),
            ((None, None, "ready", False, None, None), ("tick", "queued")),
            (("weird_kind", "weird_kind", "blocked", False, None, None), o("blocked: weird_kind")),
            ((None, None, "running", True, None, None), (None, None)),
            # ruling 2 / review C2: unblock leaves tasks.block_kind set; with no open block it means nothing
            (("needs_input", None, "ready", True, None, None), (None, None)),
            (("needs_input", None, "ready", False, None, None), ("tick", "queued")),
            # a blocked card whose open event predates the watermark: block_kind still speaks for it
            (("capability", None, "blocked", False, None, None), o("missing tool or access")),
            # N3: tick.py parks a card in triage with block_kind guide_ruling while the guide rules itself
            (("guide_ruling", None, "triage", False, None, None), ("tick", "guide is ruling")),
            ((None, "contradiction_loop", "blocked", False, None, None), o("rules disagree")),
        ]
        for args, want in cases:
            with self.subTest(args=args):
                self.assertEqual(lookout.derive_waiting(*args), want)

    def test_transient_pause_names_the_local_retry_time(self):
        until = 1790000000   # 2026-09-21T14:13:20Z = 21:13 Asia/Bangkok
        self.assertEqual(lookout.derive_waiting(None, None, "ready", False, None, until),
                         ("tick", "retrying automatically at 21:13"))

    def test_human_park_names_the_trigger_reset_only(self):
        """t_0a8333bf / row 506: a human `park` (block kind stays `parked`, `human=True`) carries
        the trigger text and never the failure-limit wording, unlike a bare `parked` (no open_kind,
        tasks.block_kind None) at the same status, which keeps today's words."""
        self.assertEqual(lookout.derive_waiting(None, "parked", "blocked", False, None, None,
                                                 "flaky benchmark", True),
                         ("owner", "you parked it: flaky benchmark"))
        self.assertEqual(lookout.derive_waiting(None, "parked", "triage", False, None, None),
                         ("owner", "failed too often, reset it"))

    def test_phase_mapping(self):
        self.assertEqual(lookout.phase_of("done", None, "owner"), "waiting")  # ruling 3 screen hold
        self.assertEqual(lookout.phase_of("archived", None, "owner"), "archived")
        self.assertEqual(lookout.phase_of("done", None, None), "done")
        self.assertEqual(lookout.phase_of("triage", None, "tick", "guide_ruling"), "guide_ruling")
        self.assertEqual(lookout.phase_of("archived", None, None), "archived")
        self.assertEqual(lookout.phase_of("running", "implementer", None), "implementing")
        self.assertEqual(lookout.phase_of("review", "validator", None), "validating")
        self.assertEqual(lookout.phase_of("review", "reviewer", None), "reviewing")
        self.assertEqual(lookout.phase_of("review", "guide", None), "guide_ruling")
        self.assertEqual(lookout.phase_of("running", "korin", None), "korin")
        self.assertEqual(lookout.phase_of("blocked", None, "owner"), "waiting")
        self.assertEqual(lookout.phase_of("ready", None, "tick"), "queued")

    def test_era(self):
        for runner, era in ((None, f"{_LN.NAME}"), ("", f"{_LN.NAME}"), ("host", "capsule")):
            self.assertEqual(lookout.era_of(runner), era)

    def test_repo_slug(self):
        self.assertEqual(lookout.repo_slug("https://github.com/alice/my-app.git"), "alice/my-app")
        self.assertEqual(lookout.repo_slug("git@github.com:alice/my-app.git"), "alice/my-app")
        self.assertEqual(lookout.repo_slug("/tmp/x/origin"), "x/origin")

    def test_event_id_is_stable_sha1(self):
        want = hashlib.sha1("my-app\x1f42".encode()).hexdigest()  # noqa: S324
        self.assertEqual(lookout.event_id("my-app", 42), want)


class Collect(Board):
    def test_intent_needs_input_block_waits_on_owner(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        row = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((row["waiting_on"], row["waiting_words"], row["phase"]),
            ("owner", "needs your answer", "waiting"))

    def test_intent_accepted_round_counts_as_a_review_round(self):
        tid = rc.accepted_card(self.conn)
        self.collect()
        row = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(row["review_rounds"], 1)

    def test_intent_heartbeat_is_not_an_event(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner,"
                              " last_heartbeat_at) VALUES (?,?,?,?,?,?,?)",
                              (tid, "implementer", "running", 1, "r_live", "host", 5))
            db.event(self.conn, tid, "heartbeat", {"n": 1})
        self.collect()
        self.assertNotIn("heartbeat", [e["payload"]["event"] for e in self.events() if e["kind"] == "card"])
        self.assertEqual([e["payload"]["event"] for e in self.events() if e["kind"] == "card"], ["created"])
        run = [s["row"] for s in self.snaps("cap_runs") if s["row"]["run_id"] == "r_live"][-1]
        self.assertEqual(run["last_heartbeat_at"], lookout.iso(5))

    def test_intent_legacy_run_is_legacy_era(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        with db.txn(self.conn):
            old = self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner)"
                                    " VALUES (?,?,?,?,?,?)", (tid, "dev", "done", 1, None, None)).lastrowid
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner)"
                              " VALUES (?,?,?,?,?,?)", (tid, "implementer", "done", 2, "r_new", "host"))
        self.collect()
        eras = {s["row"]["run_id"]: s["row"]["era"] for s in self.snaps("cap_runs")}
        self.assertEqual(eras, {f"{_LN.NAME}-{old}": f"{_LN.NAME}", "r_new": "capsule"})

    def test_intent_unblocked_card_leaves_waiting_on_you(self):
        from capsule import blocks
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        blocks.unblock(self.conn, tid, by="worker")   # row 511: only a human release zeroes block_kind
        self.assertEqual(self.conn.execute("SELECT block_kind FROM tasks WHERE id=?", (tid,)).fetchone()[0],
                         "needs_input")                     # the stale column this rule must ignore
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["waiting_on"], card["block_kind"]), ("tick", None))

    def test_intent_screen_hold_on_a_done_card_waits_on_owner(self):
        from capsule import blocks
        tid = db.new_task(self.conn, title="first screen", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        blocks.hold_first_screen(self.conn, tid, "epic/screens", screens=["app.py"], commit="abc")
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["phase"], card["waiting_on"], card["waiting_words"], card["block_kind"]),
                         ("waiting", "owner", "screen to approve", "first_screen_hold"))

    def test_intent_parked_card_waits_on_owner(self):
        from capsule import blocks
        tid = db.new_task(self.conn, title="flaky", branch="epic/screens")
        blocks.record_failure(self.conn, tid, reason="tests failed", limit=1)
        self.assertEqual(self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()[:],
                         ("triage", None))
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["waiting_on"], card["waiting_words"], card["block_kind"]),
                         ("owner", "failed too often, reset it", "failure_limit"))

    def test_intent_transient_block_waits_on_owner_even_when_paused(self):
        self.assertEqual(tlt.collect_cards(self), [tlt.WANT] * 2)

    def test_intent_human_park_on_a_ready_card_names_the_trigger(self):
        """t_0a8333bf (row 506, F1: block kind stays `parked`, human=True only steers the words):
        The owner's own `park` on a card with no other open block must not read as the failure-limit
        wording - it carries his trigger text and answers with `reset` only."""
        from capsule import blocks
        tid = db.new_task(self.conn, title="flaky", branch="epic/screens")
        blocks.park(self.conn, tid, "check with design first")
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["waiting_on"], card["waiting_words"], card["block_kind"], card["block_reason"]),
                         ("owner", "you parked it: check with design first", "parked_by_human",
                          "check with design first"))

    def test_intent_human_park_closes_an_open_needs_input_block(self):
        """t_0a8333bf (F1, case 2): a park over an open needs_input hold must close that hold
        (never merge into it) - otherwise the card ships `block_kind=needs_input` and the
        Waiting-on-you view tells the owner to run `unblock`, which `park` now refuses."""
        from capsule import blocks
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        blocks.park(self.conn, tid, "hold for now")
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["waiting_on"], card["waiting_words"], card["block_kind"]),
                         ("owner", "you parked it: hold for now", "parked_by_human"))
        rows = [s["row"] for s in self.snaps("cap_blocks") if s["row"]["task_id"] == tid]
        needs_input_row = [r for r in rows if r["kind"] == "needs_input"][-1]
        self.assertEqual(needs_input_row["closed_by"], "reset")
        self.assertIsNotNone(needs_input_row["closed_at"])
        parked_row = [r for r in rows if r["kind"] == "parked_by_human"][-1]
        self.assertIsNone(parked_row["closed_at"])
        # R1 (round 2 review): `human` only steers waiting_on/words - it must never leak into
        # the spooled cap_blocks row (not a DDL column there; the shipper would choke on it).
        cap_blocks_cols = {"task_id", "opened_at", "kind", "reason", "waiting_on", "opened_event_id",
                           "closed_at", "closed_by"}
        for r in rows:
            self.assertEqual(set(r), cap_blocks_cols)

    def test_every_line_has_the_5_2_envelope(self):
        db.new_task(self.conn, title="t", branch="epic/screens")
        self.collect()
        lines = spool_lines(self.home)
        self.assertEqual({l["t"] for l in lines}, {"snap", "event", "tickmark"})
        for l in lines:
            self.assertEqual(l["v"], 1)
            if l["t"] in ("snap", "tickmark"):
                self.assertEqual(l["board"], "my-app")
                self.assertEqual(l["pass_id"], "h:1:1")
                self.assertIn("+00:00", l["snap_ts"])

    def test_event_row_carries_the_ops_pg_envelope(self):
        tid = db.new_task(self.conn, title="timer, \"quoted\"\nline é", branch="epic/screens")
        self.collect()
        ev = self.events()[0]
        self.assertEqual(set(ev), {"v", "event_id", "ts", "kind", "source", "board", "task_id", "slug", "title",
                                   "text", "link", "severity", "payload"})
        first_id = self.conn.execute("SELECT MIN(id) FROM task_events").fetchone()[0]
        self.assertEqual(ev["event_id"], lookout.event_id("my-app", first_id))
        self.assertEqual((ev["kind"], ev["source"], ev["task_id"]), ("card", "capsule", tid))
        self.assertEqual(ev["payload"]["epic"], "epic/screens")
        self.assertEqual(ev["payload"]["repo"], "alice/my-app")

    def test_watermark_advances_only_past_what_was_written(self):
        db.new_task(self.conn, title="a", branch="epic/screens")
        self.collect()
        top = self.conn.execute("SELECT MAX(id) FROM task_events").fetchone()[0]
        self.assertEqual(db.guard_get(self.conn, lookout.EVENTS_WATERMARK)[0], {"event_id": top})
        self.collect()
        self.assertEqual(len([e for e in self.events() if e["kind"] == "card"]), 1)   # not re-sent

    def test_unwritable_spool_counts_error_and_keeps_watermark(self):
        db.new_task(self.conn, title="a", branch="epic/screens")
        lookout.lookout_dir(self.home).parent.mkdir(parents=True, exist_ok=True)
        lookout.lookout_dir(self.home).write_text("a file where the directory should be")
        with self.assertRaises(OSError):
            self.collect()
        self.assertEqual(db.guard_get(self.conn, lookout.EVENTS_WATERMARK)[0], None)

    def test_failed_write_leaves_rows_unsent_for_the_next_pass(self):
        """F3 (review round 1): a row is only marked sent once `_append` actually returns. A pass
        whose spool write fails must not count its cap_cards row as shipped, or it is lost until
        the hourly resync - review focus 3 promises nothing is lost when the disk recovers."""
        tid = db.new_task(self.conn, title="a", branch="epic/screens")
        real_append = self.col._append
        self.col._append = lambda lines: (_ for _ in ()).throw(OSError("simulated disk failure"))
        with self.assertRaises(OSError):
            self.collect()                                   # pass 1: builds the row, write fails
        self.col._append = real_append
        self.collect()                                       # pass 2: disk recovers, card untouched since
        self.assertIn(tid, [s["row"]["task_id"] for s in self.snaps("cap_cards")])

    def test_short_write_is_rolled_back_never_leaving_a_torn_line(self):
        """F4 (review round 1): a regular-file `os.write` can return fewer bytes than asked for
        without raising (a plain ENOSPC often does that first). `_append` must notice, and roll
        the file back to its pre-write size rather than fsync a torn JSON line."""
        db.new_task(self.conn, title="a", branch="epic/screens")
        self.collect()
        before = lookout.spool_path(self.home).read_bytes()

        real_write = os.write
        calls = {"n": 0}

        def short_write(fd, data):
            calls["n"] += 1
            if calls["n"] == 1:
                # R1-b (review round 2): really write the first 5 bytes via the real os.write, so
                # the file actually grows - otherwise "before == after" holds trivially whether or
                # not _append's ftruncate rollback runs, and the mutant that drops the ftruncate
                # call survives.
                return real_write(fd, data[:5])
            raise OSError("simulated ENOSPC on the retry")

        with unittest.mock.patch("capsule.lookout.os.write", side_effect=short_write):
            with self.assertRaises(OSError):
                self.col._append([{"v": 1, "t": "snap", "table": "cap_cards", "board": "my-app",
                                   "snap_ts": lookout.iso(time.time()), "pass_id": "h:1:99", "row": {}}])
        self.assertEqual(lookout.spool_path(self.home).read_bytes(), before)   # rolled back, no torn line

    def test_bad_task_event_payload_is_skipped_once_and_watermark_still_advances(self):
        """F5 (review round 1): a task_events.payload that fails to parse as JSON must not stop
        the Lookout - reproduced as 3/3 passes raising and the watermark stuck at None. Fixed: the
        poisoned row is skipped, counted once, and never seen again once the watermark moves past
        it (checked over three passes, as the review asked)."""
        tid = db.new_task(self.conn, title="a", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                              (tid, "random_junk", "not valid json{", 1))
        counts1 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts1, pass_ms=5)
        self.assertEqual(counts1["lookout_errors"], 1)
        self.assertIsNotNone(db.guard_get(self.conn, lookout.EVENTS_WATERMARK)[0])
        self.assertNotIn("random_junk", [e["payload"].get("event") for e in self.events()])
        counts2 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts2, pass_ms=5)
        self.assertEqual(counts2["lookout_errors"], 0)
        counts3 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts3, pass_ms=5)
        self.assertEqual(counts3["lookout_errors"], 0)

    def test_bad_open_block_event_is_not_recounted_every_pass(self):
        """Minor (review round 2): unlike the main events loop, the block-open walk re-reads a
        card's whole block-kind history every pass it stays in scope (live or touched) - so a
        poisoned `blocked` event on a still-live card must be counted once, not every pass."""
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (tid,))
            self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                              (tid, "blocked", "not valid json{", 1))
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner)"
                              " VALUES (?,?,?,?,?,?)", (tid, "implementer", "running", 2, "r_live", "host"))
        counts1 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts1, pass_ms=5)
        counts2 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts2, pass_ms=5)
        counts3 = dict.fromkeys(tick.COUNTS, 0)
        self.col.collect(self.conn, self.cfg, time.time(), counts3, pass_ms=5)
        self.assertEqual(counts1["lookout_errors"], 1)
        self.assertEqual(counts2["lookout_errors"], 0)
        self.assertEqual(counts3["lookout_errors"], 0)

    def test_card_without_branch_ships_with_null_epic(self):
        tid = db.new_task(self.conn, title="korin fix", branch=None)
        self.collect()
        row = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertIsNone(row["epic"])
        self.assertEqual(row["product"], "my-app")
        self.assertEqual(row["phase"], "queued")

    def test_unblock_closes_block_on_next_pass(self):
        from capsule import blocks
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        blocks.unblock(self.conn, tid, by="human")
        self.collect()
        last = [s["row"] for s in self.snaps("cap_blocks") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(last["closed_by"], "unblocked")
        self.assertIsNotNone(last["closed_at"])
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertNotEqual(card["waiting_on"], "owner")

    def test_accept_closes_block_and_stops_waiting_on_owner(self):
        """F2 (review round 1): blocks.accept's request_review {by: accept} closes the open block
        the same as unblock does - a needs_input hold, accepted, with the validator now running,
        must not still read 'Waiting on you'."""
        from capsule import blocks
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, run_id,"
                              " runner, handoff) VALUES (?,?,?,?,?,?,?,?)",
                              (tid, "implementer", "done", 1, 2, "r_impl", "host",
                               json.dumps({"schema": "report.v1", "status": "done"})))
        self.block(tid, "needs_input")
        self.collect()
        self.assertTrue(blocks.accept(self.conn, tid))
        with db.txn(self.conn):
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner)"
                              " VALUES (?,?,?,?,?,?)", (tid, "validator", "running", 3, "r_val", "host"))
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertNotEqual(card["waiting_on"], "owner")
        self.assertEqual(card["role_now"], "validator")
        last = [s["row"] for s in self.snaps("cap_blocks") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(last["closed_by"], "unblocked")
        self.assertIsNotNone(last["closed_at"])

    def test_only_accept_closes_a_block_not_any_request_review(self):
        """Minor (review round 2): the ordinary `request_review` the tick itself emits when review
        starts (`tick.py:752`, payload None) must not close an open block - only `blocks.accept`'s
        own `{"by": "accept"}` does."""
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        self.block(tid, "needs_input")
        self.collect()
        with db.txn(self.conn):
            db.event(self.conn, tid, "request_review", None)
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(card["waiting_on"], "owner")
        last = [s["row"] for s in self.snaps("cap_blocks") if s["row"]["task_id"] == tid][-1]
        self.assertIsNone(last["closed_by"])

    def test_live_card_keeps_its_open_decision_on_untouched_passes(self):
        """N4: a live run, a decision_requested, then a pass where nothing touches the card."""
        tid = db.new_task(self.conn, title="ask", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
            self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner)"
                              " VALUES (?,?,?,?,?,?)", (tid, "implementer", "running", 1, "r_ask", "host"))
            db.event(self.conn, tid, "decision_requested", {"question": "which colour?"}, run_id="r_ask")
        self.collect()
        self.collect()                                       # untouched, still live
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual((card["waiting_on"], card["waiting_words"]),
            ("owner", "needs your answer"))
        with db.txn(self.conn):                              # the run heartbeats: the row changes
            self.conn.execute("UPDATE task_runs SET last_heartbeat_at=9 WHERE run_id='r_ask'")
        self.collect()
        card = [s["row"] for s in self.snaps("cap_cards") if s["row"]["task_id"] == tid][-1]
        self.assertEqual(card["waiting_on"], "owner")

    def test_unchanged_rows_are_not_resent_but_the_tickmark_is(self):
        db.new_task(self.conn, title="a", branch="epic/screens")
        self.collect()
        n_cards = len(self.snaps("cap_cards"))
        self.collect()
        self.assertEqual(len(self.snaps("cap_cards")), n_cards)
        self.assertEqual(len([l for l in spool_lines(self.home) if l["t"] == "tickmark"]), 2)

    def test_hourly_full_resync_resends_everything(self):
        db.new_task(self.conn, title="a", branch="epic/screens")
        self.collect()
        self.col.passes = lookout.RESYNC_EVERY - 1
        self.collect()
        self.assertEqual(len(self.snaps("cap_cards")), 2)

    def test_health_event_every_tenth_pass(self):
        for _ in range(lookout.HEALTH_EVERY):
            self.collect()
        health = [e for e in self.events() if e["kind"] == "health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["payload"]["tick"], True)
        self.assertEqual(health[0]["source"], "capsule")

    def test_block_reason_is_scrubbed(self):
        tid = db.new_task(self.conn, title="t", branch="epic/screens")
        secret = "ghp_" + "a" * 36
        self.block(tid, "needs_input", reason=f"token is {secret}")
        self.collect()
        raw = lookout.spool_path(self.home).read_text(encoding="utf-8")
        self.assertNotIn(secret, raw)

    def test_epic_row_counts_cards(self):
        a = db.new_task(self.conn, title="a", branch="epic/screens")
        db.new_task(self.conn, title="b", branch="epic/screens")
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (a,))
        self.collect()
        epic = [s["row"] for s in self.snaps("cap_epics")][-1]
        self.assertEqual((epic["cards_total"], epic["cards_done"], epic["publish_state"]), (2, 1, "none"))

    def test_file_matches_tests(self):
        # Named without the "test_intent_" prefix so the dir(cls) scan below does not match itself.
        text = (INTENT / "collect.jsonl").read_text()
        sums = dict(reversed(l.split(maxsplit=1)) for l in (INTENT / "SHA256SUMS").read_text().splitlines())
        self.assertEqual(sums["collect.jsonl"], hashlib.sha256(text.encode()).hexdigest())
        ids = {json.loads(l)["id"].replace("-", "_") for l in text.splitlines()}
        tests = {n[len("test_intent_"):] for cls in (Collect, Spool) for n in dir(cls) if n.startswith("test_intent_")}
        self.assertEqual(ids, tests)


class Spool(Board):
    def test_intent_spool_over_limit_compacts_without_losing_events(self):
        sp = lookout.spool_path(self.home)
        sp.parent.mkdir(parents=True)
        lines = []
        for i in range(3):
            lines.append({"v": 1, "t": "snap", "table": "cap_cards", "board": "b", "snap_ts": f"2026-09-27T00:00:0{i}+00:00",
                          "pass_id": f"h:1:{i}", "row": {"task_id": "t1", "title": str(i)}})
            lines.append({"v": 1, "t": "event", "row": {"event_id": f"e{i}"}})
        sp.write_text("".join(json.dumps(l) + "\n" for l in lines))
        old = lookout.SPOOL_MAX_BYTES
        lookout.SPOOL_MAX_BYTES = 10
        try:
            self.assertEqual(lookout.maintain_spool(self.home), "compacted")
        finally:
            lookout.SPOOL_MAX_BYTES = old
        kept = spool_lines(self.home)
        self.assertEqual([l["row"]["event_id"] for l in kept if l["t"] == "event"], ["e0", "e1", "e2"])
        self.assertEqual([l["row"]["title"] for l in kept if l["t"] == "snap"], ["2"])
        self.assertEqual(lookout.offset_path(self.home).read_text(), "0")

    def test_intent_offset_reset_lands_before_the_spool_rename(self):
        """T3 review r2 item 4: a crash between the rename and the offset reset must never be
        able to leave the offset pointing into the *old* file's layout once the *new* (rotated
        or compacted) file is in place - that is what starts the shipper mid-line or skipping
        bytes. Fix: write the offset reset before the rename, so a crash the instant after
        `os.replace` returns can only ever land on a state where offset already matches the
        file `os.replace` just installed (oracle 1: offset == '0'), never the old, larger file
        under the new, shorter one (oracle 2: the spool bytes on disk are exactly the replaced
        (rotated/compacted) content, not a half-updated mix)."""
        sp = lookout.spool_path(self.home)
        sp.parent.mkdir(parents=True)
        lines = [{"v": 1, "t": "event", "row": {"event_id": f"e{i}"}} for i in range(3)]
        sp.write_text("".join(json.dumps(l) + "\n" for l in lines))
        lookout.offset_path(self.home).write_text(str(sp.stat().st_size))   # fully shipped: will rotate
        real_replace = lookout.os.replace

        def crash_after_replace(src, dst):
            real_replace(src, dst)
            raise RuntimeError("simulated crash right after the rename")

        old = lookout.SPOOL_MAX_BYTES
        lookout.SPOOL_MAX_BYTES = 1                          # force the rotate/compact branch
        try:
            with unittest.mock.patch.object(lookout.os, "replace", side_effect=crash_after_replace):
                with self.assertRaises(RuntimeError):
                    lookout.maintain_spool(self.home)
        finally:
            lookout.SPOOL_MAX_BYTES = old
        # oracle 1: the offset already reads 0, matching the file the (patched) replace installed
        self.assertEqual(lookout.offset_path(self.home).read_text(), "0")
        # oracle 2: the spool on disk is the rotated (now-empty) file the replace call installed,
        # never a state where offset (0) and the file on disk (still the 3-line original) disagree
        self.assertEqual(sp.read_bytes(), b"")

    def test_intent_crash_before_the_rename_leaves_the_old_file_and_offset_0(self):
        """M2 (fix round 1): the other half of the ordering - a crash before `os.replace` ever
        runs. The offset is already 0 (written first); the rename never happened, so the spool on
        disk is still the old (unrotated) file untouched. Oracle 1: offset == '0'. Oracle 2: the
        spool bytes are exactly the original, unrotated content - a safe, idempotent state (the
        next ship just re-sends the old file from byte 0; every row upserts by natural key or
        dedupes by event_id), never a mismatch."""
        sp = lookout.spool_path(self.home)
        sp.parent.mkdir(parents=True)
        lines = [{"v": 1, "t": "event", "row": {"event_id": f"e{i}"}} for i in range(3)]
        original = "".join(json.dumps(l) + "\n" for l in lines)
        sp.write_text(original)
        lookout.offset_path(self.home).write_text(str(sp.stat().st_size))

        def crash_before_replace(src, dst):
            raise RuntimeError("simulated crash right before the rename")

        old = lookout.SPOOL_MAX_BYTES
        lookout.SPOOL_MAX_BYTES = 1
        try:
            with unittest.mock.patch.object(lookout.os, "replace", side_effect=crash_before_replace):
                with self.assertRaises(RuntimeError):
                    lookout.maintain_spool(self.home)
        finally:
            lookout.SPOOL_MAX_BYTES = old
        self.assertEqual(lookout.offset_path(self.home).read_text(), "0")           # oracle 1
        self.assertEqual(sp.read_text(), original)                                  # oracle 2

    def test_still_over_after_compaction_stops_snap_lines_but_keeps_events(self):
        """Spec 5.10 second half (review m2): snap lines pause, events never drop."""
        db.new_task(self.conn, title="a", branch="epic/screens")
        old = lookout.SPOOL_MAX_BYTES
        lookout.SPOOL_MAX_BYTES = 1                          # nothing can get under it
        try:
            self.collect()
            self.assertTrue(self.col.snaps_paused)
            tid = db.new_task(self.conn, title="b", branch="epic/screens")
            self.collect()
        finally:
            lookout.SPOOL_MAX_BYTES = old
        lines = spool_lines(self.home)
        self.assertIn(tid, [l["row"].get("task_id") for l in lines if l["t"] == "event"])
        self.assertNotIn(tid, [l["row"].get("task_id") for l in lines if l["t"] == "snap"])
        self.collect()                                       # limit back to 64 MB: rows resent
        self.assertFalse(self.col.snaps_paused)
        self.assertIn(tid, [s["row"]["task_id"] for s in self.snaps("cap_cards")])

    def test_compaction_waits_while_the_shipper_holds_the_lock(self):
        import fcntl
        d = lookout.lookout_dir(self.home)
        d.mkdir(parents=True)
        lookout.spool_path(self.home).write_text("{}\n" * 100)
        with open(d / "ship.lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            old = lookout.SPOOL_MAX_BYTES
            lookout.SPOOL_MAX_BYTES = 10
            try:
                self.assertEqual(lookout.maintain_spool(self.home), "busy")
            finally:
                lookout.SPOOL_MAX_BYTES = old

    def test_benchmark_3x_my_app_median_under_50ms(self):
        """A10: 300 tasks, 1,000 runs, 15,000 events; steady-state passes (after the first full sync)."""
        with db.txn(self.conn):
            for i in range(300):
                tid = f"t_{i:04d}"
                self.conn.execute("INSERT INTO tasks (id, title, status, created_at, branch_name) VALUES (?,?,?,?,?)",
                                  (tid, f"card {i}", "done" if i % 3 else "ready", 1, "epic/screens"))
                for j in range(50):
                    self.conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?,?,?,?)",
                                      (tid, "claimed", None, 1))
            for k in range(1000):
                self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, run_id, runner)"
                                  " VALUES (?,?,?,?,?,?,?)", (f"t_{k % 300:04d}", "implementer", "done", 1, 2, f"r{k}", "host"))
        self.collect()
        times = []
        for _ in range(7):
            t0 = time.perf_counter()
            self.collect()
            times.append((time.perf_counter() - t0) * 1000)
        self.assertLess(statistics.median(times), 50.0, times)


class TickStep(tt.TickCase):
    def test_pass_counts_lookout_and_writes_a_tickmark(self):
        counts = self.tick.once()
        self.assertEqual(counts["lookout_errors"], 0)
        marks = [l for l in spool_lines(self.home) if l["t"] == "tickmark"]
        self.assertEqual(len(marks), 1)
        self.assertEqual(marks[0]["row"]["counts"]["dispatched"], counts["dispatched"])

    def test_tickmark_carries_the_previous_pass_lookout_ms(self):
        """Ruling 7 / review I3: collect cannot know its own duration, so it records the last pass's."""
        self.tick.once()                                   # builds the collector
        real = self.tick.lookout.collect

        def slow(*a, **kw):
            time.sleep(0.03)
            return real(*a, **kw)
        self.tick.lookout.collect = slow
        second = self.tick.once()
        self.tick.lookout.collect = real
        self.tick.once()
        marks = [l for l in spool_lines(self.home) if l["t"] == "tickmark"]
        self.assertGreaterEqual(second["lookout_ms"], 30)
        self.assertEqual(marks[2]["row"]["lookout_ms"], second["lookout_ms"])

    def test_tickmark_counts_carries_the_previous_pass_lookout_errors(self):
        """F8 (review round 1): `counts.lookout_errors` embedded in the tickmark/health payload
        must ship the previous pass's real total (ruling 7, same as lookout_ms) instead of always
        reading 0 - the field never reached ops-pg otherwise."""
        self.tick.once()                                   # pass 0: builds the collector, succeeds
        real = self.tick.lookout.collect

        def boom(*a, **kw):
            raise RuntimeError("lookout broke")
        self.tick.lookout.collect = boom
        first = self.tick.once()                           # pass 1: collect fails, no tickmark written
        self.tick.lookout.collect = real
        second = self.tick.once()                          # pass 2: recovers; its tickmark carries pass 1's count
        self.assertEqual(first["lookout_errors"], 1)
        self.assertEqual(second["lookout_errors"], 0)
        marks = [l for l in spool_lines(self.home) if l["t"] == "tickmark"]
        self.assertEqual(len(marks), 2)                    # pass 1 never got as far as writing one
        self.assertEqual(marks[0]["row"]["counts"]["lookout_errors"], 0)
        self.assertEqual(marks[1]["row"]["counts"]["lookout_errors"], 1)

    def fake_events(self, calls):
        """state_root: the tick's weekly-pause read stays under the test's own tmp home."""
        return types.SimpleNamespace(
            state_root=lambda: self.home / "state", log=lambda *a, **kw: calls.append("log"),
            ship=lambda *a, **kw: calls.append("ship") or 0)

    def test_heartbeat_leaves_shipping_to_the_lookout_shipper_when_it_runs(self):
        """G3: with shipping on, the heartbeat stops calling events.ship() itself (spec 5.1)."""
        calls = []
        self.tick._events_mod = self.fake_events(calls)
        self.tick.lookout_kick = lambda *a, **kw: True
        self.cfg["lookout"] = {"ship": False}
        self.write_cfg()
        self.tick.once()
        self.assertIn("ship", calls)
        calls.clear()
        self.cfg["lookout"] = {"ship": True}
        self.write_cfg()
        self.tick.once()
        self.assertEqual(calls, ["log"])

    def test_collect_exception_is_counted_and_pass_completes(self):
        def boom(*a, **kw):
            raise RuntimeError("lookout broke")
        self.tick.lookout = lookout.Collector(self.home, "test", host="h", pid=1)
        self.tick.lookout.collect = boom
        counts = self.tick.once()
        self.assertEqual(counts["lookout_errors"], 1)
        self.assertEqual(counts["dispatched"], 1)          # the rest of the pass still ran

    def test_shipping_is_on_when_the_lookout_key_is_absent_and_off_only_when_opted_out(self):
        launched = []
        self.tick.lookout_kick = lambda h, v, *a: v[1] == "ship" and launched.append(v) or True
        self.tick.once()
        self.assertEqual(launched, [["lookout", "ship"]])
        self.cfg["lookout"] = {"ship": False}
        self.write_cfg()
        self.tick.once()
        self.assertEqual(len(launched), 1)                # no second kick

    def test_truthy_non_boolean_ship_value_is_pinned(self):
        """T3 review r2 item 5: both `lookout.ship` gates - `Tick._heartbeat`'s (whether the
        heartbeat still self-ships) and `Tick._lookout`'s (whether the shipper child is kicked) -
        use a strict `is True`, so a config value that is merely truthy - a string like "yes" or the
        int 1, e.g. from a hand-edited YAML/JSON config - must NOT flip either gate: the shipper
        child stays unlaunched (oracle 1) and the tick's own heartbeat keeps shipping itself
        (oracle 2), exactly as when `ship` is absent or False. Pins the current strict-boolean
        contract against an accidental future `if flags.get("ship"):` loosening."""
        for truthy in ("yes", 1, "true"):
            with self.subTest(value=truthy):
                launched = []
                self.tick.lookout_kick = lambda h, v, *a: launched.extend([v] * (v[1] == "ship"))
                self.cfg["lookout"] = {"ship": truthy}
                self.write_cfg()

                calls = []
                self.tick._events_mod = self.fake_events(calls)
                self.tick.once()
                self.assertEqual(launched, [])              # oracle 1: no shipper child kicked
                self.assertIn("ship", calls)                 # oracle 2: heartbeat still self-ships

    def test_completed_payload_carries_gates(self):
        """A9: the completed event records every review outcome, rounds and overrides."""
        # Ruling (task-2, ambiguity): the brief's verbatim test never touches a file, so
        # workspace.commit() would return None and "commit" never reaches the payload (existing
        # `{"commit": sha, ...} if sha else {"gates": gates}` behaviour, unchanged by this task).
        # A real card writes something, so give the implementer a file to commit, matching the
        # pattern `Inheritance.test_passed_card_is_committed_...` already uses in test_tick.py.
        self.hooks["implementer"] = lambda job, sp, run: (Path(job["workspace"]) / "complete.html").write_text("x\n")
        self.script["implementer"] = [[tt.IMPL_DONE]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["gates"], {"validator": "pass", "reviewer": "pass", "guide": "pass",
                                            "rounds": 1, "overrides": []})
        self.assertIn("commit", payload)

    def test_aligned_uncited_records_the_guide_gate_as_pass(self):
        """F7 (review round 1): an uncited "drifted" verdict is treated as aligned_uncited and
        still reaches commit (row 364 gap), but the guide's own document still says
        verdict=drifted - the completed event's gates.guide must read "pass", not "fail"."""
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          ("Add the timer screen; the card names no section or row.", self.tid))
        self.hooks["implementer"] = lambda job, sp, run: (Path(job["workspace"]) / "complete.html").write_text("x\n")
        self.script["implementer"] = [[tt.IMPL_DONE]]
        self.script["guide"] = [[dict(tt.GUIDE_OK, verdict="drifted", rows=[], reason="looks unfinished")]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='completed'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["gates"]["guide"], "pass")


class GateWord(unittest.TestCase):
    def test_every_mapping(self):
        """F7 (review round 1): pin every `_gate_word` mapping, including `overridden -> pass`
        (the survivor mutant `"overridden": "fail"` was otherwise unkilled)."""
        gw = tick.Tick._gate_word
        self.assertEqual(gw(None), "infra")
        for verdict in ("pass", "aligned", "aligned_uncited", "overridden"):
            self.assertEqual(gw({"verdict": verdict}), "pass")
        for verdict in ("fail", "drifted"):
            self.assertEqual(gw({"verdict": verdict}), "fail")
        self.assertEqual(gw({"verdict": "something_unknown"}), "skipped")


if __name__ == "__main__":
    unittest.main()
