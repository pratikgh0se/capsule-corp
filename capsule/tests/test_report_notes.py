"""Row t_5a8cb343 (row 509 Zenkai Z7: new logic in new modules, tests alongside): the implementer's
own `notes` field (`capsule/report_notes.py`) never blocks, does not depend on the card declaring
a file_set, and the sample-app-board evidence (events 4832/4840/4849/4853) still blocks under the
field it actually used - this fix is forward-looking, not a retroactive rescue.

Split out of capsule/tests/test_tick.py (moved, not duplicated) so that file does not grow.
"""
import json
import unittest

from capsule import tick
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_FAIL, REV_PASS, VAL_PASS, TickCase


class InformationalLeftUndone(TickCase):
    """Row t_5a8cb343 fix round 1: `left_undone`/`issues` are always blocking when non-empty,
    unconditionally, never a heuristic guess (F1/F2 killed the regex classifier - it fired on
    fewer than half the real evidence and false-positived on genuine unfinished work in a live
    probe). Deliberate informational text belongs in the report's own optional `notes` array
    instead (schema-checked, `report.v1`), which `_route` never reads. None of this depends on
    the card declaring a `file_set` (`NotesWithNoFileSet` below proves it)."""

    def test_left_undone_always_blocks_however_it_reads(self):
        """A left_undone entry that reads exactly like the old regex's 'resolved' language still
        blocks - the heuristic is gone, the field's meaning is unconditional again."""
        note = ("Nothing left undone within this task's scope. Restored the original bytes"
               " (pre-existing behavior, unrelated to this change).")
        self.script["implementer"] = [[dict(IMPL_DONE, issues=[note])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("notes", kinds)

    def test_notes_alone_goes_to_review(self):
        """A report with empty left_undone/issues but a populated `notes` field reaches review
        exactly like a clean report - `notes` is reported (one `notes` event) but never read by
        the router."""
        note = "Restored a pre-existing, unrelated screenshot PNG the suite always regenerates."
        self.script["implementer"] = [[dict(IMPL_DONE, notes=[note])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual(row["status"], "review")
        self.assertIsNone(row["block_kind"])
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("blocked", kinds)
        self.assertIn("notes", kinds)
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='notes'", (self.tid,)).fetchone()[0])
        self.assertEqual(payload["entries"], [note])

    def test_a_mixed_report_blocks_on_left_undone_and_still_notes_the_rest(self):
        """`notes` and a genuine `left_undone` gap coexist: the round holds on the gap, and the
        `notes` text is still recorded, separately, never folded into the block reason."""
        note = "Restored a pre-existing, unrelated screenshot PNG."
        gap = "tests/test_server.py still needs a timeout assertion"
        self.script["implementer"] = [[dict(IMPL_DONE, notes=[note], left_undone=[gap])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))
        reason = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (self.tid,)).fetchone()[0])["reason"]
        self.assertIn(gap, reason)
        self.assertNotIn(note, reason)
        notes = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='notes'", (self.tid,)).fetchone()[0])
        self.assertEqual(notes["entries"], [note])


class NotesWithNoFileSet(TickCase):
    def test_notes_alone_still_goes_to_review_with_no_file_set_declared(self):
        """The `notes` field's meaning does not depend on the card declaring a file_set at all -
        unlike the old regex gate, which required one (t_5a8cb343 fix round 1, F1: only 1 of 101
        real cards ever declared a file_set)."""
        self.assertFalse(json.loads(self.task()["file_set"] or "[]"))   # this card declares none
        self.script["implementer"] = [[dict(IMPL_DONE, notes=["informational, nothing to do"])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual(row["status"], "review")
        self.assertIsNone(row["block_kind"])


class ReplaySampleAppEvidenceIsForwardLooking(TickCase):
    """Row t_5a8cb343 fix round 1 (F1): events 4832/4840/4849/4853 all wrote their informational
    text into `left_undone`/`issues`, the field that always blocks, both before and after this
    fix. Replayed here through the real field they used, they still block - this fix does not
    retroactively rescue old-style reports. What removes the loop going forward is dropping the
    false-positive-prone regex guess (it matched fewer than half of these anyway, per the
    review's own live-data probe) in favor of the new, unambiguous `notes` field
    (`contracts/implementer.md`): a future report that means to say "nothing left to fix" says
    so in `notes`, never leaves it for a heuristic to infer from `left_undone`/`issues`."""

    def _still_blocks(self, note, *, as_field):
        self.script["implementer"] = [[dict(IMPL_DONE, **{as_field: [note]})]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["block_kind"], "needs_input", note)

    def test_event_4832_text_still_blocks_under_the_old_field(self):
        self._still_blocks(
            "Running the suite regenerates 3 committed screenshot PNGs under"
            " review/soft-tick-toggle-v3/ as a side effect (pre-existing behavior of this test"
            " file, unrelated to this change). Restored the original bytes via git show"
            " HEAD:<path>.", as_field="issues")

    def test_event_4840_text_still_blocks_under_the_old_field(self):
        self._still_blocks(
            "The card names 'the two tests that cannot fail' but tests/test_smoke.py has, per"
            " its git history, only ever held the single 1+1 test.", as_field="left_undone")

    def test_event_4849_text_still_blocks_under_the_old_field(self):
        self._still_blocks(
            "tests/ui/test_settings_page.py:40 still has time.sleep(1.0); left file-local,"
            " matching this card's declared scope of tests/ui/test_soft_tick_toggle.py only.",
            as_field="left_undone")

    def test_event_4853_text_still_blocks_under_the_old_field(self):
        self._still_blocks(
            "Nothing left undone within this task's scope (tests/test_smoke.py,"
            " tests/test_server.py); tests/ui/test_soft_tick_toggle.py and the review/*.png"
            " files show as modified in git status.", as_field="left_undone")


class RouteUnitTest(unittest.TestCase):
    """`Tick._route` (card t_8463feac) as a plain unit test, no tick loop: `notes` never decides
    routing, `left_undone`/`issues` alone do, unconditionally - the second oracle beside the
    tick-path replay below (Zenkai Z2)."""

    def test_route_ignores_notes_and_reads_only_left_undone_issues(self):
        base = dict(IMPL_DONE)
        self.assertEqual(tick.Tick._route(dict(base, notes=["The BrokenPipeError tracebacks "
                          "printed during test runs are normal http.server teardown noise."])), "review")
        self.assertEqual(tick.Tick._route(dict(base, left_undone=["a real gap"])), "hold")
        self.assertEqual(tick.Tick._route(dict(base, issues=["a real gap"])), "hold")


class LeftUndone2InformationalTextGoesInNotes(TickCase):
    """Card t_8463feac: two 2026-09-28 sample-app runs blocked `needs_input` on purely
    informational text a worker put in `issues`/`left_undone` instead of `notes` (task_events
    5079, 5086, runs 26986d1b1fd341bb851d50500a778b7b and 847bbca4b17f48c380fe80e06e22ae3e).
    `Tick._route` is unchanged and stays correct (t_5a8cb343: no heuristic reclassification,
    ever - `RouteUnitTest` above proves it reads only `left_undone`/`issues`); the fix is
    delivery, not routing: the classification guidance now renders into the worker's prompt via
    `contracts/implementer.md`'s `reports:` field, and `capsule/bundles.py`'s
    `_findings_section` (fix round 1) splits a settled/no-change review finding into `notes`
    instead of always routing it to the blocking `issues`. The `_still_blocks_under_the_wrong_field`
    tests prove the two real reports would
    still block today, replayed exactly as filed - no silent behavior change; the
    `_goes_to_review_once_placed_in_notes` tests prove the SAME text, correctly filed, does
    not."""

    BROKENPIPE = ("The BrokenPipeError tracebacks printed during test runs (server.py:619 "
                 "wfile.write on client-closed sockets) are normal http.server teardown noise, "
                 "not test failures - every test still reports 'ok'.")
    PLANDOC = ("The plan doc's stale test-count line "
              "(docs/superpowers/plans/2026-09-14-screens-epic.md:180) is intentionally left "
              "untouched per row 491/4769 (DECISION plan-doc-edit-outside-file-set: REVERT; "
              "main ea24f89 already amended the Files line and will supersede at merge).")
    NOTING_ONLY = ("line 220 (design_fidelity): sampling the ticking class immediately after "
                  "each #clock text change narrows phase coverage vs the old 700ms-offset "
                  "sampling. Reviewer flagged this as inherent to the card's mandate for "
                  "explicit conditions, not requesting a change - noting only, no action taken.")

    def _goes_to_review(self, notes):
        self.script["implementer"] = [[dict(IMPL_DONE, notes=notes)]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("review", None))

    def test_event_5079_issues_text_still_blocks_under_the_wrong_field(self):
        """run 26986d1b1fd341bb851d50500a778b7b's report/1.json `issues`, replayed exactly as
        filed: still blocks today - the router did not change, only the contract's guidance."""
        self.script["implementer"] = [[dict(IMPL_DONE, issues=[self.BROKENPIPE, self.PLANDOC])]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["block_kind"], "needs_input")

    def test_event_5079_text_goes_to_review_once_placed_in_notes(self):
        self._goes_to_review([self.BROKENPIPE, self.PLANDOC])

    def test_event_5086_left_undone_text_still_blocks_under_the_wrong_field(self):
        """run 847bbca4b17f48c380fe80e06e22ae3e's report/1.json `left_undone`, replayed exactly
        as filed: still blocks today."""
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=[self.NOTING_ONLY])]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["block_kind"], "needs_input")

    def test_event_5086_text_goes_to_review_once_placed_in_notes(self):
        self._goes_to_review([self.NOTING_ONLY])

    def test_a_genuine_gap_still_blocks_alongside_correctly_filed_notes(self):
        """`notes` is never a way to launder an actual `left_undone` item past the router
        (contracts/intent/implementer.jsonl `done-with-left-undone`: any non-empty
        `left_undone` still holds, row 445)."""
        gap = "tests/test_server.py still needs a timeout assertion"
        self.script["implementer"] = [[dict(IMPL_DONE, notes=[self.BROKENPIPE], left_undone=[gap])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))
        reason = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
            (self.tid,)).fetchone()[0])["reason"]
        self.assertIn(gap, reason)

    def test_adversarial_mixed_ruling_and_gap_row_still_blocks(self):
        """Round-1 re-check R2: replays contracts/intent/implementer.jsonl's adversarial row
        `left-undone-mixed-ruling-and-real-gap-still-blocks` through the real tick path, not
        just the sha256 pin - one entry that reads partly like a settled note ("per row 491's
        ruling") but also names a real, not-yet-done gap in the same sentence must still
        block."""
        mixed = ("Line 180 was left per row 491's ruling; however tests/test_smoke.py still "
                 "needs a docstring update for the new behavior - not yet written.")
        self.script["implementer"] = [[dict(IMPL_DONE, left_undone=[mixed])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))


class LeftUndone2BundleRewordingReachesTheWorker(TickCase):
    """Card t_8463feac fix round 1 (F2, F3): bundles.py's `_findings_section` is what actually
    reaches a worker's prompt - `contracts/implementer.md` never renders its own text into the
    system prompt at all (see test_contracts.py, F1). This runs the real tick path (not a
    direct `bundles.implementer()` call) so a mutant that reverts the findings-section sentence
    to the old, unconditional "goes in `issues`" wording goes RED here too, through the second
    round's real rendered bundle.json on disk."""

    def test_the_rerun_bundle_carries_the_notes_split(self):
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.script["reviewer"] = [[REV_FAIL], [REV_PASS]]
        self.script["validator"] = [[VAL_PASS], [VAL_PASS]]
        self.script["guide"] = [[GUIDE_OK], [GUIDE_OK]]
        for _ in range(4):
            self.tick.once()
            if len(self.runs("implementer")) == 2:
                break
        _, second = self.runs("implementer")
        prompt = json.loads((self.spool_dir(second["run_id"]) / "bundle.json").read_text())["prompt"]
        section = prompt.split("## Review findings to fix", 1)[1]
        self.assertIn("a cited row or ruling already settles", section)
        # Round-1 re-check R1: tie each destination to its case (a swap mutant must go RED).
        self.assertIn("not requesting a change, goes in `notes`", section)
        self.assertIn("no such citation, goes in `issues`", section)


class HostOnlyField(TickCase):
    """W191 (t_866d0145): `host_only` lists checks the sandbox cannot run; it never blocks, rides
    the `notes` event for the reviewer and lander, and `left_undone` still blocks."""

    def test_host_only_alone_goes_to_review_and_is_noted(self):
        item = "No network in the sandbox: the live fetch check was not run."
        self.script["implementer"] = [[dict(IMPL_DONE, host_only=[item])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("review", None))
        sql = "SELECT payload FROM task_events WHERE task_id=? AND kind='notes'"
        payload = json.loads(self.conn.execute(sql, (self.tid,)).fetchone()[0])
        self.assertEqual(payload["entries"], ["host-only: " + item])

    def test_left_undone_still_blocks_beside_host_only(self):
        report = dict(IMPL_DONE, host_only=["no docker here"], left_undone=["a real gap"])
        self.script["implementer"] = [[report]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("blocked", "needs_input"))

    def test_reviewer_bundle_shows_host_only_items(self):
        from capsule import bundles
        task = {"id": "t", "title": "t", "body": ""}
        prompt = bundles.reviewer(task, "d", host_only=["no docker here"])["prompt"]
        self.assertIn("- no docker here", prompt)
        self.assertNotIn("Host-only", bundles.reviewer(task, "d")["prompt"])

    def test_tick_hands_host_only_to_the_reviewer_run(self):
        self.script["implementer"] = [[dict(IMPL_DONE, host_only=["no docker here"])]]
        for _ in range(4):
            self.tick.once()
        rev = self.runs("reviewer")[0]
        bundle = json.loads((self.spool_dir(rev["run_id"]) / "bundle.json").read_text())
        self.assertIn("- no docker here", bundle["prompt"])

    def test_implementer_brief_files_a_precheck_that_cannot_run_under_host_only(self):
        """inc_06bc9511: outbox.log 1793530743 filed a precheck that could not run under
        `issues`, which always holds. The brief routes it to `host_only`; the router stays."""
        from capsule import bundles
        prompt = bundles.implementer({"id": "t_0000aaaa", "title": "t", "body": "b"})["prompt"]
        self.assertIn("put the command and its exact error in `host_only`, never in `issues`",
                      prompt)
        self.assertNotIn("exact error in `issues`", prompt)
        self.assertIn("`left_undone` holds only work the card requires and you did not do", prompt)
        self.assertIn("Fix every finding first", prompt)
        text = ("bin/precheck-gates.py did not run: the container could not open the replaced"
                " test file. Run it on the host.")
        self.script["implementer"] = [[dict(IMPL_DONE, host_only=[text])]]
        self.tick.once()
        self.tick.once()
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"]), ("review", None))

    def test_the_implementer_and_guide_briefs_say_the_validator_runs_the_full_suite(self):
        """inc_a0206824: a skipped full-suite run reached a human as a hold. Both briefs
        carry one rule: the validator and the lander run the full suite."""
        from capsule import bundles
        task = {"id": "t_0000aaaa", "title": "t", "body": "b"}
        implementer_prompt = bundles.implementer(task)["prompt"]
        guide_prompt = bundles.guide(task, "", [])["prompt"]
        for prompt in (implementer_prompt, guide_prompt):
            self.assertIn("The validator and the lander run the full suite.", prompt)
            self.assertIn("A skipped full-suite run goes in `host_only`,"
                          " never in `left_undone` or `issues`.", prompt)
            self.assertIn("A failing test you saw stays in `issues`.", prompt)
        self.assertIn("not drift, even when the card names `make test`", guide_prompt)

    def test_the_same_precheck_text_under_issues_still_blocks(self):
        text = ("bin/precheck-gates.py did not run: the container could not open the replaced"
                " test file. Run it on the host.")
        self.assertEqual(tick.Tick._route(dict(IMPL_DONE, issues=[text])), "hold")
        self.script["implementer"] = [[dict(IMPL_DONE, issues=[text])]]
        self.tick.once()
        self.tick.once()
        self.assertEqual(self.task()["block_kind"], "needs_input")

    def test_route_and_schema(self):
        from capsule import paths
        from capsule.jsonschema import load_all, validate
        schema = load_all(paths.SCHEMAS)["report.v1"]
        doc = dict(IMPL_DONE, run_id="0" * 32, task_id="t_00000000", claim_lock="x")
        self.assertEqual(tick.Tick._route(dict(doc, host_only=["x"])), "review")
        self.assertEqual(validate(dict(doc, host_only=["x"]), schema), [])
        self.assertNotEqual(validate(dict(doc, host_onl=["x"]), schema), [])
        self.assertNotEqual(validate(dict(doc, host_only="x"), schema), [])
