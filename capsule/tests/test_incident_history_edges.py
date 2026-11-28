"""close-history edge cases (row 638 b, round 3): one-line evidence, the dry-run notice warning,
the cutoff constant and its boundaries, the live-repeat window relative to `now`, the list hash
on raw bytes, and the plan running inside the transaction. Shares the temp-board Harness; every
time is relative to CUTOFF."""
import datetime
import hashlib
import unittest
from unittest import mock

from capsule import incident_history, outbox, slack_msgs
from capsule.incident_history import CUTOFF
from capsule.tests.history_harness import DAY, Harness


class Evidence(Harness):
    def test_evidence_must_be_one_line(self):
        a = self.inc()
        for bad in ("two\nlines", "carriage\rreturn", "trailing newline\n", "\r\nboth"):
            self.assert_refused([self.entry(a, evidence=bad)], "evidence is not one line")
        self.assertEqual(self.run_cli([self.entry(a, evidence="one line, with: punctuation")])
                         .returncode, 0)

    def test_a_human_intervention_with_no_card_says_no_card(self):
        bare = self.inc("bare", kind="human_intervention")
        res = self.run_cli([self.entry(bare, "card_done")])
        self.assertIn(f"refused: {bare}: no card", res.stdout)
        self.assertNotIn("card None", res.stdout)


class Notices(Harness):
    def test_a_dry_run_warns_about_a_pending_notice_apply_would_refuse(self):
        a = self.inc("one")
        fp = self.row(a)["fingerprint"]
        outbox.add(self.conn, "slack", "REPEAT", dedup_key=slack_msgs.broken_key(fp), now=CUTOFF)
        res = self.run_cli([self.entry(a)])
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn(f"warning: pending broken notice for {fp}: run the fresh start first",
                      res.stdout)
        self.assertIn(f"would close: {a}", res.stdout)
        self.assertEqual(self.row(a)["status"], "open")

    def test_only_a_pending_row_holds_the_run(self):
        a = self.inc("one")
        fp = self.row(a)["fingerprint"]
        row_id = outbox.add(self.conn, "slack", "REPEAT", dedup_key=slack_msgs.broken_key(fp),
                            now=CUTOFF)
        for status in ("dead", "delivered"):
            self.conn.execute("UPDATE outbox SET status=? WHERE id=?", (status, row_id))
            res = self.run_cli([self.entry(a)])
            self.assertNotIn("warning:", res.stdout, status)
        res = self.run_cli([self.entry(a)], "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.row(a)["status"], "closed")


class Cutoff(Harness):
    def test_the_cutoff_is_the_bangkok_analysis_snapshot(self):
        self.assertEqual(CUTOFF, 1793329200)
        bangkok = datetime.timezone(datetime.timedelta(hours=7))
        self.assertEqual(CUTOFF, int(datetime.datetime(2026, 10, 30, 10, 0, tzinfo=bangkok)
                                     .timestamp()))

    def test_a_twin_opened_exactly_at_the_cutoff_is_a_recurrence_since(self):
        old = self.inc("edge", age=60)
        twin = self.inc("edge", opened=CUTOFF)
        self.assert_refused([self.entry(old)], f"recurred since the analysis: {twin}")

    def test_a_twin_one_second_before_the_cutoff_is_not(self):
        old = self.inc("edge", age=90)
        self.inc("edge", opened=CUTOFF - 1)
        [(outcome, _)] = self.run_direct([self.entry(old)], now=CUTOFF + 40 * DAY)
        self.assertEqual(outcome, "would close")

    def test_the_live_repeat_window_is_relative_to_now_not_to_the_cutoff(self):
        # a pre-cutoff open twin can only be live while now < CUTOFF + 30 days
        cases = ((40, 35, False), (10, 19, True), (10, 21, False), (0, 29, True))
        for n, (now_days, twin_age, blocked) in enumerate(cases):
            key = "relative-" + "abcd"[n]
            old = self.inc(key, age=90)
            twin = self.inc(key, age=twin_age)
            [(outcome, text)] = self.run_direct([self.entry(old)], now=CUTOFF + now_days * DAY)
            self.assertEqual(outcome, "refused" if blocked else "would close", cases[n])
            self.assertEqual(f"live repeat: {twin}" in text, blocked, cases[n])


class ListedTwins(Harness):
    """Same-fingerprint twins: a listed twin that closes is no live repeat (row 638 b)."""

    def outcomes(self, ids, now=CUTOFF):
        return [o for o, _ in self.run_direct([self.entry(i) for i in ids], now=now)]

    def test_two_listed_twins_both_close(self):
        a, b = self.inc("twin", age=28), self.inc("twin", age=24)
        self.assertEqual(self.outcomes([a, b]), ["would close", "would close"])
        res = self.run_cli([self.entry(a), self.entry(b)], "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual([self.row(a)["status"], self.row(b)["status"]], ["closed", "closed"])

    def test_an_unlisted_open_twin_still_blocks_both_listed_ones(self):
        a, b, c = (self.inc("trio", age=n) for n in (28, 24, 20))
        out = self.run_direct([self.entry(a), self.entry(b)])
        self.assertEqual([o for o, _ in out], ["refused", "refused"])
        self.assertIn(f"live repeat: {c}", out[0][1])
        self.assertIn("live repeat: ", out[1][1])      # a or c: both are live to b

    def test_a_listed_twin_refused_for_another_reason_blocks_its_sibling(self):
        a, b = self.inc("pair", age=28), self.inc("pair", age=24)
        self.conn.execute("UPDATE incidents SET closure='fixed' WHERE id=?", (b,))
        out = self.run_direct([self.entry(a), self.entry(b)])
        self.assertEqual([o for o, _ in out], ["refused", "refused"])
        self.assertIn(f"{a}: live repeat: {b}", out[0][1])
        self.assertIn("Korin filled the record", out[1][1])

    def test_one_twin_with_an_active_card_blocks_the_rest_in_any_order(self):
        busy = self.make_card("in_progress")
        a, b = self.inc("chain", age=28), self.inc("chain", age=24)
        c = self.inc("chain", age=20, task=busy)
        for order in ((a, b, c), (c, b, a)):
            out = self.run_direct([self.entry(i) for i in order])
            self.assertEqual([o for o, _ in out], ["refused"] * 3, order)
        self.assertIn("card still active", self.run_direct([self.entry(c)])[0][1])

    def test_a_second_apply_of_a_twin_list_changes_nothing(self):
        a, b = self.inc("again", age=28), self.inc("again", age=24)
        entries = [self.entry(a), self.entry(b)]
        self.run_cli(entries, "--apply")
        events, closed = self.count("task_events"), [self.row(a)["closed_at"],
                                                      self.row(b)["closed_at"]]
        res = self.run_cli(entries, "--apply")
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(res.stdout.count("skipped:"), 2)
        self.assertEqual((self.count("task_events"), [self.row(a)["closed_at"],
                                                      self.row(b)["closed_at"]]), (events, closed))


class ListHash(Harness):
    def test_only_the_whole_hash_of_the_raw_bytes_is_accepted(self):
        a = self.inc("one")
        entries = [self.entry(a)]
        real = hashlib.sha256(self.write_list(entries).read_bytes()).hexdigest()
        self.assert_run_refused(self.run_cli(entries, "--apply", sha=real[:12]), "--list-sha")
        self.assert_run_refused(self.run_cli(entries, "--apply", sha=real[:-1]), "--list-sha")
        # a trailing newline after the dry run is a different list
        self.assert_run_refused(self.run_cli(entries + [""], "--apply", sha=real), "--list-sha")
        self.assertEqual(self.row(a)["status"], "open")
        # the hash is hex: upper case is the same value and is accepted
        res = self.run_cli(entries, "--apply", sha=real.upper())
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertEqual(self.row(a)["status"], "closed")


class PlanInsideTheTransaction(Harness):
    def test_apply_plans_inside_the_transaction_and_a_dry_run_does_not(self):
        a = self.inc("one")
        real_plan, seen = incident_history._plan, []

        def probe(conn, entries, now):
            seen.append(conn.in_transaction)
            return real_plan(conn, entries, now)
        with mock.patch.object(incident_history, "_plan", probe):
            self.run_direct([self.entry(a)])
            self.run_direct([self.entry(a)], apply=True)
        self.assertEqual(seen, [False, True])
        self.assertEqual(self.row(a)["status"], "closed")


if __name__ == "__main__":
    unittest.main()
