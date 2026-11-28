"""D225 slice 3: two writers that pick the same next number must both land, as distinct files.
Oracle 1: the files on disk (count, distinct names, every text present). Oracle 2: a simulated
collision, where another writer takes the number between the scan and the link."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from capsule import human, row_schemas, spool

BOARD = "b"
TID = "t_0123abcd"


def with_provenance_fields():
    schema = json.loads(json.dumps(row_schemas.load("intervention.v1")))
    schema["properties"]["via"] = {"enum": ["terminal", "slack"]}
    schema["properties"]["ask"] = {"type": "string", "pattern": "^[0-9a-f]{16}$"}
    return mock.patch.object(row_schemas, "load", return_value=schema)


def without_provenance_fields():
    """The schema as it was before via/ask landed: the writer must leave them out."""
    schema = json.loads(json.dumps(row_schemas.load("intervention.v1")))
    schema["properties"].pop("via", None)
    schema["properties"].pop("ask", None)
    return mock.patch.object(row_schemas, "load", return_value=schema)


class HumanConcurrent(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.d = spool.human_dir(self.home, BOARD)

    def files(self):
        return sorted(p for p in self.d.iterdir())

    def test_threads_racing_on_the_same_number_all_land(self):
        real_next = human._next_name
        gate = threading.Barrier(4)

        seen = set()

        def synced(d):
            name = real_next(d)    # every writer's first scan happens before any links
            if threading.get_ident() not in seen:
                seen.add(threading.get_ident())
                gate.wait(timeout=5)
            return name

        self.d.mkdir(parents=True)
        out = []
        with mock.patch.object(human, "_next_name", synced):
            threads = [threading.Thread(target=lambda i=i: out.append(
                human.write(self.home, BOARD, TID, "unblock", f"go {i}", now=1_700_000_000)))
                for i in range(4)]
            [t.start() for t in threads]
            [t.join() for t in threads]
        self.assertEqual(len(set(out)), 4)
        self.assertEqual(sorted(json.loads(p.read_text())["text"] for p in self.files()),
                         [f"go {i}" for i in range(4)])

    def test_a_number_taken_between_scan_and_link_moves_to_the_next(self):
        self.d.mkdir(parents=True)
        real_next = human._next_name
        calls = []

        def taken(d):
            name = real_next(d)
            if not calls:    # the other writer lands this exact number first
                calls.append(name)
                (d / name).write_text('{"other": true}')
            return name

        with mock.patch.object(human, "_next_name", taken):
            final = human.write(self.home, BOARD, TID, "unblock", "mine", now=1_700_000_000)
        self.assertNotEqual(final.name, calls[0])
        self.assertEqual(json.loads((self.d / calls[0]).read_text()), {"other": True})
        self.assertEqual(json.loads(final.read_text())["text"], "mine")

    def test_no_temp_left_and_existing_shape_unchanged(self):
        first = human.write(self.home, BOARD, TID, "unblock", "go", now=5)
        second = human.write(self.home, BOARD, TID, "unblock", "go again", now=6)
        self.assertEqual((first.name, second.name), ("1.json", "2.json"))
        self.assertEqual(json.loads(first.read_text()),
                         {"task_id": TID, "kind": "unblock", "text": "go", "at": 5,
                          "author": "owner"})
        self.assertEqual([p.name for p in self.files()], ["1.json", "2.json"])

    def test_via_and_ask_are_written_only_when_the_schema_has_them(self):
        key = "ab" * 8
        with without_provenance_fields():
            plain = human.write(self.home, BOARD, TID, "unblock", "x", now=1, via="slack", ask=key)
        self.assertNotIn("via", json.loads(plain.read_text()))
        self.assertNotIn("ask", json.loads(plain.read_text()))
        with with_provenance_fields():
            rich = human.write(self.home, BOARD, TID, "unblock", "y", now=2, via="slack", ask=key)
            bare = human.write(self.home, BOARD, TID, "unblock", "z", now=3)
        doc = json.loads(rich.read_text())
        self.assertEqual((doc["via"], doc["ask"]), ("slack", key))
        self.assertNotIn("via", json.loads(bare.read_text()))


if __name__ == "__main__":
    unittest.main()
