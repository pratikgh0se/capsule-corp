"""Replay of the `incident_history` intent rows (row 638 b) against `capsule incidents
close-history`, in process (time fixed at CUTOFF, so nothing depends on the clock). Each row is a
world built here and a set of observed values; EVERY `expect` key of every row is asserted, and a
key this file cannot observe fails the test. Oracle 1: the exit code and printed lines. Oracle 2:
the incidents table and the connection's change counter read back. The rows file is shipped by the
CoS (contracts/intent/incident_history.jsonl): until it exists the shipped-rows test passes
with nothing to replay (CASES still run); once it exists it FAILS when it holds no rows, an
id outside CASES or a different `expect` than CASES."""
import argparse
import contextlib
import hashlib
import io
import json
import re
import unittest
from unittest import mock

from capsule import incidents_cli, intent
from capsule.incident_history import CUTOFF
from capsule.tests.history_harness import DAY, Harness

OBSERVABLE = ("exit", "closed", "db_changed", "output", "refusal", "refusal_names_row",
              "status_after", "closure", "closed_by", "outbox_rows_added",
              "evidence_has_verdict", "evidence_has_list_hash", "event_has_list_sha",
              "outbox_log_unchanged", "incident_closed_events")
WRONG_SHA = "0" * 64

CASES = {
    "history-close-needs-the-638-row": {
        "exit": 1, "closed": 0, "db_changed": False, "refusal_names_row": 638},
    "history-close-apply-needs-the-638-row": {
        "exit": 1, "closed": 0, "db_changed": False, "refusal_names_row": 638},
    "history-close-dry-run-writes-nothing": {
        "exit": 0, "db_changed": False, "output": "would close"},
    "history-close-never-closes-a-repeat-or-escaped-bug": {
        "exit": 1, "closed": 0, "status_after": "open"},
    "history-close-leaves-a-live-repeat-open": {
        "exit": 1, "closed": 0, "refusal": "live repeat"},
    "history-close-sends-no-notice": {
        "closed": 3, "closure": "history", "closed_by": "cos-history", "outbox_rows_added": 0,
        "outbox_log_unchanged": True, "incident_closed_events": 3},
    "history-close-refuses-an-incident-opened-after-the-analysis": {
        "exit": 1, "closed": 0, "refusal": "opened after the row-638 analysis"},
    "history-close-refuses-a-fingerprint-recurred-since-analysis": {
        "exit": 1, "closed": 0, "refusal": "recurred since the analysis"},
    "history-close-card-done-needs-a-finished-card": {
        "exit": 1, "closed": 0, "refusal": "card still active"},
    "history-close-refuses-an-incident-korin-worked": {
        "exit": 1, "closed": 0, "refusal": "Korin filled the record"},
    "history-close-refuses-an-incident-with-a-fix-card": {
        "exit": 1, "closed": 0, "refusal": "Korin filled the record"},
    "history-close-resolved-needs-the-card-not-blocked": {
        "exit": 1, "closed": 0, "refusal": "card still active"},
    "history-close-records-who-verdict-evidence-and-list": {
        "closed": 1, "closed_by": "cos-history", "evidence_has_verdict": True,
        "evidence_has_list_hash": True, "event_has_list_sha": True},
    "history-close-apply-with-a-wrong-list-hash-is-refused": {
        "exit": 1, "closed": 0, "db_changed": False, "refusal": "--list-sha"},
    "history-close-two-listed-twins-both-close": {"exit": 0, "closed": 2},
    "history-close-an-unlisted-twin-still-blocks": {
        "exit": 1, "closed": 0, "refusal": "live repeat"},
}


class Replay(Harness):
    def play(self, entries, *, apply=False, sha="auto"):
        """Run close-history in process at CUTOFF; the observed values for every OBSERVABLE key."""
        listing = self.write_list(entries)
        digest = hashlib.sha256(listing.read_bytes()).hexdigest()
        args = argparse.Namespace(list=str(listing), apply=apply,
                                  list_sha=digest if sha == "auto" else sha)
        cfg = incidents_cli._cfg(self.home, "b")
        outbox_before, changes = self.count("outbox"), self.conn.total_changes
        log_before = self.log_size()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("time.time", return_value=CUTOFF), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = incidents_cli._close_history(self.conn, args, cfg)
        text = out.getvalue() + err.getvalue()
        closed = list(self.conn.execute("SELECT * FROM incidents WHERE closure='history'"))
        evidence = " ".join(json.loads(closed[0]["evidence"])) if closed else ""
        who = re.search(r"history by (\S+) at ", evidence)
        events = [json.loads(r["payload"]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='incident_closed'")]
        verdict = entries[0].get("verdict")
        return {
            "exit": code, "closed": len(closed), "db_changed": self.conn.total_changes != changes,
            "output": out.getvalue(), "refusal": text,
            "refusal_names_row": 638 if "'| 638 |'" in text else None,
            "status_after": self.row(entries[0]["id"])["status"],
            "closure": closed[0]["closure"] if closed else None,
            "closed_by": who.group(1) if who else None,
            "outbox_rows_added": self.count("outbox") - outbox_before,
            "evidence_has_verdict": f"row 638: {verdict}: " in evidence,
            "evidence_has_list_hash": f"(list {digest[:12]})" in evidence,
            "event_has_list_sha": bool(events) and all(e["list_sha"] == digest for e in events),
            "outbox_log_unchanged": self.log_size() == log_before,
            "incident_closed_events": len(events),
        }

    # -- the worlds, one per row id ---------------------------------------------------------

    NO_ROW = "| 637 | 2026-10-29 | something else |\nsee | 638 | elsewhere, mid-line\n"

    def w_needs_the_638_row(self):
        a = self.inc("one")
        self.write_note(self.NO_ROW)
        return self.play([self.entry(a)])

    def w_apply_needs_the_638_row(self):
        a = self.inc("one")
        self.write_note(self.NO_ROW)
        return self.play([self.entry(a)], apply=True)

    def w_dry_run_writes_nothing(self):
        return self.play([self.entry(self.inc("one"))])

    def w_never_closes_a_repeat_or_escaped_bug(self):
        first = self.inc("again")
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (CUTOFF - DAY, first))
        rep = self.inc("again", opened=CUTOFF - 3600)
        bug = self.inc("bug", kind="escaped_bug")
        return self.play([self.entry(rep), self.entry(bug)], apply=True)

    def w_leaves_a_live_repeat_open(self):
        old = self.inc("same", age=60)
        self.inc("same", age=3)
        return self.play([self.entry(old)], apply=True)

    def w_sends_no_notice(self):
        ids = [self.inc(k, task=self.card) for k in ("one", "two", "three")]
        return self.play([self.entry(i) for i in ids], apply=True)

    def w_refuses_an_incident_opened_after_the_analysis(self):
        late = self.inc("late", opened=CUTOFF)
        return self.play([self.entry(late)], apply=True)

    def w_refuses_a_fingerprint_recurred_since_analysis(self):
        old = self.inc("again", age=60)
        twin = self.inc("again", opened=CUTOFF + 3600)
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=? WHERE id=?",
                          (CUTOFF + 7200, twin))
        return self.play([self.entry(old)], apply=True)

    def w_card_done_needs_a_finished_card(self):
        hi = self.inc("hi", kind="human_intervention", task=self.make_card("running"))
        return self.play([self.entry(hi, "card_done")], apply=True)

    def w_refuses_an_incident_korin_worked(self):
        a = self.inc("one")
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (a,))
        return self.play([self.entry(a)], apply=True)

    def w_refuses_an_incident_with_a_fix_card(self):
        a = self.inc("one")
        self.conn.execute("UPDATE incidents SET fix_card='t_fix00000' WHERE id=?", (a,))
        return self.play([self.entry(a)], apply=True)

    def w_resolved_needs_the_card_not_blocked(self):
        a = self.inc("one", task=self.make_card("blocked"))
        return self.play([self.entry(a)], apply=True)

    def w_records_who_verdict_evidence_and_list(self):
        one = self.inc("one", task=self.card)
        return self.play([self.entry(one, "false_alarm")], apply=True)

    def w_apply_with_a_wrong_list_hash_is_refused(self):
        ids = [self.inc("one"), self.inc("two")]
        return self.play([self.entry(i) for i in ids], apply=True, sha=WRONG_SHA)

    def w_two_listed_twins_both_close(self):
        ids = [self.inc("twin", age=28), self.inc("twin", age=24)]
        return self.play([self.entry(i) for i in ids], apply=True)

    def w_an_unlisted_twin_still_blocks(self):
        ids = [self.inc("trio", age=n) for n in (28, 24, 20)]
        return self.play([self.entry(i) for i in ids[:2]], apply=True)

    WORLDS = {
        "history-close-needs-the-638-row": w_needs_the_638_row,
        "history-close-apply-needs-the-638-row": w_apply_needs_the_638_row,
        "history-close-dry-run-writes-nothing": w_dry_run_writes_nothing,
        "history-close-never-closes-a-repeat-or-escaped-bug":
            w_never_closes_a_repeat_or_escaped_bug,
        "history-close-leaves-a-live-repeat-open": w_leaves_a_live_repeat_open,
        "history-close-sends-no-notice": w_sends_no_notice,
        "history-close-refuses-an-incident-opened-after-the-analysis":
            w_refuses_an_incident_opened_after_the_analysis,
        "history-close-refuses-a-fingerprint-recurred-since-analysis":
            w_refuses_a_fingerprint_recurred_since_analysis,
        "history-close-card-done-needs-a-finished-card": w_card_done_needs_a_finished_card,
        "history-close-refuses-an-incident-korin-worked": w_refuses_an_incident_korin_worked,
        "history-close-refuses-an-incident-with-a-fix-card": w_refuses_an_incident_with_a_fix_card,
        "history-close-resolved-needs-the-card-not-blocked":
            w_resolved_needs_the_card_not_blocked,
        "history-close-records-who-verdict-evidence-and-list":
            w_records_who_verdict_evidence_and_list,
        "history-close-apply-with-a-wrong-list-hash-is-refused":
            w_apply_with_a_wrong_list_hash_is_refused,
        "history-close-two-listed-twins-both-close": w_two_listed_twins_both_close,
        "history-close-an-unlisted-twin-still-blocks": w_an_unlisted_twin_still_blocks,
    }

    # -- the checks -------------------------------------------------------------------------

    def assert_row(self, row_id, expect):
        observed = self.WORLDS[row_id](self)
        for key, want in expect.items():
            self.assertIn(key, OBSERVABLE, f"{row_id}: cannot observe {key!r}")
            got = observed[key]
            if key in ("output", "refusal"):
                self.assertIn(want, got, f"{row_id}: {key}")
            else:
                self.assertEqual(got, want, f"{row_id}: {key}")

    def test_every_case_world_gives_its_expected_values(self):
        for row_id, expect in CASES.items():
            with self.subTest(row_id):
                self.tearDown()
                self.setUp()
                self.assert_row(row_id, expect)

    def test_the_shipped_rows_match_the_cases_and_replay(self):
        shipped = intent.role_path("incident_history").is_file()
        rows = intent.load("incident_history")
        # not shipped yet (the CoS ships it): no rows and nothing to replay; shipped: rows, and
        # an empty file fails here instead of passing for lack of anything to check
        self.assertEqual(bool(rows), shipped, "incident_history.jsonl: file and rows disagree")
        if shipped:  # 1-3 new rows per round (row 454): the shipped ids are a subset of CASES
            self.assertLessEqual({r["id"] for r in rows}, set(CASES))
        for row in rows:
            self.assertEqual(row["expect"], CASES[row["id"]], row["id"])
            with self.subTest(row["id"]):
                self.tearDown()
                self.setUp()
                self.assert_row(row["id"], row["expect"])


if __name__ == "__main__":
    unittest.main()
