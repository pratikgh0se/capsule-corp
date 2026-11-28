"""Runner meets open/close (M204): both go through brainstorm_store; thread adoption, closed
brainstorms, the session rule and the close race."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import brainstorm_store as store, db, paths
from capsule.models import Result
from capsule.models.stub import StubAdapter
from capsule.tests.test_brainstorm_run import BID, NOW, Base, ok


class Integration(Base):
    """Runner and open/close share brainstorm_store: thread adoption, closed, session, races."""

    def spy_model(self, on_run=None):
        seen = []

        class Spy:
            def run(_, role, bundle, workspace, limits):
                seen.append(limits)
                if on_run:
                    on_run()
                return ok("fine", "sess-live")
        self.model = Spy()
        return seen

    def test_no_thread_yet_waits_then_adopts_the_openers_thread(self):
        seen = self.spy_model()
        self.meta({"title": "T", "channel": "", "thread_ts": "", "status": "open"})
        self.say("hello")
        self.run_once()
        self.assertEqual((seen, self.rows(), self.pending()), ([], [], ["Ev1.json"]))
        conn = db.connect(paths.board_db("b", self.home))
        conn.execute("INSERT INTO slack_msg VALUES (?,?,?,?,'open','x',1,1,NULL)",
                     (store.OPEN_KEY.format(BID), "brainstorm", "C9", "5.5"))
        conn.close()
        self.run_once(NOW + 10)
        self.assertEqual(self.rows()[0]["thread_key"], "C9|5.5")
        self.assertEqual(store.read_meta(self.home, "b", BID)["thread_ts"], "5.5")
        self.assertEqual(self.pending(), [])

    def test_closed_brainstorm_is_untouched_pending_stays(self):
        seen = self.spy_model()
        self.meta({"channel": "C", "thread_ts": "9.9", "status": "closed"})
        self.say("late")
        self.run_once()
        self.assertEqual((seen, self.rows(), self.pending()), ([], [], ["Ev1.json"]))
        self.assertFalse((self.dir / "turns.jsonl").exists())

    def test_capsule_started_first_call_passes_the_opened_session_id_once(self):
        seen = self.spy_model()
        store.append_turn(self.home, "b", BID, "capsule", "opener")     # B3's opener turn
        self.meta({"title": "T", "channel": "C", "thread_ts": "9.9", "status": "open",
                   "session_id": "S-open", "name": "n", "started_by": "capsule"})
        self.say("one", "Ev1")
        self.run_once()
        self.say("two", "Ev2")
        self.run_once(NOW + 10)
        self.assertEqual(seen[0]["session_id"], "S-open")
        self.assertNotIn("resume", seen[0])
        self.assertEqual(seen[1]["resume"], "sess-live")
        self.assertNotIn("session_id", seen[1])
        meta = store.read_meta(self.home, "b", BID)
        self.assertEqual((meta["session_id"], meta["session_live"], meta["name"]),
                         ("sess-live", True, "n"))

    def test_live_session_is_resumed_never_recreated(self):
        seen = self.spy_model()
        self.meta({"channel": "C", "thread_ts": "9.9", "status": "open",
                   "session_id": "S1", "session_live": True})
        self.say("x")
        self.run_once()
        self.assertEqual(seen[0]["resume"], "S1")
        self.assertNotIn("session_id", seen[0])

    def test_a_close_during_the_model_call_posts_nothing_and_stays_closed(self):
        self.spy_model(lambda: store.update_meta(self.home, "b", BID, status="closed", note="N"))
        self.say("x")
        self.run_once()
        meta = store.read_meta(self.home, "b", BID)
        self.assertEqual((meta["status"], meta["note"], meta.get("session_live")),
                         ("closed", "N", None))
        self.assertEqual((self.rows(), self.pending()), ([], ["Ev1.json"]))
        self.assertFalse((self.dir / "turns.jsonl").exists())

    def test_session_already_in_use_is_retried_as_a_resume_and_goes_live(self):
        seen = []
        clash = Result(None, 1, {}, 1, "fatal", None, None, "",
                       "Error: Session ID S1 is already in use")

        class Script(StubAdapter):
            def run(self_, role, bundle, ws, limits):
                seen.append(limits)
                return super().run(role, bundle, ws, limits)

        self.model = Script([clash, ok("hello again")])
        self.meta({"channel": "C", "thread_ts": "9.9", "status": "open", "session_id": "S1"})
        self.say("x")
        self.run_once()
        self.assertEqual(["session_id" in d for d in seen], [True, False])
        self.assertEqual(seen[1]["resume"], "S1")
        self.assertTrue(store.read_meta(self.home, "b", BID)["session_live"])
        self.assertEqual(len(self.rows()), 1)


class Publish(Base):
    def setUp(self):
        super().setUp()
        self.vault = Path(tempfile.mkdtemp())
        self.pushed = []
        pusher = mock.patch.dict("sys.modules", {"vault_git": mock.Mock(
            push=lambda *a, **k: self.pushed.append(a[1]))})
        pusher.start()
        self.addCleanup(pusher.stop)
        vp = mock.patch.dict(os.environ, {"OBSIDIAN_VAULT_PATH": str(self.vault)})
        vp.start()
        self.addCleanup(vp.stop)

    def test_a_closed_thread_without_a_note_is_published_once_by_the_runner(self):
        self.meta({"title": "T", "channel": "C", "thread_ts": "9.9", "status": "open",
                   "started_at": NOW, "session_id": "S1", "name": "n", "started_by": "owner"})
        self.assertTrue(store.mark_closed(self.home, "b", BID, NOW + 5))
        self.assertFalse(store.mark_closed(self.home, "b", BID, NOW + 9))     # second done: no-op
        self.run_once()
        note = store.read_meta(self.home, "b", BID)["note"]
        self.assertTrue((self.vault / note).is_file())
        self.run_once(NOW + 60)
        self.assertEqual(len(self.pushed), 1)       # published once


class Store(Base):
    def test_list_ids_pending_and_clear(self):
        (self.dir.parent / "_scratch").mkdir()
        (self.dir.parent / "nothex").mkdir()
        self.assertEqual(store.list_ids(self.home, "b"), [BID])
        self.assertEqual(store.list_ids(self.home, "nope"), [])
        self.say("late", "EvB")
        self.say("early", "EvA")
        (self.dir / "pending" / "junk.json").write_text("{")
        (self.dir / "pending" / "noid.json").write_text("{}")
        self.assertEqual([d["event_id"] for d in store.list_pending(self.home, "b", BID)],
                         ["EvB", "EvA"])
        store.clear_pending(self.home, "b", BID, ["EvB", "../meta", "missing"])
        self.assertEqual(self.pending(), ["EvA.json", "junk.json", "noid.json"])
        self.assertTrue((self.dir / "meta.json").exists())

    def test_append_turn_keeps_extra_keys(self):
        turn = store.append_turn(self.home, "b", BID, "capsule", "hi", ts=5, n=2, fixed="daily")
        self.assertEqual(store.read_turns(self.home, "b", BID), [turn])
        self.assertEqual((turn["n"], turn["fixed"], turn["ts"]), (2, "daily", 5))


if __name__ == "__main__":
    unittest.main()
