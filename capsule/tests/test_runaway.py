# mutation-check: targets-predate-range b957739c9 -- owner-word rename, no behaviour pinned ::
#   Check.test_a_safety_finding_skips_the_guide_pass_and_asks_owner,
#   RoundFour.test_a_round_four_fail_without_a_safety_flag_still_routes_to_the_contradiction_path,
#   Trip.test_a_failed_round_over_the_limit_stops_the_card_and_opens_one_incident,
#   Trip.test_ask_owner_leaves_the_card_waiting_as_a_needs_you_item,
#   Trip.test_continue_reruns_once_with_the_fix_list_and_a_second_trip_goes_to_the_owner,
#   Trip.test_the_guide_gets_every_finding_and_the_three_options
"""W200 stop-the-line: a card that keeps failing review is stopped, ruled on once, then released or
handed to the owner. Unit tests drive `runaway.check` on a bare board; the tick tests run the real
review loop with scripted roles."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from capsule import blocks, config, db, incidents, runaway
from capsule.tests import test_tick as tt
from capsule.tests.test_cost import BoardCase
from capsule.tests.test_tick import IMPL_DONE, REV_FAIL, TickCase
from capsule.tests.fixtures.board_ddl import board_ddl


def rev_doc(*classes, verdict="fail"):
    return json.dumps({"verdict": verdict, "findings": [
        {"file": "a.py", "line": 1, "class": c, "text": "x"} for c in classes]})


def decision(choice, context="1. fix X and its sibling in b.py"):
    return {"schema": "decision.v1", "question": "stopped card", "recommended": choice,
            "context": context, "options": [{"id": i, "label": i} for i in runaway.IDS]}


class Check(BoardCase):
    def setUp(self):
        super().setUp()
        self.cfg = {}
        self.tid = self.card(status="review")

    def runs(self, n, **kw):
        for _ in range(n):
            self.run_row(self.tid, **kw)

    def check(self):
        return runaway.check(self.conn, self.cfg, self.tid)

    def events(self, kind):
        return self.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                                 (self.tid, kind)).fetchall()

    def test_four_review_rounds_trip_and_three_do_not(self):
        for _ in range(3):
            self.complete_run()
        self.assertIsNone(self.check())
        self.complete_run()
        self.assertEqual(self.check()[0], "rounds")

    def complete_run(self, **kw):
        run = self.run_row(self.tid, **kw)
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (run["id"],))

    def rounds_at(self, n, start):
        for _ in range(n):
            run = self.run_row(self.tid, start=start)
            self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (run["id"],))

    def human_reset(self, now):
        blocks.reset_failures(self.conn, self.tid, by="human", now=now)

    def test_five_rounds_with_no_human_reset_trip(self):
        self.rounds_at(5, 10)
        self.assertEqual(self.check()[0], "owner_rounds")

    def test_one_round_after_a_human_reset_does_not_trip(self):
        self.rounds_at(5, 10)
        self.human_reset(100)
        self.rounds_at(1, 200)
        self.assertIsNone(self.check())

    def test_four_rounds_after_a_human_reset_trip(self):
        self.rounds_at(5, 10)
        self.human_reset(100)
        self.rounds_at(4, 200)
        self.assertEqual(self.check()[0], "rounds")

    def test_a_guide_reset_does_not_restart_the_count(self):
        self.rounds_at(5, 10)
        blocks.reset_failures(self.conn, self.tid, by="guide", now=100)
        self.rounds_at(1, 200)
        self.assertEqual(self.check()[0], "owner_rounds")

    def test_a_same_class_streak_before_a_human_reset_does_not_carry_over(self):
        for start in (10, 20):
            self.complete_run(start=start)
            self.run_row(self.tid, role="reviewer", start=start, handoff=rev_doc("c"))
        self.human_reset(100)
        self.complete_run(start=200)
        self.run_row(self.tid, role="reviewer", start=200, handoff=rev_doc("c"))
        self.assertIsNone(self.check())

    def failed_round(self, start):
        self.complete_run(start=start)
        self.run_row(self.tid, role="reviewer", start=start, handoff=rev_doc("c"))

    def amend(self, before, after, now):
        db.event(self.conn, self.tid, "contract_amended",
                 {"body_hash_before": before, "body_hash_after": after}, now=now)

    def test_a_same_class_streak_before_a_body_amendment_does_not_carry_over(self):
        self.failed_round(10)
        self.failed_round(20)
        self.amend("a", "b", 100)
        self.failed_round(200)
        self.assertIsNone(self.check())

    def test_three_same_class_rounds_after_a_body_amendment_trip(self):
        self.amend("a", "b", 5)
        for start in (10, 20, 30):
            self.failed_round(start)
        self.assertEqual(self.check()[0], "same_class")

    def test_an_amendment_with_equal_body_hashes_keeps_the_streak(self):
        self.failed_round(10)
        self.failed_round(20)
        self.amend("a", "a", 100)
        self.failed_round(200)
        self.assertEqual(self.check()[0], "same_class")

    def test_a_body_amendment_does_not_lift_the_round_limit(self):
        self.rounds_at(3, 10)
        self.amend("a", "b", 100)
        self.rounds_at(1, 200)
        self.assertEqual(self.check()[0], "rounds")

    def test_a_human_reset_later_than_the_amendment_stays_the_floor(self):
        self.amend("a", "b", 5)
        self.failed_round(10)
        self.failed_round(20)
        self.human_reset(100)
        self.failed_round(200)
        self.assertIsNone(self.check())

    def test_a_runaway_ruling_before_a_human_reset_is_not_a_second_trip(self):
        self.cfg = {"board": "b"}
        db.event(self.conn, self.tid, "guide_ruling", {"role": "runaway"}, now=50)
        self.human_reset(100)
        self.rounds_at(4, 200)
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        impl = self.run_row(self.tid, start=300)
        self.assertTrue(runaway.step(self.conn, self.cfg, task, impl, 300))
        ev = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='runaway'",
                               (self.tid,)).fetchone()
        self.assertTrue(json.loads(ev[0])["pass"])

    def trip(self, now=300):
        """Run `runaway.step` for the failed round; (runaway event payload, outbox bodies)."""
        self.cfg = {"board": "b", **self.cfg}
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        impl = self.run_row(self.tid, start=now)
        self.assertTrue(runaway.step(self.conn, self.cfg, task, impl, now))
        ev = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind="
                               "'runaway' ORDER BY id DESC", (self.tid,)).fetchone()
        bodies = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE thread_key=? ORDER BY id", (self.tid,))]
        return json.loads(ev[0]), bodies

    def test_the_fifth_round_after_a_guide_continue_goes_straight_to_the_owner(self):
        self.rounds_at(5, 10)
        db.event(self.conn, self.tid, "guide_ruling", {"role": "runaway"}, now=50)
        blocks.reset_failures(self.conn, self.tid, by="guide", now=60)
        ev, bodies = self.trip()
        self.assertEqual((ev["trigger"], ev["pass"]), ("owner_rounds", False))
        self.assertIsNone(runaway.pending_event(self.conn, self.tid))
        row = self.conn.execute("SELECT status, block_kind FROM tasks WHERE id=?",
                                (self.tid,)).fetchone()
        self.assertEqual(tuple(row), ("triage", "runaway"))
        self.assertTrue(any("Your call" in b for b in bodies))

    def test_the_fifth_round_goes_to_the_owner_even_with_no_guide_pass_before(self):
        self.rounds_at(5, 10)
        ev, _ = self.trip()
        self.assertEqual((ev["trigger"], ev["pass"]), ("owner_rounds", False))

    def test_the_fourth_round_still_lets_the_guide_rule_once(self):
        self.rounds_at(4, 10)
        ev, bodies = self.trip()
        self.assertEqual((ev["trigger"], ev["pass"]), ("rounds", True))
        self.assertFalse(any("Your call" in b for b in bodies))
        self.assertIsNotNone(runaway.pending_event(self.conn, self.tid))

    def test_a_human_reset_gives_five_more_rounds(self):
        self.rounds_at(5, 10)
        self.human_reset(100)
        self.rounds_at(3, 200)
        self.assertIsNone(self.check())
        self.rounds_at(1, 300)
        self.assertEqual(self.check()[0], "rounds")
        self.rounds_at(1, 400)
        self.assertEqual(self.check()[0], "owner_rounds")

    def test_a_token_trip_before_the_fifth_round_keeps_the_guide_once_path(self):
        self.landed(*[200000] * 5)
        self.rounds_at(2, 10)
        self.run_row(self.tid, tin=600000)
        ev, _ = self.trip()
        self.assertEqual((ev["trigger"], ev["pass"]), ("tokens", True))

    def test_the_owner_cap_is_five_by_default_and_the_ladder_lifts_it(self):
        self.assertEqual(runaway.settings({})["owner_rounds"], 5)
        cfg = {"redesign": {"escalation": True}, "routing": {"implementer_restart": {}}}
        self.assertEqual(runaway.settings(cfg)["owner_rounds"], runaway.restart.LADDER)
        self.assertEqual(runaway.settings({"runaway": {"owner_rounds": 8}})["owner_rounds"], 8)
        config._validate_runaway({"owner_rounds": 8})

    def test_with_the_ladder_on_the_fifth_failed_round_does_not_ask_the_owner(self):
        self.cfg = {"redesign": {"escalation": True}, "routing": {"implementer_restart": {}}}
        self.rounds_at(5, 10)
        self.assertIsNone(self.check())

    def test_with_the_ladder_on_the_sixth_failed_round_is_the_owner_backstop(self):
        self.cfg = {"redesign": {"escalation": True}, "routing": {"implementer_restart": {}}}
        self.rounds_at(6, 10)
        self.assertEqual(self.check()[0], "owner_rounds")   # the ladder's own stop fires first

    def test_the_owner_message_is_plain_words_with_the_three_choices(self):
        self.rounds_at(5, 10)
        _, bodies = self.trip()
        (body,) = bodies
        for part in ('Card "card" is stuck', "failed review 5 times", "restart it with the"
                     " reviewer's findings as the fix list", "drop it and I'll file a smaller"
                     " card", "narrow it to fewer files"):
            self.assertIn(part, body)
        for part in (self.tid, "owner_rounds", "runaway", "python3", "reply:"):
            self.assertNotIn(part, body)
        self.assertNotIn("safety", body)

    def test_the_owner_message_says_when_a_finding_is_a_safety_one(self):
        self.rounds_at(5, 10)
        doc = {"verdict": "fail", "findings": [{"file": "a.py", "line": 1, "class": "c",
                                                "text": "x", "safety": True}]}
        self.run_row(self.tid, role="reviewer", start=20, handoff=json.dumps(doc))
        _, (body,) = self.trip()
        self.assertIn("(includes a safety finding)", body)

    def test_runs_trip_at_three_times_the_floor_when_few_cards_landed(self):
        limit = runaway.DEFAULTS["runs_factor"] * runaway.DEFAULTS["floor_runs"]
        self.runs(limit - 1, role="reviewer")
        self.assertIsNone(self.check())
        self.runs(1, role="reviewer")
        self.assertEqual(self.check()[0], "runs")

    def test_runs_limit_follows_the_median_of_landed_cards_not_the_floor(self):
        for _ in range(5):
            self.runs_for(self.card(), 20)
        self.runs(59, role="reviewer")
        self.assertIsNone(self.check())
        self.runs(1, role="reviewer")
        self.assertEqual(self.check()[0], "runs")

    def runs_for(self, tid, n):
        for _ in range(n):
            self.run_row(tid)

    def test_tokens_trip_at_three_times_the_median(self):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=599999)
        self.assertIsNone(self.check())
        self.run_row(self.tid, tin=1)
        self.assertEqual(self.check()[0], "tokens")

    def spent_around_a_reset(self, after, by="human"):
        self.landed(*[200000] * 5)
        self.run_row(self.tid, tin=700000, start=10)
        blocks.reset_failures(self.conn, self.tid, by=by, now=100)
        self.run_row(self.tid, tin=after, start=200)

    def test_tokens_before_a_human_reset_do_not_count_toward_the_limit(self):
        self.spent_around_a_reset(1)
        self.assertIsNone(self.check())

    def test_tokens_after_a_human_reset_still_trip(self):
        self.spent_around_a_reset(600000)
        self.assertEqual(self.check()[0], "tokens")

    def test_a_guide_reset_does_not_restart_the_token_count(self):
        self.spent_around_a_reset(1, by="guide")
        self.assertEqual(self.check()[0], "tokens")

    def test_runs_before_a_human_reset_do_not_count_toward_the_limit(self):
        self.runs(24, start=10)
        self.human_reset(100)
        self.runs(1, start=200)
        self.assertIsNone(self.check())

    def review_round(self, *classes, verdict="fail"):
        """One review round: a completed implementer run, then its reviewer verdict."""
        self.complete_run()
        self.run_row(self.tid, role="reviewer", handoff=rev_doc(*classes, verdict=verdict))

    def test_the_same_finding_class_three_rounds_running_trips(self):
        for classes in (("slop", "least_code"), ("least_code",), ("least_code", "slop")):
            self.review_round(*classes)
        self.assertEqual(self.check(),
                         ("same_class", "finding class least_code failed 3 rounds running"))

    def test_a_class_that_changes_or_a_pass_in_between_does_not_trip(self):
        for classes in (("slop",), ("least_code",), ("slop",)):
            self.review_round(*classes)
        self.assertIsNone(self.check())
        self.cfg = {"runaway": {"max_rounds": 9, "owner_rounds": 9}}
        self.review_round("slop", verdict="pass")
        self.assertIsNone(self.check())

    def test_a_pass_in_the_last_round_breaks_a_shared_class(self):
        self.review_round("slop")
        self.review_round("slop")
        self.review_round("slop", verdict="pass")
        self.assertIsNone(self.check())

    def test_one_same_class_round_trips_when_the_limit_is_one(self):
        self.cfg = {"runaway": {"same_class_rounds": 1}}
        self.review_round("c")
        self.assertEqual(self.check(), ("same_class", "finding class c failed 1 rounds running"))

    def test_a_pre_accept_verdict_does_not_count_after_an_accept(self):
        self.review_round("c")
        self.review_round("c")
        impl = self.run_row(self.tid, role="implementer")
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (impl["id"],))
        self.run_row(self.tid, role="reviewer", handoff=rev_doc("c"))   # before the accept
        last = self.run_row(self.tid, role="reviewer")                  # after it, no handoff
        db.event(self.conn, self.tid, "request_review",
                 {"by": "accept", "after_run": last["id"] - 1}, run_pk=impl["id"])
        self.assertIsNone(self.check())

    def test_two_reviewer_runs_in_one_round_count_as_one_same_class_round(self):
        self.review_round("c")
        self.run_row(self.tid, role="reviewer", handoff=rev_doc("c"))   # a re-run, same round
        self.review_round("c")
        self.assertIsNone(self.check())
        self.review_round("c")
        self.assertEqual(self.check(), ("same_class", "finding class c failed 3 rounds running"))

    def test_a_ruling_of_reviewer_run_does_not_extend_a_streak(self):
        self.review_round("c")
        self.review_round("c")
        self.review_round("d")
        ruling = self.run_row(self.tid, role="reviewer", handoff=rev_doc("c"))
        self.conn.execute("UPDATE task_runs SET metadata=? WHERE id=?",
                          (json.dumps({"ruling_of": 1}), ruling["id"]))
        self.assertIsNone(self.check())

    def test_a_rate_limited_reviewer_run_does_not_count_as_a_verdict(self):
        self.review_round("c")
        self.review_round("c")
        self.review_round("d")
        limited = self.run_row(self.tid, role="reviewer", handoff=rev_doc("c"))
        self.conn.execute("UPDATE task_runs SET outcome='rate_limited' WHERE id=?",
                          (limited["id"],))
        self.assertIsNone(self.check())

    def test_zero_turns_the_same_class_trigger_off(self):
        for _ in range(3):
            self.review_round("slop")
        self.cfg = {"runaway": {"same_class_rounds": 0}}
        self.assertIsNone(self.check())

    def test_the_warn_level_records_one_event_and_never_stops(self):
        self.runs(runaway.DEFAULTS["runs_warn_factor"] * runaway.DEFAULTS["floor_runs"],
                  role="reviewer")
        self.assertIsNone(self.check())
        self.assertIsNone(self.check())
        self.assertEqual(len(self.events("runaway_warn")), 1)

    def test_config_limits_override_the_defaults_and_bad_ones_are_refused(self):
        self.cfg = {"runaway": {"max_rounds": 1}}
        run = self.run_row(self.tid)
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (run["id"],))
        self.assertEqual(self.check()[0], "rounds")
        for bad in ({"max_rounds": 0}, {"nope": 1}, {"max_rounds": True}, {"max_rounds": 2.5},
                    {"same_class_rounds": -1}, {"runs_warn_factor": 3}):
            with self.assertRaises(ValueError):
                config._validate_runaway(bad)
        config._validate_runaway({"same_class_rounds": 0})
        config._validate_runaway({"runs_factor": 2.5, "runs_warn_factor": 1.5})

    def test_a_safety_finding_skips_the_guide_pass_and_asks_owner(self):
        self.cfg = {"runaway": {"max_rounds": 1}}
        impl = self.run_row(self.tid)
        self.conn.execute("UPDATE task_runs SET outcome='completed' WHERE id=?", (impl["id"],))
        finding = {"file": "a.py", "line": 1, "class": "security", "text": "x", "safety": True}
        self.run_row(self.tid, role="reviewer",
                     handoff=json.dumps({"verdict": "fail", "findings": [finding]}))
        task = self.conn.execute("SELECT * FROM tasks WHERE id=?", (self.tid,)).fetchone()
        self.assertTrue(runaway.step(self.conn, {"board": "b", **self.cfg}, task, impl, 5))
        self.assertIsNone(runaway.pending_event(self.conn, self.tid))
        (body,) = self.conn.execute("SELECT body FROM outbox WHERE thread_key=?",
                                    (self.tid,)).fetchone()
        self.assertIn("a safety finding", body)

    def test_two_cards_tripping_the_same_day_are_two_incidents_one_card_is_one(self):
        def opened(task_id):
            row = {"kind": "runaway", "task_id": task_id}
            key = incidents._classify(row, {"trigger": "rounds"})[2]
            return incidents.open_incident(self.conn, board="b", kind="runaway", key=key,
                                           task_id=task_id, now=100)[0]
        other = self.card(status="review")
        self.assertNotEqual(opened(self.tid), opened(other))
        self.assertEqual(opened(self.tid), opened(self.tid))


class Migration(unittest.TestCase):
    def test_an_old_incidents_table_keeps_its_rows_and_then_accepts_runaway(self):
        from capsule.tests.test_db import OLD_INCIDENTS_DDL
        path = Path(tempfile.mkdtemp()) / "kanban.db"
        raw = sqlite3.connect(path)
        raw.executescript(board_ddl())
        for table, col, decl in db.ADDED_COLUMNS:
            raw.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        raw.executescript(OLD_INCIDENTS_DDL)
        raw.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint, status,"
                    " evidence) VALUES ('inc_00000001', 'b', 'crash', 1, 'f', 'open', '[]')")
        raw.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            raw.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                        " ('inc_00000002', 'b', 'runaway', 2, 'f2')")
        raw.close()
        conn = db.connect(path)
        self.addCleanup(conn.close)
        db.migrate(conn)
        conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint) VALUES"
                     " ('inc_00000002', 'b', 'runaway', 2, 'f2')")
        row = conn.execute("SELECT id, kind, opened_at, fingerprint, status FROM incidents"
                           " WHERE id='inc_00000001'").fetchone()
        self.assertEqual(tuple(row), ("inc_00000001", "crash", 1, "f", "open"))


class Trip(TickCase):
    """max_rounds 1: the first failed review round trips."""

    def setUp(self):
        super().setUp()
        self.cfg["runaway"] = {"max_rounds": 1}
        self.cfg["korin"] = dict(self.cfg["korin"], enabled=False)
        self.write_cfg()
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL], [REV_FAIL]]
        self.script["guide"] = [[{"schema": "alignment_verdict.v1", "verdict": "aligned",
                                  "rows": [417],
                                  "reason": "fits"}]]

    def run_ticks(self, n=8):
        for _ in range(n):
            self.tick.once()

    def events(self, kind):
        return [json.loads(r[0] or "{}") for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id",
            (self.tid, kind))]

    def ruling_runs(self):
        return [r for r in self.runs("guide")
                if json.loads(r["metadata"] or "{}").get("ruling_of")]

    def outbox(self):
        rows = self.conn.execute("SELECT body FROM outbox WHERE thread_key=? ORDER BY id",
                                 (self.tid,))
        return [r[0] for r in rows]

    def test_a_failed_round_over_the_limit_stops_the_card_and_opens_one_incident(self):
        self.script["guide"].append([decision("ask-owner", "only the owner can say")])
        self.run_ticks()
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "runaway"))
        (ev,) = self.events("runaway")
        self.assertEqual((ev["trigger"], ev["pass"]), ("rounds", True))
        self.assertEqual(len(self.runs("implementer")), 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM incidents")]
        self.assertEqual(kinds, ["runaway"])

    def test_the_guide_gets_every_finding_and_the_three_options(self):
        self.script["guide"].append([decision("ask-owner", "only the owner can say")])
        self.run_ticks()
        (run,) = self.ruling_runs()
        sp = self.spool_dir(run["run_id"])
        packet = json.loads((sp / "job.json").read_text())["inputs"]["ruling"]
        self.assertEqual(packet["kind"], "runaway")
        self.assertIn("dead branch", packet["findings"][0])
        prompt = json.loads((sp / "bundle.json").read_text())["prompt"]
        for part in ("Runaway card (stop-the-line)", "sibling instance", "continue", "split",
                     "ask-owner"):
            self.assertIn(part, prompt)

    def test_ask_owner_leaves_the_card_waiting_as_a_needs_you_item(self):
        self.script["guide"].append([decision("ask-owner", "only the owner can say")])
        self.run_ticks()
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "runaway"))
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertTrue(any("the guide says ask-owner" in line for line in self.outbox()))
        (r,) = self.events("guide_ruling")
        self.assertEqual((r["choice"], r["role"]), ("ask-owner", "runaway"))

    def test_a_human_reset_restarts_the_reviewers_round_count(self):
        sql = ("INSERT INTO task_runs (task_id, profile, status, started_at, outcome, run_id,"
               " metadata) VALUES (?,?,?,?,?,?,?)")
        for i in range(5):
            self.conn.execute(sql, (self.tid, "implementer", "done", 1, "completed", f"old{i}",
                                    '{"start_sha": "HEAD"}'))
        blocks.reset_failures(self.conn, self.tid, by="human", now=100)
        self.script["implementer"] = [[tt.IMPL_DONE]]
        self.script["reviewer"] = [[tt.REV_PASS]]
        self.script["validator"] = [[tt.VAL_PASS]]
        self.run_ticks(15)
        (run,) = self.runs("reviewer")
        job = json.loads((self.spool_dir(run["run_id"]) / "job.json").read_text())
        self.assertEqual(job["inputs"]["round"], 1)

    def test_continue_reruns_once_with_the_fix_list_and_a_second_trip_goes_to_the_owner(self):
        fix = "1. fix dead branch in app.py and its siblings"
        self.script["guide"].append([decision("continue", fix)])
        self.run_ticks(14)
        self.assertEqual(len(self.runs("implementer")), 2)
        second = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(second["run_id"]) / "job.json").read_text())
        self.assertIn(fix, job["inputs"]["guide_rulings"][0])
        first, second = self.events("runaway")
        self.assertEqual((first["pass"], second["pass"]), (True, False))
        self.assertEqual(len(self.ruling_runs()), 1)
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "runaway"))
        self.assertTrue(any("tripped again after the Opus pass" in line for line in self.outbox()))

    def test_a_guide_drift_hold_over_the_limit_is_not_swallowed_into_a_runaway(self):
        drift = dict(tt.GUIDE_OK, verdict="drifted", rows=[417], reason="row 417 differs")
        self.script["guide"] = [[drift]]
        self.run_ticks()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
        self.assertEqual(self.events("runaway"), [])

    def test_a_spec_stale_hold_over_the_limit_is_not_swallowed_into_a_runaway(self):
        self._add_spec = lambda: tt.SpecConflictRows._add_spec(self)
        tt.SpecConflictRows._cite(self)
        self.script["guide"] = [[dict(tt.GUIDE_OK, rows=[417], spec_conflict_rows=[417],
                                      reason="row 417 is what contradicts the spec section")]]
        self.run_ticks()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "spec_stale"))
        self.assertEqual(self.events("runaway"), [])

    def test_continue_without_a_fix_list_is_not_a_ruling(self):
        self.script["guide"].append([decision("continue", "")])
        self.run_ticks()
        self.assertEqual(self.events("guide_ruling"), [])
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "runaway"))
        self.assertEqual(len(self.runs("implementer")), 1)
        self.assertIn("fix list", " ".join(r["error"] or "" for r in self.ruling_runs()))


class RoundFour(TickCase):
    """Row 540: a round-4 reviewer fail with a new finding and no safety flag (the second one,
    after the first is refused once) is the intent contradiction, not a runaway, even with the
    default `max_rounds` 4."""

    def test_a_round_four_fail_without_a_safety_flag_still_routes_to_the_contradiction_path(self):
        n = 4
        self.cfg["runaway"] = {"same_class_rounds": 0}
        self.write_cfg()
        late = {"file": "app.py", "line": 20, "class": "slop", "text": "round 4 finding"}
        self.script["implementer"] = [[IMPL_DONE] for _ in range(n)]
        self.script["validator"] = [[tt.VAL_PASS] for _ in range(n)]
        self.script["guide"] = [[tt.GUIDE_OK] for _ in range(n)]
        self.script["reviewer"] = [[REV_FAIL]] * (n - 1) + [[dict(REV_FAIL, findings=[late])]] * 2
        for _ in range(20):
            self.tick.once()
            if self.task()["status"] == "triage":
                break
        blocks.reset_failures(self.conn, self.tid, by="owner")
        for _ in range(20):
            self.tick.once()
            if self.task()["block_kind"] == "guide_ruling":
                break
        self.assertEqual(self.task()["block_kind"], "guide_ruling")
        tripped = self.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind='runaway'",
                                    (self.tid,)).fetchall()
        self.assertEqual(tripped, [])
        sql = "SELECT 1 FROM task_events WHERE task_id=? AND kind='review_retry'"
        self.assertEqual(len(self.conn.execute(sql, (self.tid,)).fetchall()), 1)


if __name__ == "__main__":
    unittest.main()
