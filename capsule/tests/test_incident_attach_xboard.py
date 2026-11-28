# mutation-check: targets-predate-range ff625e94 -- fixture rename only, no behaviour pinned ::
#   Xboard.test_a_local_new_card_gets_an_incident_reattached_event,
#   Xboard.test_an_unreadable_old_board_is_refused,
#   Xboard.test_missing_old_board_db_is_repointed_and_the_evidence_names_it,
#   Xboard.test_same_card_again_is_not_a_repoint
"""`incidents attach` re-points an incident whose current fix card is on another board only when
that card is no longer live there. Oracle 1: the stored `fix_card` and the evidence list read back
through `incidents.get`. Oracle 2: the other board's db is read with raw sqlite and never written
(its event count is unchanged)."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from capsule import db, incident_attach, incidents, paths

T0 = 1_700_000_000


class Xboard(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.mine = paths.board_db("my-app", self.home)
        self.old_db = paths.board_db("capsule-corp", self.home)
        for p in (self.mine, self.old_db):
            p.parent.mkdir(parents=True)
            db.create_board(p)
        self.conn = db.connect(self.mine)
        self.oldc = db.connect(self.old_db)
        self.addCleanup(self.conn.close)
        self.addCleanup(self.oldc.close)
        self.inc, _ = incidents.open_incident(
            self.conn, board="my-app", kind="crash", key="implementer: fatal: boom",
            evidence=["event:1"], now=T0)
        self.old = db.new_task(self.oldc, title="dropped fix", status="ready")
        self.new = db.new_task(self.oldc, title="enforcer", status="ready")
        self.conn.execute("UPDATE incidents SET fix_card=? WHERE id=?",
                          (f"capsule-corp:{self.old}", self.inc))

    def attach(self):
        return incident_attach.attach(self.conn, self.inc, f"capsule-corp:{self.new}",
                                      board="my-app", home=self.home)

    def third_board_card(self):
        """A new fix card on a board that is not the old card's, so the old db can be spoiled."""
        third = paths.board_db("factory", self.home)
        third.parent.mkdir(parents=True)
        db.create_board(third)
        tconn = db.connect(third)
        self.addCleanup(tconn.close)
        return f"factory:{db.new_task(tconn, title='enforcer', status='ready')}"

    def record(self):
        rec = incidents.get(self.conn, self.inc)
        return rec["fix_card"], json.loads(rec["evidence"])

    def events(self):
        return self.oldc.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]

    def test_archived_old_card_on_another_board_is_repointed(self):
        self.oldc.execute("UPDATE tasks SET status='archived' WHERE id=?", (self.old,))
        before = self.events()
        self.assertEqual(self.attach(), f"capsule-corp:{self.new}")
        card, evidence = self.record()
        self.assertEqual(card, f"capsule-corp:{self.new}")
        self.assertTrue(any(self.old in e and "archived" in e for e in evidence), evidence)
        self.assertEqual(self.events(), before)          # the other board is never written

    def test_parked_old_card_is_repointed(self):
        self.oldc.execute("UPDATE tasks SET status='blocked', block_kind='parked' WHERE id=?",
                          (self.old,))
        self.assertEqual(self.attach(), f"capsule-corp:{self.new}")

    def test_done_old_card_is_repointed(self):
        self.oldc.execute("UPDATE tasks SET status='done' WHERE id=?", (self.old,))
        self.assertEqual(self.attach(), f"capsule-corp:{self.new}")

    def test_absent_old_card_is_repointed(self):
        self.oldc.execute("DELETE FROM tasks WHERE id=?", (self.old,))
        self.assertEqual(self.attach(), f"capsule-corp:{self.new}")
        self.assertTrue(any("absent" in e for e in self.record()[1]))

    def test_live_old_card_is_still_refused_and_the_message_says_so(self):
        for status in ("ready", "running", "blocked"):
            self.oldc.execute("UPDATE tasks SET status=?, block_kind=NULL WHERE id=?",
                              (status, self.old))
            with self.assertRaisesRegex(
                    ValueError, f"keeps its card.*{self.old}.*live on board capsule-corp"):
                self.attach()
        self.assertEqual(self.record()[0], f"capsule-corp:{self.old}")

    def test_missing_old_board_db_is_repointed_and_the_evidence_names_it(self):
        self.oldc.close()
        card = self.third_board_card()
        for suffix in ("", "-wal", "-shm"):
            Path(str(self.old_db) + suffix).unlink(missing_ok=True)
        got = incident_attach.attach(self.conn, self.inc, card, board="my-app", home=self.home)
        self.assertEqual(got, card)
        evidence = self.record()[1]
        self.assertTrue(any("capsule-corp" in e and "missing" in e and self.old in e
                            for e in evidence), evidence)

    def test_an_unreadable_old_board_is_refused(self):
        self.oldc.close()
        card = self.third_board_card()
        self.old_db.write_bytes(b"not a database" * 100)
        for suffix in ("-wal", "-shm"):
            Path(str(self.old_db) + suffix).unlink(missing_ok=True)
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            incident_attach.attach(self.conn, self.inc, card, board="my-app", home=self.home)

    def test_a_local_new_card_gets_an_incident_reattached_event(self):
        self.oldc.execute("UPDATE tasks SET status='archived' WHERE id=?", (self.old,))
        local = db.new_task(self.conn, title="local fix", status="blocked")
        incident_attach.attach(self.conn, self.inc, local, board="my-app", home=self.home)
        row = self.conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind='incident_reattached'",
            (local,)).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row["payload"])["old"], f"capsule-corp:{self.old}")

    def test_same_card_again_is_not_a_repoint(self):
        self.assertEqual(incident_attach.attach(self.conn, self.inc, f"capsule-corp:{self.old}",
                                                board="my-app", home=self.home),
                         f"capsule-corp:{self.old}")
        self.assertEqual(self.record()[1], ["event:1"])


if __name__ == "__main__":
    unittest.main()
