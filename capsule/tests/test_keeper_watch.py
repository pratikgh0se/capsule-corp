# mutation-check: targets-predate-range b957739c9 -- names out of tests, no behaviour pinned ::
#   WaitingTest.test_restart_markers_never_change_the_count_on_a_real_repo
"""Oracle 1 for the keeper watchdog (t_1d641f6e): decision, waiting-work count, heartbeat row."""
import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
import unittest.mock
from pathlib import Path

from capsule import db, paths
from capsule import keeper_watch as kw

HOUR = 3600
MTIME = 1000.0
NOW = 50000.0


def _create_board(path):
    """A Capsule board db plus the `config.json` that makes `capsule unblocked` read it (D244)."""
    db.create_board(path)
    cfg = path.parent / "config.json"
    if not cfg.exists():
        cfg.write_text("{}")


def decide(age, waiting, state, mtime=MTIME, now=NOW):
    return kw.decide(age, waiting, state, mtime, now)


class DecideTest(unittest.TestCase):
    def test_fresh_log_with_waiting_work_does_not_page(self):
        self.assertEqual(decide(20 * 60, 3, {})[0], "none")

    def test_silent_log_with_waiting_work_pages_once_and_records_mtime_and_time(self):
        action, state = decide(75 * 60, 3, {})
        self.assertEqual(action, "page")
        self.assertEqual((state["paged_mtime"], state["paged_at"]), (MTIME, NOW))

    def test_same_episode_next_firing_does_not_page_again(self):
        _, state = decide(75 * 60, 3, {})
        self.assertEqual(decide(90 * 60, 3, state, now=NOW + 900)[0], "none")

    def test_silent_log_with_nothing_waiting_does_not_page(self):
        self.assertEqual(decide(75 * 60, 0, {})[0], "none")

    def test_escalates_four_hours_after_the_first_page_once(self):
        _, state = decide(75 * 60, 3, {})
        self.assertEqual(decide(5 * HOUR, 3, state, now=NOW + 3 * HOUR)[0], "none")
        action, state = decide(6 * HOUR, 3, state, now=NOW + 4 * HOUR)
        self.assertEqual(action, "escalate")
        self.assertEqual(decide(7 * HOUR, 3, state, now=NOW + 5 * HOUR)[0], "none")

    def test_ten_hour_old_log_on_wake_pages_once_then_escalates_only_after_four_hours(self):
        action, state = decide(10 * HOUR, 3, {})
        self.assertEqual(action, "page")
        self.assertEqual(decide(10 * HOUR + 900, 3, state, now=NOW + 900)[0], "none")
        self.assertEqual(decide(14 * HOUR, 3, state, now=NOW + 4 * HOUR)[0], "escalate")

    def test_log_that_moved_then_went_silent_again_pages_again(self):
        _, state = decide(75 * 60, 3, {})
        action, state = decide(75 * 60, 3, state, mtime=MTIME + 9 * HOUR, now=NOW + 9 * HOUR)
        self.assertEqual(action, "page")
        self.assertEqual(state["paged_mtime"], MTIME + 9 * HOUR)


class WaitingTest(unittest.TestCase):
    def home(self, d, ready=0):
        home = Path(d) / "home"
        board = home / "boards" / "capsule-corp" / "kanban.db"
        board.parent.mkdir(parents=True)
        _create_board(board)
        conn = db.connect(board)
        for n in range(ready):
            db.new_task(conn, title=f"card {n}", status="ready", body="work")
        conn.close()
        (home / "config.json").write_text(json.dumps(
            {"unblocked": {"boards": ["capsule-corp"], "repos": [], "runs": []}}))
        return home

    def count(self, home, d):
        return kw.waiting(home, detect=lambda **kwargs: kw.unblocked.detect(**kwargs))

    def test_counts_unowned_ready_cards(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(self.count(self.home(d, ready=2), d), 2)

    def git(self, repo, *args):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", str(repo), *args],
                       check=True, capture_output=True)

    def test_restart_markers_never_change_the_count_on_a_real_repo(self):
        """A merged branch is never waiting; each unmerged branch counts once, whatever its marker's
        tick_restart says. Unmerged landed/superseded branches are excluded by unblocked's done
        rule unless they have commits newer than the marker (here the marker is written after
        the commit, so fix/landed is done and only fix/pending counts)."""
        with tempfile.TemporaryDirectory() as d:
            home = self.home(d)
            repo = Path(d) / "repo"
            repo.mkdir()
            self.git(repo, "init", "-q", "-b", "main")
            self.git(repo, "commit", "-q", "--allow-empty", "-m", "base")
            for branch in ("fix/merged", "fix/landed", "fix/pending"):
                self.git(repo, "branch", branch)
            for branch in ("fix/landed", "fix/pending"):
                self.git(repo, "checkout", "-q", branch)
                self.git(repo, "commit", "-q", "--allow-empty", "-m", branch)
            self.git(repo, "checkout", "-q", "main")
            cfg = json.loads((home / "config.json").read_text())
            cfg["unblocked"]["repos"] = [str(repo)]
            (home / "config.json").write_text(json.dumps(cfg))
            queue = home / "queue" / "repo" / "fix"
            queue.mkdir(parents=True)
            for name, state, restart in (("landed", "landed", "NEEDED (Owner restarts)"),
                                         ("pending", "built-unreviewed", "PENDING (Owner)"),
                                         ("merged", "landed", "NEEDED (Owner restarts)")):
                doc = {"state": state, "landed_sha": "abc1234", "tick_restart": restart}
                (queue / f"{name}.json").write_text(json.dumps(doc))
            self.assertEqual(self.count(home, d), 1)

    def test_unreadable_config_raises_so_run_pages_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            home = self.home(d)
            (home / "config.json").write_text("not json")
            with self.assertRaises(Exception):
                self.count(home, d)


class HeartbeatTest(unittest.TestCase):
    def test_fresh_stale_and_absent(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "keeper-log.md"
            self.assertEqual(kw.heartbeat_row(log, 5000)[0], "info")
            log.write_text("x\n")
            os.utime(log, (4000, 4000))
            level, name, detail = kw.heartbeat_row(log, 4000 + 10 * 60)
            self.assertEqual((level, name), ("ok", "keeper-heartbeat"))
            level, _, detail = kw.heartbeat_row(log, 4000 + 2 * HOUR)
            self.assertEqual(level, "info")
            self.assertIn("WARN", detail)

    def test_heartbeat_row_shows_an_active_pause_and_warns_on_a_malformed_one(self):
        with tempfile.TemporaryDirectory() as d:
            log = Path(d) / "keeper-log.md"
            log.write_text("x\n")
            os.utime(log, (NOW - 2 * HOUR, NOW - 2 * HOUR))
            pause = Path(d) / "keeper-pause.json"
            pause.write_text(json.dumps({"until": iso(HOUR), "reason": "sleeping"}))
            os.utime(pause, (NOW, NOW))
            level, name, detail = kw.heartbeat_row(log, NOW, home=d)
            self.assertEqual((level, name), ("info", "keeper-heartbeat"))
            self.assertIn("paused until", detail)
            self.assertIn("sleeping", detail)
            pause.write_bytes(b"\xff\xfe")
            level, _, detail = kw.heartbeat_row(log, NOW, home=d)
            self.assertEqual(level, "info")
            self.assertIn("WARN pause file malformed", detail)
            pause.unlink()
            self.assertNotIn("pause", kw.heartbeat_row(log, NOW, home=d)[2])


def iso(offset):
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(NOW + offset))


class PauseTest(unittest.TestCase):
    """Declared pauses (keeper-pause.json): only a future `until` silences the page."""
    def state(self, doc, offset=0, written=NOW):
        text = doc if isinstance(doc, str) else json.dumps(doc)
        return kw._pause_state(text, written, NOW + offset)

    def test_future_until_is_active_and_quiet(self):
        kind, detail = self.state({"until": iso(3 * HOUR), "reason": "weekend"})
        self.assertEqual(kind, "active")
        self.assertIn("weekend", detail)

    def test_expired_until_does_not_silence(self):
        self.assertEqual(self.state({"until": iso(-60), "reason": "x"})[0], "expired")

    def test_malformed_files_are_reported_not_silencing(self):
        for text in ("not json", "[]", "{}", '{"until": "tomorrow-ish"}', '{"until": 5}'):
            kind, detail = self.state(text)
            self.assertEqual(kind, "malformed", text)
            self.assertTrue(detail)

    def test_until_beyond_twelve_hours_is_clamped_to_written_time_plus_twelve(self):
        far = {"until": iso(40 * HOUR), "reason": "forgot"}
        self.assertEqual(self.state(far, offset=11 * HOUR)[0], "active")
        self.assertEqual(self.state(far, offset=13 * HOUR)[0], "expired")

    def test_mtime_far_in_the_future_is_malformed_not_a_longer_cap(self):
        kind, detail = self.state({"until": "2099-01-01T00:00:00+00:00"}, written=NOW + 10 * 86400)
        self.assertEqual(kind, "malformed")
        self.assertIn("future", detail)

    def test_write_pause_makes_a_file_pause_state_accepts_and_clamps(self):
        with tempfile.TemporaryDirectory() as d:
            path = kw._write_pause(d, "30h", "long one", now=NOW)
            doc = json.loads(path.read_text())
            self.assertEqual(path.name, "keeper-pause.json")
            self.assertEqual(doc["reason"], "long one")
            self.assertEqual(kw._pause_state(path.read_text(), NOW, NOW + 11 * HOUR)[0], "active")
            self.assertEqual(kw._pause_state(path.read_text(), NOW, NOW + 13 * HOUR)[0], "expired")
            with self.assertRaises(ValueError):
                kw._write_pause(d, "soon", "bad", now=NOW)


class RunPauseTest(unittest.TestCase):
    """run() end to end: a silent log with waiting cards, with and without a pause file."""
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        d = Path(tmp.name)
        self.home = WaitingTest().home(d, ready=2)
        self.log = d / "keeper-log.md"
        self.log.write_text("last line\n")
        os.utime(self.log, (NOW - 2 * HOUR, NOW - 2 * HOUR))
        env = {"AGENT_OPS_NO_SEND": "1",
               "LEGACY_KANBAN_HOME": str(d / "no-legacy")}
        patcher = unittest.mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_watch(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            kw.run(self.home, "capsule-corp", self.log, now=NOW)
        return out.getvalue(), self.pages()

    def pages(self):
        """Bodies of the outbox file rows: the only place a page lands now."""
        conn = db.connect(paths.board_db("capsule-corp", self.home))
        try:
            rows = conn.execute("select body from outbox where channel='file'")
            return "\n".join(r[0] for r in rows)
        finally:
            conn.close()

    def pause(self, text):
        path = self.home / "keeper-pause.json"
        path.write_text(text)
        os.utime(path, (NOW, NOW))

    def test_silent_log_without_a_pause_pages(self):
        self.assertIn("keeper silent", self.run_watch()[1])

    def test_active_pause_is_quiet_and_leaves_no_state(self):
        self.pause(json.dumps({"until": iso(HOUR), "reason": "sleeping"}))
        out, calls = self.run_watch()
        self.assertEqual(calls, "")
        self.assertIn("paused", out)
        self.assertFalse((self.home / "keeper-watch.state").exists())

    def test_expired_pause_pages(self):
        self.pause(json.dumps({"until": iso(-60), "reason": "old"}))
        self.assertIn("keeper silent", self.run_watch()[1])

    def test_non_utf8_pause_file_pages_instead_of_crashing(self):
        (self.home / "keeper-pause.json").write_bytes(b"\xff\xfe\x00bad")
        out, calls = self.run_watch()
        self.assertIn("pause file malformed", calls)

    def test_far_future_until_with_a_thirteen_hour_old_file_pages(self):
        """The cap binds the file's real mtime: a rolling `now` anchor would stay silent."""
        self.pause(json.dumps({"until": iso(40 * HOUR), "reason": "forgot"}))
        old = NOW - 13 * HOUR
        os.utime(self.home / "keeper-pause.json", (old, old))
        self.assertIn("keeper silent", self.run_watch()[1])

    def test_symlinked_pause_file_is_malformed_even_when_its_target_is_valid(self):
        """A symlink takes its age from its target, which another process could keep fresh."""
        target = self.home.parent / "elsewhere.json"
        target.write_text(json.dumps({"until": iso(HOUR), "reason": "linked"}))
        os.utime(target, (NOW, NOW))
        (self.home / "keeper-pause.json").symlink_to(target)
        self.assertEqual(kw._read_pause(self.home, NOW)[0], "malformed")
        out, calls = self.run_watch()
        self.assertIn("pause file malformed", calls)
        self.assertIn("symlink", out)

    def test_malformed_pause_pages_and_says_so(self):
        self.pause("{oops")
        out, calls = self.run_watch()
        self.assertIn("pause file malformed", calls)
        self.assertIn("malformed", out)


if __name__ == "__main__":
    unittest.main()
