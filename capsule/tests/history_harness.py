"""A temp home, vault and board for the close-history tests (row 638 b), shared by
test_incident_history.py and test_incident_history_rows.py. No tests of its own. Every incident
time is relative to CUTOFF (never to the clock), so the tests do not rot as the date moves."""
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from capsule import db, incident_history, incidents, paths
from capsule.incident_history import CUTOFF

DAY = 86400
NOTE = "Decisions/Capsule Corp - System design decisions 2026-09-06.md"


class Harness(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp())
        self.vault = Path(tempfile.mkdtemp())
        cfg = json.loads((paths.REPO / "capsule" / "config.example.json").read_text())
        cfg.update(board="b", korin={"vault": str(self.vault)})
        paths.config_path(self.home, "b").parent.mkdir(parents=True)
        paths.config_path(self.home, "b").write_text(json.dumps(cfg))
        self.db_path = paths.board_db("b", self.home)
        db.create_board(self.db_path)
        self.conn = db.connect(self.db_path)
        self.card = self.make_card("done")
        self.write_note("| 638 | 2026-10-30 | close old incidents as history |\n")

    def tearDown(self):
        self.conn.close()

    def make_card(self, status):
        tid = db.new_task(self.conn, title="a card", assignee="dev", branch="epic/x")
        self.conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
        return tid

    def write_note(self, text):
        (self.vault / NOTE).parent.mkdir(parents=True, exist_ok=True)
        (self.vault / NOTE).write_text(text)

    def inc(self, key="boom", kind="crash", age=60, task=None, opened=None):
        """An incident opened `age` days before the cutoff (or at `opened`)."""
        at = CUTOFF - age * DAY if opened is None else opened
        return incidents.open_incident(self.conn, board="b", kind=kind, key=key, task_id=task,
                                       now=at)[0]

    def write_list(self, entries):
        listing = Path(tempfile.mkdtemp()) / "list.jsonl"
        listing.write_text("\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries))
        return listing

    def run_cli(self, entries, *flags, sha="auto"):
        listing = self.write_list(entries)
        if "--apply" in flags and sha == "auto":
            sha = hashlib.sha256(listing.read_bytes()).hexdigest()
        extra = ["--list-sha", sha] if "--apply" in flags and sha not in (None, "auto") else []
        self.conn.close()      # a plain close checkpoints the WAL: the hashes below are stable
        res = subprocess.run(
            [sys.executable, "-m", "capsule", "incidents", "close-history", "--board", "b",
             "--capsule-home", str(self.home), "--list", str(listing), *flags, *extra],
            cwd=paths.REPO, capture_output=True, text=True, timeout=60)
        self.conn = db.connect(self.db_path)
        return res

    def run_direct(self, entries, *, apply=False, now=CUTOFF):
        listing = self.write_list(entries)
        sha = hashlib.sha256(listing.read_bytes()).hexdigest() if apply else None
        return incident_history.run(self.conn, listing, vault=self.vault, decisions_note=NOTE,
                                    apply=apply, list_sha=sha, now=now)[0]

    def row(self, inc_id):
        return incidents.get(self.conn, inc_id)

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def log_size(self):
        log = self.home / "outbox.log"
        return log.stat().st_size if log.exists() else 0

    @staticmethod
    def entry(inc_id, verdict="resolved", evidence="fixed on main in t_x"):
        return {"id": inc_id, "verdict": verdict, "evidence": evidence}

    def assert_refused(self, entries, reason, *flags):
        res = self.run_cli(entries, *flags)
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn("refused: ", res.stdout)
        self.assertIn(reason, res.stdout)

    def assert_run_refused(self, res, reason):
        self.assertEqual(res.returncode, 1, res.stderr)
        self.assertIn(reason, res.stderr)
