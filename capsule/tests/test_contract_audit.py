"""D220 (row M111): the reviewer audits each numbered acceptance row (`capsule/contract_audit.py`).
Soft: notes only, never a refusal. The tick-level schema field needs the proposed
`review_verdict.v1` applied, so these tests stay at module and prompt level."""
import contextlib
import json
import unittest
from unittest import mock

from capsule import bundles, contract_audit
from capsule.tests.test_tick import IMPL_DONE, TickCase

BODY = ("## Goal\nx\n\n## Acceptance criteria\n1. The widget renders\n2. The widget saves\n"
        "- [ ] bullet rows are not numbered here\n\n## Notes\n1. not an acceptance row\n")


class Rows(unittest.TestCase):
    def test_numbered_rows_only_under_acceptance_heading(self):
        self.assertEqual(contract_audit.rows(BODY)[2], "The widget saves")
        self.assertNotIn("not an acceptance row", contract_audit.rows(BODY).values())

    def test_checkbox_rows_take_their_place(self):
        body = "## Done when\n- [ ] first\n- [x] second\n"
        self.assertEqual(contract_audit.rows(body), {1: "first", 2: "second"})

    def test_no_heading_no_rows(self):
        self.assertEqual(contract_audit.rows("just prose\n1. thing"), {})


    def test_sub_bullets_are_not_rows(self):
        body = "## Acceptance criteria\n1. top\n    - nested\n2. next\n"
        self.assertEqual(contract_audit.rows(body), {1: "top", 2: "next"})

    def test_fenced_hash_line_keeps_the_section(self):
        body = "## Acceptance criteria\n1. a\n```\n# not a heading\n2. in code\n```\n2. b\n"
        self.assertEqual(contract_audit.rows(body), {1: "a", 2: "b"})

    def test_repeated_number_falls_back_to_position(self):
        body = "## Acceptance criteria\n1. a\n1. b\n1. c\n"
        self.assertEqual(contract_audit.rows(body), {1: "a", 2: "b", 3: "c"})


class Prompt(unittest.TestCase):
    def setUp(self):
        fields = ["contract_audit"]
        patch = mock.patch.object(contract_audit, "_schema_fields", lambda name: fields)
        patch.start()
        self.addCleanup(patch.stop)

    def test_section_waits_for_the_schema_field(self):
        with mock.patch.object(contract_audit, "_schema_fields", lambda name: ["verdict"]):
            self.assertEqual(contract_audit.section(BODY), "")
        self.assertIn("## Audit the contract", contract_audit.section(BODY))

    def test_reviewer_prompt_lists_rows_and_instructs(self):
        text = bundles.reviewer({"id": "t_00000000", "title": "t", "body": BODY}, "")["prompt"]
        self.assertIn("## Audit the contract", text)
        self.assertIn("2. The widget saves", text)
        self.assertIn("contract_audit", text)

    def test_no_rows_no_section(self):
        text = bundles.reviewer({"id": "t_00000000", "title": "t", "body": "prose"}, "")["prompt"]
        self.assertNotIn("Audit the contract", text)


class Soft(unittest.TestCase):
    def entries(self, doc, role="reviewer", body=BODY):
        """The `notes` entries `note` would write, with the DB calls stubbed."""
        calls = []
        run = {"id": 1, "run_id": "r"}
        with mock.patch.object(contract_audit, "event", lambda *a, **k: calls.append(a[3])), \
                mock.patch.object(contract_audit, "txn", lambda c: contextlib.nullcontext()):
            contract_audit.note(None, "t_00000000", run, role, doc, body, 0)
        return [e for c in calls for e in c["entries"]]

    def test_missing_row_noted(self):
        audit = [{"row": 1, "met": "yes", "evidence": "a"}]
        got = self.entries({"verdict": "fail", "contract_audit": audit})
        self.assertEqual(len(got), 1)
        self.assertIn("2, 3", got[0])

    def test_pass_with_no_row_noted(self):
        audit = [{"row": n, "met": "no" if n == 2 else "yes", "evidence": "a"}
                 for n in (1, 2, 3)]
        self.assertEqual(len(self.entries({"verdict": "pass", "contract_audit": audit})), 1)
        self.assertEqual(self.entries({"verdict": "fail", "contract_audit": audit}), [])

    def test_complete_audit_quiet(self):
        audit = [{"row": n, "met": "yes", "evidence": "a"} for n in (1, 2, 3)]
        self.assertEqual(self.entries({"verdict": "pass", "contract_audit": audit}), [])

    def test_no_audit_at_all_is_noted_not_refused(self):
        self.assertEqual(len(self.entries({"verdict": "pass"})), 1)

    def test_other_roles_and_rowless_cards_quiet(self):
        self.assertEqual(self.entries({"verdict": "pass"}, role="security"), [])
        self.assertEqual(self.entries({"verdict": "pass"}, body="prose"), [])


class TickCloses(TickCase):
    """D220 at the tick call site: a verdict with no `contract_audit` on a card with acceptance
    rows gets one soft `notes` event; the review still completes."""

    def test_missing_audit_notes_once_and_run_completes(self):
        self.conn.execute("UPDATE tasks SET body=? WHERE id=?",
                          ("## Acceptance criteria\n1. renders\n2. saves\n", self.tid))
        self.conn.commit()
        self.script["implementer"] = [[IMPL_DONE]]
        for _ in range(4):
            self.tick.once()
        entries = [json.loads(r[0])["entries"] for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='notes'", (self.tid,))]
        self.assertEqual(len([e for e in entries if any("does not cover" in x for x in e)]), 1)
        outcomes = [r[0] for r in self.conn.execute(
            "SELECT outcome FROM task_runs WHERE task_id=? AND profile='reviewer'", (self.tid,))]
        self.assertEqual(outcomes, ["completed"])


if __name__ == "__main__":
    unittest.main()
