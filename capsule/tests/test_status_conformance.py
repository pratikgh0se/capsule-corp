# mutation-check: targets-predate-range 6b792dc2 --
#   test-only timing fix (per-test self.now replaces module-level NOW), no production change ::
#   StatusJson.test_json_values_for_the_seeded_tasks
"""Conformance of `capsule status` and `capsule board` to the visibility spec
(docs/superpowers/specs/2026-10-21-visibility-status-board-slack-design.md, sections 2A, 2C, 3, 4):
one seeded board with a task of each class, driven through the real CLI entry points."""
import io
import json
import re
import subprocess
import sys
import tempfile
import time
import unittest
import unittest.mock
from contextlib import redirect_stdout
from pathlib import Path

from capsule import db
from capsule.__main__ import main

ROOT = Path(__file__).resolve().parents[2]
BOARD = "capsule-corp"      # the first of unblocked.DEFAULT_BOARDS: what `capsule board` opens
ID_RE = re.compile(r"t_[0-9a-f]{6,}|inc_|python3 -m|/Users/|/tmp/")

# Spec section 4, written by hand (not derived from the code under test).
TOP_KEYS = {"generated_at", "boards", "running", "needs_you", "blocked", "attention",
            "outside_factory"} | {"redesign", "lanes"}
BOARD_KEYS = {"board", "loop", "auto_landing"}
RUNNING_KEYS = {"board", "number", "name", "role", "role_label", "model", "minutes"} | {
    "role_emoji", "quiet_minutes"}
NEEDS_KEYS = {"board", "number", "name", "ask", "since"}
BLOCKED_KEYS = {"board", "number", "name", "reason"}
ATTENTION_KEYS = {"open", "handled_count"}
OUTSIDE_KEYS = {"seats", "summary"} | {"ages_min"}


class Seeded(unittest.TestCase):
    """capsule-corp with one task per class; its loop is down (no tick lock in a temp home)."""

    def setUp(self):
        # time is taken per test: a module-level stamp goes stale in a long full-suite run
        self.now = int(time.time())
        self.old = self.now - 10 * 3600
        self.home = Path(tempfile.mkdtemp())
        d = self.home / "boards" / BOARD
        d.mkdir(parents=True)
        db.create_board(d / "kanban.db")
        self.conn = db.connect(d / "kanban.db")
        self.addCleanup(self.conn.close)
        self.task("Needs your answer", "blocked", kind="needs_input", short_name="Ruling please")
        self.task("Waiting on another", "blocked", kind="dependency", short_name="Digest layout")
        self.task("Gone quiet for ages", "ready", short_name="Forgotten card")
        run = self.task("Build the thing", "running", short_name="Retry on rate limit")
        cur = self.conn.execute(
            "INSERT INTO task_runs (task_id, profile, status, started_at, model, last_heartbeat_at)"
            " VALUES (?,?,'running',?,?,?)",
            (run, "implementer", self.now - 720, "claude-sonnet-5-5", self.now - 30))
        self.conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (cur.lastrowid, run))
        self.conn.commit()
        queue = self.home / "queue" / "repo"
        queue.mkdir(parents=True)
        (queue / "owners.json").write_text(json.dumps(
            {"seat-a": {"owner": "cos", "heartbeat": self.now - 60}}))

    def task(self, title, status, kind=None, short_name=None):
        tid = db.new_task(self.conn, title=title, assignee="dev-backend", status=status,
                          short_name=short_name, now=self.old)
        self.conn.execute("UPDATE tasks SET block_kind=? WHERE id=?", (kind, tid))
        return tid

    def cli(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            code = main([*argv, "--capsule-home", str(self.home)])
        return code, out.getvalue()


class StatusScreen(Seeded):
    def sections(self):
        _, text = self.cli("status")
        parts = re.split(r"\n(?=[^\s].* (?:RUNNING|NEEDS YOU|BLOCKED|ATTENTION|OUTSIDE FACTORY))",
                         text)
        return {re.search(r"(RUNNING|NEEDS YOU|BLOCKED|ATTENTION|OUTSIDE FACTORY)", p).group(1): p
                for p in parts[1:]}

    def test_each_class_lands_in_its_section(self):
        got = self.sections()
        self.assertEqual(list(got), ["RUNNING", "NEEDS YOU", "BLOCKED", "ATTENTION",
                                     "OUTSIDE FACTORY"])
        self.assertIn("Retry on rate limit", got["RUNNING"])
        self.assertIn("Ruling please", got["NEEDS YOU"])
        self.assertIn("Digest layout", got["BLOCKED"])
        self.assertIn("Forgotten card", got["ATTENTION"])
        self.assertIn("1 CoS keeper", got["OUTSIDE FACTORY"])
        for name in ("Ruling please", "Digest layout", "Forgotten card"):
            self.assertEqual(sum(name in s for s in got.values()), 1, name)

    def test_screen_has_no_raw_ids_and_works_with_the_loop_down(self):
        code, text = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn(f"🔴 {BOARD} (down)", text)
        self.assertIn("the loop is not running", text)
        self.assertIsNone(ID_RE.search(text), text)


class StatusJson(Seeded):
    """Second oracle: the real `python3 -m capsule status --json` process, keys from spec 4."""

    def run_json(self):
        proc = subprocess.run(
            [sys.executable, "-m", "capsule", "status", "--json", "--capsule-home",
             str(self.home)], cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout), proc.stdout

    def test_json_keys_match_spec_section_4(self):
        doc, text = self.run_json()
        self.assertEqual(set(doc) - {"ref"}, TOP_KEYS)
        self.assertEqual(set(doc["boards"][0]), BOARD_KEYS)
        for section, keys in (("running", RUNNING_KEYS), ("needs_you", NEEDS_KEYS),
                              ("blocked", BLOCKED_KEYS)):
            self.assertEqual(len(doc[section]), 1, section)
            self.assertEqual(set(doc[section][0]) - {"ref"}, keys, section)
        self.assertEqual(set(doc["attention"]) - {"ref"}, ATTENTION_KEYS)
        self.assertEqual(set(doc["outside_factory"]) - {"ref"}, OUTSIDE_KEYS)
        self.assertNotIn('"ref"', text)

    def test_json_values_for_the_seeded_tasks(self):
        doc, _ = self.run_json()
        run = doc["running"][0]
        self.assertEqual((run["name"], run["role"], run["model"]),
                         ("Retry on rate limit", "implementer", "Sonnet 5.5"))
        started = (int(time.time()) - (self.now - 720)) // 60
        self.assertIn(run["minutes"], (started - 1, started, started + 1))
        self.assertEqual(doc["needs_you"][0]["name"], "Ruling please")
        self.assertEqual(doc["blocked"][0]["reason"], "waiting on another task")
        self.assertEqual(doc["boards"][0]["loop"], "down")
        self.assertTrue(any("Forgotten card" in a["what"] for a in doc["attention"]["open"]))
        self.assertEqual(doc["outside_factory"]["seats"], 1)


class BoardScreen(Seeded):
    def test_no_argument_opens_the_first_default_board_as_a_list_on_a_non_tty(self):
        code, text = self.cli("board")      # stdout is a StringIO: not a tty, so no curses
        self.assertEqual(code, 0)
        for heading in ("NEEDS YOU (1)", "RUNNING (1)", "BLOCKED (1)"):
            self.assertIn(heading, text)
        self.assertIn("Ruling please", text)
        self.assertIsNone(ID_RE.search(text), text)

    def test_once_on_a_non_tty_matches_no_flag(self):
        self.assertEqual(self.cli("board"), self.cli("board", "--once"))

    def test_unknown_board_exits_two_without_a_traceback(self):
        err = io.StringIO()
        with unittest.mock.patch("sys.stderr", err):
            code, text = self.cli("board", "nope")
        self.assertEqual((code, text), (2, ""))
        self.assertIn("no board named nope", err.getvalue())


if __name__ == "__main__":
    unittest.main()
