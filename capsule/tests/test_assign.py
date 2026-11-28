# mutation-check: targets-predate-range b957739c9 -- renames only, no behaviour pinned ::
#   AssignVerb.test_event_and_outbox_line
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from capsule import assign, db, intent, paths
from capsule import config as capsule_config
from capsule.__main__ import main

ROUTE = {"provider": "p", "account": "a", "model": "m", "turn_budget": 1}
# A full, `config.validate`-clean config (row 492 fix round 1: `assign` fails closed through
# `config.load`, so every test needs one - not just the `assignees`/`routing` half).
VALID_CONFIG = {
    "board": "sample-app",
    "epics": ["epic/x"],
    "caps": {"max_running": 1, "claim_ttl": 60, "failure_limit": 3, "checkpoint_chain": 1, "tick_seconds": 30},
    "contract_commands": ["true"],
    "routing": {"implementer": ROUTE, "reviewer": ROUTE, "validator": ROUTE, "guide": ROUTE},
    "assignees": {"impl-1": "implementer"},
}


def setup_board(status="ready", assignee=None, config=None):
    home = Path(tempfile.mkdtemp())
    board = "sample-app"
    board_db = paths.board_db(board, home)
    db.create_board(board_db)
    conn = db.connect(board_db)
    tid = db.new_task(conn, title="card", status=status, assignee=assignee, now=10)
    conn.close()
    (home / "config.json").write_text(json.dumps(config if config is not None else VALID_CONFIG))
    return home, board, tid


def row(home, board, tid):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute("SELECT status, assignee FROM tasks WHERE id=?", (tid,)).fetchone()
    finally:
        conn.close()


def events(home, board, tid, kind):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute(
            "SELECT payload FROM task_events WHERE task_id=? AND kind=?", (tid, kind)).fetchall()
    finally:
        conn.close()


def outbox_rows(home, board, tid):
    conn = db.connect(paths.board_db(board, home))
    try:
        return conn.execute("SELECT body FROM outbox WHERE thread_key=?", (tid,)).fetchall()
    finally:
        conn.close()


class AssignVerb(unittest.TestCase):
    def test_ready_card_gets_assigned(self):
        home, board, tid = setup_board(status="ready")
        backups = Path(tempfile.mkdtemp())
        result = assign.assign(home, board, tid, "impl-1", now=100, backup_dir=backups)
        self.assertEqual(row(home, board, tid)["assignee"], "impl-1")
        self.assertIn("event_id", result)

    def test_blocked_card_gets_assigned(self):
        home, board, tid = setup_board(status="blocked")
        result = assign.assign(home, board, tid, "impl-1", now=100,
                                backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["assignee"], "impl-1")
        self.assertIn("event_id", result)

    def test_triage_card_gets_assigned(self):
        home, board, tid = setup_board(status="triage")
        result = assign.assign(home, board, tid, "impl-1", now=100,
                                backup_dir=Path(tempfile.mkdtemp()))
        self.assertEqual(row(home, board, tid)["assignee"], "impl-1")
        self.assertIn("event_id", result)

    def test_running_card_refused(self):
        home, board, tid = setup_board(status="running")
        backups = Path(tempfile.mkdtemp())
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=backups)
        self.assertIn("running", str(ctx.exception))
        self.assertIsNone(row(home, board, tid)["assignee"])
        self.assertEqual(list(backups.iterdir()), [])   # refused before any backup was taken

    def test_todo_card_refused(self):
        home, board, tid = setup_board(status="todo")
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("todo", str(ctx.exception))

    def test_done_card_refused(self):
        home, board, tid = setup_board(status="done")
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("done", str(ctx.exception))

    def test_missing_task_refused(self):
        home, board, _ = setup_board()
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, "t_nosuchcard", "impl-1", now=100,
                          backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("no such task", str(ctx.exception))

    def test_mistyped_board_refused_before_touching_the_filesystem(self):
        """Row 492 fix round 1: a wrong board name must not leave a stray, empty kanban.db
        behind (the old `db.connect` straight through would create one)."""
        home, _, _ = setup_board()
        (home / "boards" / "wrong-name").mkdir(parents=True)
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, "wrong-name", "t_x", "impl-1", now=100,
                          backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("no such board", str(ctx.exception))
        self.assertFalse((home / "boards" / "wrong-name" / "kanban.db").exists())

    def test_unknown_assignee_key_refused(self):
        home, board, tid = setup_board(status="ready")
        backups = Path(tempfile.mkdtemp())
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "nobody", now=100, backup_dir=backups)
        self.assertIn("nobody", str(ctx.exception))
        self.assertIsNone(row(home, board, tid)["assignee"])
        self.assertEqual(list(backups.iterdir()), [])   # refused before any backup was taken

    def test_assignee_mapping_to_an_unrouted_role_refused(self):
        # `impl-1` maps to a name that is not a key of `routing` at all - `config.validate`
        # only requires the four ROUTING_ROLES entries to exist under `routing`, it never
        # constrains what `assignees` maps *to*.
        cfg = dict(VALID_CONFIG, assignees={"impl-1": "not-a-routed-role"})
        home, board, tid = setup_board(status="ready", config=cfg)
        with self.assertRaises(assign.AssignRefused):
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))

    def test_empty_assignee_refused(self):
        home, board, tid = setup_board()
        with self.assertRaises(assign.AssignRefused):
            assign.assign(home, board, tid, "", now=100, backup_dir=Path(tempfile.mkdtemp()))

    def test_missing_config_refused(self):
        home = Path(tempfile.mkdtemp())
        board = "sample-app"
        board_db = paths.board_db(board, home)
        db.create_board(board_db)
        conn = db.connect(board_db)
        tid = db.new_task(conn, title="card", status="ready")
        conn.close()
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("config invalid", str(ctx.exception))

    def test_invalid_json_config_refused(self):
        home, board, tid = setup_board()
        (home / "config.json").write_text("{not json")
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("config invalid", str(ctx.exception))

    def test_config_failing_validation_refused(self):
        """A `config.json` that is valid JSON but fails `config.validate` (missing `caps`, here)
        must refuse cleanly too, not raise a raw `KeyError`/`ValueError`."""
        home, board, tid = setup_board(config={"board": "sample-app"})
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("config invalid", str(ctx.exception))

    def test_non_string_assignees_value_refused_by_config_validate(self):
        """`{"assignees": {"impl-1": ["list"]}}` fails `config.validate`, so `assign` refuses
        with `config invalid` and the tick's own `dispatch.ready` never sees it."""
        cfg = dict(VALID_CONFIG, assignees={"impl-1": ["not", "a", "role", "string"]})
        with self.assertRaises(ValueError):
            capsule_config.validate(cfg)
        home, board, tid = setup_board(config=cfg)
        with self.assertRaises(assign.AssignRefused) as ctx:
            assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIn("config invalid", str(ctx.exception))

    def test_backup_created_before_the_ready_card_is_mutated(self):
        home, board, tid = setup_board(status="ready")
        backups = Path(tempfile.mkdtemp())
        result = assign.assign(home, board, tid, "impl-1", now=100, backup_dir=backups)
        files = list(backups.iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith(f"{board}-"))
        self.assertEqual(str(files[0]), result["backup"])
        old_conn = db.connect(files[0])
        try:
            pre_assignee = old_conn.execute("SELECT assignee FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        finally:
            old_conn.close()
        self.assertIsNone(pre_assignee)

    def test_event_and_outbox_line(self):
        home, board, tid = setup_board(status="ready")
        assign.assign(home, board, tid, "impl-1", who="owner", now=100,
                      backup_dir=Path(tempfile.mkdtemp()))
        (ev,) = events(home, board, tid, "assigned")
        payload = json.loads(ev["payload"])
        self.assertEqual(payload["by"], "owner")
        self.assertEqual(payload["assignee"], "impl-1")
        self.assertIsNone(payload["old_assignee"])
        rows = outbox_rows(home, board, tid)
        self.assertEqual(len(rows), 1)
        self.assertIn(tid, rows[0]["body"])
        self.assertIn("assigned", rows[0]["body"])

    def test_old_assignee_is_recorded_on_reassignment(self):
        cfg = dict(VALID_CONFIG, assignees={"impl-1": "implementer", "stale-assignee": "implementer"})
        home, board, tid = setup_board(status="ready", assignee="stale-assignee", config=cfg)
        assign.assign(home, board, tid, "impl-1", now=100, backup_dir=Path(tempfile.mkdtemp()))
        (ev,) = events(home, board, tid, "assigned")
        self.assertEqual(json.loads(ev["payload"])["old_assignee"], "stale-assignee")


class AssignCLI(unittest.TestCase):
    """`python3 -m capsule human <board> <task> assign <assignee>` (capsule/__main__.py) -
    argument parsing, the refusal exit code, and the success path, under a temp home/backup dir."""

    def setUp(self):
        self.home, self.board, self.tid = setup_board(status="ready")
        self.backups = Path(tempfile.mkdtemp())

    def cli(self, assignee):
        out, err = io.StringIO(), io.StringIO()
        argv = ["human", self.board, self.tid, "assign", assignee, "--author", "cos",
                "--capsule-home", str(self.home)]
        with mock.patch.dict("os.environ", {"CAPSULE_BACKUP_DIR": str(self.backups)}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = main(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_success_exits_0_and_assigns(self):
        rc, out, err = self.cli("impl-1")
        self.assertEqual((rc, err), (0, ""))
        self.assertEqual(row(self.home, self.board, self.tid)["assignee"], "impl-1")
        result = json.loads(out)
        self.assertEqual(Path(result["backup"]).parent, self.backups)
        self.assertTrue(Path(result["backup"]).is_file())
        (ev,) = events(self.home, self.board, self.tid, "assigned")
        self.assertEqual(json.loads(ev["payload"])["by"], "cos")

    def test_size_and_workload_record_the_cli_author(self):
        for kind, text in (("size", "S"), ("workload", "upkeep")):
            argv = ["human", self.board, self.tid, kind, text, "--author", "cos",
                    "--capsule-home", str(self.home)]
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0, kind)
        conn = db.connect(paths.board_db(self.board, self.home))
        try:
            who = [json.loads(r["payload"])["by"] for r in conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='card_fields_set'",
                (self.tid,))]
        finally:
            conn.close()
        self.assertEqual(who, ["cos", "cos"])

    def test_unroutable_assignee_exits_1_with_no_write(self):
        rc, out, err = self.cli("nobody")
        self.assertEqual((rc, out), (1, ""))
        self.assertTrue(err.startswith("capsule human assign: "), err)
        self.assertIn("nobody", err)
        self.assertIsNone(row(self.home, self.board, self.tid)["assignee"])
        self.assertEqual(events(self.home, self.board, self.tid, "assigned"), [])
        self.assertEqual(list(self.backups.iterdir()), [])   # refused before the backup


class AssignIntent(unittest.TestCase):
    """Z1/Z2 (row 492): `contracts/intent/interventions.jsonl`'s own `assign` example, run
    through the real `assign.assign` - the same live-second-oracle shape
    `InterventionsIntent` (test_blocks.py) already holds `epic`/`park` to."""

    def test_assign_refuses_an_unroutable_assignee(self):
        examples = [e for e in intent.load("interventions") if e["given"]["document"]["verb"] == "assign"]
        self.assertEqual(len(examples), 1)
        ex = examples[0]
        doc = ex["given"]["document"]
        cfg = dict(VALID_CONFIG, **doc["config"])
        home, board, tid = setup_board(status=doc["status"], config=cfg)
        with self.assertRaises(assign.AssignRefused):
            assign.assign(home, board, tid, doc["assignee"], now=100, backup_dir=Path(tempfile.mkdtemp()))
        self.assertIsNone(row(home, board, tid)["assignee"])
        self.assertEqual(events(home, board, tid, "assigned"), [])
        self.assertEqual(ex["expect"], {"applied": False})


if __name__ == "__main__":
    unittest.main()
