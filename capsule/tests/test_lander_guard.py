# mutation-check: targets-predate-range ff625e94 -- main passes :: Guard.test_fresh_board_clear
"""G3 (rq_65cf5b3b): the keeper and the lander never land at once. Real files, a real board DB and
the real `bin/capsule-own`; nothing is mocked. Oracle 1 is the outcome dict or the guard's exit
code and stdout; oracle 2 is the bare remote's `main` (refs unmoved) read with plain git."""
import contextlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import time
import unittest
from unittest import mock

from capsule import db, intent, lander, landing, paths
from capsule.tests import test_landing

NOW = int(time.time())
OWN = paths.REPO / "bin" / "capsule-own"
STALE = NOW - 22 * 3600


class Base(test_landing.Landing):
    def own(self, branch, owner):
        env = {**os.environ, "CAPSULE_HOME": str(self.home)}
        (self.dir / "b").mkdir(exist_ok=True)       # the queue key is the repo dir's name
        subprocess.run([str(OWN), str(self.dir / "b"), branch, owner], check=True,
                       capture_output=True, env=env)

    def age_claims(self, beat):
        path = paths.queue_dir("b", self.home) / "owners.json"
        doc = json.loads(path.read_text())
        for row in doc.values():
            row["heartbeat"] = beat
        path.write_text(json.dumps(doc))

    def marker_file(self, name, state):
        path = paths.queue_dir("b", self.home) / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"state": state}))

    def land_quietly(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            got = self.land()
        return got, err.getvalue()

    def verify_owed(self):
        db.guard_set(self.conn, landing.LANDED + self.tid,
                     {"state": "revert_owed", "sha": "abc", "commits": ["abc"]})
        with contextlib.redirect_stderr(io.StringIO()):
            return landing.verify(self.conn, self.tid, cfg=self.cfg, release=self.release,
                                  home=self.home)


class Mutex(Base):
    def test_a_merge_keeper_claim_means_busy_and_nothing_moves(self):
        self.put("a.txt", "work\n", "card work")
        self.own("capsule/other", "merge-keeper")
        before = self.remote_main()
        got, err = self.land_quietly()
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("merge-keeper", got["detail"])
        self.assertIn("merge-keeper", err)      # a block is visible, never silent
        self.assertEqual(self.remote_main(), before)
        self.assertFalse(db.guard_get(self.conn, landing.BUSY)[0]["busy"])

    def test_a_marker_in_state_landing_means_busy_and_nothing_moves(self):
        self.put("a.txt", "work\n", "card work")
        self.marker_file("capsule/keeper-cards", "landing")
        before = self.remote_main()
        got, _ = self.land_quietly()
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("landing", got["detail"])
        self.assertEqual(self.remote_main(), before)

    def test_a_keeper_claim_made_after_the_first_look_still_stops_the_push(self):
        self.put("a.txt", "work\n", "card work")
        before, real = self.remote_main(), landing.acquire

        def claim_then_acquire(*args):      # the keeper claims between the lander's look and CAS
            self.own("capsule/other", "merge-keeper")
            return real(*args)

        with mock.patch.object(landing, "acquire", claim_then_acquire):
            got, _ = self.land_quietly()
        self.assertEqual(got["outcome"], "busy")
        self.assertEqual(self.remote_main(), before)
        self.assertFalse(db.guard_get(self.conn, landing.BUSY)[0]["busy"])

    def test_a_keeper_claim_holds_back_the_revert_push_and_says_why(self):
        self.own("capsule/other", "merge-keeper")
        got = self.verify_owed()
        self.assertEqual(got["outcome"], "busy")
        self.assertIn("merge-keeper", got["detail"])
        self.assertEqual(db.guard_get(self.conn, landing.LANDED + self.tid)[0]["state"],
                         "revert_owed")

    def test_a_busy_lander_flag_on_verify_carries_a_detail(self):
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "t_o", "at": NOW - 5})
        self.assertIn("lander.busy", self.verify_owed()["detail"])

    def test_stale_claim_asks_once(self):
        self.put("a.txt", "work\n", "card work")
        self.own("capsule/hook-seats", "merge-keeper")
        self.age_claims(STALE)
        with mock.patch.object(landing, "time", mock.Mock(time=lambda: NOW)):  # frozen clock
            for _ in range(3):
                got, err = self.land_quietly()
                self.assertEqual(got["outcome"], "busy")
        self.assertIn("22.0h old", got["detail"])
        self.assertIn("22.0h old", err)
        rows = [json.loads(r[0]) for r in self.conn.execute(
            "SELECT payload FROM task_events WHERE kind='landing'")]
        self.assertEqual([r["outcome"] for r in rows], ["busy_keeper"])
        asks = self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0]
        self.assertEqual(asks, 1)

    def test_a_fresh_claim_asks_nothing(self):
        self.put("a.txt", "work\n", "card work")
        self.own("capsule/other", "merge-keeper")
        self.land_quietly()
        self.assertEqual(self.conn.execute("SELECT count(*) FROM outbox").fetchone()[0], 0)

    def test_other_owners_and_finished_markers_do_not_block_a_landing(self):
        head = self.put("a.txt", "work\n", "card work")
        self.own("capsule/other", "implementer")
        self.marker_file("capsule/done-card", "landed")
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)

    def test_a_released_keeper_claim_no_longer_blocks(self):
        head = self.put("a.txt", "work\n", "card work")
        self.own("capsule/other", "merge-keeper")
        self.own("capsule/other", "--release")
        self.assertEqual(self.land()["outcome"], "landed")
        self.assertEqual(self.remote_main(), head)


class WithBoard(Base):
    def guard(self, board="b"):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = lander.main(["guard", "--board", board, "--home", str(self.home)])
        return code, out.getvalue()

    def setUp(self):
        super().setUp()
        board = paths.board_db("b", self.home)     # the guard reads the board's own DB
        board.parent.mkdir(parents=True)
        db.create_board(board)
        self.conn.close()
        self.conn = db.connect(board)


class Guard(WithBoard):

    def test_fresh_board_clear(self):
        from capsule import cli_ops
        cfg = cli_ops._new_config("capsule-corp", "https://example.test/r.git")
        self.assertFalse(set(cfg.get("lander") or ()) - {"branch"})
        fresh = paths.config_path(self.home, "fresh")
        fresh.parent.mkdir(parents=True)
        fresh.write_text(json.dumps(cfg))
        code, text = self.guard("fresh")
        self.assertEqual((code, "STOP" in text), (0, False), text)

    def test_a_free_board_passes(self):
        self.assertEqual(self.guard()[0], 0)

    def test_a_held_lander_busy_flag_stops_the_keeper(self):
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "t_x", "at": NOW - 5})
        code, text = self.guard()
        self.assertEqual(code, 1)
        self.assertIn("lander.busy", text)

    def test_a_freed_or_dead_busy_flag_passes(self):
        db.guard_set(self.conn, landing.BUSY, {"busy": False, "task": "t_x", "at": NOW})
        self.assertEqual(self.guard()[0], 0)
        db.guard_set(self.conn, landing.BUSY,
                     {"busy": True, "task": "t_x", "at": NOW - landing.BUSY_TTL - 1})
        self.assertEqual(self.guard()[0], 0)

    def test_a_pushed_but_unverified_landing_stops_the_keeper(self):
        db.guard_set(self.conn, landing.LANDED + "t_y", {"state": "pushed", "sha": "abc"})
        code, text = self.guard()
        self.assertEqual(code, 1)
        self.assertIn("t_y", text)

    def test_a_revert_owed_landing_stops_the_keeper(self):
        db.guard_set(self.conn, landing.LANDED + "t_z", {"state": "revert_owed", "sha": "abc"})
        self.assertEqual(self.guard()[0], 1)

    def test_verified_and_reverted_landings_pass(self):
        db.guard_set(self.conn, landing.LANDED + "t_a", {"state": "verified", "sha": "abc"})
        db.guard_set(self.conn, landing.LANDED + "t_b", {"state": "reverted", "sha": "abc"})
        self.assertEqual(self.guard()[0], 0)

    def test_the_keepers_own_claim_does_not_stop_its_own_guard(self):
        self.own("capsule/x", "merge-keeper")
        self.assertEqual(self.guard()[0], 0)

    def wal_board(self, name, ddl, config=None):
        """A WAL-mode board DB made by `db.connect` (as every real board is), then closed."""
        path = paths.board_db(name, self.home)
        path.parent.mkdir(parents=True)
        if ddl:
            with contextlib.closing(db.connect(path)) as raw:
                raw.execute(ddl)
        if config is not None:
            paths.config_path(self.home, name).write_text(json.dumps(config))
        return path

    def snapshot(self, path):
        return sorted(p.name for p in path.parent.iterdir()), path.read_bytes()

    def test_a_board_never_taken_is_clear_and_nothing_is_created(self):
        code, text = self.guard("untaken")
        want = f"lander guard: clear (board not taken: {paths.board_db('untaken', self.home)})"
        self.assertEqual((code, text.strip()), (0, want))
        self.assertFalse(paths.board_db("untaken", self.home).parent.exists())   # oracle 2: disk

    def test_a_wal_db_without_guard_state_is_clear_and_byte_for_byte_untouched(self):
        path = self.wal_board("empty", "CREATE TABLE other (x)")
        before = self.snapshot(path)
        self.assertEqual(before[0], ["kanban.db"])      # a clean close leaves no sidecars
        code, text = self.guard("empty")
        want = f"lander guard: clear (board not taken: {path})"
        self.assertEqual((code, text.strip()), (0, want))
        self.assertEqual(self.snapshot(path), before)

    def test_a_wal_db_with_guard_state_is_read_without_sidecars(self):
        path = self.wal_board("taken", "CREATE TABLE guard_state (key, value, version, updated_at)")
        before = self.snapshot(path)
        code, text = self.guard("taken")
        self.assertEqual((code, text.strip()), (0, f"lander guard: clear ({path})"))
        self.assertEqual(self.snapshot(path), before)

    def test_a_board_configured_for_a_lander_with_no_db_stops(self):
        path = paths.board_db("cfg", self.home)
        path.parent.mkdir(parents=True)
        paths.config_path(self.home, "cfg").write_text(json.dumps({"lander": {"mode": "land"}}))
        code, text = self.guard("cfg")
        self.assertEqual(code, 1)
        self.assertIn(f"STOP, lander configured but no guard_state ({path})", text)
        self.assertFalse(path.exists())

    def test_a_lander_board_whose_db_lacks_guard_state_stops(self):
        path = self.wal_board("cfg2", "CREATE TABLE other (x)", {"lander": {"mode": "land"}})
        before = self.snapshot(path)
        self.assertEqual(self.guard("cfg2")[0], 1)
        self.assertEqual(self.snapshot(path), before)

    def test_a_config_without_a_lander_section_stays_clear(self):
        self.wal_board("nolander", None, {"board": "nolander"})
        self.assertEqual(self.guard("nolander")[0], 0)

    def test_a_sqlite_operational_error_fails_closed(self):
        self.wal_board("badcols", "CREATE TABLE guard_state (key)")   # guard_get: no such column
        with self.assertRaises(sqlite3.OperationalError):
            self.guard("badcols")

    def test_a_file_that_is_not_a_database_fails_closed(self):
        path = paths.board_db("junk", self.home)
        path.parent.mkdir(parents=True)
        path.write_text("this is not a database " * 50)
        with self.assertRaises(sqlite3.DatabaseError):
            self.guard("junk")

    def test_the_module_command_exits_nonzero_with_a_reason(self):
        db.guard_set(self.conn, landing.BUSY, {"busy": True, "task": "t_x", "at": NOW - 5})
        done = subprocess.run([sys.executable, "-m", "capsule.lander", "guard", "--board", "b",
                               "--home", str(self.home)], cwd=str(paths.REPO),
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 1)
        self.assertIn("lander.busy", done.stdout + done.stderr)


class IntentExamples(WithBoard):
    """Z1: the `mutex` examples in contracts/intent/lander.jsonl (the reviewer's three lines).
    Red until the chief of staff adds them: the count below is the only expected red."""

    def replay(self, case):
        sha = self.put("a.txt", "work\n", "card work")
        if case == "guard_board_not_taken":
            code = self.guard("untaken")[0]
            made = paths.board_db("untaken", self.home).parent.exists()
            return ("clear" if code == 0 and not made else "stop"), code
        if case == "guard_landed_pushed":
            db.guard_set(self.conn, landing.LANDED + "t_y", {"state": "pushed", "sha": sha})
            code = self.guard()[0]
            return ("stop" if code else "go"), code
        before = self.remote_main()
        self.own("capsule/other", "merge-keeper")
        if case == "keeper_claim_stale":
            self.age_claims(STALE)
            got, _ = self.land_quietly()
        else:
            got = self.verify_owed()
        return got["outcome"], self.remote_main() != before

    def test_the_mutex_examples(self):
        shipped = [e for e in intent.load("lander") if "mutex" in e["expect"]]
        self.assertEqual(len(shipped), 4)
        for ex in shipped:
            self.setUp()
            got = self.replay(ex["given"]["document"]["case"])
            want = ex["expect"]
            if "exit" in want:
                self.assertEqual(got, (want["mutex"], want["exit"]), ex["id"])
            else:
                self.assertEqual(got, ("busy", want["pushed"]), ex["id"])


if __name__ == "__main__":
    unittest.main()
