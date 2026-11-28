"""Row 569 item 5: `contracts/schemas/rows/outbox_row.v1.json`, one row of the `outbox` table the
tick's senders read. Oracle 1: rows made by the real `outbox.notify` for EVERY message class in
`slack.CLASSES` (file and slack channel), by `outbox.add`, and after the real `outbox.flush` moved
rows to delivered, pending-with-error and dead, all validate - and a slack row's class glyph routes
back to the class it was made for (template/sender sync). Oracle 2: hand-written bad rows are
rejected."""
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, intent, outbox, row_schemas, slack

GOOD = {"id": 1, "channel": "file", "thread_key": "t_0123abcd", "body": "capsule: done", "created_at": 1790000000,
        "attempts": 0, "delivered_at": None, "last_error": None, "status": "pending"}


class RealRows(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        db.create_board(self.d / "k.db")
        self.conn = db.connect(self.d / "k.db")
        self.addCleanup(shutil.rmtree, self.d, True)
        self.addCleanup(self.conn.close)
        outbox.set_slack(self.conn, True)

    def rows(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM outbox ORDER BY id")]

    def test_every_message_class_makes_valid_file_and_slack_rows_whose_glyph_routes_back(self):
        for cls in slack.CLASSES:
            outbox.notify(self.conn, cls, f"t_0123abcd {cls} headline", fields=[("branch", "epic/x"), ("commit", "abc")],
                          reply="python3 -m capsule human b t_0123abcd unblock", thread_key="t_0123abcd", now=100)
        rows = self.rows()
        posting = [c for c in slack.CLASSES if c not in outbox.LOG_ONLY_CLASSES]
        self.assertEqual(len(rows), len(slack.CLASSES) + len(posting))
        for row in rows:
            with self.subTest(id=row["id"], channel=row["channel"]):
                self.assertEqual(row_schemas.problems("outbox_row.v1", row), [])
        slack_rows = [r for r in rows if r["channel"] == "slack"]
        self.assertEqual([slack.classify(r["body"]) for r in slack_rows], posting)

    def test_a_file_only_row_and_a_row_without_a_thread_validate(self):
        outbox.notify(self.conn, "blocked", "x blocked", file=False, now=100)   # slack row only
        outbox.add(self.conn, "file", "capsule: override granted", now=100)  # no thread_key
        rows = self.rows()
        self.assertEqual([r["channel"] for r in rows], ["slack", "file"])
        for row in rows:
            self.assertEqual(row_schemas.problems("outbox_row.v1", row), [])

    def test_rows_after_a_real_flush_delivered_failed_and_dead_all_validate(self):
        for n in range(3):
            outbox.add(self.conn, "file", f"capsule: line {n}", thread_key="t_0123abcd", now=100)

        def send(row):
            if row["body"].endswith("1"):
                raise slack.Rejected("channel_not_found")      # dies alone
            if row["body"].endswith("2"):
                raise slack.SendError("boom")                  # stays pending with last_error
        self.assertEqual(outbox.flush(self.conn, send, now=200), 1)
        rows = self.rows()
        self.assertEqual({r["status"] for r in rows}, {"delivered", "dead", "pending"})
        for row in rows:
            with self.subTest(status=row["status"]):
                self.assertEqual(row_schemas.problems("outbox_row.v1", row), [])

    def test_the_schema_channels_are_exactly_the_routers_channels(self):
        schema = row_schemas.load("outbox_row.v1")
        self.assertEqual(set(schema["properties"]["channel"]["enum"]), {"file", "slack"})
        with self.assertRaisesRegex(ValueError, "unknown outbox channel"):
            outbox.Router()({"channel": "email", "body": "x"})


class Rejections(unittest.TestCase):
    def test_a_good_row_is_valid(self):
        self.assertEqual(row_schemas.problems("outbox_row.v1", GOOD), [])

    def test_hand_written_bad_rows_are_rejected(self):
        bad = {
            "unknown channel": dict(GOOD, channel="email"),
            "empty body": dict(GOOD, body=""),
            "missing body": {k: v for k, v in GOOD.items() if k != "body"},
            "status outside the lifecycle": dict(GOOD, status="sent"),
            "negative attempts": dict(GOOD, attempts=-1),
            "attempts as text": dict(GOOD, attempts="0"),
            "created_at as text": dict(GOOD, created_at="1790000000"),
            "extra column": dict(GOOD, class_="done"),
            "body is a list": dict(GOOD, body=["x"]),
        }
        for label, row in bad.items():
            with self.subTest(label):
                self.assertTrue(row_schemas.problems("outbox_row.v1", row), label)


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/outbox_row.jsonl`, each example run through the real schema."""

    def test_every_example_matches_what_the_schema_does(self):
        examples = intent.load("outbox_row")
        self.assertTrue(1 <= len(examples) <= 3)
        for ex in examples:
            row = ex["given"]["document"]["row"]
            self.assertEqual(row_schemas.problems("outbox_row.v1", row) == [], ex["expect"]["valid"], ex["id"])


class SpecificViolations(unittest.TestCase):
    def test_the_message_names_the_field_and_the_rule(self):
        self.assertEqual(row_schemas.problems("outbox_row.v1", dict(GOOD, channel="email")),
                         ["#/channel: must be one of ['file', 'slack']"])
        self.assertEqual(row_schemas.problems("outbox_row.v1", dict(GOOD, body="")), ["#/body: shorter than 1"])
