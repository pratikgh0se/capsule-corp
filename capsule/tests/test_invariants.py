"""C15b (t_ad8eadd2): the invariant incident kinds and their detectors. Real temp boards,
a real temp git checkout. A detector that
fires opens a Korin incident, pauses the board (`pause:board`) and pages once (`needs_input`)."""
import contextlib
import io
import json
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import blocks, db, incidents, intent, invariants, paths, tick
from capsule.tests.private_names import retired_names

NOW = 1_790_000_000
BOARD = "cc"


def git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(cwd), *args],
                   check=True, capture_output=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.home = self.dir / "home"
        self.board = self.home / "boards" / BOARD / "kanban.db"
        self.board.parent.mkdir(parents=True)
        db.create_board(self.board)
        self.conn = db.connect(self.board)
        self.addCleanup(self.conn.close)
        self.dev = self.dir / "dev"
        self.dev.mkdir()
        git(self.dev, "init", "-q", "-b", "main")
        (self.dev / "a.txt").write_text("a")
        git(self.dev, "add", "a.txt")
        git(self.dev, "commit", "-q", "-m", "x")
        self.cfg = {"board": BOARD, "invariants": {"enabled": True, "dev_checkout": str(self.dev)}}

    def sweep(self, **kw):
        kw.setdefault("cwd", self.dir)
        return invariants.sweep(self.conn, self.cfg, now=NOW, **kw)

    def kinds(self):
        return sorted(r["kind"] for r in self.conn.execute("SELECT kind FROM incidents"))

    def add_task(self, tid, status, events=()):
        with db.txn(self.conn):
            self.conn.execute("INSERT INTO tasks (id, title, status, created_at) VALUES (?,?,?,1)",
                              (tid, "x", status))
            for i, kind in enumerate(events):
                self.conn.execute("INSERT INTO task_events (task_id, kind, created_at)"
                                  " VALUES (?,?,?)", (tid, kind, 10 + i))


class Paging(Base):
    def pages(self):
        return self.conn.execute("SELECT body FROM outbox WHERE channel='file'").fetchall()

    def test_the_tick_cwd_branch_pauses(self):
        self.sweep(cwd=self.dev)
        self.assertIsNotNone(db.guard_get(self.conn, "pause:board")[0])

    def test_a_dirty_tree_pages_once_and_does_not_pause(self):
        (self.dev / "a.txt").write_text("changed")
        self.sweep()
        self.sweep()
        self.assertEqual(self.kinds(), ["main_checkout_write"])
        self.assertIsNone(db.guard_get(self.conn, "pause:board")[0])
        self.assertEqual(len(self.pages()), 1)

    def test_lost_card_state_pages_and_does_not_pause(self):
        self.sweep()
        self.add_task("t_00000001", "running", ["created", "completed"])
        self.sweep()
        self.assertEqual(self.kinds(), ["lost_card_state"])
        self.assertIsNone(db.guard_get(self.conn, "pause:board")[0])
        self.assertEqual(len(self.pages()), 1)

    def test_both_park_names_are_state_events(self):
        """t_3e108a93: a `parked_by_human` row and a pre-rename `parked` row, each newer than
        `completed`, supersede it (triage is not a `completed` status, so a miss would page)."""
        self.sweep()
        self.add_task("t_00000002", "triage", ["created", "completed", "parked_by_human"])
        self.add_task("t_00000003", "triage", ["created", "completed", "parked"])
        self.sweep()
        self.assertEqual(self.kinds(), [])

    def test_disabled_block_does_nothing(self):
        self.cfg["invariants"]["enabled"] = False
        (self.dev / "a.txt").write_text("changed")
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.kinds(), [])


class MainCheckoutWrite(Base):
    def test_clean_checkout_is_quiet(self):
        self.assertEqual(self.sweep(), [])

    def test_a_modified_tracked_file_fires_and_names_the_path(self):
        (self.dev / "a.txt").write_text("changed")
        (ids,) = self.sweep()
        row = incidents.get(self.conn, ids)
        self.assertEqual(row["kind"], "main_checkout_write")
        self.assertIn("a.txt", row["evidence"])

    def test_untracked_files_are_not_a_write(self):
        (self.dev / "notes.md").write_text("x")
        self.assertEqual(self.sweep(), [])

    def test_scratchpad_and_worktrees_are_exempt(self):
        for d in ("scratchpad", ".worktrees"):
            (self.dev / d).mkdir()
            (self.dev / d / "f").write_text("x")
            git(self.dev, "add", "-f", d)
        git(self.dev, "commit", "-q", "-m", "y")
        for d in ("scratchpad", ".worktrees"):
            (self.dev / d / "f").write_text("changed")
        self.assertEqual(self.sweep(), [])

    def test_a_tick_cwd_in_the_dev_checkout_fires(self):
        (ids,) = self.sweep(cwd=self.dev)
        self.assertEqual(incidents.get(self.conn, ids)["kind"], "main_checkout_write")

    def test_a_tick_cwd_in_a_worktree_is_fine(self):
        (self.dev / ".worktrees" / "w").mkdir(parents=True)
        self.assertEqual(self.sweep(cwd=self.dev / ".worktrees" / "w"), [])

    def test_a_missing_checkout_cannot_prove_anything(self):
        shutil.rmtree(self.dev)
        self.assertEqual(self.sweep(), [])

    def test_enabled_without_a_dev_checkout_is_refused_not_defaulted(self):
        del self.cfg["invariants"]["dev_checkout"]
        with self.assertRaises(ValueError):
            self.sweep()


class RecurredCondition(Base):
    def alerts(self):
        return self.conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='file'").fetchone()[0]

    def test_a_sweep_condition_matching_a_fix_pending_incident_does_not_page_or_pause(self):
        (first,) = self.sweep(cwd=self.dev)
        self.assertEqual(self.alerts(), 1)
        self.assertIsNotNone(db.guard_get(self.conn, "pause:board")[0])
        with db.txn(self.conn):
            self.conn.execute("UPDATE incidents SET status='fix_pending', fix_card='t_0000aaaa'"
                              " WHERE id=?", (first,))
            self.conn.execute("DELETE FROM outbox")
            self.conn.execute("DELETE FROM guard_state WHERE key='pause:board'")
        self.assertEqual(self.sweep(cwd=self.dev), [first])
        (body,) = [r[0] for r in self.conn.execute(
            "SELECT body FROM outbox WHERE channel != 'local'")]
        self.assertIn("RECURRED", body)                     # the one alert, no needs_input line
        self.assertNotIn("PAUSED", body)
        self.assertIsNone(db.guard_get(self.conn, "pause:board")[0])
        (local,) = self.conn.execute("SELECT COUNT(*) FROM outbox WHERE channel='local'").fetchone()
        self.assertEqual(local, 1)                          # one Mac page


class FirstPass(Base):
    def test_history_before_the_first_pass_is_never_judged(self):
        self.add_task("t_0000000f", "running", ["created", "completed"])
        self.add_task("t_0000000e", "archived", ["created", "completed"])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.kinds(), [])


class LostCardState(Base):
    def setUp(self):
        super().setUp()
        self.sweep()                       # the first pass records the baseline

    def test_consistent_cards_are_quiet(self):
        self.add_task("t_00000001", "done", ["created", "completed"])
        self.add_task("t_00000002", "blocked", ["created", "blocked", "notes"])
        self.add_task("t_00000003", "ready", ["created", "blocked", "unblocked"])
        self.add_task("t_00000004", "archived", ["created", "completed", "archived"])
        self.add_task("t_00000005", "archived", ["created", "blocked"])
        self.assertEqual(self.sweep(), [])

    def test_done_event_with_a_different_status_fires(self):
        self.add_task("t_00000001", "running", ["created", "completed"])
        (ids,) = self.sweep()
        self.assertEqual(incidents.get(self.conn, ids)["kind"], "lost_card_state")
        self.assertIn("t_00000001", incidents.get(self.conn, ids)["evidence"])

    def test_a_human_close_of_a_blocked_card_is_quiet(self):
        """inc_a35d2666: `closed` is the newest state event, so the older `blocked` is not."""
        self.add_task("t_00000001", "blocked", ["created", "blocked"])
        self.assertTrue(blocks.close(self.conn, "t_00000001", "landed outside"))
        self.assertEqual(invariants._lost_card_state(self.conn), [])
        self.assertEqual(self.sweep(), [])
        self.assertEqual(self.kinds(), [])

    def test_every_status_verb_leaves_the_detector_quiet(self):
        """A verb that writes an event the detector cannot read fails here, not in production."""
        c = self.conn
        self.add_task("t_00000001", "ready", ["created"])
        self.assertTrue(blocks.park(c, "t_00000001", "wait for x"))
        self.assertEqual(invariants._lost_card_state(c), [])
        self.add_task("t_00000005", "blocked", ["created", "blocked"])
        blocks.reset_failures(c, "t_00000005", by="human")
        self.assertEqual(invariants._lost_card_state(c), [])
        self.add_task("t_00000002", "blocked", ["created", "blocked"])
        self.assertTrue(blocks.unblock(c, "t_00000002", by="human"))
        self.assertEqual(invariants._lost_card_state(c), [])
        self.add_task("t_00000003", "blocked", ["created", "blocked"])
        with db.txn(c):
            c.execute("INSERT INTO task_runs (task_id, profile, status, started_at, handoff)"
                      " VALUES ('t_00000003','implementer','done',1,?)",
                      (json.dumps({"status": "done"}),))
        self.assertTrue(blocks.accept(c, "t_00000003"))
        self.assertEqual(invariants._lost_card_state(c), [])
        self.add_task("t_00000004", "blocked", ["created", "blocked"])
        self.assertTrue(blocks.close(c, "t_00000004", "landed outside"))
        self.assertEqual(invariants._lost_card_state(c), [])
        self.add_task("t_00000006", "done", ["created", "completed"])
        with db.txn(c):
            c.execute("UPDATE tasks SET branch_name='epic/x' WHERE id='t_00000006'")
            db.guard_set(c, "first_screen:epic/x",
                         {"task_id": "t_00000006", "state": "held"}, now=NOW)
        self.assertTrue(blocks.answer_first_screen(c, "t_00000006", "screen-fix", "x"))
        self.assertEqual(invariants._lost_card_state(c), [])

    def test_an_archived_event_with_a_live_status_fires(self):
        self.add_task("t_00000001", "running", ["created", "completed", "archived"])
        self.assertEqual(len(self.sweep()), 1)

    def test_blocked_event_with_a_ready_status_fires(self):
        self.add_task("t_00000001", "ready", ["created", "blocked"])
        self.assertEqual(len(self.sweep()), 1)

    def test_status_change_with_no_event_between_passes_fires(self):
        self.add_task("t_00000001", "review", ["created", "request_review"])
        self.assertEqual(self.sweep(), [])
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='blocked' WHERE id='t_00000001'")
        self.assertEqual(len(self.sweep()), 1)

    def test_status_change_with_an_event_is_fine(self):
        self.add_task("t_00000001", "review", ["created", "request_review"])
        self.sweep()
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='blocked' WHERE id='t_00000001'")
            db.event(self.conn, "t_00000001", "blocked", {"reason": "x"}, now=NOW)
        self.assertEqual(self.sweep(), [])

    def test_requeue_without_an_event_is_allowed(self):
        self.add_task("t_00000001", "running", ["created", "claimed"])
        self.sweep()
        with db.txn(self.conn):
            self.conn.execute("UPDATE tasks SET status='ready' WHERE id='t_00000001'")
        self.assertEqual(self.sweep(), [])

    def test_a_card_that_vanishes_fires(self):
        self.add_task("t_00000001", "ready", ["created"])
        self.sweep()
        with db.txn(self.conn):
            self.conn.execute("DELETE FROM tasks WHERE id='t_00000001'")
        (ids,) = self.sweep()
        self.assertIn("card gone", incidents.get(self.conn, ids)["evidence"])


class WiredIntoTheTick(Base):
    def test_the_legacy_two_dispatcher_probe_is_retired(self):
        import inspect
        self.assertFalse(hasattr(invariants, "_two_dispatchers"))
        for name in retired_names():
            self.assertFalse(hasattr(invariants, f"_{name}_dispatch_events"))
        self.assertNotIn("run_cmd", inspect.signature(invariants.sweep).parameters)
        self.assertNotIn("home", inspect.signature(invariants.sweep).parameters)
        self.assertNotIn("home", inspect.signature(invariants.count).parameters)

    def test_count_adds_the_sweep_to_the_touched_ids(self):
        (self.dev / "a.txt").write_text("changed")
        got = invariants.count(self.conn, self.cfg, NOW,
                               ["inc_aaaaaaaa", "inc_aaaaaaaa"])
        self.assertEqual(got, 2)

    def test_a_crashing_sweep_is_printed_and_does_not_lose_the_touched_ids(self):
        err = io.StringIO()
        with mock.patch.object(invariants, "_lost_card_state", side_effect=RuntimeError("boom")), \
                contextlib.redirect_stderr(err):
            got = invariants.count(self.conn, self.cfg, NOW, ["inc_aaaaaaaa"])
        self.assertEqual(got, 1)
        self.assertIn("invariants sweep failed", err.getvalue())
        self.assertEqual(self.kinds(), [])

    def test_the_tick_pass_runs_the_sweep_and_survives_a_crash(self):
        """The real `Tick._incidents`: a dirty tracked file counts, a bad sweep is not fatal."""
        (self.dev / "a.txt").write_text("changed")
        cfg = dict(self.cfg, routing={})
        counts = {"tick_errors": 0}
        tick.Tick(self.home)._incidents(self.conn, cfg, NOW, counts)
        self.assertEqual(counts["incidents"], 1)
        self.assertEqual(self.kinds(), ["main_checkout_write"])
        self.assertEqual(counts["tick_errors"], 0)
        del cfg["invariants"]["dev_checkout"]
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            tick.Tick(self.home)._incidents(self.conn, cfg, NOW, counts)
        self.assertEqual(counts["incidents"], 0)
        self.assertEqual(counts["tick_errors"], 0)            # the gate still ran
        self.assertIn("incidents_closed", counts)
        self.assertIn("invariants sweep failed", err.getvalue())

    def test_kinds_are_in_both_independent_lists(self):
        for kind in invariants.KINDS:
            self.assertIn(kind, incidents.KINDS)
            self.assertIn(kind, db.INCIDENTS_KIND)

    def test_an_existing_board_is_widened_by_migrate(self):
        old = self.dir / "old.db"
        db.create_board(old)
        conn = sqlite3.connect(old)
        old_kinds = ",".join(f"'{k}'" for k in db.INCIDENTS_KIND if k not in invariants.KINDS)
        new_kinds = ",".join(f"'{k}'" for k in db.INCIDENTS_KIND)
        body = db._incidents_body().replace(new_kinds, old_kinds)
        self.assertNotIn("lost_card_state", body)
        conn.executescript("DROP TABLE incidents; CREATE TABLE incidents (" + body + ");")
        conn.commit()
        conn.close()
        conn = db.connect(old)
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO incidents (id, board, kind, opened_at, fingerprint)"
                         " VALUES ('inc_00000000','b','lost_card_state',1,'f')")
        db.migrate(conn)
        incidents.open_incident(conn, board=BOARD, kind="lost_card_state", key="k", now=NOW)
        conn.close()


class IntentExamples(Base):
    """Z1: the shipped examples replayed through the real detectors."""
    NAMES = ("invariants",)

    def replay(self, doc):
        check = doc["check"]
        if check == "main_checkout":
            for rel in doc["modified_tracked"]:
                p = self.dev / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("v1")
                git(self.dev, "add", rel)
                git(self.dev, "commit", "-q", "-m", "tracked")
                p.write_text("v2")
            ids = self.sweep()
        else:
            self.sweep()                       # baseline pass: only newer history is judged
            self.add_task("t_00000001", doc["status"], doc["events"])
            ids = self.sweep()
        kind = incidents.get(self.conn, ids[0])["kind"] if ids else "none"
        paused = db.guard_get(self.conn, "pause:board")[0] is not None
        return ("opened" if ids else "quiet"), kind, paused

    def test_the_shipped_examples(self):
        n = 0
        for name in self.NAMES:
            for ex in intent.load(name):
                n += 1
                self.setUp()
                got = self.replay(ex["given"]["document"])
                want = (ex["expect"]["outcome"], ex["expect"]["kind"], ex["expect"]["paused"])
                self.assertEqual(got, want, ex["id"])
        self.assertEqual(n, 2)   # the two-dispatchers row retired with handover (2026-11-13)


if __name__ == "__main__":
    unittest.main()
