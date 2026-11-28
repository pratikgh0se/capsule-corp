"""Blind text and the review stage (D161), second round: free text a validator writes is withheld
in a job with blind rows (S45); a review-infra hold, a retry event, a spawn failure and a tick
error on the review route never carry it (S46). A plain job shows everything it showed before."""
import json
import time
import unittest
from unittest import mock

from capsule import incident_blind, incident_inputs, incidents, paths, tick, validator_report
from capsule.tests.test_blind_text import MARK, blind_rows, fail
from capsule.tests.test_tick import IMPL_DONE, VAL_PASS, TickCase

OUT = "ran 3\n1 failure\nFAILED (failures=1)"
ROWS = [{"cmd": "blind:t1.test_x", "blind": "report"}, {"cmd": "make test", "output": OUT}]
PLAIN = [{"cmd": "make test"}, {"cmd": "make lint"}]
GIVEN = "given: GIVENTOK"


def doc(*assertions, not_run=()):
    return {"verdict": "fail", "assertions": list(assertions), "not_run": list(not_run)}


class FreeText(unittest.TestCase):
    def test_a_cmd_that_is_no_plain_row_of_a_blind_job_is_the_fixed_phrase(self):
        for cmd in ("unit", "Blind:t1", " blind:t1", "`blind:t1`", "BLIND:T1.x", "t1: " + GIVEN):
            got = validator_report.findings(doc(fail(cmd, GIVEN)), outputs=ROWS)
            self.assertEqual(got, [validator_report.BLIND_FINDING], cmd)

    def test_a_plain_row_keeps_its_excerpt_a_blind_row_never_does(self):
        mixed = doc(fail("make test", "1 failure"), fail("blind:t1.test_x", GIVEN),
                    fail("elsewhere", GIVEN))
        got = validator_report.findings(mixed, outputs=ROWS)
        self.assertEqual(got, ["validator: `make test` exit 1 fail: 1 failure",
                               validator_report.BLIND_FINDING])
        self.assertNotIn("GIVENTOK", "".join(got))

    def test_a_plain_rows_excerpt_is_kept_only_when_its_lines_are_in_that_rows_own_output(self):
        def shown(excerpt):
            return validator_report.findings(doc(fail("make test", excerpt)), outputs=ROWS)[0]
        self.assertEqual(shown("1 failure"), "validator: `make test` exit 1 fail: 1 failure")
        self.assertIn("ran 3\n1 failure", shown("ran 3\n1 failure"))          # every line is there
        self.assertIn("FAILED", shown("  FAILED (failures=1)  "))                # strip only
        for excerpt in ("given: BLINDMARK", "1 failure\ngiven: BLINDMARK", "BLINDMARK\n1 failure"):
            self.assertEqual(shown(excerpt), "validator: `make test` exit 1 fail", excerpt)
        other = [{"cmd": "make lint", "output": "given: BLINDMARK"}, *ROWS]   # another row's text
        self.assertEqual(validator_report.findings(doc(fail("make test", "given: BLINDMARK")),
                                                   outputs=other),
                         ["validator: `make test` exit 1 fail"])

    def test_the_spellings_of_a_blind_cmd_are_the_phrase_with_no_rows_at_all(self):
        for cmd in ("Blind:t1", " blind:t1", "`blind:t1`"):
            self.assertEqual(validator_report.findings(doc(fail(cmd, GIVEN))),
                             [validator_report.BLIND_FINDING], cmd)

    def test_a_plain_job_shows_what_it_showed_before(self):
        free = doc(fail("unit", GIVEN), fail("make test", "1 failure"))
        want = [f"validator: `unit` exit 1 fail: {GIVEN}",
                "validator: `make test` exit 1 fail: 1 failure"]
        for rows in (None, [], PLAIN):
            self.assertEqual(validator_report.findings(free, outputs=rows), want, rows)
        self.assertIs(validator_report.withhold(free, PLAIN), free)

    def test_withhold_is_applied_once_and_leaves_the_rest_of_the_document(self):
        raw = dict(doc(fail("unit", GIVEN), fail("blind:t1.test_x", GIVEN)), summary="s")
        once = validator_report.withhold(raw, ROWS)
        self.assertEqual([(a["cmd"], a["excerpt"]) for a in once["assertions"]],
                         [(validator_report.WITHHELD_CMD, None), ("blind:t1.test_x", None)])
        self.assertEqual((once["summary"], once["verdict"]), ("s", "fail"))
        self.assertEqual(validator_report.withhold(once, ROWS), once)
        self.assertEqual(raw["assertions"][0]["excerpt"], GIVEN)       # the input is not changed

    def test_a_not_run_entry_that_is_no_plain_row_is_not_quoted_in_a_blind_job(self):
        raw = doc(not_run=["t1: " + GIVEN, "make lint", "blind:t1.test_x"])
        text = validator_report.not_run_violation(raw, ROWS + [{"cmd": "make lint"}])
        self.assertNotIn("GIVENTOK", text)
        self.assertNotIn("t1", text)
        self.assertIn("make lint", text)
        self.assertIn("3 item(s)", text)
        plain = validator_report.not_run_violation(raw, PLAIN)          # a plain job: as before
        self.assertIn("t1: " + GIVEN, plain)
        self.assertIn("a blind row", plain)                             # but never a blind: name


class Tick(TickCase):
    def run_ticks(self, n=8):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        now = int(time.time())
        for k in range(n):
            self.tick.once(now + k)
            if self.task()["status"] == "blocked":
                break

    def surfaces(self):
        """What Korin's input and the implementer's can read: events, outbox, its log, incidents."""
        rows = {t: [dict(r) for r in self.conn.execute(f"SELECT * FROM {t}")]
                for t in ("task_events", "outbox", "incidents")}
        log = paths.outbox_log(self.home, "test")
        return json.dumps(rows) + (log.read_text() if log.is_file() else "")

    def hold(self):
        """The reason text of the card's `review_infra` hold."""
        return " ".join(r[0] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind IN ('blocked', 'block_loop_detected')"))

    def test_a_validator_document_with_free_text_never_reaches_the_implementers_findings(self):
        val = dict(VAL_PASS, verdict="fail", assertions=[
            fail("unit", GIVEN), fail("make test", "1 failure"),
            fail("make test", "given: GIVENTOK")])
        seen = []
        self.hooks["implementer"] = lambda job, sp, run: seen.append(json.dumps(job))
        self.hooks["validator"] = lambda job, sp, run: (sp / "job.json").write_text(json.dumps(
            dict(job, inputs=dict(job.get("inputs") or {}, outputs=ROWS))))
        self.script["implementer"], self.script["validator"] = [[IMPL_DONE]] * 2, [[val]]
        for _ in range(3):
            self.tick.once()
        self.runner.up = False
        for _ in range(3):
            self.tick.once()
        comments = " ".join(r[0] for r in self.conn.execute(
            "SELECT body FROM task_comments WHERE body LIKE 'Review failed:%'"))
        self.assertIn(validator_report.BLIND_FINDING, comments)
        self.assertIn("`make test` exit 1 fail: 1 failure", comments)    # its own line stays
        self.assertNotIn("GIVENTOK", self.surfaces() + "".join(seen) + comments)
        self.assertGreaterEqual(len(seen), 2)                  # the rerun's job was read
        handoff = " ".join(r[0] or "" for r in self.conn.execute("SELECT handoff FROM task_runs"))
        self.assertNotIn("GIVENTOK", handoff)

    def test_run_row_tolerates_a_run_with_no_run_id_or_a_missing_key(self):
        root = self.spool_dir("x").parent
        for row in ({"error": "e", "profile": "validator", "run_id": None},
                    {"error": "e", "profile": "validator"}, {"error": "e", "run_id": "r1"},
                    {"error": "e"}, {"profile": "validator", "run_id": None}):
            self.assertEqual(incident_inputs.run_row(row, root), row, row)
        tester = {"error": "e", "error_class": "fatal", "profile": "blind-tester", "run_id": None}
        self.assertEqual(incident_inputs.run_row(tester, root)["error"],
                         "fatal: " + incident_inputs.BLIND_RUN_ERROR)

    def test_a_review_infra_hold_and_its_retry_never_carry_a_blind_runs_error(self):
        self.hooks["validator"] = blind_rows()
        with mock.patch.object(validator_report, "not_run_violation",
                               return_value=f"bad document {MARK}"):
            self.run_ticks()
        self.assertEqual(self.task()["block_kind"], "review_infra")
        self.assertIn(incident_inputs.BLIND_RUN_ERROR, self.hold())
        events = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='review_retry'")]
        self.assertIn(incident_inputs.BLIND_RUN_ERROR, json.dumps(events))
        self.assertNotIn("BLINDMARK", self.surfaces())

    def test_an_ordinary_validators_error_keeps_its_text(self):
        with mock.patch.object(validator_report, "not_run_violation",
                               return_value=f"bad document {MARK}"):
            self.run_ticks()
        self.assertEqual(self.task()["block_kind"], "review_infra")
        self.assertIn("BLINDMARK", self.hold())

    def test_a_spawn_failure_of_a_blind_validator_keeps_the_type_and_drops_the_text(self):
        def hook(job, sp, run):
            blind_rows()(job, sp, run)
            raise RuntimeError(f"cannot start {MARK}")
        self.hooks["validator"] = hook
        self.run_ticks(6)
        failed = [dict(r) for r in self.conn.execute(
            "SELECT error FROM task_runs WHERE outcome='spawn_failed'")]
        self.assertTrue(failed)
        self.assertIn("RuntimeError: " + incident_inputs.WITHHELD_TEXT, failed[0]["error"])
        self.assertNotIn("BLINDMARK", self.surfaces() + json.dumps(failed))

    def test_a_tick_error_on_the_review_route_is_withheld_for_a_blind_validator(self):
        self.hooks["validator"] = blind_rows()
        with mock.patch.object(tick.Tick, "_verdict", side_effect=ValueError(f"bad {MARK}")):
            self.run_ticks(8)
        errors = [json.loads(r[0])["error"] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='tick_error'")]
        self.assertTrue(errors)
        self.assertTrue(errors[0].endswith("ValueError: " + incident_inputs.WITHHELD_TEXT), errors)
        self.assertNotIn("BLINDMARK", self.surfaces())

    def refuse(self, message, kind=RuntimeError):
        def work():
            raise kind(message)
        self.tick._guarded(self.conn, self.cfg, self.tid, int(time.time()), {"tick_errors": 0},
                           work)

    def test_a_blind_mount_refusal_on_a_card_with_a_tester_run_keeps_its_class_and_key(self):
        self.conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id)"
                          " VALUES (?, 'blind-tester', 'completed', 1, 'tester1')", (self.tid,))
        check = incident_blind.BLIND_CHECK
        incidents.from_events(self.conn, board="test", now=1)                    # the watermark
        for message, kind in ((f"{check}: /w/blind-work/{MARK}/tests [run capsule-aaaa]", None),
                              (f"{check}: /w/blind-work/{MARK}/tests [run capsule-bbbb]", None),
                              (f"{check}: /w/blind-work/{MARK}/other [run capsule-bbbb]", None),
                              (f"{check}: /blind [run capsule-bbbb]", None),
                              (f"{check}: /w/x [run capsule-cccc] {MARK}", None),
                              (f"wrapped {check}: /p {MARK}", None),
                              (f"{check}: /p {MARK}", ValueError)):
            self.refuse(message, kind or RuntimeError)
        errors = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='tick_error' ORDER BY id")]
        self.assertEqual(len(errors), 7)
        for e in errors[:5]:                               # the host's own refusal keeps its shape
            self.assertTrue(incident_blind.mount_path(e), e)
        self.assertEqual(len({e["error"] for e in errors[:4]}), 4)           # per run and per path
        self.assertIn("capsule-aaaa", errors[0]["error"])                      # the run tag stays
        self.assertIn("capsule-bbbb", errors[1]["error"])
        for e in errors[5:]:                                  # not that refusal: the fixed phrase
            self.assertTrue(e["error"].endswith(incident_inputs.WITHHELD_TEXT), e)
        root = self.spool_dir("x").parent
        incidents.from_events(self.conn, board="test", spool_root=root, now=2)
        by_kind = {r["kind"]: r for r in incidents.listing(self.conn)}
        self.assertEqual(by_kind["review_infra"]["root_key"],
                         f"review_infra: {check}:{self.tid}: {check}")
        self.assertIn("crash", by_kind)
        self.assertNotIn("BLINDMARK", self.surfaces())
        self.assertNotIn("blind-work", self.surfaces())


if __name__ == "__main__":
    unittest.main()
