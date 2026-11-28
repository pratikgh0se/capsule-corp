"""`capsule incidents close-history` (row 638 b): old incidents close as history, with refusals.
Temp home, temp vault and temp board only; the real boards are never opened. Oracle 1: the
incidents table read back by a separate connection. Oracle 2: the file hash of the board db, the
outbox/task_events row counts and the outbox.log size. Every timestamp is relative to CUTOFF (never
to the clock), so the tests do not rot as the date moves."""
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, idle_lane, incident_history, outbox, replay, slack_msgs
from capsule.incident_history import CUTOFF
from capsule.tests.history_harness import DAY, NOTE, Harness

NOW = int(time.time())


class CloseHistory(Harness):
    # -- dry run, apply, evidence ---------------------------------------------------------

    def test_dry_run_writes_nothing_and_prints_the_list_hash(self):
        a, b = self.inc("one"), self.inc("two")
        self.conn.close()
        before = hashlib.sha256(self.db_path.read_bytes()).hexdigest()
        self.conn = db.connect(self.db_path)
        entries = [self.entry(a), self.entry(b, "false_alarm")]
        res = self.run_cli(entries)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.count("would close:"), 2)
        self.assertIn("dry run", res.stderr)
        listing = Path(res.args[res.args.index("--list") + 1])
        self.assertIn("list sha256: " + hashlib.sha256(listing.read_bytes()).hexdigest(),
                      res.stderr)
        self.conn.close()
        self.assertEqual(hashlib.sha256(self.db_path.read_bytes()).hexdigest(), before)
        self.assertFalse(self.db_path.with_name("kanban.db-wal").exists())
        self.conn = db.connect(self.db_path)
        self.assertEqual([self.row(a)["status"], self.row(b)["status"]], ["open", "open"])

    def test_apply_closes_as_history_records_who_what_and_list_without_outbox(self):
        a = self.inc("one", task=self.card)
        b = self.inc("two")
        c = self.inc("three")
        self.conn.execute("UPDATE incidents SET status='fix_pending' WHERE id=?", (c,))
        outbox_before, log_before = self.count("outbox"), self.log_size()
        entries = [self.entry(a, "false_alarm", "detector misread"), self.entry(b),
                   self.entry(c, "resolved", "t_y landed")]
        res = self.run_cli(entries, "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("closed: " + a, res.stdout)
        self.assertIn("3 closed", res.stderr)
        listing = Path(res.args[res.args.index("--list") + 1])
        sha = hashlib.sha256(listing.read_bytes()).hexdigest()
        for inc_id, verdict in ((a, "false_alarm"), (b, "resolved"), (c, "resolved")):
            rec = self.row(inc_id)
            self.assertEqual((rec["status"], rec["closure"]), ("closed", "history"))
            self.assertGreaterEqual(rec["closed_at"], NOW)
            stamped = json.loads(rec["evidence"])[-1]
            self.assertRegex(stamped, rf"^history by cos-history at \d+: row 638: {verdict}: ")
            self.assertTrue(stamped.endswith(f"(list {sha[:12]})"), stamped)
            self.assertNotIn("history, history", stamped)
        self.assertEqual((self.count("outbox"), self.log_size()), (outbox_before, log_before))
        ev = self.conn.execute(
            "SELECT task_id, payload FROM task_events WHERE kind='incident_closed'").fetchall()
        self.assertEqual([e["task_id"] for e in ev], [self.card])
        payload = json.loads(ev[0]["payload"])
        self.assertEqual((payload["by"], payload["list_sha"]), ("cos-history", sha))
        self.assertRegex(payload["reason"], r"^history by cos-history at \d+: row 638: false_alarm"
                                            r": detector misread \(list " + sha[:12] + r"\)$")

    def test_apply_leaves_refused_rows_and_closes_the_rest(self):
        good, repeat_kind = self.inc("one"), self.inc("two", kind="escaped_bug")
        res = self.run_cli([self.entry(good), self.entry(repeat_kind)], "--apply")
        self.assertEqual(res.returncode, 1)
        self.assertEqual(self.row(good)["status"], "closed")
        self.assertEqual(self.row(repeat_kind)["status"], "open")

    def test_second_apply_is_idempotent(self):
        a = self.inc()
        self.run_cli([self.entry(a)], "--apply")
        events = self.count("task_events")
        closed_at = self.row(a)["closed_at"]
        res = self.run_cli([self.entry(a)], "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("skipped: " + a + ": already closed", res.stdout)
        self.assertEqual((self.count("task_events"), self.row(a)["closed_at"]), (events, closed_at))

    # -- per-id refusals ------------------------------------------------------------------

    def test_refuses_unknown_id(self):
        self.assert_refused([self.entry("inc_00000000")], "not found")

    def test_refuses_an_id_that_is_not_a_string(self):
        a = self.inc()
        for bad in (5, None, ["inc_x"], {"a": 1}, True):
            res = self.run_cli([{"id": bad, "verdict": "resolved", "evidence": "e"},
                                self.entry(a)])
            self.assertEqual(res.returncode, 1, res.stderr)
            self.assertIn("refused: id is not a string", res.stdout)
            self.assertNotIn("Traceback", res.stderr)
            self.assertIn("would close: " + a, res.stdout)

    def test_refuses_status_not_open(self):
        a = self.inc()
        self.conn.execute("UPDATE incidents SET status='postmortem' WHERE id=?", (a,))
        self.assert_refused([self.entry(a)], "status is postmortem")
        a2 = self.inc("other")
        self.conn.execute("UPDATE incidents SET status='superseded' WHERE id=?", (a2,))
        self.assert_refused([self.entry(a2)], "status is superseded")

    def test_refuses_repeat_and_escaped_bug_kinds(self):
        first = self.inc("again")
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (CUTOFF - DAY, first))
        rep = self.inc("again", opened=CUTOFF - 3600)
        self.assertEqual(self.row(rep)["kind"], "repeat")
        self.assert_refused([self.entry(rep)], "kind repeat is never closed as history")
        bug = self.inc("bug", kind="escaped_bug")
        self.assert_refused([self.entry(bug)], "kind escaped_bug is never closed as history")

    def test_refuses_bad_verdict_and_bad_evidence(self):
        a = self.inc()
        self.assert_refused([self.entry(a, "fixed")], "verdict 'fixed'")
        self.assert_refused([self.entry(a, evidence="")], "evidence is empty")
        self.assert_refused([self.entry(a, evidence="   ")], "evidence is empty")
        self.assert_refused([{"id": a, "verdict": "resolved"}], "evidence is empty")
        self.assert_refused([self.entry(a, evidence="x" * 301)], "over 300")
        res = self.run_cli([self.entry(a, evidence="x" * 300)])
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_refuses_bad_lines_and_duplicates(self):
        a = self.inc()
        self.assert_refused(["not json", self.entry(a)], "line 1 is not a JSON object")
        self.assert_refused([self.entry(a), self.entry(a)], f"{a}: listed twice")

    # -- the cutoff: only incidents the analysis saw ---------------------------------------

    def test_refuses_an_incident_opened_at_or_after_the_cutoff(self):
        late = self.inc("late", opened=CUTOFF + 3600)
        edge = self.inc("edge", opened=CUTOFF)
        before = self.inc("before", opened=CUTOFF - 1)
        res = self.run_cli([self.entry(late), self.entry(edge), self.entry(before)])
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(f"refused: {late}: opened after the row-638 analysis", res.stdout)
        self.assertIn(f"refused: {edge}: opened after the row-638 analysis", res.stdout)
        self.assertIn(f"would close: {before}", res.stdout)

    def test_refuses_when_the_fingerprint_recurred_since_the_cutoff(self):
        for status in ("open", "closed", "superseded", "fix_pending"):
            key = f"again-{status}"
            old = self.inc(key, age=60)
            twin = self.inc(key, opened=CUTOFF + 3600)
            self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, twin))
            self.assert_refused([self.entry(old)], f"recurred since the analysis: {twin}")
            self.assertEqual(self.row(old)["status"], "open")

    def test_a_twin_before_the_cutoff_is_not_a_recurrence_since(self):
        old, twin = self.inc("pair", age=90), self.inc("pair", age=70)
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (CUTOFF - 60 * DAY, twin))
        self.assertEqual(self.run_cli([self.entry(old)]).returncode, 0)

    # -- Korin-worked records go through `incidents close` ---------------------------------

    def test_refuses_an_incident_korin_filled(self):
        closure = self.inc("one")
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (closure,))
        fixcard = self.inc("two")
        self.conn.execute("UPDATE incidents SET fix_card='t_fix00000', status='fix_pending'"
                          " WHERE id=?", (fixcard,))
        res = self.run_cli([self.entry(closure), self.entry(fixcard)])
        self.assertEqual(res.stdout.count("Korin filled the record: use incidents close"), 2)
        self.assertEqual(res.returncode, 1)

    # -- card state, every verdict ---------------------------------------------------------

    def test_refuses_while_the_card_is_active_for_every_verdict(self):
        every = set(replay._STATUSES) | set(db.TRANSITIONS) | set(idle_lane.LIVE_STATUSES)
        self.assertLessEqual({"done", "archived", "ready", "blocked", "claimed"}, every)
        for status in sorted(every - {"done", "archived"}):
            card = self.make_card(status)
            crash = self.inc(f"c-{status}", task=card)
            hi = self.inc(f"hi-{status}", kind="human_intervention", task=card)
            for verdict, target in (("resolved", crash), ("false_alarm", crash),
                                    ("resolved", hi), ("false_alarm", hi), ("card_done", hi)):
                self.assert_refused([self.entry(target, verdict)],
                                    f"card still active: the issue is open (card {card}, status"
                                    f" {status})")
            self.assertEqual((self.row(crash)["status"], self.row(hi)["status"]), ("open", "open"))

    def test_card_done_is_only_for_a_human_intervention(self):
        crash = self.inc("c", task=self.card)
        self.assert_refused([self.entry(crash, "card_done")], "only for human_intervention")

    def test_human_intervention_needs_its_card_on_this_board(self):
        ghost = self.inc("ghost", kind="human_intervention", task="t_00000000")
        self.assert_refused([self.entry(ghost, "card_done")], "status missing")
        self.assert_refused([self.entry(ghost, "resolved")], "status missing")
        bare = self.inc("bare", kind="human_intervention")
        self.assert_refused([self.entry(bare, "card_done")], "no card:")

    def test_finished_cards_let_every_verdict_close(self):
        for status in ("done", "archived"):
            card = self.make_card(status)
            crash = self.inc(f"c-{status}", task=card)
            hi = self.inc(f"hi-{status}", kind="human_intervention", task=card)
            res = self.run_cli([self.entry(crash, "resolved"),
                                self.entry(hi, "card_done", "card finished")], "--apply")
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertEqual([self.row(crash)["status"], self.row(hi)["status"]],
                             ["closed", "closed"])

    # -- live repeat: the 30-day window, both sides ----------------------------------------

    def test_live_repeat_window_blocks_inside_30_days_and_not_outside(self):
        cases = ((29, "open", True), (31, "open", False), (29, "fix_pending", True),
                 (29, "postmortem", True), (29, "closed", False), (29, "superseded", False))
        for n, (days, status, blocked) in enumerate(cases):
            key = "window-" + "abcdef"[n]      # digits are normalised away in a fingerprint
            old = self.inc(key, age=90)
            twin = self.inc(key, age=days)
            self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, twin))
            [(outcome, text)] = self.run_direct([self.entry(old)], now=CUTOFF)
            self.assertEqual(outcome, "refused" if blocked else "would close", (days, status))
            if blocked:
                self.assertIn(f"live repeat: {twin}", text)

    def test_refuses_a_live_repeat_at_a_fixed_now(self):
        # in process with `now` passed in: the CLI reads the real clock, which would rot this
        old, recent = self.inc("same", age=60), self.inc("same", age=3)
        self.assertEqual(self.row(recent)["fingerprint"], self.row(old)["fingerprint"])
        [(outcome, text)] = self.run_direct([self.entry(old)], apply=True, now=CUTOFF)
        self.assertEqual((outcome, text.split(" has ")[0]), ("refused",
                                                             f"{old}: live repeat: {recent}"))
        self.assertEqual(self.row(old)["status"], "open")

    # -- transaction -----------------------------------------------------------------------

    def test_a_status_change_between_plan_and_close_writes_nothing(self):
        a = self.inc("one", task=self.card)
        real_plan = incident_history._plan

        def plan_then_change(conn, entries, now):
            plan = real_plan(conn, entries, now)
            conn.execute("UPDATE incidents SET status='postmortem' WHERE id=?", (a,))
            return plan
        with mock.patch.object(incident_history, "_plan", plan_then_change):
            with self.assertRaisesRegex(incident_history.HistoryRefused,
                                        f"{a}: changed during run"):
                self.run_direct([self.entry(a)], apply=True)
        rec = self.row(a)    # the raise rolled the whole run back, the status poke included
        self.assertEqual((rec["status"], rec["closed_at"], rec["closure"]),
                         ("open", None, None))
        self.assertEqual(json.loads(rec["evidence"]), [])
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE kind='incident_closed'").fetchone()[0], 0)

    def test_a_failure_mid_run_rolls_the_whole_run_back(self):
        a, b = self.inc("one"), self.inc("two")
        real = incident_history._close_one
        calls = []

        def second_fails(conn, inc, entry, now, sha):
            calls.append(inc["id"])
            if len(calls) == 2:
                raise RuntimeError("boom")
            return real(conn, inc, entry, now, sha)
        with mock.patch.object(incident_history, "_close_one", second_fails):
            with self.assertRaises(RuntimeError):
                self.run_direct([self.entry(a), self.entry(b)], apply=True)
        self.assertEqual([self.row(a)["status"], self.row(b)["status"]], ["open", "open"])
        self.assertEqual(json.loads(self.row(a)["evidence"]), [])

    # -- the row-638 gate, dry run included ------------------------------------------------

    def test_whole_run_refused_without_the_row_638(self):
        a = self.inc()
        for flags in ((), ("--apply",)):
            self.write_note("| 637 | 2026-10-29 | something else |\n| 6380 | not it |\n")
            res = self.run_cli([self.entry(a)], *flags)
            self.assert_run_refused(res, "no '| 638 |' row")
            self.assertEqual(res.stdout, "")
            self.assertEqual(self.row(a)["status"], "open")
        (self.vault / NOTE).unlink()
        res = self.run_cli([self.entry(a)], "--apply")
        self.assertIn("decisions note not found", res.stderr)
        res = self.run_cli([self.entry(a)])
        self.assertIn("decisions note not found", res.stderr)
        self.assertEqual(self.row(a)["status"], "open")

    def test_the_638_row_must_start_its_line(self):
        a = self.inc()
        self.write_note("see | 638 | elsewhere\n  text | 638 |\n")
        self.assert_run_refused(self.run_cli([self.entry(a)]), "no '| 638 |' row")
        self.write_note("intro\n|638|2026-10-30|row at line start, no spaces\n")
        self.assertEqual(self.run_cli([self.entry(a)]).returncode, 0)

    # -- the list hash ---------------------------------------------------------------------

    def test_apply_needs_the_hash_of_this_very_list(self):
        a, b = self.inc("one"), self.inc("two")
        entries = [self.entry(a), self.entry(b)]
        self.assert_run_refused(self.run_cli(entries, "--apply", sha=None), "--list-sha")
        self.assert_run_refused(self.run_cli(entries, "--apply", sha="0" * 64), "--list-sha")
        self.assert_run_refused(self.run_cli(entries, "--apply", sha="abc"), "--list-sha")
        listing = self.write_list(entries)
        dry = hashlib.sha256(listing.read_bytes()).hexdigest()
        self.assert_run_refused(self.run_cli([self.entry(a)], "--apply", sha=dry), "--list-sha")
        self.assertEqual([self.row(a)["status"], self.row(b)["status"]], ["open", "open"])
        ok = self.run_cli(entries, "--apply")
        self.assertEqual(ok.returncode, 0, ok.stderr)

    # -- no Slack traffic: a queued broken notice is the fresh start's -----------------------

    def test_apply_refuses_while_a_broken_notice_is_queued_for_a_listed_fingerprint(self):
        a, b = self.inc("one"), self.inc("two")
        fp = self.row(a)["fingerprint"]
        row_id = outbox.add(self.conn, "slack", "REPEAT", dedup_key=slack_msgs.broken_key(fp),
                            now=CUTOFF)
        outbox.add(self.conn, "slack", "other", dedup_key=slack_msgs.broken_key("zzz"), now=CUTOFF)
        outbox_before = self.count("outbox")
        res = self.run_cli([self.entry(a), self.entry(b)], "--apply")
        self.assert_run_refused(res, f"pending broken notice for {fp}: run the fresh start first")
        self.assertEqual([self.row(a)["status"], self.row(b)["status"]], ["open", "open"])
        self.assertEqual(self.run_cli([self.entry(a)]).returncode, 0)     # a dry run is not held
        self.conn.execute("UPDATE outbox SET status='delivered' WHERE id=?", (row_id,))
        res = self.run_cli([self.entry(a), self.entry(b)], "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.count("outbox"), outbox_before)

    def test_a_notice_for_an_unlisted_fingerprint_does_not_hold_the_run(self):
        a, b = self.inc("one"), self.inc("two")
        outbox.add(self.conn, "slack", "REPEAT",
                   dedup_key=slack_msgs.broken_key(self.row(b)["fingerprint"]), now=CUTOFF)
        self.assertEqual(self.run_cli([self.entry(a)], "--apply").returncode, 0)


if __name__ == "__main__":
    unittest.main()
