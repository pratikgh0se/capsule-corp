"""Korin fix cards name each fact outside the repo, and the hold shows it (inc_e7e506f3)."""
import json
import tempfile
import unittest
from pathlib import Path

from capsule import bundles, db, incidents

INC = {"id": "inc_e7e506f3", "task_id": None, "fix_card": None}


def _doc(body):
    return {"fix_card": {"title": "fix", "body": body}, "root_cause": "r", "mitigation": "m",
            "enforcer": "e", "recurrence_test": "capsule.tests.test_x.X.test_y"}


class OutsideFacts(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "kanban.db"
        db.create_board(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)

    def reason(self, body, branch="epic/x"):
        fix = incidents.file_fix_card(self.conn, None, dict(INC), _doc(body), branch, "dev", 1)
        row = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (fix,)).fetchone()
        return json.loads(row[0])["reason"]

    def test_the_korin_prompt_requires_a_quoted_shape_or_a_needs_fact_line(self):
        prompt = bundles.korin({"id": "t", "title": "k", "body": ""}, {}, [], [])["prompt"]
        self.assertIn("quote its shape in the body from evidence", prompt)
        self.assertIn("Needs fact: <path>: <what the builder needs>", prompt)

    def test_the_hold_reason_lists_the_needs_fact_lines(self):
        body = ("Build it.\nNeeds fact: ~/vault/a.md: the heading\n"
                "  - Needs fact: /host/b.json: the keys  \nnot a Needs fact: line")
        reason = self.reason(body)
        self.assertTrue(reason.startswith("Korin fix card for incident inc_e7e506f3"))
        self.assertIn("\nNeeds fact: ~/vault/a.md: the heading", reason)
        self.assertIn("\nNeeds fact: /host/b.json: the keys", reason)
        self.assertNotIn("not a", reason)

    def test_a_body_with_no_fact_line_keeps_the_exact_reason(self):
        self.assertEqual(self.reason("no facts here"),
                         "Korin fix card for incident inc_e7e506f3: unblock it to dispatch")
        self.assertEqual(
            self.reason("no facts here", branch=None),
            "Korin fix card for incident inc_e7e506f3: no epic: `python3 -m capsule incidents"
            " fixcard inc_e7e506f3 --epic <branch>`, then unblock")


if __name__ == "__main__":
    unittest.main()
