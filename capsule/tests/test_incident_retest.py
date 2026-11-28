"""`capsule incidents retest <id> <new_test_id> --reason ...`: re-point a live incident at the
recurrence test its fix really landed under (the old file was size-frozen). Oracle 1: the
incident row and the task_events row read straight from the temp board db. Oracle 2: the
closure gate (`incidents.missing`) handed the id it was asked to prove new. Temp boards only."""
import argparse
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import db, incidents, incidents_cli, paths

OLD = "capsule.tests.test_old.Frozen.test_gone"
NEW = "capsule.tests.test_incident_retest_target.Fresh.test_new_file"
EVENT = "SELECT payload FROM task_events WHERE kind='incident_retest'"


class Retest(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home, True)
        self.path = paths.board_db("b", self.home)
        db.create_board(self.path)
        self.conn = db.connect(self.path)
        self.addCleanup(self.conn.close)
        self.card = db.new_task(self.conn, title="fix", status="done")
        self.inc, _ = incidents.open_incident(
            self.conn, board="b", kind="crash", key="implementer: boom", evidence=["event:1"],
            main_sha="a" * 40, now=1_790_000_000)
        self.conn.execute(
            "UPDATE incidents SET status='fix_pending', fix_card=?, recurrence_test=?,"
            " enforcer='the old enforcer' WHERE id=?", (self.card, OLD, self.inc))

    def run_cli(self, *argv, inc=None, agent=None):
        """`agent`: an environment marker to set (an agent session); None is a person's terminal."""
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
        env.update(agent or {})
        return subprocess.run(
            [sys.executable, "-m", "capsule", "incidents", "retest", inc or self.inc, *argv,
             "--board", "b", "--capsule-home", str(self.home)],
            cwd=paths.REPO, capture_output=True, text=True, timeout=60, env=env)

    def test_an_agent_session_must_pass_author_and_nothing_changes(self):
        for marker in ({"CLAUDECODE": "1"}, {"AI_AGENT": "claude-code_x_agent"}):
            out = self.run_cli(NEW, "--reason", "moved", agent=marker)
            self.assertNotEqual(out.returncode, 0, marker)
            self.assertIn("--author cos", out.stderr)
            self.assertIn("only for the owner typing", out.stderr)
            self.assertEqual((self.rec()["recurrence_test"], self.conn.execute(EVENT).fetchall()),
                             (OLD, []))

    def test_an_agent_session_with_an_explicit_author_records_it(self):
        out = self.run_cli(NEW, "--reason", "moved", "--author", "cos", agent={"CLAUDECODE": "1"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.rec()["recurrence_test"], NEW)

    def rec(self):
        return incidents.get(self.conn, self.inc)

    def test_fix_pending_incident_gets_the_new_id_and_stays_fix_pending(self):
        out = self.run_cli(NEW, "--reason", "old file is size-frozen")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn(OLD, out.stdout)
        self.assertIn(NEW, out.stdout)
        rec = self.rec()
        self.assertEqual((rec["recurrence_test"], rec["status"], rec["enforcer"]),
                         (NEW, "fix_pending", "the old enforcer"))
        self.assertIsNone(rec["closed_at"])

    def test_enforcer_changes_only_when_asked(self):
        out = self.run_cli(NEW, "--reason", "moved", "--enforcer", "capsule/foo.py, new test")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.rec()["enforcer"], "capsule/foo.py, new test")

    def test_open_and_postmortem_incidents_are_accepted(self):
        for status in ("open", "postmortem"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE incidents SET status=?, recurrence_test=? WHERE id=?",
                                  (status, OLD, self.inc))
                self.assertEqual(self.run_cli(NEW, "--reason", "moved").returncode, 0)
                rec = self.rec()
                self.assertEqual((rec["status"], rec["recurrence_test"]), (status, NEW))

    def test_closed_and_superseded_incidents_are_refused_unchanged(self):
        for status in ("closed", "superseded"):
            with self.subTest(status=status):
                self.conn.execute("UPDATE incidents SET status=? WHERE id=?", (status, self.inc))
                out = self.run_cli(NEW, "--reason", "moved")
                self.assertEqual(out.returncode, 1)
                self.assertIn(status, out.stderr)
                self.assertEqual(self.rec()["recurrence_test"], OLD)

    def test_a_bad_id_format_is_refused_unchanged(self):
        for bad in ("rm -rf /", "capsule.tests.test_x", "tests/not_a_test.sh"):
            with self.subTest(bad=bad):
                out = self.run_cli(bad, "--reason", "moved")
                self.assertEqual(out.returncode, 1)
                self.assertIn("recurrence_test", out.stderr)
                self.assertEqual(self.rec()["recurrence_test"], OLD)

    def test_a_trailing_newline_on_the_id_is_refused(self):
        out = self.run_cli(NEW + "\n", "--reason", "moved")
        self.assertEqual(out.returncode, 1)
        self.assertIn("recurrence_test", out.stderr)
        self.assertEqual(self.rec()["recurrence_test"], OLD)

    def test_the_id_the_incident_already_names_is_refused(self):
        out = self.run_cli(OLD, "--reason", "moved")
        self.assertEqual(out.returncode, 1)
        self.assertIn("already", out.stderr)
        self.assertEqual(self.conn.execute(EVENT).fetchall(), [])

    def test_a_blank_reason_is_refused_and_the_message_names_the_reason(self):
        for blank in ("", "   ", "\t\n"):
            with self.subTest(reason=blank):
                out = self.run_cli(NEW, "--reason", blank)
                self.assertEqual(out.returncode, 1)
                self.assertIn("--reason", out.stderr)
                self.assertNotIn("recurrence_test", out.stderr)
                self.assertEqual(self.rec()["recurrence_test"], OLD)

    def test_a_blank_enforcer_is_refused_unchanged(self):
        for blank in ("", "  "):
            with self.subTest(enforcer=blank):
                out = self.run_cli(NEW, "--reason", "moved", "--enforcer", blank)
                self.assertEqual(out.returncode, 1)
                self.assertIn("--enforcer", out.stderr)
                self.assertEqual((self.rec()["recurrence_test"], self.rec()["enforcer"]),
                                 (OLD, "the old enforcer"))

    def test_an_unknown_incident_is_refused(self):
        out = self.run_cli(NEW, "--reason", "moved", inc="inc_00000000")
        self.assertEqual(out.returncode, 1)
        self.assertIn("inc_00000000", out.stderr)

    def test_the_event_records_old_new_reason_and_author(self):
        out = self.run_cli(NEW, "--reason", "old file is size-frozen", "--author", "cos")
        self.assertEqual(out.returncode, 0, out.stderr)
        rows = self.conn.execute(EVENT + " AND task_id=?", (self.card,)).fetchall()
        self.assertEqual(len(rows), 1)
        payload = json.loads(rows[0]["payload"])
        self.assertEqual({k: payload[k] for k in ("incident", "old", "new", "reason", "by")},
                         {"incident": self.inc, "old": OLD, "new": NEW,
                          "reason": "old file is size-frozen", "by": "cos"})

    def test_the_author_defaults_to_cos_when_not_a_terminal(self):
        out = self.run_cli(NEW, "--reason", "moved")      # captured output: not a terminal
        self.assertEqual(out.returncode, 0)
        self.assertIn("recorded as cos", out.stderr)
        self.assertEqual(json.loads(self.conn.execute(EVENT).fetchone()["payload"])["by"], "cos")

    def test_the_author_defaults_to_owner_at_a_terminal(self):
        from capsule import human
        from capsule.__main__ import main
        env = {k: v for k, v in os.environ.items() if k not in ("CLAUDECODE", "AI_AGENT")}
        argv = ["incidents", "retest", self.inc, NEW, "--reason", "moved", "--board", "b",
                "--capsule-home", str(self.home)]
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(human, "_ttys", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(argv), 0)
        self.assertEqual(json.loads(self.conn.execute(EVENT).fetchone()["payload"])["by"], "owner")

    def test_the_evidence_line_is_written_even_when_the_card_event_is(self):
        out = self.run_cli(NEW, "--reason", "old file is size-frozen", "--author", "cos")
        self.assertEqual(out.returncode, 0, out.stderr)
        lines = [e for e in json.loads(self.rec()["evidence"]) if OLD in e and NEW in e]
        self.assertEqual(len(lines), 1)
        self.assertIn("cos", lines[0])
        self.assertIn("old file is size-frozen", lines[0])
        self.assertEqual(len(self.conn.execute(EVENT).fetchall()), 1)

    def test_with_no_local_card_the_change_is_noted_in_the_evidence(self):
        self.conn.execute("UPDATE incidents SET fix_card='other:t_00000000', task_id=NULL"
                          " WHERE id=?", (self.inc,))
        self.assertEqual(self.run_cli(NEW, "--reason", "moved").returncode, 0)
        self.assertTrue(any(OLD in e and NEW in e for e in json.loads(self.rec()["evidence"])))

    def test_a_close_that_lands_after_the_read_is_not_overwritten(self):
        stale = self.rec()      # what a read taken just before the close would have returned
        self.conn.execute("UPDATE incidents SET status='closed', closed_at=5 WHERE id=?",
                          (self.inc,))
        args = argparse.Namespace(
            capsule_home=str(self.home), board="b", inc_cmd="retest", incident_id=self.inc,
            new_test_id=NEW, reason="moved", enforcer=None, author="owner")
        with mock.patch.object(incidents, "get", return_value=stale), \
                contextlib.redirect_stderr(io.StringIO()):
            rc = incidents_cli.main(args)
        self.assertNotEqual(rc, 0)
        self.assertEqual((self.rec()["recurrence_test"], self.rec()["status"]), (OLD, "closed"))
        self.assertIsNone(self.conn.execute(EVENT).fetchone())

    def test_a_watch_closure_is_refused_because_its_gate_skips_the_novelty_check(self):
        self.conn.execute("UPDATE incidents SET closure='watch' WHERE id=?", (self.inc,))
        out = self.run_cli(NEW, "--reason", "moved")
        self.assertEqual(out.returncode, 1)
        self.assertIn("watch", out.stderr)
        self.assertEqual(self.rec()["recurrence_test"], OLD)

    def test_a_retest_that_lands_after_the_gate_passed_stops_the_close(self):
        def gate_passes_on_the_old_id_then_a_retest_lands(conn, incident, **_):
            self.assertEqual(incident["recurrence_test"], OLD)
            self.assertEqual(self.run_cli(NEW, "--reason", "moved").returncode, 0)
            return []
        with mock.patch.object(incidents, "missing", gate_passes_on_the_old_id_then_a_retest_lands):
            with self.assertRaises(incidents.CloseRefused) as cm:
                incidents.close(self.conn, self.inc, vault=self.home / "vault")
        self.assertIn("recurrence test changed during the close", "; ".join(cm.exception.missing))
        rec = self.rec()
        self.assertEqual((rec["status"], rec["recurrence_test"], rec["closed_at"]),
                         ("fix_pending", NEW, None))

    def test_the_close_gate_afterwards_checks_the_new_id_and_nothing_is_closed(self):
        self.assertEqual(self.run_cli(NEW, "--reason", "moved").returncode, 0)
        asked = []

        def check_new(test_id, checkout, open_sha):
            asked.append((test_id, open_sha))
            return True, "new"
        incidents.missing(self.conn, self.rec(), vault=self.home / "vault", check_new=check_new)
        # the checkout (the Capsule repo) lacks the fixture's fake main_sha: the base is that repo's
        # last main commit before the incident opened, or None when main has no commit that old
        # (a short public history)
        derived = subprocess.run(
            ["git", "-C", str(paths.REPO), "rev-list", "-1", "--first-parent",
             f"--before={int(self.rec()['opened_at'])}", "main"],
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertEqual(asked, [(NEW, derived or None)])
        self.assertEqual(self.rec()["status"], "fix_pending")


if __name__ == "__main__":
    unittest.main()
