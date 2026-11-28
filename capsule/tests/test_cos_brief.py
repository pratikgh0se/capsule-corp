"""`python3 -m capsule cos brief` (row 637, slice 4): health and open items from the status
screen, the asks still open, and "answered while you were away" with its `.seen` marker.
Boards are temp DBs built with the repo's own schema helper; asks/done records are fakes."""
import hashlib
import io
import json
import re
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from capsule import __main__ as cli
from capsule import cos_brief, db

BOARD = "capsule-corp"
ID_RE = re.compile(r"t_[0-9a-f]{8}|inc_|python3 -m|" + re.escape(str(Path.home())))
ANSWER = {"title": "Move the Slack routing live?", "task_id": "t_0123abcd", "choice": "yes",
          "answered_by": "owner", "via": "button", "answered_at": 1_800_000_000}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        d = self.home / "boards" / BOARD
        d.mkdir(parents=True)
        db.create_board(d / "kanban.db")
        self.conn = db.connect(d / "kanban.db")
        self.addCleanup(self.conn.close)
        self.asks = d / "asks"

    def card(self, title="Slack routing"):
        tid = db.new_task(self.conn, title=title, assignee="dev-backend", status="blocked",
                          now=int(time.time()) - 3600)
        self.conn.execute("UPDATE tasks SET block_kind='needs_input' WHERE id=?", (tid,))
        return tid

    def intervention(self, tid, kind, at, outcome):
        """An `intervention` event stamped the way `interventions.stamp_outcome` stamps it."""
        db.event(self.conn, tid, "intervention", {"kind": kind, "at": at}, now=at)
        self.conn.execute("UPDATE task_events SET payload=json_set(payload, '$.outcome', ?)"
                          " WHERE id=last_insert_rowid()", (outcome,))

    def answer(self, key="k1", **extra):
        done = self.asks / "done"
        done.mkdir(parents=True, exist_ok=True)
        (done / f"{key}.json").write_text(json.dumps({**ANSWER, **extra}))
        return done / f"{key}.seen"

    def brief(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = cli.main(["cos", "brief", "--capsule-home", str(self.home)])
        self.assertEqual(code, 0)
        return out.getvalue()


class Brief(Fixture):
    def test_fresh_run_prints_health_open_items_and_the_away_section(self):
        self.card("Slack routing")
        self.asks.mkdir()
        (self.asks / "k2.json").write_text(json.dumps({"title": "Reset the retry card?"}))
        self.answer(note="ship it Monday")
        text = self.brief()
        self.assertIn("🔴 capsule-corp (down)", text)                    # board health
        self.assertIn("🙋 NEEDS YOU (1)", text)
        self.assertIn("Slack routing #1 · A question is waiting for your answer.", text)
        self.assertIn("📨 ASKS WAITING FOR AN ANSWER (1)", text)
        self.assertIn("Reset the retry card? · capsule-corp", text)
        self.assertIn("📬 ANSWERED WHILE YOU WERE AWAY (1)", text)
        self.assertIn("Move the Slack routing live? · you chose yes (button)", text)
        self.assertIn("saved as a note: ship it Monday", text)
        self.assertFalse(ID_RE.search(text))

    def test_marker_is_written_only_after_the_text_is_displayed(self):
        marker = self.answer()
        seen_at_write = []

        class Out(io.StringIO):
            def write(self, s):
                seen_at_write.append(marker.exists())
                return super().write(s)

        out = Out()
        cos_brief.run(type("A", (), {"capsule_home": str(self.home)})(), out)
        self.assertIn("you chose yes", out.getvalue())
        self.assertEqual(seen_at_write, [False])
        self.assertTrue(marker.exists())

    def test_a_night_answer_shows_once_the_next_morning_then_not_again(self):
        self.answer()
        self.assertIn("ANSWERED WHILE YOU WERE AWAY (1)", self.brief())
        self.assertNotIn("ANSWERED WHILE YOU WERE AWAY", self.brief())
        self.answer("k2", title="Park the my-app card?", choice="no")
        again = self.brief()
        self.assertIn("Park the my-app card? · you chose no", again)
        self.assertNotIn("Move the Slack routing live?", again)

    def tree(self):
        """Every file under the home by content hash. SQLite's `-shm` is a shared-memory index
        every open connection rewrites; it holds no board data, so it is left out (the db and
        its `-wal` are hashed)."""
        return {str(p.relative_to(self.home)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(self.home.rglob("*"))
                if p.is_file() and not p.name.endswith("-shm")}

    def test_a_brief_changes_nothing_but_new_seen_markers(self):
        """Read-only on every board: the whole home, hashed before and after, differs only by the
        `.seen` marker of the answer it showed."""
        self.card()
        self.asks.mkdir()
        (self.asks / "k2.json").write_text(json.dumps({"title": "Reset the retry card?"}))
        marker = self.answer()
        self.conn.commit()
        before = self.tree()
        self.assertIn("ANSWERED WHILE YOU WERE AWAY (1)", self.brief())
        after = self.tree()
        self.assertEqual(set(after) - set(before), {str(marker.relative_to(self.home))})
        self.assertEqual([k for k in before if after[k] != before[k]], [])    # nothing else touched

    def test_missing_asks_directories_are_fine(self):
        self.assertFalse(self.asks.exists())
        text = self.brief()
        self.assertIn("no asks waiting", text)
        self.assertNotIn("ANSWERED WHILE YOU WERE AWAY", text)

    def test_the_answer_line_says_what_the_tick_did_with_it(self):
        tid = self.card()
        self.answer(task_id=tid)
        self.intervention(tid, "unblock", ANSWER["answered_at"] + 5, "applied")
        self.assertIn("· then: done", self.brief())
        self.answer("k2", task_id=tid, answered_at=ANSWER["answered_at"] + 10)
        self.intervention(tid, "reset", ANSWER["answered_at"] + 15, "refused:epic missing")
        self.assertIn("· then: refused, epic missing", self.brief())

    def test_unapplied_and_closed_records_get_their_own_wording(self):
        self.answer()
        self.answer("k2", title="Old question", choice=None, closed="the card moved on")
        text = self.brief()
        self.assertIn("· then: not applied yet", text)
        self.assertIn("Old question · closed without an answer (the card moved on)", text)


if __name__ == "__main__":
    unittest.main()
