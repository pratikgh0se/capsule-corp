"""EVAL-10: human minutes per day, decisions per day, delegation share. Oracle 1: hand sums of
the fixture below. Oracle 2: the Q2b act count the same report already derives from the files."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, human, paths, spool, touches

SINCE = 20 * 86400
NOW = SINCE + 2 * 86400   # a two-day window
NO_VAULT = "/nonexistent-vault"
# (kind, text, author): owner 6 files, cos 3. One file has no author (legacy = owner).
FILES = [("approve", "ok", "owner"), ("accept", "ok", "owner"), ("close", "landed", "owner"),
         ("unblock", "go", "owner"), ("answer", "ios", "owner"), ("park", "later", "owner"),
         ("approve", "ok", "cos"), ("approve", "ok", "cos"), ("reset", "again", "cos")]


class HumanMinutes(unittest.TestCase):
    def board(self):
        home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        db.create_board(paths.board_db("t", home))
        conn = db.connect(paths.board_db("t", home))
        self.tid = db.new_task(conn, title="c", now=1)
        conn.commit()
        conn.close()
        return home

    def report(self, home):
        return touches.evaluate(home, "t", since=SINCE, now=NOW, vault=NO_VAULT)

    def test_mixed_authors_exact_numbers_cos_is_not_owner(self):
        home = self.board()
        for i, (kind, text, author) in enumerate(FILES):
            human.write(home, "t", self.tid, kind, text, now=SINCE + 10 + i, author=author)
        doc = self.report(home)
        # approve 2 + accept 2 + close 1 + unblock 2 + answer 3 + park (command) 2 = 12 min / 2 d
        self.assertEqual(doc["human_minutes"], 12)
        self.assertEqual(doc["human_minutes_per_day"], 6.0)
        self.assertEqual(doc["decisions_per_day"], 2.0)   # approve, accept, close, answer
        self.assertEqual((doc["owner_actions"], doc["cos_actions"]), (6, 3))
        self.assertEqual(doc["delegation_ratio"], 3 / 9)
        files = [d for ds in spool.human_by_task(home, "t", SINCE).values() for d in ds]
        not_cos = sum(d["author"] != "cos" for d in files)
        self.assertEqual(doc["owner_actions"], not_cos)   # oracle 2: the report's own reader
        self.assertIn("estimates", doc["human_minutes_note"])

    def test_legacy_file_without_author_is_owner_and_tagged_reset_is_not_counted(self):
        home = self.board()
        d = paths.board_db("t", home).parent / "human"
        d.mkdir(parents=True, exist_ok=True)
        (d / "1.json").write_text(json.dumps(
            {"task_id": self.tid, "kind": "approve", "text": "x", "at": SINCE + 5}))
        human.write(home, "t", self.tid, "reset", "forced", now=SINCE + 6, tag="migration-reset")
        doc = self.report(home)
        got = (doc["human_minutes"], doc["owner_actions"], doc["cos_actions"])
        self.assertEqual(got, (2, 1, 0))
        self.assertEqual(doc["delegation_ratio"], 0.0)

    def test_weights_come_from_board_config_and_bad_ones_are_refused(self):
        home = self.board()
        human.write(home, "t", self.tid, "answer", "ios", now=SINCE + 10)
        cfg = paths.config_path(home, "t")
        cfg.write_text(json.dumps({"human_minutes": {"answer": 10}}))
        doc = self.report(home)
        self.assertEqual(doc["human_minutes"], 10)
        self.assertEqual(doc["human_minutes_weights"]["approve"], 2)   # default kept
        for bad in ('{"human_minutes": {"bogus": 1}}', '{"human_minutes": {"answer": NaN}}',
                    '{"human_minutes": {"answer": Infinity}}', "{not json"):
            cfg.write_text(bad)
            with self.assertRaises(ValueError):
                self.report(home)

    def test_no_actions_is_zero_and_delegation_unmeasured(self):
        doc = self.report(self.board())
        self.assertEqual((doc["human_minutes_per_day"], doc["decisions_per_day"]), (0, 0))
        self.assertEqual(doc["delegation_ratio"], "unmeasured")


if __name__ == "__main__":
    unittest.main()
