"""OBS-09 slice 3: lookout_ship builds events rows through the envelope; old columns unchanged.
Oracle 1: before/after on fixture rows (old columns equal, new ones named in the envelope).
Oracle 2: init.sql and migration 006 carry the same four columns the shipper writes."""
import contextlib
import io
import json
import re
import unittest

import sys

from capsule import lookout_ship, obs_envelope, paths

sys.path.insert(0, str(paths.REPO / "maintenance"))

NEW = ("event_name", "run_id", "trace_id", "span_id")
PG = paths.REPO / "observability" / "postgres"


def row(kind="card", payload=None, **over):
    base = {"v": 1, "event_id": "e1", "ts": "2026-09-27T05:00:00+00:00", "kind": kind,
            "source": "capsule", "board": "b", "task_id": "t_1", "slug": None, "title": "c",
            "text": "hello", "link": None, "severity": "warn", "payload": payload or {}}
    return dict(base, **over)


def parse(r):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        table, doc = lookout_ship.parse(json.dumps({"v": 1, "t": "event", "row": r}))
    return table, doc, err.getvalue()


class EventsEnvelope(unittest.TestCase):
    def test_old_columns_unchanged_new_ones_from_envelope(self):
        r = row(payload={"event": "claimed", "run_id": "run-1"})
        table, doc, err = parse(r)
        self.assertEqual(table, "events")
        self.assertEqual({k: v for k, v in doc.items() if k not in NEW}, r)
        self.assertEqual((doc["event_name"], doc["run_id"]), ("claimed", "run-1"))
        self.assertEqual(doc["trace_id"], obs_envelope.trace_id("run-1"))
        self.assertRegex(doc["span_id"], "^[0-9a-f]{16}$")
        self.assertEqual(err, "")

    def test_non_card_kind_uses_the_name_table_and_has_no_trace(self):
        _, doc, err = parse(row(kind="tick_health", severity="info"))
        self.assertEqual((doc["event_name"], doc["run_id"], doc["trace_id"]),
                         ("tick_health", None, None))
        self.assertEqual(err, "")

    def test_malformed_record_is_flagged_and_still_ships_old_columns(self):
        r = row(kind="mystery")
        _, doc, err = parse(r)
        self.assertIn("lookout ship: envelope: e1", err)
        self.assertEqual([doc[k] for k in NEW], [None] * 4)
        self.assertEqual({k: v for k, v in doc.items() if k not in NEW}, r)

    def test_unknown_card_event_is_flagged(self):
        _, doc, err = parse(row(payload={"event": "never_heard_of_it"}))
        self.assertIn("event.name", err)
        self.assertIsNone(doc["event_name"])

    def test_non_dict_payload_neither_raises_nor_blocks(self):
        for bad in ("a string", ["a", "list"], 7, {"event": ["unhashable"]}):
            with self.subTest(payload=bad):
                r = row(payload=bad)
                _, doc, err = parse(r)
                self.assertEqual([doc[k] for k in NEW], [None] * 4)
                self.assertEqual({k: v for k, v in doc.items() if k not in NEW}, r)
                self.assertIn("lookout ship: envelope:", err)

    def test_every_events_kind_has_a_name(self):
        import events
        self.assertEqual(set(events.KINDS) - {"card"}, set(obs_envelope.KIND_NAMES))


class Columns(unittest.TestCase):
    def test_init_sql_and_006_carry_the_shipped_columns(self):
        mig = (PG / "migrations" / "006-event-envelope-cols.sql").read_text()
        init = (PG / "init.sql").read_text()
        block = re.search(r"CREATE TABLE IF NOT EXISTS events \((.*?)\n\);", init, re.S).group(1)
        for col in NEW:
            self.assertIn(f"ADD COLUMN IF NOT EXISTS {col} text", mig)
            self.assertRegex(block, rf"\n\s+{col}\s+text")
            self.assertIn(col, lookout_ship.TABLES["events"][1])


if __name__ == "__main__":
    unittest.main()
