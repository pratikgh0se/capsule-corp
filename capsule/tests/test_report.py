import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import (blocks, db, human, incident_inputs, incidents, intent, override,
                     paths, report)
from capsule.tests.test_evals_report import q2b_example
from capsule import legacy_names as _LN  # noqa: E402


def setup_board():
    home = Path(tempfile.mkdtemp())
    board = "my-app"
    board_db = paths.board_db(board, home)
    db.create_board(board_db)
    conn = db.connect(board_db)
    tids = []
    for i in range(3):
        tid = db.new_task(conn, title=f"card {i}", now=10)
        tids.append(tid)
    # 7 runs total, spread across the 3 cards, in the vocabulary the tick actually
    # writes (task-11 review I1): one 'failed' (error_class 'invalid_output') and one
    # 'stale'.
    outcomes = ["completed", "completed", "failed", "completed", "stale", "completed", "completed"]
    error_classes = [None, None, "invalid_output", None, None, None, None]
    owners = [tids[0], tids[0], tids[0], tids[1], tids[1], tids[2], tids[2]]
    for task_id, outcome, error_class in zip(owners, outcomes, error_classes):
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, error_class, started_at, ended_at) VALUES"
            " (?, 'implementer', 'done', ?, ?, 10, 20)", (task_id, outcome, error_class))
    # one decision raised on card 0
    db.event(conn, tids[0], "decision_requested", {"text": "which way?"}, now=15)
    # card 1 merged
    conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (130, tids[1]))
    conn.commit()
    conn.close()
    # 2 human spool files: one on card 0, one on card 2
    human.write(home, board, tids[0], "clarify", "which branch?", now=12)
    human.write(home, board, tids[2], "approve", "looks fine", now=18)
    return home, board, tids


class KorinBacklog(unittest.TestCase):
    def test_report_shows_the_backlog_line(self):
        home, board, _ = setup_board()
        cut = incident_inputs.since_ts("2026-10-24")
        conn = db.connect(paths.board_db(board, home))
        incidents.open_incident(conn, board=board, kind="crash", key="old", now=cut - 86400)
        incidents.open_incident(conn, board=board, kind="crash", key="new", now=cut + 86400)
        conn.close()
        doc = report.interventions(home, board)       # no since configured
        self.assertEqual(doc["incidents"]["backlog"], 0)
        cfgp = paths.config_path(home, board)
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"korin": {"since": "2026-10-24"}}))
        doc = report.interventions(home, board)
        self.assertEqual(doc["incidents"]["backlog"], 1)
        self.assertIn("never dispatched): 1", report.render_md(doc))

    def test_a_superseded_pre_since_incident_is_not_backlog(self):
        home, board, _ = setup_board()
        cut = incident_inputs.since_ts("2026-10-24")
        conn = db.connect(paths.board_db(board, home))
        incidents.open_incident(conn, board=board, kind="crash", key="old", now=cut - 86400)
        gone, _ = incidents.open_incident(
            conn, board=board, kind="crash", key="gone", now=cut - 86400)
        conn.execute("UPDATE incidents SET status='superseded' WHERE id=?", (gone,))
        conn.commit()
        direct = conn.execute("SELECT COUNT(*) FROM incidents WHERE status NOT IN"
                              " ('closed','superseded') AND opened_at < ?", (cut,)).fetchone()[0]
        conn.close()
        cfgp = paths.config_path(home, board)
        cfgp.parent.mkdir(parents=True, exist_ok=True)
        cfgp.write_text(json.dumps({"korin": {"since": "2026-10-24"}}))
        doc = report.interventions(home, board)
        self.assertEqual((doc["incidents"]["backlog"], direct), (1, 1))


class Interventions(unittest.TestCase):
    def test_totals_match_the_fixture(self):
        home, board, tids = setup_board()
        doc = report.interventions(home, board)
        self.assertEqual(len(doc["cards"]), 3)
        self.assertEqual(doc["totals"]["runs"], 7)
        self.assertAlmostEqual(doc["interventions_per_card"], 2 / 3)
        self.assertAlmostEqual(doc["wasted_run_rate"], 2 / 7)
        self.assertIn("vs_u1", doc)
        self.assertLess(doc["vs_u1"]["interventions_per_card"], 0)
        self.assertEqual(doc["merged"], 1)

    def test_wasted_run_rate_counts_failed_and_stale_by_error_class(self):
        """I1: the tick never writes 'crashed'/'timed_out' outcomes - it writes
        'failed'/'stale'. Before the fix, WASTED_OUTCOMES only matched the legacy
        names and this fixture's real-vocabulary runs read as 0 wasted."""
        home, board, tids = setup_board()
        doc = report.interventions(home, board)
        self.assertAlmostEqual(doc["totals"]["wasted_run_rate"], 2 / 7)
        self.assertEqual(doc["totals"]["wasted_by_error_class"], {"invalid_output": 1, "unknown": 1})

    def test_since_filters_out_everything_before_it(self):
        home, board, tids = setup_board()
        doc = report.interventions(home, board, since=1000)
        self.assertEqual(doc["totals"]["cards"], 0)
        self.assertEqual(doc["interventions_per_card"], 0.0)

    def test_human_done_files_count_as_interventions(self):
        """I3: R5 has the tick archive handled human files into human_dir/done/. The
        report must count those too, or the day archiving lands, interventions_per_card
        silently falls to 0."""
        home, board, tids = setup_board()
        done = home / "boards" / board / "human" / "done"
        done.mkdir(parents=True)
        (done / "1.json").write_text(json.dumps({"task_id": tids[1], "kind": "clarify", "at": 20}))
        doc = report.interventions(home, board)
        self.assertEqual(doc["totals"]["cards"], 3)
        self.assertAlmostEqual(doc["interventions_per_card"], 3 / 3)
        card1 = next(c for c in doc["cards"] if c["task_id"] == tids[1])
        self.assertEqual(card1["human"], 1)

    def test_failure_events_are_listed_per_card_with_their_run_id(self):
        """R25 (t_53d35852): a failed/parked/blocked event is tied to its run through payload.run_id."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        db.event(conn, tids[0], "failed", {"reason": "x", "run_id": "a" * 32}, now=16)
        db.event(conn, tids[0], "blocked", {"reason": "q"}, now=17)            # a pre-R25 event: no run_id
        db.event(conn, tids[0], "comment", {"run_id": "b" * 32}, now=17)       # not a failure event
        conn.commit()
        conn.close()
        doc = report.interventions(home, board)
        card0 = next(c for c in doc["cards"] if c["task_id"] == tids[0])
        self.assertEqual(card0["failure_events"], [{"kind": "failed", "run_id": "a" * 32, "by": None, "at": 16},
                                                   {"kind": "blocked", "run_id": None, "by": None, "at": 17}])
        self.assertEqual(doc["totals"]["failure_events"], 2)
        self.assertEqual(doc["totals"]["failure_events_without_run_id"], 1)
        self.assertEqual(report.interventions(home, board, since=17)["totals"]["failure_events"], 1)

    def test_a_human_park_is_not_counted_as_a_failure_but_a_failure_limit_park_is(self):
        """t_91c331db fix round 1, B3 (round 2, r3: docstring corrected - this asserts only each
        card's `failure_events` list, not `totals.failure_events`): `blocks.park`'s event carries
        `by: human` and is excluded from `failure_events`/the waste totals; `blocks.record_failure`'s
        own `parked` event (the failure-limit triage park, no `by`) still counts. Every
        `failure_events` entry now carries a `by` key (an additive field alongside `kind`/`run_id`/
        `at`) - the card's own second oracle: the two cards' lists disagree on which `parked`
        survived."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        blocks.park(conn, tids[0], "waiting on a vendor key", now=19)
        for _ in range(3):
            blocks.record_failure(conn, tids[1], reason="boom", limit=3, now=20)
        conn.commit()
        conn.close()
        doc = report.interventions(home, board)
        card0 = next(c for c in doc["cards"] if c["task_id"] == tids[0])
        card1 = next(c for c in doc["cards"] if c["task_id"] == tids[1])
        kinds = [[f["kind"] for f in c["failure_events"]] for c in (card0, card1)]
        self.assertNotIn("parked_by_human", kinds[0])
        self.assertEqual([k for k in kinds[1] if k == "failure_limit"], ["failure_limit"])

    def test_legacy_parked_events_keep_the_by_human_filter(self):
        """t_3e108a93: pre-rename `parked` rows - by=human is no failure, no `by` still is."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        db.event(conn, tids[0], "parked", {"by": "human"}, now=19)
        db.event(conn, tids[1], "parked", {"reason": "boom"}, now=20)
        conn.commit()
        conn.close()
        doc = report.interventions(home, board)
        card0 = next((c for c in doc["cards"] if c["task_id"] == tids[0]), {"failure_events": []})
        card1 = next(c for c in doc["cards"] if c["task_id"] == tids[1])
        self.assertNotIn("parked", [f["kind"] for f in card0["failure_events"]])
        self.assertIn("parked", [f["kind"] for f in card1["failure_events"]])

    def test_an_implementer_timeout_counts_once(self):
        """Fix round 1 (m5): the `timed_out` event and the `failed` it caused are one failure."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        db.event(conn, tids[1], "timed_out", {"cap": 100, "run_id": "c" * 32}, now=16)
        db.event(conn, tids[1], "failed", {"reason": "timed out", "run_id": "c" * 32}, now=16)
        db.event(conn, tids[1], "timed_out", {"cap": 100, "run_id": "d" * 32}, now=17)   # a review run's
        db.event(conn, tids[1], "reclaimed", {"stale_lock": "h:x", "run_id": "e" * 32}, now=18)
        conn.commit()
        conn.close()
        doc = report.interventions(home, board)
        card1 = next(c for c in doc["cards"] if c["task_id"] == tids[1])
        self.assertEqual([(f["kind"], f["run_id"]) for f in card1["failure_events"]],
                         [("failed", "c" * 32), ("timed_out", "d" * 32), ("reclaimed", "e" * 32)])
        md = report.render_md(doc)
        self.assertIn("failure_events=3 (0 without run_id)", md)
        self.assertIn(f"| {tids[1]} card 1 | done | 2 | 1 | 0 | 0 | 1 | 3 |", md)

    def test_a_spec_stale_hold_is_not_a_failure_but_is_shown_separately(self):
        """Row 472 N2: `blocks.block(..., kind='spec_stale')` writes a `blocked` event whose
        payload carries `kind='spec_stale'` (see blocks.py `block()`) - driven here through the
        real emitter, not a hand-written fixture (row 459). The guide found a cited decision row
        contradicts the spec section, not the implementer's work - so this must not land in
        `failure_events`/the waste signal, only in a separate `spec_stale_holds` bucket, both per
        card and in totals. A second, ordinary `blocked` event (no spec_stale payload) on the
        same card is the control: it still counts as a failure."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        self.assertEqual(
            blocks.block(conn, tids[0], kind="spec_stale", reason="spec vs row 999", now=16), "blocked")
        db.event(conn, tids[0], "blocked", {"reason": "ordinary hold"}, now=17)
        conn.commit()
        conn.close()

        doc = report.interventions(home, board)
        card0 = next(c for c in doc["cards"] if c["task_id"] == tids[0])

        self.assertEqual([f["kind"] for f in card0["failure_events"]], ["blocked"])
        self.assertEqual(card0["failure_events"][0]["at"], 17)
        self.assertEqual(doc["totals"]["failure_events"], 1)

        # the control: the spec_stale hold is shown separately, never as a failure.
        self.assertEqual([h["kind"] for h in card0["spec_stale_holds"]], ["blocked"])
        self.assertEqual(card0["spec_stale_holds"][0]["at"], 16)
        self.assertEqual(doc["totals"]["spec_stale_holds"], 1)

        md = report.render_md(doc)
        self.assertIn("spec_stale_holds=1", md)
        self.assertIn("failure_events=1 (1 without run_id)", md)

    def test_a_card_with_only_a_spec_stale_hold_is_still_listed(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        extra = db.new_task(conn, title="spec stale only", now=10)
        conn.commit()
        self.assertEqual(
            blocks.block(conn, extra, kind="spec_stale", reason="spec vs row 999", now=20), "blocked")
        conn.close()
        doc = report.interventions(home, board, since=15)
        self.assertIn(extra, [c["task_id"] for c in doc["cards"]])
        card = next(c for c in doc["cards"] if c["task_id"] == extra)
        self.assertEqual(len(card["spec_stale_holds"]), 1)
        self.assertEqual(card["failure_events"], [])

    def test_both_spec_stale_hold_shapes_from_blocks_block_are_holds_not_failures(self):
        """I1 (review of t_2f7585e3 round 1): `blocks.block(kind='spec_stale')` has two shapes -
        a first hold (`blocked`) and, on the second hold for the same kind, `block_loop_detected`
        (BLOCK_RECURRENCE_LIMIT=2, the card parks in triage). Both are guide-found spec/decision
        contradictions, never the implementer's work, so both must land in `spec_stale_holds`, not
        `failure_events`. A hand mutation that dropped `block_loop_detected` from the check
        (matching only the literal event kind `blocked`) passed the old hand-written-fixture tests
        (only the recurrence-1 shape) but is caught here."""
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        t = db.new_task(conn, title="stale", now=10)
        conn.commit()
        self.assertEqual(blocks.block(conn, t, kind="spec_stale", reason="spec vs row 999", now=20), "blocked")
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (t,)); conn.commit()
        self.assertEqual(blocks.block(conn, t, kind="spec_stale", reason="spec vs row 999", now=21), "triage")
        conn.close()
        card = next(c for c in report.interventions(home, board)["cards"] if c["task_id"] == t)
        self.assertEqual([h["kind"] for h in card["spec_stale_holds"]], ["blocked", "block_loop_detected"])
        self.assertEqual(card["failure_events"], [])

    def test_md_renders_a_table_with_the_cards_and_totals(self):
        home, board, tids = setup_board()
        doc = report.interventions(home, board)
        md = report.render_md(doc)
        for tid in tids:
            self.assertIn(tid, md)
        self.assertIn("totals:", md)
        self.assertIn("vs U1", md)


class Unattended(unittest.TestCase):
    """EVAL-07: merged with zero counted human events is unattended."""

    def _doc(self, *, merged, tags=()):
        home = Path(tempfile.mkdtemp())
        board_db = paths.board_db("my-app", home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        tid = db.new_task(conn, title="card", now=10)
        conn.execute(      # a card with no run and no event is not listed at all
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at)"
            " VALUES (?, 'implementer', 'done', 'completed', 10, 20)", (tid,))
        if merged:
            conn.execute("UPDATE tasks SET status='done', completed_at=50 WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        for i, tag in enumerate(tags):
            human.write(home, "my-app", tid, "clarify", "x", now=12 + i, tag=tag)
        doc = report.interventions(home, "my-app")
        return doc, doc["cards"][0]

    def test_merged_without_human_events_is_unattended(self):
        doc, card = self._doc(merged=True)
        self.assertIs(card["unattended"], True)
        self.assertEqual((doc["totals"]["unattended"], doc["totals"]["unattended_rate"]), (1, 1.0))
        self.assertIn("unattended=1/1", report.render_md(doc))

    def test_merged_with_one_human_event_is_not(self):
        doc, card = self._doc(merged=True, tags=[None])
        self.assertIs(card["unattended"], False)
        self.assertEqual((doc["totals"]["unattended"], doc["totals"]["unattended_rate"]), (0, 0.0))
        self.assertIn("unattended=0/1", report.render_md(doc))

    def test_only_excluded_tag_events_still_unattended(self):
        doc, card = self._doc(merged=True, tags=["migration-reset"])
        self.assertIs(card["unattended"], True)
        self.assertEqual((card["human"], card["migration_resets"]), (0, 1))
        self.assertEqual(doc["totals"]["unattended"], 1)

    def test_unmerged_card_is_null(self):
        doc, card = self._doc(merged=False)
        self.assertIsNone(card["unattended"])
        self.assertEqual((doc["totals"]["unattended"], doc["totals"]["unattended_rate"]), (0, 0.0))

    def _merged_cards(self, created_times, since):
        home = Path(tempfile.mkdtemp())
        board_db = paths.board_db("my-app", home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        for i, created in enumerate(created_times):
            tid = db.new_task(conn, title=f"c{i}", now=created)
            conn.execute(
                "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at)"
                " VALUES (?, 'implementer', 'done', 'completed', 30, 40)", (tid,))
            conn.execute("UPDATE tasks SET status='done', completed_at=50 WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        return report.interventions(home, "my-app", since=since)

    def test_card_created_before_the_cutoff_is_not_judged(self):
        doc = self._merged_cards([10], since=20)
        self.assertIsNone(doc["cards"][0]["unattended"])
        self.assertEqual((doc["totals"]["merged"], doc["totals"]["unattended"],
                          doc["totals"]["unattended_rate"]), (1, 0, 0.0))
        self.assertIn("unattended=0/0", report.render_md(doc))

    def test_old_and_new_merged_cards_rate_over_judged_only(self):
        doc = self._merged_cards([10, 25], since=20)
        self.assertEqual(doc["totals"]["merged"], 2)
        self.assertEqual((doc["totals"]["unattended"], doc["totals"]["unattended_rate"]), (1, 1.0))
        self.assertIn("unattended=1/1", report.render_md(doc))

    def test_a_first_screen_hold_counts_as_attended(self):
        home = Path(tempfile.mkdtemp())
        board_db = paths.board_db("my-app", home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        tid = db.new_task(conn, title="ui", now=10)
        db.event(conn, tid, "first_screen_hold", {}, now=15)
        conn.execute("UPDATE tasks SET status='done', completed_at=50 WHERE id=?", (tid,))
        conn.commit()
        conn.close()
        doc = report.interventions(home, "my-app")
        self.assertIs(doc["cards"][0]["unattended"], False)
        self.assertEqual(doc["totals"]["unattended"], 0)

    def test_empty_board(self):
        home = Path(tempfile.mkdtemp())
        db.create_board(paths.board_db("my-app", home))
        doc = report.interventions(home, "my-app")
        self.assertEqual((doc["totals"]["unattended"], doc["totals"]["unattended_rate"]), (0, 0.0))
        self.assertIn("unattended=0/0", report.render_md(doc))


class ContractAmendments(unittest.TestCase):
    """Zenkai rule 2 (second oracle, fix round 1 on t_3e0b7bf5): a `contract_amended`
    event must be visible per card (count + last reason) and in totals, the same way
    first_screen is - but it never feeds interventions_per_card/vs_u1 (an
    operator-approved edit is not U1's kind of rework)."""

    def test_amendments_counted_per_card_and_in_totals_not_vs_u1(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        db.event(conn, tids[0], "contract_amended",
                  {"issuer": "owner", "reason": "first edit", "old_file_set": [],
                   "new_file_set": ["a.py"]},
                  now=20)
        db.event(conn, tids[0], "contract_amended",
                  {"issuer": "owner", "reason": "second edit", "old_file_set": ["a.py"],
                   "new_file_set": ["b.py"]},
                  now=21)
        conn.commit()
        conn.close()

        doc = report.interventions(home, board)
        card0 = next(c for c in doc["cards"] if c["task_id"] == tids[0])
        self.assertEqual(card0["contract_amendments"], 2)
        self.assertEqual(card0["last_amend_reason"], "second edit")
        self.assertEqual(doc["totals"]["contract_amendments"], 2)
        # untouched by the amendments (test_totals_match_the_fixture's own numbers):
        # an operator-approved contract edit is not U1's kind of rework.
        self.assertAlmostEqual(doc["interventions_per_card"], 2 / 3)
        self.assertEqual(card0["human"], 1)

        md = report.render_md(doc)
        self.assertIn("contract_amendments=2", md)
        self.assertIn("second edit", md)

    def test_a_card_with_only_an_amendment_is_still_listed(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        extra = db.new_task(conn, title="amend only", now=10)
        db.event(conn, extra, "contract_amended",
                  {"issuer": "owner", "reason": "scope narrowed", "old_file_set": [],
                   "new_file_set": []}, now=20)
        conn.commit()
        conn.close()
        doc = report.interventions(home, board, since=15)
        self.assertIn(extra, [c["task_id"] for c in doc["cards"]])
        card = next(c for c in doc["cards"] if c["task_id"] == extra)
        self.assertEqual(card["contract_amendments"], 1)

    def test_no_amendments_reports_zero_and_no_reason(self):
        home, board, tids = setup_board()
        doc = report.interventions(home, board)
        for c in doc["cards"]:
            self.assertEqual(c["contract_amendments"], 0)
            self.assertIsNone(c["last_amend_reason"])
        self.assertEqual(doc["totals"]["contract_amendments"], 0)


class MigrationReset(unittest.TestCase):
    """D6 (row 440): the handover-forced reset is shown, but kept out of the vs-U1 rate."""

    def setUp(self):    # drive the CLI as a person's terminal, whoever runs the test
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_migration_reset_is_reported_apart_from_interventions_per_card(self):
        home, board, tids = setup_board()
        human.write(home, board, tids[1], "reset", "pilot: task 7 retried", now=19, tag="migration-reset")
        doc = report.interventions(home, board)
        self.assertAlmostEqual(doc["interventions_per_card"], 2 / 3)
        self.assertAlmostEqual(doc["vs_u1"]["interventions_per_card"], 2 / 3 - 2.4)
        self.assertEqual(doc["totals"]["migration_resets"], 1)
        card1 = next(c for c in doc["cards"] if c["task_id"] == tids[1])
        self.assertEqual((card1["human"], card1["migration_resets"]), (0, 1))
        md = report.render_md(doc)
        self.assertIn("(+1 migration-reset)", md)
        self.assertIn("migration_resets=1 (not in the vs-U1 rate)", md)

    def test_a_card_with_only_a_migration_reset_is_still_listed(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        extra = db.new_task(conn, title="blocked on legacy", now=10)
        conn.commit()
        conn.close()
        human.write(home, board, extra, "reset", "r", now=19, tag="migration-reset")
        doc = report.interventions(home, board)
        self.assertIn(extra, [c["task_id"] for c in doc["cards"]])

    def test_an_unknown_tag_is_refused(self):
        with self.assertRaises(ValueError):
            human.write(Path(tempfile.mkdtemp()), "b", "t", "reset", "x", tag="whatever")

    def test_the_cli_writes_the_tag(self):
        from capsule.__main__ import main
        home = Path(tempfile.mkdtemp())
        import contextlib, io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(["human", "my-app", "t_1", "reset", "why", "--tag",
                                   "migration-reset", "--capsule-home", str(home)]), 0)
        doc = json.loads(Path(out.getvalue().strip()).read_text())
        self.assertEqual((doc["kind"], doc["tag"]), ("reset", "migration-reset"))


class MigratedBoard(unittest.TestCase):
    """I6 (final review): the report must isolate Capsule's own work on a board that
    came from a legacy handover - `--since` filters runs/interventions by their own
    timestamps rather than dropping whole cards by `created_at`, and without
    `--since` the report defaults to the handover record's own timestamp."""

    def setUp(self):
        self.capsule_home = Path(tempfile.mkdtemp())
        self.board = "screens"
        board_db = paths.board_db(self.board, self.capsule_home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        # A legacy-era card, with a legacy-era run recorded long before the handover.
        self.tid = "t_0000aaaa"
        conn.execute(
            "INSERT INTO tasks (id, title, status, created_at) VALUES (?, 'card', 'in_progress', 1)",
            (self.tid,))
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES"
            " (?, 'implementer', 'done', 'crashed', 1, 5)", (self.tid,))
        conn.commit()
        conn.close()
        # A legacy take record at t=1000 (the retired `handover take` wrote these): the moment
        # the board became Capsule's. Only the report still reads the file.
        (self.capsule_home / "handovers.jsonl").write_text(json.dumps(
            {"board": self.board, "from": f"{_LN.NAME}", "at": 1000}) + "\n")
        # A Capsule-era run, started well after the handover.
        conn = db.connect(board_db)
        conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, outcome, started_at, ended_at) VALUES"
            " (?, 'implementer', 'done', 'completed', 2000, 2010)", (self.tid,))
        conn.commit()
        conn.close()

    def test_default_since_reads_the_handover_record(self):
        doc = report.interventions(self.capsule_home, self.board)
        self.assertEqual(doc["totals"]["runs"], 1)
        self.assertEqual(doc["totals"]["cards"], 1)
        self.assertEqual(doc["cards"][0]["outcomes"], ["completed"])
        self.assertEqual(doc["since"], 1000)

    def test_explicit_since_still_filters_by_run_timestamp_not_card_created_at(self):
        doc = report.interventions(self.capsule_home, self.board, since=1500)
        self.assertEqual(doc["totals"]["runs"], 1)
        self.assertEqual(doc["totals"]["cards"], 1)

        doc = report.interventions(self.capsule_home, self.board, since=3000)
        self.assertEqual(doc["totals"]["runs"], 0)
        self.assertEqual(doc["totals"]["cards"], 0)

    def test_without_a_handover_record_since_defaults_to_zero(self):
        home = Path(tempfile.mkdtemp())
        board = "fresh"
        db.create_board(paths.board_db(board, home))
        doc = report.interventions(home, board)
        self.assertEqual(doc["since"], 0)


class Overrides(unittest.TestCase):
    """Row 454 item 4: `capsule report` lists active/expired overrides per card and totals."""

    def setUp(self):
        # C2: `override.grant` re-verifies `issuer` against a real identity file.
        self.identity_dir = Path(tempfile.mkdtemp())
        f = self.identity_dir / "fable.env"
        f.write_text("x\n")
        os.chmod(f, 0o600)
        self._old_identity_dir = os.environ.get("CAPSULE_IDENTITY_DIR")
        os.environ["CAPSULE_IDENTITY_DIR"] = str(self.identity_dir)

    def tearDown(self):
        if self._old_identity_dir is None:
            os.environ.pop("CAPSULE_IDENTITY_DIR", None)
        else:
            os.environ["CAPSULE_IDENTITY_DIR"] = self._old_identity_dir

    def test_active_expired_and_refused_show_per_card_and_in_totals(self):
        home, board, tids = setup_board()
        conn = db.connect(paths.board_db(board, home))
        cfg = {"operator": "owner", "contract_commands": ["lint", "unit-tests", "merge"]}
        # tids[1] is 'done' (setup_board merges it): a fix card must be open, so it is never used
        # as one here - only tids[0]/tids[2].
        override.grant(conn, cfg, board, tids[0], check="lint", reason="flaky rule", ttl="1h",
                       fix_card=tids[2], issuer="fable", now=1000)
        override.grant(conn, cfg, board, tids[1], check="unit-tests", reason="known gap", ttl="1h",
                       fix_card=tids[0], issuer="fable", now=1000)
        override.grant(conn, cfg, board, tids[2], check="merge", reason="ship now", ttl="1h",
                       fix_card=tids[0], issuer="fable", now=1000)   # refused: not the operator
        conn.close()

        doc = report.interventions(home, board, now=1000 + 60)   # both grants still unexpired
        by_card = {c["task_id"]: c["overrides"] for c in doc["cards"]}
        self.assertEqual([o["state"] for o in by_card[tids[0]]], ["active"])
        self.assertEqual([o["state"] for o in by_card[tids[1]]], ["active"])
        self.assertEqual([o["state"] for o in by_card[tids[2]]], ["refused"])
        self.assertEqual(doc["totals"]["overrides"],
                         {"granted": 2, "active": 2, "expired": 0, "refused": 1, "applied": 0})

        doc = report.interventions(home, board, now=1000 + 3700)   # both grants now past their 1h TTL
        self.assertEqual(doc["totals"]["overrides"],
                         {"granted": 2, "active": 0, "expired": 2, "refused": 1, "applied": 0})


class IntentExamples(unittest.TestCase):
    """Z1 (review of t_2f7585e3 round 1, I2: CLAUDE.md's unconditional "Z1 intent examples" and
    common-rules outrank the dispatch's "if report.py has an intent file", Z3 precedence) - every
    line of `contracts/intent/report.jsonl` ("report" added to `intent.STANDALONE`) run through
    `report.interventions`, the entry point `capsule report` (`__main__.py`) calls, and judged by
    `intent.matches` against its `expect`. Same pattern as `test_autoamend.py`'s `IntentExamples`."""

    def test_every_example_matches_its_oracle(self):
        lines = [json.loads(x) for x in (paths.INTENT / "report.jsonl").read_text().splitlines() if x.strip()]
        self.assertGreaterEqual(len(lines), 2)
        for ex in lines:
            doc = ex["given"]["document"]
            if "event_kind" not in doc:  # a Q2b line: judged through `touches.evaluate`
                with mock.patch.dict(os.environ, {"CAPSULE_OWNER_NAME": "Alex"}):  # rows name him
                    got = q2b_example(self.addCleanup, doc)
                seen = {k: got[k] for k in ex["expect"]}
                self.assertTrue(intent.matches(ex["expect"], seen), ex["id"])
                continue
            home = Path(tempfile.mkdtemp())
            board = "intent"
            db.create_board(paths.board_db(board, home))
            conn = db.connect(paths.board_db(board, home))
            tid = db.new_task(conn, title="card", now=10)
            payload = {"reason": "x"}
            if doc.get("payload_kind"):
                payload["kind"] = doc["payload_kind"]
            db.event(conn, tid, doc["event_kind"], payload, now=15)
            conn.commit()
            conn.close()
            card = next(c for c in report.interventions(home, board)["cards"] if c["task_id"] == tid)
            got = {"failure_events": len(card["failure_events"]), "spec_stale_holds": len(card["spec_stale_holds"])}
            self.assertTrue(intent.matches(ex["expect"], got), (ex["id"], got))


if __name__ == "__main__":
    unittest.main()


class SinceParsing(unittest.TestCase):
    def test_since_takes_epoch_seconds_as_text_or_an_iso_date(self):
        self.assertEqual(report._since_ts("1500000000"), 1500000000)
        self.assertEqual(report._since_ts(None), 0)
        self.assertNotEqual(report._since_ts("2017-07-14"), 0)
