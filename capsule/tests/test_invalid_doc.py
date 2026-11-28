"""Row 507(b) fix (card t_67a95702): the invalid_document retry-once-then-park shape.

Split out of capsule/tests/test_tick.py (Zenkai Z7, the size gate - moving this class out was
enough on its own to bring test_tick.py back under its frozen file_length). Shares TickCase and
the report fixtures from test_tick.py rather than redefining them.
"""
import json

from capsule import blocks, spool
from capsule.tests.test_tick import GUIDE_OK, IMPL_DONE, REV_PASS, TickCase


def _bad_decision(run, label_len=301):
    return {"schema": "decision.v1", "run_id": run["run_id"], "task_id": run["task_id"],
            "claim_lock": run["claim_lock"], "question": "which approach?",
            "options": [{"id": "a", "label": "x" * label_len}, {"id": "b", "label": "short"}],
            "recommended": "a"}


class InvalidDocumentRetry(TickCase):
    """Row 507(b) fix (card t_67a95702; evidence t_de1466d6 run 378, 2026-09-27 20:21): a
    decision.v1 with an option label over the schema's 300-char cap used to null the whole run's
    report and burn a full `blocks.record_failure` against the card, even though the report itself
    (a valid `report.v1`) was fine - Zenkai Z2, a side document's schema slip is not a second
    oracle on the work. Two oracles per check here: the DB event trail
    (`invalid_document`/`review_retry`) and the card's own state (`failure_count`, `status`,
    `block_kind`)."""

    def test_a_single_invalid_document_retries_the_implementer_not_the_failure_count(self):
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        calls = {"n": 0}

        def hook(job, sp, run):
            calls["n"] += 1
            if calls["n"] == 1:
                spool.write_doc(sp, "requests", _bad_decision(run))
        self.hooks["implementer"] = hook
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["failure_count"], 0)
        impl_runs = self.runs("implementer")
        self.assertEqual(len(impl_runs), 2)
        self.assertEqual(impl_runs[0]["outcome"], "invalid_document")
        self.assertEqual(impl_runs[0]["error_class"], "invalid_output")
        self.assertEqual(impl_runs[1]["outcome"], "completed")
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertIn("invalid_document", kinds)
        self.assertIn("review_retry", kinds)
        self.assertNotIn("parked", kinds)
        self.assertNotIn("failed", kinds)

    def test_the_retry_bundle_carries_the_exact_error_and_json_path(self):
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        seen = []

        def hook(job, sp, run):
            seen.append(job["inputs"].get("invalid_document_error"))
            if len(seen) == 1:
                spool.write_doc(sp, "requests", _bad_decision(run))
        self.hooks["implementer"] = hook
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertIsNone(seen[0])
        self.assertIsNotNone(seen[1])
        self.assertIn("#/options/0/label", seen[1])
        self.assertIn("300", seen[1])
        bundle = json.loads((self.spool_dir(self.runs("implementer")[1]["run_id"]) / "bundle.json").read_text())
        self.assertIn("#/options/0/label", bundle["prompt"])
        self.assertIn("Fix your last document first", bundle["prompt"])

    def test_two_invalid_documents_in_a_row_park_invalid_document_never_the_failure_count(self):
        """F2 (fix round 1): the implementer park uses its OWN block kind `invalid_document`, never
        `review_infra` - that name means a review role, and this run never reported `status: done`,
        so `blocks.accept` would refuse it right back (the loop the coordinator flagged)."""
        self.script["implementer"] = [[IMPL_DONE], [IMPL_DONE]]
        self.hooks["implementer"] = lambda job, sp, run: spool.write_doc(sp, "requests", _bad_decision(run))
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["failure_count"]), ("blocked", "invalid_document", 0))
        impl_runs = self.runs("implementer")
        self.assertEqual(len(impl_runs), 2)
        self.assertTrue(all(r["outcome"] == "invalid_document" for r in impl_runs))
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("parked", kinds)
        self.assertNotIn("failed", kinds)
        inc = self.conn.execute("SELECT kind FROM incidents WHERE task_id=?", (self.tid,)).fetchall()
        self.assertEqual([r[0] for r in inc], ["invalid_document"])
        # F2: the notice offers unblock|reset, never accept (accept would just refuse: the run's
        # handoff was never set to a `status: done` report)
        log = (self.home / "outbox.log").read_text()
        self.assertIn("unblock|reset", log)
        self.assertNotIn("accept", log)
        self.assertFalse(blocks.accept(self.conn, self.tid))

    def test_a_malformed_review_report_twice_stays_silent_failure_not_invalid_document(self):
        """F1 (fix round 1): `_verdict`'s `infra_cause` used to key on `error_class ==
        'invalid_output'`, which `_close` also sets as its GENERIC fallback for any run with no
        valid report at all (a missing report, or - this case - a schema-invalid one). A guide
        report with a stray `to` field, twice, is a malformed REPORT, never a bad requests/
        document (no `invalid_document` event fires at all); it must keep the broader
        `silent_failure` Korin classification, not be silently reclassified as `invalid_document`."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["guide"] = [[dict(GUIDE_OK, to="Owner")], [dict(GUIDE_OK, to="Owner")]]
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["failure_count"]), ("blocked", "review_infra", 0))
        guide_runs = self.runs("guide")
        self.assertEqual(len(guide_runs), 2)
        self.assertTrue(all(r["outcome"] != "invalid_document" for r in guide_runs))
        # oracle 1: no invalid_document event at all (a malformed report is not a bad requests/ doc)
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertNotIn("invalid_document", kinds)
        # oracle 2: the incident stays the broader, pre-existing classification
        inc = self.conn.execute("SELECT kind FROM incidents WHERE task_id=?", (self.tid,)).fetchall()
        self.assertEqual([r[0] for r in inc], ["silent_failure"])

    def test_unblock_after_a_park_resets_the_in_a_row_counter(self):
        """F3 (fix round 1, tightened in fix round 2 - it was vacuous: the hook only failed runs
        1-2, so the post-unblock run was always clean and never actually exercised the counter,
        which the old "previous run of this role" query would have passed too). The second-miss
        check is keyed on THIS run's own `resumed_from` pointing at a run whose outcome was
        `invalid_document` - never "the previous run of this role", which would misread a fresh
        claim after a human release as still being the second miss. Runs 1-3 all miss: run 2 (the
        retry of run 1) is the second miss in a row and parks; `unblock` releases the card; run 3
        is a FRESH claim (no `resumed_from`) that also misses, so it must retry once more (run 4,
        `resumed_from` = run 3) rather than parking straight back to triage - the exact case the
        old "previous run of this role" query got wrong (still `invalid_document`, from run 2)."""
        self.script["implementer"] = [[IMPL_DONE]] * 4
        misses = {"n": 0}

        def hook(job, sp, run):
            misses["n"] += 1
            if misses["n"] <= 3:
                spool.write_doc(sp, "requests", _bad_decision(run))
        self.hooks["implementer"] = hook
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        self.assertEqual((self.task()["status"], self.task()["block_kind"]), ("blocked", "invalid_document"))
        self.assertEqual(len(self.runs("implementer")), 2)
        self.assertTrue(blocks.unblock(self.conn, self.tid, by="human"))
        self.assertEqual(self.task()["status"], "ready")
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["failure_count"], 0)
        impl_runs = self.runs("implementer")
        self.assertEqual(len(impl_runs), 4)
        self.assertEqual([r["outcome"] for r in impl_runs],
                         ["invalid_document", "invalid_document", "invalid_document", "completed"])
        self.assertIsNone(impl_runs[2]["resumed_from"])       # run 3: a fresh claim after unblock
        self.assertEqual(impl_runs[3]["resumed_from"], impl_runs[2]["run_id"])   # run 4: its retry
        # the card never went straight back to triage after run 3: only one park (before the unblock)
        parked = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=? AND"
                                                   " kind IN ('blocked', 'block_loop_detected')", (self.tid,))]
        self.assertEqual(len(parked), 1)

    def test_a_review_role_retried_on_its_own_invalid_document_gets_the_error_in_its_bundle(self):
        """The existing R2 retry (`_review_one`) already never counts a review role's invalid
        document toward `failure_count` (review runs skip `blocks.record_failure` outright); this
        checks the one gap the fix closes for review roles too: the retried run's bundle names the
        exact error and JSON path (requirement 1), reusing the same `review_retry` event shape."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_PASS], [REV_PASS]]
        seen = []

        def hook(job, sp, run):
            seen.append(job["inputs"].get("invalid_document_error"))
            if len(seen) == 1:
                spool.write_doc(sp, "requests", _bad_decision(run))
        self.hooks["reviewer"] = hook
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "done":
                break
        self.assertEqual(self.task()["status"], "done")
        self.assertEqual(self.task()["failure_count"], 0)
        self.assertIsNone(seen[0])
        self.assertIn("#/options/0/label", seen[1])
        kinds = [r[0] for r in self.conn.execute("SELECT kind FROM task_events WHERE task_id=?", (self.tid,))]
        self.assertIn("review_retry", kinds)

    def test_a_review_role_double_invalid_document_gives_review_infra_with_the_cause_tag(self):
        """F1 positive case (fix round 2): the OTHER half of F1's fix - a review role whose OWN
        requests/ document is invalid twice in a row (never a malformed report) must still reach
        `review_infra`, tagged `cause: invalid_document`, both on the block and on the Korin
        incident. Kills the hand mutant that forces `infra_cause=None` unconditionally (M3 in the
        review), which the malformed-report test (F1's other half, right above) cannot kill since
        it asserts the OPPOSITE outcome."""
        self.script["implementer"] = [[IMPL_DONE]]
        self.script["reviewer"] = [[REV_PASS], [REV_PASS]]
        self.hooks["reviewer"] = lambda job, sp, run: spool.write_doc(sp, "requests", _bad_decision(run))
        for _ in range(8):
            self.tick.once()
            if self.task()["status"] == "blocked":
                break
        row = self.task()
        self.assertEqual((row["status"], row["block_kind"], row["failure_count"]), ("blocked", "review_infra", 0))
        reviewer_runs = self.runs("reviewer")
        self.assertEqual(len(reviewer_runs), 2)
        self.assertEqual([r["outcome"] for r in reviewer_runs], ["invalid_document", "invalid_document"])
        # oracle 1: the block event itself carries the cause tag
        payload = json.loads(self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked' ORDER BY id DESC LIMIT 1",
            (self.tid,)).fetchone()[0])
        self.assertEqual((payload["kind"], payload["cause"]), ("review_infra", "invalid_document"))
        # oracle 2: the incidents table (Korin's own classification), not the event payload alone
        inc = self.conn.execute("SELECT kind FROM incidents WHERE task_id=?", (self.tid,)).fetchall()
        self.assertEqual([r[0] for r in inc], ["review_infra"])
