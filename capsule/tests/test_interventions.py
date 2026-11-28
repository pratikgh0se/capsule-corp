"""Row t_5a8cb343 (row 509 Zenkai Z7: new logic in new modules, tests alongside): every human
intervention file becomes one `intervention` event; `accept`/`unblock` stamp it with an explicit
`outcome`, `applied` or `refused:<reason>` (`capsule/interventions.py`), never a silent no-op,
and the whole event+verb+stamp sequence is one transaction (a crash anywhere in it leaves nothing
committed).

Split out of capsule/tests/test_tick.py (moved, not duplicated) so that file does not grow.
"""
import json
from unittest import mock

from capsule import blocks, db, human, spool
from capsule.tests.private_names import personal_names
from capsule.tests.test_tick import BRANCH, IMPL_DONE, REV_FAIL, TickCase


class Interventions(TickCase):
    def test_tick_records_and_archives_human_files(self):
        """R5: each human spool file becomes one `intervention` event and moves to human/done/."""
        hdir = spool.human_dir(self.home, "test")
        human.write(self.home, "test", self.tid, "unblock", "answered the pause question", now=1)
        self.runner.up = False
        self.tick.once()
        human.write(self.home, "test", self.tid, "nudge", "second one", now=2)
        self.tick.once()
        self.assertEqual([p for p in hdir.iterdir() if p.is_file()], [])
        self.assertEqual(len(list((hdir / "done").iterdir())), 2)
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'"
                                 " ORDER BY id", (self.tid,)).fetchall()
        self.assertEqual([json.loads(r[0])["kind"] for r in rows], ["unblock", "nudge"])
        self.assertEqual(json.loads(rows[0][0])["text"], "answered the pause question")

    def test_write_refuses_a_blank_park_trigger(self):
        """Round 2, r1 (minor, residual): `human.write`'s own write-time guard (B2, defense in
        depth beside `blocks.park`'s own) had no oracle - a mutation disabling it (`if False:`)
        survived the whole suite. `blocks.park` still can't be jammed by a blank trigger either
        way, but the CLI's immediate refusal is a separate, earlier failure mode worth its own
        test: no file should ever reach the spool for the tick to see at all."""
        hdir = spool.human_dir(self.home, "test")
        with self.assertRaises(ValueError):
            human.write(self.home, "test", self.tid, "park", "   ")
        self.assertEqual([p for p in hdir.iterdir() if p.is_file()], [])

    def test_a_reprocessed_human_file_records_only_one_event(self):
        """R28 (final-fix-A re-review): a crash between the event commit and the archive move
        leaves the file in place, and the next tick sees it again. That must record at most
        one `intervention` event (and run the kind's side effect at most once), keyed on the
        file's own on-disk name."""
        hdir = spool.human_dir(self.home, "test")
        human.write(self.home, "test", self.tid, "unblock", "answered", now=1)
        self.runner.up = False
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        self.tick.once()
        moved = next((hdir / "done").iterdir())
        # Simulate the crash window: the event committed, but the archive move never happened
        # (or a fresh copy of the same-named file lands back in human/ before the next tick).
        moved.rename(hdir / moved.name)
        self.tick.once()
        rows = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'",
                                 (self.tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][0])["src"], moved.name)
        self.assertEqual([p for p in hdir.iterdir() if p.is_file()], [])
        self.assertEqual(len(list((hdir / "done").iterdir())), 1)

    def test_a_crash_between_the_verb_and_the_stamp_rolls_back_the_whole_intervention(self):
        """Fix round 1, F4: the event, the verb and the outcome stamp are ONE transaction - a
        crash anywhere inside it leaves nothing committed, so the human file is never archived
        half-done and a clean retry applies (and stamps) cleanly."""
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "unblock", "go", now=1)
        self.runner.up = False
        hdir = spool.human_dir(self.home, "test")
        with mock.patch.object(blocks, "unblock", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.tick.once()
        self.assertEqual(self.conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='intervention'",
            (self.tid,)).fetchone()[0], 0)
        self.assertEqual(len([p for p in hdir.iterdir() if p.is_file()]), 1)   # never archived
        self.assertEqual(self.task()["status"], "blocked")                    # unchanged
        self.tick.once()   # a clean retry applies and stamps in the same pass
        self.assertEqual(self.task()["status"], "ready")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["outcome"], "applied")

    def test_a_reset_file_releases_a_blocked_card(self):
        """Pilot prep: the operator releases a held card through the human spool, not by SQL."""
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='capability', failure_count=2"
                          " WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "reset", "retry task 7 on Capsule", now=1)
        self.runner.up = False
        self.assertEqual(self.tick.once()["interventions"], 1)
        row = self.task()
        self.assertEqual((row["status"], row["failure_count"], row["block_kind"]), ("ready", 0, None))

    def test_a_migration_reset_releases_the_card_and_keeps_its_tag(self):
        """D6: the tagged reset releases the card like any reset; the event carries the tag."""
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "reset", "pilot start", now=1, tag="migration-reset")
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        payload = self.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'",
                                    (self.tid,)).fetchone()[0]
        self.assertEqual(json.loads(payload)["tag"], "migration-reset")

    def test_a_human_unblock_file_on_a_done_card_notifies_and_archives(self):
        """Fix round 2, item 1: pins `tick.py:416`'s `by="human"` wiring - the refused verb still
        reaches the owner through one outbox row naming the status, and the file is archived either
        way. Flipping `tick.py:416` to `by="worker"` must make this fail (no outbox row)."""
        hdir = spool.human_dir(self.home, "test")
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "unblock", "try again", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual([p for p in hdir.iterdir() if p.is_file()], [])
        self.assertEqual(len(list((hdir / "done").iterdir())), 1)
        rows = self.conn.execute("SELECT body FROM outbox WHERE thread_key=?", (self.tid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertIn("done", rows[0][0])

    def test_an_unblock_file_lands_in_todo_behind_an_open_parent(self):
        parent = db.new_task(self.conn, title="p", status="todo")
        self.conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?,?)", (parent, self.tid))
        self.conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "unblock", "go", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "todo")

    def test_unreadable_human_file_is_archived_not_retried(self):
        hdir = spool.human_dir(self.home, "test")
        hdir.mkdir(parents=True)
        (hdir / "1.json").write_text("{not json")
        self.runner.up = False
        self.assertEqual(self.tick.once()["tick_errors"], 0)
        self.assertTrue((hdir / "done" / "1.json").exists())
        self.assertFalse((hdir / "1.json").exists())


    def test_an_unblock_answer_reaches_the_rerun_implementer(self):
        """R3 (row 445): the human answer is in the re-dispatched implementer's prompt."""
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=["golden rows: defer?"])], [IMPL_DONE]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "unblock", "Defer the golden rows to an intake card.", now=1790000000)
        self.tick.once()
        second = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(second["run_id"]) / "job.json").read_text())
        self.assertEqual(job["inputs"]["interventions"],
                         [{"kind": "unblock", "text": "Defer the golden rows to an intake card.",
                           "at": 1790000000, "author": "owner"}])
        prompt = json.loads((self.spool_dir(second["run_id"]) / "bundle.json").read_text())["prompt"]
        self.assertIn("## Human answers", prompt)
        self.assertIn("Defer the golden rows to an intake card.", prompt)
        first = self.runs("implementer")[0]
        first_prompt = json.loads((self.spool_dir(first["run_id"]) / "bundle.json").read_text())["prompt"]
        self.assertNotIn("## Human answers", first_prompt)

    def test_a_cos_note_reaches_the_rerun_as_the_cos_and_not_as_owner(self):
        """inc_22b42704: each human entry names its author, a missing author reads as owner."""
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=["which way?"])], [IMPL_DONE]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "unblock", "COS-NOTE-1", author="cos",
                    now=1790000000)
        human.write(self.home, "test", self.tid, "unblock", "OWNER-NOTE-2", now=1790000001)
        self.tick.once()
        run = self.runs("implementer")[1]
        job = json.loads((self.spool_dir(run["run_id"]) / "job.json").read_text())
        self.assertEqual([e["author"] for e in job["inputs"]["interventions"]], ["cos", "owner"])
        prompt = json.loads((self.spool_dir(run["run_id"]) / "bundle.json").read_text())["prompt"]
        lines = [x for x in prompt.splitlines() if "NOTE-" in x]
        self.assertIn("unblock by cos: COS-NOTE-1", lines[0])
        self.assertIn("unblock by owner: OWNER-NOTE-2", lines[1])
        self.assertIn("a `cos` entry is the chief of staff's note, not the owner's ruling", prompt)
        self.assertIn("does not override a spec", prompt)
        for name in personal_names():
            self.assertNotIn(f"{name}'s interventions on this card", prompt.lower())

    def _last_outcome(self):
        return json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'"
            " ORDER BY id DESC LIMIT 1", (self.tid,)).fetchone()[0])["outcome"]

    def test_an_answer_file_on_a_needs_input_block_releases_the_card(self):
        """inc_0709c333: an `answer` on a needs_input block releases the card, like `unblock`."""
        with db.txn(self.conn):
            blocks.block(self.conn, self.tid, kind="needs_input",
                         reason="left undone / issues: run the host steps")
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "answer", "HOSTSTEPS-DONE-TEXT",
                    author="cos", now=1790000000)
        self.tick.once()
        self.assertNotEqual(self.task()["status"], "blocked")
        self.assertEqual(self._last_outcome(), "applied")
        run = self.runs("implementer")[-1]
        prompt = json.loads((self.spool_dir(run["run_id"]) / "bundle.json").read_text())["prompt"]
        self.assertIn("HOSTSTEPS-DONE-TEXT", prompt)

    def test_an_answer_file_on_a_ready_card_is_noted_not_applied(self):
        self.runner.up = False
        human.write(self.home, "test", self.tid, "answer", "just a note", author="cos", now=1)
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.assertEqual(self._last_outcome(), "noted")

    def test_a_reset_on_a_closed_done_card_is_refused_not_stamped_applied(self):
        """t_d2e9707c: the card stays done, so the file must not read `applied`."""
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "reset", "closed by mistake", now=1)
        self.tick.once()
        self.assertEqual(self.task()["status"], "done")
        self.assertTrue(self._last_outcome().startswith("refused:the card is closed done"))
        kinds = [r[0] for r in self.conn.execute(
            "SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("failure_reset", kinds)
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertEqual(len(log), 1)
        self.assertNotIn("screen-fix", log[0])

    def test_an_answer_on_a_spec_stale_block_is_refused_like_unblock(self):
        self.runner.up = False
        with db.txn(self.conn):
            blocks.block(self.conn, self.tid, kind="spec_stale", reason="spec moved")
        human.write(self.home, "test", self.tid, "answer", "go on", author="cos", now=1)
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        self.assertEqual(self._last_outcome(), "refused:spec_stale: accept|reset only")

    def test_accept_moves_a_held_done_card_to_review_without_a_rerun(self):
        """R4 (row 445): `accept` on a card held after a `status: done` report goes straight to review."""
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=["golden rows stay deferred"])]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "accept", "Accepted: rows stay deferred.", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "review")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id",
                                                 (self.tid,))]
        self.assertEqual(kinds[kinds.index("intervention") + 1], "request_review")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["kind"], "accept")
        self.assertEqual(payload["outcome"], "applied")   # t_5a8cb343 AC3
        self.runner.up = True
        for _ in range(4):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(len(self.runs("implementer")), 1)        # no rerun
        self.assertEqual(sorted(r["profile"] for r in self.runs()), ["guide", "implementer", "reviewer", "validator"])

    def test_accept_leaves_a_triage_card_of_another_kind_alone(self):
        self.conn.execute("UPDATE tasks SET status='triage', block_kind='capability' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "accept", "no", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "triage")

    def test_a_human_reset_clears_the_block_recurrences(self):
        """R5: `human reset` on a block-looped card zeroes block_recurrences (and the kind)."""
        self.conn.execute("UPDATE tasks SET status='triage', block_kind='needs_input', block_recurrences=2,"
                          " failure_count=1 WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "reset", "fix the finding", now=1)
        self.runner.up = False
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["block_recurrences"], row["failure_count"]),
                         ("ready", None, 0, 0))

    def test_accept_is_refused_unless_the_last_run_reported_done(self):
        """R4: a card that is not held, or whose last run did not report done, stays put;
        the owner hears why."""
        human.write(self.home, "test", self.tid, "accept", "too early", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='needs_input' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "accept", "no run yet", now=2)
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertEqual(len(log), 2)
        self.assertTrue(all("accept refused" in line and self.tid in line for line in log))
        payloads = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention' ORDER BY id", (self.tid,))]
        self.assertTrue(all(p["outcome"].startswith("refused:") for p in payloads))   # t_5a8cb343 AC3

    def test_an_unblock_outcome_is_applied_on_a_blocked_card(self):
        """AC3: `unblock` on an ordinary blocked (non-triage) card writes `outcome: applied` on
        its own `intervention` event."""
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "unblock", "capacity opened up", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "ready")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["outcome"], "applied")

    def test_an_unblock_outcome_is_refused_with_the_same_reason_blocks_recorded(self):
        """AC3: a refused `unblock` (here: a parked card, reset-only) still gets its
        `intervention` event, with `outcome` naming the exact reason `blocks.unblock` itself
        recorded in its own `unblock_refused` event - never a bare 'no' with no trail."""
        human.write(self.home, "test", self.tid, "park", "waiting on the vendor's API key", now=1)
        self.runner.up = False
        self.tick.once()
        human.write(self.home, "test", self.tid, "unblock", "let it through", now=2)
        self.tick.once()
        refused_reason = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'", (self.tid,)).fetchone()[0])
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention' ORDER BY id DESC LIMIT 1",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["outcome"], f"refused:{refused_reason['reason']}")

    def test_a_stale_unblock_refused_event_is_never_mistaken_for_this_calls_reason(self):
        """Round 3 review, R3: `interventions.stamp_outcome`'s refusal lookup is scoped to
        `id > intervention_id` - an OLDER, unrelated `unblock_refused` event already on the
        task must never be picked up as if it were THIS call's own reason. Reproduced with a
        `blocks.unblock` that refuses WITHOUT writing its own event (the one case where,
        without the id-scoping, the query would silently fall back to the stale row instead of
        the correct generic default) - dropping `AND id > ?` makes this test fail."""
        with db.txn(self.conn):
            db.event(self.conn, self.tid, "unblock_refused", {"reason": "an unrelated, much older refusal"}, now=1)
        self.conn.execute("UPDATE tasks SET status='blocked', block_kind='capability' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "unblock", "go", now=2)
        self.runner.up = False
        with mock.patch.object(blocks, "unblock", return_value=False):
            self.tick.once()
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention' ORDER BY id DESC LIMIT 1",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["outcome"], "refused:refused")   # generic default, never the stale reason

    def test_accept_on_a_needs_input_triage_card_applies_and_stamps_the_event(self):
        """Row 447 R5 + AC3: on a needs_input triage card (the block-loop park), `accept`
        applies - status moves to review - and its own `intervention` event carries
        `outcome: applied`, not just a bare event with no verdict. Row 511: a HUMAN unblock now
        resets `block_recurrences`, so the second hold needed to reach `triage` (recurrence 2)
        is released with a WORKER unblock here (`blocks.unblock(..., by="worker")`, the same
        release path a re-dispatch uses) instead."""
        held = dict(IMPL_DONE, left_undone=["golden rows stay deferred"])
        self.script["implementer"] = [[held], [held]]
        self.tick.once()
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("blocked", "needs_input"))
        blocks.unblock(self.conn, self.tid, by="worker")
        self.tick.once()
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("triage", "needs_input"))
        human.write(self.home, "test", self.tid, "accept", "Accepted: rows stay deferred.", now=2)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "review")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention' ORDER BY id DESC LIMIT 1",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload["kind"], "accept")
        self.assertEqual(payload["outcome"], "applied")

    def test_a_second_hold_after_accept_and_a_rerun_is_fresh_not_a_loop(self):
        """Z2's second oracle beside the unit-level `blocks.accept`/`blocks.block` calls in
        test_blocks.py: `accept` resets `block_recurrences`, so a second left_undone hold after a
        review failure/rerun is a fresh first strike, not a loop - it lands back in `blocked` at
        recurrence 1, never `triage`."""
        held = dict(IMPL_DONE, left_undone=["golden rows stay deferred"])
        self.script["implementer"] = [[held], [held]]
        self.script["reviewer"] = [[REV_FAIL]]
        self.tick.once()
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_kind"], self.task()["block_recurrences"]),
                         ("blocked", "needs_input", 1))
        human.write(self.home, "test", self.tid, "accept", "Accepted: rows stay deferred.", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual((self.task()["status"], self.task()["block_recurrences"]), ("review", 0))
        self.runner.up = True
        for _ in range(6):
            self.tick.once()
            row = self.task()
            if row["status"] == "blocked" and row["block_recurrences"] == 1:
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["block_recurrences"]),
                         ("blocked", "needs_input", 1))
        self.assertEqual(len(self.runs("implementer")), 2)   # the review failure did rerun it

    def test_an_epic_file_reassigns_a_declared_branch(self):
        """t_91c331db: the tick's intervention step applies `epic` through the single-writer path."""
        other = "epic/other-epic"
        self.cfg["epics"] = [BRANCH, other]
        self.write_cfg()
        human.write(self.home, "test", self.tid, "epic", other, now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["branch_name"], other)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='epic_assigned'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload, {"branch": other})

    def test_an_epic_file_naming_an_undeclared_branch_is_refused(self):
        human.write(self.home, "test", self.tid, "epic", "epic/not-declared", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["branch_name"], BRANCH)   # unchanged
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='epic_refused'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["branch"], "epic/not-declared")
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertTrue(any("epic refused" in line and self.tid in line for line in log))

    def test_an_epic_file_on_a_running_card_is_refused(self):
        other = "epic/other-epic"
        self.cfg["epics"] = [BRANCH, other]
        self.write_cfg()
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "epic", other, now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["branch_name"], BRANCH)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='epic_refused'", (self.tid,)).fetchone()[0])
        self.assertIn("running", payload["reason"])

    def test_a_park_file_holds_the_card_with_a_trigger(self):
        """t_91c331db: status blocked, block_kind parked, event `parked {trigger}`, outbox notice."""
        human.write(self.home, "test", self.tid, "park", "waiting on the vendor's API key", now=1)
        self.runner.up = False
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='parked_by_human'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(payload, {"by": "human", "trigger": "waiting on the vendor's API key"})
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertTrue(any("waiting on the vendor's API key" in line for line in log))

    def test_a_park_file_on_a_running_card_is_refused(self):
        self.conn.execute("UPDATE tasks SET status='running' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "park", "trigger", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "running")
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='park_refused'", (self.tid,)).fetchone()[0])
        self.assertIn("running", payload["reason"])
        # round 2, r4: the unit test (test_blocks.py) already covers the notice itself; this is
        # its second oracle at tick level - the outbox flush actually wrote the line to disk.
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertTrue(any("park refused" in line and self.tid in line for line in log))

    def test_reset_lifts_a_parked_card_through_the_tick(self):
        human.write(self.home, "test", self.tid, "park", "waiting on the vendor's API key", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "reset", "vendor key arrived", now=2)
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("ready", None))

    def test_a_park_file_on_a_done_card_is_refused_through_the_tick(self):
        """B1: reproduced through the tick, not just the unit call - done -> park -> reset must
        never leave the card `ready` (dispatchable again)."""
        self.conn.execute("UPDATE tasks SET status='done' WHERE id=?", (self.tid,))
        human.write(self.home, "test", self.tid, "park", "trigger", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "done")
        human.write(self.home, "test", self.tid, "reset", "no-op", now=2)
        self.tick.once()
        self.assertEqual(self.task()["status"], "done")   # never revived

    def test_close_marks_a_parked_card_done_and_promotes_its_child(self):
        """A parked card closes to done with the reason and author on record; its
        todo child becomes ready once `promote_ready` runs."""
        child = db.new_task(self.conn, title="child", branch=BRANCH, status="todo",
                            parents=(self.tid,), now=2)
        self.conn.execute(
            "UPDATE tasks SET status='blocked', block_kind='parked' WHERE id=?",
            (self.tid,))
        self.assertEqual(db.open_parent(self.conn, child), self.tid)
        human.write(self.home, "test", self.tid, "close", "landed on main",
                    now=1, author="cos")
        self.runner.up = False
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("done", None))
        self.assertIsNotNone(row["completed_at"])
        self.assertIsNone(db.open_parent(self.conn, child))
        self.assertEqual(self.conn.execute(
            "SELECT status FROM tasks WHERE id=?", (child,)).fetchone()[0], "ready")
        intervention = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='intervention'",
            (self.tid,)).fetchone()[0])
        self.assertEqual((intervention["text"], intervention["author"]),
                         ("landed on main", "cos"))
        closed = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='closed'",
            (self.tid,)).fetchone()[0])
        self.assertEqual(closed["was"], "blocked")

    def test_close_on_a_running_or_review_card_is_refused(self):
        for status in ("running", "review"):
            self.conn.execute("UPDATE tasks SET status=? WHERE id=?",
                              (status, self.tid))
            human.write(self.home, "test", self.tid, "close", "landed", now=1)
            self.runner.up = False
            self.tick.once()
            self.assertEqual(self.task()["status"], status)
        reasons = [json.loads(r[0])["reason"] for r in self.conn.execute(
            "SELECT payload FROM task_events "
            "WHERE task_id=? AND kind='close_refused' ORDER BY id", (self.tid,))]
        self.assertEqual(len(reasons), 2)
        self.assertIn("running", reasons[0])
        self.assertIn("review", reasons[1])

    def test_write_refuses_a_blank_close_reason(self):
        with self.assertRaises(ValueError):
            human.write(self.home, "test", self.tid, "close", "  ")

    def test_a_text_less_park_file_is_archived_and_a_queued_reset_still_applies(self):
        """B2: a raw file with no `text` field (bypassing `human.write`'s own guard - e.g. an
        older writer, or a hand-edited file) used to raise inside the intervention transaction,
        rolling it back: the poison file was never archived and everything queued behind it
        (here, a `reset`) never applied either. Two oracles: the poison file's own archival/event,
        and the queued reset's own effect on the card."""
        hdir = spool.human_dir(self.home, "test")
        hdir.mkdir(parents=True, exist_ok=True)
        (hdir / "1.json").write_text(json.dumps({"task_id": self.tid, "kind": "park", "at": 1}))
        human.write(self.home, "test", self.tid, "reset", "queued behind the poison file", now=2)
        self.runner.up = False
        self.tick.once()
        self.assertEqual([p for p in hdir.iterdir() if p.is_file()], [])   # both archived
        self.assertEqual(len(list((hdir / "done").iterdir())), 2)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='park_refused'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["reason"], "park needs a non-empty trigger")
        self.assertEqual(self.task()["status"], "ready")   # the queued reset still applied

    def test_unblock_refuses_a_parked_card_through_the_tick(self):
        """B4: `unblock` must not lift a human park and must not leave `block_kind='parked'`
        behind on the card (reproduced before the fix: unblock gave status=ready, block_kind
        still 'parked', a stale/contradictory row)."""
        human.write(self.home, "test", self.tid, "park", "waiting on the vendor's API key", now=1)
        self.runner.up = False
        self.tick.once()
        self.assertEqual(self.task()["status"], "blocked")
        human.write(self.home, "test", self.tid, "unblock", "let it through", now=2)
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "parked"))
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='unblock_refused'"
            " ORDER BY id DESC LIMIT 1", (self.tid,)).fetchone()[0])
        self.assertEqual(payload, {"reason": "parked: reset only"})
        log = (self.home / "outbox.log").read_text().splitlines()
        self.assertTrue(any("unblock refused" in line for line in log))

