"""`capsule incidents --board other` must resolve `other`'s own config, not the legacy one.
Oracle 1: `capsule incidents show`'s own `main_sha` field. Oracle 2: a real
`git rev-parse main` run independently of `incidents.main_head`, on the same checkout the
config names."""
import argparse
import contextlib
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
from unittest import mock

from capsule import db, incident_attach, incidents, incidents_cli, paths


def _write(path, board, main_checkout):
    path.parent.mkdir(parents=True, exist_ok=True)
    cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
    cfg.update(board=board, korin={"main_checkout": str(main_checkout)})
    path.write_text(json.dumps(cfg))


class BoardConfig(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.empty_checkout = Path(tempfile.mkdtemp())   # not a git repo: main_head -> None
        _write(paths.config_path(self.home), "legacy", self.empty_checkout)
        _write(paths.config_path(self.home, "other"), "other", paths.REPO)
        db.create_board(paths.board_db("other", self.home))

    def _run(self, *argv):
        return subprocess.run([sys.executable, "-m", "capsule", "incidents", *argv,
                               "--capsule-home", str(self.home)],
                              cwd=paths.REPO, capture_output=True, text=True, timeout=30)

    def test_open_with_board_other_uses_others_korin_main_checkout(self):
        want = subprocess.run(["git", "-C", str(paths.REPO), "rev-parse", "-q", "--verify", "main"],
                              capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertTrue(want)   # the repo really has a main branch to compare against
        opened = self._run("open", "--board", "other", "--kind", "crash", "--root-cause-key",
                           "implementer: fatal: boom", "--evidence", "event:1")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        inc_id = opened.stdout.split()[0]
        shown = self._run("show", inc_id, "--board", "other")
        self.assertEqual(shown.returncode, 0, shown.stderr)
        rec = json.loads(shown.stdout)
        self.assertEqual(rec["main_sha"], want)   # not None, and not the legacy config's empty checkout

    def test_one_shot_write_commands_wait_at_most_ten_seconds_on_a_locked_board(self):
        # followups.md line 158: attach used to sit ~120 s on a board a tick held
        seen = []
        conn = db.connect(paths.board_db("other", self.home))
        inc_id, _ = incidents.open_incident(conn, board="other", kind="crash", key="boom",
                                            evidence=["event:1"], main_sha="a" * 40)
        conn.close()

        def spy(conn, *a, **kw):
            seen.append(conn.execute("PRAGMA busy_timeout").fetchone()[0])
            return "t_x"
        args = argparse.Namespace(capsule_home=str(self.home), board="other", inc_cmd="attach",
                                  incident_id=inc_id, card="t_x")
        with mock.patch.object(incident_attach, "attach", spy), \
                contextlib.redirect_stdout(io.StringIO()):
            incidents_cli.main(args)
        self.assertEqual(seen, [10_000])

    def test_a_locked_board_says_busy_and_retry_and_still_exits_1(self):
        conn = db.connect(paths.board_db("other", self.home))
        inc_id, _ = incidents.open_incident(conn, board="other", kind="crash", key="boom",
                                            evidence=["event:1"], main_sha="a" * 40)
        conn.close()
        args = argparse.Namespace(capsule_home=str(self.home), board="other", inc_cmd="attach",
                                  incident_id=inc_id, card="t_x")
        err = io.StringIO()
        with mock.patch.object(incident_attach, "attach",
                               side_effect=sqlite3.OperationalError("database is locked")), \
                contextlib.redirect_stderr(err):
            rc = incidents_cli.main(args)
        self.assertEqual(rc, 1)
        self.assertIn("busy", err.getvalue())
        self.assertIn("retry", err.getvalue())
        self.assertNotEqual(err.getvalue().strip(), "capsule incidents: database is locked")

    def test_open_and_close_give_up_busy_on_a_really_locked_board(self):
        # blind-tester finding: open and close hung past 40 s on a held write lock
        db_path = paths.board_db("other", self.home)
        conn = db.connect(db_path)
        inc_id, _ = incidents.open_incident(conn, board="other", kind="crash", key="boom",
                                            evidence=["event:1"], main_sha="a" * 40)
        conn.close()
        holder = sqlite3.connect(str(db_path), isolation_level=None)
        holder.execute("BEGIN IMMEDIATE")
        self.addCleanup(holder.close)
        for cmd, extra in (("open", dict(kind="crash", root_cause_key="k2", evidence=["e:1"],
                                         task=None, run=None, role=None)),
                           ("close", dict(incident_id=inc_id))):
            args = argparse.Namespace(capsule_home=str(self.home), board="other",
                                      inc_cmd=cmd, **extra)
            err = io.StringIO()
            start = time.monotonic()
            with mock.patch.object(incidents_cli, "ONE_SHOT_BUSY_MS", 300), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                rc = incidents_cli.main(args)
            self.assertLess(time.monotonic() - start, 5, cmd)
            self.assertEqual(rc, 1, cmd)
            self.assertIn("busy", err.getvalue(), cmd)

    def test_busy_wait_env_override_and_quiet_fallback(self):
        for env, want in (({}, 10_000), ({"CAPSULE_INCIDENTS_BUSY_MS": "2500"}, 2500),
                          ({"CAPSULE_INCIDENTS_BUSY_MS": "soon"}, 10_000)):
            with mock.patch.dict("os.environ", env):
                if not env:
                    os.environ.pop("CAPSULE_INCIDENTS_BUSY_MS", None)
                conn = incidents_cli._connect(paths.board_db("other", self.home), True)
                got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
                conn.close()
            self.assertEqual(got, want, env)


class Description(unittest.TestCase):
    """`--description` is stored, shown and never hashed; `--root-cause-key` stays a short slug."""

    setUp = BoardConfig.setUp
    _run = BoardConfig._run

    def _open(self, key, *extra):
        return self._run("open", "--board", "other", "--kind", "crash", "--root-cause-key", key,
                         "--evidence", "event:1", *extra)

    def test_description_is_stored_listed_and_does_not_change_the_fingerprint(self):
        prose = "the gate timed out; fixed by t_ab12cd34 raising the limit"
        first = self._open("gate-timeout", "--description", prose)
        self.assertEqual(first.returncode, 0, first.stderr)
        inc_id = first.stdout.split()[0]
        rec = json.loads(self._run("show", inc_id, "--board", "other").stdout)
        self.assertEqual(rec["description"], prose)
        self.assertIn("the gate timed out", self._run("list", "--board", "other").stdout)
        second = self._open("gate-timeout", "--description", "other words")   # deduped
        self.assertEqual(second.stdout.split(), [inc_id, "deduped"])

    def test_no_description_stays_null_and_a_long_key_is_refused(self):
        ok = self._open("short-slug")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        rec = json.loads(self._run("show", ok.stdout.split()[0], "--board", "other").stdout)
        self.assertIsNone(rec["description"])
        bad = self._open("x " * 60)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("--description", bad.stderr)
        self.assertEqual(self._open("a" * 80).returncode, 0)

    def test_a_description_over_2000_characters_is_refused_not_cut(self):
        bad = self._open("slug", "--description", "x" * 2001)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("2000", bad.stderr)
        self.assertEqual(self._run("list", "--board", "other").stdout.strip(), "")
        self.assertEqual(self._open("slug", "--description", "x" * 2000).returncode, 0)

    def test_an_old_board_without_the_column_reads_clean_and_stays_unmigrated(self):
        from capsule import incident_inputs
        path = paths.board_db("other", self.home)
        conn = db.connect(path)
        inc_id, _ = incidents.open_incident(conn, board="other", kind="crash", key="k",
                                            evidence=["event:1"])
        cols = ", ".join(c for c in db.INCIDENTS_COLUMNS if c != "description")
        old = db._incidents_body().replace(", description TEXT", "")
        conn.execute(f"CREATE TABLE incidents_old ({old})")
        copy = f"INSERT INTO incidents_old ({cols}) SELECT {cols} FROM incidents"  # noqa: S608
        conn.execute(copy)
        conn.execute("DROP " + "TABLE incidents")
        conn.execute("ALTER TABLE incidents_old RENAME TO incidents")
        conn.commit()
        listed = self._run("list", "--board", "other")
        shown = self._run("show", inc_id, "--board", "other")
        self.assertEqual((listed.returncode, shown.returncode), (0, 0), listed.stderr)
        self.assertIn(inc_id, listed.stdout)
        self.assertNotIn("description", json.loads(shown.stdout))
        self.assertIsNone(incident_inputs.record(incidents.get(conn, inc_id))["description"])
        self.assertTrue(db.migrations_pending(conn))      # the read-only verbs did not migrate
        conn.close()

    def test_korin_input_carries_the_description(self):
        from capsule import incident_inputs
        conn = db.connect(paths.board_db("other", self.home))
        inc_id, _ = incidents.open_incident(conn, board="other", kind="crash", key="k",
                                            evidence=["event:1"], description="the prose")
        got = incident_inputs.record(incidents.get(conn, inc_id))
        self.assertEqual(got["description"], "the prose")
        old = incidents.get(conn, inc_id)
        old.pop("description")
        self.assertIsNone(incident_inputs.record(old)["description"])   # an old row
        conn.close()


if __name__ == "__main__":
    unittest.main()
