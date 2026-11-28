"""t_413bdb86, row 509 Zenkai Z7: an ALIGNED verdict reports a card-text correction in `notes`
(`capsule/guide_notes.py`), never as `amendment`, which is for `drifted` only."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import bundles, contracts, db, guide_notes
from capsule.tests.test_tick import (
    DRIFTED_NO_ROW, GUIDE_OK, IMPL_DONE, REV_FAIL, REV_PASS, VAL_PASS, TickCase)

TASK = {"id": "t_0000aaaa", "title": "t", "body": "B"}


class AmendmentScopeViolations(unittest.TestCase):
    """Unit test on the validator itself (Zenkai Z2's second oracle, beside the intent.jsonl
    pattern): real code, sharing no code path with `intent.check`."""

    def test_aligned_with_amendment_is_a_violation(self):
        report = dict(GUIDE_OK, amendment=[{"target": "card", "find": "a", "replace": "b"}])
        found = guide_notes.amendment_scope_violations(report)
        self.assertEqual([f["id"] for f in found], ["aligned-amendment-scope"])

    def test_aligned_with_notes_and_no_amendment_is_clean(self):
        report = dict(GUIDE_OK, notes=["CHANGELOG.md:43 anchor does not exist"])
        self.assertEqual(guide_notes.amendment_scope_violations(report), [])

    def test_drifted_with_amendment_is_clean(self):
        report = dict(GUIDE_OK, verdict="drifted",
                      amendment=[{"target": "card", "find": "a", "replace": "b"}])
        self.assertEqual(guide_notes.amendment_scope_violations(report), [])

    def test_aligned_with_no_amendment_is_clean(self):
        self.assertEqual(guide_notes.amendment_scope_violations(GUIDE_OK), [])


class DropNoOpHunks(unittest.TestCase):
    RUN = {"id": None, "run_id": "r1"}

    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        db.create_board(self.d / "k.db")
        self.conn = db.connect(self.d / "k.db")
        self.tid = db.new_task(self.conn, title="t")

    def tearDown(self):
        self.conn.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def drop(self, report, role="guide"):
        return guide_notes.drop_no_op_hunks(role, self.conn, self.tid, self.RUN, report, 1)

    def logged(self):
        rows = self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='amendment_no_op_dropped'")
        return [json.loads(r[0])["count"] for r in rows]

    def test_identity_hunks_are_removed_and_empty_key_is_dropped(self):
        report = dict(GUIDE_OK, amendment=[{"target": "card", "find": "x", "replace": "x"}])
        before = json.dumps(report)
        out = self.drop(report)
        self.assertNotIn("amendment", out)
        self.assertEqual(json.dumps(report), before)    # input untouched
        self.assertEqual(self.logged(), [1])

    def test_real_hunk_is_kept(self):
        real = {"target": "card", "find": "a", "replace": "b"}
        noop = {"target": "card", "find": "x", "replace": "x"}
        out = self.drop(dict(GUIDE_OK, amendment=[noop, real]))
        self.assertEqual(out["amendment"], [real])
        self.assertEqual(self.logged(), [1])

    def test_nothing_to_drop_logs_nothing(self):
        real = {"target": "card", "find": "a", "replace": "b"}
        self.drop(dict(GUIDE_OK, amendment=[real]))
        self.drop(GUIDE_OK)
        self.assertEqual(self.logged(), [])

    def test_other_roles_are_left_alone(self):
        report = dict(GUIDE_OK, amendment=[{"target": "card", "find": "x", "replace": "x"}])
        self.assertIs(self.drop(report, role="implementer"), report)
        self.assertEqual(self.logged(), [])


class AlignedVerdictWithNotesRoutesToReview(TickCase):
    """Tick-path test: an aligned verdict with `notes` (amendment empty) is a clean review round -
    no contradiction, no fallback."""

    def test_notes_alone_reaches_review_without_a_contradiction(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["guide"] = [[dict(GUIDE_OK, notes=["CHANGELOG.md:43 anchor does not exist"])]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        row = self.task()
        self.assertEqual(row["status"], "done")
        self.assertIsNone(row["block_kind"])
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("contradiction", kinds)
        self.assertIn("notes", kinds)

    def test_aligned_with_a_no_op_amendment_reaches_review_without_a_contradiction(self):
        """inc_10699f92: an identity hunk (find == replace) is dropped, not refused."""
        self.script["implementer"] = [[IMPL_DONE]]
        noop = [{"target": "card", "find": "x", "replace": "x"}]
        self.script["guide"] = [[dict(GUIDE_OK, notes=["real correction"], amendment=noop)]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        self.assertEqual(self.task()["status"], "done")
        rows = self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))
        kinds = [r[0] for r in rows]
        self.assertNotIn("contradiction", kinds)
        self.assertEqual(kinds.count("amendment_no_op_dropped"), 1)
        self.assertEqual(kinds.count("notes"), 1)

    def test_repeated_identity_amendment_hunks_reach_review_without_a_contradiction(self):
        """inc_b35e0b86: identity hunks in two guide rounds are dropped each time, never refused."""
        identity = [{"target": "card", "find": "unused", "replace": "unused"}]
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        round_ = [dict(GUIDE_OK, amendment=identity)]
        self.script["guide"] = [round_, round_]
        for _ in range(12):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        self.assertEqual(self.task()["status"], "done")
        rows = self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))
        kinds = [r[0] for r in rows]
        self.assertNotIn("contradiction", kinds)
        self.assertEqual(kinds.count("amendment_no_op_dropped"), 2)

    def test_aligned_with_an_amendment_still_falls_back_as_a_contradiction(self):
        """Aligned + amendment is still refused (twice), never silently accepted just because
        `notes` now exists as an alternative."""
        self.script["implementer"] = [[IMPL_DONE]]
        amend = [{"target": "card", "find": "a", "replace": "b"}]
        self.script["guide"] = [[dict(GUIDE_OK, amendment=amend)], [dict(GUIDE_OK, amendment=amend)]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] in ("blocked", "triage"):
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "review_infra"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertEqual(kinds.count("contradiction"), 2)
        # each contradiction records both oracles (Z2)
        payloads = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='contradiction' ORDER BY id", (self.tid,))]
        for payload in payloads:
            self.assertEqual({e["id"] for e in payload["examples"]},
                             {"aligned-verdict-carries-no-amendment", "aligned-amendment-scope"})

    def test_notes_is_not_logged_while_the_round_is_refused(self):
        """`report_notes.note` runs only once the round clears the contradiction check."""
        self.script["implementer"] = [[IMPL_DONE]]
        refused = dict(DRIFTED_NO_ROW, notes=["should not appear while refused"])
        clean = dict(GUIDE_OK, notes=["appears once the round is clean"])
        self.script["guide"] = [[refused], [clean]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] in ("done", "blocked", "triage"):
                break
        self.assertEqual(self.task()["status"], "done")
        notes_events = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='notes' ORDER BY id", (self.tid,))]
        self.assertEqual(len(notes_events), 1)
        self.assertEqual(notes_events[0]["entries"], clean["notes"])


class NotesVsAmendmentGuidanceReachesEveryPromptSurface(unittest.TestCase):
    """The aligned->notes / drifted->amendment split must reach the model: reverting either source
    sentence (`bundles.VERDICT_NOTE` or `contracts/guide.md`'s `reports:` field) must fail this
    test."""

    def test_the_split_is_in_verdict_note_and_the_composed_prompt(self):
        self.assertIn("put it in `notes` instead and leave `amendment` empty", bundles.VERDICT_NOTE)
        prompt = bundles.guide(TASK, "S", [417], row_texts={"417": "t"})["prompt"]
        self.assertIn("put it in `notes` instead and leave `amendment` empty", prompt)

    def test_the_guide_is_told_to_omit_the_key(self):
        note = "means omit the key: the schema refuses [], and a hunk where find equals replace"
        self.assertIn(note, bundles.VERDICT_NOTE)
        self.assertIn(note, bundles.guide(TASK, "S", [417], row_texts={"417": "t"})["prompt"])

    def test_the_split_is_in_the_rendered_guide_contract(self):
        rendered = contracts.render("guide")
        self.assertIn("put it in `notes`, leave `amendment` empty", rendered)


if __name__ == "__main__":
    unittest.main()
