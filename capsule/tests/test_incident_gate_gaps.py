"""Korin close-gate gaps (row 456): a test renamed after its fix (`landed --renamed-from`), a
`def` that wraps over two lines, and `close --author`. Oracle 1: the verdict of the real gate
functions on a temp git repo; oracle 2: the incident evidence read from the temp board db."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import db, incident_landed, incident_recurrence as rec, incidents, paths

OLD = "pkg.test_guard.Guard.test_old_name"
NEW = "pkg.test_guard.Guard.test_new_name"
SRC = "import unittest\n\n\nclass Guard(unittest.TestCase):\n    def {name}(self):\n        pass\n"


class Repo(unittest.TestCase):
    def git(self, *args):
        res = subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                              *args], cwd=self.repo, check=True, capture_output=True, text=True)
        return res.stdout.strip()

    def commit(self, text, msg):
        (self.repo / "pkg" / "test_guard.py").write_text(text)
        self.git("add", ".")
        self.git("commit", "-qm", msg)
        return self.git("rev-parse", "HEAD")

    def setUp(self):
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo, True)
        (self.repo / "pkg").mkdir()
        (self.repo / "pkg" / "__init__.py").write_text("")
        self.git("init", "-q", "-b", "main")
        self.base = self.commit(SRC.format(name="test_other"), "base")


class WrappedDef(Repo):
    def test_a_def_that_wraps_over_two_lines_counts_as_on_main(self):
        self.commit(SRC.format(name="\\\n    test_wrapped"), "fix")
        ok, why = rec.recurrence_test_is_new("pkg.test_guard.Guard.test_wrapped", self.repo,
                                             self.base)
        self.assertTrue(ok, why)

    def test_a_name_in_a_comment_or_another_class_is_not_a_test(self):
        text = SRC.format(name="test_other") + "\n# def test_ghost(self):\n\n\nclass Other:\n" \
            "    def test_elsewhere(self):\n        pass\n"
        self.commit(text, "fix")
        for name in ("Guard.test_ghost", "Guard.test_elsewhere"):
            ok, _ = rec.recurrence_test_is_new(f"pkg.test_guard.{name}", self.repo, self.base)
            self.assertFalse(ok, name)


class Renamed(Repo):
    def setUp(self):
        super().setUp()
        self.fix = self.commit(SRC.format(name="test_old_name"), "fix")
        self.commit(SRC.format(name="test_new_name"), "rename")
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        path = paths.board_db("b", self.home)
        db.create_board(path)
        self.conn = db.connect(path)
        self.addCleanup(self.conn.close)
        self.card = db.new_task(self.conn, title="fix", status="done")
        self.inc, _ = incidents.open_incident(
            self.conn, board="b", kind="crash", key="boom", evidence=["event:1"],
            main_sha=self.base, now=1_790_000_000)
        self.conn.execute("UPDATE incidents SET status='fix_pending', fix_card=?,"
                          " recurrence_test=? WHERE id=?", (self.card, NEW, self.inc))

    def land(self, **kw):
        return incident_landed.landed(
            self.conn, self.inc, self.fix, author="cos", board="b", checkout=self.repo,
            home=self.home, **kw)

    def test_a_rename_with_the_old_id_on_the_fix_commit_lands_and_is_recorded(self):
        self.land(renamed_from=OLD)
        evidence = incidents.get(self.conn, self.inc)["evidence"]
        self.assertIn(f"renamed after the fix: {OLD} -> {NEW}", evidence)
        self.assertIn(self.fix, evidence)
        self.assertEqual([], incidents.recurrence_gaps(
            incidents.get(self.conn, self.inc), self.repo, rec.run_recurrence_test, 60,
            rec.recurrence_test_is_new, None))

    def test_without_the_mapping_the_new_name_is_refused_as_not_on_the_fix(self):
        with self.assertRaisesRegex(ValueError, "is not on"):
            self.land()

    def test_an_old_id_the_fix_commit_does_not_hold_is_refused(self):
        with self.assertRaisesRegex(ValueError, "is not on"):
            self.land(renamed_from="pkg.test_guard.Guard.test_never_existed")

    def test_an_old_id_that_was_already_on_the_base_is_refused_as_not_new(self):
        with self.assertRaisesRegex(ValueError, "not new"):
            self.land(renamed_from="pkg.test_guard.Guard.test_other")

    def test_a_new_id_that_is_not_on_main_is_refused(self):
        self.conn.execute("UPDATE incidents SET recurrence_test=? WHERE id=?",
                          ("pkg.test_guard.Guard.test_missing", self.inc))
        with self.assertRaises(ValueError):
            self.land(renamed_from=OLD)

    def test_the_mapping_to_itself_is_refused(self):
        with self.assertRaisesRegex(ValueError, "renamed-from"):
            self.land(renamed_from=NEW)
        self.assertNotIn("fix landed", incidents.get(self.conn, self.inc)["evidence"])


    def test_a_watch_closure_refuses_the_flag(self):
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (self.inc,))
        with self.assertRaisesRegex(ValueError, "watch"):
            self.land(renamed_from=OLD)

    def test_an_old_id_still_on_main_was_not_renamed(self):
        self.commit(SRC.format(name="test_old_name") + "\n    def test_new_name(self):\n"
                    "        pass\n", "keep both")
        with self.assertRaisesRegex(ValueError, "still on main"):
            self.land(renamed_from=OLD)

    def test_the_mapping_line_is_written_when_the_fix_sha_is_already_recorded(self):
        db.event(self.conn, self.card, "completed", {"commit": self.fix, "outside": True})
        incidents.attach(self.conn, self.inc, [f"fix landed outside the factory: card {self.card}"
                                               f" done at {self.fix} by cos"])
        rename = f"recurrence test renamed after the fix: {OLD} -> {NEW}"
        self.land(renamed_from=OLD)
        self.land(renamed_from=OLD)      # the same line is not added twice
        self.assertEqual(1, incidents.get(self.conn, self.inc)["evidence"].count(rename))

    def test_the_refusal_hint_names_the_old_id(self):
        with self.assertRaisesRegex(ValueError, f"recurrence test {OLD}b in the fix"):
            self.land(renamed_from=OLD + "b")


class BaseSideParse(Repo):
    def test_a_base_file_with_the_test_and_a_syntax_error_is_not_new_later(self):
        base = self.commit(SRC.format(name="test_x") + "\nbroken(\n", "base2")
        self.commit(SRC.format(name="test_x"), "repair")
        ok, why = rec.recurrence_test_is_new("pkg.test_guard.Guard.test_x", self.repo, base)
        self.assertFalse(ok)
        self.assertIn("not new", why)


class CloseAuthor(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        db.create_board(paths.board_db("b", self.home))

    def close(self, *extra, agent=False):
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
        env.update({"CLAUDECODE": "1"} if agent else {})
        return subprocess.run(
            [sys.executable, "-m", "capsule", "incidents", "close", "inc_nope", "--board", "b",
             "--capsule-home", str(self.home), *extra],
            cwd=paths.REPO, capture_output=True, text=True, timeout=60, env=env)

    def test_close_takes_the_author_flag_and_an_agent_session_must_pass_it(self):
        out = self.close("--author", "cos", agent=True)
        self.assertNotIn("unrecognized", out.stderr)
        self.assertIn("no incident 'inc_nope'", out.stderr)
        out = self.close(agent=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("--author cos", out.stderr)

    def test_a_close_records_the_author_as_by_on_the_incident_closed_event(self):
        from unittest import mock
        from capsule import incidents_cli
        conn = db.connect(paths.board_db("b", self.home))
        self.addCleanup(conn.close)
        card = db.new_task(conn, title="card", status="done")
        inc, _ = incidents.open_incident(conn, board="b", kind="crash", key="boom",
                                         evidence=["event:1"], task_id=card, now=1_790_000_000)
        conn.execute("UPDATE incidents SET status='fix_pending' WHERE id=?", (inc,))
        args = type("A", (), {"incident_id": inc, "author": "cos", "quiet": True})
        with mock.patch.object(incidents, "missing", return_value=[]):
            self.assertEqual(0, incidents_cli._close(conn, args, {}, self.home))
        payload = conn.execute("SELECT payload FROM task_events WHERE kind='incident_closed'"
                               ).fetchone()["payload"]
        self.assertEqual("cos", json.loads(payload)["by"])


if __name__ == "__main__":
    unittest.main()
