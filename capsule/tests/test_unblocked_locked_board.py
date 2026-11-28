# mutation-check: targets-predate-range b957739c9 -- rename only, no behaviour pinned ::
#   ReadableBoardIsNotExit2.test_detect_no_longer_takes_legacy_home
"""N1 (t_ae574709, row 470 I7 re-review): a locked board whose `-wal` still holds committed
frames is NEVER read through the `immutable=1` fallback - sqlite ignores the WAL there, so a
fallback read would report a stale snapshot as if it were the board. The reader must fail loud
(the detector then exits 2), not guess.

Z2, two oracles per case: `db.connect_ro_tolerant` itself (the guard) and `unblocked.main`'s
own exit code / `board_items` (what the detector, the thing a hook shells out to, does with it).
Fixtures: `wal_oracle` builds each board in a separate writer PROCESS; the contention test
holds its lock from a second process too (a same-process pair would share sqlite's own lock
state and hide the very failure a real second reader hits).
"""
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from capsule import db, paths, unblocked
from capsule.tests import wal_oracle

BOARD = "sample-app"

_HOLDER = """
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1], isolation_level=None)
conn.execute("PRAGMA locking_mode=EXCLUSIVE")     # WAL + exclusive locking: no other process may read
conn.execute("SELECT count(*) FROM tasks").fetchall()
print("locked", flush=True)
sys.stdin.read()          # hold the lock until the test closes our stdin
"""


def _home_with_board(frames):
    """A capsule home whose `sample-app` board is a real `wal_oracle` board, plus a config naming
    only that board; returns (home, db_path)."""
    home = Path(tempfile.mkdtemp())
    (home / "config.json").write_text(json.dumps({"unblocked": {"boards": [BOARD], "repos": []}}))
    board_dir = paths.board_db(BOARD, home).parent
    path = wal_oracle.make_empty_wal(board_dir) if frames is None else wal_oracle.make(board_dir, frames=frames)
    (board_dir / "config.json").write_text("{}")    # only a configured board is read (D244)
    return home, path


def _lock_down(testcase, path):
    """Directory not writable and no `-shm`: the plain `mode=ro` open cannot create/map the
    WAL index, which is exactly the state that sends `connect_ro_tolerant` to its fallback."""
    if os.geteuid() == 0:
        testcase.skipTest("root ignores directory write bits")
    shm = Path(str(path) + "-shm")
    if shm.exists():
        shm.unlink()
    path.parent.chmod(0o555)
    testcase.addCleanup(path.parent.chmod, 0o755)


class NonEmptyWalIsNeverReadThroughTheFallback(unittest.TestCase):
    def test_the_guard_refuses_and_a_truncated_wal_in_the_same_state_still_reads(self):
        """Control + subject share the lock-down shape; only the WAL content differs."""
        _, frames_db = _home_with_board(frames=True)
        _lock_down(self, frames_db)
        with self.assertRaises(sqlite3.OperationalError):
            db.connect_ro_tolerant(frames_db)
        _, empty_db = _home_with_board(frames=None)
        _lock_down(self, empty_db)
        conn = db.connect_ro_tolerant(empty_db)
        self.addCleanup(conn.close)
        self.assertEqual([r[0] for r in conn.execute("SELECT title FROM tasks")], ["committed"])

    def test_the_detector_exits_2_instead_of_reporting_a_stale_board(self):
        """Oracle 2: through `unblocked.main`, the uncheckpointed `pending` ready card is never
        silently dropped (a fallback read would show a board without it) - exit 2, named error."""
        home, path = _home_with_board(frames=True)
        _lock_down(self, path)
        out = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=2000, stdout=out)
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("OperationalError", out.getvalue())
        self.assertNotIn("UNBLOCKED", out.getvalue())

    def test_the_same_lock_down_with_a_truncated_wal_reports_the_committed_card(self):
        """The detector-level control: the guard must not turn every locked board into exit 2."""
        home, path = _home_with_board(frames=None)
        _lock_down(self, path)
        items = unblocked.board_items(BOARD, capsule_home=home, now=2000,
                                      run_status_by_commit={})
        self.assertEqual([i.item.split(" ", 1)[1] for i in items], ["committed"])


class ReadableBoardIsNotExit2(unittest.TestCase):
    """Control for the exit-2 cases above: the same detector on an openable board reports the
    work (exit 1, one unowned ready card), so exit 2 really is the lock/WAL guard speaking."""

    def test_an_openable_board_is_reported_not_refused(self):
        from capsule.tests.test_unblocked_ownership import _board
        home = _board("ended")
        (home / "config.json").write_text(json.dumps({"unblocked": {"boards": ["capsule-corp"], "repos": []}}))
        out = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=2000, stdout=out)
        self.assertEqual(rc, 1, out.getvalue())
        self.assertIn("UNBLOCKED 1 UNOWNED 1", out.getvalue())

    def test_detect_no_longer_takes_legacy_home(self):
        """D244: the old board fallback is gone, so the kwarg is too."""
        with self.assertRaises(TypeError):
            unblocked.detect(capsule_home=Path(tempfile.mkdtemp()), legacy_home=Path("/no-home"))


class ContentionFailsFastNotSlow(unittest.TestCase):
    """The 1-second timeout, exercised as behaviour: a second PROCESS holds a real WAL board
    in `locking_mode=EXCLUSIVE`, so the plain open genuinely waits on the lock and then fails.
    (A plain WAL writer never blocks a reader - sqlite's own reader/writer concurrency - which
    is why the holder takes exclusive locking.)"""

    def _locked_board(self, path):
        holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(path)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdin.close)
        self.assertEqual(holder.stdout.readline().strip(), "locked")

    def test_a_locked_board_with_a_non_empty_wal_fails_after_about_one_second(self):
        path = wal_oracle.make(tempfile.mkdtemp(), frames=True)
        self._locked_board(path)
        self.assertGreater(os.path.getsize(str(path) + "-wal"), 0)
        started = time.monotonic()
        with self.assertRaises(sqlite3.OperationalError):
            db.connect_ro_tolerant(path)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)    # it really did wait on the lock...
        self.assertLess(elapsed, 4.5)            # ...for the 1 s timeout, not a 5 s / 120 s one

    def test_the_same_lock_with_a_truncated_wal_reads_through_the_fallback_in_about_one_second(self):
        """Control: only the non-empty `-wal` made the case above raise - the committed row
        comes back, after the same one-second wait."""
        path = wal_oracle.make_empty_wal(tempfile.mkdtemp())
        self._locked_board(path)
        started = time.monotonic()
        conn = db.connect_ro_tolerant(path)
        self.addCleanup(conn.close)
        elapsed = time.monotonic() - started
        self.assertGreaterEqual(elapsed, 0.8)
        # Only has to separate 1 s from the old 5 s; 3.0 flaked (inc_2f3fcfa1).
        self.assertLess(elapsed, 4.5)
        self.assertEqual([r[0] for r in conn.execute("SELECT title FROM tasks")], ["committed"])

    def test_a_locked_non_empty_wal_board_is_exit_2_through_the_detector(self):
        """Oracle 2 for the contention case: the detector must not report a stale board."""
        home, path = _home_with_board(frames=True)
        self._locked_board(path)
        out = io.StringIO()
        rc = unblocked.main([], capsule_home=home, now=2000, stdout=out)
        self.assertEqual(rc, 2, out.getvalue())
        self.assertIn("locked", out.getvalue())


if __name__ == "__main__":
    unittest.main()
