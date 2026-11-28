"""Row t_5a8cb343 (row 509 Zenkai Z7: new logic in new modules, tests alongside): a reviewer
finding whose file is outside the card's declared file_set moves to `out_of_set`
(`capsule/review_scope.py`) only when this round's own diff did NOT touch that file - a file
outside the set the diff DID touch is scope creep and stays a failure.

Split out of capsule/tests/test_tick.py (moved, not duplicated) so that file does not grow.
"""
import json
from pathlib import Path

from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_FAIL, REV_PASS, VAL_PASS, TickCase, git


class OutOfSetFindings(TickCase):
    """Row t_5a8cb343, AC2 (fix round 1, F3): a reviewer finding whose file is outside the
    card's declared file_set moves to `out_of_set` ONLY when this round's own diff did not
    touch that file - reported (one `out_of_set_findings` event + one outbox line), never
    failing the card. A file outside the set the diff DID touch is scope creep (spec A.4: the
    diff must stay in the declared file set) and still fails, never moved."""

    def setUp(self):
        super().setUp()
        self.conn.execute("UPDATE tasks SET file_set=? WHERE id=?", (json.dumps(["app.py"]), self.tid))

    def test_an_in_set_finding_still_fails_the_card(self):
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL]]     # REV_FAIL's finding is app.py:12, in the set
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("out_of_set_findings", kinds)

    def test_an_out_of_set_finding_never_fails_the_card(self):
        finding = {"file": "tests/ui/test_settings_page.py", "line": 40, "class": "least_code",
                  "text": "still has time.sleep(1.0)"}
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[finding])]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["failure_count"], 0)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='out_of_set_findings'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["findings"], [finding])
        log = (self.home / "outbox.log").read_text()
        self.assertIn(self.tid, log)
        self.assertIn("out_of_set", log)

    def test_an_untouched_out_of_set_finding_with_safety_still_fails_the_card(self):
        finding = {"file": "tests/ui/test_settings_page.py", "line": 40, "class": "out_of_set",
                   "text": "leaks a secret", "safety": True}
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[finding])]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)
        sql = "SELECT kind FROM task_events WHERE task_id=? AND kind='out_of_set_findings'"
        self.assertEqual(self.conn.execute(sql, (self.tid,)).fetchall(), [])

    def test_an_out_of_set_finding_on_a_file_the_diff_touched_still_fails(self):
        """Fix round 1, F3: scope creep - the diff itself touches a file outside the declared
        set - must keep failing, never move, even though the finding names that same file
        (the review's own run-839f3172 example: 'this diff carries a second card's work')."""
        self.script["implementer"] = [[IMPL_DONE]]

        def touch_out_of_set(job, sp, run):
            ws = Path(job["workspace"])
            (ws / "tests" / "ui").mkdir(parents=True, exist_ok=True)
            (ws / "tests" / "ui" / "test_settings_page.py").write_text("# a second card's work\n")
            git(ws, "add", ".")
            git(ws, "commit", "-qm", "scope creep")
        self.hooks["implementer"] = touch_out_of_set
        finding = {"file": "tests/ui/test_settings_page.py", "line": 1, "class": "out_of_set",
                  "text": "this diff carries a second card's work"}
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[finding])]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("out_of_set_findings", kinds)
        comments = [r[0] for r in self.conn.execute("SELECT body FROM task_comments")]
        self.assertTrue(any("test_settings_page.py" in c for c in comments), comments)

    def test_out_of_set_only_verdict_pass_never_contradicts_pass_with_findings(self):
        """Fix round 1, F3 (the real 4872-shaped contradiction): a reviewer document with
        `verdict: pass` and only out-of-set findings must not trip the `pass-with-findings`
        intent example (which judges the RAW document before this fix) - the move now happens
        before `intent.check` ever sees it (`_judged_reviewer_doc`), so the round proceeds to
        done instead of a contradiction/triage detour."""
        finding = {"file": "tests/ui/test_settings_page.py", "line": 1, "class": "out_of_set",
                  "text": "flagged by the reviewer itself"}
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="pass", findings=[finding])]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] in ("done", "triage"):
                break
        self.assertEqual(self.task()["status"], "done")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("contradiction", kinds)

    def test_a_reshaped_but_equivalent_path_still_matches_the_declared_set(self):
        """Fix round 1, F5: a leading `./` must not let an in-set finding dodge its own file_set
        match (which would silently move it and never fail the round)."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_FAIL, findings=[
            {"file": "./app.py", "line": 12, "class": "least_code", "text": "dead branch"}])]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)

    def test_an_absolute_path_finding_is_never_moved(self):
        """Fix round 1, F5: an absolute path fails closed - always in-set, never a move
        candidate, even when it happens to end with a name outside the declared set."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[
            {"file": "/etc/passwd", "line": 1, "class": "security", "text": "absolute path probe"}])]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("out_of_set_findings", kinds)

    def test_a_self_tagged_out_of_set_class_finding_never_fails_even_when_in_set(self):
        """A reviewer that tags its own finding `class: out_of_set` is honored even for a file
        that IS in the declared set - the reviewer, not just the file path, may name it."""
        finding = {"file": "app.py", "line": 5, "class": "out_of_set", "text": "flagged by the reviewer itself"}
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[finding])]]
        for _ in range(6):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")

    def test_no_declared_file_set_never_moves_a_finding(self):
        """No file_set on the card at all: no in/out line to draw, unchanged from before this
        fix - every finding stays in `findings` and can still fail the round."""
        self.conn.execute("UPDATE tasks SET file_set=NULL WHERE id=?", (self.tid,))
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[
            {"file": "tests/ui/test_settings_page.py", "line": 40, "class": "least_code", "text": "x"}])]]
        self.tick.once()
        self.tick.once()
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self.task()["failure_count"], 1)

    def test_the_rerun_bundle_excludes_an_out_of_set_finding(self):
        """Fix round 1, F3: `_open_findings` applies the same move `_verdict` does, so a rerun
        implementer is never told to fix a finding that would push its own diff outside its
        file set."""
        in_set = {"file": "app.py", "line": 12, "class": "least_code", "text": "dead branch"}
        out_of_set = {"file": "tests/ui/test_settings_page.py", "line": 40, "class": "least_code",
                     "text": "unrelated sibling issue"}
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[in_set, out_of_set])], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for _ in range(8):
            self.tick.once()
            if len(self.runs("guide")) == 2:
                break
        second = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(second["run_id"]) / "job.json").read_text())
        self.assertEqual(job["inputs"]["review_findings"], ["reviewer: app.py:12 [least_code] dead branch"])
        # F3 "when present": the second round's own guide bundle carries the prior round's
        # out-of-set finding, for context - never judged, never counted.
        second_guide = self.runs("guide")[1]
        gjob = json.loads((self.spool_dir(second_guide["run_id"]) / "job.json").read_text())
        self.assertEqual(gjob["inputs"]["out_of_set"], [out_of_set])
        gbundle = json.loads((self.spool_dir(second_guide["run_id"]) / "bundle.json").read_text())["prompt"]
        self.assertIn("tests/ui/test_settings_page.py:40", gbundle)
        first_guide = self.runs("guide")[0]
        gjob1 = json.loads((self.spool_dir(first_guide["run_id"]) / "job.json").read_text())
        self.assertNotIn("out_of_set", gjob1["inputs"])

    def test_the_rerun_bundle_still_includes_a_touched_out_of_set_finding(self):
        """Round 3 review, R2: a scope-creep finding (a file outside the declared set that the
        diff DID touch) must still reach the rerun bundle's `review_findings` - this is the
        oracle for `review_scope.open_round_touched` actually reading the round's diff. A mutant
        that always returned `[]` there would treat every out-of-set file as untouched and
        wrongly move this one out too, and it would be missing from `review_findings` here."""
        touched_once = []

        def touch_out_of_set(job, sp, run):
            if touched_once:
                return
            touched_once.append(True)
            ws = Path(job["workspace"])
            (ws / "tests" / "ui").mkdir(parents=True, exist_ok=True)
            (ws / "tests" / "ui" / "test_settings_page.py").write_text("# a second card's work\n")
            git(ws, "add", ".")
            git(ws, "commit", "-qm", "scope creep")
        self.hooks["implementer"] = touch_out_of_set
        finding = {"file": "tests/ui/test_settings_page.py", "line": 1, "class": "least_code",
                  "text": "this diff carries a second card's work"}
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[dict(REV_PASS, verdict="fail", findings=[finding])], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for _ in range(8):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        second = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(second["run_id"]) / "job.json").read_text())
        self.assertEqual(job["inputs"]["review_findings"],
                         ["reviewer: tests/ui/test_settings_page.py:1 [least_code] this diff"
                          " carries a second card's work"])


