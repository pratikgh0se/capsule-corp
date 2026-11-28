"""inc_e1d965b7 (row 456): report.v1 `follow_ups` through the real tick. Contained work outside
the card's file set becomes an intake card and the run goes to review, never a needs_input hold.
Split out of capsule/tests/test_tick.py (docs/ZENKAI.md Z7: that file sits at its size baseline).
Needs the optional `follow_ups` array in contracts/schemas/report.v1.json.
"""
import json
from unittest import mock

from capsule import bundles, follow_ups
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_FAIL, REV_PASS, VAL_PASS, TickCase

FOLLOW_UP = {"title": "bin/capsule-unblocked-hook.sh: use the GNU-first stat form",
             "files": ["bin/capsule-unblocked-hook.sh"],
             "body": "Line 95 calls the BSD stat form first; a sibling of this card's case."}
IMPL_FU = dict(IMPL_DONE, follow_ups=[FOLLOW_UP])
EVENTS = "SELECT kind FROM task_events WHERE task_id=? ORDER BY id"


class FollowUps(TickCase):
    def kinds(self, task_id=None):
        return [r[0] for r in self.conn.execute(EVENTS, (task_id or self.tid,))]

    def cards(self):
        sql = "SELECT * FROM tasks WHERE title=?"
        return self.conn.execute(sql, (FOLLOW_UP["title"],)).fetchall()

    def test_out_of_set_follow_up_files_a_card_and_requests_review(self):
        self.script["implementer"] = [[IMPL_FU]]
        for _ in range(2):
            self.tick.once()
        self.assertEqual(self.task()["status"], "review")
        self.assertEqual(self.kinds().count("request_review"), 1)
        self.assertNotIn("blocked", self.kinds())
        self.tick.once()                                    # step 1b ingests the intake file
        (card,) = self.cards()
        self.assertEqual(json.loads(card["file_set"]), FOLLOW_UP["files"])
        self.assertIn("intake_ingested", self.kinds(card["id"]))
        filed = [json.loads(p) for (p,) in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='follow_up_filed'",
            (self.tid,))]
        self.assertEqual([f["files"] for f in filed], [FOLLOW_UP["files"]])

    def test_follow_up_with_real_left_undone_still_holds(self):
        self.script["implementer"] = [[dict(IMPL_FU, left_undone=["the reset button"])]]
        for _ in range(3):
            self.tick.once()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
        self.assertNotIn("request_review", self.kinds())
        self.assertEqual(self.cards(), [])
        self.assertNotIn("follow_up_filed", self.kinds())

    def test_a_retried_report_files_no_duplicate_card(self):
        self.script["implementer"] = [[IMPL_FU], [IMPL_FU]]
        self.script["reviewer"] = [[REV_FAIL], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for _ in range(8):
            self.tick.once()
            if len(self.runs("implementer")) == 2 and self.task()["status"] == "done":
                break
        self.assertEqual(len(self.runs("implementer")), 2)
        self.assertEqual(self.kinds().count("follow_up_filed"), 2)   # both reports asked for it
        self.tick.once()
        (card,) = self.cards()                              # one card: the retry folded into it
        deduped = [r[0] for r in self.conn.execute(
            "SELECT json_extract(payload,'$.deduped_into') FROM task_events"
            " WHERE kind='intake_ingested' ORDER BY id")]
        self.assertEqual(deduped, [None, card["id"]])

    def set_file_set(self, files):
        self.conn.execute("UPDATE tasks SET file_set=? WHERE id=?", (json.dumps(files), self.tid))
        self.conn.commit()

    def assert_held_with_no_card(self):
        for _ in range(3):
            self.tick.once()
        task = self.task()
        self.assertEqual((task["status"], task["block_kind"]), ("blocked", "needs_input"))
        reason = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
            (self.tid,)).fetchone()[0])["reason"]
        self.assertIn("names this card's own files", reason)
        self.assertEqual(self.cards(), [])
        self.assertNotIn("follow_up_filed", self.kinds())

    def test_a_follow_up_touching_the_cards_own_file_set_holds_and_files_no_card(self):
        self.set_file_set(["bin/*.sh"])
        self.script["implementer"] = [[IMPL_FU]]
        self.assert_held_with_no_card()

    def assert_path_form_holds(self, path, own="bin/capsule-unblocked-hook.sh"):
        self.set_file_set([own])
        item = dict(FOLLOW_UP, files=[path])
        self.script["implementer"] = [[dict(IMPL_DONE, follow_ups=[item])]]
        for _ in range(3):
            self.tick.once()
        self.assertEqual(self.task()["block_kind"], "needs_input")
        self.assertNotIn("follow_up_filed", self.kinds())

    def test_an_absolute_path_follow_up_holds(self):
        self.assert_path_form_holds("/work/bin/capsule-unblocked-hook.sh")

    def test_a_glob_follow_up_holds(self):
        self.assert_path_form_holds("bin/*")

    def test_a_directory_follow_up_holds(self):
        self.assert_path_form_holds("bin/")

    def test_a_file_under_an_own_directory_entry_holds(self):
        self.assert_path_form_holds("static/sr/timer.js", own="static/sr")

    def test_a_bracketed_real_path_outside_the_set_files_a_card(self):
        self.set_file_set(["app.py"])
        item = dict(FOLLOW_UP, files=["pages/[id].tsx"])
        self.script["implementer"] = [[dict(IMPL_DONE, follow_ups=[item])]]
        for _ in range(2):
            self.tick.once()
        self.assertEqual(self.task()["status"], "review")
        self.tick.once()
        self.assertEqual(len(self.cards()), 1)

    def test_a_bracketed_path_equal_to_an_own_file_or_its_parent_holds(self):
        self.assert_path_form_holds("app/[id].tsx", own="app/[id].tsx")
        self.assert_path_form_holds("app/[id]", own="app/[id]/page.tsx")

    def hold_body_block(self, block, path="app/main.py"):
        body = "Fix the app.\n\n" + block
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        self.conn.commit()
        item = dict(FOLLOW_UP, files=[path])
        self.script["implementer"] = [[dict(IMPL_DONE, follow_ups=[item])]]
        self.assert_held_with_no_card()

    def test_a_card_declaring_files_only_in_its_body_bullets_holds(self):
        self.hold_body_block("File set:\n- app/main.py\n- `app/util.py` (new)\n")

    def test_a_card_declaring_files_in_one_comma_line_holds(self):
        self.hold_body_block("Files: app/util.py, `app/main.py`\n", path="app/main.py")

    def test_a_body_line_naming_two_files_in_prose_holds_on_the_second(self):
        self.hold_body_block("Files: app/util.py and app/main.py\n")

    def test_a_stray_body_files_line_does_not_switch_off_the_touched_files(self):
        body = "Fix the hook.\n\nFiles touched: none\n"
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?", (body, self.tid))
        self.conn.commit()
        self.script["implementer"] = [[IMPL_FU]]
        with mock.patch.object(follow_ups.review_scope, "open_round_touched",
                               return_value=FOLLOW_UP["files"]):
            self.assert_held_with_no_card()

    def test_the_reviewer_sees_the_follow_ups_like_the_guide(self):
        task = {"id": "t_00000001", "title": "x", "body": "B", "file_set": None}
        prompt = bundles.reviewer(task, "", follow_ups=[FOLLOW_UP])["prompt"]
        self.assertIn(FOLLOW_UP["title"], prompt)
        self.assertNotIn("Follow-ups the implementer reported",
                         bundles.reviewer(task, "")["prompt"])

    def test_the_tick_hands_the_reviewer_the_reports_follow_ups(self):
        self.script["implementer"] = [[IMPL_FU]]
        self.script["reviewer"] = [[REV_PASS]]
        with mock.patch.object(bundles, "reviewer", wraps=bundles.reviewer) as spy:
            for _ in range(4):
                self.tick.once()
        self.assertEqual(spy.call_args.kwargs["follow_ups"], [FOLLOW_UP])

    def test_two_items_naming_the_same_files_file_two_cards(self):
        other = dict(FOLLOW_UP, title="bin/capsule-unblocked-hook.sh: a second, different defect")
        self.script["implementer"] = [[dict(IMPL_DONE, follow_ups=[FOLLOW_UP, other])]]
        for _ in range(3):
            self.tick.once()
        titles = {r[0] for r in self.conn.execute(
            "SELECT title FROM tasks WHERE title IN (?, ?)", (FOLLOW_UP["title"], other["title"]))}
        self.assertEqual(titles, {FOLLOW_UP["title"], other["title"]})

    def test_bypass_probes_all_count_as_the_cards_own_files(self):
        own = ["bin/capsule-unblocked-hook.sh"]
        for path in ("bin/*", "bin/capsule-unblocked-hook.s?", "/work/bin/x.sh", "bin", "bin/",
                     "./", "//", "x/../", " bin/capsule-unblocked-hook.sh ", ".", ""):
            self.assertTrue(follow_ups._touches(follow_ups.norm(path), own), repr(path))
        for path in ("pages/[id].tsx", "docs/a.md", "bin2/x.sh"):
            self.assertFalse(follow_ups._touches(follow_ups.norm(path), own), repr(path))

    def test_glob_forms_that_name_an_own_parent_directory_count_as_own_files(self):
        hook = "bin/capsule-unblocked-hook.sh"
        for path, own in (("b?n", hook), ("bi[n]", hook), ("s*/sr/timer.js", "static/sr"),
                          ("[^x]in/" + hook.split("/")[1], hook), ("[!x]in", hook)):
            with self.subTest(path=path):
                self.assertTrue(follow_ups._touches(follow_ups.norm(path), [own]), (path, own))

    def test_a_whole_repo_follow_up_holds(self):
        self.assert_path_form_holds("./")

    def test_a_follow_up_outside_the_cards_file_set_files_a_card_and_requests_review(self):
        self.set_file_set(["app.py"])
        self.script["implementer"] = [[IMPL_FU]]
        for _ in range(2):
            self.tick.once()
        self.assertEqual(self.task()["status"], "review")
        self.tick.once()
        self.assertEqual(len(self.cards()), 1)

    def test_with_no_declared_file_set_the_runs_touched_files_are_the_card_files(self):
        self.script["implementer"] = [[IMPL_FU]]
        with mock.patch.object(follow_ups.review_scope, "open_round_touched",
                               return_value=FOLLOW_UP["files"]):
            self.assert_held_with_no_card()

    def test_a_report_without_follow_ups_is_returned_untouched(self):
        report = dict(IMPL_DONE)
        held = follow_ups.hold_own_files(self.conn, self.cfg, self.task(), report, "x")
        self.assertIs(held, report)

    def test_the_guide_sees_the_follow_ups(self):
        section = bundles._impl_report_section(IMPL_FU)
        line = f"- {FOLLOW_UP['title']} [bin/capsule-unblocked-hook.sh]: {FOLLOW_UP['body']}"
        self.assertIn(line, section)
