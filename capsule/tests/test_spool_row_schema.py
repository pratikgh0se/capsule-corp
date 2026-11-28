"""Row 569 item 3: the Lookout spool-line schemas (`spool_snap.v1`, `spool_event.v1`,
`spool_tickmark.v1` under `contracts/schemas/rows/`), checked at CI time only: the runtime stays
tolerant (`lookout._payload`, the shipper's skip of a bad line), this test is the loud half.
Oracle 1: every line the real `lookout.Collector` writes over a board with cards, runs, a block,
a completed commit, an incident and ten passes (so a health event) validates. Oracle 2: hand-broken
copies of those same lines are rejected."""
import copy
import json
import shutil
import tempfile
import time
import unittest
from pathlib import Path

from capsule import blocks, db, intent, lookout, paths, row_schemas, tick

LINE_SCHEMAS = {"snap": "spool_snap.v1", "event": "spool_event.v1", "tickmark": "spool_tickmark.v1"}


def line_problems(line):
    schema = LINE_SCHEMAS.get(line.get("t")) if isinstance(line, dict) else None
    return [f"unknown spool line type {line.get('t')!r}" if isinstance(line, dict) else "not an object"] \
        if schema is None else row_schemas.problems(schema, line)


class SpoolLines(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.home = Path(tempfile.mkdtemp())
        board_db = paths.board_db("my-app", cls.home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        cfg = {"board": "my-app", "epics": ["epic/screens"],
               "repo_url": "https://github.com/owner/my-app.git",
               "caps": {"tick_seconds": 30}}
        tid = db.new_task(conn, title="timer, \"quoted\"\nline é", branch="epic/screens")
        other = db.new_task(conn, title="second", branch="epic/screens")
        with db.txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
            conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, run_id, runner, model)"
                         " VALUES (?,?,?,?,?,?,?)", (other, "implementer", "running", 1, "r_live", "host", "m"))
        blocks.block(conn, other, kind="needs_input", reason="need a name")
        db.event(conn, tid, "completed", {"commit": "c0ffee1", "gates": {"validator": "pass", "rounds": 1}})
        with db.txn(conn):
            conn.execute("INSERT INTO incidents (id, board, task_id, kind, opened_at, fingerprint) VALUES"
                         " ('inc_1','my-app',?,'crash',5,'fp')", (tid,))
        collector = lookout.Collector(cls.home, "my-app", host="h", pid=1)
        counts = dict.fromkeys(tick.COUNTS, 0)
        for _ in range(10):                       # the tenth pass also ships a health event
            collector.collect(conn, cfg, time.time(), counts, pass_ms=5)
        conn.close()
        cls.lines = [json.loads(l) for l in lookout.spool_path(cls.home).read_text(encoding="utf-8").splitlines()]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def of(self, kind, **where):
        return [l for l in self.lines if l["t"] == kind and all(l.get(k) == v for k, v in where.items())]

    def test_the_fixture_exercises_every_line_type_and_every_snap_table_the_collector_writes(self):
        self.assertEqual({l["t"] for l in self.lines}, {"snap", "event", "tickmark"})
        self.assertEqual({l["table"] for l in self.of("snap")},
                         {"cap_cards", "cap_runs", "cap_blocks", "cap_epics", "cap_commits", "cap_incidents"})
        self.assertEqual({l["row"]["kind"] for l in self.of("event")}, {"card", "health"})

    def test_every_line_the_real_collector_wrote_validates(self):
        for n, line in enumerate(self.lines):
            with self.subTest(n=n, t=line["t"], table=line.get("table")):
                self.assertEqual(line_problems(line), [])

    def test_hand_broken_copies_of_real_lines_are_rejected(self):
        snap, event = self.of("snap")[0], self.of("event", )[0]
        tickmark = self.of("tickmark")[0]

        def broken(line, **change):
            out = copy.deepcopy(line)
            for key, value in change.items():
                target = out["row"] if key.startswith("row_") else out
                key = key[4:] if key.startswith("row_") else key
                if value is KeyError:
                    del target[key]
                else:
                    target[key] = value
            return out
        cases = {
            "snap without pass_id": broken(snap, pass_id=KeyError),
            "snap with an unknown table": broken(snap, table="cap_cardz"),
            "snap with a naive timestamp": broken(snap, snap_ts="2026-10-07T10:00:00"),
            "snap whose row is a list": broken(snap, row=[1]),
            "snap with version 2": broken(snap, v=2),
            "snap with an unknown top-level key": broken(snap, extra=1),
            "event with an unknown kind": broken(event, row_kind="alert"),
            "event with a bad severity": broken(event, row_severity="fatal"),
            "event with a short event_id": broken(event, row_event_id="abc"),
            "event without its payload": broken(event, row_payload=KeyError),
            "event with a string payload": broken(event, row_payload="x"),
            "tickmark without counts": broken(tickmark, row_counts=KeyError),
            "tickmark with a string pid": broken(tickmark, row_pid="1"),
            "tickmark with an unexpected row key": broken(tickmark, row_extra=1),
        }
        for label, line in cases.items():
            with self.subTest(label):
                self.assertTrue(line_problems(line), label)

    def test_an_unknown_line_type_is_not_a_spool_line(self):
        self.assertTrue(line_problems({"v": 1, "t": "snapshot"}))
        self.assertTrue(line_problems([1]))


class IntentExamples(unittest.TestCase):
    """Z1: `contracts/intent/lookout_spool_schema.jsonl`, each example run through the real schema."""

    def test_every_example_matches_what_the_schema_does(self):
        examples = intent.load("lookout_spool_schema")
        self.assertTrue(1 <= len(examples) <= 3)
        for ex in examples:
            doc = ex["given"]["document"]
            self.assertEqual(line_problems(doc["line"]) == [], ex["expect"]["valid"], ex["id"])


class SpecificViolations(unittest.TestCase):
    def test_the_message_names_the_field_and_the_rule(self):
        line = {"v": 1, "t": "snap", "board": "b", "snap_ts": "2026-10-07T10:00:00+00:00", "pass_id": "h:1:1",
                "table": "cap_cards", "row": {}}
        self.assertEqual(line_problems(line), [])
        self.assertEqual(line_problems(dict(line, table="cap_cardz")),
                         [f"#/table: must be one of {row_schemas.load('spool_snap.v1')['properties']['table']['enum']}"])
        self.assertEqual(line_problems(dict(line, v=2)), ["#/v: must equal 1"])
